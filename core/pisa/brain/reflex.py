"""Pisa's reflex, a small fast model on the user's **partial** words.

Two short calls, no tools, structured output:
- `read`: what the user is after (intent, the goal in a few words) and which of Pisa's own reads would answer
  it (the day's agenda, the tasks, today's plan, what waits for a yes, a travel time), with the day and the
  place.
- `answer`: once those reads are in, the obvious answer drafted from them only (or nothing): ready when the
  user stops.

It never writes and never decides: code runs the reads under the speculation rails
(`pisa.domain.speculation`), checks the ready answer against the data (`pisa.domain.state.grounded`), and the
main thinker answers the final words with this as a head start. Without a model (none picked, a failure, over
its time) the rules read the words (`pisa.domain.reflex`): free.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage

from pisa.brain.call import fast_settings
from pisa.brain.models.compat import output_spec
from pisa.brain.untrusted import data_block

SLOT = "conversation.reflex"
# The structured answer's own tokens (reasoning has its room beside them: `fast_settings`). Each fits its
# output's longest valid JSON at 2 characters a token (the read: a 120-character goal, an 80-character place,
# three reads and a day, ~310 characters; the answer: 240 characters): 120 and 90 were short of it.
READ_TOKENS = 200
ANSWER_TOKENS = 160


INTENTS = ("question", "change", "chat", "unclear")
READS = ("agenda", "tasks", "plan", "waiting", "travel")


def _cut(chars: int) -> Any:
    """A text over its limit is cut, never refused: with no retry (`retries=0`, a fast call) a goal one word
    too long failed the whole read and rested the reflex for 5 minutes."""

    def cut(value: Any) -> Any:
        return value[:chars].rstrip() if isinstance(value, str) else value

    return BeforeValidator(cut)


def _intent(value: Any) -> Any:
    return value if value in INTENTS else "unclear"  # an unknown intent says nothing yet


def _reads(value: Any) -> Any:
    """Pisa's reads it named, known ones only, at most three."""
    if not isinstance(value, list):
        return value
    return list(dict.fromkeys(r for r in value if r in READS))[:3]


class ReflexRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Annotated[Literal["question", "change", "chat", "unclear"], BeforeValidator(_intent)] = Field(
        description="question: they ask about their day or data; change: they ask Pisa to add, move, cancel, "
        "send or remember something; chat: talk; unclear: too few words yet."
    )
    goal: Annotated[str, _cut(120)] = Field(
        max_length=120, description="What the user wants, in a few words, their language."
    )
    reads: Annotated[
        list[Literal["agenda", "tasks", "plan", "waiting", "travel"]], BeforeValidator(_reads)
    ] = Field(
        max_length=3,
        description="Pisa's reads that answer it or that the change needs: agenda (a day's events and tasks "
        "due), tasks (open tasks), plan (today's plan), waiting (what Pisa asked them), "
        "travel (a travel time).",
    )
    day: str | None = Field(
        default=None, description='"today", "tomorrow" or YYYY-MM-DD, when they name one.'
    )
    place: Annotated[str | None, _cut(80)] = Field(
        default=None, max_length=80, description="The destination, for travel."
    )


class ReflexAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Annotated[str | None, _cut(240)] = Field(
        default=None,
        max_length=240,
        description="The answer in at most 20 words, the user's language, only from Pisa's data; null when "
        "the data doesn't answer it.",
    )


READ_INSTRUCTIONS = """\
You are Pisa's reflex: you hear the user's words while they are still speaking (they may be cut mid-sentence)
and say, in one quick pass, what they are after and which of Pisa's own reads would answer it. Never answer,
never act. A change ("mettimi…", "sposta…", "ricordami…") still lists the reads it needs (the day's
agenda)."""

ANSWER_INSTRUCTIONS = """\
You are Pisa's reflex. Draft the obvious answer to the user's words from Pisa's data below only: at most 20
words, their language, "tu", every time exactly as in the data. If the data doesn't answer it, or they ask for
a change, answer null. Never invent anything."""


async def read(model: Model, words: str, lang: str, now: str) -> tuple[ReflexRead, RunUsage]:
    agent: Agent[None, ReflexRead] = Agent(
        model, output_type=output_spec(model, ReflexRead), instructions=READ_INSTRUCTIONS,
        model_settings=fast_settings(model, READ_TOKENS), name="pisa-reflex-read", retries=0,
    )  # fmt: skip
    result = await agent.run(f"Now: {now}. Language: {lang}.\nThe user, so far: {words}")
    return result.output, result.usage


async def answer(
    model: Model, words: str, data: list[str], lang: str, now: str
) -> tuple[str | None, RunUsage]:
    block = data_block("day-context", "\n".join(data), what="Pisa's own data, read for these words")
    agent: Agent[None, ReflexAnswer] = Agent(
        model, output_type=output_spec(model, ReflexAnswer), instructions=ANSWER_INSTRUCTIONS,
        model_settings=fast_settings(model, ANSWER_TOKENS), name="pisa-reflex-answer", retries=0,
    )  # fmt: skip
    data_text = block.render()
    result = await agent.run(f"Now: {now}. Language: {lang}.\n{data_text}\nThe user: {words}")
    text = (result.output.answer or "").strip()
    return text or None, result.usage


def prompt_chars(words: str, data: list[str] | None = None) -> int:
    base = len(READ_INSTRUCTIONS) + len(words) + 200
    return base if data is None else len(ANSWER_INSTRUCTIONS) + len(words) + sum(map(len, data)) + 400
