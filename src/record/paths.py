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

_PID_FILENAME = "capture.pid"
_STATE_FILENAME = "capture-state.json"
_DAEMON_LOG_FILENAME = "daemon.log"
_ORCHESTRATOR_LOG_FILENAME = "orchestrator.log"


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
    "resolve_paths",
    "ensure_dirs",
]
