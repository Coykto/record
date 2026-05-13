# Technical Specification: Hotkey-Triggered Background Daemon

- **Functional Specification:** `context/spec/003-hotkey-triggered-background-daemon/functional-spec.md`
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

This spec dissolves the per-capture supervisor model from spec 001 into a single, long-lived Python process — `record-daemon` — that owns the hotkey-driven lifecycle. The daemon:

- supervises **one** long-lived Swift `record-capture` child running in a new **daemon mode**, which gains a global hotkey listener and learns to accept repeated `start` / `stop` cycles instead of `exit()`-ing after each capture;
- listens on a **Unix-domain socket** for control messages from the `record` CLI (`start`, `stop`, `status`, `quit`);
- runs under **launchd** as a per-user LaunchAgent, registered by a new `record install` command;
- reads a small **TOML config** at `~/.config/record/config.toml` (pydantic-settings) for the hotkey, output folder, log folder, and audible-feedback switch;
- plays start/stop/error sounds via `afplay` and surfaces hotkey errors via `osascript`-driven notification banners.

Hotkey detection sits inside the Swift binary, using **Carbon `RegisterEventHotKey`** (rather than `NSEvent.addGlobalMonitorForEvents`) so that a conflict with another app is reported as a hard `eventHotKeyExistsErr` rather than silently swallowed — required to satisfy FR 2.13's explicit "hotkey may be inactive" surface in `record status`.

The existing CLI commands (`record start`, `record stop`) keep their names but now route through the daemon's control socket rather than spawning a `python -m record.supervisor` subprocess; the legacy supervisor entry-point is retained as `python -m record.supervisor` for tests and emergency offline use, but is no longer reachable through the `record` CLI when the daemon is running. The capture pipeline itself (audio + video + state file + finalize semantics) is **unchanged** from specs 001 and 002 — this spec only changes who *triggers* a capture and where the orchestrator process lives.

**Intentional deviations from `context/product/architecture.md`** (each is forced by the functional spec):

| Architecture says | This spec uses | Reason |
|---|---|---|
| Config at `~/Library/Application Support/record/config.toml` (XDG honored if set) | `~/.config/record/config.toml` (hard-coded) | FR 2.10 names this path explicitly as the single user-facing knob; matches the consultant's expectation of a Unix-style dotfile location. |
| Output root `~/Movies/record/{ts}/` (per-meeting folder) | `~/record/` flat layout, configurable as `output_folder` | FR 2.11 makes this the daemon's interim contract; full layout is deferred to the "local folder output" spec. |
| Daemon log at `~/Library/Logs/record/daemon.log` | `~/record/logs/daemon.log`, configurable as `log_folder` | FR 2.14 names this path and requires it to be sibling to the recordings, not under `~/Library`. The Swift child's stderr-drain log moves with it; orchestrator structlog output is folded into the same file (see §2.10). |
| Hotkey via `NSEvent.addGlobalMonitorForEvents` | Carbon `RegisterEventHotKey` | FR 2.13 requires conflict detection; NSEvent has no API for it. |
| "No LaunchAgent autostart in v1" | LaunchAgent autostart shipped via `record install` | This is the autostart spec; the architecture's "v1" referred to the pre-daemon Phase 1 contour. |

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 Repository layout

Additions only — no renames.

```
src/record/
├── (existing files — see §2.10 for edits)
├── daemon.py             (NEW) — long-running daemon process; entrypoint `python -m record.daemon`.
├── control.py            (NEW) — Unix-domain socket server + client; JSON-line request/response framing.
├── config.py             (NEW) — pydantic-settings models for ~/.config/record/config.toml.
├── launchagent.py        (NEW) — install/uninstall the LaunchAgent plist; `launchctl` shell-outs.
├── feedback.py           (NEW) — afplay-based sound playback + osascript notification banner.
└── hotkey.py             (NEW) — pydantic model + parser for the hotkey-string config value.

swift-capture/Sources/RecordCapture/
├── (existing files — see §2.3 for edits)
└── HotkeyMonitor.swift   (NEW) — Carbon RegisterEventHotKey wrapper; emits hotkey_pressed events.

context/spec/003-hotkey-triggered-background-daemon/
├── functional-spec.md         (existing)
└── technical-considerations.md (this file)
```

### 2.2 The Python daemon (`record.daemon`)

Single process, single thread of control on the asyncio event loop. Responsibilities:

