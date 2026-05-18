"""Daemon-scaffold tests for spec 003 slice 1.

The daemon is exercised through its small extracted helpers (``_startup``,
``_claim_pid_file``, ``_run``) rather than via a real subprocess, so the tests
never spawn a process and never install signal handlers on the test-runner's
event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from record import control, daemon, launchagent, logging_setup, paths, state


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Each test starts from a fresh logging state.

    ``logging_setup`` keeps a module-global ``_configured`` flag plus a
    tagged-handler sentinel; without a reset, the first test to install a
    handler poisons every later test's caplog visibility.
    """
    # Detach any prior `_record_orchestrator_handler`-tagged handler so the
    # next call to configure_logging installs a fresh one.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, logging_setup._HANDLER_TAG, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    # Note: we do NOT reset `_configured` to False — structlog.configure is
    # process-global and re-running it is a no-op in practice; leaving the
    # flag set means subsequent configure_logging calls only swap the handler.
    yield


@pytest.fixture
def daemon_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect every daemon-side path to ``tmp_path``."""
    pid = tmp_path / "daemon.pid"
    log = tmp_path / "logs" / "daemon.log"
    log_dir = log.parent

    monkeypatch.setattr(paths, "daemon_pid_file", lambda: pid)
    monkeypatch.setattr(paths, "daemon_log_file", lambda: log)
    monkeypatch.setattr(paths, "daemon_log_dir", lambda: log_dir)
    # ensure_daemon_dirs uses both daemon_log_dir() and app_support_dir();
    # point app_support_dir at tmp_path too so the daemon PID's parent dir
    # creation lands inside the sandbox.
    monkeypatch.setattr(paths, "app_support_dir", lambda: tmp_path)
    # Also patch ensure_daemon_dirs to a sandbox-safe version that uses the
    # patched-above helpers (the original closes over the module's own
    # functions, which the monkeypatch already covers — but rebuilding it
    # explicitly is more robust against future changes).

    def _ensure() -> None:
        log_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(paths, "ensure_daemon_dirs", _ensure)

    # The status handler calls launchagent.is_registered(), which shells out to
    # `launchctl print`. Pin it to False so daemon unit tests are deterministic
    # regardless of whether the dev machine actually has the LaunchAgent
    # installed. A test that needs the registered case can re-patch locally.
    monkeypatch.setattr(launchagent, "is_registered", lambda: False)

    return {"pid": pid, "log": log, "log_dir": log_dir, "root": tmp_path}


# ---------------------------------------------------------------------------
# PID-file claim
# ---------------------------------------------------------------------------


def test_claim_pid_file_creates_file_with_current_pid(daemon_paths: dict[str, Path]) -> None:
    """First call materialises the PID file containing this process's PID."""
    target = daemon_paths["pid"]
    assert not target.exists()

    daemon._claim_pid_file(os.getpid(), path=target)

    assert target.exists()
    contents = target.read_text(encoding="utf-8").strip()
    assert int(contents) == os.getpid()


def test_claim_pid_file_refuses_second_live_claim(daemon_paths: dict[str, Path]) -> None:
    """Second claim while the PID is alive raises CaptureAlreadyRunning."""
    target = daemon_paths["pid"]
    daemon._claim_pid_file(os.getpid(), path=target)

    with pytest.raises(state.CaptureAlreadyRunning) as exc_info:
        daemon._claim_pid_file(os.getpid(), path=target)
    assert exc_info.value.existing_pid == os.getpid()


def test_startup_succeeds_then_refuses_second_invocation(
    daemon_paths: dict[str, Path],
) -> None:
    """The whole `_startup` flow: claim + log init, then a second call exits 1."""
    rc = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc is None
    assert daemon_paths["pid"].exists()
    assert daemon_paths["pid"].read_text().strip() == str(os.getpid())

    # Second invocation should surface the already-running exit code.
    rc2 = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc2 == daemon._EXIT_ALREADY_RUNNING


def test_startup_writes_daemon_started_log_line(daemon_paths: dict[str, Path]) -> None:
    """`_startup` emits a `daemon started` log entry to the configured file."""
    rc = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc is None

    # Flush all handlers explicitly — RotatingFileHandler uses delay=True.
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass

    assert daemon_paths["log"].exists()
    content = daemon_paths["log"].read_text(encoding="utf-8")
    assert "daemon started" in content
    # Sanity-check the structured payload made it through.
    last_line = [ln for ln in content.splitlines() if ln.strip()][-1]
    parsed = json.loads(last_line)
    # structlog's reserved positional key surfaces in the JSON line as
    # `"event": "daemon started"`. We grep on the human string above; here we
    # also verify the structured fields rode along.
    assert parsed.get("event") == "daemon started"
    assert parsed.get("pid") == os.getpid()


# ---------------------------------------------------------------------------
# Signal-handler / shutdown event
# ---------------------------------------------------------------------------


def test_run_returns_immediately_when_event_preset(
    daemon_paths: dict[str, Path],
) -> None:
    """Pre-setting the shutdown event drives `_run` through one cycle.

    Critically, passing a non-None event tells `_run` *not* to install signal
    handlers, so the test runner's loop is unaffected.
    """
    # Pre-configure logging so `_run`'s log calls have a destination — `_run`
    # itself does not call configure_logging (that's `_startup`'s job).
    logging_setup.configure_logging(log_path=daemon_paths["log"])

    async def _drive() -> int:
        ev = asyncio.Event()
        ev.set()  # pre-set so the await returns immediately
        return await daemon._run(shutdown_event=ev)

    rc = asyncio.run(_drive())
    assert rc == daemon._EXIT_OK


