"""The push relay's server side (CONTRACT-2 §9; `relay/PROTOCOL.md`): the HPKE seal against RFC 9180's own
test vectors for its suite, a round trip, a blob that opens only with the device's key and its handle,
tampering, the two padded sizes and the cut of a long notice; then the sending, against an in-process fake
relay that checks the HMAC and the 5-minute replay window and records what it got: a notice reaches it sealed
(and opens on the "phone" only), a background wake carries no blob, a forgotten handle drops the registration,
and a Live Activity update through the relay carries no personal text. Discovery and readyz say whether the
relay is set."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pisa_conformance.cli import discovery_problems
from pisa_conformance.paths import data_root
from pisa_conformance.validate import EXT_ID, errors
from pydantic import SecretStr
from pyhpke import AEADId, KDFId, KEMId
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from pisa.connectors.push_relay import RelayClient
from pisa.db.pool import open_pool
from pisa.domain import live_activity as la_rules
from pisa.domain import push_seal
from pisa.infra.keys import KeyStore
from pisa.services.notifications import Dispatcher, enqueue
from pisa.services.push import PushService
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.test_push import NOW, TEAM_ID, TOPIC, Live

RELAY = "https://relay.example.net"
SENT = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)

# RFC 9180, appendix A.2.1: DHKEM(X25519, HKDF-SHA256), HKDF-SHA256, ChaCha20Poly1305, base mode
RFC = {
    "info": "4f6465206f6e2061204772656369616e2055726e",
    "ikmE": "909a9b35d3dc4713a5e72a4da274b55d3d3821a37e5d099e74a647db583a904b",
    "pkEm": "1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a",
    "skEm": "f4ec9b33b792c372c1d2c2063507b684ef925b8c75a42dbcbf57d63ccd381600",
    "ikmR": "1ac01f181fdf9f352797655161c58b75c656a6cc2716dcb66372da835542e1df",
    "pkRm": "4310ee97d88cc1f088a5576c77ab0cf5c3ac797f3d95139c6c84b5429c59662a",
    "skRm": "8057991eef8f1f1af18f4a9491d16a1ce333f695d4db8e38da75975c4478e0fb",
    "enc": "1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a",
    "pt": "4265617574792069732074727574682c20747275746820626561757479",
    "encryptions": {
        0: ("436f756e742d30",
            "1c5250d8034ec2b784ba2cfd69dbdb8af406cfe3ff938"
            "e131f0def8c8b60b4db21993c62ce81883d2dd1b51a28"),
        1: ("436f756e742d31",
            "6b53c051e4199c518de79594e1c4ab18b96f081549d45"
            "ce015be002090bb119e85285337cc95ba5f59992dc98c"),
        2: ("436f756e742d32",
            "71146bd6795ccc9c49ce25dda112a48f202ad22055950"
            "2cef1f34271e0cb4b02b4f10ecac6f48c32f878fae86b"),
        4: ("436f756e742d34",
            "63357a2aa291f5a4e5f27db6baa2af8cf77427c7c1a90"
            "9e0b37214dd47db122bb153495ff0b02e9e54a50dbe16"),
        255: ("436f756e742d323535",
            "18ab939d63ddec9f6ac2b60d61d36a7375d2070c9b683"
            "861110757062c52b8880a5f6b3936da9cd6c23ef2a95c"),
        256: ("436f756e742d323536",
            "7a4a13e9ef23978e2c520fd4d2e757514ae160cd0cd05"
            "e556ef692370ca53076214c0c40d4c728d6ed9e727a5b"),
    },
    "exports": [
        ("", "4bbd6243b8bb54cec311fac9df81841b6fd61f56538a775e7c80a9f40160606e"),
        ("00", "8c1df14732580e5501b00f82b10a1647b40713191b7c1240ac80e2b68808ba69"),
        ("54657374436f6e74657874", "5acb09211139c43b3090489a9da433e8a30ee7188ba8b0a9a1ccf0c229283e53"),
    ],
}  # fmt: skip


def h(text: str) -> bytes:
    return bytes.fromhex(text)


def test_the_suite_passes_rfc_9180_s_test_vectors_for_it() -> None:
    suite = push_seal.suite()
    assert (suite.kem.id, suite.kdf.id, suite.aead.id) == (
        KEMId.DHKEM_X25519_HKDF_SHA256,
        KDFId.HKDF_SHA256,
        AEADId.CHACHA20_POLY1305,
    )
    ephemeral = suite.kem.derive_key_pair(h(RFC["ikmE"]))
    recipient = suite.kem.derive_key_pair(h(RFC["ikmR"]))
    assert ephemeral.public_key.to_public_bytes() == h(RFC["pkEm"])
    assert ephemeral.private_key.to_private_bytes() == h(RFC["skEm"])
    assert recipient.public_key.to_public_bytes() == h(RFC["pkRm"])
    assert recipient.private_key.to_private_bytes() == h(RFC["skRm"])
    enc, sender = suite.create_sender_context(recipient.public_key, h(RFC["info"]), eks=ephemeral)
    assert enc == h(RFC["enc"])
    receiver = suite.create_recipient_context(enc, recipient.private_key, h(RFC["info"]))
    sealed = {seq: sender.seal(h(RFC["pt"]), h(aad)) for seq, (aad, _) in
              ((s, RFC["encryptions"].get(s, (f"{s:02x}", ""))) for s in range(257))}  # fmt: skip
    for seq, (_, ct) in RFC["encryptions"].items():
        assert sealed[seq] == h(ct), seq
    for seq in range(257):  # the recipient's sequence follows the sender's
        aad = RFC["encryptions"].get(seq, (f"{seq:02x}", ""))[0]
        assert receiver.open(sealed[seq], h(aad)) == h(RFC["pt"])
    for context, value in RFC["exports"]:
        assert sender.export(h(context), 32) == h(value)


def device_keys() -> tuple[bytes, bytes]:
    private = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    return (
        private.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
        private.public_key().public_bytes(raw, serialization.PublicFormat.Raw),
    )


NOTICE = {"noticeId": "44444444-4444-4444-8444-444444444444", "kind": "reminder", "priority": "normal",
          "lang": "it-IT", "text": "Tra 15 minuti: Dentista (Via Immaginaria 1).", "privacy": "health",
          "genericText": "Un promemoria da Pisa"}  # fmt: skip


def raw(sealed: str) -> bytes:
    return base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4))


def test_a_sealed_notice_opens_with_the_matching_key_and_handle_only() -> None:
    private, public = device_keys()
    apns = {"category": "NOTICE", "threadId": "reminder", "interruption": "active"}
    sealed = push_seal.seal(push_seal.plaintext(NOTICE, apns, sent_at=SENT), public, "h-1")
    opened = push_seal.open_sealed(sealed, private, "h-1")
    assert opened == {"v": 2, "notice": NOTICE, "sentAt": "2026-11-18T10:00:00Z", "apns": apns}
    assert errors(opened, EXT_ID + "#/$defs/PushPlaintext") == []
    assert errors(opened["notice"], EXT_ID + "#/$defs/s_pisa_notice") == []
    other, _ = device_keys()
    for key, handle in (
        (other, "h-1"),
        (private, "h-2"),
    ):  # another phone, or the blob moved to another handle
        with pytest.raises(push_seal.SealError):
            push_seal.open_sealed(sealed, key, handle)
    assert NOTICE["text"].encode() not in raw(sealed)  # nothing readable in the blob


@pytest.mark.parametrize("where", [0, 16, 31, 32, 100, 511])  # the encapsulated key, the ciphertext, its tag
def test_a_tampered_blob_is_refused(where: int) -> None:
    private, public = device_keys()
    blob = bytearray(raw(push_seal.seal(push_seal.plaintext(NOTICE, sent_at=SENT), public, "h-1")))
    blob[where] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(blob)).rstrip(b"=").decode()
    with pytest.raises(push_seal.SealError):
        push_seal.open_sealed(tampered, private, "h-1")


def test_padding_gives_512_bytes_else_2048_and_a_long_notice_is_cut_to_fit() -> None:
    private, public = device_keys()
    short = push_seal.seal(push_seal.plaintext(NOTICE, sent_at=SENT), public, "h")
    medium = push_seal.seal(push_seal.plaintext(NOTICE | {"text": "x" * 600}, sent_at=SENT), public, "h")
    # 1000 characters of three bytes each: 3000 bytes of text, cut by the notice rules to fit 2 KB
    long_text = "è" * 400 + "€" * 600
    body = push_seal.plaintext(NOTICE | {"text": long_text, "title": "t" * 200}, sent_at=SENT)
    long = push_seal.seal(body, public, "h")
    assert [len(raw(s)) for s in (short, medium, long)] == [512, 2048, 2048]
    opened = push_seal.open_sealed(long, private, "h")["notice"]
    assert opened["text"].endswith("…") and long_text.startswith(opened["text"][:-1])
    assert opened["title"] == "t" * 200 and opened["genericText"] == NOTICE["genericText"]
    assert errors(opened, EXT_ID + "#/$defs/s_pisa_notice") == []
    assert push_seal.open_sealed(medium, private, "h")["notice"]["text"] == "x" * 600


# --- the fake relay -----------------------------------------------------------------------------------------
class FakeRelay:
    """The relay's sending API as `relay/PROTOCOL.md` defines it: the handle's key is sha256(secret); the HMAC
    over `<timestamp>\\n<body>`; a 5-minute window on its own clock; a signature accepted once."""

    def __init__(self, clock: Any = time.time) -> None:
        self.keys: dict[str, bytes] = {}
        self.got: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.refused: list[str] = []
        self.seen: set[str] = set()
        self.clock = clock
        self.answer = 202  # what it answers a valid request (422: a Live Activity's token is dead)
        self.forget_answer = 204
        self.repeats = 0
        self.forgotten: list[str] = []

    def register(self, handle: str, secret: bytes) -> None:
        self.keys[handle] = hashlib.sha256(secret).digest()

    async def push(self, request: Request) -> Response:
        body = await request.body()
        try:
            data = json.loads(body)
            stamp = int(request.headers[push_seal.TIMESTAMP_HEADER])
            signature = request.headers[push_seal.SIGNATURE_HEADER]
        except (ValueError, KeyError):
            self.refused.append("malformed")
            return Response(status_code=400)
        key = self.keys.get(data.get("handle"))
        if key is None:
            return Response(status_code=410)
        expected = hmac.new(key, str(stamp).encode() + b"\n" + body, hashlib.sha256).digest()
        sent = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, sent):
            self.refused.append("signature")
            return Response(status_code=401)
        if abs(self.clock() - stamp) > push_seal.REPLAY_WINDOW_S:
            self.refused.append("window")
            return Response(status_code=401)
        if signature in self.seen:  # an exact repeat of an accepted push: 202, sent once (as the relay does)
            self.repeats += 1
            return Response(status_code=202)
        self.seen.add(signature)
        self.got.append(data)
        self.raw.append(body)
        return Response(status_code=self.answer)

    async def forget(self, request: Request) -> Response:
        """`DELETE /v1/register` `{handle}`, signed as a push is (E1 R13)."""
        body = await request.body()
        handle = json.loads(body).get("handle")
        key = self.keys.get(handle)
        if key is None:
            return Response(status_code=410)
        stamp = request.headers.get(push_seal.TIMESTAMP_HEADER, "")
        signature = request.headers.get(push_seal.SIGNATURE_HEADER, "")
        expected = hmac.new(key, stamp.encode() + b"\n" + body, hashlib.sha256).digest()
        sent = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if (
            not hmac.compare_digest(expected, sent)
            or abs(self.clock() - int(stamp)) > push_seal.REPLAY_WINDOW_S
        ):
            self.refused.append("forget")
            return Response(status_code=401)
        if self.forget_answer != 204:
            return Response(status_code=self.forget_answer)
        del self.keys[handle]
        self.forgotten.append(handle)
        return Response(status_code=204)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/push", self.push, methods=["POST"]),
                Route("/v1/register", self.forget, methods=["DELETE"]),
            ]
        )


def register(db: psycopg.Connection, secrets_key: str, device_id: str, handle: str, secret: bytes,
             public_key: bytes) -> None:  # fmt: skip
    keys = KeyStore(SecretStr(secrets_key))
    db.execute(
        "INSERT INTO device_push_relay (device_id, handle, secret, public_key, created_at) VALUES (%s, %s, "
        "%s, %s, now())",
        (device_id, handle, keys.seal_bytes(secret), public_key),
    )


def run_with_relay(
    db: psycopg.Connection, secrets_key: str, relay: FakeRelay, go: Any, *, clock: Any = None
) -> Any:
    keys = KeyStore(SecretStr(secrets_key))

    async def main() -> Any:
        pool = await open_pool(dsn(db), max_size=3)
        await pool.wait()
        try:
            transport = httpx.ASGITransport(app=relay.app())
            async with httpx.AsyncClient(transport=transport, base_url=RELAY) as http:
                push = PushService(pool, keys, http, team_id=TEAM_ID, topic=TOPIC, relay_url=RELAY)
                if clock is not None:
                    push.relay = RelayClient(http, RELAY, clock=clock)
                return await go(pool, push)
        finally:
            await pool.close()

    return asyncio.run(main())


@pytest.fixture
def relayed(db: psycopg.Connection, secrets_key: str) -> dict[str, Any]:
    device_id, token = pair(db)
    private, public = device_keys()
    secret = bytes(range(32))
    handle = "h-" + uuid.uuid4().hex[:12]
    register(db, secrets_key, device_id, handle, secret, public)
    relay = FakeRelay()
    relay.register(handle, secret)
    return {
        "device_id": device_id,
        "private": private,
        "handle": handle,
        "secret": secret,
        "relay": relay,
        "token": token,
    }


def test_a_notice_for_a_relay_device_reaches_the_relay_sealed_and_opens_on_the_phone_only(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    relay: FakeRelay = relayed["relay"]

    async def go(pool: Any, push: PushService) -> str | None:
        async with pool.connection() as conn:
            notification_id, _ = await enqueue(
                conn, kind="reminder", text="Tra 15 minuti: Dentista (Via Immaginaria 1).", lang="it-IT",
                now=NOW,
                timezone="Europe/Rome",
            )  # fmt: skip
        return await Dispatcher(pool, Live(set()), push, clock=Clock(NOW), owner=owner_at()).deliver(
            notification_id
        )

    via = run_with_relay(db, secrets_key, relay, go)
    assert via == "apns" and relay.refused == []
    (sent,) = relay.got
    assert set(sent) == {"handle", "type", "collapse_id", "sealed"} and sent["type"] == "alert"
    assert sent["handle"] == relayed["handle"]
    assert b"Dentista" not in relay.raw[0] and b"Roma" not in relay.raw[0]  # the relay sees no text
    assert len(raw(sent["sealed"])) == 512
    opened = push_seal.open_sealed(sent["sealed"], relayed["private"], relayed["handle"])
    notice = opened["notice"]
    assert errors(notice, EXT_ID + "#/$defs/s_pisa_notice") == []
    assert notice["text"] == "Tra 15 minuti: Dentista (Via Immaginaria 1)." and notice["kind"] == "reminder"
    assert notice["privacy"] == "health" and notice["genericText"] == "Un promemoria da Pisa"
    assert opened["apns"]["category"] == "NOTICE" and opened["apns"]["threadId"] == "reminder"
    assert sent["collapse_id"] == notice["noticeId"]
    # the extension's replay gate (E1 R11): a fresh sentAt in UTC, and the stored notification's own id
    assert errors(opened, EXT_ID + "#/$defs/PushPlaintext") == []
    sent_at = datetime.fromisoformat(opened["sentAt"].replace("Z", "+00:00"))
    assert opened["sentAt"].endswith("Z") and abs((datetime.now(UTC) - sent_at).total_seconds()) < 300
    stored_id = db.execute("SELECT id FROM notifications").fetchone()
    assert stored_id is not None and notice["noticeId"] == str(stored_id[0])


def test_a_request_out_of_the_window_or_forged_is_refused_and_a_repeat_is_sent_once(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    relay: FakeRelay = relayed["relay"]
    late = time.time() - push_seal.REPLAY_WINDOW_S - 5

    async def go(pool: Any, push: PushService) -> list[Any]:
        assert push.relay is not None
        async with pool.connection() as conn:
            stale = await push.send_relay_notice(conn, relayed["device_id"], NOTICE)
        body = push_seal.relay_request(relayed["handle"], "background")
        fresh = int(time.time())
        headers = push_seal.relay_headers(relayed["secret"], fresh, body)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app()), base_url=RELAY) as http:
            first = await http.post("/v1/push", content=body, headers=headers)
            again = await http.post("/v1/push", content=body, headers=headers)  # the same request, replayed
            forged = await http.post(
                "/v1/push", content=body, headers=push_seal.relay_headers(b"\x00" * 32, fresh + 1, body)
            )
        return [stale, first.status_code, again.status_code, forged.status_code]

    stale, first, again, forged = run_with_relay(db, secrets_key, relay, go, clock=lambda: late)
    assert stale is not None and stale.status == "failed"
    assert (first, again, forged) == (202, 202, 401)  # a repeat is accepted and sent once
    assert relay.refused == ["window", "signature"] and relay.repeats == 1 and len(relay.got) == 1


def test_a_forgotten_handle_drops_the_registration(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    relay: FakeRelay = relayed["relay"]

    async def go(pool: Any, push: PushService) -> list[Any]:
        async with pool.connection() as conn:
            sent = await push.send_relay_notice(conn, relayed["device_id"], NOTICE)
            relay.keys.clear()  # the app unregistered: the relay answers 410
            gone = await push.send_relay_notice(conn, relayed["device_id"], NOTICE)
            after = await push.send_relay_notice(conn, relayed["device_id"], NOTICE)
        return [sent.status if sent else None, gone.status if gone else None, after]

    assert run_with_relay(db, secrets_key, relay, go) == ["sent", "token_invalid", None]
    assert [(g["handle"], g["type"]) for g in relay.got] == [(relayed["handle"], "alert")]
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (0,)


def test_a_relay_device_without_a_relay_url_gets_nothing_and_says_why(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    async def go(pool: Any, push: PushService) -> Any:
        push.relay = None
        async with pool.connection() as conn:
            return await push.send_relay_notice(conn, relayed["device_id"], NOTICE)

    outcome = run_with_relay(db, secrets_key, relayed["relay"], go)
    assert outcome.status == "failed" and "PISA_PUSH_RELAY_URL" in str(outcome.message)
    assert relayed["relay"].got == []


def test_an_approval_s_relay_push_is_a_notice_that_opens_the_approval() -> None:
    from pisa.ports.push import OutgoingApproval
    from pisa.services.approvals import approval_notice

    approval_id = str(uuid.uuid4())
    outgoing = OutgoingApproval(approval_id, "email_send", "Send the email to Anna", [], True,
                                "2026-10-20T10:10:00Z", None, lang="it-IT")  # fmt: skip
    value = approval_notice(outgoing, "Invio la mail ad Anna?")
    assert errors(value, EXT_ID + "#/$defs/s_pisa_notice") == []
    assert value["refs"] == [f"approval:{approval_id}"] and value["privacy"] == "people"
    assert value["genericText"] == "Pisa aspetta una tua risposta"


def test_live_activity_updates_through_the_relay_carry_no_personal_text() -> None:
    now = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    items = [
        {"kind": "event", "title": "Dentista", "start": now - timedelta(hours=2),
         "end": now - timedelta(hours=1), "place_id": None},
        {"kind": "travel", "start": now + timedelta(minutes=10), "end": now + timedelta(minutes=30),
         "leave_at": now + timedelta(minutes=10), "minutes": 20, "mode": "walk"},
        {"kind": "event", "title": "Cena da Marco", "start": now + timedelta(minutes=30),
         "end": now + timedelta(hours=2), "place_id": "p1"},
    ]  # fmt: skip
    content = la_rules.content_state(
        items, now, {"p1": "Via Immaginaria 1"}, "it-IT", now + timedelta(hours=8)
    )
    assert content["title"] == "Cena da Marco"  # the APNs content (direct mode) names it, with its times
    # through the relay (E1 R5): no progress, no time, no stale date; only the token, the event, the priority
    # and the moment of sending
    stamp = int(now.timestamp())
    assert la_rules.relay_activity("ab" * 32, "update", 5, stamp) == {
        "token": "ab" * 32, "event": "update", "priority": 5, "timestamp": stamp
    }  # fmt: skip
    assert (
        la_rules.relay_activity("ab" * 32, "start", 10, stamp)["attributesType"] == la_rules.ATTRIBUTES_TYPE
    )
    assert la_rules.relay_activity("ab" * 32, "end", 10, stamp)["dismissalDate"] == stamp


VECTORS_RELAY = json.loads((data_root() / "fixtures" / "pairing" / "vectors.json").read_text())["relay"]


def test_the_relay_mode_live_activity_vector_and_its_refused_variants(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    """`relay.liveActivityRelayMode`: what this server sends is exactly the vector's shape; and
    `relay.liveActivityRelayModeRefused`: the sender refuses a stale date, a progress or a time, sending
    nothing."""
    good = VECTORS_RELAY["liveActivityRelayMode"]
    a = good["activity"]
    assert la_rules.relay_activity(a["token"], a["event"], a["priority"], a["timestamp"]) == a
    body = json.loads(push_seal.relay_request(good["handle"], good["type"], activity=a))
    assert body == good
    relay: FakeRelay = relayed["relay"]

    async def go(pool: Any, push: PushService) -> list[str]:
        refused = []
        async with pool.connection() as conn:
            for case in VECTORS_RELAY["liveActivityRelayModeRefused"]:
                with pytest.raises(ValueError, match="carries no"):
                    await push.send_relay_activity(conn, relayed["device_id"], case["activity"])
                refused.append(case["why"])
            await push.send_relay_activity(conn, relayed["device_id"], a)
        return refused

    assert len(run_with_relay(db, secrets_key, relay, go)) == 3
    (sent,) = relay.got  # only the good one reached the relay
    assert sent["activity"] == a


CONTENT = {"kind": "event", "title": "Cena da Marco", "start": "2026-10-20T12:30:00+02:00",
           "end": "2026-10-20T14:00:00+02:00", "place": "Via Immaginaria 1",
           "leave_at": "2026-10-20T12:10:00+02:00", "minutes": 20, "mode": "walk", "status": "planned",
           "late_min": 0}  # fmt: skip


def tick(db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]) -> int:
    """One minute of the Live Activity service, for a relay device with a running activity."""
    from pisa.services.live_activity import LiveActivityService

    content = CONTENT
    db.execute(
        "INSERT INTO device_live_activity (device_id, kind, token, updated_at) VALUES (%s, 'update', %s, "
        "now())",
        (relayed["device_id"], "cd" * 32),
    )
    plan = {
        "items": [
            {"kind": "event", "title": "Cena da Marco", "start": content["start"], "end": content["end"]}
        ]
    }

    class Planner:
        async def current(self, day: Any = None) -> dict[str, Any]:
            return plan

    class Follow:
        async def overlay(self, value: Any) -> Any:
            return value

    async def go(pool: Any, push: PushService) -> int:
        service = LiveActivityService(pool, push, Planner(), Follow(), owner=owner_at(),  # type: ignore[arg-type]
                                      clock=lambda: datetime(2026, 10, 20, 9, 0, tzinfo=UTC))  # fmt: skip
        service._content = lambda *a, **k: _async(content)  # type: ignore[method-assign]
        return await service.tick()

    return run_with_relay(db, secrets_key, relayed["relay"], go)


def test_the_live_activity_tick_sends_a_relay_device_only_a_progress_and_a_time(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    relay: FakeRelay = relayed["relay"]
    content = CONTENT
    assert tick(db, secrets_key, relayed) == 1
    (sent,) = relay.got
    assert sent["type"] == "liveactivity" and "sealed" not in sent
    activity = sent["activity"]
    sending = int(datetime(2026, 10, 20, 9, 0, tzinfo=UTC).timestamp())
    assert activity == {"token": "cd" * 32, "event": "update", "priority": activity["priority"],
                        "timestamp": sending}  # fmt: skip
    for personal in (b"Cena", b"Marco", b"Roma", b"walk"):
        assert personal not in relay.raw[0]
    # no time of the user's day, in any unit, and no share of the day done
    for moment in (content["start"], content["end"], content["leave_at"]):
        seconds = int(datetime.fromisoformat(moment).timestamp())
        assert str(seconds).encode() not in relay.raw[0] and str(seconds * 1000).encode() not in relay.raw[0]
    for field in (b"staleDate", b"progress", b'"time"'):  # never a stale date, a progress or a time (R5)
        assert field not in relay.raw[0]


@pytest.mark.parametrize(
    ("status", "activity_kept", "registration_kept"), [(422, False, True), (410, True, False)]
)
def test_a_dead_activity_token_drops_that_token_and_a_forgotten_handle_drops_the_registration(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any], status: int, activity_kept: bool,
    registration_kept: bool,
) -> None:  # fmt: skip
    """422 to a Live Activity push: that activity's token is dead, the device's relay registration stays.
    410: the relay forgot the handle, so the registration goes (and the activity's token stays for later)."""
    relayed["relay"].answer = status
    assert tick(db, secrets_key, relayed) == 0
    assert relayed["relay"].got[0]["type"] == "liveactivity"
    tokens = db.execute(
        "SELECT count(*) FROM device_live_activity WHERE device_id = %s", (relayed["device_id"],)
    )
    assert tokens.fetchone() == ((1,) if activity_kept else (0,))
    registration = db.execute(
        "SELECT count(*) FROM device_push_relay WHERE device_id = %s", (relayed["device_id"],)
    )
    assert registration.fetchone() == ((1,) if registration_kept else (0,))


def test_a_401_to_a_live_activity_push_is_logged_and_keeps_the_registration(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    """A Live Activity token the app never registered with the relay gets 401: the app's job (it registers its
    activity tokens itself); the server logs it and keeps the handle and the token."""
    relayed["relay"].answer = 401
    assert tick(db, secrets_key, relayed) == 0
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM device_live_activity").fetchone() == (1,)


def test_a_422_to_a_notice_is_a_failure_and_keeps_the_registration(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    relayed["relay"].answer = 422

    async def go(pool: Any, push: PushService) -> Any:
        async with pool.connection() as conn:
            return await push.send_relay_notice(conn, relayed["device_id"], NOTICE)

    assert run_with_relay(db, secrets_key, relayed["relay"], go).status == "failed"
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (1,)


RELAY_ANSWERS = json.loads((data_root() / "fixtures" / "pairing" / "vectors.json").read_text())["relay"][
    "answers"
]
OUTCOMES = {"sent": "sent", "log": "failed", "delete_registration": "token_invalid",
            "drop_activity_token": "activity_invalid"}  # fmt: skip
# D-139: the vectors (CONTRACT-2 §9) allow a relay to answer 404 for a gone handle; Pisa's relay never does
# (`relay/pisa_relay/app.py`: 410 only), so a 404 is a mistyped relay URL or a proxy: logged, never "gone".
SERVER_READS = {404: "log"}


@pytest.mark.parametrize("row", RELAY_ANSWERS, ids=lambda r: f"{r['status']}-{r['type']}")
def test_every_relay_answer_of_the_contract(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any], row: dict[str, Any]
) -> None:
    """The vectors' `relay.answers`: only 410 deletes the registration (404 too in the vectors: see
    SERVER_READS); 422 to a Live Activity push is that activity's token alone (the Live Activity service then
    drops it); anything else is logged."""
    row = row | {"action": SERVER_READS.get(row["status"], row["action"])}
    relayed["relay"].answer = row["status"]
    activity = {
        "token": "cd" * 32,
        "event": "update",
        "priority": 5,
        "timestamp": 1,
    }

    async def go(pool: Any, push: PushService) -> Any:
        async with pool.connection() as conn:
            if row["type"] == "alert":
                return await push.send_relay_notice(conn, relayed["device_id"], NOTICE)
            if row["type"] == "liveactivity":
                return await push.send_relay_activity(conn, relayed["device_id"], activity)
            # Pisa sends no background wakes: a 410 to one of its notices is the same gone handle
            return await push.send_relay_notice(conn, relayed["device_id"], NOTICE)

    outcome = run_with_relay(db, secrets_key, relayed["relay"], go)
    assert outcome.status == OUTCOMES[row["action"]]
    kept = db.execute("SELECT count(*) FROM device_push_relay").fetchone()
    assert kept == ((0,) if row["action"] == "delete_registration" else (1,))


@pytest.mark.parametrize("status", [200, 204, 404, 500])
def test_only_the_relay_s_own_gone_drops_a_registration(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any], status: int
) -> None:
    """A 404 (a mistyped relay URL, a proxy) or any answer the relay never gives is a failure, logged: every
    device's registration stays (D-139)."""
    relayed["relay"].answer = status

    async def go(pool: Any, push: PushService) -> Any:
        async with pool.connection() as conn:
            return await push.send_relay_notice(conn, relayed["device_id"], NOTICE)

    assert run_with_relay(db, secrets_key, relayed["relay"], go).status == "failed"
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (1,)


@pytest.mark.parametrize(
    ("answer", "status"), [(204, "sent"), (410, "sent"), (404, "failed"), (200, "failed")]
)
def test_forget_is_done_only_on_the_relay_s_own_answers(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any], answer: int, status: str
) -> None:
    """`DELETE /v1/register`: 204 forgotten, 410 gone already; a 404 is not the relay's and leaves the handle
    live, so it is a failure, logged (D-139)."""
    relayed["relay"].forget_answer = answer

    async def go(pool: Any, push: PushService) -> Any:
        assert push.relay is not None
        return await push.relay.forget(relayed["secret"], relayed["handle"])

    assert run_with_relay(db, secrets_key, relayed["relay"], go).status == status


