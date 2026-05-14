import Foundation
import ScreenCaptureKit

// MARK: - Stdout: line-buffered, single-line JSON events

// Force line-buffered stdout so the Python supervisor sees each event as soon
// as it's written. macOS defaults to block-buffered when stdout is a pipe.
setvbuf(stdout, nil, _IOLBF, 0)

/// Lock around `FileHandle.standardOutput` so events emitted from SCStream
/// callback threads and the main thread don't interleave bytes on the wire.
let stdoutLock = NSLock()

/// Write a single event as one JSON line followed by `\n`, then flush.
@inline(__always)
func emit(_ event: Event) {
    do {
        let line = try IPCCodec.encode(event)
        // Defensive: encoder shouldn't produce embedded newlines, but if some
        // future field ever did, collapse them so the line protocol stays intact.
        let safe = line.replacingOccurrences(of: "\n", with: " ")
        stdoutLock.lock()
        FileHandle.standardOutput.write(Data((safe + "\n").utf8))
        fflush(stdout)
        stdoutLock.unlock()
    } catch {
        // Last-ditch fallback so we don't drop the protocol entirely.
        let fallback = #"{"event":"error","message":"event encode failed"}"# + "\n"
        stdoutLock.lock()
        FileHandle.standardOutput.write(Data(fallback.utf8))
        fflush(stdout)
        stdoutLock.unlock()
    }
}

// MARK: - Time formatting

/// ISO-8601 UTC formatter, e.g. `2026-05-10T14:32:08Z`.
let iso8601: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = TimeZone(identifier: "UTC")
    return f
}()

// MARK: - CLI argument parsing

/// Parsed CLI flags. Returned as a struct so future additions don't keep
/// growing the tuple.
struct CLIFlags {
    var testSilentSources: Bool = false
    /// When non-nil, schedule `VideoCapture.simulateStreamFailure()` N seconds
    /// after video starts. Debug-only injection mechanism for slice 5's manual
    /// verification scenario 2; intentionally undocumented in `record start
    /// --help`. The Swift binary takes the flag directly when invoked
    /// standalone. Composes cleanly with `--test-silent-sources`.
    var simulateVideoFailureAfterSeconds: Double? = nil
    /// When true, swap the real `SCStream`-driven `VideoCapture` for a
    /// deterministic `SyntheticVideoSource`: single-color 640×360 frames at
    /// 30 fps fed directly into `MP4Writer`. Required by Slice 6's
    /// integration test so the full `start → frame flow → stop` cycle can
    /// run in CI without Screen Recording TCC or attached display hardware.
    /// Composes cleanly with `--test-silent-sources` (audio synthetic) and
    /// with `--simulate-video-failure-after-seconds` (synthetic failure
    /// injection runs against the synthetic source too).
    var testSyntheticVideo: Bool = false
    /// When true, the binary runs in **daemon mode**: it does NOT start a
    /// capture implicitly. It emits `ready`, idles on the main RunLoop, and
    /// services repeated `start` / `stop` cycles over stdin. After each
    /// `stopped` event, `capture` is set back to `nil` and the process stays
    /// alive — the orchestrator can issue another `start` against the same
    /// long-lived child. Only `shutdown` (explicit), EOF on stdin, or SIGTERM
    /// terminate the process. Composes cleanly with `--test-silent-sources`
    /// and `--test-synthetic-video`. Absent the flag, behavior is bit-for-bit
    /// unchanged from the legacy one-shot mode (`exit(0)` after `stopped`).
    var daemon: Bool = false
}

