"""Push (contract E): the APNs client against a mock transport (JWT claims, topic, hosts, 410 → token
dropped), the delivery rule (ws vs apns vs none), token registration and push_not_configured."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from pisa.connectors.apns import JWT_RENEW_S, ApnsClient, ApnsKey
from pisa.db.pool import open_pool
from pisa.infra.keys import KeyStore
from pisa.ports.push import OutgoingApproval, OutgoingNotification
from pisa.services.notifications import Dispatcher, enqueue
from pisa.services.push import PushService, set_apns_key
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at

KEY_ID = "ABCDE12345"
TEAM_ID = "EXAMPLETEAM"
TOPIC = "com.example.pisa"
TOKEN = "ab" * 32
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)  # 12:00 in Rome: outside the default quiet hours


def p8() -> tuple[str, ec.EllipticCurvePublicKey]:
    """A throwaway P-256 key made for this test run (never a real APNs key)."""
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, private.public_key()


class Apple:
    """A mock APNs: records each request and answers with the queued (status, reason) or 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list[tuple[int, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, reason = self.answers.pop(0) if self.answers else (200, "")
        if status == 200:
            return httpx.Response(200, headers={"apns-id": request.headers["apns-id"]})
        return httpx.Response(status, json={"reason": reason})


def send(client: ApnsClient, environment: str = "development") -> Any:
    return asyncio.run(
        client.send(
            TOKEN,
            environment,
            title="Pisa",
            body="Tra 15 minuti: Dentista.",
            thread_id="reminder",
            data={"kind": "reminder", "notification_id": "n-1"},
            push_id="0b6f3c1e-0000-4000-8000-000000000001",
        )
    )


def test_apns_request_jwt_topic_and_payload() -> None:
    pem, public = p8()
    apple = Apple()
    clock = Clock(1_800_000_000.0)

    async def go() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
            client = ApnsClient(http, ApnsKey(KEY_ID, TEAM_ID, pem), topic=TOPIC, clock=clock)
            out = [await client.send(TOKEN, env, title="Pisa", body="Ciao.", thread_id="briefing",
                                     data={"kind": "briefing", "notification_id": "n-1"}, push_id="p-1")
                   for env in ("development", "production")]  # fmt: skip
            return out

    outcomes = asyncio.run(go())
    assert [o.status for o in outcomes] == ["sent", "sent"]
    sandbox, production = apple.requests
    assert str(sandbox.url) == f"https://api.sandbox.push.apple.com/3/device/{TOKEN}"
    assert str(production.url) == f"https://api.push.apple.com/3/device/{TOKEN}"
    headers = sandbox.headers
    assert headers["apns-topic"] == TOPIC and headers["apns-push-type"] == "alert"
    assert headers["apns-id"] == "p-1"
    token = headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    claims = jwt.decode(token, public, algorithms=["ES256"], options={"verify_iat": False})
    assert claims == {"iss": TEAM_ID, "iat": 1_800_000_000}
    assert json.loads(sandbox.content) == {
        "aps": {"alert": {"title": "Pisa", "body": "Ciao."}, "thread-id": "briefing", "sound": "default"},
        "kind": "briefing",
        "notification_id": "n-1",
    }


def test_jwt_is_cached_and_renewed_every_50_minutes() -> None:
    pem, _ = p8()
    clock = Clock(1_800_000_000.0)
    client = ApnsClient(httpx.AsyncClient(), ApnsKey(KEY_ID, TEAM_ID, pem), topic=TOPIC, clock=clock)
    first = client.provider_token()
    clock.now += JWT_RENEW_S - 1
    assert client.provider_token() == first  # cached
    clock.now += 1
    renewed = client.provider_token()
    assert renewed != first and jwt.decode(renewed, options={"verify_signature": False})["iat"] == int(
        clock.now
    )
    assert JWT_RENEW_S == 50 * 60


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (410, "Unregistered", "token_invalid"),
        (410, "ExpiredToken", "token_invalid"),
        (400, "BadDeviceToken", "token_invalid"),
        (410, "SomethingNew", "failed"),  # only the push contract's reasons drop a token (as at the relay)
        (400, "BadTopic", "failed"),
        (500, "InternalServerError", "failed"),
    ],
)
def test_apns_errors(status: int, reason: str, expected: str) -> None:
    pem, _ = p8()
    apple = Apple()
    apple.answers.append((status, reason))
    http = httpx.AsyncClient(transport=httpx.MockTransport(apple))
    client = ApnsClient(http, ApnsKey(KEY_ID, TEAM_ID, pem), topic=TOPIC)
    outcome = send(client)
    assert outcome.status == expected
    assert reason in (outcome.message or "")


