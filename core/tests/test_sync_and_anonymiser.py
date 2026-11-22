"""Queued fixes: email results as a data block (in test_conversation_tools), the revoked
re-check, independent prunes, invalid_client, one agenda entry per event id, date-time formats, and the
anonymiser for real Google fixtures."""

import base64
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from pisa.services.google_sync import sync_gmail
from pisa.workers.main import SCHEDULES
from pisa_tools.google_capture import anonymize
from tests.conftest import PROTOCOL_DIR, pair
from tests.gateway_env import run
from tests.google_fake import FakeGoogle, load
from tests.test_google import T0, google_env
from tests.test_tasks import add, event


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def test_invalid_client_asks_to_reenter_the_credentials(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    fake = FakeGoogle()
    fake.refresh_status, fake.refresh_error_file = 401, "token_invalid_client.json"

    async def go() -> Any:
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            await sync_gmail(pool, google, now=T0)
            return await google.connectors()

    connector = run(go)[0]
    assert connector["status"] == "error"
    assert connector["message"] == (
        "Google rejected Pisa's client credentials (invalid_client): re-enter the client credentials in "
        "Settings → Connectors → Google, then reconnect."
    )


def test_an_event_in_two_calendars_is_listed_once(client, auth, clock, db) -> None:  # type: ignore[no-untyped-def]
    clock.now = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    event(db, "same-id", "Riunione", "2026-10-20T15:00:00+02:00", "2026-10-20T16:00:00+02:00")
    db.execute("INSERT INTO calendars (id, name) VALUES ('shared', 'Lavoro')")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES ('shared', 'same-id', 'Riunione', '2026-10-20T15:00:00+02:00', '2026-10-20T16:00:00+02:00', "
        "false, 'confirmed')"
    )
    add(client, auth, title="Unrelated task")
    events = client.get("/v1/agenda?days=1", headers=auth).json()["days"][0]["events"]
    assert [e["id"] for e in events] == ["same-id"]


def test_prunes_are_independent_schedules() -> None:
    names = {s.name for s in SCHEDULES}
    # Their locations and mail are kept; their prunes are gone (and removed from a deployed worker)
    assert not {"prune-locations", "prune-email", "daily-prune"} & names


def test_openapi_marks_timestamps_as_date_time() -> None:
    spec = json.loads((PROTOCOL_DIR / "openapi.json").read_text())
    event = spec["components"]["schemas"]["AgendaEvent"]["properties"]
    assert event["start"]["format"] == "date-time" and event["end"]["format"] == "date-time"
    task = spec["components"]["schemas"]["Task"]["properties"]
    assert task["created_at"]["format"] == "date-time"


def test_anonymizer_keeps_shape_and_drops_personal_data() -> None:
    message = anonymize(load("gmail_message_parcel.json"))
    text = json.dumps(message, ensure_ascii=False)
    for personal in (
        "Poste Italiane",
        "noreply@posteitaliane.example",
        "pacco",
        "Gentile",
        "owner@example.org",
    ):
        assert personal not in text
    assert message["id"] == "m-parcel" and message["labelIds"] == ["INBOX", "CATEGORY_UPDATES", "UNREAD"]
    headers = {h["name"]: h["value"] for h in message["payload"]["headers"]}
    assert headers["From"] == "Sender <sender@example.com>" and headers["Subject"] == "Subject redacted"
    body = message["payload"]["parts"][0]["body"]["data"]
    assert base64.urlsafe_b64decode(body + "==") == b"Body redacted."
    tokens = anonymize(load("token_exchange.json"))
    assert tokens["access_token"] == tokens["refresh_token"] == tokens["id_token"] == "redacted"
    events = json.dumps(anonymize(load("calendar_events_full.json")), ensure_ascii=False)
    assert "Dentista" not in events and "Via dei Sogni" not in events and "ev-dentist" in events


def test_a_device_revoked_during_the_handshake_is_closed(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from starlette.websockets import WebSocketDisconnect

    from pisa.services import devices
    from tests.live_app import connect, hello

    device_id, token = pair(db)
    real = devices.authenticate

    async def check_then_revoked(conn, token_value):  # type: ignore[no-untyped-def]
        device = await real(conn, token_value)
        # `pisa devices revoke` lands right after the token check, before the session registers.
        await conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (device.id,))
        return device

    monkeypatch.setattr("pisa.api.live.authenticate", check_then_revoked)
    with connect(client, token) as ws:
        ws.send_json(hello(device_id))
        err = ws.receive_json()
        assert err["control"] == "error" and err["code"] == "device_revoked"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4003
