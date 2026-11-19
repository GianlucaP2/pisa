"""The weather (contract W): Open-Meteo's forecast in the user's language, the endpoint and its cache, the
get_weather tool, the briefing's line, and the rain rule for walking legs."""

import asyncio
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from pisa.domain import weather as rules
from pisa.domain.errors import PisaError
from tests.assistant_env import add_event, rome, set_settings
from tests.conftest import pair
from tests.owner import owner_at
from tests.places_env import HERE_MILAN, at_location
from tests.test_place_tools import ask, data_of
from tests.test_planner import place, with_place
from tests.weather_fake import FakeMeteoAPI, fake_meteo

DASH = "\N{EN DASH}"  # a range read aloud as "to"

# --- the rules (pure) -------------------------------------------------------------------------------------


def test_summaries_cells_and_days() -> None:
    assert rules.summary(61, "it-IT") == "Pioggia debole" and rules.summary(61, "en-US") == "Light rain"
    assert rules.summary(53, "it-IT") == "Pioviggine" and rules.summary(1234, "en-US") == "Changeable"
    assert rules.cell(52.381, 4.886) == rules.cell(52.380, 4.890) == "52.38,4.89"  # one ~1 km cell
    thursday = date(2026, 10, 22)
    assert rules.parse_when("oggi", thursday) == thursday
    assert rules.parse_when("Domani", thursday) == date(2026, 10, 23)
    assert rules.parse_when("venerdì", thursday) == date(2026, 10, 23)
    assert rules.parse_when("thursday", thursday) == thursday
    assert rules.parse_when("2026-10-29", thursday) == date(2026, 10, 29)
    with pytest.raises(PisaError) as far:
        rules.parse_when("2026-10-30", thursday)
    assert far.value.message == "The forecast covers today and the next 7 days, not 2026-10-30."


def test_the_briefing_line() -> None:
    wet = rules.Day(date(2026, 10, 22), 9.2, 12.6, 4.2, 80, 25.0, 61)
    assert rules.briefing_line(wet, "it-IT") == f"Oggi: pioggia debole, 9{DASH}13 °C; prendi l'ombrello."
    dry = rules.Day(date(2026, 10, 22), 11, 16, 0, 10, 12, 2)
    assert rules.briefing_line(dry, "en-US") == f"Today: partly cloudy, 11{DASH}16 °C."


def test_the_open_meteo_adapter_reads_the_forecast() -> None:
    forecast = asyncio.run(fake_meteo(date(2026, 10, 22)).forecast(52.357, 4.957, "Europe/Rome"))
    assert len(forecast.days) == 8 and len(forecast.hours) == 193  # The 25th has 25 hours
    first = forecast.days[0]
    assert (first.min_c, first.max_c, first.precip_mm, first.precip_prob, first.code) == (
        9.2,
        12.6,
        4.2,
        80,
        61,
    )
    assert forecast.hours[13].time == rome(2026, 10, 22, 13) and forecast.hours[13].precip_prob == 80


# --- GET /v1/weather --------------------------------------------------------------------------------------


@pytest.fixture
def meteo(http_handler, clock) -> FakeMeteoAPI:  # type: ignore[no-untyped-def]
    fake = FakeMeteoAPI(clock.now.astimezone(ZoneInfo("Europe/Rome")).date())
    http_handler["handler"] = fake
    return fake


def forecasts(meteo: FakeMeteoAPI) -> int:
    return sum(1 for r in meteo.requests if r.url.host == "api.open-meteo.com")


