# Technical Specification: Primary-Display Video Capture

- **Functional Specification:** `context/spec/002-primary-display-video-capture/functional-spec.md`
- **Status:** Draft
- **Author(s):** e

---

## 1. High-Level Technical Approach

This spec **extends the same `record-capture` Swift binary** introduced by the audio spec (`001-mixed-mic-system-audio-capture`) rather than introducing a second binary. The existing JSON-line protocol gains a small set of video-related commands/events; everything else stays compatible.

Inside the Swift binary:

- The existing `AudioCapture.swift` is **untouched** — it keeps its own audio-only `SCStream` (system audio) plus `AVAudioEngine` (microphone) and continues writing the WAV.
- A **new, independent video-only `SCStream`** (driven by a new `VideoCapture.swift`) captures the macOS primary display.
- Video frames go through a new `MP4Writer.swift` wrapping `AVAssetWriter` (H.264, `.mp4`, no audio track).
- The two streams are intentionally independent: a video failure cannot take down audio. If video's `SCStreamDelegate.didStopWithError` fires, audio's `SCStream` is untouched.

Inside the Python orchestrator:

- `record start` resolves BOTH an absolute audio path (`$CWD/{ts}.wav` — existing behavior) AND an absolute video path (`$CWD/{ts}.mp4` — new, same timestamp stem). Both are passed in the `start` command payload.
- `record stop` reads the (extended) `capture-state.json` and prints a summary naming both files, total duration, and which sources actually captured.
- System events (screen lock / display sleep / system sleep) are detected by the Swift binary; on any of them the binary stops cleanly (exactly like the `stop` command) and emits a `capture_ended_by_system_event` event. The supervisor sees the clean exit, finalizes state to disk, writes the summary to `orchestrator.log`, and exits. No auto-resume. No new CLI command — the user reads the log if they want details.

**No new TCC permission.** Screen Recording is already required by the 001 spec for system audio. Adding video does not change the permission surface.

**Intentional deviations from `context/product/architecture.md`** (each will be revisited):

| Architecture says | This spec uses | Reason |
|---|---|---|
| `~/Movies/record/{ts}/video.mp4` | `$CWD/{ts}.mp4` | Functional spec's placeholder pending the "local folder output" spec (same as 001). |
| "System audio capture: ScreenCaptureKit (**same** `SCStream`)" | **Two** `SCStream` instances — one audio-only (existing), one video-only (new). | Decouples failure domains so video can crash without losing the (more valuable) audio. Confirmed by Swift API review of `SCStreamDelegate.didStopWithError` cascading behavior. |
| "Active meeting window" | Primary display | Phase 1 scope simplification, per the functional spec and the updated roadmap line 13. |

---

## 2. Proposed Solution & Implementation Plan (The "How")

### 2.1 New repository layout

Additions only — nothing existing is renamed or moved.

```
swift-capture/Sources/RecordCapture/
├── (existing files — unchanged)
├── VideoCapture.swift           (NEW)
├── MP4Writer.swift              (NEW)
├── DisplayMonitor.swift         (NEW)  — primary-display lookup + CGDisplayReconfiguration callback
└── SystemEventMonitor.swift     (NEW)  — sleep / display-sleep / screen-lock observer

src/record/
└── (existing files — minor edits only; no new files)
```

### 2.2 Swift binary — new files and responsibilities

