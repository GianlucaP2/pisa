"""Fog of war with one glint, and the Map of the Life. Pure rules, no I/O,
no model: every number is counted here from their own places, stays and trips.

- **Fog of war**: the land around their centre (home, else the place they stay at most) cut into 250 m
  cells. Their **usual radius** is where 90% of their stays fall (trips far away left out), between 1 and 10
  km. A cell is explored once a stay or a location fix fell in it. The sheet: how much is explored, by
  direction, and the places Pisa knows there (from their own searches) where they have never been.
- **One glint**: the nearest unexplored cell to their centre (at least 400 m out), where Pisa looks
  for one new place to offer as a question (the something different, at most once a week, never at night).
- **The Map of the Life**: each season (meteorological: winter is December to February), a sheet
  built from that season's stays and trips: the places, the firsts, the most-returned-to, the trips, the
  countries.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pisa.domain.places import distance_m

Coord = tuple[float, float]
CELL_M = 250
RADIUS_MIN_M, RADIUS_MAX_M = 1_000, 10_000
FAR_M = 50_000  # a stay further than this from their centre is a trip, not their usual ground
RADIUS_SHARE = 0.9
GLINT_MIN_M = 400  # the glint is beyond their doorstep
PLACES_MAX = 8
_M_PER_DEG = 111_320.0

# --- the grid ----------------------------------------------------------------------------------------------


def cell_of(point: Coord, centre: Coord) -> tuple[int, int]:
    """The 250 m cell a point falls in, on a grid anchored at their centre (rows north, columns east)."""
    north = (point[0] - centre[0]) * _M_PER_DEG
    east = (point[1] - centre[1]) * _M_PER_DEG * math.cos(math.radians(centre[0]))
    return (math.floor(north / CELL_M + 0.5), math.floor(east / CELL_M + 0.5))


def cell_centre(cell: tuple[int, int], centre: Coord) -> Coord:
    lat = centre[0] + cell[0] * CELL_M / _M_PER_DEG
    lon = centre[1] + cell[1] * CELL_M / (_M_PER_DEG * math.cos(math.radians(centre[0])))
    return (round(lat, 6), round(lon, 6))


def _cell_metres(cell: tuple[int, int]) -> float:
    return math.hypot(cell[0], cell[1]) * CELL_M


def usual_radius(stays: Sequence[Coord], centre: Coord) -> int:
    """Where 90% of their stays fall, rounded to 500 m, between 1 and 10 km (trips far away left out)."""
    near = sorted(d for d in (distance_m(centre, s) for s in stays) if d <= FAR_M)
    if not near:
        return RADIUS_MIN_M
    at = near[min(len(near) - 1, math.ceil(RADIUS_SHARE * len(near)) - 1)]
    rounded = int(math.ceil(at / 500.0) * 500)
    return max(RADIUS_MIN_M, min(RADIUS_MAX_M, rounded))


def cells_within(radius_m: int) -> list[tuple[int, int]]:
    n = radius_m // CELL_M
    return [(i, j) for i in range(-n, n + 1) for j in range(-n, n + 1) if _cell_metres((i, j)) <= radius_m]


_SECTORS_IT = ("nord", "nord-est", "est", "sud-est", "sud", "sud-ovest", "ovest", "nord-ovest")
_SECTORS_EN = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def sector(cell: tuple[int, int]) -> int:
    """0 = north, clockwise in eighths."""
    bearing = math.degrees(math.atan2(cell[1], cell[0])) % 360
    return int(((bearing + 22.5) % 360) // 45)


@dataclass(frozen=True)
class Fog:
    centre: Coord
    radius_m: int
    cells: int
    explored: int
    by_sector: tuple[tuple[int, int], ...]  # (explored, cells) for each eighth, north first
    never: tuple[str, ...]  # places Pisa knows within the radius where they have never been

    @property
    def share(self) -> int:
        return round(100 * self.explored / self.cells) if self.cells else 0

    def least_sector(self) -> int | None:
        open_ = [(e / c, i) for i, (e, c) in enumerate(self.by_sector) if c]
        return min(open_)[1] if open_ else None


def fog(
    centre: Coord,
    stays: Sequence[Coord],
    seen: Iterable[Coord],
    known: Sequence[tuple[str, Coord, bool]],
) -> Fog:
    """The fog around `centre`. `stays`: where they stopped (the radius); `seen`: every stay and fix (the
    cells explored); `known`: (name, where, ever stayed there) of the places Pisa knows."""
    radius = usual_radius(stays, centre)
    inside = set(cells_within(radius))
    visited = {c for c in (cell_of(p, centre) for p in seen) if c in inside}
    sectors = [[0, 0] for _ in range(8)]
    for c in inside:
        if c == (0, 0):
            continue
        s = sectors[sector(c)]
        s[1] += 1
        s[0] += c in visited
    never: list[str] = []
    for name, where, stayed in sorted(known, key=lambda k: distance_m(centre, k[1])):
        if stayed or distance_m(centre, where) > radius or cell_of(where, centre) in visited:
            continue
        if name not in never:
            never.append(name)
        if len(never) >= PLACES_MAX:
            break
    return Fog(centre, radius, len(inside), len(visited), tuple((e, c) for e, c in sectors), tuple(never))


def glint_cell(f: Fog, seen: Iterable[Coord]) -> Coord | None:
    """The centre of the nearest unexplored cell at least 400 m out (None when all of it is explored)."""
    visited = {cell_of(p, f.centre) for p in seen}
    candidates = [c for c in cells_within(f.radius_m) if c not in visited and _cell_metres(c) >= GLINT_MIN_M]
    if not candidates:
        return None
    best = min(candidates, key=lambda c: (_cell_metres(c), c))
    return cell_centre(best, f.centre)


JITTER_M = 100


def jitter(point: Coord, unit: tuple[float, float]) -> Coord:
    """The point moved by up to 100 m each way (`unit` in [-1, 1]²): a search never reveals the grid."""
    north, east = unit[0] * JITTER_M, unit[1] * JITTER_M
    lat = point[0] + north / _M_PER_DEG
    lon = point[1] + east / (_M_PER_DEG * math.cos(math.radians(point[0])))
    return (round(lat, 6), round(lon, 6))


def unexplored(point: Coord, f: Fog, seen: Iterable[Coord]) -> bool:
    """A place in a cell they have never been in, within their usual radius."""
    cell = cell_of(point, f.centre)
    return _cell_metres(cell) <= f.radius_m and cell not in {cell_of(p, f.centre) for p in seen}


def fog_sheet(f: Fog, lang: str) -> dict[str, Any]:
    """The sheet of their fog of war."""
    it = lang == "it-IT"
    names = _SECTORS_IT if it else _SECTORS_EN
    blocks: list[dict[str, Any]] = [
        {"type": "stat", "label": "Esplorato" if it else "Explored", "value": str(f.share), "unit": "%"},
        {"type": "stat", "label": "Il tuo raggio" if it else "Your radius",
         "value": f"{f.radius_m / 1000:.1f}".rstrip("0").rstrip("."), "unit": "km"},
        {"type": "fact", "label": "Zone viste" if it else "Cells seen",
         "value": f"{f.explored} / {f.cells} (250 m)"},
    ]  # fmt: skip
    rows = [[names[i], f"{round(100 * e / c)}%"] for i, (e, c) in enumerate(f.by_sector) if c]
    if rows:
        blocks += [
            {"type": "section", "title": "Per direzione" if it else "By direction"},
            {"type": "table", "rows": rows,
             "columns": ["Direzione" if it else "Direction", "Esplorato" if it else "Explored"]},
        ]  # fmt: skip
    if (least := f.least_sector()) is not None and f.share < 100:
        least_line = (
            f"La zona che conosci meno è a {names[least]}."
            if it
            else f"The part you know least is to the {names[least]}."
        )
        blocks.append({"type": "text", "text": least_line})
    if f.never:
        blocks += [
            {"type": "section", "title": "Dove non sei mai stato" if it else "Where you've never been"},
            {"type": "list", "items": [n[:200] for n in f.never]},
        ]
    return {"title": "La tua nebbia di guerra" if it else "Your fog of war", "blocks": blocks}


# --- "qualcosa di diverso" offered a hotel in another country, then a hotel nearby --------------------------

# A place to sleep is never something different to try (their trips' hotels are the places they "stays" most)
_LODGING = (
    "lodging", "hotel", "hostel", "motel", "bed_and_breakfast", "b&b", "guest_house", "guesthouse", "resort",
    "campground", "camping", "rv_park", "inn", "albergo", "alberghi", "ostello", "alloggio", "agriturismo",
    "affittacamere", "pensione", "apartment", "appartament", "residence", "airbnb",
)  # fmt: skip
IDEA_NEAR_M = 5_000  # an idea is this close to where they are now (never far, never another city or country)
HERE_FIX_FOR_S = 2 * 3600  # where they are now: a phone fix this recent …
HERE_STAY_FOR_S = 12 * 3600  # … else the stay they arrived at this recently; else Pisa doesn't know: no idea


def lodging(category: str | None) -> bool:
    """A place's kind is somewhere to sleep."""
    text = (category or "").casefold()
    return any(word in text for word in _LODGING)


