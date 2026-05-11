"""JSON-line IPC protocol between the Python orchestrator and the Swift capture daemon.

This module is the single source of truth for the wire schema described in
`context/spec/001-mixed-mic-system-audio-capture/technical-considerations.md` §2.7.
The Swift `Codable` structs in `swift-capture/Sources/RecordCapture/Protocol.swift`
hand-mirror these models.

Field names are kept snake_case so they match the wire format directly without
needing pydantic field aliases.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class AudioFormat(BaseModel):
    """PCM format descriptor carried in the `start` command."""

    model_config = ConfigDict(extra="forbid")

    sample_rate: int = Field(..., description="Sample rate in Hz, e.g. 16000.")
    bit_depth: int = Field(..., description="Bits per sample, e.g. 16.")
    channels: int = Field(..., description="Channel count, e.g. 1 for mono.")


# ---------------------------------------------------------------------------
# Commands (orchestrator -> daemon, on daemon stdin)
# ---------------------------------------------------------------------------


class VideoConfig(BaseModel):
    """Video-capture parameters carried in the `start` command.

    Optional in the wire schema: when ``video_output_path`` / ``video`` are
    omitted, the Swift binary skips video capture entirely. This keeps
    audio-only callers (and audio-focused tests) backwards-compatible.
    """

    model_config = ConfigDict(extra="forbid")

    fps: int = Field(..., description="Target capture frame rate, e.g. 30.")
    show_cursor: bool = Field(..., description="Whether to render the cursor in the MP4.")


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: Literal["start"] = "start"
    output_path: str = Field(..., description="Absolute path of the WAV file to write.")
    format: AudioFormat
    video_output_path: str | None = Field(
        default=None,
        description=(
            "Absolute path of the MP4 file to write. When None (or omitted on "
            "the wire), the binary skips video capture entirely."
        ),
    )
    video: VideoConfig | None = Field(
        default=None,
        description="Video capture parameters; required iff video_output_path is set.",
    )


class StopCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: Literal["stop"] = "stop"


class ShutdownCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: Literal["shutdown"] = "shutdown"


Command = Annotated[
    Union[StartCommand, StopCommand, ShutdownCommand],
    Field(discriminator="cmd"),
]

_command_adapter: TypeAdapter[Command] = TypeAdapter(Command)


# ---------------------------------------------------------------------------
# Events (daemon -> orchestrator, on daemon stdout)
# ---------------------------------------------------------------------------


# Closed enums for event payloads.
SourceName = Literal["mic", "system_audio"]
PermissionKind = Literal["microphone", "screen_recording"]
VideoReconfigReason = Literal["primary_changed", "resolution_changed", "display_removed"]
SystemEventReason = Literal["system_sleep", "display_sleep", "screen_locked"]


class ReadyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["ready"] = "ready"


class PermissionRequiredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["permission_required"] = "permission_required"
    kind: PermissionKind


class PermissionDeniedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["permission_denied"] = "permission_denied"
    kind: PermissionKind


class StartedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["started"] = "started"
    start_time: str = Field(
        ..., description="ISO-8601 UTC timestamp like '2026-05-10T14:32:08Z'."
    )


class SourceAttachedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["source_attached"] = "source_attached"
    source: SourceName


class SourceLostEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["source_lost"] = "source_lost"
    source: SourceName
    at_offset_seconds: float
    reason: str


class StoppedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["stopped"] = "stopped"
    duration_seconds: float
    output_path: str


class ErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["error"] = "error"
    message: str


class VideoStartedEvent(BaseModel):
    """Emitted once MP4Writer has accepted its first frame."""

    model_config = ConfigDict(extra="forbid")

    event: Literal["video_started"] = "video_started"
    display_id: int
    width_px: int
    height_px: int
    fps: int


class VideoLostEvent(BaseModel):
    """Emitted when the video SCStream errors out or is otherwise terminated.

    ``reason`` is free-form (e.g. ``sc_stream_error``, ``permission_denied``,
    ``writer_failure``). The audio capture is independent and is unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    event: Literal["video_lost"] = "video_lost"
    at_offset_seconds: float
    reason: str
    message: str | None = None


