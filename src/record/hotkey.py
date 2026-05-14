"""Pure-Python parser for the FR 2.10 / tech spec §2.8 hotkey grammar.

Grammar: ``<modifier>+<modifier>+...+<key>`` where one or more modifiers
prefix exactly one non-modifier key. Modifiers are case-insensitive among
``cmd``/``command``, ``opt``/``option``/``alt``, ``ctrl``/``control``, and
``shift``. The key is one of ``a-z``, ``0-9``, ``f1``..``f20``, or one of the
named whitelist ``space``, ``tab``, ``return``, ``escape``, ``delete``.

This module is intentionally framework-free: it only validates and
canonicalises the string. The Carbon keycode translation lives behind a
different seam (Swift, slice 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: The default hotkey when ``config.toml`` does not specify one.
DEFAULT_HOTKEY: str = "option+command+r"

#: Closed set of canonical modifier names, in canonical sort order.
Modifier = Literal["cmd", "option", "control", "shift"]

# Sort order applied to the canonical modifier list so two semantically
# equivalent inputs produce byte-identical canonical strings.
_MODIFIER_ORDER: tuple[Modifier, ...] = ("cmd", "option", "control", "shift")
_MODIFIER_RANK: dict[Modifier, int] = {m: i for i, m in enumerate(_MODIFIER_ORDER)}

# Alias → canonical modifier. Lower-cased lookup keys.
_MODIFIER_ALIASES: dict[str, Modifier] = {
    "cmd": "cmd",
    "command": "cmd",
    "opt": "option",
    "option": "option",
    "alt": "option",
    "ctrl": "control",
    "control": "control",
    "shift": "shift",
}

# Named-key whitelist per tech spec §2.8. "enter" is NOT a member; only
# "return" — kept narrow to match Carbon's keycode whitelist later.
_NAMED_KEYS: frozenset[str] = frozenset(
    {"space", "tab", "return", "escape", "delete"}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HotkeyParseError(ValueError):
    """Raised when a hotkey string cannot be parsed.

    Subclasses :class:`ValueError` so callers that prefer the broader
    exception type still catch it; we narrow to the typed exception in
    :mod:`record.config` where the fall-back-to-default branch lives.
    """


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedHotkey:
    """Canonicalised result of :func:`parse`.

    ``modifiers`` is a sorted, de-duplicated list in canonical order; ``key``
    is the lower-cased canonical key. Use :meth:`canonical` to render a
    round-trip-safe string.
    """

    modifiers: tuple[Modifier, ...]
    key: str

    def canonical(self) -> str:
        """Return the canonical ``mod+mod+...+key`` string."""
        return "+".join((*self.modifiers, self.key))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.canonical()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _is_valid_key(token: str) -> bool:
    """Return True if ``token`` is a valid non-modifier key after lower-casing."""
    if len(token) == 1:
        # Single letter or digit.
        return ("a" <= token <= "z") or ("0" <= token <= "9")
    if token in _NAMED_KEYS:
        return True
    if token.startswith("f") and token[1:].isdigit():
        # Function keys f1..f20 per tech spec §2.8.
        try:
            n = int(token[1:])
        except ValueError:  # pragma: no cover - isdigit guard
            return False
        return 1 <= n <= 20
    return False


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse(s: str) -> ParsedHotkey:
    """Parse a hotkey string into a canonical :class:`ParsedHotkey`.

    Accepts case-insensitive modifier aliases and a closed key whitelist.
    Duplicate modifiers in the input are deduped silently (e.g. ``"cmd+cmd+r"``
    canonicalises to ``"cmd+r"``); this matches what a user-typed accidental
    repeat should mean.

    Raises :class:`HotkeyParseError` on any of:
        - empty input
        - missing modifier prefix (lone key)
        - unknown modifier alias
        - unknown key
        - more than one non-modifier key
    """
    if not s or not s.strip():
        raise HotkeyParseError("hotkey is empty")

    raw_tokens = [tok.strip() for tok in s.split("+")]
    if any(not tok for tok in raw_tokens):
        raise HotkeyParseError(f"hotkey has empty token: {s!r}")

    tokens = [tok.lower() for tok in raw_tokens]

    # Walk tokens. Every token except the *last* must be a modifier; the last
    # must be either a modifier (→ rejected: no key) or a valid key.
    *prefix, last = tokens

    # First, classify the prefix tokens — they must all be modifiers.
    modifiers: list[Modifier] = []
    for tok in prefix:
        canonical = _MODIFIER_ALIASES.get(tok)
        if canonical is None:
            # Could be a misplaced key (e.g. "r+cmd"). Distinguish for a
            # clearer message.
            if _is_valid_key(tok):
                raise HotkeyParseError(
                    f"hotkey has a key {tok!r} before the final position: {s!r}"
                )
            raise HotkeyParseError(
                f"hotkey has unknown modifier {tok!r}: {s!r}"
            )
        modifiers.append(canonical)

    # Classify the last token.
    last_as_modifier = _MODIFIER_ALIASES.get(last)
    if last_as_modifier is not None:
        # Either user wrote "cmd+option" (no key), or repeated a modifier.
        raise HotkeyParseError(
            f"hotkey is missing a non-modifier key: {s!r}"
        )

    if not _is_valid_key(last):
        raise HotkeyParseError(f"hotkey has unknown key {last!r}: {s!r}")

    if not modifiers:
        raise HotkeyParseError(
            f"hotkey must include at least one modifier: {s!r}"
        )

    # Dedupe while preserving canonical order.
    deduped: list[Modifier] = []
    seen: set[Modifier] = set()
    for m in sorted(modifiers, key=lambda x: _MODIFIER_RANK[x]):
        if m not in seen:
            deduped.append(m)
            seen.add(m)

    return ParsedHotkey(modifiers=tuple(deduped), key=last)


__all__ = [
    "DEFAULT_HOTKEY",
    "HotkeyParseError",
    "Modifier",
    "ParsedHotkey",
    "parse",
]
