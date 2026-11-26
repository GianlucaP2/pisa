"""Pairing with the server's commitment (CONTRACT-2 §4.3 step 3, §15 S1) and from a paired phone (§4.3.1):
- every value of the contract's vectors (`protocol/v2/fixtures/pairing/vectors.json`) recomputed by this
  server's own code: the pin, the QR and its typed forms, the transcript, both MACs, the commitment and its
  reveal, a reveal that does not match, the SAS, a MAC made with the next pin, the session signature, the
  relay's headers (their exact spelling) and both sealed pushes, byte for byte;
- the headless client aborts on a reveal that does not match the commitment, before it shows any digits;
- `POST /v1/pair/begin`: the commitment comes first, a begun pairing serves one request, an unknown or expired
  one is refused, and begins are limited per address; a begun pairing's nonce is never readable;
- a paired phone's offer: only it reads and confirms its pairing (signed, with the SAS it computed itself from
  the offer's transcript), never the server's CLI; a wrong SAS confirms nothing; withdrawing refuses."""

import asyncio
import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pisa_conformance.paths import data_root
from pyhpke import KEMKeyPair

from pisa import cli
from pisa.client import REVEAL_MISMATCH, Client, ClientError
from pisa.domain import guide, push_seal
from pisa.domain import pairing as rules
from pisa.services import pairing
from tests.conftest import dsn, pair_v2, pair_v2_key
from tests.test_openapi_v2_delta import DELTA, _inline, delta_body_errors
from tests.test_privacy_v2 import Phone, app_settings, send, tls  # noqa: F401 (fixtures: the TLS pin, the settings)

VECTORS = json.loads((data_root() / "fixtures" / "pairing" / "vectors.json").read_text(encoding="utf-8"))
PA, INPUTS = VECTORS["pairing"], VECTORS["pairing"]["inputs"]


# --- the contract's vectors ---------------------------------------------------------------------------------
def test_the_pairing_vectors_recompute_byte_for_byte() -> None:
    tls_spki = base64.b64decode(INPUTS["tlsSpkiDerBase64"])
    assert base64.b64encode(hashlib.sha256(tls_spki).digest()).decode() == PA["spkiPin"]
    token = bytes.fromhex(INPUTS["tokenHex"])
    assert base64.b32encode(token).decode().rstrip("=") == PA["token"]
    assert rules.qr_uri(INPUTS["addrs"], PA["spkiPin"], PA["token"], INPUTS["exp"]) == PA["qr"]
    assert rules.parse_qr(PA["qr"]) == {"v": 1, "addrs": INPUTS["addrs"], "spki": PA["spkiPin"],
                                        "token": PA["token"], "exp": INPUTS["exp"]}  # fmt: skip
    for typed in PA["typedVariants"]:
        assert rules.token_bytes(typed) == token and rules.parse_qr(typed) == {"token": PA["token"]}
    for bad in PA["rejects"]["typed"]:
        with pytest.raises(rules.PairingInputError):
            rules.token_bytes(bad)
    text = rules.transcript(PA["spkiPin"], INPUTS["deviceKey"], INPUTS["clientNonce"])
    assert text.decode() == PA["transcript"]
    assert rules.client_mac(token, text) == PA["appMac"]
    assert rules.server_mac(token, text, INPUTS["serverNonce"]) == PA["serverMac"]
    assert rules.commitment(INPUTS["serverNonce"]) == PA["commitment"]
    assert rules.reveal_matches(PA["commitment"], INPUTS["serverNonce"])
    mismatch = PA["revealMismatch"]
    assert mismatch["mustAbort"] and not rules.reveal_matches(mismatch["commitment"],
                                                              mismatch["revealedServerNonce"])  # fmt: skip
    assert rules.sas(text, INPUTS["serverNonce"]) == PA["sas"]
    assert guide.sas_shown(PA["sas"]) == PA["sasDisplay"]
    # the app made its MAC with the next pin: the server, which tries that pin too, matches it; the current
    # pin's transcript does not
    next_text = rules.transcript(PA["nextSpkiPin"], INPUTS["deviceKey"], INPUTS["clientNonce"])
    assert rules.client_mac(token, next_text) == PA["rejects"]["macWithTheNextPin"] != PA["appMac"]
    der, _ = rules.device_key(INPUTS["deviceKey"])
    assert rules.b64url(der) == rules.b64url(base64.b64decode(INPUTS["deviceKey"]))


def test_the_session_vector_verifies() -> None:
    s = VECTORS["session"]
    assert rules.session_text(s["deviceId"], s["nonce"]).decode() == s["text"]
    der, _ = rules.device_key(s["devicePublicKeySpkiBase64"])
    assert rules.verify_session(der, s["deviceId"], s["nonce"], s["signatureDerBase64"])
    assert not rules.verify_session(der, s["deviceId"], s["nonce"][::-1], s["signatureDerBase64"])
    assert s["expiry"]["live"] == {"closeCode": 4003, "closeReason": "session_expired", "controlFrame": None}


