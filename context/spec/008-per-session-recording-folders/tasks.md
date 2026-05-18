# Tasks: Per-Session Recording Folders with a Human-Readable Name

> Spec: [`functional-spec.md`](./functional-spec.md) · [`technical-considerations.md`](./technical-considerations.md)
>
> Each top-level slice leaves `record` runnable end-to-end. Sub-tasks inside a slice are not individually runnable; verify at slice boundaries.

---

## Slice 1 — All session files land inside a per-session folder, with role-only names

After this slice: stopping a capture produces `<output_folder>/<timestamp>/` containing `mic.wav`, `system.wav`, `combined.wav`, `video.mp4`, and `transcript.{json,txt,srt}`. No renaming yet — the folder is always timestamp-only. App is fully runnable; transcription pipeline unchanged in behavior.

- [x] **Swift capture binary: treat `output_path` as a directory and write `mic.wav` / `system.wav` inside.** In the audio-writer setup, switch from `"\(basename)-mic.wav"` / `"\(basename)-system.wav"` to `basename.appendingPathComponent("mic.wav")` / `"system.wav"`. The IPC schema (`StartCommand.output_path`, `StoppedEvent.basename`, `AudioFileEvent.path`) is unchanged in shape — only the semantics shift. **[Agent: macos-swift]**

- [x] **Python `daemon.py` `_handle_start`: create the session folder and pass it as `output_path`.** Compute `session_dir = (self._output_folder or Path.cwd()) / stem` (where `stem` is the existing `_filename_timestamp()` value). Call `session_dir.mkdir(parents=True, exist_ok=False)`. Pass `output_path=str(session_dir)` and `video_output_path=str(session_dir / "video.mp4")` in `StartCommand`. Let a `mkdir` failure propagate as a normal start failure through the state machine. **[Agent: python-backend]**

- [x] **Python `daemon.py` `_run_combine`: write `combined.wav` inside the session folder.** Replace the current `basename.parent / f"{basename.name}.wav"` derivation with `session_dir / "combined.wav"`. **[Agent: python-backend]**

- [x] **Python `daemon.py` `_spawn_transcription`: derive the transcript stem from the session folder.** Change `stem_path = audio_path.with_suffix("")` to `stem_path = audio_path.parent / "transcript"` so `write_transcript` produces `transcript.{json,txt,srt}` instead of `combined.{json,txt,srt}`. **[Agent: python-backend]**

- [x] **Python `daemon.py`: ensure any per-session state file (`capture-state.json`) is written inside the session folder, not in the flat output folder.** Today it sits next to the audio files; it just needs to follow them into the subfolder. **[Agent: python-backend]**

- [x] **Update Python unit tests for the new on-disk layout.**
  - `tests/python/test_daemon.py`: rewrite `_FakeSession` and the `basename`-based fixtures so each session has a `session_dir` and the mic/system/combined paths live inside it. Update `test_stop_spawns_one_transcription_task_and_writes_files` to assert `<session_dir>/transcript.{json,txt,srt}`.
  - `tests/python/test_cli.py`: rewrite hard-coded literals (`-mic.wav`, `-system.wav`, the flat `<basename>.wav`) in mock daemon replies and state JSON to point inside `<session_dir>/`. Stop-summary assertions: paths now contain the session folder.
  - `tests/python/test_combine.py`, `tests/python/test_transcribe.py`: leave alone (path-in/path-out, no semantic change). **[Agent: python-backend]**

- [x] **Update integration tests for the new IPC semantics and on-disk layout.**
  - `tests/integration/test_end_to_end.py`, `test_two_track_audio.py`, `test_real_capture.py`: switch `output_basename = tmp_path / "session"` to `output_dir = tmp_path / "session"`; assert `mic.wav` / `system.wav` / `video.mp4` exist inside `output_dir`. Adjust `stopped.basename` comparisons against the directory string. Update the combined-stem derivation from `audio_path.stem.removesuffix("-mic")` to `audio_path.parent / "combined.wav"`. **[Agent: macos-swift]**