/// Parse CLI flags before any IPC begins. Supported flags:
///
/// - `--test-silent-sources` — swap the real `SCStream` / `AVAudioEngine`
///   audio sources for a deterministic synthetic feeder (slice 7 territory).
/// - `--test-synthetic-video` — swap the real `SCStream`-driven `VideoCapture`
///   for a deterministic `SyntheticVideoSource` that feeds `MP4Writer` with
///   single-color 640×360 frames at 30 fps. CI / headless test affordance;
///   independent parsing branch from `--test-silent-sources`.
/// - `--simulate-video-failure-after-seconds <N>` — schedule a synthetic
///   SCStream failure N seconds after video starts. Debug-only.
///
/// Unknown flags or malformed arguments emit a single `error` event on stdout
/// and exit non-zero, so the Python supervisor sees a predictable failure
/// rather than a hung process.
func parseCLIFlags() -> CLIFlags {
    var flags = CLIFlags()
    // CommandLine.arguments[0] is the executable path.
    let args = Array(CommandLine.arguments.dropFirst())
    var i = 0
    while i < args.count {
        let arg = args[i]
        switch arg {
        case "--test-silent-sources":
            flags.testSilentSources = true
            i += 1
        case "--test-synthetic-video":
            flags.testSyntheticVideo = true
            i += 1
        case "--daemon":
            // Order-independent boolean flag, no value argument. Composes
            // with `--test-silent-sources` and `--test-synthetic-video`.
            flags.daemon = true
            i += 1
        case "--simulate-video-failure-after-seconds":
            // Consume the following positional value. Accept either an Int or
            // a Double; reject negative, NaN, or missing values so behavior is
            // explicit (matching the `--test-silent-sources` "predictable
            // failure rather than a hung process" convention).
            guard i + 1 < args.count else {
                emitCLIFlagErrorAndExit(message: "--simulate-video-failure-after-seconds requires a numeric argument")
            }
            let raw = args[i + 1]
            guard let value = Double(raw), value.isFinite, value >= 0 else {
                emitCLIFlagErrorAndExit(message: "--simulate-video-failure-after-seconds: invalid value '\(raw)' (expected non-negative number)")
            }
            flags.simulateVideoFailureAfterSeconds = value
            i += 2
        default:
            emitCLIFlagErrorAndExit(message: "unknown CLI flag: \(arg)")
        }
    }
    return flags
}

/// Helper for `parseCLIFlags` — emit a single `error` event and exit. Marked
/// `Never` so the compiler knows control flow ends.
private func emitCLIFlagErrorAndExit(message: String) -> Never {
    // Emit directly to stdout; the lock-protected `emit` helper isn't strictly
    // required here (we're still single-threaded), but use the same shape as
    // the rest of the protocol.
    let escaped = message.replacingOccurrences(of: "\"", with: "\\\"")
    let line = #"{"event":"error","message":""# + escaped + #""}"# + "\n"
    FileHandle.standardOutput.write(Data(line.utf8))
    fflush(stdout)
    exit(1)
}

let cliFlags = parseCLIFlags()
let testSilentSources = cliFlags.testSilentSources
/// Top-level mirror of `cliFlags.daemon` for readability in the stop / dispatch
/// paths. See `CLIFlags.daemon` for semantics. When false (the legacy default),
/// the binary preserves the original one-shot behavior — `handleStop` exits the
/// process immediately after emitting `stopped`. When true, the same path
/// drops `capture` to nil and leaves the main RunLoop running so another
/// `start` can be serviced over stdin.
let daemonMode = cliFlags.daemon

// MARK: - Video startup error discrimination

/// Returns `true` when `error` represents a ScreenCaptureKit "the user has
/// denied Screen Recording" condition.
///
/// SCK's denial signal is `SCStreamError.Code.userDeclined` (raw value
/// `-3801`) inside the `SCStreamErrorDomain`. Our code wraps the underlying
/// SCK error inside `VideoCaptureError.primaryDisplayResolutionFailed` /
/// `VideoCaptureError.streamStartFailed`, so we unwrap those first before
/// reaching the bridged `NSError` view of the inner error.
///
/// Sources of the constants:
///   - `SCStreamError.Code.userDeclined.rawValue` (ScreenCaptureKit)
///   - Domain string: `SCStreamErrorDomain` (constant exposed by the framework)
///
/// Centralized here so the discrimination logic lives in one place. A future
/// unit test can drive this through the same surface without booting an
/// `SCStream`.
func isPermissionDenied(_ error: Error) -> Bool {
    let unwrapped: Error
    switch error {
    case let vce as VideoCaptureError:
        switch vce {
        case .primaryDisplayResolutionFailed(let underlying):
            unwrapped = underlying
        case .streamStartFailed(let underlying):
            unwrapped = underlying
        }
    default:
        unwrapped = error
    }
    let nsErr = unwrapped as NSError
    return nsErr.domain == SCStreamErrorDomain
        && nsErr.code == SCStreamError.Code.userDeclined.rawValue
}

