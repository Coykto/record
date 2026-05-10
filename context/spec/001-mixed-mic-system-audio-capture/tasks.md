# Tasks: Mixed Mic + System Audio Capture

## Slice 1 — Build scaffolding & `record --help` works

Goal: both binaries build and install; the CLI exists (commands stubbed); the Swift binary runs and prints a version string.

- [x] **Slice 1: Build scaffolding**
  - [x] Create `src/record/` Python package layout (`__init__.py`, `cli.py` with empty Typer app exposing no-op `start` and `stop` subcommands), update `pyproject.toml` to add `typer`, `pydantic`, `structlog` deps and the `[project.scripts] record = "record.cli:app"` entry point. **[Agent: python-backend]**
  - [x] Create `swift-capture/` Swift package: `Package.swift` targeting macOS 13+, `Sources/RecordCapture/main.swift` that just prints a version string and exits. Include the `ScreenCaptureKit`, `AVFoundation`, `CoreAudio`, `AppKit` framework imports as a build-time check. **[Agent: macos-swift]**
  - [x] Add `Makefile` at repo root with `make swift` (runs `swift build -c release` in `swift-capture/`, copies binary to `src/record/bin/record-capture`, chmods +x), `make install` (`make swift` + `uv pip install -e .`), and a placeholder `make test`. Add `src/record/bin/` to `.gitignore`. **[Agent: general-purpose]**
  - [x] **Verification:** Run `make install`. Run `record --help` and confirm both `start` and `stop` subcommands appear. Run `src/record/bin/record-capture` directly and confirm version line is printed. **[Agent: python-backend]**

## Slice 2 — End-to-end seam works with a stub Swift binary (no real audio)

Goal: `record start` and `record stop` actually wire through to a Swift binary that speaks the JSON-line protocol but does not yet capture any audio. PID file, state file, and summary printout work.

