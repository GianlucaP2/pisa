"""Pisa's read-only public data: the next departures for home and a
line's problems (Rome's RSM GTFS-RT, New York's MTA), the weather alerts where they are (the DPC bulletin, the
NWS), Rome's ZTL now (a reviewed table), the nearest pharmacies in Rome (the Lazio registry, no hours) and
when to leave for the next flight by transit.

- **Rails**: GET only, no key, nothing of their sent but a rounded point to the NWS; every answer is outside
  data (`public-data`) and names its source; read-only, never an action. Fresh copies are reused (the
  rate limit: one call per source per window, `domain.public_sources`); after a failure Pisa waits before
  asking again and uses a recent copy said as such; with neither, the answer is "couldn't read it" (`ok:
  false`), never a guess (fail quietly: nothing raises out of here).
- **Static data** (a city's stops: Rome's GTFS is ~47 MB) is loaded by the worker's tick, only for a city the
  user asked about (the first ask marks it wanted), weekly after that.
- Logs: sources, sizes, outcomes; never their place or their words.
"""

import asyncio
import io
import json
import zipfile
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import departures as flights
from pisa.domain import gtfs_rt, transit
from pisa.domain import pharmacies as pharmacy_rules
from pisa.domain import weather_alerts as alert_rules
from pisa.domain import ztl as ztl_rules
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.domain.public_sources import FAIL_BACKOFF, NYC_GROUP_OF, NYC_GROUPS, SOURCES, credit, on
from pisa.logging import get_logger
from pisa.ports.public_data import PublicDataError, PublicFetch
from pisa.services import home as home_store
from pisa.services.trips import trip_buffers

_log = get_logger("pisa.public_data")
Coord = tuple[float, float]
STATIC_BACKOFF = timedelta(hours=6)  # a failed static download (47 MB) isn't tried again for this long
LOCATION_OLD = timedelta(hours=12)  # an older location isn't "where they are"
TRAVEL_S = 4.0  # the routes tools' answer for the airport, or the usual time instead
LINES_KEY = "public_lines"  # app_settings: the lines they asked about lately (the briefing's, at most 3)
HOME_WORDS = {"home", "casa", "a casa", "per casa", "verso casa"}
# The reads that only Italian sources answer, and their source: off unless PISA_COUNTRY is IT
ITALIAN_ONLY = {"ztl_status": "rome_ztl", "nearest_pharmacy": "lazio_pharmacies"}
OFF_HERE = "That public source is Italian, and Pisa reads it only for a user in Italy (PISA_COUNTRY)."
# Where the airports are (the routes tools' destination); Italy's only for a user in Italy (PISA_COUNTRY)
ITALIAN_AIRPORTS = frozenset({"FCO", "CIA", "MXP", "LIN"})
AIRPORTS: dict[str, Coord] = {
    "FCO": (41.8, 12.239), "CIA": (41.799, 12.595), "JFK": (40.641, -73.778),
    "LGA": (40.777, -73.8740), "EWR": (40.69, -74.174), "MXP": (45.63, 8.726),
    "LIN": (45.445, 9.277),
}  # fmt: skip
# Rough boxes for the country when the phone gave no region
_ITALY = (35.4, 6.6, 47.1, 18.6)
_US = (24.4, -125.0, 49.5, -66.9)
Travel = Callable[[Coord, Coord, datetime], Awaitable[int | None]]
# The read in progress: the sources whose older copy it used, and what it says of where they are (per task)
_OLD: ContextVar[list[str] | None] = ContextVar("public_old", default=None)
_NOTES: ContextVar[list[str] | None] = ContextVar("public_notes", default=None)


def _note(text: str) -> None:
    notes = _NOTES.get()
    if notes is not None and text not in notes:
        notes.append(text)


CityName = Callable[[Coord], Awaitable[str | None]]


def _inside(box: tuple[float, float, float, float], c: Coord) -> bool:
    return box[0] <= c[0] <= box[2] and box[1] <= c[1] <= box[3]


def _failed(why: str, sources: list[str] | None = None) -> dict[str, Any]:
    return {"ok": False, "why": why, "summary": why, "credit": credit(sources or [])}