1. **Lifecycle management of the Swift child** — spawn `record-capture --daemon` at startup; reap and respawn on unexpected exit (bounded restart loop, see §3); send `shutdown` on graceful daemon stop.
2. **Hotkey registration** — on Swift `ready`, send a `register_hotkey` command derived from the current config. Translate the resulting `hotkey_registered` event into a state field consumed by `record status`.
3. **Capture state machine** — exactly the three states from FR 2.1: `IDLE`, `STARTING`, `RUNNING`, `STOPPING`. Hotkey presses, `start` IPC requests, and `stop` IPC requests are funneled through a single asyncio lock so that double-press / race-against-stop / race-against-start (FR 2.5 final bullet) cannot lead to two captures.
4. **Per-capture orchestration** — on a `start` decision: compute output paths under the configured `output_folder`, send the existing `start` command (spec 001 / 002 payload) to the Swift child, write `capture-state.json` from the event stream exactly as `supervisor.py` does today.
5. **Control socket** — accept connections on `~/Library/Application Support/record/daemon.sock`, dispatch requests against the state machine. Socket lives under Application Support (not under `output_folder`) because it's machine-local state, not user content.
6. **Feedback** — invoke `feedback.play_start()` / `play_stop()` / `play_error()` and `feedback.notify(message)` at the right state transitions; gated on the `audible_feedback` config flag (sounds only — notifications always fire on error per FR 2.9).
7. **Logging** — structlog to the configured `log_folder`/`daemon.log`. The Swift child's stderr drain writes to the same file (interleaved, line-prefixed by stderr drainer; see §2.10).

The daemon's PID lives in `~/Library/Application Support/record/daemon.pid`, claimed atomically at startup (`O_CREAT|O_EXCL`, same pattern as `state.claim_pid_file`). The existing `capture.pid` semantics from 001 are dropped — the daemon process *is* the single instance, and the capture-running flag lives in the in-memory state machine plus `capture-state.json`.

### 2.3 Swift binary — daemon-mode extensions

The existing `main.swift` is patched (not rewritten) to support a single new flag and to relax the "exit after `stopped`" assumption.

**New CLI flag:** `--daemon`.
- When absent, the binary behaves **exactly as today** (one-shot capture, `exit(0)` after `stopped`). This preserves the foreground `python -m record.supervisor` test path and the integration suite's `--test-synthetic-video` use.
- When present, the binary:
  - emits `ready` and then idles, waiting for commands (no implicit `start`);
  - resets `capture = nil` after `stopped` instead of `exit()`-ing — ready for another `start`;
  - accepts the new `register_hotkey` / `unregister_hotkey` commands at any time;
  - only exits on `shutdown` command, stdin EOF, or SIGTERM (existing handler unchanged).

**New file: `HotkeyMonitor.swift`** — wraps Carbon's `RegisterEventHotKey`:
- `register(modifiers: UInt32, keyCode: UInt32) throws -> Result` where `Result` is one of `.registered`, `.conflict`, `.invalid`. The conflict case is recognized via `OSStatus == eventHotKeyExistsErr`.
- Uses Carbon's `InstallEventHandler` on `EventTypeSpec(eventClass: kEventClassKeyboard, eventKind: kEventHotKeyPressed)` to receive presses. Each press posts a `Hotkey.pressed` notification on the main queue, which `main.swift` translates into a `hotkey_pressed` event on stdout.
- `unregister()` calls `UnregisterEventHotKey`; idempotent.
- Carbon API import: `import Carbon.HIToolbox`. No new framework link — Carbon is already in the SDK.

**main.swift edits:**
- Read `--daemon` early in `parseCLIFlags`.
- Add `register_hotkey` / `unregister_hotkey` to the command dispatch in `stdinThread`.
- Replace the `exit(0)` at the end of `handleStop`'s task with: if `--daemon`, `capture = nil` and emit nothing further; else, existing `exit(0)`.
- The `atexit` and `SIGTERM` mp4-finalize paths stay as they are.

**Permission surface:** Carbon's `RegisterEventHotKey` requires **Accessibility** TCC permission (Privacy & Security → Accessibility), distinct from Screen Recording and Microphone. The Swift binary triggers the standard macOS Accessibility prompt on first registration attempt. On denial, `register` returns `.invalid` with an "accessibility denied" diagnostic; the daemon logs and keeps running (FR 2.12).

### 2.4 JSON-line protocol — additions

