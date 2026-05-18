# Technical Specification: Per-Session Recording Folders with a Human-Readable Name

- **Functional Specification:** [`functional-spec.md`](./functional-spec.md)
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

The change is structurally small but spans the cross-platform seam:

1. **Folder layout shifts from flat to one-folder-per-session.** Python creates a per-session directory `<output_folder>/<timestamp>/` at `start` time and tells the Swift capture binary to write its outputs inside it. Combine and transcribe write their outputs into the same folder. Files inside use role-only names (`mic.wav`, `system.wav`, `combined.wav`, `video.mp4`, `transcript.json`/`.txt`/`.srt`).
2. **The Swift binary's audio-naming derivation changes.** Today Swift takes `output_path` (a basename) and appends `-mic.wav` / `-system.wav`. After this change `output_path` is a *directory*, and Swift writes `mic.wav` / `system.wav` inside it. The IPC schema stays the same on the wire (still a string field); only the semantics shift.
3. **A new naming step runs after transcription succeeds.** Inside the existing detached `transcribe:<stem>` task in `daemon.py`, after `write_transcript` writes the three transcript files, the orchestrator (a) checks `transcript.json` for silence, (b) for non-silent transcripts, invokes `claude -p --model claude-haiku-4-5` with the truncated transcript text on stdin to generate a short English kebab-case description, (c) validates the output against a strict regex, and (d) atomically renames the session folder to `<timestamp>-<description>` (or `<timestamp>-silent`).
4. **Any failure in steps 3a–3d is caught, logged, and swallowed.** The detached task already swallows exceptions today (`daemon.py:940-959`); the new code uses the same pattern. The session folder stays at its timestamp-only name with no terminal-side surface.

No new long-running services. No new external dependencies on the Python side beyond a hard dependency on the `claude` CLI being present on the user's `PATH` (already implicit in the spec's wording — see Risks).

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 Folder & file layout (final)

For a session that starts at `2026-05-16T14-30-00`:

| Path | Producer | Notes |
|---|---|---|
| `<output_folder>/2026-05-16T14-30-00/` | Python (`daemon.py`, `_handle_start`) | Created with `mkdir(parents=True, exist_ok=False)` before sending `start` to Swift. |
| `<output_folder>/2026-05-16T14-30-00/mic.wav` | Swift | Written into the directory Python passed as `output_path`. |
| `<output_folder>/2026-05-16T14-30-00/system.wav` | Swift | Same directory. |
| `<output_folder>/2026-05-16T14-30-00/video.mp4` | Swift | Path is passed in full via `video_output_path` (Python picks the leaf). |
| `<output_folder>/2026-05-16T14-30-00/combined.wav` | Python (`combine.combine_wavs`) | Replaces today's `<basename>.wav`. |
| `<output_folder>/2026-05-16T14-30-00/transcript.{json,txt,srt}` | Python (`transcribe.write_transcript`) | Triple is written using stem `<folder>/transcript`. |
| `<output_folder>/2026-05-16T14-30-00/capture-state.json` | Python (`capture.py` persistence) | Per-session sidecar moves into the session folder. Today it lives next to the audio files in the flat output folder; semantics are unchanged, only the location moves. |

After successful naming, the entire directory is renamed in place to `<output_folder>/2026-05-16T14-30-00-pricing-call-with-acme/`. All contained file paths shift accordingly; nothing inside the folder is touched.

### 2.2 IPC protocol change (semantic, not schema)

- `StartCommand.output_path` (`ipc.py:52-62`) — **semantics change** from "basename, Swift appends `-mic.wav`/`-system.wav`" to "directory, Swift writes `mic.wav`/`system.wav` inside". JSON shape unchanged.
- `StartCommand.video_output_path` — unchanged. Python now sets it to `<session_folder>/video.mp4`.
- `StoppedEvent.basename` — **semantics change** to mirror: it now echoes back the directory path. (Tests asserting `stopped.basename` continue to pass as long as they construct the expected value the same way Python does.)
- `AudioFileEvent.path` — unchanged in shape; the absolute paths it reports are now `<dir>/mic.wav` and `<dir>/system.wav`.

