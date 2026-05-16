# Functional Specification: Real-Capture End-to-End Test Harness

- **Roadmap Item:** Engineering-quality capability (not on the product roadmap). Adds an integration-test layer that exercises the real macOS capture pipeline end-to-end, to prevent regressions in `SCStream` system audio, `AVAudioEngine` mic capture, and ScreenCaptureKit video that synthetic-mode tests cannot see.
- **Status:** Draft
- **Author:** e

---

## 1. Overview and Rationale (The "Why")

The product's existing automated test suite drives the capture binary in a **synthetic mode** — silent audio sources, a synthesised video frame — so the tests run on any developer's Mac without screen-recording permission, audio hardware, or a virtual audio device. That speed has a hidden cost: every test in the suite bypasses the actual macOS capture pipeline (system-audio capture, microphone capture, screen capture). A recent regression broke real system-audio capture and the synthetic-mode suite reported green throughout. The only thing that surfaced the bug was a developer manually running the product and listening to the resulting file.

This specification adds a second layer of tests that exercise the **real** capture pipeline on a developer's Mac. A known audio signal is played through the operating system while a capture is in progress; once the capture stops, the harness opens the resulting files and asserts they actually contain audio and video — not just that they have the right name and shape on disk.

The audience for this capability is the project's engineers. The harness is invoked explicitly with a dedicated command, runs locally, and never participates in continuous integration: it depends on screen-recording permission, audio hardware, and a virtual audio device that cannot be provisioned in CI.

**Success measure:** A regression that produces a silent or unreadable output file is caught by the real-capture suite on the next local run, before it reaches the main branch. Engineers reach for this command before merging changes that touch capture code.

---

## 2. Functional Requirements (The "What")

### 2.1 A dedicated command to invoke the real-capture suite

- **As an engineer, I want a single command that builds the capture binary and runs the real-capture tests, so that I can verify the real pipeline with one step before I ship a change.**
  - **Acceptance Criteria:**
    - [ ] Running `make test-real` from the repository root builds the capture binary (if it needs building) and then runs the real-capture tests.
    - [ ] The command runs only the real-capture tests. It does not run the rest of the integration suite.
    - [ ] Test output is verbose: the engineer sees each test starting, the prerequisite checks, and a clear pass/fail line per test.
    - [ ] The exit code is zero only if every real-capture test passed.

### 2.2 Default test suite stays unchanged

- **As an engineer, I want the everyday test command to keep behaving exactly as it does today, so that the new heavy tests do not start running by accident on a teammate's machine or in CI.**
  - **Acceptance Criteria:**
    - [ ] Running the existing `make test` target produces the same set of tests it did before this change. The real-capture tests are not included.
    - [ ] Running `pytest` without any extra flags from the repository root does not run the real-capture tests.
    - [ ] The real-capture tests run only when the engineer opts in explicitly (via the `make test-real` target or the underlying opt-in flag it passes to pytest).

### 2.3 Drives the real capture pipeline (not synthetic mode)

- **As an engineer, I want the harness to drive the same capture pipeline that runs in production, so that a regression in the real pipeline cannot hide behind a test-only shortcut.**
  - **Acceptance Criteria:**
    - [ ] The harness starts and stops the capture through the same entry point the daemon uses in production (the daemon's control socket — the same path a user's `record start` / `record stop` invocation takes).
    - [ ] During the test, the capture binary runs against the real macOS audio and screen-capture APIs. No internal test-only flag that substitutes silent or synthetic sources is set.
    - [ ] A change that only affects the production code path (and not any test fixture) can cause the real-capture tests to fail.

### 2.4 A known audio signal is played during the capture

- **As an engineer, I want the harness to play a known audio file through the operating system while the capture is running, so that the resulting output files have something to record.**
  - **Acceptance Criteria:**
    - [ ] While the capture is running, the harness plays a fixed test audio file from the repository (`tests/data/test.wav`) through the operating system.
    - [ ] The audio is played long enough for a non-trivial portion of it to reach the capture pipeline given typical Swift-side startup latency (capture window of roughly four seconds).
    - [ ] Playback is started and stopped by the harness itself; the engineer does not need to interact with the machine while the test runs.

### 2.5 The microphone source is routed from the test audio

- **As an engineer, I want the microphone source to receive the known test audio during the test, so that the microphone output file has predictable, verifiable content instead of whatever noise happens to be in the room.**
  - **Acceptance Criteria:**
    - [ ] For the duration of the test, the macOS system default input device is switched to the project's virtual audio device.
    - [ ] The test audio is played into that same virtual device, so that what the microphone "hears" is the test audio.
    - [ ] After the test finishes — whether it passed, failed, or errored — the macOS system default input device is restored to whatever it was set to before the test ran.

### 2.6 Output files are asserted to contain real content

