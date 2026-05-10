import Foundation
// Imports retained as build-time checks for the SDK frameworks slice 3 will use.
// They are unused in this stub; do not remove.
import ScreenCaptureKit
import AVFoundation
import CoreAudio
import AppKit

// MARK: - Stdout: line-buffered, single-line JSON events

// Force line-buffered stdout so the Python supervisor sees each event as soon
// as it's written. macOS defaults to block-buffered when stdout is a pipe.
setvbuf(stdout, nil, _IOLBF, 0)

/// Write a single event as one JSON line followed by `\n`, then flush.
@inline(__always)
func emit(_ event: Event) {
    do {
        let line = try IPCCodec.encode(event)
        // Defensive: encoder shouldn't produce embedded newlines, but if some
        // future field ever did, collapse them so the line protocol stays intact.
        let safe = line.replacingOccurrences(of: "\n", with: " ")
        FileHandle.standardOutput.write(Data((safe + "\n").utf8))
    } catch {
        // Last-ditch fallback so we don't drop the protocol entirely.
        let fallback = #"{"event":"error","message":"event encode failed"}"# + "\n"
        FileHandle.standardOutput.write(Data(fallback.utf8))
    }
    fflush(stdout)
}

// MARK: - Time formatting

/// ISO-8601 UTC formatter, e.g. `2026-05-10T14:32:08Z`.
let iso8601: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = TimeZone(identifier: "UTC")
    return f
}()

// MARK: - Capture state (stub)

/// In-flight capture state. `nil` between `start` and `stop`.
struct CaptureState {
    let startMonotonic: Date
    let outputPath: String
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

// MARK: - Stdin command loop

func handleStart(outputPath: String, format _: AudioFormat) {
    // Stub: do not actually open audio sources or write a file.
    let now = Date()
    capture = CaptureState(startMonotonic: now, outputPath: outputPath)

    emit(.started(startTime: iso8601.string(from: now)))
    emit(.sourceAttached(source: .mic))
    emit(.sourceAttached(source: .systemAudio))
}

func handleStop() {
    guard let state = capture else {
        // Stop without a prior start — surface as an error and stay alive so
        // the supervisor can decide what to do.
        emit(.error(message: "stop received before start"))
        return
    }
    let duration = Date().timeIntervalSince(state.startMonotonic)
    emit(.stopped(durationSeconds: duration, outputPath: state.outputPath))
    // Supervisor expects the binary to exit on its own after `stop`.
    exit(0)
}

// 1. Announce readiness immediately on startup.
emit(.ready)

// 2. Blocking, line-oriented read loop on stdin. `readLine()` strips the
//    trailing newline and returns nil on EOF.
while let line = readLine(strippingNewline: true) {
    // Skip blank lines silently — they're not malformed, just noise.
    let trimmed = line.trimmingCharacters(in: .whitespaces)
    if trimmed.isEmpty { continue }

    let command: Command
    do {
        command = try IPCCodec.decodeCommand(line: trimmed)
    } catch {
        emit(.error(message: "malformed command: \(error.localizedDescription)"))
        continue
    }

    switch command {
    case .start(let outputPath, let format):
        handleStart(outputPath: outputPath, format: format)
    case .stop:
        handleStop()
    case .shutdown:
        // Clean exit, no `stopped` event.
        exit(0)
    }
}

// EOF on stdin: treat as a clean shutdown so we don't linger as a zombie.
exit(0)
