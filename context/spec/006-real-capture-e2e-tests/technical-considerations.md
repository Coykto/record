<!--
This document describes HOW to build the feature at an architectural level.
It is NOT a copy-paste implementation guide.
-->

# Technical Specification: Real-Capture End-to-End Test Harness

- **Functional Specification:** [`./functional-spec.md`](./functional-spec.md)
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

Add one new opt-in pytest integration test that drives the **real** macOS capture pipeline end-to-end, plus a small helper module, a conftest hook for the opt-in flag and shared fixture, a pytest marker, and a new `make test-real` target.

The harness reuses the existing daemon-driven pattern from `tests/integration/test_end_to_end.py::test_end_to_end_daemon_driven_start_stop` (line 828) — sandboxed `$HOME`, daemon control socket, `start` → `stop` cycle — but **omits** the `RECORD_CAPTURE_TEST_FLAGS=--test-silent-sources --test-synthetic-video` env var. With those flags absent the Swift capture binary runs against the real `SCStream` (system audio + video) and `AVAudioEngine` (mic) paths.

During the capture window, the test plays the existing `tests/data/test.wav` (7.2 s mono, 16 kHz PCM) through `afplay`. The system default input is switched to BlackHole 2ch for the duration of the test, and `afplay -d 'BlackHole 2ch'` routes the same WAV into the virtual input so `AVAudioEngine.inputNode` records known audio. After `stop`, the harness opens both per-source WAVs, measures RMS loudness in dBFS, and asserts each is above a clear silence threshold; the MP4 is probed via the existing `_probe_mp4` cascade (pyobjc → ffprobe → atom-parse) for non-zero duration.

There are no source-tree code changes. All edits live under `tests/integration/`, plus `pyproject.toml` (marker) and `Makefile` (target). The capture binary gains one new flag (`--check-permissions`) used by the harness's pre-flight; that flag emits two JSON-line events (one per permission) and exits — it is not invoked from the production path.

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 New and modified files

| Path | Action | Responsibility |
|---|---|---|
| `tests/integration/test_real_capture.py` | new | The single real-capture pytest test. |
| `tests/integration/_real_capture_helpers.py` | new (leading underscore so pytest does not collect it) | RMS/dBFS computation, BlackHole + SwitchAudioSource detection, input-device context manager, `afplay` wrapper, TCC pre-flight via the capture binary's probe mode. |
| `tests/integration/conftest.py` | modified | Register `--run-real-capture` flag; deselect items marked `real_capture` unless the flag is set; add a `real_capture_sandbox` fixture (sandboxed `$HOME` + control socket + state path, no synthetic flags). |
| `pyproject.toml` | modified | Register the `real_capture` pytest marker. |
| `Makefile` | modified | Add a `test-real` target (depends on `swift`) that invokes `pytest tests/integration/test_real_capture.py --run-real-capture -v`. Carry the prerequisite-install commands in a comment block above the target. |
| `swift-capture/Sources/...` (one file) | modified | Add a `--check-permissions` flag to the capture binary that emits JSON-line events `{"event":"permission","name":"screen_recording","granted":bool}` and `{"event":"permission","name":"microphone","granted":bool}` then exits. No effect on the production capture path. |

### 2.2 The test itself

One test: `test_real_capture_end_to_end`, marked `@pytest.mark.real_capture`. Sequenced as:

1. **Pre-flight (all-or-nothing):** invoke the helpers below in order. The first that fails causes the test to call `pytest.fail()` with a message naming the missing prerequisite *and* the install/grant command for it. The test does **not** `pytest.skip()` — when the engineer invokes `make test-real`, missing prereqs are real failures, per Section 2.7 of the functional spec.
   - `assert_binary_built(capture_binary)`
   - `assert_blackhole_installed()` (greps `SwitchAudioSource -a -t input` output for `BlackHole 2ch`)
   - `assert_switchaudio_available()` (`shutil.which("SwitchAudioSource")`)
   - `assert_tcc_permissions(capture_binary)` (invokes `<binary> --check-permissions`, parses both events, asserts both `granted: true`)
2. **Acquire sandbox** via the new `real_capture_sandbox` fixture (`(sandbox, cwd, env, socket_path, state_path)`).
3. **Spawn daemon** (`python -m record.daemon`) with the sandboxed env. **No `RECORD_CAPTURE_TEST_FLAGS`.**
4. **Wait for socket** via the existing `_wait_for_socket`.
5. **Switch system default input to BlackHole 2ch** inside a `temporary_input_device("BlackHole 2ch")` context manager (saves prior input, restores in `finally`).
6. **Send `start`** via the existing `_send_control_request`. The daemon's `start` blocks until Swift emits `started` + the two `source_attached` events; once it returns, the capture is hot end-to-end.
7. **Play `tests/data/test.wav` twice in parallel** via two `afplay` subprocesses (Popen):
   - One into the default output device (system-audio capture path).
   - One into `BlackHole 2ch` (mic path via the swapped default input).
   Both run from inside the `with` block so they are guaranteed to be terminated even if an assertion fails.
