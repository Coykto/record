"""Long-running background daemon for the ``record`` orchestrator.

Slice 1 stood up the bare scaffold (PID claim + idle on SIGTERM). Slice 2 of
spec 003 adds:

1. A Unix-domain control socket (:mod:`record.control`) listening at
   :func:`paths.daemon_socket`. CLI commands ``record start`` / ``record stop``
   / ``record status`` now send JSON requests over it.
2. A capture state machine (``IDLE`` → ``STARTING`` → ``RUNNING`` →
   ``STOPPING`` → ``IDLE``) protected by a single :class:`asyncio.Lock` so a
   double-press race (FR 2.5 final bullet) cannot start two captures.
3. Per-request orchestration via :class:`record.capture.CaptureSession`. The
   Swift child is still one-shot in slice 2 — slice 4 introduces ``--daemon``
   mode.

The daemon stays focused on the lifecycle. The hotkey / config / autostart /
sound / banner additions arrive in slices 3-8.
"""

from __future__ import annotations

import asyncio
import enum
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import control, feedback, ipc, launchagent, paths, secrets, state
from . import transcribe as transcribe_module
from .capture import (
    CaptureFailedToStart,
    CaptureSession,
    SwiftChild,
    SwiftChildUnavailable,
)
from .config import Config, ConfigError
from .logging_setup import configure_logging, get_logger

# Exit codes. Surfaced via ``record daemon start`` polling.
_EXIT_OK = 0
_EXIT_ALREADY_RUNNING = 1
_EXIT_SOCKET_BOUND = 1  # other daemon won the socket race — same semantic
_EXIT_CONFIG_INVALID = 2  # spec 003 slice 3: bad config = hard startup failure
_EXIT_BINARY_MISSING = 3  # spec 003 slice 4: swift child cannot be spawned

# Audio capture format. Mirrors `cli._SAMPLE_RATE` etc; both are pinned to the
# 16 kHz / 16-bit / mono target the Deepgram backend is tuned for.
_SAMPLE_RATE = 16000
_BIT_DEPTH = 16
_CHANNELS = 1


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class _CaptureState(enum.Enum):
    """In-memory state of the daemon's single capture slot.

    Tech spec §2.2 #3: there are exactly four states and one asyncio lock
    guards every transition. The handlers below never branch on anything
    other than this enum.
    """

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claim_pid_file(pid: int, *, path: Path | None = None) -> None:
    """Claim the daemon PID file atomically. Idempotent on a stale file."""
    target = path if path is not None else paths.daemon_pid_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    state.claim_pid_file(pid, path=target)


