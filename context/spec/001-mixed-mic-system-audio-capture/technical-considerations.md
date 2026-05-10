# Technical Specification: Mixed Mic + System Audio Capture

- **Functional Specification:** `context/spec/001-mixed-mic-system-audio-capture/functional-spec.md`
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

This is the **first implementation of the architecture's two-process seam** — a Swift capture binary (`record-capture`) and a Python orchestrator that supervises it via JSON-line IPC over stdin/stdout.

For this spec only, the Swift binary is built in an **audio-only mode** (no video capture yet — that's a separate spec). It captures the user's microphone and the system's audio output, mixes them in software, and writes a single mono 16 kHz / 16-bit PCM `.wav`.

The Python orchestrator exposes a Typer CLI with `record start` and `record stop`. `record start` spawns a detached supervisor process that owns the Swift binary; the prompt returns immediately. `record stop` finds the running supervisor through a PID file, asks it to stop, reads the resulting state file, and prints the final summary. Live verbose logs stream to log files only — the user runs `tail -f` in another terminal if they want to watch.

**Intentional deviations from `context/product/architecture.md`** (each will be revisited in a follow-on spec):

| Architecture says | This spec uses | Reason |
|---|---|---|
| `~/Movies/record/{ts}/audio.wav` | `$CWD/{ts}.wav` | Functional spec's placeholder pending the "local folder output" spec. |
| WAV stereo, 16-bit, 48 kHz | WAV mono, 16-bit, 16 kHz | Speech-optimized; smaller; perfect for Deepgram. Will be re-evaluated when transcription lands. |

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 New repository layout

```
record/                          (repo root, existing)
├── pyproject.toml               (existing — gains deps)
├── Makefile                     (new — orchestrates Swift build + Python install)
├── swift-capture/               (new — Swift package)
│   ├── Package.swift
│   └── Sources/RecordCapture/
│       ├── main.swift
│       ├── AudioCapture.swift
│       ├── WAVWriter.swift
│       ├── Permissions.swift
│       ├── Protocol.swift
│       └── StateFile.swift
└── src/record/                  (new — Python package)
    ├── __init__.py
    ├── cli.py                   (Typer: `start`, `stop`)
    ├── supervisor.py            (long-running child of `start`; owns Swift subprocess)
    ├── ipc.py                   (pydantic models for JSON-line protocol)
    ├── state.py                 (PID file + capture-state.json)
    ├── paths.py                 (XDG/macOS path resolution)
    ├── logging_setup.py         (structlog → ~/Library/Logs/record/orchestrator.log)
    └── bin/record-capture       (built artifact; gitignored; populated by Makefile)
```

### 2.2 Swift capture binary (`record-capture`)

- **Build target:** macOS 13.0+ executable. Built via `swift build -c release` from `swift-capture/`.
- **Frameworks:** `ScreenCaptureKit`, `AVFoundation`, `CoreAudio`, `AppKit`, `Foundation`.
- **Process model:** foreground process. Reads JSON-line commands on stdin, emits JSON-line events on stdout, structured logs to stderr (also tee'd to `~/Library/Logs/record/daemon.log` by the supervisor).
- **Source files (responsibilities only — no implementations):**
  - `main.swift` — stdin command loop, command dispatch, graceful shutdown on `stop` / SIGTERM.
  - `Permissions.swift` — preflight checks for `AVCaptureDevice.authorizationStatus(for: .audio)` and Screen Recording (`CGPreflightScreenCaptureAccess()` / SCK availability check). Triggers macOS prompts via `AVCaptureDevice.requestAccess` and the first `SCShareableContent.current` call.
  - `AudioCapture.swift` — wires up the system-audio source (`SCStream` configured for audio-only with `SCStreamConfiguration.capturesAudio = true`) and the mic source (`AVAudioEngine` input node). Owns an `AVAudioMixerNode` that feeds `WAVWriter`. Handles `AVAudioEngine` configuration-change notifications and `SCStreamDelegate` errors → emits `source_lost` events.
  - `WAVWriter.swift` — wraps `AVAudioFile` (or `ExtAudioFile`) configured for **mono, 16-bit signed integer PCM, 16 kHz**. Resamples mic + system audio to the target rate via `AVAudioConverter` before mixing.
  - `Protocol.swift` — `Codable` structs for the protocol below.
  - `StateFile.swift` — atomic write (write-temp-then-rename) of `capture-state.json` on every status change.

### 2.3 Python orchestrator

- **Dependencies added to `pyproject.toml`:** `typer`, `pydantic`, `structlog`. (No `httpx`/`keyring` yet — they belong to the transcription specs.)
- **Entry point:** `record = record.cli:app` registered in `[project.scripts]`.
- **Source files:**
  - `cli.py` — Typer app exposing only `start` and `stop` for this spec.
  - `supervisor.py` — long-running process spawned by `record start` (via `os.fork` + `os.setsid` + `os.execvp`, or `subprocess.Popen` with `start_new_session=True`). Owns the Swift binary subprocess, reads its stdout JSON-line stream, writes stderr to `daemon.log`, updates `capture-state.json`, listens for SIGTERM (sent by `record stop`) → forwards `stop` command to the Swift binary → waits for clean exit → writes final state → exits.
  - `ipc.py` — pydantic models matching the Swift `Codable` structs. Single source of truth for the schema (Swift side hand-mirrors).
  - `state.py` — PID file + state file CRUD with stale-PID detection (`os.kill(pid, 0)` test).
  - `paths.py` — resolves `~/Library/Application Support/record/`, `~/Library/Logs/record/`. (No `$XDG_CONFIG_HOME` work yet; deferred.)
  - `logging_setup.py` — `structlog` JSON renderer → file handler.

### 2.4 CLI behavior (this spec)

| Command | Behavior | Exit codes |
|---|---|---|
| `record start` | Resolve absolute output path (`$CWD/{ISO-8601}.wav`). Check PID file — if alive, exit 1 with "capture already in progress". Otherwise spawn detached supervisor, write PID file, return. | 0 success; 1 already running; 2 permission denied; 3 binary not found / launch failure. |
| `record stop` | Read PID file. If absent or stale → exit 1 with "no capture running". Otherwise SIGTERM the supervisor, wait (with timeout) for it to exit, read final `capture-state.json`, print summary, remove PID + state files. | 0 success; 1 no capture; 4 supervisor did not exit cleanly. |

### 2.5 File and state locations

| Purpose | Path |
|---|---|
| Output WAV | `$CWD/{ISO-8601-timestamp}.wav` (timestamp like `2026-05-10T14-32-08`) |
| Capture PID | `~/Library/Application Support/record/capture.pid` |
| Capture state | `~/Library/Application Support/record/capture-state.json` |
| Daemon log | `~/Library/Logs/record/daemon.log` |
| Orchestrator log | `~/Library/Logs/record/orchestrator.log` |

### 2.6 `capture-state.json` shape

```
{
  "pid": 12345,
  "start_time": "2026-05-10T14:32:08Z",
  "output_path": "/abs/path/to/2026-05-10T14-32-08.wav",
  "sources": {
    "mic":          {"status": "attached"|"lost"|"never_attached", "attached_at": "...", "lost_at": null},
    "system_audio": {"status": "...", "attached_at": "...", "lost_at": null}
  },
  "warnings": [
    {"timestamp": "...", "source": "mic", "message": "input device disconnected"}
  ],
  "last_event_at": "2026-05-10T14:35:11Z"
}
```

### 2.7 JSON-line IPC protocol (subset for this spec)

This spec defines the audio-related subset of the architecture's documented protocol. Later specs will extend it with `video_*`, `transcription_*`, etc.

**Commands (orchestrator → daemon, on daemon stdin):**

| Command | Payload |
|---|---|
| `start` | `{"cmd":"start","output_path":"/abs.wav","format":{"sample_rate":16000,"bit_depth":16,"channels":1}}` |
| `stop`  | `{"cmd":"stop"}` |
| `shutdown` | `{"cmd":"shutdown"}` (graceful exit, no capture) |

**Events (daemon → orchestrator, on daemon stdout):**

| Event | Payload |
|---|---|
| `ready` | `{"event":"ready"}` |
| `permission_required` | `{"event":"permission_required","kind":"microphone"\|"screen_recording"}` |
| `permission_denied` | `{"event":"permission_denied","kind":"..."}` |
| `started` | `{"event":"started","start_time":"..."}` |
| `source_attached` | `{"event":"source_attached","source":"mic"\|"system_audio"}` |
| `source_lost` | `{"event":"source_lost","source":"...","at_offset_seconds":134.2,"reason":"..."}` |
| `stopped` | `{"event":"stopped","duration_seconds":...,"output_path":"..."}` |
| `error` | `{"event":"error","message":"..."}` |

### 2.8 Permission flow

1. On `start` command, daemon checks both permissions.
2. If a permission status is `notDetermined`, daemon emits `permission_required` and triggers the macOS prompt. Capture waits.
3. If a permission ends up `denied`/`restricted`, daemon emits `permission_denied` and exits non-zero.
4. Orchestrator translates `permission_denied` events into the user-facing CLI message (naming the System Settings panel) and exits 2.

### 2.9 Mid-capture resilience

- The mixer always pulls from both inputs. If `AVAudioEngine` fires `audioEngineConfigurationChange` (mic device change) or `SCStream` errors out (system-audio source lost), the affected input is detached, marked `lost` in the state file with `at_offset_seconds`, a warning is logged, and the mixer continues with the remaining input (silence buffers fill the lost side).
- The final WAV is always finalized from whatever was actually mixed. `record stop`'s summary surfaces lost-source timestamps from the state file.

### 2.10 Single-instance enforcement

- Atomic PID-file creation via `O_CREAT|O_EXCL` write.
- On collision: open existing PID file, check `os.kill(pid, 0)`. Alive → refuse with "already in progress". Dead/missing process → treat as stale, remove, retry once.

### 2.11 Build & packaging

- **Makefile targets:**
  - `make swift` — `swift build -c release` in `swift-capture/`, copy binary to `src/record/bin/record-capture`, `chmod +x`.
  - `make install` — `make swift` + `uv pip install -e .`
  - `make test` — Python tests + Swift tests.
- The Swift binary is **bundled inside the Python package** at `record/bin/record-capture`. The orchestrator resolves it via `importlib.resources.files("record") / "bin" / "record-capture"` at startup.
- `src/record/bin/` is `.gitignore`d; the binary is a build artifact.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **macOS 13.0+** required for ScreenCaptureKit's audio capture.
- **TCC permissions:** Microphone + Screen Recording. Not Accessibility yet (no hotkey in this spec).
- **No external services** in this spec — Deepgram integration belongs to the transcription specs.
- This spec **establishes the JSON-line protocol schema** that all later capture features will extend. Schema changes after this spec lands will require coordinated Swift + Python edits.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| First-run TCC prompt UX is confusing — user runs `record start`, prompt returns immediately, then macOS dialogs appear with no terminal context. | The supervisor logs a clear "waiting for permission" line to `daemon.log`; the README documents the first-run flow. `record stop` after a permission denial reads the state file and surfaces "denied: <kind>". |
| `record start`'s CWD vs. `record stop`'s CWD mismatch — if user `cd`s before stopping, file is "missing". | `record start` resolves the absolute output path before forking and stores it in `capture-state.json`. `record stop` reads it from the state file, not from its own CWD. |
| Stale PID file after a crash. | `record stop` validates the PID is alive; if dead, reports "supervisor crashed" with whatever partial state exists in the file, then cleans up. |
| Sample-rate conversion CPU cost (mic at e.g. 48 kHz → 16 kHz). | `AVAudioConverter` is hardware-accelerated and well below relevant cost. No mitigation needed; flag in testing. |
| Swift binary bundled inside the Python package complicates editable installs. | Makefile re-runs `swift build` and copies the binary; the Python loader rechecks the bundled path at every `start`. Document the `make install` flow. |
| Architecture-doc deviations (mono/16 kHz/CWD) drift from the wider design. | Explicit deviation table in §1; both deviations have a named follow-on spec that will revisit them. |
| Detached-daemon double-fork edge cases (zombie processes, terminal-session ownership). | Use `subprocess.Popen(..., start_new_session=True)` and explicitly close stdio in the child to avoid the controlling-terminal trap. |

---

## 4. Testing Strategy

### Unit tests (Python — `tests/python/`)

- `test_ipc.py` — round-trip every command/event through pydantic models; reject malformed JSON-lines.
- `test_state.py` — PID file create/stale-detect/cleanup; state file atomic write; concurrent-read consistency.
- `test_cli.py` — Typer `runner` invoking `start`/`stop` against a stub supervisor (subprocess replaced with a fake binary that emits scripted JSON lines). Verifies exit codes for the table in §2.4.
- `test_paths.py` — XDG/macOS path resolution.

### Unit tests (Swift — `swift-capture/Tests/`)

- `ProtocolTests.swift` — `Codable` round-trip for every command/event struct against the same JSON fixtures used by `test_ipc.py` (shared fixture directory).
- `StateFileTests.swift` — atomic write semantics under `dispatch_async` racing.

### Integration tests (`tests/integration/`)

- Spawn the actual Swift binary against a controlled audio source. Two tactics, in order of preference:
  1. **Silent-source mode:** add a `--test-silent-sources` flag to the Swift binary that bypasses real `SCStream` / `AVAudioEngine` and feeds the mixer with deterministic synthetic buffers (e.g., 1-second of silence then 1-second of a 440 Hz tone). No TCC permissions required, no audio hardware, runnable in CI.
  2. **Headless real capture (developer machine only):** rely on granted TCC permissions; verify against a sine-wave file played by `afplay`.
- Verifies:
  - Expected event sequence on stdout (`ready` → `started` → `source_attached` ×2 → `stopped`).
  - Resulting WAV header is mono / 16 kHz / 16-bit PCM (verify with Python `wave` module).
  - Resulting WAV duration ≈ requested capture duration (within ±100 ms).
  - State file at end contains expected sources and no warnings.
  - Mid-capture resilience: a synthetic `source_lost` injection produces the expected warning entry and the WAV is still well-formed.

### Manual smoke test (documented in spec README)

- Real terminal, real meeting app playing audio, real microphone.
- `record start` → talk + system audio for ~30 s → `record stop`.
- Verify `.wav` plays back with both sources audible; verify daemon.log shows expected events; verify state files are cleaned up.
