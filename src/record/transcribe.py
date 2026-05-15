"""Transcription backends and transcript writers (spec 004 slice 1).

This module is **entirely orchestrator-side** — it runs against an audio
``.wav`` that already exists on disk after a capture finalizes.

It defines:

- :class:`Transcript` / :class:`Segment` — the provider-agnostic in-memory
  result model (tech spec §2.2, §2.6).
- :class:`TranscriptionBackend` — the abstract interface. This is the
  architecture's seam for the Phase 5 on-device backend (whisper.cpp +
  pyannote / sherpa-onnx); a future backend implements the same
  ``async transcribe(audio_path) -> Transcript`` contract and nothing else
  changes.
- :class:`DeepgramBackend` — the v1 implementation. POSTs the WAV to
  Deepgram's pre-recorded endpoint (Nova-3, diarization on, multi-language
  auto-detect) and maps the diarized response into a :class:`Transcript`.
- :class:`TranscriptionError` — raised on any failure (network, non-2xx,
  malformed response); carries a human-readable message for the log line.
- :func:`write_transcript` — writes ``{stem}.json`` / ``{stem}.txt`` /
  ``{stem}.srt`` atomically (temp-then-rename per file).

Privacy: the API key is **never** logged. ``DeepgramBackend`` logs request
metadata (endpoint, byte count, status) only — never the key, never the
transcript text.
"""

from __future__ import annotations

import abc
import json
import os
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .logging_setup import get_logger

_log = get_logger("record.transcribe")

# ---------------------------------------------------------------------------
# Deepgram request constants (tech spec §2.7)
# ---------------------------------------------------------------------------

DEEPGRAM_ENDPOINT: str = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL: str = "nova-3"
_PROVIDER_NAME: str = "deepgram"

# Test-only override for the Deepgram endpoint. Honored by
# :meth:`DeepgramBackend.transcribe` when set to a non-empty value. Lets the
# slice-2 integration test point the daemon's HTTP traffic at a localhost
# stub server (the test runs the daemon as a real subprocess, so module-level
# monkeypatching wouldn't survive the process boundary). NOT documented as
# user-facing config and NOT part of the architecture's env-var surface.
_ENDPOINT_OVERRIDE_ENV: str = "RECORD_DEEPGRAM_ENDPOINT"

# Query params per tech spec §2.7. ``language=multi`` turns on Deepgram's
# multi-language auto-detect; the detected languages come back in the response.
_DEEPGRAM_QUERY_PARAMS: dict[str, str] = {
    "model": DEEPGRAM_MODEL,
    "diarize": "true",
    "language": "multi",
    "smart_format": "true",
    "punctuate": "true",
    "utterances": "true",
}

# httpx timeouts: a short connect bound, but the read can take a long time —
# Deepgram transcribes the whole file before responding to a pre-recorded
# request, and an hour-long call is not unusual. ``read=None`` is unbounded.
_DEEPGRAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TranscriptionError(Exception):
    """Raised on any transcription failure.

    Carries a human-readable message suitable for both an ``orchestrator.log``
    line and (for the ``record transcribe`` CLI) printing to the user. No
    secrets are ever placed in the message.
    """


# ---------------------------------------------------------------------------
# Result model (tech spec §2.2 / §2.6)
# ---------------------------------------------------------------------------


class Segment(BaseModel):
    """One diarized utterance: a contiguous span attributed to one speaker."""

    speaker: str  # "Speaker 1", "Speaker 2", … — generic, user-unidentifiable.
    start: float  # seconds from the start of the recording
    end: float  # seconds from the start of the recording
    text: str


