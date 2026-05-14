"""Tests for the audible feedback + notification banner surface (spec 003 slice 6).

We stub :func:`subprocess.Popen` to record the argv each call site sends and
assert the wire shape. The real ``afplay`` / ``osascript`` binaries are never
invoked here — those land in the manual smoke matrix.
"""

from __future__ import annotations

from typing import Any

import pytest

from record import feedback


class _PopenRecorder:
    """Records every ``Popen`` invocation and the argv it received.

    Doubles as the return value (``Popen`` is callable in production; tests
    don't ``wait()`` on it). The ``raise_on_call`` knob covers the
    ``/usr/bin/afplay`` missing branch.
    """

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self._raise = raise_on_call

    def __call__(self, argv: list[str], **kwargs: Any) -> "_PopenRecorder":
        if self._raise:
            raise OSError(2, "No such file or directory")
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        return self


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _PopenRecorder:
    rec = _PopenRecorder()
    monkeypatch.setattr(feedback.subprocess, "Popen", rec)
    return rec


# ---------------------------------------------------------------------------
# Sound playback
# ---------------------------------------------------------------------------


def test_play_start_invokes_afplay_with_tink(recorder: _PopenRecorder) -> None:
    feedback.play_start()
    assert recorder.calls == [["/usr/bin/afplay", "/System/Library/Sounds/Tink.aiff"]]


def test_play_stop_invokes_afplay_with_pop(recorder: _PopenRecorder) -> None:
    feedback.play_stop()
    assert recorder.calls == [["/usr/bin/afplay", "/System/Library/Sounds/Pop.aiff"]]


def test_play_error_invokes_afplay_with_funk(recorder: _PopenRecorder) -> None:
    feedback.play_error()
    assert recorder.calls == [["/usr/bin/afplay", "/System/Library/Sounds/Funk.aiff"]]


def test_play_start_disabled_does_not_invoke_popen(
    recorder: _PopenRecorder,
) -> None:
    feedback.play_start(enabled=False)
    assert recorder.calls == []


def test_play_stop_disabled_does_not_invoke_popen(
    recorder: _PopenRecorder,
) -> None:
    feedback.play_stop(enabled=False)
    assert recorder.calls == []


def test_play_error_disabled_does_not_invoke_popen(
    recorder: _PopenRecorder,
) -> None:
    feedback.play_error(enabled=False)
    assert recorder.calls == []


def test_play_routes_stdio_to_devnull(recorder: _PopenRecorder) -> None:
    """``afplay`` is fire-and-forget; we must not leak its FDs into the daemon."""
    import subprocess as real_subprocess

    feedback.play_start()
    kwargs = recorder.kwargs[0]
    assert kwargs["stdin"] == real_subprocess.DEVNULL
    assert kwargs["stdout"] == real_subprocess.DEVNULL
    assert kwargs["stderr"] == real_subprocess.DEVNULL


# ---------------------------------------------------------------------------
# Notification banner
# ---------------------------------------------------------------------------


def test_notify_invokes_osascript_with_display_notification(
    recorder: _PopenRecorder,
) -> None:
    feedback.notify("hello")

    assert len(recorder.calls) == 1
    argv = recorder.calls[0]
    assert argv[0] == "/usr/bin/osascript"
    assert argv[1] == "-e"
    script = argv[2]
    assert 'display notification "hello"' in script
    assert 'with title "record"' in script


def test_notify_respects_custom_title(recorder: _PopenRecorder) -> None:
    feedback.notify("hello", title="custom-title")
    script = recorder.calls[0][2]
    assert 'with title "custom-title"' in script


def test_notify_escapes_double_quotes_in_message(
    recorder: _PopenRecorder,
) -> None:
    """Unescaped ``"`` would break out of the AppleScript string literal."""
    feedback.notify('he said "hi"')
    script = recorder.calls[0][2]
    # The raw double-quote inside the message must appear as ``\"`` in the
    # final AppleScript expression.
    assert 'display notification "he said \\"hi\\""' in script


def test_notify_escapes_backslashes_in_message(
    recorder: _PopenRecorder,
) -> None:
    """A literal ``\\`` must become ``\\\\`` so AppleScript doesn't consume it."""
    feedback.notify("path: C:\\Users\\eb")
    script = recorder.calls[0][2]
    # In Python source, ``"C:\\\\Users\\\\eb"`` is the four-character sequence
    # ``\\Users\\eb`` — what AppleScript needs to render ``\Users\eb``.
    assert 'display notification "path: C:\\\\Users\\\\eb"' in script


def test_notify_escapes_quote_inside_title(recorder: _PopenRecorder) -> None:
    feedback.notify("body", title='ti"tle')
    script = recorder.calls[0][2]
    assert 'with title "ti\\"tle"' in script


def test_notify_does_not_accept_enabled_kwarg() -> None:
    """The banner is unconditional per FR 2.9 — no ``enabled`` parameter."""
    import inspect

    sig = inspect.signature(feedback.notify)
    assert "enabled" not in sig.parameters


# ---------------------------------------------------------------------------
# Defensive Popen-failure path
# ---------------------------------------------------------------------------


def test_play_swallows_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/usr/bin/afplay`` missing must not propagate out of ``play_start``."""
    raising = _PopenRecorder(raise_on_call=True)
    monkeypatch.setattr(feedback.subprocess, "Popen", raising)

    # Each entry point must swallow the OSError.
    feedback.play_start()
    feedback.play_stop()
    feedback.play_error()


def test_notify_swallows_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``osascript`` must not propagate out of ``notify``."""
    raising = _PopenRecorder(raise_on_call=True)
    monkeypatch.setattr(feedback.subprocess, "Popen", raising)

    feedback.notify("anything")


# ---------------------------------------------------------------------------
# Constant exposure
# ---------------------------------------------------------------------------


def test_module_exports_sound_constants() -> None:
    """The three sound paths are public — tests + daemon both reference them."""
    assert feedback.START_SOUND == "/System/Library/Sounds/Tink.aiff"
    assert feedback.STOP_SOUND == "/System/Library/Sounds/Pop.aiff"
    assert feedback.ERROR_SOUND == "/System/Library/Sounds/Funk.aiff"
    assert "START_SOUND" in feedback.__all__
    assert "STOP_SOUND" in feedback.__all__
    assert "ERROR_SOUND" in feedback.__all__