- [x] **Slice 2: Stub IPC end-to-end**
  - [x] Define the JSON-line protocol on the Swift side: `Sources/RecordCapture/Protocol.swift` with `Codable` structs for the `start`/`stop`/`shutdown` commands and the `ready`/`started`/`source_attached`/`source_lost`/`stopped`/`error`/`permission_required`/`permission_denied` events per tech spec §2.7. **[Agent: macos-swift]**
  - [x] Implement `Sources/RecordCapture/main.swift` stdin command loop: emits `ready` on startup, accepts a `start` command and immediately emits `started` then two `source_attached` events (mic + system_audio) without actually opening any sources, accepts a `stop` command and emits `stopped` with duration computed from internal start time, exits cleanly on `shutdown` or SIGTERM. **[Agent: macos-swift]**
  - [x] Mirror the protocol on the Python side: `src/record/ipc.py` with pydantic models for every command and event; one helper to serialize a command to a JSON line and one to parse a JSON line into the appropriate event model. **[Agent: python-backend]**
  - [x] Implement `src/record/paths.py` (resolves and ensures `~/Library/Application Support/record/` and `~/Library/Logs/record/`) and `src/record/state.py` (PID file create/read/stale-detect/cleanup; capture-state.json atomic read/write per tech spec §2.6). **[Agent: python-backend]**
  - [x] Implement `src/record/logging_setup.py` (`structlog` JSON renderer writing to `~/Library/Logs/record/orchestrator.log` with size-based rotation per architecture doc). **[Agent: python-backend]**
  - [x] Implement `src/record/supervisor.py`: long-running process that resolves the bundled `record-capture` binary path via `importlib.resources`, spawns it via `subprocess.Popen(start_new_session=True)`, reads its stdout JSON-line stream, tee's its stderr to `daemon.log`, updates `capture-state.json` on every event, listens for SIGTERM → forwards `{"cmd":"stop"}` to the binary's stdin, waits for clean exit, finalizes state file, exits. **[Agent: python-backend]**
  - [x] Implement `record start` in `src/record/cli.py`: resolve absolute output path (`$CWD/{ISO-8601}.wav`), check PID file (exit 1 if alive), spawn the supervisor as a detached process (`start_new_session=True`, stdio closed), write PID file with the supervisor's PID, return immediately. **[Agent: python-backend]**
  - [x] Implement `record stop` in `src/record/cli.py`: read PID file (exit 1 if absent or stale), SIGTERM the supervisor, wait with timeout, read final `capture-state.json`, print summary (path / duration / sources / warnings), remove PID and state files. **[Agent: python-backend]**
  - [x] **Verification:** Run `record start`. Confirm: prompt returns immediately; `~/Library/Application Support/record/capture.pid` exists; `capture-state.json` shows `started` with both sources `attached`. Wait ~3 s, run `record stop`. Confirm: summary prints expected output path (`$CWD/...wav`), duration ≈ 3 s, sources show "mic + system audio", PID and state files are removed; `~/Library/Logs/record/daemon.log` and `orchestrator.log` contain expected JSON event lines. (No `.wav` file is produced yet — that's slice 3.) **[Agent: python-backend]**

## Slice 3 — System audio capture produces a real WAV

Goal: `record start`/`record stop` produces a real `.wav` with system audio in it. Microphone source is still faked with silence in this slice.

- [x] **Slice 3: System audio → WAV**
  - [x] Implement `Sources/RecordCapture/Permissions.swift`: query `CGPreflightScreenCaptureAccess()` (or first call to `SCShareableContent.current`); if `notDetermined`, emit `permission_required` event and trigger the macOS prompt; if denied, emit `permission_denied` and exit non-zero. **[Agent: macos-swift]**
  - [x] Implement `Sources/RecordCapture/WAVWriter.swift`: wraps `AVAudioFile`/`ExtAudioFile` configured for **mono, 16-bit signed integer PCM, 16 kHz**. Accepts `AVAudioPCMBuffer` writes, finalizes file on close. **[Agent: macos-swift]**
  - [x] Implement system-audio path in `Sources/RecordCapture/AudioCapture.swift`: configure `SCStream` with `SCStreamConfiguration.capturesAudio = true` (audio-only — no video output), receive `CMSampleBuffer`s in the audio callback, convert to `AVAudioPCMBuffer`, run through `AVAudioConverter` to the target mono/16 kHz/16-bit format, feed into the `AVAudioMixerNode`. The mic input is a silent stub for this slice. Emit `source_attached` for `system_audio` once the stream is running; emit `source_attached` for `mic` immediately (still stubbed). **[Agent: macos-swift]**
  - [x] Wire `main.swift`'s `start` command to construct `AudioCapture` with the requested output path, kick off capture, emit `started`. The `stop` command stops capture, finalizes the WAV, emits `stopped`. **[Agent: macos-swift]**
  - [x] Update `record start` to pass `format` (sample_rate=16000, bit_depth=16, channels=1) and absolute `output_path` in the `start` command per protocol. **[Agent: python-backend]**
  - [x] **Verification (manual — requires Screen Recording TCC permission grant):** First-run: `record start`. Confirm macOS Screen Recording prompt appears; grant it. Play audio in another app (`afplay /System/Library/Sounds/Glass.aiff` repeatedly, or play a YouTube video). Run `record stop`. Confirm `.wav` is produced in CWD; verify with `afinfo <file>.wav` that format is `16000 Hz, 1 ch, 16-bit Linear PCM`; play the file and confirm the system audio is audible. **[Agent: macos-swift]**

## Slice 4 — Microphone capture mixed into the WAV

Goal: WAV contains both the user's mic and the system's audio output, mixed.

- [x] **Slice 4: Mic + system mix**
  - [x] Extend `Permissions.swift` to also check `AVCaptureDevice.authorizationStatus(for: .audio)`; if `notDetermined`, emit `permission_required` and call `AVCaptureDevice.requestAccess(for: .audio)`; if denied, emit `permission_denied` and exit non-zero. **[Agent: macos-swift]**
  - [x] Replace the mic stub in `AudioCapture.swift` with a real `AVAudioEngine` input node attached to the system default input device. Resample mic buffers to mono/16 kHz via a second `AVAudioConverter` and feed into the same `AVAudioMixerNode` as the system-audio path. Emit `source_attached` for `mic` only when the engine is running successfully. **[Agent: macos-swift]**
  - [x] **Verification (manual — requires Microphone TCC permission grant):** First-run: `record start`. Confirm macOS Microphone prompt appears; grant it. Talk into the mic while `afplay` plays system audio in another terminal. Run `record stop`. Play the resulting `.wav` and confirm both your voice and the system audio are clearly audible in the same file. Switch the system default input (System Settings → Sound → Input) to a different device; repeat and confirm the new device is captured. **[Agent: macos-swift]**

## Slice 5 — Mid-capture source loss is recorded & surfaced

Goal: if mic or system audio drops mid-capture, the other continues, the warning is recorded in state, and `record stop` surfaces it.

- [x] **Slice 5: Resilience**
  - [x] In `AudioCapture.swift`, observe `AVAudioEngineConfigurationChange` notifications and `SCStreamDelegate.stream(_:didStopWithError:)`. On either, mark the affected source `lost`, replace its mixer input with silence buffers, compute `at_offset_seconds` from the capture start time, emit a `source_lost` event with reason. Continue the mixer with the remaining input. **[Agent: macos-swift]**
  - [x] Update `supervisor.py` to append an entry to `capture-state.json`'s `warnings` array on each `source_lost` event, and update the source's `status`/`lost_at` fields. **[Agent: python-backend]**
  - [x] Update the `record stop` summary to render warnings (e.g., `"microphone dropped at 02:14 — remainder is system audio only"`) when present. **[Agent: python-backend]**
  - [x] **Verification (manual):** Use a USB mic or AirPods. `record start`. After ~10 s, unplug/disconnect the input mid-capture. Continue another ~10 s with system audio playing. `record stop`. Confirm: summary lists the mic-dropped warning at the right offset; the resulting WAV is playable end-to-end with mic audio in the first half and silence on the mic side after the disconnect; `daemon.log` shows the `source_lost` event. **[Agent: macos-swift]**

## Slice 6 — Robust CLI error paths

Goal: every CLI exit code path from tech spec §2.4 behaves correctly with clear messages.

- [x] **Slice 6: Error paths**
  - [x] `record start` while a capture is already running: confirm PID is alive, exit 1 with `"capture already in progress (PID <n>)"`. **[Agent: python-backend]**
  - [x] `record start` with a stale PID file (PID not alive): silently clean up and proceed normally. **[Agent: python-backend]**
  - [x] `record stop` with no PID file or stale PID file: exit 1 with `"no capture running"`. **[Agent: python-backend]**
  - [x] Permission-denied flow: when supervisor receives `permission_denied` event from the daemon, log it, leave a marker in `capture-state.json`, exit 2 with a user-facing message naming the System Settings panel (e.g., `"microphone permission denied — grant access in System Settings → Privacy & Security → Microphone"`). **[Agent: python-backend]**
  - [x] Binary-not-found / launch failure: if `subprocess.Popen` of `record-capture` raises, exit 3 with a clear message pointing at `make install`. **[Agent: python-backend]**
  - [x] Supervisor did-not-exit-cleanly: in `record stop`, if SIGTERM + wait times out, exit 4 with a message telling the user to inspect `daemon.log`. Force-kill is NOT used — leave the process for the user to inspect. **[Agent: python-backend]**
  - [x] **Verification:** Shell-script through every exit code: (1) start twice in a row → second exits 1; (2) `kill -9` the supervisor then `record stop` → exits 1 with stale-PID message; (3) `rm` the bundled binary then `record start` → exits 3; (4) revoke microphone permission in System Settings then `record start` → exits 2 with the System Settings message; (5) confirm exit codes via `echo $?` after each. **[Agent: python-backend]**

## Slice 7 — Automated tests, CI test mode, and README

Goal: the slice's verification is no longer purely manual. `make test` runs unit + integration tests; the integration test exercises the real Swift binary against a synthetic source.

- [x] **Slice 7: Tests & docs**
  - [x] Add `--test-silent-sources` flag to the Swift binary that bypasses `SCStream` and `AVAudioEngine` and feeds the mixer with deterministic synthetic buffers (1 s silence + 1 s 440 Hz tone, looped). No TCC permissions required. Mode is opt-in via the flag only. **[Agent: macos-swift]**
  - [x] Python unit tests under `tests/python/`: `test_ipc.py` (round-trip every command/event through pydantic; reject malformed lines), `test_state.py` (PID create/stale/cleanup; state-file atomic writes), `test_cli.py` (Typer `CliRunner` against a stub supervisor binary that emits scripted JSON lines; covers all exit codes from slice 6), `test_paths.py`. **[Agent: python-backend]**
  - [x] Swift unit tests under `swift-capture/Tests/`: `ProtocolTests.swift` (Codable round-trip against shared JSON fixtures), `StateFileTests.swift` (atomic write under concurrent dispatch). **[Agent: macos-swift]**
  - [x] Integration test under `tests/integration/test_end_to_end.py`: spawns the real `record-capture` binary with `--test-silent-sources` for ~2 s, verifies the JSON-line event sequence (`ready` → `started` → 2× `source_attached` → `stopped`), opens the resulting WAV with Python's `wave` module and asserts mono/16 kHz/16-bit and duration within ±100 ms. **[Agent: python-backend]**
  - [x] Update `make test` to run `pytest tests/` and `swift test` from `swift-capture/`. **[Agent: general-purpose]**
  - [x] Add a short `README.md` (or update an existing one) with: installation (`make install`), first-run permission flow (Mic + Screen Recording prompts), the manual smoke test (start → talk + play audio → stop → play resulting WAV), troubleshooting (where to find `daemon.log` / `orchestrator.log`, how to check `capture-state.json`). **[Agent: general-purpose]**
  - [x] **Verification:** Run `make test` from a clean checkout — all unit and integration tests pass without requiring any TCC permissions. Manually re-run the slice 4 smoke test from the README's steps and confirm everything still works end-to-end. **[Agent: python-backend]**

---

### Notes on agent assignments / dependencies

| Item | Issue | Recommendation |
|---|---|---|
| Slice 1 sub-task 3 (Makefile) and Slice 7 sub-tasks 5–6 (Makefile + README) | Assigned to `general-purpose` — no dedicated build/docs specialist | Acceptable; these are simple cross-cutting files. No new agent needed. |
| Slice 3 verification, Slice 4 verification, Slice 5 verification | Require macOS TCC permissions (Screen Recording, Microphone) granted on a real Mac with audio devices | Cannot be performed by an agent in CI — must be run on the developer's machine. Slice 7's `--test-silent-sources` integration test gives an automated approximation. |