def test_the_relay_s_answers_are_one_table() -> None:
    assert push_seal.relay_meaning("push", 410) == "gone" and push_seal.relay_meaning("push", 404) is None
    assert push_seal.relay_meaning("forget", 410) == "gone" and push_seal.relay_meaning("forget", 404) is None
    assert push_seal.relay_meaning("push", 202) == "done" and push_seal.relay_meaning("forget", 204) == "done"


@pytest.mark.parametrize("how", ["none", "direct", "relay again"])
def test_leaving_relay_mode_tells_the_relay_to_forget_the_handle(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any], how: str
) -> None:
    async def go(pool: Any, push: PushService) -> None:
        if how == "none":
            await push.unregister(relayed["device_id"])
        elif how == "direct":
            await push.register(relayed["device_id"], "ab" * 32, "production")
        else:  # the app registered again: its new handle replaces the old one
            await push.register_relay(relayed["device_id"], "h-new", bytes(32), bytes(32))

    run_with_relay(db, secrets_key, relayed["relay"], go)
    assert relayed["relay"].forgotten == [relayed["handle"]] and relayed["relay"].refused == []
    left = [r[0] for r in db.execute("SELECT handle FROM device_push_relay").fetchall()]
    assert left == (["h-new"] if how == "relay again" else [])


