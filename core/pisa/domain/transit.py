"""Skills 2 and 3: the next bus or train "for home" and the line's problems, from the city's open
feeds (Rome: Roma Servizi per la Mobilità's GTFS + GTFS-RT; New York: the MTA's subway GTFS-RT and alerts).

- **Where**: the city is the one the user's latest location is in, never a fixed one.
- **The next ones**: the real-time trip updates at the stops within a short walk of them; "per casa" keeps the
  trips that go on to a stop near home after their stop (no timetable guess: only what the feed predicts).
- **Problems**: the alerts active now on the lines they name (or on the metro/subway as a whole).
- Every line is data from outside; nothing here acts. No I/O."""

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pisa.domain.gtfs_rt import Alert, TripUpdate, clean
from pisa.domain.places import distance_m

City = Literal["rome", "nyc"]
NEAR_M = 450  # a stop within a short walk (about 5 minutes)
NEAR_STOPS_MAX = 30  # Termini alone is a dozen stop ids
HOME_M = 600  # "per casa": a stop this close to home
LIST_MAX = 6
STOPS_MAX = 25_000
LOOK_AHEAD_S = 90 * 60  # predictions further ahead than this aren't "the next one"
# Boxes around each city's transit area (a location outside both has no feed)
_BOXES: dict[City, tuple[float, float, float, float]] = {
    "rome": (41.60, 12.20, 42.15, 12.90),
    "nyc": (40.45, -74.30, 40.95, -73.65),
}
TIMEZONES: dict[City, str] = {"rome": "Europe/Rome", "nyc": "America/New_York"}
# The metro lines (Rome's RSM route ids; every NYC subway route is one)
ROME_METRO = {"MEA": "Metro A", "MEB": "Metro B", "MEB1": "Metro B1", "MEC": "Metro C"}


@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    lat: float
    lon: float
    parent: str | None = None


@dataclass(frozen=True)
class Arrival:
    route: str
    stop: str  # the stop's name
    at: int  # POSIX seconds, as predicted by the feed
    toward: str  # the trip's last stop in the feed


def city_at(lat: float, lon: float) -> City | None:
    for city, (s, w, n, e) in _BOXES.items():
        if s <= lat <= n and w <= lon <= e:
            return city
    return None


def route_name(city: City, route: str) -> str:
    if city == "rome":
        return ROME_METRO.get(route, route)
    return route


def line_key(text: str) -> str:
    """A line as the user says it ("la A", "metro b", "il 64", "the 7 train", "linea C") → the feed's id
    form, upper case ("A", "B", "64", "7", "C")."""
    words = re.findall(r"[a-z0-9]+", text.casefold())
    words = [
        w
        for w in words
        if w
        not in {
            "la",
            "il",
            "lo",
            "l",
            "metro",
            "metropolitana",
            "linea",
            "line",
            "the",
            "train",
            "treno",
            "bus",
            "autobus",
            "tram",
            "subway",
            "di",
        }
    ]
    return words[0].upper() if words else ""


def wanted_routes(city: City, line: str) -> set[str]:
    """The feed's route ids for the line they named: Rome's "A" is the metro MEA (and "B" both B and B1)."""
    key = line_key(line)
    if not key:
        return set()
    if city == "rome" and key in {"A", "B", "C", "B1"}:
        return {f"ME{key}"} | ({"MEB1"} if key == "B" else set())
    return {key}


# --- the static stops ---------------------------------------------------------------------------------------


def _float(text: str | None) -> float | None:
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if value == value else None  # NaN is no coordinate


def stops_from_csv(text: str) -> list[Stop]:
    """GTFS `stops.txt`: stops and stations with coordinates (at most STOPS_MAX; a row without numbers is
    skipped)."""
    out: list[Stop] = []
    for row in csv.DictReader(io.StringIO(text)):
        lat, lon = _float(row.get("stop_lat")), _float(row.get("stop_lon"))
        stop_id = (row.get("stop_id") or "").strip()
        if lat is None or lon is None or not stop_id:
            continue
        name = " ".join((row.get("stop_name") or stop_id).split())[:80]
        out.append(Stop(stop_id[:40], name, lat, lon, (row.get("parent_station") or "").strip() or None))
        if len(out) >= STOPS_MAX:
            break
    return out


def nearby(stops: list[Stop], at: tuple[float, float], radius_m: int = NEAR_M) -> list[tuple[int, Stop]]:
    """Stops within the radius, closest first (a cheap box first: the city has thousands)."""
    dlat = radius_m / 111_000
    found = [
        (distance_m(at, (s.lat, s.lon)), s)
        for s in stops
        if abs(s.lat - at[0]) <= dlat and abs(s.lon - at[1]) <= dlat * 1.6
    ]
    return sorted(((d, s) for d, s in found if d <= radius_m), key=lambda pair: pair[0])


