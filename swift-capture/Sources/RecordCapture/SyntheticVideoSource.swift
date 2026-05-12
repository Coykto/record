import Foundation
import CoreMedia
import CoreVideo

/// Common surface implemented by both the production `VideoCapture` (real
/// `SCStream` against the primary display) and the `SyntheticVideoSource`
/// (deterministic synthetic frames, no SCK / no display required).
///
/// Exists so `main.swift`'s `handleStop()`, signal handlers, and the synthetic-
/// failure injection wiring can treat the two paths uniformly. Anything specific
/// to one or the other (e.g. `VideoCapture.reconfigure(to:reason:)` for display
/// hot-plugging, or the `SCStream` build path) stays on the concrete type.
protocol VideoSource: AnyObject {
    /// Stop the source and finalize the underlying MP4. Returns the encoded
    /// duration in seconds reported by the writer. Safe to call multiple times.
    func stop() async -> Double

    /// Best-effort synchronous finalize for signal handlers / `atexit`. Bounded
    /// to `timeout` seconds; returns `false` if the writer did not finish in
    /// time.
    @discardableResult
    func finalizeSync(timeout: TimeInterval) -> Bool

    /// Synthesize a stream failure for debug injection
    /// (`--simulate-video-failure-after-seconds <N>`). Drives the same
    /// `video_lost(reason: "sc_stream_error")` codepath as a real SCK failure.
    func simulateStreamFailure()
}

// MARK: - VideoCapture conformance

// `VideoCapture` already exposes `stop()`, `finalizeSync(timeout:)`, and
// `simulateStreamFailure()` with the matching signatures, so an empty
// conformance declaration is sufficient.
extension VideoCapture: VideoSource {}

// MARK: - SyntheticVideoSource

/// Deterministic synthetic video source for `--test-synthetic-video`.
///
/// Bypasses `SCStream` entirely: a periodic `DispatchSourceTimer` produces
/// single-color `CVPixelBuffer`s at a fixed 640×360 resolution with
/// monotonically increasing PTS at 30 fps, and feeds them to an `MP4Writer`.
///
/// ## Why this exists
///
/// CI and headless test environments don't have Screen Recording TCC granted
/// and may not have a real display attached. The integration test suite still
/// needs to drive a full `start → frame flow → stop → playable mp4` cycle
/// against the production `MP4Writer` to catch protocol/wiring regressions.
/// Synthetic mode satisfies that without depending on `SCShareableContent`.
///
/// ## Lifecycle
///
/// Constructed by `main.swift` when both `--test-synthetic-video` and a
/// `video_output_path` are present. `start()` emits `video_started` against a
/// synthetic display id (`0`) and kicks off the 30 fps timer.
/// `stop()` cancels the timer and finalizes the writer.
///
/// ## Composability
///
/// Independent of `--test-silent-sources`: the audio flag swaps audio-source
/// engines inside `AudioCapture`; this flag swaps the video source. The two
/// flags compose without sharing state, so a CI run can drive a fully
/// headless capture by passing both.
final class SyntheticVideoSource {

    /// The fixed synthetic capture size. Matches the value the integration
    /// test asserts against and is independent of any real display. 640×360
    /// is small enough to encode quickly in CI while still being a
    /// non-degenerate H.264 resolution (both dimensions even, ≥ 16).
    static let widthPx = 640
    static let heightPx = 360

    private let writer: MP4Writer
    private let emit: (Event) -> Void
    private let fps: Int
    private let displayId: Int

    /// Dedicated queue for the frame-generation timer and CVPixelBuffer
    /// allocation. Kept off the main queue so the heavy work doesn't stall
    /// stdin parsing or IPC writes. Matches the spirit of `VideoCapture`'s
    /// `sampleQueue`.
    private let frameQueue = DispatchQueue(
        label: "record.syntheticvideo.frames",
        qos: .userInitiated
    )

    /// Reusable BGRA pixel buffer pool. `CVPixelBufferCreate` per frame
    /// allocates ~900 KB at 640×360 BGRA, which is wasteful — a pool reuses
    /// buffers across frames once consumers are done with them.
    private var pixelBufferPool: CVPixelBufferPool?

