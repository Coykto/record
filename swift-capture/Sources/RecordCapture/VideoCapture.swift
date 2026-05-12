import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreVideo
import CoreGraphics

/// Errors thrown by `VideoCapture` setup.
enum VideoCaptureError: Error, CustomStringConvertible {
    case primaryDisplayResolutionFailed(underlying: Error)
    case streamStartFailed(underlying: Error)

    var description: String {
        switch self {
        case .primaryDisplayResolutionFailed(let underlying):
            return "failed to resolve primary display: \(underlying)"
        case .streamStartFailed(let underlying):
            return "failed to start video SCStream: \(underlying)"
        }
    }
}

/// Owns the video-only `SCStream` for the primary display and forwards good
/// frames to an `MP4Writer`.
///
/// This is an **independent** `SCStream` from the audio-only one owned by
/// `AudioCapture` — a deliberate deviation from `architecture.md` documented in
/// `technical-considerations.md` §1: decoupling failure domains lets video
/// crash without taking down (the more valuable) audio.
final class VideoCapture: NSObject {

    private let writer: MP4Writer
    private let emit: (Event) -> Void
    private let fps: Int
    private let showsCursor: Bool

    /// Dedicated queue for SCStream's `.screen` output callbacks. Kept off the
    /// main queue so heavy encoder back-pressure can't stall stdin parsing or
    /// IPC writes.
    private let sampleQueue = DispatchQueue(
        label: "record.videocapture.samples",
        qos: .userInitiated
    )

    private var stream: SCStream?
    private var display: PrimaryDisplay?
    private var startedAt: Date?

    /// Has `video_started` been emitted yet? Flipped on the first `.complete`
    /// frame.
    private var videoStartedEmitted = false
    private let startedLock = NSLock()

    /// Has a `video_lost` event already been emitted for this capture? Used to
    /// suppress duplicates from the SCStream delegate firing more than once.
    private var videoLost = false
    private let lossLock = NSLock()

    init(
        writer: MP4Writer,
        fps: Int,
        showsCursor: Bool,
        emit: @escaping (Event) -> Void
    ) {
        self.writer = writer
        self.fps = fps
        self.showsCursor = showsCursor
        self.emit = emit
        super.init()
        // Hook MP4Writer's async-failure callback so an AVAssetWriter failure
        // (encoder error, startWriting failure, finishWriting failure) surfaces
        // as `video_lost(reason: "writer_failure")` instead of just a stderr
        // line. Routed through `claimVideoLoss()` so a writer failure and an
        // SCStream error can't both emit — whichever wins reports the cause.
        writer.onAsyncFailure = { [weak self] message in
            self?.handleWriterFailure(message: message)
        }
    }

    // MARK: - Lifecycle

    /// Build the `SCStream`, attach this object as `.screen` output, and start
    /// capture. Throws if the primary display can't be resolved or `SCStream`
    /// fails to start.
    func start(display: PrimaryDisplay) async throws {
        let stream = try await buildStream(for: display)
        self.stream = stream
        self.display = display
        startedAt = Date()
    }

