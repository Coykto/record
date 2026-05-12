# record

A privacy-first macOS meeting recorder. It runs from a terminal, captures your microphone plus the system audio output on the same machine, and writes a mixed `.wav` plus a primary-display `.mp4` (same timestamp stem) to the current directory. There is no recording bot, no cloud upload, no calendar integration — it's for someone who wants a faithful local copy of every client call without having a third party in the meeting. This is an early Phase 1 build.

## Output

Each `record start` → `record stop` cycle produces, in the current working directory:

- `{ISO-8601-timestamp}.wav` — mixed mic + system audio (mono, 16 kHz, 16-bit PCM).
- `{ISO-8601-timestamp}.mp4` — primary display at native resolution, 30 fps, cursor visible, no audio track.

Audio is always-on; video is best-effort. Any video failure (permission denied, SCStream error, writer failure) leaves audio capture untouched — the `.wav` is still produced, and the `.mp4` is either omitted (if video never started) or finalized with whatever was captured. The `record stop` summary describes the outcome.

## Requirements

- macOS 13.0 (Ventura) or later.
- Xcode command-line tools (provides the Swift toolchain used to build the capture binary). Install with `xcode-select --install` if you haven't already.
- Python 3.11 or later.
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management.

## Installation

```sh
make install
```

That builds the Swift `record-capture` binary into `src/record/bin/` (release config), then runs `uv pip install -e .`. After install, the `record` command is on your `$PATH`.

## First-run permission flow

The first time you run `record start`, macOS will prompt for two permissions:

1. **Microphone** — required to capture your voice.
2. **Screen Recording** (called **Screen & System Audio Recording** on macOS 14+) — gates **both** system-audio capture (via ScreenCaptureKit) **and** primary-display video capture.

Grant both. If you deny microphone, `record start` exits with code `2`. If you deny Screen Recording, audio capture still proceeds (system audio may degrade) but no `.mp4` is produced and the `record stop` summary reports `video: unavailable — permission_denied`.

To re-grant later:

- System Settings → Privacy & Security → **Microphone**
- System Settings → Privacy & Security → **Screen & System Audio Recording** (labelled **Screen Recording** on macOS 13)

Toggle the entry for `record` (or your terminal / Python interpreter) on. macOS sometimes shows the permission prompts silently in the background — if `record start` seems to hang the first time, check the macOS notification center.

## Manual smoke test

```sh
record start
# In another terminal, play any audio so something is on the system output:
afplay /System/Library/Sounds/Glass.aiff
# Talk into your mic, move the cursor around on the primary display.
record stop
# Play back the files that the stop summary printed:
afplay 2026-05-10T14-32-08.wav
open  2026-05-10T14-32-08.mp4
```

You should hear both your voice and the system audio in the wav playback. Verify the format with `afinfo 2026-05-10T14-32-08.wav` — it should report mono, 16 kHz, 16-bit PCM. The `.mp4` should open in QuickTime, play end-to-end, and show the primary display at its native resolution with the cursor visible.

### System-event shutdown

Closing the laptop lid, locking the screen (⌃⌘Q), or letting the display sleep all end the capture cleanly — both files are finalized and saved. The orchestrator appends a one-line summary to `~/Library/Logs/record/orchestrator.log` of the form:

```
[<utc-iso>] capture ended by system event reason=<system_sleep|display_sleep|screen_locked> audio=<abs.wav> [video=<abs.mp4>] [duration_seconds=<float>]
```

There is **no auto-resume** on wake / unlock — run `record start` again if you want to keep recording. The `com.apple.screenIsLocked` notification is best-effort; if your macOS version doesn't deliver it, capture simply continues until you `record stop` manually.

### Best-effort video

To verify the audio-always-on / video-best-effort contract: revoke Screen Recording for your terminal (System Settings → Privacy & Security → Screen Recording), then run `record start`, speak for ~10 s, and `record stop`. Confirm the `.wav` is produced, no `.mp4` exists, and the summary says `video: unavailable — permission_denied`. Re-grant Screen Recording afterwards.

## CLI

- `record start` — spawns a detached supervisor that runs the capture in the background. Prints the supervisor PID and the absolute output path, then returns.
- `record stop` — signals the active supervisor to wind down, then prints a summary line with the output path, duration, sources captured, and any warnings. Removes the PID and state files on success.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success. |
| 1 | Already-running, no-capture, or stale-PID condition. |
| 2 | Permission denied (microphone or screen recording). |
| 3 | Capture binary not found or supervisor failed to launch. |
| 4 | Supervisor did not shut down cleanly within the stop timeout. |

Use `echo $?` after a command to read the exit code.

## Troubleshooting

### Logs

- `~/Library/Logs/record/orchestrator.log` — structured JSON log lines from the Python supervisor, plus the one-line summary written on a system-event-triggered shutdown.
- `~/Library/Logs/record/daemon.log` — stderr from the Swift `record-capture` binary, including `video_started`, `video_lost`, `video_file`, and `display_reconfigured` events.

For video issues (no `.mp4` produced, partial `.mp4`, unexpected `video: unavailable` summary), start with `daemon.log` to see the Swift-side reason, then check `orchestrator.log` for how the supervisor handled it.

### State

- `~/Library/Application Support/record/capture-state.json` — last known capture state (PID, sources attached, warnings, output path). Inspect this if you're unsure whether a capture is still live.
- `~/Library/Application Support/record/capture.pid` — supervisor PID file.

### Stuck supervisor

If `record stop` exits with code `4`, the supervisor is still alive. Inspect `~/Library/Logs/record/daemon.log` for what's holding it. As a last resort:

```sh
kill -9 $(cat ~/Library/Application\ Support/record/capture.pid)
```

### Stale PID file

If `record start` complains "capture already in progress" but no capture is actually running, the PID file should self-heal on the next start. If it doesn't (e.g. the file is unreadable), remove it manually:

```sh
rm ~/Library/Application\ Support/record/capture.pid
```

### Wrong microphone

The system default input device is what's recorded. Switch in System Settings → Sound → Input before starting a capture.

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
