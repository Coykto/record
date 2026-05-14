"""Tests for :mod:`record.hotkey` — the FR 2.10 hotkey-string parser."""

from __future__ import annotations

import pytest

from record.hotkey import DEFAULT_HOTKEY, HotkeyParseError, parse


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_default_hotkey_round_trips() -> None:
    parsed = parse(DEFAULT_HOTKEY)
    assert parsed.modifiers == ("cmd", "option")
    assert parsed.key == "r"
    assert parsed.canonical() == "cmd+option+r"


def test_case_insensitive_modifiers_and_key() -> None:
    parsed = parse("OPTION+COMMAND+R")
    assert parsed.canonical() == "cmd+option+r"


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("cmd", "cmd"),
        ("command", "cmd"),
        ("opt", "option"),
        ("option", "option"),
        ("alt", "option"),
        ("ctrl", "control"),
        ("control", "control"),
        ("shift", "shift"),
    ],
)
def test_modifier_aliases(alias: str, expected: str) -> None:
    parsed = parse(f"{alias}+r")
    assert expected in parsed.modifiers


def test_modifier_order_does_not_affect_canonical_output() -> None:
    a = parse("shift+cmd+option+r").canonical()
    b = parse("option+cmd+shift+r").canonical()
    c = parse("cmd+shift+option+r").canonical()
    assert a == b == c


def test_duplicate_modifiers_are_deduped() -> None:
    # Document choice: duplicates are silently deduped, NOT rejected.
    parsed = parse("cmd+cmd+r")
    assert parsed.modifiers == ("cmd",)
    assert parsed.canonical() == "cmd+r"


# ---------------------------------------------------------------------------
# Key whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["a", "z", "0", "9"])
def test_letter_and_digit_keys_accepted(key: str) -> None:
    parsed = parse(f"cmd+{key}")
    assert parsed.key == key


@pytest.mark.parametrize("n", [1, 5, 10, 20])
def test_function_keys_f1_through_f20_accepted(n: int) -> None:
    parsed = parse(f"cmd+f{n}")
    assert parsed.key == f"f{n}"


@pytest.mark.parametrize("bad", ["f0", "f21", "f99"])
def test_function_keys_outside_range_rejected(bad: str) -> None:
    with pytest.raises(HotkeyParseError):
        parse(f"cmd+{bad}")


@pytest.mark.parametrize("name", ["space", "tab", "return", "escape", "delete"])
def test_named_keys_accepted(name: str) -> None:
    parsed = parse(f"cmd+{name}")
    assert parsed.key == name


def test_enter_is_not_in_whitelist() -> None:
    # Spec says only "return" is whitelisted, not "enter".
    with pytest.raises(HotkeyParseError):
        parse("cmd+enter")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_empty_string_raises() -> None:
    with pytest.raises(HotkeyParseError):
        parse("")


def test_whitespace_only_raises() -> None:
    with pytest.raises(HotkeyParseError):
        parse("   ")


def test_lone_key_without_modifier_raises() -> None:
    with pytest.raises(HotkeyParseError):
        parse("r")


def test_unknown_modifier_raises() -> None:
    with pytest.raises(HotkeyParseError):
        parse("hyper+r")


@pytest.mark.parametrize("bad", ["cmd+`", "cmd+!", "cmd+notakey", "cmd+@"])
def test_unknown_key_raises(bad: str) -> None:
    with pytest.raises(HotkeyParseError):
        parse(bad)


def test_multiple_non_modifier_keys_rejected() -> None:
    # "r" appears mid-token; the parser must reject this.
    with pytest.raises(HotkeyParseError):
        parse("cmd+r+t")


def test_empty_token_rejected() -> None:
    with pytest.raises(HotkeyParseError):
        parse("cmd++r")


def test_only_modifiers_no_key_rejected() -> None:
    with pytest.raises(HotkeyParseError):
        parse("cmd+option")
