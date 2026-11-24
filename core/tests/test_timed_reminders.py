"""Real timed reminders (gate 7).

The owner's "Ricordami tra 2 minuti di …" on v0.1.0: the model called `remember_to(when="tra 2 minuti")`,
whose `when` took only HH:MM or an ISO date-time ("Not saved: give when as HH:MM…"): nothing was stored, and
the reply "Te lo ricordo alle 12:28" went out (no rail backed a promise then). Even stored (HH:MM), a timed
promise was only a sense of the `attend` step (Pisa's initiative, decided by a model, after a 2-minute batch
window, as an `info` candidate it may fold or drop): the reminders step never read it. Now:
- `when` takes their words ("tra 2 minuti", "domani alle 9"); the result says "Reminder set: «…» at 13:02
  (id …)" or "Not set: …";
- the reminders step fires a timed reminder at its exact minute as a floor (the push with the app closed);
- a task that got its time inside its lead is reminded at its due time, never silently missed;
- a promise for later stands only on a reminder set this turn, for the time it names.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.domain import abilities
from pisa.domain.reminders import fire_time
from pisa.infra.keys import KeyStore
from pisa.services.notifications import Dispatcher
from pisa.services.push import PushService, set_apns_key
from tests.assistant_env import add_task, rome, services, set_settings
from tests.conftest import Clock, FakeModels, dsn, pair_v2, pause_outbox, set_key
from tests.test_abilities import _ask
from tests.test_push import KEY_ID, TEAM_ID, TOPIC, Apple, Live, p8, with_token

TZ = ZoneInfo("Europe/Rome")


# --- when: their words ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("tra 2 minuti", datetime(2026, 11, 22, 12, 28, tzinfo=TZ)),  # 12:26:40 + 2 min, at its minute
        ("fra un'ora", datetime(2026, 11, 22, 13, 26, tzinfo=TZ)),
        ("in 10 minutes", datetime(2026, 11, 22, 12, 36, tzinfo=TZ)),
        ("tra mezz'ora", datetime(2026, 11, 22, 12, 56, tzinfo=TZ)),
        ("domani alle 9", datetime(2026, 11, 23, 9, 0, tzinfo=TZ)),
        ("alle 9", datetime(2026, 11, 23, 9, 0, tzinfo=TZ)),  # past today: tomorrow's
        ("18:30", datetime(2026, 11, 22, 18, 30, tzinfo=TZ)),
        ("lunedì alle 9.15", datetime(2026, 11, 23, 9, 15, tzinfo=TZ)),
        ("tomorrow at 9", datetime(2026, 11, 23, 9, 0, tzinfo=TZ)),
        ("2026-11-24T10:00", datetime(2026, 11, 24, 10, 0, tzinfo=TZ)),
        ("oggi alle 9", None),  # past
        ("tra 0 minuti", None),
        ("dopo", None),
        ("", None),
    ],
)
def test_when_is_their_words(when: str, expected: datetime | None) -> None:
    now = datetime(2026, 11, 22, 12, 26, 40, tzinfo=TZ)
    assert fire_time(when, now, TZ) == expected


# --- end to end: the turn, the reminders step, the push ------------------------------------------------


def _scripted(tool: str, args: dict[str, Any], reply: str, results: list[str] | None = None) -> Any:
    async def model(messages, info):  # type: ignore[no-untyped-def]
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returned:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}
            return
        if results is not None:
            results.append(str(returned[-1].content))
        yield reply

    return model


def _minute(client: Any) -> None:
    """The worker's on-time steps for this minute: reminders, then the floors they proposed."""
    runtime = client.app.state.runtime
    client.portal.call(runtime.reminders.run)
    client.portal.call(runtime.attend.floors)