/// Pick a wire-level `video_lost.reason` string for an error caught during
/// `handleStart`'s video bring-up. See the `catch` block in `handleStart` for
/// the documented set of reasons; this helper just collapses the Swift error
/// type into one of them.
func classifyVideoStartupError(_ error: Error) -> String {
    if isPermissionDenied(error) {
        return "permission_denied"
    }
    if let vce = error as? VideoCaptureError {
        switch vce {
        case .primaryDisplayResolutionFailed:
            return "display_resolution_failed"
        case .streamStartFailed:
            return "stream_start_failed"
        }
    }
    if error is MP4WriterError {
        return "writer_init_failed"
    }
    return "startup_failed"
}

// MARK: - Capture state

/// In-flight capture state. `nil` between `start` and `stop`.
///
/// `videoSource` is the unified surface across the production `VideoCapture`
/// (real `SCStream`) and `SyntheticVideoSource` (`--test-synthetic-video`).
/// `displayMonitor` is only populated for the real path — synthetic mode has
/// no display to monitor for reconfiguration.
final class CaptureState {
    let capture: AudioCapture
    let videoSource: VideoSource?
    let displayMonitor: DisplayReconfigurationMonitor?
    let systemEventMonitor: SystemEventMonitor?
    let outputPath: String
    let videoOutputPath: String?
    let startedAt: Date

    /// Non-nil when a `SystemEventMonitor` notification (sleep / display sleep
    /// / screen lock) initiated the in-flight stop. Read-and-write on the main
    /// queue only, so no lock is needed. Carries one of the `SystemEventReason`
    /// raw values (`"system_sleep"`, `"display_sleep"`, `"screen_locked"`)
    /// straight through to `Event.captureEndedBySystemEvent`.
    var endedBy: String? = nil

    init(
        capture: AudioCapture,
        videoSource: VideoSource?,
        displayMonitor: DisplayReconfigurationMonitor?,
        systemEventMonitor: SystemEventMonitor?,
        outputPath: String,
        videoOutputPath: String?,
        startedAt: Date
    ) {
        self.capture = capture
        self.videoSource = videoSource
        self.displayMonitor = displayMonitor
        self.systemEventMonitor = systemEventMonitor
        self.outputPath = outputPath
        self.videoOutputPath = videoOutputPath
        self.startedAt = startedAt
    }
}

var capture: CaptureState? = nil

// MARK: - Hotkey monitor

/// Single process-wide `HotkeyMonitor`. Constructed lazily on the first
/// `register_hotkey` command so the legacy one-shot path (which never
/// sends hotkey commands) doesn't pay the cost of installing the Carbon
/// event handler. The closure emits `hotkey_pressed` via the existing
/// `emit(...)` lock convention.
///
/// Race-mitigation per tech spec §3 ("Race: user presses hotkey while
/// daemon is mid-startup"): `register()` is synchronous and completes
/// before we emit `hotkey_registered`; any subsequent press dispatches
/// onto the main queue, where it is well-ordered after the registration
/// event already on the wire.
var hotkeyMonitor: HotkeyMonitor? = nil

func ensureHotkeyMonitor() -> HotkeyMonitor {
    if let m = hotkeyMonitor { return m }
    let m = HotkeyMonitor(onPress: {
        emit(.hotkeyPressed)
    })
    hotkeyMonitor = m
    return m
}

// MARK: - SIGTERM handler

// Ignore SIGTERM at the libc level so it doesn't kill us before our handler
// runs; Dispatch will deliver it to our handler instead.
signal(SIGTERM, SIG_IGN)
let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler {
    // Best-effort: finalize the mp4 synchronously so a SIGTERM mid-capture
    // still leaves a playable file. The audio path uses an `AVAudioFile`
    // which flushes on dealloc, so we don't need a comparable hook there.
    // Bounded by a short timeout so we don't hang the supervisor's TERM-then-
    // KILL escalation.
    if let videoSource = capture?.videoSource {
        _ = videoSource.finalizeSync(timeout: 2.0)
    }
    exit(0)
}
sigtermSource.resume()