def near_here(point: Coord, here: Coord) -> bool:
    """Within IDEA_NEAR_M of where they are now."""
    return distance_m(point, here) <= IDEA_NEAR_M


def glint_question(lang: str, idea: str) -> str:
    """The question, for a place in their fog."""
    return (
        f"Vuoi provare qualcosa di diverso oggi? Poco lontano da te, in una zona dove non sei mai stato, c'è "
        f"{idea}."
        if lang == "it-IT"
        else f"Fancy something different today? Not far from you, somewhere you've never been: {idea}."
    )


# --- the Map of the Life ---------------------------------------------------------------------------

SEASONS_IT = {"winter": "inverno", "spring": "primavera", "summer": "estate", "autumn": "autunno"}
SEASONS_EN = {"winter": "winter", "spring": "spring", "summer": "summer", "autumn": "autumn"}


@dataclass(frozen=True)
class Season:
    name: str  # winter | spring | summer | autumn
    start: date  # its first day
    end: date  # the day after its last

    def label(self, lang: str) -> str:
        names = SEASONS_IT if lang == "it-IT" else SEASONS_EN
        year = (
            f"{self.start.year}/{str(self.end.year)[2:]}" if self.name == "winter" else str(self.start.year)
        )
        return f"{names[self.name]} {year}"


