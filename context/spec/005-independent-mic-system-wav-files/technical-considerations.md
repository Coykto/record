# Technical Specification: Independent Mic and System Audio WAV Files

- **Functional Specification:** `context/spec/005-independent-mic-system-wav-files/functional-spec.md`
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

This spec changes the audio capture pipeline from **one mixed `.wav` writer** to **two independent per-source `.wav` writers**, with no shared writer and no mixing step. The Swift binary already runs two independent capture pipelines (mic via `AVAudioEngine`, system audio via `SCStream`) that feed two independent ring buffers with their own format converters — today they're combined by a software sum-and-clamp step in `AudioCapture.drainAndMix(…)` before reaching the single `WAVWriter`. The change is to delete that mixing step, instantiate `WAVWriter` twice (one per source), and route each ring buffer's drained samples directly to its own writer.

The wire protocol carries a single `output_path` field today. The user-approved design is to keep that field as a **basename without extension** (e.g. `/abs/path/2026-05-15T14-32-08`); the Swift binary derives `<basename>-mic.wav` and `<basename>-system.wav` from it. The orchestrator no longer constructs final filenames itself — it constructs the basename and lets the capture backend own the suffix convention. This keeps the protocol footprint small and the orchestrator agnostic about how many files a capture session produces.

The Python orchestrator's `CaptureSession` and `record stop` summary are updated to track and render **two output paths** instead of one. Spec 001's behavior is fully removed; the mixer code is deleted (not gated behind a flag — the functional spec replaces 001 outright).

**Carried over from spec 001 unchanged:** the JSON-line protocol shape (commands/events list), the supervisor + PID-file model, the structured-log destinations, the permission flow, the single-instance enforcement, the Makefile build flow, and the macOS 13.0+ floor. The single new event is `audio_file` (one per produced file), analogous to the existing `video_file`.

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 Swift binary changes

**File: `swift-capture/Sources/RecordCapture/AudioCapture.swift`**

| Existing element | Change |
|---|---|
| `private var wavWriter: WAVWriter` (around line 98) | Replace with `private var micWriter: WAVWriter?` and `private var systemWriter: WAVWriter?`. Both optional so a failed source closes its writer without touching the other. |
| `init(outputURL:…)` | Replace `outputURL: URL` with `basename: URL` (no extension). Inside the init, build the two output URLs by appending `-mic.wav` and `-system.wav` to the basename. Instantiate both writers from those URLs. |
| `drainAndMix(finalFlush:)` (lines ~724-776) | Rename to `drainAndWrite(finalFlush:)`. Drop the sum-and-clamp step entirely. Drain each ring queue independently: write each drained mic buffer to `micWriter`, each drained system buffer to `systemWriter`. No zero-padding of "the shorter side" — each writer advances on its own source's samples only. |
| `handleMicBuffer(_:)` / `handleSystemBuffer(_:)` (lines ~544-557) | Unchanged. They still enqueue into their respective ring buffers. |
| Mid-capture source-lost handling (`AVAudioEngine` config change, `SCStream` delegate error) | When a source's pipeline errors out: emit `source_lost` event (as today), drain whatever's left in that source's ring queue to its writer, then call `micWriter?.close()` / `systemWriter?.close()` for the failed side and set the field to `nil`. The surviving writer continues. |
| `stop()` final flush | Drain both queues to their respective writers, then close whichever writers are still open. Emit one `audio_file` event per file that was actually written. |

**File: `swift-capture/Sources/RecordCapture/WAVWriter.swift`**

No public-API changes. Format stays mono / 16-bit / 16 kHz / interleaved Int16 PCM as documented at `WAVWriter.swift:41-49`. The class is already per-instance with its own internal serial queue (`WAVWriter.swift:35`), so instantiating it twice is safe with no further work.

**File: `swift-capture/Sources/RecordCapture/Protocol.swift`**