def _reminders(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    return db.execute("SELECT id, kind, text FROM notifications ORDER BY created_at").fetchall()


def test_tra_2_minuti_fires_at_its_minute_and_is_pushed_with_the_app_closed(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models: FakeModels, clock
) -> None:
    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    results: list[str] = []
    fake_models.default = _scripted(
        "remember_to",
        {"what": "chiamare Marco", "condition": "time", "when": "tra 2 minuti"},
        "Fatto, te lo ricordo alle 13:02.",
        results,
    )
    answer = _ask(client, token, device_id, "Ricordami tra 2 minuti di chiamare Marco")
    (promise_id, due_at), = db.execute("SELECT id, due_at FROM pisa_promises").fetchall()  # fmt: skip
    assert due_at == clock.now + timedelta(minutes=2)  # 11:00 UTC is 13:00 in Rome
    # the result carries the stored record's id and its exact time; the promise stands on it
    assert results and results[0].startswith(f"Reminder set: «chiamare Marco» at 13:02 (id {promise_id}).")
    assert answer == "Fatto, te lo ricordo alle 13:02."

    pause_outbox(client)  # this test's dispatcher delivers, as the app is closed
    clock.now += timedelta(minutes=1)
    _minute(client)
    assert _reminders(db) == []  # not before its minute
    clock.now += timedelta(minutes=1)
    _minute(client)
    (nid, kind, text), = _reminders(db)  # fmt: skip
    assert (kind, text) == ("reminder", "Ti avevo detto che te lo ricordavo: chiamare Marco.")
    clock.now += timedelta(minutes=1)
    _minute(client)
    assert len(_reminders(db)) == 1  # once

    # the app closed (no socket): the push path
    with_token(db, device_id)
    apple = Apple()
    runtime = client.app.state.runtime

    async def deliver() -> str | None:
        keys = KeyStore(SecretStr(secrets_key))
        async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
            push = PushService(runtime.pool, keys, http, team_id=TEAM_ID, topic=TOPIC)
            async with runtime.pool.connection() as conn:
                await set_apns_key(conn, keys, KEY_ID, p8()[0])
            dispatcher = Dispatcher(runtime.pool, Live(set()), push, clock=runtime.clock, owner=runtime.owner)
            return await dispatcher.deliver(nid)

    assert client.portal.call(deliver) == "apns"
    (request,) = apple.requests
    body = json.loads(request.content)
    assert body["aps"]["alert"]["body"] == "Ti avevo detto che te lo ricordavo: chiamare Marco."
    assert body["notification_id"] == str(nid)


def test_a_failed_remember_to_backs_no_fatto(client, db, secrets_key, fake_models: FakeModels) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    results: list[str] = []
    fake_models.default = _scripted(
        "remember_to",
        {"what": "chiamare Marco", "condition": "time", "when": "quando capita"},
        "Fatto, te lo ricordo alle 13:02.",
        results,
    )
    answer = _ask(client, token, device_id, "Ricordami quando capita di chiamare Marco")
    assert results and results[0].startswith("Not set: nothing was stored")
    assert "te lo ricordo" not in answer.lower() and "fatto" not in answer.lower()
    assert db.execute("SELECT count(*) FROM pisa_promises").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_a_promise_naming_another_time_than_the_one_set_is_held(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models: FakeModels
) -> None:
    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    fake_models.default = _scripted(
        "remember_to",
        {"what": "chiamare Marco", "condition": "time", "when": "tra 2 minuti"},
        "Te lo ricordo alle 13:20.",  # its own sum, not the result's 13:02
    )
    answer = _ask(client, token, device_id, "Ricordami tra 2 minuti di chiamare Marco")
    assert "13:20" not in answer


def test_a_promise_stands_only_on_the_time_it_was_set_for() -> None:
    set_at = ["Reminder set: «chiamare Marco» at 12:28 (id 1).", '{"status": "done"}']
    assert abilities.promised_time_set("Te lo ricordo alle 12:28.", set_at)
    assert not abilities.promised_time_set("Te lo ricordo alle 12:30.", set_at)
    assert abilities.promised_time_set("Te lo ricordo tra poco.", set_at)  # no clock time named
    assert not abilities.promised_time_set("Te lo ricordo alle 12:28.", ["Not set: nothing was stored"])
    tomorrow = ["Reminder set: «x» at 09:00 on Mon 23 Nov (id 2)."]
    assert abilities.promised_time_set("Domani alle 9:00 te lo ricordo.", tomorrow)


# --- the reminders step: exact times, leads, misses ------------------------------------------------------


@pytest.fixture
def no_quiet_hours(db: psycopg.Connection) -> None:
    set_settings(db, {"quiet": {"hours": None}})


def _check(db: psycopg.Connection, secrets_key: str, now: datetime) -> Any:
    async def go() -> Any:
        async with services(dsn(db), secrets_key, FakeModels(), Clock(now)) as env:
            return await env.reminders.run()

    return asyncio.run(go())


def _promise(db: psycopg.Connection, what: str, due: datetime, created: datetime) -> uuid.UUID:
    promise_id = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_promises (id, what, condition, due_at, lang, provenance, created_at) "
        "VALUES (%s, %s, 'time', %s, 'it-IT', 'owner_words', %s)",
        (promise_id, what, due, created),
    )
    return promise_id


