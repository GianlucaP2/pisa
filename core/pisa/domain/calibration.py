"""Is Pisa's confidence worth anything? Pure: no I/O.

- `ece`: expected calibration error (bias): over 10 equal-width bins of confidence, the item-weighted gap
  between the mean confidence and the pass rate.
- `auroc`: type-2 AUROC (sensitivity): the chance a passing item has a higher confidence than a failing one
  (ties count half). `None` when one class is empty.
- `line`: both, with "n/a (n=…)" under `MIN_ITEMS` rated items (a number on a handful of items is noise).
"""

from collections.abc import Sequence

MIN_ITEMS = 20
BINS = 10


def ece(items: Sequence[tuple[float, bool]]) -> float:
    if not items:
        return 0.0
    bins: dict[int, list[tuple[float, bool]]] = {}
    for confidence, passed in items:
        bins.setdefault(min(BINS - 1, int(confidence * BINS)), []).append((confidence, passed))
    return sum(
        len(b) / len(items) * abs(sum(c for c, _ in b) / len(b) - sum(1 for _, p in b if p) / len(b))
        for b in bins.values()
    )


def auroc(items: Sequence[tuple[float, bool]]) -> float | None:
    good = [c for c, p in items if p]
    bad = [c for c, p in items if not p]
    if not good or not bad:
        return None
    wins = sum(1.0 if g > b else 0.5 if g == b else 0.0 for g in good for b in bad)
    return wins / (len(good) * len(bad))


def line(items: Sequence[tuple[float, bool]], over_asking: int, confident_failures: int) -> str:
    """One line: ECE, type-2 AUROC, over-asking (under-confidence), confident failures (over-confidence)."""
    n = len(items)
    if n < MIN_ITEMS:
        e = a = f"n/a (n={n})"
    else:
        e = f"{ece(items):.3f}"
        got = auroc(items)
        a = f"{got:.3f}" if got is not None else f"n/a (n={n}, one class)"
    return (f"calibration: ECE {e} · type-2 AUROC {a} · over-asking {over_asking} · "
            f"confident failures {confident_failures}")  # fmt: skip
