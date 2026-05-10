import Foundation

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

/// Parse CLI flags before any IPC begins. The only supported flag right now is
/// `--test-silent-sources`, which swaps the real `SCStream` / `AVAudioEngine`
/// sources for a deterministic synthetic feeder (Slice 7). Unknown flags emit
/// a single `error` event and exit non-zero, so the Python supervisor sees a
/// predictable failure rather than a hung process.
func parseCLIFlags() -> Bool {
    var testSilent = false
    // CommandLine.arguments[0] is the executable path.
    let args = Array(CommandLine.arguments.dropFirst())
    for arg in args {
        switch arg {
        case "--test-silent-sources":
            testSilent = true
        default:
            // Emit directly to stdout; the lock-protected `emit` helper isn't
            // strictly required here (we're still single-threaded), but use
            // the same path for consistency.
            let line = #"{"event":"error","message":"unknown CLI flag: "# + arg + #""}"# + "\n"
            FileHandle.standardOutput.write(Data(line.utf8))
            fflush(stdout)
            exit(1)
        }
    }
    return testSilent
}

let testSilentSources = parseCLIFlags()

// MARK: - Capture state

/// In-flight capture state. `nil` between `start` and `stop`.
final class CaptureState {
    let capture: AudioCapture
    let outputPath: String
    let startedAt: Date

    init(capture: AudioCapture, outputPath: String, startedAt: Date) {
        self.capture = capture
        self.outputPath = outputPath
        self.startedAt = startedAt
    }
}

var capture: CaptureState? = nil

// MARK: - SIGTERM handler

// Ignore SIGTERM at the libc level so it doesn't kill us before our handler
// runs; Dispatch will deliver it to our handler instead.
signal(SIGTERM, SIG_IGN)
let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSource.setEventHandler {
    // Clean exit on SIGTERM, no `stopped` emitted (matches `shutdown` semantics).
    exit(0)
}
sigtermSource.resume()

// MARK: - Command handlers

func handleStart(outputPath: String, format _: AudioFormat) {
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
    capture = CaptureState(
        capture: audioCapture,
        outputPath: outputPath,
        startedAt: startedAt
    )

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

    Task {
        let duration = await state.capture.stop()
        emit(.stopped(durationSeconds: duration, outputPath: state.outputPath))
        exit(0)
    }
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
            case .start(let outputPath, let format):
                handleStart(outputPath: outputPath, format: format)
            case .stop:
                handleStop()
            case .shutdown:
                exit(0)
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