def _sent(db: psycopg.Connection) -> list[str]:
    return [r[0] for r in db.execute("SELECT text FROM notifications ORDER BY created_at").fetchall()]


def test_domani_alle_9_a_reminder_fires_at_9_a_task_at_its_lead(  # type: ignore[no-untyped-def]
    db, secrets_key, no_quiet_hours
) -> None:
    """The rule: a timed reminder (`remember_to`) fires at its exact time; a task's reminder keeps its lead
    (task_lead_min, 15) before its due time."""
    _promise(db, "chiamare Marco", rome(2026, 10, 21, 9), rome(2026, 10, 20, 18))
    add_task(db, "Pagare la bolletta", rome(2026, 10, 21, 9))
    for minute in range(40, 60):
        _check(db, secrets_key, rome(2026, 10, 21, 8, minute))
    assert _sent(db) == ["Scade tra 15 minuti: Pagare la bolletta."]  # 8:45
    _check(db, secrets_key, rome(2026, 10, 21, 9, 0))
    assert _sent(db)[1:] == ["Ti avevo detto che te lo ricordavo: chiamare Marco."]  # 9:00, not 8:45
    _check(db, secrets_key, rome(2026, 10, 21, 9, 1))
    assert len(_sent(db)) == 2
    assert db.execute("SELECT fired_at FROM pisa_promises").fetchone() == (rome(2026, 10, 21, 9),)


def test_a_task_timed_inside_its_lead_fires_at_its_due_time(db, secrets_key, no_quiet_hours) -> None:  # type: ignore[no-untyped-def]
    """Due in 3 minutes with a 15-minute lead: its lead moment was past when it was written; before, the
    candidate query never saw it."""
    _check(db, secrets_key, rome(2026, 10, 20, 12, 25))
    task = add_task(db, "Chiamare Marco", rome(2026, 10, 20, 12, 28), created=rome(2026, 10, 20, 12, 25))
    for minute in range(26, 28):
        _check(db, secrets_key, rome(2026, 10, 20, 12, minute))
    assert _sent(db) == []
    _check(db, secrets_key, rome(2026, 10, 20, 12, 28))
    assert _sent(db) == ["Scade ora: Chiamare Marco."]
    for minute in range(29, 35):
        _check(db, secrets_key, rome(2026, 10, 20, 12, minute))
    assert len(_sent(db)) == 1
    sent = db.execute("SELECT kind, ref, fire_at, outcome FROM reminders_sent").fetchall()
    assert sent == [("task", str(task), rome(2026, 10, 20, 12, 28), "sent")]


