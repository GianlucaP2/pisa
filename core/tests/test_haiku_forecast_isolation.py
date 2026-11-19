# ruff: noqa: E501
"""Fixes from a Haiku replay (134/162). Each test fails with its fix reverted; the `b7-*` scenarios in
`tests/replay/scenarios.py` play Haiku's own calls and words."""

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tests.replay.scenarios import SCENARIOS
from tests.test_replay import _play


def _scenario(name: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == name)


# --- Row 2: views (b3-k13, b4-k13, b6-k13) ---------------------------------------------------------------------


def test_s_forecast_is_not_b3_k13_s_in_one_database(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """The server plays every scenario in one database: k22 (3 Nov 21:00) cached Roma's forecast, then b3-k13
    (31 Oct 09:00) read that row as fresh ("fetched in the last 30 minutes": after its clock) and got no 31 Oct
    in it: "il servizio meteo non mi dà le previsioni per Roma"."""
    first = _play(
        client,
        db,
        fake_models,
        secrets_key,
        clock,
        http_handler,
        _scenario("k22-a-wish-becomes-a-view-and-is-switched-on"),
    )
    assert first.passed, first.problems
    after = _play(
        client,
        db,
        fake_models,
        secrets_key,
        clock,
        http_handler,
        _scenario("b3-k13-haiku-saves-then-says-meteo"),
    )
    assert after.passed, after.problems
    # the scripted model goes on past a failed read; the forecast b3-k13 read is its own day's, never k22's
    fetched = db.execute("SELECT max(fetched_at) FROM weather_cache").fetchone()[0]
    assert fetched is not None and fetched <= clock.now


def test_the_replay_empties_the_forecasts_between_scenarios() -> None:
    from tests.replay import harness

    assert "weather_cache" in harness.WORLD_TABLES  # what a scenario leaves behind, as the server plays them


def test_a_forecast_fetched_after_the_clock_is_never_served(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair
    from tests.weather_fake import FakeMeteoAPI

    today = clock.now.astimezone(ZoneInfo("Europe/Rome")).date()
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    http_handler["handler"] = FakeMeteoAPI(today + timedelta(days=14))
    clock.now += timedelta(days=14)
    assert client.get("/v1/weather", headers=auth, params={"near": "41.826,12.432"}).status_code == 200
    clock.now -= timedelta(days=14)
    http_handler["handler"] = FakeMeteoAPI(today)
    back = client.get("/v1/weather", headers=auth, params={"near": "41.826,12.432"})
    assert back.status_code == 200, back.json()
    assert back.json()["date"] == today.isoformat()


# --- Row 3: b3-k22, the wish's trial --------------------------------------------------------------------------


def test_haiku_s_tool_named_placeholder_is_refused_with_its_read_s_fields() -> None:
    """k22 (5 Nov Haiku): "{{get_weather.max}}" is no {{n.path}}: B5's check never matched it (its test mocked
    the refusal), so the trial kept it as text and the phone showed the placeholder. Now it is unresolved, and
    the refusal lists the fields of the read the tool name points to."""
    from pisa.domain import views as rules
    from pisa.services.views import ViewService

    weather = {"label": "Roma", "date": "2026-11-04", "day": {"min_c": 11.0, "max_c": 16.0}}
    reads = [{"tool": "get_weather", "args": {"where": "Roma", "when": "tomorrow"}}]
    haiku = rules.Recipe(
        reads, [{"type": "stat", "label": "Massima", "value": "{{get_weather.max}}", "unit": "°C"}]
    )
    assert rules.unresolved(haiku, [weather]) == ["{{get_weather.max}}"]
    said = ViewService._wrong(haiku, [weather], "Call wish_skill again")
    assert said is not None and "0.day.max_c" in said and "{{n.path}}" in said
    good = rules.Recipe(
        reads, [{"type": "stat", "label": "Massima", "value": "{{0.day.max_c}}", "unit": "°C"}]
    )
    assert rules.unresolved(good, [weather]) == []
    assert rules.unresolved(good, [None]) == []  # a read failing now is never held against it