// Defense-in-depth: if an `exit()` path is hit that bypasses the SIGTERM
// handler (e.g. an unexpected fatal error), still try to flush the mp4. The
// atexit closure must be C-compatible, so we read `capture` directly here.
atexit {
    if let videoSource = capture?.videoSource {
        _ = videoSource.finalizeSync(timeout: 2.0)
    }
}

// MARK: - Command handlers

func handleStart(
    outputPath: String,
    videoOutputPath: String?,
    format _: AudioFormat,
    video: VideoConfig?
) {
    if capture != nil {
        emit(.error(message: "start received while capture is already running"))
        return
    }

    let url = URL(fileURLWithPath: outputPath)

    let audioCapture: AudioCapture
    do {
        audioCapture = try AudioCapture(
            outputURL: url,
            emit: emit,
            testSilentSources: testSilentSources
        )
    } catch {
        emit(.error(message: "failed to construct AudioCapture: \(error)"))
        exit(1)
    }

    let startedAt = Date()

    Task {
        do {
            // Protocol-required ordering on the wire is:
            //   (permission_required*) → started → source_attached(*) → … → stopped
            //
            // The permission preflights may emit `permission_required` /
            // `permission_denied` events, so they must run *before* `started`.
            // Each source self-emits its own `source_attached` event as soon
            // as its underlying engine/stream is producing audio, so those
            // must run *after* `started`.
            try await audioCapture.checkPermissions()
            emit(.started(startTime: iso8601.string(from: startedAt)))
            try await audioCapture.startSources()

            // Now bring up video, if requested. Construction of the MP4
            // writer happens after audio is up so an mp4 file isn't created
            // unless we got past the audio-permission gate.
            //
            // Two parallel branches share the same `video_output_path` gate:
            //   - `--test-synthetic-video` → `SyntheticVideoSource` (fixed
            //     640×360 single-color frames, no SCK, no display required).
            //   - otherwise                → real `VideoCapture` against the
            //     primary display via `SCStream`.
            // Both produce a `VideoSource` and feed an `MP4Writer`; the rest
            // of the pipeline (display monitor wiring, finalize, signal
            // handlers) treats them uniformly via the protocol.
            var videoSource: VideoSource? = nil
            var displayMonitor: DisplayReconfigurationMonitor? = nil
            if let videoPath = videoOutputPath {
                let fps = video?.fps ?? 30
                let showsCursor = video?.showsCursor ?? true
                let videoURL = URL(fileURLWithPath: videoPath)
                // Track whether we created the MP4Writer (and therefore the
                // file on disk) so the catch block knows whether it needs to
                // clean up. AVAssetWriter creates the output file at
                // `startWriting`, but the writer instance touches the URL
                // earlier — we remove the file when no frame ever landed
                // (`hasStartedSession == false`).
                var writerForCleanup: MP4Writer? = nil
                do {
                    if cliFlags.testSyntheticVideo {
                        // Synthetic path: fixed 640×360, no SCK, no TCC, no
                        // display hardware required. Used by the integration
                        // test suite. The writer is configured to the same
                        // pixel dimensions as the synthetic frames so the
                        // encoder doesn't have to resample.
                        let writer = try MP4Writer(
                            url: videoURL,
                            widthPx: SyntheticVideoSource.widthPx,
                            heightPx: SyntheticVideoSource.heightPx,
                            fps: fps
                        )
                        writerForCleanup = writer
                        let source = SyntheticVideoSource(
                            writer: writer,
                            fps: fps,
                            displayId: 0,
                            emit: emit
                        )
                        try source.start()
                        videoSource = source
                        // No `DisplayReconfigurationMonitor` in synthetic mode
                        // — there is no real display to monitor.
                    } else {
                        let primary = try await DisplayMonitor.resolvePrimary()
                        let writer = try MP4Writer(
                            url: videoURL,
                            widthPx: primary.widthPx,
                            heightPx: primary.heightPx,
                            fps: fps
                        )
                        writerForCleanup = writer
                        let vc = VideoCapture(
                            writer: writer,
                            fps: fps,
                            showsCursor: showsCursor,
                            emit: emit
                        )
                        try await vc.start(display: primary)
                        videoSource = vc

                        // Video came up cleanly — start watching for display
                        // reconfigurations. Construction is gated on the
                        // `vc.start(...)` success above so we don't register a
                        // CG callback for a capture that never began.
                        let monitor = DisplayReconfigurationMonitor()
                        monitor.onReconfigure = { [weak vc, weak monitor] reason, newPrimary in
                            // Sync closure — bridge into async land to drive the
                            // reconfigure. We capture `vc` and `monitor` weakly
                            // so a late CG callback after `stop()` can't keep
                            // the capture state alive past its useful life.
                            guard let vc = vc else { return }
                            Task {
                                await vc.reconfigure(to: newPrimary, reason: reason)
                                monitor?.updateCapturedDisplayID(newPrimary.displayID)
                            }
                        }
                        monitor.onError = { msg in
                            FileHandle.standardError.write(Data((msg + "\n").utf8))
                        }
                        monitor.start(initialDisplayID: primary.displayID)
                        displayMonitor = monitor
                    }

                    // Schedule the synthetic-failure injection, if the debug
                    // flag is set. Reads `capture` on the main queue so the
                    // closure body needs no captured non-Sendable references.
                    // The flag composes with both video paths via the
                    // `VideoSource.simulateStreamFailure()` protocol surface.
                    if let after = cliFlags.simulateVideoFailureAfterSeconds {
                        DispatchQueue.main.asyncAfter(deadline: .now() + after) {
                            capture?.videoSource?.simulateStreamFailure()
                        }
                    }
                } catch {
                    // Best-effort: video failed at startup, but audio is
                    // already running and must continue. Discriminate the
                    // reason on the wire so the supervisor / CLI can render
                    // the right summary line.
                    //
                    // Recognized reasons (free-form on the wire — see
                    // `ipc.VideoLostEvent`):
                    //   - "permission_denied"       Screen Recording TCC denial
                    //   - "display_resolution_failed" SCShareableContent / primary lookup failed
                    //   - "writer_init_failed"      MP4Writer/AVAssetWriter ctor threw
                    //   - "stream_start_failed"     SCStream addOutput/startCapture threw (non-permission)
                    //   - "startup_failed"          Catch-all fallback
                    let reason = classifyVideoStartupError(error)
                    emit(.videoLost(
                        atOffsetSeconds: 0,
                        reason: reason,
                        message: "\(error)"
                    ))

                    // Don't leave a zero-byte / partial .mp4 on disk when no
                    // frame ever landed. AVAssetWriter creates the file at
                    // `startWriting`; if init succeeded but capture never
                    // started a session, the file is empty (or absent) and
                    // we still remove the path to be safe. We only delete in
                    // the never-started case — once a frame has been written
                    // we leave the partial file for the user.
                    if writerForCleanup?.hasStartedSession != true {
                        try? FileManager.default.removeItem(at: videoURL)
                    }
                }
            }

            // Install the system-event monitor *after* audio (and video, if
            // any) are running, so we don't trigger a stop during startup.
            // Sleep / display sleep / screen lock all funnel through the
            // regular `handleStop` path — the only difference is `endedBy`
            // is set first so the finalize block emits
            // `capture_ended_by_system_event` before `stopped`.
            let systemEventMonitor = SystemEventMonitor()
            systemEventMonitor.start { reason in
                DispatchQueue.main.async {
                    // Race guard: an in-flight `handleStop` may have already
                    // nilled `capture`. The monitor's first-event-wins flag
                    // means we won't be called twice, but the user could
                    // have run `record stop` between the notification and
                    // this main-queue hop.
                    guard let state = capture else { return }
                    // First-event-wins at the wiring layer too: only the
                    // first system event sets `endedBy`. (The monitor itself
                    // already guarantees a single fire, so this is belt-and-
                    // suspenders against any future change there.)
                    if state.endedBy == nil {
                        state.endedBy = reason
                    }
                    handleStop()
                }
            }

            capture = CaptureState(
                capture: audioCapture,
                videoSource: videoSource,
                displayMonitor: displayMonitor,
                systemEventMonitor: systemEventMonitor,
                outputPath: outputPath,
                videoOutputPath: videoOutputPath,
                startedAt: startedAt
            )
        } catch {
            emit(.error(message: "capture start failed: \(error)"))
            exit(1)
        }
    }
}

