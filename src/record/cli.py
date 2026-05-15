"""Typer CLI for the record orchestrator.

Spec 003 slice 2 dissolves the legacy ``record start`` / ``record stop`` →
``python -m record.supervisor`` spawn into a thin **client** of the running
daemon's Unix-domain control socket:

- ``record start`` sends a ``{"op":"start"}`` request.
- ``record stop`` sends a ``{"op":"stop"}`` request, then re-renders the same
  human-readable summary as the legacy stop (driven off the daemon-finalized
  ``capture-state.json``).
- ``record status`` sends a ``{"op":"status"}`` request and prints a short
  block per FR 2.4.

When the daemon isn't reachable the CLI prints the FR 2.7 "daemon is not
running" message and exits non-zero. The legacy ``python -m record.supervisor``
entry-point remains intact for the integration suite and offline use.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from . import config as config_module
from . import capture, control, ipc, launchagent, paths, secrets, state
from . import transcribe as transcribe_module
from .logging_setup import get_logger

app = typer.Typer(help="record: privacy-first meeting recorder")

# `record daemon ...` sub-app — spec 003 slice 1 + 2 commands.
daemon_app = typer.Typer(help="Control the background record daemon.")
app.add_typer(daemon_app, name="daemon")

# How long `record daemon stop` (and legacy `record stop` paths still in tests)
# wait for a process to exit after SIGTERM.
_STOP_TIMEOUT_SECONDS = 10.0
_STOP_POLL_INTERVAL = 0.1

# Daemon-spawn handshake window (slice 1).
_DAEMON_START_HANDSHAKE_SECONDS = 3.0
_DAEMON_START_POLL_INTERVAL = 0.05

# Audio capture format. Pinned to the Deepgram nova-3 target; kept here as a
# historical anchor — the daemon owns the negotiation in slice 2.
_SAMPLE_RATE = 16000
_BIT_DEPTH = 16
_CHANNELS = 1

# Lookup: permission_denied kind -> human-readable description for the user.
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

# FR 2.7 third bullet — surfaced anywhere a CLI socket request fails because
# the daemon isn't running.
_DAEMON_NOT_RUNNING_MESSAGE = (
    "daemon is not running — try `record daemon start` or `record install`"
)


# ---------------------------------------------------------------------------
# Hotkey rendering helpers (spec 003 slice 5 — `record status`)
# ---------------------------------------------------------------------------

# macOS menu-style glyphs for each modifier. Drives the visual rendering of
# ``record status`` so the user sees the same combo macOS itself would print.
_HOTKEY_GLYPHS: dict[str, str] = {
    "cmd": "⌘",
    "option": "⌥",
    "control": "⌃",
    "shift": "⇧",
}
# Display order matches macOS menu rendering convention (control, option,
# shift, command — left-to-right on a menu shortcut).
_HOTKEY_GLYPH_ORDER: tuple[str, ...] = ("control", "option", "shift", "cmd")
# Named-key display overrides. Keys not in this dict use ``upper()`` for
# single letters / function keys, otherwise the raw canonical name.
_NAMED_KEY_DISPLAY: dict[str, str] = {
    "space": "Space",
    "tab": "Tab",
    "return": "Return",
    "escape": "Escape",
    "delete": "Delete",
}


def _format_hotkey_glyphs(configured: str | None) -> str:
    """Render a canonical hotkey string in macOS glyph form.

    ``"cmd+option+r"`` → ``"⌥⌘R"``. Returns ``"(unknown)"`` when
    ``configured`` is ``None`` or the string can't be parsed. The renderer is
    intentionally tolerant — a malformed config-time hotkey string shouldn't
    take down ``record status``.
    """
    if not configured:
        return "(unknown)"
    parts = configured.split("+")
    if not parts:
        return "(unknown)"
    *mods, key = parts
    mods_set = set(mods)
    ordered_glyphs = "".join(
        _HOTKEY_GLYPHS[m] for m in _HOTKEY_GLYPH_ORDER if m in mods_set
    )
    if key in _NAMED_KEY_DISPLAY:
        key_display = _NAMED_KEY_DISPLAY[key]
    elif len(key) == 1:
        key_display = key.upper()
    elif key.startswith("f") and key[1:].isdigit():
        key_display = key.upper()
    else:
        key_display = key
    return ordered_glyphs + key_display


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filename_timestamp(now: datetime | None = None) -> str:
    """Return a filename-safe timestamp like ``2026-05-10T14-32-08``."""
    moment = now if now is not None else datetime.now()
    return moment.strftime("%Y-%m-%dT%H-%M-%S")


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"


def _format_offset(seconds: float | None) -> str:
    if seconds is None:
        return "??:??"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _summarize_video(state_dict: dict[str, Any]) -> str:
    """Build the human-friendly video-source line for the stop summary."""
    sources = state_dict.get("sources") or {}
    video = sources.get("video") or {}
    status = video.get("status", "never_attached")

    if status == "attached":
        path = state_dict.get("video_output_path") or "(unknown)"
        duration_seconds = state_dict.get("video_file_duration_seconds")
        if duration_seconds is None:
            duration_seconds = state_dict.get("duration_seconds")
        duration = _format_duration(duration_seconds)
        width = video.get("width_px")
        height = video.get("height_px")
        dims = f"{width}×{height}" if width and height else "unknown"
        return f"video: {path} ({duration}, {dims})"

    if status == "lost":
        video_warning: dict[str, Any] | None = None
        for w in state_dict.get("warnings", []) or []:
            if isinstance(w, dict) and w.get("source") == "video":
                video_warning = w
                break
        if video_warning is None:
            path = state_dict.get("video_output_path") or "(unknown)"
            return f"video: {path} (lost)"
        reason = video_warning.get("message") or "unknown"
        offset = video_warning.get("at_offset_seconds")
        if offset == 0:
            return f"video: unavailable — {reason}"
        path = state_dict.get("video_output_path") or "(unknown)"
        return f"video: {path} — stopped at {_format_offset(offset)}, reason {reason}"

    return f"video: {status}"


def _summarize_sources(state_dict: dict[str, Any]) -> str:
    """Build the human-friendly sources line for the stop summary."""
    sources = state_dict.get("sources") or {}
    mic = sources.get("mic") or {}
    sysa = sources.get("system_audio") or {}

    mic_status = mic.get("status", "never_attached")
    sysa_status = sysa.get("status", "never_attached")

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
    extras: list[str] = []
    for w in state_dict.get("warnings", []) or []:
        if w.get("source") in ("mic", "system_audio", "video"):
            continue
        msg = w.get("message")
        if msg:
            extras.append(str(msg))
    return extras


def _permission_message(kind: str) -> str:
    return _PERMISSION_MESSAGES.get(
        kind,
        f"{kind} permission denied — grant access in System Settings → Privacy & Security",
    )


def _print_stop_summary(final: dict[str, Any]) -> None:
    """Render the same summary block the legacy ``record stop`` produced.

    Factored out so the new socket-client ``record stop`` and the legacy
    summary tests share one renderer.
    """
    output_path = final.get("output_path") or "(unknown)"
    duration = _format_duration(final.get("duration_seconds"))
    sources_line = _summarize_sources(final)
    video_line = _summarize_video(final)

    typer.echo("capture stopped")
    typer.echo(f"  output:   {output_path}")
    typer.echo(f"  duration: {duration}")
    typer.echo(f"  sources:  {sources_line}")
    typer.echo(f"  {video_line}")
    for warning in _extra_warnings(final):
        typer.echo(f"  warning:  {warning}")


# ---------------------------------------------------------------------------
# Daemon-routed commands (spec 003 slice 2)
# ---------------------------------------------------------------------------


@app.command()
def start() -> None:
    """Start a recording session via the running daemon.

    Sends a ``{"op":"start"}`` request to the daemon's control socket. The
    daemon spawns the Swift capture binary, sends a ``start`` command, and
    replies with the resolved audio/video paths.

    Exit codes:
      * 0 — capture started.
      * 1 — capture already in progress, or daemon not running.
      * 2 — daemon refused with a permission-denied condition (rare on start
            in slice 2; slice 6 fills out the audible/banner error path).
      * 3 — daemon errored (capture failed to start, e.g. missing binary).
    """
    try:
        resp = control.send_request_sync(control.StartRequest())
    except control.DaemonUnreachable:
        typer.echo(_DAEMON_NOT_RUNNING_MESSAGE, err=True)
        raise typer.Exit(code=1) from None

    if resp.status == "ok":
        typer.echo(
            f"capture started -> audio={resp.audio_path}, video={resp.video_path}"
        )
        return

    if resp.status == "already_running":
        typer.echo("capture already in progress", err=True)
        raise typer.Exit(code=1)

    if resp.status == "busy":
        typer.echo(f"daemon busy: {resp.detail or 'try again shortly'}", err=True)
        raise typer.Exit(code=1)

    # "error" / anything else — surface the daemon's detail string.
    typer.echo(
        f"capture failed to start: {resp.detail or resp.status}",
        err=True,
    )
    raise typer.Exit(code=3)


@app.command()
def stop() -> None:
    """Stop the active recording session via the running daemon.

    Sends a ``{"op":"stop"}`` request. The daemon finalizes
    ``capture-state.json`` (``final: true``) before replying, so reading the
    state file after the response gives the same summary the legacy stop
    produced.

    Exit codes mirror the legacy stop: 0 (success), 1 (not running / daemon
    unreachable), 2 (permission denied), 4 (timeout — daemon didn't reply).
    """
    try:
        resp = control.send_request_sync(control.StopRequest())
    except control.DaemonUnreachable:
        typer.echo(_DAEMON_NOT_RUNNING_MESSAGE, err=True)
        raise typer.Exit(code=1) from None

    if resp.status == "not_running":
        typer.echo("no capture running", err=True)
        raise typer.Exit(code=1)

    if resp.status == "busy":
        typer.echo(f"daemon busy: {resp.detail or 'try again shortly'}", err=True)
        raise typer.Exit(code=1)

    if resp.status not in ("ok",):
        typer.echo(
            f"capture failed to stop: {resp.detail or resp.status}",
            err=True,
        )
        raise typer.Exit(code=4)

    # Daemon finalized capture-state.json with `final: true` before replying.
    # Re-render the same summary block the legacy stop did.
    final = state.read_state() or {}

    kind = final.get("permission_denied")
    if isinstance(kind, str):
        typer.echo(_permission_message(kind), err=True)
        raise typer.Exit(code=2)

    _print_stop_summary(final)


@app.command()
def status() -> None:
    """Print a short, human-readable summary of the daemon and current capture.

    Exit code 0 if the daemon is running, non-zero otherwise (FR 2.4 final
    bullet — lets scripts rely on it).
    """
    try:
        resp = control.send_request_sync(control.StatusRequest())
    except control.DaemonUnreachable:
        # Partial status: the daemon isn't reachable, but autostart-registered
        # is still meaningful via `launchctl print` (tech spec §2.6).
        typer.echo("daemon: not running")
        autostart = (
            "registered" if launchagent.is_registered() else "not registered"
        )
        typer.echo(f"autostart: {autostart}")
        raise typer.Exit(code=1) from None

    if resp.status != "ok" or resp.daemon is None:
        typer.echo(
            f"status request failed: {resp.detail or resp.status}", err=True
        )
        raise typer.Exit(code=1)

    daemon_info = resp.daemon
    hotkey = resp.hotkey
    capture = resp.capture

    # daemon line
    pid_str = f"PID {daemon_info.pid}" if daemon_info.pid is not None else "PID ?"
    since = daemon_info.started_at or "?"
    typer.echo(f"daemon: running ({pid_str}, since {since})")

    # hotkey line — slice 5 renders the configured combo as macOS glyphs
    # alongside the state. When the daemon reports the message in conflict /
    # invalid / disabled_no_permission, that message carries the FR 2.13
    # wording and is echoed verbatim.
    if hotkey is None:
        typer.echo("hotkey: unregistered")
    else:
        glyphs = _format_hotkey_glyphs(hotkey.configured)
        if hotkey.state == "registered":
            typer.echo(f"hotkey: {glyphs} (registered)")
        elif hotkey.state == "conflict":
            # The daemon already constructs the FR 2.13 wording; emit it
            # verbatim after the glyph form so the line reads
            # "hotkey: ⌥⌘R — hotkey may be inactive — another …".
            msg = hotkey.message or (
                "another application has registered the same combination"
            )
            typer.echo(f"hotkey: {glyphs} — {msg}")
        elif hotkey.state == "invalid":
            msg = hotkey.message or "invalid configuration"
            typer.echo(f"hotkey: invalid configuration — {msg}")
        elif hotkey.state == "disabled_no_permission":
            msg = hotkey.message or "Accessibility permission missing"
            typer.echo(f"hotkey: disabled — {msg}")
        else:
            # "unregistered" or any future state — fall through to the bare
            # state name. Matches the slice-2 contract for existing tests.
            typer.echo(f"hotkey: {hotkey.state}")

    # autostart line — slice 2 stubs this as False.
    autostart = "registered" if daemon_info.autostart_registered else "not registered"
    typer.echo(f"autostart: {autostart}")

    # capture line
    if capture is not None and capture.running:
        bits = []
        if capture.started_at:
            bits.append(f"since {capture.started_at}")
        if capture.audio_path:
            bits.append(f"audio={capture.audio_path}")
        if capture.video_path:
            bits.append(f"video={capture.video_path}")
        detail = ", ".join(bits) if bits else ""
        typer.echo(
            "capture: running" + (f" ({detail})" if detail else "")
        )
    else:
        typer.echo("capture: idle")


# ---------------------------------------------------------------------------
# `record install` / `record uninstall` — spec 003 slice 7
# ---------------------------------------------------------------------------

# Hard cap on the prime subprocess, which walks the user through the
# Microphone / Accessibility / Screen Recording prompts one at a time (each
# polls and exits early the moment its grant lands). Just a kill-switch for a
# user who walks away mid-install — generous so a slow-but-present user is
# never cut off.
_PRIME_TIMEOUT_SECONDS = 300


def _prime_permissions() -> None:
    """Trigger the Screen Recording + Microphone TCC prompts before bootstrap.

    macOS will not present TCC permission UI to a launchd-spawned process, so a
    daemon started purely via ``launchctl bootstrap`` can never prompt for the
    microphone. ``record install`` runs from a terminal-rooted process tree, so
    we spawn the capture binary here with ``--prime-permissions`` to surface the
    prompts while we still can.

    The spawn goes through the same disclaiming ``posix_spawn`` path the daemon
    uses (:func:`capture._spawn_swift_child`) so the priming process and the
    launchd daemon's Swift child resolve to the *same* TCC responsible
    process — the capture binary's own ``com.record.capture`` identity. Prime
    without disclaim and the grant would land on a different identity than the
    one the daemon checks.

    Best-effort: a missing binary, a timeout, or a denied permission is
    reported but does not abort the install — the LaunchAgent is still useful
    once the user grants the permission and the daemon restarts.
    """
    binary = capture._resolve_binary()
    if binary is None:
        typer.echo(
            "warning: capture binary not found — skipping permission priming",
            err=True,
        )
        return

    typer.echo("priming permissions — approve the macOS prompts if they appear...")
    proc = capture._spawn_swift_child([str(binary), "--prime-permissions"])

    # Drain stdout (event lines) and stderr (live `[prime]` progress) on
    # threads so a hung prompt can't wedge us past the timeout, and so the
    # user gets per-permission feedback instead of an opaque wait.
    stdout_lines: list[str] = []

    def _drain_stdout() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                stdout_lines.append(line)
        except Exception:
            pass

    def _drain_stderr() -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.strip()
                # The Swift prime emits `[prime] <permission>: <status>` lines
                # as each permission settles — echo them as live progress.
                if line.startswith("[prime]"):
                    typer.echo(f"  {line[len('[prime]'):].strip()}")
        except Exception:
            pass

    readers = [
        threading.Thread(target=_drain_stdout, name="record-prime-out", daemon=True),
        threading.Thread(target=_drain_stderr, name="record-prime-err", daemon=True),
    ]
    for r in readers:
        r.start()

    try:
        returncode = proc.wait(timeout=_PRIME_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        for r in readers:
            r.join(timeout=2.0)
        typer.echo(
            "warning: permission priming timed out waiting for a response",
            err=True,
        )
        return
    for r in readers:
        r.join(timeout=2.0)

    if returncode == 0:
        typer.echo("permissions: screen recording + microphone granted")
        return

    denied: set[str] = set()
    for line in stdout_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = ipc.parse_event(line)
        except Exception:
            continue
        if isinstance(event, ipc.PermissionDeniedEvent):
            denied.add(event.kind.replace("_", " "))

    if denied:
        typer.echo(
            f"warning: permission not granted: {', '.join(sorted(denied))}",
            err=True,
        )
    else:
        typer.echo("warning: permissions not fully granted", err=True)
    typer.echo(
        "  grant them in System Settings → Privacy & Security, "
        "then re-run `record install`",
        err=True,
    )


@app.command()
def install() -> None:
    """Register the daemon as a LaunchAgent and start it.

    Writes ``~/Library/LaunchAgents/com.record.daemon.plist`` and bootstraps it
    into the per-user launchd domain (``launchctl bootstrap gui/$UID``). Re-runs
    safely: if the agent is already loaded, the bootstrap is preceded by a
    bootout so plist edits get picked up.

    Before bootstrapping, the macOS TCC prompts for Screen Recording and
    Microphone are primed from this terminal-rooted process — a launchd-spawned
    daemon cannot present those prompts itself.
    """
    try:
        cfg = config_module.load_config()
    except config_module.ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    # The plist names StandardOutPath/StandardErrorPath under log_folder; ensure
    # the directory exists so launchd doesn't fail-fast on its own log writes.
    try:
        paths.ensure_dirs_from_config(cfg)
    except config_module.ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    # Prime TCC permissions before the daemon is bootstrapped under launchd.
    _prime_permissions()

    result = launchagent.install(cfg)
    if not result.success:
        if result.launchctl_stderr:
            typer.echo(result.launchctl_stderr, err=True)
        typer.echo(f"install failed: {result.message}", err=True)
        raise typer.Exit(code=3)

    pid_str = f"PID {result.pid}" if result.pid is not None else "PID ?"
    verb = "re-registered" if result.re_registered else "registered"
    typer.echo(f"{verb} to start on login; running now ({pid_str})")

    # Blank input clears any previously stored key — re-running install with an
    # empty prompt response is how the user turns transcription off without
    # touching the Keychain by hand. A closed stdin (non-interactive
    # invocation) is treated as blank rather than aborting the install.
    try:
        api_key = typer.prompt(
            "Deepgram API key (leave blank to disable transcription)",
            hide_input=True,
            default="",
            show_default=False,
        ).strip()
    except typer.Abort:
        api_key = ""
    if api_key:
        secrets.set_deepgram_api_key(api_key)
        typer.echo("Deepgram API key stored.")
    else:
        secrets.delete_deepgram_api_key()
        typer.echo("Transcription disabled; any previously stored Deepgram key was cleared.")


@app.command()
def uninstall() -> None:
    """Bootout the LaunchAgent and remove its plist.

    Idempotent — running with no agent registered exits 0.
    """
    result = launchagent.uninstall()
    if not result.success:
        if result.launchctl_stderr:
            typer.echo(result.launchctl_stderr, err=True)
        typer.echo(f"uninstall failed: {result.message}", err=True)
        raise typer.Exit(code=3)

    if result.already_unregistered:
        typer.echo("already not registered")
    else:
        typer.echo("removed from login items; daemon stopped")


# ---------------------------------------------------------------------------
# `record transcribe` — spec 004 slice 1
# ---------------------------------------------------------------------------


def _resolve_wav_path(recording: Path) -> Path | None:
    """Resolve a user-supplied ``recording`` arg to an existing ``.wav`` path.

    Accepts either the ``.wav`` itself or its stem (the path with no suffix, or
    any other suffix). Returns the resolved ``.wav`` path if it exists on disk,
    otherwise ``None``.
    """
    candidate = recording if recording.suffix == ".wav" else recording.with_suffix(
        ".wav"
    )
    if candidate.is_file():
        return candidate
    return None


@app.command()
def transcribe(
    recording: Path = typer.Argument(
        ...,
        help="Path to a recording .wav (or its stem).",
    ),
) -> None:
    """Transcribe a recording's audio into speaker-attributed transcript files.

    Resolves ``recording`` to its ``.wav``, runs the Deepgram backend
    synchronously, and writes ``{stem}.json`` / ``{stem}.txt`` / ``{stem}.srt``
    next to it (overwriting any existing ones).

    Exit codes:
      * 0 — transcription succeeded; three files written.
      * 1 — the ``.wav`` file was not found.
      * 2 — no Deepgram API key configured.
      * 3 — transcription failed (reason printed to stderr and logged).
    """
    log = get_logger("record.cli")

    wav_path = _resolve_wav_path(recording)
    if wav_path is None:
        typer.echo(f"recording not found: {recording}", err=True)
        raise typer.Exit(code=1)

    api_key = secrets.get_deepgram_api_key()
    if api_key is None:
        typer.echo(
            "no Deepgram API key configured — set RECORD_DEEPGRAM_API_KEY or "
            "run `record install` to store one",
            err=True,
        )
        raise typer.Exit(code=2)

    backend = transcribe_module.DeepgramBackend(api_key)
    try:
        transcript = asyncio.run(backend.transcribe(wav_path))
        written = transcribe_module.write_transcript(transcript, wav_path)
    except transcribe_module.TranscriptionError as exc:
        log.error("transcription_failed", recording=str(wav_path), reason=str(exc))
        typer.echo(f"transcription failed: {exc}", err=True)
        raise typer.Exit(code=3) from None
    except Exception as exc:  # unexpected — still fail cleanly, no stack trace.
        log.error(
            "transcription_failed",
            recording=str(wav_path),
            reason=str(exc),
            unexpected=True,
        )
        typer.echo(f"transcription failed: {exc}", err=True)
        raise typer.Exit(code=3) from None

    languages = ", ".join(transcript.language) if transcript.language else "unknown"
    typer.echo(
        f"transcribed {wav_path.name}: {len(transcript.segments)} segments, "
        f"language {languages} -> {written[0].name}, {written[1].name}, "
        f"{written[2].name}"
    )


# ---------------------------------------------------------------------------
# `record daemon ...` — spec 003 slice 1 + 2
# ---------------------------------------------------------------------------


def _daemon_start_impl() -> int:
    """Spawn the daemon detached; wait briefly for the PID file to appear.

    Returns the would-be CLI exit code. Unchanged from slice 1.
    """
    daemon_pid_path = paths.daemon_pid_file()

    existing_pid = state.read_pid_file(path=daemon_pid_path)
    if existing_pid is not None:
        if state.is_alive(existing_pid):
            typer.echo(f"daemon already running (PID {existing_pid})")
            return 0
        state.remove_pid_file(path=daemon_pid_path)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "record.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, FileNotFoundError) as exc:
        typer.echo(f"failed to launch daemon: {exc}", err=True)
        return 3

    deadline = time.monotonic() + _DAEMON_START_HANDSHAKE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_hint = paths.daemon_log_file()
            typer.echo(
                f"daemon failed to start (exit code {proc.returncode}); "
                f"see {log_hint} for clues",
                err=True,
            )
            return 3
        if state.read_pid_file(path=daemon_pid_path) is not None:
            typer.echo(f"daemon started (PID {proc.pid})")
            return 0
        time.sleep(_DAEMON_START_POLL_INTERVAL)

    typer.echo(
        f"daemon spawned (PID {proc.pid}) but PID file did not appear within "
        f"{_DAEMON_START_HANDSHAKE_SECONDS:.0f}s — check {paths.daemon_log_file()}",
        err=True,
    )
    return 0


def _daemon_stop_impl() -> int:
    """Send SIGTERM to the daemon and wait for it to exit. Unchanged from slice 1.

    Note: slice 2's tech spec §2.11 describes ``record daemon stop`` as routed
    through the control socket's ``quit`` request. We keep the SIGTERM path
    for now because the daemon's SIGTERM handler runs the same graceful
    shutdown (it sets the shutdown event, which finalizes any in-flight
    capture in :meth:`Daemon.serve_forever`). Slice 4/5 may switch to the
    socket path once the long-lived Swift child arrives.
    """
    daemon_pid_path = paths.daemon_pid_file()
    pid = state.read_pid_file(path=daemon_pid_path)
    if pid is None:
        typer.echo("daemon is not running", err=True)
        return 1

    if not state.is_alive(pid):
        state.remove_pid_file(path=daemon_pid_path)
        typer.echo("daemon is not running (stale PID cleaned up)", err=True)
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        state.remove_pid_file(path=daemon_pid_path)
        typer.echo("daemon is not running (process disappeared)", err=True)
        return 1

    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not state.is_alive(pid):
            break
        time.sleep(_STOP_POLL_INTERVAL)
    else:
        typer.echo(
            f"daemon (PID {pid}) did not exit within "
            f"{_STOP_TIMEOUT_SECONDS:.0f}s — inspect "
            f"{paths.daemon_log_file()} for clues",
            err=True,
        )
        return 4

    typer.echo("daemon stopped")
    return 0


@daemon_app.command("start")
def daemon_start() -> None:
    """Launch the background daemon for this login session."""
    code = _daemon_start_impl()
    if code != 0:
        raise typer.Exit(code=code)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the running daemon."""
    code = _daemon_stop_impl()
    if code != 0:
        raise typer.Exit(code=code)


@daemon_app.command("restart")
def daemon_restart() -> None:
    """Stop the daemon (if running) and start a fresh one."""
    stop_code = _daemon_stop_impl()
    if stop_code not in (0, 1):
        raise typer.Exit(code=stop_code)
    start_code = _daemon_start_impl()
    if start_code != 0:
        raise typer.Exit(code=start_code)
