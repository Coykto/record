"""Audible feedback + macOS notification banner surface.

Spec 003 / tech spec §2.9. The daemon plays a short macOS built-in sound at
each capture-state transition (Submarine on start, Pop on stop, Funk on failure)
and surfaces a notification banner on error conditions the user must see —
notably "hotkey pressed but capture cannot start" (FR 2.8) and daemon-level
warnings like missing Accessibility permission.

Why this lives in its own module
--------------------------------

Putting the ``afplay`` and ``osascript`` shell-outs behind named functions
keeps the daemon's state-machine code readable (``feedback.play_start()`` reads
as the intent, not the implementation) and gives the tests a single seam:
patch ``subprocess.Popen`` here and the daemon's wiring assertions stay
independent of the wire format of ``afplay``/``osascript`` argv.

Why fire-and-forget
-------------------

``afplay`` runs for the length of the sound (well under a second) and
``osascript`` for the lifetime of the notification banner. The daemon runs on
an asyncio loop and must not block waiting for either — even
:meth:`subprocess.run` with a timeout would freeze the loop while it waits.
We :class:`subprocess.Popen` and return immediately. The OS reaps the child
when it exits; we never read its output.

Why ``audible_feedback`` is a sound-only knob
---------------------------------------------

FR 2.9: "When audible feedback is off, error situations still surface to the
user via a macOS notification banner naming the specific problem. The banner
is unaffected by the audible-feedback setting." :func:`notify` therefore takes
no ``enabled`` flag — banners always fire.
"""

from __future__ import annotations

import subprocess

from .logging_setup import get_logger

_log = get_logger("record.feedback")


# ---------------------------------------------------------------------------
# Sound file constants
# ---------------------------------------------------------------------------

#: Played on a successful capture start (FR 2.8 first bullet).
START_SOUND: str = "/System/Library/Sounds/Submarine.aiff"

#: Played on a successful capture stop (FR 2.8 second bullet).
STOP_SOUND: str = "/System/Library/Sounds/Pop.aiff"

#: Played when the hotkey is pressed but a capture cannot start (FR 2.8 third
#: bullet) — clearly distinct from the start/stop sounds by ear.
ERROR_SOUND: str = "/System/Library/Sounds/Funk.aiff"


# ---------------------------------------------------------------------------
# Sound playback
# ---------------------------------------------------------------------------


def _play(path: str) -> None:
    """Fire-and-forget ``afplay`` against ``path``.

    Catches :class:`OSError` defensively — if ``/usr/bin/afplay`` is missing
    (extremely unlikely on a stock macOS install but plausible in a stripped
    test container) we log at WARNING and return rather than propagate.
    """
    try:
        subprocess.Popen(  # noqa: S603 - argv is a fixed list, no shell
            ["/usr/bin/afplay", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        _log.warning("feedback_play_failed", path=path, error=str(exc))


def play_start(*, enabled: bool = True) -> None:
    """Play the capture-start sound (Submarine). No-op when ``enabled=False``."""
    if not enabled:
        return
    _play(START_SOUND)


def play_stop(*, enabled: bool = True) -> None:
    """Play the capture-stop sound (Pop). No-op when ``enabled=False``."""
    if not enabled:
        return
    _play(STOP_SOUND)


def play_error(*, enabled: bool = True) -> None:
    """Play the capture-error sound (Funk). No-op when ``enabled=False``."""
    if not enabled:
        return
    _play(ERROR_SOUND)


# ---------------------------------------------------------------------------
# Notification banner
# ---------------------------------------------------------------------------


def _escape_applescript_string(value: str) -> str:
    """Escape a Python string for embedding inside an AppleScript ``"..."``.

    AppleScript uses C-style escapes for ``\\`` and ``"`` inside double-quoted
    string literals. Order matters: the backslash substitution must precede the
    quote substitution, or the inserted ``\\"`` from the second pass would be
    re-escaped into ``\\\\"``.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def notify(message: str, *, title: str = "record") -> None:
    """Display a macOS notification banner via ``osascript``.

    Always fires regardless of any audible-feedback setting (FR 2.9). The
    osascript invocation is fire-and-forget — the tech spec §2.9 mentions a
    2-second timeout, but that bound only matters when waiting; since we
    Popen and return immediately, a hung osascript can't stall the daemon.

    Catches :class:`OSError` defensively for the same reason as :func:`_play`.
    """
    escaped_message = _escape_applescript_string(message)
    escaped_title = _escape_applescript_string(title)
    script = (
        f'display notification "{escaped_message}" with title "{escaped_title}"'
    )
    try:
        subprocess.Popen(  # noqa: S603 - argv is a fixed list, no shell
            ["/usr/bin/osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        _log.warning("feedback_notify_failed", error=str(exc))


__all__ = [
    "START_SOUND",
    "STOP_SOUND",
    "ERROR_SOUND",
    "play_start",
    "play_stop",
    "play_error",
    "notify",
]