| Existing element | Change |
|---|---|
| `Command.start.outputPath: String` (line ~63) | Semantics changed: this is now a **basename without extension** (e.g. `/abs/2026-05-15T14-32-08`). Field name and JSON key (`output_path`) unchanged. The doc-comment on the struct documents the new semantics: "absolute path basename; the daemon appends `-mic.wav` and `-system.wav`". |
| `Event.stopped(durationSeconds: Double, outputPath: String)` (line ~182) | Replace `outputPath` parameter with `basename: String`. JSON key becomes `basename`. Carries the basename only; per-file paths are reported separately via `audio_file` events. |
| **New** `Event.audioFile(path: String, source: SourceKind, durationSeconds: Double, status: String)` | New variant, analogous to the existing `videoFile` (line ~186). Emitted once per finalized WAV. `source` reuses the existing `SourceKind` enum (`mic` / `system_audio` — already at line 154). `status` is one of `"captured_normally"`, `"silent_throughout"`, `"truncated_at_offset"`. When `status == "truncated_at_offset"`, the event also carries `truncatedAtOffsetSeconds: Double?`. |

**File: `swift-capture/Sources/RecordCapture/main.swift`**

| Existing element | Change |
|---|---|
| `CaptureState.outputPath: String` (line ~275) | Rename to `basename`. Carried through to the `stopped` event. |
| `handleStart(_:)` (line ~361) | Instead of constructing one `outputURL`, construct the basename URL and pass it to `AudioCapture(basename:…)`. Track the two derived filenames in `CaptureState` so the stop summary can include them in the per-file `audio_file` events. |
| Stop emit (line ~686) | After draining, emit one `audio_file` event per finalized writer (sourced from the per-source status the new `AudioCapture` exposes), then emit `stopped` with the basename and the overall session duration. |

### 2.2 Python orchestrator changes

**File: `src/record/ipc.py`**

| Existing model | Change |
|---|---|
| `StartCommand.output_path: str` (line ~56) | Documented semantics changed to "absolute basename without extension". No field rename needed (preserves the existing IPC byte-shape decision). |
| `StoppedEvent.output_path: str` (line ~204) | Renamed field to `basename`. JSON alias updated to `basename`. |
| **New** `AudioFileEvent(path: str, source: Literal["mic", "system_audio"], duration_seconds: float, status: Literal["captured_normally", "silent_throughout", "truncated_at_offset"], truncated_at_offset_seconds: Optional[float])` | Mirrors the new Swift event. Registered in the event union alongside `VideoFileEvent`. |

**File: `src/record/capture.py`**

| Existing element | Change |
|---|---|
| `CaptureSession.__init__(…, output_path: Path)` (line ~1122) | Replace `output_path: Path` with `basename: Path` (no `.wav` suffix). Caller responsibility: pass `/abs/path/<timestamp>`, not `<timestamp>.wav`. |
| Initial state pre-population (line ~1289) | Replace the `output_path` key with two keys: `mic_path` and `system_path`, computed as `basename.with_name(basename.name + "-mic.wav")` and `…+ "-system.wav"`. Also store `basename` itself for the summary. Pre-populate each path's `status` as `pending`. |
| Send `ipc.StartCommand(output_path=…, …)` (line ~1314) | Send the basename string (no extension). |
| `_apply_event` for `StoppedEvent` (line ~475) | Map the renamed field: `current["basename"] = event.basename`. The per-file paths are populated by `AudioFileEvent` handling. |
| **New** `_apply_event` branch for `AudioFileEvent` | For each `AudioFileEvent`, store `path`, `duration_seconds`, `status`, and `truncated_at_offset_seconds` under `current["audio_files"][event.source]`. |

**File: `src/record/cli.py`**

| Existing element | Change |
|---|---|
| `_print_stop_summary` (line ~252) | Replace the single `output: {output_path}` line with two lines, one per audio file. For each of `mic` and `system_audio`, render `<source>: <abs path>   <duration>   <status>` — where `status` is the human-readable mapping of the protocol status (`"captured normally"`, `"silent throughout"`, `"truncated at 02:14 — file ends there"`). Falls back to `"(not produced)"` if the entry is missing from state. Existing `video_line` rendering is unchanged. |

**File: `src/record/supervisor.py`**

| Existing element | Change |
|---|---|
| `run(output_path: Path, …)` / `_run_session(output_path, …)` (lines ~39, ~131) | Rename parameter to `basename: Path`. |
| CLI flag `--output-path` (line ~201) | Rename to `--basename` (developer-facing flag; not user-visible — the user-facing `record start` constructs it internally). |

**File: `src/record/cli.py` (the `record start` command itself)**

