// swift-tools-version:6.0
// PISA contract 2, Swift twin of the conformance kit: the AG-UI 1.0 + pisa.* codec, frames, negotiation,
// discovery, the feature gate, the approval canonical text, and LiveClient. Foundation only: builds on Linux,
// macOS, iOS and watchOS. Its tests read the shared fixtures in ../fixtures. Apache-2.0 (protocol/LICENSE).
import PackageDescription

let package = Package(
    name: "PisaAGUI",
    platforms: [.iOS(.v17), .macOS(.v14), .watchOS(.v10)],
    products: [
        .library(name: "PisaAGUI", targets: ["PisaAGUI"]),
    ],
    targets: [
        .target(name: "PisaAGUI"),
        .testTarget(name: "PisaAGUITests", dependencies: ["PisaAGUI"]),
    ],
    swiftLanguageModes: [.v6]
)
