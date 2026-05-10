import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import CoreAudio

/// Errors thrown by `AudioCapture` setup.
enum AudioCaptureError: Error, CustomStringConvertible {
    case permissionDenied
    case noDisplayAvailable
    case converterCreationFailed(from: AVAudioFormat, to: AVAudioFormat)
    case bufferFormatUnavailable

    var description: String {
        switch self {
        case .permissionDenied:
            return "screen recording or microphone permission denied"
        case .noDisplayAvailable:
            return "no display available for SCStream"
        case .converterCreationFailed(let from, let to):
            return "failed to construct AVAudioConverter from \(from) to \(to)"
        case .bufferFormatUnavailable:
            return "could not derive AVAudioFormat from CMSampleBuffer"
        }
    }
}

/// A tiny thread-safe FIFO of `Int16` mono samples.
///
/// Both the SCStream sample-handler queue and the `AVAudioEngine` tap
/// closure (which runs off the audio render thread per Apple's contract
/// for `installTap`) hand off into one of these. The mixer drain pump
/// pulls from both and writes the sum to the WAV.
///
/// `NSLock` is fine here: neither producer nor consumer runs on the
/// real-time audio render thread, so we're not violating the no-lock rule.
private final class LockedQueue {
    private var samples: [Int16] = []
    private let lock = NSLock()

    /// Append a chunk of samples to the tail.
    func append(_ chunk: UnsafeBufferPointer<Int16>) {
        lock.lock()
        samples.append(contentsOf: chunk)
        lock.unlock()
    }

    /// Drain up to `count` samples from the head. If the queue has fewer,
    /// returns all of them. Returns an empty array when nothing is buffered.
    func drain(upTo count: Int) -> [Int16] {
        lock.lock()
        defer { lock.unlock() }
        if samples.isEmpty || count <= 0 {
            return []
        }
        let n = min(count, samples.count)
        let head = Array(samples.prefix(n))
        samples.removeFirst(n)
        return head
    }

    /// Drain everything currently buffered.
    func drainAll() -> [Int16] {
        lock.lock()
        defer { lock.unlock() }
        let all = samples
        samples.removeAll(keepingCapacity: false)
        return all
    }

    /// Number of samples currently buffered.
    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return samples.count
    }
}

/// Owns the SCStream system-audio capture, the AVAudioEngine mic capture,
/// the per-source converters, the software mixer, and the WAV writer.
///
/// ## Mixer design (Slice 4)
///
/// We use a software ring-buffer mixer rather than an `AVAudioMixerNode`.
/// Rationale: the SCStream and engine paths are already independent producers
/// that each emit `AVAudioPCMBuffer`s via their own callbacks. Sending those
/// through an `AVAudioMixerNode` would mean either an `AVAudioSourceNode`
/// (whose render block runs on the real-time audio thread and must drain a
/// lock-free queue) or a player node (which buffers whole files, not realtime
/// streams). Both add complexity for no gain. Instead each producer converts
/// to int16 mono 16 kHz, appends to a `LockedQueue`, and a serial timer pump
/// pulls equal-length frames from both queues every ~10 ms, sums with
/// clamped addition, and writes the result to the WAV.
final class AudioCapture: NSObject {

    private let outputURL: URL
    private let emit: (Event) -> Void
    private let wavWriter: WAVWriter

    /// Background queue that receives `CMSampleBuffer`s from SCStream.
    private let sampleQueue = DispatchQueue(
        label: "record.audiocapture.samples",
        qos: .userInitiated
    )

    /// Serial queue that drives the mixer pump timer. The pump pulls from both
    /// ring buffers, sums, and writes to the WAV.
    private let mixerQueue = DispatchQueue(
        label: "record.audiocapture.mixer",
        qos: .userInitiated
    )

    // --- System audio (SCStream) ---
    private var stream: SCStream?
    private var systemConverter: AVAudioConverter?
    private var systemConverterInputFormat: AVAudioFormat?
    private let systemQueue = LockedQueue()

