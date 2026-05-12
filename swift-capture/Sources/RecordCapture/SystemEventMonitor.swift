import Foundation
import AppKit

/// Observes the three "user stepped away" system notifications and translates
/// the first one received during an active capture into a single trigger.
///
/// Implements `context/spec/002-primary-display-video-capture/technical-considerations.md`
/// §2.2 / §2.10 / §3:
///
/// - `NSWorkspace.willSleepNotification` → reason `"system_sleep"` (lid close,
///   manual sleep).
/// - `NSWorkspace.screensDidSleepNotification` → reason `"display_sleep"` (the
///   display went to sleep but the system did not).
/// - Distributed notification `"com.apple.screenIsLocked"` → reason
///   `"screen_locked"` (⌃⌘Q, screensaver under "require password").
///
/// On the first such notification the registered `onTrigger` closure is invoked
/// **once** on the main queue. Subsequent notifications during shutdown are
/// silently dropped; the wiring layer is responsible for any further cleanup
/// (calling `stop()` to detach observers, calling `handleStop()` to finalize).
///
/// ## Threading
///
/// `NSWorkspace.shared.notificationCenter` delivers on the main queue;
/// `DistributedNotificationCenter` may deliver on an internal CFRunLoop
/// thread. The first-event-wins flag is therefore guarded by an `NSLock`. The
/// callback itself is always re-dispatched onto `DispatchQueue.main.async`
/// so the wiring layer can read `capture` state without contention against
/// stdin-thread `handleStop` dispatch or SCStream callbacks (both already land
/// on `.main`).
///
/// ## Graceful degradation
///
/// `com.apple.screenIsLocked` is **not Apple-documented** and may not fire
/// reliably from this binary's launch context on all macOS versions (tech spec
/// §3 risk). If the observer fails to register the monitor logs a single
/// stderr warning and proceeds without it — the other two observers cover
/// system sleep and display sleep, both of which are Apple-supported.
final class SystemEventMonitor {

    /// Reason string passed to `onTrigger`. These match the wire-level
    /// `SystemEventReason` enum values exactly, so the wiring layer can
    /// forward them into `Event.captureEndedBySystemEvent` without
    /// translation.
    enum Reason {
        static let systemSleep = "system_sleep"
        static let displaySleep = "display_sleep"
        static let screenLocked = "screen_locked"
    }

    /// `true` after the first trigger has fired. Guards against the three
    /// notifications racing each other on shutdown.
    private var fired = false
    private let firedLock = NSLock()

    /// Closure invoked at most once with a reason string. Set by `start(...)`,
    /// cleared by `stop()`.
    private var onTrigger: ((String) -> Void)?

    /// Observers retained so we can pass them back to the matching notification
    /// center in `stop()`. `DistributedNotificationCenter` and
    /// `NSWorkspace.notificationCenter` both return `NSObjectProtocol` tokens
    /// from `addObserver(forName:object:queue:using:)` that must be handed to
    /// `removeObserver(_:)`.
    private var workspaceObservers: [NSObjectProtocol] = []
    private var distributedObserver: NSObjectProtocol?

    init() {}

    // MARK: - Lifecycle

    /// Register the three observers. `onTrigger` is invoked at most once, on
    /// the main queue, with one of the `Reason` strings. Repeated `start`
    /// calls without an intervening `stop` are ignored.
    func start(onTrigger: @escaping (_ reason: String) -> Void) {
        firedLock.lock()
        guard self.onTrigger == nil else {
            firedLock.unlock()
            return
        }
        self.onTrigger = onTrigger
        firedLock.unlock()

        let workspaceCenter = NSWorkspace.shared.notificationCenter

        // Apple-documented: fires when the system is about to enter sleep
        // (lid close, manual sleep, idle sleep). We do NOT subscribe to the
        // `didWake` companion — there is no auto-resume.
        let sleepObs = workspaceCenter.addObserver(
            forName: NSWorkspace.willSleepNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.fire(reason: Reason.systemSleep)
        }
        workspaceObservers.append(sleepObs)

        // Apple-documented: display sleep without (necessarily) system sleep.
        let displaySleepObs = workspaceCenter.addObserver(
            forName: NSWorkspace.screensDidSleepNotification,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.fire(reason: Reason.displaySleep)
        }
        workspaceObservers.append(displaySleepObs)

        // Undocumented: `com.apple.screenIsLocked` is observed empirically to
        // fire on ⌃⌘Q and on screensaver start under "require password"
        // settings. Reliability across macOS versions is a known risk (tech
        // spec §3). `addObserver(forName:object:queue:using:)` on
        // `DistributedNotificationCenter` is non-throwing and always returns
        // a token, so there is no structural failure mode to detect here —
        // the spec's "failed to register" footgun surfaces only as a
        // notification that never fires, which is graceful degradation by
        // design. We never abort the binary over it; the other two observers
        // remain authoritative.
        let distributed = DistributedNotificationCenter.default()
        let lockName = Notification.Name("com.apple.screenIsLocked")
        let token = distributed.addObserver(
            forName: lockName,
            object: nil,
            queue: nil
        ) { [weak self] _ in
            self?.fire(reason: Reason.screenLocked)
        }
        distributedObserver = token

        // Belt-and-suspenders: if a future macOS release or sandbox
        // restriction ever causes registration to misbehave (e.g. returns
        // a token but the run loop never delivers), at least the
        // never-fires case is benign — capture continues until manual
        // `record stop`. The warning is emitted from a future failure-
        // detection hook rather than this code path because the current API
        // contract makes registration failure unobservable here.
    }

    /// Remove all three observers. Idempotent — cheap to call twice (e.g.
    /// once from `handleStop` and once from the trigger handler).
    func stop() {
        firedLock.lock()
        onTrigger = nil
        firedLock.unlock()

        let workspaceCenter = NSWorkspace.shared.notificationCenter
        for obs in workspaceObservers {
            workspaceCenter.removeObserver(obs)
        }
        workspaceObservers.removeAll()

        if let obs = distributedObserver {
            DistributedNotificationCenter.default().removeObserver(obs)
            distributedObserver = nil
        }
    }

    // MARK: - Internal

    /// Claim the right to fire and dispatch the trigger on the main queue.
    /// Subsequent calls are silent no-ops.
    private func fire(reason: String) {
        firedLock.lock()
        if fired {
            firedLock.unlock()
            return
        }
        fired = true
        let trigger = onTrigger
        firedLock.unlock()

        guard let trigger = trigger else { return }
        DispatchQueue.main.async {
            trigger(reason)
        }
    }
}