### 2.3 Swift binary change

Single-purpose change inside the macOS capture binary's audio writer setup: when computing per-source output paths, instead of `"\(basename)-mic.wav"` / `"\(basename)-system.wav"`, use `basename.appendingPathComponent("mic.wav")` (and `"system.wav"`). The `basename` parameter received from Python is now a directory. No protocol fields are added or removed.

### 2.4 Python orchestrator changes

#### 2.4.1 Path assembly (`daemon.py`)

- `_handle_start` (`daemon.py:308-311`):
  - Continue computing `stem = _filename_timestamp()` (kept as the `capture_id`).
  - Compute `session_dir = (self._output_folder or Path.cwd()) / stem`.
  - `session_dir.mkdir(parents=True, exist_ok=False)`. If `exist_ok=False` raises (collision on a same-second start), treat as a daemon-level start failure (current state machine already handles arbitrary `_handle_start` exceptions).
  - Send `StartCommand(output_path=str(session_dir), video_output_path=str(session_dir / "video.mp4"), ...)`.
- `_handle_stop` reply (`daemon.py:359-360`) — derive `mic_path = session_dir / "mic.wav"`, `system_path = session_dir / "system.wav"` (or read them from `audio_file` events as today; the value is now a path inside the folder).
- `_run_combine` (`daemon.py:637-800`):
  - `combined_path = session_dir / "combined.wav"` (no leaf renaming math).
- `_spawn_transcription` (`daemon.py:906-973`):
  - Change `stem_path = audio_path.with_suffix("")` → `stem_path = audio_path.parent / "transcript"`. This makes `write_transcript` produce `transcript.json` / `transcript.txt` / `transcript.srt`.
  - After `write_transcript(transcript, stem_path)` returns, call `await naming.try_rename_session_folder(session_dir=audio_path.parent, transcript=transcript)`. The call must catch all exceptions internally so the existing log-and-swallow envelope around the task body is sufficient.

#### 2.4.2 New module: `src/record/naming.py`

Single public coroutine and a handful of internal helpers. Responsibilities:

| Function | Purpose |
|---|---|
| `async try_rename_session_folder(session_dir: Path, transcript: Transcript) -> None` | Public entry point. Orchestrates the silent check, the `claude -p` call, the validation, and the atomic rename. Catches every exception, logs it, returns normally. |
| `is_silent(transcript: Transcript) -> bool` | True iff every segment's `text` is empty or whitespace after `.strip()`. |
| `async generate_description(transcript_text: str) -> str` | Spawns `claude -p --model claude-haiku-4-5` via `asyncio.create_subprocess_exec`, writes the (truncated) transcript on stdin, reads stdout, returns the stripped string. Raises on non-zero exit, timeout, or empty stdout. |
| `validate_description(raw: str) -> str` | Returns the cleaned description iff it matches `^[a-z0-9]+(?:-[a-z0-9]+){1,5}$` and `len() <= 60`; raises otherwise. Tolerates one trailing newline. |
| `atomic_rename(session_dir: Path, suffix: str) -> Path` | Computes `target = session_dir.with_name(f"{session_dir.name}-{suffix}")`; `os.rename(session_dir, target)`. Raises if `target` already exists or any OS error. |

Constants (module-level, not in `config.toml`):

| Constant | Value | Rationale |
|---|---|---|
| `MODEL` | `"claude-haiku-4-5"` | Cheapest current Haiku; matches spec decision. |
| `TIMEOUT_S` | `30` | Bounds the worst-case wait so a hung CLI never lingers behind a finished recording. |
| `MAX_TRANSCRIPT_CHARS` | `32_000` | Truncates `transcript.txt` content before piping to `claude -p`. |
| `DESCRIPTION_REGEX` | `^[a-z0-9]+(?:-[a-z0-9]+){1,5}$` | Lowercase kebab-case, 2–6 hyphen-separated tokens. Filesystem-safe by construction. |
| `MAX_DESCRIPTION_CHARS` | `60` | Per functional spec §2.4. |
| `SILENT_SUFFIX` | `"silent"` | Per functional spec §2.5. |

Prompt shape (passed to `claude -p` on argv; transcript on stdin):

