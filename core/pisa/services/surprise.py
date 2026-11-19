"""Pisa's morning thought (`pisa.domain.surprise`), offered in the first automatic briefing's transaction.

`offer` reads their records (a confirmed trip home in the last 18 hours; the pool's oldest task with no
deadline), keeps the day's one thought (`morning_thoughts`, unique per day and per record) and, for a stuck
task, Pisa's one-yes question (`ritual_asks`, kind `thought`, until their day's end). Nothing else is written,
and nothing goes outward. No model call. Logs: ids and counts only.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from pisa.domain import profile as profile_rules
from pisa.domain import surprise as rules
from pisa.domain.owner import Owner
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services.foreign import FOREIGN_TASK

_log = get_logger("pisa.surprise")
SLEEPERS = (  # constant SQL fragments, no input
    "SELECT t.id::text, t.title, t.created_at FROM tasks t WHERE t.status = 'open' "  # noqa: S608
    f"AND NOT {FOREIGN_TASK} "
    "AND t.deleted_at IS NULL AND t.due_at IS NULL AND t.repeat IS NULL "
    "AND t.expected_until IS NULL AND t.aging_asked_at IS NULL AND t.created_at < %s "
    "AND NOT EXISTS (SELECT 1 FROM morning_thoughts m WHERE m.kind = 'stuck_task' AND m.ref = t.id) "
    "ORDER BY t.created_at, t.id LIMIT 5"
)


class SurpriseService:
    def __init__(self, *, owner: Owner, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._owner = owner
        self._clock = clock

    async def candidates(self, conn: AsyncConnection, now: datetime, said: str = "") -> list[rules.Candidate]:
        """Records they gave Pisa themselves, never one already thought about: their booking, their own list.
        `said`: the briefing's text, so a task it already names (the morning's one thing) is never said
        twice."""
        tz = self._owner.zone
        out: list[rules.Candidate] = []
        # the booking alone is no landing. The phone saw the return leg land (its key in
        # `landed_keys`), nothing speaks of a cancellation or a moved leg, their zone is no longer the trip's
        trip = await (
            await conn.execute(
                "SELECT t.id::text, t.to_city FROM trips t WHERE t.status = 'planned' AND t.doubt IS NULL "
                "AND t.back_at IS NOT NULL AND t.back_at <= %s AND t.back_at > %s "
                "AND cardinality(t.cancel_mentions) = 0 AND t.proposals = '[]'::jsonb "
                "AND t.tz_to IS DISTINCT FROM %s "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(t.legs) l "
                "WHERE l->>'key' = ANY(t.landed_keys) "
                "AND coalesce(l->>'until', l->>'at')::timestamptz = t.back_at) "
                "AND coalesce(t.to_city, '') <> '' AND NOT EXISTS (SELECT 1 FROM morning_thoughts m "
                "WHERE m.kind = 'welcome_back' AND m.ref = t.id) ORDER BY t.back_at DESC LIMIT 1",
                (now, now - rules.BACK_WITHIN, self._owner.tz),
            )
        ).fetchone()
        if trip is not None:
            out.append(rules.Candidate("welcome_back", trip[0], str(trip[1])))
        today = now.astimezone(tz).date()
        start, _ = local_day_bounds(today - timedelta(days=rules.STUCK_DAYS), self._owner.tz)
        # The pool's sleepers: their own (never one made from an email, a stranger wrote its title),
        # open, no deadline (an overdue one has the aging question), not a series, not a delivery they
        # waits for, never asked about by aging, in their list since before the cut-off
        tasks = await (
            await conn.execute(
                SLEEPERS,
                (start,),
            )
        ).fetchall()
        named = said.casefold()
        task = next(
            (
                t
                for t in tasks
                if str(t[1]).casefold() not in named and not profile_rules.is_sensitive("task", str(t[1]))
            ),
            None,
        )
        if task is not None:
            days = (today - task[2].astimezone(tz).date()).days
            out.append(rules.Candidate("stuck_task", task[0], str(task[1]), days))
        return out

    async def declined(self, conn: AsyncConnection, now: datetime) -> set[str]:
        rows = await (
            await conn.execute(
                "SELECT DISTINCT kind FROM morning_thoughts WHERE answer = 'no' AND answered_at > %s",
                (now - rules.DECLINED_FOR,),
            )
        ).fetchall()
        return {str(r[0]) for r in rows}

    async def offer(
        self, conn: AsyncConnection, lang: str, said: str = "", day: date | None = None
    ) -> tuple[rules.Thought, uuid.UUID | None] | None:
        """Today's thought and its question's id, kept on `conn` (the briefing's transaction: it is kept only
        if the briefing is). None: one was already said today, or nothing is worth saying. `said`: the
        briefing's text; `day`: the briefing's day, else their local date."""
        now = self._clock()
        tz = self._owner.zone
        local = now.astimezone(tz)
        today = day or local.date()
        today_had = await (
            await conn.execute("SELECT 1 FROM morning_thoughts WHERE day = %s", (today,))
        ).fetchone()
        if today_had is not None:
            return None
        thought = rules.pick(
            await self.candidates(conn, now, said),
            await self.declined(conn, now),
            lang,
            morning=local.hour < rules.MORNING_UNTIL,
        )
        if thought is None:
            _log.info("morning_thought_none")
            return None
        ask_id: uuid.UUID | None = None
        if thought.question is not None:
            ask_id = uuid.uuid4()
            _, day_end = local_day_bounds(local.date(), self._owner.tz)
            await conn.execute(
                "INSERT INTO ritual_asks (id, kind, question, payload, created_at, expires_at) "
                "VALUES (%s, 'thought', %s, %s, %s, %s)",
                (ask_id, thought.question, Jsonb({"kind": thought.kind, "ref": thought.ref}), now, day_end),
            )
        row = await (
            await conn.execute(
                "INSERT INTO morning_thoughts (id, day, kind, ref, ask_id, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (uuid.uuid4(), today, thought.kind, uuid.UUID(thought.ref), ask_id, now),
            )
        ).fetchone()
        if row is None:  # a race with another briefing: that one has it
            if ask_id is not None:
                await conn.execute("DELETE FROM ritual_asks WHERE id = %s", (ask_id,))
            return None
        _log.info("morning_thought", thought_id=str(row[0]), kind=thought.kind, asks=ask_id is not None)
        return thought, ask_id


async def answered(conn: AsyncConnection, ask_id: uuid.UUID, yes: bool, now: datetime) -> str | None:
    """Their bare yes or no (the rituals' `answer`, in its transaction): kept on the day's thought. Returns
    the task's title as it is now (the yes names it, so the next turn knows which), None if gone."""
    row = await (
        await conn.execute(
            "UPDATE morning_thoughts SET answer = %s, answered_at = %s WHERE ask_id = %s RETURNING kind, ref",
            ("yes" if yes else "no", now, ask_id),
        )
    ).fetchone()
    if row is None or row[0] != "stuck_task":
        return None
    title = await (
        await conn.execute("SELECT title FROM tasks WHERE id = %s AND status = 'open'", (row[1],))
    ).fetchone()
    return str(title[0]) if title else None
