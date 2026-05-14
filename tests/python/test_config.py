"""Tests for :mod:`record.config` — the slice-3 config loader.

These tests use ``tmp_path`` and an explicit ``load_config(path=...)`` call
to avoid touching the real ``~/.config/record/config.toml``. Where the home
default matters (the path-expansion test), ``Path.home`` itself is
monkeypatched to sandbox it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from record import config as config_module
from record.config import Config, ConfigError, load_config
from record.hotkey import DEFAULT_HOTKEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ``Path.home()`` to ``tmp_path`` for the duration of the test."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Missing / empty file
# ---------------------------------------------------------------------------


def test_missing_file_returns_defaults(sandbox_home: Path) -> None:
    cfg = load_config(path=sandbox_home / "does" / "not" / "exist.toml")
    assert cfg.hotkey == DEFAULT_HOTKEY
    assert cfg.output_folder == sandbox_home / "record"
    assert cfg.log_folder == sandbox_home / "record" / "logs"
    assert cfg.audible_feedback is True
    assert cfg.hotkey_parse_error is None


def test_empty_file_returns_defaults(sandbox_home: Path, tmp_path: Path) -> None:
    cfg_path = tmp_path / "empty.toml"
    cfg_path.write_text("", encoding="utf-8")
    cfg = load_config(path=cfg_path)
    assert cfg.hotkey == DEFAULT_HOTKEY
    assert cfg.output_folder == sandbox_home / "record"


# ---------------------------------------------------------------------------
# Single-key override
# ---------------------------------------------------------------------------


def test_partial_override_keeps_defaults_for_other_keys(
    sandbox_home: Path, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('audible_feedback = false\n', encoding="utf-8")
    cfg = load_config(path=cfg_path)
    assert cfg.audible_feedback is False
    # Untouched keys keep defaults.
    assert cfg.hotkey == DEFAULT_HOTKEY
    assert cfg.output_folder == sandbox_home / "record"


# ---------------------------------------------------------------------------
# Unknown keys → WARNING
# ---------------------------------------------------------------------------


def test_unknown_top_level_keys_logged_at_warning(
    sandbox_home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'audible_feedback = true\nfuture_setting = "value"\nanother = 42\n',
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger="record.config")
    cfg = load_config(path=cfg_path)

    # Load still succeeded.
    assert cfg.audible_feedback is True

    # Both unknown keys produced warning records.
    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    full = messages + "\n" + "\n".join(str(rec.__dict__) for rec in caplog.records)
    assert "future_setting" in full
    assert "another" in full


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def test_tilde_expansion_in_output_folder(
    sandbox_home: Path, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('output_folder = "~/foo"\n', encoding="utf-8")
    cfg = load_config(path=cfg_path)
    assert cfg.output_folder == sandbox_home / "foo"


def test_non_absolute_output_folder_rejected(
    sandbox_home: Path, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('output_folder = "relative/path"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=cfg_path)


def test_non_absolute_log_folder_rejected(
    sandbox_home: Path, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('log_folder = "also/relative"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path=cfg_path)


def test_output_folder_collides_with_file(
    sandbox_home: Path, tmp_path: Path
) -> None:
    # A regular file already exists at the configured output_folder path.
    collide = tmp_path / "not-a-dir"
    collide.write_text("hello", encoding="utf-8")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'output_folder = "{collide}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(path=cfg_path)
    assert str(collide) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Invalid hotkey → fall back + record error
# ---------------------------------------------------------------------------


def test_invalid_hotkey_falls_back_and_records_error(
    sandbox_home: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('hotkey = "command+oops+r"\n', encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="record.config")
    cfg = load_config(path=cfg_path)

    assert cfg.hotkey == DEFAULT_HOTKEY
    assert cfg.hotkey_parse_error is not None
    assert "oops" in cfg.hotkey_parse_error or "command+oops+r" in cfg.hotkey_parse_error
