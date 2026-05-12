import Foundation
import ScreenCaptureKit
import CoreGraphics

/// Errors thrown while resolving the primary display.
enum DisplayMonitorError: Error, CustomStringConvertible {
    case primaryDisplayNotFound

    var description: String {
        switch self {
        case .primaryDisplayNotFound:
            return "primary display not found in SCShareableContent"
        }
    }
}

/// Snapshot of the primary display at the moment capture starts.
///
/// `widthPx` / `heightPx` are **pixels**, derived from `CGDisplayPixelsWide` /
/// `CGDisplayPixelsHigh`. The `SCDisplay.width` / `.height` properties report
/// **points** — using them for `SCStreamConfiguration.width` produces a
/// half-resolution capture on Retina displays. This is documented as a Retina
/// half-res footgun in the spec; keep this struct's accessors honest by sourcing
/// pixel dimensions exclusively from `CGDisplayPixelsWide` / `CGDisplayPixelsHigh`.
struct PrimaryDisplay {
    let scDisplay: SCDisplay
    let displayID: CGDirectDisplayID
    let widthPx: Int
    let heightPx: Int
}

/// Resolves the macOS primary display and exposes its pixel dimensions for
/// downstream `SCStream` configuration.
///
/// The reconfiguration-callback machinery lives in
/// `DisplayReconfigurationMonitor` below; this enum is intentionally a thin
/// lookup helper so it can be called from `main.swift` (initial resolve) and
/// from the reconfiguration monitor (re-resolve after a CG event) without
/// either side owning instance state.
enum DisplayMonitor {

    /// Look up the primary `SCDisplay` and read its pixel dimensions.
    ///
    /// `SCShareableContent.current` triggers the Screen Recording TCC prompt
    /// the first time it's called in a process. Callers are expected to have
    /// preflighted Screen Recording permission already (`AudioCapture`
    /// currently does this in its `checkPermissions()` path), so this should
    /// not be the call that prompts.
    static func resolvePrimary() async throws -> PrimaryDisplay {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: true
        )
        let mainID = CGMainDisplayID()
        guard let display = content.displays.first(where: { $0.displayID == mainID }) else {
            throw DisplayMonitorError.primaryDisplayNotFound
        }
        // Pixels, not points — see the `PrimaryDisplay` doc comment.
        let widthPx = CGDisplayPixelsWide(mainID)
        let heightPx = CGDisplayPixelsHigh(mainID)
        return PrimaryDisplay(
            scDisplay: display,
            displayID: mainID,
            widthPx: widthPx,
            heightPx: heightPx
        )
    }
}

// MARK: - Reconfiguration monitor

/// Watches `CGDisplayRegisterReconfigurationCallback` for the lifetime of an
/// active capture and notifies a handler when the captured display (or the
/// primary display) is affected by a reconfig.
///
/// ## Why this is its own class
///
/// `CGDisplayRegisterReconfigurationCallback` is a C API that takes a function
/// pointer + an opaque `userInfo` pointer; the natural Swift wrapper is a
/// class instance whose lifetime spans `start()` → `stop()`. The instance
/// pointer is threaded through `userInfo` via `Unmanaged.passUnretained` —
/// see the discussion on `start(initialDisplayID:)`.
///
/// ## CG callback semantics this class handles
///
/// CG fires the callback **twice** per logical reconfig — once with just
/// `beginConfigurationFlag` set (before the change), then again with the
/// actual change flags set (after). We early-return on the begin pulse and
/// only act on the trailing one.
///
/// ## Flag-to-reason priority
///
/// A single reconfig commonly sets multiple flags at once. Removing an
/// external primary display, for example, fires one callback against the
/// removed display with `.removeFlag` set and another against the laptop
/// display with `.setMainFlag` set; sometimes both come bundled. The priority
/// `displayRemoved > primaryChanged > resolutionChanged` reflects "what most
/// changes the capture surface": a removed display invalidates the SCStream
/// entirely; a primary change forces a different display; a mode change
/// keeps the same display but at new dimensions.
final class DisplayReconfigurationMonitor: @unchecked Sendable {

    /// Closure invoked on the main queue after a relevant reconfig has been
    /// observed and the new primary display has been re-resolved. The handler
    /// is responsible for driving `VideoCapture.reconfigure(...)` and for
    /// telling this monitor about the new captured display ID via
    /// `updateCapturedDisplayID(_:)` once the rebuild succeeds.
    var onReconfigure: ((DisplayReconfigurationReason, PrimaryDisplay) -> Void)?

    /// Optional sink for diagnostic events (e.g. failed re-resolution). The
    /// monitor itself never emits over the IPC protocol; the wiring layer
    /// supplies an `emit` shim if it wants visibility.
    var onError: ((String) -> Void)?

