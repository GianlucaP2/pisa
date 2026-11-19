"""The conversation eval with the real model, through Pisa's own gateway: every call metered
in the ledger on the evals' own budget (the eval cap, never the user's caps), estimated first and
refused above `max_eur`. The model gets Pisa's system prompt, the language line and the case's few turns
before, no tools: the eval holds the conversational form, and cases hold the newest-wins, the move verb's own
time and a fresh start. Numbers only: each case's rule names and word count, never a reply's text (the
maintenance job's log is read by people)."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.settings import ModelSettings

from pisa.brain.conversation import SLOT, asked_instruction, language_instruction, system_prompt
from pisa.domain import talk
from pisa.domain.ai import CHARS_PER_TOKEN
from pisa.domain.errors import PisaError
from pisa.infra import ledger

MAX_EUR = 0.50
ANSWER_TOKENS = 200  # a spoken reply is 1-2 sentences; the cap is the estimate's output side


@dataclass(frozen=True)
class Case:
    name: str
    lang: str
    words: str
    before: tuple[tuple[str, str], ...] = ()  # (role, text) turns before it
    asked_long: bool = False
    after_yes: bool = False
    check: str | None = None


CASES = [
    Case("weather", "it-IT", "che tempo fa domani a Milano?"),
    Case("move-gym", "it-IT", "spostami la palestra a domani"),
    Case("tired", "it-IT", "sono stanco morto, cosa ho stasera?"),
    Case("book-table", "it-IT", "prenotami un tavolo da Mario"),
    Case("whole-week", "it-IT", "dimmi tutto quello che ho questa settimana", asked_long=True),
    Case("train-cancelled", "it-IT", "il treno delle 18 è cancellato?"),
    Case("disagree", "it-IT", "secondo me il dentista è alle 11"),
    Case("tomorrow-en", "en-US", "what's on tomorrow?"),
    Case("late-en", "en-US", "I'm running late, what should I skip?"),
    Case("book-en", "en-US", "book me a table at Mario's"),
    # the two: a check-in after Pisa's question takes the "sì"; the train's time is no move time.
    Case(
        "check-in-yes",
        "it-IT",
        "sì",
        before=(
            ("user", "sposta la cena alle 21"),
            ("assistant", "Sposto «Cena» a oggi alle 21 e avviso Paola?"),
            ("assistant", "Sei passato in farmacia?"),
        ),
        check="newest",
    ),
    Case("train-then-move", "it-IT", "alle 9 ho il treno, sposta la cena di domani", check="train"),
    # the user changes their mind: a fresh start, not a contradiction to point out
    Case(
        "fresh-start",
        "it-IT",
        "non serve mantenere le cose vecchie",
        before=(
            ("user", "tienimi le attività della settimana scorsa"),
            ("assistant", "Va bene, le tengo."),
        ),
        check="fresh",
    ),
]

Price = Callable[[int, int], Awaitable[Decimal | None]]


def _history(case: Case) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=text)])
        if role == "user"
        else ModelResponse(parts=[TextPart(content=text)])
        for role, text in case.before
    ]


def latest_question(case: Case) -> str | None:
    """Pisa's newest question before the case's words, as production gives it (`asked_instruction`: the
    eval had left it out, and `check-in-yes` broke on the real model for want of it)."""
    last = next((text for role, text in reversed(case.before) if role == "assistant"), None)
    return last if last is not None and last.rstrip().endswith("?") else None


def _prompt_chars(case: Case) -> int:
    return len(system_prompt(lang="it-IT")) + len(case.words) + sum(len(t) for _, t in case.before) + 100


async def gateway_price(runtime: Any, slot: str = SLOT) -> Price:
    """What one call costs in EUR at the slot's model (the conversation's unless named), or None when its
    price is unknown. The gateway may fall back to the slot's fallback model: the dearer of the two is the
    price, and an unknown price for either refuses the run."""
    from pisa.brain.models.compat import resolve_provider
    from pisa.brain.models.pricing import call_cost_usd
    from pisa.brain.models.slots import get_slot
    from pisa.domain.ai import ModelRef
    from pisa.infra.fx import usd_to_eur

    async with runtime.pool.connection() as conn:
        state = await get_slot(conn, slot)
        fx = await usd_to_eur(conn)
    refs = [r for r in (state.model, state.fallback) if r]

    async def price(tokens_in: int, tokens_out: int) -> Decimal | None:
        if state.model is None:
            return None
        costs: list[Decimal] = []
        for ref in refs:
            model = ModelRef.parse(ref)
            try:
                async with runtime.pool.connection() as conn:
                    spec = await resolve_provider(conn, model.provider, runtime.settings.ollama_base_url)
                    if spec is None:
                        return None
                    cost = await call_cost_usd(conn, spec, model, tokens_in, tokens_out)
            except PisaError:  # no price known at all: refused, like an unknown one
                return None
            if not cost.known:
                return None
            costs.append(cost.usd * fx)
        return max(costs)

    return price


async def estimate(price: Price, cases: list[Case] = CASES) -> Decimal | None:
    """The run's cost in EUR before any call; None when a price is unknown (then nothing runs)."""
    total = Decimal(0)
    for case in cases:
        eur = await price(_prompt_chars(case) // CHARS_PER_TOKEN, ANSWER_TOKENS)
        if eur is None:
            return None
        total += eur
    return total


@ledger.on_eval_budget  # the evals' own budget, never the user's caps or guard
async def run(
    runtime: Any, price: Price, *, cases: list[Case] = CASES, max_eur: float = MAX_EUR, call: bool = True
) -> dict[str, Any]:
    """Estimate, then (when `call` and within `max_eur`) ask the real model each case through the gateway."""
    planned = await estimate(price, cases)
    out: dict[str, Any] = {"cases": len(cases), "estimate_eur": float(round(planned, 4)) if planned else None}
    if planned is None:
        return out | {"refused": "a price is unknown: nothing was called"}
    if planned > Decimal(str(max_eur)):
        return out | {"refused": f"estimated €{planned:.3f} is above €{max_eur:.2f}: nothing was called"}
    if not call:
        return out
    results: dict[str, Any] = {}
    for case in cases:

        async def ask(ctx: Any, case: Case = case) -> str:
            # RAIL: the answer's cap is the estimate's (it was only estimated).
            agent = Agent(
                ctx.model,
                instructions=system_prompt(lang="it-IT"),
                model_settings=ModelSettings(max_tokens=ANSWER_TOKENS),
            )
            result = await agent.run(
                case.words, message_history=_history(case),
                instructions=language_instruction(case.lang) + asked_instruction(latest_question(case)),
            )  # fmt: skip
            ctx.usage.update(result.usage)
            return str(result.output)

        started = time.monotonic()
        try:
            reply = await runtime.gateway.run(
                SLOT, ask, prompt_chars=_prompt_chars(case), max_output_tokens=ANSWER_TOKENS
            )
        except Exception as err:  # one failed call is a number, never the end of the run
            results[case.name] = {"failed": getattr(err, "code", type(err).__name__)}
            continue
        broken = talk.violations(
            reply,
            case.lang,
            asked_long=case.asked_long,
            after_yes=case.after_yes,
            check=case.check,
            owner_words=case.words,
        )
        results[case.name] = {"broke": broken, "words": talk.word_count(reply),
                              "s": round(time.monotonic() - started, 2)}  # fmt: skip
    passed = sum(1 for r in results.values() if r.get("broke") == [])
    return out | {"passed": passed, "results": results}