8. **Sleep ~4 s** so a non-trivial slice of the 7.2 s WAV is captured (room for Swift startup latency).
9. **Terminate both `afplay` processes.**
10. **Send `stop`.** Both per-source WAVs (`<stem>-mic.wav`, `<stem>-system.wav`) and the MP4 land in `cwd`.
11. **Send `quit`.** Daemon exits cleanly.
12. **Assert content:**
    - `compute_rms_dbfs(mic_wav) > -40.0` — fails with the actual dBFS in the message.
    - `compute_rms_dbfs(system_wav) > -40.0` — same.
    - `_probe_mp4(video_path)` returns `duration > 0.5` and the file size is above a minimum (~50 KB) — anything smaller is a stub, not a real-encoded H.264 stream.

The capture window is 4 s — long enough to push real signal through `SCStream` and `AVAudioEngine` from a cold start, short enough that the test is not painful to run locally.

### 2.3 The helpers module — `tests/integration/_real_capture_helpers.py`

| Function | Responsibility |
|---|---|
| `compute_rms_dbfs(wav_path: Path) -> float` | Open the file with stdlib `wave`, read all samples (16-bit PCM little-endian), compute RMS, return `20*log10(rms / 32768.0)`. Returns `-math.inf` for an all-zero file. |
| `assert_non_silent(wav_path: Path, threshold_dbfs: float = -40.0)` | Helper that calls the above and raises `AssertionError` with both the actual dBFS and the threshold. |
| `play_audio_async(wav_path: Path, device: str | None = None) -> subprocess.Popen` | Spawns `afplay [-d device] <path>`; caller is responsible for `terminate()`. |
| `blackhole_available() -> bool` | Runs `SwitchAudioSource -a -t input`; grep stdout for `BlackHole 2ch`. |
| `switchaudio_available() -> bool` | `shutil.which("SwitchAudioSource") is not None`. |
| `temporary_input_device(name: str)` | `@contextmanager`: reads current default input via `SwitchAudioSource -t input -c`, runs `SwitchAudioSource -t input -s <name>`, and restores the prior name in `finally`. |
| `check_capture_permissions(binary: Path) -> dict[str, bool]` | Run `<binary> --check-permissions`; parse two JSON-line `{"event":"permission",...}` events; return `{"screen_recording": bool, "microphone": bool}`. |
| `assert_prereqs_or_fail(binary: Path) -> None` | Calls all the checks above in order; raises `pytest.Failed` with an actionable install/grant message on the first failure. Carries the install commands inline so the message matches what's in the Makefile. |

### 2.4 The conftest changes — `tests/integration/conftest.py`

Three additions:

1. **`pytest_addoption(parser)`** — register a `--run-real-capture` boolean flag.
2. **`pytest_collection_modifyitems(config, items)`** — when the flag is **not** set, deselect every item that carries the `real_capture` marker. (Implementing this via collection-modifyitems rather than `addopts` keeps the deselect a pytest-internal decision and avoids stomping on `addopts` configured elsewhere.)
3. **`real_capture_sandbox` fixture** — a function-scoped fixture that produces `(sandbox, cwd, env, socket_path, state_path)` identical to the inline boilerplate at `test_end_to_end.py:843–867`, but **without** the `RECORD_CAPTURE_TEST_FLAGS` env var. Cleans up via `shutil.rmtree(sandbox, ignore_errors=True)` on teardown.

### 2.5 The `pyproject.toml` change

Under `[tool.pytest.ini_options]`, add:

```toml
markers = [
  "real_capture: hits the real macOS capture pipeline; needs BlackHole + SwitchAudioSource + TCC. Opt-in via --run-real-capture.",
]
```

### 2.6 The `Makefile` change

Add one target (depending on `swift` so the binary is freshly built):

```make
# Prerequisites (one-time setup):
#   brew install --cask blackhole-2ch
#   brew install switchaudio-osx
#   Grant Screen Recording + Microphone permission to record-capture
#     (the binary will prompt on first run via `make test-real`).
# Heads-up: during this test, audio is routed through BlackHole — your
# Mac will look silent for ~4 seconds. That's expected.
test-real: swift
	uv run pytest tests/integration/test_real_capture.py --run-real-capture -v
```

### 2.7 The Swift-side change — `--check-permissions`

The capture binary already starts an `SCShareableContent` request on launch, and ScreenCaptureKit's TCC dialog fires the first time that runs against a fresh identity. Add a small command-line branch:

- When `--check-permissions` is passed, the binary:
  1. Calls `SCShareableContent.current` and emits `{"event":"permission","name":"screen_recording","granted": <true|false>}` based on whether the call returned content or threw `notAuthorized`.
  2. Calls `AVCaptureDevice.authorizationStatus(for: .audio)` and emits `{"event":"permission","name":"microphone","granted": <bool>}`.
  3. Exits 0 in both cases (the test interprets the events; the exit code is not load-bearing).

Crucially, this branch does **not** call `requestAccess` or prompt — it only reports. The dialog is triggered by the real capture path on first `record start`, as today.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **Capture binary path / signing identity.** TCC grants are keyed to the binary's signing identity. The existing `scripts/ensure-signing-cert.sh` already gives `record-capture` a stable local identity that persists across `make swift` rebuilds, so prior grants remain valid for `make test-real`. No new infrastructure needed.
- **`tests/data/test.wav`** is already committed (added in `324e60e audio split`) — no new test data.
- **BlackHole 2ch** and **switchaudio-osx** are external runtime dependencies, but only on the engineer's machine, not the project. Installable via `brew`; documented at the make target.
- **The daemon's control protocol** (the `start` / `stop` / `quit` ops at `src/record/control.py`) is the integration surface — no protocol changes needed.

### Potential Risks & Mitigations

- **Risk: `afplay` finishes before the capture window closes.** `tests/data/test.wav` is 7.2 s and the capture window is 4 s, so this is structurally impossible. No mitigation needed.
- **Risk: System default input is not restored after a crashing test.** Mitigated by wrapping the entire capture sequence in `temporary_input_device(...)`'s context manager (`finally` runs even on most failure modes; pytest does not call `os._exit`). As belt-and-suspenders, the helper logs the previous device name to stderr before switching so the engineer can recover manually if something is truly catastrophic.
- **Risk: The system-audio test picks up other audio playing on the developer's machine (Spotify, YouTube), polluting the system WAV.** Acceptable — the assertion is "non-silent", not "exactly the known WAV". Any extra audio strengthens the signal.
- **Risk: `-40 dBFS` threshold is too tight (false negatives on quiet captures) or too loose (false positives on hum / noise).** `test.wav` is human speech at conversational volume; captured at AVAudio default gain it sits around -25 to -15 dBFS. A truly silent capture is `-∞ dBFS`. The 25 dB margin between the two regimes is comfortable. If false positives appear we can tighten the threshold to `-30 dBFS`; if false negatives appear (unlikely) we can loosen to `-50 dBFS`.
- **Risk: A test failure leaves a `daemon` subprocess running.** The existing daemon-driven test pattern at line 969–980 already handles this with a `finally` block that `SIGTERM`s the process and `kill`s on timeout; the new test reuses that pattern verbatim.
- **Risk: BlackHole's TCC entry is treated as a separate microphone by macOS, so granting Microphone to `record-capture` once isn't enough; engineer might need to grant it again after switching the default input.** macOS's Microphone permission is per-app, not per-device. One grant suffices. Flagging as the most likely source of "why is the test failing on my machine" confusion.
- **Risk: `record-capture --check-permissions` triggers the TCC dialog on a clean machine.** Calling `SCShareableContent.current` *will* fire the dialog if no decision has been made yet. This is desirable on the engineer's first run (they get prompted; the test then fails on the next run if they denied, succeeds if they granted). The pre-flight message after a denial should say "denied — re-grant in System Settings → Privacy & Security → Screen Recording".
- **Risk: A future change to `make swift` regenerates the signing identity and orphans the TCC grant.** Already mitigated by `scripts/ensure-signing-cert.sh`. If this changes, `make test-real` will start failing in the pre-flight with a clear "Screen Recording permission missing" message — which is the right failure mode.

---

## 4. Testing Strategy

This specification *is* a testing strategy — there is no production code under test. The verification approach is:

- **Negative control:** run the test once with the `afplay` lines commented out; both the mic and system-audio assertions must fail with `RMS = -inf dBFS, expected > -40.0 dBFS`. Proves the assertion bites. Documented in the spec verification steps; not enshrined as an automated test.
- **Regression-catches-the-known-bug check (manual, one-time):** with the recent system-audio regression still in tree (before the fix lands), `make test-real` should fail on the system-audio RMS assertion. This is the practical demonstration that the harness has teeth. Documented in the verification steps of `plan.md`; not part of the acceptance criteria.
- **Default suite remains green:** `make test` should run the same set of tests it did before this change. Verified by the conftest's `pytest_collection_modifyitems` deselecting the `real_capture` marker unless `--run-real-capture` is passed.
- **Teardown invariant:** after a successful `make test-real` and after a deliberately-broken `make test-real` (e.g., temporarily set the dBFS threshold to `+0`), the developer's default macOS input device matches what it was before — verified by running `SwitchAudioSource -t input -c` before and after.
- **Cross-machine sanity:** the spec author runs the harness on at least one second Mac (or one second user account on the same Mac) to confirm the prereq error messages are actually actionable for someone who hasn't already set up the project.
