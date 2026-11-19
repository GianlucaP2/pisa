"""The user's home. Pure rules, no I/O.

Home is the user's first: About you, the first talk, or by voice ("abito a …") set it (`source: owner`). Only
when the user never said, Pisa learns it from where the phone sleeps, and only from LEARN_NIGHTS nights of
history with most of them in one place: a phone paired during a trip has seen only hotels (without that rule,
home would be learned as a trip's hotel). A learned home is a guess ("I think you live in …")
until the user confirms it; a confirmed or user's home never changes by itself.

While home is unknown, nothing is scoped home or travel and no trip starts (pisa.services.learning).
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

Coord = tuple[float, float]
Source = Literal["owner", "learned"]

LEARN_NIGHTS = 21  # nights Pisa must have seen before it guesses a home
LEARN_DAYS = 28  # the nights a learned home is worked out from
LEARN_SHARE = 0.5  # the home's cell holds more than this share of those nights
CELL_DEG = 0.003  # ~300 m: nights are counted by cells this size
NAME_MAX = 120


@dataclass(frozen=True)
class Home:
    lat: float
    lon: float
    name: str | None
    source: Source
    confirmed: bool
    place_id: str | None = None

    @property
    def coord(self) -> Coord:
        return (self.lat, self.lon)

    @property
    def fixed(self) -> bool:
        """The user's, or confirmed by the user: Pisa never moves it by itself."""
        return self.source == "owner" or self.confirmed

    def stored(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "name": self.name,
            "source": self.source,
            "confirmed": self.confirmed,
            "place_id": self.place_id,
        }

    def public(self) -> dict[str, Any]:
        """GET /v1/profile's `home`."""
        return {
            "place": {
                "name": self.name or f"{self.lat:.3f}, {self.lon:.3f}",
                "lat": self.lat,
                "lon": self.lon,
            },
            "source": self.source,
            "confirmed": self.fixed,
        }


UNKNOWN: dict[str, Any] = {"place": None, "source": None, "confirmed": False}


def parse(value: Any) -> Home | None:
    """The stored home; before it was a bare [lat, lon] that Pisa had learned."""
    if isinstance(value, list) and len(value) == 2:
        return Home(float(value[0]), float(value[1]), None, "learned", False)
    if isinstance(value, dict) and value.get("lat") is not None and value.get("lon") is not None:
        source: Source = "owner" if value.get("source") == "owner" else "learned"
        name = value.get("name")
        return Home(
            float(value["lat"]),
            float(value["lon"]),
            str(name)[:NAME_MAX] if name else None,
            source,
            bool(value.get("confirmed")) or source == "owner",
            value.get("place_id"),
        )
    return None


def cell(coord: Coord) -> tuple[int, int]:
    return round(coord[0] / CELL_DEG), round(coord[1] / CELL_DEG)


def learn(nights: dict[date, Coord], today: date) -> Coord | None:
    """Where the user sleeps, when Pisa has seen enough nights to say: at least LEARN_NIGHTS of them, and more
    than half of the last LEARN_DAYS in one ~300 m cell (its centre). Else None: Pisa doesn't know yet."""
    if len(nights) < LEARN_NIGHTS:
        return None
    recent = [c for d, c in nights.items() if d >= today - timedelta(days=LEARN_DAYS)]
    if not recent:
        return None
    top, count = Counter(cell(c) for c in recent).most_common(1)[0]
    if count <= LEARN_SHARE * len(recent):
        return None
    members = [c for c in recent if cell(c) == top]
    return (sum(c[0] for c in members) / len(members), sum(c[1] for c in members) / len(members))


# The user's words that say where they live ("abito a Roma", "vivo in via …", "I live in …", "casa mia è …"):
# the place they name becomes home only with one of these in the same message (never from outside).
HOME_PHRASES = (
    "abito", "vivo a", "vivo in", "vivo al", "vivo vicino", "casa mia", "la mia casa", "sto di casa",
    "i live", "my home", "my house", "home is", "ik woon",
)  # fmt: skip


def says_home(plain_words: str) -> bool:
    """`plain_words`: the user's message through pisa.domain.profile.plain."""
    said = f" {plain_words} "
    return any(f" {p} " in said for p in HOME_PHRASES)
