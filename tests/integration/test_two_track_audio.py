"""Two-track audio integration test (spec 005 slice 2).

Drives the real ``record-capture`` binary in ``--test-silent-sources`` mode
(which feeds a deterministic 440 Hz sine on the mic source and an 880 Hz sine
on the system source — see ``AudioCapture.swift::tickSyntheticFeeder``) and
asserts that:

  * Two independent WAVs land at the basename-derived paths.
  * The mic WAV's dominant tone is 440 Hz with no significant energy at 880 Hz,
    and vice versa for the system WAV. This is the central correctness check
    for the "no merging / no cross-talk" requirement.
  * Each WAV's duration is within +/-100 ms of the requested capture window.
  * Exactly two ``audio_file`` events appear before the final ``stopped``
    event, each with ``status="captured_normally"`` and the right source/path.

Black-box against the wire protocol; no ``record.*`` imports (matches the
convention in ``tests/integration/test_end_to_end.py``).
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

# Capture window. Long enough for plenty of full sine cycles at both 440 Hz
# (≈660 cycles in 1.5 s) and 880 Hz, and to make the FFT-bin-resolution gentle.
CAPTURE_WINDOW_SECONDS = 1.5
TEST_DEADLINE_SECONDS = 15.0

EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPWIDTH_BYTES = 2

MIC_TONE_HZ = 440.0
SYSTEM_TONE_HZ = 880.0
# Tolerance for the detected peak vs. the tone we asked for. Bin resolution at
# 16 kHz over ~1.5 s is ~0.67 Hz, so 10 Hz is generous.
PEAK_TOLERANCE_HZ = 10.0
# A strong sine has all its energy concentrated near one bin; the magnitude at
# the *other* tone's frequency must be well below the dominant bin.
CROSS_TALK_RATIO_MAX = 0.05  # 5%


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="record-capture is macOS-only",
)


def _read_int16_samples(wav_path: Path) -> tuple[list[int], int]:
    """Return (samples, sample_rate) for a mono int16 PCM WAV."""
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == EXPECTED_CHANNELS
        assert wf.getframerate() == EXPECTED_SAMPLE_RATE
        assert wf.getsampwidth() == EXPECTED_SAMPWIDTH_BYTES
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    samples = list(struct.unpack(f"<{nframes}h", raw))
    return samples, EXPECTED_SAMPLE_RATE


def _goertzel_magnitude(samples: list[int], sample_rate: int, target_hz: float) -> float:
    """Return the (normalized) magnitude of ``target_hz`` in ``samples``.

    Goertzel's algorithm — single-frequency DFT bin in O(N) without numpy.
    Picking the nearest integer bin keeps the comparison apples-to-apples
    across the two probe frequencies.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(round(target_hz * n / sample_rate))
    omega = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = float(x) + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return math.sqrt(max(power, 0.0)) / n


def _detect_peak_hz(
    samples: list[int],
    sample_rate: int,
    candidates: tuple[float, ...] = (MIC_TONE_HZ, SYSTEM_TONE_HZ),
) -> tuple[float, float]:
    """Return (peak_frequency_hz, magnitude) over a small candidate set.

    A full FFT would let us discover an unknown peak; for this test we know
    the only two tones the synthetic feeder produces, so a Goertzel-per-tone
    sweep is both sufficient and dependency-free. We also probe a handful of
    nearby bins to defend against off-bin smearing artificially boosting the
    "other" tone's magnitude.
    """
    best_hz = 0.0
    best_mag = -1.0
    # Probe at the exact target plus a few neighbors so a tiny off-bin shift
    # doesn't cause the wrong tone to look stronger.
    for target in candidates:
        for delta in (-2.0, -1.0, 0.0, 1.0, 2.0):
            mag = _goertzel_magnitude(samples, sample_rate, target + delta)
            if mag > best_mag:
                best_mag = mag
                best_hz = target + delta
    return best_hz, best_mag