    private var timer: DispatchSourceTimer?
    private var startedAt: Date?
    private var frameIndex: Int64 = 0

    /// First-frame-wins gate for `video_started`. Mirrors `VideoCapture`'s flag.
    private var videoStartedEmitted = false
    private let startedLock = NSLock()

    /// First-failure-wins gate for `video_lost`. Mirrors `VideoCapture`'s flag.
    private var videoLost = false
    private let lossLock = NSLock()

    init(
        writer: MP4Writer,
        fps: Int = 30,
        displayId: Int = 0,
        emit: @escaping (Event) -> Void
    ) {
        self.writer = writer
        self.fps = fps
        self.displayId = displayId
        self.emit = emit
        // Hook MP4Writer's async-failure callback the same way `VideoCapture`
        // does — a writer-side failure surfaces as `video_lost(writer_failure)`
        // through the same first-event-wins gate as our own `simulateStreamFailure`.
        writer.onAsyncFailure = { [weak self] message in
            self?.handleWriterFailure(message: message)
        }
    }

    // MARK: - Lifecycle

    /// Allocate the pixel buffer pool and start the 30 fps generation timer.
    /// Throws only on pool creation failure (extremely unlikely for a fixed
    /// 640×360 BGRA pool); the caller treats this like any other video startup
    /// error and the catch block in `main.swift` will emit
    /// `video_lost(reason: "startup_failed")`.
    func start() throws {
        let pool = try Self.createPixelBufferPool(
            widthPx: Self.widthPx,
            heightPx: Self.heightPx
        )
        pixelBufferPool = pool
        startedAt = Date()

        // Interval-based timer at the configured fps. Using `.never` for the
        // leeway gives the encoder predictable PTS jitter; the actual PTS we
        // write into the sample buffer is derived from `frameIndex`, not from
        // when the timer happened to fire, so frame-rate stability on the wire
        // is unaffected by timer drift.
        let t = DispatchSource.makeTimerSource(queue: frameQueue)
        let intervalNs = UInt64(1_000_000_000 / max(1, fps))
        t.schedule(deadline: .now(), repeating: .nanoseconds(Int(intervalNs)), leeway: .nanoseconds(0))
        t.setEventHandler { [weak self] in
            self?.produceFrame()
        }
        timer = t
        t.resume()
    }

    /// Cancel the timer, then asynchronously finalize the writer. Idempotent.
    func stop() async -> Double {
        cancelTimer()
        return await writer.finalize()
    }

    /// Best-effort synchronous finalize for signal handlers. Cancels the timer
    /// fire-and-forget and runs `MP4Writer.finalizeSync`.
    @discardableResult
    func finalizeSync(timeout: TimeInterval) -> Bool {
        cancelTimer()
        return writer.finalizeSync(timeout: timeout)
    }

    private func cancelTimer() {
        if let t = timer {
            t.cancel()
        }
        timer = nil
    }

    // MARK: - Failure injection

    /// Synthesize a stream failure (the synthetic equivalent of
    /// `SCStreamDelegate.didStopWithError`). Cancels the timer, emits
    /// `video_lost(reason: "sc_stream_error")` exactly once, and eagerly
    /// finalizes the MP4 so a partial-but-playable file is on disk before
    /// the user runs `record stop`. Matches `VideoCapture.handleStreamError`
    /// shape so the production code path is exercised end-to-end under
    /// `--test-synthetic-video --simulate-video-failure-after-seconds N`.
    func simulateStreamFailure() {
        guard claimVideoLoss() else { return }
        cancelTimer()
        let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        emit(.videoLost(
            atOffsetSeconds: offset,
            reason: "sc_stream_error",
            message: "synthetic stream failure (--simulate-video-failure-after-seconds)"
        ))
        // Eagerly finalize so the partial mp4 is playable before `handleStop`
        // runs. The writer is idempotent — a later `stop()` will return the
        // cached duration.
        _ = writer.finalizeSync(timeout: 2.0)
    }