func handleStop() {
    guard let state = capture else {
        // Stop without a prior start — surface as an error and stay alive so
        // the supervisor can decide what to do.
        emit(.error(message: "stop received before start"))
        return
    }

    // Detach the system-event monitor as the very first step so a second
    // notification arriving mid-finalize (e.g. display sleep firing right
    // after system sleep) can't re-enter `handleStop`. `SystemEventMonitor`
    // is internally idempotent, but we want zero further callbacks once
    // teardown begins.
    state.systemEventMonitor?.stop()

    // Unregister the CG reconfiguration callback before we begin finalizing
    // video, so no late callback can trigger a stream rebuild against a
    // writer that's already on its way to `finishWriting`.
    state.displayMonitor?.stop()

    Task {
        // Finalize audio and video in parallel and bound the total wait at 5 s
        // so a hung writer can't hold the process open. A healthy
        // `AVAssetWriter.finishWriting` on a 1 h capture finishes well under a
        // second; a healthy WAV close is near-instant.
        let timeoutNs: UInt64 = 5 * 1_000_000_000

        let (audioDur, videoDur) = await withTaskGroup(
            of: (kind: String, audio: Double?, video: Double?).self,
            returning: (Double, Double?).self
        ) { group in
            group.addTask {
                let d = await state.capture.stop()
                return ("audio", d, nil)
            }
            if let vs = state.videoSource {
                group.addTask {
                    let d = await vs.stop()
                    return ("video", nil, d)
                }
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: timeoutNs)
                return ("timeout", nil, nil)
            }

            var audio: Double = 0
            var video: Double? = nil
            var sawTimeout = false
            let expected = (state.videoSource == nil) ? 1 : 2
            var completed = 0
            while let result = await group.next() {
                switch result.kind {
                case "audio":
                    audio = result.audio ?? 0
                    completed += 1
                case "video":
                    video = result.video
                    completed += 1
                case "timeout":
                    sawTimeout = true
                default:
                    break
                }
                if completed >= expected || sawTimeout {
                    break
                }
            }
            group.cancelAll()
            if sawTimeout && completed < expected {
                FileHandle.standardError.write(Data(
                    "warning: finalize did not complete within 5s timeout (audio+video may be partial)\n".utf8
                ))
            }
            return (audio, video)
        }

        if let path = state.videoOutputPath, let vDur = videoDur {
            emit(.videoFile(path: path, durationSeconds: vDur))
        }

        // Wire-order contract (the supervisor reads it in this order):
        //   video_file (if any) → capture_ended_by_system_event (if system-
        //   triggered) → stopped → exit 0.
        // Only emit `capture_ended_by_system_event` when the trigger came
        // from `SystemEventMonitor` — a user-initiated `record stop` must
        // not emit it.
        if let reasonRaw = state.endedBy,
           let reason = SystemEventReason(rawValue: reasonRaw)
        {
            // Round to 3 decimals to match the SourceLost precision
            // convention; spurious sub-millisecond jitter would only
            // confuse the orchestrator log without adding information.
            let raw = Date().timeIntervalSince(state.startedAt)
            let offset = (raw * 1000).rounded() / 1000
            emit(.captureEndedBySystemEvent(
                reason: reason,
                atOffsetSeconds: offset
            ))
        }

        emit(.stopped(durationSeconds: audioDur, outputPath: state.outputPath))

        if daemonMode {
            // Daemon mode: keep the process alive and reset for the next
            // `start`. Dropping the only strong reference to `CaptureState`
            // is what releases every per-capture resource (AudioCapture,
            // VideoSource, MP4Writer, DisplayReconfigurationMonitor,
            // SystemEventMonitor). The leak audit accompanying slice 4
            // verifies each of those classes' `stop()` paths fully releases
            // observers / streams / taps / DispatchSources, so this single
            // assignment is the complete teardown trigger.
            //
            // The stdin thread is still running and the RunLoop is still
            // turning, so a subsequent `start` command will land on the
            // main queue and bring up a fresh capture.
            capture = nil
        } else {
            // One-shot mode (legacy / supervisor.py / 001+002 integration
            // suite): behavior must be byte-for-byte identical to before
            // slice 4. Exit immediately after `stopped`.
            exit(0)
        }
    }
}