This spec extends the protocol from 001 (extended again in 002). All existing commands and events are unchanged.

**New commands** (orchestrator → Swift on stdin):

| Command | Payload |
|---|---|
| `register_hotkey` | `{"cmd":"register_hotkey","modifiers":["cmd","option"],"key":"r"}` — `modifiers` is a closed set: `cmd` / `option` / `control` / `shift`. `key` is a single non-modifier key (letters, digits, function keys; see §2.8 for the parse rules). |
| `unregister_hotkey` | `{"cmd":"unregister_hotkey"}` |

**New events** (Swift → orchestrator on stdout):

| Event | Payload |
|---|---|
| `hotkey_registered` | `{"event":"hotkey_registered","status":"registered"\|"conflict"\|"invalid","modifiers":["cmd","option"],"key":"r","message":"..."}` — emitted in response to every `register_hotkey`, plus once on daemon-mode startup if a hotkey was pre-registered. |
| `hotkey_pressed` | `{"event":"hotkey_pressed"}` — emitted once per press while a hotkey is currently registered. |
| `hotkey_unregistered` | `{"event":"hotkey_unregistered"}` — emitted in response to `unregister_hotkey`. |

**Modified discriminator behavior:** `record.ipc` already uses a pydantic `Field(discriminator="cmd"/"event")` `Union`; the three new event models and two new command models are appended to those unions. No breaking change to the wire format.

### 2.5 Control IPC — Unix-domain socket between CLI and daemon

**Socket path:** `~/Library/Application Support/record/daemon.sock` (created by the daemon on startup; removed on graceful exit). Permission mode `0600` so only the user can connect.

**Framing:** one request per line, one response per line — same convention as the Swift IPC. Newline-terminated UTF-8 JSON. The daemon closes the connection after replying so the CLI never has to handle multi-response streams.

**Request schema (pydantic models, live in `record/control.py`):**

| Request | Payload | Daemon behavior |
|---|---|---|
| `start` | `{"op":"start"}` | If `IDLE` → start a capture (delegates to the per-capture orchestration in §2.2). If `RUNNING`/`STARTING` → respond `{"status":"already_running","pid":<daemon pid>,"capture_id":"..."}`. If `STOPPING` → respond `{"status":"busy","detail":"capture is being finalized"}`. |
| `stop` | `{"op":"stop"}` | If `RUNNING` → stop the capture (same path as a hotkey press while running). If `IDLE` → respond `{"status":"not_running"}`. |
| `status` | `{"op":"status"}` | Always responds with the full status payload (§2.6 below). |
| `quit` | `{"op":"quit","finalize":true\|false}` | Graceful daemon shutdown. If a capture is in progress and `finalize=true` (the default), the daemon stops it cleanly first; if `finalize=false`, the daemon refuses with `{"status":"capture_in_progress"}`. Used by `record daemon stop`. |

**Response schema:** every response has at minimum `{"status":"ok"|"error"|...}` plus request-specific fields. Errors carry a `detail` string suitable for direct echo to the user's terminal.

**CLI side:** a thin client in `record/control.py` opens the socket, sends one line, reads one line, exits. Timeout: 5 s on connect, 30 s on response (covers a slow finalize). On `ECONNREFUSED` / missing-socket the CLI prints `daemon is not running — try \`record daemon start\` or \`record install\`` and exits non-zero (FR 2.7 third bullet).

### 2.6 `record status` payload

A single `status` request returns:

```
{
  "status": "ok",
  "daemon": {
    "running": true,
    "pid": 12345,
    "started_at": "2026-05-13T09:14:02Z",
    "autostart_registered": true        // bool, derived from `launchctl print` (§2.7)
  },
  "hotkey": {
    "configured": "option+command+r",
    "state": "registered" | "conflict" | "invalid" | "disabled_no_permission",
    "message": "..."                    // human-readable, e.g. "Accessibility permission missing"
  },
  "capture": {
    "running": true,
    "started_at": "2026-05-13T09:21:48Z",
    "duration_seconds": 184.3,
    "audio_path": "/Users/.../2026-05-13T09-21-48.wav",
    "video_path": "/Users/.../2026-05-13T09-21-48.mp4"
  } | { "running": false }
}
```

The `record status` CLI command renders this as a short, human-readable block and sets exit code 0 if `daemon.running` is true, non-zero otherwise (FR 2.4 final bullet). When the daemon is unreachable, the CLI falls back to `{"daemon":{"running":false,"autostart_registered":<probe>}}` and exits non-zero — `autostart_registered` is still meaningful via `launchctl print` even when the daemon process is dead.

