import Foundation
import ScreenCaptureKit
import CoreGraphics
import AVFoundation
import ApplicationServices

/// Permission preflight + prompt orchestration for the capture daemon.
///
/// ScreenCaptureKit doesn't expose a tri-state authorization status the way
/// `AVCaptureDevice.authorizationStatus(for:)` does. The available signals are:
///
///   - `CGPreflightScreenCaptureAccess()` — returns `true` only when access is
///     already granted. Returns `false` for both not-determined and denied.
///   - `SCShareableContent.current` — succeeds when access is granted; the very
///     first call from a process triggers the system prompt; throws when denied.
///
/// The strategy here: preflight first; if not yet granted, emit
/// `permission_required`, attempt `SCShareableContent.current` to trigger the
/// prompt, then re-preflight. If the second preflight is still false (or the
/// SCK call threw), emit `permission_denied`.
enum Permissions {

    /// Check (and, if needed, request) Screen Recording permission.
    ///
    /// Returns `true` when access is granted by the time the function returns;
    /// `false` when the user denied the prompt or the permission is not granted
    /// for some other reason.
    ///
    /// Emits at most one `permission_required` and at most one
    /// `permission_denied` event via the supplied closure.
    static func checkScreenRecording(emit: (Event) -> Void) async -> Bool {
        if CGPreflightScreenCaptureAccess() {
            return true
        }

        // Either not-determined or denied. We can't tell which without making
        // the SCK call, so announce the requirement and let SCK either prompt
        // or fail.
        emit(.permissionRequired(kind: .screenRecording))

        do {
            // Triggers the macOS Screen Recording prompt the first time it's
            // called from this process. On a denied state, throws.
            _ = try await SCShareableContent.current
        } catch {
            emit(.permissionDenied(kind: .screenRecording))
            return false
        }

        // The prompt may have been granted, dismissed, or denied. Re-preflight
        // to learn the post-prompt state authoritatively.
        if CGPreflightScreenCaptureAccess() {
            return true
        }

        emit(.permissionDenied(kind: .screenRecording))
        return false
    }

