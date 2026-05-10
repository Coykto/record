import XCTest
@testable import RecordCapture

/// Exercises `StateFile.atomicWrite` under concurrent dispatch. The invariant
/// the test enforces: a reader observing the destination URL after the race
/// always sees one of the writers' payloads — never a half-written file,
/// never zero bytes, never a leftover `.tmp` sibling.
final class StateFileTests: XCTestCase {
    private func makeTempDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("record-statefile-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    func testSingleAtomicWriteProducesExactBytes() throws {
        let dir = try makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let target = dir.appendingPathComponent("state.json")

        let payload = Data(#"{"hello":"world"}"#.utf8)
        try StateFile.atomicWrite(payload, to: target)

        let readBack = try Data(contentsOf: target)
        XCTAssertEqual(readBack, payload)
    }

    func testConcurrentWritesProduceOneCompletePayload() throws {
        let dir = try makeTempDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }
        let target = dir.appendingPathComponent("state.json")

        // 50 distinct payloads. After the race, the destination file must
        // contain exactly one of them — no partial data, no truncation.
        let writerCount = 50
        let payloads: [Data] = (0..<writerCount).map { i in
            // Vary length a bit so a torn write would be visible.
            let body = String(repeating: "x", count: 32 + (i % 17))
            let json = #"{"writer":\#(i),"body":"\#(body)"}"#
            return Data(json.utf8)
        }

        DispatchQueue.concurrentPerform(iterations: writerCount) { i in
            do {
                try StateFile.atomicWrite(payloads[i], to: target)
            } catch {
                XCTFail("atomicWrite[\(i)] failed: \(error)")
            }
        }

        // Final contents must match exactly one writer's payload.
        let finalBytes = try Data(contentsOf: target)
        XCTAssertFalse(finalBytes.isEmpty, "atomic write must never leave a zero-byte file")
        XCTAssertTrue(
            payloads.contains(finalBytes),
            "final file contents must exactly equal one of the writers' payloads"
        )

        // And the directory must not be littered with `.tmp` staging siblings.
        let leftovers = try FileManager.default
            .contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasSuffix(".tmp") }
        XCTAssertTrue(leftovers.isEmpty, "no .tmp staging files should remain: \(leftovers)")
    }
}
