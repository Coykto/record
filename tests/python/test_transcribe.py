"""Tests for :mod:`record.transcribe` — DeepgramBackend + transcript writers.

No real network and no real API key: every Deepgram interaction goes through
an :class:`httpx.MockTransport` returning a canned response. The recorded
Deepgram JSON fixture below exercises the response parser, speaker
renumbering, and detected-language passthrough.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from record import transcribe
from record.transcribe import (
    DeepgramBackend,
    Segment,
    Transcript,
    TranscriptionError,
    write_transcript,
)


# ---------------------------------------------------------------------------
# Recorded Deepgram pre-recorded response fixture
# ---------------------------------------------------------------------------
#
# Shaped after a real `diarize=true` + `utterances=true` + `language=multi`
# response. The speaker integers deliberately appear in the order 1, 0, 1 so
# the "first-appearance" renumbering is observable: Deepgram speaker 1 must
# become "Speaker 1" (seen first) and Deepgram speaker 0 must become
# "Speaker 2".
_DEEPGRAM_FIXTURE: dict = {
    "metadata": {
        "duration": 12.34,
        "channels": 1,
        "models": ["nova-3"],
    },
    "results": {
        "channels": [
            {
                "detected_language": "en",
                "alternatives": [
                    {
                        "transcript": "hello there how are you doing today",
                        "languages": ["en", "ru"],
                    }
                ],
            }
        ],
        "utterances": [
            {
                "speaker": 1,
                "start": 0.0,
                "end": 2.5,
                "transcript": "hello there",
            },
            {
                "speaker": 0,
                "start": 2.6,
                "end": 6.1,
                "transcript": "how are you doing today",
            },
            {
                "speaker": 1,
                "start": 6.2,
                "end": 12.34,
                "transcript": "doing great thanks",
            },
        ],
    },
}


def _make_backend(
    handler,
    api_key: str = "test-key",
) -> DeepgramBackend:
    """Build a DeepgramBackend whose AsyncClient uses a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return DeepgramBackend(api_key, client=client)


def _run_transcribe(backend: DeepgramBackend, wav_path: Path) -> Transcript:
    """Drive ``backend.transcribe`` to completion.

    The project does not depend on ``pytest-asyncio``; like the rest of the
    suite (see ``test_daemon.py``), async work is driven through
    :func:`asyncio.run`.
    """
    return asyncio.run(backend.transcribe(wav_path))


@pytest.fixture
def wav_file(tmp_path: Path) -> Path:
    """A throwaway ``.wav`` on disk — its bytes are never actually parsed."""
    path = tmp_path / "2026-05-14T10-00-00.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


# ---------------------------------------------------------------------------
# Response parsing + speaker renumbering + language passthrough
# ---------------------------------------------------------------------------


def test_transcribe_parses_fixture_and_renumbers_speakers(
    wav_file: Path,
) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = request.content
        return httpx.Response(200, json=_DEEPGRAM_FIXTURE)

    backend = _make_backend(_handler)

    async def _drive() -> Transcript:
        return await backend.transcribe(wav_file)

    transcript = asyncio.run(_drive())

    # Request shape: auth header, content type, query params, raw body.
    assert captured["auth"] == "Token test-key"
    assert captured["content_type"] == "audio/wav"
    assert captured["body"] == wav_file.read_bytes()
    assert "model=nova-3" in captured["url"]
    assert "diarize=true" in captured["url"]
    assert "language=multi" in captured["url"]
    assert "utterances=true" in captured["url"]

    # Provider / model.
    assert transcript.provider == "deepgram"
    assert transcript.model == "nova-3"
    assert transcript.duration_seconds == 12.34

    # Detected languages passed through.
    assert transcript.language == ["en", "ru"]

    # Speaker renumbering: Deepgram 1 -> "Speaker 1" (first seen),
    # Deepgram 0 -> "Speaker 2".
    assert [s.speaker for s in transcript.segments] == [
        "Speaker 1",
        "Speaker 2",
        "Speaker 1",
    ]
    assert transcript.segments[0].start == 0.0
    assert transcript.segments[0].end == 2.5
    assert transcript.segments[0].text == "hello there"
    assert transcript.segments[2].text == "doing great thanks"


def test_transcribe_duration_falls_back_to_last_utterance(
    wav_file: Path,
) -> None:
    payload = json.loads(json.dumps(_DEEPGRAM_FIXTURE))
    del payload["metadata"]["duration"]

    backend = _make_backend(lambda req: httpx.Response(200, json=payload))
    transcript = _run_transcribe(backend, wav_file)
    # Last utterance ends at 12.34.
    assert transcript.duration_seconds == 12.34


# ---------------------------------------------------------------------------
# Error paths -> TranscriptionError
# ---------------------------------------------------------------------------


def test_non_2xx_raises_transcription_error(wav_file: Path) -> None:
    backend = _make_backend(
        lambda req: httpx.Response(401, json={"err_code": "INVALID_AUTH"})
    )

    async def _drive() -> None:
        await backend.transcribe(wav_file)

    with pytest.raises(TranscriptionError) as exc:
        asyncio.run(_drive())
    assert "401" in str(exc.value)


def test_malformed_body_raises_transcription_error(wav_file: Path) -> None:
    backend = _make_backend(
        lambda req: httpx.Response(200, text="this is not json")
    )

    async def _drive() -> None:
        await backend.transcribe(wav_file)

    with pytest.raises(TranscriptionError):
        asyncio.run(_drive())