def test_weather_today_and_another_day(client, db, clock, meteo) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    today = client.get("/v1/weather", headers=auth, params={"near": "52.389,4.869"})
    assert today.status_code == 200, today.json()
    body = today.json()
    assert body["date"] == "2026-10-20" and body["label"]
    assert body["day"] == {"min_c": 9.2, "max_c": 12.6, "precip_mm": 4.2, "precip_prob": 80, "wind_kmh": 25.0,
                           "summary": "Pioggia debole"}  # fmt: skip
    hours = body["hours"]  # the next 24 hours, from 13:00 (the clock) in Rome
    assert len(hours) == 24 and hours[0]["time"] == "2026-10-20T13:00:00+02:00"
    assert hours[0] | {"time": "x"} == {
        "time": "x",
        "temp_c": 11.0,
        "precip_prob": 80,
        "summary": "Pioviggine",
    }
    later = client.get("/v1/weather", headers=auth, params={"near": "52.389,4.869", "date": "2026-10-22"})
    assert [h["time"][11:16] for h in later.json()["hours"]][:2] == ["00:00", "01:00"]
    assert len(later.json()["hours"]) == 24 and later.json()["day"]["summary"] == "Parzialmente nuvoloso"
    assert forecasts(meteo) == 1  # the same cell, within 30 minutes: cached
    clock.now += timedelta(minutes=31)
    client.get("/v1/weather", headers=auth, params={"near": "52.389,4.869"})
    assert forecasts(meteo) == 2


def test_weather_errors_and_places(client, db, clock, meteo) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    nowhere = client.get("/v1/weather", headers=auth, params={"near": "here"})
    assert nowhere.status_code == 409 and nowhere.json()["error"]["code"] == "location_unknown"
    far = client.get("/v1/weather", headers=auth, params={"near": "52.387,4.864", "date": "2026-10-28"})
    assert far.status_code == 400 and "next 7 days" in far.json()["error"]["message"]
    spiraal = place(db, "Spiraal", (52.352, 4.868))
    named = client.get("/v1/weather", headers=auth, params={"near": f"place:{spiraal}"}).json()
    assert named["label"] == "Spiraal"


# --- the tool and the briefing ----------------------------------------------------------------------------