def test_a_task_reminded_at_its_lead_is_not_reminded_again_after_an_edit(  # type: ignore[no-untyped-def]
    db, secrets_key, no_quiet_hours
) -> None:
    task = add_task(db, "Pagare la bolletta", rome(2026, 10, 20, 18))
    _check(db, secrets_key, rome(2026, 10, 20, 17, 45))
    db.execute("UPDATE tasks SET title = 'Pagare la luce', updated_at = %s WHERE id = %s",
               (rome(2026, 10, 20, 17, 50), task))  # fmt: skip
    for minute in range(51, 60):
        _check(db, secrets_key, rome(2026, 10, 20, 17, minute))
    _check(db, secrets_key, rome(2026, 10, 20, 18, 0))
    assert _sent(db) == ["Scade tra 15 minuti: Pagare la bolletta."]


def test_a_timed_reminder_missed_past_the_catch_up_is_skipped_once(  # type: ignore[no-untyped-def]
    db, secrets_key, no_quiet_hours
) -> None:
    _check(db, secrets_key, rome(2026, 10, 20, 12, 0))
    _promise(db, "chiamare Marco", rome(2026, 10, 20, 12, 10), rome(2026, 10, 20, 12, 0))
    report = _check(db, secrets_key, rome(2026, 10, 20, 12, 16))  # the worker was down: 6 minutes late
    assert (report.sent, report.skipped) == (0, 1) and _sent(db) == []
    assert _check(db, secrets_key, rome(2026, 10, 20, 12, 17)).skipped == 0