def test_missing_utterances_raises_transcription_error(
    wav_file: Path,
) -> None:
    backend = _make_backend(
        lambda req: httpx.Response(200, json={"results": {"channels": []}})
    )

    async def _drive() -> None:
        await backend.transcribe(wav_file)

    with pytest.raises(TranscriptionError) as exc:
        asyncio.run(_drive())
    assert "utterances" in str(exc.value)


def test_network_error_raises_transcription_error(wav_file: Path) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    backend = _make_backend(_handler)

    async def _drive() -> None:
        await backend.transcribe(wav_file)

    with pytest.raises(TranscriptionError) as exc:
        asyncio.run(_drive())
    assert "network" in str(exc.value).lower()


def test_missing_audio_file_raises_transcription_error(
    tmp_path: Path,
) -> None:
    backend = _make_backend(lambda req: httpx.Response(200, json=_DEEPGRAM_FIXTURE))

    async def _drive() -> None:
        await backend.transcribe(tmp_path / "does-not-exist.wav")

    with pytest.raises(TranscriptionError):
        asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Writers — content + atomic write
# ---------------------------------------------------------------------------


def _sample_transcript() -> Transcript:
    return Transcript(
        provider="deepgram",
        model="nova-3",
        language=["en", "ru"],
        duration_seconds=12.34,
        segments=[
            Segment(speaker="Speaker 1", start=0.0, end=2.5, text="hello there"),
            Segment(
                speaker="Speaker 2",
                start=2.6,
                end=6.1,
                text="how are you doing today",
            ),
            Segment(
                speaker="Speaker 1",
                start=66.2,
                end=72.34,
                text="doing great thanks",
            ),
        ],
    )


def test_write_transcript_produces_three_files(tmp_path: Path) -> None:
    stem = tmp_path / "2026-05-14T10-00-00.wav"
    written = write_transcript(_sample_transcript(), stem)

    json_path = tmp_path / "2026-05-14T10-00-00.json"
    txt_path = tmp_path / "2026-05-14T10-00-00.txt"
    srt_path = tmp_path / "2026-05-14T10-00-00.srt"
    assert written == [json_path, txt_path, srt_path]
    assert json_path.is_file()
    assert txt_path.is_file()
    assert srt_path.is_file()


def test_write_transcript_json_is_source_of_truth(tmp_path: Path) -> None:
    stem = tmp_path / "rec.wav"
    write_transcript(_sample_transcript(), stem)
    data = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
    assert data["provider"] == "deepgram"
    assert data["model"] == "nova-3"
    assert data["language"] == ["en", "ru"]
    assert data["duration_seconds"] == 12.34
    assert len(data["segments"]) == 3
    assert data["segments"][0] == {
        "speaker": "Speaker 1",
        "start": 0.0,
        "end": 2.5,
        "text": "hello there",
    }


def test_write_transcript_txt_format(tmp_path: Path) -> None:
    stem = tmp_path / "rec.wav"
    write_transcript(_sample_transcript(), stem)
    lines = (tmp_path / "rec.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[00:00:00] Speaker 1: hello there"
    assert lines[1] == "[00:00:02] Speaker 2: how are you doing today"
    # 66.2s -> 00:01:06.
    assert lines[2] == "[00:01:06] Speaker 1: doing great thanks"


def test_write_transcript_srt_format(tmp_path: Path) -> None:
    stem = tmp_path / "rec.wav"
    write_transcript(_sample_transcript(), stem)
    content = (tmp_path / "rec.srt").read_text(encoding="utf-8")
    blocks = content.strip().split("\n\n")
    assert len(blocks) == 3
    first = blocks[0].splitlines()
    assert first[0] == "1"
    assert first[1] == "00:00:00,000 --> 00:00:02,500"
    assert first[2] == "Speaker 1: hello there"
    third = blocks[2].splitlines()
    assert third[0] == "3"
    assert third[1] == "00:01:06,200 --> 00:01:12,340"


def test_write_transcript_leaves_no_temp_file(tmp_path: Path) -> None:
    stem = tmp_path / "rec.wav"
    write_transcript(_sample_transcript(), stem)
    leftover = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


def test_write_transcript_overwrites_existing(tmp_path: Path) -> None:
    stem = tmp_path / "rec.wav"
    (tmp_path / "rec.json").write_text("STALE", encoding="utf-8")
    (tmp_path / "rec.txt").write_text("STALE", encoding="utf-8")
    (tmp_path / "rec.srt").write_text("STALE", encoding="utf-8")
    write_transcript(_sample_transcript(), stem)
    assert "STALE" not in (tmp_path / "rec.json").read_text(encoding="utf-8")
    assert "STALE" not in (tmp_path / "rec.txt").read_text(encoding="utf-8")
    assert "STALE" not in (tmp_path / "rec.srt").read_text(encoding="utf-8")


def test_atomic_write_cleans_up_temp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rename fails, the temp file is removed — no stray .tmp."""
    target = tmp_path / "rec.json"

    def _boom(src, dst) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(transcribe.os, "replace", _boom)
    with pytest.raises(OSError):
        transcribe._atomic_write_text(target, "content")
    leftover = [p.name for p in tmp_path.iterdir()]
    assert leftover == []