### 2.7 LaunchAgent installation (`record install` / `record uninstall`)

**Plist path:** `~/Library/LaunchAgents/com.record.daemon.plist`.

**Plist shape (responsibilities, not a copy-paste artifact):**

| Key | Value | Why |
|---|---|---|
| `Label` | `com.record.daemon` | launchctl identifier. |
| `ProgramArguments` | `[<resolved python>, "-m", "record.daemon"]` | Resolved Python is `sys.executable` at install time (covers `uv`-managed venv installs). |
| `RunAtLoad` | `true` | Start on login session bootstrap. |
| `KeepAlive` | `{"SuccessfulExit": false}` | Restart on crash, but not after a clean `record daemon stop`. |
| `ProcessType` | `Background` | macOS scheduling hint; this is a background process. |
| `StandardOutPath` / `StandardErrorPath` | `~/record/logs/daemon-launchd.{out,err}.log` | Catches catastrophic startup failures that happen before structlog is initialized. |
| `EnvironmentVariables` | `{"RECORD_LAUNCHD": "1"}` | Allows the daemon to detect launchd-managed invocation if needed (e.g. avoid double `osascript` prompts on first launch). |

**Install flow (`record install`):**
1. Write the plist to the canonical path (idempotent — overwrite if present).
2. `launchctl bootstrap gui/$UID <plist>` to register and start it (the gui domain is the per-user session domain on modern macOS; `launchctl load` is deprecated). If already bootstrapped, `bootstrap` returns an error; we detect that and run `launchctl bootout gui/$UID <plist>` then re-bootstrap, so re-install picks up plist changes.
3. Print "registered to start on login; running now (PID …)".

**Uninstall flow (`record uninstall`):**
1. `launchctl bootout gui/$UID <plist>` (idempotent — ignore "not loaded" error).
2. Remove the plist file.
3. The bootout kills the daemon; the daemon's SIGTERM handler finalizes any in-flight capture before exiting (same path as `record daemon stop --finalize`).
4. Print "removed from login items; daemon stopped".

`launchctl` is invoked via `subprocess.run`; stdout/stderr captured and surfaced on non-zero exit codes. We never silently swallow a `launchctl` failure — install/uninstall must print a clear diagnostic per FR 2.2 final bullet.

### 2.8 Configuration file

**Path:** `~/.config/record/config.toml`. Missing file → all defaults; no error (FR 2.10).

**Schema (pydantic-settings):**

| Key | Type | Default | Validation |
|---|---|---|---|
| `hotkey` | string | `"option+command+r"` | Parsed by `hotkey.parse` (§below). Invalid → fall back to default, log warning, set `status.hotkey.state = "invalid"` (FR 2.6). |
| `output_folder` | string (path) | `"~/record/"` | Tilde-expanded. Created on first capture if missing. Path must be absolute after expansion. |
| `log_folder` | string (path) | `"~/record/logs/"` | Same rules as above. Created on daemon start. |
| `audible_feedback` | bool | `true` | — |

Any other top-level key is logged at WARNING and ignored (FR 2.10 final-but-one bullet).

**Hotkey string grammar:** `<modifier>+<modifier>+...+<key>` where modifiers are case-insensitive among `cmd|command`, `opt|option|alt`, `ctrl|control`, `shift`, and `<key>` is one of `a-z`, `0-9`, `f1-f20`, or a small whitelist of named keys (`space`, `tab`, `return`, `escape`, `delete`). The parser converts this to `(modifiers: list[Literal], keyCode: int)` for the `register_hotkey` command. Empty modifier list is rejected (FR 2.6 mandates "one or more modifier keys") — though Carbon would technically allow it.

**Reload semantics:** the config is read once at daemon startup; edits take effect on next `record daemon restart` or login (FR 2.10 final bullet). No hot-reload.

### 2.9 Audible feedback + notification surface

**Sounds (FR 2.8):**

| Trigger | File |
|---|---|
| `play_start()` | `/System/Library/Sounds/Tink.aiff` |
| `play_stop()` | `/System/Library/Sounds/Pop.aiff` |
| `play_error()` | `/System/Library/Sounds/Funk.aiff` |

