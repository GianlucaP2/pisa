"""Which model `pisa setup` gives each feature slot once a provider's key is set. Pure: no I/O.

Every text slot gets a model of the chosen provider that can do what the slot needs (`SlotSpec.needs`):
the provider's main model where the work is the conversation's or as hard, its smallest (cheapest) one where
the slot asks for small and fast. `memory.embed` takes an embeddings model from any provider with a key, the
chosen one first. A slot no model fits stays off, and setup says so.

The catalog rows are `AiService.models(None)`'s: `ref`, `provider`, `model_id`, `capabilities`, `key_set` and
`price_eur_per_mtok` (None when unknown).
"""

from decimal import Decimal
from typing import Any

from pisa.domain.ai import SLOTS_BY_ID, missing_capabilities

EMBED_SLOT = "memory.embed"
# every slot whose model reads and writes text, in setup's order
TEXT_SLOTS: tuple[str, ...] = (
    "chat.conversation", "conversation.reflex", "intake.triage", "intake.extract", "briefing.compose",
    "places.research", "understand.confirm", "attend.decide", "learn.answer",
)  # fmt: skip
# the slots whose description asks for a small, fast (cheap) model
FAST_SLOTS = frozenset({"conversation.reflex", "understand.confirm", "attend.decide"})
# model ids that name something other than a text model (audio, images, embeddings, moderation...)
NOT_TEXT = (
    "embed", "tts", "whisper", "audio", "realtime", "image", "transcribe", "moderation", "dall-e", "live",
    "search", "guard", "computer-use", "deep-research", "speech", "veo", "imagen", "rerank", "ocr",
)  # fmt: skip
# unfinished models: taken only when the provider has nothing else for the slot
UNFINISHED = ("preview", "-exp", "experimental", "beta")
# the main model is the provider's most capable one below this output price (€ per million tokens): the
# flagships, not a reasoning tier priced for research
MAIN_MAX_EUR = Decimal(25)


def _output(row: dict[str, Any]) -> Decimal | None:
    price = row.get("price_eur_per_mtok")
    if not price or price.get("output") is None:
        return None
    return Decimal(str(price["output"]))


def _text(row: dict[str, Any]) -> bool:
    model = str(row["model_id"]).lower()
    return not any(word in model for word in NOT_TEXT)


def pick(rows: list[dict[str, Any]], slot_id: str) -> str | None:
    """The ref of the model `rows` (one provider's) offer for the slot, or None when none can serve it."""
    needs = SLOTS_BY_ID[slot_id].needs
    fits = [r for r in rows if _text(r) and not missing_capabilities(needs, r["capabilities"])]
    priced = [r for r in fits if _output(r) is not None] or fits
    finished = [r for r in priced if not any(w in str(r["model_id"]).lower() for w in UNFINISHED)] or priced
    if not finished:
        return None

    def price(row: dict[str, Any]) -> Decimal:
        return _output(row) or Decimal(0)

    if slot_id in FAST_SLOTS:
        return min(finished, key=lambda r: (price(r), r["ref"]))["ref"]
    affordable = [r for r in finished if price(r) <= MAIN_MAX_EUR]
    if affordable:
        return max(affordable, key=lambda r: (price(r), r["ref"]))["ref"]
    return min(finished, key=lambda r: (price(r), r["ref"]))["ref"]


def plan(rows: list[dict[str, Any]], provider: str) -> tuple[dict[str, str], list[str]]:
    """({slot: ref} for every slot a model fits, [the slots none fits]): the text slots from `provider`'s
    models, `memory.embed` from any provider with a key (`provider` first, then the cheapest)."""
    own = [r for r in rows if r["provider"] == provider]
    chosen: dict[str, str] = {}
    missing: list[str] = []
    for slot_id in TEXT_SLOTS:
        ref = pick(own, slot_id)
        if ref is None:
            missing.append(slot_id)
        else:
            chosen[slot_id] = ref
    keyed = [r for r in rows if r.get("key_set") and "embeddings" in r["capabilities"]]
    embeds = sorted(keyed, key=lambda r: (r["provider"] != provider, _output(r) or Decimal(0), r["ref"]))
    if embeds:
        chosen[EMBED_SLOT] = embeds[0]["ref"]
    else:
        missing.append(EMBED_SLOT)
    return chosen, missing
