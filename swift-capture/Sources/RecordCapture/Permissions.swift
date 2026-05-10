import Foundation
import ScreenCaptureKit
import CoreGraphics
import AVFoundation

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
}
