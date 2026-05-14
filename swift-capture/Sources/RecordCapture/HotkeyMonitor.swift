import Foundation
import Carbon.HIToolbox
import ApplicationServices

// MARK: - Translation tables

/// Translate a list of canonical modifier names (matching `HotkeyModifier`
/// raw values) into a Carbon modifier mask suitable for
/// `RegisterEventHotKey`. Order in the input is irrelevant — the mask is
/// commutative under OR.
///
/// Centralised as a free function so unit tests can exercise the mapping
/// without booting Carbon (which requires Accessibility TCC).
func modifierMask(from modifiers: [HotkeyModifier]) -> UInt32 {
    var mask: UInt32 = 0
    for m in modifiers {
        switch m {
        case .cmd:     mask |= UInt32(cmdKey)
        case .option:  mask |= UInt32(optionKey)
        case .control: mask |= UInt32(controlKey)
        case .shift:   mask |= UInt32(shiftKey)
        }
    }
    return mask
}

/// Translate a key string from the closed grammar (`a`-`z`, `0`-`9`,
/// `f1`-`f20`, plus the named keys `space`, `tab`, `return`, `escape`,
/// `delete`) into the US ANSI virtual keycode used by Carbon's
/// `RegisterEventHotKey`. Returns `nil` for any unknown key — the caller
/// is expected to surface `unknown_key:<key>` to the orchestrator.
///
/// Lookup is case-sensitive on the lower-cased input by design: the
/// orchestrator already canonicalises hotkeys via `src/record/hotkey.py`,
/// which lower-cases everything before it reaches the wire.
func keyCode(for key: String) -> UInt32? {
    switch key {
    // Letters — non-sequential in the Carbon virtual keycode table.
    case "a": return UInt32(kVK_ANSI_A)
    case "b": return UInt32(kVK_ANSI_B)
    case "c": return UInt32(kVK_ANSI_C)
    case "d": return UInt32(kVK_ANSI_D)
    case "e": return UInt32(kVK_ANSI_E)
    case "f": return UInt32(kVK_ANSI_F)
    case "g": return UInt32(kVK_ANSI_G)
    case "h": return UInt32(kVK_ANSI_H)
    case "i": return UInt32(kVK_ANSI_I)
    case "j": return UInt32(kVK_ANSI_J)
    case "k": return UInt32(kVK_ANSI_K)
    case "l": return UInt32(kVK_ANSI_L)
    case "m": return UInt32(kVK_ANSI_M)
    case "n": return UInt32(kVK_ANSI_N)
    case "o": return UInt32(kVK_ANSI_O)
    case "p": return UInt32(kVK_ANSI_P)
    case "q": return UInt32(kVK_ANSI_Q)
    case "r": return UInt32(kVK_ANSI_R)
    case "s": return UInt32(kVK_ANSI_S)
    case "t": return UInt32(kVK_ANSI_T)
    case "u": return UInt32(kVK_ANSI_U)
    case "v": return UInt32(kVK_ANSI_V)
    case "w": return UInt32(kVK_ANSI_W)
    case "x": return UInt32(kVK_ANSI_X)
    case "y": return UInt32(kVK_ANSI_Y)
    case "z": return UInt32(kVK_ANSI_Z)
    // Digits.
    case "0": return UInt32(kVK_ANSI_0)
    case "1": return UInt32(kVK_ANSI_1)
    case "2": return UInt32(kVK_ANSI_2)
    case "3": return UInt32(kVK_ANSI_3)
    case "4": return UInt32(kVK_ANSI_4)
    case "5": return UInt32(kVK_ANSI_5)
    case "6": return UInt32(kVK_ANSI_6)
    case "7": return UInt32(kVK_ANSI_7)
    case "8": return UInt32(kVK_ANSI_8)
    case "9": return UInt32(kVK_ANSI_9)
    // Function keys.
    case "f1":  return UInt32(kVK_F1)
    case "f2":  return UInt32(kVK_F2)
    case "f3":  return UInt32(kVK_F3)
    case "f4":  return UInt32(kVK_F4)
    case "f5":  return UInt32(kVK_F5)
    case "f6":  return UInt32(kVK_F6)
    case "f7":  return UInt32(kVK_F7)
    case "f8":  return UInt32(kVK_F8)
    case "f9":  return UInt32(kVK_F9)
    case "f10": return UInt32(kVK_F10)
    case "f11": return UInt32(kVK_F11)
    case "f12": return UInt32(kVK_F12)
    case "f13": return UInt32(kVK_F13)
    case "f14": return UInt32(kVK_F14)
    case "f15": return UInt32(kVK_F15)
    case "f16": return UInt32(kVK_F16)
    case "f17": return UInt32(kVK_F17)
    case "f18": return UInt32(kVK_F18)
    case "f19": return UInt32(kVK_F19)
    case "f20": return UInt32(kVK_F20)
    // Named keys.
    case "space":  return UInt32(kVK_Space)
    case "tab":    return UInt32(kVK_Tab)
    case "return": return UInt32(kVK_Return)
    case "escape": return UInt32(kVK_Escape)
    case "delete": return UInt32(kVK_Delete)
    default:
        return nil
    }
}