Implementation: `subprocess.Popen(["/usr/bin/afplay", path], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)` — fire-and-forget; the daemon does not wait for playback. `afplay` is part of every macOS install. Sound playback is short-circuited when `audible_feedback=false`.

**Notification banner (FR 2.8, FR 2.9):**

```
osascript -e 'display notification "<escaped message>" with title "record"'
```

Used for capture-cannot-start errors (FR 2.8 third bullet) and for daemon-level warnings the user must know about (e.g. Accessibility permission denied). Always fires regardless of `audible_feedback` — the flag silences sound only. The osascript subprocess call is bounded by a 2-second timeout so a hung notification subsystem can't stall the daemon.

### 2.10 Path, log, and file changes

| Concern | Before (specs 001/002) | After (this spec) |
|---|---|---|
| Output folder | `$CWD/{ts}.wav`, `$CWD/{ts}.mp4` | `<output_folder>/{ts}.wav`, `<output_folder>/{ts}.mp4`. Default `~/record/`. Auto-created. |
| Filename stem | ISO-8601 timestamp (e.g. `2026-05-13T09-21-48`) | Unchanged. |
| `capture-state.json` | `~/Library/Application Support/record/capture-state.json`, lifecycle = one per supervisor process | Same path. Lifecycle = the daemon writes through it for the currently-running capture; on `stopped`, the file remains as the post-mortem snapshot until the next capture, when it's overwritten. |
| `capture.pid` (spec 001) | Used by `record start`/`record stop` to enforce singleton | **Removed.** The daemon process is the singleton; capture-in-progress is the daemon's in-memory state. |
| `daemon.pid` (new) | — | `~/Library/Application Support/record/daemon.pid`, atomic create on daemon start. Used by `record daemon start` to refuse a second daemon, and by `launchctl bootout` failure recovery. |
| `daemon.sock` (new) | — | `~/Library/Application Support/record/daemon.sock`, 0600. |
| Daemon log | `~/Library/Logs/record/daemon.log` (Swift stderr drain) | `<log_folder>/daemon.log`. Default `~/record/logs/daemon.log`. Contains *both* the Python daemon's structlog output and the Swift child's stderr (the daemon's stderr-drain thread interleaves them, line-buffered). |
| Orchestrator log | `~/Library/Logs/record/orchestrator.log` (separate file in 001/002) | Folded into `daemon.log` — single log file matches FR 2.14 wording ("a log file inside the configured log_folder"). The 002 system-event summary write moves to the same file. |

The `paths.py` module gains a `daemon_pid_file()` / `daemon_socket()` and gains a "resolve from config" overload that takes a `Config` object for `output_folder` / `log_folder`. The hard-coded `~/Library/Logs/record/` constants stay reachable as a fallback for the foreground `supervisor.py` test path.

### 2.11 CLI changes (`record/cli.py`)

The existing `start` / `stop` commands change their implementation but keep their user-visible contract (FR 2.7):

- `record start` — opens the control socket, sends `{"op":"start"}`. Translates `already_running` into exit-1 with a clear message; translates `not_connected` into the "daemon is not running" message per FR 2.7.
- `record stop` — same pattern with `{"op":"stop"}`. The summary printer (the `_summarize_*` helpers in current `cli.py`) is reused — but it now reads `capture-state.json` *after* the daemon has finalized it (the daemon writes `final: true` exactly as the supervisor does today before responding to the `stop` request).

New subcommands:

| Command | Behavior |
|---|---|
| `record install` | §2.7 install flow. |
| `record uninstall` | §2.7 uninstall flow. |
| `record daemon start` | If a daemon is already running (per `daemon.pid` + `is_alive`) → print "daemon already running" and exit 0. Else spawn `python -m record.daemon` detached (same Popen pattern current `cli.py:start` uses for the supervisor). Wait briefly for the socket to appear (handshake same idea as `_wait_for_early_failure`). |
| `record daemon stop` | Send `{"op":"quit","finalize":true}` to the socket. Wait for the socket to close + daemon PID to disappear (10 s timeout, matches existing `_STOP_TIMEOUT_SECONDS`). |
| `record daemon restart` | `daemon stop` (finalize=true) then `daemon start`. |
| `record status` | §2.6 status request. |

Existing supervisor-related code in `cli.py` is reduced to a thin "client of the daemon"; the `_resolve_capture_binary` / `_wait_for_early_failure` helpers move into a new shared location used by both `cli.py:daemon_start` and `daemon.py`.

### 2.12 Hotkey conflict detection

`HotkeyMonitor.register` returns one of three terminal outcomes, translated into the `hotkey_registered` event's `status` field:

| Carbon return | Wire `status` | Surfaced in `record status` |
|---|---|---|
| `noErr` | `"registered"` | `state = "registered"` |
| `eventHotKeyExistsErr` | `"conflict"` | `state = "conflict"`, message names the configured combo |
| `paramErr` / `invalidPSNErr` / unknown OSStatus | `"invalid"` | `state = "invalid"`, message names the parse / OS error |
| (Accessibility TCC denied — no Carbon call reaches RegisterEventHotKey because Carbon refuses to fire the event-tap install without it) | `"invalid"` with detail `"accessibility_denied"` | `state = "disabled_no_permission"` |

The daemon never **retries** registration silently — a conflict reported once stays as `state = "conflict"` until the next `record daemon restart` (FR 2.13 final bullet: "user can fix the conflict and then `record daemon restart` to retry").

### 2.13 Behavior inherited from specs 001 and 002

Per FR 2.15: the audio + video capture, mid-capture resilience, system-event-triggered shutdown, mp4 finalize semantics, "audio required / video best-effort", and "invisible to the meeting" are all unchanged. Concretely:

- Each capture session sends the same `start` command, receives the same event stream, and writes the same `capture-state.json` schema as today.
- The "screen lock / display sleep / system sleep ends the capture cleanly" path from 002 is unchanged — but the daemon now observes the resulting clean Swift exit (in fact in daemon mode, the Swift child no longer exits; it just emits `capture_ended_by_system_event` + `stopped` and resets to idle). The daemon writes the same `orchestrator.log` summary line that today's `supervisor.py` writes, now into `daemon.log` (§2.10).
- The `--test-synthetic-video` and `--test-silent-sources` flags continue to work on the one-shot Swift path and are also accepted under `--daemon` so the integration suite can drive end-to-end daemon-mode tests in CI without TCC.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **macOS 13.0+** — unchanged.
- **launchd:** required for autostart. The `launchctl bootstrap gui/$UID` invocation is the modern (macOS 11+) idiom and is stable on the target OS range.
- **TCC permissions:** *new* requirement — **Accessibility** (for Carbon `RegisterEventHotKey`). Microphone, Screen Recording unchanged. Without Accessibility, the daemon runs and the terminal path still works; only the hotkey is disabled (FR 2.12).
- **Swift framework usage:** adds `Carbon.HIToolbox` (already in the SDK; no new link target). No new Python dependencies — `pydantic-settings` was already implied by the architecture's mention.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **State leakage across captures in daemon mode** — the Swift binary was designed as one-shot; reusing the process for many captures could leak file descriptors, encoder state, or SCStream subscriptions. | Audit `handleStop` for every resource lifetime: `AudioCapture`, `VideoSource`, `DisplayMonitor`, `SystemEventMonitor`, `MP4Writer`, `AVAudioEngine`. After `stopped`, the `capture = nil` assignment must drop the last reference to every one of them. Add an integration test that runs 5 back-to-back captures in a single daemon-mode binary and asserts file descriptor count + memory footprint do not grow monotonically. |
| **Carbon hotkey on Apple Silicon under Rosetta / virtualization** is known to occasionally miss key events. Also, `eventHotKeyExistsErr` is only returned for system-wide conflicts; a Carbon HotKey registered by a *different* Carbon API path elsewhere may or may not be detected. | Document as a known limitation in `daemon.log` on registration. Treat the `conflict` state as advisory: the daemon stays running and the terminal CLI works regardless. Empirically verify across macOS 13 / 14 / 15. |
| **Accessibility prompt fatigue** — a TCC prompt on first daemon start, launched by launchd in a session with no foreground terminal, may go un-noticed by the user. | The plist's `RunAtLoad=true` runs the daemon during login, when a TCC prompt is *visible* to the user in the standard way. The first `record install` runs the daemon synchronously in the foreground (via `launchctl bootstrap` returning after start), so the prompt fires while the user is at the terminal — they see it. We also surface a notification banner naming the missing permission if the registration comes back `disabled_no_permission`. |
| **Sockets surviving a crashed daemon** — a stale `daemon.sock` will cause `bind` to fail on next start. | On daemon start, attempt to connect to the existing socket; if it answers, refuse to start (another daemon is alive); if it errors / times out, `unlink` the stale socket and proceed. Same pattern as `claim_pid_file` for the PID file. |
| **launchctl error reporting is opaque** — `bootstrap` can fail with an `EALREADY` or domain errors that aren't self-explanatory. | Capture stderr from every `launchctl` call and surface it verbatim with the exit code. Document the most common bootstrap-after-uninstall race in the daemon log. |
| **Race: user presses hotkey while daemon is mid-startup** — the hotkey can fire before `register_hotkey` has been sent and acknowledged. | The Swift child only emits `hotkey_pressed` after a successful `hotkey_registered`, so this race is impossible by construction. The daemon's startup ordering is: spawn Swift child → wait for `ready` → send `register_hotkey` → consume `hotkey_registered` before opening the control socket. |
| **Two captures via double-press** — pressing the hotkey during `STARTING` or during the `stop`-then-finalize window must not start a second capture (FR 2.5). | The daemon's state machine uses a single asyncio lock around state transitions. Hotkey presses received in `STARTING` / `STOPPING` are logged + audibly errored (Funk.aiff) but discarded. The "exactly one capture" rule is enforced in one place. |
| **Config typo for `hotkey`** | The parser is strict (returns a typed error with the offending substring); the daemon falls back to the default hotkey and logs at WARNING (FR 2.6 final bullet). |
| **`~/record/` collides with an existing user-owned file or symlink** | The daemon attempts `mkdir(parents=True, exist_ok=True)` only on the configured `output_folder` and `log_folder`. A collision (file exists, not a dir) surfaces as a hard startup error with a clear message naming the path. |
| **launchd `KeepAlive` restart loop on persistent failure** (e.g. corrupted binary) | `KeepAlive` defaults will throttle to a minimum 10 s respawn interval; we accept that. The launchd-managed stdout/stderr files (`daemon-launchd.{out,err}.log`) capture pre-structlog failure output so the user can diagnose. |
| **Swift `record-capture` daemon-mode regressions** breaking the one-shot test path | The `--daemon` flag is the only switch; absent it, every code path stays exactly as today. The integration suite continues to exercise the non-`--daemon` path through `supervisor.py`. A new test module exercises the daemon path directly. |

---

## 4. Testing Strategy

### Unit tests (Python — `tests/python/`)

- **`test_ipc.py` (extend):** round-trip the three new events (`hotkey_registered`, `hotkey_pressed`, `hotkey_unregistered`) and the two new commands through the pydantic discriminated unions.
- **`test_config.py` (new):** pydantic-settings parsing of `~/.config/record/config.toml`. Cases: missing file → defaults; missing key → default for that key; unknown key → ignored with warning emitted via caplog; invalid hotkey → parser raises, daemon will fall back; output_folder with tilde / non-absolute / collides-with-file.
- **`test_hotkey.py` (new):** the modifier+key string parser (round-trips, case-insensitive modifiers, rejects empty modifier list, rejects unknown keys, accepts the function-key range).
- **`test_control.py` (new):** the Unix-socket request/response protocol — drive the server in-process with a real socket, exercise each request type against a stubbed state machine, verify framing and error paths (`already_running`, `not_running`, `busy`, malformed request).
- **`test_daemon.py` (new):** the daemon state machine driven by a fake Swift child (an asyncio coroutine that emits canned event sequences). Cover: hotkey-press while idle → start; hotkey-press while running → stop; double-press during STARTING → second press dropped + error sound; system-event termination → state returns to idle and `capture-state.json` is `final: true`; `register_hotkey` returns `conflict` → daemon stays running, no hotkey fires, status reports correctly.
- **`test_launchagent.py` (new):** plist generation (golden file diff), `record install` flow with `launchctl` replaced by a stub that records its argv (no real `bootstrap` in CI).
- **`test_cli.py` (extend):** `record install`, `record uninstall`, `record daemon start`/`stop`/`restart`, `record status` against a stub daemon control socket.
- **`test_state.py` (extend):** schema unchanged, but verify the daemon's "overwrite-on-next-capture" lifecycle by simulating two consecutive captures and asserting that `capture-state.json` reflects only the second.

### Unit tests (Swift — `swift-capture/Tests/`)

- **`ProtocolTests.swift` (extend):** add round-trip fixtures for the new commands/events. Share JSON fixtures with the Python side (same pattern as 001 / 002).
- **`HotkeyMonitorTests.swift` (new):** as much of `HotkeyMonitor` as can be unit-tested without actually installing a system hotkey — primarily the modifier/keycode translation and the OSStatus → status-string mapping. The real `RegisterEventHotKey` call is exercised in the manual smoke suite (it requires Accessibility, which we don't grant in CI).

### Integration tests (`tests/integration/`)

The existing synthetic-source plumbing (`--test-silent-sources`, `--test-synthetic-video`) is the right tool here. Add a new integration test that:

1. Spawns the Python daemon against a Swift child launched with `--daemon --test-silent-sources --test-synthetic-video` (no TCC needed).
2. Sends `{"op":"start"}` over the socket → asserts the resulting event sequence (`started` → `source_attached` × N → `video_started` → … ) and that `capture-state.json` reflects a running capture.
3. Sends `{"op":"stop"}` → asserts `stopped` + `video_file` + `final: true` on the state file + valid `.wav` and `.mp4` in `output_folder`.
4. Repeats the start/stop cycle 3 times in the same daemon process — asserts that each cycle produces a fresh pair of files, the state file is overwritten correctly each time, and the daemon's RSS / file descriptor count does not grow unboundedly.
5. Drives `{"op":"status"}` between each phase and asserts the schema.
6. Final `{"op":"quit"}` → asserts socket closes, daemon exits 0, socket file is removed.

A second integration test exercises the conflict path by sending `register_hotkey` for a combo that a sibling fixture process pre-registers via the same Carbon API on a non-CI machine — this one stays in the **manual smoke** bucket because CI lacks Accessibility.

### Manual smoke tests (developer machine)

Each scenario starts from a clean state (`record uninstall` then `record install`).

1. **Hotkey happy path:** press ⌥⌘R from a focused Chrome tab in a Google Meet session → Tink sound; speak for 30 s; press ⌥⌘R again → Pop sound. Verify both files in `~/record/`, both play back, audio contains your voice and the meeting audio, video shows the primary display.
2. **Terminal-CLI parity:** `record start` from a terminal → press hotkey to stop → both finalize via the same daemon path; confirm `record status` between phases shows the correct state.
3. **Hotkey conflict:** bind ⌥⌘R in another app (e.g. a Keyboard Maestro macro) → `record daemon restart` → press hotkey → other app fires, not record → `record status` says "hotkey ⌥⌘R may be inactive" → unbind in the other app → `record daemon restart` → hotkey works again.
4. **Accessibility denied:** revoke Accessibility for the daemon's binary in System Settings → `record daemon restart` → daemon runs, no hotkey, notification banner names Accessibility → `record start` from terminal still works.
5. **Login autostart:** `record install` → log out and back in → `record status` immediately after login shows daemon running, hotkey registered.
6. **Mid-capture daemon stop:** start a capture via hotkey → `record daemon stop` from terminal mid-capture → Pop sound plays, files finalized in `~/record/`, daemon exits 0; `record daemon start` brings it back; capture state is empty.
7. **Config edit:** change `output_folder` to `/tmp/record-test/` in `~/.config/record/config.toml` → `record daemon restart` → next capture writes there → status confirms.
8. **Audible feedback off:** set `audible_feedback = false` → restart → hotkey press is silent on start/stop; trigger an error (e.g. with Mic permission revoked) → notification banner still fires, no Funk sound.
9. **System-event end:** start a capture via hotkey → close laptop lid → reopen → no recording in progress; `daemon.log` contains the system-event summary; files in `~/record/` are playable.

### Pre-implementation empirical verifications

These are spike checks to run **before** writing production code:

1. **Carbon `RegisterEventHotKey` returns `eventHotKeyExistsErr` reliably** when another app (Keyboard Maestro, Hammerspoon, BetterTouchTool) already holds the combo — verify on macOS 13 / 14 / 15.
2. **`launchctl bootstrap gui/$UID`** on a freshly written plist behaves identically across macOS 13 / 14 / 15 and reports clear stderr on the "already bootstrapped" case so `record install` can re-bootstrap cleanly.
3. **`afplay` invoked from a launchd-managed (no controlling terminal, no GUI app) process** still produces audible sound from the active output device — confirm before committing to the no-Swift-NSSound choice.
4. **Reuse of the existing `record-capture` process across multiple `start` / `stop` cycles** does not leak `SCStream`, `AVAudioEngine`, or `AVAssetWriter` resources (run 50 cycles via `--test-synthetic-video --test-silent-sources` and inspect `lsof` + `vmmap`).