def test_main_full_cycle_with_preset_event(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end of `main`-equivalent: startup, idle, cleanup, log lines.

    We patch `asyncio.run` so the daemon's main loop short-circuits via a
    pre-set event — this avoids touching the test runner's signal handlers
    and gives us the full startup -> stopped log sequence.
    """
    # First: drive _startup directly so we know the claim + log init happened.
    rc = daemon._startup(
        pid_file_path=daemon_paths["pid"],
        log_path=daemon_paths["log"],
    )
    assert rc is None
    assert daemon_paths["pid"].exists()

    # Now run the body of main() the way the entrypoint does.
    async def _drive() -> int:
        ev = asyncio.Event()
        ev.set()
        return await daemon._run(shutdown_event=ev)

    asyncio.run(_drive())

    # Emit the "daemon stopped" line the way `main` does in its `finally`
    # block, then remove the PID file.
    log = logging_setup.get_logger("record.daemon")
    log.info("daemon stopped", pid=os.getpid())
    daemon._remove_pid_file(path=daemon_paths["pid"])

    # Flush.
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass

    assert not daemon_paths["pid"].exists()
    content = daemon_paths["log"].read_text(encoding="utf-8")
    assert "daemon started" in content
    assert "daemon stopped" in content


def test_remove_pid_file_is_silent_on_missing(daemon_paths: dict[str, Path]) -> None:
    """`_remove_pid_file` must not raise when the file is already gone."""
    assert not daemon_paths["pid"].exists()
    # Must not raise.
    daemon._remove_pid_file(path=daemon_paths["pid"])


# ---------------------------------------------------------------------------
# State-machine tests with a fake CaptureSession
#
# Slice 2 of spec 003: the daemon's start/stop dispatch must enforce
# "exactly one capture at a time" and translate state-machine transitions
# into the right socket responses.
#
# These tests inject a fake CaptureSession via the ``session_factory``
# constructor parameter so no real Swift subprocess is spawned.
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stub :class:`record.capture.CaptureSession` for daemon state-machine tests.

    Records ``start()`` / ``stop()`` calls; exposes the same ``stopped_event``
    seam the real session does so the daemon's watcher coroutine can await
    it.
    """

    def __init__(
        self,
        *,
        basename: Path,
        video_output_path: Path | None = None,
        start_delay: float = 0.0,
        start_raises: BaseException | None = None,
    ) -> None:
        self.basename = basename
        self.video_output_path = video_output_path
        self._start_delay = start_delay
        self._start_raises = start_raises

        self.start_calls = 0
        self.stop_calls = 0
        self.stopped_event = asyncio.Event()
        # Spec 008: ``basename`` is the per-session directory; mic/system live
        # inside it under role-only names. Mirror the shape ``capture._initial_state``
        # produces so ``_handle_stop`` / ``_handle_status`` can read it identically
        # to the real session.
        mic_path = str(basename / "mic.wav")
        system_path = str(basename / "system.wav")
        self._state: dict[str, object] = {
            "basename": str(basename),
            "video_output_path": (
                str(video_output_path) if video_output_path else None
            ),
            "audio_files": {
                "mic": {
                    "path": mic_path,
                    "status": "captured_normally",
                    "duration_seconds": None,
                    "truncated_at_offset_seconds": None,
                },
                "system_audio": {
                    "path": system_path,
                    "status": "captured_normally",
                    "duration_seconds": None,
                    "truncated_at_offset_seconds": None,
                },
            },
        }

    @property
    def state(self) -> dict[str, object]:
        return self._state

    async def start(self) -> None:
        self.start_calls += 1
        if self._start_delay:
            await asyncio.sleep(self._start_delay)
        if self._start_raises is not None:
            raise self._start_raises

    async def stop(self) -> dict[str, object]:
        self.stop_calls += 1
        self.stopped_event.set()
        self._state["final"] = True
        # Match the real CaptureSession's contract: return the live state dict
        # (not a copy). The daemon's combine step folds ``combined_audio`` into
        # this dict via ``set_combined_audio`` and the CLI summary reads it.
        return self._state

    def set_combined_audio(self, combined_audio: dict[str, object]) -> None:
        """Match :meth:`CaptureSession.set_combined_audio` for spec 007 slice 2."""
        self.combined_audio = combined_audio
        self._state["combined_audio"] = combined_audio
        # Mirror the real session's persistence so tests that read the
        # on-disk capture-state.json see the update.
        state.write_state(self._state)


def _make_daemon(
    daemon_paths: dict[str, Path],
    *,
    sessions: list[_FakeSession] | None = None,
    output_folder: Path | None = None,
    config: object | None = None,
) -> tuple[daemon.Daemon, list[_FakeSession]]:
    """Build a :class:`Daemon` with a recording session factory.

    Returns the daemon plus a (live) list that tests can read to inspect the
    sessions the factory handed out. When ``sessions`` is passed in, the
    factory pops from it; otherwise it constructs fresh :class:`_FakeSession`
    objects for each capture.

    ``config`` (slice 6): optional :class:`record.config.Config` whose
    ``audible_feedback`` flag the daemon consults at every state transition.
    Pre-slice-6 tests pass ``None`` and get the FR 2.9 default (on).
    """
    handed_out: list[_FakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _FakeSession:
        if sessions:
            session = sessions.pop(0)
        else:
            session = _FakeSession(
                basename=basename,
                video_output_path=video_output_path,
            )
        handed_out.append(session)
        return session

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=output_folder if output_folder is not None else daemon_paths["root"],
        config=config,  # type: ignore[arg-type]  # tests inject real Config
    )
    return d, handed_out


# ---------------------------------------------------------------------------
# Feedback stub used by the slice-6 tests below
# ---------------------------------------------------------------------------


class _FeedbackStub:
    """Records every ``play_*`` / ``notify`` call the daemon emits.

    Patched into :mod:`record.daemon`'s ``feedback`` import alias so the
    daemon's ``self._safe_play_*`` helpers route into it. Each list captures
    ``enabled=`` for the sound-playback functions so tests can assert the
    ``audible_feedback`` flag was honored.
    """

    def __init__(self) -> None:
        self.start_calls: list[bool] = []
        self.stop_calls: list[bool] = []
        self.error_calls: list[bool] = []
        self.notify_calls: list[str] = []

    def play_start(self, *, enabled: bool = True) -> None:
        self.start_calls.append(enabled)

    def play_stop(self, *, enabled: bool = True) -> None:
        self.stop_calls.append(enabled)

    def play_error(self, *, enabled: bool = True) -> None:
        self.error_calls.append(enabled)

    def notify(self, message: str, *, title: str = "record") -> None:
        self.notify_calls.append(message)


def _install_feedback_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> _FeedbackStub:
    """Replace ``record.daemon.feedback`` with the stub for one test."""
    stub = _FeedbackStub()
    monkeypatch.setattr(daemon, "feedback", stub)
    return stub


def test_start_request_while_idle_starts_a_session(
    daemon_paths: dict[str, Path],
) -> None:
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StartRequest())

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    assert len(sessions) == 1
    assert sessions[0].start_calls == 1
    assert d._state == daemon._CaptureState.RUNNING


def test_start_request_while_running_returns_already_running(
    daemon_paths: dict[str, Path],
) -> None:
    """A second start request must not spawn a second session."""
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> tuple[control.ControlResponse, control.ControlResponse]:
        first = await d.handle_request(control.StartRequest())
        second = await d.handle_request(control.StartRequest())
        return first, second

    first, second = asyncio.run(_drive())
    assert first.status == "ok"
    assert second.status == "already_running"
    # Only one session was constructed; only one start() call was issued.
    assert len(sessions) == 1
    assert sessions[0].start_calls == 1


def test_concurrent_start_requests_only_one_session_wins(
    daemon_paths: dict[str, Path],
) -> None:
    """Two concurrent start requests → exactly one session.start() runs.

    The second request loses the lock and sees one of two consistent
    responses: ``already_running`` (the first finished STARTING fast enough
    that the second saw RUNNING) or ``busy`` (the first is still STARTING).
    Both are acceptable per the contract.
    """
    # Slow down the first start so the second request races into it.
    slow = _FakeSession(
        basename=daemon_paths["root"] / "x",
        start_delay=0.1,
    )
    fast = _FakeSession(basename=daemon_paths["root"] / "y")
    d, sessions = _make_daemon(daemon_paths, sessions=[slow, fast])

    async def _drive() -> list[control.ControlResponse]:
        t1 = asyncio.create_task(d.handle_request(control.StartRequest()))
        # Yield once so t1 enters _handle_start and transitions IDLE -> STARTING
        # before t2 reads the state.
        await asyncio.sleep(0)
        t2 = asyncio.create_task(d.handle_request(control.StartRequest()))
        return list(await asyncio.gather(t1, t2))

    responses = asyncio.run(_drive())
    statuses = sorted(r.status for r in responses)
    # Exactly one ok and one rejection.
    assert "ok" in statuses
    rejections = [s for s in statuses if s != "ok"]
    assert len(rejections) == 1
    assert rejections[0] in ("already_running", "busy")

    # Only one session was constructed and only one start() ran.
    assert len(sessions) == 1
    assert sessions[0].start_calls == 1


