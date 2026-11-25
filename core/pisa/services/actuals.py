"""Planned against actual (`pisa.domain.actuals`): the night scores the day that ended and keeps their
band; the planner reads it.

- **`score`** (the night's step `p1_actuals`, after the connectors' readers, under the frame's stop and
  budget; no model, no search): the day before the night's (`night - 1`), each item of its plans (the last
  plan made before a task was done, before a trip began) against their stays, the calendar and their done
  times. One row an item (`plan_actuals`), minutes only. Then the band per kind over the last `WINDOW_DAYS`
  (`plan_bands`).
- **`load_bands`**: what the planner reads once a plan.
- Logs carry counts only.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import actuals as rules
from pisa.domain.owner import Owner
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger

_log = get_logger("pisa.actuals")
STAYS_AROUND = timedelta(hours=6)  # stays read around the day (a trip after midnight, a stay begun the eve)


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment is not None and moment.tzinfo is None else moment


async def load_bands(conn: AsyncConnection) -> dict[str, rules.Band]:
    """Their band per kind (none until the night has scored MIN_SAMPLES of a kind)."""
    rows = await (await conn.execute("SELECT kind, n, lo, mid, hi FROM plan_bands")).fetchall()
    return {k: rules.Band(k, int(n), float(lo), float(m), float(h)) for k, n, lo, m, h in rows}


class ActualsService:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {"p1_actuals": self.score}

    async def score(self, night: date) -> int:
        """The day that ended (the night's eve), scored; then their band. Returns the items scored."""
        day = night - timedelta(days=1)
        start, end = local_day_bounds(day, self._owner.tz)
        async with self._pool.connection() as conn, conn.transaction():
            plans = await (
                await conn.execute(
                    "SELECT generated_at, plan FROM plans WHERE day = %s ORDER BY generated_at", (day,)
                )
            ).fetchall()
            stays = [
                rules.Stay(a, d, str(p) if p else None)
                for a, d, p in await (
                    await conn.execute(
                        "SELECT arrived_at, departed_at, place_id FROM stays WHERE arrived_at < %s "
                        "AND coalesce(departed_at, last_seen_at) > %s ORDER BY arrived_at",
                        (end + STAYS_AROUND, start - STAYS_AROUND),
                    )
                ).fetchall()
            ]
            planned = self._planned(plans)
            task_ids = [ref for kind, ref in planned if kind == "task"]
            done = {
                str(i): d
                for i, d in await (
                    await conn.execute(
                        "SELECT id, done_at FROM tasks WHERE id::text = ANY(%s) AND status = 'done' "
                        "AND done_at >= %s AND done_at < %s",
                        (task_ids, start, end),
                    )
                ).fetchall()
            }
            samples = [s for s in (self._sample(k, r, i, done, stays) for (k, r), i in planned.items()) if s]
            scored = 0
            for s in samples:
                cur = await conn.execute(
                    "INSERT INTO plan_actuals (id, day, kind, ref, planned_min, actual_min, source, "
                    "scored_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (day, kind, ref) DO NOTHING",
                    (uuid.uuid4(), day, s.kind, s.ref, s.planned_min, s.actual_min, s.source, self._clock()),
                )
                scored += max(cur.rowcount, 0)
            bands = await self._bands(conn, day)
        _log.info("actuals_scored", planned=len(planned), scored=scored, bands=bands)
        return scored

    def _planned(self, plans: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
        """Each item's last planned shape across the day's plans, **unscaled**: a plan made with
        their band says the factor it used per kind (`factors`), and the night scores against the plan's own
        minutes divided by it, never against its own band. A task as last planned before it was done (its
        later plans no longer hold it), an event as last planned, a trip by its leg (its place and the item it
        leads to) as last planned before it began."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for generated_at, plan in plans:
            factors = (plan or {}).get("factors") or {}
            items = sorted(
                (plan or {}).get("items") or [], key=lambda it: str(it.get("start") or "")
            )  # the timeline: a trip leads to the next task or event
            for n, item in enumerate(items):
                kind = item.get("kind")
                if item.get("status") == "skipped":
                    continue
                start, finish = _at(item.get("start")), _at(item.get("end"))
                if start is None or finish is None:
                    continue
                minutes = int(item.get("minutes") or 0)
                if kind == "task" and item.get("task_id"):
                    key = ("task", str(item["task_id"]))
                    if item.get("scaled"):  # a task at their own time was never scaled
                        finish = start + (finish - start) / float(factors.get("task") or 1.0)
                elif kind == "event" and item.get("event_id"):
                    key = ("event", str(item["event_id"]))
                elif kind == "travel" and item.get("to_place_id") and minutes:
                    if generated_at > start:  # a plan made after the trip began: not the one they followed
                        continue
                    target = next(
                        (
                            str(t.get("task_id") or t.get("event_id"))
                            for t in items[n + 1 :]
                            if t.get("kind") in ("task", "event") and (t.get("task_id") or t.get("event_id"))
                        ),
                        "end",
                    )
                    key = ("travel", f"{item['to_place_id']}>{target}")
                    minutes = max(1, round(minutes / float(factors.get("travel") or 1.0)))
                else:
                    continue
                out[key] = {
                    "start": start,
                    "end": finish,
                    "place_id": str(item["place_id"]) if item.get("place_id") else None,
                    "to_place_id": str(item["to_place_id"]) if item.get("to_place_id") else None,
                    "minutes": minutes,
                }
        return out

    @staticmethod
    def _sample(
        kind: str, ref: str, item: dict[str, Any], done: dict[str, datetime], stays: list[rules.Stay]
    ) -> rules.Sample | None:
        if kind == "task":
            return rules.task_actual(ref, item["start"], item["end"], item["place_id"], done.get(ref), stays)
        if kind == "event":
            return rules.event_actual(ref, item["start"], item["end"], item["place_id"], stays)
        return rules.travel_actual(ref, item["start"], item["minutes"], item["to_place_id"], stays)

    async def _bands(self, conn: AsyncConnection, day: date) -> int:
        """Their band per kind over the last WINDOW_DAYS. A kind with fewer than MIN_SAMPLES there has no
        band: its row goes, so a band built from days they deleted never drives the planner."""
        rows = await (
            await conn.execute(
                "SELECT kind, actual_min::float / planned_min FROM plan_actuals WHERE day > %s AND day <= %s",
                (day - timedelta(days=rules.WINDOW_DAYS), day),
            )
        ).fetchall()
        kept = 0
        for kind in rules.KINDS:
            b = rules.band(kind, [float(r) for k, r in rows if k == kind])
            if b is None:
                await conn.execute("DELETE FROM plan_bands WHERE kind = %s", (kind,))
                continue
            await conn.execute(
                "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (kind) DO UPDATE SET n = EXCLUDED.n, lo = EXCLUDED.lo, mid = EXCLUDED.mid, "
                "hi = EXCLUDED.hi, computed_at = EXCLUDED.computed_at",
                (kind, b.n, b.lo, b.mid, b.hi, self._clock()),
            )
            kept += 1
        return kept
