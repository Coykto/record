"""Shared test configuration for the Python unit-test suite.

We expose the shared JSON-line fixtures (consumed by both the Python and Swift
test suites) via ``FIXTURES_DIR`` so individual tests can locate them without
hard-coding relative paths.

Note: ``pyproject.toml`` injects ``src/`` onto ``pythonpath`` via
``[tool.pytest.ini_options]``, so ``import record.ipc`` etc. works on a fresh
checkout without ``make install`` / ``pip install -e .``.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR: Path = (
    Path(__file__).resolve().parents[2]
    / "swift-capture"
    / "Tests"
    / "RecordCaptureTests"
    / "Fixtures"
)
