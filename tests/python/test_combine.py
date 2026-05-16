"""Unit tests for :mod:`record.combine` (spec 007 slice 1).

Pure stdlib mixer in isolation: mix math, atomic-write semantics, and
format-validation surfacing. No daemon / CLI involvement.
"""

from __future__ import annotations

import array
import wave
from pathlib import Path

import pytest

from record.combine import CombineError, CombineResult, combine_wavs


def _write_wav(
    path: Path,
    samples: list[int],
    *,
    channels: int = 1,
    sampwidth: int = 2,
    framerate: int = 16_000,
) -> None:
    """Write a PCM WAV at ``path`` with the given samples and format."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        if sampwidth == 2:
            wf.writeframes(array.array("h", samples).tobytes())
        elif sampwidth == 1:
            # Unsigned 8-bit PCM.
            wf.writeframes(bytes((s & 0xFF) for s in samples))
        elif sampwidth == 4:
            wf.writeframes(array.array("i", samples).tobytes())
        else:
            raise ValueError(f"unsupported sampwidth {sampwidth}")


def _read_int16(path: Path) -> array.array:
    with wave.open(str(path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        frames = wf.readframes(wf.getnframes())
    arr = array.array("h")
    arr.frombytes(frames)
    return arr


# ---------------------------------------------------------------------------
# 1. Equal-duration tone sum
# ---------------------------------------------------------------------------


def test_equal_duration_tone_sum(tmp_path: Path) -> None:
    mic = [100, -200, 300, -400, 500]
    sysm = [10, -20, 30, -40, 50]
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, mic)
    _write_wav(sys_path, sysm)

    result = combine_wavs(mic_path, sys_path, out_path)
    assert isinstance(result, CombineResult)
    assert result.path == out_path
    assert out_path.exists()

    samples = _read_int16(out_path)
    assert list(samples) == [m + s for m, s in zip(mic, sysm)]


# ---------------------------------------------------------------------------
# 2. Longer mic, shorter system -> zero-pad
# ---------------------------------------------------------------------------


def test_longer_mic_shorter_system_zero_pads_tail(tmp_path: Path) -> None:
    # Cross a chunk boundary so both EOF logic and chunk-stride logic run.
    mic = [i % 1000 for i in range(16_500)]
    sysm = [(-i) % 500 for i in range(5_000)]
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, mic)
    _write_wav(sys_path, sysm)

    result = combine_wavs(mic_path, sys_path, out_path)
    samples = list(_read_int16(out_path))
    assert len(samples) == len(mic)

    # Head: element-wise sum.
    for i in range(len(sysm)):
        assert samples[i] == mic[i] + sysm[i], f"mismatch at {i}"
    # Tail: mic unchanged.
    for i in range(len(sysm), len(mic)):
        assert samples[i] == mic[i], f"tail mismatch at {i}"

    assert result.duration_seconds == pytest.approx(len(mic) / 16_000.0)


# ---------------------------------------------------------------------------
# 3. Longer system, shorter mic -> zero-pad (symmetric)
# ---------------------------------------------------------------------------


def test_longer_system_shorter_mic_zero_pads_tail(tmp_path: Path) -> None:
    mic = [(i * 3) % 700 for i in range(4_000)]
    sysm = [(i * 5) % 1100 for i in range(20_000)]
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, mic)
    _write_wav(sys_path, sysm)

    result = combine_wavs(mic_path, sys_path, out_path)
    samples = list(_read_int16(out_path))
    assert len(samples) == len(sysm)

    for i in range(len(mic)):
        assert samples[i] == mic[i] + sysm[i], f"head mismatch at {i}"
    for i in range(len(mic), len(sysm)):
        assert samples[i] == sysm[i], f"tail mismatch at {i}"

    assert result.duration_seconds == pytest.approx(len(sysm) / 16_000.0)


# ---------------------------------------------------------------------------
# 4. All-silent one side leaves the other intact
# ---------------------------------------------------------------------------


def test_one_side_silent_passes_through_other(tmp_path: Path) -> None:
    mic = [1234, -5678, 9012, -3456, 7890, 0, -1, 1]
    sysm = [0] * len(mic)
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, mic)
    _write_wav(sys_path, sysm)

    combine_wavs(mic_path, sys_path, out_path)
    assert list(_read_int16(out_path)) == mic

    # Symmetric: silent mic.
    out_path2 = tmp_path / "out2.wav"
    _write_wav(mic_path, sysm)  # all-zero mic
    _write_wav(sys_path, mic)   # nonzero system
    combine_wavs(mic_path, sys_path, out_path2)
    assert list(_read_int16(out_path2)) == mic


# ---------------------------------------------------------------------------
# 5. Saturation clamp at int16 boundaries (no wraparound)
# ---------------------------------------------------------------------------


def test_saturation_clamp_positive_and_negative(tmp_path: Path) -> None:
    mic = [20_000, -20_000, 20_000, -20_000, 32_767, -32_768]
    sysm = [20_000, -20_000, 15_000, -15_000, 32_767, -32_768]
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, mic)
    _write_wav(sys_path, sysm)

    combine_wavs(mic_path, sys_path, out_path)
    samples = list(_read_int16(out_path))
    # Position-by-position saturation. NOT wraparound: 20000+20000=40000
    # would wrap to -25536 in int16; saturation gives 32767.
    expected = [
        32_767,    # 20000 + 20000 -> +overflow
        -32_768,   # -20000 + -20000 -> -overflow
        32_767,    # 20000 + 15000 = 35000 -> +overflow
        -32_768,   # -20000 + -15000 = -35000 -> -overflow
        32_767,    # 32767 + 32767 -> +overflow
        -32_768,   # -32768 + -32768 -> -overflow
    ]
    assert samples == expected
    # And explicitly: nothing wrapped to the opposite sign.
    for s in samples:
        assert -32_768 <= s <= 32_767


# ---------------------------------------------------------------------------
# 6. Format-validation rejection (three sub-tests)
# ---------------------------------------------------------------------------


def test_rejects_wrong_sample_rate_mic(tmp_path: Path) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [0] * 100, framerate=8_000)
    _write_wav(sys_path, [0] * 100)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)
    assert "mic" in str(exc.value)
    assert not out_path.exists()


def test_rejects_wrong_sample_rate_system(tmp_path: Path) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [0] * 100)
    _write_wav(sys_path, [0] * 100, framerate=44_100)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)
    assert "system" in str(exc.value)
    assert not out_path.exists()


def test_rejects_wrong_channel_count_stereo_mic(tmp_path: Path) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    # Stereo: interleaved L/R samples.
    _write_wav(mic_path, [0, 0] * 100, channels=2)
    _write_wav(sys_path, [0] * 100)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)
    assert "mic" in str(exc.value)
    assert not out_path.exists()


def test_rejects_wrong_channel_count_stereo_system(tmp_path: Path) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [0] * 100)
    _write_wav(sys_path, [0, 0] * 100, channels=2)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)
    assert "system" in str(exc.value)
    assert not out_path.exists()


def test_rejects_wrong_sample_width_8bit_mic(tmp_path: Path) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [128] * 100, sampwidth=1)
    _write_wav(sys_path, [0] * 100)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)
    assert "mic" in str(exc.value)
    assert not out_path.exists()


def test_rejects_wrong_sample_width_32bit_system(tmp_path: Path) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [0] * 100)
    _write_wav(sys_path, [0] * 100, sampwidth=4)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)
    assert "system" in str(exc.value)
    assert not out_path.exists()


# ---------------------------------------------------------------------------
# 7. Atomic write: mid-write injected failure
# ---------------------------------------------------------------------------


def test_atomic_write_cleans_up_on_mid_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [1000] * 32_000)  # 2 seconds
    _write_wav(sys_path, [2000] * 32_000)

    def boom(src, dst):  # type: ignore[no-untyped-def]
        # Ensure the .tmp file was actually produced before we explode.
        assert Path(src).exists()
        raise OSError("simulated rename failure")

    # Patch os.replace as seen by the combine module so the rename step
    # fails AFTER the WAV writer has already produced a complete .tmp.
    from record import combine as combine_mod
    monkeypatch.setattr(combine_mod.os, "replace", boom)

    with pytest.raises(CombineError) as exc:
        combine_wavs(mic_path, sys_path, out_path)

    # Plain-language reason (not a stack trace).
    assert str(exc.value)
    assert "simulated rename failure" in str(exc.value) or "write" in str(
        exc.value
    ).lower()

    # No artefacts left behind.
    assert not out_path.exists()
    tmp = out_path.with_name(out_path.name + ".tmp")
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# 8. duration_seconds matches the longer source within one frame
# ---------------------------------------------------------------------------


def test_duration_seconds_matches_longer_source_within_one_frame(
    tmp_path: Path,
) -> None:
    mic_len = 25_123  # ~1.57s
    sys_len = 9_999   # ~0.625s
    mic_path = tmp_path / "mic.wav"
    sys_path = tmp_path / "sys.wav"
    out_path = tmp_path / "out.wav"
    _write_wav(mic_path, [0] * mic_len)
    _write_wav(sys_path, [0] * sys_len)

    result = combine_wavs(mic_path, sys_path, out_path)
    expected = max(mic_len, sys_len) / 16_000.0
    assert abs(result.duration_seconds - expected) <= 1.0 / 16_000.0
