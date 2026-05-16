"""Mix mic + system WAVs into a single mono int16 16 kHz WAV.

Pure stdlib mixer used by the transcription pipeline. This module is the
isolated, well-tested core of spec 007: it holds the mix math and the
atomic-write semantics and nothing else. Daemon / CLI wiring lives in
later slices.
"""

from __future__ import annotations

import array
import os
import wave
from dataclasses import dataclass
from pathlib import Path

# Format the Swift capture backend guarantees on disk
# (see swift-capture/Sources/RecordCapture/WAVWriter.swift): mono / int16 LE
# / 16 kHz. Validation against these constants is cheap because the producer
# is in-tree.
_EXPECTED_CHANNELS = 1
_EXPECTED_SAMPWIDTH = 2  # bytes -> int16
_EXPECTED_FRAMERATE = 16_000
_CHUNK_FRAMES = 16_000  # one second of audio


@dataclass(frozen=True)
class CombineResult:
    """Outcome of a successful combine."""

    path: Path
    duration_seconds: float


class CombineError(RuntimeError):
    """Raised when combining mic + system WAVs fails.

    ``str(exc)`` is a short plain-language reason suitable for the
    stop-summary message shown to the user.
    """


def _validate(wav: wave.Wave_read, side: str) -> None:
    if (
        wav.getnchannels() != _EXPECTED_CHANNELS
        or wav.getsampwidth() != _EXPECTED_SAMPWIDTH
        or wav.getframerate() != _EXPECTED_FRAMERATE
    ):
        raise CombineError(f"unexpected audio format in {side}")


def _open_validated(path: Path, side: str) -> wave.Wave_read:
    try:
        wav = wave.open(str(path), "rb")
    except (OSError, wave.Error) as exc:
        raise CombineError(f"cannot read {side} audio: {exc}") from exc
    try:
        _validate(wav, side)
    except Exception:
        wav.close()
        raise
    return wav


def combine_wavs(
    mic_path: Path, system_path: Path, output_path: Path
) -> CombineResult:
    """Combine two mono int16 16 kHz WAVs into one, written atomically.

    Returns a :class:`CombineResult` whose ``duration_seconds`` is the
    longer source's duration. Raises :class:`CombineError` on any failure;
    on a mid-write failure the destination and the ``.tmp`` sidecar are
    both removed before the exception propagates.
    """
    # Validate mic first, then system. The error message identifies which
    # side failed so the user-facing summary is actionable.
    mic = _open_validated(mic_path, "mic")
    try:
        system = _open_validated(system_path, "system")
    except Exception:
        mic.close()
        raise

    mic_frames = mic.getnframes()
    system_frames = system.getnframes()
    longer_frames = max(mic_frames, system_frames)

    tmp_path = output_path.with_name(output_path.name + ".tmp")
    out: wave.Wave_write | None = None
    try:
        out = wave.open(str(tmp_path), "wb")
        out.setnchannels(_EXPECTED_CHANNELS)
        out.setsampwidth(_EXPECTED_SAMPWIDTH)
        out.setframerate(_EXPECTED_FRAMERATE)

        mic_done = mic_frames == 0
        system_done = system_frames == 0
        zero_chunk = b"\x00\x00" * _CHUNK_FRAMES

        while not (mic_done and system_done):
            if mic_done:
                mic_block = b""
            else:
                mic_block = mic.readframes(_CHUNK_FRAMES)
                if len(mic_block) < _CHUNK_FRAMES * _EXPECTED_SAMPWIDTH:
                    mic_done = True
            if system_done:
                system_block = b""
            else:
                system_block = system.readframes(_CHUNK_FRAMES)
                if len(system_block) < _CHUNK_FRAMES * _EXPECTED_SAMPWIDTH:
                    system_done = True

            if not mic_block and not system_block:
                break

            # Decide chunk length: full _CHUNK_FRAMES unless both sides are
            # exhausted on this iteration, in which case we shrink to the
            # longer remaining partial block so we don't pad past EOF.
            mic_samples_len = len(mic_block) // _EXPECTED_SAMPWIDTH
            system_samples_len = len(system_block) // _EXPECTED_SAMPWIDTH
            if mic_done and system_done:
                chunk_len = max(mic_samples_len, system_samples_len)
            else:
                chunk_len = _CHUNK_FRAMES

            if chunk_len == 0:
                break

            mic_arr = array.array("h", mic_block)
            system_arr = array.array("h", system_block)
            # Zero-pad the shorter side up to chunk_len so element-wise sum
            # is well-defined. When one source has hit EOF, its array is
            # empty and we effectively sum zeros for the rest of the file.
            if len(mic_arr) < chunk_len:
                mic_arr.extend([0] * (chunk_len - len(mic_arr)))
            if len(system_arr) < chunk_len:
                system_arr.extend([0] * (chunk_len - len(system_arr)))

            # Saturate at int16 boundaries. Per spec 007: equal-levels mix
            # with no attenuation, so clipping when both sides happen to
            # peak simultaneously is the accepted behavior. Wraparound
            # would produce harsh artifacts on loud passages.
            summed = array.array("h", [0] * chunk_len)
            for i in range(chunk_len):
                s = mic_arr[i] + system_arr[i]
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                summed[i] = s

            out.writeframesraw(summed.tobytes())

        out.close()
        out = None
        os.replace(tmp_path, output_path)
    except CombineError:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise
    except Exception as exc:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise CombineError(f"failed to write combined audio: {exc}") from exc
    finally:
        mic.close()
        system.close()

    return CombineResult(
        path=output_path,
        duration_seconds=longer_frames / float(_EXPECTED_FRAMERATE),
    )


__all__ = ["CombineResult", "CombineError", "combine_wavs"]