def platform_ids(stops: list[Stop], near: list[tuple[int, Stop]]) -> set[str]:
    """The ids a feed predicts at: the stops themselves and, for a station, its platforms (NYC's "A27N")."""
    ids = {s.stop_id for _, s in near}
    return ids | {s.stop_id for s in stops if s.parent in ids}


# --- the next ones ------------------------------------------------------------------------------------------


def next_arrivals(
    trips: list[TripUpdate],
    at_stops: set[str],
    names: dict[str, str],
    now: int,
    *,
    home_stops: set[str] | None = None,
    routes: set[str] | None = None,
    limit: int = LIST_MAX,
) -> list[Arrival]:
    """The predicted arrivals at their stops from now on (not past ones, not beyond LOOK_AHEAD_S), soonest
    first; `home_stops`: only trips that reach one of them later; `routes`: only those lines."""
    out: list[Arrival] = []
    for trip in trips:
        if trip.cancelled or (routes and trip.route_id not in routes):
            continue
        for i, stop in enumerate(trip.stops):
            if stop.stop_id not in at_stops or stop.skipped:
                continue
            if i == len(trip.stops) - 1 or trip.stops[-1].stop_id in at_stops:
                break  # it ends here (or at this station): nothing leaves
            when = stop.arrival or stop.departure
            if when is None or not now - 30 <= when <= now + LOOK_AHEAD_S:
                continue
            if home_stops is not None and not any(s.stop_id in home_stops for s in trip.stops[i + 1 :]):
                break
            last = trip.stops[-1].stop_id if trip.stops else ""
            out.append(Arrival(trip.route_id, names.get(stop.stop_id, stop.stop_id), when,
                               names.get(last, last)))  # fmt: skip
            break
    out.sort(key=lambda a: a.at)
    kept: list[Arrival] = []
    for arrival in out:  # at most two of the same line and direction: the list stays a choice
        if sum(1 for k in kept if (k.route, k.toward) == (arrival.route, arrival.toward)) < 2:
            kept.append(arrival)
    return kept[:limit]


def minutes_until(at: int, now: int) -> int:
    return max(0, round((at - now) / 60))


# --- problems -----------------------------------------------------------------------------------------------


def active(alert: Alert, now: int) -> bool:
    if not alert.periods:
        return True
    return any((s is None or s <= now) and (e is None or now < e) for s, e in alert.periods)


def problems(alerts: list[Alert], now: int, routes: set[str] | None) -> list[Alert]:
    """The alerts active now on these lines (None: every line the caller passed alerts for)."""
    return [a for a in alerts if active(a, now) and (routes is None or set(a.routes) & routes)]


def nyc_alerts(body: Any) -> list[Alert]:
    """The MTA's subway alerts feed (GTFS-RT as JSON, with its Mercury extensions): each alert's lines,
    active periods, its kind ("Delays", "Suspended"…) and its plain English header."""
    if isinstance(body, bytes | str):
        body = json.loads(body)
    out: list[Alert] = []
    for entity in (body or {}).get("entity") or []:
        alert = entity.get("alert") if isinstance(entity, dict) else None
        if not isinstance(alert, dict):
            continue
        routes = tuple(
            clean(str(e.get("route_id")), 10) for e in alert.get("informed_entity") or []
            if isinstance(e, dict) and e.get("route_id")
        )  # fmt: skip
        periods = tuple(
            (_seconds(p.get("start")), _seconds(p.get("end")))
            for p in alert.get("active_period") or []
            if isinstance(p, dict)
        )
        header = ""
        texts = alert.get("header_text")
        for t in (texts.get("translation") if isinstance(texts, dict) else None) or []:
            if isinstance(t, dict) and t.get("language") in ("en", None, ""):
                header = clean(str(t.get("text") or ""), 400)
                break
        mercury = alert.get("transit_realtime.mercury_alert")
        kind = clean(str((mercury if isinstance(mercury, dict) else {}).get("alert_type") or ""), 40)
        out.append(Alert(clean(str(entity.get("id") or ""), 80), routes, (), periods, header, kind))
    return out


def _seconds(value: Any) -> int | None:
    """A period's bound as POSIX seconds ("1793704424" or 1793704424), else open."""
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def alert_line(city: City, alert: Alert) -> str:
    lines = ", ".join(dict.fromkeys(route_name(city, r) for r in alert.routes)) or "-"
    text = alert.header or alert.description
    if text.casefold().startswith(lines.casefold()):  # "Metro A: rallentamenti…" names its line already
        return text[:300]
    return f"{lines}: {text}"[:300]
