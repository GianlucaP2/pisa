"""Rows 2-3 (pisa.domain.explore): their fog of war, the weekly glint, the Map of the Life.

- `fog_sheet` (their word: "mostrami la nebbia"): the sheet of the ground around their centre they know and
  doesn't, from their stays (365 days) and location fixes (30 days); no provider call.
- `glint` (the rituals' something different): at most once a week, one new place in a cell they have
  never been in, near their centre, as a question; one provider search when it runs (the rituals' one look a
  day).
- `life_sheet` (their word, "la mappa della mia vita") and `season_tick` (the rituals' tick: the first day of
  a season at 19:00, once a season, the season just ended): code's counts only, never a model.

Logs carry ids and counts only: never a place, a name or a coordinate.
"""

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import explore as rules
from pisa.domain import rituals as ritual_rules
from pisa.domain.errors import PisaError
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services import home as home_store

if TYPE_CHECKING:
    from pisa.services.places import PlacesService

_log = get_logger("pisa.explore")
STAYS_FOR = timedelta(days=365)
FIXES_FOR = timedelta(days=30)
FIX_ACCURACY_M = 100
GLINT_SEARCHED_KEY = "explore_glint_searched"  # the Monday of the week Pisa last searched for a glint
SEASON_AT = ritual_rules.WEEK_POSTER_AT  # 19:00 local, on the new season's first day
Idea = dict[str, str]


