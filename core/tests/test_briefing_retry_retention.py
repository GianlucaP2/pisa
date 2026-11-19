"""Queued fixes: the BRIEFING try-lock (F5), usage and the first answer on a retry timeout
(F8), 90-day retention (F10), the fixture anonymiser and the 400 (F11), the two unique indexes (F12),
background delivery with apns-collapse-id, the reworded quiet summary, and the one per-minute job."""

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from pisa.connectors.google.errors import ConnectorError
from pisa.services import maintenance, worker_steps
from pisa.services.notifications import release_held
from pisa_tools.google_capture import anonymize, capture
from tests.assistant_env import TZ, add_event, notifications, rome, services, set_settings
from tests.conftest import Clock, FakeModels, dsn, pair
from tests.owner import owner_at
from tests.support.minute import run_minute
from tests.test_push import TOKEN, deliver, with_token

# --- F5: try, never wait ----------------------------------------------------------------------------------


def test_a_trigger_that_loses_the_race_returns_at_once(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.infra import locks

    device = uuid.UUID(pair(db)[0])
    add_event(db, "Dentista", rome(2026, 10, 20, 9))
    db.execute("SELECT pg_advisory_lock(%s)", (locks.BRIEFING,))  # another trigger is composing
    try:

        async def go() -> Any:
            async with services(dsn(db), secrets_key, FakeModels(), Clock(rome(2026, 10, 20, 8))) as env:
                return await asyncio.wait_for(env.briefings.on_presence(device, "glasses_on"), 2)

        assert asyncio.run(go()) is None
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (locks.BRIEFING,))
    assert db.execute("SELECT count(*) FROM briefings").fetchone() == (0,)


# --- F8: a retry that runs out of time --------------------------------------------------------------------


def test_retry_timeout_keeps_the_trimmed_first_answer_and_its_usage(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"max_words": 20}})
    add_event(db, "Dentista", rome(2026, 10, 20, 9))
    fake = FakeModels()
    first = "Alle 9 hai il dentista in Via Immaginaria. " * 5  # 40 words: over the limit, so a retry is asked

    async def answer(messages: Any, info: Any) -> str:
        if len(fake.seen_messages) > 1:
            await asyncio.sleep(5)  # the shorter-answer retry never makes the deadline (2 s: CI's load)
        return first

    fake.default = answer

    async def go() -> str:
        async with services(
            dsn(db), secrets_key, fake, Clock(rome(2026, 10, 20, 7, 30)), deadline_s=2.0
        ) as e:
            return (await e.briefings.ask()).text

    assert (
        asyncio.run(go())
        == "Alle 9 hai il dentista in Via Immaginaria. Alle 9 hai il dentista in Via Immaginaria."
    )
    status, output_tokens = db.execute("SELECT status, output_tokens FROM llm_calls").fetchone()  # type: ignore[misc]
    assert status == "settled" and output_tokens > 0  # the first answer's tokens are on the ledger


# --- F10: retention --------------------------------------------------------------------------------------