> "Read the meeting transcript on stdin. Output one short English description of what the meeting was about, suitable as a filename suffix: lowercase, 3–6 words separated by single hyphens, no punctuation, no quotes, no trailing newline, maximum 60 characters total. Output only the description and nothing else."

The subprocess call:

- `asyncio.create_subprocess_exec("claude", "-p", "--model", MODEL, prompt, stdin=PIPE, stdout=PIPE, stderr=PIPE)`
- `await asyncio.wait_for(proc.communicate(input=text.encode("utf-8")), timeout=TIMEOUT_S)`
- Non-zero exit → raise `RuntimeError(stderr.decode(..., errors="replace"))`.

#### 2.4.3 Logging

All naming-related log lines go through `structlog` at `INFO` for the rename success path and `WARNING` for the failure path (consistent with the "diagnose later from logs" line in functional spec §2.6). One log event per outcome — `session_renamed` or `session_rename_failed` — with `session_dir`, `attempted_suffix`, and a redacted `reason`. No new log file; rides on `orchestrator.log`.

#### 2.4.4 `_capture_id` and the stop summary

- `_capture_id` remains the timestamp string. The CLI's stop summary continues to print file paths derived from the session folder Python knows about *at stop time* — i.e., paths inside `<output_folder>/<timestamp>/`. After the later rename those exact strings become stale, which functional spec §2.7 explicitly accepts.

### 2.5 No configuration changes

`config.toml` is unchanged. `output_folder` keeps its meaning as the parent that holds per-session subfolders. No opt-out flag, no model override, no timeout override.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **The `claude` CLI must be present on `PATH` of the user who runs the orchestrator.** No existing module shells out to it. If the CLI is missing, `asyncio.create_subprocess_exec` raises `FileNotFoundError`, which `try_rename_session_folder` catches → folder stays timestamp-only. Acceptable per functional spec §2.6 but worth documenting in the README/install docs as part of this change.
- **Cross-language IPC**: the Swift binary's audio-path logic must change in lockstep with Python's `output_path` semantics. Mixed versions (new orchestrator + old Swift binary, or vice versa) silently break filenames. Mitigated by the orchestrator + binary being packaged and released together (per `architecture.md` §3, "Build pipeline (v1): local builds"), and by integration tests exercising the real binary.
- **No new third-party Python packages.**

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Model returns punctuation, quotes, accents, slashes, or other unsafe chars.** | `validate_description` rejects anything outside the strict regex → falls into the standard failure path → folder stays timestamp-only. Prompt explicitly forbids punctuation and quotes; validation is the guardrail, not the prompt. |
| **Model returns a path-traversal-looking string (`../foo`, `foo/bar`).** | Regex forbids `.` and `/` entirely; even if the prompt is fooled, `validate_description` rejects it before reaching `os.rename`. |
| **Rename collision.** Two captures finish at the same second (impossible today but technically possible with two daemons or manual filesystem fiddling) → both folders compete for the same name. | `os.rename` is atomic on the same filesystem; if `target` already exists on macOS the call raises → failure path → timestamp-only folder kept. `atomic_rename` also checks `target.exists()` before calling rename so the error message in logs is descriptive. |
| **`claude -p` hangs or is interactive on a misconfigured machine.** | 30-second `asyncio.wait_for` timeout; the subprocess is killed on timeout via the standard `proc.kill()` / `await proc.wait()` cleanup pattern. |
| **Very large transcript blown into `claude -p`.** | Truncated to `MAX_TRANSCRIPT_CHARS = 32_000` before stdin write; bounded memory and bounded model cost. |
| **In-flight rename of session A interferes with concurrently-started session B.** | Sessions live in distinct folders; session B's folder is created from a fresh timestamp before its start. The detached transcription/rename task for A only touches A's folder. No shared mutable state. |
| **Stop-summary paths become stale after rename.** | Explicitly accepted by functional spec §2.7. The CLI prints what is true at stop time. Documented behavior. |
| **Existing recordings (pre-change, flat layout) on the user's machine.** | Out of scope per functional spec §3 ("Out-of-Scope: Re-running description generation against already-named folders"). The orchestrator does not scan, migrate, or rename historical files. New sessions use the new layout; old files sit where they are. |
| **`capture-state.json` references its own folder.** | The sidecar uses paths inside the same folder; relocating those paths during rename is automatic. No path-fix-up code needed. |
| **Process exit during transcription/rename.** | The detached task is abandoned today on quit (`daemon.py:1133-1154`); the rename inherits the same behavior. Worst case: a folder stays at `<timestamp>` because the daemon was killed before the rename ran. Acceptable per §2.6. |

