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
/// for `installTap`) hand off into one of these. The drain pump pulls
/// from each queue independently and writes the samples to that source's
/// own WAV (no mixing — spec 005).
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
/// the per-source converters, and the two independent per-source WAV writers.
///
/// ## Two-writer design (spec 005)
///
/// Each source is converted to int16 mono 16 kHz and appended to its own
/// `LockedQueue`. A serial timer pump drains each queue independently and
/// writes each side's samples to its own `WAVWriter` — no mixing, no
/// cross-talk, no zero-padding the "shorter side". A failure on one source
/// closes only its writer; the other continues until `stop()` runs.
final class AudioCapture: NSObject {

    /// Output basename (no extension). The two writers' file URLs are
    /// derived by appending `-mic.wav` and `-system.wav` to this path.
    private let basename: URL
    private let emit: (Event) -> Void
    /// Shared int16 / mono / 16 kHz interleaved processing format. Both writers
    /// share it; the per-source converters target it; the synthetic feeder
    /// builds buffers against it. Constructed once at init so we never re-derive
    /// it from one of the writers (which may be nilled on close).
    private let processingFormat: AVAudioFormat
    /// Derived output URLs, retained so `stop()` can emit `audio_file` events
    /// carrying the on-disk paths after the writers are closed and nilled.
    private let micURL: URL
    private let systemURL: URL
    private var micWriter: WAVWriter?
    private var systemWriter: WAVWriter?

    /// Background queue that receives `CMSampleBuffer`s from SCStream.
    private let sampleQueue = DispatchQueue(
        label: "record.audiocapture.samples",
        qos: .userInitiated
    )

    /// Serial queue that drives the drain pump timer. The pump pulls from
    /// each ring buffer independently and writes to its source's own WAV.
    private let mixerQueue = DispatchQueue(
        label: "record.audiocapture.drain",
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
    /// Offset-from-start (seconds) at which mic was declared lost. Drives the
    /// `truncated_at_offset` status path in `finalizeWriters`. Nil means the
    /// mic file was not truncated.
    private var micTruncatedAtOffsetSeconds: Double?
    private var systemTruncatedAtOffsetSeconds: Double?

    // --- Silent-source detection (Slice 4) ---
    //
    // Flipped to `true` the first time `writeSamples` observes any non-zero
    // Int16 sample for the corresponding source, and never flipped back. On
    // stop, `finalizeWriters` reads these to choose between
    // `"captured_normally"` and `"silent_throughout"`. Mutated only from the
    // mixer-pump queue (`drainAndWrite`) and the source-loss handlers
    // (`handleMicLost` / `handleSystemAudioLost`), both of which are
    // serialized w.r.t. each other by the lifecycle of the writer they
    // share — once the writer is closed, the loss handler no longer calls
    // `writeSamples` for that source. A plain stored property is therefore
    // sufficient; no lock needed.
    private var micHasNonzeroSamples = false
    private var systemHasNonzeroSamples = false

    /// Token for the `AVAudioEngineConfigurationChange` observer. Retained
    /// here so `stop()` can remove it; the block-based NotificationCenter API
    /// would leak otherwise.
    private var engineConfigObserver: NSObjectProtocol?

    /// Monotonically incremented on every `AVAudioEngineConfigurationChange`.
    /// Each scheduled fallback captures the generation it was queued under;
    /// if the counter has advanced by the time the fallback fires, a fresher
    /// route change is in flight and the older chain abandons itself.
    /// Mutated only from the main queue, so no lock is needed.
    private var micRestartGeneration: Int = 0

    /// True between the `AVAudioEngineConfigurationChange` notification and
    /// the moment we successfully bring the engine back up. Read/written
    /// only on main.
    private var pendingMicRestart: Bool = false

    /// CoreAudio system-object listener that fires when the *system default
    /// input device* changes (AirPods becoming default, USB mic unplugged,
    /// etc.). Used to know when to re-target the per-device stream-format
    /// listener. Stored so we can remove it in `stop()` / `handleMicLost`.
    private var defaultInputListenerBlock: AudioObjectPropertyListenerBlock?

    /// CoreAudio system-object listener that fires when the *system default
    /// output device* changes. Important even though we don't render output:
    /// AirPods becoming the default output can cause the OS to silently
    /// pause our mic input AU without firing any other notification we
    /// observe. We use this as one of several triggers to re-evaluate
    /// whether the input has stopped flowing.
    private var defaultOutputListenerBlock: AudioObjectPropertyListenerBlock?

    /// CoreAudio device listener that fires when the currently-default input
    /// device's stream format changes. For Bluetooth headsets this is the
    /// "SCO profile finished negotiating, mic is ready" signal — exactly
    /// when `engine.start()` will succeed after a route change.
    private var formatListenerBlock: AudioObjectPropertyListenerBlock?

    /// CoreAudio device listener for
    /// `kAudioDevicePropertyDeviceIsRunningSomewhere` on the current
    /// default input device. The OS toggles this whenever the device's I/O
    /// state changes — including when an output route change (AirPods
    /// connecting) causes the system to silently stop I/O on the input.
    /// This is the direct, event-based signal "the mic stopped".
    private var runningSomewhereListenerBlock: AudioObjectPropertyListenerBlock?

    /// AudioObjectID of the device currently being watched by the per-input
    /// listeners (`formatListenerBlock`, `runningSomewhereListenerBlock`).
    /// Tracked so we know which object to call
    /// `AudioObjectRemovePropertyListenerBlock` against when the default
    /// input changes (or capture stops).
    private var watchedInputDeviceID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)

