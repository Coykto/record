# record

A privacy-first macOS meeting recorder. A background daemon runs while you're logged in, listening for a single global hotkey. Press it to start a capture; press it again to stop. Each capture records your microphone and the system audio output on the same machine as two independent `.wav` files (`mic.wav` and `system.wav`), plus a primary-display `video.mp4`, into a per-session folder under a predictable root. There is no recording bot, no cloud upload, no calendar integration — it's for someone who wants a faithful local copy of every client call without having a third party in the meeting. This is an early Phase 1 build.

## Quickstart

```sh
make install      # build the Swift binary, install the `record` CLI
record install    # register the daemon to autostart on login, and start it now
```

`record install` primes the macOS permission prompts (grant them when asked), writes the LaunchAgent, and bootstraps the daemon. After that, the everyday flow has no terminal in it:

- Press **⌥⌘R** (Option + Command + R) to start a capture — you'll hear a short "Submarine" ping.
- Press **⌥⌘R** again to stop — you'll hear a short "Pop".
- Recordings land in a per-session subfolder under `~/record/`: `~/record/<timestamp>/mic.wav`, `system.wav`, `combined.wav`, `video.mp4`, and `transcript.{json,txt,srt}`. After transcription, the folder is best-effort renamed to `~/record/<timestamp>-<short-description>/` (or `~/record/<timestamp>-silent/` if nothing was said).

The daemon has no menu bar icon, no Dock icon, and no window. To remove it:

```sh
record uninstall  # bootout the LaunchAgent, stop the daemon
```

`record install` is safe to re-run — it re-registers the agent and picks up plist changes. After `record uninstall` you can `record install` again to re-register.

## Output

Each capture (hotkey press to hotkey press, or `record start` to `record stop`) produces a per-session subfolder `<output_folder>/<ISO-8601-timestamp>/` (default `<output_folder>` is `~/record/`) containing:

- `mic.wav` — microphone audio only (mono, 16 kHz, 16-bit PCM).
- `system.wav` — system audio output only (mono, 16 kHz, 16-bit PCM).
- `combined.wav` — the two tracks mixed for transcription.
- `video.mp4` — primary display at native resolution, 30 fps, cursor visible, no audio track.
- `transcript.json`, `transcript.txt`, `transcript.srt` — transcription output.

The two source `.wav` files are written independently — no mixing into `mic.wav` / `system.wav`, so the mic track contains only your voice and the system track contains only what the meeting app played. The `output_folder` is created automatically on first use. Daemon-driven captures do **not** land in the current working directory — that placeholder behavior from the pre-daemon build is gone.

After transcription completes, the session folder is best-effort renamed to `<output_folder>/<timestamp>-<short-description>/` (a kebab-case English summary of what was discussed), or to `<output_folder>/<timestamp>-silent/` if no one spoke. If renaming fails for any reason, the folder stays at `<output_folder>/<timestamp>/` and all files inside are untouched.

Audio is always-on; video is best-effort. Any video failure (permission denied, SCStream error, writer failure) leaves audio capture untouched — the two `.wav` files are still produced, and the `.mp4` is either omitted (if video never started) or finalized with whatever was captured. The `record stop` summary describes the outcome.

## Requirements

- macOS 13.0 (Ventura) or later.
- Xcode command-line tools (provides the Swift toolchain used to build the capture binary). Install with `xcode-select --install` if you haven't already.
- Python 3.11 or later.
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management.

## Installation

```sh
make install
```

That builds the Swift `record-capture` binary into `src/record/bin/` (release config), then runs `uv pip install -e .`. After install, the `record` command is on your `$PATH`. Run `record install` (see Quickstart) to register and start the daemon.

The orchestrator shells out to `claude -p` after each capture to auto-name session folders. If the `claude` CLI is not on `PATH`, session folders remain at their timestamp-only name and nothing else is affected.

## Permissions

The daemon and capture pipeline need three macOS TCC permissions:

1. **Microphone** — required to capture your voice. Prompted the first time a capture is started.
2. **Screen Recording** (called **Screen & System Audio Recording** on macOS 14+) — gates **both** system-audio capture (via ScreenCaptureKit) **and** primary-display video capture. Prompted the first time a capture is started.
3. **Accessibility** — *new in the daemon build*. Required for the global hotkey (Carbon `RegisterEventHotKey`). Prompted the first time the daemon registers the hotkey.

