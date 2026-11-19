"""The latency report: close → first audio by reply kind, before and after, on the test fixtures.

No real provider runs in tests (no paid calls outside the ledgers), so the model is a stand-in with a
provider's shape of latency: the first token 0.45 s + 0.12 s per 1,000 prompt tokens after it is asked, then
0.25 s per sentence; each round after a tool pays the first-token wait again. "First audio" is the first
`say` frame the device gets (a holding phrase counts: the app plays it from its cache); the voice's own
synthesis time is the same before and after, so it is left out.

- **before**: no holding phrase, a second model round after an action ("Fatto: …"),
  the research guide in every turn, the user's "sì" through the model ("Rispondi sul telefono").
- **after**: the holding phrases, the reply from the action's result and the yes heard by code, all on.

PISA_LATENCY_REPS sets the turns per kind (1 in the suite; 10 for the handoff's table, by hand).

The time is an injected `Clock`. In the suite (one turn per kind) it runs ten times faster:
the modelled waits and Pisa's own timers (the holding phrase's 0.7 s, the turn budget) shrink together, so
what comes first is the same (the loop runs timers in deadline order) and the reported seconds are the
modelled ones. The handoff's table (PISA_LATENCY_REPS) runs at real speed."""

import asyncio
import json
import os
import random
import statistics
import time
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall

FIRST_TOKEN_S = 0.45
PER_1K_TOKENS_S = 0.12
PER_SENTENCE_S = 0.25
TOOL_TOKENS = 60  # a tool's schema in the prompt, roughly
SUITE_SPEED = 10.0  # The suite's clock runs this many times faster than the wall


class Clock:
    """The report's time: `sleep` and `now` in modelled seconds, `speed` times faster than the wall."""

    def __init__(self, speed: float = 1.0, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.speed = speed
        self._monotonic = monotonic  # the wall's clock; a test of the Clock itself gives its loop's

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)

    def now(self) -> float:
        return self._monotonic() * self.speed

    def real(self, seconds: float) -> float:
        """A modelled duration on the wall (Pisa's own timers)."""
        return seconds / self.speed


def _tokens(messages: list[Any], info: Any) -> int:
    chars = sum(len(str(p.content)) for m in messages for p in m.parts if hasattr(p, "content"))
    chars += len(info.instructions or "")
    return chars // 4 + TOOL_TOKENS * len(info.function_tools)


def stand_in(rng: random.Random, calls: list[int] | None = None, clock: Clock | None = None):  # type: ignore[no-untyped-def]
    """The fixture turns' model, with modelled latency (`calls` counts the rounds)."""
    clock = clock or Clock()

    async def model(messages, info):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls[0] += 1
        wait = (FIRST_TOKEN_S + PER_1K_TOKENS_S * _tokens(messages, info) / 1000) * rng.uniform(0.85, 1.25)
        await clock.sleep(wait)
        words = [str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart)][-1]
        returned = [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
        if words.startswith("sposta la cena") and not returned:
            yield {
                0: DeltaToolCall(
                    name="move_event", json_args=json.dumps({"event": "la cena", "start": "oggi alle 21"})
                )
            }
            return
        if words.startswith("che faccio") and not returned:
            yield {0: DeltaToolCall(name="get_agenda", json_args=json.dumps({"day": "today"}))}
            return
        for sentence in ("Fatto. ", "Ecco cosa ho trovato. ") if returned else ("Va bene. ", "Ecco. "):
            yield sentence
            await clock.sleep(PER_SENTENCE_S)

    return model


TURNS = {  # reply kind → (the user's words before it, the measured words)
    "chat": ([], "che ore sono?"),
    "tool": ([], "che faccio oggi?"),
    "action": ([], "sposta la cena alle 21"),
    "yes": (["sposta la cena"], "sì"),
    "research": ([], "trova un bar aperto qui vicino"),
}


def _first_say_s(
    client: Any, db: Any, secrets_key: str, words: list[str], last: str, calls: list[int], clock: Clock
) -> tuple[float, float, bool]:
    """(first audio: the first `say`, a holding phrase included; the answer's own first sentence; whether a
    holding phrase came first). `calls` is reset before the measured words: their model rounds only."""
    from tests.conftest import pair, set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        for said in words:
            app.say(said, lang="it-IT", source="earbuds_mic")
            list(app.reply())
        calls[0] = 0
        start = clock.now()
        app.say(last, lang="it-IT", source="earbuds_mic")
        frame = app.next()
        while frame["type"] != "say":
            frame = app.next()
        first = answer = clock.now() - start
        held = bool(frame.get("holding"))
        while frame.get("holding") or frame["type"] != "say":
            frame = app.next()
            answer = clock.now() - start
        if not frame["final"]:  # a reply said in one chunk (the yes by code) is already over
            list(app.reply())
    return first, answer, held


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"p50": round(statistics.median(ordered), 2), "p95": round(p95, 2), "n": len(ordered)}


def run_report(client: Any, db: Any, google: Any, secrets_key: str, fake_models: Any) -> dict[str, Any]:
    from pisa.services import conversation as service
    from pisa.services.conversation import ConversationService
    from tests.test_approvals import dinner

    reps = int(os.environ.get("PISA_LATENCY_REPS", "1"))
    clock = Clock(1.0 if os.environ.get("PISA_LATENCY_REPS") else SUITE_SPEED)
    conversations = client.app.state.runtime.conversations
    out: dict[str, Any] = {}
    for kind, (before_words, words) in TURNS.items():
        results = {}
        for era in ("before", "after"):
            rng = random.Random(f"{kind}-{era}")  # noqa: S311 (modelled latency, not security)
            calls = [0]
            fake_models.default = stand_in(rng, calls, clock)
            samples, answers, rounds, holds = [], [], [], []
            for _ in range(reps):
                db.execute("DELETE FROM approvals")
                db.execute("DELETE FROM calendar_events")
                google.events.clear()
                attendees = [{"email": "paola@y.example", "displayName": "Paola"}] if kind == "yes" else []
                dinner(google, db, **({"attendees": attendees} if attendees else {}))
                with ExitStack() as stack:
                    # the turn budget's holding phrase is newer than the baseline: none "before"
                    budget = 1e9 if era == "before" else clock.real(service.TURN_BUDGET_S)
                    stack.enter_context(patch.object(service, "TURN_BUDGET_S", budget))
                    if era == "before":
                        conversations.hold_after_s = 1e9
                        stack.enter_context(
                            patch.object(
                                ConversationService, "_research_follow_up", staticmethod(lambda earlier: True)
                            )
                        )
                        stack.enter_context(patch.object(ConversationService, "_answered", _no_answer))
                    else:
                        conversations.hold_after_s = clock.real(0.7)
                    first, answer, held = _first_say_s(
                        client, db, secrets_key, before_words, words, calls, clock
                    )
                    samples.append(first)
                    answers.append(answer)
                    rounds.append(calls[0])
                    holds.append(held)
            results[era] = (
                _stats(samples)
                | {"answer_" + k: v for k, v in _stats(answers).items() if k != "n"}
                | {"model_rounds": max(rounds), "held": any(holds)}
            )
        out[kind] = (results["before"], results["after"])
    conversations.hold_after_s = 60.0
    return out


async def _no_answer(self: Any, device_id: Any, text: str, lang: str, spoken: bool = True) -> None:
    return None