// MARK: - Hotkey command handlers

/// Handle `register_hotkey`. Validates the modifier list and key against
/// the closed grammar without touching Carbon (so we can fail loud on
/// obviously bad input without producing an `unknown_osstatus_*` token),
/// then calls into `HotkeyMonitor.register` and emits one
/// `hotkey_registered` event with the outcome. Always runs on the main
/// queue (the stdin reader dispatches here).
func handleRegisterHotkey(modifiers: [HotkeyModifier], key: String) {
    // FR 2.6 requires at least one modifier. Reject before we even
    // construct the monitor so the `accessibility_denied` probe doesn't
    // race ahead of a structurally-invalid request.
    if modifiers.isEmpty {
        emit(.hotkeyRegistered(
            status: .invalid,
            modifiers: modifiers,
            key: key,
            message: "no_modifiers"
        ))
        return
    }
    // Closed-grammar key validation. Mirrors the Python parser in
    // `src/record/hotkey.py`; the daemon shouldn't ship anything out of
    // range here, but failing loud beats silently sending a zero keycode
    // to Carbon (which would map to "a").
    guard let code = keyCode(for: key) else {
        emit(.hotkeyRegistered(
            status: .invalid,
            modifiers: modifiers,
            key: key,
            message: "unknown_key:\(key)"
        ))
        return
    }
    let mask = modifierMask(from: modifiers)
    let monitor = ensureHotkeyMonitor()
    let result = monitor.register(modifiers: mask, keyCode: code)
    switch result {
    case .registered:
        emit(.hotkeyRegistered(
            status: .registered,
            modifiers: modifiers,
            key: key,
            message: "registered"
        ))
    case .conflict:
        emit(.hotkeyRegistered(
            status: .conflict,
            modifiers: modifiers,
            key: key,
            message: "conflict"
        ))
    case .invalid(let message):
        emit(.hotkeyRegistered(
            status: .invalid,
            modifiers: modifiers,
            key: key,
            message: message
        ))
    }
}

