"""Real-capture end-to-end test (opt-in).

Drives the production capture pipeline against the real macOS audio + video
APIs; requires BlackHole 2ch, SwitchAudioSource, and TCC grants. Opt in with
`--run-real-capture` (or `make test-real`).

Slice 5 layers known-audio playback over the daemon-driven start/stop
sequence: two ``afplay`` processes push ``tests/data/test.wav`` into the mic
(via BlackHole) and the system-audio path while capture is hot, and the
post-stop assertions verify both per-source WAVs are non-silent at -40 dBFS
and the MP4 is above the 50 KB real-encode threshold.
"""

from __future__ import annotations

import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

# Reuse the daemon-IPC helpers from the synthetic-mode daemon-driven test.
# This module imports test-helper functions (not production code) from a
# sibling test file — that is the established pattern in this directory.
from tests.integration._real_capture_helpers import (
    assert_non_silent,
    assert_prereqs_or_fail,
    play_audio_async,
    temporary_input_device,
)
from tests.integration.test_end_to_end import (
    _probe_mp4,
    _send_control_request,
    _wait_for_socket,
)

# Format the orchestrator always negotiates (architecture / Slice 3).
# Duplicated here on purpose: the real-capture suite must not import from
# the synthetic-mode test module's constant set, so it does not silently
# track a future change there.
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPWIDTH_BYTES = 2  # 16-bit signed PCM


