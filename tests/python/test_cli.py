"""CLI tests for the daemon-mediated commands (spec 003 slice 2).

The legacy ``record start`` / ``record stop`` → supervisor-PID-file flow is
gone; the CLI is now a thin socket client of the running daemon. These tests
either spin up an in-process control-socket server backed by a stub handler
(for the happy paths and the daemon-side error responses), or just monkeypatch
``control.send_request_sync`` (for the FR 2.7 "daemon is not running" branch).

The slice-1 ``record daemon start/stop/restart`` tests at the bottom are
unchanged — those still exercise the Popen / SIGTERM / PID-file plumbing.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
from typer.testing import CliRunner

from record import cli, control, launchagent, paths, state
from record.config import Config


runner = CliRunner()


# ---------------------------------------------------------------------------
# Stub control-socket server
# ---------------------------------------------------------------------------


_DEAD_PID = 99999999


class _StubServer:
    """Run :func:`control.serve` against a configurable handler.

    Wraps the asyncio server in its own thread + event loop so the synchronous
    Typer CliRunner can drive it without nesting event loops. Each test
    constructs one of these, passes the socket path into a CLI invocation via
    monkeypatching :func:`paths.daemon_socket`, and tears it down at the end.
    """

    def __init__(
        self,
        socket_path: Path,
        handler: Callable[[control.ControlRequest], control.ControlResponse],
    ) -> None:
        self._socket_path = socket_path
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.calls: list[control.ControlRequest] = []

    def start(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)

            async def _bootstrap() -> None:
                async def _handler(
                    req: control.ControlRequest,
                ) -> control.ControlResponse:
                    self.calls.append(req)
                    return self._handler(req)

                self._server = await control.serve(
                    _handler, socket_path=self._socket_path
                )
                self._ready.set()
                # Idle until the server is closed externally via stop().
                async with self._server:
                    await self._server.serve_forever()

            try:
                loop.run_until_complete(_bootstrap())
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - defensive
                pass
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        # Wait until serve() has bound the socket.
        assert self._ready.wait(timeout=5.0), "stub control server did not start"

    def stop(self) -> None:
        loop = self._loop
        server = self._server
        if loop is None or server is None:
            return

        def _shutdown() -> None:
            server.close()

        loop.call_soon_threadsafe(_shutdown)
        if self._thread is not None:
            self._thread.join(timeout=5.0)


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Redirect every path the CLI consults to ``tmp_path``.

    Covers both the capture-state file (re-read by ``record stop`` to render
    the summary) and the daemon socket path (consulted by every socket-routed
    command).

    The socket path lives under :func:`tempfile.mkdtemp` rather than
    ``tmp_path`` because macOS imposes a 104-character limit on AF_UNIX paths
    and pytest's ``tmp_path`` under ``/private/var/folders/.../pytest-of-.../``
    routinely exceeds it.
    """
    import shutil
    import tempfile

    fake_state = tmp_path / "capture-state.json"
    short_dir = Path(tempfile.mkdtemp(prefix="rcd-"))
    fake_socket = short_dir / "d.sock"

    monkeypatch.setattr(paths, "state_file", lambda: fake_state)
    monkeypatch.setattr(state, "_default_state_file", lambda: fake_state)
    monkeypatch.setattr(paths, "daemon_socket", lambda: fake_socket)
    # ``record stop`` no longer touches the legacy capture.pid, but a couple
    # of helpers still consult it — point the supervisor-PID path at tmp_path
    # too so a stray write can't escape the sandbox.
    fake_pid = tmp_path / "capture.pid"
    monkeypatch.setattr(paths, "pid_file", lambda: fake_pid)
    monkeypatch.setattr(state, "_default_pid_file", lambda: fake_pid)
    yield {
        "state": fake_state,
        "socket": fake_socket,
        "pid": fake_pid,
        "root": tmp_path,
        "socket_dir": short_dir,
    }
    shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture
def stub_server(
    fake_paths: dict[str, Path],
) -> Any:
    """Factory: caller passes a handler callable, gets a started stub server back."""
    servers: list[_StubServer] = []

    def _factory(
        handler: Callable[[control.ControlRequest], control.ControlResponse],
    ) -> _StubServer:
        srv = _StubServer(fake_paths["socket"], handler)
        srv.start()
        servers.append(srv)
        return srv

    yield _factory

    for srv in servers:
        srv.stop()