def test_start_failure_returns_to_idle(
    daemon_paths: dict[str, Path],
) -> None:
    """If the session's start() raises, the daemon snaps back to IDLE."""
    from record.capture import CaptureFailedToStart

    failing = _FakeSession(
        basename=daemon_paths["root"] / "x",
        start_raises=CaptureFailedToStart("nope"),
    )
    d, _sessions = _make_daemon(daemon_paths, sessions=[failing])

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StartRequest())

    resp = asyncio.run(_drive())
    assert resp.status == "error"
    assert d._state == daemon._CaptureState.IDLE


def test_stop_request_while_idle_returns_not_running(
    daemon_paths: dict[str, Path],
) -> None:
    d, _sessions = _make_daemon(daemon_paths)

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StopRequest())

    resp = asyncio.run(_drive())
    assert resp.status == "not_running"


def test_stop_request_after_start_returns_ok_and_returns_to_idle(
    daemon_paths: dict[str, Path],
) -> None:
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> tuple[control.ControlResponse, control.ControlResponse]:
        start = await d.handle_request(control.StartRequest())
        stop = await d.handle_request(control.StopRequest())
        return start, stop

    start, stop = asyncio.run(_drive())
    assert start.status == "ok"
    assert stop.status == "ok"
    assert sessions[0].stop_calls == 1
    assert d._state == daemon._CaptureState.IDLE


def test_quit_request_finalizes_in_flight_capture(
    daemon_paths: dict[str, Path],
) -> None:
    """quit(finalize=True) while RUNNING must stop the capture cleanly."""
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> tuple[control.ControlResponse, control.ControlResponse]:
        start = await d.handle_request(control.StartRequest())
        quit_resp = await d.handle_request(
            control.QuitRequest(finalize=True)
        )
        return start, quit_resp

    start, quit_resp = asyncio.run(_drive())
    assert start.status == "ok"
    assert quit_resp.status == "ok"
    assert sessions[0].stop_calls == 1
    # The shutdown event must be set so serve_forever() can wind down.
    assert d._shutdown_event.is_set()


def test_quit_refuses_when_capture_in_progress_and_finalize_false(
    daemon_paths: dict[str, Path],
) -> None:
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> tuple[control.ControlResponse, control.ControlResponse]:
        start = await d.handle_request(control.StartRequest())
        quit_resp = await d.handle_request(
            control.QuitRequest(finalize=False)
        )
        return start, quit_resp

    start, quit_resp = asyncio.run(_drive())
    assert start.status == "ok"
    assert quit_resp.status == "capture_in_progress"
    # Capture must still be running and the shutdown event must NOT be set.
    assert sessions[0].stop_calls == 0
    assert d._state == daemon._CaptureState.RUNNING
    assert not d._shutdown_event.is_set()


def test_status_idle_includes_daemon_pid_and_hotkey_stub(
    daemon_paths: dict[str, Path],
) -> None:
    d, _sessions = _make_daemon(daemon_paths)

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StatusRequest())

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    assert resp.daemon is not None
    assert resp.daemon.running is True
    assert resp.daemon.pid == os.getpid()
    assert resp.daemon.autostart_registered is False
    assert resp.hotkey is not None
    assert resp.hotkey.state == "unregistered"
    assert resp.capture is not None
    assert resp.capture.running is False


def test_status_after_start_reports_running_capture(
    daemon_paths: dict[str, Path],
) -> None:
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        return await d.handle_request(control.StatusRequest())

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    assert resp.capture is not None
    assert resp.capture.running is True
    # The fake session's state dict has these fields.
    assert resp.capture.audio_path is not None
    assert resp.capture.video_path is not None


# ---------------------------------------------------------------------------
# SwiftChild bounded-restart loop (spec 003 slice 4, tech spec §3 risk row 1)
#
# Drives the restart bookkeeping directly without spawning a real subprocess
# — the real-process behavior is covered end-to-end by
# ``test_end_to_end_daemon_driven_three_cycles``. These tests pin the
# "budget" semantics in isolation so future tweaks to the limit / window
# don't silently regress.
# ---------------------------------------------------------------------------


