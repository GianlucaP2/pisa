"""The AI disclosure and the user's consent (CONTRACT-2 §4.4; App Review guideline 5.1.2(i)). Pure: no I/O.

For each feature slot with a model, the disclosure names the provider (`cloud`: a provider's API;
`self_hosted`: the user's own OpenAI-compatible server; `on_server`: a model server on Pisa's own machine),
the model, and the categories of the user's data the slot may send it. `revision` is the first 32 hex digits
of SHA-256 over the JCS of the processors, so it changes whenever a provider, a model or a category does.

The server enforces the answer: a run whose consent doesn't cover the current disclosure (another revision
that sends more: a processor, a model or a data category it didn't allow, `covers`), or that touches health
without `health: true`, ends with `RUN_ERROR ai_consent_required` before any AI call. A revision that sends
the same or less (natural → economy drops the talker) stays allowed. Whether a run touches health is
judged from the user's words by code's own lists (health places and visits, the same lists the wrist and the
lock screen use), never by a model.
"""

from typing import Any

from pisa.domain import locale
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
# natural mode's talker (Gemini Live, Grok Voice): the user's voice itself, plus whatever the conversation's
# model may be sent (today's conversation, where they are and the plan's next item in its instructions, and
# every answer Pisa's brain gives it to say)
SLOT_CATEGORIES["conversation.talker"] = ("voice", *SLOT_CATEGORIES["chat.conversation"])
# the talker's processor (not a model slot: `settings.conversation`, in natural mode only)
TALKER_SLOT = "conversation.talker"
# the server's voice (`settings.voice`: OpenAI speech or ElevenLabs, not the phone's own): every sentence Pisa
# says is sent to it to be spoken, so whatever the conversation's model may be sent (an answer about their
# health, their mail, their day). Not a model slot: a processor only while a server voice speaks.
VOICE_SLOT = "conversation.voice"
SLOT_CATEGORIES[VOICE_SLOT] = SLOT_CATEGORIES["chat.conversation"]
# the server's voices (`domain.voice.PROVIDERS` but the phone's own `device`)
VOICE_PROVIDERS: tuple[str, ...] = ("openai", "elevenlabs")
# each talker (`settings.conversation.talker`) and the provider whose key and API it uses
# (`connectors.talker_*`: `key_provider`)
TALKER_PROVIDERS: dict[str, str] = {"gemini": "google", "xai": "xai"}
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
    "conversation.talker": ("conversation",),
    "conversation.voice": ("conversation",),
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


# why a run (or a talk) may not use AI, in the user's language (`refused`); plain text: the app shows it as
# written, so the command has no Markdown backticks
REFUSED: dict[str, dict[str, str]] = {
    "en": {
        "changed": "The AI providers or models changed since you allowed them: review and allow them "
        "again in the app, or on the server run pisa consent.",
        "missing": "Allow Pisa to send your words to the AI models it uses first: allow it in the app, or "
        "on the server run pisa consent.",
        "health": "This touches your health: allow health to go to AI first, in the app or on the server "
        "with pisa consent --health.",
        "action": "Allow it in the app, or run pisa consent on the server",
    },
    "it": {
        "changed": "I fornitori o i modelli di IA sono cambiati da quando li hai permessi: rivedili e "
        "permettili di nuovo nell'app, o sul server con pisa consent.",
        "missing": "Prima permetti a Pisa di mandare le tue parole ai modelli di IA che usa: permettilo "
        "nell'app, o sul server con pisa consent.",
        "health": "Questo riguarda la tua salute: prima permetti che la salute vada all'IA, nell'app o sul "
        "server con pisa consent --health.",
        "action": "Permettilo nell'app, o sul server con pisa consent",
    },
}


def refused_action(lang: str = "en") -> str:
    """What to do about `ai_consent_required` (the error's `action`), in `lang`."""
    return locale.pick(REFUSED, lang)["action"]


def _who(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    provider = entry.get("provider") or {}
    return (
        str(entry.get("slot")),
        str(provider.get("id")),
        str(provider.get("kind")),
        str(entry.get("model")),
    )


def covers(consented: list[dict[str, Any]] | None, current: list[dict[str, Any]]) -> bool:
    """The processors the user allowed cover the current ones: each current processor (its slot, provider,
    kind and model) was allowed, with the same or fewer data categories. Fewer processors, or fewer
    categories, send less: covered. A new or changed processor, model or category: not covered."""
    if consented is None:
        return False
    allowed: dict[tuple[str, str, str, str], set[str]] = {}
    for entry in consented:
        allowed.setdefault(_who(entry), set()).update(str(c) for c in entry.get("dataCategories") or ())
    return all(
        _who(entry) in allowed and set(map(str, entry.get("dataCategories") or ())) <= allowed[_who(entry)]
        for entry in current
    )


def refused(
    consent: dict[str, Any] | None,
    revision: str,
    health: bool,
    lang: str = "en",
    *,
    consented: list[dict[str, Any]] | None = None,
    current: list[dict[str, Any]] | None = None,
) -> str | None:
    """Why a run may not use AI (the RUN_ERROR's message, in `lang`), or None when the user's consent covers
    it: its revision is the current one, or the processors it allowed (`consented`) cover the current ones
    (`current`)."""
    words = locale.pick(REFUSED, lang)
    same = bool(consent) and consent.get("aiRevision") == revision  # type: ignore[union-attr]
    if not consent or not (same or (current is not None and covers(consented, current))):
        if consent and consent.get("aiRevision"):
            return words["changed"]
        return words["missing"]
    if health and consent.get("health") is not True:
        return words["health"]
    return None