# ---------------------------------------------------------------------------
# `record start` — socket-client semantics
# ---------------------------------------------------------------------------


def test_start_prints_daemon_not_running_when_socket_missing(
    fake_paths: dict[str, Path],
) -> None:
    """No socket file → DaemonUnreachable → FR 2.7 message + exit 1."""
    assert not fake_paths["socket"].exists()
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 1
    assert "daemon is not running" in result.stderr


def test_start_happy_path_prints_paths_from_response(
    stub_server: Any,
) -> None:
    """Daemon replies ok with audio/video paths → printed to stdout, exit 0."""

    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        assert isinstance(req, control.StartRequest)
        return control.ControlResponse(
            status="ok",
            capture_id="2026-05-13T09-21-48",
            audio_path="/abs/2026-05-13T09-21-48.wav",
            video_path="/abs/2026-05-13T09-21-48.mp4",
        )

    srv = stub_server(_handler)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 0, result.stderr
    assert "capture started" in result.stdout
    assert "audio=/abs/2026-05-13T09-21-48.wav" in result.stdout
    assert "video=/abs/2026-05-13T09-21-48.mp4" in result.stdout
    assert len(srv.calls) == 1
    assert isinstance(srv.calls[0], control.StartRequest)


def test_start_already_running_exits_1(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="already_running",
            detail="capture already in progress",
            capture_id="abc",
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 1
    assert "capture already in progress" in result.stderr


def test_start_busy_exits_1(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="busy", detail="capture is being finalized"
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 1
    assert "daemon busy" in result.stderr


def test_start_error_exits_3(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="error", detail="capture binary missing or not executable"
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 3
    assert "capture failed to start" in result.stderr


# ---------------------------------------------------------------------------
# `record stop` — socket-client semantics
# ---------------------------------------------------------------------------


def test_stop_prints_daemon_not_running_when_socket_missing(
    fake_paths: dict[str, Path],
) -> None:
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "daemon is not running" in result.stderr


def test_stop_not_running_exits_1(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="not_running", detail="no capture running"
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "no capture running" in result.stderr


def test_stop_busy_exits_1(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="busy", detail="capture is still starting"
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "daemon busy" in result.stderr


def test_stop_error_exits_4(stub_server: Any) -> None:
    """A non-ok / non-busy / non-not_running response → exit 4 (legacy timeout code)."""

    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="error", detail="binary crashed"
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 4
    assert "capture failed to stop" in result.stderr


def test_stop_happy_path_renders_summary_from_state_file(
    stub_server: Any, fake_paths: dict[str, Path]
) -> None:
    """Daemon responds ok → CLI re-reads capture-state.json → prints summary."""
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/x.wav", "duration_seconds": 42.5, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}, '
            '"video": {"status": "never_attached"}}, '
            '"warnings": [], "final": true}'
        ),
        encoding="utf-8",
    )

    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        assert isinstance(req, control.StopRequest)
        return control.ControlResponse(
            status="ok",
            audio_path="/abs/x.wav",
            video_path=None,
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "capture stopped" in result.stdout
    assert "/abs/x.wav" in result.stdout
    assert "microphone + system audio" in result.stdout


def test_stop_exits_2_on_permission_denied_in_state_file(
    stub_server: Any, fake_paths: dict[str, Path]
) -> None:
    """capture-state.json with permission_denied=microphone → exit 2.

    The daemon still answers ok to the stop request (the supervisor finalized
    the file with a `permission_denied` payload); the CLI translates that into
    the System-Settings-aware message + exit 2, mirroring the legacy behavior.
    """
    fake_paths["state"].write_text(
        '{"permission_denied": "microphone", "final": true}', encoding="utf-8"
    )

    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(status="ok")

    stub_server(_handler)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 2
    assert "microphone permission denied" in result.stderr
    assert "System Settings" in result.stderr


# ---------------------------------------------------------------------------
# `record status` — slice 2
# ---------------------------------------------------------------------------


def test_status_prints_not_running_when_daemon_unreachable(
    fake_paths: dict[str, Path],
) -> None:
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 1
    assert "daemon: not running" in result.stdout


def test_status_idle_daemon(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="ok",
            daemon=control.DaemonInfo(
                running=True,
                pid=4242,
                started_at="2026-05-13T09:14:02Z",
                autostart_registered=False,
            ),
            hotkey=control.HotkeyInfo(state="unregistered"),
            capture=control.CaptureState(running=False),
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.stderr
    assert "daemon: running" in result.stdout
    assert "PID 4242" in result.stdout
    assert "hotkey: unregistered" in result.stdout
    assert "autostart: not registered" in result.stdout
    assert "capture: idle" in result.stdout


def test_status_running_capture(stub_server: Any) -> None:
    def _handler(req: control.ControlRequest) -> control.ControlResponse:
        return control.ControlResponse(
            status="ok",
            daemon=control.DaemonInfo(
                running=True,
                pid=4242,
                started_at="2026-05-13T09:14:02Z",
            ),
            hotkey=control.HotkeyInfo(state="unregistered"),
            capture=control.CaptureState(
                running=True,
                started_at="2026-05-13T09:20:00Z",
                audio_path="/abs/x.wav",
                video_path="/abs/x.mp4",
            ),
        )

    stub_server(_handler)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "capture: running" in result.stdout
    assert "/abs/x.wav" in result.stdout
    assert "/abs/x.mp4" in result.stdout


# ---------------------------------------------------------------------------
# `_summarize_video` / `_summarize_sources` / `_extra_warnings` — unchanged
# unit coverage for the summary-rendering helpers ported across.
# ---------------------------------------------------------------------------


def test_summarize_video_never_attached() -> None:
    state_dict: dict[str, Any] = {
        "sources": {"video": {"status": "never_attached"}},
    }
    assert cli._summarize_video(state_dict) == "video: never_attached"


def test_summarize_video_attached_renders_path_duration_dimensions() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/2026-05-11T12-00-00.mp4",
        "video_file_duration_seconds": 12.3,
        "sources": {
            "video": {
                "status": "attached",
                "width_px": 2560,
                "height_px": 1440,
            },
        },
    }
    assert cli._summarize_video(state_dict) == (
        "video: /abs/2026-05-11T12-00-00.mp4 (12.3 s, 2560×1440)"
    )


def test_summarize_video_attached_minute_plus_duration_uses_mss_format() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/long.mp4",
        "video_file_duration_seconds": 125.0,
        "sources": {
            "video": {
                "status": "attached",
                "width_px": 1920,
                "height_px": 1080,
            },
        },
    }
    assert cli._summarize_video(state_dict) == (
        "video: /abs/long.mp4 (2m 05s, 1920×1080)"
    )


def test_summarize_video_attached_falls_back_to_audio_duration() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/x.mp4",
        "duration_seconds": 7.4,
        "sources": {
            "video": {
                "status": "attached",
                "width_px": 1280,
                "height_px": 720,
            },
        },
    }
    assert cli._summarize_video(state_dict) == (
        "video: /abs/x.mp4 (7.4 s, 1280×720)"
    )


def test_summarize_video_attached_missing_duration_renders_unknown() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/x.mp4",
        "sources": {
            "video": {
                "status": "attached",
                "width_px": 800,
                "height_px": 600,
            },
        },
    }
    assert cli._summarize_video(state_dict) == (
        "video: /abs/x.mp4 (unknown, 800×600)"
    )


def test_summarize_video_lost_defensive_fallback_no_warning() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/x.mp4",
        "sources": {"video": {"status": "lost"}},
        "warnings": [],
    }
    assert cli._summarize_video(state_dict) == "video: /abs/x.mp4 (lost)"