    /// Background queue the CoreAudio property listeners dispatch on.
    private let coreAudioListenerQueue = DispatchQueue(
        label: "record.audiocapture.coreaudio",
        qos: .userInitiated
    )

    // --- Mic-flow watchdog ---
    //
    // The route-change recovery path above is purely event-driven. In
    // practice, some macOS scenarios — most notably AirPods connecting as
    // the new default *output* device only — cause AVAudioEngine to
    // silently stop calling the mic tap closure without ever firing
    // `AVAudioEngineConfigurationChange` and without any CoreAudio
    // default-input change (because the default input genuinely didn't
    // change). Without a watchdog the mic tap just stops, no event reaches
    // the orchestrator, the WAV writer stops getting samples, and at stop
    // time we mistakenly report `captured_normally` with full elapsed
    // duration even though the file is truncated.
    //
    // The watchdog periodically checks whether mic buffers have arrived
    // recently. If the gap exceeds `micFlowStallThresholdSeconds` we
    // assume the engine has silently wedged and trigger a full restart
    // (same recovery path used by `handleEngineConfigurationChange`).
    private let micFlowLock = NSLock()
    private var lastMicBufferAt: Date?
    private var micWatchdogTimer: DispatchSourceTimer?
    /// Restart the engine if no mic buffer has arrived in this long.
    /// 2 s is well above the typical inter-buffer interval (~25 ms at
    /// 1024-frame buffers) but short enough that the user hears the
    /// recovery rather than losing minutes of audio to a silent wedge.
    private let micFlowStallThresholdSeconds: Double = 2.0

    /// Drain interval. Every 10 ms gives ~160 mono samples at 16 kHz —
    /// plenty small to keep WAV latency negligible without burning CPU.
    private let drainIntervalMs: Int = 10

    // --- Test mode (Slice 7) ---
    //
    // When `testSilentSources == true` the real SCStream + AVAudioEngine paths
    // are entirely bypassed and a deterministic synthetic feeder appends
    // int16 samples directly to micQueue and systemQueue. No TCC permissions
    // are touched. The mixer pump then drains/mixes/writes the WAV unchanged.
    private let testSilentSources: Bool
    /// When non-nil and `testSilentSources == true`, the synthetic feeder
    /// schedules a one-shot `handleMicLost` after this many seconds so the
    /// integration test can exercise the mid-capture mic-truncation path
    /// deterministically. Ignored when sources are real.
    private let injectMicLossAfterSeconds: Double?
    /// When true and `testSilentSources == true`, the synthetic feeder writes
    /// all-zero Int16 samples on the mic side (system side unchanged). Drives
    /// the slice 4 integration test for `status="silent_throughout"`. Ignored
    /// when sources are real.
    private let silentMicSource: Bool
    private var syntheticQueue: DispatchQueue?
    private var syntheticTimer: DispatchSourceTimer?
    /// Frame index of the next synthetic sample to emit. Used to derive the
    /// 1 s silence + 1 s 440 Hz tone loop deterministically across ticks.
    private var syntheticFrameIndex: Int = 0

    init(
        basename: URL,
        emit: @escaping (Event) -> Void,
        testSilentSources: Bool = false,
        injectMicLossAfterSeconds: Double? = nil,
        silentMicSource: Bool = false
    ) throws {
        self.basename = basename
        self.emit = emit
        self.testSilentSources = testSilentSources
        self.injectMicLossAfterSeconds = injectMicLossAfterSeconds
        self.silentMicSource = silentMicSource
        // Append the per-source suffix to the basename's path. We can't use
        // `URL.appendingPathExtension` because the basename has no extension
        // *and* we want to add a literal `-mic`/`-system` infix before `.wav`.
        let micURL = URL(fileURLWithPath: basename.path + "-mic.wav")
        let systemURL = URL(fileURLWithPath: basename.path + "-system.wav")
        self.micURL = micURL
        self.systemURL = systemURL
        let micWriter = try WAVWriter(url: micURL)
        let systemWriter = try WAVWriter(url: systemURL)
        self.processingFormat = micWriter.processingFormat
        self.micWriter = micWriter
        self.systemWriter = systemWriter
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
        // Test mode: skip TCC entirely. No `permission_required` or
        // `permission_denied` events are emitted, and no SCK / AVCaptureDevice
        // APIs are touched — so the binary can run in CI without prompting.
        if testSilentSources {
            return
        }

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
        if testSilentSources {
            // Test mode: feed both ring buffers from a deterministic synthetic
            // stream. Emit source_attached events in the same order as the
            // real path (mic first, then system_audio) so the protocol order
            // on the wire is indistinguishable.
            emit(.sourceAttached(source: .mic))
            emit(.sourceAttached(source: .systemAudio))
            startedAt = Date()
            startSyntheticFeeder()
            startMixerPump()
            return
        }

        // Mic is best-effort. AVAudioEngine can fail transiently
        // (`kAudioUnitErr_FormatNotSupported` = -10868) right after the
        // previous capture's teardown, or while a Bluetooth route is still
        // transitioning. We surface the failure as an `error` event and
        // continue with system audio only rather than aborting the entire
        // session.
        do {
            try startMic()
        } catch {
            // Mark mic lost so a subsequent route-change notification can't
            // try to bring it back up against the same broken engine.
            markMicLost()
            emit(.error(
                message: "mic startup failed, continuing with system audio only: \(error.localizedDescription)"
            ))
        }
        try await startSystemAudio()

        startedAt = Date()
        startMixerPump()
    }