def _remove_pid_file(*, path: Path | None = None) -> None:
    """Remove the daemon PID file; never raises on missing file."""
    target = path if path is not None else paths.daemon_pid_file()
    try:
        state.remove_pid_file(path=target)
    except Exception:  # pragma: no cover - defensive
        pass


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filename_timestamp() -> str:
    """Filename-safe local timestamp like ``2026-05-13T09-21-48``.

    Mirrors ``cli._filename_timestamp``. The daemon computes its own paths in
    slice 2 (the CLI just relays a ``start`` request); slice 3 swaps the CWD
    base for the configured ``output_folder``.
    """
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class Daemon:
    """Owns the capture slot, the state-machine lock, and the control server.

    Constructed once per process. Tests inject a custom ``session_factory`` so
    no real Swift subprocess is spawned — the production factory builds a
    real :class:`CaptureSession`.
    """

    def __init__(
        self,
        *,
        daemon_log_path: Path,
        session_factory: object | None = None,
        output_folder: Path | None = None,
        swift_child: SwiftChild | None = None,
        config: Config | None = None,
    ) -> None:
        self._daemon_log_path = daemon_log_path
        # output_folder is the slice-2 placeholder for slice 3's config-driven
        # value. ``None`` → use CWD, mirroring the legacy CLI's behavior.
        self._output_folder = output_folder

        # Slice 4: the daemon owns one long-lived ``record-capture --daemon``
        # subprocess for its entire lifetime. When ``swift_child`` is None we
        # construct one lazily in ``serve_forever``; tests inject a stub via
        # session_factory and never need a real child.
        self._swift_child: SwiftChild | None = swift_child

        # Slice 5: the parsed config drives hotkey registration on startup.
        # Tests that don't exercise the hotkey path pass ``config=None`` and
        # the daemon simply skips registration (legacy slice-2/4 behavior).
        self._config: Config | None = config

        # session_factory: callable taking (basename, video_output_path) →
        # CaptureSession-like. Default uses the real session.
        if session_factory is None:
            session_factory = self._default_session_factory
        self._session_factory = session_factory

        self._lock = asyncio.Lock()
        self._state: _CaptureState = _CaptureState.IDLE
        self._session: CaptureSession | None = None
        self._stopped_at_unset_time: str | None = None
        self._started_at: str | None = None
        self._capture_id: str | None = None

        # Set by main() once the daemon is fully up so status() can include
        # daemon.started_at.
        self._daemon_started_at = _utcnow_iso()

        # Set by main() to fire the graceful-shutdown sequence.
        self._shutdown_event = asyncio.Event()
        # Background tasks created by handlers (the stop-on-system-event
        # observer); we keep references so they don't get GC'd.
        self._background: set[asyncio.Task[None]] = set()

        # Slice 5: hotkey state. The wire ``hotkey_registered`` event from
        # the Swift child sets ``_hotkey_state``; ``_handle_status`` returns
        # it verbatim. The event is the asyncio signal used by
        # ``_register_configured_hotkey`` to await the registration reply
        # before opening the control socket (tech spec §3 race row).
        self._hotkey_state: control.HotkeyInfo = control.HotkeyInfo(
            state="unregistered"
        )
        self._hotkey_registered_event: asyncio.Event = asyncio.Event()

        self._log = get_logger("record.daemon")

    # ----- Factory --------------------------------------------------------

    def _default_session_factory(
        self, basename: Path, video_output_path: Path | None
    ) -> CaptureSession:
        return CaptureSession(
            basename=basename,
            video_output_path=video_output_path,
            sample_rate=_SAMPLE_RATE,
            bit_depth=_BIT_DEPTH,
            channels=_CHANNELS,
            daemon_log_path=self._daemon_log_path,
            owner_pid=os.getpid(),
            # Bind every session to the daemon's single shared Swift child.
            # ``self._swift_child`` is set in ``serve_forever`` before any
            # session can be requested (a start request can't arrive until
            # the control socket is bound, which happens after).
            child=self._swift_child,
        )

    # ----- Feedback helpers (slice 6) -------------------------------------

    def _audible_feedback_enabled(self) -> bool:
        """Whether the configured ``audible_feedback`` flag permits sound.

        Tests / legacy callers that pass ``config=None`` get the FR 2.9 default
        (audible feedback on) so the slice-2/4 test contracts keep working
        without forcing every test to construct a real :class:`Config`.
        """
        if self._config is None:
            return True
        return self._config.audible_feedback

    def _safe_play_start(self) -> None:
        """Invoke :func:`feedback.play_start`, swallowing exceptions.

        ``feedback`` already swallows :class:`OSError` from ``Popen``; this
        wrap is defense-in-depth so a future bug in the feedback module can
        never escape the state-machine handlers.
        """
        try:
            feedback.play_start(enabled=self._audible_feedback_enabled())
        except Exception as exc:  # pragma: no cover - defensive
            self._log.warning("feedback_play_start_raised", error=str(exc))

    def _safe_play_stop(self) -> None:
        try:
            feedback.play_stop(enabled=self._audible_feedback_enabled())
        except Exception as exc:  # pragma: no cover - defensive
            self._log.warning("feedback_play_stop_raised", error=str(exc))

    def _safe_play_error(self) -> None:
        try:
            feedback.play_error(enabled=self._audible_feedback_enabled())
        except Exception as exc:  # pragma: no cover - defensive
            self._log.warning("feedback_play_error_raised", error=str(exc))

    def _safe_notify(self, message: str) -> None:
        try:
            feedback.notify(message)
        except Exception as exc:  # pragma: no cover - defensive
            self._log.warning("feedback_notify_raised", error=str(exc))

    # ----- Control dispatch ----------------------------------------------

    async def handle_request(
        self, req: control.ControlRequest
    ) -> control.ControlResponse:
        """Route one control request through the state machine."""
        if isinstance(req, control.StartRequest):
            return await self._handle_start()
        if isinstance(req, control.StopRequest):
            return await self._handle_stop()
        if isinstance(req, control.StatusRequest):
            return await self._handle_status()
        if isinstance(req, control.QuitRequest):
            return await self._handle_quit(finalize=req.finalize)
        # Unreachable: parse_request would have raised on an unknown op.
        return control.ControlResponse(  # pragma: no cover - defensive
            status="error", detail="unknown request"
        )

    # ----- Handlers -------------------------------------------------------

    async def _handle_start(self) -> control.ControlResponse:
        # Fast paths for non-idle states. These don't take the lock — a
        # concurrent transition could lie to us, but the worst case is the
        # caller sees "already_running" briefly when the truth is "STOPPING";
        # they retry and we tell them "busy". A unified message that takes
        # the lock first would be cleaner but at the cost of serializing
        # status-equivalent reads behind a slow finalize.
        if self._state == _CaptureState.STARTING:
            return control.ControlResponse(
                status="busy", detail="capture is starting"
            )
        if self._state == _CaptureState.STOPPING:
            return control.ControlResponse(
                status="busy", detail="capture is being finalized"
            )

        # Lock for the actual transition. A concurrent start that loses this
        # race sees self._state == RUNNING below and returns already_running.
        async with self._lock:
            if self._state == _CaptureState.RUNNING:
                return control.ControlResponse(
                    status="already_running",
                    detail="capture already in progress",
                    capture_id=self._capture_id,
                )
            if self._state == _CaptureState.STARTING:
                return control.ControlResponse(
                    status="busy", detail="capture is starting"
                )
            if self._state == _CaptureState.STOPPING:
                return control.ControlResponse(
                    status="busy", detail="capture is being finalized"
                )

            # IDLE → STARTING.
            self._state = _CaptureState.STARTING

        # Spawn the session outside the lock so a slow Swift start doesn't
        # block status() or a concurrent quit(). The state-machine guarantees
        # nothing else is happening to _session right now.
        stem = _filename_timestamp()
        base = self._output_folder if self._output_folder is not None else Path.cwd()
        basename = (base / stem).resolve()
        video_output_path = (base / f"{stem}.mp4").resolve()

        session = self._session_factory(basename, video_output_path)

        try:
            await session.start()
        except CaptureFailedToStart as exc:
            self._log.warning("capture_start_failed", error=str(exc))
            async with self._lock:
                self._state = _CaptureState.IDLE
                self._session = None
            return control.ControlResponse(
                status="error", detail=f"capture failed to start: {exc}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("capture_start_crashed")
            async with self._lock:
                self._state = _CaptureState.IDLE
                self._session = None
            return control.ControlResponse(
                status="error",
                detail=f"capture failed to start: {type(exc).__name__}: {exc}",
            )

        async with self._lock:
            self._session = session
            self._state = _CaptureState.RUNNING
            self._started_at = _utcnow_iso()
            self._capture_id = stem  # filename stem doubles as a session id

        # Watch for the binary stopping on its own (system-event-triggered
        # shutdown). When that happens we drive the same finalize path as an
        # explicit stop request.
        watcher = asyncio.create_task(
            self._watch_for_system_event_stop(session),
            name="record-daemon-system-event-watcher",
        )
        self._background.add(watcher)
        watcher.add_done_callback(self._background.discard)

        # Slice 6: FR 2.8 first bullet. Tink fires on every successful
        # IDLE→RUNNING regardless of the input channel that triggered it
        # (hotkey or socket-driven ``record start``).
        self._safe_play_start()

        # Spec 005: capture now produces two WAVs derived from the basename.
        # Surface both via ``audio_paths`` and keep ``audio_path`` populated
        # with the mic file for the existing single-field CLI surface.
        mic_path = str(basename) + "-mic.wav"
        system_path = str(basename) + "-system.wav"
        return control.ControlResponse(
            status="ok",
            capture_id=stem,
            audio_path=mic_path,
            audio_paths={"mic": mic_path, "system_audio": system_path},
            video_path=str(video_output_path),
        )

    async def _watch_for_system_event_stop(self, session: CaptureSession) -> None:
        """Drive a clean finalize if the binary exits on its own.

        Mirrors the legacy supervisor's "binary emitted stopped without us
        asking" path. We await ``session.stopped_event``; when it fires
        without a concurrent ``stop`` request having transitioned us to
        STOPPING, finalize the session and return to IDLE.
        """
        await session.stopped_event.wait()

        async with self._lock:
            if self._session is not session:
                # Already finalized by an explicit stop request — nothing to do.
                return
            if self._state in (_CaptureState.STOPPING, _CaptureState.IDLE):
                # An explicit stop is in flight; that path will finalize.
                return
            self._state = _CaptureState.STOPPING

        finalized_audio_paths: list[str] = []
        try:
            final = await session.stop()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("capture_system_event_stop_failed", error=str(exc))
            final = None
        else:
            if isinstance(final, dict):
                files = final.get("audio_files") or {}
                if isinstance(files, dict):
                    for entry in files.values():
                        if isinstance(entry, dict):
                            p = entry.get("path")
                            if isinstance(p, str):
                                finalized_audio_paths.append(p)

        async with self._lock:
            self._session = None
            self._state = _CaptureState.IDLE
            self._started_at = None
            self._capture_id = None

        # Spec 004 slice 2: a system-event-driven stop is just as eligible for
        # auto-transcription as an explicit ``record stop``. Spawn after the
        # capture has been confirmed finalized and the state has unwound to
        # IDLE (a transcription failure must never trap the state machine).
        # Spec 005: two WAVs per session — kick off one transcription per file.
        for audio_str in finalized_audio_paths:
            self._spawn_transcription(Path(audio_str))

    # ----- Hotkey routing (slice 5) ---------------------------------------

    def _on_hotkey_event(self, event: ipc.Event) -> None:
        """Dispatch one hotkey-related event from the Swift child.

        Called synchronously from :class:`SwiftChild`'s stdout reader (which
        is itself driven by the asyncio loop). Must not ``await``. For
        ``hotkey_pressed`` we schedule the actual async work as a task so the
        reader doesn't block on a slow start/stop.
        """
        if isinstance(event, ipc.HotkeyRegisteredEvent):
            configured = "+".join([*event.modifiers, event.key])
            if event.status == "registered":
                self._hotkey_state = control.HotkeyInfo(
                    configured=configured,
                    state="registered",
                    message=None,
                )
            elif event.status == "conflict":
                # FR 2.13 third bullet wording — must match verbatim,
                # including the em-dash.
                self._hotkey_state = control.HotkeyInfo(
                    configured=configured,
                    state="conflict",
                    message=(
                        "hotkey may be inactive — another application has "
                        "registered the same combination"
                    ),
                )
            elif event.status == "invalid" and event.message == "accessibility_denied":
                self._hotkey_state = control.HotkeyInfo(
                    configured=configured,
                    state="disabled_no_permission",
                    message=(
                        "Accessibility permission missing — grant in "
                        "System Settings → Privacy & Security → Accessibility"
                    ),
                )
                # Slice 6: tech spec §2.9 — daemon-level warnings the user
                # must know about (Accessibility denied is the canonical
                # example) surface as a banner. No sound: the daemon is in
                # startup, not in a hotkey-press error path.
                self._safe_notify(
                    "Accessibility permission denied — grant in "
                    "System Settings → Privacy & Security → Accessibility "
                    "to enable the hotkey."
                )
            else:
                # status == "invalid" with any other message.
                self._hotkey_state = control.HotkeyInfo(
                    configured=configured,
                    state="invalid",
                    message=event.message,
                )

            self._hotkey_registered_event.set()
            self._log.info(
                "hotkey_registered",
                status=event.status,
                configured=configured,
                message=event.message,
            )
            return

        if isinstance(event, ipc.HotkeyPressedEvent):
            self._log.info("hotkey_pressed")
            task = asyncio.create_task(
                self._on_hotkey_pressed(),
                name="record-daemon-hotkey-press",
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return

        if isinstance(event, ipc.HotkeyUnregisteredEvent):
            self._hotkey_state = control.HotkeyInfo(state="unregistered")
            self._log.info("hotkey_unregistered")
            return

    async def _on_hotkey_pressed(self) -> None:
        """Translate a hotkey press into a start or stop decision.

        Per FR 2.5: a press while ``IDLE`` starts a capture; while ``RUNNING``
        stops it; while ``STARTING`` / ``STOPPING`` is dropped (the
        state-machine handlers re-check state under the lock so the TOCTOU
        window between snapshot here and re-lock there is benign — the
        handlers will return ``busy`` / ``already_running`` / ``not_running``
        as appropriate).

        Never raises.
        """
        try:
            async with self._lock:
                snap = self._state

            if snap == _CaptureState.IDLE:
                resp = await self._handle_start()
                self._log.info(
                    "hotkey_press_start_dispatched", status=resp.status
                )
                # Slice 6 / FR 2.8 third bullet: hotkey-pressed-but-cannot-start
                # plays Funk + raises a banner naming the cause. Socket-driven
                # ``record start`` does NOT take this path — the CLI already
                # echoes the detail to the user's terminal.
                if resp.status != "ok":
                    self._safe_play_error()
                    self._safe_notify(self._hotkey_start_error_message(resp))
            elif snap == _CaptureState.RUNNING:
                resp = await self._handle_stop()
                self._log.info(
                    "hotkey_press_stop_dispatched", status=resp.status
                )
                # A failing stop is exotic (we just observed RUNNING), but
                # treat it symmetrically with start: surface the cause.
                if resp.status != "ok":
                    self._safe_play_error()
                    self._safe_notify(self._hotkey_stop_error_message(resp))
            elif snap in (_CaptureState.STARTING, _CaptureState.STOPPING):
                self._log.info(
                    "hotkey_press_dropped_during_transition", state=snap.value
                )
                # Slice 6: double-press during a transition is FR 2.8's third
                # bullet's "in the middle of stopping a previous capture"
                # branch — Funk + banner.
                self._safe_play_error()
                self._safe_notify(
                    "Cannot start capture — a capture is in transition. "
                    "Try again in a moment."
                )
        except Exception:  # pragma: no cover - defensive
            self._log.exception("hotkey_press_handler_crashed")

    @staticmethod
    def _hotkey_start_error_message(resp: control.ControlResponse) -> str:
        """Map a non-ok start response into a user-facing banner string.

        Keeps the wording in one place so the daemon log line and the banner
        stay aligned with FR 2.8's "naming the specific problem" expectation.
        """
        if resp.status == "already_running":
            return "A capture is already in progress."
        if resp.status == "busy":
            return (
                "Cannot start capture — "
                + (resp.detail or "a capture is in transition")
                + ". Try again in a moment."
            )
        # error or any other non-ok status: use the detail verbatim if we have
        # one (the handlers populate it with the concrete failure reason).
        return resp.detail or "Capture failed to start."

    @staticmethod
    def _hotkey_stop_error_message(resp: control.ControlResponse) -> str:
        """Map a non-ok stop response into a user-facing banner string."""
        if resp.status == "busy":
            return (
                "Cannot stop capture — "
                + (resp.detail or "a capture is in transition")
                + ". Try again in a moment."
            )
        return resp.detail or "Capture failed to stop."

    async def _register_configured_hotkey(self) -> None:
        """Send ``register_hotkey`` and await the ``hotkey_registered`` reply.

        Never raises. On any failure (parse error, no swift child, timeout)
        leaves ``self._hotkey_state`` at its pre-call value and logs a warning
        so the daemon proceeds with the rest of startup. Per tech spec §3 the
        control socket must not bind until this returns — a half-registered
        hotkey could otherwise race a terminal ``record start``.
        """
        from . import hotkey as hotkey_module

        assert self._swift_child is not None
        assert self._config is not None

        # Install the hotkey event handler so HotkeyRegisteredEvent fires our
        # asyncio.Event below — without this the reader would log+drop the
        # event and we'd time out below.
        self._swift_child.set_hotkey_event_handler(self._on_hotkey_event)

        try:
            parsed = hotkey_module.parse(self._config.hotkey)
        except hotkey_module.HotkeyParseError as exc:
            self._log.warning(
                "hotkey_parse_failed_at_register",
                hotkey=self._config.hotkey,
                error=str(exc),
            )
            return

        self._hotkey_registered_event.clear()
        # Pre-set a "pending" state so a status request during the window
        # between sending and receiving the reply reports a sensible value.
        self._hotkey_state = control.HotkeyInfo(
            configured=parsed.canonical(),
            state="unregistered",
            message="registration in progress",
        )

        self._swift_child.send_command(
            ipc.RegisterHotkeyCommand(
                modifiers=list(parsed.modifiers),
                key=parsed.key,
            )
        )

        try:
            await asyncio.wait_for(
                self._hotkey_registered_event.wait(),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            self._log.warning(
                "hotkey_registration_timed_out",
                configured=parsed.canonical(),
            )
            self._hotkey_state = control.HotkeyInfo(
                configured=parsed.canonical(),
                state="unregistered",
                message="hotkey registration timed out",
            )

    async def _handle_stop(self) -> control.ControlResponse:
        async with self._lock:
            if self._state == _CaptureState.IDLE:
                return control.ControlResponse(
                    status="not_running", detail="no capture running"
                )
            if self._state == _CaptureState.STARTING:
                return control.ControlResponse(
                    status="busy", detail="capture is still starting"
                )
            if self._state == _CaptureState.STOPPING:
                return control.ControlResponse(
                    status="busy", detail="capture is already being finalized"
                )

            # RUNNING → STOPPING. Capture a reference so we can release the
            # lock for the actual finalize.
            session = self._session
            assert session is not None
            self._state = _CaptureState.STOPPING

        try:
            final = await session.stop()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("capture_stop_failed", error=str(exc))
            async with self._lock:
                self._session = None
                self._state = _CaptureState.IDLE
            return control.ControlResponse(
                status="error",
                detail=f"capture failed to stop: {type(exc).__name__}: {exc}",
            )

        async with self._lock:
            self._session = None
            self._state = _CaptureState.IDLE
            self._started_at = None
            self._capture_id = None

        # Slice 6: FR 2.8 second bullet. Pop fires on every successful
        # RUNNING→IDLE regardless of input channel.
        self._safe_play_stop()

        # Spec 004 slice 2 + spec 005: kick off auto-transcription per file.
        # Fire-and-forget — the control reply must not wait on Deepgram.
        # ``_spawn_transcription`` is safe to call without a configured key
        # (it logs and returns). With independent mic / system files we now
        # spawn one transcription per produced WAV.
        audio_paths_map: dict[str, str] = {}
        files = final.get("audio_files") or {}
        if isinstance(files, dict):
            for source_name, entry in files.items():
                if isinstance(entry, dict):
                    p = entry.get("path")
                    if isinstance(p, str):
                        audio_paths_map[source_name] = p
                        self._spawn_transcription(Path(p))

        # Confirm the final state was persisted (CaptureSession set final=True
        # before returning). The CLI re-reads `capture-state.json` to print
        # the same summary the legacy stop did. Mic file is the single-field
        # surface for backwards compatibility; ``audio_paths`` carries both.
        return control.ControlResponse(
            status="ok",
            audio_path=audio_paths_map.get("mic"),
            audio_paths=audio_paths_map or None,
            video_path=final.get("video_output_path"),
        )

    # ----- Transcription spawning (spec 004 slice 2) ----------------------

    def _spawn_transcription(self, audio_path: Path) -> None:
        """Fire off a background transcription job for ``audio_path``.

        Tech spec 004 §2.4: on every successful finalize the daemon launches a
        detached transcription task. The control reply has already been (or is
        about to be) returned; this method must not block on Deepgram.

        Behavior:
        - If no API key is configured, log ``transcription_skipped`` at WARNING
          and return. Capture already succeeded — the missing key is a quiet
          configuration outcome, never an error.
        - Otherwise spawn an ``asyncio.Task`` named ``transcribe:<stem>`` that
          awaits :meth:`TranscriptionBackend.transcribe` then writes the three
          transcript files. On any exception, log ``transcription_failed`` at
          ERROR with ``str(exc)`` (never the API key) and swallow — the daemon
          must remain healthy. The task is added to ``self._background`` with a
          done-callback that discards it and consumes any unretrieved
          exception so asyncio doesn't warn at shutdown.
        - The task is named ``transcribe:<stem>`` so the shutdown path in
          :meth:`serve_forever` can partition the background set and avoid
          awaiting in-flight transcriptions on quit.
        """
        api_key = secrets.get_deepgram_api_key()
        if api_key is None:
            self._log.warning(
                "transcription_skipped",
                audio_path=str(audio_path),
                reason="no_api_key",
            )
            return

        backend = transcribe_module.DeepgramBackend(api_key)
        stem = audio_path.stem

        async def _run() -> None:
            # ``stem_path`` is the .wav with its suffix dropped; the writers
            # append .json/.txt/.srt per their contract.
            stem_path = audio_path.with_suffix("")
            try:
                transcript = await backend.transcribe(audio_path)
                transcribe_module.write_transcript(transcript, stem_path)
            except transcribe_module.TranscriptionError as exc:
                self._log.error(
                    "transcription_failed",
                    audio_path=str(audio_path),
                    reason=str(exc),
                )
            except Exception as exc:  # any unexpected failure — log + swallow
                self._log.error(
                    "transcription_failed",
                    audio_path=str(audio_path),
                    reason=str(exc),
                    error_type=type(exc).__name__,
                )

        task = asyncio.create_task(_run(), name=f"transcribe:{stem}")
        self._background.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._background.discard(t)
            # Consume any unretrieved exception so asyncio doesn't warn — _run
            # already swallows internally, but a future refactor could leak.
            try:
                t.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass

        task.add_done_callback(_done)

    def _probe_autostart_registered(self) -> bool:
        """Best-effort ``launchctl print`` probe; never raises."""
        try:
            return launchagent.is_registered()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.warning("autostart_probe_failed", error=str(exc))
            return False

    async def _handle_status(self) -> control.ControlResponse:
        # No state transition; a snapshot is fine without the lock — every
        # field read is a single attribute load. Worst-case staleness is a
        # field updated between two reads, which is acceptable for a status
        # printout.
        if self._state == _CaptureState.RUNNING and self._session is not None:
            session_state = self._session.state
            started_at = self._started_at
            duration: float | None = None
            if started_at:
                try:
                    started_dt = datetime.strptime(
                        started_at, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    duration = (datetime.now(timezone.utc) - started_dt).total_seconds()
                except ValueError:  # pragma: no cover - defensive
                    duration = None
            # Spec 005: status surfaces only the mic-side path on its single
            # ``audio_path`` field; both files are listed in the stop summary.
            audio_files = session_state.get("audio_files") or {}
            mic_entry = audio_files.get("mic") if isinstance(audio_files, dict) else None
            audio_path = (
                mic_entry.get("path") if isinstance(mic_entry, dict) else None
            )
            video_path = session_state.get("video_output_path")
            capture = control.CaptureState(
                running=True,
                started_at=started_at,
                duration_seconds=duration,
                audio_path=audio_path,
                video_path=video_path,
            )
        else:
            capture = control.CaptureState(running=False)

        return control.ControlResponse(
            status="ok",
            daemon=control.DaemonInfo(
                running=True,
                pid=os.getpid(),
                started_at=self._daemon_started_at,
                autostart_registered=self._probe_autostart_registered(),
            ),
            hotkey=self._hotkey_state,
            capture=capture,
        )

    async def _handle_quit(self, *, finalize: bool) -> control.ControlResponse:
        # Refuse to exit with a capture in flight unless the caller opted into
        # the finalize-first path.
        async with self._lock:
            in_progress = self._state in (
                _CaptureState.STARTING,
                _CaptureState.RUNNING,
                _CaptureState.STOPPING,
            )

        if in_progress and not finalize:
            return control.ControlResponse(
                status="capture_in_progress",
                detail=(
                    "capture is running — pass finalize=true to stop it cleanly"
                ),
            )

        # If a capture is running, drive a clean stop first. Reuse the public
        # handler so the state-machine bookkeeping stays in one place.
        if in_progress:
            stop_resp = await self._handle_stop()
            if stop_resp.status not in ("ok", "not_running"):
                # We're trying to exit; surface the issue but proceed to
                # signal shutdown anyway so a wedged STOPPING doesn't trap
                # the daemon forever.
                self._log.warning(
                    "quit_stop_failed_proceeding",
                    detail=stop_resp.detail,
                )

        # Signal the main loop to exit.
        self._shutdown_event.set()
        return control.ControlResponse(status="ok")

    # ----- Lifecycle ------------------------------------------------------

    async def serve_forever(self) -> int:
        """Spawn the Swift child, bind the socket, idle, clean up.

        Returns the desired process exit code.
        """
        # Slice 4: spawn the long-lived ``record-capture --daemon`` child up
        # front. Tests that pass a stub via ``session_factory`` (and don't
        # need a real subprocess) construct the daemon with
        # ``swift_child=None``; that path skips the spawn entirely. Production
        # ``main()`` builds a real :class:`SwiftChild` and hands it in.
        if self._swift_child is not None:
            try:
                await self._swift_child.start()
            except SwiftChildUnavailable as exc:
                self._log.error("swift_child_unavailable", error=str(exc))
                return _EXIT_BINARY_MISSING

        # Slice 5: register the configured hotkey. Tech spec §3 race row: the
        # control socket must NOT bind until the hotkey is registered (or
        # registration has timed out) — otherwise a terminal ``record start``
        # could race a hotkey-press dispatch on a half-registered hotkey.
        if self._swift_child is not None and self._config is not None:
            await self._register_configured_hotkey()

        try:
            server = await control.serve(self.handle_request)
        except control.SocketAlreadyBound as exc:
            self._log.warning("control_socket_already_bound", error=str(exc))
            # If we spawned a child above, take it down before bailing.
            if self._swift_child is not None:
                try:
                    await self._swift_child.shutdown()
                except Exception:  # pragma: no cover - defensive
                    pass
            return _EXIT_SOCKET_BOUND

        self._log.info(
            "daemon_socket_listening",
            socket_path=str(paths.daemon_socket()),
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass

        try:
            await self._shutdown_event.wait()
        finally:
            # If a capture is somehow still running (we got here via SIGTERM
            # without going through quit), finalize it before letting the
            # server close.
            if self._state in (_CaptureState.RUNNING, _CaptureState.STARTING):
                self._log.info("daemon_finalizing_capture_on_shutdown")
                try:
                    await self._handle_stop()
                except Exception:  # pragma: no cover - defensive
                    self._log.exception("shutdown_stop_failed")

            # Partition background tasks. Spec 004 tech spec §2.4 + risk row:
            # in-flight transcription jobs are abandoned at quit time, not
            # awaited — a slow Deepgram call must not gate a ``record quit``.
            # Other background tasks (the system-event watcher) are awaited
            # as before so the finalize bookkeeping completes cleanly.
            transcription_tasks: list[asyncio.Task[None]] = []
            other_tasks: list[asyncio.Task[None]] = []
            for task in list(self._background):
                if (task.get_name() or "").startswith("transcribe:"):
                    transcription_tasks.append(task)
                else:
                    other_tasks.append(task)

            # Log + abandon transcription tasks. We do NOT cancel them: if the
            # loop happens to run them to completion before the process exits,
            # the user gets the transcript files for free. If it doesn't,
            # they're orphaned silently per the functional spec (no retry).
            for task in transcription_tasks:
                if not task.done():
                    name = task.get_name()
                    # Name is ``transcribe:<stem>``; surface the stem so the
                    # log line names the audio the user lost.
                    _, _, stem = name.partition(":")
                    self._log.info(
                        "transcription_abandoned_at_quit",
                        audio_stem=stem,
                    )

            # Cancel any lingering non-transcription background tasks (the
            # system-event watcher) and await them so finalize bookkeeping
            # finishes before the server closes.
            for task in other_tasks:
                if not task.done():
                    task.cancel()
            for task in other_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            server.close()
            try:
                await server.wait_closed()
            except Exception:  # pragma: no cover - defensive
                pass

            # asyncio.start_unix_server leaves the socket file behind on
            # close. Tidy it so a future daemon doesn't probe a stale path.
            try:
                paths.daemon_socket().unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover - defensive
                self._log.warning("daemon_socket_unlink_failed", error=str(exc))

            # Slice 5: best-effort hotkey release. The Swift binary also
            # releases the Carbon hotkey in its SIGTERM handler, but sending
            # the explicit ``unregister_hotkey`` here lets the binary emit a
            # ``hotkey_unregistered`` event for the daemon log first.
            if self._swift_child is not None:
                try:
                    self._swift_child.send_command(ipc.UnregisterHotkeyCommand())
                except Exception as exc:  # pragma: no cover - defensive
                    self._log.warning(
                        "hotkey_unregister_send_failed", error=str(exc)
                    )

            # Slice 4: tear down the long-lived Swift child. ``shutdown()``
            # sends a ``shutdown`` command (and closes stdin as a backstop)
            # so the binary finalizes anything in flight before exiting.
            if self._swift_child is not None:
                try:
                    await self._swift_child.shutdown()
                except Exception as exc:  # pragma: no cover - defensive
                    self._log.warning(
                        "swift_child_shutdown_failed", error=str(exc)
                    )

            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):  # pragma: no cover
                    pass

        return _EXIT_OK

    def _on_signal(self, sig: signal.Signals) -> None:
        if self._shutdown_event.is_set():
            return
        try:
            self._log.info("daemon_stop_signal_received", signal=sig.name)
        except Exception:  # pragma: no cover - defensive
            pass
        self._shutdown_event.set()


# ---------------------------------------------------------------------------
# Module-level entrypoint (used by ``python -m record.daemon``)
# ---------------------------------------------------------------------------


def _startup(
    *,
    pid_file_path: Path | None = None,
    log_path: Path | None = None,
    config: Config | None = None,
) -> int | tuple[Config, Path] | None:
    """Claim the PID file + configure logging. Returns exit code on failure.

    Slice 3 wiring: when ``config`` is not provided, load it from
    ``~/.config/record/config.toml``. A failed load surfaces as
    :data:`_EXIT_CONFIG_INVALID` after logging to the hard-coded daemon log
    so the user gets a diagnostic even if their configured ``log_folder`` is
    the broken path.

    Return shape:
        - ``None`` for legacy callers (slice 1 / slice 2 tests pass
          ``log_path`` explicitly and don't care about the config).
        - ``(config, log_path)`` for :func:`main` when the load succeeded —
          ``main`` uses these to construct :class:`Daemon`.
        - Non-zero int on hard failure.
    """
    # Load config first when the caller didn't inject one. We need it before
    # ensure_dirs because the dirs to create come from the config.
    cfg: Config | None = config
    config_error: ConfigError | None = None
    if cfg is None and log_path is None:
        # Production path. Tests that pass log_path keep getting the legacy
        # hard-coded path without going through config (preserves slice-1/2
        # test contracts).
        try:
            cfg = config_module.load_config()
        except ConfigError as exc:
            config_error = exc
            cfg = None

    # Resolve directory creation + log path.
    if cfg is not None:
        try:
            paths.ensure_dirs_from_config(cfg)
        except ConfigError as exc:
            config_error = exc
            cfg = None

    if config_error is not None:
        # Configure logging to the hard-coded fallback so the user gets a
        # diagnostic in the standard location.
        paths.ensure_daemon_dirs()
        try:
            configure_logging(log_path=paths.daemon_log_file())
            log = get_logger("record.daemon")
            log.error("daemon_config_invalid", error=str(config_error))
        except Exception:  # pragma: no cover - defensive
            pass
        return _EXIT_CONFIG_INVALID

    if cfg is None:
        # Legacy / test path: no config injected, no production load requested.
        paths.ensure_daemon_dirs()
    # cfg is now either the loaded Config or None (legacy path).

    target_pid = pid_file_path if pid_file_path is not None else paths.daemon_pid_file()
    if log_path is not None:
        target_log = log_path
    elif cfg is not None:
        target_log = paths.resolve_daemon_log_file(cfg)
    else:
        target_log = paths.daemon_log_file()

    try:
        _claim_pid_file(os.getpid(), path=target_pid)
    except state.CaptureAlreadyRunning as exc:
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
    log.info(
        "daemon started",
        pid=os.getpid(),
        daemon_log_path=str(target_log),
        daemon_pid_file=str(target_pid),
    )

    if cfg is not None:
        # FR 2.14: the configuration-loaded log line names every resolved
        # field so the user has one timestamped record of what the daemon is
        # actually running with.
        log.info(
            "daemon_config_loaded",
            hotkey=cfg.hotkey,
            output_folder=str(cfg.output_folder),
            log_folder=str(cfg.log_folder),
            audible_feedback=cfg.audible_feedback,
            hotkey_parse_error=cfg.hotkey_parse_error,
        )
        return (cfg, target_log)
    return None


async def _run(shutdown_event: asyncio.Event | None = None) -> int:
    """Slice-1 compatible coroutine.

    Tests pass a pre-set event to drive the coroutine through one cycle
    without touching real signals. The slice-2 production path goes through
    :meth:`Daemon.serve_forever` instead.
    """
    log = get_logger("record.daemon")
    loop = asyncio.get_running_loop()

    event = shutdown_event if shutdown_event is not None else asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
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

    handlers_installed: list[signal.Signals] = []
    if shutdown_event is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_signal, sig)
                handlers_installed.append(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass

    try:
        await event.wait()
    finally:
        for sig in handlers_installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass

    return _EXIT_OK


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001 - reserved
    """Synchronous entrypoint. Returns the process exit code."""
    startup_result = _startup()
    if isinstance(startup_result, int):
        return startup_result

    log = get_logger("record.daemon")

    cfg: Config | None
    target_log: Path
    if isinstance(startup_result, tuple):
        cfg, target_log = startup_result
    else:
        cfg = None
        target_log = paths.daemon_log_file()

    output_folder = cfg.output_folder if cfg is not None else None

    async def _serve() -> int:
        # Slice 4: one ``record-capture --daemon`` for the daemon's lifetime.
        # Reused across every start/stop cycle. The bounded restart loop
        # inside :class:`SwiftChild` handles unexpected exits (tech spec §3
        # risk row 1).
        swift_child = SwiftChild(
            daemon_log_path=target_log,
            daemon=True,
        )
        daemon = Daemon(
            daemon_log_path=target_log,
            output_folder=output_folder,
            swift_child=swift_child,
            config=cfg,
        )
        return await daemon.serve_forever()

    try:
        rc = asyncio.run(_serve())
        log.info("daemon stopped", pid=os.getpid())
        return rc
    except Exception as exc:  # pragma: no cover - defensive
        log.error("daemon_crashed", error=str(exc), error_type=type(exc).__name__)
        return 1
    finally:
        _remove_pid_file()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))


__all__ = [
    "Daemon",
    "main",
    "_CaptureState",
    "_EXIT_CONFIG_INVALID",
    "_EXIT_BINARY_MISSING",
    "_claim_pid_file",
    "_remove_pid_file",
    "_run",
    "_startup",
]