def test_quiet_mode_holds_a_timed_reminder_like_any_reminder(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": {"start": "22:30", "end": "07:00"}}})
    _promise(db, "chiamare Marco", rome(2026, 10, 20, 23, 0), rome(2026, 10, 20, 22, 0))
    _check(db, secrets_key, rome(2026, 10, 20, 23, 0))
    assert db.execute("SELECT kind, delivered_via FROM notifications").fetchall() == [("reminder", "held")]


def test_floors_run_seconds_into_the_minute_still_deliver_a_timed_reminder_and_a_near_task(  # type: ignore[no-untyped-def]
    client, db, secrets_key, clock, no_quiet_hours
) -> None:
    """Gate 7 (audit §22): the worker runs reminders, then attend_floors, some seconds into the minute. A
    timed reminder's floor had its own minute as deadline (a near task's, its due time minus 5 minutes), so
    attend_floors expired it as past before delivering it. Each is delivered once, through attend."""
    pause_outbox(client)
    due = clock.now.replace(second=0, microsecond=0) + timedelta(minutes=2)
    _promise(db, "bere acqua", due, clock.now)
    task = add_task(db, "Chiamare Marco", due, created=clock.now)
    clock.now = due + timedelta(seconds=20)
    _minute(client)
    texts = sorted(t for _, _, t in _reminders(db))
    assert texts == ["Scade ora: Chiamare Marco.", "Ti avevo detto che te lo ricordavo: bere acqua."]
    outcomes = db.execute(
        "SELECT key, outcome, notification_id IS NOT NULL FROM attend_candidates ORDER BY key"
    ).fetchall()
    assert all(o != "expired" and linked for _, o, linked in outcomes), outcomes
    clock.now += timedelta(minutes=1)
    _minute(client)
    assert len(_reminders(db)) == 2  # once each
    assert db.execute("SELECT ref FROM reminders_sent").fetchall() == [(str(task),)]


# --- the rail reads the reply's time words as remember_to does (security review TK-2) ----------------------

_TZ = ZoneInfo("Europe/Rome")
_NOW = datetime(2026, 11, 24, 12, 26, 40, tzinfo=_TZ)  # a Tuesday
_AT_1228 = ["Reminder set: «chiamare Marco» at 12:28 (id 1)."]
_TOMORROW_9 = ["Reminder set: «chiamare Marco» at 09:00 on Wed 25 Nov (id 2)."]
_TOMORROW_930 = ["Reminder set: «chiamare Marco» at 09:30 on Wed 25 Nov (id 3)."]
_TONIGHT_21 = ["Reminder set: «chiamare Marco» at 21:00 (id 4)."]


@pytest.mark.parametrize(
    ("sentence", "results"),
    [
        ("Te lo ricordo alle 9.", _TOMORROW_9),
        ("Te lo ricordo alle nove.", _TOMORROW_9),
        ("Te lo ricordo domani alle 9.", _TOMORROW_9),
        ("Domani alle nove te lo ricordo.", _TOMORROW_9),
        ("Te lo ricordo alle 9 e mezza.", _TOMORROW_930),
        ("Te lo ricordo domani alle nove e mezza.", _TOMORROW_930),
        ("Te lo ricordo tra 2 minuti.", _AT_1228),
        ("Te lo ricordo tra due minuti.", _AT_1228),
        ("Te lo ricordo alle 12:28.", _AT_1228),
        ("Te lo ricordo alle 9 di sera.", _TONIGHT_21),
        ("Te lo ricordo stasera alle 21.", _TONIGHT_21),
        ("I'll remind you at 9am.", _TOMORROW_9),
        ("I'll remind you tomorrow at 9am.", _TOMORROW_9),
        ("I'll remind you at nine.", _TOMORROW_9),
        ("I'll remind you at half past nine.", _TOMORROW_930),
        ("I'll remind you in 2 minutes.", _AT_1228),
        ("I'll remind you at 9pm.", _TONIGHT_21),
    ],
)
def test_a_promise_in_the_reply_s_own_time_words_stands_on_the_time_set(
    sentence: str, results: list[str]
) -> None:
    assert abilities.promised_time_set(sentence, results, _NOW, _TZ)


@pytest.mark.parametrize(
    ("sentence", "results"),
    [
        ("Te lo ricordo alle 10.", _TOMORROW_9),
        ("Te lo ricordo alle dieci.", _TOMORROW_9),
        ("Te lo ricordo alle 9 e mezza.", _TOMORROW_9),
        ("Te lo ricordo tra 5 minuti.", _AT_1228),
        ("Te lo ricordo tra un'ora.", _AT_1228),
        ("Te lo ricordo lunedì alle 9.", _TOMORROW_9),  # a day it names is the stored one, or held
        ("Te lo ricordo oggi alle 9.", _TOMORROW_9),
        ("Te lo ricordo domani alle 12:28.", _AT_1228),
        ("Te lo ricordo domani alle 9.", ["Not set: nothing was stored, `when` names no future time."]),
        ("I'll remind you at 9pm.", _TOMORROW_9),
        ("I'll remind you at 10am.", _TOMORROW_9),
        ("I'll remind you in 10 minutes.", _AT_1228),
        ("I'll remind you on Friday at 9am.", _TOMORROW_9),
    ],
)
def test_a_promise_naming_another_moment_in_words_is_held(sentence: str, results: list[str]) -> None:
    assert not abilities.promised_time_set(sentence, results, _NOW, _TZ)


def test_a_delay_resolves_from_the_turn_s_clock_with_a_minute_s_slack() -> None:
    # set at 12:26:40 for 12:28; the reply's turn may read its clock a little later (12:27:05: 12:29)
    assert abilities.promised_time_set(
        "Te lo ricordo tra 2 minuti.", _AT_1228, _NOW + timedelta(seconds=25), _TZ
    )
    assert not abilities.promised_time_set(
        "Te lo ricordo tra 2 minuti.", _AT_1228, _NOW + timedelta(minutes=2), _TZ
    )


def test_a_reply_naming_no_moment_stands_as_before() -> None:
    for sentence in ("Te lo ricordo.", "Te lo ricordo tra poco.", "I'll remind you later."):
        assert abilities.promised_time_set(sentence, _AT_1228, _NOW, _TZ)


def test_a_promise_in_words_naming_another_time_is_held_in_a_turn(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models: FakeModels
) -> None:
    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    fake_models.default = _scripted(
        "remember_to",
        {"what": "chiamare Marco", "condition": "time", "when": "tra 2 minuti"},
        "Te lo ricordo tra venti minuti.",  # not the minute set
    )
    answer = _ask(client, token, device_id, "Ricordami tra 2 minuti di chiamare Marco")
    assert "venti minuti" not in answer
