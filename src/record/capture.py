"""Per-capture orchestration extracted from ``supervisor.py``.

Slice 2 of spec 003 extracted per-capture lifecycle into :class:`CaptureSession`
so the daemon and the legacy ``python -m record.supervisor`` entrypoint could
share one code path. Slice 4 splits that further: the subprocess work (spawn /
stderr drain / stdout reader / lifetime management) moves into
:class:`SwiftChild`, and :class:`CaptureSession` becomes a pure orchestration
object that operates over an *injected* shared child surface.

Why this shape:

- In slice-2 the Swift child was one-shot — spawn, run one capture, exit. The
  session naturally owned the subprocess.
- In slice-4 the daemon spawns ONE long-lived ``record-capture --daemon`` and
  drives many start/stop cycles through it. The session can't own the
  subprocess any more; it has to be threaded through.
- The legacy supervisor path still wants a single-capture lifecycle. So
  :class:`SwiftChild` takes a ``daemon: bool`` flag — ``daemon=True`` keeps the
  process alive across many ``run_capture`` calls, ``daemon=False`` lets the
  Swift binary ``exit(0)`` after the first ``stopped`` (matching the legacy
  one-shot contract).

Test seam: ``RECORD_CAPTURE_TEST_FLAGS`` (space-separated argv tokens) is
appended to the Swift binary's argv when set. The integration suite uses this
to pipe through ``--test-silent-sources --test-synthetic-video`` without
production code growing a "test mode" branch.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from . import ipc, state
from .logging_setup import get_logger

# Test-only env var: when set, its value is parsed via ``shlex.split`` and
# appended to the Swift binary's argv on spawn. Production code never sets
# this; integration tests do (see tests/integration/test_end_to_end.py).
_TEST_FLAGS_ENV = "RECORD_CAPTURE_TEST_FLAGS"

# Bounded restart loop for the long-lived Swift child (tech spec §3 risk row 1).
# More than this many unexpected exits inside the rolling window → give up and
# surface a permanent failure on subsequent ``run_capture`` calls.
_RESTART_LIMIT = 3
_RESTART_WINDOW_SECONDS = 60.0

# How long to wait for the Swift child's ``ready`` event after spawn before
# treating the spawn as a failure.
_READY_TIMEOUT_SECONDS = 10.0

# How long to wait for the Swift child to exit cleanly after ``shutdown`` /
# stdin EOF before escalating to SIGKILL.
_SHUTDOWN_TIMEOUT_SECONDS = 5.0

_log = get_logger("record.capture")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return the current UTC time as a Z-suffixed ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_binary() -> Path | None:
    """Resolve the bundled Swift binary's path on disk.

    Returns ``None`` if the binary is missing or not executable. Mirrors
    :func:`record.cli._resolve_capture_binary` — kept separate so the two
    layers can diverge (e.g. test-seam search paths) without surprising each
    other.
    """
    try:
        # The binary ships inside a .app bundle so macOS TCC attributes its
        # permission requests to the bundle's own identity (com.record.capture)
        # rather than walking up to the parent python3 process, which carries
        # no Info.plist / NSMicrophoneUsageDescription.
        resource = (
            files("record")
            / "bin"
            / "record-capture.app"
            / "Contents"
            / "MacOS"
            / "record-capture"
        )
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        with as_file(resource) as path:
            target = Path(path)
            if not target.exists() or not os.access(target, os.X_OK):
                return None
            return target
    except (FileNotFoundError, ModuleNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Disclaiming spawn — macOS TCC "responsible process" handling
# ---------------------------------------------------------------------------
#
# A plain ``subprocess.Popen`` leaves the Swift child's TCC *responsible
# process* as this Python orchestrator (and, when autostarted, launchd).
# macOS then consults *that* process's bundle for permission grants — and the
# uv-managed standalone python has no Info.plist at all, so the capture
# binary's own ``com.record.capture`` grants are never seen. The symptom: the
# microphone prompt fast-fails for the launchd-spawned daemon even though the
# grant exists.
#
# ``posix_spawn`` with ``responsibility_spawnattrs_setdisclaim`` makes the
# spawned child its *own* responsible process, so TCC keys permission grants
# on the capture binary's bundle identity regardless of who launched the
# daemon. This is the only fix that works without a full Developer ID
# signature (which the project defers).

# macOS <spawn.h>: spawn the child into a new session (replaces Popen's
# start_new_session=True).
_POSIX_SPAWN_SETSID = 0x0400


class _DisclaimUnavailable(RuntimeError):
    """Raised when disclaiming spawn is not available (non-macOS, or the
    private ``responsibility_spawnattrs_setdisclaim`` symbol is missing). The
    caller falls back to a plain ``subprocess.Popen``."""


def _exit_status_to_returncode(status: int) -> int:
    """Map a raw ``os.waitpid`` status to a Popen-style return code."""
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return -1


def _posix_spawn_disclaimed(
    argv: list[str], *, stdin_fd: int, stdout_fd: int, stderr_fd: int
) -> int:
    """``posix_spawn`` ``argv`` with the responsibility-disclaim attribute set.

    ``stdin_fd`` / ``stdout_fd`` / ``stderr_fd`` are the child-side pipe fds
    to wire onto descriptors 0/1/2. Returns the child pid. Raises
    :class:`_DisclaimUnavailable` when the platform/symbol can't support it,
    or :class:`OSError` on a genuine spawn failure.
    """
    if sys.platform != "darwin":
        raise _DisclaimUnavailable("disclaiming spawn is macOS-only")

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        _disclaim = libc.responsibility_spawnattrs_setdisclaim
    except AttributeError as exc:
        raise _DisclaimUnavailable(
            "responsibility_spawnattrs_setdisclaim not available"
        ) from exc

    _voidpp = ctypes.POINTER(ctypes.c_void_p)
    libc.posix_spawn_file_actions_init.argtypes = [_voidpp]
    libc.posix_spawn_file_actions_adddup2.argtypes = [
        _voidpp, ctypes.c_int, ctypes.c_int
    ]
    libc.posix_spawn_file_actions_destroy.argtypes = [_voidpp]
    libc.posix_spawnattr_init.argtypes = [_voidpp]
    libc.posix_spawnattr_setflags.argtypes = [_voidpp, ctypes.c_short]
    libc.posix_spawnattr_destroy.argtypes = [_voidpp]
    _disclaim.argtypes = [_voidpp, ctypes.c_int]
    libc.posix_spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        _voidpp,
        _voidpp,
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]

    file_actions = ctypes.c_void_p()
    attr = ctypes.c_void_p()
    libc.posix_spawn_file_actions_init(ctypes.byref(file_actions))
    libc.posix_spawnattr_init(ctypes.byref(attr))
    try:
        for src, dst in ((stdin_fd, 0), (stdout_fd, 1), (stderr_fd, 2)):
            libc.posix_spawn_file_actions_adddup2(
                ctypes.byref(file_actions), src, dst
            )
        # Make the child its own TCC responsible process, and give it its own
        # session (parity with the previous Popen start_new_session=True).
        _disclaim(ctypes.byref(attr), 1)
        libc.posix_spawnattr_setflags(
            ctypes.byref(attr), _POSIX_SPAWN_SETSID
        )

        argv_c = (ctypes.c_char_p * (len(argv) + 1))()
        for i, a in enumerate(argv):
            argv_c[i] = a.encode()
        env_items = [f"{k}={v}".encode() for k, v in os.environ.items()]
        envp_c = (ctypes.c_char_p * (len(env_items) + 1))()
        for i, e in enumerate(env_items):
            envp_c[i] = e

        pid = ctypes.c_int()
        rc = libc.posix_spawn(
            ctypes.byref(pid),
            argv[0].encode(),
            ctypes.byref(file_actions),
            ctypes.byref(attr),
            argv_c,
            envp_c,
        )
    finally:
        libc.posix_spawn_file_actions_destroy(ctypes.byref(file_actions))
        libc.posix_spawnattr_destroy(ctypes.byref(attr))

    if rc != 0:
        raise OSError(rc, os.strerror(rc), argv[0])
    return pid.value


class _DisclaimedChild:
    """A ``subprocess.Popen``-compatible handle for a child spawned via
    :func:`_posix_spawn_disclaimed`.

    Implements only the surface :class:`SwiftChild` uses: text-mode
    ``stdin`` / ``stdout`` / ``stderr`` streams, ``poll()``, ``wait()``,
    ``send_signal()`` / ``terminate()`` / ``kill()``, ``pid``, ``returncode``.
    """

    def __init__(self, argv: list[str]) -> None:
        # stdin: parent writes stdin_w, child reads stdin_r. stdout/stderr:
        # child writes *_w, parent reads *_r.
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        try:
            pid = _posix_spawn_disclaimed(
                argv, stdin_fd=stdin_r, stdout_fd=stdout_w, stderr_fd=stderr_w
            )
        except BaseException:
            for fd in (
                stdin_r, stdin_w, stdout_r, stdout_w, stderr_r, stderr_w
            ):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

        # Child-side ends now live in the child; close our copies so the read
        # pipes see EOF when the child exits.
        os.close(stdin_r)
        os.close(stdout_w)
        os.close(stderr_w)

        self.args: list[str] = list(argv)
        self.pid: int = pid
        self.returncode: int | None = None
        # Line-buffered text streams — parity with Popen(text=True, bufsize=1).
        self.stdin = os.fdopen(stdin_w, "w", buffering=1, encoding="utf-8")
        self.stdout = os.fdopen(stdout_r, "r", buffering=1, encoding="utf-8")
        self.stderr = os.fdopen(stderr_r, "r", buffering=1, encoding="utf-8")

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # Already reaped — shouldn't happen (we are the sole owner), but
            # don't wedge the caller if it does.
            self.returncode = -1
            return self.returncode
        if wpid == 0:
            return None
        self.returncode = _exit_status_to_returncode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            _, status = os.waitpid(self.pid, 0)
            self.returncode = _exit_status_to_returncode(status)
            return self.returncode
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc = self.poll()
            if rc is not None:
                return rc
            time.sleep(0.02)
        raise subprocess.TimeoutExpired(self.args, timeout)

    def send_signal(self, sig: int) -> None:
        if self.returncode is not None:
            return
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            pass

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


# A running Swift child is either our disclaiming wrapper (the macOS norm) or a
# plain Popen (the fallback path). Both are duck-type compatible for the
# surface SwiftChild uses.
_SwiftProc = subprocess.Popen[str] | _DisclaimedChild


def _spawn_swift_child(argv: list[str]) -> _SwiftProc:
    """Spawn the Swift capture binary as its own TCC responsible process.

    Uses :class:`_DisclaimedChild` (``posix_spawn`` + disclaim) on macOS; falls
    back to a plain ``subprocess.Popen`` when disclaiming spawn is unavailable
    — degraded (TCC may misattribute) but never worse than the prior behavior.
    """
    try:
        return _DisclaimedChild(argv)
    except _DisclaimUnavailable:
        _log.warning("disclaim_spawn_unavailable_falling_back")
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )


def _drain_stderr(proc: _SwiftProc, log_path: Path) -> None:
    """Append the binary's stderr line-by-line to the daemon log file.

    Runs in a background thread for the lifetime of the subprocess. IO errors
    are downgraded to a structlog warning — they must not terminate the
    capture.
    """
    assert proc.stderr is not None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8") as fp:
            for line in proc.stderr:
                fp.write(line if line.endswith("\n") else line + "\n")
                fp.flush()
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("daemon_stderr_drain_failed", error=str(exc))


# ---------------------------------------------------------------------------
# State translation (previously in supervisor.py)
# ---------------------------------------------------------------------------


def _initial_state(supervisor_pid: int) -> dict[str, Any]:
    """Build the state dict written when the binary first emits ``ready``."""
    return {
        "pid": supervisor_pid,
        "start_time": None,
        "basename": None,
        "video_output_path": None,
        "audio_files": {
            "mic": {
                "path": None,
                "status": "pending",
                "duration_seconds": None,
                "truncated_at_offset_seconds": None,
            },
            "system_audio": {
                "path": None,
                "status": "pending",
                "duration_seconds": None,
                "truncated_at_offset_seconds": None,
            },
        },
        "sources": {
            "mic": {
                "status": "never_attached",
                "attached_at": None,
                "lost_at": None,
            },
            "system_audio": {
                "status": "never_attached",
                "attached_at": None,
                "lost_at": None,
            },
            "video": {
                "status": "never_attached",
                "attached_at": None,
                "lost_at": None,
                "display_id": None,
                "width_px": None,
                "height_px": None,
                "fps": None,
            },
        },
        "warnings": [],
        "display_changes": [],
        "ended_by": None,
        "last_event_at": _utcnow_iso(),
        "final": False,
    }


def _apply_event(current: dict[str, Any], event: ipc.Event) -> dict[str, Any]:
    """Return an updated state dict reflecting ``event``.

    Pure-ish: only depends on its inputs and the current wall clock. The
    caller is responsible for persisting the result.
    """
    now = _utcnow_iso()
    current["last_event_at"] = now

    if isinstance(event, ipc.ReadyEvent):
        return current

    if isinstance(event, ipc.StartedEvent):
        current["start_time"] = event.start_time
        return current

    if isinstance(event, ipc.SourceAttachedEvent):
        src = current["sources"][event.source]
        src["status"] = "attached"
        src["attached_at"] = now
        return current

    if isinstance(event, ipc.SourceLostEvent):
        src = current["sources"][event.source]
        if src.get("status") == "lost":
            return current
        src["status"] = "lost"
        src["lost_at"] = now
        current["warnings"].append(
            {
                "timestamp": now,
                "source": event.source,
                "message": event.reason,
                "at_offset_seconds": event.at_offset_seconds,
            }
        )
        return current

    if isinstance(event, ipc.PermissionRequiredEvent):
        return current

    if isinstance(event, ipc.PermissionDeniedEvent):
        current["permission_denied"] = event.kind
        current["warnings"].append(
            {
                "timestamp": now,
                "source": None,
                "message": f"permission denied: {event.kind}",
            }
        )
        return current

    if isinstance(event, ipc.ErrorEvent):
        current["warnings"].append(
            {
                "timestamp": now,
                "source": None,
                "message": event.message,
            }
        )
        return current

    if isinstance(event, ipc.StoppedEvent):
        current["stopped_at"] = now
        current["duration_seconds"] = event.duration_seconds
        current["basename"] = event.basename
        return current

    if isinstance(event, ipc.VideoStartedEvent):
        video = current["sources"]["video"]
        video["status"] = "attached"
        video["attached_at"] = now
        video["display_id"] = event.display_id
        video["width_px"] = event.width_px
        video["height_px"] = event.height_px
        video["fps"] = event.fps
        return current

    if isinstance(event, ipc.VideoLostEvent):
        video = current["sources"]["video"]
        if video.get("status") == "lost":
            return current
        video["status"] = "lost"
        video["lost_at"] = now
        current["warnings"].append(
            {
                "timestamp": now,
                "source": "video",
                "message": event.reason,
                "at_offset_seconds": event.at_offset_seconds,
            }
        )
        # Offset 0 = no frame ever made it to disk (typically permission
        # denial). Clear the configured path so the stop summary won't dangle
        # a phantom file name. Mid-capture losses keep the path: the partial
        # mp4 was finalized by the binary up to the failure point.
        if event.at_offset_seconds == 0:
            current["video_output_path"] = None
        return current

    if isinstance(event, ipc.VideoFileEvent):
        current["video_output_path"] = event.path
        current["video_file_duration_seconds"] = event.duration_seconds
        return current

    if isinstance(event, ipc.AudioFileEvent):
        entry = current["audio_files"][event.source]
        entry["path"] = event.path
        entry["status"] = event.status
        entry["duration_seconds"] = event.duration_seconds
        entry["truncated_at_offset_seconds"] = event.truncated_at_offset_seconds
        return current

    if isinstance(event, ipc.DisplayReconfiguredEvent):
        current["display_changes"].append(
            {
                "timestamp": now,
                "reason": event.reason,
                "new_display_id": event.new_display_id,
                "new_width_px": event.new_width_px,
                "new_height_px": event.new_height_px,
            }
        )
        return current

    if isinstance(event, ipc.CaptureEndedBySystemEventEvent):
        current["ended_by"] = event.reason
        return current

    return current


def _log_event(event: ipc.Event) -> None:
    """Emit a structlog line for ``event``."""
    name = event.event
    payload = event.model_dump(exclude={"event"})
    if isinstance(event, (ipc.SourceLostEvent, ipc.ErrorEvent, ipc.PermissionDeniedEvent)):
        _log.warning(name, **payload)
    else:
        _log.info(name, **payload)


def _send_command(proc: _SwiftProc, cmd: ipc.Command) -> None:
    """Write a single command line to the binary's stdin and flush."""
    assert proc.stdin is not None
    line = ipc.serialize_command(cmd) + "\n"
    try:
        proc.stdin.write(line)
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        _log.warning("command_send_failed", cmd=cmd.cmd, error=str(exc))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CaptureFailedToStart(RuntimeError):
    """Raised by :meth:`CaptureSession.start` when the capture never reaches RUNNING.

    Carries the final state dict (with ``final=True`` already set) so the
    caller can surface details — typically used by the daemon to translate a
    failed-to-start into a clean ``status="error"`` socket reply.
    """

    def __init__(self, message: str, *, final_state: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.final_state = final_state or {}


class SwiftChildUnavailable(RuntimeError):
    """Raised by :class:`SwiftChild` when the subprocess cannot be reached.

    Covers two cases:
      - the binary is missing / not executable (hard startup failure);
      - the bounded restart loop has exceeded its budget (the child has died
        too many times in the rolling window and we've given up).
    """


# ---------------------------------------------------------------------------
# SwiftChild
# ---------------------------------------------------------------------------


class SwiftChild:
    """Owner of one ``record-capture`` subprocess.

    In ``daemon=True`` mode (the spec-003-slice-4 daemon path) the child is
    long-lived: spawn once at :meth:`start`, then service many
    :meth:`run_capture` calls before :meth:`shutdown`. In ``daemon=False``
    mode (the legacy ``python -m record.supervisor`` path) the child exits
    after the first ``stopped`` event and :meth:`run_capture` returns once
    the binary has wound down.

    A single stdout-reader task fans out events to whichever
    :class:`CaptureSession` is currently active. A single stderr-drain
    thread appends to ``daemon_log_path`` for the subprocess's lifetime.

    Concurrency:
      - :meth:`run_capture` is **not** safe to call concurrently with itself.
        The daemon's state-machine lock already serializes captures; we
        depend on that invariant.
      - :meth:`shutdown` is idempotent.
    """

    def __init__(
        self,
        *,
        daemon_log_path: Path,
        daemon: bool = True,
    ) -> None:
        self._daemon_log_path = daemon_log_path
        self._daemon_mode = daemon

        self._proc: _SwiftProc | None = None
        self._stderr_thread: threading.Thread | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._watcher_task: asyncio.Task[None] | None = None

        # The currently active CaptureSession, if any. The reader feeds events
        # into this. A lock would be overkill: the daemon's state-machine
        # already guarantees at most one session attached at a time.
        self._active_session: "CaptureSession | None" = None

        # ``ready`` event from the Swift child; set by the reader once it
        # arrives. Tested by :meth:`start` to detect spawn failure.
        self._ready_event: asyncio.Event = asyncio.Event()

        # Set when the subprocess exits (for any reason). The session's
        # in-flight ``await started_event / stopped_event`` paths race against
        # this so they don't hang forever on a dead child.
        self._exited_event: asyncio.Event = asyncio.Event()

        # Set when ``shutdown()`` is called; tells the watcher that any
        # subsequent process exit is expected and must not trigger a respawn.
        self._shutting_down: bool = False

        # Bounded restart loop bookkeeping (tech spec §3 risk row 1).
        # Timestamps of recent unexpected exits within the rolling window.
        self._recent_exits: list[float] = []
        # Set once the restart budget is blown; subsequent run_capture calls
        # fail fast rather than respawning.
        self._permanently_failed: bool = False
        self._permanent_failure_reason: str | None = None

        # Slice-5 hotkey routing seam. The daemon installs a handler via
        # :meth:`set_hotkey_event_handler` so hotkey events are dispatched to
        # the daemon's state machine rather than the active capture session
        # (which has no concept of hotkeys).
        self._hotkey_event_handler: Callable[[ipc.Event], None] | None = None

    # ----- Public surface -------------------------------------------------

    @property
    def is_daemon_mode(self) -> bool:
        return self._daemon_mode

    @property
    def proc(self) -> _SwiftProc | None:
        return self._proc

    async def start(self) -> None:
        """Spawn the child, drain stderr, await ``ready``.

        Raises :class:`SwiftChildUnavailable` if the binary is missing or
        ``ready`` does not arrive within :data:`_READY_TIMEOUT_SECONDS`.
        """
        binary = _resolve_binary()
        if binary is None:
            _log.error("capture_binary_missing")
            self._permanently_failed = True
            self._permanent_failure_reason = (
                "capture binary missing or not executable"
            )
            raise SwiftChildUnavailable(
                "capture binary missing or not executable"
            )

        await self._spawn(binary)

    async def run_capture(self, session: "CaptureSession") -> None:
        """Attach ``session`` as the active sink and drive one capture cycle.

        Sends ``start`` derived from the session's parameters, then awaits
        either the session's ``stopped_event`` or the child exiting. Returns
        once the session has been finalized (the caller is expected to call
        :meth:`CaptureSession.stop` themselves to send the ``stop`` command
        and persist the final state).

        On unexpected child death in daemon mode, the bounded restart loop
        kicks in before the next call to :meth:`run_capture`. In one-shot
        mode the child is expected to exit after ``stopped``; no respawn.
        """
        if self._permanently_failed:
            raise SwiftChildUnavailable(
                self._permanent_failure_reason or "swift child unavailable"
            )

        # In daemon mode: if the child died between captures, eagerly respawn
        # before we attach the session. (Eager respawn is the simpler design
        # — the alternative would be a background task that races against
        # _exited_event; this is the same effect with one less moving part
        # in the happy path.)
        if self._daemon_mode and self._proc is not None and self._proc.poll() is not None:
            await self._maybe_restart_after_unexpected_exit()
            if self._permanently_failed:
                raise SwiftChildUnavailable(
                    self._permanent_failure_reason or "swift child unavailable"
                )

        if self._proc is None or self._proc.poll() is not None:
            raise SwiftChildUnavailable("swift child is not running")

        self._active_session = session
        session._attach_to_child(self)

        # Send start now. The session's start() awaits started_event (set by
        # the reader when ``started`` lands); stop() awaits stopped_event.
        # In daemon mode the child stays alive after stopped, so we just
        # detach. In one-shot mode the child will EOF on stdout and the
        # reader will exit naturally; the session's stop() waits for the
        # reader to drain.

    def detach_session(self, session: "CaptureSession") -> None:
        """Drop the session reference once it has been finalized.

        Called by :meth:`CaptureSession.stop` at the tail of finalization.
        Idempotent: a no-op if the session isn't the active one (e.g. the
        child died and a different session was attached in the meantime —
        not possible today, but cheap defense in depth).
        """
        if self._active_session is session:
            self._active_session = None

    def set_hotkey_event_handler(
        self, handler: Callable[[ipc.Event], None] | None
    ) -> None:
        """Install (or clear) the hotkey-event dispatch handler.

        Slice-5 routing: hotkey-related events (``hotkey_registered``,
        ``hotkey_pressed``, ``hotkey_unregistered``) are daemon-level signals
        — they have nothing to do with the currently active capture session.
        The daemon installs its ``_on_hotkey_event`` method here; when ``None``
        is passed the handler is detached (the reader logs at ``debug`` and
        drops the event).

        Idempotent: the most recent installer wins.
        """
        self._hotkey_event_handler = handler

    def send_command(self, cmd: ipc.Command) -> None:
        """Forward ``cmd`` to the Swift child's stdin."""
        if self._proc is None or self._proc.poll() is not None:
            _log.warning("command_send_skipped_no_child", cmd=cmd.cmd)
            return
        _send_command(self._proc, cmd)

    async def shutdown(self) -> None:
        """Send ``shutdown`` and wait for the subprocess to exit.

        Idempotent. Joins the stderr-drain thread on the way out. Falls back
        to SIGTERM + SIGKILL if the binary doesn't respond to the command.
        """
        self._shutting_down = True

        proc = self._proc
        if proc is None:
            return

        if proc.poll() is None:
            # Best-effort: send the explicit shutdown command first so the
            # binary can drain its dispatch queue and finalize cleanly.
            try:
                _send_command(proc, ipc.ShutdownCommand())
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning("shutdown_send_failed", error=str(exc))

            # Then close stdin so the Swift stdin thread sees EOF and exits
            # even if the shutdown command dispatch lost a race.
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:  # pragma: no cover - defensive
                pass

            await self._wait_proc(proc, timeout=_SHUTDOWN_TIMEOUT_SECONDS)

            if proc.poll() is None:
                _log.warning("swift_child_did_not_exit_terminating")
                try:
                    proc.terminate()
                except Exception:  # pragma: no cover - defensive
                    pass
                await self._wait_proc(proc, timeout=2.0)

            if proc.poll() is None:  # pragma: no cover - defensive
                _log.warning("swift_child_did_not_exit_killing")
                try:
                    proc.kill()
                except Exception:
                    pass
                await self._wait_proc(proc, timeout=2.0)

        # Wait for the reader to drain. It exits when stdout closes (EOF).
        if self._reader_task is not None:
            try:
                await self._reader_task
            except asyncio.CancelledError:  # pragma: no cover - defensive
                pass

        # Cancel the unexpected-exit watcher (if running) — we already know
        # the process is gone, and we already set _shutting_down so it would
        # noop anyway.
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except (asyncio.CancelledError, Exception):
                pass

        # Join stderr thread (daemon=True; will exit on its own when the
        # pipe closes).
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)

    # ----- Internals ------------------------------------------------------

    async def _spawn(self, binary: Path) -> None:
        argv: list[str] = [str(binary)]
        if self._daemon_mode:
            argv.append("--daemon")
        extra = os.environ.get(_TEST_FLAGS_ENV, "")
        if extra:
            argv.extend(shlex.split(extra))

        _log.info(
            "swift_child_spawning",
            binary=str(binary),
            daemon_mode=self._daemon_mode,
            extra_argv=argv[1:],
        )

        proc = _spawn_swift_child(argv)
        self._proc = proc

        # Fresh per-spawn events. The session's reader-task races against
        # ``_exited_event``; reusing a set event from a previous spawn would
        # immediately fail the next ``start``.
        self._ready_event = asyncio.Event()
        self._exited_event = asyncio.Event()

        # Background stderr drainer → daemon.log.
        self._stderr_thread = threading.Thread(
            target=_drain_stderr,
            args=(proc, self._daemon_log_path),
            name="record-capture-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        # Background reader task.
        self._reader_task = asyncio.create_task(
            self._read_events(), name="record-capture-reader"
        )

        # Wait for ``ready`` (or process death within the timeout).
        try:
            await asyncio.wait_for(
                self._await_ready_or_exit(), timeout=_READY_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            _log.error("swift_child_ready_timeout")
            try:
                proc.kill()
            except Exception:  # pragma: no cover - defensive
                pass
            self._permanently_failed = True
            self._permanent_failure_reason = (
                f"swift child did not emit ready within {_READY_TIMEOUT_SECONDS}s"
            )
            raise SwiftChildUnavailable(
                self._permanent_failure_reason
            ) from exc

        if not self._ready_event.is_set():
            # Process exited before emitting ready.
            self._permanently_failed = True
            self._permanent_failure_reason = (
                "swift child exited before emitting ready"
            )
            raise SwiftChildUnavailable(self._permanent_failure_reason)

        # In daemon mode, start a background watcher that tracks unexpected
        # exits. In one-shot mode the exit is expected (after stopped) so the
        # session handles it.
        if self._daemon_mode:
            self._watcher_task = asyncio.create_task(
                self._watch_for_unexpected_exit(),
                name="record-swift-child-watcher",
            )

    async def _await_ready_or_exit(self) -> None:
        """Race the ``ready`` event against the process exiting."""
        ready_wait = asyncio.create_task(self._ready_event.wait())
        exit_wait = asyncio.create_task(self._exited_event.wait())
        try:
            await asyncio.wait(
                {ready_wait, exit_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (ready_wait, exit_wait):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _watch_for_unexpected_exit(self) -> None:
        """Set exited_event whenever the child exits.

        The actual respawn happens lazily in :meth:`run_capture` — that
        keeps the recovery logic simple (no race between watcher and a
        concurrent ``run_capture``). The watcher's only job is to surface
        the exit to any in-flight session that's awaiting events.
        """
        try:
            await self._exited_event.wait()
        except asyncio.CancelledError:  # pragma: no cover - defensive
            return

        if self._shutting_down:
            return

        # If a session is currently attached, signal it so its waits can
        # break. The session's stop() / start() logic checks
        # _child_exited_event and finalizes abnormally.
        session = self._active_session
        if session is not None:
            session._on_child_exited_unexpectedly()

    async def _maybe_restart_after_unexpected_exit(self) -> None:
        """Respawn the Swift child if the budget allows.

        Called lazily by :meth:`run_capture` when it observes a dead process
        in daemon mode. Updates the restart bookkeeping and surfaces a
        permanent failure if the budget is blown.
        """
        if self._shutting_down:
            # Daemon is winding down; don't fight it.
            self._permanently_failed = True
            self._permanent_failure_reason = "swift child shut down"
            return

        now = time.monotonic()
        # Drop exits outside the rolling window.
        self._recent_exits = [
            t for t in self._recent_exits if now - t < _RESTART_WINDOW_SECONDS
        ]
        self._recent_exits.append(now)

        if len(self._recent_exits) > _RESTART_LIMIT:
            self._permanently_failed = True
            self._permanent_failure_reason = (
                f"swift child exited {_RESTART_LIMIT} times within "
                f"{_RESTART_WINDOW_SECONDS:.0f}s — giving up"
            )
            _log.error(
                "swift_child_restart_budget_exhausted",
                limit=_RESTART_LIMIT,
                window_seconds=_RESTART_WINDOW_SECONDS,
            )
            return

        _log.warning(
            "swift_child_respawning",
            recent_exits=len(self._recent_exits),
            limit=_RESTART_LIMIT,
        )

        # Join the dead reader / stderr threads before spawning a new pair.
        if self._reader_task is not None and not self._reader_task.done():
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)

        binary = _resolve_binary()
        if binary is None:
            self._permanently_failed = True
            self._permanent_failure_reason = (
                "capture binary missing on respawn attempt"
            )
            return

        try:
            await self._spawn(binary)
        except SwiftChildUnavailable as exc:
            # _spawn already set _permanently_failed.
            _log.error("swift_child_respawn_failed", error=str(exc))

    async def _wait_proc(
        self, proc: _SwiftProc, *, timeout: float
    ) -> None:
        """Wait up to ``timeout`` seconds for the binary to exit."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.05)

    async def _read_events(self) -> None:
        """Drain the binary's stdout, fanning events to the active session.

        Runs as a background task for the lifetime of the subprocess. Ends
        when stdout closes (EOF), at which point :attr:`_exited_event` is
        set so anyone awaiting events can break.
        """
        proc = self._proc
        assert proc is not None and proc.stdout is not None

        loop = asyncio.get_running_loop()
        stdout = proc.stdout
        try:
            while True:
                line = await loop.run_in_executor(None, stdout.readline)
                if line == "":
                    break
                line = line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = ipc.parse_event(line)
                except ValidationError as exc:
                    _log.warning("event_parse_failed", raw=line, error=str(exc))
                    continue
                except ValueError as exc:
                    _log.warning("event_parse_failed", raw=line, error=str(exc))
                    continue

                if isinstance(event, ipc.ReadyEvent):
                    self._ready_event.set()
                    # The ready event itself doesn't go into the session —
                    # it's a child-level signal.
                    continue

                # Slice-5: hotkey events are daemon-level signals. They never
                # flow into the active session (sessions don't track hotkey
                # state). Dispatch to the daemon-installed handler if any;
                # otherwise log + drop.
                if isinstance(
                    event,
                    (
                        ipc.HotkeyRegisteredEvent,
                        ipc.HotkeyPressedEvent,
                        ipc.HotkeyUnregisteredEvent,
                    ),
                ):
                    hotkey_handler = self._hotkey_event_handler
                    if hotkey_handler is not None:
                        try:
                            hotkey_handler(event)
                        except Exception as exc:  # pragma: no cover - defensive
                            _log.warning(
                                "hotkey_handler_raised",
                                event=event.event,
                                error=str(exc),
                            )
                    else:
                        _log.debug("hotkey_event_dropped", event=event.event)
                    continue

                # Fan into the active session, if any.
                session = self._active_session
                if session is not None:
                    try:
                        session.on_event(event)
                    except Exception as exc:  # pragma: no cover - defensive
                        _log.warning(
                            "session_event_handler_raised",
                            error=str(exc),
                            event=event.event,
                        )
        finally:
            self._exited_event.set()


# ---------------------------------------------------------------------------
# CaptureSession
# ---------------------------------------------------------------------------


class CaptureSession:
    """One capture's worth of state translation and lifecycle.

    Slice 4 contract: the session no longer owns a subprocess. It is created
    fresh per capture, attached to a long-lived :class:`SwiftChild` via
    :meth:`SwiftChild.run_capture`, then drives the state machine off the
    events the child fans in via :meth:`on_event`.

    The session does **not** claim a PID file — that's the caller's job (the
    daemon owns ``daemon.pid``; the legacy supervisor owns ``capture.pid``).
    """

    def __init__(
        self,
        *,
        basename: Path,
        video_output_path: Path | None,
        sample_rate: int,
        bit_depth: int,
        channels: int,
        daemon_log_path: Path,
        owner_pid: int | None = None,
        state_file_path: Path | None = None,
        child: "SwiftChild | None" = None,
    ) -> None:
        self._basename = basename
        self._video_output_path = video_output_path
        self._sample_rate = sample_rate
        self._bit_depth = bit_depth
        self._channels = channels
        self._daemon_log_path = daemon_log_path
        self._owner_pid = owner_pid if owner_pid is not None else os.getpid()
        self._state_file_path = state_file_path

        # The Swift child surface this session sends commands through. Either
        # passed in (the daemon path) or attached at start() time (the legacy
        # supervisor path, which spins up its own one-shot SwiftChild). May be
        # None until _attach_to_child runs.
        self._child: "SwiftChild | None" = child

        self._state: dict[str, Any] = _initial_state(self._owner_pid)
        self._stop_requested = False
        self._saw_stopped = False
        self._stopped_event = asyncio.Event()
        # Set as soon as the Swift child emits ``started`` so start() can
        # await "capture is actually running" rather than racing on the
        # spawn alone.
        self._started_event = asyncio.Event()
        # Set when the SwiftChild signals an unexpected exit; races against
        # _started_event / _stopped_event so the session doesn't hang on a
        # dead child.
        self._child_exited_event = asyncio.Event()
        self._child_exited_abnormally = False
        # Set when the Swift child emits an ``error`` event before ``started``
        # — a start failure (e.g. a permission denial) that, in daemon mode,
        # leaves the child alive rather than exiting. Races against
        # _started_event so start() doesn't hang waiting for a ``started`` that
        # will never arrive.
        self._start_failed_event = asyncio.Event()
        self._start_error: str | None = None

    # ----- Public surface -------------------------------------------------

    @property
    def stopped_event(self) -> asyncio.Event:
        """Event set when the Swift child emits ``stopped`` (any reason).

        The daemon awaits this to react to a system-event-triggered shutdown
        without forcing a ``stop`` command itself.
        """
        return self._stopped_event

    @property
    def state(self) -> dict[str, Any]:
        """Current state dict (live reference — caller must not mutate)."""
        return self._state

    @property
    def basename(self) -> Path:
        return self._basename

    @property
    def video_output_path(self) -> Path | None:
        return self._video_output_path

    def on_event(self, event: ipc.Event) -> None:
        """Apply one event to the session state and persist.

        Called by :class:`SwiftChild`'s reader from the event loop thread.
        Idempotent on individual event types — the underlying ``_apply_event``
        already handles repeated lost/attached transitions.
        """
        _log_event(event)
        self._state = _apply_event(self._state, event)
        try:
            self._write_state(self._state)
        except OSError as exc:
            _log.warning("state_write_failed", error=str(exc))

        if isinstance(event, ipc.StartedEvent):
            self._started_event.set()

        if isinstance(event, ipc.StoppedEvent):
            self._saw_stopped = True
            self._stopped_event.set()

        if (
            isinstance(event, ipc.ErrorEvent)
            and not self._started_event.is_set()
            and not self._saw_stopped
        ):
            self._start_error = event.message
            self._start_failed_event.set()

    def _attach_to_child(self, child: "SwiftChild") -> None:
        """Internal: bind this session to its driving :class:`SwiftChild`.

        Called by :meth:`SwiftChild.run_capture`. The session keeps a back
        reference so :meth:`start` / :meth:`stop` can send commands.
        """
        self._child = child

    def _on_child_exited_unexpectedly(self) -> None:
        """Internal: notify the session that its child is gone.

        Called by :class:`SwiftChild`'s watcher. Sets the abort signal so
        start() / stop() don't hang forever, and marks the session abnormal.
        """
        self._child_exited_abnormally = True
        self._child_exited_event.set()
        # Unblock anyone awaiting started/stopped so they can finalize.
        self._started_event.set()
        self._stopped_event.set()

    async def start(self) -> None:
        """Send ``start`` via the attached child, await ``started``.

        Raises :class:`CaptureFailedToStart` if no child is attached, if the
        binary fails to spawn, or if the child exits before emitting
        ``started``. When no child was injected at construction time, this
        method creates a one-shot ``SwiftChild(daemon=False)`` on the fly —
        the legacy ``python -m record.supervisor`` path relies on this so it
        keeps working without managing a SwiftChild itself.
        """
        if self._child is None:
            # Legacy supervisor path: spin up a one-shot SwiftChild on the
            # fly. The supervisor's main() could do this itself, but keeping
            # the fallback inside the session means existing test paths
            # ("create a session and call start()") keep working unchanged.
            child = SwiftChild(
                daemon_log_path=self._daemon_log_path,
                daemon=False,
            )
            try:
                await child.start()
            except SwiftChildUnavailable as exc:
                final = dict(self._state)
                final["final"] = True
                final["warnings"] = list(final.get("warnings", [])) + [
                    {
                        "timestamp": _utcnow_iso(),
                        "source": None,
                        "message": str(exc),
                    }
                ]
                final["last_event_at"] = _utcnow_iso()
                self._write_state(final)
                raise CaptureFailedToStart(
                    str(exc), final_state=final
                ) from exc
            self._child = child
            self._owns_child = True
        else:
            self._owns_child = False

        # Attach this session as the active sink for the child's event reader.
        assert self._child is not None
        await self._child.run_capture(self)

        # Persist the initial state with the configured paths so a status
        # snapshot taken between attach and ``started`` still reports the
        # right filenames. Spec 005: two derived per-source paths instead of
        # one mixed output path.
        mic_path = self._basename.parent / (self._basename.name + "-mic.wav")
        system_path = self._basename.parent / (self._basename.name + "-system.wav")
        self._state["basename"] = str(self._basename)
        self._state["audio_files"]["mic"]["path"] = str(mic_path)
        self._state["audio_files"]["system_audio"]["path"] = str(system_path)
        self._state["video_output_path"] = (
            str(self._video_output_path) if self._video_output_path else None
        )
        self._write_state(self._state)

        _log.info(
            "capture_session_starting",
            basename=str(self._basename),
            video_output_path=(
                str(self._video_output_path) if self._video_output_path else None
            ),
            owner_pid=self._owner_pid,
        )

        # Send the ``start`` command. The Swift binary's first event is
        # ``ready`` (consumed by SwiftChild before this point); ``started``
        # arrives after our command lands. ``output_path`` keeps its JSON
        # key but now carries a basename (no extension); the binary derives
        # the two .wav paths from it.
        video_config = (
            ipc.VideoConfig(fps=30, show_cursor=True)
            if self._video_output_path
            else None
        )
        assert self._child is not None
        self._child.send_command(
            ipc.StartCommand(
                output_path=str(self._basename),
                format=ipc.AudioFormat(
                    sample_rate=self._sample_rate,
                    bit_depth=self._bit_depth,
                    channels=self._channels,
                ),
                video_output_path=(
                    str(self._video_output_path)
                    if self._video_output_path
                    else None
                ),
                video=video_config,
            ),
        )

        # Wait for either ``started`` or the child dying. The session's
        # ``_child_exited_event`` is set by :meth:`_on_child_exited_unexpectedly`
        # when the SwiftChild watcher sees an exit.
        started_wait = asyncio.create_task(self._started_event.wait())
        exited_wait = asyncio.create_task(self._child_exited_event.wait())
        start_failed_wait = asyncio.create_task(self._start_failed_event.wait())
        try:
            await asyncio.wait(
                {started_wait, exited_wait, start_failed_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (started_wait, exited_wait, start_failed_wait):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        if (
            self._start_error is not None
            and not self._started_event.is_set()
            and not self._saw_stopped
        ):
            # The Swift child reported an error before ``started`` (e.g. a
            # permission denial). In daemon mode the child stays alive, so we
            # surface the failure cleanly: the daemon resets to IDLE and the
            # shared child is reusable for the next start.
            final = dict(self._state)
            final["final"] = True
            final["last_event_at"] = _utcnow_iso()
            self._write_state(final)
            raise CaptureFailedToStart(
                self._start_error, final_state=final
            )

        if self._child_exited_abnormally and not self._saw_stopped:
            # The child died before our capture got off the ground.
            final = dict(self._state)
            final["final"] = True
            final["last_event_at"] = _utcnow_iso()
            self._write_state(final)
            raise CaptureFailedToStart(
                "capture binary exited before emitting started",
                final_state=final,
            )

        # In some races the child exits cleanly *with* a started event (e.g.
        # `started` and `stopped` arrive back-to-back). That's not a startup
        # failure — let the caller progress to stop().

    async def stop(self) -> dict[str, Any]:
        """Send ``stop`` to the binary and wait for it to finalize.

        Returns the final state dict (also persisted to disk with
        ``final=True``).

        Idempotent — calling :meth:`stop` after a system-event-triggered shutdown
        (i.e. ``stopped_event`` already set without us asking) just finalizes.
        """
        if self._child is None:
            # start() was never called or failed before attaching the child.
            final = dict(self._state)
            final["final"] = True
            final["last_event_at"] = _utcnow_iso()
            self._write_state(final)
            return final

        child = self._child

        # If the child is still running and we haven't already seen stopped /
        # asked for it, send the stop command.
        proc = child.proc
        proc_alive = proc is not None and proc.poll() is None
        if proc_alive and not self._stop_requested and not self._saw_stopped:
            self._stop_requested = True
            _log.info("capture_session_stopping")
            child.send_command(ipc.StopCommand())

        # Wait for ``stopped`` or for the child to exit. In daemon mode the
        # child stays alive after stopped, so the stopped_event is the only
        # signal we'll see. In one-shot mode, the child will exit shortly
        # after — both signals fire and we proceed.
        stopped_wait = asyncio.create_task(self._stopped_event.wait())
        exited_wait = asyncio.create_task(self._child_exited_event.wait())
        try:
            await asyncio.wait(
                {stopped_wait, exited_wait},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=_SHUTDOWN_TIMEOUT_SECONDS + 5.0,
            )
        finally:
            for t in (stopped_wait, exited_wait):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        return_code = proc.returncode if proc is not None else None
        abnormal = False
        if not self._saw_stopped and not self._stop_requested:
            abnormal = True
            _log.warning(
                "binary_exited_without_stopped", return_code=return_code
            )
            self._state["warnings"].append(
                {
                    "timestamp": _utcnow_iso(),
                    "source": None,
                    "message": (
                        f"supervisor terminated abnormally "
                        f"(binary exit code {return_code})"
                    ),
                }
            )

        # System-event-triggered clean exit: the binary emitted ``stopped`` on
        # its own without us asking. Mirrors the legacy supervisor's
        # orchestrator-log summary line so verification scripts that grep for
        # it (spec 002 manual smoke) keep working.
        system_event_reasons = {"system_sleep", "display_sleep", "screen_locked"}
        ended_by = self._state.get("ended_by")
        if (
            self._saw_stopped
            and not self._stop_requested
            and ended_by in system_event_reasons
        ):
            parts = [
                f"[{_utcnow_iso()}]",
                "capture ended by system event",
                f"reason={ended_by}",
                f"audio={self._state.get('basename')}",
            ]
            video_path = self._state.get("video_output_path")
            if video_path:
                parts.append(f"video={video_path}")
            duration = self._state.get("duration_seconds")
            if duration is not None:
                parts.append(f"duration_seconds={duration}")
            summary_line = " ".join(parts) + "\n"
            try:
                self._daemon_log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._daemon_log_path, "a", encoding="utf-8") as fp:
                    fp.write(summary_line)
            except OSError as exc:  # pragma: no cover - defensive
                _log.warning("system_event_summary_write_failed", error=str(exc))

        self._state["final"] = True
        self._state["last_event_at"] = _utcnow_iso()
        self._write_state(self._state)

        _log.info(
            "capture_session_finished", abnormal=abnormal, return_code=return_code
        )

        # Detach from the child so the next capture cycle can attach fresh.
        # If we own the child (legacy supervisor path), shut it down now.
        owns_child = getattr(self, "_owns_child", False)
        child.detach_session(self)
        if owns_child:
            await child.shutdown()

        return self._state

    def set_combined_audio(self, combined_audio: dict[str, Any]) -> None:
        """Merge ``combined_audio`` into the session state and re-persist.

        Spec 007 slice 2: the daemon runs the combine step after :meth:`stop`
        and folds the outcome back into ``capture-state.json`` so the CLI's
        stop summary can render it. ``stop`` returns ``self._state`` itself
        (live reference) — mutating it via this helper also updates the dict
        the daemon holds.
        """
        self._state["combined_audio"] = combined_audio
        self._write_state(self._state)

    # ----- Internals ------------------------------------------------------

    def _write_state(self, snapshot: dict[str, Any]) -> None:
        """Persist ``snapshot`` to ``capture-state.json`` (or the override path)."""
        if self._state_file_path is None:
            state.write_state(snapshot)
        else:
            state.write_state(snapshot, path=self._state_file_path)


__all__ = [
    "CaptureSession",
    "CaptureFailedToStart",
    "SwiftChild",
    "SwiftChildUnavailable",
    # Re-exported helpers so supervisor.py and the daemon don't need to
    # import the private ones directly.
    "_resolve_binary",
    "_drain_stderr",
    "_initial_state",
    "_apply_event",
    "_log_event",
    "_send_command",
    "_utcnow_iso",
]
