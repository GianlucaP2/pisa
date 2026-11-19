"""Contract 2's privacy revision (CONTRACT-2 §3.4, §4.3-§4.5), end to end over REST and
`/v2/live`:
- QR pairing: the QR's shape and no long-term secret in it; the app's HMAC, the server's HMAC, the same SAS on
  both sides; confirmation on the server side only; a token used once; wrong requests never spend a code;
  refused and expired pairings; the typed code;
- session tokens from the device key: nonces (60 s, single use, at most 5 per address, spent even when the
  session fails, only hashes stored), 1 h tokens (only hashes stored), `session_expired` on REST and as
  `/v2/live`'s close reason, also for a socket that outlives its token; only a session token authenticates;
- unpair and delete (`DELETE /v1/devices/me`) and `device_revoked` on REST and the socket;
- `GET /v1/server/endpoints` and `pisa.endpoints` after a change (the next pin of a rotation);
- the AI disclosure, the consent the server enforces before any AI call (and health's own), and
  `pisa.ai.disclosure` when a slot changes;
- the notice privacy classes and their generic text.
Every response is checked against the delta's schema, every stream with the conformance kit."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pisa_conformance.reference.consent import check_run
from pisa_conformance.validate import EXT_ID, errors
from starlette.websockets import WebSocketDisconnect

from pisa.domain import notice as notice_rules
from pisa.domain import pairing as rules
from pisa.services import pairing
from tests.conftest import FakeModels, dsn, pair, pair_v2, pair_v2_key, seed_catalog, session_for, set_key
from tests.test_live_v2_socket import connect, hello, opened, run
from tests.test_openai_compat import _certificates
from tests.test_openapi_v2_delta import delta_body_errors

ADDRESSES = "https://pisa-home.local:8443,https://[2001:db8::1]:8443,relay://relay.example.net/h-123"


@pytest.fixture
def tls(tmp_path: Path) -> dict[str, str]:
    """The server's TLS certificate (made here) and the next key of a rotation, as PEM files."""
    _, cert, _ = _certificates(tmp_path)
    nxt = ec.generate_private_key(ec.SECP256R1()).public_key()
    next_path = tmp_path / "next.pem"
    next_path.write_bytes(
        nxt.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return {
        "cert": str(cert),
        "next": str(next_path),
        "pin": rules.spki_pin(cert.read_text()),
        "next_pin": rules.spki_pin(next_path.read_text()),
    }


@pytest.fixture
def app_settings(app_settings: Any, tls: dict[str, str]) -> Any:
    return app_settings.model_copy(update={"tls_cert": tls["cert"], "tls_next_key": tls["next"],
                                           "addresses": ADDRESSES})  # fmt: skip


def on_db(db: psycopg.Connection, go: Any) -> Any:
    async def main() -> Any:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await go(conn)

    return asyncio.run(main())


def make_token(
    db: psycopg.Connection, secrets_key: str, label: str = "iPhone", **kw: Any
) -> pairing.PairToken:
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore

    return on_db(db, lambda c: pairing.create_token(c, KeyStore(SecretStr(secrets_key)), label, **kw))


def b64(der: bytes) -> str:
    return rules.b64url(der)


class Phone:
    """The app's side of pairing: its device key, nonce and transcript."""

    def __init__(self, token: str, spki: str, name: str = "iPhone", platform: str = "ios") -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        der = self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.device_key, self.nonce, self.token = b64(der), rules.new_nonce(), rules.token_bytes(token)
        self.text = rules.transcript(spki, self.device_key, self.nonce)
        self.begun: dict[str, Any] = {}
        self.body = {"v": 1, "deviceKey": self.device_key, "name": name, "platform": platform,
                     "clientNonce": self.nonce, "mac": rules.client_mac(self.token, self.text)}  # fmt: skip

    def sign(self, device_id: str, nonce: str) -> str:
        return b64(self.key.sign(rules.session_text(device_id, nonce), ec.ECDSA(hashes.SHA256())))


def send(client: Any, phone: Phone) -> Any:
    """`POST /v1/pair/begin` (the server's commitment), then `POST /v1/pair` with its pairing id; the first
    answer that is not a success."""
    begun = client.post("/v1/pair/begin")
    if begun.status_code != 201:
        return begun
    phone.begun = begun.json()
    return client.post("/v1/pair", json=phone.body | {"pairingId": phone.begun["pairingId"]})


def session(client: Any, phone: Phone, device_id: str) -> Any:
    nonce = client.get("/v1/devices/session/nonce").json()["nonce"]
    return client.post(
        "/v1/devices/session",
        json={"deviceId": device_id, "nonce": nonce, "signature": phone.sign(device_id, nonce)},
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- QR pairing ---------------------------------------------------------------------------------------------
def test_the_qr_carries_the_addresses_pin_token_and_expiry_and_no_long_term_secret(
    tls: dict[str, str],
) -> None:
    token = rules.new_token()
    uri = rules.qr_uri(ADDRESSES.split(","), tls["pin"], token, 1_800_000_000)
    qr = rules.parse_qr(uri)
    assert errors(qr, EXT_ID + "#/$defs/PairingQR") == []
    assert qr["addrs"] == ADDRESSES.split(",") and qr["spki"] == tls["pin"] and qr["token"] == token
    assert len(token) == 26 and len(rules.token_bytes(token)) == 16
    assert rules.parse_qr(token.lower()[:13] + "-" + token.lower()[13:]) == {"token": token}  # typed
    comma = "https://a.example:8443/x,y"
    assert rules.parse_qr(rules.qr_uri([comma], tls["pin"], token, 1))["addrs"] == [comma]


def test_pairing_with_hmac_and_sas_confirmed_on_the_server_then_a_session(
    client: Any, db: psycopg.Connection, secrets_key: str, tls: dict[str, str]
) -> None:
    made = make_token(db, secrets_key)
    phone = Phone(made.token, tls["pin"])
    answer = send(client, phone)
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert delta_body_errors("post", "/v1/pair/begin", "201", phone.begun) == []
    assert delta_body_errors("post", "/v1/pair", "201", body) == []
    assert rules.reveal_matches(
        phone.begun["commitment"], body["serverNonce"]
    )  # the nonce committed to first
    assert body["state"] == "awaiting_confirmation"
    # the server proves it holds the token too; both sides derive the same 6 digits
    assert rules.macs_equal(body["mac"], rules.server_mac(phone.token, phone.text, body["serverNonce"]))
    sas = rules.sas(phone.text, body["serverNonce"])
    (waiting,) = on_db(db, lambda c: pairing.waiting(c, token_id=made.id))
    assert waiting.sas == sas and waiting.name == "iPhone" and waiting.platform == "ios"
    # no device and no session until the user confirms on the server
    assert db.execute("SELECT count(*) FROM devices").fetchone() == (0,)
    assert session(client, phone, body["deviceId"]).json()["error"]["code"] == "auth_failed"
    state = client.get(f"/v1/pair/{body['pairingId']}")
    assert delta_body_errors("get", "/v1/pair/{pairingId}", "200", state.json()) == []
    assert state.json()["state"] == "awaiting_confirmation"
    on_db(db, lambda c: pairing.decide(c, body["pairingId"], True))
    assert client.get(f"/v1/pair/{body['pairingId']}").json()["state"] == "confirmed"
    row = db.execute("SELECT device_key, paired_via, platform FROM devices").fetchone()
    assert row is not None and row[1] == "host" and row[2] == "ios"
    assert bytes(row[0]) == rules.unb64(phone.device_key, "k")
    made_session = session(client, phone, body["deviceId"])
    assert made_session.status_code == 200
    assert delta_body_errors("post", "/v1/devices/session", "200", made_session.json()) == []
    token = made_session.json()["token"]
    assert client.get("/v1/devices", headers=auth(token)).status_code == 200
    with connect(client, token) as ws:
        opened(ws, hello(body["deviceId"]))
    # nothing secret is kept in the clear: the pairing token is erased once used, sessions are hashes
    assert db.execute("SELECT token FROM pair_tokens").fetchone() == (None,)
    hashes_kept = [r[0] for r in db.execute("SELECT token_hash FROM device_sessions").fetchall()]
    assert token not in hashes_kept and rules.unb64(token, "t") and all(len(h) == 64 for h in hashes_kept)


def test_the_typed_code_pairs_with_the_pin_of_the_key_the_server_presented(
    client: Any, db: psycopg.Connection, secrets_key: str, tls: dict[str, str]
) -> None:
    made = make_token(db, secrets_key)
    for pin in (tls["next_pin"], tls["pin"]):  # the next key of a rotation is the server's too
        phone = Phone(made.token if pin == tls["pin"] else make_token(db, secrets_key).token, pin)
        assert send(client, phone).status_code == 201


def test_a_wrong_mac_a_used_token_and_a_foreign_pin_are_refused_and_never_spend_or_lock_a_code(
    client: Any, db: psycopg.Connection, secrets_key: str, tls: dict[str, str]
) -> None:
    """CONTRACT-2 §4.3 step 5 (E1 P2): a failed request names no token, so it never touches one and locks no
    address out; it counts only toward the address backstop (`test_pairing_lockout.py`)."""
    made = make_token(db, secrets_key)
    first = Phone(made.token, tls["pin"])
    assert send(client, first).status_code == 201
    again = send(client, Phone(made.token, tls["pin"]))  # single use
    assert again.status_code == 400 and again.json()["error"]["code"] == "pairing_code_invalid"
    fresh = make_token(db, secrets_key)
    in_between = Phone(fresh.token, "A" * 43 + "=")  # a key in between: not the server's pin
    assert send(client, in_between).status_code == 400
    wrong = Phone(rules.new_token(), tls["pin"])
    for _ in range(8):  # more than the 5 that once locked the address out
        refused = send(client, wrong)
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "pairing_code_invalid"
    assert send(client, Phone(fresh.token, tls["pin"])).status_code == 201  # the code is still the user's


def test_a_refused_pairing_makes_no_device_and_an_unanswered_one_expires(
    client: Any, db: psycopg.Connection, secrets_key: str, tls: dict[str, str]
) -> None:
    refused = send(client, Phone(make_token(db, secrets_key).token, tls["pin"])).json()
    assert on_db(db, lambda c: pairing.decide(c, refused["pairingId"], False)) is None
    assert client.get(f"/v1/pair/{refused['pairingId']}").json()["state"] == "refused"
    late = send(client, Phone(make_token(db, secrets_key).token, tls["pin"])).json()
    db.execute(
        "UPDATE pairings SET expires_at = now() - interval '1 second' WHERE id = %s", (late["pairingId"],)
    )
    assert client.get(f"/v1/pair/{late['pairingId']}").json()["state"] == "expired"
    with pytest.raises(Exception, match="no longer waiting"):
        on_db(db, lambda c: pairing.decide(c, late["pairingId"], True))
    assert db.execute("SELECT count(*) FROM devices").fetchone() == (0,)
    assert client.get("/v1/pair/pair_unknown_unknown").status_code == 404


def test_without_a_tls_pin_there_is_no_qr_pairing(
    app_settings: Any, db: Any, fake_models: Any, clock: Any, secrets_key: str
) -> None:
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    async def factory(s: Any) -> Any:
        return await Runtime.start(s, model_factory=fake_models, clock=clock)

    settings = app_settings.model_copy(update={"tls_cert": None, "addresses": ""})
    with TestClient(create_app(settings, runtime_factory=factory)) as bare:
        phone = Phone(rules.new_token(), "A" * 43 + "=")
        assert send(bare, phone).status_code == 404
        _, token = pair_v2(db)
        assert bare.get("/v1/server/endpoints", headers=auth(token)).status_code == 404


# --- session tokens -----------------------------------------------------------------------------------------
def test_nonces_are_single_use_spent_on_failure_and_at_most_5_per_address(
    client: Any, db: psycopg.Connection
) -> None:
    device_id, _, key = pair_v2_key(db)
    phone = Phone(rules.new_token(), "x" * 8)
    phone.key = key
    nonces = [client.get("/v1/devices/session/nonce") for _ in range(5)]
    assert all(n.status_code == 200 for n in nonces)
    assert delta_body_errors("get", "/v1/devices/session/nonce", "200", nonces[0].json()) == []
    over = client.get("/v1/devices/session/nonce")
    assert over.status_code == 429 and over.json()["error"]["code"] == "rate_limited"
    first = nonces[0].json()["nonce"]
    bad = client.post(
        "/v1/devices/session", json={"deviceId": device_id, "nonce": first, "signature": b64(b"x" * 70)}
    )
    assert bad.status_code == 401
    retry = client.post(  # the right signature now, but the nonce was spent by the failed attempt
        "/v1/devices/session",
        json={"deviceId": device_id, "nonce": first, "signature": phone.sign(device_id, first)},
    )
    assert retry.status_code == 401 and retry.json()["error"]["code"] == "auth_failed"
    second = nonces[1].json()["nonce"]
    good = {"deviceId": device_id, "nonce": second, "signature": phone.sign(device_id, second)}
    assert client.post("/v1/devices/session", json=good).status_code == 200
    assert client.post("/v1/devices/session", json=good).status_code == 401  # single use
    assert client.get("/v1/devices/session/nonce").status_code == 200  # two were used: room again
    stored = {r[0] for r in db.execute("SELECT nonce_hash FROM session_nonces").fetchall()}
    assert first not in stored and second not in stored
    late = nonces[2].json()["nonce"]
    db.execute("UPDATE session_nonces SET expires_at = now() - interval '1 second'")
    expired = {"deviceId": device_id, "nonce": late, "signature": phone.sign(device_id, late)}
    assert client.post("/v1/devices/session", json=expired).status_code == 401


def test_an_expired_session_is_401_session_expired_and_closes_the_socket_with_that_reason(
    client: Any, db: psycopg.Connection
) -> None:
    device_id, token, key = pair_v2_key(db)
    db.execute("UPDATE device_sessions SET expires_at = now() - interval '1 second'")
    expired = client.get("/v1/devices", headers=auth(token))
    assert expired.status_code == 401 and expired.json()["error"]["code"] == "session_expired"
    with connect(client, token) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4003 and closed.value.reason == "session_expired"
    # a socket outlives nothing: it closes when its token's hour is over
    fresh = session_for(db, device_id, key)
    db.execute("UPDATE device_sessions SET expires_at = now() + interval '1 second' WHERE expires_at > now()")
    with connect(client, fresh) as ws:
        opened(ws, hello(device_id))
        with pytest.raises(WebSocketDisconnect) as ended:
            while True:
                ws.receive_json()
    assert ended.value.code == 4003 and ended.value.reason == "session_expired"


def test_only_a_session_token_authenticates_on_every_route(client: Any, db: psycopg.Connection) -> None:
    """A bearer that is not one of this server's session tokens (a v1 device token, a guess) is `auth_failed`
    on the live socket, on `/v2/runs` and on the `/v1` routes alike."""
    pair(db)
    with connect(client, "not-a-session-token") as ws:
        refused = ws.receive_json()
        assert refused["control"] == "error" and refused["code"] == "auth_failed"
    body = run("ciao")["input"]
    stranger = auth("not-a-session-token")
    assert client.post("/v2/runs", json=body, headers=stranger).status_code == 401
    refused_rest = client.get("/v1/devices", headers=stranger)
    assert refused_rest.status_code == 401 and refused_rest.json()["error"]["code"] == "auth_failed"


# --- unpair and delete, device_revoked ----------------------------------------------------------------------
def test_unpair_and_delete_revokes_and_deletes_keys_push_and_live_activity(
    client: Any, db: psycopg.Connection
) -> None:
    device_id, token = pair_v2(db)
    other_id, other_token = pair_v2(db, "iPad")
    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, label, device_id, created_at) VALUES ('k1', %s, 'x', "
        "%s, now())",
        (b"\x04" + b"\x01" * 64, device_id),
    )
    db.execute(
        "UPDATE devices SET apns_token = %s, apns_environment = 'production' WHERE id = %s",
        ("ab" * 32, device_id),
    )
    db.execute(
        "INSERT INTO device_live_activity (device_id, kind, token, updated_at) VALUES (%s, 'update', %s, "
        "now())",
        (device_id, "cd" * 32),
    )
    with connect(client, token) as ws:
        opened(ws, hello(device_id))
        gone = client.delete("/v1/devices/me", headers=auth(token))
        assert gone.status_code == 204
        frames = []
        with pytest.raises(WebSocketDisconnect):
            while True:
                frames.append(ws.receive_json())
    assert any(f.get("control") == "error" and f.get("code") == "device_revoked" for f in frames)
    row = db.execute(
        "SELECT revoked_at IS NOT NULL, apns_token FROM devices WHERE id = %s", (device_id,)
    ).fetchone()
    assert row == (True, None)
    for table in ("phone_keys", "device_push_relay", "device_live_activity"):
        assert db.execute(f"SELECT count(*) FROM {table} WHERE device_id = %s", (device_id,)).fetchone() == (
            0,
        )
    after = client.get("/v1/devices", headers=auth(token))
    assert after.status_code == 401 and after.json()["error"]["code"] == "device_revoked"
    with connect(client, token) as ws:
        refused = ws.receive_json()
    assert refused["control"] == "error" and refused["code"] == "device_revoked"
    assert client.get("/v1/devices", headers=auth(other_token)).status_code == 200  # the other device stays
    assert other_id != device_id


# --- endpoints ----------------------------------------------------------------------------------------------
def test_endpoints_list_the_addresses_and_both_pins_and_a_change_reaches_the_socket(
    client: Any, db: psycopg.Connection, tls: dict[str, str]
) -> None:
    device_id, token = pair_v2(db)
    answer = client.get("/v1/server/endpoints", headers=auth(token))
    assert answer.status_code == 200
    doc = answer.json()
    assert errors(doc, EXT_ID + "#/$defs/s_pisa_endpoints") == []
    assert delta_body_errors("get", "/v1/server/endpoints", "200", doc) == []
    assert doc["spki"] == [tls["pin"], tls["next_pin"]]
    assert [e["kind"] for e in doc["endpoints"]] == ["lan", "ipv6", "relay"]
    assert doc["endpoints"][2] == {"kind": "relay", "address": "relay.example.net", "relayHandle": "h-123"}
    db.execute("UPDATE devices SET endpoints_told = 'an-older-one' WHERE id = %s", (device_id,))
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        frames = app.until(lambda f: f.get("event", {}).get("name") == "pisa.endpoints")
        app.until(lambda f: f.get("event", {}).get("type") == "RUN_FINISHED")
        app.check()
    told = next(f["event"]["value"] for f in frames if f.get("event", {}).get("name") == "pisa.endpoints")
    assert told == doc and frames[-1]["threadId"] == f"pisa-notices:{device_id}"
    assert db.execute("SELECT endpoints_told FROM devices WHERE id = %s", (device_id,)).fetchone() == (
        doc["revision"],
    )


# --- AI disclosure and consent ------------------------------------------------------------------------------
def test_the_disclosure_names_providers_models_and_categories_and_its_revision_tracks_them(
    client: Any, db: psycopg.Connection
) -> None:
    _, token = pair_v2(db)
    doc = client.get("/v1/ai/disclosure", headers=auth(token)).json()
    assert errors(doc, EXT_ID + "#/$defs/AIDisclosure") == []
    assert delta_body_errors("get", "/v1/ai/disclosure", "200", doc) == []
    conversation = next(p for p in doc["processors"] if p["slot"] == "chat.conversation")
    assert conversation["provider"] == {"id": "openai", "name": "OpenAI", "kind": "cloud"}
    assert "health" in conversation["dataCategories"] and conversation["model"] == "gpt-6-luna"
    db.execute("UPDATE model_slots SET model_ref = 'google:gemini-3.5-flash-lite' WHERE id = 'intake.triage'")
    assert client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"] != doc["revision"]


def test_the_research_slot_discloses_what_its_engines_send() -> None:
    """`places.research` also runs the curiosity, the ideas, the desk and il prurito: their words and their
    calendar go to it, besides places. Every category is the wire's own."""
    from pisa.domain.disclosure import SLOT_CATEGORIES

    schema = json.loads(
        (Path(__file__).parents[2] / "protocol/v2/schema/pisa-contract-2.schema.json").read_text()
    )
    processors = schema["$defs"]["AIDisclosure"]["properties"]["processors"]["items"]["properties"]
    allowed = set(processors["dataCategories"]["items"]["enum"])
    assert SLOT_CATEGORIES["places.research"] == ("places", "conversation", "calendar")
    assert all(set(categories) <= allowed for categories in SLOT_CATEGORIES.values())


def test_consent_is_recorded_only_for_the_current_revision(client: Any, db: psycopg.Connection) -> None:
    device_id, token = pair_v2(db, consent=False)
    revision = client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"]
    stale = client.post("/v1/ai/consent", json={"revision": "0" * 32, "health": False}, headers=auth(token))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "ai_consent_required"
    ok = client.post("/v1/ai/consent", json={"revision": revision, "health": True}, headers=auth(token))
    assert ok.status_code == 204
    row = db.execute("SELECT ai_consent_revision, ai_consent_health FROM devices WHERE id = %s", (device_id,))
    assert row.fetchone() == (revision, True)


def finished(client: Any, token: str, text: str, **pisa: Any) -> dict[str, Any]:
    body = run(text, **pisa)["input"]
    answer = client.post("/v2/runs", json=body, headers=auth(token) | {"Accept": "application/json"})
    assert answer.status_code == 200, answer.text
    return answer.json()["events"][-1]


def test_a_run_without_current_consent_ends_ai_consent_required_before_any_ai_call(
    client: Any, db: psycopg.Connection, fake_models: FakeModels, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    _, token = pair_v2(db, consent=False)
    revision = client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"]
    disclosed = {"revision": revision}
    cases = [
        ({}, "ciao", False),  # no consent at all
        ({"consent": {"aiRevision": "0" * 32}}, "ciao", False),  # an older revision: ask again
        ({"consent": {"aiRevision": revision}}, "ciao", True),
        (
            {"consent": {"aiRevision": revision}},
            "sono stato dal dentista ieri?",
            False,
        ),  # health needs its own
        ({"consent": {"aiRevision": revision, "health": True}}, "sono stato dal dentista ieri?", True),
    ]
    for props, text, allowed in cases:
        before = len(fake_models.seen_messages)
        end = finished(client, token, text, **props)
        assert (check_run(props, disclosed, "dentista" in text) is None) is allowed  # the kit's own rule
        if allowed:
            assert end["type"] == "RUN_FINISHED", (props, end)
        else:
            assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end
            assert len(fake_models.seen_messages) == before  # no AI call
    # the consent recorded for REST-started work (the watch) covers a run that carries none
    client.post("/v1/ai/consent", json={"revision": revision, "health": False}, headers=auth(token))
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"


def test_the_socket_refuses_a_turn_without_consent_and_is_told_when_the_disclosure_changes(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    device_id, token = pair_v2(db, consent=False)
    revision = client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"]
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        frame = run("ciao")
        app.send(frame)
        end = app.run_ends(frame["input"]["runId"])[-1]["event"]
        assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required"
        allowed = run("ciao", consent={"aiRevision": revision})
        app.send(allowed)
        last = app.run_ends(allowed["input"]["runId"])[-1]["event"]
        assert last["type"] == "RUN_FINISHED", last
        seed_catalog(db, [{"ref": "openai:brief-model", "input": 1.0, "output": 2.0}])
        changed = client.put(
            "/v1/ai/slots/briefing.compose", json={"model": "openai:brief-model", "fallback": None},
            headers=auth(token),
        )  # fmt: skip
        assert changed.status_code == 200, changed.text
        frames = app.until(lambda f: f.get("event", {}).get("name") == "pisa.ai.disclosure")
        app.until(lambda f: f.get("event", {}).get("type") == "RUN_FINISHED")
        app.check()
    value = frames[-1]["event"]["value"]
    assert value["revision"] != revision and errors(value, EXT_ID + "#/$defs/AIDisclosure") == []


# --- notice privacy -----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fields", "privacy"),
    [
        ({"text": "Tra 15 minuti: riunione."}, "none"),
        ({"text": "Tra 15 minuti: dentista."}, "health"),
        ({"text": "La settimana è pronta.", "ledger_week": "2026-W46"}, "finance"),
        ({"kind": "done", "text": "Fatto.", "place": "Via Immaginaria 1", "title": "Spesa"}, "place"),
        ({"text": "Un pensiero per Anna.", "ritual": "people"}, "people"),
        ({"text": "Sei arrivato.", "sensitive": True}, "place"),
    ],
)
def test_a_notice_carries_its_privacy_class_and_a_generic_line(fields: dict[str, Any], privacy: str) -> None:
    base = {"notification_id": str(uuid.uuid4()), "kind": "reminder", "priority": "normal", "lang": "it-IT"}
    value = notice_rules.value(base | fields, default_lang="it-IT")
    assert value["privacy"] == privacy
    assert value["genericText"] and value["text"] not in value["genericText"]
    assert errors(value, EXT_ID + "#/$defs/s_pisa_notice") == []
    english = notice_rules.value(base | fields | {"lang": "en-US"}, default_lang="it-IT")["genericText"]
    assert english != value["genericText"] and json.dumps(english).isascii()