def season_of(day: date) -> Season:
    """The meteorological season `day` is in (winter runs December to February)."""
    m = day.month
    if m in (12, 1, 2):
        year = day.year if m == 12 else day.year - 1
        return Season("winter", date(year, 12, 1), date(year + 1, 3, 1))
    if m in (3, 4, 5):
        return Season("spring", date(day.year, 3, 1), date(day.year, 6, 1))
    if m in (6, 7, 8):
        return Season("summer", date(day.year, 6, 1), date(day.year, 9, 1))
    return Season("autumn", date(day.year, 9, 1), date(day.year, 12, 1))


def season_before(season: Season) -> Season:
    return season_of(date.fromordinal(season.start.toordinal() - 1))


@dataclass(frozen=True)
class Visit:
    """One stay at a known place: its name, its local day, and whether it was their first ever there."""

    place_id: str
    name: str
    day: date
    first_ever: bool


@dataclass(frozen=True)
class TripLine:
    city: str
    country: str | None
    day: date


@dataclass(frozen=True)
class LifeMap:
    season: Season
    places: int
    firsts: tuple[str, ...]
    top: tuple[tuple[str, int], ...]  # (place, days they went), most first
    trips: tuple[TripLine, ...]
    countries: tuple[str, ...]
    days_out: int  # the days with at least one stay away from home

    @property
    def empty(self) -> bool:
        return not self.places and not self.trips


