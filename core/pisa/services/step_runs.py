"""Once per period, for the worker's periodic steps: the one record of the last period each step ran
(`step_runs`), and the claim the scheduler (`maintenance.run_job`) takes before it runs one.

The step's period (`domain/schedule.py`) is read on the Owner's clock: their zone now. The claim is taken
under the step's own advisory lock, before its work, so a step runs at most once a period; a failed run with
`retry` gives the period back (it is tried again next minute), one without keeps it spent."""

from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncConnection

from pisa.domain.schedule import Period


@dataclass(frozen=True)
class Claim:
    step: str
    period: str  # the key claimed
    before: tuple[str, datetime] | None  # the record it replaced (put back if the run gives it back)


async def last(conn: AsyncConnection, step: str) -> tuple[str, datetime] | None:
    """The last period `step` ran and when, or None."""
    row = await (
        await conn.execute("SELECT period, ran_at FROM step_runs WHERE step = %s", (step,))
    ).fetchone()
    return (str(row[0]), row[1]) if row else None


async def claim(
    conn: AsyncConnection, step: str, period: Period, local: datetime, now: datetime
) -> Claim | None:
    """The period `local` is in, claimed for `step` when due and not yet run (None otherwise). The caller
    holds the step's lock."""
    key = period.due(local)
    if key is None:
        return None
    before = await last(conn, step)
    if period.ran(key, before[0] if before else None):
        return None
    await conn.execute(
        "INSERT INTO step_runs (step, period, ran_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (step) DO UPDATE SET period = EXCLUDED.period, ran_at = EXCLUDED.ran_at",
        (step, key, now),
    )
    return Claim(step, key, before)


async def give_back(conn: AsyncConnection, claim: Claim) -> None:
    """A failed run's period returned: the record as it was before the claim."""
    if claim.before is None:
        await conn.execute(
            "DELETE FROM step_runs WHERE step = %s AND period = %s", (claim.step, claim.period)
        )
        return
    await conn.execute(
        "UPDATE step_runs SET period = %s, ran_at = %s WHERE step = %s AND period = %s",
        (*claim.before, claim.step, claim.period),
    )