@pytest.mark.real_capture
def test_real_capture_end_to_end(
    capture_binary: Path, real_capture_sandbox
) -> None:
    """End-to-end real-capture test driven via the daemon control socket.

    Sequence (mirrors ``test_end_to_end_daemon_driven_start_stop`` but
    without the synthetic-mode flags):

      1. Pre-flight prereqs (BlackHole, SwitchAudioSource, TCC grants).
      2. Switch macOS default input to BlackHole 2ch (restored in ``finally``).
      3. Spawn ``python -m record.daemon`` against the sandboxed env.
      4. Wait for the daemon to bind its control socket.
      5. Send ``start``, sleep ~4 s, send ``stop``, send ``quit``.
      6. During the capture window, play ``tests/data/test.wav`` twice in
         parallel via ``afplay`` (one into BlackHole for the mic path, one
         on the default output for the system-audio path).
      7. Assert the three output files exist; WAVs parse with the expected
         format and are non-silent at -40 dBFS; the MP4 probes to a non-zero
         duration and is above the 50 KB stub threshold.
    """
    assert_prereqs_or_fail(capture_binary)
    sandbox, cwd, env, socket_path, state_path = real_capture_sandbox

    with temporary_input_device("BlackHole 2ch"):
        daemon_proc = subprocess.Popen(
            [sys.executable, "-m", "record.daemon"],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Wait for the daemon to bind its socket.
            if not _wait_for_socket(socket_path, timeout=10.0):
                assert daemon_proc.stderr is not None
                try:
                    stderr_dump = daemon_proc.stderr.read() or ""
                except Exception:
                    stderr_dump = ""
                pytest.fail(
                    f"daemon did not bind socket at {socket_path} within 10s; "
                    f"stderr:\n{stderr_dump}"
                )

            # ----- start -------------------------------------------------
            start_resp = _send_control_request(socket_path, {"op": "start"})
            assert start_resp["status"] == "ok", start_resp
            audio_path = Path(start_resp["audio_path"])
            audio_paths = start_resp.get("audio_paths") or {}
            system_audio_path = Path(audio_paths["system_audio"])
            video_path = Path(start_resp["video_path"])

            # Play the known test signal into BOTH paths in parallel:
            # ``-d "BlackHole 2ch"`` routes into the (now-default) input the
            # mic captures, while the unrouted afplay lands on the default
            # output the system-audio path records. test.wav is 7.2 s, so
            # neither player finishes before the 4 s capture window closes.
            test_wav = (
                Path(__file__).resolve().parents[1] / "data" / "test.wav"
            )
            assert test_wav.exists(), (
                f"known-audio fixture missing at {test_wav}"
            )
            mic_player = play_audio_async(test_wav, device="BlackHole 2ch")
            system_player = play_audio_async(test_wav, device=None)

            try:
                # ~4 s capture window: long enough for cold-start Swift to
                # push real audio + video frames through the real
                # ``SCStream`` and ``AVAudioEngine`` paths.
                time.sleep(4.0)

                # ----- stop ---------------------------------------------
                stop_resp = _send_control_request(
                    socket_path, {"op": "stop"}, timeout=30.0
                )
                assert stop_resp["status"] == "ok", stop_resp
            finally:
                # Acceptance criterion 2.8: afplay processes must be killed
                # even if any subsequent assertion fails.
                for player in (mic_player, system_player):
                    try:
                        player.terminate()
                        player.wait(timeout=2.0)
                    except Exception:
                        pass

            # Files materialised on disk.
            assert audio_path.exists(), (
                f"mic audio file missing at {audio_path}"
            )
            assert system_audio_path.exists(), (
                f"system audio file missing at {system_audio_path}"
            )
            assert video_path.exists(), (
                f"video file missing at {video_path}"
            )

            # WAV format sanity — both per-source files.
            for label, wav_path in (
                ("mic", audio_path),
                ("system_audio", system_audio_path),
            ):
                with wave.open(str(wav_path), "rb") as wf:
                    nchannels = wf.getnchannels()
                    framerate = wf.getframerate()
                    sampwidth = wf.getsampwidth()
                    nframes = wf.getnframes()
                assert nchannels == EXPECTED_CHANNELS, (
                    f"{label} WAV {wav_path}: expected "
                    f"{EXPECTED_CHANNELS} channel(s), got {nchannels}"
                )
                assert framerate == EXPECTED_SAMPLE_RATE, (
                    f"{label} WAV {wav_path}: expected "
                    f"{EXPECTED_SAMPLE_RATE} Hz, got {framerate}"
                )
                assert sampwidth == EXPECTED_SAMPWIDTH_BYTES, (
                    f"{label} WAV {wav_path}: expected "
                    f"{EXPECTED_SAMPWIDTH_BYTES}-byte samples, got {sampwidth}"
                )
                wav_duration = nframes / framerate if framerate else 0.0
                assert wav_duration > 0.5, (
                    f"{label} WAV {wav_path}: duration {wav_duration:.3f}s "
                    f"is too short (expected > 0.5s)"
                )

            # Content assertions: the known WAV was played into both paths
            # during the capture window, so neither output may be silent.
            assert_non_silent(audio_path)
            assert_non_silent(system_audio_path)

            # MP4 probe — real capture, so screen-resolution-dependent
            # dimensions cannot be asserted exactly; just non-zero.
            width, height, mp4_duration = _probe_mp4(video_path)
            assert width > 0, f"mp4 width {width} is not positive"
            assert height > 0, f"mp4 height {height} is not positive"
            assert mp4_duration > 0.5, (
                f"mp4 duration {mp4_duration:.3f}s is too short "
                f"(expected > 0.5s)"
            )
            # Anything under ~50 KB is a stub MP4 (header-only / no real
            # H.264 frames), not a successful real capture.
            mp4_size = video_path.stat().st_size
            assert mp4_size > 50_000, (
                f"mp4 size {mp4_size} bytes is below 50KB stub threshold"
            )

            # ----- quit --------------------------------------------------
            quit_resp = _send_control_request(socket_path, {"op": "quit"})
            assert quit_resp["status"] == "ok"

            # Daemon should exit cleanly.
            try:
                daemon_proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "daemon did not exit within 10s after quit request"
                )
            assert daemon_proc.returncode == 0, (
                f"daemon exited with code {daemon_proc.returncode}"
            )
        finally:
            if daemon_proc.poll() is None:
                try:
                    daemon_proc.send_signal(15)  # SIGTERM
                    daemon_proc.wait(timeout=5.0)
                except Exception:
                    pass
            if daemon_proc.poll() is None:
                try:
                    daemon_proc.kill()
                except Exception:
                    pass

            if daemon_proc.stderr is not None:
                try:
                    stderr_dump = daemon_proc.stderr.read() or ""
                except Exception:
                    stderr_dump = ""
                if stderr_dump:
                    print(f"record.daemon stderr:\n{stderr_dump}")
