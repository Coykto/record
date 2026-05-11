import Foundation

// MARK: - Audio format

/// Audio format payload carried inside a `start` command.
///
/// JSON shape: `{"sample_rate":16000,"bit_depth":16,"channels":1}`
struct AudioFormat: Codable, Equatable {
    var sampleRate: Int
    var bitDepth: Int
    var channels: Int

    enum CodingKeys: String, CodingKey {
        case sampleRate = "sample_rate"
        case bitDepth = "bit_depth"
        case channels
    }
}

/// Video config payload carried inside a `start` command when video capture is requested.
///
/// JSON shape: `{"fps":30,"show_cursor":true}`
struct VideoConfig: Codable, Equatable {
    var fps: Int
    var showsCursor: Bool

    enum CodingKeys: String, CodingKey {
        case fps
        case showsCursor = "show_cursor"
    }
}

// MARK: - Commands (orchestrator → daemon)

/// A command read off stdin from the orchestrator.
///
/// Discriminator is the `cmd` field. Unknown values throw a decoding error
/// so that callers can surface a clean `error` event upstream.
enum Command: Equatable {
    case start(outputPath: String, videoOutputPath: String?, format: AudioFormat, video: VideoConfig?)
    case stop
    case shutdown

    private enum CodingKeys: String, CodingKey {
        case cmd
        case outputPath = "output_path"
        case videoOutputPath = "video_output_path"
        case format
        case video
    }

    private enum CommandKind: String, Decodable {
        case start
        case stop
        case shutdown
    }
}

extension Command: Codable {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try container.decode(CommandKind.self, forKey: .cmd)
        switch kind {
        case .start:
            let path = try container.decode(String.self, forKey: .outputPath)
            // Both video fields are optional: audio-only callers (and the
            // existing Slice 1 fixtures/tests) omit them entirely.
            let videoPath = try container.decodeIfPresent(String.self, forKey: .videoOutputPath)
            let fmt = try container.decode(AudioFormat.self, forKey: .format)
            let video = try container.decodeIfPresent(VideoConfig.self, forKey: .video)
            self = .start(outputPath: path, videoOutputPath: videoPath, format: fmt, video: video)
        case .stop:
            self = .stop
        case .shutdown:
            self = .shutdown
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .start(let path, let videoPath, let fmt, let video):
            try container.encode("start", forKey: .cmd)
            try container.encode(path, forKey: .outputPath)
            try container.encodeIfPresent(videoPath, forKey: .videoOutputPath)
            try container.encode(fmt, forKey: .format)
            try container.encodeIfPresent(video, forKey: .video)
        case .stop:
            try container.encode("stop", forKey: .cmd)
        case .shutdown:
            try container.encode("shutdown", forKey: .cmd)
        }
    }
}

// MARK: - Events (daemon → orchestrator)

/// Permission domain referenced by `permission_required` and `permission_denied`.
enum PermissionKind: String, Codable {
    case microphone
    case screenRecording = "screen_recording"
}

/// Audio source referenced by `source_attached` and `source_lost`.
enum SourceKind: String, Codable {
    case mic
    case systemAudio = "system_audio"
}

/// Closed set of reasons for `display_reconfigured` events.
enum DisplayReconfigurationReason: String, Codable {
    case primaryChanged = "primary_changed"
    case resolutionChanged = "resolution_changed"
    case displayRemoved = "display_removed"
}

/// Closed set of reasons for `capture_ended_by_system_event` events.
enum SystemEventReason: String, Codable {
    case systemSleep = "system_sleep"
    case displaySleep = "display_sleep"
    case screenLocked = "screen_locked"
}

/// An event written as a single JSON line to stdout.
///
/// Discriminator is the `event` field.
enum Event: Equatable {
    case ready
    case permissionRequired(kind: PermissionKind)
    case permissionDenied(kind: PermissionKind)
    case started(startTime: String)
    case sourceAttached(source: SourceKind)
    case sourceLost(source: SourceKind, atOffsetSeconds: Double, reason: String)
    case stopped(durationSeconds: Double, outputPath: String)
    case error(message: String)
    case videoStarted(displayId: Int, widthPx: Int, heightPx: Int, fps: Int)
    case videoLost(atOffsetSeconds: Double, reason: String, message: String)
    case videoFile(path: String, durationSeconds: Double)
    case displayReconfigured(reason: DisplayReconfigurationReason, newDisplayId: Int, newWidthPx: Int, newHeightPx: Int)
    case captureEndedBySystemEvent(reason: SystemEventReason, atOffsetSeconds: Double)