def test_summarize_video_lost_offset_zero_renders_unavailable() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": None,
        "sources": {"video": {"status": "lost"}},
        "warnings": [
            {
                "source": "video",
                "at_offset_seconds": 0,
                "message": "permission_denied",
            }
        ],
    }
    assert cli._summarize_video(state_dict) == (
        "video: unavailable — permission_denied"
    )


def test_summarize_video_lost_mid_capture_renders_path_and_offset() -> None:
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/path.mp4",
        "sources": {"video": {"status": "lost"}},
        "warnings": [
            {
                "source": "video",
                "at_offset_seconds": 134.0,
                "message": "sc_stream_error",
            }
        ],
    }
    assert cli._summarize_video(state_dict) == (
        "video: /abs/path.mp4 — stopped at 02:14, reason sc_stream_error"
    )


def test_summary_video_warning_not_duplicated_as_generic_warning() -> None:
    state_dict: dict[str, Any] = {
        "sources": {"video": {"status": "lost"}},
        "warnings": [
            {
                "source": "video",
                "at_offset_seconds": 0,
                "message": "permission_denied",
            }
        ],
    }
    assert cli._extra_warnings(state_dict) == []


# ---------------------------------------------------------------------------
# `record stop` full-flow renderer coverage (against stub daemon + seeded state)
# ---------------------------------------------------------------------------


