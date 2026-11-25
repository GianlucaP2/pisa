import XCTest
@testable import PisaAGUI

final class ApprovalErrorTests: XCTestCase {
    func testActionFailedArrivesOnTheApprovalThread() throws {
        let frames = try Fixtures.frames("approval_failed").map { try Wire.decoder.decode(ServerFrame.self, from: Data($0.utf8)) }
        guard case .event(_, let thread, _, let ev) = frames.last!, case .runError(let e) = ev.payload else { return XCTFail("no RUN_ERROR") }
        XCTAssertTrue(thread.hasPrefix("pisa-approval:"))
        XCTAssertEqual(e.code.flatMap(ApprovalErrorCode.init(rawValue:)), .actionFailed)
        XCTAssertEqual(ApprovalErrorCode.allCases.map(\.rawValue),
                       ["approval_changed", "approval_expired", "approval_decided", "biometric_required", "action_failed"])
    }
}
