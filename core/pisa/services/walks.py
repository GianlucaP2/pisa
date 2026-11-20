"""The end-of-walk recap (pisa.domain.walks).

Run by the rituals' minute tick (Pisa's initiative: quiet hours and "ferma tutto" send nothing, never a recap
held for later). When the phone has stopped at a known place (one of their places, or home) within the last 20
minutes, and the stretch before the stop was a walk of 15 minutes or more, Pisa says once a day what they said
during it, from the records themselves (tasks they added by voice, place notes). Logs carry counts only: never
a place, a coordinate or a title.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import geo_rules as geo
from pisa.domain import walks as rules
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services import zone
from pisa.services.notifications import enqueue
from pisa.services.zone import located_phone

_log = get_logger("pisa.walks")
LOOK_BACK = timedelta(hours=3)  # a walk is looked for at most this far before the stop


class WalksService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    async def _known(self, conn: AsyncConnection, place_id: object, lat: float, lon: float) -> bool:
        if place_id is not None:  # a walk that ended at a sensitive place gets no recap
            row = await (
                await conn.execute("SELECT name, category FROM places WHERE id = %s", (place_id,))
            ).fetchone()
            return row is not None and not sensitive_place(row[0], row[1])
        home = await home_store.read(conn)
        return home is not None and distance_m(home.coord, (lat, lon)) <= geo.HOME_M

    async def ended_walk(self, conn: AsyncConnection) -> rules.Walk | None:
        """The walk that just ended at a known place, if any (the stop began in the last 20 minutes)."""
        now = self._clock()
        device = await located_phone(conn)
        if device is None:
            return None
        stay = await (
            await conn.execute(
                "SELECT arrived_at, lat, lon, place_id FROM stays WHERE device_id = %s AND departed_at "
                "IS NULL AND arrived_at > %s AND arrived_at <= %s ORDER BY arrived_at DESC LIMIT 1",
                (device, now - rules.RECAP_AFTER, now),
            )
        ).fetchone()
        if stay is None:
            return None
        arrived, lat, lon, place_id = stay
        if not await self._known(conn, place_id, lat, lon):
            return None
        before = await (
            await conn.execute(
                "SELECT max(coalesce(departed_at, last_seen_at)) FROM stays WHERE device_id = %s "
                "AND arrived_at < %s",
                (device, arrived),
            )
        ).fetchone()
        since = max(before[0], arrived - LOOK_BACK) if before and before[0] else arrived - LOOK_BACK
        fixes = await zone.fixes(conn, device, since, arrived)
        return rules.walk_before(fixes, since, arrived)

    async def said_during(self, conn: AsyncConnection, walk: rules.Walk) -> list[rules.Item]:
        """What their own words made during the walk, oldest first: every item is a record (none invented)."""
        window = (walk.start, walk.end)
        rows: list[tuple[datetime, str, str]] = []
        for at, title in await (
            await conn.execute(
                "SELECT created_at, title FROM tasks WHERE source_kind = 'voice' AND created_at BETWEEN %s "
                "AND %s",
                window,
            )
        ).fetchall():
            rows.append((at, "task", title))
        for at, text in await (
            await conn.execute(
                "SELECT created_at, text FROM place_notes WHERE turn_id IS NOT NULL AND created_at "
                "BETWEEN %s AND %s",
                window,
            )
        ).fetchall():
            rows.append((at, "note", text))
        rows.sort(key=lambda r: r[0])
        return [rules.Item(kind, text) for _, kind, text in rows if text]  # type: ignore[arg-type]

    async def tick(self) -> bool:
        """Once a day at most: the recap of a walk that just ended (quiet hours are the caller's)."""
        now = self._clock()
        day = now.astimezone(self._owner.zone).date()
        async with self._pool.connection() as conn, conn.transaction():
            done = await (await conn.execute("SELECT 1 FROM walk_recaps WHERE day = %s", (day,))).fetchone()
            if done is not None:
                return False
            walk = await self.ended_walk(conn)
            if walk is None:
                return False
            items = await self.said_during(conn, walk)
            lang = await self._owner.lang(conn)
            text = rules.recap(items, walk.minutes, lang)
            if text is None:  # nothing said on the walk: nothing to recap (a later walk today still may)
                return False
            inserted = await (
                await conn.execute(
                    "INSERT INTO walk_recaps (day, walk_start, walk_end, items, created_at) VALUES "
                    "(%s, %s, %s, %s, %s) ON CONFLICT (day) DO NOTHING RETURNING day",
                    (day, walk.start, walk.end, min(len(items), 20), now),
                )
            ).fetchone()
            if inserted is None:
                return False
            notification_id, _ = await enqueue(
                conn,
                kind="info",
                text=text,
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                respect_quiet=False,
                path="walk.recap",
            )  # quiet was checked by the rituals' tick: a recap is never held for the morning
        _log.info(
            "walk_recap_sent", notification_id=str(notification_id), items=len(items), minutes=walk.minutes
        )
        return True