    /// Currently-captured display ID. Read from the CG callback thread, written
    /// from `start(initialDisplayID:)` and `updateCapturedDisplayID(_:)`.
    private var capturedDisplayID: CGDirectDisplayID = 0
    private let stateLock = NSLock()

    /// `true` between `start()` and `stop()`. Guards against late-arriving CG
    /// callbacks delivered after `stop()` has unregistered (CG documents that
    /// callbacks can still fire briefly during teardown).
    private var active: Bool = false

    init() {}

    // MARK: - Lifecycle

    /// Register the CG reconfiguration callback. `initialDisplayID` is the
    /// display currently being captured; the monitor uses it to ignore
    /// reconfigs that don't touch our capture surface.
    func start(initialDisplayID: CGDirectDisplayID) {
        stateLock.lock()
        guard !active else {
            stateLock.unlock()
            return
        }
        capturedDisplayID = initialDisplayID
        active = true
        stateLock.unlock()

        // `passUnretained` is safe because this monitor outlives its
        // callback registration — `stop()` unregisters the callback before
        // the monitor is allowed to go out of scope. Retaining inside the
        // callback would create a reference cycle with `CaptureState`.
        let context = Unmanaged.passUnretained(self).toOpaque()
        CGDisplayRegisterReconfigurationCallback(reconfigurationCallback, context)
    }

    /// Unregister the CG callback. Idempotent — subsequent calls are no-ops.
    func stop() {
        stateLock.lock()
        guard active else {
            stateLock.unlock()
            return
        }
        active = false
        stateLock.unlock()

        let context = Unmanaged.passUnretained(self).toOpaque()
        CGDisplayRemoveReconfigurationCallback(reconfigurationCallback, context)
    }

    /// Update the display ID treated as "the one we're currently capturing".
    /// Called by the wiring layer after a successful `VideoCapture.reconfigure`.
    func updateCapturedDisplayID(_ newID: CGDirectDisplayID) {
        stateLock.lock()
        capturedDisplayID = newID
        stateLock.unlock()
    }

    // MARK: - Callback dispatch

    /// Called from the C trampoline. Runs on a CG-internal thread.
    fileprivate func handleReconfiguration(
        displayID: CGDirectDisplayID,
        flags: CGDisplayChangeSummaryFlags
    ) {
        // Debounce the documented dual-callback pattern: CG fires once with
        // `beginConfigurationFlag` *before* the change and again with the real
        // flags *after*. Only the trailing callback carries actionable info.
        if flags.contains(.beginConfigurationFlag) {
            return
        }

        // Snapshot state we need without holding the lock across the rest of
        // the work.
        stateLock.lock()
        let isActive = active
        let captured = capturedDisplayID
        stateLock.unlock()

        guard isActive else { return }

        // Filter: only act when this reconfig touches the captured display,
        // OR when the primary has just changed (we'll need to switch to it),
        // OR when our captured display was removed. The third case is already
        // covered by "displayID == captured" since CG fires the removal
        // callback against the removed display's ID.
        let touchesCaptured = (displayID == captured)
        let primaryChanged = flags.contains(.setMainFlag)
        guard touchesCaptured || primaryChanged else { return }

        // Flag-to-reason mapping: pick the highest-priority change present.
        // Justification for this order is in the class doc comment.
        let reason: DisplayReconfigurationReason
        if flags.contains(.removeFlag) {
            reason = .displayRemoved
        } else if flags.contains(.setMainFlag) {
            reason = .primaryChanged
        } else if flags.contains(.setModeFlag) {
            reason = .resolutionChanged
        } else {
            // No flag we care about (e.g. mirror toggle, rotation-only on a
            // non-primary display). Ignore.
            return
        }

        // Bridge from the sync CG callback into async land to re-resolve the
        // new primary display. The resulting handler call is delivered on the
        // main queue so the wiring layer's `Task { await reconfigure(...) }`
        // runs from a stable execution context.
        let weakSelf = self
        Task {
            do {
                let primary = try await DisplayMonitor.resolvePrimary()
                DispatchQueue.main.async { [weak weakSelf] in
                    guard let handler = weakSelf?.onReconfigure else { return }
                    handler(reason, primary)
                }
            } catch {
                DispatchQueue.main.async { [weak weakSelf] in
                    weakSelf?.onError?("display reconfig: failed to re-resolve primary: \(error)")
                }
            }
        }
    }
}

// MARK: - C trampoline

/// Free function so we can pass it as a C function pointer. Decodes the
/// `userInfo` pointer back to the Swift instance and forwards to its handler.
private func reconfigurationCallback(
    display: CGDirectDisplayID,
    flags: CGDisplayChangeSummaryFlags,
    userInfo: UnsafeMutableRawPointer?
) {
    guard let userInfo = userInfo else { return }
    let monitor = Unmanaged<DisplayReconfigurationMonitor>
        .fromOpaque(userInfo)
        .takeUnretainedValue()
    monitor.handleReconfiguration(displayID: display, flags: flags)
}
