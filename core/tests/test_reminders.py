"""Reminders (no model): timed events and date-time tasks, at most once per (kind, ref, fire_at), a moved one
reminded again, a cancelled or done one never, the 5-minute catch-up, misses skipped, DST days."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.domain.reminders import reminder_text
from pisa.services.reminders import ReminderReport
from tests.assistant_env import add_event, add_task, notifications, rome, services, set_settings
from tests.conftest import Clock, FakeModels, dsn


@pytest.fixture(autouse=True)
def no_quiet_hours(db: psycopg.Connection) -> None:
    set_settings(db, {"quiet": {"hours": None}})


def check(db: psycopg.Connection, secrets_key: str, now: datetime) -> ReminderReport:
    async def go() -> ReminderReport:
        async with services(dsn(db), secrets_key, FakeModels(), Clock(now)) as env:
            return await env.reminders.run()

    return asyncio.run(go())


def texts(db: psycopg.Connection) -> list[str]:
    return [text for kind, text, _, _ in notifications(db) if kind == "reminder"]


def sent_rows(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    return db.execute("SELECT kind, fire_at, outcome FROM reminders_sent ORDER BY fire_at").fetchall()


def test_event_reminder_once(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Dentista", rome(2026, 10, 20, 10), location="Via Immaginaria 1")
    add_event(db, "Tutto il giorno", rome(2026, 10, 20), all_day=True)  # all-day: never reminded
    assert check(db, secrets_key, rome(2026, 10, 20, 9, 44) + timedelta(seconds=10)).sent == 0
    report = check(db, secrets_key, rome(2026, 10, 20, 9, 45) + timedelta(seconds=10))
    assert report.sent == 1
    assert texts(db) == ["Tra 15 minuti: Dentista (Via Immaginaria 1)."]
    assert check(db, secrets_key, rome(2026, 10, 20, 9, 46) + timedelta(seconds=10)).sent == 0
    assert check(db, secrets_key, rome(2026, 10, 20, 9, 49)).sent == 0  # each (kind, ref, fire_at) once
    assert sent_rows(db) == [("event", rome(2026, 10, 20, 9, 45), "sent")]
    kind, _, device, via = notifications(db)[0]
    assert (kind, device, via) == ("reminder", None, None)  # every device, by the delivery rule


def test_english_and_custom_lead(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "en-US", "reminders": {"event_lead_min": 30, "task_lead_min": 0}})
    add_event(db, "Dentist", rome(2026, 10, 20, 10), location="Via Immaginaria 1")
    add_task(db, "Call Marco", rome(2026, 10, 20, 10), place="Office")
    check(db, secrets_key, rome(2026, 10, 20, 9, 30))
    check(db, secrets_key, rome(2026, 10, 20, 10, 0))
    assert texts(db) == ["In 30 minutes: Dentist (Via Immaginaria 1).", "Due now: Call Marco (Office)."]


def test_task_reminder(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_task(db, "Pagare la bolletta", rome(2026, 10, 20, 18))
    add_task(db, "Solo data", rome(2026, 10, 20).date())  # due_kind date: no reminder
    add_task(db, "Già fatto", rome(2026, 10, 20, 18), status="done")  # done: none
    check(db, secrets_key, rome(2026, 10, 20, 17, 45))
    for minute in range(46, 60):
        check(db, secrets_key, rome(2026, 10, 20, 17, minute))
    check(db, secrets_key, rome(2026, 10, 20, 23, 59))
    assert texts(db) == ["Scade tra 15 minuti: Pagare la bolletta."]


def test_moved_event_gets_a_new_reminder(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    event = add_event(db, "Dentista", rome(2026, 10, 20, 10))
    check(db, secrets_key, rome(2026, 10, 20, 9, 45))
    add_event(db, "Dentista", rome(2026, 10, 20, 11), event_id=event)  # moved by an hour
    check(db, secrets_key, rome(2026, 10, 20, 10, 44))
    check(db, secrets_key, rome(2026, 10, 20, 10, 45))
    assert texts(db) == ["Tra 15 minuti: Dentista.", "Tra 15 minuti: Dentista."]
    assert [r[1] for r in sent_rows(db)] == [rome(2026, 10, 20, 9, 45), rome(2026, 10, 20, 10, 45)]


def test_moved_task_gets_a_new_reminder(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    task = add_task(db, "Chiamare Marco", rome(2026, 10, 20, 12))
    check(db, secrets_key, rome(2026, 10, 20, 11, 45))
    db.execute("UPDATE tasks SET due_at = %s WHERE id = %s", (rome(2026, 10, 20, 16), task))
    check(db, secrets_key, rome(2026, 10, 20, 15, 45))
    assert len(texts(db)) == 2


def test_cancelled_event_and_done_task_get_none(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    event = add_event(db, "Annullato", rome(2026, 10, 20, 10))
    task = add_task(db, "Fatto prima", rome(2026, 10, 20, 10))
    check(db, secrets_key, rome(2026, 10, 20, 9, 40))
    db.execute("DELETE FROM calendar_events WHERE id = %s", (event,))  # the sync removes cancelled events
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task,))
    for minute in range(41, 50):
        check(db, secrets_key, rome(2026, 10, 20, 9, minute))
    assert texts(db) == [] and sent_rows(db) == []


def test_one_reminder_for_an_event_in_two_calendars(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Riunione", rome(2026, 10, 20, 10), calendar="primary", event_id="shared-1")
    add_event(db, "Riunione", rome(2026, 10, 20, 10), calendar="team", event_id="shared-1")
    check(db, secrets_key, rome(2026, 10, 20, 9, 45))
    assert texts(db) == ["Tra 15 minuti: Riunione."]


def test_catch_up_within_5_minutes(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Dentista", rome(2026, 10, 20, 10))
    check(db, secrets_key, rome(2026, 10, 20, 9, 30))
    # The worker was down from 09:31 to 09:49: the 09:45 reminder is 4 minutes late, and says the truth.
    assert check(db, secrets_key, rome(2026, 10, 20, 9, 49)).sent == 1
    assert texts(db) == ["Tra 11 minuti: Dentista."]


def test_older_misses_are_skipped_and_logged(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Dentista", rome(2026, 10, 20, 10))
    add_task(db, "Chiamare Marco", rome(2026, 10, 20, 10))
    check(db, secrets_key, rome(2026, 10, 20, 9, 30))
    report = check(db, secrets_key, rome(2026, 10, 20, 9, 51))  # 6 minutes late
    assert (report.sent, report.skipped) == (0, 2)
    assert texts(db) == []
    assert [r[2] for r in sent_rows(db)] == ["skipped", "skipped"]
    assert check(db, secrets_key, rome(2026, 10, 20, 9, 52)).skipped == 0  # logged once


def test_first_run_does_not_report_old_misses(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Ieri", rome(2026, 10, 19, 10))
    assert check(db, secrets_key, rome(2026, 10, 20, 9, 0)) == ReminderReport()


@pytest.mark.parametrize(
    ("start_utc", "fire_utc", "label"),
    [
        # 25 Oct 2026, clocks go back at 03:00 CEST → 02:00 CET. An event at 03:10 CET (02:10 UTC) is reminded
        # at 02:55 CET (01:55 UTC), the second 02:55 of the night, not the first (00:55 UTC).
        (datetime(2026, 10, 25, 2, 10, tzinfo=UTC), datetime(2026, 10, 25, 1, 55, tzinfo=UTC), "autumn"),
        # 29 Mar 2026, clocks go forward at 02:00 CET → 03:00 CEST. An event at 03:10 CEST (01:10 UTC) is
        # reminded at 01:55 CET (00:55 UTC): 15 real minutes, across the missing hour.
        (datetime(2026, 3, 29, 1, 10, tzinfo=UTC), datetime(2026, 3, 29, 0, 55, tzinfo=UTC), "spring"),
    ],
)
def test_dst_days(db, secrets_key, start_utc, fire_utc, label) -> None:  # type: ignore[no-untyped-def]
    add_event(db, "Treno", start_utc)
    for minutes in range(-70, -1, 5):  # an hour before, including the repeated or missing local hour
        check(db, secrets_key, fire_utc + timedelta(minutes=minutes))
    assert texts(db) == [], label
    check(db, secrets_key, fire_utc + timedelta(seconds=10))
    assert texts(db) == ["Tra 15 minuti: Treno."], label
    assert sent_rows(db) == [("event", fire_utc, "sent")]


def test_quiet_mode_holds_reminders(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": {"start": "22:30", "end": "07:00"}}})
    add_event(db, "Volo", rome(2026, 10, 20, 23, 30))
    check(db, secrets_key, rome(2026, 10, 20, 23, 15))
    assert [(k, via) for k, _, _, via in notifications(db)] == [("reminder", "held")]


@pytest.mark.parametrize(
    ("kind", "minutes", "lang", "text"),
    [
        ("event", 15, "it-IT", "Tra 15 minuti: Dentista (Via Immaginaria 1)."),
        ("event", 1, "it-IT", "Tra 1 minuto: Dentista (Via Immaginaria 1)."),
        ("event", 0, "it-IT", "Ora: Dentista (Via Immaginaria 1)."),
        ("task", 15, "it-IT", "Scade tra 15 minuti: Dentista (Via Immaginaria 1)."),
        ("task", 0, "it-IT", "Scade ora: Dentista (Via Immaginaria 1)."),
        ("event", 15, "en-US", "In 15 minutes: Dentista (Via Immaginaria 1)."),
        ("event", 1, "en-US", "In 1 minute: Dentista (Via Immaginaria 1)."),
        ("task", 15, "en-US", "Due in 15 minutes: Dentista (Via Immaginaria 1)."),
        ("task", 0, "en-US", "Due now: Dentista (Via Immaginaria 1)."),
    ],
)
def test_templates(kind: Any, minutes: int, lang: str, text: str) -> None:
    assert reminder_text(kind, "Dentista", "Via Immaginaria 1", minutes, lang) == text