def test_stop_summary_video_attached_renders_path_and_dimensions(
    stub_server: Any, fake_paths: dict[str, Path]
) -> None:
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/2026-05-11T12-00-00.wav", '
            '"video_output_path": "/abs/2026-05-11T12-00-00.mp4", '
            '"duration_seconds": 12.3, "video_file_duration_seconds": 12.3, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}, '
            '"video": {"status": "attached", "width_px": 2560, "height_px": 1440}}, '
            '"warnings": [], "final": true}'
        ),
        encoding="utf-8",
    )
    stub_server(lambda req: control.ControlResponse(status="ok"))
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "capture stopped" in result.stdout
    assert (
        "video: /abs/2026-05-11T12-00-00.mp4 (12.3 s, 2560×1440)"
        in result.stdout
    )


def test_stop_summary_video_lost_offset_zero_renders_unavailable(
    stub_server: Any, fake_paths: dict[str, Path]
) -> None:
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/2026-05-11T12-00-00.wav", '
            '"video_output_path": null, "duration_seconds": 9.5, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}, '
            '"video": {"status": "lost"}}, '
            '"warnings": [{"source": "video", "at_offset_seconds": 0, '
            '"message": "permission_denied"}], "final": true}'
        ),
        encoding="utf-8",
    )
    stub_server(lambda req: control.ControlResponse(status="ok"))
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "video: unavailable — permission_denied" in result.stdout
    assert ".mp4" not in result.stdout


def test_stop_summary_video_lost_mid_capture_renders_path_and_offset(
    stub_server: Any, fake_paths: dict[str, Path]
) -> None:
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/2026-05-11T12-00-00.wav", '
            '"video_output_path": "/abs/2026-05-11T12-00-00.mp4", '
            '"duration_seconds": 200.0, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}, '
            '"video": {"status": "lost"}}, '
            '"warnings": [{"source": "video", "at_offset_seconds": 134.0, '
            '"message": "sc_stream_error"}], "final": true}'
        ),
        encoding="utf-8",
    )
    stub_server(lambda req: control.ControlResponse(status="ok"))
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert (
        "video: /abs/2026-05-11T12-00-00.mp4 — stopped at 02:14, reason sc_stream_error"
        in result.stdout
    )


def test_stop_summary_video_never_attached_renders_status(
    stub_server: Any, fake_paths: dict[str, Path]
) -> None:
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/2026-05-11T12-00-00.wav", '
            '"video_output_path": "/abs/2026-05-11T12-00-00.mp4", '
            '"duration_seconds": 3.0, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}, '
            '"video": {"status": "never_attached"}}, '
            '"warnings": [], "final": true}'
        ),
        encoding="utf-8",
    )
    stub_server(lambda req: control.ControlResponse(status="ok"))
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "video: never_attached" in result.stdout


# ---------------------------------------------------------------------------
# `record daemon start/stop/restart` — spec 003 slice 1 (unchanged)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_daemon_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Redirect every daemon-side path the CLI consults."""
    daemon_pid = tmp_path / "daemon.pid"
    daemon_log = tmp_path / "logs" / "daemon.log"
    daemon_log_dir = daemon_log.parent

    monkeypatch.setattr(paths, "daemon_pid_file", lambda: daemon_pid)
    monkeypatch.setattr(paths, "daemon_log_file", lambda: daemon_log)
    monkeypatch.setattr(paths, "daemon_log_dir", lambda: daemon_log_dir)
    return {
        "pid": daemon_pid,
        "log": daemon_log,
        "log_dir": daemon_log_dir,
        "root": tmp_path,
    }