    // --- Microphone (AVAudioEngine) ---
    private let engine = AVAudioEngine()
    private var micConverter: AVAudioConverter?
    private var micConverterInputFormat: AVAudioFormat?
    private let micQueue = LockedQueue()
    private var micTapInstalled = false

    // --- Mixer pump ---
    private var mixerTimer: DispatchSourceTimer?

    private var startedAt: Date?

    // --- Source-loss tracking (Slice 5) ---
    //
    // A single lock guards both flags so that two concurrent loss events for
    // the same source (e.g. the SCStream delegate firing twice, or an
    // engine-config notification arriving alongside a manual teardown) can't
    // both pass the idempotency check. Flipped at most once per source per
    // capture, so a plain NSLock is plenty.
    private let lossLock = NSLock()
    private var micLost = false
    private var systemAudioLost = false

    /// Token for the `AVAudioEngineConfigurationChange` observer. Retained
    /// here so `stop()` can remove it; the block-based NotificationCenter API
    /// would leak otherwise.
    private var engineConfigObserver: NSObjectProtocol?

    /// Drain interval. Every 10 ms gives ~160 mono samples at 16 kHz —
    /// plenty small to keep WAV latency negligible without burning CPU.
    private let drainIntervalMs: Int = 10

    init(outputURL: URL, emit: @escaping (Event) -> Void) throws {
        self.outputURL = outputURL
        self.emit = emit
        self.wavWriter = try WAVWriter(url: outputURL)
        super.init()
    }

    // MARK: - Lifecycle

    /// Phase 1 of startup: run the Screen Recording and Microphone permission
    /// preflights. Throws `permissionDenied` if either is denied (after the
    /// underlying check has already emitted the corresponding
    /// `permission_denied` event), before any audio resources are allocated.
    ///
    /// `main.swift` calls this *before* emitting `started`, because the
    /// permission preflights may emit `permission_required` events and the
    /// protocol requires those to come before `started`.
    func checkPermissions() async throws {
        let screenGranted = await Permissions.checkScreenRecording(emit: emit)
        guard screenGranted else {
            throw AudioCaptureError.permissionDenied
        }
        let micGranted = await Permissions.checkMicrophone(emit: emit)
        guard micGranted else {
            throw AudioCaptureError.permissionDenied
        }
    }

    /// Phase 2 of startup: bring up the mic engine and the SCStream and
    /// kick off the mixer pump. Emits `source_attached` for each source as
    /// soon as it is actually producing data (mic after `engine.start()`
    /// returns, system_audio after `stream.startCapture()` returns).
    ///
    /// `main.swift` calls this *after* emitting `started`, because each
    /// source self-emits its `source_attached` event from here, and the
    /// protocol requires those to come after `started`.
    func startSources() async throws {
        // Mic first: cheaper to bring up and gives the user audible feedback
        // sooner if they're testing the pipeline.
        try startMic()
        try await startSystemAudio()

        startedAt = Date()
        startMixerPump()
    }

    /// Stop the SCStream and the mic engine, drain any leftover frames into
    /// the WAV, finalize the WAV, return elapsed seconds since `start()`
    /// returned.
    func stop() async -> Double {
        // Drop the engine-config observer before tearing anything down: once
        // we stop the engine ourselves, AVAudioEngine may post a final
        // notification we don't want to misinterpret as a mid-capture loss.
        if let token = engineConfigObserver {
            NotificationCenter.default.removeObserver(token)
            engineConfigObserver = nil
        }

        // Tear down sources first so no new samples land in the queues while
        // we're draining.
        if let stream = stream {
            try? await stream.stopCapture()
        }
        stream = nil

        if micTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }

        stopMixerPump()
        // One final mix pass to flush whatever samples are still buffered.
        drainAndMix(finalFlush: true)

