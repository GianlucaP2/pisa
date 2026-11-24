"""The hard spending limit: one ceiling over every paid service together (AI models, the voice, maps,
places and web research), per day and per month, set on the server only.

The user's caps (Settings → AI spend, Settings → Places) can only be lower. The providers themselves (Google
Cloud, OpenAI, Tavily) have no hard stop, so this is the one that holds: every paid call is reserved through
the AI ledger or the places ledger, and both ask `refusal` under one lock before they reserve.

The limit comes from the server's settings (`PISA_HARD_DAILY_EUR`, `PISA_HARD_MONTHLY_EUR`), handed to each
service that reserves; the app can't change it.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from psycopg import AsyncConnection

from pisa.domain.errors import PisaError
from pisa.domain.place_prices import PRICES, month_cost_usd
from pisa.infra import locks, spend
from pisa.infra.fx import usd_to_eur
from pisa.settings import Settings


@dataclass(frozen=True)
class HardLimits:
    daily_eur: Decimal
    monthly_eur: Decimal


def limits(settings: Settings) -> HardLimits:
    """The server's hard limit, from its settings."""
    return HardLimits(settings.hard_daily_eur, settings.hard_monthly_eur)


# The settings' own defaults (€3 a day, €30 a month): the limit of a service built without the server's
# settings (a test, a CLI command that never reserves).
DEFAULT = HardLimits(
    Settings.model_fields["hard_daily_eur"].default, Settings.model_fields["hard_monthly_eur"].default
)


async def spent(conn: AsyncConnection, now: datetime, tz: str) -> tuple[Decimal, Decimal]:
    """(today, this month) in EUR over every paid service, reservations still running included."""
    day, month = spend.period_starts(now, tz)
    ai = await (
        await conn.execute(
            f"SELECT {spend.SPEND}, {spend.SPEND_SINCE} FROM llm_calls WHERE created_at >= %s",  # noqa: S608
            (day, month),
        )
    ).fetchone()
    ai_month, ai_today = (Decimal(ai[0]), Decimal(ai[1])) if ai else (Decimal(0), Decimal(0))
    rows = await (
        await conn.execute(
            "SELECT sku, sum(units), coalesce(sum(units) FILTER (WHERE at < %s), 0) FROM place_calls "
            "WHERE at >= %s GROUP BY sku",
            (day, month),
        )
    ).fetchall()
    usd_month = usd_before_today = Decimal(0)
    for sku, units, before in rows:
        if sku in PRICES:  # the free allowance is the month's: today pays only what passes it
            usd_month += month_cost_usd(sku, int(units))
            usd_before_today += month_cost_usd(sku, int(before))
    fx = await usd_to_eur(conn)
    return ai_today + (usd_month - usd_before_today) * fx, ai_month + usd_month * fx


async def refusal(
    conn: AsyncConnection, *, extra_eur: Decimal, now: datetime, tz: str, hard: HardLimits
) -> PisaError | None:
    """None when `extra_eur` more keeps today and this month under the hard limit, else the error to raise.
    Call it inside the transaction that then reserves, after the ledger's own lock."""
    if extra_eur <= 0:
        return None
    # after the ledger's own lock, never before (the order: pisa/infra/locks.py)
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.HARD_LIMIT,))
    today, month = await spent(conn, now, tz)
    if today + extra_eur > hard.daily_eur:
        which, used, cap, until = "today", today, hard.daily_eur, "tomorrow"
    elif month + extra_eur > hard.monthly_eur:
        which, used, cap, until = "this month", month, hard.monthly_eur, "the 1st of next month"
    else:
        return None
    return PisaError(
        "budget_exceeded",
        f"Pisa's hard spending limit is reached: €{used:.2f} of €{cap:.2f} {which}, for every paid service "
        f"together (AI, voice, maps, web). Paid features stop until {until}.",
        "Free features keep working. Only the server's operator can raise the hard limit",
    )
