// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "RecordCapture",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "record-capture", targets: ["RecordCapture"])
    ],
    targets: [
        .executableTarget(
            name: "RecordCapture",
            path: "Sources/RecordCapture",
            linkerSettings: [
                // Embed an Info.plist into the bare executable's __TEXT
                // segment. Required so AVCaptureDevice.requestAccess(for:
                // .audio) can show the microphone prompt — without
                // NSMicrophoneUsageDescription the request fails fast — and
                // so the binary carries a stable CFBundleIdentifier for TCC
                // to key permission grants on. The linker resolves this path
                // relative to the package directory (swift-capture/).
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Info.plist",
                ])
            ]
        ),
        .testTarget(
            name: "RecordCaptureTests",
            dependencies: ["RecordCapture"],
            path: "Tests/RecordCaptureTests",
            resources: [.copy("Fixtures")]
        )
    ]
)
