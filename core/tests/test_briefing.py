"""The morning briefing (contracts D and F): each trigger alone, the window and the briefing day it
defines, the repeat rule, one briefing for simultaneous triggers, no double send across a restart, the time
trigger's 30-min catch-up, quiet mode per trigger, the phrase always delivering, and asking."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

import psycopg
import pytest

from pisa.brain.briefing import EMPTY_DAY
from pisa.services.notifications import release_held
from tests.assistant_env import (
    TZ,
    Env,
    add_event,
    briefings,
    notifications,
    only_trigger,
    rome,
    services,
    set_settings,
)
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.owner import owner_at

SPOKEN = "Alle 9 hai il dentista in Via Immaginaria 1."


def model() -> FakeModels:
    fake = FakeModels()
    fake.default = SPOKEN
    return fake


def at(
    db: psycopg.Connection,
    secrets_key: str,
    now: datetime,
    action: Callable[[Env], Awaitable[Any]],
    fake: FakeModels | None = None,
) -> Any:
    """Run `action` with the clock at `now` (a fresh service each time, like a new process)."""

    async def go() -> Any:
        async with services(dsn(db), secrets_key, fake or model(), Clock(now)) as env:
            return await action(env)

    return asyncio.run(go())


@pytest.fixture
def device(db: psycopg.Connection) -> uuid.UUID:
    return uuid.UUID(pair(db)[0])


@pytest.fixture
def busy_day(db: psycopg.Connection) -> None:
    add_event(db, "Dentista", rome(2026, 10, 20, 9), location="Via Immaginaria 1")
    add_event(db, "Dentista", rome(2026, 10, 21, 9), location="Via Immaginaria 1")


def glasses(device: uuid.UUID) -> Callable[[Env], Awaitable[Any]]:
    return lambda env: env.briefings.on_presence(device, "glasses_on")


def app_open(device: uuid.UUID) -> Callable[[Env], Awaitable[Any]]:
    return lambda env: env.briefings.on_presence(device, "app_open")


def time_check(env: Env) -> Awaitable[Any]:
    return env.briefings.on_time()


# --- each trigger alone -----------------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["glasses_on", "app_open", "time", "phrase"])
def test_each_trigger_alone(db, secrets_key, device, busy_day, trigger: str) -> None:  # type: ignore[no-untyped-def]
    only_trigger(db, trigger)

    async def all_four(env: Env) -> dict[str, Any]:
        return {
            "glasses_on": await env.briefings.on_presence(device, "glasses_on"),
            "app_open": await env.briefings.on_presence(device, "app_open"),
            "time": await env.briefings.on_time(),
            "phrase": await env.briefings.for_phrase("Buongiorno Pisa"),
        }

    fired = at(db, secrets_key, rome(2026, 10, 20, 7, 30), all_four)
    assert [name for name, b in fired.items() if b is not None] == [trigger]
    assert briefings(db) == [(trigger, date(2026, 10, 20), SPOKEN)]
    sent = notifications(db)
    if trigger in ("glasses_on", "app_open"):
        assert sent == [("briefing", SPOKEN, device, None)]  # to that device
    elif trigger == "time":
        assert sent == [("briefing", SPOKEN, None, None)]  # every device (the delivery rule)
    else:
        assert sent == []  # the phrase: spoken as the reply to the utterance


def test_all_triggers_off_asking_still_works(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(
        db,
        {"briefing": {"triggers": {"glasses_on": False, "app_open": False, "phrase": False, "time": False}}},
    )

    async def everything(env: Env) -> list[Any]:
        return [
            await env.briefings.on_presence(device, "glasses_on"),
            await env.briefings.on_presence(device, "app_open"),
            await env.briefings.on_time(),
            await env.briefings.for_phrase("Buongiorno Pisa"),
        ]

    assert at(db, secrets_key, rome(2026, 10, 20, 7, 30), everything) == [None] * 4
    asked = at(db, secrets_key, rome(2026, 10, 20, 7, 31), lambda env: env.briefings.ask())
    assert asked.trigger == "ask" and asked.text == SPOKEN
    assert notifications(db) == []  # asked: returned, never delivered


# --- the window and the briefing day ----------------------------------------------------------------------


def test_window_limits_presence_triggers(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    assert at(db, secrets_key, rome(2026, 10, 20, 13, 0), glasses(device)) is None  # window is 04:00-13:00
    assert at(db, secrets_key, rome(2026, 10, 20, 3, 59), app_open(device)) is None
    assert at(db, secrets_key, rome(2026, 10, 20, 12, 59), glasses(device)) is not None


def test_window_crossing_midnight_and_its_briefing_day(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"window": {"start": "20:00", "end": "03:00"}}})
    assert at(db, secrets_key, rome(2026, 10, 20, 12, 0), glasses(device)) is None
    first = at(db, secrets_key, rome(2026, 10, 20, 23, 0), glasses(device))
    assert first.day == date(2026, 10, 20)
    # 01:30 on the 21st is still the briefing day of the 20th: once per briefing day.
    assert at(db, secrets_key, rome(2026, 10, 21, 1, 30), app_open(device)) is None
    assert at(db, secrets_key, rome(2026, 10, 21, 3, 0), glasses(device)) is None  # window over
    second = at(db, secrets_key, rome(2026, 10, 21, 20, 0), glasses(device))
    assert second.day == date(2026, 10, 21)


def test_no_window_means_any_time_and_a_day_from_04_00(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"window": None}})
    first = at(db, secrets_key, rome(2026, 10, 20, 15, 0), glasses(device))
    assert first is not None and first.day == date(2026, 10, 20)
    assert at(db, secrets_key, rome(2026, 10, 21, 3, 59), glasses(device)) is None  # still the 20th
    assert at(db, secrets_key, rome(2026, 10, 21, 4, 0), glasses(device)).day == date(2026, 10, 21)


# --- the repeat rule -------------------------------------------------------------------------------------


def test_repeat_null_is_once_per_briefing_day(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 0), glasses(device)) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 1), app_open(device)) is None
    assert at(db, secrets_key, rome(2026, 10, 20, 12, 30), glasses(device)) is None
    assert at(db, secrets_key, rome(2026, 10, 21, 4, 0), glasses(device)) is not None  # a new briefing day
    assert [t for t, _, _ in briefings(db)] == ["glasses_on", "glasses_on"]


def test_repeat_after_minutes(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"repeat_after_min": 30}})
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 0), glasses(device)) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 29), app_open(device)) is None
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 30), app_open(device)) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 45), glasses(device)) is None
    assert len(briefings(db)) == 2


def test_explicit_requests_never_count_against_the_repeat_rule(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    at(db, secrets_key, rome(2026, 10, 20, 7, 0), lambda env: env.briefings.ask())
    at(db, secrets_key, rome(2026, 10, 20, 7, 1), lambda env: env.briefings.for_phrase("Buongiorno Pisa"))
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 2), glasses(device)) is not None


# --- concurrency and restarts ----------------------------------------------------------------------------


def test_simultaneous_triggers_give_one_briefing(db, secrets_key, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"time": True}}})
    phone, ipad = uuid.UUID(pair(db, "iPhone")[0]), uuid.UUID(pair(db, "iPad")[0])
    fake = model()
    fake.chunk_delay = 0.05

    async def together(env: Env) -> list[Any]:
        return await asyncio.gather(  # type: ignore[return-value]
            env.briefings.on_presence(phone, "glasses_on"),
            env.briefings.on_presence(phone, "app_open"),
            env.briefings.on_presence(ipad, "app_open"),
            env.briefings.on_time(),
        )

    results = at(db, secrets_key, rome(2026, 10, 20, 7, 30), together, fake)
    assert sum(r is not None for r in results) == 1
    assert len(briefings(db)) == 1 and len(notifications(db)) == 1


def test_no_double_send_across_a_restart(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"time": True}}})
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 30), time_check) is not None
    # A new process (new service, new pool): the decision reads the briefings table, not memory.
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 31), time_check) is None
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 32), glasses(device)) is None
    assert len(notifications(db)) == 1


# --- the time trigger ------------------------------------------------------------------------------------


def test_time_trigger_and_its_30_minute_catch_up(db, secrets_key, busy_day) -> None:  # type: ignore[no-untyped-def]
    only_trigger(db, "time")
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 29), time_check) is None
    late = at(db, secrets_key, rome(2026, 10, 20, 7, 59), time_check)  # the server was down: 29 min late
    assert late is not None and late.trigger == "time"


def test_time_missed_by_30_minutes_is_skipped_and_logged(db, secrets_key, busy_day) -> None:  # type: ignore[no-untyped-def]
    only_trigger(db, "time")
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 0), time_check) is None
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 1), time_check) is None
    assert briefings(db) == [] and notifications(db) == []
    row = db.execute("SELECT value FROM app_settings WHERE key = 'briefing_time_missed'").fetchone()
    assert row == ("2026-10-20",)  # logged once for that briefing day
    assert at(db, secrets_key, rome(2026, 10, 21, 7, 30), time_check) is not None  # the next day works


def test_time_trigger_after_an_automatic_briefing_stays_silent(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"time": True}}})
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 0), glasses(device)) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 30), time_check) is None


def test_time_with_repeat_fires_once_per_day_after_the_interval(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"time": True}, "repeat_after_min": 15}})
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 20), glasses(device)) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 30), time_check) is None  # 10 min since the last one
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 35), time_check) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 51), time_check) is None  # time fires once a day


# --- quiet mode per trigger ------------------------------------------------------------------------------


def test_presence_and_phrase_ignore_quiet_mode(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    assert at(db, secrets_key, rome(2026, 10, 20, 6, 0), glasses(device)) is not None  # quiet until 07:00
    assert notifications(db) == [("briefing", SPOKEN, device, None)]  # not held: you just showed up
    assert at(db, secrets_key, rome(2026, 10, 20, 23, 0), lambda env: env.briefings.for_phrase("Buongiorno"))


def test_time_trigger_is_held_then_delivered_as_itself(db, secrets_key, busy_day) -> None:  # type: ignore[no-untyped-def]
    only_trigger(db, "time", time="06:30")
    assert at(db, secrets_key, rome(2026, 10, 20, 6, 30), time_check) is not None
    assert notifications(db) == [("briefing", SPOKEN, None, "held")]

    def release(now: datetime) -> Callable[[Env], Awaitable[int]]:
        return lambda env: release_held(env.pool, now=now, owner=owner_at(TZ))

    assert at(db, secrets_key, rome(2026, 10, 20, 6, 59), release(rome(2026, 10, 20, 6, 59))) == 0
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 0), release(rome(2026, 10, 20, 7, 0))) == 1
    assert notifications(db) == [("briefing", SPOKEN, None, None)]  # pending delivery, as the briefing


def test_showing_up_during_quiet_supersedes_a_held_time_briefing(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    # The held time briefing must not use up the day, nor arrive later as a second one.
    set_settings(db, {"briefing": {"triggers": {"time": True}, "time": "06:30"}})
    assert at(db, secrets_key, rome(2026, 10, 20, 6, 30), time_check) is not None
    assert at(db, secrets_key, rome(2026, 10, 20, 6, 45), glasses(device)) is not None
    assert notifications(db) == [("briefing", SPOKEN, None, "none"), ("briefing", SPOKEN, device, None)]
    now = rome(2026, 10, 20, 7, 0)
    assert at(db, secrets_key, now, lambda env: release_held(env.pool, now=now, owner=owner_at(TZ))) == 0


def test_an_undelivered_briefing_does_not_use_up_the_day(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    assert at(db, secrets_key, rome(2026, 10, 20, 7, 0), glasses(device)) is not None
    db.execute("UPDATE notifications SET delivered_via = 'none', delivered_at = now()")  # no channel reached
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 0), app_open(device)) is not None
    db.execute(
        "UPDATE notifications SET delivered_via = 'ws', delivered_at = now() WHERE delivered_at IS NULL"
    )
    assert at(db, secrets_key, rome(2026, 10, 20, 9, 0), app_open(device)) is None


# --- the phrase ------------------------------------------------------------------------------------------


def test_phrase_always_delivers_freshly_composed(db, secrets_key, device, busy_day) -> None:  # type: ignore[no-untyped-def]
    fake = model()
    replies = iter(["Prima versione.", "Seconda versione.", "Terza versione."])
    fake.default = lambda messages, info: next(replies)
    add_event(db, "Cena", rome(2026, 10, 20, 20))  # still ahead at 15:00
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 0), glasses(device), fake) is not None
    second = at(
        db, secrets_key, rome(2026, 10, 20, 8, 1), lambda e: e.briefings.for_phrase("Buongiorno Pisa"), fake
    )
    third = at(
        db,
        secrets_key,
        rome(2026, 10, 20, 15, 0),
        lambda e: e.briefings.for_phrase("good morning pisa"),
        fake,
    )
    assert (second.text, third.text) == ("Seconda versione.", "Terza versione.")  # no window, no repeat rule
    assert [t for t, _, _ in briefings(db)] == ["glasses_on", "phrase", "phrase"]


@pytest.mark.parametrize("said", ["buongiorno a tutti", "Buongiorno Pisa, che tempo fa?", "Ciao Pisa"])
def test_other_utterances_go_to_the_agent(db, secrets_key, busy_day, said: str) -> None:  # type: ignore[no-untyped-def]
    assert at(db, secrets_key, rome(2026, 10, 20, 8, 0), lambda env: env.briefings.for_phrase(said)) is None
    assert briefings(db) == []


def test_custom_phrases(db, secrets_key, busy_day) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"phrases": ["Com'è la giornata, Pisa?"]}})
    assert at(
        db, secrets_key, rome(2026, 10, 20, 8, 0), lambda e: e.briefings.for_phrase("com e la giornata pisa")
    )
    assert (
        at(db, secrets_key, rome(2026, 10, 20, 8, 0), lambda e: e.briefings.for_phrase("Buongiorno Pisa"))
        is None
    )


# --- contract F (REST) ------------------------------------------------------------------------------------


def test_latest_and_ask(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    missing = client.get("/v1/briefings/latest", headers=auth)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    created = client.post("/v1/briefings", headers=auth)
    assert created.status_code == 201
    body = created.json()
    # The client's clock: 2026-10-20 11:00 UTC. Nothing on the calendar: the fixed sentence, no model call.
    assert body["trigger"] == "ask" and body["day"] == "2026-10-20" and body["lang"] == "it-IT"
    assert body["text"] == EMPTY_DAY["it-IT"]
    assert set(body) == {"id", "day", "trigger", "text", "lang", "created_at"}
    assert client.get("/v1/briefings/latest", headers=auth).json() == body
    assert notifications(db) == []  # returned, not delivered
