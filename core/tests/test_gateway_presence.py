"""The device session's part in "Pisa speaks first", on `/v2/live`: presence is stored and only a change
triggers a briefing (delivered as a notice on the live socket), the state resets when the socket closes, and a
briefing phrase is answered with the briefing as the turn's reply, which `cancel` stops."""

import asyncio
import time
import uuid
from typing import Any

import psycopg
import pytest

from pisa.brain.briefing import EMPTY_DAY
from pisa.services import presence
from tests.assistant_env import add_event, rome, set_settings
from tests.conftest import FakeModels, dsn, pair, set_key
from tests.live_app import live


@pytest.fixture
def device(db: psycopg.Connection) -> tuple[str, str]:
    set_settings(db, {"briefing": {"window": None}})  # the client's clock is 13:00 in Rome
    return pair(db)


def presence_row(db: psycopg.Connection, device_id: str) -> Any:
    return db.execute(
        "SELECT app, glasses FROM device_presence WHERE device_id = %s", (device_id,)
    ).fetchone()


def wait_for(check: Any, timeout: float = 60.0) -> Any:
    """`check()` once it is true. The timeout only bounds a hang: every caller waits for something that comes
    (5 s ran out on a loaded machine)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return check()


def test_app_open_gives_a_briefing_notify_once(client, device, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with live(client, token, device_id) as app:
        app.custom("pisa.presence", {"event": "app_foreground"})
        notify = app.next()
        assert notify["type"] == "notify" and notify["kind"] == "briefing"
        assert notify["text"] == EMPTY_DAY["it-IT"] and notify["lang"] == "it-IT"
        app.custom("pisa.presence", {"event": "glasses_on"})  # a second trigger: the repeat rule says no
        assert app.ping() == []
        app.check()
    delivered = "SELECT delivered_via FROM notifications WHERE delivered_via IS NOT NULL"
    assert wait_for(lambda: db.execute(delivered).fetchall()) == [("ws",)]  # not the row before it's marked
    assert db.execute("SELECT trigger FROM briefings").fetchall() == [("app_open",)]


def test_presence_is_stored_and_reset_when_the_socket_closes(client, device, db) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"glasses_on": False, "app_open": False}}})
    device_id, token = device
    with live(client, token, device_id) as app:
        for event in ("app_foreground", "glasses_on"):
            app.custom("pisa.presence", {"event": event})
        assert app.ping() == []  # Pisa never replies to presence
        assert presence_row(db, device_id) == ("foreground", "on")
    assert wait_for(lambda: presence_row(db, device_id) == ("background", None)), presence_row(db, device_id)


def test_a_new_session_starts_from_a_reset_state(client, device, db) -> None:  # type: ignore[no-untyped-def]
    # After a crash (no clean close) the stored state still says foreground / glasses on.
    device_id, token = device
    db.execute(
        "INSERT INTO device_presence (device_id, app, glasses, updated_at) "
        "VALUES (%s, 'foreground', 'on', now())",
        (device_id,),
    )
    with live(client, token, device_id) as app:
        assert presence_row(db, device_id) == ("background", None)
        app.custom("pisa.presence", {"event": "app_foreground"})
        notify = app.next()
        assert notify["type"] == "notify" and notify["kind"] == "briefing"


def test_change_detection(db) -> None:  # type: ignore[no-untyped-def]
    device_id = uuid.UUID(pair(db)[0])
    now = rome(2026, 10, 20, 8)

    async def go() -> list[Any]:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            out = [await presence.update(conn, device_id, e, now) for e in (
                "app_foreground", "app_foreground", "glasses_on", "glasses_on", "glasses_off", "glasses_on",
                "app_background",
            )]  # fmt: skip
            await presence.reset(conn, device_id, now)  # the socket closed (app killed)
            out.append(await presence.update(conn, device_id, "app_foreground", now))
            out.append(
                await presence.update(conn, device_id, "glasses_off", now)
            )  # unknown → off: no trigger
            out.append(await presence.update(conn, device_id, "companion_on", now))  # ignored for now
            return out

    assert asyncio.run(go()) == [
        "app_open",
        None,  # the same state again
        "glasses_on",
        None,
        None,
        "glasses_on",
        None,
        "app_open",  # reopening after the reset counts
        None,
        None,
    ]


def test_new_session_keeps_its_state_when_the_old_one_closes(client, device, db) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"glasses_on": False, "app_open": False}}})
    device_id, token = device
    with live(client, token, device_id):
        with live(client, token, device_id) as new:
            new.custom("pisa.presence", {"event": "app_foreground"})
            assert new.ping() == []
            time.sleep(0.2)  # the replaced session's cleanup has run
            assert presence_row(db, device_id) == ("foreground", None)


def test_phrase_is_answered_with_the_briefing(client, device, db, fake_models: FakeModels) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with live(client, token, device_id) as app:
        turn = app.say("Buongiorno, Pisa!", lang="en-US", source="glasses_mic")
        chunks = app.reply()
        app.check()
    assert all(c["type"] == "say" and c["reply_to"] == turn["messages"][0]["id"] for c in chunks)
    assert "".join(c["delta"] for c in chunks) == EMPTY_DAY["it-IT"]
    assert {c["lang"] for c in chunks} == {"it-IT"}  # Pisa's language for briefings (settings.language)
    assert fake_models.calls == []  # an empty day needs no model; the agent was never asked
    assert db.execute("SELECT trigger FROM briefings").fetchall() == [("phrase",)]
    assert db.execute("SELECT role, content FROM messages ORDER BY id").fetchall() == [
        ("user", "Buongiorno, Pisa!"),
        ("assistant", EMPTY_DAY["it-IT"]),
    ]
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)


def test_phrase_trigger_off_goes_to_the_agent(client, device, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"triggers": {"phrase": False}}})
    set_key(db, secrets_key)
    fake_models.default = "Buongiorno a te."
    device_id, token = device
    with live(client, token, device_id) as app:
        app.say("Buongiorno Pisa", source="glasses_mic")
        text = "".join(c["delta"] for c in app.reply())
    assert text.strip() == "Buongiorno a te."
    assert db.execute("SELECT count(*) FROM briefings").fetchone() == (0,)


def test_cancel_stops_the_phrase_briefing(client, device, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    set_key(db, secrets_key)
    add_event(db, "Riunione", rome(2026, 10, 20, 15))  # something to say: the model is asked

    async def slow(messages, info):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)
        return "Mai."

    fake_models.default = slow
    device_id, token = device
    with live(client, token, device_id) as app:
        turn = app.say("Buongiorno Pisa", source="glasses_mic")
        started = "SELECT 1 FROM llm_calls WHERE status = 'reserved' AND slot <> 'voice.speak'"
        assert wait_for(lambda: db.execute(started).fetchone())  # cancel during the model call, not the plan
        app.cancel(turn["runId"])
        app.run_ends(turn["runId"])
        assert app.ping() == []
    assert db.execute("SELECT count(*) FROM briefings").fetchone() == (0,)

    def settled() -> Any:
        row = db.execute("SELECT status, error_code FROM llm_calls WHERE slot <> 'voice.speak'").fetchone()
        return row if row and row[0] != "reserved" else None

    assert wait_for(settled) == ("settled", "cancelled")
