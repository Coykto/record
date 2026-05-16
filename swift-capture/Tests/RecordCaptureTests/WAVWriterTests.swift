import XCTest
import AVFoundation
@testable import RecordCapture

/// Verifies the multi-writer story we rely on for spec 005: two `WAVWriter`s
/// can write independent files in the same process, and `close()` is
/// idempotent (safe to call from a source-lost path and again from stop).
final class WAVWriterTests: XCTestCase {

    func testTwoIndependentWritersAndDoubleClose() throws {
        let tmp = FileManager.default.temporaryDirectory
        let uuid = UUID().uuidString
        let urlA = tmp.appendingPathComponent("\(uuid)-A.wav")
        let urlB = tmp.appendingPathComponent("\(uuid)-B.wav")

        defer {
            try? FileManager.default.removeItem(at: urlA)
            try? FileManager.default.removeItem(at: urlB)
        }

        let writerA = try WAVWriter(url: urlA)
        let writerB = try WAVWriter(url: urlB)

        // Build a ~100 ms int16 / mono / 16 kHz / interleaved buffer with a
        // simple non-zero pattern so we know the writer actually persisted
        // sample data (not just a header).
        let frameCount: AVAudioFrameCount = 1600
        let bufferA = try makeInt16Buffer(format: writerA.processingFormat, frameCount: frameCount)
        let bufferB = try makeInt16Buffer(format: writerB.processingFormat, frameCount: frameCount)

        try writerA.write(bufferA)
        try writerB.write(bufferB)

        // Idempotency: a second close() must be a no-op (no throw, no crash).
        writerA.close()
        writerA.close()
        writerB.close()
        writerB.close()

        // Both files exist and are non-empty.
        let attrsA = try FileManager.default.attributesOfItem(atPath: urlA.path)
        let attrsB = try FileManager.default.attributesOfItem(atPath: urlB.path)
        let sizeA = (attrsA[.size] as? NSNumber)?.intValue ?? 0
        let sizeB = (attrsB[.size] as? NSNumber)?.intValue ?? 0
        XCTAssertGreaterThan(sizeA, 0, "writer A produced an empty file")
        XCTAssertGreaterThan(sizeB, 0, "writer B produced an empty file")

        // Validate the WAV header on both files.
        try assertValidWAVHeader(at: urlA)
        try assertValidWAVHeader(at: urlB)
    }

    // MARK: - Helpers

    private func makeInt16Buffer(
        format: AVAudioFormat,
        frameCount: AVAudioFrameCount
    ) throws -> AVAudioPCMBuffer {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount) else {
            XCTFail("could not allocate AVAudioPCMBuffer")
            throw NSError(domain: "WAVWriterTests", code: 1)
        }
        buffer.frameLength = frameCount
        guard let channelData = buffer.int16ChannelData else {
            XCTFail("buffer has no int16 channel data — format is \(format)")
            throw NSError(domain: "WAVWriterTests", code: 2)
        }
        // Interleaved mono → channel 0 holds the whole sample stream.
        let samples = channelData[0]
        for i in 0..<Int(frameCount) {
            // Pattern: 1, -1, 2, -2, 3, -3, … capped at ±127 to stay tiny but
            // unambiguously non-zero in the resulting file.
            let magnitude = Int16((i / 2 % 127) + 1)
            samples[i] = (i % 2 == 0) ? magnitude : -magnitude
        }
        return buffer
    }

    private func assertValidWAVHeader(at url: URL) throws {
        let data = try Data(contentsOf: url)
        XCTAssertGreaterThanOrEqual(data.count, 44, "WAV at \(url.path) is shorter than the canonical 44-byte header")

        // "RIFF" at offset 0
        XCTAssertEqual(String(data: data[0..<4], encoding: .ascii), "RIFF", "missing RIFF marker at \(url.path)")
        // "WAVE" at offset 8
        XCTAssertEqual(String(data: data[8..<12], encoding: .ascii), "WAVE", "missing WAVE marker at \(url.path)")
        // "fmt " at offset 12
        XCTAssertEqual(String(data: data[12..<16], encoding: .ascii), "fmt ", "missing fmt  chunk at \(url.path)")
        // audio_format (PCM = 1) at offset 20 (UInt16 LE)
        let audioFormat = readUInt16LE(data, at: 20)
        XCTAssertEqual(audioFormat, 1, "audio_format must be PCM (1) at \(url.path)")
        // channels (1) at offset 22 (UInt16 LE)
        let channels = readUInt16LE(data, at: 22)
        XCTAssertEqual(channels, 1, "channels must be 1 (mono) at \(url.path)")
        // sample_rate (16000) at offset 24 (UInt32 LE)
        let sampleRate = readUInt32LE(data, at: 24)
        XCTAssertEqual(sampleRate, 16000, "sample_rate must be 16000 at \(url.path)")
        // bits_per_sample (16) at offset 34 (UInt16 LE)
        let bitsPerSample = readUInt16LE(data, at: 34)
        XCTAssertEqual(bitsPerSample, 16, "bits_per_sample must be 16 at \(url.path)")
        // "data" at offset 36
        XCTAssertEqual(String(data: data[36..<40], encoding: .ascii), "data", "missing data chunk at \(url.path)")
        // data chunk size > 0 at offset 40 (UInt32 LE)
        let dataSize = readUInt32LE(data, at: 40)
        XCTAssertGreaterThan(dataSize, 0, "data chunk size must be > 0 at \(url.path)")
    }

    private func readUInt16LE(_ data: Data, at offset: Int) -> UInt16 {
        let b0 = UInt16(data[offset])
        let b1 = UInt16(data[offset + 1])
        return b0 | (b1 << 8)
    }

    private func readUInt32LE(_ data: Data, at offset: Int) -> UInt32 {
        let b0 = UInt32(data[offset])
        let b1 = UInt32(data[offset + 1])
        let b2 = UInt32(data[offset + 2])
        let b3 = UInt32(data[offset + 3])
        return b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
    }
}
