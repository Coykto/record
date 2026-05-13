"""Long-running background daemon for the ``record`` orchestrator.

Slice 1 of spec 003 — bare scaffold only. The daemon:

1. Claims an atomic PID file at ``~/Library/Application Support/record/daemon.pid``
   so a second daemon refuses to start while one is alive.
2. Initialises structlog into ``~/record/logs/daemon.log`` (hard-coded for
   slice 1; slice 3 makes the log folder configurable via
   ``~/.config/record/config.toml``).
3. Idles on an :class:`asyncio.Event` until SIGTERM/SIGINT.
4. Cleans up the PID file on exit.

The Swift child, control socket, and config loader are **explicitly out of
scope** for this slice — they arrive in slices 2, 3, and 4. The CLI
(``record daemon start`` / ``stop`` / ``restart``) spawns this module as
``python -m record.daemon`` exactly the way ``record start`` spawns
``record.supervisor`` today.

The legacy ``record start`` / ``python -m record.supervisor`` path is
**untouched** by this module; the supervisor's PID file (``capture.pid``) and
this daemon's PID file (``daemon.pid``) are independent.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from . import paths, state
from .logging_setup import configure_logging, get_logger

# Exit codes used by the daemon process itself. ``record daemon start`` reads
# these via the spawned process's early-exit polling (mirrors the supervisor's
# fail-fast handshake).
_EXIT_OK = 0
_EXIT_ALREADY_RUNNING = 1


def _claim_pid_file(pid: int, *, path: Path | None = None) -> None:
    """Claim the daemon PID file atomically.

    Thin wrapper around :func:`state.claim_pid_file` that defaults the path to
    :func:`paths.daemon_pid_file` and creates the parent directory first.
    Factored out so unit tests can drive the claim/cleanup pair without
    spinning up the whole asyncio main loop.
    """
    target = path if path is not None else paths.daemon_pid_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    state.claim_pid_file(pid, path=target)


def _remove_pid_file(*, path: Path | None = None) -> None:
    """Remove the daemon PID file; never raises on missing file."""
    target = path if path is not None else paths.daemon_pid_file()
    try:
        state.remove_pid_file(path=target)
    except Exception:  # pragma: no cover - defensive, remove_pid_file is silent
        pass


async def _run(shutdown_event: asyncio.Event | None = None) -> int:
    """Async main coroutine.

    Parameters
    ----------
    shutdown_event:
        Optional pre-built event. When ``None``, a fresh
        :class:`asyncio.Event` is created and SIGTERM/SIGINT handlers are
        installed via :meth:`asyncio.AbstractEventLoop.add_signal_handler`.
        Tests pass a pre-set event to drive the coroutine through one cycle
        without touching real signals.
    """
    log = get_logger("record.daemon")
    loop = asyncio.get_running_loop()

    event = shutdown_event if shutdown_event is not None else asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        # Best effort: structlog dispatches lazily, so logging from inside a
        # signal handler is safe here — but we still guard against re-entry by
        # checking the event before logging a duplicate line.
        if event.is_set():
            return
        try:
            log.info(
                "daemon_stop_signal_received",
                signal=sig.name,
                signal_number=int(sig),
            )
        except Exception:  # pragma: no cover - defensive
            pass
        event.set()

    # When the caller pre-set the event we're being driven by a test — don't
    # install real signal handlers on the test runner's loop.
    handlers_installed: list[signal.Signals] = []
    if shutdown_event is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal, sig)
                handlers_installed.append(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                # add_signal_handler is unsupported on some platforms (Windows)
                # — the daemon is macOS-only so this is purely defensive.
                pass

    try:
        await event.wait()
    finally:
        # Remove the handlers before the loop exits so a re-entrant call can
        # install fresh ones (relevant when the test suite spins up multiple
        # event loops in a row).
        for sig in handlers_installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass

    return _EXIT_OK


def _startup(*, pid_file_path: Path | None = None, log_path: Path | None = None) -> int | None:
    """Claim the PID file + configure logging.

    Returns ``None`` on success, or an exit code on failure (so :func:`main`
    can ``sys.exit`` directly). Split out from :func:`main` so tests can
    exercise the claim path against a ``tmp_path``-rooted PID file without
    going through ``asyncio.run``.
    """
    # Ensure both the log directory and the Application Support directory
    # exist before we touch a single file in either. ensure_daemon_dirs is
    # idempotent.
    paths.ensure_daemon_dirs()

    target_pid = pid_file_path if pid_file_path is not None else paths.daemon_pid_file()
    target_log = log_path if log_path is not None else paths.daemon_log_file()

    # PID claim *before* logging setup. If a second daemon races us we don't
    # want it to truncate or rotate the live daemon's log file as a side
    # effect of opening the rotating handler.
    try:
        _claim_pid_file(os.getpid(), path=target_pid)
    except state.CaptureAlreadyRunning as exc:
        # Logging may not be configured yet; fall back to the structlog
        # default (stderr) for this single line. We point structlog at the
        # daemon log anyway so any subsequent get_logger calls land in the
        # right file — but this branch ends in sys.exit so there are no
        # subsequent calls.
        try:
            configure_logging(log_path=target_log)
            log = get_logger("record.daemon")
            log.warning(
                "daemon_already_running",
                existing_pid=exc.existing_pid,
                daemon_pid_file=str(target_pid),
            )
        except Exception:  # pragma: no cover - defensive
            pass
        return _EXIT_ALREADY_RUNNING

    configure_logging(log_path=target_log)
    log = get_logger("record.daemon")
    # NB: `event` is structlog's reserved positional-arg key. We use the
    # literal "daemon started" as the event name so the verification step's
    # `grep "daemon started"` matches the log line directly. The extra
    # context (pid, log path, PID-file path) goes in dedicated fields.
    log.info(
        "daemon started",
        pid=os.getpid(),
        daemon_log_path=str(target_log),
        daemon_pid_file=str(target_pid),
    )
    return None


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - argv reserved for future use
    """Synchronous entrypoint. Returns the process exit code."""
    rc = _startup()
    if rc is not None:
        return rc

    log = get_logger("record.daemon")
    try:
        asyncio.run(_run())
        log.info("daemon stopped", pid=os.getpid())
        return _EXIT_OK
    except Exception as exc:  # pragma: no cover - defensive
        log.error("daemon_crashed", error=str(exc), error_type=type(exc).__name__)
        return 1
    finally:
        _remove_pid_file()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))


__all__ = [
    "main",
    "_claim_pid_file",
    "_remove_pid_file",
    "_run",
    "_startup",
]
