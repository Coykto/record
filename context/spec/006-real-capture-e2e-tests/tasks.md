# Tasks — 006 Real-Capture End-to-End Test Harness

- **Functional Specification:** [`./functional-spec.md`](./functional-spec.md)
- **Technical Considerations:** [`./technical-considerations.md`](./technical-considerations.md)

Each slice keeps `make test` green and leaves `make test-real` doing one more useful thing than the previous slice did.

---

### - [x] **Slice 1: Pytest opt-in plumbing is in place (test target exists, default suite is unaffected)**

- [x] Add the `real_capture` marker under `[tool.pytest.ini_options]` in `pyproject.toml`. **[Agent: python-backend]**
- [x] Add `pytest_addoption` (`--run-real-capture` boolean flag) and `pytest_collection_modifyitems` (deselect items marked `real_capture` unless the flag is set) to `tests/integration/conftest.py`. **[Agent: python-backend]**
- [x] Create `tests/integration/test_real_capture.py` with a single stub test marked `@pytest.mark.real_capture` that does nothing but `assert True`. This proves the plumbing works before real capture is wired in. **[Agent: python-backend]**
- [x] Add a `test-real` target to `Makefile` (depending on `swift`) with the prerequisite comment block (BlackHole, SwitchAudioSource, Screen Recording + Microphone TCC). **[Agent: python-backend]**
- [x] **Verify:**
  - [x] `uv run pytest tests` produces the same test count it did before the change (the new file's stub test is deselected). **[Agent: python-backend]**
  - [x] `make test-real` (or `uv run pytest tests/integration/test_real_capture.py --run-real-capture -v`) runs exactly one test and passes. **[Agent: python-backend]**

---

### - [x] **Slice 2: Capture binary reports its TCC permission state via `--check-permissions`**

- [x] Add a `--check-permissions` command-line branch to the Swift capture binary: call `SCShareableContent.current` and `AVCaptureDevice.authorizationStatus(for: .audio)`, emit two JSON-line `{"event":"permission","name":..., "granted":bool}` events on stdout, exit 0. Do **not** call `requestAccess`. **[Agent: macos-swift]**
- [x] Rebuild the binary via `make swift`. **[Agent: macos-swift]**
- [x] **Verify:**
  - Run `src/record/bin/record-capture.app/Contents/MacOS/record-capture --check-permissions` from a shell and confirm exactly two JSON-line events on stdout (`screen_recording` and `microphone`, each with a boolean `granted`). The values reflect the real TCC state — both `true` on a granted machine, `false` or mixed otherwise. **[Agent: macos-swift]**
  - Confirm the production path is unaffected: `make test` still passes. **[Agent: python-backend]**

---

### - [x] **Slice 3: Pre-flight that fails loudly with actionable messages when any prerequisite is missing**

- [x] Create `tests/integration/_real_capture_helpers.py` (leading underscore so pytest does not collect it) and implement: `blackhole_available()`, `switchaudio_available()`, `check_capture_permissions(binary)`, and `assert_prereqs_or_fail(binary)`. The last one calls each check in order and raises via `pytest.fail()` (not `pytest.skip()`) on the first failure, with a message that names the missing prerequisite and quotes the install/grant command verbatim. **[Agent: python-backend]**
- [x] Wire `assert_prereqs_or_fail` as the first call inside `test_real_capture.py`'s stub test. **[Agent: python-backend]**
- [x] **Verify:**
  - With all prereqs satisfied, `make test-real` passes the pre-flight and the stub test still ends green. **[Agent: python-backend]**
  - Temporarily simulate one missing prereq (e.g., point `SwitchAudioSource` lookup at a non-existent name) and confirm the test fails with a message that names the missing prereq and includes the `brew` install command. Revert the simulation. **[Agent: python-backend]**

---

### - [x] **Slice 4: Real-capture end-to-end runs against the production pipeline, files materialize, sandbox is cleaned up**

- [x] Add a `real_capture_sandbox` fixture to `tests/integration/conftest.py` that yields `(sandbox, cwd, env, socket_path, state_path)` — the same boilerplate as `test_end_to_end.py:843–867` but **without** the `RECORD_CAPTURE_TEST_FLAGS` env var. Clean up via `shutil.rmtree(sandbox, ignore_errors=True)` on teardown. **[Agent: python-backend]**
- [x] Add `temporary_input_device(name)` (`@contextmanager`) to `_real_capture_helpers.py`. **[Agent: python-backend]**
- [x] Replace the stub body of `test_real_capture_end_to_end` with the daemon-driven start/stop sequence (reusing `_wait_for_socket`, `_send_control_request`, and the daemon-cleanup `finally` block patterned on `test_end_to_end.py:969–980`). Inside the `temporary_input_device("BlackHole 2ch")` block: spawn daemon, wait for socket, send `start`, sleep ~4 s, send `stop`, send `quit`. Assert both per-source WAVs and the MP4 exist and are well-formed (use stdlib `wave` for the WAVs; `_probe_mp4` for the MP4). **[Agent: python-backend]**
- [x] **Verify:**
  - `make test-real` runs the real capture pipeline end-to-end and passes. The capture binary is invoked without the synthetic flags. **[Agent: python-backend]**
  - After the run, the macOS default input device is the same as it was before the test (check with `SwitchAudioSource -t input -c` before and after). **[Agent: python-backend]**
  - No `rd-*` sandbox directories are left in `/tmp/` after the run. **[Agent: python-backend]**

---

### - [x] **Slice 5: Known audio is played during the capture and the harness asserts the output is non-silent**

- [x] Add `compute_rms_dbfs(wav_path)`, `assert_non_silent(wav_path, threshold_dbfs=-40.0)`, and `play_audio_async(wav_path, device=None)` to `_real_capture_helpers.py`. **[Agent: python-backend]**
- [x] Update `test_real_capture_end_to_end`: after `start` returns, spawn two `afplay` processes via `play_audio_async` (one default output → system-audio path, one `-d "BlackHole 2ch"` → mic path), sleep ~4 s, terminate both in a `finally`, then send `stop` and `quit`. **[Agent: python-backend]**
- [x] Add the content assertions: `assert_non_silent(mic_wav)`, `assert_non_silent(system_wav)`, and an MP4-content assertion (size > ~50 KB and `_probe_mp4` duration > 0.5 s). Failure messages must include the measured dBFS / size / duration. **[Agent: python-backend]**
- [x] **Verify:**
  - `make test-real` passes end-to-end on a fully-set-up machine. **[Agent: python-backend]**
  - **Negative control:** comment out the `afplay` lines locally; rerun `make test-real`; both audio assertions must fail with a message of the form `RMS = -inf dBFS, expected > -40.0 dBFS`. Restore the lines after the demonstration. **[Agent: python-backend]**
  - **Catches the known regression:** if the current `main` still has the system-audio capture regression, the system-audio RMS assertion fails on `make test-real`; once the bug is fixed, it passes. **[Agent: python-backend]**