def with_fake_relay(client: Any, relay: FakeRelay) -> None:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app()), base_url=RELAY)
    client.app.state.runtime.push.relay = RelayClient(http, RELAY)


@pytest.mark.parametrize("answer", [204, 503])
def test_unpairing_a_relay_device_makes_the_relay_forget_it_best_effort(
    client: Any, db: psycopg.Connection, relayed: dict[str, Any], answer: int
) -> None:
    """E1 R13: `DELETE /v1/devices/me` signs `DELETE /v1/register` for the device's handle before its own row
    goes; a relay that fails is logged and never stops the unpairing."""
    relay: FakeRelay = relayed["relay"]
    relay.forget_answer = answer
    with_fake_relay(client, relay)
    gone = client.delete("/v1/devices/me", headers={"Authorization": f"Bearer {relayed['token']}"})
    assert gone.status_code == 204
    assert relay.forgotten == ([relayed["handle"]] if answer == 204 else [])
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (0,)


def test_purging_the_smoke_device_makes_the_relay_forget_its_handle(
    db: psycopg.Connection, secrets_key: str
) -> None:
    """D-139: `pisa devices purge` ends the device as every other path does (`devices.end`): revoked, then the
    relay forgets its handle, then everything it made goes."""
    from pisa.services.devices import PURGE_NAME, PURGE_PLATFORM, purge_device

    device_id, _ = pair(db, PURGE_NAME, PURGE_PLATFORM)
    secret, handle = bytes(range(32)), "h-" + uuid.uuid4().hex[:12]
    register(db, secrets_key, device_id, handle, secret, device_keys()[1])
    relay = FakeRelay()
    relay.register(handle, secret)

    async def go(pool: Any, push: PushService) -> None:
        async with pool.connection() as conn:
            await purge_device(
                conn, uuid.UUID(device_id), datetime.now(UTC), keys=push.keys, relay=push.relay
            )

    run_with_relay(db, secrets_key, relay, go)
    assert relay.forgotten == [handle] and relay.refused == []
    assert db.execute("SELECT count(*) FROM devices WHERE id = %s", (device_id,)).fetchone() == (0,)


