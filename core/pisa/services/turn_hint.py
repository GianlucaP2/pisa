"""The end-of-turn hint: the answer to a partial transcript, behind `turn_hint`.

- **Today: the word rules**, the table the app uses too (`pisa.domain.turn`, `protocol/turn-rules.v1.json`).
  No model, nothing fetched, no paid call: no open model is known to beat the rules, and
  LiveKit's detector is licensed only inside LiveKit Agents.
- **A model can replace them later** (`TurnModel`: the words and the language → p_end and wait_ms). It runs on
  its own thread, one partial at a time, within `BUDGET_S`; when it is still busy with the previous partial,
  slower than the budget or failing, the hint comes from the rules (`source: rules`), within 150 ms.
- **Off unless the app asks**: the gateway calls this only for a device whose hello said `"turn_hints": true`.
- A partial's words are never stored or logged here; the caller logs their length only.
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from pisa.domain import turn
from pisa.logging import get_logger

_log = get_logger("pisa.turn_hint")

BUDGET_S = 0.12  # a model's share of the 150 ms answer (the socket and the rules take the rest)

TurnModel = Callable[[str, str], tuple[float, int]]  # (words so far, language) → (p_end, wait_ms); sync
Source = Literal["model", "rules"]


@dataclass(frozen=True)
class Hint:
    p_end: float
    wait_ms: int
    source: Source


def rules_hint(text: str, lang: str) -> Hint:
    where = turn.kind(text, lang)
    return Hint(turn.P_END[where], turn.WAITS_MS[where], "rules")


class TurnHintService:
    def __init__(self, model: TurnModel | None = None, *, budget_s: float = BUDGET_S) -> None:
        self._model = model
        self._budget_s = budget_s
        self._busy = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turn-hint") if model else None

    async def hint(self, text: str, lang: str) -> Hint:
        """The hint for the words so far: the model's within the budget, else the rules'."""
        if self._model is None or self._executor is None or self._busy:
            return rules_hint(text, lang)
        self._busy = True
        future = asyncio.get_running_loop().run_in_executor(self._executor, self._model, text, lang)
        future.add_done_callback(self._idle)
        try:
            p_end, wait_ms = await asyncio.wait_for(asyncio.shield(future), self._budget_s)
            return Hint(round(min(1.0, max(0.0, float(p_end))), 4), max(0, min(10000, int(wait_ms))), "model")
        except TimeoutError:
            _log.info("turn_hint_slow", budget_ms=round(self._budget_s * 1000))
        except Exception as exc:
            _log.warning("turn_hint_failed", cause=exc.__class__.__name__)
        return rules_hint(text, lang)

    def _idle(self, _: object) -> None:
        self._busy = False

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
