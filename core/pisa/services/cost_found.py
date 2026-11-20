"""The month's "Pisa cost you / found you", read from what Pisa recorded: the AI ledger
(`llm_calls`, what was paid: `spend.PAID`, settled calls only, not the caps' spend), the paid services' calls
(`place_calls`, at the price table), and the money found (claims they marked paid, refunds received,
subscriptions they cancelled). Never estimated.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import LiteralString
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from pisa.domain import cost_found as rules
from pisa.domain.place_prices import PRICES, month_cost_usd
from pisa.infra import spend
from pisa.infra.fx import usd_to_eur


async def month(conn: AsyncConnection, day: date, until: datetime, timezone: str) -> rules.CostFound:
    """The month of `day`, from its 1st to `until` (the card's week end, or now)."""
    tz = ZoneInfo(timezone)
    start = datetime.combine(day.replace(day=1), time(0), tz)
    ai = await (
        await conn.execute(
            f"SELECT {spend.PAID} FROM llm_calls WHERE created_at >= %s AND created_at < %s",  # noqa: S608
            (start, until),
        )
    ).fetchone()
    rows = await (
        await conn.execute(
            "SELECT sku, sum(units) FROM place_calls WHERE ok AND NOT reserved AND at >= %s AND at < %s "
            "GROUP BY sku",
            (start, until),
        )
    ).fetchall()
    usd = sum((month_cost_usd(sku, int(units)) for sku, units in rows if sku in PRICES), Decimal(0))
    fx = await usd_to_eur(conn)
    services = usd * fx if usd else Decimal(0)
    found: list[rules.Found] = []
    queries: tuple[tuple[str, LiteralString], ...] = (
        (
            "claim",
            "SELECT amount, currency FROM flight_claims WHERE status = 'paid' AND paid_at >= %s "
            "AND paid_at < %s",
        ),
        (
            "refund",
            "SELECT amount, currency FROM refunds_promised WHERE status = 'received' AND received_at >= %s "
            "AND received_at < %s",
        ),
        (
            "subscription",
            "SELECT cancelled_amount, cancelled_currency FROM subscription_marks WHERE cancelled_on >= %s "
            "AND cancelled_on <= %s",
        ),
    )
    for kind, sql in queries:
        # a cancellation is a local day: the days from the 1st up to the one `until` falls in (exclusive end)
        last_day = (until - timedelta(microseconds=1)).astimezone(tz).date()
        bounds = (start, until) if kind != "subscription" else (start.date(), last_day)
        for amount, currency in await (await conn.execute(sql, bounds)).fetchall():
            found.append(rules.Found(kind, Decimal(amount) if amount is not None else None, currency))
    return rules.CostFound(
        month=start.strftime("%Y-%m"), ai_eur=Decimal(ai[0]) if ai else Decimal(0), services_eur=services,
        found=found,
    )  # fmt: skip