class _FakePopen:
    """Minimal ``subprocess.Popen`` stub for daemon-spawn tests."""

    instances: list["_FakePopen"] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self._returncode: int | None = None
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self._returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    _FakePopen.instances.clear()
    monkeypatch.setattr(cli.subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_daemon_start_spawns_daemon_module(
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "start"])
    assert result.exit_code == 0, result.stderr
    assert "daemon started" in result.stdout
    assert "PID 4242" in result.stdout
    assert len(_FakePopen.instances) == 1
    argv = _FakePopen.instances[0].argv
    assert argv[1:] == ["-m", "record.daemon"]


def test_daemon_start_when_already_running_prints_message_and_skips_spawn(
    fake_daemon_paths: dict[str, Path], fake_popen: type[_FakePopen]
) -> None:
    fake_daemon_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["daemon", "start"])
    assert result.exit_code == 0
    assert "daemon already running" in result.stdout
    assert str(os.getpid()) in result.stdout
    assert _FakePopen.instances == []


def test_daemon_start_clears_stale_pid_file_before_spawn(
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not state.is_alive(_DEAD_PID)
    fake_daemon_paths["pid"].write_text(f"{_DEAD_PID}\n", encoding="utf-8")

    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "start"])
    assert result.exit_code == 0, result.stderr
    assert "daemon started" in result.stdout
    assert len(_FakePopen.instances) == 1
    assert int(fake_daemon_paths["pid"].read_text().strip()) == 4242


def test_daemon_stop_exits_nonzero_when_no_pid_file(
    fake_daemon_paths: dict[str, Path],
) -> None:
    result = runner.invoke(cli.app, ["daemon", "stop"])
    assert result.exit_code != 0
    assert "daemon is not running" in result.stderr


def test_daemon_stop_exits_nonzero_on_stale_pid_file(
    fake_daemon_paths: dict[str, Path],
) -> None:
    assert not state.is_alive(_DEAD_PID)
    fake_daemon_paths["pid"].write_text(f"{_DEAD_PID}\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["daemon", "stop"])
    assert result.exit_code != 0
    assert "stale" in result.stderr.lower()
    assert not fake_daemon_paths["pid"].exists()


def test_daemon_stop_happy_path(
    fake_daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_daemon_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:  # noqa: ARG001
        calls["is_alive"] += 1
        return calls["is_alive"] == 1

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["daemon", "stop"])
    assert result.exit_code == 0, result.stderr
    assert "daemon stopped" in result.stdout


def test_daemon_stop_timeout_exits_4(
    fake_daemon_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_daemon_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_STOP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cli, "_STOP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["daemon", "stop"])
    assert result.exit_code == 4
    assert "did not exit" in result.stderr


def test_daemon_restart_chains_stop_and_start(
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_daemon_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:  # noqa: ARG001
        calls["is_alive"] += 1
        return calls["is_alive"] == 1

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "restart"])
    assert result.exit_code == 0, result.stderr
    assert "daemon stopped" in result.stdout
    assert "daemon started" in result.stdout
    assert len(_FakePopen.instances) == 1


def test_daemon_restart_when_not_running_just_starts(
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "restart"])
    assert result.exit_code == 0, result.stderr
    assert "daemon started" in result.stdout
    assert "daemon is not running" in result.stderr


# ---------------------------------------------------------------------------
# `record install` / `record uninstall` — spec 003 slice 7
# ---------------------------------------------------------------------------


@pytest.fixture
def install_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Sandbox launchagent plist + daemon PID file + config loader."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    fake_log_folder = tmp_path / "record" / "logs"
    fake_output = tmp_path / "record"
    fake_pid = tmp_path / "daemon.pid"

    cfg = Config(
        hotkey="option+command+r",
        output_folder=fake_output,
        log_folder=fake_log_folder,
        audible_feedback=True,
    )

    monkeypatch.setattr(cli.config_module, "load_config", lambda: cfg)
    monkeypatch.setattr(paths, "daemon_pid_file", lambda: fake_pid)

    yield {
        "root": tmp_path,
        "log_folder": fake_log_folder,
        "output_folder": fake_output,
        "pid": fake_pid,
    }
    launchagent.set_launchctl_runner(None)


