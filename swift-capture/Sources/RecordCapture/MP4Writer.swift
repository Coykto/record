import Foundation
import AVFoundation
import CoreMedia
import VideoToolbox

/// Errors thrown by `MP4Writer` setup or finalization.
enum MP4WriterError: Error, CustomStringConvertible {
    case writerCreationFailed(underlying: Error)
    case cannotAddInput
    case startWritingFailed(status: AVAssetWriter.Status, error: Error?)

    var description: String {
        switch self {
        case .writerCreationFailed(let underlying):
            return "AVAssetWriter init failed: \(underlying)"
        case .cannotAddInput:
            return "AVAssetWriter cannot add video input"
        case .startWritingFailed(let status, let error):
            let detail = error.map { String(describing: $0) } ?? "no underlying error"
            return "AVAssetWriter.startWriting failed (status=\(status.rawValue)): \(detail)"
        }
    }
}

/// Wraps an `AVAssetWriter` configured for H.264 inside an `.mp4` container,
/// video-only. Single input, real-time mode, started against the first accepted
/// frame's PTS.
///
/// ## Footguns this class explicitly avoids
///
/// - `startSession(atSourceTime: .zero)` against SCK buffers (which carry
///   host-clock PTS) produces a zero-byte file. We defer `startSession` until
///   the first accepted frame and pass that frame's PTS.
/// - Standard `.mp4` is not crash-recoverable: if the process dies before
///   `finishWriting` completes the moov atom is never written. Callers wire
///   `finalize()` into the `stop` path, SIGTERM, and `atexit` for defense in
///   depth (fragmented mp4 is an explicit non-goal for Phase 1).
final class MP4Writer {

    private let url: URL
    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput

    /// Guards `state`, `firstPTS`, and `lastPTS` against simultaneous touches
    /// from the SCStream sample queue (append) and the main queue (finalize).
    private let stateLock = NSLock()

    private enum State {
        case idle
        case writing
        case finalizing
        case finished
        case failed
    }
    private var state: State = .idle
    private var firstPTS: CMTime?
    private var lastPTS: CMTime?

    let widthPx: Int
    let heightPx: Int
    let fps: Int

    /// Optional callback invoked once when the writer asynchronously transitions
    /// into a failed state during normal operation — i.e. `startWriting()`
    /// returns false on the first frame, or `AVAssetWriter` flips to
    /// `.failed` after an `input.append(...)` call. The string carries a
    /// short human-readable description (typically the wrapped `writer.error`).
    ///
    /// Wired by `VideoCapture` into its `video_lost(reason: "writer_failure")`
    /// path so the supervisor can distinguish encoder failures from
    /// `SCStreamDelegate.didStopWithError` failures. The callback is invoked
    /// from the SCStream sample queue (i.e. off the main queue) — the receiver
    /// must be thread-safe or hop to its own queue. Fired **at most once** per
    /// writer instance; subsequent failures are silently swallowed because
    /// the supervisor only wants one `video_lost` per capture.
    var onAsyncFailure: ((String) -> Void)?
    private var asyncFailureFired = false

    init(url: URL, widthPx: Int, heightPx: Int, fps: Int) throws {
        self.url = url
        self.widthPx = widthPx
        self.heightPx = heightPx
        self.fps = fps

        // Remove a stale file at the same path — AVAssetWriter refuses to
        // overwrite. This matches the WAV writer's create-fresh semantics.
        if FileManager.default.fileExists(atPath: url.path) {
            try? FileManager.default.removeItem(at: url)
        }

        do {
            self.writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        } catch {
            throw MP4WriterError.writerCreationFailed(underlying: error)
        }

        // Bitrate formula: linear-in-pixel-count scaling around a 1080p30
        // anchor of 6 Mbps, clamped to [6 Mbps, 25 Mbps]. Produces ~6 Mbps at
        // 1080p, ~10–11 Mbps at 1440p, capped at 25 Mbps for 4K/5K to keep
        // file sizes sane for hour-long meetings.
        let pixels = Double(widthPx * heightPx)
        let anchorPixels = 1920.0 * 1080.0
        let bitrate = max(6_000_000, min(25_000_000, Int(pixels / anchorPixels * 6_000_000)))

        let compression: [String: Any] = [
            AVVideoAverageBitRateKey: bitrate,
            AVVideoExpectedSourceFrameRateKey: fps,
            AVVideoMaxKeyFrameIntervalKey: 60,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel
        ]
        let outputSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: widthPx,
            AVVideoHeightKey: heightPx,
            AVVideoCompressionPropertiesKey: compression
        ]