`record install` primes the Microphone and Screen Recording prompts from the terminal, because a launchd-spawned daemon cannot present those prompts itself. The Accessibility prompt appears when the daemon registers the hotkey.

If you deny **Microphone**, a capture cannot start (exit code `2` from `record start`). If you deny **Screen Recording**, mic capture still proceeds (`mic.wav` is produced) but `system.wav` is empty/silent and no `video.mp4` is produced; the stop summary reports `video: unavailable — permission_denied`. If you deny **Accessibility**, the daemon keeps running and the terminal `record start` / `record stop` commands still work — only the hotkey is disabled. `record status` reports `hotkey: disabled — Accessibility permission missing`.

To grant or re-grant later:

- System Settings → Privacy & Security → **Microphone**
- System Settings → Privacy & Security → **Screen & System Audio Recording** (labelled **Screen Recording** on macOS 13)
- System Settings → Privacy & Security → **Accessibility**

Toggle the entry for `record` (or your terminal / Python interpreter) on, then `record daemon restart` to pick up an Accessibility grant. macOS sometimes shows permission prompts silently in the background — if a command seems to hang the first time, check the macOS notification center.

## CLI

### Capture control

- `record start` — starts a capture via the running daemon. Same capture session a hotkey press would start. Prints the resolved audio/video paths.
- `record stop` — stops the active capture via the daemon, then prints a summary line with the output path, duration, sources captured, and any warnings.
- `record status` — prints a short, human-readable block: whether the daemon is running, whether it's registered to autostart, the hotkey state, and whether a capture is in progress (with elapsed time and paths). Exits `0` if the daemon is running, non-zero otherwise.

A capture started by the hotkey can be stopped by `record stop`, and vice versa — there is exactly one capture, regardless of which channel started or stopped it. If the daemon is not running, `record start` / `record stop` print `daemon is not running — try `record daemon start` or `record install`` and exit non-zero; they never spawn a one-off capture.

### Daemon lifecycle

- `record install` — write the LaunchAgent and bootstrap the daemon; registers autostart on login.
- `record uninstall` — bootout the LaunchAgent and remove its plist; stops the daemon (finalizing any in-flight capture first).
- `record daemon start` — launch the daemon for this login session. If already running, prints a message and exits `0`.
- `record daemon stop` — stop the running daemon. An in-progress capture is finalized cleanly first.
- `record daemon restart` — `daemon stop` followed by `daemon start`. Use this to pick up a `config.toml` edit or to recover a wedged daemon. None of the `daemon` subcommands change the autostart registration set by `record install`.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success. |
| 1 | Already-running, not-running, or daemon-not-reachable condition. |
| 2 | Permission denied (microphone or screen recording). |
| 3 | Capture binary not found, or daemon / install failed to launch. |
| 4 | Daemon did not shut down cleanly within the stop timeout. |

Use `echo $?` after a command to read the exit code.

## Configuration

The daemon reads an optional TOML file at `~/.config/record/config.toml`. If the file is missing, all defaults apply — no error. It exposes exactly four settings:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `hotkey` | string | `option+command+r` | The start/stop shortcut. One or more modifiers (`cmd`/`command`, `opt`/`option`/`alt`, `ctrl`/`control`, `shift`) plus a single key (`a-z`, `0-9`, `f1-f20`, or `space`/`tab`/`return`/`escape`/`delete`). An invalid value logs a warning and falls back to the default — the daemon never refuses to start. |
| `output_folder` | path | `~/record/` | Absolute root folder under which each capture creates a per-session subfolder (`<timestamp>/`, later renamed) holding `mic.wav`, `system.wav`, `combined.wav`, `video.mp4`, and `transcript.{json,txt,srt}`. Auto-created on first capture. |
| `log_folder` | path | `~/record/logs/` | Absolute folder for the daemon log. Auto-created on daemon start. Independent of `output_folder`. |
| `audible_feedback` | bool | `true` | Whether the start/stop/error sounds play. When off, error conditions still surface via a macOS notification banner. |

Any unrecognized key is ignored with a logged warning. Edits take effect on the **next daemon restart** (`record daemon restart` or next login) — there is no hot-reload.