/// Handle `unregister_hotkey`. Idempotent — calling against a never-
/// constructed monitor is a no-op (the `hotkey_unregistered` event is
/// still emitted so the orchestrator gets the ack it's waiting on).
func handleUnregisterHotkey() {
    hotkeyMonitor?.unregister()
    emit(.hotkeyUnregistered)
}

// MARK: - Stdin reader

/// The stdin reader runs on a background thread so the main thread is free for
/// the RunLoop that backs `Task` and SCStream's async work. `readLine()` is
/// blocking and would otherwise stall the main RunLoop, leaving the capture
/// task no chance to make progress.
let stdinThread = Thread {
    // Announce readiness immediately on startup.
    emit(.ready)

    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { continue }

        let command: Command
        do {
            command = try IPCCodec.decodeCommand(line: trimmed)
        } catch {
            emit(.error(message: "malformed command: \(error.localizedDescription)"))
            continue
        }

        // Dispatch onto the main queue so handlers observe a consistent
        // capture-state view and so `Task`s they spawn join the main
        // RunLoop's executor.
        DispatchQueue.main.async {
            switch command {
            case .start(let outputPath, let videoOutputPath, let format, let video):
                handleStart(
                    outputPath: outputPath,
                    videoOutputPath: videoOutputPath,
                    format: format,
                    video: video
                )
            case .stop:
                handleStop()
            case .shutdown:
                exit(0)
            case .registerHotkey(let modifiers, let key):
                handleRegisterHotkey(modifiers: modifiers, key: key)
            case .unregisterHotkey:
                handleUnregisterHotkey()
            }
        }
    }

    // EOF on stdin: treat as a clean shutdown so we don't linger as a zombie.
    DispatchQueue.main.async {
        exit(0)
    }
}
stdinThread.name = "record.stdin"
stdinThread.start()

// Drive the main RunLoop so dispatched work, SCStream callbacks delivered to
// the main queue, and the SIGTERM source all get to run.
RunLoop.main.run()
