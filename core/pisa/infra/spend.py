"""What "spent" means, once, and the periods it is counted over.

Spend is what the caps count: every model call on the ledger (`llm_calls`), settled and failed calls at their
recorded cost, calls still running and swept ghosts at their reservation. The user's caps, the night's and the
evals' caps, the hard limit, the weekly card and the night steps' own € limits all read it.

One other meaning is kept on purpose, under its own name: `PAID`, what was actually paid (settled calls at
their recorded cost, never an estimate), for the month's "Pisa cost you" report.

The expressions are SQL over `llm_calls` rows; the callers add their own WHERE.
"""

from datetime import datetime
from typing import LiteralString
from zoneinfo import ZoneInfo

# What one row counts for: a running or swept reservation at its estimate, anything else at its recorded cost.
ROW_SPEND: LiteralString = (
    "CASE WHEN status IN ('reserved', 'swept') THEN reserved_eur ELSE coalesce(cost_eur, 0) END"
)
# Spend over the rows selected (0 when none).
SPEND: LiteralString = f"coalesce(sum({ROW_SPEND}), 0)"
# Spend over the rows selected created at or after a moment: one `%s` parameter, the moment.
SPEND_SINCE: LiteralString = f"coalesce(sum({ROW_SPEND}) FILTER (WHERE created_at >= %s), 0)"
# The dearest single row selected (0 when none): a night step's guess at its next call.
DEAREST: LiteralString = f"coalesce(max({ROW_SPEND}), 0)"

# A different meaning: what was actually paid, settled calls only, at their recorded cost. Not what the caps
# count (a running call's reservation is not paid yet).
PAID: LiteralString = "coalesce(sum(cost_eur) FILTER (WHERE status = 'settled'), 0)"


def period_starts(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """Start of the user's local day and month, as aware datetimes."""
    local = now.astimezone(ZoneInfo(tz))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)
