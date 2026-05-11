"""Long-running supervisor process.

The supervisor is spawned (detached, in its own session) by ``record start``.
It owns the Swift ``record-capture`` subprocess for the entire capture
lifetime, translating its JSON-line event stream into updates of
``capture-state.json`` and structured log lines.

Lifecycle:

1. Resolve the bundled Swift binary via ``importlib.resources``.
2. Spawn it with line-buffered text pipes; start a stderr-draining thread
   that appends to ``daemon.log``.
3. Issue the ``start`` command immediately.
4. Read events on stdout; persist state, log, react.
5. On SIGTERM (sent by ``record stop``), forward a ``stop`` command into the
   Swift binary's stdin and continue draining stdout until it closes.
6. Finalize ``capture-state.json`` and exit.

Invocation: ``python -m record.supervisor --output-path /abs/path.wav``.
We pick CLI args (rather than env vars) for the supervisor because the
output path is the only required value and it's easier to debug from
``ps aux`` when it appears in the command line.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import ipc, paths, state
from .logging_setup import configure_logging, get_logger

# Exit codes used by the supervisor itself. CLI-facing exit codes (1, 2, 3, 4)
# live in cli.py; the supervisor's exits are mostly observed by `record stop`
# via state-file inspection rather than by direct exit-code reading, but we
# still pick informative numbers.
_EXIT_OK = 0
_EXIT_BINARY_MISSING = 10
_EXIT_BINARY_ABNORMAL = 11

_log = get_logger("record.supervisor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return the current UTC time as a Z-suffixed ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_binary() -> Path | None:
    """Resolve the bundled Swift binary's path on disk.

    Returns ``None`` if the binary is missing or not executable.
    """
    try:
        resource = files("record") / "bin" / "record-capture"
    except (ModuleNotFoundError, FileNotFoundError):
        return None

    # `as_file` materializes a real filesystem path even if the package is
    # installed in a zip. For an editable install it's a no-op.
    try:
        with as_file(resource) as path:
            target = Path(path)
            if not target.exists() or not os.access(target, os.X_OK):
                return None
            return target
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def _drain_stderr(proc: subprocess.Popen[str], log_path: Path) -> None:
    """Append the binary's stderr line-by-line to ``daemon.log``.

    Runs in a background thread for the lifetime of the subprocess. Best
    effort — IO errors here are logged at warning level to the orchestrator
    log but don't terminate the supervisor.
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
# State machine
# ---------------------------------------------------------------------------


