import XCTest
@testable import RecordCapture

/// Round-trips the shared IPC fixtures through `IPCCodec`. The Python sister
/// test (`tests/python/test_ipc.py`) round-trips the *same* files via pydantic.
/// Drift on either side breaks both suites — that's the contract.
final class ProtocolTests: XCTestCase {
    // MARK: - Fixture loading

    private func fixtureURL(subdirectory: String, name: String) throws -> URL {
        // SwiftPM exposes copied resources under `Bundle.module`. The `Fixtures`
        // directory is copied verbatim, so the layout is preserved.
        let url = Bundle.module.url(
            forResource: name,
            withExtension: "json",
            subdirectory: "Fixtures/\(subdirectory)"
        )
        guard let url else {
            XCTFail("Missing fixture: Fixtures/\(subdirectory)/\(name).json")
            throw NSError(domain: "ProtocolTests", code: 1)
        }
        return url
    }

    private func loadFixtureLine(subdirectory: String, name: String) throws -> String {
        let url = try fixtureURL(subdirectory: subdirectory, name: name)
        let data = try Data(contentsOf: url)
        guard let raw = String(data: data, encoding: .utf8) else {
            XCTFail("Fixture \(name) is not valid UTF-8")
            throw NSError(domain: "ProtocolTests", code: 2)
        }
        return raw.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Commands

    /// Each fixture file paired with its expected canonical `Command` value.
    /// Decoding must produce *exactly* this value; re-encoding then re-decoding
    /// must come back to the same value (key ordering is implementation-defined
    /// so we compare via the typed value, not the JSON bytes).
    private var commandFixtures: [(file: String, expected: Command)] {
        [
            // Audio-only `start` fixture from 001. The video fields are
            // optional on the wire and must round-trip as `nil` so older
            // audio-only callers stay compatible.
            (
                "start",
                .start(
                    outputPath: "/abs/path/to/2026-05-10T14-32-08.wav",
                    videoOutputPath: nil,
                    format: AudioFormat(sampleRate: 16000, bitDepth: 16, channels: 1),
                    video: nil
                )
            ),
            // Extended `start` fixture from 002 (this spec). Both new fields
            // are populated so a regression that drops them on decode/encode
            // surfaces here.
            (
                "start_with_video",
                .start(
                    outputPath: "/abs/path/to/2026-05-10T14-32-08.wav",
                    videoOutputPath: "/abs/path/to/2026-05-10T14-32-08.mp4",
                    format: AudioFormat(sampleRate: 16000, bitDepth: 16, channels: 1),
                    video: VideoConfig(fps: 30, showsCursor: true)
                )
            ),
            ("stop", .stop),
            ("shutdown", .shutdown),
            // Slice 5: hotkey commands.
            (
                "register_hotkey",
                .registerHotkey(modifiers: [.cmd, .option], key: "r")
            ),
            ("unregister_hotkey", .unregisterHotkey)
        ]
    }

    func testCommandFixturesDecodeToCanonicalValues() throws {
        for (file, expected) in commandFixtures {
            let line = try loadFixtureLine(subdirectory: "commands", name: file)
            let decoded = try IPCCodec.decodeCommand(line: line)
            XCTAssertEqual(decoded, expected, "decode mismatch for commands/\(file).json")
        }
    }

    func testCommandFixturesRoundTrip() throws {
        for (file, expected) in commandFixtures {
            let line = try loadFixtureLine(subdirectory: "commands", name: file)
            let decoded = try IPCCodec.decodeCommand(line: line)
            let reencoded = try IPCCodec.encode(decoded)
            // Re-decode the re-encoded string and assert structural equality
            // — sidesteps the JSON-key-ordering trap.
            let reDecoded = try IPCCodec.decodeCommand(line: reencoded)
            XCTAssertEqual(reDecoded, expected, "round-trip mismatch for commands/\(file).json")
            XCTAssertFalse(reencoded.contains("\n"), "encoded command must be single-line")
        }
    }

    // MARK: - Events

    private var eventFixtures: [(file: String, expected: Event)] {
        [
            ("ready", .ready),
            ("permission_required_microphone", .permissionRequired(kind: .microphone)),
            ("permission_required_screen_recording", .permissionRequired(kind: .screenRecording)),
            ("permission_denied_microphone", .permissionDenied(kind: .microphone)),
            ("permission_denied_screen_recording", .permissionDenied(kind: .screenRecording)),
            ("started", .started(startTime: "2026-05-10T14:32:08Z")),
            ("source_attached_mic", .sourceAttached(source: .mic)),
            ("source_attached_system_audio", .sourceAttached(source: .systemAudio)),
            (
                "source_lost_mic",
                .sourceLost(
                    source: .mic,
                    atOffsetSeconds: 134.2,
                    reason: "input device disconnected"
                )
            ),
            (
                "source_lost_system_audio",
                .sourceLost(
                    source: .systemAudio,
                    atOffsetSeconds: 7.5,
                    reason: "display disappeared"
                )
            ),
            (
                "stopped",
                .stopped(
                    durationSeconds: 42.5,
                    outputPath: "/abs/path/to/2026-05-10T14-32-08.wav"
                )
            ),
            ("error", .error(message: "capture binary missing")),
            // Spec 002 events. Field values mirror the JSON examples in
            // `technical-considerations.md` §2.6 so the fixtures are
            // recognizably the same shapes documented there.
            (
                "video_started",
                .videoStarted(
                    displayId: 1,
                    widthPx: 2560,
                    heightPx: 1440,
                    fps: 30
                )
            ),
            (
                "video_lost",
                .videoLost(
                    atOffsetSeconds: 134.2,
                    reason: "sc_stream_error",
                    message: "stream stopped: -16665"
                )
            ),
            (
                "video_file",
                .videoFile(
                    path: "/abs/path/to/2026-05-10T14-32-08.mp4",
                    durationSeconds: 612.4
                )
            ),
            (
                "display_reconfigured",
                .displayReconfigured(
                    reason: .primaryChanged,
                    newDisplayId: 2,
                    newWidthPx: 1920,
                    newHeightPx: 1080
                )
            ),
            (
                "capture_ended_by_system_event",
                .captureEndedBySystemEvent(
                    reason: .systemSleep,
                    atOffsetSeconds: 134.2
                )
            ),
            // Slice 5: hotkey events.
            (
                "hotkey_registered",
                .hotkeyRegistered(
                    status: .registered,
                    modifiers: [.cmd, .option],
                    key: "r",
                    message: "registered"
                )
            ),
            ("hotkey_pressed", .hotkeyPressed),
            ("hotkey_unregistered", .hotkeyUnregistered)
        ]
    }

    func testEventFixturesDecodeToCanonicalValues() throws {
        for (file, expected) in eventFixtures {
            let line = try loadFixtureLine(subdirectory: "events", name: file)
            let decoded = try IPCCodec.decode(eventLine: line)
            XCTAssertEqual(decoded, expected, "decode mismatch for events/\(file).json")
        }
    }

    func testEventFixturesRoundTrip() throws {
        for (file, expected) in eventFixtures {
            let line = try loadFixtureLine(subdirectory: "events", name: file)
            let decoded = try IPCCodec.decode(eventLine: line)
            let reencoded = try IPCCodec.encode(decoded)
            let reDecoded = try IPCCodec.decode(eventLine: reencoded)
            XCTAssertEqual(reDecoded, expected, "round-trip mismatch for events/\(file).json")
            XCTAssertFalse(reencoded.contains("\n"), "encoded event must be single-line")
        }
    }

    // MARK: - Malformed input

    func testGarbageInputThrows() {
        XCTAssertThrowsError(try IPCCodec.decodeCommand(line: "garbage"))
    }

    func testUnknownCommandKindThrows() {
        XCTAssertThrowsError(try IPCCodec.decodeCommand(line: "{\"cmd\":\"unknown\"}"))
    }
}
