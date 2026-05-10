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


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: Literal["start"] = "start"
    output_path: str = Field(..., description="Absolute path of the WAV file to write.")
    format: AudioFormat


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
    """
    return _command_adapter.dump_json(cmd).decode("utf-8")


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
    "Event",
    "SourceName",
    "PermissionKind",
    "serialize_command",
    "parse_command",
    "serialize_event",
    "parse_event",
]
