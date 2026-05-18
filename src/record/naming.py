"""Post-transcription session-folder renaming (spec 008).

Runs at the tail of the detached transcription task: inspects the transcript
and, on success, renames the session folder in place to carry a meaningful
suffix. Every failure is caught, logged, and swallowed — a problem in the
naming chain never costs the user their recording (functional spec §2.6).
"""

from __future__ import annotations

import asyncio
import os
import re
from asyncio.subprocess import PIPE
from pathlib import Path

from .logging_setup import get_logger
from .transcribe import Transcript

_log = get_logger("record.naming")

SILENT_SUFFIX = "silent"

# --- Description-generation constants (tech-considerations §2.4.2) ----------

MODEL = "claude-haiku-4-5"
TIMEOUT_S = 30
MAX_TRANSCRIPT_CHARS = 32_000
MAX_DESCRIPTION_CHARS = 60
DESCRIPTION_REGEX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,5}$")

PROMPT = (
    "Read the meeting transcript on stdin. Output one short English "
    "description of what the meeting was about, suitable as a filename "
    "suffix: lowercase, 3–6 words separated by single hyphens, no "
    "punctuation, no quotes, no trailing newline, maximum 60 characters "
    "total. Output only the description and nothing else."
)


def is_silent(transcript: Transcript) -> bool:
    """True iff every segment's text is empty/whitespace after ``.strip()``."""
    return all(not segment.text.strip() for segment in transcript.segments)


def atomic_rename(session_dir: Path, suffix: str) -> Path:
    """Rename ``session_dir`` in place to ``<name>-<suffix>``.

    Raises ``FileExistsError`` if the target path already exists; ``OSError``
    if the source is missing or ``os.rename`` fails. Returns the new path on
    success.
    """
    target = session_dir.with_name(f"{session_dir.name}-{suffix}")
    if target.exists():
        raise FileExistsError(f"target already exists: {target}")
    os.rename(session_dir, target)
    return target


async def generate_description(transcript_text: str) -> str:
    """Run ``claude -p`` to produce a short description for ``transcript_text``.

    The transcript is truncated to :data:`MAX_TRANSCRIPT_CHARS` before being
    piped to the CLI on stdin. Raises ``RuntimeError`` on non-zero exit or
    empty stdout, ``asyncio.TimeoutError`` if the call exceeds
    :data:`TIMEOUT_S`, and propagates ``FileNotFoundError`` from
    ``create_subprocess_exec`` if ``claude`` is not on ``PATH``.
    """
    text = transcript_text[:MAX_TRANSCRIPT_CHARS]
    proc = await asyncio.create_subprocess_exec(
        "claude",
        "-p",
        "--model",
        MODEL,
        PROMPT,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=text.encode("utf-8")),
            timeout=TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", errors="replace"))

    decoded = stdout.decode("utf-8", errors="replace").strip()
    if not decoded:
        raise RuntimeError("empty stdout from claude -p")
    return decoded


def validate_description(raw: str) -> str:
    """Strip one trailing newline and enforce the description regex.

    Raises ``ValueError`` if ``raw`` does not match
    :data:`DESCRIPTION_REGEX` or exceeds :data:`MAX_DESCRIPTION_CHARS`.
    Returns the cleaned string on success.
    """
    cleaned = raw.rstrip("\n")
    if len(cleaned) > MAX_DESCRIPTION_CHARS or not DESCRIPTION_REGEX.fullmatch(
        cleaned
    ):
        raise ValueError(f"invalid description: {cleaned!r}")
    return cleaned


async def try_rename_session_folder(
    session_dir: Path, transcript: Transcript
) -> None:
    """Rename ``session_dir`` based on ``transcript`` content.

    Silent transcripts get the ``-silent`` suffix; non-silent transcripts go
    through ``claude -p`` to produce a kebab-case English description that is
    appended as the suffix. Every exception is caught, logged, and swallowed.
    """
    attempted_suffix: str | None = None
    try:
        if is_silent(transcript):
            attempted_suffix = SILENT_SUFFIX
            new_dir = atomic_rename(session_dir, attempted_suffix)
        else:
            transcript_text = (session_dir / "transcript.txt").read_text(
                encoding="utf-8"
            )
            transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]
            raw = await generate_description(transcript_text)
            description = validate_description(raw)
            attempted_suffix = description
            new_dir = atomic_rename(session_dir, attempted_suffix)

        _log.info(
            "session_renamed",
            session_dir=str(session_dir),
            new_dir=str(new_dir),
            suffix=attempted_suffix,
        )
    except Exception as exc:
        _log.warning(
            "session_rename_failed",
            session_dir=str(session_dir),
            attempted_suffix=attempted_suffix,
            reason=str(exc),
            error_type=type(exc).__name__,
        )
