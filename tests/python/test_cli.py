"""CLI tests using Typer's ``CliRunner``.

The tests never spawn a real supervisor — every code path that depends on the
supervisor subprocess is exercised by pre-seeding the PID and state files in
a ``tmp_path``-rooted fake "app support" directory. ``record.paths.pid_file``
and ``record.paths.state_file`` are monkeypatched so the CLI reads our fakes
instead of the user's real ``~/Library/Application Support/record/``.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from record import cli, paths, state


runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_DEAD_PID = 99999999


@pytest.fixture
def fake_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect ``record.paths.{pid_file,state_file}`` to ``tmp_path``.

    Also redirects the module-level references inside ``record.state`` (it
    imports them as ``_default_pid_file`` / ``_default_state_file`` at module
    import time), so calls to ``state.read_pid_file()`` with no ``path=``
    argument see the fakes too.
    """
    fake_pid = tmp_path / "capture.pid"
    fake_state = tmp_path / "capture-state.json"

    monkeypatch.setattr(paths, "pid_file", lambda: fake_pid)
    monkeypatch.setattr(paths, "state_file", lambda: fake_state)
    # state.py captured these at import time as `_default_pid_file` /
    # `_default_state_file`; patch those bindings too.
    monkeypatch.setattr(state, "_default_pid_file", lambda: fake_pid)
    monkeypatch.setattr(state, "_default_state_file", lambda: fake_state)
    # Also redirect ensure_dirs so `record start` doesn't try to create
    # ~/Library/Application Support/record on the test machine.
    monkeypatch.setattr(
        paths,
        "ensure_dirs",
        lambda: paths.RecordPaths(
            app_support_dir=tmp_path,
            logs_dir=tmp_path,
            pid_file=fake_pid,
            state_file=fake_state,
            daemon_log=tmp_path / "daemon.log",
            orchestrator_log=tmp_path / "orchestrator.log",
        ),
    )
    return {"pid": fake_pid, "state": fake_state, "root": tmp_path}


# ---------------------------------------------------------------------------
# `record stop` exit codes
# ---------------------------------------------------------------------------


def test_stop_exits_1_when_no_pid_file(fake_paths: dict[str, Path]) -> None:
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "no capture running" in result.stderr