- [x] **Verification: run the full unit test suite plus one real-capture integration test, then perform a short manual smoke.**
  - `uv run pytest tests/python` — all green.
  - `uv run pytest tests/integration/test_real_capture.py` — green against the real Swift binary.
  - Manual smoke: `record start`, speak a few words, `record stop`. Confirm `<output_folder>/<timestamp>/` exists and contains exactly `mic.wav`, `system.wav`, `combined.wav`, `video.mp4`, `transcript.json`, `transcript.txt`, `transcript.srt`, and `capture-state.json`. Confirm no files are written directly into `<output_folder>/`. **[Agent: general-purpose]**

---

## Slice 2 — Silent-meeting folders get the `-silent` suffix

After this slice: a silent capture (mic muted + no system audio) produces a folder renamed to `<timestamp>-silent`. Talking captures still land at `<timestamp>` (description-from-claude not built yet). App fully runnable.

- [x] **Create `src/record/naming.py` with the silent-only path of `try_rename_session_folder`.** Public coroutine `async try_rename_session_folder(session_dir: Path, transcript: Transcript) -> None`. Helpers: `is_silent(transcript) -> bool` (every segment's `.text.strip()` is empty) and `atomic_rename(session_dir, suffix) -> Path` (computes `session_dir.with_name(f"{session_dir.name}-{suffix}")`, checks `target.exists()`, calls `os.rename`). For this slice the coroutine handles only the silent branch — if `is_silent` returns False, it returns without doing anything. All exceptions are caught, logged via `structlog` (`session_renamed` / `session_rename_failed`), and swallowed. Module constants `SILENT_SUFFIX = "silent"`. **[Agent: python-backend]**

- [x] **Hook `try_rename_session_folder` into `_spawn_transcription` after `write_transcript` returns.** Call `await naming.try_rename_session_folder(session_dir=audio_path.parent, transcript=transcript)` inside the existing detached task body. The existing log-and-swallow envelope around the task body is sufficient since the function catches its own exceptions. **[Agent: python-backend]**

- [x] **Unit tests for `naming.py` (silent-only surface).** New file `tests/python/test_naming.py`:
  - `is_silent`: empty transcript → True; whitespace-only segments → True; one non-empty segment → False.
  - `atomic_rename`: renames a tmp folder; raises when target already exists; raises when source missing.
  - `try_rename_session_folder` integration (subprocess and FS real, no `claude` involved yet): silent transcript → folder renamed to `<name>-silent`; non-silent transcript → folder name unchanged.
  - Failure injection: target already exists → folder kept at original name, one WARNING log event emitted. **[Agent: python-backend]**

- [x] **Unit test in `tests/python/test_daemon.py`: rename is invoked after a successful `write_transcript`.** Patch `naming.try_rename_session_folder` with an `AsyncMock`; assert it is called exactly once with the session dir and transcript. Add a second test: when `try_rename_session_folder` raises (it shouldn't, but defense in depth), the detached task still completes without propagating the exception. **[Agent: python-backend]**

- [x] **Verification: run the full unit suite, then a manual silent-capture smoke.**
  - `uv run pytest tests/python` — all green.
  - Manual: mute the mic, play silence (or nothing) as system audio, `record start`, wait ~5 s, `record stop`. After transcription completes, confirm the folder is renamed to `<timestamp>-silent` and all files remain inside. **[Agent: general-purpose]**

---

## Slice 3 — Non-silent meetings get a `claude -p`-generated description suffix

After this slice: a real meeting transcript produces a kebab-case English description and the folder is renamed to `<timestamp>-<description>`. Any failure in the description chain (CLI missing, non-zero exit, timeout, invalid output, rename collision) leaves the folder at `<timestamp>`. Full feature complete.

- [x] **Extend `src/record/naming.py` with `generate_description` and `validate_description`.**
  - `async generate_description(transcript_text: str) -> str`: spawns `asyncio.create_subprocess_exec("claude", "-p", "--model", "claude-haiku-4-5", PROMPT, stdin=PIPE, stdout=PIPE, stderr=PIPE)`; writes the (truncated to `MAX_TRANSCRIPT_CHARS = 32_000`) transcript on stdin; awaits with `asyncio.wait_for(..., timeout=30)`; raises on non-zero exit, timeout (kills proc and re-raises), or empty stdout. PROMPT is the literal from technical-considerations §2.4.2.
  - `validate_description(raw: str) -> str`: strip a single trailing newline, then enforce `re.fullmatch(r"^[a-z0-9]+(?:-[a-z0-9]+){1,5}$", raw)` and `len(raw) <= 60`; raise on mismatch.
  - Module constants: `MODEL = "claude-haiku-4-5"`, `TIMEOUT_S = 30`, `MAX_TRANSCRIPT_CHARS = 32_000`, `DESCRIPTION_REGEX`, `MAX_DESCRIPTION_CHARS = 60`. **[Agent: python-backend]**

- [x] **Wire `generate_description` + `validate_description` into `try_rename_session_folder`.** Full flow: if `is_silent` → `atomic_rename(..., SILENT_SUFFIX)`; else read `transcript.txt`, truncate to `MAX_TRANSCRIPT_CHARS`, call `generate_description`, `validate_description`, `atomic_rename(..., description)`. Every failure path catches and logs `session_rename_failed` with a redacted reason; the folder is left at its timestamp-only name. **[Agent: python-backend]**

- [x] **Unit tests for the new `naming.py` surface.** Extend `tests/python/test_naming.py`:
  - `validate_description`: accepts `pricing-call-with-acme`, `weekly-1-1-with-anna`; rejects uppercase, spaces, punctuation, `../foo`, `foo/bar`, empty string, single token, >60 chars.
  - `generate_description`: install a tiny fake `claude` shell script on a tmp `PATH` directory (or monkeypatch `asyncio.create_subprocess_exec`). Cases: happy path returns expected string; non-zero exit raises; timeout raises and the subprocess is reaped; empty stdout raises; stdin truncation to `MAX_TRANSCRIPT_CHARS` is observed by the fake.
  - `try_rename_session_folder` end-to-end (subprocess fake, real FS):
    - Happy path: non-silent transcript + cooperative fake `claude` → folder renamed to `<orig>-<expected-suffix>`.
    - Each failure source independently (CLI missing → `FileNotFoundError`; non-zero exit; timeout; invalid output; rename collision) → folder stays at `<orig>`, exactly one `session_rename_failed` log event. **[Agent: python-backend]**

- [x] **Integration test: end-to-end naming with a stub `claude` on `PATH`.** New test in `tests/integration/` (or extend an existing file): run a short real capture (or feed a pre-baked transcript into the detached transcription task), then wait for the rename to complete. The stub `claude` script lives in a tmp dir prepended to `PATH` for the test; one variant prints a valid description, another exits non-zero. Assert the folder ends up at `<timestamp>-<description>` in the success case and at `<timestamp>` (with files intact) in the failure case. **[Agent: macos-swift]**

- [x] **Documentation: note the `claude` CLI dependency in the README/install docs.** One sentence in the install section: the orchestrator shells out to `claude -p` after each capture to auto-name session folders; if `claude` is not on `PATH`, folders remain timestamp-only and nothing else is affected. **[Agent: python-backend]**

- [x] **Verification: full unit + integration suite, then real-meeting manual smoke.**
  - `uv run pytest tests/python tests/integration` — all green.
  - Manual smoke A: `record start`, speak for ~30 s about a recognizable topic (e.g. "pricing for the Acme renewal"), `record stop`. Wait for transcription. Confirm the folder is renamed to `<timestamp>-<plausible-suffix>` and the suffix passes the validator's regex.
  - Manual smoke B: temporarily move `claude` off `PATH` (`alias claude=false` in a fresh shell). Record a short clip. Confirm the folder stays at `<timestamp>` and one `session_rename_failed` log event appears in `~/Library/Logs/record/orchestrator.log` with `FileNotFoundError` in the reason. **[Agent: general-purpose]**
