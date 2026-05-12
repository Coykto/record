"""PID-file and capture-state.json tests.

Every call passes ``path=`` explicitly so the test never touches the user's
real ``~/Library/Application Support/record/`` directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from record.state import (
    CaptureAlreadyRunning,
    claim_pid_file,
    create_pid_file,
    is_alive,
    read_pid_file,
    read_state,
    remove_pid_file,
    remove_state,
    write_state,
)


# A PID very unlikely to belong to a live process. ``is_alive`` guards each
# usage so the test is robust if reality disagrees.
_DEAD_PID = 99999999


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------


def test_create_pid_file_writes_pid_with_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    create_pid_file(4242, path=target)
    assert target.read_text(encoding="utf-8") == "4242\n"


def test_create_pid_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "capture.pid"
    create_pid_file(7, path=target)
    assert target.exists()


def test_read_pid_file_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    create_pid_file(1234, path=target)
    assert read_pid_file(path=target) == 1234


def test_create_pid_file_raises_when_existing(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    create_pid_file(1, path=target)
    with pytest.raises(FileExistsError):
        create_pid_file(2, path=target)


@pytest.mark.parametrize("contents", ["", "   ", "\n\t\n", "not-an-int", "12 34"])
def test_read_pid_file_returns_none_for_garbage(tmp_path: Path, contents: str) -> None:
    target = tmp_path / "capture.pid"
    target.write_text(contents, encoding="utf-8")
    assert read_pid_file(path=target) is None


def test_read_pid_file_returns_none_for_missing(tmp_path: Path) -> None:
    target = tmp_path / "does-not-exist.pid"
    assert read_pid_file(path=target) is None


# ---------------------------------------------------------------------------
# is_alive
# ---------------------------------------------------------------------------


def test_is_alive_for_self() -> None:
    assert is_alive(os.getpid()) is True


def test_is_alive_for_dead_pid() -> None:
    # Skip if reality contradicts our assumption — extremely unlikely.
    assert is_alive(_DEAD_PID) is False


@pytest.mark.parametrize("pid", [0, -1, -99999])
def test_is_alive_rejects_nonpositive(pid: int) -> None:
    assert is_alive(pid) is False


# ---------------------------------------------------------------------------
# claim_pid_file
# ---------------------------------------------------------------------------


def test_claim_pid_file_succeeds_on_empty_dir(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    claim_pid_file(1234, path=target)
    assert read_pid_file(path=target) == 1234


def test_claim_pid_file_rejects_when_live_pid_exists(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    create_pid_file(os.getpid(), path=target)
    with pytest.raises(CaptureAlreadyRunning) as exc:
        claim_pid_file(7777, path=target)
    assert exc.value.existing_pid == os.getpid()


def test_claim_pid_file_recovers_stale_file(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    assert not is_alive(_DEAD_PID), "test prerequisite: _DEAD_PID must be dead"
    create_pid_file(_DEAD_PID, path=target)
    claim_pid_file(5555, path=target)
    assert read_pid_file(path=target) == 5555


def test_claim_pid_file_recovers_unreadable_file(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    target.write_text("garbage\n", encoding="utf-8")
    claim_pid_file(5555, path=target)
    assert read_pid_file(path=target) == 5555


def test_remove_pid_file_is_silent_on_missing(tmp_path: Path) -> None:
    target = tmp_path / "missing.pid"
    remove_pid_file(path=target)  # must not raise


def test_remove_pid_file_removes_existing(tmp_path: Path) -> None:
    target = tmp_path / "capture.pid"
    create_pid_file(1, path=target)
    remove_pid_file(path=target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# capture-state.json
# ---------------------------------------------------------------------------


def test_state_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "capture-state.json"
    payload = {
        "pid": 1234,
        "output_path": "/abs/x.wav",
        "sources": {"mic": {"status": "attached"}},
        "warnings": [],
        "final": False,
    }
    write_state(payload, path=target)
    assert read_state(path=target) == payload


def test_write_state_is_atomic_no_tmp_remains(tmp_path: Path) -> None:
    target = tmp_path / "capture-state.json"
    write_state({"a": 1}, path=target)
    tmp_sibling = target.with_name(target.name + ".tmp")
    assert target.exists()
    assert not tmp_sibling.exists()


def test_write_state_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "capture-state.json"
    write_state({"a": 1}, path=target)
    write_state({"a": 2}, path=target)
    assert read_state(path=target) == {"a": 2}


def test_write_state_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "capture-state.json"
    write_state({"hello": "world"}, path=target)
    assert read_state(path=target) == {"hello": "world"}


def test_read_state_missing_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "missing.json"
    assert read_state(path=target) is None


def test_read_state_malformed_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "bad.json"
    target.write_text("not-json{", encoding="utf-8")
    assert read_state(path=target) is None


def test_read_state_list_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "list.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_state(path=target) is None


def test_remove_state_silent_on_missing(tmp_path: Path) -> None:
    target = tmp_path / "missing.json"
    remove_state(path=target)  # must not raise


def test_remove_state_removes_existing(tmp_path: Path) -> None:
    target = tmp_path / "capture-state.json"
    write_state({"a": 1}, path=target)
    remove_state(path=target)
    assert not target.exists()


# ---------------------------------------------------------------------------
# Widened state schema (slice 1 plumbing, slices 2-5 mutations)
#
# The state file is a free-form dict on the Python side — there is no pydantic
# model gating it; the supervisor is the only writer. These tests assert the
# shape the supervisor produces is what ``read_state`` returns, with the new
# video / display_changes / ended_by / video_output_path fields all surviving
# the JSON round trip.
# ---------------------------------------------------------------------------


def _initial_state_like() -> dict[str, Any]:
    """Return the same dict shape ``supervisor._initial_state`` builds.

    Duplicated here on purpose so this test doesn't depend on the supervisor's
    internal helper — the contract is the shape on disk, not the Python
    function that produced it. If the supervisor's shape ever drifts from this
    fixture, that's a real wire-shape change worth surfacing in the diff.
    """
    return {
        "pid": 1234,
        "start_time": None,
        "output_path": None,
        "video_output_path": None,
        "sources": {
            "mic": {
                "status": "never_attached",
                "attached_at": None,
                "lost_at": None,
            },
            "system_audio": {
                "status": "never_attached",
                "attached_at": None,
                "lost_at": None,
            },
            "video": {
                "status": "never_attached",
                "attached_at": None,
                "lost_at": None,
                "display_id": None,
                "width_px": None,
                "height_px": None,
                "fps": None,
            },
        },
        "warnings": [],
        "display_changes": [],
        "ended_by": None,
        "last_event_at": "2026-05-11T12:00:00Z",
        "final": False,
    }


def test_initial_widened_state_round_trips(tmp_path: Path) -> None:
    """The widened initial-state shape (video source + display_changes +
    ended_by + video_output_path) round-trips through write_state/read_state
    without mutation."""
    target = tmp_path / "capture-state.json"
    initial = _initial_state_like()
    write_state(initial, path=target)
    assert read_state(path=target) == initial


def test_widened_state_defaults(tmp_path: Path) -> None:
    """Defensive: defaults for the new fields are exactly what slices 1-5 expect."""
    initial = _initial_state_like()

    assert initial["video_output_path"] is None
    assert initial["display_changes"] == []
    assert initial["ended_by"] is None
    assert initial["sources"]["video"]["status"] == "never_attached"
    for k in ("attached_at", "lost_at", "display_id", "width_px", "height_px", "fps"):
        assert initial["sources"]["video"][k] is None


@pytest.mark.parametrize("status", ["attached", "lost", "never_attached"])
def test_video_status_values_round_trip(tmp_path: Path, status: str) -> None:
    target = tmp_path / "capture-state.json"
    payload = _initial_state_like()
    payload["sources"]["video"]["status"] = status
    write_state(payload, path=target)
    loaded = read_state(path=target)
    assert loaded is not None
    assert loaded["sources"]["video"]["status"] == status


def test_video_attached_state_round_trip(tmp_path: Path) -> None:
    """Slice 2: ``video_started`` mutates the video source to ``attached`` with
    display + pixel + fps detail. All those fields round-trip through JSON."""
    target = tmp_path / "capture-state.json"
    payload = _initial_state_like()
    payload["video_output_path"] = "/abs/2026-05-11T12-00-00.mp4"
    payload["sources"]["video"].update(
        {
            "status": "attached",
            "attached_at": "2026-05-11T12:00:01Z",
            "display_id": 1,
            "width_px": 2560,
            "height_px": 1440,
            "fps": 30,
        }
    )
    payload["video_file_duration_seconds"] = 612.4
    write_state(payload, path=target)
    loaded = read_state(path=target)
    assert loaded is not None
    video = loaded["sources"]["video"]
    assert video["status"] == "attached"
    assert video["display_id"] == 1
    assert video["width_px"] == 2560
    assert video["height_px"] == 1440
    assert video["fps"] == 30
    assert loaded["video_output_path"] == "/abs/2026-05-11T12-00-00.mp4"
    assert loaded["video_file_duration_seconds"] == 612.4


def test_video_lost_state_round_trip(tmp_path: Path) -> None:
    """Slice 5: ``video_lost`` mutates the video source to ``lost`` and appends
    a warning carrying offset + reason. Both survive the JSON round trip."""
    target = tmp_path / "capture-state.json"
    payload = _initial_state_like()
    payload["sources"]["video"]["status"] = "lost"
    payload["sources"]["video"]["lost_at"] = "2026-05-11T12:00:01Z"
    payload["warnings"].append(
        {
            "timestamp": "2026-05-11T12:00:01Z",
            "source": "video",
            "message": "sc_stream_error",
            "at_offset_seconds": 134.2,
        }
    )
    write_state(payload, path=target)
    loaded = read_state(path=target)
    assert loaded is not None
    assert loaded["sources"]["video"]["status"] == "lost"
    video_warnings = [
        w for w in loaded["warnings"] if w.get("source") == "video"
    ]
    assert len(video_warnings) == 1
    assert video_warnings[0]["at_offset_seconds"] == 134.2
    assert video_warnings[0]["message"] == "sc_stream_error"


def test_display_changes_array_round_trip(tmp_path: Path) -> None:
    """Slice 3: each ``display_reconfigured`` event appends one entry into
    ``display_changes``. The list (and each entry's shape) round-trips."""
    target = tmp_path / "capture-state.json"
    payload = _initial_state_like()
    payload["display_changes"] = [
        {
            "timestamp": "2026-05-11T12:00:05Z",
            "reason": "primary_changed",
            "new_display_id": 2,
            "new_width_px": 1920,
            "new_height_px": 1080,
        },
        {
            "timestamp": "2026-05-11T12:00:09Z",
            "reason": "resolution_changed",
            "new_display_id": 2,
            "new_width_px": 2560,
            "new_height_px": 1440,
        },
    ]
    write_state(payload, path=target)
    loaded = read_state(path=target)
    assert loaded is not None
    assert loaded["display_changes"] == payload["display_changes"]
    assert loaded["display_changes"][0]["reason"] == "primary_changed"
    assert loaded["display_changes"][1]["reason"] == "resolution_changed"


@pytest.mark.parametrize(
    "ended_by",
    [None, "stop_command", "system_sleep", "display_sleep", "screen_locked", "audio_failure"],
)
def test_ended_by_values_round_trip(tmp_path: Path, ended_by: str | None) -> None:
    """Slice 4: ``ended_by`` accepts every documented terminal reason (plus
    ``None`` while the capture is still running)."""
    target = tmp_path / "capture-state.json"
    payload = _initial_state_like()
    payload["ended_by"] = ended_by
    write_state(payload, path=target)
    loaded = read_state(path=target)
    assert loaded is not None
    assert loaded["ended_by"] == ended_by