class Transcript(BaseModel):
    """A provider-agnostic, speaker-attributed transcript.

    ``{stem}.json`` is a direct dump of this model — the source of truth. The
    ``.txt`` and ``.srt`` derivatives are rendered from the in-memory instance.
    """

    provider: str
    model: str
    language: list[str] = Field(default_factory=list)  # detected, e.g. ["en", "ru"]
    duration_seconds: float
    segments: list[Segment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Backend interface — the Phase 5 seam
# ---------------------------------------------------------------------------


class TranscriptionBackend(abc.ABC):
    """Abstract transcription backend.

    The single method any backend must implement. A future on-device backend
    (whisper.cpp + diarization) plugs in here with no changes to callers.
    """

    @abc.abstractmethod
    async def transcribe(self, audio_path: Path) -> Transcript:
        """Transcribe the WAV at ``audio_path`` into a :class:`Transcript`.

        Raises :class:`TranscriptionError` on any failure.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Deepgram backend
# ---------------------------------------------------------------------------


class DeepgramBackend(TranscriptionBackend):
    """Deepgram Nova-3 pre-recorded transcription with speaker diarization.

    Parameters
    ----------
    api_key:
        The Deepgram API key. Sent as ``Authorization: Token <key>`` and never
        logged.
    client:
        Optional pre-built :class:`httpx.AsyncClient` — used by tests to inject
        an :class:`httpx.MockTransport`. When ``None``, a client is created per
        :meth:`transcribe` call with the module's timeout policy.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client

    async def transcribe(self, audio_path: Path) -> Transcript:
        try:
            audio_bytes = audio_path.read_bytes()
        except OSError as exc:
            raise TranscriptionError(
                f"could not read audio file {audio_path}: {exc}"
            ) from exc

        headers = {
            "Authorization": f"Token {self._api_key}",
            "Content-Type": "audio/wav",
        }

        # Resolve the endpoint. Production always hits Deepgram; the slice-2
        # integration test sets ``RECORD_DEEPGRAM_ENDPOINT`` to point at a
        # localhost stub server.
        endpoint = os.environ.get(_ENDPOINT_OVERRIDE_ENV) or DEEPGRAM_ENDPOINT

        # Log request metadata only — never the key, never the audio.
        _log.info(
            "transcription_request",
            endpoint=endpoint,
            model=DEEPGRAM_MODEL,
            audio_bytes=len(audio_bytes),
        )

        try:
            if self._client is not None:
                response = await self._client.post(
                    endpoint,
                    params=_DEEPGRAM_QUERY_PARAMS,
                    headers=headers,
                    content=audio_bytes,
                )
            else:
                async with httpx.AsyncClient(timeout=_DEEPGRAM_TIMEOUT) as client:
                    response = await client.post(
                        endpoint,
                        params=_DEEPGRAM_QUERY_PARAMS,
                        headers=headers,
                        content=audio_bytes,
                    )
        except httpx.HTTPError as exc:
            raise TranscriptionError(
                f"network error contacting Deepgram: {exc}"
            ) from exc

        if response.status_code != httpx.codes.OK:
            # 401 (bad key), 4xx, 5xx — all land here. Include a short snippet
            # of the body for the log line; the body is Deepgram's error JSON,
            # never our key.
            detail = response.text.strip()
            if len(detail) > 300:
                detail = detail[:300] + "…"
            raise TranscriptionError(
                f"Deepgram returned HTTP {response.status_code}: {detail}"
            )

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TranscriptionError(
                f"Deepgram response was not valid JSON: {exc}"
            ) from exc

        transcript = _map_deepgram_response(payload)
        _log.info(
            "transcription_complete",
            segments=len(transcript.segments),
            duration_seconds=transcript.duration_seconds,
            languages=transcript.language,
        )
        return transcript


def _map_deepgram_response(payload: Any) -> Transcript:
    """Map a Deepgram pre-recorded JSON response into a :class:`Transcript`.

    Deepgram (with ``utterances=true`` + ``diarize=true``) returns a
    ``results.utterances`` array where each entry has an integer ``speaker``,
    ``start``, ``end`` and ``transcript``. Those integers (0, 1, 2, …) are
    renumbered to ``"Speaker 1"``, ``"Speaker 2"``, … in **first-appearance
    order** so the labels are stable and 1-based regardless of which integer
    Deepgram happened to assign first.

    Detected languages (``language=multi``) and the audio duration come from
    elsewhere in the response; we look in the documented locations and degrade
    gracefully if a field is absent. Any structural surprise that prevents
    building a valid :class:`Transcript` raises :class:`TranscriptionError`.
    """
    if not isinstance(payload, dict):
        raise TranscriptionError(
            "Deepgram response was not a JSON object"
        )

    results = payload.get("results")
    if not isinstance(results, dict):
        raise TranscriptionError(
            "Deepgram response missing 'results' object"
        )

    utterances = results.get("utterances")
    if not isinstance(utterances, list):
        raise TranscriptionError(
            "Deepgram response missing 'results.utterances' — was "
            "utterances=true sent?"
        )

    # Renumber Deepgram speaker integers to "Speaker N" in first-appearance
    # order.
    speaker_labels: dict[int, str] = {}

    def _label_for(raw_speaker: Any) -> str:
        # Deepgram emits an int; tolerate a numeric string defensively.
        try:
            speaker_int = int(raw_speaker)
        except (TypeError, ValueError) as exc:
            raise TranscriptionError(
                f"Deepgram utterance had a non-integer speaker: {raw_speaker!r}"
            ) from exc
        if speaker_int not in speaker_labels:
            speaker_labels[speaker_int] = f"Speaker {len(speaker_labels) + 1}"
        return speaker_labels[speaker_int]

    segments: list[Segment] = []
    for index, utterance in enumerate(utterances):
        if not isinstance(utterance, dict):
            raise TranscriptionError(
                f"Deepgram utterance #{index} was not an object"
            )
        try:
            segment = Segment(
                speaker=_label_for(utterance.get("speaker")),
                start=float(utterance["start"]),
                end=float(utterance["end"]),
                text=str(utterance.get("transcript", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscriptionError(
                f"Deepgram utterance #{index} was malformed: {exc}"
            ) from exc
        segments.append(segment)

    duration_seconds = _extract_duration(payload)
    languages = _extract_languages(results)

    try:
        return Transcript(
            provider=_PROVIDER_NAME,
            model=DEEPGRAM_MODEL,
            language=languages,
            duration_seconds=duration_seconds,
            segments=segments,
        )
    except Exception as exc:  # pydantic ValidationError, etc.
        raise TranscriptionError(
            f"could not build transcript from Deepgram response: {exc}"
        ) from exc


def _extract_duration(payload: dict[str, Any]) -> float:
    """Pull the audio duration (seconds) out of a Deepgram response.

    Lives at ``metadata.duration`` in the documented schema. Falls back to the
    end of the last utterance, then 0.0 — a missing duration must not fail the
    whole transcription.
    """
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        duration = metadata.get("duration")
        if isinstance(duration, (int, float)):
            return float(duration)

    # Fallback: the end timestamp of the last utterance.
    results = payload.get("results")
    if isinstance(results, dict):
        utterances = results.get("utterances")
        if isinstance(utterances, list) and utterances:
            last = utterances[-1]
            if isinstance(last, dict) and isinstance(
                last.get("end"), (int, float)
            ):
                return float(last["end"])
    return 0.0


def _extract_languages(results: dict[str, Any]) -> list[str]:
    """Pull the detected language code(s) out of a Deepgram ``results`` object.

    With ``language=multi`` Deepgram reports detected languages per channel.
    Depending on the response variant this is either
    ``channels[].detected_language`` (a single code) or
    ``channels[].alternatives[].languages`` (a list of codes). We collect every
    code we find across channels, de-duplicated, preserving first-seen order.
    """
    channels = results.get("channels")
    if not isinstance(channels, list):
        return []

    found: list[str] = []

    def _add(code: Any) -> None:
        if isinstance(code, str) and code and code not in found:
            found.append(code)

    for channel in channels:
        if not isinstance(channel, dict):
            continue
        _add(channel.get("detected_language"))
        alternatives = channel.get("alternatives")
        if isinstance(alternatives, list):
            for alt in alternatives:
                if not isinstance(alt, dict):
                    continue
                langs = alt.get("languages")
                if isinstance(langs, list):
                    for code in langs:
                        _add(code)
                _add(alt.get("language"))
    return found


# ---------------------------------------------------------------------------
# Transcript writers (tech spec §2.2 / §2.6)
# ---------------------------------------------------------------------------


def _atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically: temp file then rename.

    A crash mid-write leaves at most a stray ``.tmp`` file next to the target,
    never a half-written ``.json`` / ``.txt`` / ``.srt``. The temp file shares
    the target's directory so the final :func:`os.replace` is a same-filesystem
    rename.
    """
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            pass
        raise


def _format_timestamp_txt(seconds: float) -> str:
    """Render ``seconds`` as ``hh:mm:ss`` for the ``.txt`` derivative."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_timestamp_srt(seconds: float) -> str:
    """Render ``seconds`` as SubRip ``hh:mm:ss,mmm``."""
    if seconds < 0:
        seconds = 0.0
    millis_total = int(round(seconds * 1000))
    hours, remainder = divmod(millis_total, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _render_txt(transcript: Transcript) -> str:
    """Render the human-readable ``.txt`` derivative: one line per utterance."""
    lines = [
        f"[{_format_timestamp_txt(seg.start)}] {seg.speaker}: {seg.text}"
        for seg in transcript.segments
    ]
    # Trailing newline so the file is a well-formed text file.
    return "\n".join(lines) + ("\n" if lines else "")


def _render_srt(transcript: Transcript) -> str:
    """Render the standard SubRip ``.srt`` derivative."""
    blocks: list[str] = []
    for index, seg in enumerate(transcript.segments, start=1):
        start = _format_timestamp_srt(seg.start)
        end = _format_timestamp_srt(seg.end)
        blocks.append(
            f"{index}\n{start} --> {end}\n{seg.speaker}: {seg.text}\n"
        )
    return "\n".join(blocks)


def write_transcript(transcript: Transcript, stem_path: Path) -> list[Path]:
    """Write the three transcript files next to ``stem_path``.

    ``stem_path`` is the recording's stem path (e.g. the ``.wav`` path with the
    suffix stripped, or any path whose ``.with_suffix`` gives the target).
    Produces ``{stem}.json`` (full :class:`Transcript` dump — source of truth),
    ``{stem}.txt`` (readable) and ``{stem}.srt`` (SubRip). Each file is written
    temp-then-rename so a crash never leaves a half-written file. Returns the
    three written paths in ``[json, txt, srt]`` order.
    """
    json_path = stem_path.with_suffix(".json")
    txt_path = stem_path.with_suffix(".txt")
    srt_path = stem_path.with_suffix(".srt")

    json_content = json.dumps(
        transcript.model_dump(), ensure_ascii=False, indent=2
    )
    _atomic_write_text(json_path, json_content)
    _atomic_write_text(txt_path, _render_txt(transcript))
    _atomic_write_text(srt_path, _render_srt(transcript))

    return [json_path, txt_path, srt_path]


__all__ = [
    "DEEPGRAM_ENDPOINT",
    "DEEPGRAM_MODEL",
    "DeepgramBackend",
    "Segment",
    "Transcript",
    "TranscriptionBackend",
    "TranscriptionError",
    "write_transcript",
]
