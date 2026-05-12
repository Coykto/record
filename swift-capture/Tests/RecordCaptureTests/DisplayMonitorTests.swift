import XCTest
import CoreGraphics
import ScreenCaptureKit
@testable import RecordCapture

/// Sanity checks for `DisplayMonitor.resolvePrimary()`.
///
/// The single piece of behavior we want to lock in at the unit level is the
/// pixels-vs-points convention documented in `technical-considerations.md` §3:
/// `widthPx` / `heightPx` must come from `CGDisplayPixelsWide` /
/// `CGDisplayPixelsHigh`, **not** from `SCDisplay.width/height` (which report
/// points and on a Retina display would yield half the pixel count).
///
/// ## Why this test skips on headless CI
///
/// Resolving the primary display requires both (a) an attached display whose
/// id is non-zero and (b) Screen Recording TCC for `SCShareableContent`.
/// Neither is true in a CI runner. `CGMainDisplayID()` returns `0` (kCGNullDirectDisplay)
/// when no display is attached; in that case we skip rather than asserting,
/// matching the convention from `MP4WriterTests` of running where the
/// environment supports the assertion and otherwise yielding.
final class DisplayMonitorTests: XCTestCase {

    /// The pixels-vs-points footgun: assert the values surfaced by
    /// `DisplayMonitor.resolvePrimary()` match `CGDisplayPixelsWide` /
    /// `CGDisplayPixelsHigh` of `CGMainDisplayID()`. This is the same call
    /// `DisplayMonitor.resolvePrimary` uses internally, so the test pins the
    /// **convention** (pixels, not points) rather than a magic constant.
    func testResolvePrimaryReturnsPixelDimensions() async throws {
        let mainID = CGMainDisplayID()
        // `kCGNullDirectDisplay == 0` on a headless runner. We can't call
        // `SCShareableContent.current` either — it would fail with a TCC
        // error. Skip in that environment.
        try XCTSkipUnless(
            mainID != 0,
            "no primary display attached (CGMainDisplayID == 0) — skipping"
        )

        // Skip if Screen Recording TCC isn't granted, otherwise
        // `SCShareableContent.current` would throw and obscure the failure
        // mode we're actually testing.
        try XCTSkipUnless(
            CGPreflightScreenCaptureAccess(),
            "Screen Recording TCC not granted — skipping (this is a sanity test, not a permission test)"
        )

        let expectedWidth = CGDisplayPixelsWide(mainID)
        let expectedHeight = CGDisplayPixelsHigh(mainID)

        let primary = try await DisplayMonitor.resolvePrimary()

        XCTAssertEqual(
            primary.displayID,
            mainID,
            "resolved displayID must equal CGMainDisplayID()"
        )
        XCTAssertEqual(
            primary.widthPx,
            expectedWidth,
            "widthPx must come from CGDisplayPixelsWide (pixels, not points)"
        )
        XCTAssertEqual(
            primary.heightPx,
            expectedHeight,
            "heightPx must come from CGDisplayPixelsHigh (pixels, not points)"
        )
    }
}
