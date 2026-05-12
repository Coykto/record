import XCTest
import AVFoundation
import CoreMedia
import CoreVideo
@testable import RecordCapture

/// Drives `MP4Writer` end-to-end against deterministic synthetic
/// `CMSampleBuffer`s and re-opens the resulting `.mp4` with `AVAsset` to assert
/// it's playable, has the expected duration, and was muxed with the configured
/// dimensions.
///
/// Reuses `SyntheticVideoSource`'s static buffer helpers so the production
/// `--test-synthetic-video` runtime path and these tests share the same frame
/// factory — drift on either side would surface here.
final class MP4WriterTests: XCTestCase {
    /// Tracked temp URLs so `tearDown` can remove them. Each test that
    /// produces an mp4 appends to this list.
    private var producedURLs: [URL] = []

    override func tearDown() {
        for url in producedURLs {
            try? FileManager.default.removeItem(at: url)
        }
        producedURLs.removeAll()
        super.tearDown()
    }

    // MARK: - Helpers

    private func makeTempMP4URL() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("record-mp4writer-tests-\(UUID().uuidString).mp4")
        producedURLs.append(url)
        return url
    }

    /// Drive `MP4Writer` with `frameCount` synthetic frames at `fps` and
    /// finalize. Returns the output URL.
    @discardableResult
    private func writeSyntheticMP4(
        widthPx: Int,
        heightPx: Int,
        fps: Int,
        frameCount: Int
    ) async throws -> URL {
        let url = makeTempMP4URL()
        let writer = try MP4Writer(url: url, widthPx: widthPx, heightPx: heightPx, fps: fps)

        let pool = try SyntheticVideoSource.createPixelBufferPool(
            widthPx: widthPx,
            heightPx: heightPx
        )

        for i in 0..<frameCount {
            guard let sample = SyntheticVideoSource.makeSampleBuffer(
                fromPool: pool,
                frameIndex: Int64(i),
                fps: fps
            ) else {
                XCTFail("failed to construct synthetic CMSampleBuffer at index \(i)")
                continue
            }
            // The writer is in real-time mode (`expectsMediaDataInRealTime =
            // true`). When fed back-to-back without a wall-clock delay, the
            // encoder occasionally back-pressures and rejects a frame. A tiny
            // poll-and-retry keeps the test deterministic without sleeping
            // for any meaningful duration on a healthy machine.
            var attempts = 0
            while !writer.append(sample) {
                attempts += 1
                if attempts > 100 { break }
                try? await Task.sleep(nanoseconds: 1_000_000) // 1 ms
            }
        }

        _ = await writer.finalize()
        return url
    }

    // MARK: - Tests

    /// Feed 60 frames at 30 fps (≈2 s of video) at 640×360, finalize, then
    /// re-open with `AVAsset` and check the file is playable, has exactly
    /// one video track, the right duration (within ±100 ms), and the right
    /// pixel dimensions. This is the primary correctness test for the
    /// writer pipeline.
    func testWriterProducesPlayableMP4WithExpectedDimensionsAndDuration() async throws {
        let widthPx = 640
        let heightPx = 360
        let fps = 30
        let frameCount = 60
        let expectedDuration = Double(frameCount) / Double(fps)

        let url = try await writeSyntheticMP4(
            widthPx: widthPx,
            heightPx: heightPx,
            fps: fps,
            frameCount: frameCount
        )

        // Non-zero file size — the foremost footgun the writer guards
        // against is `startSession(atSourceTime: .zero)` producing an empty
        // file. If that regresses this assertion catches it first.
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        let fileSize = (attrs[.size] as? Int) ?? 0
        XCTAssertGreaterThan(fileSize, 0, "output mp4 must be non-zero size")

        // Re-open with AVAsset and inspect the muxed track. Using the
        // async accessors (Swift 6 / macOS 13+) so we don't get deprecation
        // warnings against `tracks(withMediaType:)` / `.duration`.
        let asset = AVURLAsset(url: url)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        XCTAssertEqual(tracks.count, 1, "mp4 must contain exactly one video track")

        let duration = try await asset.load(.duration)
        let durationSeconds = CMTimeGetSeconds(duration)
        XCTAssertEqual(
            durationSeconds,
            expectedDuration,
            accuracy: 0.1,
            "track duration must be within ±100 ms of \(expectedDuration) s"
        )

        guard let videoTrack = tracks.first else { return }
        let naturalSize = try await videoTrack.load(.naturalSize)
        // H.264 rounds odd dimensions to even, but 640 and 360 are both
        // even so the natural size should equal the configured size.
        XCTAssertEqual(Int(naturalSize.width), widthPx, "natural width must equal configured width")
        XCTAssertEqual(Int(naturalSize.height), heightPx, "natural height must equal configured height")

        // Nominal frame rate should land near 30 fps — we won't pin it to
        // exactly 30.0 because AVFoundation derives it from the average
        // sample duration and can quantize across the 600-timescale clock,
        // but anything significantly below 25 or above 35 indicates a
        // timing bug.
        let nominalFPS = try await videoTrack.load(.nominalFrameRate)
        XCTAssertGreaterThan(Double(nominalFPS), 25.0, "nominal fps too low: \(nominalFPS)")
        XCTAssertLessThan(Double(nominalFPS), 35.0, "nominal fps too high: \(nominalFPS)")
    }

    /// Finalizing a writer that never received a frame must not produce a
    /// half-finished file and must report a zero duration. Otherwise a
    /// race between video-startup failure and `handleStop` could leave a
    /// zero-byte mp4 referenced by `video_file`.
    func testFinalizeBeforeAnyFrameReturnsZeroDuration() async throws {
        let url = makeTempMP4URL()
        let writer = try MP4Writer(url: url, widthPx: 640, heightPx: 360, fps: 30)
        let duration = await writer.finalize()
        XCTAssertEqual(duration, 0, "writer with no frames must report zero duration")
    }

    /// `finalize()` is documented as idempotent. Double-finalize must return
    /// the same duration and must not throw. This guards the wire-order
    /// contract in `main.swift` where `VideoCapture.handleStreamError` calls
    /// `writer.finalizeSync` and then `handleStop` later calls `vc.stop() →
    /// writer.finalize()` — both must succeed.
    func testFinalizeIsIdempotent() async throws {
        let url = try await writeSyntheticMP4(
            widthPx: 640,
            heightPx: 360,
            fps: 30,
            frameCount: 30
        )
        // Re-open the writer for a second finalize: we can't — the writer
        // is consumed by the helper. Instead, drive a fresh writer through
        // the same lifecycle and call finalize twice.
        _ = url
        let url2 = makeTempMP4URL()
        let writer = try MP4Writer(url: url2, widthPx: 640, heightPx: 360, fps: 30)
        let pool = try SyntheticVideoSource.createPixelBufferPool(widthPx: 640, heightPx: 360)
        for i in 0..<10 {
            if let sample = SyntheticVideoSource.makeSampleBuffer(
                fromPool: pool,
                frameIndex: Int64(i),
                fps: 30
            ) {
                _ = writer.append(sample)
            }
        }
        let d1 = await writer.finalize()
        let d2 = await writer.finalize()
        XCTAssertEqual(d1, d2, "double-finalize must return the cached duration")
    }
}
