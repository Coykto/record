"""Tests for :mod:`record.secrets` — Deepgram API key resolution.

The macOS Keychain is never touched: ``keyring`` is stubbed with an in-memory
backend so the tests are hermetic and run on any platform / CI box.
"""

from __future__ import annotations

import keyring
import pytest

from record import secrets


class _MemoryKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module surface we use."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.store[(service, account)] = password


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> _MemoryKeyring:
    """Replace ``keyring.get_password`` / ``set_password`` with an in-memory map."""
    backend = _MemoryKeyring()
    monkeypatch.setattr(secrets.keyring, "get_password", backend.get_password)
    monkeypatch.setattr(secrets.keyring, "set_password", backend.set_password)
    return backend


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the env-var fallback is unset unless a test sets it explicitly."""
    monkeypatch.delenv(secrets.ENV_VAR, raising=False)


def test_returns_none_when_neither_env_nor_keychain_set(
    memory_keyring: _MemoryKeyring,
) -> None:
    assert secrets.get_deepgram_api_key() is None


def test_reads_from_keychain_when_env_unset(
    memory_keyring: _MemoryKeyring,
) -> None:
    memory_keyring.store[
        (secrets.KEYCHAIN_SERVICE, secrets.KEYCHAIN_ACCOUNT)
    ] = "key-from-keychain"
    assert secrets.get_deepgram_api_key() == "key-from-keychain"


def test_env_var_takes_precedence_over_keychain(
    memory_keyring: _MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_keyring.store[
        (secrets.KEYCHAIN_SERVICE, secrets.KEYCHAIN_ACCOUNT)
    ] = "key-from-keychain"
    monkeypatch.setenv(secrets.ENV_VAR, "key-from-env")
    assert secrets.get_deepgram_api_key() == "key-from-env"


def test_blank_env_var_falls_through_to_keychain(
    memory_keyring: _MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_keyring.store[
        (secrets.KEYCHAIN_SERVICE, secrets.KEYCHAIN_ACCOUNT)
    ] = "key-from-keychain"
    monkeypatch.setenv(secrets.ENV_VAR, "   ")
    assert secrets.get_deepgram_api_key() == "key-from-keychain"


def test_env_var_only_no_keychain(
    memory_keyring: _MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(secrets.ENV_VAR, "env-only-key")
    assert secrets.get_deepgram_api_key() == "env-only-key"


def test_set_deepgram_api_key_stores_into_keychain(
    memory_keyring: _MemoryKeyring,
) -> None:
    secrets.set_deepgram_api_key("a-new-key")
    assert (
        memory_keyring.store[(secrets.KEYCHAIN_SERVICE, secrets.KEYCHAIN_ACCOUNT)]
        == "a-new-key"
    )
    # And it round-trips through the getter.
    assert secrets.get_deepgram_api_key() == "a-new-key"


def test_keyring_error_is_swallowed_as_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Keychain backend → ``None``, not a crash."""

    def _boom(service: str, account: str) -> str | None:
        raise keyring.errors.KeyringError("backend unavailable")

    monkeypatch.setattr(secrets.keyring, "get_password", _boom)
    assert secrets.get_deepgram_api_key() is None
