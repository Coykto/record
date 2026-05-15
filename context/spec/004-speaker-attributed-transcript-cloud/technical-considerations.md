# Technical Specification: Speaker-Attributed Transcript File (Cloud)

- **Functional Specification:** `context/spec/004-speaker-attributed-transcript-cloud/functional-spec.md`
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

This feature is **entirely Python orchestrator-side** — no Swift capture binary or JSON-line IPC changes. The audio `.wav` already exists on disk when a capture finalizes; transcription is a post-capture step.

A new `record.transcribe` module defines a `TranscriptionBackend` interface and a `DeepgramBackend` implementation (`httpx` → Deepgram Nova-3, diarization on, multi-language auto-detect). On every successful finalize, the daemon launches a detached `asyncio` task that runs the backend against that session's audio and writes `{stem}.json`, `{stem}.txt`, `{stem}.srt` next to the recording. Jobs are independent and never serialized. Failures are logged to `orchestrator.log` — no retry, no notification. A `record transcribe <recording>` CLI command runs the same path manually.

**Intentional deviations from `context/product/architecture.md`:**

| Architecture says | This spec uses | Reason |
|---|---|---|
| `~/Movies/record/{ts}/` per-meeting directory holding `audio/video/transcript.*` | Flat `{stem}.json/.txt/.srt` next to `{stem}.wav` | Keeps the flat layout specs 001–003 established; functional spec only requires "same folder, shares the recording's name". Per-meeting directory deferred to the Phase 4 library. |
| `transcript.txt` the only human-readable derivative | `transcript.json` (source of truth) + `.txt` + `.srt` | Functional spec adds a subtitle-style file. |
| Speaker labels with the user identified | Generic "Speaker N" only | Spec 001 produces a single **mixed** mic+system stream — the mic owner can't be attributed. Functional spec amended accordingly. |
| `languages` configurable | No language config; always auto-detect | Decision: Deepgram `multi` mode auto-detects; functional spec amended. |

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 New / changed files

```
src/record/
├── transcribe.py        (new — TranscriptionBackend interface, DeepgramBackend, transcript writers)
├── secrets.py           (new — Keychain get/set for the Deepgram API key; env-var fallback)
├── daemon.py            (changed — spawn a transcription task on finalize)
├── cli.py               (changed — `record transcribe` command; `record install` prompts for the key)
└── pyproject.toml       (changed — add httpx, keyring)
tests/python/
├── test_transcribe.py   (new)
└── test_secrets.py      (new)
```

`config.py` is **unchanged** — no new config keys (language is auto-detected).

### 2.2 `record.transcribe` module

- **`Transcript`** — pydantic model: `provider`, `model`, `language` (detected, list), `duration_seconds`, and `segments: list[Segment]`. Each `Segment`: `speaker: str` ("Speaker 1", …), `start: float`, `end: float`, `text: str`.
- **`TranscriptionBackend`** — interface: `async def transcribe(self, audio_path: Path) -> Transcript`. The architecture's seam for the Phase 5 on-device backend.
- **`DeepgramBackend`** — implements it. POSTs the audio to Deepgram's pre-recorded endpoint via `httpx.AsyncClient`; maps the diarized response into `Transcript`, renumbering Deepgram's speaker integers (0,1,2…) to "Speaker 1/2/3…" in first-appearance order.
- **`TranscriptionError`** — raised on any failure (network, non-2xx, malformed response); carries a human-readable message for the log line.
- **Writers** — `write_transcript(transcript, stem_path)` writes all three files **atomically** (temp-then-rename, per file): `.json` (full `Transcript` dump — source of truth), `.txt` (readable, `[hh:mm:ss] Speaker N: text` per utterance), `.srt` (standard SubRip). `.txt` and `.srt` are derived from the in-memory `Transcript`.

### 2.3 `record.secrets` module

- `get_deepgram_api_key() -> str | None` — checks the `RECORD_DEEPGRAM_API_KEY` env var first (dev fallback per architecture), then the macOS Keychain via `keyring`. `None` if neither is set.
- `set_deepgram_api_key(key: str)` — stores into the Keychain; used only by the `record install` prompt.
- Keychain service/account names are module constants. The key is never logged.

### 2.4 Daemon integration

- New helper `Daemon._spawn_transcription(audio_path)`:
  - Resolves a backend — builds `DeepgramBackend` if a key is available; if not, logs `transcription_skipped` (no key) at WARNING and returns. Capture already succeeded.
  - Creates an `asyncio.Task` that `await`s `backend.transcribe(...)` then `write_transcript(...)`. On `TranscriptionError` or any exception, logs `transcription_failed` at ERROR with the reason. The task is added to `self._background` (so it isn't GC'd) with a done-callback to discard.
- Called at the tail of both `_handle_stop` and `_watch_for_system_event_stop`, using the finalized session's audio path.
- **Does not block** the control response — `_handle_stop` returns immediately, as today.
- Each call creates its own task; multiple in flight is fine; a new `start` is never gated on them.

### 2.5 CLI changes

