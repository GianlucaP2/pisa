"""AI disclosure and consent (guideline 5.1.2(i); CONTRACT-2 §4.4). Spike."""
from __future__ import annotations

from typing import Any

from .approvals import jcs, sha256_hex


def disclosure(processors: list[dict[str, Any]]) -> dict[str, Any]:
    """The server's disclosure; its revision changes whenever any provider, model or data category changes."""
    return {"revision": sha256_hex(jcs(processors))[:32], "processors": processors, "healthRequiresSeparateConsent": True}


def check_run(turn_props: dict[str, Any], current: dict[str, Any], touches_health: bool) -> str | None:
    """None when the run may use AI; else the RUN_ERROR code. The server enforces what the app asked the user."""
    consent = (turn_props or {}).get("consent")
    if not consent or consent.get("aiRevision") != current["revision"]:
        return "ai_consent_required"
    if touches_health and not consent.get("health", False):
        return "ai_consent_required"
    return None
