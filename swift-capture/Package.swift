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
            path: "Sources/RecordCapture"
        )
    ]
)
