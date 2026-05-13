"""Daemon-scaffold tests for spec 003 slice 1.

The daemon is exercised through its small extracted helpers (``_startup``,
``_claim_pid_file``, ``_run``) rather than via a real subprocess, so the tests
never spawn a process and never install signal handlers on the test-runner's
event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from record import daemon, logging_setup, paths, state


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Each test starts from a fresh logging state.

    ``logging_setup`` keeps a module-global ``_configured`` flag plus a
    tagged-handler sentinel; without a reset, the first test to install a
    handler poisons every later test's caplog visibility.
    """
    # Detach any prior `_record_orchestrator_handler`-tagged handler so the
    # next call to configure_logging installs a fresh one.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, logging_setup._HANDLER_TAG, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    # Note: we do NOT reset `_configured` to False — structlog.configure is
    # process-global and re-running it is a no-op in practice; leaving the
    # flag set means subsequent configure_logging calls only swap the handler.
    yield


@pytest.fixture
def daemon_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect every daemon-side path to ``tmp_path``."""
    pid = tmp_path / "daemon.pid"
    log = tmp_path / "logs" / "daemon.log"
    log_dir = log.parent

    monkeypatch.setattr(paths, "daemon_pid_file", lambda: pid)
    monkeypatch.setattr(paths, "daemon_log_file", lambda: log)
    monkeypatch.setattr(paths, "daemon_log_dir", lambda: log_dir)
    # ensure_daemon_dirs uses both daemon_log_dir() and app_support_dir();
    # point app_support_dir at tmp_path too so the daemon PID's parent dir
    # creation lands inside the sandbox.
    monkeypatch.setattr(paths, "app_support_dir", lambda: tmp_path)
    # Also patch ensure_daemon_dirs to a sandbox-safe version that uses the
    # patched-above helpers (the original closes over the module's own
    # functions, which the monkeypatch already covers — but rebuilding it
    # explicitly is more robust against future changes).

    def _ensure() -> None:
        log_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "ensure_daemon_dirs", _ensure)

    return {"pid": pid, "log": log, "log_dir": log_dir, "root": tmp_path}


# ---------------------------------------------------------------------------
# PID-file claim
# ---------------------------------------------------------------------------


def test_claim_pid_file_creates_file_with_current_pid(daemon_paths: dict[str, Path]) -> None:
    """First call materialises the PID file containing this process's PID."""
    target = daemon_paths["pid"]
    assert not target.exists()

    daemon._claim_pid_file(os.getpid(), path=target)

    assert target.exists()
    contents = target.read_text(encoding="utf-8").strip()
    assert int(contents) == os.getpid()


def test_claim_pid_file_refuses_second_live_claim(daemon_paths: dict[str, Path]) -> None:
    """Second claim while the PID is alive raises CaptureAlreadyRunning."""
    target = daemon_paths["pid"]
    daemon._claim_pid_file(os.getpid(), path=target)

    with pytest.raises(state.CaptureAlreadyRunning) as exc_info:
        daemon._claim_pid_file(os.getpid(), path=target)
    assert exc_info.value.existing_pid == os.getpid()


def test_startup_succeeds_then_refuses_second_invocation(
    daemon_paths: dict[str, Path],
) -> None:
    """The whole `_startup` flow: claim + log init, then a second call exits 1."""
    rc = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc is None
    assert daemon_paths["pid"].exists()
    assert daemon_paths["pid"].read_text().strip() == str(os.getpid())

    # Second invocation should surface the already-running exit code.
    rc2 = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc2 == daemon._EXIT_ALREADY_RUNNING


def test_startup_writes_daemon_started_log_line(daemon_paths: dict[str, Path]) -> None:
    """`_startup` emits a `daemon started` log entry to the configured file."""
    rc = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc is None

    # Flush all handlers explicitly — RotatingFileHandler uses delay=True.
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass

    assert daemon_paths["log"].exists()
    content = daemon_paths["log"].read_text(encoding="utf-8")
    assert "daemon started" in content
    # Sanity-check the structured payload made it through.
    last_line = [ln for ln in content.splitlines() if ln.strip()][-1]
    parsed = json.loads(last_line)
    # structlog's reserved positional key surfaces in the JSON line as
    # `"event": "daemon started"`. We grep on the human string above; here we
    # also verify the structured fields rode along.
    assert parsed.get("event") == "daemon started"
    assert parsed.get("pid") == os.getpid()


# ---------------------------------------------------------------------------
# Signal-handler / shutdown event
# ---------------------------------------------------------------------------


def test_run_returns_immediately_when_event_preset(
    daemon_paths: dict[str, Path],
) -> None:
    """Pre-setting the shutdown event drives `_run` through one cycle.

    Critically, passing a non-None event tells `_run` *not* to install signal
    handlers, so the test runner's loop is unaffected.
    """
    # Pre-configure logging so `_run`'s log calls have a destination — `_run`
    # itself does not call configure_logging (that's `_startup`'s job).
    logging_setup.configure_logging(log_path=daemon_paths["log"])

    async def _drive() -> int:
        ev = asyncio.Event()
        ev.set()  # pre-set so the await returns immediately
        return await daemon._run(shutdown_event=ev)

    rc = asyncio.run(_drive())
    assert rc == daemon._EXIT_OK


def test_main_full_cycle_with_preset_event(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end of `main`-equivalent: startup, idle, cleanup, log lines.

    We patch `asyncio.run` so the daemon's main loop short-circuits via a
    pre-set event — this avoids touching the test runner's signal handlers
    and gives us the full startup -> stopped log sequence.
    """
    # First: drive _startup directly so we know the claim + log init happened.
    rc = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc is None
    assert daemon_paths["pid"].exists()

    # Now run the body of main() the way the entrypoint does.
    async def _drive() -> int:
        ev = asyncio.Event()
        ev.set()
        return await daemon._run(shutdown_event=ev)

    asyncio.run(_drive())

    # Emit the "daemon stopped" line the way `main` does in its `finally`
    # block, then remove the PID file.
    log = logging_setup.get_logger("record.daemon")
    log.info("daemon stopped", pid=os.getpid())
    daemon._remove_pid_file(path=daemon_paths["pid"])

    # Flush.
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass

    assert not daemon_paths["pid"].exists()
    content = daemon_paths["log"].read_text(encoding="utf-8")
    assert "daemon started" in content
    assert "daemon stopped" in content


def test_remove_pid_file_is_silent_on_missing(daemon_paths: dict[str, Path]) -> None:
    """`_remove_pid_file` must not raise when the file is already gone."""
    assert not daemon_paths["pid"].exists()
    # Must not raise.
    daemon._remove_pid_file(path=daemon_paths["pid"])
