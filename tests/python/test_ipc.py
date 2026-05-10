"""IPC wire-format tests.

Every Command and Event variant has a canonical JSON-line fixture under
``swift-capture/Tests/RecordCaptureTests/Fixtures/``. The Swift test suite
loads the same files, so any byte-level drift here will also break Swift —
that's the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from record.ipc import (
    AudioFormat,
    ErrorEvent,
    PermissionDeniedEvent,
    PermissionRequiredEvent,
    ReadyEvent,
    ShutdownCommand,
    SourceAttachedEvent,
    SourceLostEvent,
    StartCommand,
    StartedEvent,
    StopCommand,
    StoppedEvent,
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
    ("stop.json", StopCommand),
    ("shutdown.json", ShutdownCommand),
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
]


def _read_fixture(subdir: str, name: str) -> str:
    path: Path = FIXTURES_DIR / subdir / name
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("filename,model_cls", COMMAND_FIXTURES, ids=[f[0] for f in COMMAND_FIXTURES])
def test_command_fixture_round_trip(filename: str, model_cls: type) -> None:
    line = _read_fixture("commands", filename)
    parsed = parse_command(line)
    assert isinstance(parsed, model_cls)
    assert serialize_command(parsed).strip() == line.strip()


@pytest.mark.parametrize("filename,model_cls", EVENT_FIXTURES, ids=[f[0] for f in EVENT_FIXTURES])
def test_event_fixture_round_trip(filename: str, model_cls: type) -> None:
    line = _read_fixture("events", filename)
    parsed = parse_event(line)
    assert isinstance(parsed, model_cls)
    assert serialize_event(parsed).strip() == line.strip()


# ---------------------------------------------------------------------------
# Object-level round-trip (build via constructor, not fixture)
# ---------------------------------------------------------------------------


def test_start_command_object_round_trip() -> None:
    """Build the model via constructor → serialize → parse → assert equality."""
    cmd = StartCommand(
        output_path="/abs/path/to/2026-05-10T14-32-08.wav",
        format=AudioFormat(sample_rate=16000, bit_depth=16, channels=1),
    )
    line = serialize_command(cmd)
    parsed = parse_command(line)
    assert parsed == cmd


def test_source_lost_event_object_round_trip() -> None:
    evt = SourceLostEvent(
        source="mic", at_offset_seconds=134.2, reason="input device disconnected"
    )
    line = serialize_event(evt)
    parsed = parse_event(line)
    assert parsed == evt


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
