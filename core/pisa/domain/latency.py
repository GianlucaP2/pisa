"""The measures a turn's speed is held
to. Pure rules, no I/O.

- **The cached share** of a turn's input: the tokens the provider served from its prompt cache over every
  token it read, summed over the turn's model rounds (`talk_turns.parts`, the replay's turns).
- **The turn budget** (`TURN_BUDGET_S`): a turn still silent this long after their words arrived hears Pisa
  say it is taking long; the turn goes on. Nothing is cut at the budget. Chosen from the replay:
  model turns p50 4.3 s, p95 8.9 s, max 14.5 s. At 8 s about one turn in twenty hears
  the line, the ones they would otherwise wait on in silence; a correct but slow turn (the 14.5 s one) still
  finishes and is said.
- **The provider's silence** (`REQUEST_TIMEOUT_S`): the model request's HTTP timeout (`ModelSettings.timeout`,
  httpx's per-operation timeout: connecting, or waiting this long for the next bytes; not a ceiling on the
  whole request, so a long streamed tool turn that keeps sending is never cut by it). A provider silent this
  long is hung. 20 s is above the replay's slowest whole turn (14.5 s, several rounds), so no working
  request waits that long between bytes; the gateway then falls back to the slot's other model when nothing
  was said yet, and otherwise the error path speaks its error sentence (no claim). It replaces the 45 s.
- **An honest p95**: every percentile is printed with its n (`figure`); the replay's first model
  turn after a model change (each pass's first, a cold prompt cache) is dropped from its figures, and said so.
- **The reflex against its window** (`reflex`): the reflex model's read time p50/p95 with n, how many
  reads ran past the 2 s window (the rules read the words then: such a read is cut at the window, so a p95 at
  the window is "at least"), and the reflex-first share: the turns whose first words were the reflex's.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

TURN_BUDGET_S = 8.0
REQUEST_TIMEOUT_S = 20.0
REFLEX_WINDOW_MS = 2000  # the reflex's 2 s window (`services.reflex.REFLEX_BUDGET_S`)
MODEL_READS = ("model", "slow")  # the reflex's read by the model: inside the window, or past it (cut)


def percentile(values: Sequence[float], q: float) -> float | None:
    """The talk report's own percentile (the value at index n·q, the last for q = 1)."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def figure(values: Sequence[float], unit: str = "s") -> str:
    """p50 and p95 with their n: "p50 4.3 s, p95 8.9 s (n=171)"; "- (n=0)" with no value."""
    if not values:
        return "p50 -, p95 - (n=0)"
    p50, p95 = percentile(values, 0.50), percentile(values, 0.95)
    if unit == "ms":
        return f"p50 {p50:.0f} ms, p95 {p95:.0f} ms (n={len(values)})"
    return f"p50 {p50:.1f} s, p95 {p95:.1f} s (n={len(values)})"


@dataclass(frozen=True)
class Reflex:
    """The reflex's model reads against its window, and who said a turn's first words."""

    ms: tuple[int, ...]  # each model read's time (a read past the window counts at the window: it was cut)
    over: int  # model reads past the window (the rules read the words instead)
    first: int  # turns whose first content was the reflex's
    turns: int  # turns with a first content
    window_ms: int = REFLEX_WINDOW_MS

    @property
    def p50_ms(self) -> int | None:
        return percentile_int(self.ms, 0.50)

    @property
    def p95_ms(self) -> int | None:
        return percentile_int(self.ms, 0.95)


def percentile_int(values: Sequence[int], q: float) -> int | None:
    value = percentile([float(v) for v in values], q)
    return int(value) if value is not None else None


def reflex(
    reads: Iterable[tuple[str | None, int | None]],
    firsts: Iterable[str | None],
    window_ms: int = REFLEX_WINDOW_MS,
) -> Reflex:
    """From (by, ms) per reflex read (`model`, `slow`, `rules`, `final`; only the model's two count) and each
    turn's first-content source (None: no first content, not counted)."""
    ms: list[int] = []
    over = 0
    for by, took in reads:
        if by not in MODEL_READS:
            continue
        late = by == "slow" or (took is not None and took > window_ms)
        over += late
        ms.append(max(took or 0, window_ms) if late else int(took or 0))
    said = [f for f in firsts if f is not None]
    return Reflex(tuple(ms), over, sum(1 for f in said if f == "reflex"), len(said), window_ms)


def reflex_wire(r: Reflex) -> dict[str, Any]:
    """The talk report's reflex figures as numbers."""
    return {
        "reads": len(r.ms),
        "p50_ms": r.p50_ms,
        "p95_ms": r.p95_ms,
        "over_window": r.over,
        "window_ms": r.window_ms,
        "reflex_first": r.first,
        "turns": r.turns,
    }
