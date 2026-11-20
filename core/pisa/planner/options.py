"""Plan options on request: what the variants are, how they rank, and how they read.
No I/O.

- A **change** is what an adjustment or an option does to one item: `move` (start, end), `skip`,
  `replace_place` (place_id), `shorten` (duration_min). Adjustments are stored changes; every plan applies
  them.
- **Meals** are flexible in their window: breakfast 07:00-11:00, lunch 11:30-15:30, aperitivo 17:30-20:30,
  dinner 18:30-23:30, at least 30 minutes. An item is a meal when its title or its place's category says so.
- **Rank**: least minutes late for fixed events, then fewest items dropped, then least extra travel, then
  (meals) the earliest start in the window. At most three options.
- `summary` and `why` are deterministic sentences in the user's language (Italian or English).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain.lexicon.travel import MODE_WORDS

# a change to one event (what the app sends and is shown), and the whole-plan rebuild beside them
ChangeAction = Literal["move", "skip", "replace_place", "shorten"]
Action = Literal[ChangeAction, "plan"]
Goal = Literal["fit", "alternative", "reorganize"]
Variant = Literal["now", "after_next", "alternative", "shorten", "skip", "as_is"]

MEAL_WINDOWS: dict[str, tuple[time, time]] = {
    "breakfast": (time(7, 0), time(11, 0)),
    "lunch": (time(11, 30), time(15, 30)),
    "aperitivo": (time(17, 30), time(20, 30)),
    "dinner": (time(18, 30), time(23, 30)),
}
MEAL_MIN = 30
MAX_OPTIONS = 3
_MEAL_WORDS = (
    ("breakfast", r"breakfast|colazione"),
    ("lunch", r"lunch|pranzo"),
    ("aperitivo", r"aperitivo"),
    ("dinner", r"dinner|cena"),
)
MEAL_CATEGORIES = re.compile(r"restaurant|ristorante|cafe|café|caffe|caffè|coffee", re.IGNORECASE)


def meal_of(title: str, category: str | None = None) -> str | None:
    """The meal an item is, from its title, else its place's category; None when it isn't one."""
    for text in (title, category or ""):
        for meal, words in _MEAL_WORDS:
            if re.search(rf"(?<![a-zà-ü])({words})(?![a-zà-ü])", text, re.IGNORECASE):
                return meal
    return None


@dataclass(frozen=True)
class Change:
    action: Action
    event_id: str | None = None
    task_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    place_id: str | None = None
    duration_min: int | None = None

    def json(self) -> dict[str, object]:
        out: dict[str, object] = {"action": self.action}
        out |= {"event_id": self.event_id} if self.event_id else {"task_id": self.task_id}
        if self.action == "move":
            out |= {"start": self.start, "end": self.end}
        if self.action == "replace_place":
            out["place_id"] = self.place_id
        if self.action == "shorten":
            out["duration_min"] = self.duration_min
        return out


@dataclass
class Effects:
    late_min: int
    extra_travel_min: int
    dropped: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    """One solved variant of the day, before ranking."""

    variant: Variant
    changes: list[Change]
    effects: Effects
    plan: dict[str, object]
    summary: str
    why: str
    meal_start: datetime | None = None  # meals: where in the window the item starts (earlier ranks first)


def rank(candidates: list[Candidate]) -> list[Candidate]:
    far = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    return sorted(
        candidates,
        key=lambda c: (
            c.effects.late_min,
            len(c.effects.dropped),
            c.effects.extra_travel_min,
            c.meal_start or far,
        ),
    )[:MAX_OPTIONS]


# --- words ------------------------------------------------------------------------------------------------


def _it(lang: str) -> bool:
    return lang == "it-IT"


def _hhmm(moment: datetime, timezone: str) -> str:
    return f"{moment.astimezone(ZoneInfo(timezone)):%H:%M}"


def summary(
    variant: Variant,
    lang: str,
    *,
    title: str = "",
    place: str | None = None,
    after: str | None = None,
    minutes: int | None = None,
) -> str:
    """ "Adesso da Toko Sambalaya" / "Now at Toko Sambalaya"; "Dopo Spiraal"; "Più breve: 30 min"; "Salta il
    pranzo"."""
    it = _it(lang)
    at = f" {'da' if it else 'at'} {place}" if place else ""
    if variant == "now":
        return ("Adesso" if it else "Now") + at
    if variant == "after_next":
        return (f"Dopo {after}" if it else f"After {after}") + at
    if variant == "alternative":
        return (
            (f"Da {place}" if it else f"At {place}")
            + (f", dopo {after}" if it and after else "")
            + (f", after {after}" if not it and after else "")
        )
    if variant == "shorten":
        return f"Più breve: {minutes} min" if it else f"Shorter: {minutes} min"
    if variant == "skip":
        return f"Salta {title}" if it else f"Skip {title}"
    return "Continua da qui" if it else "Carry on from here"


def why(
    lang: str,
    timezone: str,
    *,
    next_title: str | None,
    next_arrival: datetime | None,
    next_late_min: int,
    effects: Effects,
    mode: str,
    hours_unknown: bool = False,
) -> str:
    """ "Arrivi allo Spiraal alle 14:40 in orario; 9 min a piedi in più." The deciding facts: the next fixed
    stop, lateness, travel versus now, what drops out, and a place whose opening hours aren't known."""
    it = _it(lang)
    parts: list[str] = []
    if next_title and next_arrival:
        at = _hhmm(next_arrival, timezone)
        if next_late_min > 0:
            parts.append(
                f"Arrivi a {next_title} alle {at}, {next_late_min} min in ritardo"
                if it
                else f"You reach {next_title} at {at}, {next_late_min} min late"
            )
        else:
            parts.append(
                f"Arrivi a {next_title} alle {at} in orario"
                if it
                else f"You reach {next_title} at {at} on time"
            )
    elif effects.late_min > 0:
        parts.append(
            f"{effects.late_min} min di ritardo in tutto" if it else f"{effects.late_min} min late in all"
        )
    extra = effects.extra_travel_min
    if extra:
        how = MODE_WORDS["it-IT" if it else "en-US"].get(mode, "")
        if it:
            parts.append(f"{abs(extra)} min {how} {'in più' if extra > 0 else 'in meno'}")
        else:
            parts.append(f"{abs(extra)} min {'more' if extra > 0 else 'less'} {how}")
    if effects.dropped:
        names = ", ".join(effects.dropped)
        parts.append(f"salta: {names}" if it else f"drops: {names}")
    if hours_unknown:
        parts.append("orari non noti" if it else "hours unknown")
    if not parts:
        parts.append("Nessun cambiamento per il resto della giornata" if it else "Nothing else changes today")
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]