    private enum CodingKeys: String, CodingKey {
        case event
        case kind
        case source
        case startTime = "start_time"
        case atOffsetSeconds = "at_offset_seconds"
        case reason
        case durationSeconds = "duration_seconds"
        case outputPath = "output_path"
        case message
        case displayId = "display_id"
        case widthPx = "width_px"
        case heightPx = "height_px"
        case fps
        case path
        case newDisplayId = "new_display_id"
        case newWidthPx = "new_width_px"
        case newHeightPx = "new_height_px"
    }

    private enum EventKind: String, Codable {
        case ready
        case permissionRequired = "permission_required"
        case permissionDenied = "permission_denied"
        case started
        case sourceAttached = "source_attached"
        case sourceLost = "source_lost"
        case stopped
        case error
        case videoStarted = "video_started"
        case videoLost = "video_lost"
        case videoFile = "video_file"
        case displayReconfigured = "display_reconfigured"
        case captureEndedBySystemEvent = "capture_ended_by_system_event"
    }
}

extension Event: Codable {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try container.decode(EventKind.self, forKey: .event)
        switch kind {
        case .ready:
            self = .ready
        case .permissionRequired:
            let k = try container.decode(PermissionKind.self, forKey: .kind)
            self = .permissionRequired(kind: k)
        case .permissionDenied:
            let k = try container.decode(PermissionKind.self, forKey: .kind)
            self = .permissionDenied(kind: k)
        case .started:
            let t = try container.decode(String.self, forKey: .startTime)
            self = .started(startTime: t)
        case .sourceAttached:
            let s = try container.decode(SourceKind.self, forKey: .source)
            self = .sourceAttached(source: s)
        case .sourceLost:
            let s = try container.decode(SourceKind.self, forKey: .source)
            let off = try container.decode(Double.self, forKey: .atOffsetSeconds)
            let reason = try container.decode(String.self, forKey: .reason)
            self = .sourceLost(source: s, atOffsetSeconds: off, reason: reason)
        case .stopped:
            let d = try container.decode(Double.self, forKey: .durationSeconds)
            let p = try container.decode(String.self, forKey: .outputPath)
            self = .stopped(durationSeconds: d, outputPath: p)
        case .error:
            let m = try container.decode(String.self, forKey: .message)
            self = .error(message: m)
        case .videoStarted:
            let id = try container.decode(Int.self, forKey: .displayId)
            let w = try container.decode(Int.self, forKey: .widthPx)
            let h = try container.decode(Int.self, forKey: .heightPx)
            let f = try container.decode(Int.self, forKey: .fps)
            self = .videoStarted(displayId: id, widthPx: w, heightPx: h, fps: f)
        case .videoLost:
            let off = try container.decode(Double.self, forKey: .atOffsetSeconds)
            let reason = try container.decode(String.self, forKey: .reason)
            let m = try container.decode(String.self, forKey: .message)
            self = .videoLost(atOffsetSeconds: off, reason: reason, message: m)
        case .videoFile:
            let p = try container.decode(String.self, forKey: .path)
            let d = try container.decode(Double.self, forKey: .durationSeconds)
            self = .videoFile(path: p, durationSeconds: d)
        case .displayReconfigured:
            let r = try container.decode(DisplayReconfigurationReason.self, forKey: .reason)
            let id = try container.decode(Int.self, forKey: .newDisplayId)
            let w = try container.decode(Int.self, forKey: .newWidthPx)
            let h = try container.decode(Int.self, forKey: .newHeightPx)
            self = .displayReconfigured(reason: r, newDisplayId: id, newWidthPx: w, newHeightPx: h)
        case .captureEndedBySystemEvent:
            let r = try container.decode(SystemEventReason.self, forKey: .reason)
            let off = try container.decode(Double.self, forKey: .atOffsetSeconds)
            self = .captureEndedBySystemEvent(reason: r, atOffsetSeconds: off)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .ready:
            try container.encode(EventKind.ready, forKey: .event)
        case .permissionRequired(let kind):
            try container.encode(EventKind.permissionRequired, forKey: .event)
            try container.encode(kind, forKey: .kind)
        case .permissionDenied(let kind):
            try container.encode(EventKind.permissionDenied, forKey: .event)
            try container.encode(kind, forKey: .kind)
        case .started(let startTime):
            try container.encode(EventKind.started, forKey: .event)
            try container.encode(startTime, forKey: .startTime)
        case .sourceAttached(let source):
            try container.encode(EventKind.sourceAttached, forKey: .event)
            try container.encode(source, forKey: .source)
        case .sourceLost(let source, let offset, let reason):
            try container.encode(EventKind.sourceLost, forKey: .event)
            try container.encode(source, forKey: .source)
            try container.encode(offset, forKey: .atOffsetSeconds)
            try container.encode(reason, forKey: .reason)
        case .stopped(let duration, let path):
            try container.encode(EventKind.stopped, forKey: .event)
            try container.encode(duration, forKey: .durationSeconds)
            try container.encode(path, forKey: .outputPath)
        case .error(let message):
            try container.encode(EventKind.error, forKey: .event)
            try container.encode(message, forKey: .message)
        case .videoStarted(let displayId, let widthPx, let heightPx, let fps):
            try container.encode(EventKind.videoStarted, forKey: .event)
            try container.encode(displayId, forKey: .displayId)
            try container.encode(widthPx, forKey: .widthPx)
            try container.encode(heightPx, forKey: .heightPx)
            try container.encode(fps, forKey: .fps)
        case .videoLost(let offset, let reason, let message):
            try container.encode(EventKind.videoLost, forKey: .event)
            try container.encode(offset, forKey: .atOffsetSeconds)
            try container.encode(reason, forKey: .reason)
            try container.encode(message, forKey: .message)
        case .videoFile(let path, let duration):
            try container.encode(EventKind.videoFile, forKey: .event)
            try container.encode(path, forKey: .path)
            try container.encode(duration, forKey: .durationSeconds)
        case .displayReconfigured(let reason, let newDisplayId, let newWidthPx, let newHeightPx):
            try container.encode(EventKind.displayReconfigured, forKey: .event)
            try container.encode(reason, forKey: .reason)
            try container.encode(newDisplayId, forKey: .newDisplayId)
            try container.encode(newWidthPx, forKey: .newWidthPx)
            try container.encode(newHeightPx, forKey: .newHeightPx)
        case .captureEndedBySystemEvent(let reason, let offset):
            try container.encode(EventKind.captureEndedBySystemEvent, forKey: .event)
            try container.encode(reason, forKey: .reason)
            try container.encode(offset, forKey: .atOffsetSeconds)
        }
    }
}