    /// Atomically claim the right to emit a single `video_lost`. Mirror of
    /// `VideoCapture.claimVideoLoss`.
    private func claimVideoLoss() -> Bool {
        lossLock.lock()
        defer { lossLock.unlock() }
        if videoLost { return false }
        videoLost = true
        return true
    }

    /// Sibling of `simulateStreamFailure` for `MP4Writer.onAsyncFailure`.
    private func handleWriterFailure(message: String) {
        guard claimVideoLoss() else { return }
        cancelTimer()
        let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        emit(.videoLost(
            atOffsetSeconds: offset,
            reason: "writer_failure",
            message: message
        ))
    }

    // MARK: - Frame production

    private func produceFrame() {
        guard let pool = pixelBufferPool else { return }
        let currentIndex = frameIndex
        frameIndex += 1

        guard let buffer = Self.makeSampleBuffer(
            fromPool: pool,
            frameIndex: currentIndex,
            fps: fps
        ) else {
            return
        }

        let accepted = writer.append(buffer)
        if !accepted { return }

        emitVideoStartedIfNeeded()
    }

    private func emitVideoStartedIfNeeded() {
        startedLock.lock()
        let shouldEmit = !videoStartedEmitted
        if shouldEmit { videoStartedEmitted = true }
        startedLock.unlock()
        guard shouldEmit else { return }
        emit(.videoStarted(
            displayId: displayId,
            widthPx: Self.widthPx,
            heightPx: Self.heightPx,
            fps: fps
        ))
    }

    // MARK: - Pixel buffer / sample buffer construction

