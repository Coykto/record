"""Typer CLI for the record orchestrator.

Exposes ``record start`` and ``record stop`` per
``context/spec/001-mixed-mic-system-audio-capture/technical-considerations.md``
§2.4. Both commands return promptly: ``start`` spawns a detached supervisor
and exits, ``stop`` signals the supervisor and waits for it to exit (with a
timeout) before printing a final summary.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import typer

from . import paths, state

app = typer.Typer(help="record: privacy-first meeting recorder")

# How long `record stop` waits for the supervisor to exit after SIGTERM.
_STOP_TIMEOUT_SECONDS = 10.0
_STOP_POLL_INTERVAL = 0.1

# Brief synchronous handshake after `record start` spawns the supervisor: we
# poll for early supervisor exit (binary missing, permission denied) so we can
# surface the right exit code instead of returning 0 and leaving a stale PID.
_START_HANDSHAKE_SECONDS = 3.0
_START_HANDSHAKE_INTERVAL = 0.1

# Audio capture format. The Swift binary writes a WAV at exactly these
# parameters; downstream transcription (Deepgram nova-3) is tuned for
# 16 kHz / 16-bit / mono so we pin them here rather than exposing them as
# user flags. The supervisor's argparse defaults match these values as a
# backstop, but the CLI is the authoritative source.
_SAMPLE_RATE = 16000
_BIT_DEPTH = 16
_CHANNELS = 1

# Lookup: permission_denied kind -> human-readable description for the user.
# Modern macOS labels the screen-recording panel "Screen & System Audio
# Recording"; on macOS 13 it's still "Screen Recording" — we go with the
# current label since the project targets macOS 13+ but most users will be on
# 14+.
_PERMISSION_MESSAGES: dict[str, str] = {
    "microphone": (
        "microphone permission denied — grant access in "
        "System Settings → Privacy & Security → Microphone"
    ),
    "screen_recording": (
        "screen recording permission denied — grant access in "
        "System Settings → Privacy & Security → Screen & System Audio Recording"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filename_timestamp(now: datetime | None = None) -> str:
    """Return a filename-safe timestamp like ``2026-05-10T14-32-08``.

    Local time is used because the on-disk file lives next to the user's
    other meeting artifacts, where wall-clock-local is the natural sort key.
    """
    moment = now if now is not None else datetime.now()
    return moment.strftime("%Y-%m-%dT%H-%M-%S")


def _format_duration(seconds: float | None) -> str:
    """Format a duration for the stop-summary line.

    Sub-minute durations get one decimal of seconds (``3.2 s``); minute-plus
    durations switch to ``Mm SSs`` for readability.
    """
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"


def _format_offset(seconds: float | None) -> str:
    """Format an at-offset-seconds value as ``MM:SS``."""
    if seconds is None:
        return "??:??"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _summarize_sources(state_dict: dict[str, Any]) -> str:
    """Build the human-friendly sources line for the stop summary."""
    sources = state_dict.get("sources") or {}
    mic = sources.get("mic") or {}
    sysa = sources.get("system_audio") or {}

    mic_status = mic.get("status", "never_attached")
    sysa_status = sysa.get("status", "never_attached")

    # Drop offsets from the warnings list so we can annotate "lost at MM:SS".
    lost_offsets: dict[str, float] = {}
    for w in state_dict.get("warnings", []) or []:
        src = w.get("source")
        if src in ("mic", "system_audio") and "at_offset_seconds" in w:
            lost_offsets[src] = w["at_offset_seconds"]

    if mic_status == "attached" and sysa_status == "attached":
        return "microphone + system audio"
    if mic_status == "attached" and sysa_status == "lost":
        return (
            f"system audio dropped at "
            f"{_format_offset(lost_offsets.get('system_audio'))} "
            f"— remainder is microphone only"
        )
    if mic_status == "lost" and sysa_status == "attached":
        return (
            f"microphone dropped at "
            f"{_format_offset(lost_offsets.get('mic'))} "
            f"— remainder is system audio only"
        )
    if mic_status == "attached":
        return "microphone only"
    if sysa_status == "attached":
        return "system audio only"
    return "no audio sources captured"


def _extra_warnings(state_dict: dict[str, Any]) -> list[str]:
    """Return non-source-loss warnings that are worth showing on stop."""
    extras: list[str] = []
    # Note: permission_denied is handled separately by the exit-2 branch in
    # `stop`; intentionally not included here.
    for w in state_dict.get("warnings", []) or []:
        if w.get("source") in ("mic", "system_audio"):
            continue  # already covered by the sources line
        msg = w.get("message")
        if msg:
            extras.append(str(msg))
    return extras


def _permission_message(kind: str) -> str:
    """Map a permission_denied kind to a user-facing message."""
    return _PERMISSION_MESSAGES.get(
        kind,
        f"{kind} permission denied — grant access in System Settings → Privacy & Security",
    )


def _resolve_capture_binary() -> Path | None:
    """Locate the bundled `record-capture` binary.

    Mirrors `supervisor._resolve_binary` so the CLI can fail fast with exit 3
    before claiming the PID file. Returns ``None`` if missing or not
    executable.
    """
    try:
        resource = files("record") / "bin" / "record-capture"
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


def _wait_for_early_failure(
    proc: subprocess.Popen[Any], timeout: float
) -> bool:
    """Poll up to ``timeout`` seconds for the supervisor to either fail fast
    or signal healthy startup.

    Returns ``True`` if the supervisor exited within the window (a failure
    signal for `record start`), ``False`` if it appears healthy.

    Healthy is detected either by the deadline elapsing without an exit, OR
    by ``capture-state.json`` reporting a ``start_time`` (set when the Swift
    binary emits the ``started`` event). The latter lets the common happy
    path return well under a second instead of always waiting the full
    timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        snapshot = state.read_state() or {}
        if snapshot.get("start_time"):
            return False
        time.sleep(_START_HANDSHAKE_INTERVAL)
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def start() -> None:
    """Start a recording session.

    Resolves an absolute output path, spawns a detached supervisor process,
    and returns immediately. The supervisor owns the Swift capture binary
    for the rest of the capture lifetime.
    """
    paths.ensure_dirs()

    # Single-instance check: refuse if a live supervisor is already running.
    existing_pid = state.read_pid_file()
    if existing_pid is not None:
        if state.is_alive(existing_pid):
            typer.echo(
                f"capture already in progress (PID {existing_pid})", err=True
            )
            raise typer.Exit(code=1)
        # Stale PID file — clean it up so claim_pid_file below succeeds.
        state.remove_pid_file()
        # Don't leave a half-written state file pointing at a dead PID either.
        state.remove_state()

    # Pre-check the bundled Swift binary before claiming the PID file so a
    # missing binary doesn't leave half-set-up artifacts on disk.
    binary = _resolve_capture_binary()
    if binary is None:
        # Best-effort: surface the path we expected so the user can see what's
        # missing. If the resource can't be resolved at all, fall back to a
        # generic message.
        try:
            resource = files("record") / "bin" / "record-capture"
            with as_file(resource) as path:
                expected = str(Path(path))
        except Exception:
            expected = "src/record/bin/record-capture"
        typer.echo(
            f"capture binary not found at {expected} — "
            f"run `make install` to build and install it",
            err=True,
        )
        raise typer.Exit(code=3)

    # Resolve the absolute output path before forking so the supervisor sees
    # a fully qualified path even if its CWD differs from ours.
    output_path = (Path.cwd() / f"{_filename_timestamp()}.wav").resolve()

    # Spawn the supervisor fully detached from this terminal. Format params
    # are passed explicitly so the CLI owns the capture-format decision; the
    # supervisor forwards them verbatim into the `start` IPC command.
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "record.supervisor",
                "--output-path",
                str(output_path),
                "--sample-rate",
                str(_SAMPLE_RATE),
                "--bit-depth",
                str(_BIT_DEPTH),
                "--channels",
                str(_CHANNELS),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as exc:
        typer.echo(
            f"failed to launch supervisor: {exc} — "
            f"run `make install` to (re)build the capture pipeline",
            err=True,
        )
        raise typer.Exit(code=3) from None

    # Record the supervisor's PID. We use claim_pid_file so a leftover stale
    # file (e.g. from a crash since our earlier check) is still recovered.
    try:
        state.claim_pid_file(proc.pid)
    except state.CaptureAlreadyRunning as exc:
        # Lost the race against another `record start`. Tell the supervisor
        # we just spawned to back off, then exit.
        typer.echo(str(exc), err=True)
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise typer.Exit(code=1) from None

    # Brief synchronous handshake: if the supervisor dies within the window we
    # inspect the final state file and translate it into the right CLI exit
    # code. Healthy startup returns within a tick or two of polling.
    if _wait_for_early_failure(proc, _START_HANDSHAKE_SECONDS):
        final = state.read_state() or {}
        # Tidy up before exiting — the supervisor is dead, nothing else owns
        # these files.
        state.remove_pid_file()
        state.remove_state()

        kind = final.get("permission_denied")
        if isinstance(kind, str):
            typer.echo(_permission_message(kind), err=True)
            raise typer.Exit(code=2) from None

        # The supervisor leaves a "capture binary missing" warning when its
        # own _resolve_binary returns None — match it here so a race between
        # our pre-check and supervisor startup still surfaces correctly.
        warnings = final.get("warnings") or []
        for w in warnings:
            msg = (w.get("message") or "") if isinstance(w, dict) else ""
            if "capture binary missing" in msg.lower():
                typer.echo(
                    "capture binary missing or not executable — "
                    "run `make install` to build and install it",
                    err=True,
                )
                raise typer.Exit(code=3) from None

        # Generic launch failure.
        typer.echo(
            "supervisor failed to start — "
            "inspect ~/Library/Logs/record/daemon.log for clues",
            err=True,
        )
        raise typer.Exit(code=3) from None

    typer.echo(f"capture started (pid {proc.pid}) -> {output_path}")


