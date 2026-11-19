"""Errand combos. Once a day, from 9:00 to 19:00 where they are, Pisa looks at their open
tasks that have a place (never one done online, never one they set to start later) and today's events with a
place: two errands a short walk apart, or on the way to where they are going today, become one question
("passi da X e Y in 15 minuti?"), sent as a notice. Never in quiet hours (nothing held for later), never the
same tasks twice in a week, never a plan change: their answer is an ordinary request. Pisa's own initiative: a
stop pauses it."""

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta

from psycopg_pool import AsyncConnectionPool

from pisa.domain import errands as rules
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.domain.tasks import local_day_bounds, online_task
from pisa.logging import get_logger
from pisa.services.notifications import enqueue
from pisa.services.rituals import ritual_on

_log = get_logger("pisa.errands")
FROM, UNTIL = time(9, 0), time(19, 0)
ERRANDS_MAX = 40


class ErrandsService:
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

    async def tick(self) -> bool:
        """The worker, every minute: at most one suggestion a day. True when one was sent."""
        now = self._clock()
        local = now.astimezone(self._owner.zone)
        if not FROM <= local.time() < UNTIL:
            return False
        today = local.date()
        async with self._pool.connection() as conn:
            if not await ritual_on(conn, "errands"):
                return False  # their switch (Settings, or "spegni le commissioni")
            if await (
                await conn.execute("SELECT 1 FROM errand_suggestions WHERE day = %s", (today,))
            ).fetchone():
                return False
            settings = await self._owner.settings(conn)
            if is_quiet(now, settings, self._owner.tz):
                return False
            lo, hi = local_day_bounds(today, self._owner.tz)
            tasks = await (
                await conn.execute(
                    "SELECT t.id::text, t.title, p.name, p.lat, p.lon, t.source_kind, t.place_by_owner "
                    "FROM tasks t JOIN places p ON p.id = t.place_id WHERE t.status = 'open' "
                    "AND p.lat IS NOT NULL AND (t.expected_from IS NULL OR t.expected_from < %s) "
                    "ORDER BY t.priority, t.created_at LIMIT %s",
                    (hi, ERRANDS_MAX),
                )
            ).fetchall()
            events = await (
                await conn.execute(
                    "SELECT e.title, p.name, p.lat, p.lon FROM calendar_events e JOIN places p ON p.id = "
                    "e.place_id WHERE NOT e.all_day AND e.status <> 'cancelled' "
                    "AND coalesce(e.accepted, true) "
                    "AND e.starts_at >= %s AND e.starts_at < %s AND e.ends_at > %s AND p.lat IS NOT NULL",
                    (lo, hi, now),
                )
            ).fetchall()
            recent = {
                k
                for (k,) in await (
                    await conn.execute(
                        "SELECT combo_key FROM errand_suggestions WHERE day > %s",
                        (today - timedelta(days=rules.REPEAT_DAYS),),
                    )
                ).fetchall()
            }
        errands = [
            rules.Errand(i, title, name, lat, lon)
            for i, title, name, lat, lon, kind, by_owner in tasks
            if not (kind == "email" and not by_owner and online_task(title))  # the rule: online stays
        ]
        stops = [rules.Stopover(title, name, lat, lon) for title, name, lat, lon in events]
        combo = rules.best(errands, stops, recent)
        if combo is None:
            return False
        lang = str(settings["language"])
        text = rules.question(combo, lang)
        async with self._pool.connection() as conn, conn.transaction():
            made = await (
                await conn.execute(
                    "INSERT INTO errand_suggestions (day, combo_key, text, created_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (day) DO NOTHING RETURNING 1",
                    (today, combo.key, text, now),
                )
            ).fetchone()
            if made is None:
                return False  # another worker sent today's
            await enqueue(
                conn,
                kind="info",
                text=text,
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                respect_quiet=False,
                path="errands.combo",
            )
        _log.info("errand_combo_suggested", errands=len(combo.errands), minutes=combo.minutes)
        return True