        let input = AVAssetWriterInput(mediaType: .video, outputSettings: outputSettings)
        input.expectsMediaDataInRealTime = true
        self.input = input

        guard writer.canAdd(input) else {
            throw MP4WriterError.cannotAddInput
        }
        writer.add(input)
    }

    // MARK: - Lifecycle

    /// Has the writer accepted at least one frame? Used by callers (e.g.
    /// `VideoCapture`) to know when to emit `video_started`.
    var hasStartedSession: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return firstPTS != nil
    }

    /// Append one `CMSampleBuffer` to the video track.
    ///
    /// On the very first accepted frame, calls `startWriting()` +
    /// `startSession(atSourceTime: firstPTS)`. Returns `true` if the frame
    /// was appended (or buffered for append), `false` if the input wasn't
    /// ready or the writer was already finalized.
    @discardableResult
    func append(_ sampleBuffer: CMSampleBuffer) -> Bool {
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard CMTIME_IS_VALID(pts) else { return false }

        stateLock.lock()
        switch state {
        case .finalizing, .finished, .failed:
            stateLock.unlock()
            return false
        case .idle:
            // First accepted frame: kick the writer over. Has to happen
            // inside the lock so a concurrent append can't observe `.idle`
            // and double-start.
            if !writer.startWriting() {
                state = .failed
                let errDesc = String(describing: writer.error)
                stateLock.unlock()
                FileHandle.standardError.write(Data(
                    "mp4 writer: startWriting failed: \(errDesc)\n".utf8
                ))
                fireAsyncFailureOnce(message: "startWriting failed: \(errDesc)")
                return false
            }
            writer.startSession(atSourceTime: pts)
            firstPTS = pts
            state = .writing
        case .writing:
            break
        }
        lastPTS = pts
        stateLock.unlock()

        if !input.isReadyForMoreMediaData {
            // Real-time mode: drop the frame rather than block the capture
            // queue. SCK's queueDepth=5 absorbs short hiccups; sustained
            // back-pressure indicates the encoder can't keep up.
            return false
        }
        let appended = input.append(sampleBuffer)
        if !appended {
            // `input.append(_:)` returns false either because the input wasn't
            // ready (handled above) or because the writer has transitioned to
            // `.failed` asynchronously — e.g. VideoToolbox surfaced an encode
            // error. Discriminate by checking `writer.status` and surface the
            // latter as a writer failure to the wiring layer.
            if writer.status == .failed {
                stateLock.lock()
                state = .failed
                let errDesc = String(describing: writer.error)
                stateLock.unlock()
                FileHandle.standardError.write(Data(
                    "mp4 writer: input.append failed (writer.status=.failed): \(errDesc)\n".utf8
                ))
                fireAsyncFailureOnce(message: "append failed: \(errDesc)")
            }
        }
        return appended
    }

    /// Fire `onAsyncFailure` at most once for the lifetime of this writer. Called
    /// from `append()` when the writer flips into a failed state, and from
    /// `recordFinishWritingOutcome` if the failure shows up only at finalize.
    /// Must not be invoked with `stateLock` held — the callback runs the wiring
    /// layer's emit closure, which acquires its own stdout lock.
    private func fireAsyncFailureOnce(message: String) {
        stateLock.lock()
        if asyncFailureFired {
            stateLock.unlock()
            return
        }
        asyncFailureFired = true
        let cb = onAsyncFailure
        stateLock.unlock()
        cb?(message)
    }

    /// Outcome of the synchronous pre-finalize state transition. Encodes the
    /// short list of things `finalize()` needs to know after grabbing the lock
    /// without holding it across an `await`.
    private enum FinalizePrep {
        case alreadyDone(duration: Double)
        case nothingWritten
        case waitForOtherCaller
        case proceed
    }

    private func prepareFinalize() -> FinalizePrep {
        stateLock.lock()
        defer { stateLock.unlock() }
        switch state {
        case .finished, .failed:
            return .alreadyDone(duration: computeDurationLocked())
        case .finalizing:
            return .waitForOtherCaller
        case .idle:
            state = .finished
            return .nothingWritten
        case .writing:
            state = .finalizing
            return .proceed
        }
    }

    /// Poll the state lock once; return a duration if a sibling finalize call
    /// has reached a terminal state, otherwise nil to keep polling.
    private func pollOtherCallerFinish() -> Double? {
        stateLock.lock()
        defer { stateLock.unlock() }
        switch state {
        case .finished, .failed:
            return computeDurationLocked()
        default:
            return nil
        }
    }

    private func recordFinishWritingOutcome() -> Double {
        stateLock.lock()
        var failedDescription: String? = nil
        if writer.status == .failed {
            state = .failed
            let errDesc = String(describing: writer.error)
            FileHandle.standardError.write(Data(
                "mp4 writer: finishWriting failed: \(errDesc)\n".utf8
            ))
            failedDescription = "finishWriting failed: \(errDesc)"
        } else {
            state = .finished
        }
        let duration = computeDurationLocked()
        stateLock.unlock()

        // Notify the wiring layer **only if** this is the first observed
        // failure — an earlier `startWriting`/`append` failure path would
        // already have called `fireAsyncFailureOnce`. The helper itself is
        // idempotent so the late call here is safe even if the SCStream
        // delegate path also called us.
        if let msg = failedDescription {
            fireAsyncFailureOnce(message: msg)
        }
        return duration
    }

    /// Finalize the writer: mark the input finished, run `finishWriting`, and
    /// return the recorded duration in seconds (or 0 if nothing was ever
    /// written).
    ///
    /// Idempotent: subsequent calls return the cached result.
    ///
    /// All state-lock interactions are confined to non-async helpers so the
    /// lock is never held across an `await` (Swift 6 forbids that and emits a
    /// `NSLock.lock unavailable from async contexts` warning otherwise).
    func finalize() async -> Double {
        switch prepareFinalize() {
        case .alreadyDone(let duration):
            return duration
        case .nothingWritten:
            return 0
        case .waitForOtherCaller:
            while true {
                if let d = pollOtherCallerFinish() {
                    return d
                }
                try? await Task.sleep(nanoseconds: 10_000_000) // 10 ms
            }
        case .proceed:
            break
        }

        input.markAsFinished()

        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            writer.finishWriting {
                continuation.resume()
            }
        }

        return recordFinishWritingOutcome()
    }

    /// Best-effort synchronous finalize, intended for signal handlers and
    /// `atexit`. Bounded to `timeout` seconds; returns `false` if the writer
    /// did not finish in time. The async `finalize()` is strongly preferred
    /// for the stop path.
    @discardableResult
    func finalizeSync(timeout: TimeInterval) -> Bool {
        stateLock.lock()
        switch state {
        case .finished, .failed:
            stateLock.unlock()
            return true
        case .idle:
            state = .finished
            stateLock.unlock()
            return true
        case .finalizing:
            // Already in progress — wait it out.
            stateLock.unlock()
        case .writing:
            state = .finalizing
            stateLock.unlock()
            input.markAsFinished()
            let semaphore = DispatchSemaphore(value: 0)
            writer.finishWriting {
                semaphore.signal()
            }
            let waited = semaphore.wait(timeout: .now() + timeout)
            stateLock.lock()
            if waited == .timedOut {
                stateLock.unlock()
                return false
            }
            state = (writer.status == .failed) ? .failed : .finished
            stateLock.unlock()
            return true
        }

        // .finalizing branch fell through: poll-wait on the lock.
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            stateLock.lock()
            if case .finished = state { stateLock.unlock(); return true }
            if case .failed = state { stateLock.unlock(); return true }
            stateLock.unlock()
            Thread.sleep(forTimeInterval: 0.01)
        }
        return false
    }

    /// Caller must hold `stateLock`. Returns the duration in seconds spanned by
    /// `firstPTS` … `lastPTS`, or 0 when no frame was ever accepted.
    private func computeDurationLocked() -> Double {
        guard let first = firstPTS, let last = lastPTS else { return 0 }
        let delta = CMTimeSubtract(last, first)
        let seconds = CMTimeGetSeconds(delta)
        return seconds.isFinite ? max(0, seconds) : 0
    }
}