class ExploreService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        places: "PlacesService | None" = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._places = places
        self._clock = clock
        self._random: Callable[[], tuple[float, float]] = _unit

    # --- the ground -------------------------------------------------------------------------------------

    async def centre(self, conn: AsyncConnection) -> rules.Coord | None:
        """Home when they have one; else the place they stay at most; else where they stayed last."""
        home = await home_store.read(conn)
        if home is not None:
            return home.coord
        row = await (
            await conn.execute(
                "SELECT p.lat, p.lon FROM stays s JOIN places p ON p.id = s.place_id WHERE p.lat IS NOT NULL "
                "GROUP BY p.id, p.lat, p.lon ORDER BY count(*) DESC LIMIT 1"
            )
        ).fetchone()
        if row is None:
            last = await conn.execute("SELECT lat, lon FROM stays ORDER BY arrived_at DESC LIMIT 1")
            row = await last.fetchone()
        return (row[0], row[1]) if row else None

    async def _ground(self, conn: AsyncConnection) -> tuple[rules.Fog, list[rules.Coord]] | None:
        centre = await self.centre(conn)
        if centre is None:
            return None
        now = self._clock()
        stays = rules.coords(
            await (
                await conn.execute("SELECT lat, lon FROM stays WHERE arrived_at > %s", (now - STAYS_FOR,))
            ).fetchall()
        )
        fixes = rules.coords(
            await (
                await conn.execute(
                    "SELECT lat, lon FROM device_locations WHERE measured_at > %s AND accuracy_m <= %s",
                    (now - FIXES_FOR, FIX_ACCURACY_M),
                )
            ).fetchall()
        )
        seen = stays + fixes
        # the places Pisa knows near them (their own searches), and whether they ever stayed at one
        span = rules.RADIUS_MAX_M / 111_320.0 * 2
        known = [
            (name, (lat, lon), bool(stayed))
            for name, lat, lon, stayed in await (
                await conn.execute(
                    "SELECT p.name, p.lat, p.lon, EXISTS (SELECT 1 FROM stays s WHERE s.place_id = p.id) "
                    "FROM places p WHERE p.lat BETWEEN %s AND %s AND p.lon BETWEEN %s AND %s LIMIT 2000",
                    (centre[0] - span, centre[0] + span, centre[1] - span * 2, centre[1] + span * 2),
                )
            ).fetchall()
        ]
        return rules.fog(centre, stays, seen, known), seen

    async def fog_sheet(self, lang: str) -> dict[str, Any] | None:
        """Their fog of war as a sheet (None before Pisa knows where they go)."""
        async with self._pool.connection() as conn:
            ground = await self._ground(conn)
        if ground is None:
            return None
        _log.info("fog_shown", explored=ground[0].explored, cells=ground[0].cells)
        return rules.fog_sheet(ground[0], lang)

    # --- one glint a week -------------------------------------------------------------------------

    def _week_start(self) -> tuple[datetime, str]:
        local = self._owner.local(self._clock())
        monday = local.date() - timedelta(days=local.weekday())
        return local_day_bounds(monday, self._owner.tz)[0], monday.isoformat()

    async def glint_due(self, conn: AsyncConnection) -> bool:
        """No glint searched for, nor offered, yet this week (their local week, Monday first): one provider
        search a week at most."""
        week_start, monday = self._week_start()
        if await get_setting(conn, GLINT_SEARCHED_KEY) == monday:
            return False
        row = await (
            await conn.execute(
                "SELECT 1 FROM ritual_asks WHERE kind = 'idea' AND payload->>'glint' = 'true' AND "
                "created_at >= %s LIMIT 1",
                (week_start,),
            )
        ).fetchone()
        return row is None

    async def here(self, conn: AsyncConnection) -> rules.Coord | None:
        return await where_now(conn, self._clock())

    async def _away(self, conn: AsyncConnection, centre: rules.Coord) -> bool:
        """They aren't around their usual ground (the phone's zone isn't home's, or their last
        stay is more than 50 km from it): "poco lontano da te" would be a lie there."""
        if self._owner.away:
            return True
        last = await conn.execute("SELECT lat, lon FROM stays ORDER BY arrived_at DESC LIMIT 1")
        row = await last.fetchone()
        return row is not None and distance_m(centre, (row[0], row[1])) > rules.FAR_M

    async def glint(self, declined: set[str], used: set[str]) -> list[Idea]:
        """One new place in their fog: of the kind they go to most (else a café), in the nearest cell they
        have never been in, never a place they stayed at, never one offered before (the same key as every idea
        of that place). Only around home, and one search a week. The provider's names are data: only a name
        and an address are said."""
        if self._places is None:
            return []
        async with self._pool.connection() as conn:
            if not await self.glint_due(conn):
                return []
            ground = await self._ground(conn)
            if ground is None or await self._away(conn, ground[0].centre):
                return []
            here = await self.here(conn) or ground[0].centre
            if not rules.near_here(ground[0].centre, here):
                return []  # they are out of their own ground now: "poco lontano da te" would be a lie
            kinds = await (
                await conn.execute(
                    "SELECT p.category FROM stays s JOIN places p ON p.id = s.place_id WHERE "
                    "p.category IS NOT NULL GROUP BY p.category ORDER BY count(*) DESC LIMIT 5"
                )
            ).fetchall()
            visited = {
                str(r[0])
                for r in await (
                    await conn.execute("SELECT DISTINCT place_id FROM stays WHERE place_id IS NOT NULL")
                ).fetchall()
            }
            lang = await self._owner.lang(conn)
        fog, seen = ground
        target = rules.glint_cell(fog, seen)
        if target is None:
            return []
        async with self._pool.connection() as conn:
            await set_setting(conn, GLINT_SEARCHED_KEY, Jsonb(self._week_start()[1]))
        kind = next(  # never a health kind ("a pharmacy you haven't been to")
            (
                str(k[0]) for k in kinds
                if str(k[0]) not in declined and not rules.lodging(str(k[0]))
                and not sensitive_place(None, str(k[0]))
            ),
            None,
        )  # fmt: skip
        query = kind or ("caffè" if lang == "it-IT" else "cafe")
        near = rules.jitter(target, self._random())  # never home's grid offset, search by search
        try:
            found = await self._places.search(query, near=near, limit=5, radius_m=rules.CELL_M * 3)  # type: ignore[arg-type]
        except PisaError as err:
            _log.info("glint_search_skipped", cause=err.code)
            return []
        for place in found:
            lat, lon = place.get("lat"), place.get("lon")
            key = ritual_rules.idea_key(query, str(place.get("id") or place.get("name")))
            if lat is None or lon is None or str(place.get("id")) in visited or key in used:
                continue
            if not rules.unexplored((lat, lon), fog, seen) or rules.lodging(place.get("category")):
                continue
            if not rules.near_here((lat, lon), here):
                continue
            name = str(place.get("name") or "")[:80]
            where = str(place.get("address") or "")[:80]
            _log.info("glint_found")
            return [{"kind": query, "key": key, "text": f"{name}, {where}" if where else name, "glint": "1"}]
        return []

    # --- the Map of the Life ------------------------------------------------------------------------------

    async def life_map(self, season: rules.Season) -> rules.LifeMap:
        tz = self._owner.zone
        lo = local_day_bounds(season.start, self._owner.tz)[0]
        hi = local_day_bounds(season.end, self._owner.tz)[0]
        async with self._pool.connection() as conn:
            centre = await self.centre(conn)
            rows = await (
                await conn.execute(
                    "SELECT s.place_id::text, p.name, s.arrived_at, s.arrived_at = (SELECT min(f.arrived_at) "
                    "FROM stays f WHERE f.place_id = s.place_id), p.category FROM stays s JOIN places p ON "
                    "p.id = s.place_id WHERE s.arrived_at >= %s AND s.arrived_at < %s",
                    (lo, hi),
                )
            ).fetchall()
            out = await (
                await conn.execute(
                    "SELECT arrived_at, lat, lon FROM stays WHERE arrived_at >= %s AND arrived_at < %s",
                    (lo, hi),
                )
            ).fetchall()
            trips = await (
                await conn.execute(
                    "SELECT to_city, to_country, depart_at FROM trips WHERE status = 'planned' AND doubt IS "
                    "NULL AND depart_at >= %s AND depart_at < %s AND depart_at <= %s",
                    (lo, hi, self._clock()),
                )
            ).fetchall()
            regions = await (
                await conn.execute(
                    "SELECT DISTINCT unnest(phone_regions) FROM residency_days WHERE day >= %s AND day < %s",
                    (season.start, season.end),
                )
            ).fetchall()
        visits = [  # a sensitive place is never on the map Pisa shows them
            rules.Visit(pid, name, at.astimezone(tz).date(), bool(first))
            for pid, name, at, first, category in rows
            if not sensitive_place(name, category)
        ]
        away = {
            at.astimezone(tz).date()
            for at, lat, lon in out
            if centre is None or distance_m(centre, (lat, lon)) > rules.GLINT_MIN_M
        }
        lines = [
            rules.TripLine(city or "?", country, at.astimezone(tz).date()) for city, country, at in trips
        ]
        countries = [r[0] for r in regions] + [t.country for t in lines if t.country]
        return rules.life_map(season, visits, lines, countries, len(away))

    async def life_sheet(self, lang: str, which: str = "current") -> dict[str, Any] | None:
        """A season's Map of the Life as a sheet: `current` (so far) or `last` (the one before)."""
        today = self._owner.day(self._clock())
        season = rules.season_of(today)
        if which == "last":
            season = rules.season_before(season)
        m = await self.life_map(season)
        if m.empty:
            return None
        return rules.life_sheet(m, lang)

    async def season_tick(self, store: Callable[..., Any], notify: Callable[..., Any]) -> bool:
        """The rituals' tick: on a season's first day at 19:00 (once a season), the season just ended as a
        sheet kept like a ritual's, and one line saying it's ready. `store(conn, ritual, day, data, actions)`
        and `notify(conn, ritual, text, lang, sheet_id)` are the rituals' own."""
        local = self._owner.local(self._clock())
        season = rules.season_of(local.date())
        if local.date() != season.start or ritual_rules.due_at(local, SEASON_AT) is None:
            return False
        ended = rules.season_before(season)
        async with self._pool.connection() as conn:
            made = await (
                await conn.execute(
                    "SELECT 1 FROM ritual_sheets WHERE ritual = 'life_map' AND day = %s", (ended.start,)
                )
            ).fetchone()
            lang = await self._owner.lang(conn)
        if made is not None:
            return False
        m = await self.life_map(ended)
        if m.empty:
            return False
        async with self._pool.connection() as conn, conn.transaction():
            sheet_id: uuid.UUID | None = await store(
                conn, "life_map", ended.start, rules.life_sheet(m, lang), []
            )
            if sheet_id is None:
                return False
            # the notice names no ritual and no sheet: the schema's enum doesn't know life_map yet (an older
            # app must not fail on it); they ask for the sheet ("la mappa della mia vita")
            await notify(conn, None, rules.life_line(m, lang), lang, None)
        _log.info("life_map_made", sheet_id=str(sheet_id), places=m.places, trips=len(m.trips))
        return True


def _unit() -> tuple[float, float]:
    """A random offset in [-1, 1]² (the glint's search point: not a secret, only no fixed grid)."""
    return (random.uniform(-1, 1), random.uniform(-1, 1))  # noqa: S311


async def where_now(conn: AsyncConnection, now: datetime) -> rules.Coord | None:
    """Where they are now: a recent accurate phone fix, else the stay they arrived at lately; None
    when Pisa doesn't know (then no idea is offered: fails closed)."""
    row = await (
        await conn.execute(
            "SELECT lat, lon FROM device_locations WHERE measured_at > %s AND accuracy_m <= %s "
            "ORDER BY measured_at DESC LIMIT 1",
            (now - timedelta(seconds=rules.HERE_FIX_FOR_S), FIX_ACCURACY_M),
        )
    ).fetchone()
    if row is None:
        row = await (
            await conn.execute(
                "SELECT lat, lon FROM stays WHERE arrived_at > %s ORDER BY arrived_at DESC LIMIT 1",
                (now - timedelta(seconds=rules.HERE_STAY_FOR_S),),
            )
        ).fetchone()
    return (float(row[0]), float(row[1])) if row else None