// MARK: - Carbon event handler glue

/// Module-level holder for the singleton `HotkeyMonitor`. The Carbon event
/// handler is a C function pointer with no captured state, so it needs a
/// well-known place to find the press callback. We do not support more
/// than one active `HotkeyMonitor` at a time — the daemon constructs a
/// single instance for its lifetime.
private var sharedMonitor: HotkeyMonitor?

/// Static C-compatible callback. Carbon hands us an `EventRef`; we don't
/// need its details — only that a registered hot key fired. We forward to
/// the shared monitor's `onPress`, dispatched onto the main queue.
private func hotkeyEventHandler(
    _ nextHandler: EventHandlerCallRef?,
    _ event: EventRef?,
    _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    if let onPress = sharedMonitor?.onPress {
        DispatchQueue.main.async {
            onPress()
        }
    }
    return noErr
}

// MARK: - HotkeyMonitor

/// Thin wrapper around Carbon's `RegisterEventHotKey` / `InstallEventHandler`
/// pair. Tech spec §2.12 commits us to Carbon (not `NSEvent` global
/// monitors) because Carbon hotkeys do NOT require Accessibility TCC at
/// registration time — `AXIsProcessTrusted()` is consulted defensively
/// here so the daemon can surface a clean `accessibility_denied` to the
/// orchestrator before touching Carbon. (In practice macOS often delivers
/// the press events even without Accessibility, but we honour the
/// product-level requirement to fail loud rather than appear broken.)
final class HotkeyMonitor {
    /// Closed result of `register(modifiers:keyCode:)`.
    enum RegistrationResult: Equatable {
        case registered
        /// `OSStatus == eventHotKeyExistsErr` (-9878). Another process has
        /// claimed the same global hotkey; the orchestrator should surface
        /// it as a conflict and prompt the user to choose a different
        /// chord.
        case conflict
        /// Anything else. The `message` is a stable machine-readable token
        /// so the orchestrator can map it onto user-facing copy:
        ///   - `"accessibility_denied"`     `AXIsProcessTrusted()` was false
        ///   - `"param_err"`                Carbon `paramErr` (-50)
        ///   - `"no_modifiers"`             empty modifier list (FR 2.6)
        ///   - `"unknown_key:<key>"`        key not in the closed grammar
        ///   - `"unknown_osstatus_<code>"`  catch-all
        case invalid(message: String)
    }