def life_map(
    season: Season,
    visits: Sequence[Visit],
    trips: Sequence[TripLine],
    countries: Iterable[str],
    days_out: int,
) -> LifeMap:
    days: dict[str, set[date]] = {}
    names: dict[str, str] = {}
    firsts: list[str] = []
    for v in sorted(visits, key=lambda v: v.day):
        days.setdefault(v.place_id, set()).add(v.day)
        names[v.place_id] = v.name
        if v.first_ever and v.name not in firsts:
            firsts.append(v.name)
    top = sorted(((names[p], len(d)) for p, d in days.items()), key=lambda t: (-t[1], t[0]))
    return LifeMap(
        season=season,
        places=len(days),
        firsts=tuple(firsts),
        top=tuple(top[:5]),
        trips=tuple(sorted(trips, key=lambda t: t.day)),
        countries=tuple(sorted({c for c in countries if c})),
        days_out=days_out,
    )


def life_sheet(m: LifeMap, lang: str) -> dict[str, Any]:
    """The sheet of a season: code's counts only."""
    it = lang == "it-IT"
    blocks: list[dict[str, Any]] = [
        {"type": "stat", "label": "Luoghi" if it else "Places", "value": str(m.places)},
        {"type": "stat", "label": "Prime volte" if it else "Firsts", "value": str(len(m.firsts))},
        {"type": "stat", "label": "Viaggi" if it else "Trips", "value": str(len(m.trips))},
        {"type": "stat", "label": "Giorni fuori casa" if it else "Days out", "value": str(m.days_out)},
    ]
    if m.top:
        name, count = m.top[0]
        blocks += [
            {"type": "section", "title": "Dove torni di più" if it else "Where you go back most"},
            {"type": "fact", "label": name[:40], "value": (f"{count} giorni" if it else f"{count} days")
             if count != 1 else ("1 giorno" if it else "1 day")},
        ]  # fmt: skip
        if len(m.top) > 1:
            columns = ["Posto" if it else "Place", "Giorni" if it else "Days"]
            blocks.append({"type": "table", "columns": columns, "rows": [[n[:80], c] for n, c in m.top]})
    if m.firsts:
        blocks += [
            {"type": "section", "title": "Le prime volte" if it else "Your firsts"},
            {"type": "list", "items": [f[:200] for f in m.firsts[:8]]},
        ]
    if m.trips:
        blocks += [
            {"type": "section", "title": "I viaggi" if it else "Trips"},
            {"type": "list", "items": [f"{t.city} ({t.day:%d/%m})"[:200] for t in m.trips[:8]]},
        ]
    if m.countries:
        blocks.append({"type": "chips", "items": list(m.countries)[:12]})
    title = (
        f"La mappa della tua vita: {m.season.label(lang)}"
        if it
        else (f"The map of your life: {m.season.label(lang)}")
    )
    return {"title": title[:120], "blocks": blocks}


def life_line(m: LifeMap, lang: str) -> str:
    """The season's notice: one line, then the sheet when they ask."""
    if lang == "it-IT":
        return (
            f"La mappa della tua vita, {m.season.label(lang)}, è pronta: {m.places} luoghi, "
            f"{len(m.firsts)} prime volte. Chiedimela quando vuoi."
        )
    return (
        f"The map of your life, {m.season.label(lang)}, is ready: {m.places} places, {len(m.firsts)} firsts. "
        "Ask me for it any time."
    )


def coords(rows: Iterable[tuple[float | None, float | None]]) -> list[Coord]:
    return [(a, b) for a, b in rows if a is not None and b is not None]