def test_stop_exits_1_on_stale_pid_file(fake_paths: dict[str, Path]) -> None:
    assert not state.is_alive(_DEAD_PID), "test prerequisite: dead PID must be dead"
    fake_paths["pid"].write_text(f"{_DEAD_PID}\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "stale" in result.stderr.lower()
    # Stale PID file must be cleaned up.
    assert not fake_paths["pid"].exists()


def test_stop_exits_4_when_supervisor_does_not_exit(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM never actually fires (we stub os.kill), so the live PID won't
    'exit' before the timeout. We shrink the timeout to make the test quick.
    """
    # The test process itself is the "supervisor". It's definitely alive.
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_STOP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cli, "_STOP_POLL_INTERVAL", 0.01)

    # Critically: stub os.kill so we don't actually SIGTERM the test runner.
    def _fake_kill(pid: int, sig: int) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(cli.os, "kill", _fake_kill)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 4
    assert "did not exit" in result.stderr


def test_stop_exits_1_when_process_disappears_after_check(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_alive`` says yes, then SIGTERM raises ProcessLookupError — race
    handling should clean up and exit 1.

    ``state.is_alive`` uses ``os.kill(pid, 0)`` so we only raise
    ``ProcessLookupError`` for non-zero signals (SIGTERM); the zero-signal
    probe still succeeds and tells the CLI the process is alive.
    """
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    real_kill = os.kill

    def _kill_raises_on_term(pid: int, sig: int) -> None:
        if sig == 0:
            real_kill(pid, 0)  # let is_alive's probe behave normally
            return
        raise ProcessLookupError()

    monkeypatch.setattr(cli.os, "kill", _kill_raises_on_term)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "process disappeared" in result.stderr


# ---------------------------------------------------------------------------
# `record start` exit codes
# ---------------------------------------------------------------------------


def test_start_exits_1_when_capture_already_running(fake_paths: dict[str, Path]) -> None:
    """A live PID in the pid file → exit 1, no supervisor spawned."""
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 1
    assert "capture already in progress" in result.stderr


def test_start_exits_3_when_binary_missing(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_resolve_capture_binary", lambda: None)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 3
    assert "make install" in result.stderr


def test_start_clears_stale_pid_before_spawning(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale PID file plus a missing binary → still exit 3 (not 1), and the
    stale file should have been cleaned up before the binary check ran."""
    assert not state.is_alive(_DEAD_PID)
    fake_paths["pid"].write_text(f"{_DEAD_PID}\n", encoding="utf-8")
    # Also leave a half-written state file behind to make sure it's swept too.
    fake_paths["state"].write_text('{"final": false}\n', encoding="utf-8")

    monkeypatch.setattr(cli, "_resolve_capture_binary", lambda: None)
    result = runner.invoke(cli.app, ["start"])
    assert result.exit_code == 3
    # The stale state file gets removed alongside the stale PID file before
    # `_resolve_capture_binary` runs.
    assert not fake_paths["state"].exists()


# ---------------------------------------------------------------------------
# `record stop` happy-path summary
# ---------------------------------------------------------------------------


def test_stop_prints_summary_when_state_present(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub os.kill + is_alive so the loop terminates on the first poll
    iteration, then assert the summary text from the state file."""
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    fake_paths["state"].write_text(
        (
            '{"output_path": "/abs/x.wav", "duration_seconds": 42.5, '
            '"sources": {"mic": {"status": "attached"}, '
            '"system_audio": {"status": "attached"}}, '
            '"warnings": [], "final": true}'
        ),
        encoding="utf-8",
    )

    # First call (real, before kill) sees alive=True; after our stubbed kill,
    # the polling loop sees alive=False on the first iteration. Easiest path:
    # stub os.kill to no-op and flip is_alive to False for any subsequent call.
    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:
        calls["is_alive"] += 1
        return calls["is_alive"] == 1  # alive once, dead thereafter

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0
    assert "capture stopped" in result.stdout
    assert "/abs/x.wav" in result.stdout
    assert "microphone + system audio" in result.stdout
    # Cleanup happened.
    assert not fake_paths["pid"].exists()
    assert not fake_paths["state"].exists()


# ---------------------------------------------------------------------------
# `_summarize_video` unit coverage
# ---------------------------------------------------------------------------


def test_summarize_video_never_attached() -> None:
    """Default state (no video source seen) keeps the slice-1 minimal form."""
    state_dict: dict[str, Any] = {
        "sources": {"video": {"status": "never_attached"}},
    }
    assert cli._summarize_video(state_dict) == "video: never_attached"


def test_summarize_video_attached_renders_path_duration_dimensions() -> None:
    """Slice-2 happy path: `video: <path> (<duration>, <w>×<h>)` using `×`."""
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
    """Long captures switch to ``Mm SSs`` like the audio summary."""
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
    """Defensive: if `video_file_duration_seconds` is absent but the audio
    capture has a duration, surface that rather than ``unknown``."""
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
    """If both `video_file_duration_seconds` and `duration_seconds` are absent
    (the `video_file` and `stopped` events never arrived), render `unknown`."""
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
    """Defensive: ``status=lost`` without the accompanying video warning falls
    back to the slice-2 ``(lost)`` shape rather than crashing."""
    state_dict: dict[str, Any] = {
        "video_output_path": "/abs/x.mp4",
        "sources": {"video": {"status": "lost"}},
        "warnings": [],
    }
    assert cli._summarize_video(state_dict) == "video: /abs/x.mp4 (lost)"


def test_summarize_video_lost_offset_zero_renders_unavailable() -> None:
    """Slice 5: offset 0 (video never started — typically permission denied)
    renders ``"video: unavailable — <reason>"`` with no path."""
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
    """Slice 5: mid-capture loss surfaces the partial mp4 path, the MM:SS offset
    where it stopped, and the reason."""
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
    """Slice 5: the video warning must not also appear as a generic
    ``warning:`` line; it's reflected on the ``video:`` line already."""
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
# `record stop` end-to-end summary against scripted state files
#
# The `_summarize_video` unit tests above pin the per-status string formats;
# these tests drive the full `record stop` CLI codepath against a pre-seeded
# state file (the equivalent of the supervisor having processed the matching
# event sequence) and assert the full summary block. The state files mirror
# what `supervisor._apply_event` would write after the corresponding event
# stream:
#
#   attached       : video_started → video_file
#   lost-offset-0  : video_lost(at_offset_seconds=0, reason="permission_denied")
#   lost-mid-cap   : video_lost(at_offset_seconds=N>0, reason="...")
#   never_attached : neither event observed (video skipped or pre-frame error)
# ---------------------------------------------------------------------------


def _install_stop_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Common stubs so the polling loop in `record stop` terminates immediately.

    Matches the pattern from ``test_stop_prints_summary_when_state_present``:
    stub ``os.kill`` to no-op and flip ``is_alive`` to ``False`` after the
    first probe so the loop exits on the next iteration.
    """
    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:  # noqa: ARG001
        calls["is_alive"] += 1
        return calls["is_alive"] == 1

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)


def _seed_stop_state(fake_paths: dict[str, Path], payload: dict[str, Any]) -> None:
    import json as _json

    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    fake_paths["state"].write_text(_json.dumps(payload), encoding="utf-8")


def test_stop_summary_video_attached_renders_path_and_dimensions(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 2 happy path under the full `record stop` flow: the summary block
    includes ``video: <path> (<duration>, <w>×<h>)`` on its own line."""
    _seed_stop_state(
        fake_paths,
        {
            "output_path": "/abs/2026-05-11T12-00-00.wav",
            "video_output_path": "/abs/2026-05-11T12-00-00.mp4",
            "duration_seconds": 12.3,
            "video_file_duration_seconds": 12.3,
            "sources": {
                "mic": {"status": "attached"},
                "system_audio": {"status": "attached"},
                "video": {
                    "status": "attached",
                    "width_px": 2560,
                    "height_px": 1440,
                },
            },
            "warnings": [],
            "final": True,
        },
    )
    _install_stop_stubs(monkeypatch)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "capture stopped" in result.stdout
    assert (
        "video: /abs/2026-05-11T12-00-00.mp4 (12.3 s, 2560×1440)"
        in result.stdout
    )


def test_stop_summary_video_lost_offset_zero_renders_unavailable(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5 case 1: permission denied / pre-first-frame failure renders
    ``video: unavailable — <reason>`` with no path."""
    _seed_stop_state(
        fake_paths,
        {
            "output_path": "/abs/2026-05-11T12-00-00.wav",
            # Supervisor clears the path on offset-0 loss; mirror that here.
            "video_output_path": None,
            "duration_seconds": 9.5,
            "sources": {
                "mic": {"status": "attached"},
                "system_audio": {"status": "attached"},
                "video": {"status": "lost"},
            },
            "warnings": [
                {
                    "source": "video",
                    "at_offset_seconds": 0,
                    "message": "permission_denied",
                }
            ],
            "final": True,
        },
    )
    _install_stop_stubs(monkeypatch)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "video: unavailable — permission_denied" in result.stdout
    # No phantom path on disk in the rendered summary block.
    assert ".mp4" not in result.stdout


def test_stop_summary_video_lost_mid_capture_renders_path_and_offset(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5 case 2: mid-capture loss renders the partial mp4 path plus the
    MM:SS offset and the failure reason on one line."""
    _seed_stop_state(
        fake_paths,
        {
            "output_path": "/abs/2026-05-11T12-00-00.wav",
            "video_output_path": "/abs/2026-05-11T12-00-00.mp4",
            "duration_seconds": 200.0,
            "sources": {
                "mic": {"status": "attached"},
                "system_audio": {"status": "attached"},
                "video": {"status": "lost"},
            },
            "warnings": [
                {
                    "source": "video",
                    "at_offset_seconds": 134.0,
                    "message": "sc_stream_error",
                }
            ],
            "final": True,
        },
    )
    _install_stop_stubs(monkeypatch)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert (
        "video: /abs/2026-05-11T12-00-00.mp4 — stopped at 02:14, reason sc_stream_error"
        in result.stdout
    )


def test_stop_summary_video_never_attached_renders_status(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 1 baseline: audio-only run (or pre-frame video skip) renders
    ``video: never_attached``. No file path on the line — there's no mp4."""
    _seed_stop_state(
        fake_paths,
        {
            "output_path": "/abs/2026-05-11T12-00-00.wav",
            "video_output_path": "/abs/2026-05-11T12-00-00.mp4",
            "duration_seconds": 3.0,
            "sources": {
                "mic": {"status": "attached"},
                "system_audio": {"status": "attached"},
                "video": {"status": "never_attached"},
            },
            "warnings": [],
            "final": True,
        },
    )
    _install_stop_stubs(monkeypatch)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0, result.stderr
    assert "video: never_attached" in result.stdout


def test_stop_exits_2_on_permission_denied(
    fake_paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """capture-state.json with `permission_denied: "microphone"` → exit 2.

    We stub os.kill and is_alive the same way the happy-path test does so we
    can reach the post-wait state-read branch without touching real signals.
    """
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    fake_paths["state"].write_text(
        '{"permission_denied": "microphone", "final": true}', encoding="utf-8"
    )

    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:
        calls["is_alive"] += 1
        return calls["is_alive"] == 1

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 2
    assert "microphone permission denied" in result.stderr
    assert "System Settings" in result.stderr
    assert not fake_paths["pid"].exists()
    assert not fake_paths["state"].exists()


# ---------------------------------------------------------------------------
# `record daemon start/stop/restart` — spec 003 slice 1
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_daemon_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Redirect every daemon-side path the CLI consults.

    Distinct from ``fake_paths`` because the daemon's PID file lives at a
    different path than the supervisor's, and we never want the CLI tests to
    create files inside the user's real ``~/Library/Application Support`` or
    ``~/record/`` directories.
    """
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
    """Minimal `subprocess.Popen` stub for daemon-spawn tests.

    Records argv on construction and reports `poll() is None` so the CLI's
    handshake loop treats the daemon as healthy. Tests that want to simulate
    a failed launch can post-hoc set `_returncode` to a non-None value.
    """

    instances: list["_FakePopen"] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242  # arbitrary; tests never call os.kill against it
        self._returncode: int | None = None
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self._returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    """Replace `cli.subprocess.Popen` with the recording stub.

    Tests opt into "writes the PID file on construction" by composing their
    own wrapper around the stub; the bare fixture just records argv.
    """
    _FakePopen.instances.clear()
    monkeypatch.setattr(cli.subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_daemon_start_spawns_daemon_module(
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: no existing PID file → Popen called → handshake sees the
    PID file appear → CLI prints "daemon started" and exits 0."""

    # Wrap Popen so it materialises the daemon PID file on construction —
    # this is what the real daemon does in its startup path. Without it, the
    # handshake would time out (the test would still exit 0 with a warning,
    # but we want to exercise the happy "PID file appeared" branch).
    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "start"])
    assert result.exit_code == 0, result.stderr
    assert "daemon started" in result.stdout
    assert "PID 4242" in result.stdout

    # The CLI spawned `python -m record.daemon` exactly once.
    assert len(_FakePopen.instances) == 1
    argv = _FakePopen.instances[0].argv
    assert argv[1:] == ["-m", "record.daemon"]


def test_daemon_start_when_already_running_prints_message_and_skips_spawn(
    fake_daemon_paths: dict[str, Path], fake_popen: type[_FakePopen]
) -> None:
    """A PID file pointing at a live process → "daemon already running", no spawn."""
    fake_daemon_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["daemon", "start"])
    assert result.exit_code == 0
    assert "daemon already running" in result.stdout
    assert str(os.getpid()) in result.stdout
    # Popen must NOT have been called.
    assert _FakePopen.instances == []


def test_daemon_start_clears_stale_pid_file_before_spawn(
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale PID file (dead PID) → CLI clears it, spawns, succeeds."""
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
    # Stale PID was overwritten with the new daemon's PID, not the dead one.
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
    """Live PID + stubbed os.kill + is_alive flipping True→False → exit 0."""
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
    """is_alive stays True past _STOP_TIMEOUT_SECONDS → exit code 4."""
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
    """Live PID → stop succeeds → start spawns → restart exits 0."""
    fake_daemon_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")

    # `is_alive` flips True→False after the first probe so the stop polling
    # loop terminates immediately, then stays False so the start path's
    # initial "already running?" check sees no live PID.
    calls: dict[str, int] = {"is_alive": 0}

    def _fake_is_alive(pid: int) -> bool:  # noqa: ARG001
        calls["is_alive"] += 1
        return calls["is_alive"] == 1

    monkeypatch.setattr(state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.state, "is_alive", _fake_is_alive)
    monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)

    # The stop path doesn't clean up the PID file (the daemon would, but
    # there's no real daemon here). Simulate the daemon's cleanup by removing
    # the PID file inside our fake kill — well, easier: have Popen overwrite
    # it on spawn. But the start path checks `read_pid_file` first; with
    # is_alive returning False on call #2 (the start path's check), the
    # stale-recovery branch kicks in and clears the file before spawning.
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
    """No PID file at all → restart treats "not running" as fine and starts fresh."""
    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "restart"])
    assert result.exit_code == 0, result.stderr
    assert "daemon started" in result.stdout
    # Stop printed its "not running" line to stderr, but exit was still 0
    # because restart treats not-running as fine.
    assert "daemon is not running" in result.stderr


def test_daemon_start_does_not_touch_capture_pid_file(
    fake_paths: dict[str, Path],
    fake_daemon_paths: dict[str, Path],
    fake_popen: type[_FakePopen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical isolation test: `record daemon start` must not interfere with
    the legacy supervisor's capture.pid bookkeeping.
    """
    # Seed a "live capture" via the legacy PID file.
    fake_paths["pid"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert fake_paths["pid"].exists()

    real_init = _FakePopen.__init__

    def _init_with_pidfile(self: _FakePopen, argv: list[str], **kwargs: Any) -> None:
        real_init(self, argv, **kwargs)
        fake_daemon_paths["pid"].write_text(f"{self.pid}\n", encoding="utf-8")

    monkeypatch.setattr(_FakePopen, "__init__", _init_with_pidfile)

    result = runner.invoke(cli.app, ["daemon", "start"])
    assert result.exit_code == 0, result.stderr
    # Legacy capture.pid is untouched.
    assert fake_paths["pid"].exists()
    assert fake_paths["pid"].read_text().strip() == str(os.getpid())
