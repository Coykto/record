import XCTest
@testable import RecordCapture

/// Slice 4 silence detection — exercises `AudioCapture` end-to-end in
/// synthetic mode and asserts the per-source `audio_file` event status.
///
/// Synthetic mode (`testSilentSources = true`) bypasses SCStream and
/// AVAudioEngine entirely, so this test doesn't require TCC permissions
/// or a display / mic device. With `silentMicSource = true` the synthetic
/// feeder writes all-zero Int16 samples on the mic side while the system
/// side keeps its 880 Hz tone — exactly the scenario the integration test
/// uses, but compressed into a short in-process run.
final class AudioCaptureSilenceTests: XCTestCase {

    func testSilentMicProducesSilentThroughoutStatus() async throws {
        let tmp = FileManager.default.temporaryDirectory
        let basenameURL = tmp.appendingPathComponent("audio-silence-\(UUID().uuidString)")
        let micURL = URL(fileURLWithPath: basenameURL.path + "-mic.wav")
        let systemURL = URL(fileURLWithPath: basenameURL.path + "-system.wav")
        defer {
            try? FileManager.default.removeItem(at: micURL)
            try? FileManager.default.removeItem(at: systemURL)
        }

        // Capture every event the AudioCapture emits. NSLock keeps the
        // append safe against the synthetic feeder's dispatch queue.
        let eventsLock = NSLock()
        var events: [Event] = []
        let emit: (Event) -> Void = { event in
            eventsLock.lock()
            events.append(event)
            eventsLock.unlock()
        }

        let capture = try AudioCapture(
            basename: basenameURL,
            emit: emit,
            testSilentSources: true,
            injectMicLossAfterSeconds: nil,
            silentMicSource: true
        )

        try await capture.checkPermissions()
        try await capture.startSources()

        // Let the synthetic feeder + drain pump tick a few times so each
        // side gets to write a non-trivial number of samples. 200 ms is
        // plenty given the 10 ms drain cadence.
        try await Task.sleep(nanoseconds: 200_000_000)

        _ = await capture.stop()

        eventsLock.lock()
        let snapshot = events
        eventsLock.unlock()

        // Locate the per-source audio_file events.
        var micEvent: (status: String, truncated: Double?)? = nil
        var sysEvent: (status: String, truncated: Double?)? = nil
        for event in snapshot {
            if case let .audioFile(_, source, _, status, truncated) = event {
                switch source {
                case .mic:
                    micEvent = (status, truncated)
                case .systemAudio:
                    sysEvent = (status, truncated)
                }
            }
        }

        guard let mic = micEvent else {
            XCTFail("no audio_file event for mic emitted; got events: \(snapshot)")
            return
        }
        guard let sys = sysEvent else {
            XCTFail("no audio_file event for system emitted; got events: \(snapshot)")
            return
        }

        XCTAssertEqual(mic.status, "silent_throughout",
            "mic source fed with all-zero samples must emit status=silent_throughout, got \(mic.status)")
        XCTAssertNil(mic.truncated,
            "silent_throughout mic must not carry a truncation offset")

        XCTAssertEqual(sys.status, "captured_normally",
            "system source fed with the 880 Hz tone must emit status=captured_normally, got \(sys.status)")
        XCTAssertNil(sys.truncated,
            "captured_normally system must not carry a truncation offset")
    }
}
