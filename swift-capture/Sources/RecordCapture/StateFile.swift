import Foundation

/// Atomic-write helper for tiny state files.
///
/// Mirrors the contract of the Python orchestrator's `state.write_state`: a
/// reader (or another writer) racing against this call will either see the
/// previous file contents or the new complete contents, never a half-written
/// file and never a missing file. Achieved by writing to a sibling temp file
/// (in the same directory, so the rename stays on the same filesystem) and
/// then performing an atomic `replaceItem` rename.
enum StateFile {
    /// Write `data` to `url` atomically. Safe under concurrent invocation from
    /// multiple threads/queues; the last writer to win the rename determines
    /// the final contents.
    static func atomicWrite(_ data: Data, to url: URL) throws {
        let directory = url.deletingLastPathComponent()
        // Unique temp sibling so concurrent writers don't clobber each other's
        // staging files before the rename step.
        let tempName = ".\(url.lastPathComponent).\(ProcessInfo.processInfo.processIdentifier).\(UUID().uuidString).tmp"
        let tempURL = directory.appendingPathComponent(tempName)
        try data.write(to: tempURL, options: .atomic)
        do {
            _ = try FileManager.default.replaceItemAt(url, withItemAt: tempURL)
        } catch {
            // Best-effort cleanup if the rename failed; swallow cleanup errors.
            try? FileManager.default.removeItem(at: tempURL)
            throw error
        }
    }
}