# --- the static datasets' reductions (the worker's, run once a week) ----------------------------------------


def stops_json(raw: bytes) -> bytes:
    """A GTFS zip → its stops as compact JSON ([[id, name, lat, lon, parent]]): the rest is never kept."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("stops.txt")
        if info.file_size > 20 * 1024 * 1024:
            raise ValueError("stops.txt too large")
        text = archive.read(info).decode("utf-8-sig", errors="replace")
    stops = transit.stops_from_csv(text)
    if not stops:
        raise ValueError("no stops")
    return json.dumps([[s.stop_id, s.name, s.lat, s.lon, s.parent] for s in stops]).encode()


def bulletin_json(raw: bytes) -> bytes:
    b = alert_rules.bulletin(raw)
    zones = {
        "stamp": b.stamp,
        **{
            day: [[z.zone, z.name, z.levels] for z in zl]
            for day, zl in (("today", b.today), ("tomorrow", b.tomorrow))
        },
    }
    return json.dumps(zones).encode()


def pharmacies_json(raw: bytes) -> bytes:
    found = pharmacy_rules.parse(raw.decode("latin-1"))
    if not found:
        raise ValueError("no pharmacies in Rome")
    return json.dumps([[p.code, p.name, p.address, p.cap, p.lat, p.lon] for p in found]).encode()


def nws_json(raw: bytes) -> bytes:
    return json.dumps(alert_rules.nws_alerts(json.loads(raw))).encode()


class PublicDataService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        fetch: PublicFetch,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        travel: Travel | None = None,
        city_name: CityName | None = None,
    ) -> None:
        self._pool = pool
        self._fetch = fetch
        self._owner = owner
        self._clock = clock
        self._travel = travel  # the routes tools' transit minutes (PlacesService)
        self._city_name = city_name  # the reverse geocoder's city (PlacesService.city_at)

    # --- the cache: fresh copies reused, failures waited out, a recent copy when the source is down ---------

    async def _cached(
        self,
        key: str,
        *,
        url: str | None = None,
        params: dict[str, str] | None = None,
        reduce: Callable[[bytes], bytes] | None = None,
        fetch: bool = True,
    ) -> tuple[bytes, datetime, bool] | None:
        """(the body, when it was fetched, whether it is an old copy) or None. `fetch` False: only what is
        kept (a static dataset is the worker's to download)."""
        source = SOURCES[key.split(":", 1)[0]]
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT body, fetched_at, failed_at FROM public_cache WHERE key = %s", (key,)
                )
            ).fetchone()
        body, fetched, failed = (bytes(row[0]) if row and row[0] is not None else None,
                                 row[1] if row else None, row[2] if row else None)  # fmt: skip
        if body is not None and fetched is not None and now - fetched < source.fresh:
            return body, fetched, False
        usable = body is not None and fetched is not None and now - fetched < source.stale
        backoff = FAIL_BACKOFF if source.fresh < timedelta(days=1) else STATIC_BACKOFF
        if not fetch or (failed is not None and now - failed < backoff):
            return self._old(source.name, body, fetched) if usable else None
        try:
            raw = await self._fetch.get(
                url or source.url, params=params, big=source.fresh >= timedelta(days=1), cap=source.cap
            )
            kept = await asyncio.to_thread(reduce, raw) if reduce is not None else raw
        except asyncio.CancelledError:  # cut at the step's limit, it backs off too
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO public_cache (key, failed_at, error) VALUES (%s, %s, %s) ON CONFLICT (key) "
                    "DO UPDATE SET failed_at = EXCLUDED.failed_at, error = EXCLUDED.error",
                    (key, now, "cut at the worker's limit"),
                )
            _log.warning("public_source_failed", source=source.key, cause="cut")
            raise
        except Exception as err:  # a source that fails or sends what Pisa can't read: quietly, said as such
            why = (
                err.message if isinstance(err, PublicDataError) else f"not understood ({type(err).__name__})"
            )
            async with self._pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO public_cache (key, failed_at, error) VALUES (%s, %s, %s) ON CONFLICT (key) "
                    "DO UPDATE SET failed_at = EXCLUDED.failed_at, error = EXCLUDED.error",
                    (key, now, why[:300]),
                )
            _log.warning("public_source_failed", source=source.key, cause=type(err).__name__)
            return self._old(source.name, body, fetched) if usable else None
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public_cache (key, body, fetched_at) VALUES (%s, %s, %s) ON CONFLICT (key) DO "
                "UPDATE SET body = EXCLUDED.body, fetched_at = EXCLUDED.fetched_at, failed_at = NULL, "
                "error = NULL",
                (key, kept, now),
            )
        _log.info("public_source_read", source=source.key, size=len(kept))
        return kept, now, False

    @staticmethod
    def _old(name: str, body: bytes | None, fetched: datetime | None) -> tuple[bytes, datetime, bool] | None:
        """A recent copy used while its source fails: noted, so the answer says so (never an old copy
        shown as current)."""
        if body is None or fetched is None:
            return None
        noted = _OLD.get()
        if noted is not None and name not in noted:
            noted.append(name)
        return body, fetched, True

    async def _want(self, key: str) -> None:
        """A static dataset the user needs: the worker downloads it on its next tick."""
        async with self._pool.connection() as conn:
            await conn.execute("INSERT INTO public_cache (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))

    async def tick(self) -> int:
        """The worker, every minute: the wanted static datasets older than a week (one per tick)."""
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT key FROM public_cache WHERE key IN ('rome_static', 'nyc_static') AND "
                    "(fetched_at IS NULL OR fetched_at < %s) ORDER BY fetched_at NULLS FIRST",
                    (now - SOURCES["rome_static"].fresh,),
                )
            ).fetchall()
        for (key,) in rows[:1]:
            got = await self._cached(key, reduce=stops_json)
            return 1 if got is not None and not got[2] else 0
        return 0

    # --- where they are
    # ----------------------------------------------------------------------------------------

    async def _point(self) -> Coord | None:
        """Their latest location; older than LOCATION_OLD, home instead, or, with no home, the old
        one said with its age; no location at all: home."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT lat, lon, measured_at FROM device_locations ORDER BY measured_at DESC LIMIT 1"
                )
            ).fetchone()
            home = await home_store.read(conn)
        if row is not None and self._clock() - row[2] <= LOCATION_OLD:
            return float(row[0]), float(row[1])
        if home is not None:
            if row is not None:
                _note("Where: their home (their latest location is more than 12 hours old).")
            return home.lat, home.lon
        if row is not None:
            hours = int((self._clock() - row[2]).total_seconds() // 3600)
            _note(f"Where: their location of {hours} hours ago (none newer).")
            return float(row[0]), float(row[1])
        return None

    async def _home(self) -> Coord | None:
        async with self._pool.connection() as conn:
            home = await home_store.read(conn)
        return (home.lat, home.lon) if home is not None else None

    def _city(self, point: Coord | None) -> transit.City | None:
        """The city whose transit feed covers the user, when its feed is on for the user's country."""
        city = self._feed_city(point)
        return city if city is not None and on(f"{city}_trips", self._owner.country) else None

    def _feed_city(self, point: Coord | None) -> transit.City | None:
        if point is not None:
            return transit.city_at(*point)
        context = self._owner.place  # no location, the phone's zone
        if context is not None:
            return next((c for c, tz in transit.TIMEZONES.items() if tz == context.tz), None)
        return None

    def _country(self, point: Coord | None) -> str | None:
        """Where the alerts are read for: Italy (when Pisa's Italian sources are on) or the US."""
        found = self._where(point)
        return None if found == "IT" and not on("dpc_bulletin", self._owner.country) else found

    def _where(self, point: Coord | None) -> str | None:
        context = self._owner.place
        if context is not None and context.region in ("IT", "US"):
            if point is None or _inside(_ITALY if context.region == "IT" else _US, point):
                return context.region
        if point is not None:
            return "IT" if _inside(_ITALY, point) else "US" if _inside(_US, point) else None
        return None

    async def _stops(self, city: transit.City) -> list[transit.Stop] | None:
        key = f"{city}_static"
        got = await self._cached(key, fetch=False)
        if got is None:
            await self._want(key)
            return None
        return [transit.Stop(i, n, la, lo, p) for i, n, la, lo, p in json.loads(got[0])]

    async def _trips(
        self, city: transit.City, routes: set[str] | None
    ) -> tuple[list[gtfs_rt.TripUpdate], bool]:
        """The city's realtime trip updates (NYC: the feeds of these lines, or all eight); (trips, any copy
        old)."""
        keys = ["rome_trips"] if city == "rome" else [
            f"nyc_trips_{g}" for g in (sorted({NYC_GROUP_OF.get(r, "") for r in routes} - {""})
                                       if routes and all(r in NYC_GROUP_OF for r in routes) else NYC_GROUPS)
        ]  # fmt: skip
        got = await asyncio.gather(*(self._cached(k) for k in keys))
        trips: list[gtfs_rt.TripUpdate] = []
        old = False
        for body in got:
            if body is None:
                continue
            old = old or body[2]
            try:
                trips += (await asyncio.to_thread(gtfs_rt.parse, body[0])).trips
            except gtfs_rt.FeedError:
                _log.warning("public_feed_unreadable", city=city)
        if all(g is None for g in got):
            raise PublicDataError("the realtime feed can't be read now")
        return trips, old

    async def _alerts(self, city: transit.City) -> tuple[list[gtfs_rt.Alert], bool] | None:
        if city == "rome":
            got = await self._cached("rome_alerts")
            if got is None:
                return None
            try:
                return (await asyncio.to_thread(gtfs_rt.parse, got[0], prefer="it")).alerts, got[2]
            except gtfs_rt.FeedError:
                return None
        got = await self._cached("nyc_alerts")
        if got is None:
            return None
        try:  # a malformed field drops the problems, never the answer they are part of
            return await asyncio.to_thread(transit.nyc_alerts, got[0]), got[2]
        except (ValueError, AttributeError, TypeError):
            return None

    # --- the reads (the brain's tools and Pisa's views run these)
    # ----------------------------------------------

    async def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """One read as JSON-able data; never raises (a failure is `ok: false` and why). A recent copy used
        while a source fails, and a location that isn't fresh, are said in its summary (`old`: true)."""
        old, notes = _OLD.set([]), _NOTES.set([])
        try:
            found = await self._read(tool, args)
            used, said = _OLD.get() or [], _NOTES.get() or []
        finally:
            _OLD.reset(old)
            _NOTES.reset(notes)
        if used and found.get("ok"):
            found["old"] = True
            found["summary"] = (
                f"{found['summary']} (An older copy: {', '.join(used)} couldn't be read just now.)"
            )
        if said and found.get("ok"):
            found["summary"] = f"{found['summary']} {' '.join(said)}"
        return found

    async def _read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool in ITALIAN_ONLY and not on(ITALIAN_ONLY[tool], self._owner.country):
            return _failed(OFF_HERE)
        try:
            if tool == "next_departures":
                return await self.departures(str(args.get("to") or ""), str(args.get("line") or ""))
            if tool == "transit_alerts":
                return await self.line_alerts(str(args.get("line") or ""))
            if tool == "weather_alerts":
                return await self.weather_alerts()
            if tool == "ztl_status":
                return self.ztl(str(args.get("zone") or ""))
            if tool == "nearest_pharmacy":
                return await self.pharmacies()
            if tool == "airport_departure":
                return await self.airport()
        except Exception as err:  # fail quietly: the turn goes on without it
            _log.warning("public_read_failed", tool=tool, cause=type(err).__name__)
            return _failed("Pisa couldn't read that public source just now.")
        return _failed(f"{tool[:40]} isn't a public read.")

    async def departures(self, to: str = "", line: str = "") -> dict[str, Any]:
        point = await self._point()
        city = self._city(point)
        if point is None:
            return _failed("Pisa doesn't know where the user is (no location from the phone, no home).")
        if city is None:
            return _failed(
                "No transit feed Pisa reads covers where the user is (Pisa reads Rome's and New York's)."
            )
        source = "rome" if city == "rome" else "nyc"
        stops = await self._stops(city)
        if stops is None:
            return _failed(
                f"Pisa is loading {'Rome' if city == 'rome' else 'New York'}'s stops for the first time; ask "
                "again in a few minutes.",
                [f"{source}_static"],
            )
        near = transit.nearby(stops, point)
        if not near:
            return _failed(f"No stop within {transit.NEAR_M} m of the user.", [f"{source}_static"])
        at_ids = transit.platform_ids(stops, near[: transit.NEAR_STOPS_MAX])
        home_ids: set[str] | None = None
        to_home = to.strip().casefold() in HOME_WORDS
        if to_home and (home := await self._home()) is not None:
            if distance_m(home, point) <= transit.HOME_M:
                to_home = False  # they are home: the next ones from here
            else:
                home_ids = transit.platform_ids(stops, transit.nearby(stops, home, transit.HOME_M))
        routes = transit.wanted_routes(city, line) or None
        try:
            trips, _ = await self._trips(city, routes)
        except PublicDataError as err:
            return _failed(f"The realtime feed can't be read now ({err.message})", [f"{source}_trips"])
        now = int(self._clock().timestamp())
        names = {s.stop_id: s.name for s in stops}
        found = transit.next_arrivals(trips, at_ids, names, now, home_stops=home_ids, routes=routes)
        tz = ZoneInfo(transit.TIMEZONES[city])
        rows = [
            {"line": transit.route_name(city, a.route), "toward": a.toward, "stop": a.stop,
             "at": f"{datetime.fromtimestamp(a.at, tz):%H:%M}", "in_min": transit.minutes_until(a.at, now)}
            for a in found
        ]  # fmt: skip
        sources = [f"{source}_static", "rome_trips" if city == "rome" else "nyc_trips_ace"]
        where = near[0][1].name
        if rows:
            summary = f"Next departures {'for home ' if home_ids else ''}near {where}: " + "; ".join(
                f"{r['line']} toward {r['toward']} at {r['at']} (in {r['in_min']} min, {r['stop']})"
                for r in rows
            )
        else:
            summary = (
                f"No departure {'for home ' if home_ids else ''}predicted in the next "
                f"{transit.LOOK_AHEAD_S // 60} min at the stops near {where} (the realtime feed shows only "
                "what it predicts; the metro in Rome has no realtime feed)."
            )
        return {"ok": True, "city": city, "stop": where, "for_home": bool(home_ids), "departures": rows,
                "summary": summary, "credit": credit(sources)}  # fmt: skip

    async def line_alerts(self, line: str = "") -> dict[str, Any]:
        point = await self._point()
        city = self._city(point)
        if city is None:
            return _failed(
                "No transit feed Pisa reads covers where the user is (Pisa reads Rome's and New York's)."
            )
        got = await self._alerts(city)
        key = "rome_alerts" if city == "rome" else "nyc_alerts"
        if got is None:
            return _failed("The transit alerts can't be read now.", [key])
        alerts = got[0]
        routes = transit.wanted_routes(city, line) or None
        if routes:
            await self._remember_line(city, line)
        elif city == "rome":
            routes = set(transit.ROME_METRO)  # "la metro ha problemi?": Rome's metro lines
        now = int(self._clock().timestamp())
        found = transit.problems(alerts, now, routes)
        if not line and city == "nyc":
            found = [a for a in found if not a.description.startswith("Planned")]  # the day's, not works
        rows = [{"lines": ", ".join(dict.fromkeys(transit.route_name(city, r) for r in a.routes)),
                 "text": a.header or a.description, "kind": a.description if city == "nyc" else ""}
                for a in found[:8]]  # fmt: skip
        what = line.strip() or ("the metro" if city == "rome" else "the subway")
        summary = (
            f"{len(found)} active alert(s) on {what}: "
            + " | ".join(transit.alert_line(city, a) for a in found[:5])
            if found
            else f"No active alert on {what} right now."
        )
        return {"ok": True, "city": city, "line": what, "count": len(found), "alerts": rows,
                "summary": summary, "credit": credit([key])}  # fmt: skip

    async def _remember_line(self, city: str, line: str) -> None:
        """The lines they asked about (the briefing's problems: their lines, never the whole network)."""
        key = f"{city}:{transit.line_key(line)}"
        async with self._pool.connection() as conn:
            kept = [k for k in (await get_setting(conn, LINES_KEY) or []) if isinstance(k, str) and k != key]
            await set_setting(conn, LINES_KEY, Jsonb([key, *kept][:3]))

    async def weather_alerts(self) -> dict[str, Any]:
        point = await self._point()
        country = self._country(point)
        if country == "IT":
            name = await self._city_of(point)
            if name is None or name.casefold() not in alert_rules.CITY_ZONES:
                return _failed(
                    f"Pisa has no alert zone for {name or 'where the user is'} (it knows the main Italian "
                    "cities).", ["dpc_bulletin"]
                )  # fmt: skip
            got = await self._cached("dpc_bulletin", reduce=bulletin_json)
            if got is None:
                return _failed("The Protezione Civile's bulletin can't be read now.", ["dpc_bulletin"])
            data = json.loads(got[0])
            b = alert_rules.Bulletin(
                data["stamp"],
                *[[alert_rules.ZoneLevel(z, n, lv) for z, n, lv in data[d]] for d in ("today", "tomorrow")],
            )
            lines = alert_rules.city_lines(b, name)
            level = alert_rules.worst(alert_rules.for_city(b.today, name))
            stamp = (
                f"{b.stamp[6:8]}/{b.stamp[4:6]} {b.stamp[9:11]}:{b.stamp[11:13]}"
                if len(b.stamp) >= 13
                else ""
            )
            summary = f"Protezione Civile bulletin of {stamp} for {name.title()}: " + "; ".join(lines)
            return {"ok": True, "country": "IT", "where": name.title(), "level_today": level,
                    "alerts": [{"text": t} for t in lines], "summary": summary,
                    "credit": credit(["dpc_bulletin"])}  # fmt: skip
        if country == "US" and point is not None:
            lat, lon = round(point[0], 2), round(point[1], 2)  # a rounded point: all Pisa sends
            got = await self._cached(
                f"nws_alerts:{lat:.2f},{lon:.2f}", params={"point": f"{lat:.2f},{lon:.2f}"}, reduce=nws_json
            )
            if got is None:
                return _failed("The National Weather Service can't be read now.", ["nws_alerts"])
            alerts = json.loads(got[0])
            summary = (
                "NWS alerts here: "
                + " | ".join(f"{a['event']} ({a['severity']}): {a['headline']}" for a in alerts)
                if alerts
                else "No active NWS alert where the user is."
            )
            return {"ok": True, "country": "US", "where": "here", "alerts": alerts, "summary": summary,
                    "credit": credit(["nws_alerts"])}  # fmt: skip
        return _failed("Pisa reads weather alerts in Italy (Protezione Civile) and the US (NWS) only.")

    async def _city_of(self, point: Coord | None) -> str | None:
        if point is not None and transit.city_at(*point) == "rome":
            return "roma"
        if point is not None and self._city_name is not None:
            try:
                async with asyncio.timeout(TRAVEL_S):
                    found = await self._city_name(point)
            except Exception:  # the city's name is a nicety: without it Pisa says it has no zone
                found = None
            if found:
                from pisa.domain.countries import plain

                return plain(found)
        return None

    def ztl(self, zone: str = "") -> dict[str, Any]:
        now = self._clock().astimezone(ZoneInfo("Europe/Rome"))
        zones = ztl_rules.named(zone)
        lines = ztl_rules.lines(zones, now)
        rows = []
        for z in zones:
            on, when = ztl_rules.status(z, now)
            rows.append(
                {"name": z.name, "active": on, "until" if on else "next": f"{when:%a %H:%M}" if when else ""}
            )
        summary = (
            f"Rome, {now:%a %H:%M}: " + "; ".join(lines) + f". Source: RSM «ZTL in Centro», updated "
            f"{ztl_rules.SOURCE_DATE}; permits, CUDE, electric cars and motorbikes have their own rules."
        )
        return {"ok": True, "zones": rows, "summary": summary, "credit": credit(["rome_ztl"])}

    async def pharmacies(self) -> dict[str, Any]:
        point = await self._point()
        if point is None or transit.city_at(*point) != "rome":
            return _failed(
                "Pisa knows Rome's pharmacies only (the Lazio registry), and the user isn't in Rome."
            )
        got = await self._cached("lazio_pharmacies", reduce=pharmacies_json)
        if got is None:
            return _failed("The Lazio pharmacy registry can't be read now.", ["lazio_pharmacies"])
        known = [pharmacy_rules.Pharmacy(*row) for row in json.loads(got[0])]
        near = pharmacy_rules.nearest(known, point)
        if not near:
            return _failed(f"No pharmacy in the registry within {pharmacy_rules.NEAR_MAX_M} m.",
                           ["lazio_pharmacies"])  # fmt: skip
        rows = [{"name": p.name, "address": f"{p.address}, {p.cap} Roma", "distance_m": d,
                 "map_url": pharmacy_rules.link(p)} for d, p in near]  # fmt: skip
        summary = (
            "Nearest pharmacies by the regional registry (hours and on-duty rota unknown; its points can be "
            "off, the address is what counts): "
        ) + "; ".join(pharmacy_rules.line(d, p) for d, p in near)
        return {"ok": True, "pharmacies": rows, "summary": summary, "credit": credit(["lazio_pharmacies"])}

    async def airport(self) -> dict[str, Any]:
        flight = await self._next_flight()
        if flight is None:
            return _failed(f"No flight in the next {flights.LOOK_DAYS} days in the user's trips or calendar.")
        async with self._pool.connection() as conn:
            buffers = await trip_buffers(conn)
        point = await self._point()
        minutes: int | None = None
        known = flight.airport not in ITALIAN_AIRPORTS or self._owner.in_country("IT")
        target = AIRPORTS.get(flight.airport) if known else None
        if self._travel is not None and point is not None and target is not None:
            buffer = buffers["international_min" if flight.abroad else "domestic_min"]
            try:
                async with asyncio.timeout(TRAVEL_S):
                    minutes = await self._travel(point, target, flight.at - timedelta(minutes=buffer))
            except Exception as err:  # the usual time instead
                _log.info("public_airport_route_skipped", cause=type(err).__name__)
        leave, way, fallback = flights.leave_by(flight, minutes, buffers)
        tz = self._owner.zone
        problems: list[str] = []
        if (lines := flights.AIRPORT_LINES.get(flight.airport)) is not None and on(
            f"{lines[0]}_alerts", self._owner.country
        ):
            city, routes = lines
            got = await self._alerts(city)  # type: ignore[arg-type]
            if got is not None:
                now = int(self._clock().timestamp())
                problems = [transit.alert_line(city, a)  # type: ignore[arg-type]
                            for a in transit.problems(got[0], now, routes)[:3]]  # fmt: skip
        local_dep = flight.at.astimezone(tz)
        summary = (
            f"Flight {flight.ref or ''} from {flight.airport} on {local_dep:%a %d %b at %H:%M}: leave by "
            f"{leave.astimezone(tz):%a %H:%M} by public transport ({way} min "
            f"{'usual time from the centre' if fallback else 'from where the user is'} + the airport's "
            f"{buffers['international_min' if flight.abroad else 'domestic_min']} min)."
            + (" Problems on the way: " + " | ".join(problems) if problems else "")
        )
        return {"ok": True, "flight": flight.ref, "airport": flight.airport,
                "departs": f"{local_dep:%a %H:%M}", "leave_at": f"{leave.astimezone(tz):%a %H:%M}",
                "way_min": way, "usual_time": fallback,
                "problems": problems, "summary": summary,
                "credit": credit(["rome_alerts" if flight.airport in ("FCO", "CIA") else "nyc_alerts"])
                if problems else ""}  # fmt: skip

    async def _next_flight(self) -> flights.Flight | None:
        now = self._clock()
        until = now + timedelta(days=flights.LOOK_DAYS)
        found: list[flights.Flight] = []
        async with self._pool.connection() as conn:
            trips = await (
                await conn.execute(
                    "SELECT legs FROM trips WHERE status = 'planned' AND doubt IS NULL AND depart_at < %s "
                    "AND coalesce(back_at, arrive_at) > %s",
                    (until, now),
                )
            ).fetchall()
            events = await (
                await conn.execute(
                    "SELECT title, location, starts_at FROM calendar_events WHERE NOT all_day AND status <> "
                    "'cancelled' AND starts_at > %s AND starts_at < %s ORDER BY starts_at LIMIT 50",
                    (now, until),
                )
            ).fetchall()
        for (legs,) in trips:
            for leg in legs or []:
                f = flights.from_leg(leg) if isinstance(leg, dict) else None
                if f is not None and now < f.at < until:
                    found.append(f)
        for title, location, start in events:
            f = flights.from_event(str(title or ""), location, start)
            if f is not None:
                found.append(f)
        return min(found, key=lambda f: f.at) if found else None

    # --- the morning brief: whichever city they are in
    # ---------------------------------------------------------

    async def briefing_lines(self) -> list[str]:
        """Facts for the briefing (English, data): the weather alert where they are (not green / Moderate+),
        the problems on Rome's metro or on the lines they asked about, and a flight today's leave-by. Never
        fails."""
        out: list[str] = []
        try:
            w = await self.weather_alerts()
            if w.get("ok"):
                if w.get("country") == "IT" and w.get("level_today") not in (None, "verde"):
                    out.append(str(w["summary"]))
                if w.get("country") == "US":
                    said = [a for a in w.get("alerts") or [] if alert_rules.worth_saying(a)]
                    if said:
                        out.append("NWS: " + " | ".join(f"{a['event']}: {a['headline']}" for a in said[:2]))
        except Exception as exc:
            _log.warning("public_brief_skipped", part="weather", cause=type(exc).__name__)
        try:
            point = await self._point()
            city = self._city(point)
            async with self._pool.connection() as conn:
                lines = [k for k in (await get_setting(conn, LINES_KEY) or []) if isinstance(k, str)]
            mine = [k.split(":", 1)[1] for k in lines if city and k.startswith(f"{city}:")]
            if city == "rome" and not mine:
                a = await self.line_alerts("")
                if a.get("ok") and a.get("count"):
                    out.append(str(a["summary"]))
            for line in mine[:2]:
                a = await self.line_alerts(line)
                if a.get("ok") and a.get("count"):
                    out.append(str(a["summary"]))
        except Exception as exc:
            _log.warning("public_brief_skipped", part="transit", cause=type(exc).__name__)
        try:
            flight = await self._next_flight()
            today = self._owner.day(self._clock())
            if flight is not None and flight.at.astimezone(self._owner.zone).date() == today:
                a = await self.airport()
                if a.get("ok"):
                    out.append(str(a["summary"]))
        except Exception as exc:
            _log.warning("public_brief_skipped", part="airport", cause=type(exc).__name__)
        return out

    # --- the sources, for Settings --------------------------------------------------------------------------

    async def sources(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = {
                k.split(":", 1)[0]: (f, x)
                for k, f, x in await (
                    await conn.execute("SELECT key, fetched_at, failed_at FROM public_cache ORDER BY key")
                ).fetchall()
            }
        out: list[dict[str, Any]] = []
        for s in SOURCES.values():
            fetched, failed = rows.get(s.key, (None, None))
            status = "reviewed" if s.licence == "reviewed table" else (
                "failing" if failed is not None and (fetched is None or failed > fetched)
                else "ok" if fetched is not None else "unused"
            )  # fmt: skip
            out.append({"key": s.key, "name": s.name, "licence": s.licence, "attribution": s.attribution,
                        "url": s.url, "official": s.official, "status": status,
                        "fetched_at": fetched.isoformat() if fetched else None})  # fmt: skip
        return out


def transit_minutes(places: Any) -> Travel:
    """The routes tools' transit minutes (`PlacesService.travel_between`), arriving by a time."""

    async def travel(origin: Coord, destination: Coord, arrive_by: datetime) -> int | None:
        estimates, _, _ = await places.travel_between(origin, [destination], "transit", arrive_by=arrive_by)
        first = estimates[0] if estimates else None
        return first.best_minutes if first is not None else None

    return travel