    /// Check (and, if needed, request) Microphone permission.
    ///
    /// Returns `true` when access is granted by the time the function returns;
    /// `false` when the user denied the prompt, the permission is restricted by
    /// MDM/parental-controls, or otherwise not granted.
    ///
    /// Emits at most one `permission_required` and at most one
    /// `permission_denied` event via the supplied closure.
    static func checkMicrophone(emit: (Event) -> Void) async -> Bool {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        switch status {
        case .authorized:
            return true
        case .notDetermined:
            emit(.permissionRequired(kind: .microphone))
            // `requestAccess(for:)` is a completion-handler API; bridge into
            // async via a checked continuation. The completion handler runs
            // exactly once, so the continuation is resumed exactly once.
            //
            // NOTE: macOS only presents the prompt when this process is in a
            // terminal-rooted process tree. A launchd-spawned daemon cannot
            // show TCC UI — `requestAccess` returns false immediately there.
            // `record install` primes the grant via `--prime-permissions`
            // before bootstrapping the LaunchAgent for exactly this reason.
            let granted: Bool = await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .audio) { allowed in
                    continuation.resume(returning: allowed)
                }
            }
            if granted {
                return true
            }
            emit(.permissionDenied(kind: .microphone))
            return false
        case .denied, .restricted:
            emit(.permissionDenied(kind: .microphone))
            return false
        @unknown default:
            emit(.permissionDenied(kind: .microphone))
            return false
        }
    }

    /// Run the full permission-priming sequence for `record install`.
    ///
    /// Each permission has different in-process observability, so each is
    /// handled differently:
    ///
    ///   - **Microphone** — `requestAccess`'s prompt is *modal*: the
    ///     completion handler blocks until the user answers, and the result
    ///     is accurate. Handled by `checkMicrophone`.
    ///   - **Accessibility** — `AXIsProcessTrusted()` reflects a fresh grant
    ///     *live* within the running process, so `primePollable` triggers the
    ///     prompt and polls, continuing the instant the user toggles it.
    ///   - **Screen Recording** — `CGPreflightScreenCaptureAccess()` is cached
    ///     at process launch and never updates in-process. Polling it is
    ///     futile (it always times out, even after a real grant), so
    ///     `primeScreenRecording` only triggers the prompt and holds briefly;
    ///     the *daemon's* fresh Swift child is what actually observes the
    ///     grant.
    ///
    /// Permissions are primed strictly one at a time: macOS only surfaces one
    /// "Open System Settings"-style prompt at a time, so firing them together
    /// silently suppresses all but the first.
    ///
    /// Returns `true` only when all three permissions are granted (Screen
    /// Recording per its *initial*, just-launched status).
    static func prime(emit: (Event) -> Void) async -> Bool {
        let micGranted = await checkMicrophone(emit: emit)
        let axGranted = await primePollable(
            emit: emit,
            kind: .accessibility,
            isGranted: { AXIsProcessTrusted() },
            trigger: {
                let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue()
                _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
            }
        )
        let screenGranted = await primeScreenRecording(emit: emit)
        return micGranted && axGranted && screenGranted
    }

    /// Trigger a permission whose status updates *live* in-process, then poll
    /// (twice a second) until it is granted or a deadline elapses. Staying
    /// alive while polling also keeps the prompt's "Open System Settings"
    /// button working — exiting orphans the prompt.
    private static func primePollable(
        emit: (Event) -> Void,
        kind: PermissionKind,
        isGranted: () -> Bool,
        trigger: () async -> Void
    ) async -> Bool {
        let label = kind.rawValue.replacingOccurrences(of: "_", with: " ")
        if isGranted() {
            return true
        }
        emit(.permissionRequired(kind: kind))
        await trigger()
        // `[prime]` stderr lines are surfaced live by `record install`.
        let deadline = Date().addingTimeInterval(45)
        while Date() < deadline {
            if isGranted() {
                FileHandle.standardError.write(
                    Data("[prime] \(label): granted\n".utf8)
                )
                return true
            }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        FileHandle.standardError.write(
            Data("[prime] \(label): not granted (timed out)\n".utf8)
        )
        emit(.permissionDenied(kind: kind))
        return false
    }

    /// Trigger the Screen Recording prompt, then poll until the grant lands.
    ///
    /// `CGPreflightScreenCaptureAccess()` caches at process launch and never
    /// refreshes, so polling it *in-process* is futile — it would always time
    /// out even after a real grant. Instead each poll tick spawns a *fresh*
    /// copy of this binary in `--check-screen-recording` mode: a newly
    /// launched process reads the current TCC state, so a grant made after
    /// priming started is genuinely observable. This means we only ever
    /// report "not granted" when a fresh check actually confirms it.
    private static func primeScreenRecording(emit: (Event) -> Void) async -> Bool {
        if CGPreflightScreenCaptureAccess() {
            return true
        }
        emit(.permissionRequired(kind: .screenRecording))
        // Shows the prompt and adds the binary to the Screen Recording list.
        _ = CGRequestScreenCaptureAccess()
        FileHandle.standardError.write(Data(
            "[prime] screen recording: toggle record-capture ON in System Settings...\n".utf8
        ))
        // Generous window — the user has to navigate System Settings — but the
        // loop exits within ~1.5s of the grant landing.
        let deadline = Date().addingTimeInterval(120)
        while Date() < deadline {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if freshScreenRecordingCheck() {
                FileHandle.standardError.write(
                    Data("[prime] screen recording: granted\n".utf8)
                )
                return true
            }
        }
        FileHandle.standardError.write(
            Data("[prime] screen recording: not granted (timed out)\n".utf8)
        )
        emit(.permissionDenied(kind: .screenRecording))
        return false
    }

    /// Spawn a fresh copy of this binary in `--check-screen-recording` mode
    /// and return whether it reported Screen Recording as granted. Each spawn
    /// reads the current TCC state at its own launch — the only way to observe
    /// a grant made after the long-lived priming process started.
    private static func freshScreenRecordingCheck() -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        proc.arguments = ["--check-screen-recording"]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            proc.waitUntilExit()
            return proc.terminationStatus == 0
        } catch {
            return false
        }
    }
}