def test_expired_provider_token_renews_and_retries_once() -> None:
    pem, _ = p8()
    apple = Apple()
    apple.answers.append((403, "ExpiredProviderToken"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(apple))
    client = ApnsClient(http, ApnsKey(KEY_ID, TEAM_ID, pem), topic=TOPIC)
    assert send(client).status == "sent"
    assert len(apple.requests) == 2


# --- the delivery rule -----------------------------------------------------------------------------------


class Live:
    """LiveDevices: the devices listed in `online` have a live socket."""

    def __init__(self, online: set[str]) -> None:
        self.online = online
        self.sent: list[tuple[str, OutgoingNotification]] = []
        self.approvals: list[tuple[str, OutgoingApproval]] = []
        self.done: list[tuple[str, dict[str, Any]]] = []  # action_done

    async def notify(self, device_id: object, notification: OutgoingNotification) -> bool:
        if str(device_id) not in self.online:
            return False
        self.sent.append((str(device_id), notification))
        return True

    async def approval(self, device_id: object, approval: OutgoingApproval) -> bool:
        if str(device_id) not in self.online:
            return False
        self.approvals.append((str(device_id), approval))
        return True

    async def action_done(self, device_id: object, done: dict[str, Any]) -> bool:
        if str(device_id) not in self.online:
            return False
        self.done.append((str(device_id), done))
        return True


def deliver(
    db: psycopg.Connection,
    secrets_key: str,
    *,
    online: set[str],
    with_key: bool = True,
    answers: list[tuple[int, str]] | None = None,
    device_id: str | None = None,
    now: datetime = NOW,
    broken_key: bool = False,
) -> tuple[str | None, Live, Apple]:
    """Store one reminder and run the API's delivery for it."""
    apple = Apple()
    apple.answers += answers or []
    live = Live(online)
    keys = KeyStore(SecretStr(secrets_key))

    async def go() -> str | None:
        pool = await open_pool(dsn(db), max_size=3)
        await pool.wait()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(apple)) as http:
                push = PushService(pool, keys, http, team_id=TEAM_ID, topic=TOPIC)
                async with pool.connection() as conn:
                    if with_key:
                        await set_apns_key(conn, keys, KEY_ID, p8()[0])
                    if broken_key:  # e.g. PISA_SECRETS_KEY changed: the stored key no longer decrypts
                        await conn.execute("UPDATE server_secrets SET ciphertext = 'not a fernet token'")
                    notification_id, _ = await enqueue(
                        conn,
                        kind="reminder",
                        text="Tra 15 minuti: Dentista (Via Immaginaria 1).",
                        lang="it-IT",
                        now=now,
                        timezone="Europe/Rome",
                        device_id=uuid.UUID(device_id) if device_id else None,
                    )
                return await Dispatcher(pool, live, push, clock=Clock(now), owner=owner_at()).deliver(
                    notification_id
                )
        finally:
            await pool.close()

    return asyncio.run(go()), live, apple


def with_token(db: psycopg.Connection, device_id: str, token: str = TOKEN) -> None:
    db.execute(
        "UPDATE devices SET apns_token = %s, apns_environment = 'development' WHERE id = %s",
        (token, device_id),
    )