def test_history_is_kept_until_he_deletes_it(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    now = rome(2026, 10, 21, 3, 30)
    old, recent = now - timedelta(days=91), now - timedelta(days=89)
    for i, at in enumerate((old, recent)):
        notification = uuid.uuid4()
        db.execute(
            "INSERT INTO notifications (id, kind, text, lang, priority, created_at, queued_at, "
            "delivered_via) "
            "VALUES (%s, 'briefing', 'x', 'it-IT', 'normal', %s, %s, 'ws')",
            (notification, at, at),
        )
        db.execute(
            "INSERT INTO briefings (id, day, trigger, text, lang, created_at, notification_id) "
            "VALUES (%s, %s, 'glasses_on', 'x', 'it-IT', %s, %s)",
            (uuid.uuid4(), at.date(), at, notification),
        )
        db.execute(
            "INSERT INTO reminders_sent (kind, ref, fire_at, outcome, created_at) VALUES ('event', "
            "%s, %s, 'sent', %s)",
            (f"e{i}", at, at),
        )
    counts = asyncio.run(maintenance.prune_history(app_settings, now))
    assert counts == {  # Only the closed list and their own setting
        "public_cache": 0, "deleted_tasks": 0, "pair_tokens": 0,
        "pisa_curious_sources": 0, "messages": 0, "messages_kept_cited": 0, "talk_timelines": 0,
        "research_findings": 0, "turns": 0, "drawings": 0,
    }  # fmt: skip
    for table in ("briefings", "notifications", "reminders_sent"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (2,)


# --- F11: the fixture anonymiser and a refused call -------------------------------------------------------


def test_anonymiser_drops_meeting_links_and_redacts_filenames() -> None:
    event = {
        "id": "e1",
        "hangoutLink": "https://meet.google.com/abc",
        "htmlLink": "https://www.google.com/calendar/event?eid=x",
        "conferenceData": {"entryPoints": [{"uri": "https://meet.google.com/abc"}]},
        "extendedProperties": {"private": {"x": "y"}},
        "attachments": [{"filename": "contratto-mario-rossi.pdf", "mimeType": "application/pdf"}],
        "start": {"dateTime": "2026-10-20T09:00:00+02:00"},
    }
    out = anonymize(event)
    assert set(out) == {"id", "attachments", "start"}
    assert out["attachments"][0] == {"filename": "redacted (25 characters)", "mimeType": "application/pdf"}
    part = anonymize({"filename": "busta-paga.pdf", "mimeType": "application/pdf", "body": {"size": 1}})
    assert part["filename"] == "redacted (14 characters)"


class _CaptureGoogle:
    """The capture's view of Google: answers every call, refuses the garbage syncToken with a 400."""

    def api(self, service: str) -> Any:
        async def get(url: str, params: Any = None) -> httpx.Response:
            if isinstance(params, dict) and params.get("syncToken") == "invalid-token":
                raise ConnectorError("The Google Calendar call failed (HTTP 400: Invalid sync token).", 400)
            if url.endswith("/calendarList"):
                return httpx.Response(200, json={"items": [{"id": "owner@example.com", "primary": True}]})
            if url.endswith("/profile"):
                return httpx.Response(200, json={"historyId": "500"})
            return httpx.Response(200, json={})

        return type("Api", (), {"get": staticmethod(get)})()


def test_a_refused_call_is_saved_and_the_capture_goes_on(tmp_path: Path) -> None:
    written = asyncio.run(capture(_CaptureGoogle(), tmp_path))  # type: ignore[arg-type]
    assert "calendar_gone.json" in written and written[-1] == "calendar_gone.json"
    assert json.loads((tmp_path / "calendar_gone.json").read_text()) == {
        "_status": 400,
        "_error": "The Google Calendar call failed (HTTP 400: Invalid sync token).",
    }


# --- F12: unique indexes ---------------------------------------------------------------------------------


def test_one_time_briefing_per_day(db) -> None:  # type: ignore[no-untyped-def]
    insert = (
        "INSERT INTO briefings (id, day, trigger, text, lang, created_at) VALUES (%s, '2026-10-20', %s, 'x', "
        "'it-IT', now())"
    )
    db.execute(insert, (uuid.uuid4(), "time"))
    db.execute(insert, (uuid.uuid4(), "glasses_on"))  # other triggers are not limited by the index
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, (uuid.uuid4(), "time"))


def test_a_token_belongs_to_one_live_device(client, db) -> None:  # type: ignore[no-untyped-def]
    old_id, old_token = pair(db, "iPhone (old pairing)")
    new_id, new_token = pair(db, "iPhone")
    for token in (old_token, new_token):
        client.put(
            "/v1/devices/me/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"mode": "direct", "token": TOKEN, "environment": "production"},
        )
    rows = dict(db.execute("SELECT id::text, apns_token FROM devices").fetchall())
    assert rows == {old_id: None, new_id: TOKEN}
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("UPDATE devices SET apns_token = %s WHERE id = %s", (TOKEN, old_id))


# --- background delivery ---------------------------------------------------------------------------------


def test_a_backgrounded_app_also_gets_the_push(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    with_token(db, device_id)
    db.execute(
        "INSERT INTO device_presence (device_id, app, glasses, updated_at) VALUES (%s, 'background', "
        "NULL, now())",
        (device_id,),
    )
    via, live, apple = deliver(db, secrets_key, online={device_id})
    assert via == "ws" and len(live.sent) == 1  # the socket is still tried
    (request,) = apple.requests
    notification_id = db.execute("SELECT id::text FROM notifications").fetchone()[0]  # type: ignore[index]
    assert request.headers["apns-collapse-id"] == notification_id == request.headers["apns-id"]


def test_a_foreground_app_gets_only_the_notify(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    with_token(db, device_id)
    db.execute(
        "INSERT INTO device_presence (device_id, app, glasses, updated_at) VALUES (%s, 'foreground', "
        "'on', now())",
        (device_id,),
    )
    via, _, apple = deliver(db, secrets_key, online={device_id})
    assert via == "ws" and apple.requests == []


# --- the quiet summary -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "summary"),
    [
        ("it-IT", "Mentre eri in modalità silenziosa: Volo alle 23:30. Pagare la bolletta entro le 23:45."),
        ("en-US", "While you were in quiet mode: Volo at 23:30. Pagare la bolletta by 23:45."),
    ],
)
def test_held_reminders_are_summarised_by_title_and_time(db, secrets_key, lang, summary) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task

    set_settings(db, {"language": lang})
    add_event(db, "Volo", rome(2026, 10, 20, 23, 30))
    add_task(db, "Pagare la bolletta", rome(2026, 10, 20, 23, 45))

    async def go() -> None:
        for minute in (15, 30):
            async with services(
                dsn(db), secrets_key, FakeModels(), Clock(rome(2026, 10, 20, 23, minute))
            ) as e:
                await e.reminders.run()
        async with services(dsn(db), secrets_key, FakeModels(), Clock(rome(2026, 10, 21, 7))) as e:
            await release_held(e.pool, now=rome(2026, 10, 21, 7), owner=owner_at(TZ))

    asyncio.run(go())
    assert notifications(db)[-1][:2] == ("info", summary)


# --- one per-minute job ----------------------------------------------------------------------------------


def test_every_minute_runs_each_step_even_when_one_fails(monkeypatch, app_settings) -> None:  # type: ignore[no-untyped-def]
    ran: list[str] = []

    async def broken() -> None:
        ran.append("briefing_time")
        raise RuntimeError("boom")

    async def reminders() -> None:
        ran.append("reminders")

    async def tick() -> None:
        ran.append("plan")

    async def done() -> None:
        ran.append("done")

    async def check_ins() -> None:
        ran.append("check_ins")

    async def attend() -> None:
        ran.append("attend")

    async def aging() -> None:
        ran.append("aging")

    async def floors() -> None:
        ran.append("attend_floors")

    async def live() -> None:
        ran.append("live_activity")

    async def lapsing() -> None:
        ran.append("approvals_lapsing")

    async def expire() -> None:
        ran.append("approvals_expire")

    async def weekly() -> None:
        ran.append("weekly_check")

    async def learn() -> dict[str, int]:  # The night is a minute step now (daily from 03:30)
        ran.append("learn")
        return {}

    async def night() -> None:
        return None

    async def handoffs() -> None:
        ran.append("handoffs")

    async def money_back() -> None:
        ran.append("money_back")

    async def leaving() -> int:
        ran.append("trip_leaving")
        return 0

    async def people() -> None:
        ran.append("people")

    async def errands() -> None:
        ran.append("errands")

    async def guardian() -> None:
        ran.append("guardian")

    async def public_data() -> None:
        ran.append("public_data")

    async def rituals() -> None:
        ran.append("rituals")

    async def sunday() -> None:
        ran.append("sunday_card")

    async def vault() -> None:
        ran.append("vault_reminders")

    async def starters() -> None:
        ran.append("vault_starters")

    async def skills() -> None:
        ran.append("skills")

    async def undos() -> None:
        ran.append("personal_evals")

    async def owner_loop() -> None:  # the user loop: the week's report and the scenario candidates
        ran.append("owner_loop")

    async def running() -> bool:  # Not stopped
        return False

    async def release(pool: Any, *, now: Any, owner: Any) -> int:
        ran.append("quiet_release")
        return 0

    runtime = type(
        "R",
        (),
        {
            "briefings": type("B", (), {"on_time": staticmethod(broken)})(),
            "reminders": type("M", (), {"run": staticmethod(reminders)})(),
            "planner": type("P", (), {"tick": staticmethod(tick)})(),
            "done": type("D", (), {"tick": staticmethod(done)})(),
            "follow": type("F", (), {"tick": staticmethod(check_ins), "aging": staticmethod(aging)})(),
            "attend": type("T", (), {"tick": staticmethod(attend), "floors": staticmethod(floors)})(),
            "live_activity": type("L", (), {"tick": staticmethod(live)})(),
            "approvals": type("A", (), {"expire": staticmethod(expire), "lapsing": staticmethod(lapsing)})(),
            "learning": type(
                "W", (), {"weekly_tick": staticmethod(weekly), "nightly": staticmethod(learn)}
            )(),
            "night": type("N", (), {"run": staticmethod(night)})(),
            "handoffs": type("H", (), {"tick": staticmethod(handoffs)})(),
            "money_back": type("K", (), {"tick": staticmethod(money_back)})(),
            "trips": type("V", (), {"leaving": staticmethod(leaving)})(),
            "people": type("Q", (), {"tick": staticmethod(people)})(),
            "errands": type("Q", (), {"tick": staticmethod(errands)})(),
            "guardian": type("G", (), {"tick": staticmethod(guardian)})(),
            "public": type("Q", (), {"tick": staticmethod(public_data)})(),
            "rituals": type("X", (), {"tick": staticmethod(rituals)})(),
            "autopilot": type(
                "X",
                (),
                {
                    "stopped": staticmethod(running),
                    "ledger": type("G", (), {"sunday_tick": staticmethod(sunday)})(),
                    "vault": type(
                        "V", (), {"tick": staticmethod(vault), "starters": staticmethod(starters)}
                    )(),
                    "skills": type("S", (), {"tick": staticmethod(skills)})(),
                    "evals": type("E", (), {"scan_undos": staticmethod(undos)})(),
                    "owner_loop": type("O", (), {"tick": staticmethod(owner_loop)})(),
                },
            )(),
            "pool": None,
            "clock": staticmethod(lambda: rome(2026, 10, 26, 8)),  # Monday: the periodic steps are due too
            "settings": app_settings,
            "owner": owner_at(app_settings.timezone),
        },
    )()

    async def load() -> None:
        return None

    def minute() -> None:  # The three groups through the worker's loop, each step under its lock and limit
        from psycopg_pool import AsyncConnectionPool

        loop = asyncio.new_event_loop()
        pool = AsyncConnectionPool(app_settings.database_url, open=False, kwargs={"autocommit": True})
        loop.run_until_complete(pool.open(wait=True))
        fake: Any = runtime
        fake.pool, fake.zone = pool, type("Z", (), {"load": staticmethod(load)})()
        try:
            run_minute(monkeypatch, lambda fn, *a: loop.run_until_complete(fn(*a)), fake)
        finally:
            loop.run_until_complete(pool.close())
            loop.close()

    monkeypatch.setattr(worker_steps, "release_held", release)
    minute()
    assert ran == [
        "briefing_time", "reminders", "attend_floors", "approvals_lapsing", "approvals_expire",
        "quiet_release", "guardian", "weekly_check", "owner_loop", "sunday_card",
        "done", "plan", "check_ins", "aging", "attend", "live_activity",
        "handoffs", "money_back", "vault_reminders", "vault_starters", "skills", "personal_evals",
        "rituals", "people", "errands", "public_data", "trip_leaving", "learn",
    ]  # fmt: skip
