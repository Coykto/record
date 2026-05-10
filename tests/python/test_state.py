"""PID-file and capture-state.json tests.

Every call passes ``path=`` explicitly so the test never touches the user's
real ``~/Library/Application Support/record/`` directory.
"""

from __future__ import annotations

import os
from pathlib import Path

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
