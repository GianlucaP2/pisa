"""Error codes shared by the live session (`/v2/live`'s runs) and the REST API.

Every error names the real cause (`message`) and what to do about it (`action`).
"""

from typing import Any, Literal, get_args

ProtocolErrorCode = Literal[
    "auth_failed",
    "invalid_message",
    "model_not_configured",
    "provider_key_missing",
    "provider_error",
    "budget_exceeded",
    "rate_limited",
    "internal",
    "approval_expired",
    "approval_decided",
    "biometric_required",
]
RestOnlyErrorCode = Literal[
    "pairing_code_invalid",
    "not_found",
    "model_incompatible",
    "model_capability_missing",
    "validation_failed",
    "connector_not_configured",
    "connector_auth_failed",
    "connector_not_connected",
    "push_not_configured",
    "location_unknown",
    "place_provider_not_configured",
    "places_cap_reached",
    "place_provider_error",
    "last_device",
    "plan_changed",
    "check_in_answered",
    "options_expired",
    "google_permission_missing",
    "undo_expired",
    "talk_economy",
    "talker_unavailable",
    "connector_off",
    "voice_consent_missing",
    "voice_cohort_missing",
    # contract 2: what the phone was shown is no longer what would run (CONTRACT-2 §6.4); nothing ran
    "approval_changed",
    # contract 2's privacy revision (CONTRACT-2 §4.3-§4.5): a session token past its hour, a device the server
    # revoked, and a run or consent the user's AI consent does not cover
    "session_expired",
    "device_revoked",
    "ai_consent_required",
    # a key still serves some feature slots (`slots` names them): give a replacement, or move them first
    "provider_in_use",
    # what another device owns (its conversation): only that device acts on it
    "other_device",
]
ErrorCode = ProtocolErrorCode | RestOnlyErrorCode

PROTOCOL_ERROR_CODES: frozenset[str] = frozenset(get_args(ProtocolErrorCode))

_HTTP_STATUS: dict[str, int] = {
    "auth_failed": 401,
    "invalid_message": 400,
    "model_not_configured": 409,
    "provider_key_missing": 409,
    "provider_error": 502,
    "budget_exceeded": 402,
    "rate_limited": 429,
    "internal": 500,
    "pairing_code_invalid": 400,
    "not_found": 404,
    "model_incompatible": 400,
    # the slot's model, on the user's own server, lacks what the use needs (probed, or set by hand)
    "model_capability_missing": 409,
    "validation_failed": 400,
    "connector_not_configured": 409,
    "connector_auth_failed": 502,
    "connector_not_connected": 409,
    "push_not_configured": 409,
    "location_unknown": 409,
    "place_provider_not_configured": 409,
    "places_cap_reached": 402,
    "place_provider_error": 502,
    "last_device": 409,
    "plan_changed": 409,
    "check_in_answered": 409,
    "options_expired": 404,
    "approval_expired": 409,
    "approval_decided": 409,
    "approval_changed": 409,
    "biometric_required": 403,
    "google_permission_missing": 409,
    "undo_expired": 409,
    # the day is in economy (the setting, or the budget guard): the app takes today's path
    "talk_economy": 409,
    "talker_unavailable": 502,  # the talker's provider refused or is unreachable: the app takes economy
    # an enrolment needs their consent first, and the public cohort built once
    "voice_consent_missing": 409,
    "voice_cohort_missing": 409,
    "connector_off": 409,  # a source switched off in Settings: no sync, no call, no data taken
    "session_expired": 401,
    "device_revoked": 401,
    "ai_consent_required": 409,
    "provider_in_use": 409,
    "other_device": 403,
}

# Contract 2's approval resume (`POST /v1/approvals/{id}/resume`, protocol/v2/openapi-v2-delta.json) answers a
# refusal made before anything ran with the table's status, except two: an expired approval is gone (410)
# and a missing Face ID is unauthorised (401). Other codes keep their own status there.
RESUME_REFUSAL_STATUS: dict[str, int] = {
    code: _HTTP_STATUS[code] for code in ("not_found", "approval_decided", "approval_changed")
} | {"approval_expired": 410, "biometric_required": 401}

_RETRYABLE = frozenset({"provider_error", "rate_limited", "internal", "place_provider_error"})


class PisaError(Exception):
    """A failure with a machine code, the real cause in plain words, and the action to take."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        action: str = "",
        *,
        detail: str = "",
        extra: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.action = action
        self.detail = detail  # a machine word for logs (never content): "token_limit", "empty"…
        # machine fields the error body carries for the app ("economy_reason", "provider", "slots")
        self.extra = extra or {}
        self._status = status  # an error that keeps its own HTTP status

    @property
    def http_status(self) -> int:
        return self._status or _HTTP_STATUS[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE

    @property
    def protocol_code(self) -> ProtocolErrorCode:
        """The code the live session sends (REST-only codes have no equivalent there: `internal`)."""
        return self.code if self.code in PROTOCOL_ERROR_CODES else "internal"  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"PisaError({self.code!r}, {self.message!r})"
