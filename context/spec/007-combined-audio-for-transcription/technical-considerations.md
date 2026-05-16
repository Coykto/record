# Technical Specification: Combined Mic + System Audio File for Transcription

- **Functional Specification:** `context/spec/007-combined-audio-for-transcription/functional-spec.md`
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

The Python orchestrator gains a stop-time **combine step** that takes the two finalized source WAV files emitted by the Swift capture backend and produces a single mono WAV at `<output_folder>/<timestamp>.wav`. The combine step runs entirely in the orchestrator — the Swift backend is unchanged. After the combine succeeds, the orchestrator hands the **combined file (only)** to the existing single-file `TranscriptionBackend.transcribe` call. The two source WAVs remain on disk, untouched.

The combine is implemented as a pure function in a new module `src/record/combine.py` using the standard-library `wave` package and integer arithmetic — no new third-party dependency. Both source WAVs are already produced by `swift-capture/Sources/RecordCapture/WAVWriter.swift` in a fixed format (int16 little-endian, mono, 16 kHz, interleaved), so the mix is a streaming sample-pair sum with int16 saturation; the shorter stream is zero-padded to match the longer one.

Stop-time coordination is unchanged in shape: `record stop` is already blocking through finalize because `Daemon._handle_stop` waits for `session.stop()` (which itself waits for the Swift `stopped` event) before replying on the control socket. The combine call slots into `_handle_stop` between `session.stop()` and the transcription spawn loop, inheriting that blocking guarantee. The same insertion is made in `_watch_for_system_event_stop` for the auto-stop path.

The session's `capture-state.json` gains a `combined_audio` field with `path`, `status`, and either `duration_seconds` (on success) or `reason` (on failure). The CLI's `_print_stop_summary` reads it and appends one extra line to the existing summary.

Transcription wiring changes from "one job per source file" to "one job against the combined file"; when the combine step records `status="failed"`, transcription is skipped entirely for that session.

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 New module: `src/record/combine.py`

A single pure function with a small result dataclass. No I/O beyond the three file paths it is given. No logger calls — failure is signaled by raising; the caller (daemon) logs.

| Symbol | Responsibility |
|---|---|
| `CombineResult` (dataclass) | Fields: `path: Path`, `duration_seconds: float`. Returned only on success. |
| `class CombineError(RuntimeError)` | Raised on any failure (missing source, format mismatch, write error). Message is a short, user-facing plain-language reason suitable for the stop summary. |
| `combine_wavs(mic_path: Path, system_path: Path, output_path: Path) -> CombineResult` | Opens both inputs via stdlib `wave.open`, validates each is int16-LE / mono / 16 kHz (rejects with `CombineError` otherwise), opens `output_path` for writing in the same format, and streams a chunked sum-and-clamp to the output. The output duration equals the longer source; the shorter source is zero-padded. |

Mix algorithm (inside `combine_wavs`):

- Block size: 16 000 frames per chunk (1 second at 16 kHz). One chunk per source per iteration.
- Per chunk: convert the `bytes` block into a sequence of int16 samples via `array.array("h", block)`, pad the shorter side with zeros up to chunk length, sum element-wise, clamp each sum to `[-32768, 32767]`, pack back to bytes via `array.array("h", summed).tobytes()`, and `writeframesraw` to the output. End-of-file on a side switches that side to a zero generator for the rest of the loop.
- Sample rate / channels / sample width on the output match the inputs (16 kHz / 1 / 2 bytes). No resampling, no channel mixing — sources are already mono.

Format validation rejects any source that fails any of: `getnchannels() == 1`, `getsampwidth() == 2`, `getframerate() == 16000`. (Defensive only — Swift fixes these — but a mismatch should fail the combine cleanly rather than corrupt the output silently.) The output file is written first to `<output_path>.tmp` and then `os.replace`'d to `<output_path>` so a partial file can never be left behind on a mid-write crash.

### 2.2 Stop-flow integration: `src/record/daemon.py`

Two code paths handle stop today and both must be updated identically: `Daemon._handle_stop` (user-initiated, `daemon.py:640-707`) and `Daemon._watch_for_system_event_stop` (system-event-triggered auto-stop, `daemon.py:368-415`). Both call `await session.stop()`, then iterate `final["audio_files"]` to spawn one transcription per source.

The new shape in each:

1. `final = await session.stop()` — unchanged.
2. **(new)** Compute `combined_path = output_folder / f"{timestamp}.wav"` (timestamp already lives in `final` / `session` — see `CaptureSession.start` at `capture.py:1312-1316`).
3. **(new)** Resolve mic and system source paths from `final["audio_files"]["mic"]["path"]` and `final["audio_files"]["system_audio"]["path"]`. If either source file's `status` field already indicates it was never produced (i.e., not present on disk at all), skip step 4 and record a `combined_audio` failure with reason "one or more source files unavailable".
4. **(new)** Run the combine off the event loop — `result = await asyncio.to_thread(combine.combine_wavs, mic_path, system_path, combined_path)`. On success, persist `state["combined_audio"] = {"path": str(combined_path), "status": "produced", "duration_seconds": result.duration_seconds}`. On `CombineError`, persist `state["combined_audio"] = {"path": str(combined_path), "status": "failed", "reason": str(exc)}` and log via `self._log.error("combine_failed", mic_path=..., system_path=..., combined_path=..., reason=str(exc), error_type=type(exc).__name__)`. Bracket the call with a `_log.info("combine_started", ...)` / `_log.info("combine_complete", duration_seconds=...)` pair to match the `transcription_request` / `transcription_complete` pattern in `transcribe.py:186, 233`.
5. **(changed)** Transcription spawn loop is replaced by: if `state["combined_audio"]["status"] == "produced"`, call `self._spawn_transcription(Path(state["combined_audio"]["path"]))` exactly once. Otherwise log `transcription_skipped` with `reason="combine_failed"` and do nothing.
6. Persist `capture-state.json` (the existing write site at `capture.py:1509-1511` and the daemon's IDLE-state rewrite) including the new `combined_audio` field, so the CLI side can render the summary line.
7. Reply on the control socket as today.

The combine call sits **before** the socket reply on both paths, so `record stop` continues to block until the combined file is finalized (or has been determined to have failed) — satisfying functional spec §2.5.

A bounded timeout wraps the `to_thread` call (e.g., `asyncio.wait_for(..., timeout=300.0)`) so a stuck combine cannot wedge the daemon. On timeout, the call is treated as a `CombineError("combine timed out")`.

### 2.3 Capture-state schema extension

`capture-state.json` gains a single new top-level key emitted by the daemon (the Swift backend does not write it):

| Field | Type | When present | Notes |
|---|---|---|---|
| `combined_audio.path` | string (abs path) | Always after stop | The intended output path. Present even on failure so the CLI can name the file. |
| `combined_audio.status` | `"produced"` \| `"failed"` | Always after stop | Drives the summary text and the transcription gate. |
| `combined_audio.duration_seconds` | float | When `status == "produced"` | Reported in the summary. |
| `combined_audio.reason` | string | When `status == "failed"` | Short user-facing plain-language sentence (e.g., `"disk full"`, `"one or more source files unavailable"`, `"combine timed out"`). |

No migration needed — this is an additive field on an ephemeral state file. Readers (the CLI summary) treat missing `combined_audio` as a pre-feature state and fall back to current behavior.

### 2.4 Stop-summary rendering: `src/record/cli.py`

`_print_stop_summary` (cli.py:277-304) is extended:

- The existing per-source loop (`cli.py:289-299` iterating `("mic", "system_audio")`) is unchanged.
- After that loop and before the existing `duration:` / `sources:` block, one new line is appended describing `final.get("combined_audio")`:
  - If `status == "produced"`: ``combined: <path> (<formatted-duration>)``, formatted with the existing `_format_duration` helper (cli.py:145-151).
  - If `status == "failed"`: ``combined: not produced — <reason>``.
  - If the key is missing (older state files): nothing is printed (graceful degrade).
- Wording is in the same plain register as `_humanize_audio_file_status` (cli.py:154-176).

### 2.5 Transcription wiring: `src/record/transcribe.py`

`transcribe.py` itself does not change — `DeepgramBackend.transcribe(audio_path)` is already single-file. The two-jobs-per-stop fan-out lives in `Daemon._spawn_transcription` callers (daemon.py:688-696 and 414-415), and those are the only sites that change (see §2.2 step 5). The transcript output stem still derives from the audio file stem (`daemon.py:745-751`), so the resulting transcript files become `<timestamp>.json` / `<timestamp>.txt` / `<timestamp>.srt` — matching the existing per-session naming convention in `architecture.md` §2.

### 2.6 Logging conventions

Following `transcribe.py`'s pattern (`_log = get_logger("record.daemon")` already exists at daemon.py:181):

- `combine_started` — `mic_path`, `system_path`, `combined_path`.
- `combine_complete` — `combined_path`, `duration_seconds`.
- `combine_failed` — `mic_path`, `system_path`, `combined_path`, `reason`, `error_type`.
- `transcription_skipped` — `reason="combine_failed"` (reuses the existing event name from daemon.py:734-740).

No new log file, no log-rotation change. Output continues to `orchestrator.log` (architecture.md §5).

### 2.7 Files touched

| File | Change |
|---|---|
| `src/record/combine.py` | **New.** `CombineResult`, `CombineError`, `combine_wavs`. ~80 lines. |
| `src/record/daemon.py` | Insert combine step in `_handle_stop` and `_watch_for_system_event_stop`; replace per-source transcription fan-out with single-call against combined file. |
| `src/record/capture.py` | None expected — `combined_audio` is written by the daemon, not the session. |
| `src/record/cli.py` | Extend `_print_stop_summary` with one new line. |
| `src/record/transcribe.py` | None. |
| `pyproject.toml` | None (no new dependency). |

---

## 3. Impact and Risk Analysis

**System Dependencies**

- Depends on the Swift backend's WAV-format guarantee (int16 / mono / 16 kHz). If `WAVWriter.swift` ever widens the format, the validation in `combine_wavs` must update in lockstep — call this out in a code comment so the coupling is visible.
- Depends on the daemon's existing block-until-finalize behavior in `session.stop()`. No new IPC.
- The cloud transcription contract is unchanged — Deepgram still receives one WAV.

**Potential Risks & Mitigations**

- **Risk: long stop time on long meetings.** A 2-hour meeting is ~115 MB of int16 mono 16 kHz audio per source; the chunked sum runs in under a second on a modern Mac (linear in samples, stdlib `array` is C-level). The 5-minute `asyncio.wait_for` ceiling is a hard cap to prevent any worst-case wedge.
- **Risk: clipping in summed output.** Saturation to int16 min/max is the accepted behavior per the functional spec's "equal levels, no attenuation" rule. Documented in a one-line code comment so future readers don't reach for a halve-then-sum "fix".
- **Risk: partial combined file on mid-write crash.** Atomic-rename pattern (`.tmp` → `os.replace`) ensures the final path either contains a valid WAV or does not exist. Transcription gate (`status == "produced"`) guarantees we never ship a partial file to Deepgram.
- **Risk: zero-length source files.** A source with status `silent_throughout` (per spec 005 §2.7) is still a valid `wave`-readable file containing silence. `combine_wavs` treats it as zero samples on that side for as long as its duration. No special-case path required.
- **Risk: source files missing on disk despite `audio_files` entry.** Treated as a `CombineError` with reason `"one or more source files unavailable"`. Transcription is skipped, source files (whichever exist) remain, no transcript appears.
- **Risk: silent regression on the integration tests' "exactly two files per session" assumption.** Six existing tests will fail loudly — listed in §4 — surfacing the regression rather than masking it.

---

## 4. Testing Strategy

**New unit tests — `tests/python/test_combine.py`**

Pure-function tests of `combine_wavs`, using the same hand-rolled WAV-fixture pattern already in `tests/integration/_real_capture_helpers.py` (stdlib `wave`):

- Sums two equal-duration tones into a single output whose samples equal the per-frame int16 sum.
- Zero-pads the shorter source when the two durations differ; output duration equals the longer source.
- Silent (all-zero) source on one side leaves the other side's samples intact in the output.
- Saturation: a synthetic input pair whose sum exceeds int16 range produces clamped samples at the boundaries (`-32768` / `32767`), not wraparound.
- Format validation: raises `CombineError` for a source with wrong sample rate, channel count, or sample width.
- Output is atomically named: with a forced write error injected, the destination path does not exist afterward (no `.tmp` leftover acceptable as long as the final path is absent).
- Output duration reported in `CombineResult.duration_seconds` matches the longer source's duration within one frame.

**Daemon tests — update existing `tests/python/test_daemon.py`**

- The current "two transcription calls per stop" assertions (test_daemon.py:1170-1196, 1303+) are rewritten to assert **one** call, against the combined-file path.
- Stop-path tests have `session.stop` mocks extended to leave real (tiny) WAV files on disk so the combine step can run end-to-end; or the combine call is patched and asserted against. Prefer the former for fewer mocks.
- New cases: combine raises → `transcription_skipped` is logged with `reason="combine_failed"`; backend is never called; source files are still present; `combined_audio.status == "failed"` is persisted in `capture-state.json`.
- New case: one source file missing from disk → combine fails with the documented reason; no transcription; both branches (`_handle_stop` and `_watch_for_system_event_stop`) covered.

**CLI tests — update existing `tests/python/test_cli.py`**

- Stop-summary tests at cli.py:311, 353, 616, 648, 677, 709 gain one extra assertion each: that the new combined-file line is printed with the correct shape on success and on failure, and absent when `combined_audio` is missing (legacy state files).

**Integration tests — update existing**

- `tests/integration/test_two_track_audio.py` and `tests/integration/test_end_to_end.py` "exactly two WAVs per session" assertions are relaxed to "two source WAVs **plus** one combined WAV", and gain a check that the combined WAV is readable, non-empty, and has the expected mono-16k-int16 format. End-to-end tests that assert the stop-summary text gain the new line.
- `test_end_to_end.py`'s transcription assertions update from "transcript files appear for each source" to "transcript files appear for the combined file" (`<timestamp>.json` / `.txt` / `.srt`, no `-mic` / `-system` suffix).

**Manual verification (recorded but not automated)**

- Run a real ~2-minute capture in which the user speaks while a meeting plays; confirm three files in the output folder (`-mic.wav`, `-system.wav`, `.wav`), confirm the combined file plays back with both sides audible, and confirm one transcript triple is produced and contains content from both sides.
