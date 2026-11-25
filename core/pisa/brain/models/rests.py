"""A model slot resting: after a failure (a refused call, an answer it couldn't use, too slow) a slot's model
is left alone for a while and the rules do its work (the reflex rests 5 minutes after a failure).

Pisa has no operator alert channel: a rest is logged as a warning (the slot, the model and the reason, never
the user's words), at most once an hour per slot, and counted per slot in this process; `readyz` shows the
counts (`counts()`), so an operator sees a slot that keeps resting without reading every log line.
"""

import time
from collections import Counter
from collections.abc import Callable

from pisa.logging import get_logger

WARN_EVERY_S = 3600.0  # one warning per slot per hour; every rest is counted
_log = get_logger("pisa.models")
_counts: Counter[str] = Counter()
_warned: dict[str, float] = {}


def rested(
    slot: str,
    model: str | None,
    reason: str,
    seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Count the slot's rest and warn once an hour for it. `model`: the slot's model ref (None: unknown);
    `reason`: a machine word ("token_limit", "provider_error", "slow"). True when the warning was logged."""
    _counts[slot] += 1
    now = clock()
    last = _warned.get(slot)
    if last is not None and now - last < WARN_EVERY_S:
        return False
    _warned[slot] = now
    _log.warning(
        "model_slot_resting", slot=slot, model=model or "unknown", reason=reason, seconds=int(seconds),
        rests=_counts[slot],
    )  # fmt: skip
    return True


def counts() -> dict[str, int]:
    """Each slot's rests in this process since it started."""
    return dict(_counts)


def reset() -> None:
    """Tests: no rests counted, none warned."""
    _counts.clear()
    _warned.clear()
