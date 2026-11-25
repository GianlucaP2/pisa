"""The understanding step's brain: the rules of pisa.domain.understand, and the small model
(`understand.confirm`, no tools, `same | different`) only for the names in the grey zone or a real word that
sounds like a name. All the doubts of one utterance are asked at once, within UNDERSTAND_S: a model that
doesn't answer in time, or isn't configured, leaves the words as they were said.

The model sees the user's words, the words in question and one name from Pisa's tables (an email sender's
name, an event title): all of it between nonce markers, as data.
"""

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage
from wordfreq import zipf_frequency

from pisa.brain.call import fast_settings
from pisa.domain import understand as rules
from pisa.logging import get_logger

_log = get_logger("pisa.understand")

SLOT = "understand.confirm"
UNDERSTAND_S = 0.3  # the model's share of the step (≤ 300 ms)
CONFIRM_TOKENS = 16  # "same" or "different": the answer's own tokens; reasoning has its room (fast_settings)
DOUBTS_MAX = 3  # model calls per utterance at most

Ask = Callable[[str, rules.Doubt], Awaitable[bool]]  # (the utterance, a doubt) → same?


@lru_cache(maxsize=65536)
def zipf(word: str, lang: str) -> float:
    """How common a word is (wordfreq's Zipf scale: 3 is once per million words)."""
    return float(zipf_frequency(word, lang))


def warm() -> None:
    """Wordfreq reads its word lists on the first lookup (~1 s): done at startup, off the event loop, so the
    first turn after a restart doesn't pay it."""
    zipf("ciao", "it")
    zipf("hello", "en")


def confirm_prompt(utterance: str, doubt: rules.Doubt) -> str:
    nonce = secrets.token_hex(8)
    e = doubt.entry

    def clean(text: str) -> str:
        return text.replace(nonce, "")

    return (
        f"<data-{nonce}>\nOwner said (recognised): {clean(utterance)}\n"
        f"Words in question: {clean(doubt.span)}\n"
        f"Candidate: {clean(e.name)} ({e.kind}{', ' + clean(e.city) if e.city else ''})\n</data-{nonce}>"
    )


async def confirm(
    model: Model, utterance: str, doubt: rules.Doubt, report: Callable[[RunUsage], None]
) -> bool:
    """The small model's answer for one doubt: True only for a plain "same"."""
    agent: Agent[None, str] = Agent(
        model,
        instructions=rules.MODEL_PROMPT,
        model_settings=fast_settings(model, CONFIRM_TOKENS),
        name="pisa-understand",
    )
    result = await agent.run(confirm_prompt(utterance, doubt))
    report(result.usage)
    return result.output.strip().strip(".").lower() == "same"


@dataclass
class Understood:
    result: rules.Result
    asked: int  # model calls made
    seconds: float
    late: int = 0  # calls cancelled at the budget: paid for, never used


async def understand(
    text: str,
    lex: rules.Lexicon,
    lang: str,
    ask: Ask | None,
    *,
    names: bool = True,
    budget_s: float | None = None,
) -> Understood:
    """The rules once; the model for their doubts (at most DOUBTS_MAX, together, within the budget); the rules
    again with its answers. Unanswered doubts change nothing."""
    started = time.monotonic()
    first = rules.understand(text, lex, lang, zipf, names_too=names)
    if not first.doubts or ask is None:
        return Understood(first, 0, time.monotonic() - started)
    doubts = list({(d.span, d.entry.name): d for d in first.doubts}.values())[:DOUBTS_MAX]
    answers: dict[tuple[str, str], bool] = {}

    async def one(d: rules.Doubt) -> None:
        try:
            answers[(d.span, d.entry.name)] = await ask(text, d)
        except Exception as exc:  # an unanswered doubt changes nothing
            _log.warning("understand_doubt_failed", cause=exc.__class__.__name__)

    tasks = [asyncio.create_task(one(d)) for d in doubts]
    _, pending = await asyncio.wait(tasks, timeout=UNDERSTAND_S if budget_s is None else budget_s)
    for task in pending:
        task.cancel()
    second = rules.understand(
        text, lex, lang, zipf, names_too=names, oracle=lambda span, e: answers.get((span, e.name), False)
    )
    # Only calls that went out and were cut count as late: with no budget left (a slow lexicon) nothing was
    # waited for.
    late = len(pending) if budget_s is None or budget_s > 0 else 0
    return Understood(second, len(doubts), time.monotonic() - started, late)