| Command | Behavior | Exit codes |
|---|---|---|
| `record transcribe <recording>` | Accepts a path to a `.wav` (or its stem). Runs `DeepgramBackend` synchronously, writes the three transcript files next to it (overwriting any existing ones), prints a one-line summary. | 0 success; 1 file not found; 2 no API key configured; 3 transcription failed (reason printed + logged) |
| `record install` | Existing LaunchAgent registration, plus a new prompt: `Deepgram API key (leave blank to skip):`. Non-blank → stored via `record.secrets`. Blank → skipped, **any existing key left untouched**; capture still works, transcription is skipped-with-log until a key is set. | unchanged |

### 2.6 Transcript file shapes

`{stem}.json`:
```
{
  "provider": "deepgram",
  "model": "nova-3",
  "language": ["en", "ru"],
  "duration_seconds": 1432.5,
  "segments": [
    {"speaker": "Speaker 1", "start": 0.0, "end": 4.2, "text": "..."}
  ]
}
```
`{stem}.txt`: `[00:00:00] Speaker 1: ...` — one line per utterance.
`{stem}.srt`: standard SubRip (index, `start --> end`, `Speaker N: text`).

### 2.7 Deepgram request

- `POST https://api.deepgram.com/v1/listen` (pre-recorded). Refer to Deepgram's docs for exact parameter syntax.
- Auth: `Authorization: Token <key>` header.
- Query params: `model=nova-3`, `diarize=true`, `language=multi`, `smart_format=true`, `punctuate=true`, `utterances=true`.
- Body: raw WAV bytes, `Content-Type: audio/wav`.
- `httpx` timeouts: short connect, long/unbounded read (a long call takes time to transcribe). No streaming — live transcript is a Phase 3 item.

---

## 3. Impact and Risk Analysis

### System Dependencies

- New PyPI dependencies: `httpx`, `keyring`.
- External service: **Deepgram API** — network required at transcription time. The only network dependency in the product.
- macOS Keychain via `keyring` — no new TCC permission (an app reading its own Keychain items doesn't prompt).
- Depends on spec 001's audio `.wav` existing and being well-formed. Does **not** touch capture, IPC, hotkey, or video. No new JSON-line protocol surface.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Mixed-audio diarization can't identify the user ("You" was promised) | Functional spec amended to generic "Speaker N"; "You" deferred to the Phase 3 separate-tracks work. Documented as a deviation. |
| Daemon exits (quit / logout) while a transcription task is in flight → transcript lost | Accepted per functional spec (no retry, fail quietly, logged). `record transcribe` lets the user re-run manually. Proposal: `_handle_quit` does **not** await in-flight tasks (keeps quit responsive); abandoned jobs are logged. |
| Many long calls → many background tasks accumulate | Tasks are I/O-bound and lightweight; tracked in `self._background`. Fine for a single-user tool (4–8 calls/day). A bounded worker pool is a later change if it ever matters. |
| API key missing or invalid | Missing → `transcription_skipped` logged, capture unaffected. Invalid → Deepgram 401 → `TranscriptionError` logged. Both silent to the user, per functional spec. |
| Audio uploaded to a third party — privacy | The known, documented v1 trade-off (product-definition §3.3). The `TranscriptionBackend` interface is the seam that makes the Phase 5 on-device swap clean. |
| Deepgram response schema drift / partial response | `DeepgramBackend` validates the response into the `Transcript` model; a parse failure → `TranscriptionError`, logged, no files written. The `.wav` is untouched. |
| Crash mid-write of the three files | Write-temp-then-rename per file; a crash leaves at most a stray temp file, never a half-written `.json/.txt/.srt`. |
| Secrets leaking into logs | The API key is never logged; `transcribe.py` logs request metadata only. `record diagnostics` already redacts config. |

---

## 4. Testing Strategy

### Unit tests (`tests/python/`)

- **`test_transcribe.py`** — feed a recorded Deepgram JSON fixture through `DeepgramBackend`'s response parser; assert `Transcript` shape, speaker renumbering (first-appearance order), language passthrough. `httpx` mocked via `MockTransport` — no real network. Writers: assert `.json/.txt/.srt` content and atomic-write (no temp file left behind). Error paths: non-2xx, malformed body, network error → `TranscriptionError`.
- **`test_secrets.py`** — env-var precedence over Keychain; `keyring` backend stubbed; `None` when neither is set.
- **`test_daemon.py`** (extend) — on successful stop, a transcription task is spawned with the session's audio path (backend stubbed); no key → no task spawned, warning logged; a failing task → `transcription_failed` logged, no exception escapes, daemon stays IDLE; two stops in quick succession → two independent tasks, neither blocks.
- **`test_cli.py`** (extend) — `record transcribe` exit codes (0/1/2/3); `install` prompt stores the key; blank input leaves an existing key untouched.

### Integration tests (`tests/integration/`)

- End-to-end with a **stubbed Deepgram** (`httpx` transport returning a canned response): run a capture through the daemon, assert the three transcript files appear next to the `.wav` with the right stem once the stub responds.
- Developer-only (not in CI): a real Deepgram call against a short fixture WAV, gated behind an env var / pytest marker.

### Manual smoke test

- Configure a real key via `record install`. Record a short multi-speaker call → confirm `.json/.txt/.srt` appear next to the `.wav`, speakers are consistently labelled, timestamps line up. Pull the network and record again → confirm capture still works, `orchestrator.log` shows the failure, no crash, no notification.
