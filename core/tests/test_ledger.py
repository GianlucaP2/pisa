"""Cost ledger: reserve → settle, caps, ghost sweep, fallback, tiered and unknown prices, day boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from structlog.testing import capture_logs

from pisa.brain.models.gateway import CallContext
from pisa.brain.models.pricing import call_cost_usd
from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.domain.ai import ModelRef
from pisa.domain.errors import PisaError
from pisa.infra import ledger
from tests.conftest import FAKE_KEY, Clock, FakeModels, dsn, seed_catalog
from tests.gateway_env import gateway, run

OPENAI = PROVIDERS_BY_ID["openai"]


async def ask(ctx: CallContext) -> str:
    result = await Agent(ctx.model).run("hello")
    ctx.usage.input_tokens = result.usage.input_tokens
    ctx.usage.output_tokens = result.usage.output_tokens
    return result.output


def test_reserve_then_settle(db: psycopg.Connection, secrets_key: str, clock: Clock) -> None:
    fake = FakeModels()

    async def go() -> str:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            return await gw.run("chat.conversation", ask, prompt_chars=400, max_output_tokens=1024)

    assert run(go) == "Ciao. Come posso aiutarti? "
    row = db.execute(
        "SELECT status, model_ref, reserved_eur, cost_eur, input_tokens, output_tokens, price_known, "
        "latency_ms "
        "FROM llm_calls"
    ).fetchone()
    assert row is not None
    status, ref, reserved, cost, inp, out, known, latency = row
    assert (status, ref, known) == ("settled", "openai:gpt-6-luna", True)
    # Reservation: 100 prompt tokens (400 chars / 4) + 1024 output tokens at $0.10/$0.50 per 1M, x 0.86.
    assert reserved == Decimal("0.000449")
    assert 0 < cost < reserved and inp > 0 and out > 0 and latency >= 0


def test_tiered_price_is_used_not_a_flat_rate(db: psycopg.Connection) -> None:
    async def go() -> Decimal:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            return (await call_cost_usd(conn, OPENAI, ModelRef("openai", "gpt-6-luna"), 300_000, 0)).usd

    # Above 272k tokens OpenAI charges the higher tier for all input tokens ($0.20/1M instead of $0.10/1M).
    assert run(go) == Decimal("0.06")


def test_unknown_price_uses_the_providers_most_expensive_and_is_flagged(
    db: psycopg.Connection, secrets_key: str, clock: Clock
) -> None:
    seed_catalog(
        db,
        [
            {"ref": "openai:cheap", "input": Decimal("0.1"), "output": Decimal("0.5")},
            {"ref": "openai:pricey", "input": Decimal("15"), "output": Decimal("60")},
            {"ref": "anthropic:other", "input": Decimal("100"), "output": Decimal("500")},
        ],
    )
    db.execute("UPDATE model_slots SET model_ref = 'openai:mystery-model' WHERE id = 'chat.conversation'")

    async def go() -> None:
        async with gateway(dsn(db), secrets_key, FakeModels(), clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            with capture_logs() as logs:
                await gw.run("chat.conversation", ask, prompt_chars=4000, max_output_tokens=1000)
            assert any(e["event"] == "model_price_unknown" for e in logs)

    run(go)
    row = db.execute("SELECT reserved_eur, price_known FROM llm_calls").fetchone()
    # 1000 input x $15 + 1000 output x $60 per 1M = $0.075 → €0.0645
    assert row == (Decimal("0.064500"), False)


def test_daily_cap_refuses_and_notifies_once_per_day(
    db: psycopg.Connection, secrets_key: str, clock: Clock
) -> None:
    async def go() -> list[PisaError]:
        errors: list[PisaError] = []
        async with gateway(dsn(db), secrets_key, FakeModels(), clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
                await ledger.set_caps(conn, ledger.Caps(Decimal("0.0001"), Decimal("20")))
            with capture_logs() as logs:
                for _ in range(2):
                    with pytest.raises(PisaError) as info:
                        await gw.run("chat.conversation", ask, prompt_chars=400, max_output_tokens=1024)
                    errors.append(info.value)
            assert [e["event"] for e in logs].count("budget_cap_hit") == 1
        return errors

    errors = run(go)
    assert errors[0].code == "budget_exceeded"
    assert errors[0].message.startswith("Daily AI spend cap reached: €0.00 spent today")
    assert "€0.0001 cap" in errors[0].message and "chat.conversation" in errors[0].message
    assert errors[0].action == "Raise the cap in Settings → AI spend"
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (0,)


def test_monthly_cap_counts_the_whole_local_month(db: psycopg.Connection, secrets_key: str) -> None:
    clock = Clock(datetime(2026, 10, 20, 11, 0, tzinfo=UTC))
    for created, cost in (
        (datetime(2026, 10, 2, 9, tzinfo=UTC), "19.50"),
        (datetime(2026, 9, 30, 21, tzinfo=UTC), "5"),
    ):
        db.execute(
            "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, "
            "price_known, trace_id, "
            "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, "
            "%s, true, 't', %s)",
            (cost, created),
        )

    async def go() -> PisaError:
        async with gateway(dsn(db), secrets_key, FakeModels(), clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
                await ledger.set_caps(conn, ledger.Caps(Decimal("1"), Decimal("19.5001")))
            with pytest.raises(PisaError) as info:
                await gw.run("chat.conversation", ask, prompt_chars=400, max_output_tokens=1024)
            return info.value

    err = run(go)
    # 30 Sep 21:00 UTC is 23:00 in Rome: September, so not counted.
    assert err.code == "budget_exceeded" and err.message.startswith(
        "Monthly AI spend cap reached: €19.50 spent this month"
    )


def test_day_boundary_is_europe_rome(db: psycopg.Connection) -> None:
    def spend_at(created: datetime, cost: str) -> None:
        db.execute(
            "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, "
            "price_known, trace_id, "
            "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, "
            "%s, true, 't', %s)",
            (cost, created),
        )

    async def go(now: datetime) -> ledger.Spend:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            return await ledger.spend_summary(conn, now, "Europe/Rome")

    spend_at(datetime(2026, 10, 19, 21, 30, tzinfo=UTC), "0.05")  # 23:30 Rome on the 19th
    spend_at(datetime(2026, 10, 19, 22, 30, tzinfo=UTC), "0.10")  # 00:30 Rome on the 20th
    late = run(lambda: go(datetime(2026, 10, 20, 21, 45, tzinfo=UTC)))  # 23:45 Rome on the 20th
    assert (late.today_eur, late.month_eur) == (Decimal("0.10"), Decimal("0.15"))
    spend_at(datetime(2026, 10, 20, 22, 30, tzinfo=UTC), "0.20")  # 00:30 Rome on the 21st
    morning = run(lambda: go(datetime(2026, 10, 21, 6, 0, tzinfo=UTC)))  # 08:00 Rome on the 21st
    assert (morning.today_eur, morning.month_eur) == (Decimal("0.20"), Decimal("0.35"))
    assert morning.by_slot[0].calls_today == 1


def test_ghost_sweep_after_ten_minutes(db: psycopg.Connection) -> None:
    now = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    for minutes in (11, 9):
        db.execute(
            "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, price_known, "
            "trace_id, created_at) "
            "VALUES (gen_random_uuid(), 's', 'openai:x', 'reserved', 0.01, true, %s, %s)",
            (str(minutes), now - timedelta(minutes=minutes)),
        )

    async def go() -> int:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await ledger.sweep_ghosts(conn, now)

    assert run(go) == 1
    assert db.execute("SELECT trace_id, status FROM llm_calls ORDER BY trace_id").fetchall() == [
        ("11", "swept"),
        ("9", "reserved"),
    ]


def test_fallback_is_tried_once_on_provider_error(
    db: psycopg.Connection, secrets_key: str, clock: Clock
) -> None:
    fake = FakeModels()
    fake.replies["gpt-6-luna"] = ModelHTTPError(503, "gpt-6-luna", body="overloaded")
    fake.replies["claude-haiku-5-5"] = "Fallback answer."
    db.execute(
        "UPDATE model_slots SET fallback_ref = 'anthropic:claude-haiku-5-5' WHERE id = 'chat.conversation'"
    )

    async def go() -> str:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
                await keys.set(conn, "anthropic", "sk-ant-test-xxxx")
            return await gw.run("chat.conversation", ask, prompt_chars=40, max_output_tokens=100)

    assert run(go) == "Fallback answer. "
    assert [c[:2] for c in fake.calls] == [("openai", "gpt-6-luna"), ("anthropic", "claude-haiku-5-5")]
    rows = db.execute(
        "SELECT model_ref, status, error_code FROM llm_calls ORDER BY created_at, status"
    ).fetchall()
    assert sorted(rows) == [
        ("anthropic:claude-haiku-5-5", "settled", None),
        ("openai:gpt-6-luna", "failed", "provider_error"),
    ]


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            ModelHTTPError(429, "m", headers={"Retry-After": "7"}),
            "rate_limited",
            "OpenAI throttled the call (HTTP 429).",
        ),
        (ModelHTTPError(401, "m"), "provider_error", "OpenAI rejected the key (HTTP 401)."),
        (ModelHTTPError(500, "m"), "provider_error", "OpenAI failed (HTTP 500)."),
    ],
)
def test_provider_failures_name_provider_and_status(
    db: psycopg.Connection, secrets_key: str, clock: Clock, error: Exception, code: str, message: str
) -> None:
    fake = FakeModels()
    fake.default = error

    async def go() -> PisaError:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            with pytest.raises(PisaError) as info:
                await gw.run("chat.conversation", ask, prompt_chars=40, max_output_tokens=100)
            return info.value

    err = run(go)
    assert (err.code, err.message) == (code, message)
    if code == "rate_limited":
        assert err.action == "Try again in 7 s"
    assert len(fake.calls) == 1  # no fallback configured: one attempt
    assert db.execute("SELECT status, cost_eur FROM llm_calls").fetchone() == ("failed", Decimal("0"))


def test_unconfigured_slot(db: psycopg.Connection, secrets_key: str, clock: Clock) -> None:
    async def go() -> PisaError:
        async with gateway(dsn(db), secrets_key, FakeModels(), clock) as (gw, _, __):
            with pytest.raises(PisaError) as info:
                await gw.run("memory.embed", ask, prompt_chars=1, max_output_tokens=1)
            return info.value

    err = run(go)
    assert err.code == "model_not_configured" and err.action == (
        "Choose a model for memory.embed in Settings → AI models, or with `pisa models set memory.embed "
        "<provider:model>` on the server"
    )


def test_failed_model_build_leaves_no_reservation(
    db: psycopg.Connection, secrets_key: str, clock: Clock
) -> None:
    def broken_factory(spec, model_id, api_key, ollama_base_url):  # type: ignore[no-untyped-def]
        raise RuntimeError("client construction failed")

    async def go() -> None:
        async with gateway(dsn(db), secrets_key, broken_factory, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            await gw.run("chat.conversation", ask, prompt_chars=400, max_output_tokens=1024)

    with pytest.raises(RuntimeError):
        run(go)
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (0,)


def test_cached_tokens_use_the_cache_rate(db: psycopg.Connection) -> None:
    async def go(cache_read: int) -> Decimal:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            ref = ModelRef("openai", "gpt-6-luna")
            return (await call_cost_usd(conn, OPENAI, ref, 100_000, 0, cache_read_tokens=cache_read)).usd

    # gpt-6-luna: $0.10/1M input, $0.01/1M cached input.
    assert run(lambda: go(0)) == Decimal("0.01")
    assert run(lambda: go(100_000)) == Decimal("0.001")


def test_tools_reserve_one_round_trip_and_cache_tokens_are_recorded(
    db: psycopg.Connection, secrets_key: str, clock: Clock
) -> None:
    async def ask_cached(ctx: CallContext) -> str:
        result = await Agent(ctx.model).run("hello")
        ctx.usage.update(result.usage)
        ctx.usage.cache_read_tokens = 40
        return result.output

    async def go() -> None:
        async with gateway(dsn(db), secrets_key, FakeModels(), clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            await gw.run(
                "chat.conversation", ask_cached, prompt_chars=400, max_output_tokens=1024, tools=True
            )

    run(go)
    row = db.execute("SELECT reserved_eur, cache_read_tokens, cache_write_tokens FROM llm_calls").fetchone()
    # 100 prompt tokens + 2 x 1024 output tokens at $0.10/$0.50 per 1M, x 0.86
    assert row == (Decimal("0.000889"), 40, 0)
