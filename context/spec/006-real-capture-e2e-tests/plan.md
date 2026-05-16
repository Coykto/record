# E2E real-capture test harness

## Context

Unit tests and the existing synthetic-mode integration tests didn't catch the just-discovered system-audio regression. The Swift binary has a `--test-silent-sources` / `--test-synthetic-video` mode that bypasses the real `SCStream` / `AVAudioEngine` paths, so anything that goes wrong inside those paths is invisible until someone manually runs `record start` and listens to the file. We need a layer of integration tests that exercises the *real* capture path end-to-end — real `SCStream` for system audio, real `AVAudioEngine` for mic, real ScreenCaptureKit for video — and asserts the resulting files have actual audio/video content, not just the right shape.

The harness drives capture through the highest available entry point (the daemon's control socket — same as `record start`) so it would also catch Python-side regressions in `start` command construction.

Out of scope this round:
- Hotkey end-to-end. Triggering NSEvent hotkeys from a test requires Accessibility for the test runner; not worth the flake. The hotkey handler ultimately calls the same daemon `start` op these tests drive, so downstream coverage is already there.
- CI. These tests are local-only by design — they need screen access, audio hardware, and BlackHole.

## Approach

Add **one new integration test file** that reuses the existing daemon-driven pattern from `tests/integration/test_end_to_end.py:828` (`test_end_to_end_daemon_driven_start_stop`), but:

1. Drops the `RECORD_CAPTURE_TEST_FLAGS=--test-silent-sources --test-synthetic-video` env var so the Swift child runs the real capture pipeline.
2. While capture is running, plays `tests/data/test.wav` so something lands in the system-audio and mic tracks.
3. Asserts the resulting WAVs are **non-silent** (RMS above a clear threshold) and the MP4 is non-trivially sized + decodable, not just the right shape.

### Audio routing

| Surface | How the test gets known audio into it |
|---|---|
| **System audio** | `afplay tests/data/test.wav` (or `afplay -d "BlackHole 2ch"`). `SCStream` with `capturesAudio = true` grabs the system audio mix — playing anything to any output device produces samples on the capture side. |
| **Mic** | Switch the system default *input* to `BlackHole 2ch` for the duration of the test, then `afplay -d "BlackHole 2ch" tests/data/test.wav`. `AVAudioEngine.inputNode` reads from BlackHole and captures the WAV's contents. Restore the prior input on teardown. |
| **Video** | No window manipulation. `SCStream` captures whatever's on the primary display; assertions are file-size + atom-parse + duration only. Won't catch "video is all black" but matches the agreed scope. |

### Setup the developer does once

- `brew install --cask blackhole-2ch` (virtual audio device — free, signed, no kernel ext)
- `brew install switchaudio-osx` (CLI for swapping the default input device — `SwitchAudioSource -t input -s 'BlackHole 2ch'`)
- Grant Screen Recording + Microphone permission to the terminal / IDE the tests run under (one-time TCC grant; the binary at `src/record/bin/record-capture.app/Contents/MacOS/record-capture` will prompt on first real-mode run)

The harness `pytest.skip()`s cleanly if any prerequisite is missing.

### Files to create / modify

**New: `tests/integration/test_real_capture.py`**

Two tests, both gated by skip-checks:

1. `test_real_capture_system_audio_and_video` — only requires Screen Recording permission. Plays `test.wav` to default output, asserts:
   - `<stem>-system.wav` exists with RMS > -40 dBFS (clearly non-silent)
   - `<stem>.mp4` exists, opens via the existing `_probe_mp4` (re-imported from `test_end_to_end.py`), duration ≈ capture window
   - `<stem>-mic.wav` exists with valid WAV format (content not asserted — could be silent if the room is quiet, or could pick up the speakers; either is fine for this test)
2. `test_real_capture_mic_via_blackhole` — additionally requires BlackHole + SwitchAudioSource. Sets input to BlackHole, plays `test.wav` to BlackHole, asserts:
   - `<stem>-mic.wav` RMS > -40 dBFS
   - `<stem>-system.wav` is *also* non-silent (BlackHole is both input and output — playing into it generates a system-audio loopback too)
   - MP4 decodes cleanly

Both tests follow the sandbox + socket pattern from `test_end_to_end_daemon_driven_start_stop` (test_end_to_end.py:828–991). Capture window ≈ 4 s — long enough for `afplay` to push a non-trivial slice of the 7.2 s `test.wav` through the pipeline with margin for Swift startup latency.

**New: `tests/integration/_real_capture_helpers.py`** (leading underscore — not a test module)

Small helper module, ~80 lines:

- `compute_rms_dbfs(wav_path: Path) -> float` — reads with stdlib `wave`, computes RMS, returns dBFS.
- `assert_non_silent(wav_path: Path, threshold_dbfs: float = -40.0)` — fails with the actual dBFS in the message.
- `play_audio_async(wav_path: Path, device: str | None = None) -> subprocess.Popen` — wraps `afplay [-d device]`, returns the Popen so the test can `terminate()` it during teardown.
- `blackhole_available() -> bool` — `SwitchAudioSource -a -t input` and grep for `BlackHole 2ch`.
- `switchaudio_available() -> bool` — `shutil.which("SwitchAudioSource")`.
- `temporary_input_device(name: str)` — `@contextmanager`; saves current input via `SwitchAudioSource -t input -c`, switches, restores on exit (even on test failure).
- `screen_recording_granted() -> bool` — best-effort check via `tccutil` or by running `record-capture --prime-permissions` and parsing the JSON. Used in skip-checks.

**Modify: `tests/integration/conftest.py`**

Add one new fixture:

- `real_capture_sandbox` — factory that returns a `(sandbox, cwd, env, socket_path, state_path)` tuple set up exactly like the inline boilerplate in `test_end_to_end_daemon_driven_start_stop` (test_end_to_end.py:843–867), but **without** the `RECORD_CAPTURE_TEST_FLAGS` env var. Yields and cleans up `shutil.rmtree(sandbox)`. This is the same pattern that's currently copy-pasted across the three daemon-driven tests — extracting it into a fixture removes ~25 lines of duplication from the new tests and is a small de-duplication win for the existing ones too (left as a follow-up to keep this PR focused on the new tests).

**Modify: `pyproject.toml`**

Add a pytest marker so the suite stays green by default:

```toml
[tool.pytest.ini_options]
markers = [
    "real_capture: hits real ScreenCaptureKit/AVAudioEngine; needs TCC + BlackHole. Run with --run-real-capture.",
]
```

And in `tests/integration/conftest.py`, register a `--run-real-capture` flag that flips the marker from "skip by default" to "run". Pattern: `addopts` adds `-m "not real_capture"` unless the flag is passed.

**Modify: `Makefile`**

Add one target:

```make
test-real: swift
    uv run pytest tests/integration/test_real_capture.py --run-real-capture -v
```

Document the BlackHole + SwitchAudioSource + TCC prerequisites in a comment above the target. No README changes — the make target's comment is the only doc surface, in line with the project's pattern of keeping docs at the point of use.

### Existing pieces reused

- `capture_binary` fixture from `tests/integration/conftest.py:32` — unchanged.
- `_send_control_request` and `_wait_for_socket` from `test_end_to_end.py:789, 799` — re-imported (they're module-level functions, not test fixtures, so the new file can `from tests.integration.test_end_to_end import _send_control_request, _wait_for_socket, _probe_mp4`).
- `_probe_mp4` (test_end_to_end.py:466) — same three-tier pyobjc → ffprobe → atom-parse cascade we already trust.
- The whole sandbox-`$HOME` pattern (test_end_to_end.py:843–867) — moved into the new `real_capture_sandbox` fixture.

## Verification

1. **Build + run**: `make swift && make test-real`. Expect both tests to pass (provided BlackHole + SwitchAudioSource + TCC are set up; otherwise they `skip` with a clear message).
2. **Default suite stays clean**: `make test` (existing target) — the new tests should be filtered out by the `real_capture` marker; the existing 4 integration tests still pass.
3. **Confirm the harness would have caught the current bug**: with the system-audio regression still in place, `test_real_capture_system_audio_and_video` should fail with `system.wav RMS = -∞ dBFS (silent), expected > -40 dBFS`. Run before any fix is applied; this is the gate that the harness has the right shape.
4. **Negative control**: comment out the `afplay` call in `test_real_capture_system_audio_and_video`, re-run, confirm it fails with the same "silent" assertion — proving the assertion bites.
5. **Idempotency / teardown**: run `test_real_capture_mic_via_blackhole` and confirm the system default input device is restored to its prior value afterwards (`SwitchAudioSource -t input -c` should match what it was before).

## Open follow-ups (not in this PR)

- Hotkey automation via CGEventPost + Accessibility. Pre-req: granting Accessibility to the test runner; nice to have but not blocking.
- Pattern-window video assertion (open a known visual, decode a frame, verify it's there). User explicitly deferred this.
- De-dupe the sandbox boilerplate in the three existing `test_end_to_end_daemon_driven_*` tests by adopting the new `real_capture_sandbox` fixture (with an option for the test-flags env var).
- Wire transcripts: extend the system-audio test to feed the captured WAV through Deepgram and grep the transcript for content that matches `test.wav` — strongest possible "audio actually contains intelligible content" check.
