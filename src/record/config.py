"""User-facing configuration for the daemon.

Spec 003 / tech spec §2.8. The configuration file lives at
``~/.config/record/config.toml`` and exposes exactly four keys in v1:
``hotkey``, ``output_folder``, ``log_folder``, ``audible_feedback``. Missing
file → all defaults; no error.

Path validation rules
---------------------

- A leading ``~`` is expanded via :meth:`Path.expanduser`.
- After expansion the path must be absolute. Non-absolute → hard
  :class:`ConfigError`.
- The path must be either non-existent or an existing directory. A regular
  file (or symlink to file, etc.) at the configured path → hard
  :class:`ConfigError`. The directories themselves are NOT created here —
  :func:`record.paths.ensure_dirs_from_config` does that at daemon startup.

Hotkey handling
---------------

The hotkey string is parsed via :func:`record.hotkey.parse`. An invalid string
does **NOT** raise: it falls back to :data:`record.hotkey.DEFAULT_HOTKEY` and
records the parser error in :attr:`Config.hotkey_parse_error`, which the
``record status`` CLI (slice 5) surfaces. FR 2.6 last bullet: "[the daemon]
never refuses to start because of a bad hotkey value."

Unknown top-level keys are logged at WARNING via the ``record.config``
logger and otherwise ignored, so a config file written for a future daemon
version (with extra keys) keeps working on an older daemon.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .hotkey import DEFAULT_HOTKEY, HotkeyParseError, parse as parse_hotkey
from .logging_setup import get_logger

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default config path. Resolved against the *current* ``Path.home()`` at
#: import time would be wrong (tests monkeypatch ``Path.home``); this is a
#: cached property of the *function* :func:`load_config`. We keep a module
#: attribute too because the architecture doc references it, but consumers
#: should call :func:`default_config_path` if they want a fresh resolution.
DEFAULT_CONFIG_PATH: Path = Path.home() / ".config" / "record" / "config.toml"


def default_config_path() -> Path:
    """Return ``~/.config/record/config.toml`` against the live ``Path.home()``."""
    return Path.home() / ".config" / "record" / "config.toml"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised on a fatal configuration problem (non-absolute path, collision)."""


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


# Recognised top-level keys. Drives the "unknown key → warn" pass.
_KNOWN_KEYS: frozenset[str] = frozenset(
    {"hotkey", "output_folder", "log_folder", "audible_feedback"}
)


def _default_output_folder() -> Path:
    return (Path.home() / "record").resolve()


def _default_log_folder() -> Path:
    return (Path.home() / "record" / "logs").resolve()


class Config(BaseSettings):
    """Resolved configuration. All paths are absolute and tilde-expanded."""

    model_config = SettingsConfigDict(
        # We do our own TOML parse + "unknown key" warning, so let pydantic
        # ignore extras rather than raise on them.
        extra="ignore",
    )

    hotkey: str = Field(default=DEFAULT_HOTKEY)
    output_folder: Path = Field(default_factory=_default_output_folder)
    log_folder: Path = Field(default_factory=_default_log_folder)
    audible_feedback: bool = Field(default=True)

    # Set by :func:`load_config` when the configured ``hotkey`` value failed
    # to parse and we fell back to the default. Surfaced by ``record status``
    # in slice 5. ``None`` means the configured hotkey parsed cleanly.
    hotkey_parse_error: str | None = Field(default=None)

    @field_validator("output_folder", "log_folder", mode="before")
    @classmethod
    def _expand_and_validate_path(cls, v: Any) -> Path:
        if isinstance(v, Path):
            raw = str(v)
        else:
            raw = str(v)
        # Resolve ``~`` against the live :meth:`Path.home`. Path.expanduser
        # consults ``$HOME`` directly, which side-steps a ``Path.home``
        # monkeypatch (tests rely on that monkeypatch to sandbox); resolving
        # the tilde manually keeps both production and the test sandbox happy.
        if raw.startswith("~/") or raw == "~":
            tail = raw[1:].lstrip("/")
            p = Path.home() / tail if tail else Path.home()
        else:
            p = Path(raw)
        if not p.is_absolute():
            raise ConfigError(
                f"path must be absolute after tilde expansion: {v!r}"
            )
        return p


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _check_dir_collision(label: str, path: Path) -> None:
    """Raise :class:`ConfigError` if ``path`` exists and isn't a directory."""
    if path.exists() and not path.is_dir():
        raise ConfigError(
            f"{label} {path} exists but is not a directory"
        )


def load_config(path: Path | None = None) -> Config:
    """Load configuration from ``path`` (default ``~/.config/record/config.toml``).

    Missing file → return defaults. Bad path values → raise
    :class:`ConfigError`. Bad hotkey → fall back to default, record the error
    on :attr:`Config.hotkey_parse_error`.
    """
    log = get_logger("record.config")

    target = path if path is not None else default_config_path()

    raw: dict[str, Any] = {}
    if target.exists():
        try:
            with target.open("rb") as fh:
                raw = tomllib.load(fh)
        except OSError as exc:
            # An existing-but-unreadable file is a hard error — the user
            # almost certainly wanted us to read it. Treating it as "use
            # defaults" would silently hide a misconfiguration.
            raise ConfigError(f"cannot read config file {target}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {target}: {exc}") from exc

    # Warn on unknown top-level keys before dropping them. We log via stdlib's
    # logger directly because the WARNING surface here is the one the
    # ``record status`` renderer reads via caplog in tests; stdlib + structlog
    # both flow through this name.
    unknown = sorted(k for k in raw.keys() if k not in _KNOWN_KEYS)
    for key in unknown:
        log.warning(
            "config_unknown_key",
            key=key,
            path=str(target),
        )

    # Strip unknowns so pydantic-settings doesn't try to coerce them.
    cleaned = {k: v for k, v in raw.items() if k in _KNOWN_KEYS}

    # Try to construct the Config. Path validators may raise ConfigError; we
    # re-raise unchanged because pydantic wraps validation errors but keeps
    # the original cause accessible.
    try:
        cfg = Config(**cleaned)
    except Exception as exc:
        # If the root cause was a ConfigError (path validation), surface it
        # cleanly; otherwise re-raise as ConfigError so callers have one
        # exception type to catch.
        root: BaseException | None = exc
        while root is not None:
            if isinstance(root, ConfigError):
                raise root
            root = root.__cause__ or root.__context__
        raise ConfigError(f"invalid config in {target}: {exc}") from exc

    # Collision check happens after path validation so we already have an
    # absolute path to look at.
    _check_dir_collision("output_folder", cfg.output_folder)
    _check_dir_collision("log_folder", cfg.log_folder)

    # Validate the hotkey string. Fall back on failure (FR 2.6).
    try:
        parse_hotkey(cfg.hotkey)
    except HotkeyParseError as exc:
        log.warning(
            "config_invalid_hotkey",
            hotkey=cfg.hotkey,
            error=str(exc),
        )
        # Construct a new Config with the default hotkey and the original
        # error preserved for ``record status``.
        return cfg.model_copy(
            update={
                "hotkey": DEFAULT_HOTKEY,
                "hotkey_parse_error": str(exc),
            }
        )

    return cfg


__all__ = [
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "default_config_path",
    "load_config",
]