    /// Carbon's `eventHotKeyExistsErr` is `OSStatus(-9878)`. Some SDK
    /// vintages do not expose the symbol from Swift; the numeric value is
    /// documented and stable.
    private static let eventHotKeyExistsErrValue: OSStatus = -9878
    /// Carbon's `paramErr` is `OSStatus(-50)`.
    private static let paramErrValue: OSStatus = -50

    /// User callback. Invoked on the main queue every time a registered
    /// hot key fires.
    let onPress: () -> Void

    /// Active Carbon ref, non-nil between `register()` success and
    /// `unregister()`.
    private var hotKeyRef: EventHotKeyRef?

    /// Has the shared Carbon event handler been installed yet? We install
    /// it lazily on the first `register()` and leave it installed for the
    /// process lifetime — Carbon's handler dispatch is a no-op when no
    /// hot keys are registered, so there is no benefit to tearing it down
    /// on `unregister()`.
    private var handlerInstalled: Bool = false
    private var handlerRef: EventHandlerRef?

    init(onPress: @escaping () -> Void) {
        self.onPress = onPress
        // Single-instance contract: the C event handler looks up the
        // shared monitor by module-level pointer. Setting it here means
        // the most-recently-constructed monitor wins; the daemon only
        // ever constructs one.
        sharedMonitor = self
    }

    deinit {
        unregister()
        if sharedMonitor === self {
            sharedMonitor = nil
        }
    }

    /// Register a hot key. Calling twice in a row first unregisters the
    /// previous binding so we never leak a Carbon ref.
    func register(modifiers: UInt32, keyCode: UInt32) -> RegistrationResult {
        // Idempotent w.r.t. a previous registration on this instance —
        // tear it down first so the caller can re-bind freely.
        unregister()

        // Defensive: tech spec §2.12 row 4 says we surface
        // `accessibility_denied` if AX is not trusted, even though Carbon
        // hotkeys themselves do not require it. This lets the daemon
        // prompt the user for the same TCC grant the eventual focus /
        // window-name probing will need later.
        if !AXIsProcessTrusted() {
            return .invalid(message: "accessibility_denied")
        }

        // Install the shared Carbon handler on first use. The handler is
        // a single C function pointer; it forwards to `sharedMonitor`.
        if !handlerInstalled {
            var spec = EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: UInt32(kEventHotKeyPressed)
            )
            let status = InstallEventHandler(
                GetApplicationEventTarget(),
                hotkeyEventHandler,
                1,
                &spec,
                nil,
                &handlerRef
            )
            if status != noErr {
                return .invalid(message: "unknown_osstatus_\(status)")
            }
            handlerInstalled = true
        }

        // Signature is a four-char-code Carbon uses to identify the
        // registrant; the exact value is irrelevant as long as it's
        // stable for our process. Compute "rcrd" via bit-shifting so we
        // don't depend on any optional `NSString` extensions.
        let signature: OSType =
            (OSType(UInt8(ascii: "r")) << 24) |
            (OSType(UInt8(ascii: "c")) << 16) |
            (OSType(UInt8(ascii: "r")) << 8)  |
             OSType(UInt8(ascii: "d"))
        let hotKeyID = EventHotKeyID(signature: signature, id: 1)

        var ref: EventHotKeyRef?
        let status = RegisterEventHotKey(
            keyCode,
            modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &ref
        )
        switch status {
        case noErr:
            hotKeyRef = ref
            return .registered
        case Self.eventHotKeyExistsErrValue:
            return .conflict
        case Self.paramErrValue:
            return .invalid(message: "param_err")
        default:
            return .invalid(message: "unknown_osstatus_\(status)")
        }
    }

    /// Drop the active Carbon registration, if any. Idempotent. The
    /// shared event handler is intentionally left installed — it's a
    /// no-op when no hot keys are registered, and re-installing on each
    /// `register()` would risk a leak if a future caller forgot to pair
    /// the calls.
    func unregister() {
        if let ref = hotKeyRef {
            UnregisterEventHotKey(ref)
            hotKeyRef = nil
        }
    }
}