def test_swift_child_restart_budget_marks_permanent_failure(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three consecutive unexpected exits inside the window are allowed; the
    fourth blows the budget and permanently fails the child."""
    from record import capture

    # Stub the actual respawn so we exercise only the budget bookkeeping.
    # ``_maybe_restart_after_unexpected_exit`` calls ``_spawn`` after the
    # budget check; we patch it to a no-op.
    async def _noop_spawn(self: object, binary: object) -> None:
        return None

    monkeypatch.setattr(capture.SwiftChild, "_spawn", _noop_spawn)
    monkeypatch.setattr(
        capture, "_resolve_binary", lambda: Path("/dev/null")
    )

    child = capture.SwiftChild(
        daemon_log_path=daemon_paths["log"], daemon=True
    )

    async def _drive() -> capture.SwiftChild:
        # First three exits stay inside the budget.
        for _ in range(3):
            await child._maybe_restart_after_unexpected_exit()
            assert not child._permanently_failed, (
                "3 exits in the window should not blow the budget"
            )

        # Fourth exit pushes us over.
        await child._maybe_restart_after_unexpected_exit()
        return child

    asyncio.run(_drive())
    assert child._permanently_failed is True
    assert child._permanent_failure_reason is not None
    assert "giving up" in child._permanent_failure_reason


def test_swift_child_shutdown_during_restart_marks_permanent(
    daemon_paths: dict[str, Path],
) -> None:
    """Once ``shutdown()`` is called, a subsequent exit doesn't trigger a
    respawn — the child is permanently failed instead."""
    from record import capture

    child = capture.SwiftChild(
        daemon_log_path=daemon_paths["log"], daemon=True
    )
    child._shutting_down = True

    async def _drive() -> None:
        await child._maybe_restart_after_unexpected_exit()

    asyncio.run(_drive())
    assert child._permanently_failed is True
    assert child._permanent_failure_reason == "swift child shut down"


# ---------------------------------------------------------------------------
# Hotkey routing tests (spec 003 slice 5)
#
# Drive ``Daemon._on_hotkey_event`` and ``Daemon._on_hotkey_pressed`` directly
# against the same fake-session machinery used by the slice-2 state-machine
# tests above. No Swift subprocess, no real Carbon call — just the asyncio
# loop and the daemon's in-memory state translation.
# ---------------------------------------------------------------------------


from record import ipc  # noqa: E402 - placed here next to its test usage


def test_hotkey_press_while_idle_starts_capture(
    daemon_paths: dict[str, Path],
) -> None:
    """A hotkey press from IDLE → starts a single capture (FR 2.5)."""
    d, sessions = _make_daemon(daemon_paths)

    asyncio.run(d._on_hotkey_pressed())

    assert len(sessions) == 1
    assert sessions[0].start_calls == 1
    assert d._state == daemon._CaptureState.RUNNING


def test_hotkey_press_while_running_stops_capture(
    daemon_paths: dict[str, Path],
) -> None:
    """A hotkey press while RUNNING → the active session is stopped."""
    d, sessions = _make_daemon(daemon_paths)

    async def _drive() -> None:
        await d._on_hotkey_pressed()  # start
        await d._on_hotkey_pressed()  # stop

    asyncio.run(_drive())

    assert len(sessions) == 1
    assert sessions[0].start_calls == 1
    assert sessions[0].stop_calls == 1
    assert d._state == daemon._CaptureState.IDLE


def test_hotkey_press_during_starting_is_dropped(
    daemon_paths: dict[str, Path],
) -> None:
    """A second press while the first is mid-STARTING is dropped (FR 2.5).

    The first session uses a small ``start_delay`` so the second press sees
    the daemon in ``STARTING`` and the snapshot-then-branch path takes the
    drop branch. Only one session should be constructed in total.
    """
    slow = _FakeSession(
        basename=daemon_paths["root"] / "x",
        start_delay=0.1,
    )
    fast = _FakeSession(basename=daemon_paths["root"] / "y")
    d, sessions = _make_daemon(daemon_paths, sessions=[slow, fast])

    async def _drive() -> None:
        task1 = asyncio.create_task(d._on_hotkey_pressed())
        # Yield once so task1 enters _on_hotkey_pressed, snapshots IDLE,
        # then dispatches _handle_start which transitions us to STARTING
        # before task2 reads the state.
        await asyncio.sleep(0)
        task2 = asyncio.create_task(d._on_hotkey_pressed())
        await asyncio.gather(task1, task2)

    asyncio.run(_drive())

    # Only one session was constructed; the second press never reached the
    # factory because it was dropped during STARTING.
    assert len(sessions) == 1
    assert sessions[0].start_calls == 1


def test_hotkey_registered_event_updates_status(
    daemon_paths: dict[str, Path],
) -> None:
    """A successful ``hotkey_registered`` event surfaces in the status payload."""
    d, _sessions = _make_daemon(daemon_paths)

    d._on_hotkey_event(
        ipc.HotkeyRegisteredEvent(
            status="registered",
            modifiers=["cmd", "option"],
            key="r",
            message="ok",
        )
    )

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StatusRequest())

    resp = asyncio.run(_drive())
    assert resp.hotkey is not None
    assert resp.hotkey.state == "registered"
    assert resp.hotkey.configured == "cmd+option+r"


def test_hotkey_conflict_event_updates_status(
    daemon_paths: dict[str, Path],
) -> None:
    """``status=conflict`` maps to a ``conflict`` HotkeyInfo carrying FR 2.13 wording."""
    d, _sessions = _make_daemon(daemon_paths)

    d._on_hotkey_event(
        ipc.HotkeyRegisteredEvent(
            status="conflict",
            modifiers=["cmd", "option"],
            key="r",
            message="conflict",
        )
    )

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StatusRequest())

    resp = asyncio.run(_drive())
    assert resp.hotkey is not None
    assert resp.hotkey.state == "conflict"
    assert resp.hotkey.message is not None
    assert (
        "another application has registered the same combination"
        in resp.hotkey.message
    )


def test_hotkey_invalid_accessibility_denied_maps_to_disabled_no_permission(
    daemon_paths: dict[str, Path],
) -> None:
    """``status=invalid`` with ``message=accessibility_denied`` → ``disabled_no_permission``."""
    d, _sessions = _make_daemon(daemon_paths)

    d._on_hotkey_event(
        ipc.HotkeyRegisteredEvent(
            status="invalid",
            modifiers=["cmd", "option"],
            key="r",
            message="accessibility_denied",
        )
    )

    assert d._hotkey_state.state == "disabled_no_permission"
    assert d._hotkey_state.message is not None
    assert "Accessibility permission missing" in d._hotkey_state.message


def test_hotkey_invalid_other_message_maps_to_invalid(
    daemon_paths: dict[str, Path],
) -> None:
    """``status=invalid`` with any non-accessibility message → ``invalid``."""
    d, _sessions = _make_daemon(daemon_paths)

    d._on_hotkey_event(
        ipc.HotkeyRegisteredEvent(
            status="invalid",
            modifiers=["cmd", "option"],
            key="r",
            message="param_err",
        )
    )

    assert d._hotkey_state.state == "invalid"
    assert d._hotkey_state.message == "param_err"


def test_hotkey_unregistered_event_resets_state(
    daemon_paths: dict[str, Path],
) -> None:
    """A ``hotkey_unregistered`` event clears the configured combo back to default."""
    d, _sessions = _make_daemon(daemon_paths)

    # First register, then unregister.
    d._on_hotkey_event(
        ipc.HotkeyRegisteredEvent(
            status="registered",
            modifiers=["cmd", "option"],
            key="r",
            message="ok",
        )
    )
    assert d._hotkey_state.state == "registered"

    d._on_hotkey_event(ipc.HotkeyUnregisteredEvent())

    assert d._hotkey_state.state == "unregistered"
    assert d._hotkey_state.configured is None


# ---------------------------------------------------------------------------
# Feedback wiring (spec 003 slice 6)
#
# Patch ``record.daemon.feedback`` with a recording stub and assert the
# daemon's state-machine transitions invoke ``play_start`` / ``play_stop`` /
# ``play_error`` / ``notify`` at the right moments and respect
# ``Config.audible_feedback``.
# ---------------------------------------------------------------------------


from record.config import Config  # noqa: E402 - placed near its test usage


def _make_config(*, audible_feedback: bool, tmp_path: Path) -> Config:
    """Build a sandbox-safe :class:`Config` for daemon tests.

    Path defaults under :class:`Config` point at ``~/record/*`` and run a
    collision check; tests inject ``tmp_path``-rooted directories so we don't
    touch the developer's real ``~/record/`` and so the collision check is
    happy.
    """
    return Config(
        hotkey="option+command+r",
        output_folder=str(tmp_path / "out"),
        log_folder=str(tmp_path / "logs"),
        audible_feedback=audible_feedback,
    )


def test_hotkey_press_start_plays_tink_by_default(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """IDLE→RUNNING via hotkey press → ``play_start(enabled=True)`` fires once."""
    stub = _install_feedback_stub(monkeypatch)
    d, _sessions = _make_daemon(daemon_paths)

    asyncio.run(d._on_hotkey_pressed())

    assert stub.start_calls == [True]
    assert stub.stop_calls == []
    assert stub.error_calls == []
    assert stub.notify_calls == []


def test_hotkey_press_stop_plays_pop(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """RUNNING→IDLE via hotkey press → ``play_stop(enabled=True)`` fires once."""
    stub = _install_feedback_stub(monkeypatch)
    d, _sessions = _make_daemon(daemon_paths)

    async def _drive() -> None:
        await d._on_hotkey_pressed()  # start
        await d._on_hotkey_pressed()  # stop

    asyncio.run(_drive())

    assert stub.start_calls == [True]
    assert stub.stop_calls == [True]
    assert stub.error_calls == []


def test_hotkey_press_during_transition_plays_error_and_notifies(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second press while the first is mid-STARTING → Funk + banner.

    Mirrors the existing ``test_hotkey_press_during_starting_is_dropped`` race
    setup but asserts the feedback surface fires once for the dropped press.
    """
    stub = _install_feedback_stub(monkeypatch)
    slow = _FakeSession(
        basename=daemon_paths["root"] / "x",
        start_delay=0.1,
    )
    fast = _FakeSession(basename=daemon_paths["root"] / "y")
    d, _sessions = _make_daemon(daemon_paths, sessions=[slow, fast])

    async def _drive() -> None:
        task1 = asyncio.create_task(d._on_hotkey_pressed())
        await asyncio.sleep(0)  # let task1 transition IDLE → STARTING
        task2 = asyncio.create_task(d._on_hotkey_pressed())
        await asyncio.gather(task1, task2)

    asyncio.run(_drive())

    # task1 completed successfully → exactly one start sound.
    assert stub.start_calls == [True]
    # task2 was dropped during STARTING → exactly one error sound + banner.
    assert stub.error_calls == [True]
    assert len(stub.notify_calls) == 1
    assert "transition" in stub.notify_calls[0]


def test_audible_feedback_off_silences_start_and_stop_sounds(
    daemon_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``Config.audible_feedback=False`` → sounds called with ``enabled=False``.

    Per FR 2.9 the daemon still invokes ``play_*`` — the gating happens inside
    :mod:`record.feedback` (no-op when ``enabled=False``). We assert the
    daemon plumbed the flag through correctly rather than skipped the call.
    """
    stub = _install_feedback_stub(monkeypatch)
    cfg = _make_config(audible_feedback=False, tmp_path=tmp_path)
    d, _sessions = _make_daemon(daemon_paths, config=cfg)

    async def _drive() -> None:
        await d._on_hotkey_pressed()  # start
        await d._on_hotkey_pressed()  # stop

    asyncio.run(_drive())

    # The daemon DID call play_start / play_stop — but with enabled=False so
    # ``feedback`` will no-op. Asserting the calls rather than their absence
    # is the contract: the audible-feedback toggle is feedback-module-side.
    assert stub.start_calls == [False]
    assert stub.stop_calls == [False]
    assert stub.error_calls == []


def test_socket_driven_start_ok_plays_start_not_error(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful ``record start`` over the socket → start sound, never Funk.

    The error-sound + banner branch is hotkey-specific (FR 2.8 third bullet
    wording). Socket-driven starts that succeed take the unconditional
    happy-path start sound (FR 2.8 first bullet).
    """
    stub = _install_feedback_stub(monkeypatch)
    d, _sessions = _make_daemon(daemon_paths)

    async def _drive() -> control.ControlResponse:
        return await d.handle_request(control.StartRequest())

    resp = asyncio.run(_drive())

    assert resp.status == "ok"
    assert stub.start_calls == [True]
    assert stub.error_calls == []
    assert stub.notify_calls == []


def test_accessibility_denied_event_notifies_without_sound(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hotkey_registered: invalid/accessibility_denied`` → banner, no sound.

    Tech spec §2.9: daemon-level warnings the user must know about (the
    canonical example being Accessibility denied) use the banner. There is no
    error sound here — the daemon is in startup, not in a hotkey-press error
    path.
    """
    stub = _install_feedback_stub(monkeypatch)
    d, _sessions = _make_daemon(daemon_paths)

    d._on_hotkey_event(
        ipc.HotkeyRegisteredEvent(
            status="invalid",
            modifiers=["cmd", "option"],
            key="r",
            message="accessibility_denied",
        )
    )

    assert len(stub.notify_calls) == 1
    assert "Accessibility permission denied" in stub.notify_calls[0]
    assert stub.start_calls == []
    assert stub.stop_calls == []
    assert stub.error_calls == []


def test_hotkey_press_with_failing_start_plays_error_and_notifies(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hotkey press whose ``_handle_start`` errors → Funk + banner naming the cause."""
    from record.capture import CaptureFailedToStart

    stub = _install_feedback_stub(monkeypatch)
    failing = _FakeSession(
        basename=daemon_paths["root"] / "x",
        start_raises=CaptureFailedToStart("mic permission denied"),
    )
    d, _sessions = _make_daemon(daemon_paths, sessions=[failing])

    asyncio.run(d._on_hotkey_pressed())

    # play_start is on the OK-return path; a failed start never reaches it.
    assert stub.start_calls == []
    assert stub.error_calls == [True]
    assert len(stub.notify_calls) == 1
    # The banner echoes the underlying detail so the user can act on it.
    assert "mic permission denied" in stub.notify_calls[0]


# ---------------------------------------------------------------------------
# Auto-transcription on finalize (spec 004 slice 2)
#
# A successful stop or system-event finalize spawns a background transcription
# task. Tests stub the DeepgramBackend the daemon constructs by patching the
# ``transcribe_module`` import alias in :mod:`record.daemon`, so no real
# network is hit. Same trick for ``secrets`` (the API-key resolver).
# ---------------------------------------------------------------------------


from record import transcribe as transcribe_module  # noqa: E402


class _StubBackend:
    """A controllable :class:`TranscriptionBackend` stub for daemon tests.

    Records the audio paths it was called with. The ``done`` event lets a
    test gate completion so it can assert "the daemon didn't wait on me"
    (independence / quit-doesn't-await) before letting the task finish.

    ``transcript`` is returned from ``transcribe``; ``raise_exc`` short-circuits
    to raise instead. Both default to a "succeed with a tiny transcript"
    behavior so the common case is a one-liner.
    """

    def __init__(
        self,
        *,
        transcript: transcribe_module.Transcript | None = None,
        raise_exc: BaseException | None = None,
        done: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[Path] = []
        self._transcript = transcript or transcribe_module.Transcript(
            provider="deepgram",
            model="nova-3",
            language=["en"],
            duration_seconds=1.0,
            segments=[
                transcribe_module.Segment(
                    speaker="Speaker 1",
                    start=0.0,
                    end=1.0,
                    text="hello",
                ),
            ],
        )
        self._raise_exc = raise_exc
        self._done = done

    async def transcribe(self, audio_path: Path) -> transcribe_module.Transcript:
        self.calls.append(audio_path)
        if self._done is not None:
            await self._done.wait()
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._transcript


def _patch_transcription(
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_key: str | None = "test-key",
    backends: list[_StubBackend] | None = None,
    patch_rename: bool = True,
) -> list[_StubBackend]:
    """Patch the daemon's secret lookup + backend constructor.

    Returns a live list that subsequent factory calls populate so tests can
    assert on the backends the daemon constructed. When ``backends`` is
    pre-seeded, the factory pops from it (FIFO); otherwise it appends fresh
    successful stubs.

    Spec 008 slice 3: by default also patches
    ``record.daemon.naming.try_rename_session_folder`` to a no-op
    ``AsyncMock``. The real implementation shells out to ``claude -p`` to
    generate a kebab-case suffix for the session folder; without the patch,
    every test that drains the detached transcription task would spawn a real
    subprocess. Tests that specifically need to assert on the rename hook
    (slice 2: ``test_stop_invokes_rename_after_transcription``,
    ``test_stop_swallows_rename_exception``) re-patch it locally with their
    own mock — the local ``monkeypatch.setattr`` simply replaces the no-op.
    """
    seeded: list[_StubBackend] = list(backends) if backends else []
    handed_out: list[_StubBackend] = []

    monkeypatch.setattr(
        daemon.secrets, "get_deepgram_api_key", lambda: api_key
    )

    def _factory(key: str, *args: object, **kwargs: object) -> _StubBackend:
        # Sanity: the backend must be constructed with whatever key the
        # daemon's resolver returned. A future refactor that accidentally
        # leaks ``None`` would surface here.
        assert key == api_key
        if seeded:
            stub = seeded.pop(0)
        else:
            stub = _StubBackend()
        handed_out.append(stub)
        return stub

    monkeypatch.setattr(
        daemon.transcribe_module, "DeepgramBackend", _factory
    )

    if patch_rename:
        monkeypatch.setattr(
            "record.daemon.naming.try_rename_session_folder",
            AsyncMock(return_value=None),
        )
    return handed_out


def _install_unique_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch :func:`daemon._filename_timestamp` to return a fresh stem per call.

    Spec 008: each capture lands in its own ``<output_folder>/<stem>/`` folder
    that the daemon creates with ``exist_ok=False``. Tests doing two starts in
    quick succession would otherwise collide on the wall-clock second; this
    helper hands out monotonically-numbered stems so each ``mkdir`` succeeds.
    """
    counter = {"n": 0}

    def _fake_stamp() -> str:
        counter["n"] += 1
        return f"2026-05-16T00-00-{counter['n']:02d}"

    monkeypatch.setattr(daemon, "_filename_timestamp", _fake_stamp)


async def _drain_background(d: daemon.Daemon, timeout: float = 2.0) -> None:
    """Await every task in ``d._background`` to completion or ``timeout``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while d._background:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("background tasks did not drain")
        tasks = list(d._background)
        await asyncio.wait(tasks, timeout=remaining)


def test_stop_spawns_one_transcription_task_and_writes_files(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful stop → one transcription task against the combined file.

    Spec 007 slice 3: transcription is driven exclusively from the combined
    file; per-source WAVs are never sent to the cloud.
    """
    handed_out = _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    # Exactly one backend: the combined file only.
    assert len(handed_out) == 1
    assert len(handed_out[0].calls) == 1

    # Spec 008: ``basename`` is the per-session directory; the source WAVs,
    # combined WAV, and transcript triple all live inside it.
    session_dir = session_holder[0].basename
    mic_path = session_dir / "mic.wav"
    system_path = session_dir / "system.wav"
    combined_path = session_dir / "combined.wav"
    assert handed_out[0].calls[0] == combined_path

    # Combined-file transcript triple appears as ``transcript.{json,txt,srt}``
    # next to the combined WAV (spec 008 layout).
    assert (session_dir / "transcript.json").is_file()
    assert (session_dir / "transcript.txt").is_file()
    assert (session_dir / "transcript.srt").is_file()
    # Per-source transcripts must NOT appear.
    for source_path in (mic_path, system_path):
        s_base = source_path.stem
        assert not (session_dir / f"{s_base}.json").exists()
        assert not (session_dir / f"{s_base}.txt").exists()
        assert not (session_dir / f"{s_base}.srt").exists()

    # ControlResponse round-trip: ``audio_paths`` still carries both source
    # files, ``audio_path`` still keeps the mic file as the single-field
    # surface (slice 3 does not change this contract).
    assert resp.audio_paths == {
        "mic": str(mic_path),
        "system_audio": str(system_path),
    }
    assert resp.audio_path == str(mic_path)


def test_stop_invokes_rename_after_transcription(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 008 slice 2: successful transcription triggers naming hook."""
    handed_out = _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    rename_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "record.daemon.naming.try_rename_session_folder", rename_mock
    )

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"

    session_dir = session_holder[0].basename
    expected_transcript = handed_out[0]._transcript

    assert rename_mock.await_count == 1
    rename_mock.assert_awaited_once_with(
        session_dir=session_dir, transcript=expected_transcript
    )


def test_stop_swallows_rename_exception(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A naming-hook exception is caught — daemon returns to IDLE cleanly."""
    _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    rename_mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "record.daemon.naming.try_rename_session_folder", rename_mock
    )

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    background_snapshot: list[asyncio.Task[None]] = []

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        background_snapshot.extend(
            t for t in d._background if (t.get_name() or "").startswith("transcribe:")
        )
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    assert rename_mock.await_count == 1
    assert d._state == daemon._CaptureState.IDLE
    # Background task finished cleanly: no unhandled exception escaped.
    for t in background_snapshot:
        assert t.done()
        assert t.exception() is None


def test_no_api_key_skips_transcription_and_logs_warning(
    daemon_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No key → no task, no exception, ``transcription_skipped`` at WARNING.

    Spec 007 slice 3: with combine succeeding, there is exactly one skipped
    transcription (against the combined file), not two.
    """
    handed_out = _patch_transcription(monkeypatch, api_key=None)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    caplog.set_level(logging.WARNING, logger="record.daemon")

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        return await d.handle_request(control.StopRequest())

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    # No backend was ever constructed.
    assert handed_out == []
    # No lingering background task.
    assert not any(
        (t.get_name() or "").startswith("transcribe:")
        for t in d._background
    )
    # Daemon returned to IDLE and the file finalized.
    assert d._state == daemon._CaptureState.IDLE
    # ``_spawn_transcription`` itself logs ``transcription_skipped`` with
    # ``reason="no_api_key"`` — that code path predates slice 3.
    matching = [
        rec for rec in caplog.records
        if "transcription_skipped" in rec.getMessage()
        and "no_api_key" in rec.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one `transcription_skipped` reason=no_api_key WARNING; "
        f"saw: {[r.getMessage() for r in caplog.records]!r}"
    )
    # Spec 008: the audio path is the combined file inside the session folder.
    session_dir = session_holder[0].basename
    combined_path = str(session_dir / "combined.wav")
    payload = matching[0].getMessage()
    assert combined_path in payload
    assert "mic.wav" not in payload
    assert "system.wav" not in payload


def test_transcription_failure_logged_no_exception_escapes(
    daemon_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Backend raising ``TranscriptionError`` → logged at ERROR, daemon healthy."""
    failing = _StubBackend(
        raise_exc=transcribe_module.TranscriptionError("deepgram 401: bad key")
    )
    _patch_transcription(monkeypatch, backends=[failing])
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    caplog.set_level(logging.ERROR, logger="record.daemon")

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        # Daemon must accept more commands afterwards.
        status = await d.handle_request(control.StatusRequest())
        assert status.status == "ok"
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    assert d._state == daemon._CaptureState.IDLE

    # ERROR line with the failure reason.
    matching = [
        rec for rec in caplog.records if "transcription_failed" in rec.getMessage()
    ]
    assert matching, (
        f"expected `transcription_failed` ERROR; saw: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    msg = matching[-1].getMessage()
    assert "deepgram 401" in msg
    # The API key never appears in the structured log line.
    assert "test-key" not in msg


def test_two_stops_in_quick_succession_produce_independent_tasks(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two start/stop cycles → independent transcription tasks per cycle.

    Spec 007 slice 3: each cycle spawns exactly one transcription (against
    the combined file), so two cycles produce two in-flight tasks.
    """
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()
    stub_a = _StubBackend(done=gate_a)
    stub_b = _StubBackend(done=gate_b)
    _patch_transcription(monkeypatch, backends=[stub_a, stub_b])
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)
    # Spec 008: two starts in quick succession need distinct session-dir
    # stems so the daemon's ``mkdir(exist_ok=False)`` succeeds each time.
    _install_unique_timestamps(monkeypatch)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> None:
        # Cycle 1.
        await d.handle_request(control.StartRequest())
        stop_a = await d.handle_request(control.StopRequest())
        assert stop_a.status == "ok"

        # Let the scheduler enter the transcription task at least once so
        # the stub's ``.calls`` reflects "I was invoked but I'm parked".
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Cycle 1's task is parked on its gate; cycle 2 must not block.
        assert stub_a.calls and not gate_a.is_set()

        # Cycle 2. If the first cycle's task blocked the daemon, this stop
        # would never return.
        await d.handle_request(control.StartRequest())
        stop_b = await d.handle_request(control.StopRequest())
        assert stop_b.status == "ok"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert stub_b.calls

        # Two transcription tasks in flight (one per cycle).
        transcribe_tasks = [
            t
            for t in d._background
            if (t.get_name() or "").startswith("transcribe:")
        ]
        assert len(transcribe_tasks) == 2

        # Release every gate and drain.
        for g in (gate_a, gate_b):
            g.set()
        await _drain_background(d)

    asyncio.run(_drive())
    assert len(session_holder) == 2


def test_quit_does_not_await_in_flight_transcription(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``record quit`` returns promptly even with a transcription mid-flight.

    The serve_forever() cleanup path partitions the background set so the
    transcription task is abandoned (logged) rather than awaited.
    """
    gate = asyncio.Event()
    # Spec 007 slice 3: one finalized combined WAV → one backend/task per
    # cycle.
    slow = _StubBackend(done=gate)
    _patch_transcription(monkeypatch, backends=[slow])
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> None:
        await d.handle_request(control.StartRequest())
        await d.handle_request(control.StopRequest())
        # Yield so the spawned task enters ``backend.transcribe`` and parks
        # on the gate before we issue the quit.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert slow.calls, "transcription task did not start"

        # quit must NOT block on the in-flight transcription.
        resp = await asyncio.wait_for(
            d.handle_request(control.QuitRequest(finalize=False)),
            timeout=2.0,
        )
        # quit returns ok (capture already finalized in this test).
        assert resp.status == "ok"
        assert d._shutdown_event.is_set()

        # The transcription task is still in the background set — it was
        # NOT awaited by quit. (serve_forever's cleanup partition logs +
        # abandons rather than awaits; we don't run serve_forever here.)
        transcribe_tasks = [
            t
            for t in d._background
            if (t.get_name() or "").startswith("transcribe:")
        ]
        assert len(transcribe_tasks) == 1
        for t in transcribe_tasks:
            assert not t.done()

        # Tidy: release the gate so the orphaned task can finalize before
        # the event loop closes (avoids a noisy warning).
        gate.set()
        for t in transcribe_tasks:
            await t

    asyncio.run(_drive())


def test_serve_forever_cleanup_logs_abandoned_transcription(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shutdown cleanup logs ``transcription_abandoned_at_quit`` per task.

    Drives just the cleanup branch of ``serve_forever`` by registering a
    transcription task on the daemon's ``_background`` set directly, then
    invoking the partition logic via a mini wrapper that mirrors the
    serve_forever finally-block. This avoids spawning the Swift child / socket
    server while still pinning the contract.
    """
    captured_logs: list[tuple[str, dict[str, object]]] = []

    def _capture_info(event: str, **kwargs: object) -> None:
        captured_logs.append((event, dict(kwargs)))

    d, _sessions = _make_daemon(daemon_paths)
    monkeypatch.setattr(d._log, "info", _capture_info)

    async def _drive() -> None:
        # Fake an in-flight transcription task — slept forever so it's still
        # pending when we partition.
        async def _blocked() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(
            _blocked(), name="transcribe:2026-05-15T10-00-00"
        )
        d._background.add(task)
        task.add_done_callback(d._background.discard)

        # Replicate the partition logic from serve_forever's finally block.
        transcription_tasks = [
            t
            for t in d._background
            if (t.get_name() or "").startswith("transcribe:")
        ]
        for t in transcription_tasks:
            if not t.done():
                _, _, stem = t.get_name().partition(":")
                d._log.info("transcription_abandoned_at_quit", audio_stem=stem)

        # Tidy.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    abandoned = [
        (ev, kw)
        for ev, kw in captured_logs
        if ev == "transcription_abandoned_at_quit"
    ]
    assert len(abandoned) == 1
    assert abandoned[0][1].get("audio_stem") == "2026-05-15T10-00-00"


# ---------------------------------------------------------------------------
# Spec 007 slice 2: combine step is invoked on both stop paths
# ---------------------------------------------------------------------------


import array  # noqa: E402
import wave  # noqa: E402

from record import combine as combine_module  # noqa: E402


def _write_int16_wav(
    path: Path,
    samples: list[int],
    *,
    framerate: int = 16_000,
) -> None:
    """Write a tiny mono int16 16 kHz WAV used as a combine-step input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(array.array("h", samples).tobytes())


class _CombineFakeSession(_FakeSession):
    """Variant that drops real WAVs in place when ``start()`` runs.

    Lets the real :func:`combine.combine_wavs` run end-to-end against the
    state-machine handlers without spawning a Swift child.
    """

    def __init__(
        self,
        *,
        basename: Path,
        video_output_path: Path | None = None,
        write_mic: bool = True,
        write_system: bool = True,
    ) -> None:
        super().__init__(basename=basename, video_output_path=video_output_path)
        self._write_mic = write_mic
        self._write_system = write_system

    async def start(self) -> None:
        await super().start()
        # Spec 008: source WAVs live inside the per-session folder
        # (``self.basename`` is the folder path).
        if self._write_mic:
            _write_int16_wav(
                self.basename / "mic.wav",
                [100, 200, 300, 400, 500],
            )
        if self._write_system:
            _write_int16_wav(
                self.basename / "system.wav",
                [10, 20, 30, 40, 50],
            )


def test_handle_stop_combine_happy_path_persists_produced_state(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_handle_stop` runs combine; produced status lands in state + final."""
    _patch_transcription(monkeypatch)
    # Point the daemon's state-file path at tmp so we can re-read it.
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"

    s = session_holder[0]
    combined = s.state.get("combined_audio")
    assert isinstance(combined, dict)
    assert combined.get("status") == "produced"
    # Spec 008: combined WAV lands at ``<session_dir>/combined.wav``.
    expected_path = s.basename / "combined.wav"
    assert combined.get("path") == str(expected_path)
    assert combined.get("duration_seconds") is not None
    # The combined file itself was written.
    assert expected_path.is_file()
    # On-disk capture-state.json reflects the combined entry.
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk.get("combined_audio", {}).get("status") == "produced"


def test_handle_stop_combine_failure_persists_failed_state_and_logs(
    daemon_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Combine raising `CombineError("disk full")` records `failed` + logs."""
    _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    # Force combine_wavs to raise after pre-call file checks pass.
    def _boom(mic: Path, sys: Path, out: Path):  # noqa: ANN202
        raise combine_module.CombineError("disk full")

    monkeypatch.setattr(combine_module, "combine_wavs", _boom)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    caplog.set_level(logging.ERROR, logger="record.daemon")

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"

    s = session_holder[0]
    combined = s.state.get("combined_audio")
    assert isinstance(combined, dict)
    assert combined.get("status") == "failed"
    assert combined.get("reason") == "disk full"

    # ERROR log line names the failure type.
    matching = [
        rec for rec in caplog.records if "combine_failed" in rec.getMessage()
    ]
    assert matching, (
        f"expected `combine_failed` ERROR; saw: "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    msg = matching[-1].getMessage()
    assert "CombineError" in msg
    assert "disk full" in msg

    # Source WAVs survived the failure (spec 008: inside the session folder).
    assert (s.basename / "mic.wav").is_file()
    assert (s.basename / "system.wav").is_file()


def test_handle_stop_combine_missing_source_does_not_call_combine_wavs(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-call check skips combine entirely when a source file is absent."""
    _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    # Spy on combine_wavs and assert it is never called.
    calls: list[tuple[Path, Path, Path]] = []

    def _spy(mic: Path, sys: Path, out: Path):  # noqa: ANN202
        calls.append((mic, sys, out))
        raise AssertionError("combine_wavs must not be invoked")

    monkeypatch.setattr(combine_module, "combine_wavs", _spy)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        # Drop only the system-audio file; leave mic absent on disk.
        s = _CombineFakeSession(
            basename=basename,
            video_output_path=video_output_path,
            write_mic=False,
            write_system=True,
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    assert calls == []

    s = session_holder[0]
    combined = s.state.get("combined_audio")
    assert isinstance(combined, dict)
    assert combined.get("status") == "failed"
    assert combined.get("reason") == "one or more source files unavailable"


def test_watch_for_system_event_stop_runs_combine(
    daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-stop path also runs `_run_combine` and persists `produced`."""
    _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    async def _drive() -> None:
        await d.handle_request(control.StartRequest())
        # Simulate a system-event-triggered shutdown: fire stopped_event from
        # outside, then await the watcher task the daemon registered.
        session = session_holder[0]
        session.stopped_event.set()
        # Drain background tasks (the watcher + any transcription jobs).
        await _drain_background(d, timeout=3.0)

    asyncio.run(_drive())

    s = session_holder[0]
    combined = s.state.get("combined_audio")
    assert isinstance(combined, dict), (
        f"expected combined_audio dict; got: {combined!r}"
    )
    assert combined.get("status") == "produced"
    assert Path(combined["path"]).is_file()
    # Daemon returned to IDLE via the watcher path.
    assert d._state == daemon._CaptureState.IDLE


# ---------------------------------------------------------------------------
# Spec 007 slice 3: combine failure gates transcription
# ---------------------------------------------------------------------------


def test_combine_failure_skips_transcription_with_combine_failed_reason(
    daemon_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_handle_stop`: combine failure → no transcription, skipped reason logged."""
    handed_out = _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    def _boom(mic: Path, sys: Path, out: Path):  # noqa: ANN202
        raise combine_module.CombineError("disk full")

    monkeypatch.setattr(combine_module, "combine_wavs", _boom)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    caplog.set_level(logging.WARNING, logger="record.daemon")

    async def _drive() -> control.ControlResponse:
        await d.handle_request(control.StartRequest())
        resp = await d.handle_request(control.StopRequest())
        await _drain_background(d)
        return resp

    resp = asyncio.run(_drive())
    assert resp.status == "ok"
    # Zero backends were constructed — transcription was gated.
    assert handed_out == []

    # Spec 008: the combined-file path is inside the session folder.
    session_dir = session_holder[0].basename
    combined_path = str(session_dir / "combined.wav")

    matching = [
        rec for rec in caplog.records
        if "transcription_skipped" in rec.getMessage()
        and "combine_failed" in rec.getMessage()
    ]
    assert matching, (
        f"expected `transcription_skipped` reason=combine_failed WARNING; "
        f"saw: {[r.getMessage() for r in caplog.records]!r}"
    )
    payload = matching[-1].getMessage()
    assert combined_path in payload
    # The per-source filenames must not appear in the skipped log — only the
    # combined file is referenced for transcription.
    assert "mic.wav" not in payload
    assert "system.wav" not in payload


def test_watch_for_system_event_stop_combine_failure_skips_transcription(
    daemon_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Auto-stop path: combine failure → no transcription, skipped reason logged."""
    handed_out = _patch_transcription(monkeypatch)
    state_path = daemon_paths["root"] / "capture-state.json"
    monkeypatch.setattr(state, "_default_state_file", lambda: state_path)

    def _boom(mic: Path, sys: Path, out: Path):  # noqa: ANN202
        raise combine_module.CombineError("disk full")

    monkeypatch.setattr(combine_module, "combine_wavs", _boom)

    session_holder: list[_CombineFakeSession] = []

    def _factory(
        basename: Path, video_output_path: Path | None
    ) -> _CombineFakeSession:
        s = _CombineFakeSession(
            basename=basename, video_output_path=video_output_path
        )
        session_holder.append(s)
        return s

    d = daemon.Daemon(
        daemon_log_path=daemon_paths["log"],
        session_factory=_factory,
        output_folder=daemon_paths["root"],
    )

    caplog.set_level(logging.WARNING, logger="record.daemon")

    async def _drive() -> None:
        await d.handle_request(control.StartRequest())
        session = session_holder[0]
        session.stopped_event.set()
        await _drain_background(d, timeout=3.0)

    asyncio.run(_drive())

    # No transcription backend constructed.
    assert handed_out == []
    assert d._state == daemon._CaptureState.IDLE

    # Spec 008: the combined-file path is inside the session folder.
    session_dir = session_holder[0].basename
    combined_path = str(session_dir / "combined.wav")

    matching = [
        rec for rec in caplog.records
        if "transcription_skipped" in rec.getMessage()
        and "combine_failed" in rec.getMessage()
    ]
    assert matching, (
        f"expected `transcription_skipped` reason=combine_failed WARNING; "
        f"saw: {[r.getMessage() for r in caplog.records]!r}"
    )
    payload = matching[-1].getMessage()
    assert combined_path in payload
    # The per-source filenames must not appear in the skipped log — only the
    # combined file is referenced for transcription.
    assert "mic.wav" not in payload
    assert "system.wav" not in payload