    /// Stop the SCStream and the mic engine, drain any leftover frames into
    /// each writer, finalize both writers, emit one `audio_file` event per
    /// finalized writer, return elapsed seconds since `start()` returned.
    ///
    /// The `audio_file` events are emitted on the wire *before* this function
    /// returns so callers can emit `stopped` immediately after — preserving
    /// the documented event order (per-file `audio_file` events first, then
    /// `stopped`).
    func stop() async -> Double {
        if testSilentSources {
            // Test mode: no real sources were ever started; just halt the
            // synthetic feeder, drain each writer one last time, and finalize.
            stopSyntheticFeeder()
            stopMixerPump()
            drainAndWrite(finalFlush: true)
            let elapsed = startedAt.map { Date().timeIntervalSince($0) } ?? 0
            finalizeWriters(elapsed: elapsed)
            return elapsed
        }

        // Drop the engine-config observer and the CoreAudio listeners
        // before tearing anything down: once we stop the engine ourselves,
        // AVAudioEngine may post a final notification — and CoreAudio may
        // fire one final format-change — that we don't want to misinterpret
        // as a mid-capture loss.
        if let token = engineConfigObserver {
            NotificationCenter.default.removeObserver(token)
            engineConfigObserver = nil
        }
        uninstallCoreAudioListeners()

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
        // One final drain pass to flush whatever samples are still buffered.
        drainAndWrite(finalFlush: true)

        let elapsed = startedAt.map { Date().timeIntervalSince($0) } ?? 0
        finalizeWriters(elapsed: elapsed)
        return elapsed
    }

    /// Close any writer still open and emit one `audio_file` event per source.
    /// A source that was lost mid-capture has already had its writer closed
    /// by the loss handler — we still emit its event here, with
    /// `status="truncated_at_offset"` and the captured offset as the duration.
    private func finalizeWriters(elapsed: Double) {
        if let writer = systemWriter {
            writer.close()
            systemWriter = nil
        }
        if let writer = micWriter {
            writer.close()
            micWriter = nil
        }

        // Status precedence per spec 005 slice 4:
        //   1. truncation (slice 3 behavior — kept exactly bit-for-bit)
        //   2. silent_throughout (no non-zero sample ever observed)
        //   3. captured_normally (default)
        if let offset = systemTruncatedAtOffsetSeconds {
            emit(.audioFile(
                path: systemURL.path,
                source: .systemAudio,
                durationSeconds: offset,
                status: "truncated_at_offset",
                truncatedAtOffsetSeconds: offset
            ))
        } else if !systemHasNonzeroSamples {
            emit(.audioFile(
                path: systemURL.path,
                source: .systemAudio,
                durationSeconds: elapsed,
                status: "silent_throughout",
                truncatedAtOffsetSeconds: nil
            ))
        } else {
            emit(.audioFile(
                path: systemURL.path,
                source: .systemAudio,
                durationSeconds: elapsed,
                status: "captured_normally",
                truncatedAtOffsetSeconds: nil
            ))
        }

        if let offset = micTruncatedAtOffsetSeconds {
            emit(.audioFile(
                path: micURL.path,
                source: .mic,
                durationSeconds: offset,
                status: "truncated_at_offset",
                truncatedAtOffsetSeconds: offset
            ))
        } else if !micHasNonzeroSamples {
            emit(.audioFile(
                path: micURL.path,
                source: .mic,
                durationSeconds: elapsed,
                status: "silent_throughout",
                truncatedAtOffsetSeconds: nil
            ))
        } else {
            emit(.audioFile(
                path: micURL.path,
                source: .mic,
                durationSeconds: elapsed,
                status: "captured_normally",
                truncatedAtOffsetSeconds: nil
            ))
        }
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
        if let converter = AVAudioConverter(from: inputFormat, to: processingFormat) {
            micConverter = converter
            micConverterInputFormat = inputFormat
        } // else: built lazily in handleMicBuffer once we see a real format.

        try startEngineWithRetries()

        // Watch for hardware route changes (AirPods connect/disconnect, USB
        // mic plug/unplug, system default input swap). Per Apple's docs the
        // engine has already stopped itself by the time this fires — we
        // rebuild the tap against the new input format and restart, which
        // keeps mic capture alive across the route change. Only if the
        // rebuild fails do we fall back to declaring the mic lost.
        engineConfigObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            self?.handleEngineConfigurationChange()
        }