def _initial_state(supervisor_pid: int) -> dict[str, Any]:
    """Build the state dict written when the binary first emits ``ready``."""
    return {
        "pid": supervisor_pid,
        "start_time": None,
        "output_path": None,
        "video_output_path": None,
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
        # Already initialized at spawn time; nothing extra to record.
        return current

    if isinstance(event, ipc.StartedEvent):
        current["start_time"] = event.start_time
        # output_path was set when the supervisor sent the `start` command;
        # we keep it from the command issuance rather than re-deriving it.
        return current

    if isinstance(event, ipc.SourceAttachedEvent):
        src = current["sources"][event.source]
        src["status"] = "attached"
        src["attached_at"] = now
        # `lost_at` stays null unless the source had previously dropped.
        return current

    if isinstance(event, ipc.SourceLostEvent):
        src = current["sources"][event.source]
        # Idempotency: the first loss is the one that matters. If the binary
        # ever re-emits source_lost for an already-lost source, ignore it so
        # we don't overwrite lost_at or duplicate the warning entry.
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
        # Surfaced via logs only. The user-facing message is generated by
        # `record stop` if a denial follows; this event alone is just info.
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
        # Trust the binary's idea of the output path; it should match what we
        # told it on `start`, but if it ever diverges we want the truth.
        current["output_path"] = event.output_path
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
        # Idempotency: mirror SourceLostEvent — the first loss is the one that
        # matters; later duplicates are silently dropped.
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
        return current

    if isinstance(event, ipc.VideoFileEvent):
        # Trust the binary's reported path on `video_file`, similar to how
        # StoppedEvent overrides output_path.
        current["video_output_path"] = event.path
        current["video_file_duration_seconds"] = event.duration_seconds
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
        # Real summary-writing into orchestrator.log happens in a later slice;
        # for now we just record the reason so `record stop` can surface it.
        current["ended_by"] = event.reason
        return current

    return current


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _send_command(proc: subprocess.Popen[str], cmd: ipc.Command) -> None:
    """Write a single command line to the binary's stdin and flush."""
    assert proc.stdin is not None
    line = ipc.serialize_command(cmd) + "\n"
    try:
        proc.stdin.write(line)
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        # Binary may have already exited (e.g. crashed before we got SIGTERM).
        _log.warning("command_send_failed", cmd=cmd.cmd, error=str(exc))


def _log_event(event: ipc.Event) -> None:
    """Emit a structlog line for ``event``."""
    name = event.event
    payload = event.model_dump(exclude={"event"})
    if isinstance(event, (ipc.SourceLostEvent, ipc.ErrorEvent, ipc.PermissionDeniedEvent)):
        _log.warning(name, **payload)
    else:
        _log.info(name, **payload)


def run(
    output_path: Path,
    sample_rate: int,
    bit_depth: int,
    channels: int,
    video_output_path: Path | None = None,
) -> int:
    """Main supervisor entry point. Returns the desired process exit code."""
    configure_logging()
    paths.ensure_dirs()
    resolved = paths.resolve_paths()

    binary = _resolve_binary()
    if binary is None:
        _log.error("capture_binary_missing")
        # Persist a minimal final state so `record stop` can surface this.
        state.write_state(
            {
                "pid": os.getpid(),
                "final": True,
                "warnings": [
                    {
                        "timestamp": _utcnow_iso(),
                        "source": None,
                        "message": "capture binary missing or not executable",
                    }
                ],
                "last_event_at": _utcnow_iso(),
            }
        )
        return _EXIT_BINARY_MISSING

    _log.info(
        "supervisor_starting",
        binary=str(binary),
        output_path=str(output_path),
        video_output_path=str(video_output_path) if video_output_path else None,
        pid=os.getpid(),
    )

    # Spawn the Swift binary in its own session so signals delivered to the
    # supervisor don't propagate by accident.
    proc = subprocess.Popen(
        [str(binary)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )

    # Background drainer for stderr -> daemon.log.
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(proc, resolved.daemon_log),
        name="record-capture-stderr",
        daemon=True,
    )
    stderr_thread.start()

    # Initial state file. The supervisor's own PID is what `record stop`
    # signals, so that's what we record here. The Swift binary's PID is not
    # currently reflected.
    current_state = _initial_state(supervisor_pid=os.getpid())
    current_state["output_path"] = str(output_path)
    current_state["video_output_path"] = (
        str(video_output_path) if video_output_path else None
    )
    state.write_state(current_state)

    # Send the `start` command. We do this before reading any events because
    # the Swift binary's first event (`ready`) is emitted at startup; on the
    # next event after `ready` we expect `started` to follow our command.
    #
    # Video parameters are optional on the wire: when ``video_output_path`` is
    # ``None`` the field is omitted entirely (see ``ipc.serialize_command``)
    # and the binary skips video capture. Slice 1 wires the plumbing without
    # yet rendering any frames.
    video_config = (
        ipc.VideoConfig(fps=30, show_cursor=True) if video_output_path else None
    )
    _send_command(
        proc,
        ipc.StartCommand(
            output_path=str(output_path),
            format=ipc.AudioFormat(
                sample_rate=sample_rate,
                bit_depth=bit_depth,
                channels=channels,
            ),
            video_output_path=(
                str(video_output_path) if video_output_path else None
            ),
            video=video_config,
        ),
    )

    # SIGTERM handler: forward a `stop` command and let the read loop drain
    # the remaining events. Using a flag so re-entry is safe.
    stop_requested = threading.Event()

    def _handle_sigterm(signum: int, frame: Any) -> None:
        if stop_requested.is_set():
            return
        stop_requested.set()
        _log.info("sigterm_received")
        _send_command(proc, ipc.StopCommand())

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    # Read loop. ``proc.stdout`` is line-buffered in text mode so each
    # ``readline()`` returns at most one event.
    assert proc.stdout is not None
    abnormal = False
    saw_stopped = False

    while True:
        try:
            line = proc.stdout.readline()
        except KeyboardInterrupt:
            # Treat Ctrl-C as a stop request, then resume reading.
            _handle_sigterm(signal.SIGINT, None)
            continue

        if line == "":
            # EOF — binary closed stdout (clean exit or crash).
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

        _log_event(event)
        current_state = _apply_event(current_state, event)
        try:
            state.write_state(current_state)
        except OSError as exc:
            _log.warning("state_write_failed", error=str(exc))

        if isinstance(event, ipc.StoppedEvent):
            saw_stopped = True
            # Don't break: keep draining until the binary closes stdout, so
            # any trailing events (rare) still land in the state file.

    # Reap the binary. Give it a brief grace period in case stopped happened
    # but exit() hasn't completed yet.
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _log.warning("binary_did_not_exit_killing")
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass

    return_code = proc.returncode
    if not saw_stopped and not stop_requested.is_set():
        abnormal = True
        _log.warning(
            "binary_exited_without_stopped",
            return_code=return_code,
        )
        current_state["warnings"].append(
            {
                "timestamp": _utcnow_iso(),
                "source": None,
                "message": (
                    f"supervisor terminated abnormally "
                    f"(binary exit code {return_code})"
                ),
            }
        )

    current_state["final"] = True
    current_state["last_event_at"] = _utcnow_iso()
    try:
        state.write_state(current_state)
    except OSError as exc:
        _log.warning("final_state_write_failed", error=str(exc))

    _log.info("supervisor_exiting", abnormal=abnormal, return_code=return_code)
    return _EXIT_BINARY_ABNORMAL if abnormal else _EXIT_OK


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record.supervisor",
        description="Long-running supervisor for the Swift capture binary.",
    )
    parser.add_argument(
        "--output-path",
        required=True,
        type=Path,
        help="Absolute path of the WAV file the binary will eventually write.",
    )
    parser.add_argument(
        "--video-output-path",
        required=False,
        default=None,
        type=Path,
        help=(
            "Absolute path of the MP4 file the binary will eventually write. "
            "When omitted, video capture is skipped entirely."
        ),
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--bit-depth", type=int, default=16)
    parser.add_argument("--channels", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_path = args.output_path
    if not output_path.is_absolute():
        output_path = output_path.resolve()
    video_output_path: Path | None = args.video_output_path
    if video_output_path is not None and not video_output_path.is_absolute():
        video_output_path = video_output_path.resolve()
    return run(
        output_path=output_path,
        sample_rate=args.sample_rate,
        bit_depth=args.bit_depth,
        channels=args.channels,
        video_output_path=video_output_path,
    )


if __name__ == "__main__":
    sys.exit(main())