def test_get_weather_by_voice(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    said = data_of(ask(db, secrets_key, "get_weather", {"where": "Amsterdam", "when": "domani"})[0])
    first = said.splitlines()[0]
    assert first.startswith("Amsterdam, ") and "Parzialmente nuvoloso, 11 to 16 °C, rain chance 10%" in first
    at_location(db, HERE_MILAN, rome(2026, 10, 20, 12, 55))
    here = data_of(ask(db, secrets_key, "get_weather", {})[0])
    assert "Pioggia debole, 9.2 to 12.6 °C, rain chance 80% (4.2 mm)" in here
    nowhere = ask(db, secrets_key, "get_weather", {"where": "Atlantide"})[0]
    assert "No place or city called 'Atlantide'." in nowhere


def test_the_briefing_says_the_weather(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain import briefing as brain
    from pisa.services.briefing import BriefingService
    from pisa.services.tasks import TasksService
    from tests.conftest import Clock, dsn
    from tests.places_env import place_env
    from tests.places_fake import FakePlaces

    now = rome(2026, 10, 20, 8)
    at_location(db, HERE_MILAN, now - timedelta(hours=3))

    async def go():  # type: ignore[no-untyped-def]
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)
            service = BriefingService(
                env.pool, env.places._gateway, tasks, owner=owner_at(), clock=env.clock,
                weather=env.weather,
            )  # fmt: skip
            settings = {"language": "it-IT", "briefing": {"sections": ["weather"], "max_words": 60}}
            return await service.gather(settings, now)

    data = asyncio.run(go())
    assert data.weather == f"Oggi: pioggia debole, 9{DASH}13 °C; prendi l'ombrello."
    assert "Weather where the user is: Oggi: pioggia debole" in brain.prompt(data)[1]
    assert brain.fallback(data) == data.weather


# --- the rain rule ----------------------------------------------------------------------------------------


def test_in_the_rain_a_long_walk_goes_by_transit(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Walking, a 2.5 km trip to a 14:00 event falls in the rainy hour (13:00-14:00): transit, and it says so.
    The same trip at 17:00, dry, is walked."""
    from pisa.services.planner import PlannerService
    from pisa.services.tasks import TasksService
    from tests.conftest import Clock, dsn
    from tests.places_env import place_env
    from tests.places_fake import FakePlaces

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}, "places": {"default_mode": "walk"}})
    far = place(db, "Fondazione Inventata", (45.445, 9.108))  # ~2.5 km from here
    wet = add_event(db, "Mostra", rome(2026, 10, 20, 14), minutes=60)
    dry = add_event(db, "Aperitivo", rome(2026, 10, 20, 17, 30), minutes=60)
    with_place(db, "calendar_events", wet, far)
    # Back the other way, within a 45-minute walk (a longer one goes by transit anyway).
    with_place(db, "calendar_events", dry, place(db, "Bar Bislacco", (45.4590, 9.1130)))
    now = rome(2026, 10, 20, 12, 30)
    at_location(db, HERE_MILAN, now - timedelta(minutes=1))

    async def go():  # type: ignore[no-untyped-def]
        async with place_env(dsn(db), secrets_key, FakePlaces(), Clock(now), keys=()) as env:
            tasks = TasksService(env.pool, owner=owner_at(), clock=env.clock)
            planner = PlannerService(env.pool, tasks, env.places, owner=owner_at(), clock=env.clock,
                                     weather=env.weather)  # fmt: skip
            return await planner.plan()

    plan = asyncio.run(go())
    trips = [i for i in plan["items"] if i["kind"] == "travel"]
    to_show, to_bar = trips[0], trips[-1]
    assert to_show["mode"] == "transit" and to_show["title"] == f"Piove: {to_show['minutes']} min con i mezzi"
    assert to_show["rain"] is True  # IOS shows the rain note only on a leg the rain rule switched (IOS Q3)
    assert to_bar["mode"] == "walk" and to_bar["title"].endswith("on foot") and "rain" not in to_bar


def test_a_leg_the_resolve_moved_out_of_the_rain_says_transit_not_rain() -> None:
    """LOW: after the re-solve with transit times, a leg leaving at 16:05 is past the rain (13:00-15:59): it
    still goes by transit (its times are transit times), but it says so plainly, without "Piove" or rain."""
    from types import SimpleNamespace

    from pisa.planner.solver import Leg, Solution
    from pisa.services.planner import Built, PlannerService

    start = rome(2026, 10, 20, 12)
    day = SimpleNamespace(
        win=SimpleNamespace(start=start), rain=[rome(2026, 10, 20, h) for h in (13, 14, 15)]
    )
    solution = Solution(
        "optimal", arrivals={"A": 255, "B": 120},  # A at 16:15 (left 16:05), B at 14:00 (left 13:50)
        legs=[Leg("B", 0, 1, 10, 0), Leg("A", 1, 2, 10, 0)],
    )  # fmt: skip
    built = Built(solution, [], {}, [], [], [])
    planner = object.__new__(PlannerService)
    planner.with_rain_modes(day, built, None, {(0, 1), (1, 2)})  # type: ignore[arg-type]
    assert built.transit_legs == {(0, 1), (1, 2)} and built.rain_legs == {(0, 1)}


def test_the_night_the_clocks_go_back_has_25_distinct_hours() -> None:
    """LOW: Open-Meteo's times as instants (timeformat=unixtime): on 25 Oct 02:00-02:59 happens twice, and
    both hours are kept, in order."""
    from datetime import date

    first = date(2026, 10, 25)
    forecast = asyncio.run(fake_meteo(first).forecast(41.9, 12.5, "Europe/Rome"))
    that_day = [h.time for h in forecast.hours if h.time.date() == first]
    assert len(that_day) == 25 and that_day == sorted(that_day, key=lambda t: t.timestamp())
    assert [t.utcoffset().total_seconds() / 3600 for t in that_day if t.hour == 2] == [2.0, 1.0]  # type: ignore[union-attr]
    assert forecast.days[0].date == first


def test_the_rain_flag_reaches_the_app() -> None:
    """IOS Q3: `rain: true` is part of the plan's travel item in the API, absent elsewhere."""
    from pisa.api.schemas import PlanItem

    wet = {"kind": "travel", "start": "2026-10-20T13:34:00+02:00", "end": "2026-10-20T13:50:00+02:00",
           "mode": "transit", "minutes": 16, "rain": True}  # fmt: skip
    assert PlanItem.model_validate(wet).model_dump(exclude_unset=True)["rain"] is True
    dry = PlanItem.model_validate(wet | {"mode": "walk"} | {"rain": None}).model_dump(exclude_none=True)
    assert "rain" not in dry
