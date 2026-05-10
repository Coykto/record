import Foundation
import AVFoundation

/// Errors thrown by `WAVWriter`.
enum WAVWriterError: Error, CustomStringConvertible {
    case incompatibleBufferFormat(expected: AVAudioFormat, got: AVAudioFormat)
    case unableToCreateProcessingFormat

    var description: String {
        switch self {
        case .incompatibleBufferFormat(let expected, let got):
            return "wav writer: buffer format mismatch — expected \(expected), got \(got)"
        case .unableToCreateProcessingFormat:
            return "wav writer: failed to construct int16/mono/16k processing format"
        }
    }
}

/// Writes 16-bit signed PCM, mono, 16 kHz audio to a `.wav` on disk.
///
/// The on-disk file format is fixed at int16 LE / mono / 16 kHz regardless of
/// the processing format — `AVAudioFile`'s `settings` initializer takes the
/// file format, and the processing format is what callers hand us as
/// `AVAudioPCMBuffer`s.
///
/// Thread safety: writes from the SCStream audio callback may arrive on an
/// arbitrary background queue. A serial `DispatchQueue` serializes
/// `write(_:)` and `close()`.
final class WAVWriter {

    /// The processing format callers must match in their `AVAudioPCMBuffer`s.
    /// int16 / mono / 16 kHz / interleaved.
    let processingFormat: AVAudioFormat

    private let queue = DispatchQueue(label: "record.wavwriter", qos: .userInitiated)
    private var file: AVAudioFile?
    private var closed = false

    init(url: URL) throws {
        // File-side settings: 16-bit signed LE PCM, mono, 16 kHz.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]

        // Processing format: same shape as on-disk so `write(_:)` is a direct
        // byte-stream and the converter inside `AudioCapture` produces buffers
        // we can pass through unchanged.
        guard let processingFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 16000,
            channels: 1,
            interleaved: true
        ) else {
            throw WAVWriterError.unableToCreateProcessingFormat
        }
        self.processingFormat = processingFormat

        // `interleaved: true` for the processing format matches the file's
        // `AVLinearPCMIsNonInterleavedKey: false`. For mono, interleaved vs.
        // non-interleaved is functionally identical, but matching keeps the
        // converter's job trivial.
        self.file = try AVAudioFile(
            forWriting: url,
            settings: settings,
            commonFormat: .pcmFormatInt16,
            interleaved: true
        )
    }

    /// Append one buffer of PCM. The buffer's `format` must equal
    /// `processingFormat`.
    func write(_ buffer: AVAudioPCMBuffer) throws {
        try queue.sync {
            guard !closed, let file = file else { return }
            guard buffer.format.commonFormat == processingFormat.commonFormat,
                  buffer.format.sampleRate == processingFormat.sampleRate,
                  buffer.format.channelCount == processingFormat.channelCount,
                  buffer.format.isInterleaved == processingFormat.isInterleaved
            else {
                throw WAVWriterError.incompatibleBufferFormat(
                    expected: processingFormat,
                    got: buffer.format
                )
            }
            try file.write(from: buffer)
        }
    }

    /// Finalize the file. Idempotent — safe to call more than once.
    func close() {
        queue.sync {
            guard !closed else { return }
            // `AVAudioFile` finalizes its on-disk WAV header on deinit.
            file = nil
            closed = true
        }
    }

    deinit {
        close()
    }
}
