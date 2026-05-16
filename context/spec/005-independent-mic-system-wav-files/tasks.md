# Tasks: Independent Mic and System Audio WAV Files

- **Functional Specification:** `context/spec/005-independent-mic-system-wav-files/functional-spec.md`
- **Technical Considerations:** `context/spec/005-independent-mic-system-wav-files/technical-considerations.md`

---

## Slice 1: Protocol foundations (additive — no behavior change)

Adds the new `audio_file` event variant on both sides of the seam and makes `WAVWriter.close()` idempotent, without changing any existing behavior. The app keeps producing the mixed WAV from spec 001 throughout this slice.

  - [x] Add `Event.audioFile(path:source:durationSeconds:status:truncatedAtOffsetSeconds:)` case to `swift-capture/Sources/RecordCapture/Protocol.swift` (alongside the existing `videoFile` variant). Not emitted by any code path yet. **[Agent: macos-swift]**
  - [x] Add `AudioFileEvent` pydantic model to `src/record/ipc.py` and register it in the event union next to `VideoFileEvent`. **[Agent: python-backend]**
  - [x] Add a `closed` flag to `WAVWriter` so `close()` is a no-op on second call. **[Agent: macos-swift]**
  - [x] Swift unit test (`swift-capture/Tests/ProtocolTests.swift`): Codable round-trip for the new `audio_file` event using fixture JSON for each of the three `status` values. **[Agent: macos-swift]**
  - [x] Swift unit test (`swift-capture/Tests/WAVWriterTests.swift`, create if absent): instantiate two `WAVWriter`s pointing at different temp URLs, write a buffer to each, close each twice; verify both files exist with valid mono / 16-bit / 16 kHz PCM headers and double-close did not throw. **[Agent: macos-swift]**
  - [x] Python unit test (`tests/python/test_ipc.py`): pydantic round-trip of `AudioFileEvent`; reject unknown `status` values. **[Agent: python-backend]**
  - [x] Verification: run `make swift test` and `pytest tests/python` — both pass; nothing in app behavior changed. Existing integration test still produces a mixed `.wav`. **[Agent: general-purpose]**

---

## Slice 2: Two independent writers — the atomic flip

This is the heart of the change: drop the mixer, instantiate two writers, rename `output_path` to a basename concept end-to-end, and emit `audio_file` events on stop. After this slice, `record start` produces `<basename>-mic.wav` and `<basename>-system.wav` and no mixed file. **No new behaviors for mid-capture loss or silent-source detection yet — those land in slices 3 and 4.**

  - [x] In `swift-capture/Sources/RecordCapture/AudioCapture.swift`: rename `init(outputURL:)` to `init(basename:)`, derive the two output URLs by appending `-mic.wav` and `-system.wav`, replace the single `wavWriter` field with optional `micWriter` and `systemWriter` fields. **[Agent: macos-swift]**
  - [x] In the same file: rename `drainAndMix(finalFlush:)` to `drainAndWrite(finalFlush:)` and remove the `Int16(clamping: s + m)` summing step. Each ring queue's drained samples write directly to its own writer; no zero-padding of "the shorter side". **[Agent: macos-swift]**
  - [x] In the same file: when `stop()` runs, drain both queues to their writers, close both writers, and emit one `audio_file` event per finalized writer with `status="captured_normally"` (loss/silence statuses are wired in later slices). **[Agent: macos-swift]**
  - [x] In `swift-capture/Sources/RecordCapture/Protocol.swift`: rename `Event.stopped`'s `outputPath` parameter to `basename` (JSON key `basename`). Update the doc-comment on `Command.start.outputPath` to state it is now an absolute basename without extension. **[Agent: macos-swift]**
  - [x] In `swift-capture/Sources/RecordCapture/main.swift`: rename `CaptureState.outputPath` to `basename`, pass it to `AudioCapture(basename:)`, and have the stop emit go: per-finalized-file `audio_file` events → final `stopped` event with the basename. **[Agent: macos-swift]**
  - [x] In `src/record/ipc.py`: rename `StoppedEvent.output_path` to `basename` (with matching JSON alias). Update `StartCommand.output_path`'s docstring to say "absolute basename without extension". **[Agent: python-backend]**
  - [x] In `src/record/capture.py`: rename `CaptureSession.__init__`'s `output_path` parameter to `basename`. Update the initial state pre-population to populate `state["audio_files"]["mic"]` and `state["audio_files"]["system_audio"]` with derived paths and `status="pending"`, and store the basename. Update `_apply_event` to handle `AudioFileEvent` (writes into `state["audio_files"][event.source]`) and to set `state["basename"]` from `StoppedEvent`. **[Agent: python-backend]**
  - [x] In `src/record/supervisor.py`: rename the `output_path` parameter on `run()` and `_run_session()` to `basename`; rename the developer-facing `--output-path` CLI flag to `--basename`. **[Agent: python-backend]**
  - [x] In `src/record/cli.py` (`record start`): compute `$CWD/<ISO-8601-timestamp>` (no extension) as the basename and pass it through to the supervisor. **[Agent: python-backend]**
  - [x] In `src/record/cli.py` (`_print_stop_summary`): replace the single `output:` line with two lines pulled from `state["audio_files"]` — one for `mic` and one for `system_audio` — each rendered as `<source>: <abs path>   <duration>   <human status>` (with `(not produced)` fallback). **[Agent: python-backend]**
  - [x] Add an integration test (`tests/integration/test_two_track_audio.py`) using the existing `--test-silent-sources` synthetic-source mode but extended to inject a 440 Hz tone on the mic source and an 880 Hz tone on the system source. Assert: both files exist at the derived paths; the mic file's FFT peak is at 440 Hz with no peak near 880 (and vice versa for the system file); each duration is within ±100 ms of the requested length; the stdout event stream contains exactly two `audio_file` events followed by `stopped` with the basename. **[Agent: python-backend]**
  - [x] Update existing tests broken by the renames: `tests/python/test_ipc.py`, `tests/python/test_capture.py` (if present), `tests/python/test_cli.py`. **[Agent: python-backend]**
  - [x] Verification: run `make swift test`, `pytest tests/python`, `pytest tests/integration`. Then run `record start` from a scratch directory, talk for ~10 seconds, run `record stop`. Verify the working directory contains exactly two WAVs named `<timestamp>-mic.wav` and `<timestamp>-system.wav`, that the stop summary lists both lines with `captured normally` status, and that `daemon.log` shows two `audio_file` events. **[Agent: general-purpose]**

