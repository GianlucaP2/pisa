"""Model gateway vocabulary: model references, capabilities and feature slots."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pisa.domain.errors import PisaError

Capability = Literal["tools", "structured_output", "vision", "embeddings", "audio_in", "audio_out"]
CHARS_PER_TOKEN = 4  # a text's tokens, estimated: about four characters each
MTOK = Decimal(1_000_000)  # a price is per million tokens


@dataclass(frozen=True)
class ModelRef:
    """`"<provider_id>:<model_id>"`. The model id may itself contain ':' or '/' (e.g. OpenRouter ids)."""

    provider: str
    model_id: str

    @classmethod
    def parse(cls, ref: str) -> "ModelRef":
        provider, sep, model_id = ref.partition(":")
        if not sep or not provider or not model_id:
            raise PisaError(
                "validation_failed",
                f"The model reference {ref!r} is not in the form '<provider_id>:<model_id>'.",
                "Pick a model from the list returned by GET /v1/ai/models",
            )
        return cls(provider, model_id)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model_id}"


@dataclass(frozen=True)
class SlotSpec:
    id: str
    name: str
    description: str
    needs: tuple[Capability, ...]
    phrase: str  # how a sentence names the feature: "the conversation uses …"


SLOTS: tuple[SlotSpec, ...] = (
    SlotSpec(
        "chat.conversation",
        "Voice conversation",
        "Answers what you say to Pisa through the glasses or the app. Needs a model that can call tools.",
        ("tools",),
        "the conversation",  # typed and spoken turns alike: never "voice" for a typed run
    ),
    SlotSpec(
        "conversation.reflex",
        "Pisa's reflex while you talk",
        "Reads your words while you are still speaking (what you're after, which of Pisa's data answers it) "
        "and drafts the obvious answer from Pisa's own data, so Pisa is ready when you stop. It never "
        "acts. "
        "Small and fast (about 1 s from Pisa's server; the aim is 300 ms), no tools; without one, Pisa's "
        "word rules do it for free.",
        ("structured_output",),
        "Pisa's reflex",
    ),
    SlotSpec(
        "intake.triage",
        "Read a page or an email for Pisa",
        "Writes a short neutral summary of an outside text (an email, a web page, an invite) for Pisa to "
        "read, with no tools.",
        ("structured_output",),
        "reading outside text",
    ),
    SlotSpec(
        "intake.extract",
        "Read new email into events",
        "Turns each new email into what happened in it and what it asks, with a likely reaction as a "
        "proposal; Pisa's rules turn those into storylines and tasks.",
        ("structured_output",),
        "reading email",
    ),
    SlotSpec(
        "briefing.compose",
        "Morning briefing",
        "Writes the morning briefing (reminders use fixed wording, no model).",
        (),
        "the morning briefing",
    ),
    SlotSpec(
        "places.research",
        "Research a place on the web",
        "Summarises what websites and reviews say about a place, with its sources (no tools).",
        ("structured_output",),
        "place research",
    ),
    SlotSpec(
        "memory.embed",
        "Embeddings for search",
        "Turns notes and memories into vectors so Pisa can search them.",
        ("embeddings",),
        "memory search",
    ),
    SlotSpec(
        "understand.confirm",
        "Check a name Pisa isn't sure it heard",
        "When a word only might be a name Pisa knows, says whether the user meant it (same or different). "
        "Cheap and fast: it answers in one word, no tools.",
        (),
        "checking names Pisa heard",
    ),
    SlotSpec(
        "attend.decide",
        "Decide when Pisa speaks up",
        "Decides when and how Pisa tells you what it noticed (now, at a pause, later, on the phone, or not "
        "at all), within Pisa's rules: deadlines, quiet hours, meetings and the voice limit. Cheap and "
        "fast, no tools.",
        ("structured_output",),
        "deciding when Pisa speaks up",
    ),
    SlotSpec(
        "learn.answer",
        "Pisa Learn's answers from your sources",
        "Answers your questions about a course's papers, each sentence cited (source, section, page) through "
        "Anthropic's Citations API, so it needs an Anthropic model; also the fallback when the cheapest "
        "model "
        "can't rank passages or check a sentence.",
        (),
        "Pisa Learn's answers",
    ),
)
SLOTS_BY_ID: dict[str, SlotSpec] = {s.id: s for s in SLOTS}


def missing_capabilities(needs: tuple[Capability, ...], has: list[str] | tuple[str, ...]) -> list[str]:
    return [c for c in needs if c not in has]
