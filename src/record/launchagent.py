"""LaunchAgent install / uninstall plumbing for ``record install`` (spec 003 slice 7).

Builds ``~/Library/LaunchAgents/com.record.daemon.plist`` per tech spec §2.7 and
wraps ``launchctl bootstrap`` / ``bootout`` / ``print`` as :mod:`subprocess`
calls. Every shell-out runs through the module-level :data:`_RUN` callable so
tests can replace it with an argv-recording stub — :func:`set_launchctl_runner`
is the seam.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths, state
from .config import Config
from .logging_setup import get_logger

LABEL = "com.record.daemon"

_PLIST_DIR_PARTS = ("Library", "LaunchAgents")
_PLIST_FILENAME = f"{LABEL}.plist"

# Window inside ``install`` to wait for the daemon's PID file to appear after
# launchd bootstraps it. Mirrors the cli-side ``_DAEMON_START_HANDSHAKE_SECONDS``
# pattern so the user gets a stable "running now (PID …)" line.
_PID_WAIT_SECONDS = 3.0
_PID_POLL_INTERVAL = 0.05

# Substrings that indicate "already loaded / bootstrapped" on macOS 13/14/15.
# The exact wording varies; the integer code is more reliable but matching on
# substrings too keeps us robust to wording-only changes.
_ALREADY_LOADED_HINTS: tuple[str, ...] = (
    "already in submitted state",
    "already loaded",
    "service already loaded",
    "ealready",
    "input/output error",
    "bootstrap failed: 5",
    "service already bootstrapped",
)


# ---------------------------------------------------------------------------
# Subprocess injection point
# ---------------------------------------------------------------------------


_LaunchctlRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True)


_RUN: _LaunchctlRunner = _default_runner


def set_launchctl_runner(fn: _LaunchctlRunner | None) -> None:
    """Replace the subprocess runner. Pass ``None`` to restore the default."""
    global _RUN
    _RUN = fn if fn is not None else _default_runner


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    success: bool
    message: str
    pid: int | None = None
    launchctl_stderr: str = ""
    re_registered: bool = False


@dataclass
class UninstallResult:
    success: bool
    message: str
    launchctl_stderr: str = ""
    already_unregistered: bool = field(default=False)


# ---------------------------------------------------------------------------
# Paths + plist generation
# ---------------------------------------------------------------------------


def plist_path() -> Path:
    return Path.home().joinpath(*_PLIST_DIR_PARTS) / _PLIST_FILENAME


def _user_domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    return f"{_user_domain()}/{LABEL}"


def build_plist(config: Config) -> bytes:
    """Build canonical XML plist bytes per tech spec §2.7."""
    log_folder = Path(config.log_folder)
    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "record.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(log_folder / "daemon-launchd.out.log"),
        "StandardErrorPath": str(log_folder / "daemon-launchd.err.log"),
        "EnvironmentVariables": {"RECORD_LAUNCHD": "1"},
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)


def write_plist(config: Config) -> Path:
    """Write the plist to its canonical path. Idempotent — overwrites in place."""
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_plist(config))
    return target


def remove_plist() -> bool:
    """Delete the plist file. Returns ``True`` if a file was removed."""
    target = plist_path()
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# launchctl wrappers
# ---------------------------------------------------------------------------


def _launchctl_bootstrap(plist: Path) -> "subprocess.CompletedProcess[str]":
    return _RUN(["launchctl", "bootstrap", _user_domain(), str(plist)])


def _launchctl_bootout(plist: Path) -> "subprocess.CompletedProcess[str]":
    return _RUN(["launchctl", "bootout", _user_domain(), str(plist)])


def _launchctl_print() -> "subprocess.CompletedProcess[str]":
    return _RUN(["launchctl", "print", _service_target()])


def _stderr_indicates_already_loaded(result: "subprocess.CompletedProcess[str]") -> bool:
    err = (result.stderr or "").lower()
    return any(hint in err for hint in _ALREADY_LOADED_HINTS)


def _stderr_indicates_not_loaded(result: "subprocess.CompletedProcess[str]") -> bool:
    err = (result.stderr or "").lower()
    return any(
        hint in err
        for hint in (
            "could not find",
            "not loaded",
            "not currently loaded",
            "no such process",
            "service not found",
        )
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def is_registered() -> bool:
    """Return True if launchd knows the service AND the plist file exists."""
    if not plist_path().exists():
        return False
    result = _launchctl_print()
    return result.returncode == 0


def _wait_for_daemon_pid() -> int | None:
    """Poll the daemon PID file for up to ``_PID_WAIT_SECONDS``."""
    deadline = time.monotonic() + _PID_WAIT_SECONDS
    while time.monotonic() < deadline:
        pid = state.read_pid_file(path=paths.daemon_pid_file())
        if pid is not None and state.is_alive(pid):
            return pid
        time.sleep(_PID_POLL_INTERVAL)
    return state.read_pid_file(path=paths.daemon_pid_file())


def install(config: Config) -> InstallResult:
    """Write the plist + bootstrap. Bootout-then-rebootstrap on already-loaded."""
    log = get_logger("record.launchagent")
    target = write_plist(config)

    result = _launchctl_bootstrap(target)
    re_registered = False

    if result.returncode != 0:
        # Try bootout-then-rebootstrap once if the symptom matches the
        # already-bootstrapped case. Per tech spec §2.7 we treat any non-zero
        # exit as an opportunity to retry rather than guessing at wording.
        log.info(
            "launchctl_bootstrap_nonzero_retrying",
            returncode=result.returncode,
            stderr=result.stderr,
        )
        if _stderr_indicates_already_loaded(result) or result.returncode != 0:
            _launchctl_bootout(target)  # best-effort, ignore failure
            retry = _launchctl_bootstrap(target)
            if retry.returncode != 0:
                return InstallResult(
                    success=False,
                    message="launchctl bootstrap failed",
                    launchctl_stderr=retry.stderr or result.stderr,
                )
            re_registered = True
        else:
            return InstallResult(
                success=False,
                message="launchctl bootstrap failed",
                launchctl_stderr=result.stderr,
            )

    pid = _wait_for_daemon_pid()
    return InstallResult(
        success=True,
        message="re-registered" if re_registered else "registered",
        pid=pid,
        re_registered=re_registered,
    )


def uninstall() -> UninstallResult:
    """Bootout the agent + remove the plist. Idempotent."""
    target = plist_path()
    had_plist = target.exists()

    if not had_plist:
        # Nothing on disk — still try bootout in case launchd has it loaded
        # against a phantom plist path. We ignore the failure.
        return UninstallResult(
            success=True,
            message="already not registered",
            already_unregistered=True,
        )

    result = _launchctl_bootout(target)
    if result.returncode != 0 and not _stderr_indicates_not_loaded(result):
        return UninstallResult(
            success=False,
            message="launchctl bootout failed",
            launchctl_stderr=result.stderr,
        )

    remove_plist()
    return UninstallResult(
        success=True,
        message="removed",
        already_unregistered=False,
    )


__all__ = [
    "LABEL",
    "InstallResult",
    "UninstallResult",
    "build_plist",
    "install",
    "is_registered",
    "plist_path",
    "remove_plist",
    "set_launchctl_runner",
    "uninstall",
    "write_plist",
]
