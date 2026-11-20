"""Streaks that matter — the gym, running, reading — from their calendar and the tasks they
marked done, counted in weeks (a week with one session keeps it). Shown in the week's poster when a streak is
two weeks or more; never a push, never "you broke it" (a streak that ends simply isn't shown). Code's numbers
only. No I/O."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pisa.domain.profile import plain

Kind = Literal["gym", "run", "reading"]
KINDS: tuple[Kind, ...] = ("gym", "run", "reading")
WEEKS_BACK = 12
SHOW_FROM = 2  # a streak is shown from two weeks in a row
_WORDS: dict[Kind, re.Pattern[str]] = {
    "gym": re.compile(r"\b(?:palestra|gym|allenamento|workout|crossfit|pilates|yoga|pesi)\b"),
    "run": re.compile(r"\b(?:corsa|corsetta|correre|run|running|jogging|corro)\b"),
    "reading": re.compile(r"\b(?:leggere|lettura|letto|leggo|read|reading|libro|book)\b"),
}
_NOT = re.compile(r"\b(?:annull\w*|cancel\w*|disdire|disdetta|rinnov\w*|abbonamento|iscrizion\w*|pagare|"
                  r"comprare|buy|pay|renew)\b")  # fmt: skip
LABELS = {
    "it-IT": {"gym": "Palestra", "run": "Corsa", "reading": "Lettura"},
    "en-US": {"gym": "Gym", "run": "Running", "reading": "Reading"},
}


@dataclass(frozen=True)
class Streak:
    kind: Kind
    weeks: int  # weeks in a row, up to this one (or last week, while this one has none yet)
    this_week: int  # sessions this week


def kind_of(title: str) -> Kind | None:
    """What a title is a session of ("Palestra", "Corsa al parco", "Leggere 30 minuti"); never a chore about
    it ("Rinnovare l'abbonamento della palestra", "Comprare un libro")."""
    text = plain(title)
    if _NOT.search(text):
        return None
    for kind in KINDS:
        if _WORDS[kind].search(text):
            return kind
    return None


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def streaks(sessions: list[tuple[str, date]], today: date) -> list[Streak]:
    """`sessions`: (title, local day) of past calendar events and done tasks. One Streak per kind seen."""
    weeks: dict[Kind, set[date]] = {k: set() for k in KINDS}
    this: dict[Kind, int] = dict.fromkeys(KINDS, 0)
    current = _monday(today)
    for title, day in sessions:
        kind = kind_of(title)
        if kind is None or day > today or day < current - timedelta(weeks=WEEKS_BACK):
            continue
        weeks[kind].add(_monday(day))
        if _monday(day) == current:
            this[kind] += 1
    out: list[Streak] = []
    for kind in KINDS:
        if not weeks[kind]:
            continue
        start = current if current in weeks[kind] else current - timedelta(weeks=1)
        n = 0
        while start - timedelta(weeks=n) in weeks[kind]:
            n += 1
        out.append(Streak(kind, n, this[kind]))
    return out


def poster_blocks(found: list[Streak], lang: str) -> list[dict[str, str]]:
    """The poster's stats for the streaks worth showing (two weeks or more), longest first."""
    it = lang == "it-IT"
    labels = LABELS["it-IT" if it else "en-US"]
    shown = sorted((s for s in found if s.weeks >= SHOW_FROM), key=lambda s: (-s.weeks, s.kind))
    return [
        {
            "type": "stat",
            "label": (f"{labels[s.kind]}, di fila" if it else f"{labels[s.kind]}, in a row"),
            "value": str(s.weeks),
            "unit": "sett." if it else "wk",
            "trend": "up" if s.this_week else "flat",
        }
        for s in shown
    ]


def into_poster(blocks: list[dict[str, Any]], found: list[Streak], lang: str) -> list[dict[str, Any]]:
    """The poster's blocks with the streak stats after its gym stat (in "Le costanti"), or at the end."""
    extra = poster_blocks(found, lang)
    if not extra:
        return blocks
    at = next(
        (
            i + 1
            for i, b in enumerate(blocks)
            if b.get("type") == "stat" and b.get("label") in ("Palestra", "Gym")
        ),
        len(blocks),
    )
    return [*blocks[:at], *extra, *blocks[at:]]