def _make_runner_stub(
    responses: list[tuple[int, str]] | None = None,
) -> Callable[[list[str]], Any]:
    import subprocess as _sp

    state_box = {"responses": list(responses or []), "calls": []}

    def _runner(argv: list[str]) -> Any:
        state_box["calls"].append(list(argv))
        if state_box["responses"]:
            rc, err = state_box["responses"].pop(0)
        else:
            rc, err = 0, ""
        return _sp.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

    _runner.calls = state_box["calls"]  # type: ignore[attr-defined]
    return _runner


def test_install_happy_path_prints_registered_and_pid(
    install_sandbox: dict[str, Path],
) -> None:
    # Pre-seed the daemon PID file so the install path reports a real PID.
    install_sandbox["pid"].parent.mkdir(parents=True, exist_ok=True)
    install_sandbox["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    launchagent.set_launchctl_runner(_make_runner_stub())

    result = runner.invoke(cli.app, ["install"])
    assert result.exit_code == 0, result.stderr
    assert "registered to start on login" in result.stdout
    assert f"PID {os.getpid()}" in result.stdout


def test_install_when_already_loaded_prints_re_registered(
    install_sandbox: dict[str, Path],
) -> None:
    install_sandbox["pid"].parent.mkdir(parents=True, exist_ok=True)
    install_sandbox["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    runner_stub = _make_runner_stub(
        responses=[
            (1, "Bootstrap failed: 5: Input/output error"),
            (0, ""),  # bootout
            (0, ""),  # retry bootstrap
        ]
    )
    launchagent.set_launchctl_runner(runner_stub)

    result = runner.invoke(cli.app, ["install"])
    assert result.exit_code == 0, result.stderr
    assert "re-registered to start on login" in result.stdout


def test_install_failure_surfaces_launchctl_stderr(
    install_sandbox: dict[str, Path],
) -> None:
    runner_stub = _make_runner_stub(
        responses=[
            (1, "Bootstrap failed: 125: Operation not permitted"),
            (0, ""),  # bootout
            (1, "Bootstrap failed: 125: Operation not permitted"),
        ]
    )
    launchagent.set_launchctl_runner(runner_stub)

    result = runner.invoke(cli.app, ["install"])
    assert result.exit_code != 0
    assert "Operation not permitted" in result.stderr
    assert "install failed" in result.stderr


def test_uninstall_happy_path(
    install_sandbox: dict[str, Path],
) -> None:
    # Pre-create the plist so uninstall has something to remove.
    cfg = Config(
        hotkey="option+command+r",
        output_folder=install_sandbox["output_folder"],
        log_folder=install_sandbox["log_folder"],
        audible_feedback=True,
    )
    launchagent.write_plist(cfg)
    launchagent.set_launchctl_runner(_make_runner_stub())

    result = runner.invoke(cli.app, ["uninstall"])
    assert result.exit_code == 0, result.stderr
    assert "removed from login items" in result.stdout
    assert not launchagent.plist_path().exists()


def test_uninstall_when_nothing_registered(
    install_sandbox: dict[str, Path],
) -> None:
    launchagent.set_launchctl_runner(_make_runner_stub())
    assert not launchagent.plist_path().exists()

    result = runner.invoke(cli.app, ["uninstall"])
    assert result.exit_code == 0, result.stderr
    assert "already not registered" in result.stdout


def test_status_when_daemon_unreachable_probes_autostart(
    fake_paths: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When daemon is down but autostart IS registered, both lines render."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = Config(
        hotkey="option+command+r",
        output_folder=tmp_path / "record",
        log_folder=tmp_path / "record" / "logs",
        audible_feedback=True,
    )
    launchagent.write_plist(cfg)
    # Stub launchctl print to return success → registered.
    launchagent.set_launchctl_runner(_make_runner_stub())

    try:
        result = runner.invoke(cli.app, ["status"])
        assert result.exit_code == 1
        assert "daemon: not running" in result.stdout
        assert "autostart: registered" in result.stdout
    finally:
        launchagent.set_launchctl_runner(None)