def test_the_relay_vector_headers_are_spelled_exactly() -> None:
    r = VECTORS["relay"]
    secret = bytes.fromhex(r["secretHex"])
    assert push_seal.relay_key(secret).hex() == r["keyHex"]
    headers = push_seal.relay_headers(secret, r["timestamp"], r["body"].encode())
    assert headers == r["headers"] and list(headers) == list(r["headers"])
    assert push_seal.REPLAY_WINDOW_S == r["windowSeconds"]


@pytest.mark.parametrize("case", VECTORS["push"]["cases"], ids=lambda c: c["name"])
def test_the_sealed_push_vectors_open_and_seal_byte_for_byte(case: dict[str, Any]) -> None:
    push = VECTORS["push"]
    private, public = (
        bytes.fromhex(push["recipientPrivateKeyHex"]),
        bytes.fromhex(push["recipientPublicKeyHex"]),
    )
    assert push_seal.INFO.decode() == push["info"]
    assert push_seal.open_sealed(case["sealed"], private, push["aad"]) == json.loads(case["plaintext"])
    doc = json.loads(case["plaintext"])
    sent_at = datetime.fromisoformat(doc["sentAt"].replace("Z", "+00:00"))
    padded = push_seal.padded(
        push_seal.fitted(push_seal.plaintext(doc["notice"], doc.get("apns"), sent_at=sent_at))
    )
    assert padded == case["plaintext"].encode() and len(padded) == case["plaintextLength"]
    suite = push_seal.suite()
    ephemeral = bytes.fromhex(case["ephemeralPrivateKeyHex"])
    ephemeral_public = (
        X25519PrivateKey.from_private_bytes(ephemeral)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    eks = KEMKeyPair(
        suite.kem.deserialize_private_key(ephemeral), suite.kem.deserialize_public_key(ephemeral_public)
    )
    enc, context = suite.create_sender_context(
        suite.kem.deserialize_public_key(public), push_seal.INFO, eks=eks
    )
    sealed = rules.b64url(enc + context.seal(padded, push["aad"].encode()))
    assert sealed == case["sealed"] and len(enc) + len(padded) + 16 == case["sealedLength"]


@pytest.mark.parametrize("case", VECTORS["push"]["nse"]["cases"], ids=lambda c: c["name"])
def test_the_extension_s_replay_cases_are_plaintexts_this_server_writes(case: dict[str, Any]) -> None:
    """The server's half of the replay gate (security review finding R11): every plaintext carries `sentAt` in
    UTC ending in Z, the same text this server writes for that moment, and the notice's own id (the
    extension's verdict on each case
    is the app's: show, repeat, stale or future)."""
    from pisa_conformance.validate import EXT_ID, errors

    doc = case["plaintext"]
    assert errors(doc, EXT_ID + "#/$defs/PushPlaintext") == []
    sent_at = datetime.fromisoformat(doc["sentAt"].replace("Z", "+00:00"))
    assert push_seal.plaintext(doc["notice"], doc.get("apns"), sent_at=sent_at) == doc
    nse = VECTORS["push"]["nse"]
    age = case["now"] - sent_at.timestamp()
    expected = "stale" if age > nse["windowSeconds"] else "future" if -age > nse["futureSeconds"] else None
    if expected is not None:
        assert case["result"] == expected
    else:
        assert case["result"] in ("show", "repeat")


# --- the client aborts on a reveal that does not match ------------------------------------------------------
def fake_server(reveal: str) -> httpx.MockTransport:
    """A server that commits to the vectors' nonce, then reveals another one (`revealMismatch`)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/pair/begin":
            return httpx.Response(201, json={"pairingId": "pair_vector", "commitment": PA["commitment"],
                                             "expiresAt": "2026-11-18T10:05:00+00:00"})  # fmt: skip
        if request.url.path == "/v1/pair":
            body = json.loads(request.content)
            token = bytes.fromhex(INPUTS["tokenHex"])
            text = rules.transcript(PA["spkiPin"], body["deviceKey"], body["clientNonce"])
            answer = {"pairingId": "pair_vector", "deviceId": VECTORS["session"]["deviceId"],
                      "serverNonce": reveal, "mac": rules.server_mac(token, text, reveal),
                      "state": "awaiting_confirmation"}  # fmt: skip
            return httpx.Response(201, json=answer)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "No.", "action": ""}})

    return httpx.MockTransport(handle)


def test_the_client_aborts_on_a_reveal_that_is_not_the_commitment_s(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    mismatch = PA["revealMismatch"]["revealedServerNonce"]

    async def go(reveal: str) -> dict[str, Any]:
        client = Client.new("https://pisa.example:443")
        async with client.http(transport=fake_server(reveal)) as http:
            return await client.pair(http, PA["qr"], "CLI")

    with pytest.raises(ClientError, match="Do not confirm on the server"):
        asyncio.run(go(mismatch))
    assert asyncio.run(go(INPUTS["serverNonce"]))["sas"]  # the committed nonce: digits are shown
    # the command line: an error, no digits, nothing saved
    real_http = Client.http
    monkeypatch.setattr(Client, "http", lambda self, transport=None: real_http(self, fake_server(mismatch)))
    path = tmp_path / "c.json"
    assert (
        cli.main(["client", "pair", PA["qr"], "--server", "https://pisa.example:443", "--file", str(path)])
        == 1
    )
    out = capsys.readouterr()
    assert REVEAL_MISMATCH in out.err and "check that the server shows" not in out.out and not path.exists()


def pin_of(client: Any) -> str:
    from pisa.services import endpoints

    return endpoints.pins(client.app.state.runtime.settings)[0]


# --- begin
# ----------------------------------------------------------------------------------------------------
def make_token(db: psycopg.Connection, secrets_key: str) -> pairing.PairToken:
    from tests.test_privacy_v2 import make_token as made

    return made(db, secrets_key)


def test_a_begun_pairing_serves_one_request_and_its_nonce_is_never_readable(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    begun = client.post("/v1/pair/begin").json()
    assert delta_body_errors("post", "/v1/pair/begin", "201", begun) == []
    assert client.get(f"/v1/pair/{begun['pairingId']}").status_code == 404  # no reveal before the request
    phone = Phone(make_token(db, secrets_key).token, pin_of(client))
    unknown = client.post("/v1/pair", json=phone.body | {"pairingId": "pair_never_begun_here"})
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "pairing_code_invalid"
    answer = client.post("/v1/pair", json=phone.body | {"pairingId": begun["pairingId"]})
    assert answer.status_code == 201
    assert rules.reveal_matches(begun["commitment"], answer.json()["serverNonce"])
    again = Phone(
        make_token(db, secrets_key).token, pin_of(client)
    )  # a valid code, but the pairing id is used
    reused = client.post("/v1/pair", json=again.body | {"pairingId": begun["pairingId"]})
    assert reused.status_code == 400
    late = client.post("/v1/pair/begin").json()
    db.execute(
        "UPDATE pairings SET expires_at = now() - interval '1 second' WHERE id = %s", (late["pairingId"],)
    )
    expired = client.post("/v1/pair", json=again.body | {"pairingId": late["pairingId"]})
    assert expired.status_code == 400


def test_begins_are_limited_per_address(client: Any) -> None:
    """A backstop shared by everyone behind one address, so well above what one household begins at once."""
    limit = pairing.MAX_OPEN_BEGINS
    answers = [client.post("/v1/pair/begin").status_code for _ in range(limit + 1)]
    assert limit >= 20 and answers == [201] * limit + [429]


# --- a paired phone's offer
# -----------------------------------------------------------------------------------
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signed(client: Any, token: str, key: Any, key_id: str, op: str, target: str) -> dict[str, str]:
    """The phone's signature over a `device` challenge (the Face ID key's, here a test key)."""
    challenge = client.post("/v1/phone/challenge", headers=auth(token),
                            json={"action": "device", "op": op, "target": target}).json()  # fmt: skip
    signature = key.sign(challenge["canonical"].encode(), ec.ECDSA(hashes.SHA256()))
    return auth(token) | {"X-Pisa-Nonce": challenge["nonce"], "X-Pisa-Key-Id": key_id,
                          "X-Pisa-Signature": base64.b64encode(signature).decode()}  # fmt: skip


@pytest.fixture
def phone(db: psycopg.Connection) -> dict[str, Any]:
    """A paired phone whose Face ID key is registered (so offers and confirmations must be signed)."""
    device_id, token, key = pair_v2_key(db)
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, label, device_id, created_at) VALUES ('face', %s, 'x', "
        "%s, "
        "now())",
        (raw, device_id),
    )
    return {"id": device_id, "token": token, "key": key}


def offer(client: Any, phone: dict[str, Any], body: dict[str, Any]) -> Any:
    text = json.dumps(body)
    target = hashlib.sha256(text.encode()).hexdigest()
    headers = signed(client, phone["token"], phone["key"], "face", "pairing_offer", target)
    return client.post(
        "/v1/pair/offers", content=text, headers=headers | {"content-type": "application/json"}
    )


def offer_schema_errors(body: Any) -> list[str]:
    from jsonschema import Draft202012Validator
    from pisa_conformance.validate import registry

    schema = _inline(DELTA["components"]["schemas"]["PairingOffer"])
    return [e.message for e in Draft202012Validator(schema, registry=registry()).iter_errors(body)]


def test_a_paired_phone_offers_and_confirms_with_the_sas_it_computed(
    client: Any, db: psycopg.Connection, phone: dict[str, Any]
) -> None:
    unsigned = client.post("/v1/pair/offers", json={"name": "Tablet"}, headers=auth(phone["token"]))
    assert unsigned.status_code == 403 and unsigned.json()["error"]["code"] == "biometric_required"
    made = offer(client, phone, {"name": "Tablet"})
    assert made.status_code == 201, made.text
    body = made.json()
    assert offer_schema_errors(body) == [] and body["state"] == "open"
    qr = rules.parse_qr(body["qr"])
    assert qr["token"] == body["token"] and qr["spki"] == pin_of(client)
    _, other = pair_v2(db, "iPad")
    assert client.get(f"/v1/pair/offers/{body['offerId']}", headers=auth(other)).status_code == 404
    tablet = Phone(body["token"], qr["spki"], name="Tablet")
    paired = send(client, tablet).json()
    # the server's CLI does not see it: the offering phone confirms it
    assert asyncio.run(_waiting(db)) == []
    state = client.get(f"/v1/pair/offers/{body['offerId']}", headers=auth(phone["token"])).json()
    assert offer_schema_errors(state) == [] and state["state"] == "awaiting_confirmation"
    assert state["pairingId"] == paired["pairingId"] and state["newDevice"] == {
        "name": "Tablet",
        "platform": "ios",
    }
    t = state["transcript"]
    assert t["commitment"] == tablet.begun["commitment"] and rules.reveal_matches(
        t["commitment"], t["serverNonce"]
    )
    phone_sas = rules.sas(rules.transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    assert phone_sas == rules.sas(tablet.text, paired["serverNonce"])  # both devices show the same digits
    url = f"/v1/pair/offers/{body['offerId']}/confirm"
    wrong = f"{(int(phone_sas) + 1) % 1_000_000:06d}"
    target = f"{paired['pairingId']}:{wrong}"
    headers = signed(client, phone["token"], phone["key"], "face", "pairing_confirm", target)
    refused = client.post(url, json={"pairingId": paired["pairingId"], "sas": wrong}, headers=headers)
    assert refused.status_code == 409 and db.execute(
        "SELECT count(*) FROM devices WHERE name = 'Tablet'"
    ).fetchone() == (0,)
    target = f"{paired['pairingId']}:{phone_sas}"
    unsigned_confirm = client.post(url, json={"pairingId": paired["pairingId"], "sas": phone_sas},
                                   headers=auth(phone["token"]))  # fmt: skip
    assert unsigned_confirm.status_code == 403
    headers = signed(client, phone["token"], phone["key"], "face", "pairing_confirm", target)
    confirmed = client.post(url, json={"pairingId": paired["pairingId"], "sas": phone_sas}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    done = confirmed.json()
    assert (
        offer_schema_errors(done) == []
        and done["state"] == "confirmed"
        and done["deviceId"] == paired["deviceId"]
    )
    assert client.get(f"/v1/pair/{paired['pairingId']}").json()["state"] == "confirmed"
    row = db.execute(
        "SELECT paired_via, platform FROM devices WHERE id = %s", (paired["deviceId"],)
    ).fetchone()
    assert row == ("app", "ios")  # paired from the app: it never registers Pisa's first Face ID key


def test_withdrawing_an_offer_refuses_the_device_that_scanned_it(
    client: Any, db: psycopg.Connection, phone: dict[str, Any]
) -> None:
    body = offer(client, phone, {}).json()
    tablet = Phone(body["token"], rules.parse_qr(body["qr"])["spki"])
    paired = send(client, tablet).json()
    assert (
        client.delete(f"/v1/pair/offers/{body['offerId']}", headers=auth(phone["token"])).status_code == 204
    )
    assert client.get(f"/v1/pair/{paired['pairingId']}").json()["state"] == "refused"
    assert (
        client.get(f"/v1/pair/offers/{body['offerId']}", headers=auth(phone["token"])).json()["state"]
        == "refused"
    )
    unused = offer(client, phone, {"name": "Laptop"}).json()
    client.delete(f"/v1/pair/offers/{unused['offerId']}", headers=auth(phone["token"]))
    late = Phone(unused["token"], rules.parse_qr(unused["qr"])["spki"])
    assert send(client, late).status_code == 400  # the token was spent


async def _waiting(db: psycopg.Connection) -> list[Any]:
    async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
        return await pairing.waiting(conn)