- **As an engineer, I want the harness to open the recorded files and assert they actually contain audio and video, so that a regression which produces a "valid but silent" file is caught.**
  - **Acceptance Criteria:**
    - [ ] After the capture stops, the microphone output file is opened and its loudness is measured; the test fails if the file is silent (loudness at or below a clear silence threshold). The failure message states the measured loudness value.
    - [ ] After the capture stops, the system-audio output file is opened and its loudness is measured the same way; the test fails the same way if the file is silent.
    - [ ] After the capture stops, the video output file is opened and inspected; the test fails if the file is missing, unreadable, or has a near-zero size or duration. The failure message states what was wrong with the file.
    - [ ] The assertions report the actual measured value in the failure message (for example, the measured loudness in dBFS), so the engineer can see how silent the file was without re-running anything.

### 2.7 Prerequisites are checked, and missing ones fail the run loudly

- **As an engineer, I want missing prerequisites to fail the run with a clear, actionable message, so that I am never misled into thinking "everything passed" on a machine that wasn't actually set up to run the real tests.**
  - **Acceptance Criteria:**
    - [ ] The harness checks, before each test starts, that: the virtual audio device is installed; the input-device-switching tool is available; the capture binary has Screen Recording permission; and the capture binary has Microphone permission.
    - [ ] If any prerequisite is missing when the engineer invokes `make test-real`, the run fails (non-zero exit code) and prints a message that names the missing prerequisite by its plain name and tells the engineer how to install or grant it (for example, "Install BlackHole 2ch with `brew install --cask blackhole-2ch`" or "Grant Screen Recording permission in System Settings → Privacy & Security").
    - [ ] The failure message for a missing prerequisite is distinct from a real test failure: it makes clear the failure is due to setup, not a regression in capture.

### 2.8 Clean teardown, no lingering changes to the machine

- **As an engineer, I want the harness to leave my machine in the state it found it, so that running real-capture tests does not silently change my microphone input or leave temporary files behind.**
  - **Acceptance Criteria:**
    - [ ] When the test ends — pass, fail, or error — the macOS system default input device is the same as it was before the test ran.
    - [ ] When the test ends — pass, fail, or error — the helper that was playing the test audio is stopped, even if the test itself failed mid-way.
    - [ ] No temporary capture files (sandbox directory, sockets, state files) are left behind on disk after the run finishes.

### 2.9 Prerequisites are discoverable from the project itself

- **As an engineer new to the project, I want to discover what I need to install to run the real-capture tests without having to read source code, so that I can set up my machine in one sitting.**
  - **Acceptance Criteria:**
    - [ ] The make target for the real-capture tests carries a comment that lists the prerequisites (virtual audio device, input-switching tool, Screen Recording + Microphone permission for the capture binary) and the one-line install/grant instruction for each.
    - [ ] The prerequisite messages printed at test time (see 2.7) match the same names and install instructions documented at the make target.

---

## 3. Scope and Boundaries

### In-Scope

- A new local test command (`make test-real`) that builds the capture binary and runs the real-capture tests.
- A real-capture test that drives the daemon control socket end-to-end, plays a known audio file through the operating system during the capture, and asserts the resulting microphone, system-audio, and video output files contain real content (audible audio, decodable video of non-trivial size and duration).
- Microphone-side routing of the known test audio via a virtual audio device, with the macOS system default input switched for the duration of the test and restored afterwards.
- Prerequisite checks (virtual audio device installed, input-switching tool available, Screen Recording permission, Microphone permission) that fail the run loudly with actionable install/grant messages when missing.
- A pytest opt-in mechanism (marker + flag) so the default test command continues to behave exactly as it did before this change.
- A short, point-of-use note at the make target listing the install / grant steps for the prerequisites.

### Out-of-Scope

The following are explicitly NOT part of this specification:

- **End-to-end hotkey testing.** Triggering the global hotkey from a test requires Accessibility permission for the test runner; the hotkey handler reaches the same capture entry point this harness already exercises, so it is left to a future, separate effort.
- **Continuous integration.** The harness is designed for local use only. It depends on Screen Recording, Microphone, audio hardware, and a virtual audio device — none of which are available in the project's CI environment.
- **Asserting the captured video frames contain a known image.** The video assertion verifies the file decodes, is non-trivially sized, and has a duration that matches the capture window. It does not verify *what* is on screen, and will not catch "video is all black" regressions.
- **Transcribing the captured audio and grepping the transcript** as an even stronger "audio contains intelligible content" check. Noted as a possible follow-up; not in this specification.
- **Removing the existing synthetic-mode integration tests.** They continue to run in `make test` and are not replaced by this work.
- **Refactoring the sandbox-setup boilerplate** that is currently duplicated across the three existing daemon-driven integration tests. A separate cleanup; out of scope here.
- **Anything from other roadmap items** (active meeting window video capture, meeting auto-detection, voice-based diarization, per-speaker transcript tracks, live transcript stream, app-managed library, transcript search, on-device transcription, on-device diarization). Each is covered by its own existing or future specification.
