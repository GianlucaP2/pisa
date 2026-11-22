"""The competence ledger, read. SHADOW ONLY: the replay's calibration and
the maintenance scripts read it; nothing in Pisa decides anything from it in this wave (the levels still read
`AutonomyService`).

The ledger is the `pisa_competence` view (migration 0074): action type and skill, runs, kept,
undone, corrected, failed, trust_fast (half-life 5 runs), trust_slow (30), n. A type or skill with no outcome
yet starts at 0.5 with n = 0.
"""

from dataclasses import dataclass

from psycopg import AsyncConnection

from pisa.domain.autonomy import TYPES

PRIOR = 0.5


@dataclass(frozen=True)
class Competence:
    kind: str  # "type" or "skill" (its id)
    key: str
    runs: int = 0
    kept: int = 0
    undone: int = 0
    corrected: int = 0
    failed: int = 0
    trust_fast: float = PRIOR
    trust_slow: float = PRIOR
    n: int = 0


async def ledger(conn: AsyncConnection) -> dict[tuple[str, str], Competence]:
    """Every type (0.5, n = 0 when Pisa has no outcome of it) and every skill and type Pisa has outcomes
    of."""
    rows = await (
        await conn.execute(
            "SELECT kind, key, runs, kept, undone, corrected, failed, trust_fast, trust_slow, n "
            "FROM pisa_competence"
        )
    ).fetchall()
    out = {("type", t): Competence("type", t) for t in TYPES}
    for row in rows:
        out[(row[0], row[1])] = Competence(*row)
    return out


async def confidence(conn: AsyncConnection) -> tuple[float, int] | None:
    """Pisa's expectation now, over everything rated: the mean slow trust of the types and skills with n > 0,
    and their n. None when nothing is rated yet (an empty ledger)."""
    rated = [c for c in (await ledger(conn)).values() if c.n > 0]
    if not rated:
        return None
    return sum(c.trust_slow for c in rated) / len(rated), sum(c.n for c in rated)