        // Register CoreAudio property listeners that drive route-change
        // recovery off real device events rather than a polling timer. See
        // `installCoreAudioListeners` for the full story.
        installCoreAudioListeners()

        emit(.sourceAttached(source: .mic))
    }

    // MARK: - Source-loss handlers (Slice 5)

    /// Rebuild the mic tap and restart the engine after an
    /// `AVAudioEngineConfigurationChange`. Apple's documented recovery path
    /// for route changes: the engine has already stopped itself, the tap is
    /// invalid, and we need to re-tap against whatever the new input format
    /// is (different sample rate / channel count after a Bluetooth headset
    /// becomes the default input, etc.).
    ///
    /// Stays silent on the wire — no `source_lost` / `source_attached`
    /// events — so from the orchestrator's POV the mic is continuously
    /// attached across the route change. Only if the rebuild itself fails
    /// (no input device present, `engine.start()` throws) do we fall
    /// through to `handleMicLost` so the orchestrator records a genuine
    /// loss.
    private func handleEngineConfigurationChange() {
        FileHandle.standardError.write(
            Data("DBG mic-route: AVAudioEngineConfigurationChange\n".utf8)
        )
        triggerMicRestart(reason: "AVAudioEngineConfigurationChange")
    }

    /// Shared restart helper invoked by every event source that wants the
    /// mic engine torn down and brought back up — `AVAudioEngineConfigurationChange`,
    /// CoreAudio default-input changes, default-output changes, and the
    /// `DeviceIsRunningSomewhere == false` transition on the current input
    /// device. Idempotent w.r.t. an in-flight restart (the generation
    /// counter ensures the latest event wins).
    private func triggerMicRestart(reason: String) {
        lossLock.lock()
        let alreadyLost = micLost
        lossLock.unlock()
        if alreadyLost { return }

        // `stop()` clears the configuration observer before tearing down —
        // treat that as the signal that recovery is no longer wanted.
        if engineConfigObserver == nil { return }

        // Re-entrance guard. `engine.stop()` below itself flips
        // `IsRunningSomewhere` to false, which re-fires that listener and
        // re-enters this method while we're still mid-restart. If a
        // restart is already pending and the engine is already torn down,
        // just re-arm listeners and retry — don't bump the generation or
        // redo the teardown. A genuinely fresh external event will arrive
        // *after* the engine is back up, so engine.isRunning will be true
        // and this guard won't swallow it.
        if pendingMicRestart && !engine.isRunning {
            rearmListenersOnCurrentDefaultInput()
            attemptMicRestartIfPending()
            return
        }

        micRestartGeneration += 1
        pendingMicRestart = true

        if micTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }
        micConverter = nil
        micConverterInputFormat = nil

        // The current default input device may have just changed, so
        // re-point both per-input listeners (format + IsRunningSomewhere)
        // at whatever the new default is. Then try to bring the engine
        // back up immediately — it will succeed if the format has already
        // settled; otherwise the format listener (or a later
        // IsRunningSomewhere transition back to true) will retrigger us.
        rearmListenersOnCurrentDefaultInput()
        attemptMicRestartIfPending()
    }

    /// Try to bring the engine back up *if* a restart is pending and the
    /// current input bus reports a valid format. Called from the
    /// configuration-change handler (immediate attempt) and from the
    /// CoreAudio stream-format listener (the actual "device is ready"
    /// signal). Idempotent: a no-op when nothing is pending or the engine
    /// is already running.
    ///
    /// Must run on the main queue — mutates main-only state
    /// (`pendingMicRestart`, `micTapInstalled`, the engine itself).
    private func attemptMicRestartIfPending() {
        if !pendingMicRestart { return }

        lossLock.lock()
        let alreadyLost = micLost
        lossLock.unlock()
        if alreadyLost { return }

        // `stop()` removes the configuration observer before tearing things
        // down — treat that as the signal to stop attempting recovery.
        if engineConfigObserver == nil {
            pendingMicRestart = false
            return
        }

        let input = engine.inputNode
        let newFormat = input.outputFormat(forBus: 0)
        guard newFormat.channelCount > 0, newFormat.sampleRate > 0 else {
            // Format isn't settled yet — wait for the next listener fire.
            return
        }

        if micTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            self?.handleMicBuffer(buffer)
        }
        micTapInstalled = true

        do {
            engine.prepare()
            try engine.start()
            pendingMicRestart = false
        } catch {
            // Engine still refusing — drop the tap, reset, and leave the
            // pending flag set. The next CoreAudio format-change listener
            // fire (or the watchdog) will re-evaluate.
            if micTapInstalled {
                engine.inputNode.removeTap(onBus: 0)
                micTapInstalled = false
            }
            engine.reset()
        }
    }

    // MARK: - CoreAudio listeners (route-change recovery)

    /// Look up the system default input device.
    private func currentDefaultInputDevice() -> AudioObjectID {
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address, 0, nil, &size, &deviceID
        )
        return status == noErr ? deviceID : AudioObjectID(kAudioObjectUnknown)
    }

    /// Register the system-object listeners (default input + default
    /// output) and the per-device listeners on whatever the current
    /// default input is (stream format + IsRunningSomewhere). All fire on
    /// `coreAudioListenerQueue`; their handlers hop to main before
    /// touching engine state.
    private func installCoreAudioListeners() {
        var defaultInputAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let defaultInputBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.handleDefaultInputDeviceChanged()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject),
            &defaultInputAddress,
            coreAudioListenerQueue,
            defaultInputBlock
        ) == noErr {
            defaultInputListenerBlock = defaultInputBlock
        }

        var defaultOutputAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let defaultOutputBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.handleDefaultOutputDeviceChanged()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject),
            &defaultOutputAddress,
            coreAudioListenerQueue,
            defaultOutputBlock
        ) == noErr {
            defaultOutputListenerBlock = defaultOutputBlock
        }

        installPerInputListenersOnCurrentDefaultInput()
    }

    /// Detach every CoreAudio listener. Safe to call multiple times.
    private func uninstallCoreAudioListeners() {
        if let block = defaultInputListenerBlock {
            var address = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultInputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            _ = AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                coreAudioListenerQueue,
                block
            )
            defaultInputListenerBlock = nil
        }
        if let block = defaultOutputListenerBlock {
            var address = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            _ = AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                coreAudioListenerQueue,
                block
            )
            defaultOutputListenerBlock = nil
        }
        removePerInputListeners()
    }

    /// Detach both per-input listeners (format + IsRunningSomewhere) from
    /// whichever device they were attached to (if any).
    private func removePerInputListeners() {
        let device = watchedInputDeviceID
        if device != AudioObjectID(kAudioObjectUnknown) {
            if let block = formatListenerBlock {
                var address = AudioObjectPropertyAddress(
                    mSelector: kAudioDevicePropertyStreamFormat,
                    mScope: kAudioDevicePropertyScopeInput,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(
                    device, &address, coreAudioListenerQueue, block
                )
            }
            if let block = runningSomewhereListenerBlock {
                var address = AudioObjectPropertyAddress(
                    mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
                    mScope: kAudioObjectPropertyScopeGlobal,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(
                    device, &address, coreAudioListenerQueue, block
                )
            }
        }
        formatListenerBlock = nil
        runningSomewhereListenerBlock = nil
        watchedInputDeviceID = AudioObjectID(kAudioObjectUnknown)
    }

    /// Install both per-input listeners (format + IsRunningSomewhere) on
    /// the *current* system default input device. If no input device is
    /// present this is a no-op; the default-input listener will re-invoke
    /// us once one shows up.
    private func installPerInputListenersOnCurrentDefaultInput() {
        let device = currentDefaultInputDevice()
        guard device != AudioObjectID(kAudioObjectUnknown) else { return }

        var formatAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamFormat,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        let formatBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                FileHandle.standardError.write(
                    Data("DBG mic-route: input stream format changed\n".utf8)
                )
                self?.attemptMicRestartIfPending()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            device, &formatAddress, coreAudioListenerQueue, formatBlock
        ) == noErr {
            formatListenerBlock = formatBlock
        }

        var runningAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let runningBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.handleInputDeviceIsRunningSomewhereChanged()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            device, &runningAddress, coreAudioListenerQueue, runningBlock
        ) == noErr {
            runningSomewhereListenerBlock = runningBlock
        }

        watchedInputDeviceID = device
    }

    /// Detach the per-input listeners and reattach them to whatever device
    /// is the default input *now*. Called from every event source that may
    /// have caused the default input to change, so by the time we try to
    /// restart the engine we're already watching the right device.
    private func rearmListenersOnCurrentDefaultInput() {
        let current = currentDefaultInputDevice()
        if current == watchedInputDeviceID
            && formatListenerBlock != nil
            && runningSomewhereListenerBlock != nil {
            return
        }
        removePerInputListeners()
        installPerInputListenersOnCurrentDefaultInput()
    }

    /// Read `kAudioDevicePropertyDeviceIsRunningSomewhere` for `deviceID`.
    /// Returns false on any CoreAudio error — the caller treats false the
    /// same as "the input isn't flowing", which is the safer default.
    private func isInputDeviceRunning(_ deviceID: AudioObjectID) -> Bool {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(
            deviceID, &address, 0, nil, &size, &value
        )
        return status == noErr && value != 0
    }

    /// Default input changed (e.g. AirPods became default *input*). The
    /// engine's tap is bound to the old input, so a full restart is
    /// always warranted.
    private func handleDefaultInputDeviceChanged() {
        FileHandle.standardError.write(
            Data("DBG mic-route: default input device changed\n".utf8)
        )
        triggerMicRestart(reason: "default input device changed")
    }

    /// Default *output* changed (e.g. AirPods became default output even
    /// though the input device didn't change). This sometimes silently
    /// causes the OS to stop I/O on the current input AU. Restart only if
    /// the input is now actually stopped — otherwise this is a benign
    /// output swap (external speakers, etc.) and we'd be tearing down a
    /// working engine for nothing.
    private func handleDefaultOutputDeviceChanged() {
        FileHandle.standardError.write(
            Data("DBG mic-route: default output device changed\n".utf8)
        )
        let device = watchedInputDeviceID
        if device == AudioObjectID(kAudioObjectUnknown) {
            return
        }
        if isInputDeviceRunning(device) {
            FileHandle.standardError.write(
                Data("DBG mic-route: input still running, no restart\n".utf8)
            )
            return
        }
        triggerMicRestart(reason: "default output changed and input stopped")
    }

    /// `DeviceIsRunningSomewhere` on the watched input toggled. If it went
    /// to false the OS has stopped I/O on the mic (the exact failure mode
    /// behind the AirPods-as-output-only bug); restart. If it went to true
    /// somebody (perhaps us, perhaps another app) is driving the device —
    /// if a restart is pending, this is a hint to retry it now.
    private func handleInputDeviceIsRunningSomewhereChanged() {
        let device = watchedInputDeviceID
        if device == AudioObjectID(kAudioObjectUnknown) { return }
        let running = isInputDeviceRunning(device)
        FileHandle.standardError.write(
            Data("DBG mic-route: input IsRunningSomewhere=\(running)\n".utf8)
        )
        if running {
            attemptMicRestartIfPending()
        } else {
            triggerMicRestart(reason: "input device IsRunningSomewhere=false")
        }
    }

    /// Atomically set `micLost = true`. Pulled out of `startSources` so the
    /// `NSLock` use stays inside a synchronous helper — async functions can't
    /// hold `NSLock` across suspension points under Swift 6.
    private func markMicLost() {
        lossLock.lock()
        micLost = true
        lossLock.unlock()
    }

    /// Call `engine.prepare()` + `engine.start()` with a small retry loop.
    ///
    /// AVAudioEngine on macOS can throw `kAudioUnitErr_FormatNotSupported`
    /// (-10868) on the first start attempt right after the previous engine's
    /// teardown, or while a Bluetooth audio route is still transitioning to
    /// or from the AirPods/headset. `engine.reset()` followed by a brief
    /// pause is the documented recovery — it returns the engine to a known
    /// state and gives the audio HAL a chance to settle. We try a small
    /// fixed number of attempts before surfacing the error to the caller.
    private func startEngineWithRetries() throws {
        let maxAttempts = 3
        var lastError: Error?
        for attempt in 0..<maxAttempts {
            if attempt > 0 {
                engine.reset()
                Thread.sleep(forTimeInterval: 0.1)
            }
            do {
                engine.prepare()
                try engine.start()
                return
            } catch {
                lastError = error
            }
        }
        throw lastError ?? AudioCaptureError.bufferFormatUnavailable
    }

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
        micTruncatedAtOffsetSeconds = offset

        // Stop producing mic samples.
        if micTapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            micTapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }
        // Mic is permanently gone for this capture — no point keeping the
        // CoreAudio listeners armed; they'd just keep firing on subsequent
        // route changes with nothing to do.
        uninstallCoreAudioListeners()
        pendingMicRestart = false

        // Drain whatever is buffered into the mic writer, then close it so the
        // file is finalized at the point of failure. `finalizeWriters` will
        // still emit the `audio_file` event at stop, reading the truncation
        // offset we just recorded.
        if let writer = micWriter {
            let leftover = micQueue.drainAll()
            writeSamples(leftover, to: writer, label: "mic")
            writer.close()
            micWriter = nil
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
        systemTruncatedAtOffsetSeconds = offset

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

        // Drain whatever is buffered into the system writer, then close it so
        // the file is finalized at the point of failure.
        if let writer = systemWriter {
            let leftover = systemQueue.drainAll()
            writeSamples(leftover, to: writer, label: "system")
            writer.close()
            systemWriter = nil
        }

        emit(.sourceLost(
            source: .systemAudio,
            atOffsetSeconds: offset,
            reason: reason
        ))
    }

    // MARK: - System-audio sample handler

    // Diagnostic counters for the empty-system.wav bug. All accessed only on
    // `sampleQueue` (SCStream's sample-handler queue is serial per Apple's
    // contract), so plain Ints are race-free. Temporary — remove once the
    // root cause is found.
    private var dbgSysEntered: Int = 0
    private var dbgSysReady: Int = 0
    private var dbgSysPcm: Int = 0
    private var dbgSysConverted: Int = 0
    private var dbgSysEnqueued: Int = 0

    private func dbgLogSys(_ tag: String, _ extra: String = "") {
        let msg = "DBG sys-audio \(tag) entered=\(dbgSysEntered) ready=\(dbgSysReady) pcm=\(dbgSysPcm) converted=\(dbgSysConverted) enqueued=\(dbgSysEnqueued) \(extra)\n"
        FileHandle.standardError.write(Data(msg.utf8))
    }

    fileprivate func handleSystemBuffer(_ sampleBuffer: CMSampleBuffer) {
        dbgSysEntered += 1
        if dbgSysEntered == 1 || dbgSysEntered % 200 == 0 {
            dbgLogSys("enter")
        }
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        dbgSysReady += 1
        guard let inputBuffer = makePCMBuffer(from: sampleBuffer) else {
            if dbgSysReady - dbgSysPcm <= 3 {
                dbgLogSys("drop:makePCMBuffer-nil")
            }
            return
        }
        dbgSysPcm += 1
        if dbgSysPcm == 1 {
            dbgLogSys("first-pcm", "frames=\(inputBuffer.frameLength) fmt=\(inputBuffer.format)")
        }
        guard let outputBuffer = convert(
            inputBuffer,
            converter: &systemConverter,
            converterInputFormat: &systemConverterInputFormat
        ) else {
            if dbgSysPcm - dbgSysConverted <= 3 {
                dbgLogSys("drop:convert-nil")
            }
            return
        }
        dbgSysConverted += 1
        if dbgSysConverted == 1 {
            dbgLogSys("first-converted", "frames=\(outputBuffer.frameLength)")
        }
        enqueue(buffer: outputBuffer, into: systemQueue)
        dbgSysEnqueued += 1
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
        let outputFormat = processingFormat

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
            self?.drainAndWrite(finalFlush: false)
        }
        mixerTimer = timer
        timer.resume()
    }

    private func stopMixerPump() {
        mixerTimer?.cancel()
        mixerTimer = nil
    }

    // MARK: - Synthetic feeder (test mode)

    /// Drive both ring buffers from a deterministic timer.
    ///
    /// Spec 005 pattern: a continuous 440 Hz sine tone on the mic source and a
    /// continuous 880 Hz sine tone on the system source. Two distinct
    /// frequencies on the two sides let the integration test FFT each output
    /// WAV independently and assert "this file contains *its* tone and *only*
    /// its tone" — i.e. the two-writer pipeline never lets one source's
    /// samples bleed into the other writer.
    private func startSyntheticFeeder() {
        let q = DispatchQueue(label: "record.audiocapture.synthetic", qos: .userInitiated)
        let timer = DispatchSource.makeTimerSource(queue: q)
        // ~160 frames per 10 ms at 16 kHz. Match the drain pump cadence so the
        // queues stay shallow.
        let framesPerTick = 160
        timer.schedule(
            deadline: .now() + .milliseconds(drainIntervalMs),
            repeating: .milliseconds(drainIntervalMs)
        )
        timer.setEventHandler { [weak self] in
            self?.tickSyntheticFeeder(framesPerTick: framesPerTick)
        }
        syntheticQueue = q
        syntheticTimer = timer
        syntheticFrameIndex = 0
        timer.resume()

        if let after = injectMicLossAfterSeconds {
            DispatchQueue.main.asyncAfter(deadline: .now() + after) { [weak self] in
                self?.handleMicLost(reason: "synthetic injection: --inject-mic-loss-after-seconds")
            }
        }
    }

    private func stopSyntheticFeeder() {
        syntheticTimer?.cancel()
        syntheticTimer = nil
        syntheticQueue = nil
    }

    /// Generate the next `framesPerTick` int16 samples for each side and
    /// enqueue them. Deterministic w.r.t. `syntheticFrameIndex` so test
    /// assertions can predict the waveform exactly. Mic side is a 440 Hz
    /// sine; system side is an 880 Hz sine.
    private func tickSyntheticFeeder(framesPerTick: Int) {
        let sampleRate = 16000.0
        let micToneHz = 440.0
        let systemToneHz = 880.0
        // Peak amplitude well below int16 full-scale; matches the spec's
        // "~0.5 of full-scale".
        let amplitude: Double = 16000.0

        var micSamples = [Int16](repeating: 0, count: framesPerTick)
        var sysSamples = [Int16](repeating: 0, count: framesPerTick)

        for i in 0..<framesPerTick {
            let frame = Double(syntheticFrameIndex + i)
            // Slice 4: when `silentMicSource == true` the mic side stays at
            // all-zero Int16 samples — the integration test asserts the
            // resulting `audio_file` event carries `status="silent_throughout"`.
            // System tone is unaffected.
            if !silentMicSource {
                let micS = sin(2.0 * .pi * micToneHz * frame / sampleRate)
                micSamples[i] = Int16(clamping: Int(amplitude * micS))
            }
            let sysS = sin(2.0 * .pi * systemToneHz * frame / sampleRate)
            sysSamples[i] = Int16(clamping: Int(amplitude * sysS))
        }
        syntheticFrameIndex += framesPerTick

        lossLock.lock()
        let skipMic = micLost
        let skipSystem = systemAudioLost
        lossLock.unlock()

        if !skipMic {
            micSamples.withUnsafeBufferPointer { micQueue.append($0) }
        }
        if !skipSystem {
            sysSamples.withUnsafeBufferPointer { systemQueue.append($0) }
        }
    }

    /// Drain each ring buffer independently and write its samples directly to
    /// the corresponding writer. No mixing, no zero-padding of the "shorter
    /// side" — each writer advances on its own source's samples only.
    /// On `finalFlush == true` we drain everything remaining from each side.
    private func drainAndWrite(finalFlush: Bool) {
        // System audio side.
        if let writer = systemWriter {
            let samples: [Int16]
            if finalFlush {
                samples = systemQueue.drainAll()
            } else {
                let depth = systemQueue.count
                samples = depth > 0 ? systemQueue.drain(upTo: depth) : []
            }
            writeSamples(samples, to: writer, label: "system")
        }

        // Mic side.
        if let writer = micWriter {
            let samples: [Int16]
            if finalFlush {
                samples = micQueue.drainAll()
            } else {
                let depth = micQueue.count
                samples = depth > 0 ? micQueue.drain(upTo: depth) : []
            }
            writeSamples(samples, to: writer, label: "mic")
        }
    }

    /// Helper: wrap `samples` in an `AVAudioPCMBuffer` and write to `writer`.
    /// No-op on empty input. Emits an `error` event on writer failure.
    ///
    /// Also folds in slice 4's silent-source detection: cheap running OR over
    /// the drained Int16 samples flips the corresponding `<source>HasNonzeroSamples`
    /// flag the first time any non-zero sample is seen, after which the loop
    /// short-circuits. `finalizeWriters` reads the flag to decide between
    /// `"captured_normally"` and `"silent_throughout"`.
    private func writeSamples(_ samples: [Int16], to writer: WAVWriter, label: String) {
        let outCount = samples.count
        if outCount == 0 { return }

        // Per-source non-zero detection. Only scan until the first non-zero
        // sample lands; once the flag is set there is no work to do.
        switch label {
        case "mic":
            if !micHasNonzeroSamples {
                for s in samples where s != 0 {
                    micHasNonzeroSamples = true
                    break
                }
            }
        case "system":
            if !systemHasNonzeroSamples {
                for s in samples where s != 0 {
                    systemHasNonzeroSamples = true
                    break
                }
            }
        default:
            break
        }

        guard let outBuffer = AVAudioPCMBuffer(
            pcmFormat: processingFormat,
            frameCapacity: AVAudioFrameCount(outCount)
        ) else { return }
        outBuffer.frameLength = AVAudioFrameCount(outCount)
        if let dst = outBuffer.int16ChannelData?[0] {
            samples.withUnsafeBufferPointer { src in
                if let base = src.baseAddress {
                    dst.update(from: base, count: outCount)
                }
            }
        }

        do {
            try writer.write(outBuffer)
        } catch {
            emit(.error(message: "\(label) wav write failed: \(error)"))
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
            if dbgSysPcm == 0 && dbgSysReady < 5 {
                FileHandle.standardError.write(Data("DBG makePCMBuffer fail: no formatDesc/asbd\n".utf8))
            }
            return nil
        }
        var asbd = asbdPointer.pointee
        guard let format = AVAudioFormat(streamDescription: &asbd) else {
            if dbgSysPcm == 0 && dbgSysReady < 5 {
                let msg = "DBG makePCMBuffer fail: AVAudioFormat(streamDescription:) returned nil; asbd sr=\(asbd.mSampleRate) fmtID=\(asbd.mFormatID) flags=\(asbd.mFormatFlags) bytesPerPacket=\(asbd.mBytesPerPacket) framesPerPacket=\(asbd.mFramesPerPacket) bytesPerFrame=\(asbd.mBytesPerFrame) chans=\(asbd.mChannelsPerFrame) bitsPerChannel=\(asbd.mBitsPerChannel)\n"
                FileHandle.standardError.write(Data(msg.utf8))
            }
            return nil
        }

        let frameCount = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frameCount > 0,
              let pcmBuffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount)
        else {
            if dbgSysPcm == 0 && dbgSysReady < 5 {
                FileHandle.standardError.write(Data("DBG makePCMBuffer fail: frameCount=\(frameCount) or AVAudioPCMBuffer alloc nil\n".utf8))
            }
            return nil
        }
        pcmBuffer.frameLength = frameCount

        // Discover the required ABL size — for non-interleaved stereo (SCK's
        // default audio shape) we need room for one AudioBuffer per channel,
        // not the single-buffer stack allocation. CoreMedia returns
        // kCMSampleBufferError_ArrayTooSmall (-12737) if `bufferListSize` is
        // smaller than required.
        var ablSize: Int = 0
        let sizeStatus = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &ablSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )
        guard sizeStatus == noErr, ablSize > 0 else {
            return nil
        }

        let ablPtr = UnsafeMutableRawPointer.allocate(
            byteCount: ablSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { ablPtr.deallocate() }
        let ablTyped = ablPtr.bindMemory(to: AudioBufferList.self, capacity: 1)

        var blockBufferOut: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: ablTyped,
            bufferListSize: ablSize,
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
        let sourceList = UnsafeMutableAudioBufferListPointer(ablTyped)
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
