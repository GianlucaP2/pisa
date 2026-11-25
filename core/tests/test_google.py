"""Google connector (contract A): credentials, the server auth code exchange, refresh and the 7-day
reconnect, disconnect; Gmail and Calendar sync against recorded-shape API responses."""

import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, LiteralString

import httpx
import psycopg
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from pisa.db.pool import open_pool
from pisa.infra.keys import KeyStore
from pisa.services.google import GoogleService
from pisa.services.google_sync import calendar_window, sync_calendar, sync_gmail
from tests.conftest import Clock, dsn, pair
from tests.gateway_env import run
from tests.google_fake import (
    ACCESS_TOKEN,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    SERVER_AUTH_CODE,
    USED_CODE,
    WEB_CLIENT_ID,
    FakeGoogle,
)
from tests.owner import owner_at
from tests.support.fake_secrets import google_refresh_token

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)  # 12:00 in Rome


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fake(http_handler: dict[str, object]) -> FakeGoogle:
    google = FakeGoogle()
    http_handler["handler"] = google
    return google


def wait_for(db: psycopg.Connection, query: LiteralString, expected: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db.execute(query).fetchone() == expected:
            return
        time.sleep(0.05)
    assert db.execute(query).fetchone() == expected


CREDENTIALS = {"web_client_id": WEB_CLIENT_ID, "web_client_secret": CLIENT_SECRET}
REDIRECT = "https://pisa.example.ts.net/oauth/google/callback"  # the user's own server (a tailnet name)
OLDER_REFRESH_TOKEN = google_refresh_token(
    "older-refresh-token"
)  # a pending account's, from an earlier start


def configure(client, auth) -> None:  # type: ignore[no-untyped-def]
    client.app.state.runtime.google.redirect_uri = REDIRECT
    put = client.put("/v1/connectors/google/credentials", headers=auth, json=CREDENTIALS)
    assert put.status_code == 204, put.json()


def start(client, auth) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    started = client.post("/v1/connectors/google/start", headers=auth)
    assert started.status_code == 200, started.json()
    return started.json()


def state_of(auth_url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(auth_url).query)["state"][0]


def sign_in(client, auth, code: str = SERVER_AUTH_CODE) -> Any:  # type: ignore[no-untyped-def]
    """The phone starts, the browser consents at Google, Google comes back to this server with the code."""
    state = state_of(start(client, auth)["auth_url"])
    return client.get("/oauth/google/callback", params={"state": state, "code": code}, follow_redirects=False)


def connect_google(client, auth) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Sign in, then the starting device's yes to the account Google came back with."""
    page = sign_in(client, auth)
    assert page.status_code == 200 and "status=confirm" in page.text, page.text
    confirmed = client.post("/v1/connectors/google/confirm", headers=auth)
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_credentials_are_validated_and_write_only(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    bad = client.put(
        "/v1/connectors/google/credentials", headers=auth, json=CREDENTIALS | {"web_client_id": "x"}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["message"] == (
        "That is not a Google client ID: it should end with .apps.googleusercontent.com."
    )
    # the iPhone's shared Sign-In client is gone: a body naming it is refused (D-122)
    old = client.put(
        "/v1/connectors/google/credentials", headers=auth, json=CREDENTIALS | {"ios_client_id": WEB_CLIENT_ID}
    )
    assert old.status_code in (400, 422)
    configure(client, auth)
    listed = client.get("/v1/connectors", headers=auth)
    assert listed.json()["connectors"][0]["google"] == {  # The registry's Google entry
        "id": "google",
        "name": "Google (Gmail + Calendar)",
        "configured": True,
        "status": "not_connected",
        "account": None,
        "scopes": [],
        "last_sync": {"gmail": None, "calendar": None},
        "message": None,
        "client": {"web_client_id": WEB_CLIENT_ID},
        "redirect_uri": REDIRECT,
        "pending_account": None,
        "pending_started_here": False,
        "requested_scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.app.created",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
        "missing_scopes": [  # not connected: everything
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.app.created",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
    }
    assert CLIENT_SECRET not in listed.text
    stored = db.execute("SELECT web_client_secret_enc FROM connector_credentials").fetchone()
    assert stored is not None and CLIENT_SECRET.encode() not in bytes(stored[0])


def test_start_before_configuration_is_409(client, auth, fake) -> None:  # type: ignore[no-untyped-def]
    client.app.state.runtime.google.redirect_uri = REDIRECT
    response = client.post("/v1/connectors/google/start", headers=auth)
    assert response.status_code == 409 and response.json()["error"]["code"] == "connector_not_configured"


def test_without_an_https_address_there_is_no_sign_in(client, auth, fake) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    client.app.state.runtime.google.redirect_uri = None
    response = client.post("/v1/connectors/google/start", headers=auth)
    assert response.status_code == 409 and "https address" in response.json()["error"]["message"]
    assert client.get("/v1/connectors/google", headers=auth).json()["redirect_uri"] is None


def test_the_consent_page_is_the_user_s_own_client_with_pkce(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import parse_qs, urlsplit

    configure(client, auth)
    started = start(client, auth)
    url = urlsplit(started["auth_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert query["client_id"] == WEB_CLIENT_ID and query["redirect_uri"] == REDIRECT
    assert query["response_type"] == "code" and query["access_type"] == "offline"
    assert query["prompt"] == "consent" and query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43 and len(query["state"]) >= 40
    assert (
        query["scope"].split() == client.get("/v1/connectors/google", headers=auth).json()["requested_scopes"]
    )
    assert started["redirect_uri"] == REDIRECT and CLIENT_SECRET not in started["auth_url"]
    # the state is kept by its hash only, the verifier sealed
    row = db.execute("SELECT state_hash, verifier_enc FROM google_oauth_states").fetchone()
    assert row is not None and row[0] != query["state"] and len(row[0]) == 64


def test_the_callback_connects_and_first_sync_runs(client, auth, fake, db, capsys) -> None:  # type: ignore[no-untyped-def]
    with capture_logs() as logs:
        configure(client, auth)
        started = start(client, auth)
        state = state_of(started["auth_url"])
        page = client.get(
            "/oauth/google/callback",
            params={"state": state, "code": SERVER_AUTH_CODE},
            follow_redirects=False,
        )
        assert page.status_code == 200, page.text
        assert "pisa://google-connected?status=confirm" in page.text  # the device says it is theirs first
        pending = client.get("/v1/connectors/google", headers=auth).json()
        assert pending["pending_account"] == "owner@example.org" and pending["status"] == "not_connected"
        assert db.execute("SELECT count(*) FROM connector_accounts").fetchone() == (0,)  # nothing yet
        assert client.post("/v1/connectors/google/confirm", headers=auth).status_code == 200
        wait_for(db, "SELECT count(*) FROM email_messages", (3,))
        wait_for(db, "SELECT calendar_synced_at IS NOT NULL FROM connector_accounts", (True,))
        connector = client.get("/v1/connectors/google", headers=auth).json()

    assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
    assert connector["status"] == "connected" and connector["account"] == "owner@example.org"
    actions = {
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.compose",
    }
    assert set(connector["missing_scopes"]) == actions and connector["message"] is None
    assert connector["last_sync"]["gmail"] and connector["last_sync"]["calendar"]
    exchange = fake.forms[0]
    verifier = exchange.pop("code_verifier")[0]
    assert 43 <= len(verifier) <= 128  # RFC 7636
    assert exchange == {
        "grant_type": ["authorization_code"],
        "code": [SERVER_AUTH_CODE],
        "client_id": [WEB_CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
        "redirect_uri": [REDIRECT],
    }
    row = db.execute("SELECT refresh_token_enc, access_token_enc FROM connector_accounts").fetchone()
    assert row is not None and REFRESH_TOKEN.encode() not in bytes(row[0]) and b"ya29" not in bytes(row[1])
    everything = json.dumps(logs, default=str) + capsys.readouterr().out + page.text + json.dumps(started)
    for secret in (
        CLIENT_SECRET,
        REFRESH_TOKEN,
        ACCESS_TOKEN,
        SERVER_AUTH_CODE,
        state,
        verifier,
    ):
        assert secret not in everything.replace(
            started["auth_url"], ""
        )  # the URL carries the state, as it must
    assert SERVER_AUTH_CODE not in page.text and "ya29" not in page.text


def test_a_state_is_single_use_and_expires(client, auth, fake, db, clock) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    state = state_of(start(client, auth)["auth_url"])
    first = client.get("/oauth/google/callback", params={"state": state, "code": SERVER_AUTH_CODE})
    assert first.status_code == 200
    again = client.get("/oauth/google/callback", params={"state": state, "code": SERVER_AUTH_CODE})
    assert again.status_code == 400 and "reason=state_expired" in again.text  # a replay does nothing
    late = state_of(start(client, auth)["auth_url"])
    clock.now = clock.now + timedelta(minutes=11)
    expired = client.get("/oauth/google/callback", params={"state": late, "code": SERVER_AUTH_CODE})
    assert expired.status_code == 400 and "reason=state_expired" in expired.text
    unknown = client.get("/oauth/google/callback", params={"state": "made-up", "code": SERVER_AUTH_CODE})
    assert unknown.status_code == 400 and "reason=state_expired" in unknown.text
    assert len([f for f in fake.forms if f.get("grant_type") == ["authorization_code"]]) == 1


def test_a_newer_start_on_the_device_refuses_the_older_state(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    older = state_of(start(client, auth)["auth_url"])
    newer = state_of(start(client, auth)["auth_url"])
    stale = client.get("/oauth/google/callback", params={"state": older, "code": SERVER_AUTH_CODE})
    assert stale.status_code == 400 and "reason=state_expired" in stale.text
    assert (
        client.get("/oauth/google/callback", params={"state": newer, "code": SERVER_AUTH_CODE}).status_code
        == 200
    )


def test_another_account_waits_for_the_starting_device_and_only_it(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    """E1 G-1: a leaked sign-in link finished with someone else's account connects nothing and deletes
    nothing until the device that started it says yes; another device's yes is refused."""
    configure(client, auth)
    assert "status=confirm" in sign_in(client, auth).text
    _, other_token = pair(db, "iPad")
    other = {"Authorization": f"Bearer {other_token}"}
    refused = client.post("/v1/connectors/google/confirm", headers=other)
    assert refused.status_code == 404
    assert db.execute("SELECT count(*) FROM connector_accounts").fetchone() == (0,)
    assert client.post("/v1/connectors/google/cancel", headers=other).status_code == 404
    assert client.get("/v1/connectors/google", headers=auth).json()["pending_account"] == "owner@example.org"


def test_only_the_starting_device_is_told_the_sign_in_is_its_own(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    """`pending_started_here`: true on the device that started the waiting sign-in (it shows yes/no), false on
    another (it shows "waiting for confirmation on the other device"), and false (never null) when nothing
    waits, on every view of the Google connector."""
    configure(client, auth)
    _, other_token = pair(db, "iPad")
    other = {"Authorization": f"Bearer {other_token}"}

    def seen(headers: dict[str, str]) -> list[Any]:
        own = client.get("/v1/connectors/google", headers=headers).json()
        listed = client.get("/v1/connectors", headers=headers).json()["connectors"]
        entry = next(c for c in listed if c["id"] == "google")["google"]
        return [own["pending_started_here"], entry["pending_started_here"]]

    assert seen(auth) == [False] * 2 and seen(other) == [False] * 2  # nothing pending: false, not null
    assert "status=confirm" in sign_in(client, auth).text
    assert seen(auth) == [True] * 2
    assert seen(other) == [False] * 2
    both = [client.get("/v1/connectors/google", headers=h).json()["pending_account"] for h in (auth, other)]
    assert both == ["owner@example.org"] * 2  # the account itself is the same on both
    assert client.post("/v1/connectors/google/cancel", headers=auth).status_code == 204
    assert seen(auth) == [False] * 2 and seen(other) == [False] * 2


def test_a_no_revokes_the_held_account_and_changes_nothing(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    assert "status=confirm" in sign_in(client, auth).text
    assert client.post("/v1/connectors/google/cancel", headers=auth).status_code == 204
    assert fake.revoked == [REFRESH_TOKEN]
    assert db.execute("SELECT count(*) FROM connector_accounts").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM google_oauth_states").fetchone() == (0,)
    assert client.get("/v1/connectors/google", headers=auth).json()["pending_account"] is None
    assert client.post("/v1/connectors/google/confirm", headers=auth).status_code == 404


def test_a_no_revokes_every_held_sign_in_of_the_device(client, auth, fake, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """E1: cancel forgets every sign-in held for the device, and revokes each one's refresh token at Google
    (best effort), not only the newest."""
    configure(client, auth)
    assert "status=confirm" in sign_in(client, auth).text
    keys = KeyStore(SecretStr(secrets_key))
    db.execute(  # an older held sign-in of the same device, with its own (synthetic) refresh token
        "INSERT INTO google_oauth_states (state_hash, device_id, verifier_enc, redirect_uri, lang, "
        "created_at, expires_at, used_at, pending_account, pending_scopes, pending_refresh_enc, "
        "pending_until) SELECT repeat('a', 64), device_id, verifier_enc, redirect_uri, lang, "
        "created_at - interval '1 minute', expires_at, used_at, pending_account, pending_scopes, %s, "
        "pending_until FROM google_oauth_states",
        (keys.seal(OLDER_REFRESH_TOKEN),),
    )
    assert client.post("/v1/connectors/google/cancel", headers=auth).status_code == 204
    assert sorted(fake.revoked) == sorted([OLDER_REFRESH_TOKEN, REFRESH_TOKEN])
    assert db.execute("SELECT count(*) FROM google_oauth_states").fetchone() == (0,)


def test_a_held_account_expires(client, auth, fake, db, clock) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    assert "status=confirm" in sign_in(client, auth).text
    clock.now = clock.now + timedelta(minutes=16)
    assert client.get("/v1/connectors/google", headers=auth).json()["pending_account"] is None
    assert client.post("/v1/connectors/google/confirm", headers=auth).status_code == 404
    assert db.execute("SELECT count(*) FROM connector_accounts").fetchone() == (0,)


def test_a_refused_consent_comes_back_as_an_error_page(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    state = state_of(start(client, auth)["auth_url"])
    page = client.get("/oauth/google/callback", params={"state": state, "error": "access_denied"})
    assert page.status_code == 400 and "pisa://google-connected?status=error&amp;reason=access_denied" in (
        page.text.replace("&reason", "&amp;reason")
    )
    assert db.execute("SELECT count(*) FROM connector_accounts").fetchone() == (0,)


@pytest.mark.parametrize(("case", "reason"), [("used", "exchange_failed"), ("partial", "missing_scopes")])
def test_rejected_codes_say_why_on_the_page(client, auth, fake, db, case, reason) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    code = USED_CODE if case == "used" else SERVER_AUTH_CODE
    if case == "partial":
        fake.token_file = "token_exchange_partial_scopes.json"
    page = sign_in(client, auth, code)
    assert page.status_code == 400 and f"reason={reason}" in page.text
    assert db.execute("SELECT count(*) FROM connector_accounts").fetchone() == (0,)


def test_disconnect_revokes_and_deletes_caches_but_keeps_tasks(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    connect_google(client, auth)
    wait_for(db, "SELECT count(*) > 0 FROM calendar_events", (True,))
    created = client.post("/v1/tasks", headers=auth, json={"title": "Ritirare il pacco"})
    assert created.status_code == 201
    assert client.delete("/v1/connectors/google", headers=auth).status_code == 204
    assert fake.revoked == [REFRESH_TOKEN]
    for table in ("connector_accounts", "email_messages", "calendars", "calendar_events"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (1,)
    assert (
        client.get("/v1/connectors", headers=auth).json()["connectors"][0]["google"]["status"]
        == "not_connected"
    )


# --- sync, driven directly --------------------------------------------------------------------------------


@asynccontextmanager
async def google_env(db: psycopg.Connection, secrets_key: str, fake: FakeGoogle, clock: Clock):  # type: ignore[no-untyped-def]
    keys = KeyStore(SecretStr(secrets_key))
    pool = await open_pool(dsn(db), max_size=4)
    await pool.wait()
    transport = httpx.MockTransport(fake)
    http = httpx.AsyncClient(transport=transport)
    sync_http = httpx.Client(transport=transport)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO connector_credentials (connector, web_client_id, web_client_secret_enc) "
                "VALUES ('google', %s, %s) ON CONFLICT DO NOTHING",
                (WEB_CLIENT_ID, keys.seal(CLIENT_SECRET)),
            )
            await conn.execute(
                "INSERT INTO connector_accounts (connector, account, scopes, refresh_token_enc, status) "
                "VALUES ('google', 'owner@example.org', '{}', %s, 'connected') ON CONFLICT DO NOTHING",
                (keys.seal(REFRESH_TOKEN),),
            )
        yield GoogleService(pool, keys, http, sync_http, owner=owner_at(), clock=clock), pool
    finally:
        await http.aclose()
        sync_http.close()  # noqa: ASYNC212 (closing an idle mock client does no I/O)
        await pool.close()


def messages(db: psycopg.Connection) -> dict[str, tuple[Any, ...]]:
    rows = db.execute(
        "SELECT id, from_name, from_address, subject, labels, body, intake_status FROM email_messages"
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def test_gmail_first_then_incremental_then_expired_history(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    fake = FakeGoogle()

    async def go(expired: bool, times: int) -> list[Any]:
        fake.history_expired = expired
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            return [await sync_gmail(pool, google, now=T0) for _ in range(times)]

    first, second = run(lambda: go(False, 2))
    assert first.ran and first.changed == 3 and first.error is None
    list_call = next(p for h, path, p in fake.calls if path.endswith("/messages"))
    assert list_call["q"] == ["newer_than:7d -in:trash -in:drafts -in:chats"] and list_call["maxResults"] == [
        "300"
    ]
    assert second.changed == 3  # one added, one relabelled, one deleted
    found = messages(db)
    assert set(found) == {"m-parcel", "m-html", "m-new"}  # the promo was deleted at Google
    parcel = found["m-parcel"]
    assert parcel[:3] == ("Poste Italiane", "noreply@posteitaliane.example", "Il tuo pacco è in giacenza")
    assert parcel[3] == ["INBOX", "CATEGORY_UPDATES"]  # UNREAD removed by the history
    assert (
        parcel[4].startswith("Gentile cliente,\nil tuo pacco è disponibile") and "ricevuta" not in parcel[4]
    )
    html_body = found["m-html"][4]
    assert html_body == (
        "Your dentist appointment is confirmed for Tuesday 21 October at 10:00.\n\nAddress: Via "
        "Verdi 5, Milano"
    )
    assert "alert" not in html_body and "color" not in html_body
    (third,) = run(lambda: go(True, 1))  # the stored history point is gone: 404 → a 7-day resync
    assert third.changed == 1 and third.error is None  # only what isn't stored yet is fetched again
    assert fake.paths("gmail.googleapis.com").count("/gmail/v1/users/me/history") == 2  # 2nd sync + the 404
    assert fake.paths("gmail.googleapis.com").count("/gmail/v1/users/me/profile") == 2


def test_the_first_gmail_sync_goes_on_over_rounds(db, secrets_key, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A low Gmail quota: each round fetches a small batch, keeps it, and the next one goes on from there."""
    from pisa.services import google_sync

    monkeypatch.setattr(google_sync, "FIRST_SYNC_BATCH", 2)
    fake = FakeGoogle()

    async def go() -> Any:
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            return await sync_gmail(pool, google, now=T0)

    def fetched() -> int:
        return sum(1 for _, path, _ in fake.calls if "/messages/" in path)

    assert run(go).changed == 2 and fetched() == 2
    assert db.execute("SELECT gmail_history_id FROM connector_accounts").fetchone() == (None,)
    fake.fail_next_message = True  # the quota runs out in the second round: nothing is lost
    assert run(go).error is not None and len(messages(db)) == 2
    assert run(go).changed == 1 and len(messages(db)) == 3  # the last one: the first sync is done
    assert db.execute("SELECT gmail_history_id FROM connector_accounts").fetchone() != (None,)
    assert fake.paths("gmail.googleapis.com").count("/gmail/v1/users/me/profile") == 1  # one starting point
    assert run(go).changed == 3  # then incremental, from that point
    assert db.execute("SELECT count(*) FROM app_settings WHERE key = 'gmail_first_sync'").fetchone() == (0,)


def test_calendar_full_incremental_gone_and_new_day(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    fake = FakeGoogle()

    def events() -> dict[str, tuple[Any, ...]]:
        rows = db.execute(
            "SELECT id, title, all_day, start_date, end_date, location FROM calendar_events"
        ).fetchall()
        return {r[0]: r[1:] for r in rows}

    async def sync(now: datetime) -> Any:
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            return await sync_calendar(pool, google, timezone="Europe/Rome", now=now)

    first = run(lambda: sync(T0))
    assert first.ran and first.error is None
    assert db.execute("SELECT id, name, access_role FROM calendars ORDER BY name").fetchall() == [
        ("it.italian#holiday@group.v.calendar.google.com", "Festività in Italia", "reader"),
        ("owner@example.org", "Personale", "owner"),
    ]  # the hidden (not selected) calendar is left out; the role decides where Pisa may write
    full = next(p for h, path, p in fake.calls if path.endswith("/events") and "timeMin" in p)
    assert full["timeMin"] == ["2026-10-19T00:00:00+02:00"] and full["timeMax"] == [
        "2026-11-20T00:00:00+01:00"
    ]
    assert full["singleEvents"] == ["true"]
    assert events()["ev-trip"] == ("Weekend a Torino", True, date(2026, 10, 24), date(2026, 10, 26), None)
    assert set(events()) == {"ev-dentist", "ev-trip", "ev-lunch", "hol-1"}

    run(lambda: sync(T0 + timedelta(minutes=5)))  # same day: incremental with the sync token
    assert set(events()) == {
        "ev-dentist",
        "ev-trip",
        "ev-call",
        "hol-1",
    }  # lunch cancelled, far future dropped
    assert db.execute("SELECT sync_token FROM calendars WHERE id = 'owner@example.org'").fetchone() == (
        "sync-token-2",
    )

    fake.calendar_gone = True
    run(lambda: sync(T0 + timedelta(minutes=10)))  # 410 → full resync
    assert set(events()) == {"ev-dentist", "ev-trip", "ev-lunch", "hol-1"}

    fake.calendar_gone = False
    fake.calls.clear()
    run(lambda: sync(datetime(2026, 10, 20, 22, 30, tzinfo=UTC)))  # 00:30 on the 21st in Rome: a new window
    assert all("syncToken" not in p for h, path, p in fake.calls if path.endswith("/events"))


def test_calendar_window_is_rome_days_across_dst() -> None:
    start, end = calendar_window(
        datetime(2026, 10, 24, 22, 30, tzinfo=UTC), "Europe/Rome"
    )  # 00:30 on the 25th
    assert start.isoformat() == "2026-10-24T00:00:00+02:00"
    assert end.isoformat() == "2026-11-25T00:00:00+01:00"


def test_refresh_invalid_grant_asks_for_reconnect_and_pauses_sync(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    fake = FakeGoogle()
    fake.refresh_status = 400  # Testing mode: the refresh token expired after 7 days

    async def go() -> Any:
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            first = await sync_gmail(pool, google, now=T0)
            calls_after_first = len(fake.calls)
            later = [
                await sync_gmail(pool, google, now=T0),
                await sync_calendar(pool, google, timezone="Europe/Rome"),
            ]
            return first, calls_after_first, later, len(fake.calls), await google.connectors()

    first, calls_after_first, later, calls_after_later, connectors = run(go)
    refresh = next(f for f in fake.forms if f.get("grant_type") == ["refresh_token"])
    assert refresh["client_id"] == [WEB_CLIENT_ID] and refresh["refresh_token"] == [REFRESH_TOKEN]
    assert first.ran and first.error == (
        "Google access expired (the app is in Testing mode): reconnect Google in Settings → Connections."
    )
    assert connectors[0]["status"] == "error" and connectors[0]["message"] == first.error
    assert [r.ran for r in later] == [
        False,
        False,
    ] and calls_after_later == calls_after_first  # paused, no loop
    assert db.execute("SELECT needs_reconnect FROM connector_accounts").fetchone() == (True,)


def test_reconnect_clears_the_pause(client, auth, fake, db) -> None:  # type: ignore[no-untyped-def]
    configure(client, auth)
    connect_google(client, auth)
    wait_for(db, "SELECT calendar_synced_at IS NOT NULL FROM connector_accounts", (True,))
    db.execute("UPDATE connector_accounts SET status = 'error', message = 'expired', needs_reconnect = true")
    page = sign_in(client, auth)  # the same account again: nothing changes hands, no confirmation
    assert page.status_code == 200 and "status=connected" in page.text
    again = client.get("/v1/connectors/google", headers=auth).json()
    assert again["status"] == "connected" and again["message"] is None
    assert db.execute("SELECT needs_reconnect FROM connector_accounts").fetchone() == (False,)


def test_rate_limits_wait_and_retry() -> None:
    """2026-10-21 live run: a first Gmail sync hit Google's per-user per-minute quota (HTTP 403 "Quota
    exceeded … per minute per user") and showed as an error. Pisa now waits and retries rate-limit answers."""
    import asyncio as aio

    import httpx

    from pisa.connectors.google.api import GoogleApi
    from pisa.connectors.google.errors import ConnectorError

    answers = [
        httpx.Response(
            403,
            json={
                "error": {
                    "message": "Quota exceeded for quota metric 'Total Query Cost' "
                    "and limit 'Units per minute per user'"
                }
            },
        ),
        httpx.Response(429, json={"error": {"message": "Rate Limit Exceeded"}}, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"ok": True}),
    ]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async def token(force: bool) -> str:
        return "access-token"

    async def go(responses: list[httpx.Response]) -> dict[str, object]:
        transport = httpx.MockTransport(lambda request: responses.pop(0))
        async with httpx.AsyncClient(transport=transport) as http:
            return await GoogleApi(http, token, service="Gmail", sleep=sleep).json("https://gmail.test/x")

    assert aio.run(go(answers)) == {"ok": True} and slept == [2.0, 7.0]
    forbidden = [httpx.Response(403, json={"error": {"message": "Request had insufficient scopes."}})]
    with pytest.raises(ConnectorError):  # a real 403 is not retried
        aio.run(go(forbidden))
    always = [httpx.Response(429, json={"error": {"message": "Rate Limit Exceeded"}}) for _ in range(4)]
    slept.clear()
    with pytest.raises(ConnectorError) as raised:
        aio.run(go(always))
    assert raised.value.status == 429 and slept == [2.0, 5.0, 15.0]


def test_the_owners_event_kind_survives_the_daily_full_resync(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    """The 12 review, H1: the first sync of every day is a full one (DELETE + re-insert); the user's kind and
    minimum stay are Pisa's own data and stay."""
    fake = FakeGoogle()

    async def sync(now: datetime) -> Any:
        async with google_env(db, secrets_key, fake, clock) as (google, pool):
            return await sync_calendar(pool, google, timezone="Europe/Rome", now=now)

    run(lambda: sync(T0))
    db.execute("UPDATE calendar_events SET kind_owner = 'fixed', min_stay_owner = 30 WHERE id = 'ev-trip'")
    run(lambda: sync(datetime(2026, 10, 20, 22, 30, tzinfo=UTC)))  # a new day: a full resync
    row = db.execute("SELECT kind_owner, min_stay_owner FROM calendar_events WHERE id = 'ev-trip'").fetchone()
    assert row == ("fixed", 30)


def test_the_setup_guide_names_this_server_s_redirect_and_the_exact_scopes(client, auth) -> None:  # type: ignore[no-untyped-def]
    client.app.state.runtime.google.redirect_uri = REDIRECT
    guide = client.get("/v1/connectors/google/setup", headers=auth)
    assert guide.status_code == 200, guide.text
    doc = guide.json()
    assert doc["redirect_uri"] == REDIRECT
    assert doc["scopes"] == client.get("/v1/connectors/google", headers=auth).json()["requested_scopes"]
    steps = {s["id"]: s for s in doc["steps"]}
    assert steps["client"]["copy"] == REDIRECT and "\n".join(doc["scopes"]) == steps["scopes"]["copy"]
    assert any("api-services-user-data-policy" in line for line in doc["legal"])  # in their language
    client.app.state.runtime.google.redirect_uri = None  # no https address: the https step first
    assert client.get("/v1/connectors/google/setup", headers=auth).json()["steps"][0]["id"] == "https"