    /// Build a `CVPixelBufferPool` capable of producing 32BGRA buffers of the
    /// requested size. Throws a generic NSError if pool creation fails so the
    /// caller can fold it into the standard video-startup catch block.
    static func createPixelBufferPool(widthPx: Int, heightPx: Int) throws -> CVPixelBufferPool {
        let pixelAttrs: [CFString: Any] = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey: widthPx,
            kCVPixelBufferHeightKey: heightPx,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
            kCVPixelBufferCGImageCompatibilityKey: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey: true
        ]
        let poolAttrs: [CFString: Any] = [
            kCVPixelBufferPoolMinimumBufferCountKey: 4
        ]
        var pool: CVPixelBufferPool?
        let status = CVPixelBufferPoolCreate(
            kCFAllocatorDefault,
            poolAttrs as CFDictionary,
            pixelAttrs as CFDictionary,
            &pool
        )
        guard status == kCVReturnSuccess, let pool = pool else {
            throw NSError(
                domain: "SyntheticVideoSource",
                code: Int(status),
                userInfo: [NSLocalizedDescriptionKey: "CVPixelBufferPoolCreate failed (status=\(status))"]
            )
        }
        return pool
    }

    /// Construct a single `CMSampleBuffer` for the frame at `frameIndex`. The
    /// buffer carries a deterministic single-color BGRA image and a PTS of
    /// `frameIndex / fps` in the standard 600-timescale clock used by SCK so
    /// the writer's `startSession(atSourceTime:)` semantics are exercised the
    /// same way they are in production.
    ///
    /// `static` so tests (`MP4WriterTests`) can call the same helper without
    /// instantiating the full source.
    static func makeSampleBuffer(
        fromPool pool: CVPixelBufferPool,
        frameIndex: Int64,
        fps: Int
    ) -> CMSampleBuffer? {
        var pixelBuffer: CVPixelBuffer?
        let status = CVPixelBufferPoolCreatePixelBuffer(
            kCFAllocatorDefault,
            pool,
            &pixelBuffer
        )
        guard status == kCVReturnSuccess, let buffer = pixelBuffer else {
            return nil
        }
        fillSingleColor(buffer, frameIndex: frameIndex)
        return makeSampleBuffer(pixelBuffer: buffer, frameIndex: frameIndex, fps: fps)
    }

    /// Wrap a pre-filled `CVPixelBuffer` in a `CMSampleBuffer` with a PTS
    /// derived from `frameIndex / fps`. Returns nil if any CoreMedia call
    /// fails. Exposed `static` so tests can reuse the timing helper against
    /// pixel buffers they allocated directly.
    static func makeSampleBuffer(
        pixelBuffer: CVPixelBuffer,
        frameIndex: Int64,
        fps: Int
    ) -> CMSampleBuffer? {
        var formatDesc: CMVideoFormatDescription?
        let descStatus = CMVideoFormatDescriptionCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pixelBuffer,
            formatDescriptionOut: &formatDesc
        )
        guard descStatus == noErr, let formatDesc = formatDesc else {
            return nil
        }

        // SCK PTS values are on the host clock; for synthetic mode we pick a
        // simple 600-timescale clock starting at 0. The timescale doesn't
        // matter for the writer's correctness — only monotonic increase does
        // — but 600 is the conventional value MediaPipeline-adjacent code
        // uses (divides evenly by 24/25/30/60). The duration is `1/fps`.
        let timescale: CMTimeScale = 600
        let pts = CMTime(value: CMTimeValue(frameIndex * Int64(timescale) / Int64(max(1, fps))), timescale: timescale)
        let duration = CMTime(value: CMTimeValue(timescale / CMTimeScale(max(1, fps))), timescale: timescale)
        var timing = CMSampleTimingInfo(
            duration: duration,
            presentationTimeStamp: pts,
            decodeTimeStamp: .invalid
        )

        var sampleBuffer: CMSampleBuffer?
        let sbStatus = CMSampleBufferCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pixelBuffer,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: formatDesc,
            sampleTiming: &timing,
            sampleBufferOut: &sampleBuffer
        )
        guard sbStatus == noErr, let sampleBuffer = sampleBuffer else {
            return nil
        }
        return sampleBuffer
    }

    /// Fill `buffer` with a deterministic single color in BGRA pixel format.
    /// The color rotates slowly with `frameIndex` so a human viewing the
    /// resulting mp4 sees motion, but the values are deterministic so the
    /// test suite can reason about byte-equivalence if it ever needs to.
    ///
    /// Internal so `MP4WriterTests` can call the same helper.
    static func fillSingleColor(_ buffer: CVPixelBuffer, frameIndex: Int64) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }

        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return }
        let bytesPerRow = CVPixelBufferGetBytesPerRow(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let width = CVPixelBufferGetWidth(buffer)

        // Hue rotates at ~one full cycle every 6 s (180 frames @ 30 fps).
        // We pre-compute the four BGRA channel bytes for the entire frame so
        // each row write is a tight memset-equivalent.
        let hue = (Double(frameIndex % 180) / 180.0) * 6.0
        let (r, g, b) = hueToRGB(hue: hue)
        let pixel: UInt32 = (UInt32(0xFF) << 24)
            | (UInt32(r) << 16) // R sits in byte 2 of 32BGRA on a little-endian platform
            | (UInt32(g) << 8)  // G in byte 1
            | UInt32(b)         // B in byte 0
        // Note: kCVPixelFormatType_32BGRA on Apple silicon is little-endian
        // so in-memory layout is B,G,R,A — the bit-shifts above match a
        // single UInt32 written natively.

        // Fill row-by-row to respect bytesPerRow padding (Core Video pads
        // rows for hardware alignment; writing one big memset against
        // base..base + height*width*4 would corrupt that padding).
        for row in 0..<height {
            let rowBase = base.advanced(by: row * bytesPerRow).assumingMemoryBound(to: UInt32.self)
            for col in 0..<width {
                rowBase[col] = pixel
            }
        }
    }

    /// Naive HSV→RGB conversion for the rotating fill color. `hue` is in
    /// `[0, 6)` (six sectors of the color wheel); saturation and value are
    /// fixed at 1.0. The output channels are 8-bit ints in `[0, 255]`.
    private static func hueToRGB(hue: Double) -> (UInt8, UInt8, UInt8) {
        let i = Int(hue) % 6
        let f = hue - Double(Int(hue))
        let q = UInt8((1.0 - f) * 255.0)
        let t = UInt8(f * 255.0)
        switch i {
        case 0: return (255, t, 0)
        case 1: return (q, 255, 0)
        case 2: return (0, 255, t)
        case 3: return (0, q, 255)
        case 4: return (t, 0, 255)
        default: return (255, 0, q)
        }
    }
}

extension SyntheticVideoSource: VideoSource {}