@app.command()
def stop() -> None:
    """Stop the active recording session.

    Sends SIGTERM to the supervisor, waits for it to finalize state, prints
    a summary, and cleans up the PID and state files.
    """
    pid = state.read_pid_file()
    if pid is None:
        typer.echo("no capture running", err=True)
        raise typer.Exit(code=1)

    if not state.is_alive(pid):
        state.remove_pid_file()
        state.remove_state()
        typer.echo("no capture running (stale PID file cleaned up)", err=True)
        raise typer.Exit(code=1)

    # Ask the supervisor to wind things down.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Race: process died between is_alive and now.
        state.remove_pid_file()
        state.remove_state()
        typer.echo("no capture running (process disappeared)", err=True)
        raise typer.Exit(code=1) from None

    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not state.is_alive(pid):
            break
        time.sleep(_STOP_POLL_INTERVAL)
    else:
        # Force-kill is intentionally NOT used here — leave the process for
        # the user to inspect via daemon.log / orchestrator.log.
        typer.echo(
            f"supervisor (PID {pid}) did not exit within "
            f"{_STOP_TIMEOUT_SECONDS:.0f}s — inspect "
            f"~/Library/Logs/record/daemon.log and "
            f"~/Library/Logs/record/orchestrator.log for clues",
            err=True,
        )
        raise typer.Exit(code=4)

    # Read the final state. The supervisor sets `final: True` as the last
    # write before it exits; if we don't see it, the file is at least the
    # latest snapshot the supervisor managed to persist.
    final = state.read_state() or {}

    # Permission denial gets its own exit code (2) and a System-Settings-aware
    # message rather than the regular summary.
    kind = final.get("permission_denied")
    if isinstance(kind, str):
        typer.echo(_permission_message(kind), err=True)
        state.remove_pid_file()
        state.remove_state()
        raise typer.Exit(code=2)

    output_path = final.get("output_path") or "(unknown)"
    duration = _format_duration(final.get("duration_seconds"))
    sources_line = _summarize_sources(final)

    typer.echo(f"capture stopped")
    typer.echo(f"  output:   {output_path}")
    typer.echo(f"  duration: {duration}")
    typer.echo(f"  sources:  {sources_line}")
    for warning in _extra_warnings(final):
        typer.echo(f"  warning:  {warning}")

    # Tidy up. Both removals are idempotent on missing files.
    state.remove_pid_file()
    state.remove_state()
