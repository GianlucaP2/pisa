import Foundation

/// RUN_ERROR.code on an approval thread (`pisa-approval:<id>`), CONTRACT-2 §6.1.
public enum ApprovalErrorCode: String, Codable, Sendable, CaseIterable {
    /// The content changed since the user saw it: nothing ran.
    case approvalChanged = "approval_changed"
    case approvalExpired = "approval_expired"
    /// Decided elsewhere (voice, another device, the watch, a notification).
    case approvalDecided = "approval_decided"
    /// This approval needs a valid Face ID signature.
    case biometricRequired = "biometric_required"
    /// Approved, the action ran, and it failed. The approval is decided: do not resume it again.
    case actionFailed = "action_failed"
}
