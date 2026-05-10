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
from pathlib import Path

import pytest

# Repo root resolution: this file lives at <repo>/tests/integration/conftest.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_BINARY = REPO_ROOT / "src" / "record" / "bin" / "record-capture"


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
