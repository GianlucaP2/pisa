"""Skill: "quando devo partire per l'aeroporto?" — the next flight (their trips, else a calendar
event naming a flight and an airport) and when to leave **by public transport** from where they are: the
departure, minus the airport buffer (the trips' own setting), minus the transit time (the routes tools',
else the usual time from the city centre below), plus the problems on the lines that reach that airport.

 already makes the "leave for the airport" task from home by car; this is the transit answer from here,
asked. Nothing is booked or written. No I/O."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pisa.domain import countries

# The usual door-to-gate transit time from the city centre, when no routes provider answers (Leonardo Express
# Termini-FCO 32 min + the way to Termini; JFK by the A/E/J and the AirTrain; LGA by the Q70; EWR by NJ
# Transit and the AirTrain). A fallback, said as such.
TRANSIT_MIN: dict[str, int] = {"FCO": 55, "CIA": 50, "JFK": 75, "LGA": 55, "EWR": 65, "MXP": 70, "LIN": 35}
# The lines whose problems matter on the way (the feeds Pisa reads: Rome's metro, NYC's subway)
AIRPORT_LINES: dict[str, tuple[str, set[str]]] = {
    "JFK": ("nyc", {"A", "E", "J", "Z"}),
    "LGA": ("nyc", {"7", "E", "F", "M", "R"}),
    "FCO": ("rome", {"MEA", "MEB", "MEB1"}),  # to Termini or Ostiense for the trains
    "CIA": ("rome", {"MEA"}),  # Anagnina's shuttle
}
_CODE = re.compile(r"\b([A-Z]{3})\b")
_FLIGHT_WORDS = re.compile(r"\b(?:volo|flight|aereo|plane)\b", re.IGNORECASE)
LOOK_DAYS = 3  # "the next flight": within three days


@dataclass(frozen=True)
class Flight:
    at: datetime
    airport: str  # the IATA code it leaves from
    ref: str
    abroad: bool


def from_leg(leg: dict[str, Any]) -> Flight | None:
    """A trip leg (its JSON) that is a flight with a known departure airport."""
    if leg.get("kind") != "flight" or not leg.get("at"):
        return None
    codes = _CODE.findall(str(leg.get("place") or ""))
    if not codes or countries.airport(codes[0]) is None:
        return None
    try:
        at = datetime.fromisoformat(str(leg["at"]))
    except ValueError:
        return None
    origin = countries.airport(codes[0])
    arrival = countries.airport(codes[1]) if len(codes) > 1 else None
    abroad = origin is not None and arrival is not None and origin.country != arrival.country
    return Flight(at, codes[0], str(leg.get("ref") or "")[:20], abroad)


def from_event(title: str, location: str | None, start: datetime) -> Flight | None:
    """A calendar event that names a flight and its airport ("Volo AZ5287 FCO-JFK", location "FCO")."""
    text = f"{title} {location or ''}"
    if not _FLIGHT_WORDS.search(text) and not re.search(r"\b[A-Z0-9]{2}\s?\d{2,4}\b", title):
        return None
    codes = [c for c in _CODE.findall(text) if countries.airport(c) is not None]
    if not codes:
        return None
    origin, arrival = countries.airport(codes[0]), countries.airport(codes[1]) if len(codes) > 1 else None
    abroad = origin is not None and arrival is not None and origin.country != arrival.country
    return Flight(start, codes[0], title[:40], abroad)


def leave_by(flight: Flight, transit_min: int | None, buffers: dict[str, int]) -> tuple[datetime, int, bool]:
    """(when to leave, the minutes of the way, whether that way is the fallback)."""
    fallback = transit_min is None
    way = (
        transit_min
        if transit_min is not None
        else TRANSIT_MIN.get(flight.airport, buffers["transfer_default_min"])
    )
    buffer = buffers["international_min" if flight.abroad else "domestic_min"]
    return flight.at - timedelta(minutes=buffer + way), way, fallback