class VideoFileEvent(BaseModel):
    """Emitted after the MP4 writer's ``finishWriting`` completes successfully."""

    model_config = ConfigDict(extra="forbid")

    event: Literal["video_file"] = "video_file"
    path: str
    duration_seconds: float


class DisplayReconfiguredEvent(BaseModel):
    """Emitted on a primary-display change (mode switch, hotplug, etc.)."""

    model_config = ConfigDict(extra="forbid")

    event: Literal["display_reconfigured"] = "display_reconfigured"
    reason: VideoReconfigReason
    new_display_id: int
    new_width_px: int
    new_height_px: int


class CaptureEndedBySystemEventEvent(BaseModel):
    """Emitted when system sleep / display sleep / screen lock ends capture.

    The binary performs the same internal stop path as the ``stop`` command,
    then emits this event immediately before the regular ``stopped`` event.
    """

    model_config = ConfigDict(extra="forbid")

    event: Literal["capture_ended_by_system_event"] = "capture_ended_by_system_event"
    reason: SystemEventReason
    at_offset_seconds: float


Event = Annotated[
    Union[
        ReadyEvent,
        PermissionRequiredEvent,
        PermissionDeniedEvent,
        StartedEvent,
        SourceAttachedEvent,
        SourceLostEvent,
        StoppedEvent,
        ErrorEvent,
        VideoStartedEvent,
        VideoLostEvent,
        VideoFileEvent,
        DisplayReconfiguredEvent,
        CaptureEndedBySystemEventEvent,
    ],
    Field(discriminator="event"),
]

_event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def serialize_command(cmd: Command) -> str:
    """Serialize a Command to a single-line JSON string.

    No trailing newline is appended — the caller is responsible for writing the
    framing newline (the protocol is JSON-lines).

    ``exclude_none=True`` keeps the wire payload minimal: when ``StartCommand``
    has no ``video_output_path`` / ``video`` set, those keys are omitted
    entirely. The Swift side uses ``decodeIfPresent`` so omission is the
    correct way to signal "audio only". None of the existing required fields
    have ``None`` defaults, so this flag is a no-op for them.
    """
    return _command_adapter.dump_json(cmd, exclude_none=True).decode("utf-8")


def parse_command(line: str) -> Command:
    """Parse a single JSON-line into the matching Command model.

    Raises ``pydantic.ValidationError`` (a subclass of ``ValueError``) on
    malformed input or unknown discriminator.
    """
    return _command_adapter.validate_json(line)


def serialize_event(event: Event) -> str:
    """Serialize an Event to a single-line JSON string (no trailing newline)."""
    return _event_adapter.dump_json(event).decode("utf-8")


def parse_event(line: str) -> Event:
    """Parse a single JSON-line into the matching Event model.

    Raises ``pydantic.ValidationError`` (a subclass of ``ValueError``) on
    malformed input or unknown discriminator.
    """
    return _event_adapter.validate_json(line)


__all__ = [
    "AudioFormat",
    "VideoConfig",
    "StartCommand",
    "StopCommand",
    "ShutdownCommand",
    "Command",
    "ReadyEvent",
    "PermissionRequiredEvent",
    "PermissionDeniedEvent",
    "StartedEvent",
    "SourceAttachedEvent",
    "SourceLostEvent",
    "StoppedEvent",
    "ErrorEvent",
    "VideoStartedEvent",
    "VideoLostEvent",
    "VideoFileEvent",
    "DisplayReconfiguredEvent",
    "CaptureEndedBySystemEventEvent",
    "Event",
    "SourceName",
    "PermissionKind",
    "VideoReconfigReason",
    "SystemEventReason",
    "serialize_command",
    "parse_command",
    "serialize_event",
    "parse_event",
]
