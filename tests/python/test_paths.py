"""Path-resolution tests.

Anything that creates directories on disk monkeypatches ``Path.home()`` to a
``tmp_path`` so the test never touches the user's real
``~/Library/Application Support/record/`` or ``~/Library/Logs/record/``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from record import paths


def test_app_support_dir_layout() -> None:
    expected = Path.home() / "Library" / "Application Support" / "record"
    assert paths.app_support_dir() == expected


def test_logs_dir_layout() -> None:
    expected = Path.home() / "Library" / "Logs" / "record"
    assert paths.logs_dir() == expected


def test_pid_file_lives_under_app_support_dir() -> None:
    assert paths.pid_file().parent == paths.app_support_dir()
    assert paths.pid_file().name == "capture.pid"


def test_state_file_lives_under_app_support_dir() -> None:
    assert paths.state_file().parent == paths.app_support_dir()
    assert paths.state_file().name == "capture-state.json"


def test_daemon_log_lives_under_logs_dir() -> None:
    assert paths.daemon_log().parent == paths.logs_dir()
    assert paths.daemon_log().name == "daemon.log"


def test_orchestrator_log_lives_under_logs_dir() -> None:
    assert paths.orchestrator_log().parent == paths.logs_dir()
    assert paths.orchestrator_log().name == "orchestrator.log"


def test_ensure_dirs_creates_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirect Path.home() to tmp_path so directory creation is sandboxed.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    expected_app = tmp_path / "Library" / "Application Support" / "record"
    expected_logs = tmp_path / "Library" / "Logs" / "record"

    assert not expected_app.exists()
    assert not expected_logs.exists()

    resolved = paths.ensure_dirs()

    assert expected_app.is_dir()
    assert expected_logs.is_dir()
    assert resolved.app_support_dir == expected_app
    assert resolved.logs_dir == expected_logs
    assert resolved.pid_file == expected_app / "capture.pid"
    assert resolved.state_file == expected_app / "capture-state.json"
    assert resolved.daemon_log == expected_logs / "daemon.log"
    assert resolved.orchestrator_log == expected_logs / "orchestrator.log"


def test_ensure_dirs_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    paths.ensure_dirs()
    # Second call must not raise even though directories already exist.
    paths.ensure_dirs()


def test_resolve_paths_returns_frozen_dataclass() -> None:
    resolved = paths.resolve_paths()
    assert dataclasses.is_dataclass(resolved)
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.pid_file = Path("/tmp/other")  # type: ignore[misc]
