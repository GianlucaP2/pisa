"""The user's ask: reset today's AI spend for the daily cap. The cap counts from the reset; the
ledger keeps every call and the hard limit still counts the whole day."""

from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.db.settings import set_setting
from pisa.domain.errors import PisaError
from pisa.infra import hard_limit, ledger, spend
from tests.conftest import Clock, dsn
from tests.gateway_env import run

TZ = "Europe/Rome"


def test_the_daily_cap_counts_from_today_s_reset_and_the_hard_limit_counts_the_day(
    db: psycopg.Connection, clock: Clock
) -> None:
    async def reserve(conn: psycopg.AsyncConnection, eur: str) -> None:
        await ledger.reserve(
            conn, slot="chat.conversation", model_ref="openai:gpt-6-luna", estimate_eur=Decimal(eur),
            price_known=True, trace_id="t", now=clock(), tz=TZ, hard=hard_limit.DEFAULT,
        )  # fmt: skip

    async def go() -> tuple[bool, Decimal]:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await reserve(conn, "0.95")  # today's spend, near the €1 cap
            with pytest.raises(PisaError):
                await reserve(conn, "0.10")
            await set_setting(conn, ledger.DAY_RESET_KEY, Jsonb((clock() + timedelta(seconds=1)).isoformat()))
            clock.now += timedelta(minutes=1)
            await reserve(conn, "0.10")  # counted from the reset: under the cap again
            whole_day, _ = await hard_limit.spent(conn, clock(), TZ)
            yesterday = clock() - timedelta(days=1)
            await set_setting(conn, ledger.DAY_RESET_KEY, Jsonb(yesterday.isoformat()))
            day_start, _ = spend.period_starts(clock(), TZ)
            return await ledger.cap_day_start(conn, day_start) == day_start, whole_day

    from_midnight_again, whole_day = run(go)
    assert whole_day == Decimal("1.05")  # the hard limit still sees the whole day
    assert from_midnight_again  # a reset from another day counts for nothing
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (2,)  # nothing deleted