// MARK: - Codec helpers

/// JSON-line codec for the IPC protocol.
///
/// `encode(_:)` emits a single-line JSON `String` with no embedded newlines;
/// the caller appends `"\n"` when writing to stdout.
/// `decodeCommand(line:)` parses a single JSON-line command, throwing on malformed input.
enum IPCCodec {
    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        // Default `.sortedKeys`/`.prettyPrinted` are off, which is what we want:
        // compact, single-line output. Key ordering is irrelevant on the wire.
        e.outputFormatting = []
        return e
    }()

    private static let decoder = JSONDecoder()

    /// Serialize an `Event` to a single-line JSON string (no trailing newline).
    static func encode(_ event: Event) throws -> String {
        let data = try encoder.encode(event)
        guard let s = String(data: data, encoding: .utf8) else {
            throw IPCCodecError.invalidUTF8
        }
        return s
    }

    /// Serialize a `Command` to a single-line JSON string (no trailing newline).
    ///
    /// Provided for symmetry with the Python `serialize_command` helper so the
    /// Swift test suite can round-trip the shared command fixtures.
    static func encode(_ command: Command) throws -> String {
        let data = try encoder.encode(command)
        guard let s = String(data: data, encoding: .utf8) else {
            throw IPCCodecError.invalidUTF8
        }
        return s
    }

    /// Parse a single JSON-line into a `Command`. Throws on malformed input
    /// or unknown command discriminator.
    static func decodeCommand(line: String) throws -> Command {
        guard let data = line.data(using: .utf8) else {
            throw IPCCodecError.invalidUTF8
        }
        return try decoder.decode(Command.self, from: data)
    }

    /// Parse a single JSON-line into an `Event`. Symmetric counterpart to
    /// `encode(_ event:)`; used by the test suite to round-trip event fixtures.
    static func decode(eventLine line: String) throws -> Event {
        guard let data = line.data(using: .utf8) else {
            throw IPCCodecError.invalidUTF8
        }
        return try decoder.decode(Event.self, from: data)
    }
}

enum IPCCodecError: Error {
    case invalidUTF8
}
