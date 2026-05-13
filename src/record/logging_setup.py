"""Structlog configuration for the orchestrator.

Per the technical-considerations doc §2.5, orchestrator-side logs land in a
single file at the path returned by :func:`record.paths.orchestrator_log` —
**not** stdout/stderr. Each line is a single JSON object with at least a UTC
ISO-8601 timestamp, level, logger name, and event/message field.

The file handler is size-rotating to keep the log bounded across long
sessions; rollover is deliberate and conservative (5 MB × 5 backups) since
the orchestrator's per-event volume is modest.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Any

import structlog
from structlog.stdlib import BoundLogger

from . import paths

# Rotation knobs. Picked to be generous for hour-long captures while still
# capping disk use at ~30 MB worst case. Nothing here is user-tunable yet —
# the broader config schema doesn't exist in this slice.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

# Sentinel attached to our handler so we can detect a previous configure call
# and stay idempotent without re-installing handlers.
_HANDLER_TAG = "_record_orchestrator_handler"

_configured = False


def _build_handler(log_path: Any) -> logging.handlers.RotatingFileHandler:
    """Create the rotating file handler that backs structlog output."""
    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_path),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,  # don't open the file until the first emit
    )
    # structlog renders the full JSON line itself — the stdlib formatter just
    # needs to pass that string through verbatim.
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, _HANDLER_TAG, True)
    return handler


def configure_logging(log_path: Path | None = None) -> None:
    """Install the structlog -> rotating-JSON-file pipeline.

    Safe to call multiple times: subsequent calls are no-ops once the handler
    is in place.

    Parameters
    ----------
    log_path:
        Optional override of the rotating-file destination. When ``None`` (the
        default), the legacy ``~/Library/Logs/record/orchestrator.log`` is used
        and :func:`paths.ensure_dirs` is invoked to materialise the parent
        directory. When set (e.g. by the spec-003 daemon pointing at
        ``~/record/logs/daemon.log``), the parent directory is created
        explicitly and :func:`paths.ensure_dirs` is **not** called — the
        daemon has its own ``ensure_daemon_dirs`` helper for that.
    """
    global _configured

    if log_path is None:
        # Make sure the log directory exists before any handler tries to write.
        paths.ensure_dirs()
        target = paths.orchestrator_log()
    else:
        target = log_path
        target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()

    # Idempotency: if our tagged handler is already attached pointing at the
    # same file, we're done. If a tagged handler exists for a *different* file
    # (e.g. tests reconfigured between sessions), swap it.
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_TAG, False):
            existing_path = getattr(existing, "baseFilename", None)
            if existing_path == str(target):
                _configured = True
                return
            # Different target — detach the stale handler before installing
            # the new one so we don't write to two files in parallel.
            root.removeHandler(existing)
            try:
                existing.close()
            except Exception:  # pragma: no cover - defensive
                pass

    handler = _build_handler(target)
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    if not _configured:
        # Configure structlog only once per process. Re-running this with new
        # handlers in place would otherwise reset cached loggers held by call
        # sites that imported get_logger earlier.
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
        _configured = True


def get_logger(name: str) -> BoundLogger:
    """Return a structlog ``BoundLogger`` bound to ``name``.

    Calls :func:`configure_logging` lazily so importers don't have to remember
    to set up logging before grabbing a logger.
    """
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
