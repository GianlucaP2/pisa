"""The sessions the streaks count (`domain.streaks`): their calendar's past events (not Pisa's
plan calendar, not declined, not cancelled) and the tasks they marked done, as (title, their local day)."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from pisa.domain import streaks as rules
from pisa.domain.tasks import local_day_bounds


async def sessions(conn: AsyncConnection, today: date, tz: str, now: datetime) -> list[tuple[str, date]]:
    """Until now: a session later today hasn't happened yet."""
    lo = local_day_bounds(today - timedelta(weeks=rules.WEEKS_BACK, days=today.weekday()), tz)[0]
    hi = now
    zone = ZoneInfo(tz)
    events = await (
        await conn.execute(
            # an event Pisa made on their yes describes their day (display, not learning): it counts
            "SELECT DISTINCT ON (id) title, starts_at FROM calendar_events WHERE NOT all_day AND status <> "
            "'cancelled' AND coalesce(accepted, true) "
            "AND starts_at >= %s "
            "AND starts_at < %s",
            (lo, hi),
        )
    ).fetchall()
    done = await (
        await conn.execute(
            "SELECT title, done_at FROM tasks WHERE status = 'done' AND done_at >= %s AND done_at < %s",
            (lo, hi),
        )
    ).fetchall()
    return [(str(t or ""), at.astimezone(zone).date()) for t, at in [*events, *done] if at is not None]