---

## 4. Testing Strategy

### Unit tests (Python)

- **New `tests/python/test_naming.py`**:
  - `is_silent`: empty transcript → True; whitespace-only segments → True; one non-empty segment → False.
  - `validate_description`: accepts `pricing-call-with-acme`, `weekly-1-1-with-anna`; rejects uppercase, spaces, punctuation, `../foo`, empty string, >60 chars, single token.
  - `generate_description`: with `claude` stubbed via `monkeypatch` of `asyncio.create_subprocess_exec` (or a fake script on `PATH` via fixture) — happy path, non-zero exit, timeout, empty stdout. Stdin truncation behavior verified.
  - `atomic_rename`: renames a tmp folder; raises when target already exists; raises when source missing.
  - `try_rename_session_folder` integration (still within unit tier, all subprocess and FS stubbed): silent transcript → `-silent` rename; happy path → `-<desc>` rename; each failure source independently leaves the folder timestamp-only.

- **Updated `tests/python/test_daemon.py`**:
  - `_FakeSession` and related fixtures updated to construct `session_dir = <root>/<timestamp>/` and place `mic.wav` / `system.wav` / `combined.wav` inside.
  - `test_stop_spawns_one_transcription_task_and_writes_files` updated to assert transcript files land at `<session_dir>/transcript.{json,txt,srt}`.
  - New test: after a successful `write_transcript`, the detached task calls `naming.try_rename_session_folder` exactly once with the session dir and transcript. (Patch `naming.try_rename_session_folder` with `AsyncMock`.)
  - New test: rename failure surfaces in logs but does not raise out of the detached task.

- **Updated `tests/python/test_cli.py`**:
  - All hard-coded literals (`-mic.wav`, `-system.wav`, flat-folder paths in mock daemon replies and state JSON) rewritten for `<session_dir>/mic.wav` etc. Stop-summary assertions adjusted to expect paths inside the session folder.

- **Unchanged**: `tests/python/test_combine.py` (path-in/path-out — no semantic change), `tests/python/test_transcribe.py` (call-site change is in daemon, not in `write_transcript` itself; existing tests of `write_transcript` with arbitrary stems remain valid).

### Integration tests (Swift binary + Python)

- **`tests/integration/test_end_to_end.py`, `test_two_track_audio.py`, `test_real_capture.py`** updated:
  - Construct `output_basename = tmp_path / "session"` → `output_dir = tmp_path / "session"`, pass it through start; assert `mic.wav` / `system.wav` appear inside `output_dir` (not `session-mic.wav` next to it).
  - `stopped.basename` echo assertions: now compare against the directory string.
  - Combined-file derivation in tests changes from `audio_path.stem.removesuffix("-mic")` → `audio_path.parent / "combined.wav"`.
  - At least one end-to-end test for spec 008 specifically: real capture → fake/mock transcript file dropped into the session folder → `naming.try_rename_session_folder` invoked → folder is renamed to `<timestamp>-<expected-stub>`. The `claude` CLI is stubbed via a tiny script on a tmp `PATH` directory.

- **New integration test (rename failure)**: same setup, but the stub `claude` script exits non-zero. Assert the folder keeps its timestamp-only name, all files remain inside it, and one `session_rename_failed` log event is emitted.

### Manual smoke

- One real capture (short, ~30 s, speaking a recognizable topic) end-to-end with a real `claude` CLI: confirm the folder is created at timestamp-only, the rename appears in Finder shortly after stop, and the resulting suffix is reasonable.
- One real capture with the microphone muted and system audio silent: confirm folder becomes `<timestamp>-silent`.
