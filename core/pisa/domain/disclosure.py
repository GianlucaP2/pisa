"""The AI disclosure and the user's consent (CONTRACT-2 §4.4; App Review guideline 5.1.2(i)). Pure: no I/O.

For each feature slot with a model, the disclosure names the provider (`cloud`: a provider's API;
`self_hosted`: the user's own OpenAI-compatible server; `on_server`: a model server on Pisa's own machine),
the model, and the categories of the user's data the slot may send it. `revision` is the first 32 hex digits
of SHA-256 over the JCS of the processors, so it changes whenever a provider, a model or a category does.

The server enforces the answer: a run whose consent names another revision, or that touches health without
`health: true`, ends with `RUN_ERROR ai_consent_required` before any AI call. Whether a run touches health is
judged from the user's words by code's own lists (health places and visits, the same lists the wrist and the
lock screen use), never by a model.
"""

from typing import Any

from pisa.domain.approval_sign import jcs, sha256_hex
from pisa.domain.health import health_kinds, visit_kinds

CONSENT_REQUIRED = "ai_consent_required"
# what each slot's model may be sent (`$defs/AIDisclosure` categories)
SLOT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "chat.conversation": (
        "conversation", "mail", "calendar", "places", "location", "people", "health", "finance", "documents",
    ),
    "conversation.reflex": ("conversation", "calendar", "places", "people"),
    "intake.triage": ("mail", "calendar", "people"),
    "intake.extract": ("mail", "calendar", "people", "places"),
    "briefing.compose": ("calendar", "mail", "places", "people"),
    # a place's research (with the user's question), the talk's research arm, and the night's engines that
    # run on this slot: the curiosity (a topic they named, their dated events and tasks), the ideas (their
    # words and goals), the desk (ideas made from their problems), il prurito (their words)
    "places.research": ("places", "conversation", "calendar"),
    "memory.embed": ("conversation", "documents", "people"),
    "understand.confirm": ("conversation", "people"),
    "attend.decide": ("calendar", "mail", "places", "people"),
    "learn.answer": ("documents",),
}  # fmt: skip
# the feature ids (as `/v1/server.features` names them) each slot serves
SLOT_FEATURES: dict[str, tuple[str, ...]] = {
    "chat.conversation": ("conversation",),
    "conversation.reflex": ("conversation",),
    "intake.triage": ("connectors",),
    "intake.extract": ("connectors", "tasks"),
    "briefing.compose": ("briefing",),
    "places.research": ("places",),
    "memory.embed": ("profile",),
    "understand.confirm": ("conversation",),
    "attend.decide": ("guardian",),
}


def processor(slot: str, provider_id: str, name: str, kind: str, model: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "slot": slot[:40],
        "provider": {"id": provider_id[:60], "name": name[:80], "kind": kind},
        "model": model[:120],
        "dataCategories": list(SLOT_CATEGORIES.get(slot, ("conversation",))),
    }
    if features := SLOT_FEATURES.get(slot):
        entry["features"] = list(features)
    return entry


def disclosure(processors: list[dict[str, Any]]) -> dict[str, Any]:
    """`$defs/AIDisclosure`."""
    return {
        "revision": sha256_hex(jcs(processors))[:32],
        "processors": processors,
        "healthRequiresSeparateConsent": True,
    }


def touches_health(text: str | None) -> bool:
    """The user's words name their health: a health place or kind of care, or a visit to one."""
    return bool(text) and bool(health_kinds(text, asked=True) or visit_kinds(text))


def refused(consent: dict[str, Any] | None, revision: str, health: bool) -> str | None:
    """Why a run may not use AI (the RUN_ERROR's message), or None when the user's consent covers it."""
    if not consent or consent.get("aiRevision") != revision:
        if consent and consent.get("aiRevision"):
            return "The AI providers or models changed since you allowed them: review and allow them again."
        return "Allow Pisa to send your words to the AI models it uses first (Settings, AI)."
    if health and consent.get("health") is not True:
        return "This touches your health: allow health to go to AI first (Settings, AI, Health)."
    return None