def stored(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    return db.execute("SELECT kind, delivered_via, delivered_at IS NOT NULL FROM notifications").fetchall()


def test_live_socket_gets_notify(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    with_token(db, device_id)
    via, live, apple = deliver(db, secrets_key, online={device_id})
    assert via == "ws" and not apple.requests  # a live socket wins over the push
    (target, notification), = live.sent  # fmt: skip
    assert target == device_id and notification.kind == "reminder" and notification.priority == "normal"
    assert stored(db) == [("reminder", "ws", True)]


def test_no_socket_gets_apns_push(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    with_token(db, device_id)
    via, live, apple = deliver(db, secrets_key, online=set())
    assert via == "apns" and not live.sent
    (request,) = apple.requests
    body = json.loads(request.content)
    assert body["aps"]["alert"] == {"title": "Pisa", "body": "Tra 15 minuti: Dentista (Via Immaginaria 1)."}
    assert body["aps"]["thread-id"] == "reminder" and body["kind"] == "reminder"
    notification_id = db.execute("SELECT id FROM notifications").fetchone()[0]  # type: ignore[index]
    assert body["notification_id"] == str(notification_id)
    assert stored(db) == [("reminder", "apns", True)]


def test_neither_socket_nor_token_gets_nothing_but_is_stored(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    pair(db)
    via, live, apple = deliver(db, secrets_key, online=set())
    assert via == "none" and not live.sent and not apple.requests
    assert stored(db) == [("reminder", "none", True)]


def test_each_device_by_its_own_channel(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    phone, _ = pair(db, "iPhone")
    ipad, _ = pair(db, "iPad")
    with_token(db, phone)
    with_token(db, ipad, "cd" * 32)
    via, live, apple = deliver(db, secrets_key, online={phone})
    assert via == "ws" and [t for t, _ in live.sent] == [phone]
    assert [r.url.path for r in apple.requests] == [f"/3/device/{'cd' * 32}"]


def test_410_unregistered_drops_the_token(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    with_token(db, device_id)
    via, _, _ = deliver(db, secrets_key, online=set(), answers=[(410, "Unregistered")])
    assert via == "none"
    row = db.execute(
        "SELECT apns_token, apns_environment FROM devices WHERE id = %s", (device_id,)
    ).fetchone()
    assert row == (None, None)


def test_unusable_key_never_stops_socket_delivery(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    # The key is resolved only for a device that needs APNs, and its failure is contained.
    phone, _ = pair(db, "iPhone")
    ipad, _ = pair(db, "iPad")
    with_token(db, ipad)
    via, live, apple = deliver(db, secrets_key, online={phone}, broken_key=True)
    assert via == "ws" and [t for t, _ in live.sent] == [phone] and not apple.requests


def test_no_key_means_no_push(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    with_token(db, device_id)
    via, _, apple = deliver(db, secrets_key, online=set(), with_key=False)
    assert via == "none" and not apple.requests


def test_targeted_message_goes_only_to_its_device(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    phone, _ = pair(db, "iPhone")
    ipad, _ = pair(db, "iPad")
    via, live, _ = deliver(db, secrets_key, online={phone, ipad}, device_id=ipad)
    assert via == "ws" and [t for t, _ in live.sent] == [ipad]


def test_stale_pending_message_is_not_sent(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, _ = pair(db)
    live = Live({device_id})
    keys = KeyStore(SecretStr(secrets_key))

    async def go() -> str | None:
        pool = await open_pool(dsn(db), max_size=3)
        await pool.wait()
        try:
            async with pool.connection() as conn:
                notification_id, _ = await enqueue(
                    conn, kind="info", text="x", lang="it-IT", now=NOW, timezone="Europe/Rome"
                )
            push = PushService(pool, keys, httpx.AsyncClient(), team_id=TEAM_ID, topic=TOPIC)
            later = Clock(NOW + timedelta(minutes=16))  # the API was down
            return await Dispatcher(pool, live, push, clock=later, owner=owner_at()).deliver(notification_id)
        finally:
            await pool.close()

    assert asyncio.run(go()) == "none" and not live.sent


# --- registration (REST) and the key (CLI) ----------------------------------------------------------------


def test_register_without_key_is_204_and_the_token_saved(client, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    direct = {"mode": "direct", "token": TOKEN.upper(), "environment": "production"}
    response = client.put("/v1/devices/me/push", headers=auth, json=direct)
    assert (
        response.status_code == 204
    )  # a valid request; push reads off (readyz, discovery) until the key is there
    row = db.execute(
        "SELECT apns_token, apns_environment FROM devices WHERE id = %s", (device_id,)
    ).fetchone()
    assert row == (TOKEN, "production")
    assert client.delete("/v1/devices/me/push", headers=auth).status_code == 204
    row = db.execute("SELECT apns_token FROM devices WHERE id = %s", (device_id,)).fetchone()
    assert row == (None,)


def test_register_with_key_is_204(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def install() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_apns_key(conn, KeyStore(SecretStr(secrets_key)), KEY_ID, p8()[0])

    asyncio.run(install())
    _, token = pair(db)
    response = client.put(
        "/v1/devices/me/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"mode": "direct", "token": TOKEN, "environment": "sandbox"},
    )
    assert response.status_code == 204


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "direct", "token": "not-hex-at-all-not-hex-at-all-xx", "environment": "sandbox"},
        {"mode": "direct", "token": "ab", "environment": "sandbox"},
        {"mode": "direct", "token": "ab" * 31, "environment": "sandbox"},  # the push contract: 64 to 200
        {"mode": "direct", "token": "ab" * 101, "environment": "sandbox"},  # the schema takes up to 400
        {"mode": "direct", "token": TOKEN, "environment": "staging"},
        {"mode": "direct", "environment": "sandbox"},
        {"apns_token": TOKEN, "environment": "development"},  # v1's body: gone (B6b)
    ],
)
def test_register_validation(client, db, body) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db)
    response = client.put("/v1/devices/me/push", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_apns_key_is_stored_encrypted(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    pem, _ = p8()

    async def install() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_apns_key(conn, KeyStore(SecretStr(secrets_key)), KEY_ID, pem)

    asyncio.run(install())
    ciphertext, meta = db.execute(  # type: ignore[misc]
        "SELECT ciphertext, meta FROM server_secrets WHERE name = 'apns'"
    ).fetchone()
    assert meta == {"key_id": KEY_ID}
    assert b"PRIVATE KEY" not in bytes(ciphertext)
    assert KeyStore(SecretStr(secrets_key)).unseal(bytes(ciphertext)).strip() == pem.strip()


@pytest.mark.parametrize(
    ("key_id", "pem", "message"),
    [
        ("ABCDE1234", None, "An APNs key id is 10 letters and digits"),
        (KEY_ID, "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n", "That is not an APNs key"),
    ],
)
def test_apns_key_validation(db, secrets_key, key_id, pem, message) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.errors import PisaError

    async def install() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_apns_key(conn, KeyStore(SecretStr(secrets_key)), key_id, pem or p8()[0])

    with pytest.raises(PisaError) as err:
        asyncio.run(install())
    assert err.value.message.startswith(message)


def test_cli_reads_the_key_from_stdin_only(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from pisa import cli

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setenv("PISA_SECRETS_KEY", "x" * 43 + "=")
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level, **_: None)  # structlog stays
    from pisa.settings import get_settings

    get_settings.cache_clear()
    try:
        assert cli.main(["secrets", "set-apns", "--key-id", KEY_ID]) == 2
    finally:
        get_settings.cache_clear()
    assert "pipe the .p8 file" in capsys.readouterr().err


def test_an_installed_key_without_team_or_topic_names_the_setting(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """No team id or topic is built in: with a key installed and either unset, the error says which to set."""
    from pisa.domain.errors import PisaError

    keys = KeyStore(SecretStr(secrets_key))

    async def go(team_id: str | None, topic: str | None) -> str:
        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            async with httpx.AsyncClient() as http, pool.connection() as conn:
                await set_apns_key(conn, keys, KEY_ID, p8()[0])
                with pytest.raises(PisaError) as caught:
                    await PushService(pool, keys, http, team_id=team_id, topic=topic).sender(conn)
                return f"{caught.value.code}: {caught.value.message}"
        finally:
            await pool.close()

    assert asyncio.run(go(None, TOPIC)).startswith("push_not_configured:")
    assert "PISA_APNS_TEAM_ID" in asyncio.run(go(None, TOPIC))
    assert "PISA_APNS_TOPIC" in asyncio.run(go(TEAM_ID, None))


def test_settings_carry_no_team_or_topic() -> None:
    from pisa.settings import Settings

    settings = Settings(_env_file=None, apns_team_id="", apns_topic="")  # type: ignore[call-arg]
    assert settings.apns_team_id is None and settings.apns_topic is None
    fields = Settings.model_fields
    assert fields["apns_team_id"].default is None and fields["apns_topic"].default is None