        wavWriter.close()
        guard let startedAt = startedAt else {
            return 0
        }
        return Date().timeIntervalSince(startedAt)
    }

    // MARK: - System-audio (SCStream) setup

    private func startSystemAudio() async throws {
        // Pick any display — we only use it to anchor an `SCContentFilter`,
        // we don't render the video frames anywhere.
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: true
        )
        guard let display = content.displays.first else {
            throw AudioCaptureError.noDisplayAvailable
        }

        let filter = SCContentFilter(display: display, excludingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // The video output is required by SCStreamConfiguration even for
        // audio-only capture. Use the smallest values the API accepts and a
        // very low frame rate to minimize CPU.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: sampleQueue)
        self.stream = stream

        try await stream.startCapture()
        emit(.sourceAttached(source: .systemAudio))
    }

    // MARK: - Mic (AVAudioEngine) setup

    private func startMic() throws {
        // On macOS the engine's `inputNode` automatically follows the system
        // default input device — no per-device selection is needed.
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)

        // Passing `nil` as the tap format captures the bus's natural format
        // (typically float32 / native sample rate / native channel count),
        // which is the safest choice across input devices.
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            self?.handleMicBuffer(buffer)
        }
        micTapInstalled = true

        // Pre-build the mic converter so the first tap callback doesn't have
        // to do allocation under the audio thread's timing pressure.
        if let converter = AVAudioConverter(from: inputFormat, to: wavWriter.processingFormat) {
            micConverter = converter
            micConverterInputFormat = inputFormat
        } // else: built lazily in handleMicBuffer once we see a real format.

        engine.prepare()
        try engine.start()

        // Watch for hardware route changes that AVAudioEngine can't recover
        // from on its own (AirPods disconnect, USB mic unplug). The spec
        // requires capture to continue with system audio only when this
        // happens, so we tear down just the mic side and let the mixer pump
        // keep going — the natural zero-padding in drainAndMix supplies
        // silence for the missing source.
        engineConfigObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            self?.handleMicLost(reason: "audio engine configuration changed")
        }

        emit(.sourceAttached(source: .mic))
    }

    // MARK: - Source-loss handlers (Slice 5)

    /// Mark the mic as lost, tear down its inputs, and emit one `source_lost`.
    /// Idempotent: only the first call for a given capture does anything.
    private func handleMicLost(reason: String) {
        lossLock.lock()
        if micLost {
            lossLock.unlock()
            return
        }
        micLost = true
        lossLock.unlock()

        let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0

        // Stop producing mic samples. Once the tap is gone, micQueue will
        // simply stop growing and the mixer will zero-fill that side.
        if micTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }

        emit(.sourceLost(
            source: .mic,
            atOffsetSeconds: offset,
            reason: reason
        ))
    }

    /// Mark system audio as lost, drop the stream, and emit one `source_lost`.
    /// Idempotent: only the first call for a given capture does anything.
    fileprivate func handleSystemAudioLost(reason: String) {
        lossLock.lock()
        if systemAudioLost {
            lossLock.unlock()
            return
        }
        systemAudioLost = true
        lossLock.unlock()

        let offset = startedAt.map { Date().timeIntervalSince($0) } ?? 0

        // Best-effort stop: SCStream may already be dead when the delegate
        // reports the error, so we don't await or surface a secondary failure.
        if let stream = stream {
            Task { try? await stream.stopCapture() }
        }
        self.stream = nil
        // Drop the converter so we don't hold a stale converter targeting a
        // format that's no longer flowing.
        systemConverter = nil
        systemConverterInputFormat = nil

        emit(.sourceLost(
            source: .systemAudio,
            atOffsetSeconds: offset,
            reason: reason
        ))
    }

    // MARK: - System-audio sample handler

    fileprivate func handleSystemBuffer(_ sampleBuffer: CMSampleBuffer) {
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        guard let inputBuffer = makePCMBuffer(from: sampleBuffer) else { return }
        guard let outputBuffer = convert(
            inputBuffer,
            converter: &systemConverter,
            converterInputFormat: &systemConverterInputFormat
        ) else { return }
        enqueue(buffer: outputBuffer, into: systemQueue)
    }

    // MARK: - Mic tap handler

    private func handleMicBuffer(_ inputBuffer: AVAudioPCMBuffer) {
        guard let outputBuffer = convert(
            inputBuffer,
            converter: &micConverter,
            converterInputFormat: &micConverterInputFormat
        ) else { return }
        enqueue(buffer: outputBuffer, into: micQueue)
    }

    // MARK: - Per-source converter

    /// Convert one `AVAudioPCMBuffer` (whatever native format) into the WAV
    /// writer's int16 / mono / 16 kHz / interleaved format. Reuses the
    /// supplied converter when its input format hasn't changed.
    private func convert(
        _ inputBuffer: AVAudioPCMBuffer,
        converter: inout AVAudioConverter?,
        converterInputFormat: inout AVAudioFormat?
    ) -> AVAudioPCMBuffer? {
        let outputFormat = wavWriter.processingFormat

        if converter == nil || converterInputFormat != inputBuffer.format {
            guard let built = AVAudioConverter(from: inputBuffer.format, to: outputFormat) else {
                emit(.error(message: AudioCaptureError.converterCreationFailed(
                    from: inputBuffer.format,
                    to: outputFormat
                ).description))
                return nil
            }
            converter = built
            converterInputFormat = inputBuffer.format
        }

        guard let converter = converter else { return nil }

        let ratio = outputFormat.sampleRate / inputBuffer.format.sampleRate
        let outputCapacity = AVAudioFrameCount(Double(inputBuffer.frameLength) * ratio) + 32
        guard let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: outputFormat,
            frameCapacity: outputCapacity
        ) else {
            return nil
        }

        var fed = false
        var convertError: NSError?
        let status = converter.convert(to: outputBuffer, error: &convertError) { _, statusOut in
            if fed {
                statusOut.pointee = .noDataNow
                return nil
            }
            fed = true
            statusOut.pointee = .haveData
            return inputBuffer
        }

        switch status {
        case .haveData, .inputRanDry, .endOfStream:
            return outputBuffer.frameLength > 0 ? outputBuffer : nil
        case .error:
            let msg = convertError?.localizedDescription ?? "unknown converter error"
            emit(.error(message: "audio converter failed: \(msg)"))
            return nil
        @unknown default:
            return nil
        }
    }

    /// Append the int16 samples in `buffer` to the supplied ring queue.
    private func enqueue(buffer: AVAudioPCMBuffer, into queue: LockedQueue) {
        guard let int16Data = buffer.int16ChannelData else { return }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return }
        // Mono interleaved → one channel pointer, frameCount samples.
        let ptr = UnsafeBufferPointer(start: int16Data[0], count: frameCount)
        queue.append(ptr)
    }

    // MARK: - Mixer pump

    private func startMixerPump() {
        let timer = DispatchSource.makeTimerSource(queue: mixerQueue)
        timer.schedule(
            deadline: .now() + .milliseconds(drainIntervalMs),
            repeating: .milliseconds(drainIntervalMs)
        )
        timer.setEventHandler { [weak self] in
            self?.drainAndMix(finalFlush: false)
        }
        mixerTimer = timer
        timer.resume()
    }

    private func stopMixerPump() {
        mixerTimer?.cancel()
        mixerTimer = nil
    }

    /// Pull whatever's available from each ring buffer, pad the shorter side
    /// with zeros, sum with clamped addition, write one mixed buffer to the
    /// WAV. On `finalFlush == true` we drain everything remaining.
    private func drainAndMix(finalFlush: Bool) {
        let sysAvailable = systemQueue.count
        let micAvailable = micQueue.count

        // Take the longer of the two so neither source is left behind. The
        // shorter side gets zero-padded to match — that's the silence-fill
        // case for a source that's briefly behind or hasn't started yet.
        let target = max(sysAvailable, micAvailable)
        if target == 0 {
            return
        }

        // On the periodic pump we cap at the larger side's current depth.
        // On the final flush we explicitly drain everything.
        let sysSamples: [Int16]
        let micSamples: [Int16]
        if finalFlush {
            sysSamples = systemQueue.drainAll()
            micSamples = micQueue.drainAll()
        } else {
            sysSamples = systemQueue.drain(upTo: target)
            micSamples = micQueue.drain(upTo: target)
        }

        let outCount = max(sysSamples.count, micSamples.count)
        if outCount == 0 { return }

        var mixed = [Int16](repeating: 0, count: outCount)
        for i in 0..<outCount {
            let s: Int32 = i < sysSamples.count ? Int32(sysSamples[i]) : 0
            let m: Int32 = i < micSamples.count ? Int32(micSamples[i]) : 0
            mixed[i] = Int16(clamping: s + m)
        }

        guard let outBuffer = AVAudioPCMBuffer(
            pcmFormat: wavWriter.processingFormat,
            frameCapacity: AVAudioFrameCount(outCount)
        ) else { return }
        outBuffer.frameLength = AVAudioFrameCount(outCount)
        if let dst = outBuffer.int16ChannelData?[0] {
            mixed.withUnsafeBufferPointer { src in
                if let base = src.baseAddress {
                    dst.update(from: base, count: outCount)
                }
            }
        }

        do {
            try wavWriter.write(outBuffer)
        } catch {
            emit(.error(message: "wav write failed: \(error)"))
        }
    }

    // MARK: - CMSampleBuffer → AVAudioPCMBuffer

    /// Wrap a `CMSampleBuffer` from SCStream as an `AVAudioPCMBuffer`. Returns
    /// nil if the sample buffer doesn't carry an audio format description we
    /// can interpret.
    private func makePCMBuffer(from sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPointer = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc)
        else {
            return nil
        }
        var asbd = asbdPointer.pointee
        guard let format = AVAudioFormat(streamDescription: &asbd) else {
            return nil
        }

        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0,
              let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount)
        else {
            return nil
        }
        pcmBuffer.frameLength = frameCount

        var blockBufferOut: CMBlockBuffer?
        var audioBufferList = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBufferOut
        )
        guard status == noErr else { return nil }

        // Copy raw bytes from the source ABL into the PCM buffer's ABL.
        // For interleaved formats there is one buffer; for non-interleaved
        // there is one per channel. Use UnsafeMutableAudioBufferListPointer
        // to walk both lists.
        let sourceList = UnsafeMutableAudioBufferListPointer(&audioBufferList)
        let destList = UnsafeMutableAudioBufferListPointer(pcmBuffer.mutableAudioBufferList)
        let pairCount = min(sourceList.count, destList.count)
        for i in 0..<pairCount {
            let src = sourceList[i]
            var dst = destList[i]
            let bytes = min(Int(src.mDataByteSize), Int(dst.mDataByteSize))
            if let srcData = src.mData, let dstData = dst.mData, bytes > 0 {
                memcpy(dstData, srcData, bytes)
            }
            dst.mDataByteSize = UInt32(bytes)
            destList[i] = dst
        }

        // Retain the block buffer for the lifetime of this scope; ABL is
        // released when blockBufferOut goes out of scope.
        _ = blockBufferOut
        return pcmBuffer
    }
}

// MARK: - SCStreamOutput

extension AudioCapture: SCStreamOutput {
    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio else { return }
        handleSystemBuffer(sampleBuffer)
    }
}

// MARK: - SCStreamDelegate

extension AudioCapture: SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // Delegate fires on an unspecified thread; handleSystemAudioLost
        // guards the lost-flag check-and-set, so concurrent invocations are
        // safe and only the first one emits.
        handleSystemAudioLost(reason: error.localizedDescription)
    }
}