Example `~/.config/record/config.toml`:

```toml
hotkey = "control+shift+r"
output_folder = "~/Movies/record"
audible_feedback = false
```

## Capture session behavior

The recording itself behaves exactly as it did in the pre-daemon build:

- Two independent `.wav` files (`mic.wav` and `system.wav`) plus a primary-display `video.mp4`, all written into the same per-session folder.
- Audio required, video best-effort (see Output above).
- Closing the laptop lid, locking the screen (⌃⌘Q), or letting the display sleep ends the capture cleanly — all files are finalized and saved. There is **no auto-resume** on wake / unlock; start a new capture if you want to keep recording. The daemon writes a one-line system-event summary into `daemon.log`.
- The hotkey is intercepted by macOS before any meeting client sees it — no extra participant, no "recording" banner, the keypress is not delivered to Zoom / Meet / Teams.

## Manual smoke test

Each scenario starts from a clean state — `record uninstall` then `record install`.

1. **Hotkey happy path.** Press ⌥⌘R from a focused Chrome tab in a Google Meet session → Submarine sound. Speak for ~30 s. Press ⌥⌘R again → Pop sound. Verify the per-session folder under `~/record/` contains `mic.wav`, `system.wav`, and `video.mp4` that play back; the mic file contains your voice, the system file contains the meeting audio, and the video shows the primary display.
2. **Terminal-CLI parity.** `record start` from a terminal → press the hotkey to stop. All files finalize via the same daemon path. Confirm `record status` between phases shows the correct state.
3. **Hotkey conflict.** Bind ⌥⌘R in another app (e.g. a Keyboard Maestro macro) → `record daemon restart` → press the hotkey → the other app fires, not `record`. `record status` reports the hotkey may be inactive. Unbind in the other app → `record daemon restart` → the hotkey works again.
4. **Accessibility denied.** Revoke Accessibility for the daemon's binary in System Settings → `record daemon restart`. The daemon runs, no hotkey, a notification banner names Accessibility. `record start` from the terminal still works.
5. **Login autostart.** `record install` → log out and back in → `record status` immediately after login shows the daemon running and the hotkey registered.
6. **Mid-capture `record daemon stop`.** Start a capture via the hotkey → `record daemon stop` from a terminal mid-capture. Pop sound plays, files are finalized in `~/record/`, the daemon exits `0`. `record daemon start` brings it back; capture state is empty.
7. **Config edit reload.** Change `output_folder` to `/tmp/record-test/` in `~/.config/record/config.toml` → `record daemon restart` → the next capture writes there. `record status` confirms.
8. **Audible feedback off.** Set `audible_feedback = false` → `record daemon restart`. A hotkey press is silent on start/stop. Trigger an error (e.g. with Microphone permission revoked) → the notification banner still fires, no Funk sound.
9. **System-event end.** Start a capture via the hotkey → close the laptop lid → reopen. No recording is in progress; `daemon.log` contains the system-event summary; the files in `~/record/` are playable.

## Troubleshooting

### Logs

- `<log_folder>/daemon.log` (default `~/record/logs/daemon.log`) — the daemon's structured log plus the Swift `record-capture` binary's stderr, interleaved. Contains daemon startup, hotkey registration outcome, the resolved config, each hotkey press, capture start/stop events (`video_started`, `video_lost`, `video_file`, `display_reconfigured`), the system-event summary, and daemon shutdown. Start here for anything that feels off.
- `<log_folder>/daemon-launchd.out.log` and `<log_folder>/daemon-launchd.err.log` — launchd-captured stdout/stderr of the daemon process. Catches catastrophic startup failures that happen before logging is initialized — check these if the daemon won't stay up.

### State

- `~/Library/Application Support/record/capture-state.json` — last known capture state (PID, sources attached, warnings, output path). Inspect this if you're unsure whether a capture is still live.
- `~/Library/Application Support/record/daemon.pid` — the daemon's PID file.
- `~/Library/Application Support/record/daemon.sock` — the daemon's Unix-domain control socket (used by the `record` CLI).

### Stuck daemon

If `record daemon stop` exits with code `4`, the daemon didn't exit in time. Inspect `daemon.log` for what's holding it. As a last resort:

