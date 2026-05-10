# record

A privacy-first macOS meeting recorder. It runs from a terminal, captures your microphone plus the system audio output on the same machine, and writes a single mixed `.wav` to the current directory. There is no recording bot, no cloud upload, no calendar integration — it's for someone who wants a faithful local copy of every client call without having a third party in the meeting. This is an early Phase 1 build; only mixed audio capture is implemented so far.

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
2. **Screen Recording** (called **Screen & System Audio Recording** on macOS 14+) — required to capture system audio via ScreenCaptureKit. The Swift binary captures audio only; no video or screen content is ever read or written.

Grant both. If you deny either, `record start` exits with code `2` and the error message tells you which System Settings panel to visit.

To re-grant later:

- System Settings → Privacy & Security → **Microphone**
- System Settings → Privacy & Security → **Screen & System Audio Recording** (labelled **Screen Recording** on macOS 13)

Toggle the entry for `record` (or your terminal / Python interpreter) on. macOS sometimes shows the permission prompts silently in the background — if `record start` seems to hang the first time, check the macOS notification center.

## Manual smoke test

```sh
record start
# In another terminal, play any audio so something is on the system output:
afplay /System/Library/Sounds/Glass.aiff
# Talk into your mic.
record stop
# Play back the file that the stop summary printed:
afplay 2026-05-10T14-32-08.wav
```

You should hear both your voice and the system audio in the playback. Verify the format with `afinfo 2026-05-10T14-32-08.wav` — it should report mono, 16 kHz, 16-bit PCM.

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

- `~/Library/Logs/record/orchestrator.log` — structured JSON log lines from the Python supervisor.
- `~/Library/Logs/record/daemon.log` — stderr from the Swift `record-capture` binary.

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