| Existing element | Change |
|---|---|
| Computation of the WAV output path (currently `$CWD/<timestamp>.wav`) | Compute basename as `$CWD/<timestamp>` (no extension). Pass it to the supervisor. |

### 2.3 IPC protocol — final shape after this spec

This is the new shape for the audio-related slice of the protocol. Existing video and lifecycle events are unchanged from spec 002.

**Commands (orchestrator → daemon):**

| Command | Payload (changed fields bold) |
|---|---|
| `start` | `{"cmd":"start","output_path":"<abs basename, no extension>","format":{…},"video_output_path":"…"}` — semantics of `output_path` is now "basename". |
| `stop` | `{"cmd":"stop"}` (unchanged) |
| `shutdown` | `{"cmd":"shutdown"}` (unchanged) |

**Events (daemon → orchestrator):**

| Event | Payload |
|---|---|
| `audio_file` *(new)* | `{"event":"audio_file","path":"…-mic.wav","source":"mic","duration_seconds":312.4,"status":"captured_normally","truncated_at_offset_seconds":null}` (one event per file produced). |
| `stopped` | `{"event":"stopped","duration_seconds":…,"basename":"<abs basename>"}` — `output_path` renamed to `basename`. |
| All other events (`ready`, `permission_required`, `permission_denied`, `started`, `source_attached`, `source_lost`, `error`, `video_file`) | Unchanged. |

### 2.4 State file (`capture-state.json`) updates

Updates to the shape documented in spec 001 §2.6:

- Top-level `output_path` is renamed to `basename`.
- New top-level `audio_files` object:
  ```
  "audio_files": {
    "mic":          {"path":"…-mic.wav",          "status":"pending|captured_normally|silent_throughout|truncated_at_offset", "duration_seconds":…, "truncated_at_offset_seconds":…},
    "system_audio": {"path":"…-system.wav",       "status":"…",                                                              "duration_seconds":…, "truncated_at_offset_seconds":…}
  }
  ```
- Existing `sources` block (per-source attach/lost tracking from spec 001) is kept as-is — it tracks lifecycle independent of the file-finalization status.

### 2.5 Spec 001 housekeeping

