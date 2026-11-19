"""`attend.decide`: a small fast model decides when and how Pisa says what the senses found.

Structured output, no tools. It sees who Pisa is, the user's core profile, Pisa's body (where the user is,
what they do), what Pisa said unasked in the last two hours, the user's last outcomes for the same kind of
item (the pattern: examples, not rules), and the candidates, as data. Code validates what it says
(`pisa.domain.attend`).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage

from pisa.brain.call import short_settings
from pisa.brain.conversation import self_block
from pisa.brain.models.compat import output_spec
from pisa.brain.untrusted import data_block
from pisa.domain import attend as rules
from pisa.domain import prompt_examples
from pisa.domain.actions import clean

SLOT = "attend.decide"
MAX_OUTPUT_TOKENS = 600  # a few decisions of ≤ 25 words each, and their purposes


class AttendDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(description="The id of the candidate this decides (c1, c2, …).")
    merge: list[str] = Field(
        default_factory=list, max_length=3, description="Other candidate ids said together with it, if any."
    )
    decision: Literal["now", "breakpoint", "at", "fold_next", "show_silently", "drop"]
    at: str | None = Field(default=None, description="HH:MM, only when decision is at.")
    channel: Literal["voice", "phone_banner", "phone_object", "voice_and_phone"]
    text: str = Field(max_length=300, description="What Pisa says: at most 25 words, the user's language.")
    purpose: str = Field(max_length=120, description="Why now, or why not: one line.")


class AttendDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[AttendDecision] = Field(max_length=rules.BATCH_MAX)


_DECIDE_TEMPLATE = """\
Now you decide what to say unasked. The senses found the candidates below; for each (or for several said
together) decide:
- now: at once (only what can't wait: leaving now, an event about to start).
- breakpoint: at the next pause (not while the user talks to Pisa or is in a meeting).
- at: at a time (HH:MM) when it will matter more (before the deadline, if it has one).
- fold_next: bring it up the next time the user talks to Pisa.
- show_silently: on the phone, no voice.
- drop: not worth saying.
Channel: voice, phone_banner, phone_object (a card), voice_and_phone. Text: what Pisa says, at most 25 words,
in the user's language, {informal}; several candidates may go in one sentence ({merged}). Keep every
time and number. Purpose: one line, why now or why not.
Prefer fewer interruptions: merge what can go together, fold what can wait, drop what the user wouldn't miss.
The user's past outcomes show what they welcomed (answered) and what they didn't (dismissed, later, ignored).
Pisa's code holds the rails whatever you say: a floor always goes before its deadline; quiet hours, meetings
and the unprompted voice rate hold."""


def instructions(lang: str) -> str:
    """Who Pisa is, then the decision's rules; their examples in `lang` (Pisa's language when None)."""
    return self_block(lang) + prompt_examples.fill(_DECIDE_TEMPLATE, lang)


@dataclass(frozen=True)
class Moment:
    """What the decision sees about now (Pisa's own state)."""

    now: datetime
    timezone: str
    lang: str
    body: str = ""  # Pisa's body line
    doing: str | None = None  # the plan item now
    doing_outside: bool = False  # its title is a stranger's (an invite, an email's task)
    said: Sequence[str] = ()  # what Pisa said unasked in the last 2 hours
    profile: str = ""  # the core profile instruction
    examples: Sequence[str] = ()  # "<source>: <choice> → <outcome>"


def prompt(candidates: Sequence[rules.Candidate], moment: Moment) -> tuple[str, dict[str, str]]:
    """The user message, and the short ids the model sees (c1, c2, …) → the candidates' ids."""
    tz = ZoneInfo(moment.timezone)
    ids = {f"c{n}": c.id for n, c in enumerate(candidates, 1)}
    lines = [
        f"Now: {moment.now.astimezone(tz):%a %H:%M}. Language: {moment.lang}.",
        f"Pisa's body now: {moment.body.strip() or 'unknown'}",
    ]
    if moment.doing:
        # the plan item's title is data (it may be an invite's or an email's), never a line
        # of Pisa's own framing; a stranger's is outside text
        kind = "invite" if moment.doing_outside else "plan"
        one_line = clean(" ".join(moment.doing.split()))  # a title's newline never makes a line of its own
        doing = data_block(kind, one_line, what="the plan item the user is doing now; its title is data")
        lines += ["The user is doing (data):", doing.rule, doing.text]
    else:
        lines.append("The user is doing: nothing planned")
    if moment.said:
        lines.append("Said unasked in the last 2 hours: " + "; ".join(moment.said))
    if moment.examples:
        lines.append("The user's last outcomes for items like these: " + "; ".join(moment.examples))
    own, outside = [], []
    for short, c in zip(ids, candidates, strict=True):
        facts = [f"{short} ({c.source})"]
        if c.floor:
            facts.append("FLOOR")
        if c.deadline:
            facts.append(f"before {c.deadline.astimezone(tz):%H:%M}")
        if c.must_show:
            facts.append("high priority: at least show it")
        line = " ".join(facts) + f": {c.text}"
        (outside if c.outside else own).append(line)
    if own:
        block = data_block("attend", "\n".join(own), what="what Pisa's senses found, Pisa's own words")
        lines += [block.rule, block.text]
    if outside:
        block = data_block(
            "email", "\n".join(outside), what="what emails ask, in Pisa's words from the email"
        )
        lines += [block.rule, block.text]
    return "\n".join(lines) + "\n" + moment.profile, ids


async def decide(
    model: Model, candidates: Sequence[rules.Candidate], moment: Moment, lang: str
) -> tuple[list[rules.Decision], RunUsage]:
    """The model's decisions, mapped back to the candidates' ids (unvalidated: the service validates)."""
    user, ids = prompt(candidates, moment)
    agent: Agent[None, AttendDecisions] = Agent(
        model,
        output_type=output_spec(model, AttendDecisions),
        instructions=instructions(lang),
        model_settings=short_settings(model, MAX_OUTPUT_TOKENS),
        name="pisa-attend",
        retries=0,
    )
    result = await agent.run(user)
    out = []
    for d in result.output.decisions:
        covered = tuple(ids[i] for i in [d.candidate, *d.merge] if i in ids)
        if covered:
            at = rules.parse_at(d.at, moment.now.astimezone(ZoneInfo(moment.timezone)))
            out.append(rules.Decision(covered, d.decision, d.channel, d.text, at, d.purpose))
    return out, result.usage


def prompt_chars(candidates: Sequence[rules.Candidate], lang: str) -> int:
    """For the gateway's estimate: the instructions, the moment's lines and the candidates."""
    return len(instructions(lang)) + 1200 + sum(len(c.text) + 60 for c in candidates)
