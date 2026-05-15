"""Secret storage for the orchestrator — the Deepgram API key.

Per ``context/product/architecture.md`` and spec 004 tech spec §2.3, the
transcription API key is **never** written to ``config.toml``. It lives in the
macOS Keychain via the :mod:`keyring` library, with the
``RECORD_DEEPGRAM_API_KEY`` environment variable honored as a developer
fallback.

Resolution order for :func:`get_deepgram_api_key`:

1. ``RECORD_DEEPGRAM_API_KEY`` environment variable (dev fallback).
2. macOS Keychain item (service/account = the module constants below).
3. ``None`` if neither is set.

The key value is **never logged** — callers that log request metadata must not
include it, and this module logs nothing containing the secret.
"""

from __future__ import annotations

import os

import keyring

# Keychain service + account names. ``keyring`` keys an item on this pair; the
# service is the app identity, the account names the specific secret. Kept as
# module constants so there is exactly one place to change them.
KEYCHAIN_SERVICE: str = "com.record.orchestrator"
KEYCHAIN_ACCOUNT: str = "deepgram-api-key"

# Developer-fallback environment variable (architecture doc §2 "Secrets").
ENV_VAR: str = "RECORD_DEEPGRAM_API_KEY"


def get_deepgram_api_key() -> str | None:
    """Return the Deepgram API key, or ``None`` if none is configured.

    Checks the ``RECORD_DEEPGRAM_API_KEY`` environment variable first (dev
    fallback), then the macOS Keychain. An empty/whitespace-only env value is
    treated as unset so an exported-but-blank variable falls through to the
    Keychain rather than masking it.
    """
    env_value = os.environ.get(ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value

    try:
        stored = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    except keyring.errors.KeyringError:
        # A broken/unavailable Keychain backend is treated as "no key" — the
        # caller decides what to do (skip transcription, exit 2, …). We do not
        # raise here so a Keychain hiccup never crashes the orchestrator.
        return None

    if stored is not None and stored.strip():
        return stored
    return None


def set_deepgram_api_key(key: str) -> None:
    """Store ``key`` in the macOS Keychain.

    Used by the ``record install`` prompt. Overwrites any existing item for the
    service/account pair.
    """
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, key)


__all__ = [
    "ENV_VAR",
    "KEYCHAIN_ACCOUNT",
    "KEYCHAIN_SERVICE",
    "get_deepgram_api_key",
    "set_deepgram_api_key",
]