```sh
kill -9 $(cat ~/Library/Application\ Support/record/daemon.pid)
```

### Hotkey not working

Run `record status`. If it reports the hotkey is disabled, grant **Accessibility** and `record daemon restart`. If it reports a conflict, another app holds ⌥⌘R — quit that app or change one of the two shortcuts, then `record daemon restart`.

### Wrong microphone

The system default input device is what's recorded. Switch in System Settings → Sound → Input before starting a capture.

## Known limitations

Findings from the pre-implementation empirical verifications. Run on macOS 15.7.5 (build 24G624), Intel (x86_64). Each item notes what was verified here and what still needs a manual developer check on other macOS versions.

- **Hotkey conflict detection (Carbon `RegisterEventHotKey`).** The Swift binary builds cleanly and `HotkeyMonitor.register` maps `eventHotKeyExistsErr` (OSStatus -9878) to a `conflict` result via a pinned constant. A *true* system-wide conflict was not reproduced: that needs a second app (Keyboard Maestro, Hammerspoon, BetterTouchTool) to already hold the chord, which cannot be set up non-interactively. The `OSStatus`-to-status mapping is also not exercised by the Swift unit tests — `HotkeyMonitorTests` covers only the modifier-mask and keycode translation, not the Carbon return-code branch. The Swift test suite could not run on this machine at all: it requires XCTest from a full Xcode install, and only the Command Line Tools are present (`swift test` fails with `no such module 'XCTest'`, as the Development section notes). Conflict behavior therefore relies on Carbon's documented contract and remains a manual smoke-test item (manual smoke test 3) across macOS 13 / 14 / 15.
- **`launchctl bootstrap gui/$UID` re-install path.** Verified end to end on this machine. A first `record install` bootstraps cleanly. Running `launchctl bootstrap gui/$UID <plist>` again against the already-loaded service fails with exit code 5 and stderr `Bootstrap failed: 5: Input/output error` — opaque, but stable. `record install` handles this: it detects the non-zero exit, runs `launchctl bootout` then re-bootstraps, and reports `re-registered to start on login`. A second `record install` was confirmed idempotent (new daemon PID, `record status` healthy). The exit-5 / "Input/output error" wording was only observed on macOS 15; macOS 13 / 14 may differ — `launchagent.py` matches on both the integer code and several wording substrings to stay robust.
- **`afplay` from a process with no controlling terminal.** Partially verified. `/usr/bin/afplay` and the three sound files (`Tink.aiff`, `Pop.aiff`, `Funk.aiff`) exist. A fire-and-forget `subprocess.Popen` of `afplay` in a new session (`start_new_session=True`, no controlling terminal) was spawned, returned immediately, and exited 0 after playback — the daemon's playback pattern works without a TTY. Whether audio is actually *audible* when the daemon is launchd-managed (no terminal, no GUI app, output routed to the active device) was not verified here; that needs the installed daemon triggering a real start/stop and remains a manual check (manual smoke tests 1, 8).
- **Capture-resource reuse across many cycles.** Verified headless. The Swift binary was driven through **50** start/stop cycles in a single `--daemon --test-silent-sources --test-synthetic-video` process. Open file descriptors (`lsof`) settled at 23 by cycle 10 and stayed flat through cycle 50 — no monotonic growth. RSS rose from ~11.7 MB to a ~25 MB steady-state working set over the first 10 cycles, then was effectively flat (~25.2 → ~25.9 MB across the final 40 cycles). The process exited 0 on `shutdown`. No `SCStream` / `AVAudioEngine` / `AVAssetWriter` leak was observed. This was run on macOS 15 / Intel only; the existing 3-cycle integration test (`tests/integration/test_end_to_end.py`) guards the same invariant in CI.

## Development

Run the unit and integration tests:

```sh
make test
```

That builds the Swift binary if needed, then runs the Python suite (`pytest tests`) followed by the Swift suite (`swift test --package-path swift-capture`). The Swift tests require a full Xcode install for XCTest; on a command-line-tools-only machine they're skipped with a notice and `make test` still exits 0.

Rebuild the Swift binary after changes:

```sh
make swift
```

The legacy one-shot `python -m record.supervisor` entry-point is retained for the integration suite and emergency offline use, but is not reachable through the `record` CLI when the daemon is in charge.