def test_revoking_a_relay_device_from_another_makes_the_relay_forget_it(
    client: Any, db: psycopg.Connection, relayed: dict[str, Any]
) -> None:
    relay: FakeRelay = relayed["relay"]
    with_fake_relay(client, relay)
    _, other = pair(db, "iPad")
    revoked = client.delete(
        f"/v1/devices/{relayed['device_id']}", headers={"Authorization": f"Bearer {other}"}
    )
    assert revoked.status_code == 204
    assert relay.forgotten == [relayed["handle"]]
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (0,)


def test_the_cli_s_revocation_makes_the_relay_forget_it(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    from pisa.services.devices import revoke_device
    from pisa.services.push import forget_at_relay

    async def go(pool: Any, push: PushService) -> None:
        async with pool.connection() as conn:
            await revoke_device(conn, uuid.UUID(relayed["device_id"]))
            await forget_at_relay(conn, KeyStore(SecretStr(secrets_key)), push.relay, relayed["device_id"])

    run_with_relay(db, secrets_key, relayed["relay"], go)
    assert relayed["relay"].forgotten == [relayed["handle"]]


def test_phone_keys_reset_makes_the_relay_forget_every_device_it_revokes(
    db: psycopg.Connection, app_settings: Any, relayed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security review E1 P4: `pisa phone-keys reset --keep` revokes every other device, and the relay is
    asked to forget each one's handle once the revocation is committed (before the fix, their rows stayed)."""
    from pisa import cli

    relay: FakeRelay = relayed["relay"]
    keep, _ = pair(db, "iPhone")
    settings = app_settings.model_copy(update={"push_relay_url": RELAY})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli,
        "_http_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=relay.app()), base_url=RELAY),
    )
    assert cli.main(["phone-keys", "reset", "--keep", keep]) == 0
    assert relay.forgotten == [relayed["handle"]] and relay.refused == []
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (0,)
    live = db.execute("SELECT id::text FROM devices WHERE revoked_at IS NULL").fetchall()
    assert live == [(keep,)]


def test_an_exact_repeat_is_success_and_sent_once(
    db: psycopg.Connection, secrets_key: str, relayed: dict[str, Any]
) -> None:
    """The vectors' `relay.exactRepeat`: the same signature (same second, same body) is the same request,
    answered
    202 and sent once; the sender counts it as sent and does not retry."""
    repeat = json.loads((data_root() / "fixtures" / "pairing" / "vectors.json").read_text())["relay"][
        "exactRepeat"
    ]
    relay: FakeRelay = relayed["relay"]

    async def go(pool: Any, push: PushService) -> list[Any]:
        push.relay = RelayClient(push.relay._http, RELAY, clock=lambda: 1_800_000_000.0)  # type: ignore[union-attr]
        relay.clock = lambda: 1_800_000_000.0
        # the same body twice (a background request is unsealed, so its bytes repeat), through the
        # same sender the notices use
        body = push_seal.relay_request(relayed["handle"], "background")
        return [await push.relay.send(relayed["secret"], body, "background") for _ in range(2)]  # type: ignore[union-attr]

    outcomes = run_with_relay(db, secrets_key, relay, go)
    assert [o.status for o in outcomes] == ["sent", "sent"] and (repeat["first"], repeat["repeat"]) == (
        202,
        202,
    )
    assert len(relay.got) == repeat["sentToApple"] and relay.repeats == 1


def test_the_server_s_delete_vector_is_signed_as_a_push_is() -> None:
    """`relay.appApi.deleteByTheServer` (the app's own registration calls are the app's, signed with its app
    secret): the server's `DELETE /v1/register` is byte for byte the vector, body and headers."""
    relay = json.loads((data_root() / "fixtures" / "pairing" / "vectors.json").read_text())["relay"]
    delete = relay["appApi"]["deleteByTheServer"]
    stamp = int(delete["headers"]["X-Pisa-Relay-Timestamp"])
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(204)

    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = RelayClient(http, RELAY, clock=lambda: float(stamp))
            return await client.forget(
                bytes.fromhex(relay["secretHex"]), json.loads(delete["body"])["handle"]
            )

    assert asyncio.run(go()).status == "sent"
    (request,) = sent
    assert request.method == "DELETE" and request.content == delete["body"].encode()
    assert {k: request.headers[k] for k in delete["headers"]} == delete["headers"]
    assert (
        len(push_seal.relay_request("h" * 64, "alert", sealed="A" * 2731, collapse_id="c" * 64))
        < relay["limits"]["pushBodyMaxBytes"]
    )  # the largest push this server sends is far below the relay's limit


async def _async(value: Any) -> Any:
    return value


def test_discovery_and_readyz_say_whether_the_relay_is_set(
    app_settings: Any, db: Any, fake_models: Any, clock: Any
) -> None:
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.api.server import discovery
    from pisa.services.runtime import Runtime

    for url in ("", RELAY):
        settings = app_settings.model_copy(update={"push_relay_url": url})

        async def factory(s: Any) -> Any:
            return await Runtime.start(s, model_factory=fake_models, clock=clock)

        with TestClient(create_app(settings, runtime_factory=factory)) as client:
            runtime = client.app.state.runtime  # type: ignore[attr-defined]
            doc = client.portal.call(discovery, runtime, "https", "pisa.local")  # type: ignore[union-attr]
            ready = client.get("/readyz").json()
        assert errors(doc, EXT_ID + "#/$defs/ServerDiscovery") == [] and discovery_problems(doc) == []
        assert doc["push"]["relayConfigured"] is bool(url)
        assert ("relay" in doc["push"]["modes"]) is bool(url) and "none" in doc["push"]["modes"]
        assert ready["relay"] == ("on" if url else "off")
        assert doc["ai"]["disclosureRevision"]
