"""Stays: where the user stopped, from the location stream. Pure functions, no I/O.

- A **stay** is at least 5 minutes within 75 m, from fixes with an accuracy of 150 m or better (a fix
  joins a stay while it is within 75 m of the stay's centre so far). Worse fixes (a phone with "Precise
  Location" off reports ~2.3 km) never make or break a stay.
- iPhone **visits** count directly: each is a stay, with or without its departure yet; like fixes, only
  with an accuracy of 150 m or better.
- Location times older than 30 days or more than 5 minutes in the future are refused (`time_problem`).
- A stay whose last fix is the device's latest one has no departure yet (**ongoing**); only the last stay can
  be ongoing.
- Two stays at the same spot (within 75 m) with a gap of at most 5 minutes are one (a stray fix in between
  doesn't split it); a visit and a fix stay that overlap at the same spot are one.
- A stay is linked to the nearest known place within max(75 m, its accuracy).
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median

from pisa.domain import geo_rules as geo
from pisa.domain.places import distance_m

STAY_MIN = timedelta(minutes=5)
STAY_RADIUS_M = 75
OLDEST = timedelta(days=30)  # the raw-data retention: nothing older is accepted (or deleted by a refresh)
NEWEST = timedelta(minutes=5)  # clocks drift a little; further ahead is a bad time


def time_problem(at: datetime, now: datetime) -> str | None:
    """Why a location time is refused, or None."""
    if at < now - OLDEST:
        return "is more than 30 days old"
    if at > now + NEWEST:
        return "is more than 5 minutes in the future"
    return None


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    accuracy_m: float
    at: datetime


@dataclass(frozen=True)
class Visit:
    lat: float
    lon: float
    accuracy_m: float
    arrived_at: datetime
    departed_at: datetime | None


@dataclass(frozen=True)
class Stay:
    arrived_at: datetime
    departed_at: datetime | None  # None: ongoing
    lat: float
    lon: float
    accuracy_m: float
    last_seen_at: datetime  # the last fix (or visit time) inside the stay

    @property
    def coord(self) -> tuple[float, float]:
        return self.lat, self.lon


def detect(fixes: Sequence[Fix], visits: Sequence[Visit] = ()) -> list[Stay]:
    """The stays of one device, in time order."""
    good = sorted((f for f in fixes if f.accuracy_m <= geo.PLACE_ACCURACY_MAX_M), key=lambda f: f.at)
    found: list[Stay] = []
    i = 0
    while i < len(good):
        cluster = [good[i]]
        lat, lon = good[i].lat, good[i].lon
        j = i + 1
        while j < len(good) and distance_m((lat, lon), (good[j].lat, good[j].lon)) <= STAY_RADIUS_M:
            cluster.append(good[j])
            lat = sum(f.lat for f in cluster) / len(cluster)
            lon = sum(f.lon for f in cluster) / len(cluster)
            j += 1
        if cluster[-1].at - cluster[0].at >= STAY_MIN:
            found.append(
                Stay(
                    arrived_at=cluster[0].at,
                    departed_at=None if j == len(good) else cluster[-1].at,
                    lat=lat,
                    lon=lon,
                    accuracy_m=median(f.accuracy_m for f in cluster),
                    last_seen_at=cluster[-1].at,
                )
            )
            i = j
        else:
            i += 1
    for v in visits:
        if v.accuracy_m > geo.PLACE_ACCURACY_MAX_M:
            continue  # a 2.3 km "visit" says nothing about where the user was
        found.append(
            Stay(v.arrived_at, v.departed_at, v.lat, v.lon, v.accuracy_m, v.departed_at or v.arrived_at)
        )
    merged = _merge(sorted(found, key=lambda s: s.arrived_at))
    # Only the last stay can be ongoing: one followed by another (a visit elsewhere) ended at its last fix.
    return [
        replace(s, departed_at=s.last_seen_at) if s.departed_at is None and i < len(merged) - 1 else s
        for i, s in enumerate(merged)
    ]


def _merge(stays: list[Stay]) -> list[Stay]:
    out: list[Stay] = []
    for stay in stays:
        if out and _same(out[-1], stay):
            last = out[-1]
            ends = [s.departed_at for s in (last, stay)]
            fix_based = last if last.accuracy_m <= stay.accuracy_m else stay  # the better centre
            out[-1] = replace(
                fix_based,
                arrived_at=min(last.arrived_at, stay.arrived_at),
                departed_at=None if None in ends else max(e for e in ends if e is not None),
                last_seen_at=max(last.last_seen_at, stay.last_seen_at),
            )
        else:
            out.append(stay)
    return out


def _same(a: Stay, b: Stay) -> bool:
    """b (arriving after a) is the same stay: at the same spot, overlapping a or right after it."""
    if distance_m(a.coord, b.coord) > max(STAY_RADIUS_M, min(a.accuracy_m, b.accuracy_m)):
        return False
    return a.departed_at is None or b.arrived_at <= a.departed_at + STAY_MIN


# FACT: a stay links to a place by distance, measured on the user's data.
def link(stay: Stay, places: Sequence[tuple[str, float, float]]) -> str | None:
    """The nearest known place (id, lat, lon) within max(75 m, the stay's accuracy)."""
    reach = max(STAY_RADIUS_M, stay.accuracy_m)
    best: tuple[float, str] | None = None
    for place_id, lat, lon in places:
        d = distance_m(stay.coord, (lat, lon))
        if d <= reach and (best is None or d < best[0]):
            best = (d, place_id)
    return best[1] if best else None
