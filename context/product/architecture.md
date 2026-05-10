# System Architecture Overview: record

_A privacy-first, local-first meeting recording utility for macOS. Captures screen + mic + system audio via OS-level APIs and produces a speaker-attributed transcript via a cloud transcription provider in v1. Zero footprint inside the meeting itself (no bots, no in-call indicators)._

**Cross-cutting principle — the cross-platform seam:** the architecture splits into a **platform-specific capture backend** (Swift on macOS for v1) and a **cross-platform orchestrator** (Python). They communicate over a stable JSON-line protocol. To extend to Windows or Linux later, write a new capture backend that emits the same protocol — the orchestrator does not change.

---

## 1. Application & Technology Stack

- **Capture daemon:** Swift (native macOS binary).
- **Window video capture:** ScreenCaptureKit (`SCStream`, window-region capture, macOS 13+).
- **System audio capture:** ScreenCaptureKit (same `SCStream`; replaces BlackHole-style virtual drivers).
- **Microphone capture:** AVFoundation / CoreAudio (`AVCaptureDevice` + `AVAudioEngine`).
- **Encoder/muxer:** `AVAssetWriter` (native — no ffmpeg dependency in v1).
- **Global hotkey:** `NSEvent.addGlobalMonitorForEvents` inside the Swift binary; emits an event to the orchestrator over IPC.
- **Orchestrator runtime:** Python 3.11+.
- **Orchestrator dependencies:** `httpx` (HTTP client for the transcription API), `pydantic` + `pydantic-settings` (config), `typer` (CLI), `keyring` (macOS Keychain for the API key), `structlog` (structured logging).
- **IPC mechanism:** JSON-line events on stdout, JSON-line commands on stdin. The orchestrator launches the Swift binary as a long-running subprocess and parses its event stream.
- **Capture-backend protocol (the cross-platform seam):** documented JSON-line schema with events such as `started`, `stopped`, `audio_file`, `video_file`, `error`, and commands such as `start`, `stop`, `shutdown`. Stable across backends.

---

## 2. Data & Persistence

- **Database (Phase 1):** None. SQLite is introduced in Phase 4 with the app-managed library.
- **Recording root (default):** `~/Movies/record/` (macOS-native `~/Movies` location for video; user-configurable).
- **Per-meeting layout:** one self-contained directory per recording at `~/Movies/record/{ISO-8601-timestamp}/` containing:
  - `video.mp4` — H.264 + AAC, active meeting window only.
  - `audio.wav` — uncompressed PCM, mixed mic + system audio (stereo, 16-bit, 48 kHz).
  - `transcript.json` — source-of-truth: array of segments with `speaker`, `start`, `end`, `text`.
  - `transcript.txt` — human-readable derivative.
  - `metadata.json` — sidecar: start/end timestamps, duration, source app (if detected), transcript provider + model, capture binary version.
- **Audio format choice:** WAV uncompressed in v1 — best transcription input quality, simplest pipeline. ~10 MB/min is acceptable for daily use; archive-side compression deferred.
- **Config file:** `~/Library/Application Support/record/config.toml` (Apple HIG default), with `$XDG_CONFIG_HOME/record/config.toml` honored if set (forward-looking for cross-platform).
- **Secrets:** transcription API key stored in the **macOS Keychain** via the `keyring` Python library; never in `config.toml`. Environment variable (`RECORD_DEEPGRAM_API_KEY`) supported as a developer fallback.

---

## 3. Infrastructure & Deployment

- **Cloud infrastructure:** None. This is a single-user desktop tool — there is no server side.
- **Distribution (v1):** Unsigned developer build attached to GitHub Releases. Suitable while the audience is the developer + pilot users. Users right-click → Open to bypass Gatekeeper on first launch.
- **Daemon lifecycle (v1):** the orchestrator launches and supervises the Swift capture binary as a child process. **No LaunchAgent autostart in v1** — the user runs the orchestrator (`record start`-style CLI) themselves. LaunchAgent autostart and signed releases land alongside Phase 2's auto-detection feature, which needs always-on operation.
- **Build pipeline (v1):** local builds. Swift binary built via `swift build`; Python orchestrator distributed as a wheel or a uv-managed venv. Release packaging is a manual `make release` script that zips both.
- **Roadmap items deferred:** code signing + notarization, Homebrew tap, LaunchAgent plist, auto-update mechanism. All needed before non-developer users can adopt the product, and all sit between Phase 1 and Phase 2.

---

## 4. External Services & APIs

- **Cloud transcription provider:** **Deepgram (Nova-3)** with built-in speaker diarization. Single API call returns transcript + speaker labels + word-level timestamps. Selected over AssemblyAI for speed and per-minute cost; selected over OpenAI Whisper API because Whisper has no built-in diarization.
- **Provider abstraction:** the orchestrator defines a `TranscriptionBackend` interface (`transcribe(audio_path) -> Transcript`). The Deepgram implementation is the v1 backend. Phase 5's on-device pipeline (whisper.cpp + pyannote / sherpa-onnx) plugs in behind the same interface.
- **macOS permissions (TCC) required:**
  - **Screen Recording** — for ScreenCaptureKit (window video + system audio).
  - **Microphone** — for AVCaptureDevice.
  - **Accessibility** — for the Swift binary's global hotkey monitor.
  - The orchestrator detects missing permissions on first run and surfaces a clear actionable message rather than failing silently.
- **No other external services.** No analytics, no crash reporting, no auth/identity, no payments.

---

## 5. Observability & Monitoring

- **Logging (both daemon and orchestrator):** structured logs (JSON via `structlog` on the Python side; OSLog or stderr-with-formatter on the Swift side) to `~/Library/Logs/record/`:
  - `daemon.log` — capture binary events.
  - `orchestrator.log` — orchestrator events including transcription pipeline.
- **Log rotation:** size-based rotation (e.g., 10 MB per file, keep last 5).
- **Diagnostics bundle:** `record diagnostics` CLI command zips logs + the active config (with the API key redacted) into a single archive for the user to attach when filing issues.
- **Telemetry:** **None.** No remote crash reporting, no usage metrics. Aligns with the privacy-first positioning. Any future opt-in telemetry must be off by default and clearly disclosed.
- **Health checks:** the orchestrator validates daemon connectivity (subprocess alive, JSON event stream flowing) on every start; failures surface as actionable error messages.
