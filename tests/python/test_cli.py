"""CLI tests using Typer's ``CliRunner``.

The tests never spawn a real supervisor — every code path that depends on the
supervisor subprocess is exercised by pre-seeding the PID and state files in
a ``tmp_path``-rooted fake "app support" directory. ``record.paths.pid_file``
and ``record.paths.state_file`` are monkeypatched so the CLI reads our fakes
instead of the user's real ``~/Library/Application Support/record/``.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from record import cli, paths, state


runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_DEAD_PID = 99999999


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect ``record.paths.{pid_file,state_file}`` to ``tmp_path``.

    Also redirects the module-level references inside ``record.state`` (it
    imports them as ``_default_pid_file`` / ``_default_state_file`` at module
    import time), so calls to ``state.read_pid_file()`` with no ``path=``
    argument see the fakes too.
    """
    fake_pid = tmp_path / "capture.pid"
    fake_state = tmp_path / "capture-state.json"

    monkeypatch.setattr(paths, "pid_file", lambda: fake_pid)
    monkeypatch.setattr(paths, "state_file", lambda: fake_state)
    # state.py captured these at import time as `_default_pid_file` /
    # `_default_state_file`; patch those bindings too.
    monkeypatch.setattr(state, "_default_pid_file", lambda: fake_pid)
    monkeypatch.setattr(state, "_default_state_file", lambda: fake_state)
    # Also redirect ensure_dirs so `record start` doesn't try to create
    # ~/Library/Application Support/record on the test machine.
    monkeypatch.setattr(
        paths,
        "ensure_dirs",
        lambda: paths.RecordPaths(
            app_support_dir=tmp_path,
            logs_dir=tmp_path,
            pid_file=fake_pid,
            state_file=fake_state,
            daemon_log=tmp_path / "daemon.log",
            orchestrator_log=tmp_path / "orchestrator.log",
        ),
    )
    return {"pid": fake_pid, "state": fake_state, "root": tmp_path}


# ---------------------------------------------------------------------------
# `record stop` exit codes
# ---------------------------------------------------------------------------


def test_stop_exits_1_when_no_pid_file(fake_paths: dict[str, Path]) -> None:
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "no capture running" in result.stderr


def test_stop_exits_1_on_stale_pid_file(fake_paths: dict[str, Path]) -> None:
    assert not state.is_alive(_DEAD_PID), "test prerequisite: dead PID must be dead"
    fake_paths["pid"].write_text(f"{_DEAD_PID}\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "stale" in result.stderr.lower()
    # Stale PID file must be cleaned up.
    assert not fake_paths["pid"].exists()


def test_stop_exits_4_when_supervisor_does_not_exit(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM never actually fires (we stub os.kill), so the live PID won't
    'exit' before the timeout. We shrink the timeout to make the test quick.
    """
    # The test process itself is the "supervisor". It's definitely alive.
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_STOP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cli, "_STOP_POLL_INTERVAL", 0.01)

    # Critically: stub os.kill so we don't actually SIGTERM the test runner.
    def _fake_kill(pid: int, sig: int) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(cli.os, "kill", _fake_kill)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 4
    assert "did not exit" in result.stderr


def test_stop_exits_1_when_process_disappears_after_check(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_alive`` says yes, then SIGTERM raises ProcessLookupError — race
    handling should clean up and exit 1.

    ``state.is_alive`` uses ``os.kill(pid, 0)`` so we only raise
    ``ProcessLookupError`` for non-zero signals (SIGTERM); the zero-signal
    probe still succeeds and tells the CLI the process is alive.
    """
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    real_kill = os.kill

    def _kill_raises_on_term(pid: int, sig: int) -> None:
        if sig == 0:
            real_kill(pid, 0)  # let is_alive's probe behave normally
            return
        raise ProcessLookupError()

    monkeypatch.setattr(cli.os, "kill", _kill_raises_on_term)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "process disappeared" in result.stderr


# ---------------------------------------------------------------------------
# `record start` exit codes
# ---------------------------------------------------------------------------


def test_start_exits_1_when_capture_already_running(fake_paths: dict[str, Path]) -> None:
    """A live PID in the pid file → exit 1, no supervisor spawned."""
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 1
    assert "capture already in progress" in result.stderr


def test_start_exits_3_when_binary_missing(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_resolve_capture_binary", lambda: None)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 3
    assert "make install" in result.stderr


def test_start_clears_stale_pid_before_spawning(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale PID file plus a missing binary → still exit 3 (not 1), and the
    stale file should have been cleaned up before the binary check ran."""
    assert not state.is_alive(_DEAD_PID)
    fake_paths["pid"].write_text(f"{_DEAD_PID}\n", encoding="utf-8")
    # Also leave a half-written state file behind to make sure it's swept too.
    fake_paths["state"].write_text('{"final": false}\n', encoding="utf-8")

    monkeypatch.setattr(cli, "_resolve_capture_binary", lambda: None)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 3
    # The stale state file gets removed alongside the stale PID file before
    # `_resolve_capture_binary` runs.
    assert not fake_paths["state"].exists()


# ---------------------------------------------------------------------------
# `record stop` happy-path summary
# ---------------------------------------------------------------------------


def test_stop_prints_summary_when_state_present(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub os.kill + is_alive so the loop terminates on the first poll
    iteration, then assert the summary text from the state file."""
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/x.wav", "duration_seconds": 42.5, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}}, '
            '"warnings": [], "final": true}'
        ),
        encoding="utf-8",
    )

    # First call (real, before kill) sees alive=True; after our stubbed kill,
    # the polling loop sees alive=False on the first iteration. Easiest path:
    # stub os.kill to no-op and flip is_alive to False for any subsequent call.
    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:
        calls["is_alive"] += 1
        return calls["is_alive"] == 1  # alive once, dead thereafter

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0
    assert "capture stopped" in result.stdout
    assert "/abs/x.wav" in result.stdout
    assert "microphone + system audio" in result.stdout
    # Cleanup happened.
    assert not fake_paths["pid"].exists()
    assert not fake_paths["state"].exists()


def test_stop_exits_2_on_permission_denied(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """capture-state.json with `permission_denied: "microphone"` → exit 2.

    We stub os.kill and is_alive the same way the happy-path test does so we
    can reach the post-wait state-read branch without touching real signals.
    """
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    fake_paths["state"].write_text(
        '{"permission_denied": "microphone", "final": true}', encoding="utf-8"
    )

    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:
        calls["is_alive"] += 1
        return calls["is_alive"] == 1

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 2
    assert "microphone permission denied" in result.stderr
    assert "System Settings" in result.stderr
    assert not fake_paths["pid"].exists()
    assert not fake_paths["state"].exists()