def test_two_track_audio_no_cross_talk(
    capture_binary: Path, tmp_path: Path
) -> None:
    """Mic file is 440 Hz only; system file is 880 Hz only; both ≈1.5 s."""
    output_basename = tmp_path / "two-track"
    mic_wav = tmp_path / "two-track-mic.wav"
    system_wav = tmp_path / "two-track-system.wav"

    proc = subprocess.Popen(
        [str(capture_binary), "--test-silent-sources"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    events: list[dict[str, object]] = []
    stderr_dump = ""
    stop_timer: threading.Timer | None = None
    stdin_lock = threading.Lock()

    def _send_stop() -> None:
        try:
            with stdin_lock:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
                    proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        deadline = time.monotonic() + TEST_DEADLINE_SECONDS

        # 1. ready
        ready_line = proc.stdout.readline()
        if not ready_line:
            pytest.fail("record-capture closed stdout before emitting `ready`")
        ready = json.loads(ready_line)
        assert ready == {"event": "ready"}

        # 2. start
        start_cmd = {
            "cmd": "start",
            "output_path": str(output_basename),
            "format": {
                "sample_rate": EXPECTED_SAMPLE_RATE,
                "bit_depth": 16,
                "channels": EXPECTED_CHANNELS,
            },
        }
        with stdin_lock:
            proc.stdin.write(json.dumps(start_cmd) + "\n")
            proc.stdin.flush()

        # 3. drain events until stopped
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                pytest.fail(
                    "record-capture closed stdout before emitting `stopped`; "
                    f"events so far: {events!r}"
                )
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON line on daemon stdout: {line!r}")
            events.append(ev)

            if ev.get("event") == "started" and stop_timer is None:
                stop_timer = threading.Timer(CAPTURE_WINDOW_SECONDS, _send_stop)
                stop_timer.daemon = True
                stop_timer.start()

            if ev.get("event") == "stopped":
                break
        else:
            pytest.fail(
                f"never received `stopped` event within {TEST_DEADLINE_SECONDS}s; "
                f"events so far: {events!r}"
            )

        # 4. close stdin and wait
        with stdin_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "record-capture did not exit within 5s after `stopped` event"
            )

        assert proc.stderr is not None
        stderr_dump = proc.stderr.read() or ""

        assert proc.returncode == 0, (
            f"record-capture exited with code {proc.returncode}; "
            f"stderr:\n{stderr_dump}"
        )

        # ------------------------------------------------------------------
        # Event-stream assertions: exactly two ``audio_file`` events, then
        # exactly one ``stopped`` whose basename matches what we sent.
        # ------------------------------------------------------------------
        audio_file_events = [
            ev for ev in events if ev.get("event") == "audio_file"
        ]
        stopped_events = [ev for ev in events if ev.get("event") == "stopped"]

        assert len(audio_file_events) == 2, (
            f"expected exactly 2 audio_file events, got "
            f"{len(audio_file_events)}: {audio_file_events!r}"
        )
        assert len(stopped_events) == 1, (
            f"expected exactly 1 stopped event, got {len(stopped_events)}"
        )

        # ``audio_file`` events come before ``stopped`` (the binary emits files
        # then closes out the session).
        last_af_idx = max(
            i for i, ev in enumerate(events) if ev.get("event") == "audio_file"
        )
        stopped_idx = next(
            i for i, ev in enumerate(events) if ev.get("event") == "stopped"
        )
        assert last_af_idx < stopped_idx, (
            f"audio_file events must precede stopped; got events: "
            f"{[ev.get('event') for ev in events]!r}"
        )

        # Map source → event for per-source assertions.
        af_by_source = {ev["source"]: ev for ev in audio_file_events}
        assert set(af_by_source) == {"mic", "system_audio"}, (
            f"audio_file events must cover both sources, got: "
            f"{sorted(af_by_source)!r}"
        )

        for source, expected_path in (
            ("mic", str(mic_wav)),
            ("system_audio", str(system_wav)),
        ):
            ev = af_by_source[source]
            assert ev["status"] == "captured_normally", (
                f"{source}: status={ev['status']!r}"
            )
            assert ev["path"] == expected_path, (
                f"{source}: path={ev['path']!r} vs expected {expected_path!r}"
            )

        stopped = stopped_events[0]
        assert stopped["basename"] == str(output_basename), (
            f"stopped.basename={stopped['basename']!r} vs "
            f"expected {str(output_basename)!r}"
        )

        # ------------------------------------------------------------------
        # On-disk: both files exist and play back as mono 16-bit 16 kHz PCM.
        # ------------------------------------------------------------------
        assert mic_wav.exists(), f"mic WAV missing at {mic_wav}"
        assert system_wav.exists(), f"system WAV missing at {system_wav}"

        # ------------------------------------------------------------------
        # Per-file FFT-peak assertions.
        # ------------------------------------------------------------------
        mic_samples, sr = _read_int16_samples(mic_wav)
        system_samples, _ = _read_int16_samples(system_wav)

        # Duration within +/-100 ms of the requested window.
        for label, samples_list in (
            ("mic", mic_samples),
            ("system", system_samples),
        ):
            duration = len(samples_list) / sr
            assert abs(duration - CAPTURE_WINDOW_SECONDS) <= 0.1, (
                f"{label} WAV duration {duration:.3f}s deviates from "
                f"{CAPTURE_WINDOW_SECONDS}s by more than 100 ms"
            )

        # Mic file: peak is at 440 Hz; magnitude at 880 Hz is < 5% of the peak.
        mic_peak_hz, mic_peak_mag = _detect_peak_hz(mic_samples, sr)
        assert abs(mic_peak_hz - MIC_TONE_HZ) <= PEAK_TOLERANCE_HZ, (
            f"mic file peak at {mic_peak_hz:.1f} Hz, expected ≈{MIC_TONE_HZ} Hz"
        )
        mic_other_mag = _goertzel_magnitude(mic_samples, sr, SYSTEM_TONE_HZ)
        assert mic_other_mag <= mic_peak_mag * CROSS_TALK_RATIO_MAX, (
            f"mic file leaks system-side {SYSTEM_TONE_HZ} Hz tone: "
            f"mag@880={mic_other_mag:.4g} vs peak={mic_peak_mag:.4g} "
            f"(ratio {mic_other_mag / max(mic_peak_mag, 1e-9):.2%})"
        )

        # System file: peak is at 880 Hz; magnitude at 440 Hz is < 5% of peak.
        sys_peak_hz, sys_peak_mag = _detect_peak_hz(system_samples, sr)
        assert abs(sys_peak_hz - SYSTEM_TONE_HZ) <= PEAK_TOLERANCE_HZ, (
            f"system file peak at {sys_peak_hz:.1f} Hz, "
            f"expected ≈{SYSTEM_TONE_HZ} Hz"
        )
        sys_other_mag = _goertzel_magnitude(system_samples, sr, MIC_TONE_HZ)
        assert sys_other_mag <= sys_peak_mag * CROSS_TALK_RATIO_MAX, (
            f"system file leaks mic-side {MIC_TONE_HZ} Hz tone: "
            f"mag@440={sys_other_mag:.4g} vs peak={sys_peak_mag:.4g} "
            f"(ratio {sys_other_mag / max(sys_peak_mag, 1e-9):.2%})"
        )
    finally:
        if stop_timer is not None:
            stop_timer.cancel()

        if proc.stderr is not None and not stderr_dump:
            try:
                stderr_dump = proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
        if stderr_dump:
            print(f"record-capture stderr:\n{stderr_dump}")

        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


def test_silent_mic_source_reports_silent_throughout(
    capture_binary: Path, tmp_path: Path
) -> None:
    """All-zero mic feeder yields a session-length silent WAV, status silent.

    Drives the slice 4 silent-source-detection path:
    ``--test-silent-sources --silent-mic-source`` keeps the system feeder
    emitting its 880 Hz tone while the mic feeder emits all-zero Int16
    samples for the full session. At stop:

      * The mic ``audio_file`` event reports ``status="silent_throughout"``
        with ``truncated_at_offset_seconds`` null and ``duration_seconds``
        within ±100 ms of the session window.
      * The system ``audio_file`` event reports ``status="captured_normally"``
        with its 880 Hz tone intact.
      * No ``source_lost`` event appears — silence is not loss.
      * The on-disk mic WAV is a valid mono / 16-bit / 16 kHz PCM file whose
        duration matches the session window and whose every sample is
        exactly zero (the synthetic feeder writes literal zeros; any
        non-zero sample would signal a regression).
    """
    output_basename = tmp_path / "silent-mic"
    mic_wav = tmp_path / "silent-mic-mic.wav"
    system_wav = tmp_path / "silent-mic-system.wav"

    proc = subprocess.Popen(
        [str(capture_binary), "--test-silent-sources", "--silent-mic-source"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    events: list[dict[str, object]] = []
    stderr_dump = ""
    stop_timer: threading.Timer | None = None
    stdin_lock = threading.Lock()

    def _send_stop() -> None:
        try:
            with stdin_lock:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
                    proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        deadline = time.monotonic() + TEST_DEADLINE_SECONDS

        # 1. ready
        ready_line = proc.stdout.readline()
        if not ready_line:
            pytest.fail("record-capture closed stdout before emitting `ready`")
        ready = json.loads(ready_line)
        assert ready == {"event": "ready"}

        # 2. start
        start_cmd = {
            "cmd": "start",
            "output_path": str(output_basename),
            "format": {
                "sample_rate": EXPECTED_SAMPLE_RATE,
                "bit_depth": 16,
                "channels": EXPECTED_CHANNELS,
            },
        }
        with stdin_lock:
            proc.stdin.write(json.dumps(start_cmd) + "\n")
            proc.stdin.flush()

        # 3. drain events until stopped
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                pytest.fail(
                    "record-capture closed stdout before emitting `stopped`; "
                    f"events so far: {events!r}"
                )
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON line on daemon stdout: {line!r}")
            events.append(ev)

            if ev.get("event") == "started" and stop_timer is None:
                stop_timer = threading.Timer(CAPTURE_WINDOW_SECONDS, _send_stop)
                stop_timer.daemon = True
                stop_timer.start()

            if ev.get("event") == "stopped":
                break
        else:
            pytest.fail(
                f"never received `stopped` event within {TEST_DEADLINE_SECONDS}s; "
                f"events so far: {events!r}"
            )

        # 4. close stdin and wait
        with stdin_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "record-capture did not exit within 5s after `stopped` event"
            )

        assert proc.stderr is not None
        stderr_dump = proc.stderr.read() or ""

        assert proc.returncode == 0, (
            f"record-capture exited with code {proc.returncode}; "
            f"stderr:\n{stderr_dump}"
        )

        # ------------------------------------------------------------------
        # Event-stream assertions.
        # ------------------------------------------------------------------
        audio_file_events = [
            ev for ev in events if ev.get("event") == "audio_file"
        ]
        stopped_events = [ev for ev in events if ev.get("event") == "stopped"]
        source_lost_events = [
            ev for ev in events if ev.get("event") == "source_lost"
        ]

        assert len(audio_file_events) == 2, (
            f"expected exactly 2 audio_file events, got "
            f"{len(audio_file_events)}: {audio_file_events!r}"
        )
        assert len(stopped_events) == 1, (
            f"expected exactly 1 stopped event, got {len(stopped_events)}"
        )
        assert source_lost_events == [], (
            f"silence must not produce a source_lost event; got: "
            f"{source_lost_events!r}"
        )

        # ``audio_file`` events come before ``stopped``.
        last_af_idx = max(
            i for i, ev in enumerate(events) if ev.get("event") == "audio_file"
        )
        stopped_idx = next(
            i for i, ev in enumerate(events) if ev.get("event") == "stopped"
        )
        assert last_af_idx < stopped_idx, (
            f"audio_file events must precede stopped; got events: "
            f"{[ev.get('event') for ev in events]!r}"
        )

        af_by_source = {ev["source"]: ev for ev in audio_file_events}
        assert set(af_by_source) == {"mic", "system_audio"}, (
            f"audio_file events must cover both sources, got: "
            f"{sorted(af_by_source)!r}"
        )

        # Mic: silent throughout, no truncation, full session duration.
        mic_ev = af_by_source["mic"]
        assert mic_ev["path"] == str(mic_wav), (
            f"mic audio_file path={mic_ev['path']!r} vs expected "
            f"{str(mic_wav)!r}"
        )
        assert mic_ev["status"] == "silent_throughout", (
            f"mic status={mic_ev['status']!r}, expected silent_throughout"
        )
        assert mic_ev.get("truncated_at_offset_seconds") is None, (
            f"mic truncated_at_offset_seconds="
            f"{mic_ev.get('truncated_at_offset_seconds')!r}, expected null "
            f"(silence is not truncation)"
        )
        mic_event_duration = float(mic_ev["duration_seconds"])
        assert abs(mic_event_duration - CAPTURE_WINDOW_SECONDS) <= 0.1, (
            f"mic event duration_seconds={mic_event_duration:.3f}s deviates "
            f"from {CAPTURE_WINDOW_SECONDS}s by more than 100 ms"
        )

        # System: captured normally; tone-content check happens on-disk below.
        sys_ev = af_by_source["system_audio"]
        assert sys_ev["path"] == str(system_wav), (
            f"system audio_file path={sys_ev['path']!r} vs expected "
            f"{str(system_wav)!r}"
        )
        assert sys_ev["status"] == "captured_normally", (
            f"system status={sys_ev['status']!r}, expected captured_normally"
        )
        assert sys_ev.get("truncated_at_offset_seconds") is None, (
            f"system truncated_at_offset_seconds="
            f"{sys_ev.get('truncated_at_offset_seconds')!r}, expected null"
        )

        stopped = stopped_events[0]
        assert stopped["basename"] == str(output_basename), (
            f"stopped.basename={stopped['basename']!r} vs "
            f"expected {str(output_basename)!r}"
        )

        # ------------------------------------------------------------------
        # On-disk: both WAVs exist and decode as mono 16-bit 16 kHz PCM.
        # ``_read_int16_samples`` asserts the format internally and will
        # raise if the mic WAV is unplayable / unfinalized.
        # ------------------------------------------------------------------
        assert mic_wav.exists(), f"mic WAV missing at {mic_wav}"
        assert system_wav.exists(), f"system WAV missing at {system_wav}"

        mic_samples, sr = _read_int16_samples(mic_wav)
        system_samples, _ = _read_int16_samples(system_wav)

        # Mic duration ≈ session window (no truncation).
        mic_duration = len(mic_samples) / sr
        assert abs(mic_duration - CAPTURE_WINDOW_SECONDS) <= 0.1, (
            f"mic WAV duration {mic_duration:.3f}s deviates from "
            f"{CAPTURE_WINDOW_SECONDS}s by more than 100 ms"
        )

        # Mic samples are exactly zero end-to-end. Synthetic mode writes
        # literal zero Int16s; any non-zero sample signals a regression
        # (e.g., cross-talk from the system side, or the silent-source
        # injection path no longer being all-zero).
        assert mic_samples, "mic WAV produced zero frames"
        mic_peak_abs = max(abs(s) for s in mic_samples)
        assert mic_peak_abs == 0, (
            f"mic WAV is not pure silence: max |sample| = {mic_peak_abs} "
            f"(expected 0)"
        )

        # System file duration ≈ session window and its dominant tone is
        # still 880 Hz (mic silence must not have disturbed it).
        sys_duration = len(system_samples) / sr
        assert abs(sys_duration - CAPTURE_WINDOW_SECONDS) <= 0.1, (
            f"system WAV duration {sys_duration:.3f}s deviates from "
            f"{CAPTURE_WINDOW_SECONDS}s by more than 100 ms"
        )

        sys_peak_hz, sys_peak_mag = _detect_peak_hz(system_samples, sr)
        assert abs(sys_peak_hz - SYSTEM_TONE_HZ) <= PEAK_TOLERANCE_HZ, (
            f"system file peak at {sys_peak_hz:.1f} Hz, "
            f"expected ≈{SYSTEM_TONE_HZ} Hz"
        )
        sys_other_mag = _goertzel_magnitude(system_samples, sr, MIC_TONE_HZ)
        assert sys_other_mag <= sys_peak_mag * CROSS_TALK_RATIO_MAX, (
            f"system file shows unexpected {MIC_TONE_HZ} Hz energy: "
            f"mag@440={sys_other_mag:.4g} vs peak={sys_peak_mag:.4g} "
            f"(ratio {sys_other_mag / max(sys_peak_mag, 1e-9):.2%})"
        )
    finally:
        if stop_timer is not None:
            stop_timer.cancel()

        if proc.stderr is not None and not stderr_dump:
            try:
                stderr_dump = proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
        if stderr_dump:
            print(f"record-capture stderr:\n{stderr_dump}")

        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


def test_mic_source_lost_truncates_mic_only(
    capture_binary: Path, tmp_path: Path
) -> None:
    """Mid-capture mic loss truncates the mic WAV; system keeps running.

    Drives the slice 3 truncation path: ``--inject-mic-loss-after-seconds 3.0``
    fires a synthetic ``handleMicLost`` after the mic feeder has run for 3 s,
    while the system feeder keeps emitting its 880 Hz tone for the full 6 s
    window. At stop:

      * The mic ``audio_file`` event reports ``status="truncated_at_offset"``
        with ``truncated_at_offset_seconds ≈ 3.0`` and ``duration_seconds ≈
        3.0``; the on-disk mic WAV is ~3 s long and decodes cleanly.
      * The system ``audio_file`` event reports ``status="captured_normally"``
        with ``duration_seconds`` close to the full session window; its WAV
        is ~6 s long.
      * A ``source_lost`` event for ``mic`` is emitted before ``stopped``.

    Tolerances are generous (±0.5 s on offsets/durations from the event
    stream, ±100 ms on WAV frame counts vs. the reported duration) because
    the synthetic feeder ticks at 10 ms and the injection uses a main-queue
    ``asyncAfter`` that can drift under load.
    """
    capture_window_seconds = 6.0
    injection_offset_seconds = 3.0
    offset_tolerance_seconds = 0.5
    system_duration_min = 5.5
    system_duration_max = 7.0
    frame_tolerance_seconds = 0.1

    output_basename = tmp_path / "trunc"
    mic_wav = tmp_path / "trunc-mic.wav"
    system_wav = tmp_path / "trunc-system.wav"

    proc = subprocess.Popen(
        [
            str(capture_binary),
            "--test-silent-sources",
            "--inject-mic-loss-after-seconds",
            str(injection_offset_seconds),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    events: list[dict[str, object]] = []
    stderr_dump = ""
    stop_timer: threading.Timer | None = None
    stdin_lock = threading.Lock()

    def _send_stop() -> None:
        try:
            with stdin_lock:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
                    proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        # Deadline covers the full 6 s window plus binary startup, stop
        # propagation, and writer finalization with a comfortable margin.
        deadline = time.monotonic() + capture_window_seconds + 9.0

        # 1. ready
        ready_line = proc.stdout.readline()
        if not ready_line:
            pytest.fail("record-capture closed stdout before emitting `ready`")
        ready = json.loads(ready_line)
        assert ready == {"event": "ready"}

        # 2. start
        start_cmd = {
            "cmd": "start",
            "output_path": str(output_basename),
            "format": {
                "sample_rate": EXPECTED_SAMPLE_RATE,
                "bit_depth": 16,
                "channels": EXPECTED_CHANNELS,
            },
        }
        with stdin_lock:
            proc.stdin.write(json.dumps(start_cmd) + "\n")
            proc.stdin.flush()

        # 3. drain events until stopped
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                pytest.fail(
                    "record-capture closed stdout before emitting `stopped`; "
                    f"events so far: {events!r}"
                )
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON line on daemon stdout: {line!r}")
            events.append(ev)

            if ev.get("event") == "started" and stop_timer is None:
                stop_timer = threading.Timer(
                    capture_window_seconds, _send_stop
                )
                stop_timer.daemon = True
                stop_timer.start()

            if ev.get("event") == "stopped":
                break
        else:
            pytest.fail(
                f"never received `stopped` event within deadline; "
                f"events so far: {events!r}"
            )

        # 4. close stdin and wait
        with stdin_lock:
            try:
                proc.stdin.close()
            except OSError:
                pass

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "record-capture did not exit within 5s after `stopped` event"
            )

        assert proc.stderr is not None
        stderr_dump = proc.stderr.read() or ""

        assert proc.returncode == 0, (
            f"record-capture exited with code {proc.returncode}; "
            f"stderr:\n{stderr_dump}"
        )

        # ------------------------------------------------------------------
        # Event-stream assertions.
        # ------------------------------------------------------------------
        audio_file_events = [
            ev for ev in events if ev.get("event") == "audio_file"
        ]
        stopped_events = [ev for ev in events if ev.get("event") == "stopped"]
        source_lost_events = [
            ev for ev in events if ev.get("event") == "source_lost"
        ]

        assert len(audio_file_events) == 2, (
            f"expected exactly 2 audio_file events, got "
            f"{len(audio_file_events)}: {audio_file_events!r}"
        )
        assert len(stopped_events) == 1, (
            f"expected exactly 1 stopped event, got {len(stopped_events)}"
        )

        # Both ``audio_file`` events must precede ``stopped``.
        last_af_idx = max(
            i for i, ev in enumerate(events) if ev.get("event") == "audio_file"
        )
        stopped_idx = next(
            i for i, ev in enumerate(events) if ev.get("event") == "stopped"
        )
        assert last_af_idx < stopped_idx, (
            f"audio_file events must precede stopped; got events: "
            f"{[ev.get('event') for ev in events]!r}"
        )

        # ``source_lost`` for mic must arrive before ``stopped`` and report
        # an offset close to the injection point.
        mic_lost = [
            ev for ev in source_lost_events if ev.get("source") == "mic"
        ]
        assert mic_lost, (
            f"expected a `source_lost` event for mic; "
            f"got events: {[ev.get('event') for ev in events]!r}"
        )
        mic_lost_idx = next(
            i
            for i, ev in enumerate(events)
            if ev.get("event") == "source_lost" and ev.get("source") == "mic"
        )
        assert mic_lost_idx < stopped_idx, (
            "source_lost(mic) must arrive before stopped"
        )
        mic_lost_offset = float(mic_lost[0]["at_offset_seconds"])
        assert (
            abs(mic_lost_offset - injection_offset_seconds)
            <= offset_tolerance_seconds
        ), (
            f"source_lost(mic).at_offset_seconds={mic_lost_offset:.3f}s "
            f"deviates from {injection_offset_seconds}s by more than "
            f"{offset_tolerance_seconds}s"
        )

        af_by_source = {ev["source"]: ev for ev in audio_file_events}
        assert set(af_by_source) == {"mic", "system_audio"}, (
            f"audio_file events must cover both sources, got: "
            f"{sorted(af_by_source)!r}"
        )

        # Mic: truncated at offset ≈ 3.0 s.
        mic_ev = af_by_source["mic"]
        assert mic_ev["path"] == str(mic_wav), (
            f"mic audio_file path={mic_ev['path']!r} vs expected "
            f"{str(mic_wav)!r}"
        )
        assert mic_ev["status"] == "truncated_at_offset", (
            f"mic status={mic_ev['status']!r}, expected truncated_at_offset"
        )
        mic_trunc_offset = float(mic_ev["truncated_at_offset_seconds"])
        assert (
            abs(mic_trunc_offset - injection_offset_seconds)
            <= offset_tolerance_seconds
        ), (
            f"mic truncated_at_offset_seconds={mic_trunc_offset:.3f}s "
            f"deviates from {injection_offset_seconds}s by more than "
            f"{offset_tolerance_seconds}s"
        )
        mic_event_duration = float(mic_ev["duration_seconds"])
        assert (
            abs(mic_event_duration - injection_offset_seconds)
            <= offset_tolerance_seconds
        ), (
            f"mic duration_seconds={mic_event_duration:.3f}s deviates from "
            f"{injection_offset_seconds}s by more than "
            f"{offset_tolerance_seconds}s"
        )

        # System: full session, no truncation.
        sys_ev = af_by_source["system_audio"]
        assert sys_ev["path"] == str(system_wav), (
            f"system audio_file path={sys_ev['path']!r} vs expected "
            f"{str(system_wav)!r}"
        )
        assert sys_ev["status"] == "captured_normally", (
            f"system status={sys_ev['status']!r}, expected captured_normally"
        )
        assert sys_ev.get("truncated_at_offset_seconds") is None, (
            f"system truncated_at_offset_seconds="
            f"{sys_ev.get('truncated_at_offset_seconds')!r}, expected null"
        )
        sys_event_duration = float(sys_ev["duration_seconds"])
        assert system_duration_min <= sys_event_duration <= system_duration_max, (
            f"system duration_seconds={sys_event_duration:.3f}s outside "
            f"[{system_duration_min}, {system_duration_max}]"
        )

        stopped = stopped_events[0]
        assert stopped["basename"] == str(output_basename), (
            f"stopped.basename={stopped['basename']!r} vs expected "
            f"{str(output_basename)!r}"
        )

        # ------------------------------------------------------------------
        # On-disk: both WAVs exist and decode as mono 16-bit 16 kHz PCM.
        # ``_read_int16_samples`` asserts the format internally and will
        # raise if the mic WAV is unplayable / unfinalized.
        # ------------------------------------------------------------------
        assert mic_wav.exists(), f"mic WAV missing at {mic_wav}"
        assert system_wav.exists(), f"system WAV missing at {system_wav}"

        mic_samples, sr = _read_int16_samples(mic_wav)
        system_samples, _ = _read_int16_samples(system_wav)

        mic_wav_duration = len(mic_samples) / sr
        assert (
            abs(mic_wav_duration - mic_event_duration)
            <= frame_tolerance_seconds
        ), (
            f"mic WAV duration {mic_wav_duration:.3f}s deviates from "
            f"event duration_seconds {mic_event_duration:.3f}s by more than "
            f"{frame_tolerance_seconds * 1000:.0f} ms"
        )

        sys_wav_duration = len(system_samples) / sr
        assert (
            abs(sys_wav_duration - sys_event_duration)
            <= frame_tolerance_seconds
        ), (
            f"system WAV duration {sys_wav_duration:.3f}s deviates from "
            f"event duration_seconds {sys_event_duration:.3f}s by more than "
            f"{frame_tolerance_seconds * 1000:.0f} ms"
        )
    finally:
        if stop_timer is not None:
            stop_timer.cancel()

        if proc.stderr is not None and not stderr_dump:
            try:
                stderr_dump = proc.stderr.read() or ""
            except Exception:
                stderr_dump = ""
        if stderr_dump:
            print(f"record-capture stderr:\n{stderr_dump}")

        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
