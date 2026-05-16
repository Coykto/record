"""Shared fixtures for the black-box integration tests.

These tests spawn the real ``record-capture`` Swift binary as a subprocess and
inspect its stdout JSON-line event stream and the resulting WAV file. They do
NOT import any ``record.*`` Python modules — the orchestrator's own pydantic
models are exercised by the unit tests under ``tests/python/``. Here we want a
true wire-level black-box check so the integration coverage stays decoupled
from the orchestrator's internal types.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Repo root resolution: this file lives at <repo>/tests/integration/conftest.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_BINARY = (
    REPO_ROOT
    / "src"
    / "record"
    / "bin"
    / "record-capture.app"
    / "Contents"
    / "MacOS"
    / "record-capture"
)


@pytest.fixture(scope="session")
def capture_binary() -> Path:
    """Return the absolute path of the built ``record-capture`` binary.

    Skips the test cleanly if the binary is not present or not executable.
    ``make swift`` builds it; ``make test`` (sub-task 5) will invoke that
    target as a prerequisite, but a manual ``pytest`` invocation against a
    fresh checkout should not fail — it should skip.
    """
    if not CAPTURE_BINARY.exists():
        pytest.skip(
            f"record-capture binary not built at {CAPTURE_BINARY}; "
            "run `make swift` first"
        )
    if not os.access(CAPTURE_BINARY, os.X_OK):
        pytest.skip(
            f"record-capture binary at {CAPTURE_BINARY} is not executable; "
            "run `make swift` first"
        )
    return CAPTURE_BINARY


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-real-capture",
        action="store_true",
        default=False,
        help=(
            "Run real-capture tests that hit the actual macOS capture "
            "pipeline (requires BlackHole + SwitchAudioSource + TCC grants)."
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-real-capture"):
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if "real_capture" in item.keywords:
            deselected.append(item)
        else:
            selected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture
def real_capture_sandbox():
    """Sandboxed ``$HOME`` for the real-capture end-to-end test.

    Mirrors the boilerplate at ``test_end_to_end.py:843-867`` but **without**
    the ``RECORD_CAPTURE_TEST_FLAGS`` env var, so the Swift child runs against
    the real ``SCStream`` / ``AVAudioEngine`` paths instead of synthetic mode.

    Yields a tuple ``(sandbox, cwd, env, socket_path, state_path)``. Cleans up
    the sandbox via ``shutil.rmtree(..., ignore_errors=True)`` on teardown.

    The sandbox path is intentionally rooted at ``/tmp/rd-XXXX`` (not the
    default ``$TMPDIR`` ``/var/folders/.../T/``) so that the
    ``$HOME/Library/Application Support/record/daemon.sock`` path stays
    inside the 104-character ``AF_UNIX`` limit on macOS.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="rd-", dir="/tmp"))
    try:
        cwd = sandbox / "out"
        cwd.mkdir()
        # Resolve symlinks (``/tmp`` -> ``/private/tmp`` on macOS) so the
        # equality check against the daemon-returned path lines up.
        cwd_resolved = cwd.resolve()

        config_dir = sandbox / ".config" / "record"
        config_dir.mkdir(parents=True)
        log_dir = sandbox / "logs"
        log_dir.mkdir()
        (config_dir / "config.toml").write_text(
            f'output_folder = "{cwd_resolved}"\n'
            f'log_folder = "{log_dir.resolve()}"\n',
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["HOME"] = str(sandbox)
        # NOTE: do NOT set ``RECORD_CAPTURE_TEST_FLAGS`` — the whole point of
        # the real-capture suite is to exercise the production pipeline.

        socket_path = (
            sandbox
            / "Library"
            / "Application Support"
            / "record"
            / "daemon.sock"
        )
        state_path = (
            sandbox
            / "Library"
            / "Application Support"
            / "record"
            / "capture-state.json"
        )

        yield sandbox, cwd, env, socket_path, state_path
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
