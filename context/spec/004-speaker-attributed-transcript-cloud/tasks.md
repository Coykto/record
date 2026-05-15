# Tasks: Speaker-Attributed Transcript File (Cloud)

## Slice 1 — `record transcribe`: manual cloud transcription of an existing recording

Goal: a `TranscriptionBackend` interface + `DeepgramBackend`, transcript writers (`.json`/`.txt`/`.srt`), a `record.secrets` module for the API key, and a `record transcribe <recording>` CLI command. Given a key (env var or Keychain) and a `.wav`, the command produces the three transcript files next to it. The daemon is untouched.

- [x] **Slice 1: Transcription backend + writers + `record transcribe` CLI**
  - [x] Update `pyproject.toml` — add `httpx` and `keyring` to `dependencies`. **[Agent: python-backend]**
  - [x] Add `src/record/secrets.py` — `get_deepgram_api_key()` (checks `RECORD_DEEPGRAM_API_KEY` env var first, then macOS Keychain via `keyring`; `None` if neither set) and `set_deepgram_api_key(key)` (stores into Keychain). Service/account names as module constants; key never logged. **[Agent: python-backend]**
  - [x] Add `src/record/transcribe.py` — `Transcript`/`Segment` pydantic models per tech spec §2.2/§2.6; `TranscriptionBackend` interface; `DeepgramBackend` (`httpx.AsyncClient` → Deepgram pre-recorded endpoint, `diarize=true`/`language=multi`/`smart_format`/`punctuate`/`utterances`, speaker integers renumbered to "Speaker N" in first-appearance order); `TranscriptionError`; atomic `write_transcript()` producing `.json` (source of truth) + `.txt` + `.srt` via temp-then-rename. **[Agent: python-backend]**
  - [x] Add `record transcribe <recording>` to `src/record/cli.py` — resolve the `.wav` path, get the key (exit 2 if none), run `DeepgramBackend` synchronously, write the three files (overwriting), print a one-line summary. Exit codes 0/1/2/3 per tech spec §2.5. **[Agent: python-backend]**
  - [x] Add `tests/python/test_secrets.py` (env-var precedence over Keychain, `keyring` stubbed, `None` when neither set) and `tests/python/test_transcribe.py` (Deepgram JSON fixture → `Transcript` parsing + speaker renumbering + language passthrough; writers' content + atomic-write; error paths — non-2xx, malformed body, network error → `TranscriptionError` — via `httpx.MockTransport`). Extend `tests/python/test_cli.py` with `record transcribe` exit-code cases. **[Agent: python-backend]**
  - [x] **Verification:** Run `make test` — green. With `RECORD_DEEPGRAM_API_KEY` set, run `record transcribe <fixture>.wav` → confirm `.json`/`.txt`/`.srt` appear with the matching stem, speakers labelled "Speaker N" consistently, timestamps present. Unset the key → exit 2. Nonexistent file → exit 1. **[Agent: python-backend]**

## Slice 2 — Automatic transcription after every daemon capture

Goal: when a daemon capture finalizes (explicit `record stop` *or* a system-event stop), the daemon spawns a background transcription task that writes the three files next to the recording. Failures are logged to `orchestrator.log` with no retry and no notification. Overlapping captures transcribe independently; `record stop` still returns immediately.

- [ ] **Slice 2: Daemon auto-transcription on finalize**
  - [ ] Add `Daemon._spawn_transcription(audio_path)` to `src/record/daemon.py` — resolve a backend (if no key, log `transcription_skipped` at WARNING and return); otherwise create a tracked `asyncio.Task` that awaits `backend.transcribe()` then `write_transcript()`; on `TranscriptionError` or any exception, log `transcription_failed` at ERROR with the reason. Add the task to `self._background` with a discard done-callback. **[Agent: python-backend]**
  - [ ] Call `_spawn_transcription` at the tail of both `_handle_stop` and `_watch_for_system_event_stop` with the finalized session's audio path. Confirm `_handle_quit` does **not** await in-flight tasks (keeps quit responsive); log any abandoned jobs. **[Agent: python-backend]**
  - [ ] Extend `tests/python/test_daemon.py` — on successful stop a transcription task is spawned with the session's audio path (backend stubbed); no key → no task spawned + warning logged; a failing task → `transcription_failed` logged, no exception escapes, daemon stays IDLE; two stops in quick succession → two independent tasks, neither blocks. **[Agent: python-backend]**
  - [ ] Extend `tests/integration/test_end_to_end.py` — daemon capture with a stubbed Deepgram (`httpx` transport returning a canned response): assert the three transcript files appear next to the `.wav` with the right stem once the stub responds. **[Agent: python-backend]**
  - [ ] **Verification:** `make test` — green. With `RECORD_DEEPGRAM_API_KEY` set: `record daemon start` → `record start` → `record stop` → after a moment `.json`/`.txt`/`.srt` appear next to the `.wav`. Unset the key → capture still works, `orchestrator.log` shows `transcription_skipped`. Disconnect the network and capture again → `transcription_failed` logged, no crash, no notification. **[Agent: python-backend]**

## Slice 3 — API key setup at install time

Goal: `record install` prompts for the Deepgram API key and stores it in the Keychain, so a fresh user can set up transcription without touching env vars. Blank input leaves any existing key untouched.

- [ ] **Slice 3: `record install` key prompt**
  - [ ] Extend `record install` in `src/record/cli.py` — after the existing LaunchAgent registration, prompt `Deepgram API key (leave blank to skip):`. Non-blank → store via `secrets.set_deepgram_api_key`. Blank → skip, leaving any existing key untouched. Input never echoed back. **[Agent: python-backend]**
  - [ ] Extend `tests/python/test_cli.py` — `install` stores a provided key; blank input leaves an existing key untouched; the key is never printed. **[Agent: python-backend]**
  - [ ] **Verification:** Run `record install`, enter a key → `record transcribe <fixture>.wav` works with no env var set (key resolved from Keychain). Re-run `record install`, leave the prompt blank → the previously stored key still works. **[Agent: python-backend]**

---

## Verification caveats

| Task/Slice | Issue | Recommendation |
|---|---|---|
| Slices 1–3: manual verification of *real* transcription | Needs a real Deepgram API key + network access — not available in CI | Automated tests use a mocked `httpx` transport (no network, no key needed). For the manual smoke checks, supply a Deepgram key. |
