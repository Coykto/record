"""macOS path resolution for the orchestrator.

Resolves the application-support and log directories used by the capture
supervisor, per `context/spec/001-mixed-mic-system-audio-capture/technical-considerations.md`
§2.5. All paths are absolute and built off ``Path.home()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Directory names live here so the rest of the orchestrator imports them from a
# single source of truth.
_APP_SUPPORT_SUBPATH = ("Library", "Application Support", "record")
_LOGS_SUBPATH = ("Library", "Logs", "record")

# Slice-1 daemon log layout (spec 003 §2.10). Hard-coded for now; slice 3
# replaces this with a config-driven resolver that consults the `log_folder`
# key from ~/.config/record/config.toml. Kept as a tuple so the override is a
# one-line edit when that lands.
_DAEMON_LOG_ROOT = ("record", "logs")

_PID_FILENAME = "capture.pid"
_STATE_FILENAME = "capture-state.json"
_DAEMON_LOG_FILENAME = "daemon.log"
_ORCHESTRATOR_LOG_FILENAME = "orchestrator.log"

# Daemon-side filenames (spec 003 §2.10). The daemon's PID file is a sibling
# of `capture.pid` under Application Support; the control socket (path-only in
# slice 1, wired in slice 2) lives next to it.
_DAEMON_PID_FILENAME = "daemon.pid"
_DAEMON_SOCKET_FILENAME = "daemon.sock"


@dataclass(frozen=True)
class RecordPaths:
    """Resolved set of directories and files used by the orchestrator."""

    app_support_dir: Path
    logs_dir: Path
    pid_file: Path
    state_file: Path
    daemon_log: Path
    orchestrator_log: Path


def app_support_dir() -> Path:
    """Return ``~/Library/Application Support/record/`` as an absolute Path."""
    return Path.home().joinpath(*_APP_SUPPORT_SUBPATH)


def logs_dir() -> Path:
    """Return ``~/Library/Logs/record/`` as an absolute Path."""
    return Path.home().joinpath(*_LOGS_SUBPATH)


def pid_file() -> Path:
    """Return the absolute path to the capture PID file."""
    return app_support_dir() / _PID_FILENAME


def state_file() -> Path:
    """Return the absolute path to ``capture-state.json``."""
    return app_support_dir() / _STATE_FILENAME


def daemon_log() -> Path:
    """Return the absolute path to the Swift daemon's log file."""
    return logs_dir() / _DAEMON_LOG_FILENAME


def orchestrator_log() -> Path:
    """Return the absolute path to the orchestrator's structlog file."""
    return logs_dir() / _ORCHESTRATOR_LOG_FILENAME


def daemon_pid_file() -> Path:
    """Return the absolute path to the daemon's PID file.

    Sibling of :func:`pid_file` under ``~/Library/Application Support/record/``.
    The daemon (spec 003) claims this atomically at startup; the legacy
    supervisor's :func:`pid_file` is unrelated and remains the singleton for
    the foreground ``python -m record.supervisor`` test path.
    """
    return app_support_dir() / _DAEMON_PID_FILENAME


def daemon_socket() -> Path:
    """Return the absolute path to the daemon's Unix-domain control socket.

    Path-only in slice 1 of spec 003 — slice 2 binds the socket. Lives next to
    :func:`daemon_pid_file` under Application Support because it's machine-local
    state, not user content.
    """
    return app_support_dir() / _DAEMON_SOCKET_FILENAME


def daemon_log_dir() -> Path:
    """Return ``~/record/logs/`` as an absolute Path.

    Hard-coded for slice 1 (spec 003 tasks §1). Slice 3 will replace this with
    a config-driven resolver that consults the ``log_folder`` key from
    ``~/.config/record/config.toml``. Distinct from :func:`logs_dir` — that
    one is the legacy ``~/Library/Logs/record/`` used by the supervisor.
    """
    return Path.home().joinpath(*_DAEMON_LOG_ROOT)


def daemon_log_file() -> Path:
    """Return the absolute path to the daemon's structured log file.

    ``~/record/logs/daemon.log`` per FR 2.14. Hard-coded in slice 1; see
    :func:`daemon_log_dir` for the reload story.
    """
    return daemon_log_dir() / _DAEMON_LOG_FILENAME


def ensure_daemon_dirs() -> None:
    """Create :func:`daemon_log_dir` and the daemon PID file's parent.

    Both are idempotent (``parents=True, exist_ok=True``). Called by the
    daemon at startup before any handler / PID-file claim runs.
    """
    daemon_log_dir().mkdir(parents=True, exist_ok=True)
    app_support_dir().mkdir(parents=True, exist_ok=True)


def resolve_paths() -> RecordPaths:
    """Resolve every path in one go and return them as a frozen dataclass."""
    return RecordPaths(
        app_support_dir=app_support_dir(),
        logs_dir=logs_dir(),
        pid_file=pid_file(),
        state_file=state_file(),
        daemon_log=daemon_log(),
        orchestrator_log=orchestrator_log(),
    )


def ensure_dirs() -> RecordPaths:
    """Create the parent directories if they're missing.

    Both directories are created with ``parents=True, exist_ok=True``. Returns
    the resolved paths so callers can immediately use them.
    """
    paths = resolve_paths()
    paths.app_support_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    return paths


__all__ = [
    "RecordPaths",
    "app_support_dir",
    "logs_dir",
    "pid_file",
    "state_file",
    "daemon_log",
    "orchestrator_log",
    "daemon_pid_file",
    "daemon_socket",
    "daemon_log_dir",
    "daemon_log_file",
    "ensure_daemon_dirs",
    "resolve_paths",
    "ensure_dirs",
]
