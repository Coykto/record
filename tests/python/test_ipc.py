"""IPC wire-format tests.

Every Command and Event variant has a canonical JSON-line fixture under
``swift-capture/Tests/RecordCaptureTests/Fixtures/``. The Swift test suite
loads the same files, so any byte-level drift here will also break Swift —
that's the point.

Round-trip equality is asserted at the JSON-object level (``json.loads(...) ==
json.loads(...)``) rather than byte-for-byte: object key order is not
semantically significant in JSON and the Swift fixtures occasionally order
fields differently from the pydantic model's declaration order (e.g.
``start_with_video.json`` puts ``video_output_path`` before ``format``). The
parse + re-serialize step still guarantees we accept every field on the wire
and emit no extras.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from record.ipc import (
    AudioFileEvent,
    AudioFormat,
    CaptureEndedBySystemEventEvent,
    DisplayReconfiguredEvent,
    ErrorEvent,
    HotkeyPressedEvent,
    HotkeyRegisteredEvent,
    HotkeyUnregisteredEvent,
    PermissionDeniedEvent,
    PermissionRequiredEvent,
    ReadyEvent,
    RegisterHotkeyCommand,
    ShutdownCommand,
    SourceAttachedEvent,
    SourceLostEvent,
    StartCommand,
    StartedEvent,
    StopCommand,
    StoppedEvent,
    UnregisterHotkeyCommand,
    VideoConfig,
    VideoFileEvent,
    VideoLostEvent,
    VideoStartedEvent,
    parse_command,
    parse_event,
    serialize_command,
    serialize_event,
)

from .conftest import FIXTURES_DIR


# ---------------------------------------------------------------------------
# Fixture-driven round-trip tests
# ---------------------------------------------------------------------------


COMMAND_FIXTURES: list[tuple[str, type]] = [
    ("start.json", StartCommand),
    ("start_with_video.json", StartCommand),
    ("stop.json", StopCommand),
    ("shutdown.json", ShutdownCommand),
    ("register_hotkey.json", RegisterHotkeyCommand),
    ("unregister_hotkey.json", UnregisterHotkeyCommand),
]

EVENT_FIXTURES: list[tuple[str, type]] = [
    ("ready.json", ReadyEvent),
    ("permission_required_microphone.json", PermissionRequiredEvent),
    ("permission_required_screen_recording.json", PermissionRequiredEvent),
    ("permission_denied_microphone.json", PermissionDeniedEvent),
    ("permission_denied_screen_recording.json", PermissionDeniedEvent),
    ("started.json", StartedEvent),
    ("source_attached_mic.json", SourceAttachedEvent),
    ("source_attached_system_audio.json", SourceAttachedEvent),
    ("source_lost_mic.json", SourceLostEvent),
    ("source_lost_system_audio.json", SourceLostEvent),
    ("stopped.json", StoppedEvent),
    ("error.json", ErrorEvent),
    ("video_started.json", VideoStartedEvent),
    ("video_lost.json", VideoLostEvent),
    ("video_file.json", VideoFileEvent),
    ("audio_file_captured_normally.json", AudioFileEvent),
    ("audio_file_silent_throughout.json", AudioFileEvent),
    ("audio_file_truncated_at_offset.json", AudioFileEvent),
    ("display_reconfigured.json", DisplayReconfiguredEvent),
    ("capture_ended_by_system_event.json", CaptureEndedBySystemEventEvent),
    ("hotkey_registered.json", HotkeyRegisteredEvent),
    ("hotkey_pressed.json", HotkeyPressedEvent),
    ("hotkey_unregistered.json", HotkeyUnregisteredEvent),
]


def _read_fixture(subdir: str, name: str) -> str:
    path: Path = FIXTURES_DIR / subdir / name
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("filename,model_cls", COMMAND_FIXTURES, ids=[f[0] for f in COMMAND_FIXTURES])
def test_command_fixture_round_trip(filename: str, model_cls: type) -> None:
    line = _read_fixture("commands", filename)
    parsed = parse_command(line)
    assert isinstance(parsed, model_cls)
    # Semantic round-trip: re-serialize the parsed model and compare the JSON
    # object content (not byte order). See the module docstring for why we
    # don't pin field declaration order across the Swift/Python boundary.
    assert json.loads(serialize_command(parsed)) == json.loads(line)


@pytest.mark.parametrize("filename,model_cls", EVENT_FIXTURES, ids=[f[0] for f in EVENT_FIXTURES])
def test_event_fixture_round_trip(filename: str, model_cls: type) -> None:
    line = _read_fixture("events", filename)
    parsed = parse_event(line)
    assert isinstance(parsed, model_cls)
    assert json.loads(serialize_event(parsed)) == json.loads(line)


# ---------------------------------------------------------------------------
# Object-level round-trip (build via constructor, not fixture)
# ---------------------------------------------------------------------------


def test_start_command_object_round_trip() -> None:
    """Build the model via constructor → serialize → parse → assert equality."""
    cmd = StartCommand(
        output_path="/abs/path/to/2026-05-10T14-32-08",
        format=AudioFormat(sample_rate=16000, bit_depth=16, channels=1),
    )
    line = serialize_command(cmd)
    parsed = parse_command(line)
    assert parsed == cmd


def test_start_command_with_video_object_round_trip() -> None:
    """``StartCommand`` with both video fields populated round-trips end-to-end."""
    cmd = StartCommand(
        output_path="/abs/path/to/2026-05-10T14-32-08",
        format=AudioFormat(sample_rate=16000, bit_depth=16, channels=1),
        video_output_path="/abs/path/to/2026-05-10T14-32-08.mp4",
        video=VideoConfig(fps=30, show_cursor=True),
    )
    line = serialize_command(cmd)
    parsed = parse_command(line)
    assert parsed == cmd
    # Bonus: serializer omits video keys when they're None (audio-only run).
    audio_only = StartCommand(
        output_path="/abs/x",
        format=AudioFormat(sample_rate=16000, bit_depth=16, channels=1),
    )
    audio_only_line = serialize_command(audio_only)
    assert "video_output_path" not in audio_only_line
    assert '"video"' not in audio_only_line


def test_source_lost_event_object_round_trip() -> None:
    evt = SourceLostEvent(
        source="mic", at_offset_seconds=134.2, reason="input device disconnected"
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_video_started_event_object_round_trip() -> None:
    evt = VideoStartedEvent(display_id=1, width_px=2560, height_px=1440, fps=30)
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_video_lost_event_object_round_trip_with_message() -> None:
    evt = VideoLostEvent(
        at_offset_seconds=134.2,
        reason="sc_stream_error",
        message="stream stopped: -16665",
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_video_lost_event_message_is_optional_in_python_model() -> None:
    """``message`` defaults to ``None`` on the Python side.

    Python only ever parses ``video_lost`` (the supervisor never emits it), so
    a permissive optional on the Python side is safe. The Swift side requires
    the field to be present on the wire — `message` is non-optional in
    ``Protocol.swift`` — so the canonical fixture always carries one. We just
    assert the Python model accepts the parameter being omitted at construction
    time, which is what the supervisor's own ``_apply_event`` would do.
    """
    evt = VideoLostEvent(at_offset_seconds=0.0, reason="permission_denied")
    assert evt.message is None
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_video_file_event_object_round_trip() -> None:
    evt = VideoFileEvent(
        path="/abs/path/to/2026-05-10T14-32-08.mp4", duration_seconds=612.4
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


# ---------------------------------------------------------------------------
# AudioFileEvent (spec 005 slice 1)
# ---------------------------------------------------------------------------


def test_audio_file_event_object_round_trip_captured_normally() -> None:
    evt = AudioFileEvent(
        path="/abs/path/to/2026-05-10T14-32-08-mic.wav",
        source="mic",
        duration_seconds=312.4,
        status="captured_normally",
    )
    assert evt.truncated_at_offset_seconds is None
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


@pytest.mark.parametrize(
    "source,status,duration,truncated_at",
    [
        ("mic", "captured_normally", 312.4, None),
        ("system_audio", "silent_throughout", 312.4, None),
        ("mic", "truncated_at_offset", 134.2, 134.2),
        ("system_audio", "truncated_at_offset", 42.0, 42.0),
    ],
)
def test_audio_file_event_round_trip_across_statuses(
    source: str, status: str, duration: float, truncated_at: float | None
) -> None:
    evt = AudioFileEvent(
        path=f"/abs/path/to/2026-05-10T14-32-08-{source}.wav",
        source=source,  # type: ignore[arg-type]
        duration_seconds=duration,
        status=status,  # type: ignore[arg-type]
        truncated_at_offset_seconds=truncated_at,
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_audio_file_event_rejects_unknown_status() -> None:
    line = (
        '{"event":"audio_file","path":"/x.wav","source":"mic",'
        '"duration_seconds":1.0,"status":"made_up","truncated_at_offset_seconds":null}'
    )
    with pytest.raises((ValidationError, ValueError)):
        parse_event(line)


def test_audio_file_event_rejects_unknown_source() -> None:
    line = (
        '{"event":"audio_file","path":"/x.wav","source":"speakers",'
        '"duration_seconds":1.0,"status":"captured_normally",'
        '"truncated_at_offset_seconds":null}'
    )
    with pytest.raises((ValidationError, ValueError)):
        parse_event(line)


def test_audio_file_event_rejects_missing_required_field() -> None:
    # Missing ``duration_seconds``.
    line = (
        '{"event":"audio_file","path":"/x.wav","source":"mic",'
        '"status":"captured_normally","truncated_at_offset_seconds":null}'
    )
    with pytest.raises((ValidationError, ValueError)):
        parse_event(line)


@pytest.mark.parametrize(
    "reason", ["primary_changed", "resolution_changed", "display_removed"]
)
def test_display_reconfigured_event_accepts_each_reason(reason: str) -> None:
    evt = DisplayReconfiguredEvent(
        reason=reason,  # type: ignore[arg-type]
        new_display_id=2,
        new_width_px=1920,
        new_height_px=1080,
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


@pytest.mark.parametrize(
    "reason", ["system_sleep", "display_sleep", "screen_locked"]
)
def test_capture_ended_by_system_event_accepts_each_reason(reason: str) -> None:
    evt = CaptureEndedBySystemEventEvent(
        reason=reason,  # type: ignore[arg-type]
        at_offset_seconds=134.2,
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


# ---------------------------------------------------------------------------
# Hotkey IPC (spec 003 slice 5)
# ---------------------------------------------------------------------------


def test_register_hotkey_command_object_round_trip() -> None:
    cmd = RegisterHotkeyCommand(modifiers=["cmd", "option"], key="r")
    line = serialize_command(cmd)
    parsed = parse_command(line)
    assert parsed == cmd


def test_unregister_hotkey_command_object_round_trip() -> None:
    cmd = UnregisterHotkeyCommand()
    line = serialize_command(cmd)
    parsed = parse_command(line)
    assert parsed == cmd


@pytest.mark.parametrize(
    "status,message",
    [
        ("registered", "registered"),
        ("conflict", "another application has registered this combination"),
        ("invalid", "accessibility_denied"),
    ],
)
def test_hotkey_registered_event_object_round_trip(
    status: str, message: str
) -> None:
    evt = HotkeyRegisteredEvent(
        status=status,  # type: ignore[arg-type]
        modifiers=["cmd", "option"],
        key="r",
        message=message,
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_hotkey_pressed_event_object_round_trip() -> None:
    evt = HotkeyPressedEvent()
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_hotkey_unregistered_event_object_round_trip() -> None:
    evt = HotkeyUnregisteredEvent()
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


def test_hotkey_registered_event_message_is_required() -> None:
    """Swift always sends a ``message`` — the field must not be optional."""
    with pytest.raises((ValidationError, ValueError)):
        parse_event(
            '{"event":"hotkey_registered","status":"registered",'
            '"modifiers":["cmd","option"],"key":"r"}'
        )


# ---------------------------------------------------------------------------
# Malformed-input rejection
# ---------------------------------------------------------------------------


MALFORMED_COMMAND_INPUTS: list[str] = [
    "not json at all",
    "[]",  # JSON, but a list, not a dict
    '{"output_path":"/x.wav","format":{"sample_rate":16000,"bit_depth":16,"channels":1}}',  # missing discriminator
    '{"cmd":"nope"}',  # unknown discriminator
    '{"cmd":"stop","extra":"field"}',  # extra fields forbidden
]


@pytest.mark.parametrize("line", MALFORMED_COMMAND_INPUTS)
def test_parse_command_rejects_malformed(line: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_command(line)


MALFORMED_EVENT_INPUTS: list[str] = [
    "not json at all",
    "42",  # JSON, but a scalar, not a dict
    '{"start_time":"2026-05-10T14:32:08Z"}',  # missing discriminator
    '{"event":"nope"}',  # unknown discriminator
    '{"event":"ready","extra":"field"}',  # extra fields forbidden
    '{"event":"source_attached","source":"speakers"}',  # bad enum
    # New video / system events: bad enums must be rejected.
    '{"event":"display_reconfigured","reason":"nope","new_display_id":1,"new_width_px":1,"new_height_px":1}',
    '{"event":"capture_ended_by_system_event","reason":"reboot","at_offset_seconds":1.0}',
    # Required fields cannot be elided.
    '{"event":"video_started","display_id":1,"width_px":640,"height_px":360}',  # missing fps
    '{"event":"video_file","path":"/x.mp4"}',  # missing duration_seconds
]


@pytest.mark.parametrize("line", MALFORMED_EVENT_INPUTS)
def test_parse_event_rejects_malformed(line: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_event(line)


def test_audio_format_rejects_extra_fields() -> None:
    """Sub-model also enforces extra='forbid' to keep the wire schema tight."""
    bad = (
        '{"cmd":"start","output_path":"/x.wav","format":'
        '{"sample_rate":16000,"bit_depth":16,"channels":1,"surprise":true}}'
    )
    with pytest.raises((ValidationError, ValueError)):
        parse_command(bad)