- Add a status note to the top of `context/spec/001-mixed-mic-system-audio-capture/functional-spec.md`: `**Superseded by:** [005 — Independent Mic and System Audio WAV Files](../005-independent-mic-system-wav-files/functional-spec.md)`.
- Mark spec 001's status `Superseded` (replacing `Draft`).
- No code is preserved from the mixed-audio path: `drainAndMix`'s sum-and-clamp step is deleted, not gated.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **No new external services.** No protocol additions beyond the single new `audio_file` event.
- **macOS 13.0+** floor unchanged.
- **TCC permissions** unchanged (Microphone + Screen Recording).
- **Transcription pipeline (spec 004)** depends on the audio file path coming out of capture. Today it reads a single `output_path`; it will need to read **two paths** from the new `audio_files` map. Audit `src/record/transcribe.py` and any caller in `supervisor.py` for the `output_path` lookup and update them to iterate over `audio_files`. Two transcripts will be produced (one per file) — out of scope for this spec, called out so the transcription spec's owner can plan.
- **Existing tests** under `tests/python/test_ipc.py`, `tests/python/test_capture.py` (if present), `tests/python/test_cli.py` will break on the field renames and need coordinated updates.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Mic-side echoes the system audio (room playback bleeds into the mic) — even though the files are independent, the mic file now contains audible system audio. | Acceptable for v1 and documented in the spec README as a "use headphones" expectation. Hardware echo cancellation is the user's responsibility (built into modern meeting clients but not into the OS audio pickup). Not a regression vs. the mixed pipeline — the same content was always going to mic. |
| The two files drift in playback when one source's clock is slightly different from the other (e.g., USB mic at 48000 nominal but 47998 effective vs. system audio at 48000). | Each writer is driven by its own source's sample clock; over a 1-hour capture, a 50 ppm drift = ~180 ms offset. Acceptable for transcription (segments don't need sub-second cross-file alignment). Calling this out under §2 of the functional spec ("Sample-accurate cross-file synchronization is not promised"). |
| Disk usage doubles roughly vs. spec 001 (two mono 16k files instead of one mono 16k file ≈ 2 × ~2 MB/min = ~4 MB/min). | Acceptable for daily use; well under the ~10 MB/min budget the architecture doc cites for the v1 WAV choice. No mitigation. |
| A bug in the new per-source close-on-lost path could double-close `WAVWriter` (close once on lost, again on stop). | `WAVWriter.close()` already serializes on its internal queue (`WAVWriter.swift:35`); idempotency via a `closed` flag inside the writer is a small targeted change (see Testing §4). |
| The downstream transcription pipeline silently picks up only one file because of an incomplete update to `transcribe.py`. | Add a sanity check in `_print_stop_summary` that both `mic_path` and `system_path` are present (or report "(not produced)" explicitly). Transcription pipeline integration is owned by spec 004 — capture-side keeps the events crisp and the state file authoritative. |
| Protocol overload: keeping the `output_path` JSON key but redefining it from "full path" to "basename" is a semantic-only break that's invisible to readers. | Update the field's doc-comment on **both** the Swift `Command` struct and the Python pydantic model. Add an integration-test assertion that the daemon refuses a path ending in `.wav` (emits an `error` event with a clear message) so a stale caller fails loudly instead of writing `<basename>.wav-mic.wav`. |
| Spec 001's code path may have lingering callers (e.g., docs, READMEs, fixtures) referencing the old single output filename. | `git grep` for `output_path`, `.wav` (in fixtures), and `_print_stop_summary`'s old format string as part of the implementation pass. |

---

## 4. Testing Strategy

### Unit tests (Python — `tests/python/`)

- **`test_ipc.py`** — round-trip the renamed `StoppedEvent.basename` and the new `AudioFileEvent` through pydantic; reject the old `output_path` key on `StoppedEvent` (must error rather than silently default).
- **`test_capture.py`** (or whatever currently covers `CaptureSession`) — verify `CaptureSession.__init__` takes a basename, and that applying an `AudioFileEvent` populates `state["audio_files"][source]` with the expected shape.
- **`test_cli.py`** — verify `_print_stop_summary` renders two lines (mic + system_audio) with path / duration / status; covers `silent_throughout` and `truncated_at_offset` mappings to human text.

### Unit tests (Swift — `swift-capture/Tests/`)

- **`ProtocolTests.swift`** — `Codable` round-trip for the new `audio_file` event and the renamed `stopped` field; shared JSON fixtures with `test_ipc.py`.
- **`WAVWriterTests.swift`** (extend if exists, otherwise new) — two writers writing to different URLs in the same test run produce two independent files of the expected header (mono / 16-bit / 16 kHz / PCM) with no truncation; calling `close()` twice on the same writer is a no-op.

### Integration tests (`tests/integration/`)

- Extend the existing `--test-silent-sources` synthetic-source mode from spec 001 to feed two different deterministic patterns (e.g., 440 Hz on mic, 880 Hz on system audio). Verify:
  - Two output files exist at the expected derived paths (`<basename>-mic.wav`, `<basename>-system.wav`).
  - The mic file contains only the 440 Hz tone (FFT peak at 440); the system file contains only the 880 Hz tone (FFT peak at 880). **No cross-talk.** This is the central correctness assertion for the "no merging" requirement.
  - Each file's duration is within ±100 ms of the requested capture duration.
  - The event sequence on stdout contains exactly two `audio_file` events (one per source), each followed by `stopped` with the basename.
- Add a mid-capture source-loss test: inject a `source_lost` for mic at offset T. Assert the mic file's duration ≈ T, the system file's duration ≈ session length, the `audio_file` event for mic has `status="truncated_at_offset"` and `truncated_at_offset_seconds≈T`, and the file is still a valid playable WAV.
- Add a silent-source test: feed mic with synthetic zero buffers throughout. Assert the mic file's duration equals the session length, its `audio_file` event has `status="silent_throughout"`, and the WAV is playable end-to-end (silence-only).

### Manual smoke test (documented in spec README)

- Real terminal, real meeting app playing audio, real microphone, headphones on (to avoid acoustic feedback).
- `record start` → talk + system audio for ~30 s → `record stop`.
- Verify both `<timestamp>-mic.wav` and `<timestamp>-system.wav` are produced. The mic file should contain only the user's voice; the system file should contain only what the meeting app played.
- Verify daemon.log shows two `audio_file` events. Verify the stop summary lists both files with `captured_normally` status.
