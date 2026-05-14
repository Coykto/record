"""Tests for :mod:`record.launchagent` — spec 003 slice 7.

The real ``launchctl`` is never invoked: every test installs an argv-recording
stub via :func:`launchagent.set_launchctl_runner` and drives the install /
uninstall flows through that.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from record import launchagent, paths, state
from record.config import Config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ``Path.home()`` to a tmp dir; reset the launchctl runner after."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    yield tmp_path
    launchagent.set_launchctl_runner(None)


@pytest.fixture
def daemon_pid_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect ``paths.daemon_pid_file()`` into the sandbox."""
    pid_file = tmp_path / "daemon.pid"
    monkeypatch.setattr(paths, "daemon_pid_file", lambda: pid_file)
    return pid_file


@pytest.fixture
def fixed_executable(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``sys.executable`` to a deterministic value for plist diffs."""
    fake = "/opt/record/.venv/bin/python"
    monkeypatch.setattr(launchagent.sys, "executable", fake)
    return fake


# ---------------------------------------------------------------------------
# Plist generation — golden-file diff
# ---------------------------------------------------------------------------


def _make_config(*, log_folder: Path, output_folder: Path | None = None) -> Config:
    return Config(
        hotkey="option+command+r",
        output_folder=output_folder or (log_folder.parent),
        log_folder=log_folder,
        audible_feedback=True,
    )


def test_build_plist_default_log_folder(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(
        log_folder=sandbox_home / "record" / "logs",
        output_folder=sandbox_home / "record",
    )
    out = launchagent.build_plist(cfg)
    parsed = plistlib.loads(out)
    assert parsed["Label"] == "com.record.daemon"
    assert parsed["ProgramArguments"] == [fixed_executable, "-m", "record.daemon"]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["ProcessType"] == "Background"
    assert parsed["EnvironmentVariables"] == {"RECORD_LAUNCHD": "1"}
    assert parsed["StandardOutPath"] == str(
        sandbox_home / "record" / "logs" / "daemon-launchd.out.log"
    )
    assert parsed["StandardErrorPath"] == str(
        sandbox_home / "record" / "logs" / "daemon-launchd.err.log"
    )
    # Canonical (sort_keys=True) XML — assert the literal byte form.
    assert out.startswith(b"<?xml")
    assert b"<key>EnvironmentVariables</key>" in out
    # Keys are sorted: EnvironmentVariables < KeepAlive < Label.
    env_idx = out.index(b"<key>EnvironmentVariables</key>")
    keep_idx = out.index(b"<key>KeepAlive</key>")
    label_idx = out.index(b"<key>Label</key>")
    assert env_idx < keep_idx < label_idx


def test_build_plist_custom_log_folder(
    sandbox_home: Path, fixed_executable: str
) -> None:
    custom = Path("/tmp/record-custom/logs")
    cfg = _make_config(log_folder=custom, output_folder=Path("/tmp/record-custom"))
    parsed = plistlib.loads(launchagent.build_plist(cfg))
    assert parsed["StandardOutPath"] == "/tmp/record-custom/logs/daemon-launchd.out.log"
    assert parsed["StandardErrorPath"] == "/tmp/record-custom/logs/daemon-launchd.err.log"


# ---------------------------------------------------------------------------
# `install` flow
# ---------------------------------------------------------------------------


class _ArgvRecorder:
    """Argv-recording stub for :data:`launchagent._RUN`.

    Each entry in :attr:`responses` is a ``(returncode, stderr)`` tuple; the
    stub consumes them in order. After exhaustion every call returns
    ``(0, "")`` so the test only has to specify the prefix it cares about.
    """

    def __init__(
        self, responses: list[tuple[int, str]] | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self.responses: list[tuple[int, str]] = list(responses or [])

    def __call__(self, argv: list[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        if self.responses:
            rc, err = self.responses.pop(0)
        else:
            rc, err = 0, ""
        return subprocess.CompletedProcess(
            args=argv, returncode=rc, stdout="", stderr=err
        )


def _expected_target() -> str:
    return f"gui/{os.getuid()}"


def test_install_happy_path_writes_plist_and_bootstraps(
    sandbox_home: Path,
    fixed_executable: str,
    daemon_pid_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _ArgvRecorder()
    launchagent.set_launchctl_runner(recorder)

    # Pretend the daemon raced ahead and wrote its PID file.
    daemon_pid_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    result = launchagent.install(cfg)

    assert result.success is True
    assert result.re_registered is False
    assert result.pid == os.getpid()

    # plist written
    assert launchagent.plist_path().exists()
    # first launchctl call is bootstrap gui/<uid> <plist>
    assert recorder.calls[0][:3] == [
        "launchctl",
        "bootstrap",
        _expected_target(),
    ]
    assert recorder.calls[0][3] == str(launchagent.plist_path())


def test_install_already_loaded_triggers_bootout_then_rebootstrap(
    sandbox_home: Path,
    fixed_executable: str,
    daemon_pid_path: Path,
) -> None:
    # First bootstrap fails with "already loaded", then bootout succeeds, then
    # the retry bootstrap succeeds.
    recorder = _ArgvRecorder(
        responses=[
            (1, "Bootstrap failed: 5: Input/output error"),
            (0, ""),  # bootout
            (0, ""),  # retry bootstrap
        ]
    )
    launchagent.set_launchctl_runner(recorder)
    daemon_pid_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_pid_path.write_text("4242\n", encoding="utf-8")
    # ``is_alive(4242)`` may return False on this machine; the install path
    # falls back to the last-seen value, so 4242 still surfaces.

    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    result = launchagent.install(cfg)

    assert result.success is True
    assert result.re_registered is True
    assert [c[1] for c in recorder.calls[:3]] == ["bootstrap", "bootout", "bootstrap"]


def test_install_bootstrap_unrecoverable_failure_surfaces_stderr(
    sandbox_home: Path,
    fixed_executable: str,
    daemon_pid_path: Path,
) -> None:
    # Both the initial bootstrap and the retry fail — install must give up.
    recorder = _ArgvRecorder(
        responses=[
            (1, "Bootstrap failed: 125: Operation not permitted"),
            (0, ""),  # bootout
            (1, "Bootstrap failed: 125: Operation not permitted"),
        ]
    )
    launchagent.set_launchctl_runner(recorder)

    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    result = launchagent.install(cfg)

    assert result.success is False
    assert "Operation not permitted" in result.launchctl_stderr


# ---------------------------------------------------------------------------
# `uninstall` flow
# ---------------------------------------------------------------------------


def test_uninstall_happy_path(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    launchagent.write_plist(cfg)
    assert launchagent.plist_path().exists()

    recorder = _ArgvRecorder()
    launchagent.set_launchctl_runner(recorder)

    result = launchagent.uninstall()

    assert result.success is True
    assert result.already_unregistered is False
    assert not launchagent.plist_path().exists()
    assert recorder.calls[0][:3] == [
        "launchctl",
        "bootout",
        _expected_target(),
    ]


def test_uninstall_when_plist_absent_is_idempotent(
    sandbox_home: Path,
) -> None:
    recorder = _ArgvRecorder()
    launchagent.set_launchctl_runner(recorder)
    assert not launchagent.plist_path().exists()

    result = launchagent.uninstall()

    assert result.success is True
    assert result.already_unregistered is True
    # No bootout invocation when the plist isn't even there.
    assert recorder.calls == []


def test_uninstall_bootout_already_unloaded_is_success(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    launchagent.write_plist(cfg)

    recorder = _ArgvRecorder(
        responses=[
            (1, "Could not find service \"com.record.daemon\" in domain"),
        ]
    )
    launchagent.set_launchctl_runner(recorder)

    result = launchagent.uninstall()

    assert result.success is True
    # Plist still cleaned up even on the "not loaded" bootout path.
    assert not launchagent.plist_path().exists()


def test_uninstall_bootout_failure_surfaces_stderr(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    launchagent.write_plist(cfg)

    recorder = _ArgvRecorder(
        responses=[
            (1, "Operation not permitted"),
        ]
    )
    launchagent.set_launchctl_runner(recorder)

    result = launchagent.uninstall()

    assert result.success is False
    assert "Operation not permitted" in result.launchctl_stderr
    # Plist preserved so a retry can use it.
    assert launchagent.plist_path().exists()


# ---------------------------------------------------------------------------
# `is_registered`
# ---------------------------------------------------------------------------


def test_is_registered_true_when_plist_exists_and_print_exit_zero(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    launchagent.write_plist(cfg)
    launchagent.set_launchctl_runner(_ArgvRecorder())  # default 0/""
    assert launchagent.is_registered() is True


def test_is_registered_false_when_plist_missing(
    sandbox_home: Path,
) -> None:
    # Even a 0-exit `launchctl print` doesn't beat the plist check.
    launchagent.set_launchctl_runner(_ArgvRecorder())
    assert launchagent.is_registered() is False


def test_is_registered_false_when_print_exit_nonzero(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    launchagent.write_plist(cfg)
    launchagent.set_launchctl_runner(
        _ArgvRecorder(responses=[(1, "no such service")])
    )
    assert launchagent.is_registered() is False


def test_is_registered_false_after_uninstall(
    sandbox_home: Path, fixed_executable: str
) -> None:
    cfg = _make_config(log_folder=sandbox_home / "record" / "logs")
    launchagent.write_plist(cfg)
    launchagent.set_launchctl_runner(_ArgvRecorder())
    launchagent.uninstall()
    assert launchagent.is_registered() is False
