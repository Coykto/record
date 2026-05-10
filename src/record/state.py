"""PID-file and capture-state.json management for the supervisor.

PID file semantics implement the single-instance rule from
`context/spec/001-mixed-mic-system-audio-capture/technical-considerations.md`
§2.10: atomic creation via ``O_CREAT|O_EXCL`` with stale-PID detection by
``os.kill(pid, 0)``.

The capture-state file is an internal contract between the supervisor and the
``record stop`` CLI; we keep it as a plain ``dict`` here rather than a pydantic
model because the supervisor is the only writer and the shape is allowed to
evolve internally without a schema bump.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .paths import pid_file as _default_pid_file
from .paths import state_file as _default_state_file


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CaptureAlreadyRunning(RuntimeError):
    """Raised when the PID file points at a live process.

    The ``existing_pid`` attribute lets the CLI surface a useful error message.
    """

    def __init__(self, existing_pid: int) -> None:
        super().__init__(f"capture already in progress (pid {existing_pid})")
        self.existing_pid = existing_pid


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------


def create_pid_file(pid: int, *, path: Path | None = None) -> None:
    """Atomically create the PID file containing ``{pid}\\n``.

    Uses ``O_CREAT | O_EXCL | O_WRONLY`` so a concurrent supervisor cannot
    silently overwrite an existing file. Raises :class:`FileExistsError` when
    the file already exists — the caller decides whether to treat that as a
    stale-file recovery case (see :func:`claim_pid_file`).
    """
    target = path if path is not None else _default_pid_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(target, flags, 0o644)
    try:
        os.write(fd, f"{pid}\n".encode("utf-8"))
    finally:
        os.close(fd)


def read_pid_file(*, path: Path | None = None) -> int | None:
    """Return the integer PID stored in the file, or ``None`` if absent.

    A file containing whitespace or a non-integer is treated as missing — the
    caller will typically remove and recreate it via :func:`claim_pid_file`.
    """
    target = path if path is not None else _default_pid_file()
    try:
        contents = target.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not contents:
        return None
    try:
        return int(contents)
    except ValueError:
        return None


def remove_pid_file(*, path: Path | None = None) -> None:
    """Delete the PID file if it exists. Silent on missing file."""
    target = path if path is not None else _default_pid_file()
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def is_alive(pid: int) -> bool:
    """Return ``True`` if the given PID corresponds to a running process.

    Implementation uses ``os.kill(pid, 0)`` — sending the null signal performs
    permission/existence checks without delivering any signal.

    - No exception → the process exists and we have permission to signal it.
    - :class:`ProcessLookupError` → no such process.
    - :class:`PermissionError` → the process exists but is owned by another
      user; we treat that as "alive" since the slot is taken.
    - Other ``OSError`` subclasses are re-raised so genuine bugs surface.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim_pid_file(pid: int, *, path: Path | None = None) -> None:
    """High-level helper used by ``record start``.

    Tries an atomic create. On collision, inspects the existing PID:

    - If alive → raise :class:`CaptureAlreadyRunning`.
    - If dead/stale → remove the file and retry the atomic create exactly once.
    - If the existing file is unreadable / contains a bad PID → treat as stale.

    A second collision after stale cleanup propagates the underlying
    :class:`FileExistsError` — that's a real race we don't want to silently
    paper over.
    """
    target = path if path is not None else _default_pid_file()
    try:
        create_pid_file(pid, path=target)
        return
    except FileExistsError:
        existing = read_pid_file(path=target)
        if existing is not None and is_alive(existing):
            raise CaptureAlreadyRunning(existing) from None
        # Stale or unreadable file — clear it and retry once.
        remove_pid_file(path=target)
        create_pid_file(pid, path=target)


# ---------------------------------------------------------------------------
# capture-state.json
# ---------------------------------------------------------------------------


def read_state(*, path: Path | None = None) -> dict[str, Any] | None:
    """Return the parsed state dict, or ``None`` if missing or unreadable.

    "Unreadable" includes truncated/partial JSON — we never want a half-written
    state file to crash ``record stop``.
    """
    target = path if path is not None else _default_state_file()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def write_state(state: dict[str, Any], *, path: Path | None = None) -> None:
    """Atomically write ``state`` as JSON to the state file.

    Implementation: serialize to a sibling temp file then ``os.replace`` it
    onto the destination. ``os.replace`` is atomic on POSIX when source and
    destination live on the same filesystem, which is guaranteed here because
    we deliberately put the temp file next to the target.
    """
    target = path if path is not None else _default_state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    # Use low-level open so we can fsync before rename.
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)


def remove_state(*, path: Path | None = None) -> None:
    """Delete the state file if it exists. Silent on missing file."""
    target = path if path is not None else _default_state_file()
    try:
        target.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "CaptureAlreadyRunning",
    "create_pid_file",
    "read_pid_file",
    "remove_pid_file",
    "is_alive",
    "claim_pid_file",
    "read_state",
    "write_state",
    "remove_state",
]