(Following the 001 spec's convention of describing responsibilities, not implementations.)

- **`VideoCapture.swift`** — owns the video-only `SCStream`. Builds `SCContentFilter(display:excludingWindows:[])` against the primary display resolved by `DisplayMonitor`. Adds itself as an `SCStreamOutput` of type `.screen` on a dedicated `DispatchQueue`. Filters out idle/blank/suspended frames using the `SCStreamFrameInfo.status` attachment on each `CMSampleBuffer`. Forwards "good" frames to `MP4Writer`. Owns the `SCStreamDelegate` for video errors — translates `didStopWithError` into a `video_lost` event (audio is untouched). Emits `video_started` once the writer has accepted its first frame.

- **`MP4Writer.swift`** — wraps `AVAssetWriter` configured for `.mp4`. Single `AVAssetWriterInput(mediaType: .video, outputSettings: ...)` with `expectsMediaDataInRealTime = true`. Calls `startSession(atSourceTime:)` with the **first frame's PTS** (SCK uses the host-time clock; using `.zero` produces a zero-byte file). Appends each `CMSampleBuffer` directly (no re-timing). Exposes a `finalize()` that calls `markAsFinished()` + `finishWriting(completionHandler:)`. Finalization is wired into:
  - the `stop` command path,
  - the system-event-triggered stop path (sleep/lock/display sleep),
  - a SIGTERM handler,
  - `atexit` (defense in depth).
  - **Known limitation, surfaced in §3 risks:** standard `.mp4` is not crash-recoverable. If the process is hard-killed before `finishWriting` completes, the partial mp4 is unplayable. Fragmented mp4 is an explicit non-goal for Phase 1 (see risks).

- **`DisplayMonitor.swift`** — resolves the primary display (`SCShareableContent.displays.first { $0.displayID == CGMainDisplayID() }`). Computes capture dimensions using `CGDisplayPixelsWide(displayID)` / `CGDisplayPixelsHigh(displayID)` — **pixels, not points** (SCK config uses pixels; `SCDisplay.width/height` are points; getting this wrong produces a half-res capture on Retina). Registers a `CGDisplayRegisterReconfigurationCallback`. Debounces the dual-callback pattern (`beginConfigurationFlag` then actual flags). On any reconfig touching the primary display (`.setMainFlag`, `.removeFlag`, `.setModeFlag`), it asks `VideoCapture` to tear down its current `SCStream` and recreate it against the new primary display. Each reconfig emits a `display_reconfigured` event with a short reason.

- **`SystemEventMonitor.swift`** — subscribes to three sources, each on `NSWorkspace.shared.notificationCenter` / `DistributedNotificationCenter.default()`:
  - `NSWorkspace.willSleepNotification` → reason `"system_sleep"`.
  - `NSWorkspace.screensDidSleepNotification` → reason `"display_sleep"`.
  - `DistributedNotificationCenter` name `"com.apple.screenIsLocked"` → reason `"screen_locked"`.
  - On the first such event during an active capture, triggers the same internal stop path as the `stop` command, then emits `capture_ended_by_system_event` with the reason just before the process exits. No auto-resume on `didWake`/`screenIsUnlocked` (we don't subscribe to those).
  - Requires AppKit import; does **not** require `NSApplication`/`NSApplicationMain`. The existing `main.swift` already runs a run loop for stdin; the same loop services notification delivery.

- **`main.swift`** — small additions:
  - Construct `VideoCapture`, `DisplayMonitor`, `SystemEventMonitor` alongside the existing `AudioCapture` on `start`.
  - On `stop` (whether from stdin, signals, or system events), call `stop()` on both `AudioCapture` and `VideoCapture` in parallel; wait for both writers to finalize; emit `stopped`; exit.

- **`Permissions.swift`** — no functional change. Screen Recording is already a prerequisite from 001. The video path simply assumes it's granted; if not, the `SCStream` creation will fail and `VideoCapture` will emit `video_lost` with reason `permission_denied` — audio carries on.

### 2.3 `SCStream` configuration for video

Specified here so the spec is reviewable without reading code. (No code in the spec.)

| `SCStreamConfiguration` field | Value | Source / rationale |
|---|---|---|
| `width` | `CGDisplayPixelsWide(primaryDisplayID)` | Pixels. |
| `height` | `CGDisplayPixelsHigh(primaryDisplayID)` | Pixels. |
| `minimumFrameInterval` | `CMTime(value: 1, timescale: 30)` | 30 fps target (functional spec). |
| `pixelFormat` | `kCVPixelFormatType_32BGRA` (default) | Standard for VideoToolbox H.264 encode. |
| `showsCursor` | `true` | Functional spec. Set once at creation; no live toggle. |
| `queueDepth` | `5` | Apple sample default for 30 fps screen capture; absorbs writer hiccups. |
| `capturesAudio` | `false` | Video-only stream. |
| HDR / dynamic-range preset | not used | Force SDR. HDR-on-SDR-decoder playback shows washed-out colors in QuickTime/browsers. |

### 2.4 `AVAssetWriter` settings (H.264, mp4, video-only)

Variable bitrate aimed at "screen-share legible text and faces."

| Output-settings key | Value |
|---|---|
| `AVVideoCodecKey` | `AVVideoCodecType.h264` |
| `AVVideoWidthKey` | capture pixel width |
| `AVVideoHeightKey` | capture pixel height |
| `AVVideoCompressionPropertiesKey` → `AVVideoAverageBitRateKey` | **12 Mbps baseline, scaled to resolution** (e.g., ~6 Mbps at 1080p, ~12 Mbps at 1440p, ~25 Mbps at 5K). Concrete scaling formula chosen at implementation time; documented in `MP4Writer.swift`. |
| `AVVideoCompressionPropertiesKey` → `AVVideoExpectedSourceFrameRateKey` | `30` |
| `AVVideoCompressionPropertiesKey` → `AVVideoMaxKeyFrameIntervalKey` | `60` (~2 s GOP) |
| `AVVideoCompressionPropertiesKey` → `AVVideoProfileLevelKey` | `AVVideoProfileLevelH264HighAutoLevel` |
| `AVAssetWriterInput.expectsMediaDataInRealTime` | `true` |
| `AVAssetWriter` `fileType` | `.mp4` |

`startSession(atSourceTime:)` is called with the **first accepted frame's PTS**.

### 2.5 File and state locations (additions)

| Purpose | Path |
|---|---|
| Output MP4 | `$CWD/{ISO-8601-timestamp}.mp4` — same timestamp stem as the audio `.wav` |
| Capture state | `~/Library/Application Support/record/capture-state.json` — unchanged path, extended schema (see §2.7) |

### 2.6 JSON-line IPC protocol — additions

This spec extends the protocol established by 001. All existing commands/events are unchanged.

**Modified command:**

| Command | Payload |
|---|---|
| `start` (extended) | `{"cmd":"start","output_path":"/abs.wav","video_output_path":"/abs.mp4","format":{...},"video":{"fps":30,"show_cursor":true}}` — `video_output_path` is **optional**; if omitted, video capture is skipped entirely (for forward/backward compat with audio-only callers and for tests). |

**New events:**

| Event | Payload |
|---|---|
| `video_started` | `{"event":"video_started","display_id":1,"width_px":2560,"height_px":1440,"fps":30}` |
| `video_lost` | `{"event":"video_lost","at_offset_seconds":134.2,"reason":"sc_stream_error"\|"permission_denied"\|"writer_failure"\|...,"message":"..."}` |
| `video_file` | `{"event":"video_file","path":"/abs.mp4","duration_seconds":612.4}` — emitted after `finishWriting` completes successfully |
| `display_reconfigured` | `{"event":"display_reconfigured","reason":"primary_changed"\|"resolution_changed"\|"display_removed","new_display_id":2,"new_width_px":1920,"new_height_px":1080}` |
| `capture_ended_by_system_event` | `{"event":"capture_ended_by_system_event","reason":"system_sleep"\|"display_sleep"\|"screen_locked","at_offset_seconds":...}` |

### 2.7 `capture-state.json` — extended shape

```
{
  "pid": 12345,
  "start_time": "2026-05-10T14:32:08Z",
  "output_path": "/abs/path/to/2026-05-10T14-32-08.wav",
  "video_output_path": "/abs/path/to/2026-05-10T14-32-08.mp4",
  "sources": {
    "mic":          { ... unchanged ... },
    "system_audio": { ... unchanged ... },
    "video":        {
      "status": "attached" | "lost" | "never_attached",
      "attached_at": "...",
      "lost_at": null | "...",
      "display_id": 1,
      "width_px": 2560,
      "height_px": 1440,
      "fps": 30
    }
  },
  "warnings": [ ... existing, plus video-related entries ... ],
  "display_changes": [
    {"timestamp": "...", "reason": "primary_changed", "new_display_id": 2, "new_width_px": 1920, "new_height_px": 1080}
  ],
  "ended_by": null | "stop_command" | "system_sleep" | "display_sleep" | "screen_locked" | "audio_failure",
  "last_event_at": "..."
}
```

### 2.8 Python orchestrator — required edits

Small, localized. No new files.

- **`ipc.py`** — add pydantic models for the new events (`video_started`, `video_lost`, `video_file`, `display_reconfigured`, `capture_ended_by_system_event`) and the extended `start` command payload.
- **`supervisor.py`**:
  - Compute `video_output_path` alongside `output_path` on startup (same timestamp stem; `.mp4`).
  - Pass both paths into the `start` command.
  - Handle the new events: update `capture-state.json` accordingly; on `capture_ended_by_system_event`, write the final summary into `orchestrator.log` (paths, duration, reason) before exiting.
  - Detect Swift binary clean exit triggered by a system event vs. a `stop` command (latter is signaled by the supervisor receiving SIGTERM from `record stop`).
- **`cli.py`** — `stop` command's summary printer is extended to include the video file path and the `video` source status, derived from the state file. No new subcommands.
- **`state.py`** — schema migration is not a concern (state is volatile per-capture). Just widen the pydantic model.

### 2.9 Best-effort video resilience — concrete behavior

| Scenario | Audio behavior | Video behavior | Final outputs |
|---|---|---|---|
| Screen Recording permission denied | Proceeds (system audio fails per existing 001 path; mic continues) | `video_lost` with reason `permission_denied`; no MP4 produced | `.wav` only |
| `VideoCapture` fails at start (no display, SCK init error) | Proceeds normally | `video_lost` at offset 0; no MP4 | `.wav` only |
| `VideoCapture` fails mid-capture (`SCStreamDelegate.didStopWithError`) | Continues to user `stop` | MP4 finalized via `MP4Writer.finalize()` with what was captured; `video_lost` emitted with `at_offset_seconds` | `.wav` + truncated `.mp4` |
| Audio fully fails (neither mic nor system audio producing samples) | Whole capture stops with error (existing 001 behavior, extended to also finalize any partial MP4 before exit) | MP4 finalized with whatever exists | partial files only if any data; non-zero exit |
| One audio source lost (existing 001 case) | Other audio source continues | Untouched | `.wav` + `.mp4` |

### 2.10 System-event-triggered shutdown — sequence

1. `SystemEventMonitor` receives one of the three notifications.
2. Marks itself "shutting down" (idempotent — ignores further notifications).
3. Invokes the internal stop path: `AudioCapture.stop()` + `VideoCapture.stop()` in parallel.
4. Waits for both writers to finalize (`finishWriting` for MP4, WAV close for audio), bounded by a timeout (e.g., 5 s).
5. Emits `capture_ended_by_system_event` with the reason.
6. Emits `stopped` (existing event).
7. Exits 0.
8. The supervisor sees clean child exit, writes the final summary to `orchestrator.log`, removes PID file, exits.

### 2.11 Build & packaging

No changes from 001. The same `make swift` rebuilds the binary; no new Swift dependencies; no new Python dependencies. The bundled binary path stays at `src/record/bin/record-capture`.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **macOS 13.0+** — already required by 001.
- **TCC permissions:** Microphone + Screen Recording (no change). Accessibility still not required (no hotkey).
- **New Swift framework usage:** `AppKit` (for `NSWorkspace` notifications), `CoreGraphics` (already transitively present; for `CGMainDisplayID`, `CGDisplayPixelsWide`, `CGDisplayRegisterReconfigurationCallback`), `AVFoundation` (already present; now also for `AVAssetWriter`). No new third-party dependencies.
- **Protocol schema:** this spec extends the JSON-line protocol established by 001. Swift and Python schemas must be edited together. Same as 001's note.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Hard process kill (SIGKILL, panic) leaves an **unplayable, unfinalized `.mp4`** because standard mp4 is not crash-recoverable. | Document as a known v1 limitation. `finishWriting` is called from `stop`, system-event handlers, SIGTERM, and `atexit` — covers all graceful exits. **Fragmented mp4** (`movieFragmentInterval`) is an explicit non-goal for Phase 1: it adds a few percent to file size and reduces compatibility with some legacy players for limited benefit given that graceful shutdown already covers the common paths. Revisit if hard-kill becomes a real-world issue. |
| `com.apple.screenIsLocked` is **not an Apple-documented notification**; its behavior could change or fail to fire from our binary's launch context on macOS 14/15. Also fires on screensaver start under some "require password" settings. | Mark as "best-effort detection" in `daemon.log`. Empirically verify on macOS 13/14/15 (see §4 testing). If it stops working, the capture simply continues until `stop` is invoked — degraded but not broken. System sleep and display sleep notifications are both Apple-supported and remain authoritative. |
| **Pixels vs. points** confusion — using `SCDisplay.width/height` instead of `CGDisplayPixelsWide` produces half-resolution capture on Retina displays. | Use only `CGDisplayPixelsWide` / `CGDisplayPixelsHigh`. Encode this as a unit test against a mocked `SCDisplay` (Apple's types make this awkward; alternative is an integration check on a known Retina test machine). Code-level comment in `DisplayMonitor.swift`. |
| **First-PTS mismatch** — calling `AVAssetWriter.startSession(atSourceTime: .zero)` while SCK delivers buffers timed against the host clock produces a zero-byte mp4. | `MP4Writer` defers `startSession` until the first valid frame arrives and uses that frame's `CMSampleBufferGetPresentationTimeStamp`. Covered by integration test (verifies non-zero file size + playable). |
| `SCStream` does **not** automatically adapt to primary-display changes; without explicit teardown/recreate, capture quietly continues against a stale display. | `DisplayMonitor`'s reconfiguration callback drives `VideoCapture` to tear down and recreate. Debounce the dual-callback pattern (`beginConfigurationFlag` first, then real flags). Test by hot-plugging an external monitor (manual smoke). |
| **Idle / blank / suspended SCK frames** appended naively to `AVAssetWriter` produce duplicate timestamps and a malformed track. | `VideoCapture` filters on `SCStreamFrameInfo.status` attachment, dropping non-`.complete` frames before forwarding to `MP4Writer`. |
| **Large file size** on 5K displays (~25 Mbps × hour ≈ 11 GB). | Variable-bitrate encoding helps when content is static (slides). Surface in README. Future "local folder output" spec will introduce a configurable bitrate ceiling. |
| Video-only `SCStream` running alongside audio-only `SCStream` could **double the SCK setup cost** (two `SCShareableContent.current` resolutions, two filter registrations). | Both calls happen at startup, well under a second total on typical hardware. If profiling shows a cost, cache `SCShareableContent` across both setups. Not blocking. |

---

## 4. Testing Strategy

### Unit tests (Swift — `swift-capture/Tests/`)

- **`ProtocolTests.swift` (extend):** add round-trips for the new events (`video_started`, `video_lost`, `video_file`, `display_reconfigured`, `capture_ended_by_system_event`) and the extended `start` command. Share JSON fixtures with the Python side (same pattern as 001).
- **`MP4WriterTests.swift` (new):** feed `MP4Writer` a sequence of synthetic `CMSampleBuffer`s (single-color `CVPixelBuffer`s with monotonically increasing PTS), call `finalize()`, then re-open the resulting `.mp4` with `AVAsset` and assert:
  - non-zero file size,
  - exactly one video track,
  - track duration ≈ expected (within ±100 ms),
  - track dimensions and frame rate match config.
- **`DisplayMonitorTests.swift` (new):** unit-test the pixel-vs-point conversion against fixed `CGDirectDisplayID` mocks (or simply assert against `CGDisplayPixelsWide/High` of `CGMainDisplayID()` on the test machine).

### Unit tests (Python — `tests/python/`)

- **`test_ipc.py` (extend):** round-trip the new event/command shapes through pydantic.
- **`test_cli.py` (extend):** verify the `stop` summary printer includes the video file path and video status when the state file contains a `video` source.
- **`test_state.py` (extend):** state file schema accepts the new `video` source, `display_changes`, and `ended_by` fields.

### Integration tests (`tests/integration/`)

- **Extend the `--test-silent-sources` fake-source mode from 001** to also accept a `--test-synthetic-video` flag that bypasses real `SCStream` and feeds `MP4Writer` with deterministic synthetic frames. No TCC permissions, no display hardware, runnable in CI.
- Verifies:
  - Event sequence: `ready` → `started` → `source_attached` ×N → `video_started` → `stopped` → `video_file`.
  - Resulting `.mp4` is playable end-to-end (`AVAsset` opens it; duration matches request within ±100 ms).
  - Resulting `.wav` and `.mp4` share the same filename stem and have aligned start times.
  - `video_lost` injection mid-capture: WAV still produced normally, MP4 finalized with truncated duration, state file records `video.status == "lost"`.
  - `capture_ended_by_system_event` injection: clean exit, summary present in `orchestrator.log`.

### Manual smoke tests (developer machine, documented in spec README)

Each scenario is run once on a Retina display and once on an attached non-Retina external monitor.

1. **Happy path:** `record start` → play audio + display content for 30 s → `record stop`. Verify both files exist, play back correctly, MP4 dimensions match `CGDisplayPixelsWide/High` of the primary display.
2. **Best-effort video:** revoke Screen Recording in System Settings → `record start` → audio plays + mic input → `record stop`. Verify `.wav` produced, no `.mp4`, summary says "video unavailable: permission_denied".
3. **System-event shutdown:** `record start` → close laptop lid after ~10 s → wait → reopen lid. Verify capture is NOT auto-resumed; verify `orchestrator.log` contains a summary with reason `system_sleep`; verify both files present and playable.
4. **Screen-lock shutdown:** `record start` → ⌃⌘Q after ~10 s. Verify summary in log with reason `screen_locked`. (This is the empirical validation of the undocumented `com.apple.screenIsLocked` notification — repeat on macOS 13, 14, 15.)
5. **Display reconfiguration:** `record start` → unplug external monitor that is currently primary mid-capture → continue 10 s → `record stop`. Verify MP4 is playable end-to-end (resolution discontinuity acceptable); `display_changes` array in state file records the event.

### Pre-implementation empirical verifications (from the Swift research)

These are not tests; they are spike checks to run **before** writing production code, to de-risk the design:

1. `com.apple.screenIsLocked` fires reliably from the binary's launch context on macOS 13/14/15.
2. Appending SCK `CMSampleBuffer`s directly to `AVAssetWriter` with `startSession(atSourceTime: firstPTS)` produces a playable mp4 of ≥10 s.
3. SCK on macOS 13 behavior across a primary-display reconfiguration (does it keep delivering against the stale display, error out, or silently switch?) — informs the exact teardown/recreate logic in `DisplayMonitor`.
