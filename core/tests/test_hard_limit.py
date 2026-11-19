"""The hard spending limit: one ceiling over every paid service together, per day and per month, set
on the server only. The providers have no hard stop; this one holds whatever the user's caps say."""

from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest

from pisa.domain.errors import PisaError
from pisa.infra import hard_limit, ledger
from pisa.infra.fx import DEFAULT_USD_TO_EUR
from pisa.infra.hard_limit import HardLimits
from tests.assistant_env import set_settings
from tests.conftest import FAKE_KEY, Clock, FakeModels, dsn, pair
from tests.gateway_env import gateway, run
from tests.test_ledger import ask
from tests.test_research_eval import research

TEXT_SEARCH = "google.text_search_enterprise_atmosphere"  # $40 per 1,000 after 1,000 free a month


def place_calls(db: psycopg.Connection, units: int, at: datetime) -> None:
    db.execute(
        "INSERT INTO place_calls (provider, sku, units, at, ok) VALUES ('google', %s, %s, %s, true)",
        (TEXT_SEARCH, units, at),
    )


def ai_spent(db: psycopg.Connection, eur: str, at: datetime) -> None:
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, %s, true, "
        "'t', %s)",
        (eur, at),
    )


def test_maps_spend_stops_the_ai_too(db: psycopg.Connection, secrets_key: str) -> None:
    """Google cost €3.4 this month (100 paid searches); the hard limit is €3: the AI's own caps (€1 a day, €20
    a month) would allow the call, the hard limit doesn't."""
    hard = HardLimits(hard_limit.DEFAULT.daily_eur, Decimal(3))
    clock = Clock(datetime(2026, 10, 20, 11, 0, tzinfo=UTC))
    place_calls(db, 1100, datetime(2026, 10, 5, 9, tzinfo=UTC))

    async def go() -> PisaError:
        async with gateway(dsn(db), secrets_key, FakeModels(), clock, hard=hard) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            with pytest.raises(PisaError) as info:
                await gw.run("chat.conversation", ask, prompt_chars=400, max_output_tokens=1024)
            return info.value

    err = run(go)
    assert err.code == "budget_exceeded" and err.message.startswith("Pisa's hard spending limit is reached:")
    assert "€3.00 this month" in err.message and "Only the server's operator" in err.action
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (0,)


def test_ai_spend_stops_paid_maps_too(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The AI spent €2.99 this month against a €3 hard limit: the conversation still runs (cents), but a paid
    Google search (≈ €0.03) would pass the limit, though the places cap (€5) allows it, and Google is never
    called."""
    hard = HardLimits(hard_limit.DEFAULT.daily_eur, Decimal(3))
    ai_spent(db, "2.99", datetime(2026, 10, 5, 9, tzinfo=UTC))
    place_calls(db, 1000, datetime(2026, 10, 5, 9, tzinfo=UTC))  # the month's free searches: the next is paid
    turn = research(
        db, secrets_key, "Un bar?", [("map_search", {"query": "bar", "provider": "google"})], "No.", hard=hard
    )
    assert "Pisa's hard spending limit is reached" in turn.result("map_search")
    assert turn.world.routes("google.search") == []


def test_the_daily_limit_holds(db: psycopg.Connection, secrets_key: str) -> None:
    hard = HardLimits(Decimal("0.0001"), hard_limit.DEFAULT.monthly_eur)
    clock = Clock(datetime(2026, 10, 20, 11, 0, tzinfo=UTC))

    async def go() -> PisaError:
        async with gateway(dsn(db), secrets_key, FakeModels(), clock, hard=hard) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            with pytest.raises(PisaError) as info:
                await gw.run("chat.conversation", ask, prompt_chars=400, max_output_tokens=1024)
            return info.value

    err = run(go)
    assert "today" in err.message and "Paid features stop until tomorrow" in err.message


def test_today_pays_only_what_passes_the_free_allowance(db: psycopg.Connection) -> None:
    """1,000 free searches used before today, 10 today: today costs 10 x $0.04; Rome's day and month."""
    place_calls(db, 1000, datetime(2026, 10, 5, 9, tzinfo=UTC))
    place_calls(db, 10, datetime(2026, 10, 20, 9, tzinfo=UTC))
    ai_spent(db, "0.50", datetime(2026, 10, 20, 8, tzinfo=UTC))
    ai_spent(db, "9", datetime(2026, 9, 30, 21, tzinfo=UTC))  # 23:00 in Rome: September

    async def go() -> tuple[Decimal, Decimal]:
        from psycopg import AsyncConnection

        async with await AsyncConnection.connect(dsn(db)) as conn:
            return await hard_limit.spent(conn, datetime(2026, 10, 20, 11, tzinfo=UTC), "Europe/Rome")

    import asyncio

    today, month = asyncio.run(go())
    fx = DEFAULT_USD_TO_EUR  # no refreshed rate stored in the test database
    assert today == Decimal("0.50") + Decimal("0.40") * fx
    assert abs(month - today) < Decimal("0.0001")  # nothing paid earlier this month


def test_the_owners_caps_cannot_pass_the_hard_limit(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    refused = client.put("/v1/ai/caps", headers=auth, json={"daily_eur": 5, "monthly_eur": 50})
    assert refused.status_code == 400 and "hard limit" in refused.json()["error"]["message"]
    assert (
        client.put("/v1/ai/caps", headers=auth, json={"daily_eur": 1, "monthly_eur": 20}).status_code == 200
    )
    places = client.patch("/v1/settings", headers=auth, json={"places": {"monthly_cap_eur": 40}})
    assert places.status_code == 400 and "hard limit" in places.json()["error"]["message"]
    assert set_settings(db, {"places": {"monthly_cap_eur": 5}})["places"]["monthly_cap_eur"] == 5
    assert (
        ledger.DEFAULT_CAPS.monthly_eur + Decimal(5) <= hard_limit.DEFAULT.monthly_eur
    )  # defaults fit under it


def test_the_limit_comes_from_the_server_s_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PISA_HARD_DAILY_EUR` and `PISA_HARD_MONTHLY_EUR` are settings, €3 and €30 when unset."""
    from pisa.settings import Settings

    monkeypatch.setenv("PISA_TIMEZONE", "Europe/Rome")
    monkeypatch.delenv("PISA_HARD_DAILY_EUR", raising=False)
    monkeypatch.delenv("PISA_HARD_MONTHLY_EUR", raising=False)
    unset = Settings(_env_file=None)  # type: ignore[call-arg]
    assert hard_limit.limits(unset) == hard_limit.DEFAULT == HardLimits(Decimal(3), Decimal(30))
    monkeypatch.setenv("PISA_HARD_DAILY_EUR", "2.5")
    monkeypatch.setenv("PISA_HARD_MONTHLY_EUR", "12")
    chosen = Settings(_env_file=None)  # type: ignore[call-arg]
    assert hard_limit.limits(chosen) == HardLimits(Decimal("2.5"), Decimal(12))