---

## Slice 3: Mid-capture source-loss truncation

Wires the `truncated_at_offset` status path so a failed source's file is finalized at the failure point and the surviving file keeps running.

  - [x] In `AudioCapture.swift`: when an `AVAudioEngine` config-change error or `SCStream` delegate error occurs, drain the affected queue, call `close()` on the corresponding writer, and remember the offset-from-start at which the failure occurred. The surviving writer is unaffected. **[Agent: macos-swift]**
  - [x] On stop, emit the failed source's `audio_file` event with `status="truncated_at_offset"` and `truncatedAtOffsetSeconds` set; the surviving source's event remains `status="captured_normally"`. **[Agent: macos-swift]**
  - [x] In `src/record/cli.py`: human-readable mapping for `truncated_at_offset` → `"truncated at MM:SS — file ends there"` in `_print_stop_summary`. **[Agent: python-backend]**
  - [x] Integration test (`tests/integration/test_two_track_audio.py`): extend the synthetic mode with a flag/event to inject a `source_lost` for mic at offset T (e.g., 3 seconds into a 6-second capture). Assert: mic WAV duration ≈ T (±100 ms); system WAV duration ≈ session length; the mic `audio_file` event has `status="truncated_at_offset"` with `truncated_at_offset_seconds ≈ T`; both files are valid playable WAVs. **[Agent: python-backend]**
  - [x] Verification: run the new integration test plus the existing suite. **[Agent: general-purpose]**

---

## Slice 4: Silent-source detection

Ensure a source that produces no audible audio for the entire session still yields a session-duration silent WAV, and that this state is reported as `status="silent_throughout"` rather than `"captured_normally"`.

  - [x] In `AudioCapture.swift`: track per-source whether any non-zero sample has been observed (cheap running OR over drained Int16 buffers). On stop, choose `status` per source: `"silent_throughout"` if no non-zero samples were ever observed, otherwise the existing status. **[Agent: macos-swift]**
  - [x] In `src/record/cli.py`: human-readable mapping for `silent_throughout` → `"silent throughout"` in `_print_stop_summary`. **[Agent: python-backend]**
  - [x] Integration test (`tests/integration/test_two_track_audio.py`): synthetic mode feeds all-zero buffers on the mic source while the system source plays the 880 Hz tone. Assert: mic file's duration equals the session length, mic `audio_file` event has `status="silent_throughout"`, mic file plays back as silence end-to-end (no truncation), system file still has its 880 Hz peak. **[Agent: python-backend]**
  - [x] Verification: run the new integration test plus the existing suite. **[Agent: general-purpose]**

---

## Slice 5: Spec 001 housekeeping and end-to-end smoke

  - [x] Edit `context/spec/001-mixed-mic-system-audio-capture/functional-spec.md`: change `Status: Draft` to `Status: Superseded` and add a `**Superseded by:** [005 — Independent Mic and System Audio WAV Files](../005-independent-mic-system-wav-files/functional-spec.md)` line below the header. **[Agent: general-purpose]**
  - [x] `git grep` for stale references to the old single-output behavior in `README.md`, `docs/`, and any test fixtures that reference `<timestamp>.wav` without a `-mic`/`-system` suffix. Update or remove. **[Agent: general-purpose]**
  - [ ] Manual smoke test on the developer machine: start a real meeting in Zoom/Meet (any meeting app that plays audio), wear headphones to avoid acoustic feedback, run `record start`, speak for ~30 seconds while remote audio plays, run `record stop`. Verify: two WAVs in `$CWD` named correctly; the mic WAV contains only the user's voice when played back; the system WAV contains only what the meeting app played; stop summary lists both lines with `captured normally`. **[Agent: general-purpose]**