    /// Build a new `SCStream` against `display`, attach this object as the
    /// `.screen` output on `sampleQueue`, and start it. Returns the running
    /// stream. Used by both initial `start(display:)` and `reconfigure(to:reason:)`.
    private func buildStream(for display: PrimaryDisplay) async throws -> SCStream {
        let filter = SCContentFilter(display: display.scDisplay, excludingWindows: [])

        let config = SCStreamConfiguration()
        config.width = display.widthPx
        config.height = display.heightPx
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
        config.showsCursor = showsCursor
        config.queueDepth = 5
        config.capturesAudio = false
        // Leave `pixelFormat` at the default kCVPixelFormatType_32BGRA — the
        // spec explicitly says don't override.

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: sampleQueue)
        } catch {
            throw VideoCaptureError.streamStartFailed(underlying: error)
        }

        do {
            try await stream.startCapture()
        } catch {
            throw VideoCaptureError.streamStartFailed(underlying: error)
        }

        return stream
    }

    /// Switch the running capture to a (possibly different) primary display
    /// after a `CGDisplayReconfiguration` event. Stops the current `SCStream`,
    /// builds a fresh one against `newDisplay`, and emits
    /// `display_reconfigured` only once the new stream is up.
    ///
    /// The `MP4Writer` is **intentionally kept** across reconfigs. Its
    /// `outputSettings` width/height were fixed at init, but
    /// `AVAssetWriterInput` accepts buffers of different dimensions in the
    /// same track — the encoder handles resampling. Per functional spec §2.7
    /// a resolution discontinuity inside the same `.mp4` is acceptable;
    /// rotating to a fresh writer would split the recording into two files
    /// instead, which is not what the spec asks for.
    func reconfigure(to newDisplay: PrimaryDisplay, reason: DisplayReconfigurationReason) async {
        // Tear down the old stream best-effort. The system may have already
        // invalidated it (e.g. the captured display was just unplugged), in
        // which case `stopCapture` throws — we swallow because the only thing
        // we'd do with the error is log it, and the rebuild result below is
        // what actually matters.
        if let old = stream {
            try? await old.stopCapture()
        }
        stream = nil

        do {
            let newStream = try await buildStream(for: newDisplay)
            self.stream = newStream
            self.display = newDisplay

            emit(.displayReconfigured(
                reason: reason,
                newDisplayId: Int(newDisplay.displayID),
                newWidthPx: newDisplay.widthPx,
                newHeightPx: newDisplay.heightPx
            ))
        } catch {
            // Rebuild failed. Don't crash — audio capture is independent and
            // must continue. Surface as a `video_lost` so the supervisor can
            // record it; the partial MP4 will be finalized by the regular
            // stop path. The lock dance is in a sync helper because Swift 6
            // forbids holding an `NSLock` across an `await`, even though no
            // await actually appears between lock/unlock here.
            if claimVideoLoss() {
                let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0
                emit(.videoLost(
                    atOffsetSeconds: offset,
                    reason: "reconfigure_failed",
                    message: "\(error)"
                ))
            }
        }
    }

    /// Atomically claim the right to emit a single `video_lost`. Returns true
    /// for the first caller, false for every subsequent one. Lives outside any
    /// async function so the lock is never observed in an async scope.
    private func claimVideoLoss() -> Bool {
        lossLock.lock()
        defer { lossLock.unlock() }
        if videoLost { return false }
        videoLost = true
        return true
    }

    /// Tear down the SCStream and finalize the MP4 writer. Returns the encoded
    /// duration in seconds reported by the writer. Safe to call multiple times.
    func stop() async -> Double {
        if let stream = stream {
            try? await stream.stopCapture()
        }
        stream = nil
        return await writer.finalize()
    }

    /// Best-effort synchronous finalize for signal handlers. Stops the stream
    /// fire-and-forget (no await) and calls `MP4Writer.finalizeSync`.
    @discardableResult
    func finalizeSync(timeout: TimeInterval) -> Bool {
        if let stream = stream {
            // Detach so the system doesn't keep delivering frames into a
            // half-finalized writer. We don't await — the signal handler
            // can't run async code.
            Task { try? await stream.stopCapture() }
        }
        stream = nil
        return writer.finalizeSync(timeout: timeout)
    }

    // MARK: - Frame handling

    fileprivate func handleVideoFrame(_ sampleBuffer: CMSampleBuffer) {
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }

        // Drop non-`.complete` frames (idle / blank / suspended). Appending
        // these to AVAssetWriter produces duplicate timestamps and corrupts
        // the track — see technical-considerations.md §3 risks.
        guard let attachmentsArray = CMSampleBufferGetSampleAttachmentsArray(
            sampleBuffer,
            createIfNecessary: false
        ) as? [[SCStreamFrameInfo: Any]],
              let attachments = attachmentsArray.first,
              let rawStatus = attachments[.status] as? Int,
              let status = SCFrameStatus(rawValue: rawStatus),
              status == .complete
        else {
            return
        }

        let accepted = writer.append(sampleBuffer)
        if !accepted { return }

        emitVideoStartedIfNeeded()
    }

    private func emitVideoStartedIfNeeded() {
        startedLock.lock()
        let shouldEmit = !videoStartedEmitted
        if shouldEmit { videoStartedEmitted = true }
        startedLock.unlock()
        guard shouldEmit, let display = display else { return }
        emit(.videoStarted(
            displayId: Int(display.displayID),
            widthPx: display.widthPx,
            heightPx: display.heightPx,
            fps: fps
        ))
    }

    fileprivate func handleStreamError(_ error: Error) {
        guard claimVideoLoss() else { return }

        let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        emit(.videoLost(
            atOffsetSeconds: offset,
            reason: "sc_stream_error",
            message: error.localizedDescription
        ))

        // Eagerly finalize the MP4 so a partial-but-playable file is on disk
        // **before** the user runs `record stop`. SCK's delegate callback runs
        // on an internal queue (Apple does not guarantee main); we can't bridge
        // into `await writer.finalize()` from here without spawning a Task and
        // risking the process exiting first. `MP4Writer.finalize()` is
        // idempotent (its `prepareFinalize()` short-circuits `.finished` /
        // `.failed`), so a later `vc.stop()` from `handleStop()` will safely
        // return the cached duration and `handleStop` will still emit
        // `video_file` in the right wire order. Bounded at 2 s so a
        // pathologically slow `finishWriting` can't keep this thread alive.
        _ = writer.finalizeSync(timeout: 2.0)
    }

    /// Sibling of `handleStreamError` invoked from `MP4Writer.onAsyncFailure`
    /// when the writer itself fails (encoder error, `startWriting` failure,
    /// `finishWriting` failure). Emits `video_lost(reason: "writer_failure")`
    /// at most once thanks to `claimVideoLoss()`. Does **not** call
    /// `finalizeSync` — `MP4Writer` has already transitioned to `.failed` by
    /// the time this fires.
    fileprivate func handleWriterFailure(message: String) {
        guard claimVideoLoss() else { return }
        let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        emit(.videoLost(
            atOffsetSeconds: offset,
            reason: "writer_failure",
            message: message
        ))
    }

    // MARK: - Test injection (debug-only, not user-facing)

    /// Synthesize an `SCStreamDelegate.didStopWithError` for the
    /// `--simulate-video-failure-after-seconds <N>` CLI flag used in slice 5's
    /// manual verification scenario 2. Routes through the same
    /// `handleStreamError` path as a real SCK failure so the production code
    /// path is exercised end-to-end (eager finalize + `video_lost(sc_stream_error)`).
    ///
    /// **Debug only.** Not advertised in any user-facing help text. The error
    /// code (-3808) is deliberately not the permission-denial code (-3801) so
    /// the discriminator in `main.swift` does not treat it as a TCC denial.
    func simulateStreamFailure() {
        let synthetic = NSError(
            domain: "SCStreamErrorDomain",
            code: -3808,
            userInfo: [NSLocalizedDescriptionKey: "synthetic SCStream failure (--simulate-video-failure-after-seconds)"]
        )
        handleStreamError(synthetic)
    }
}

// MARK: - SCStreamOutput

extension VideoCapture: SCStreamOutput {
    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .screen else { return }
        handleVideoFrame(sampleBuffer)
    }
}

// MARK: - SCStreamDelegate

extension VideoCapture: SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        handleStreamError(error)
    }
}
