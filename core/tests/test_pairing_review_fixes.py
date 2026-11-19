"""The fixes of the pairing security review (E1, part 2), each the inverse of its proof of concept:
- P1: a phone cannot offer a `signer` pairing (signer is granted on the server only), and no confirmed
  pairing puts a device key into an empty phone-key store: the first key stays the host-paired iPhone's;
- P2: a session nonce asked for with `deviceId` is that device's alone and no one behind the same address can
  starve it; per-address limits count an IPv6 client by its /64; X-Forwarded-For is believed from a trusted
  proxy only; unbound nonces have a global cap;
- P3: a signer's device key signs device ops, never approvals or a new key;
- P5: the session check refuses a stored key that is not P-256 explicitly, not by `assert`.
P4 (the relay forgets the devices `pisa phone-keys reset` revokes) is in `test_push_relay.py`."""

import base64
import hashlib
import json
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from pisa.domain import pairing as rules
from pisa.domain import proxies
from pisa.domain.errors import PisaError
from pisa.services import endpoints, pairing
from tests.conftest import pair_v2_key
from tests.phone_signing import device_headers, new_key, register, sign
from tests.test_privacy_v2 import Phone, app_settings, make_token, on_db, send, session, tls  # noqa: F401


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def offer_and_pair(client: Any, offerer: str, name: str, **body: Any) -> tuple[Any, Phone, dict[str, Any]]:
    """A phone offers a pairing, a new device pairs with it and the phone confirms by its SAS."""
    made = client.post("/v1/pair/offers", json={"name": name, **body}, headers=auth(offerer))
    assert made.status_code == 201, made.text
    offer = made.json()
    device = Phone(offer["token"], rules.parse_qr(offer["qr"])["spki"], name=name, platform="other")
    paired = send(client, device).json()
    t = client.get(f"/v1/pair/offers/{offer['offerId']}", headers=auth(offerer)).json()["transcript"]
    sas = rules.sas(rules.transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    confirmed = client.post(
        f"/v1/pair/offers/{offer['offerId']}/confirm",
        json={"pairingId": paired["pairingId"], "sas": sas},
        headers=auth(offerer),
    )
    return confirmed, device, paired


# --- P1 ----------------------------------------------------------------------------------------------------
def test_p1_a_phone_cannot_offer_a_signer_pairing_and_the_iphone_keeps_its_first_key(
    client: Any, db: psycopg.Connection
) -> None:
    """The proof of concept, inverted: on a fresh install another paired device asks for a `signer` offer,
    unsigned. Refused (400), nothing made; a plain offer still works, but its device's key never joins the
    store, the iPhone registers Pisa's first Face ID key, and the new device can't revoke it."""
    iphone_id, iphone_token, _ = pair_v2_key(db, "iPhone", "ios")
    _, other_token, _ = pair_v2_key(db, "Old laptop", "other")
    tokens = db.execute("SELECT count(*) FROM pair_tokens").fetchone()
    for signer in (True, False, None):  # CONTRACT-2 §4.3.1: there is no signer field, whatever its value
        refused = client.post(
            "/v1/pair/offers", json={"name": "attacker", "signer": signer}, headers=auth(other_token)
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == "validation_failed"
        assert "only on the server" in refused.json()["error"]["message"]
    assert db.execute("SELECT count(*) FROM pair_tokens").fetchone() == tokens  # no token was made

    confirmed, attacker, paired = offer_and_pair(client, other_token, "attacker")
    assert confirmed.status_code == 200, confirmed.text
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)  # no key from an offer

    face = register(client, auth(iphone_token), "face")  # the first key is still the iPhone's to register
    assert db.execute("SELECT key_id, kind FROM phone_keys").fetchall() == [("face", "biometric")]
    attacker_token = session(client, attacker, paired["deviceId"]).json()["token"]
    unsigned = client.delete(f"/v1/devices/{iphone_id}", headers=auth(attacker_token))
    assert unsigned.status_code == 403 and unsigned.json()["error"]["code"] == "biometric_required"
    assert db.execute("SELECT revoked_at FROM devices WHERE id = %s", (iphone_id,)).fetchone() == (None,)
    assert face is not None


def _host_signer(client: Any, db: psycopg.Connection, secrets_key: str, name: str) -> tuple[Phone, str]:
    """`pisa pair <name> --signer` on the server, confirmed there; the new device and its id."""
    made = make_token(db, secrets_key, name, signer=True)
    pin = endpoints.pins(client.app.state.runtime.settings)[0]
    device = Phone(made.token, pin, name=name, platform="cli")
    answer = send(client, device)
    assert answer.status_code == 201, answer.text
    device_id = on_db(db, lambda c: pairing.decide(c, answer.json()["pairingId"], True))
    return device, str(device_id)


def test_p1_a_host_signer_s_key_joins_the_store_only_beside_a_face_id_key(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """`decide()` never puts a device key into an empty phone-key store, even for a signer the user confirmed
    on the server: the first key comes only through the first-key rule (an iPhone paired on the server)."""
    _, first_id = _host_signer(client, db, secrets_key, "CLI")
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)
    _, iphone_token, _ = pair_v2_key(db, "iPhone", "ios")
    register(client, auth(iphone_token), "face")
    _, second_id = _host_signer(client, db, secrets_key, "CLI 2")
    stored = db.execute("SELECT key_id, kind FROM phone_keys ORDER BY key_id").fetchall()
    assert stored == [(f"device-{second_id}", "signer"), ("face", "biometric")]
    assert first_id != second_id


def test_p1_a_signer_key_left_alone_does_not_block_the_iphone_s_first_key(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """The Face ID key's iPhone is revoked and only a signer's device key is left: the iPhone paired again on
    the server registers a Face ID key as the first one; another device still can't."""
    _, old_token, _ = pair_v2_key(db, "iPhone", "ios")
    register(client, auth(old_token), "face-old")
    _host_signer(client, db, secrets_key, "CLI")
    db.execute("DELETE FROM phone_keys WHERE key_id = 'face-old'")
    assert db.execute("SELECT kind FROM phone_keys").fetchall() == [("signer",)]
    _, other_token, _ = pair_v2_key(db, "Tablet", "android")
    private, public = new_key()
    refused = client.post(
        "/v1/phone/keys", json={"key_id": "x", "public_key": public}, headers=auth(other_token)
    )
    assert refused.status_code == 400  # only the phone registers Pisa's first key
    _, iphone_token, _ = pair_v2_key(db, "iPhone", "ios")
    register(client, auth(iphone_token), "face-new")
    assert private is not None


# --- P3 ----------------------------------------------------------------------------------------------------
def test_p3_a_signer_s_device_key_signs_device_ops_but_never_an_approval_or_a_new_key(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    iphone_id, iphone_token, _ = pair_v2_key(db, "iPhone", "ios")
    face = register(client, auth(iphone_token), "face")
    cli, cli_id = _host_signer(client, db, secrets_key, "CLI")
    cli_auth = auth(session(client, cli, cli_id).json()["token"])
    signer_key = f"device-{cli_id}"

    # a device op: accepted
    body = json.dumps({"name": "iPad"})
    target = hashlib.sha256(body.encode()).hexdigest()
    headers = device_headers(client, cli_auth, cli.key, "pairing_offer", target, key_id=signer_key)
    made = client.post(
        "/v1/pair/offers", content=body, headers=headers | {"content-type": "application/json"}
    )
    assert made.status_code == 201, made.text

    # a new key: refused (add_key is Face ID's)
    _, public = new_key()
    c = client.post("/v1/phone/challenge", headers=cli_auth,
                    json={"action": "add_key", "key_id": "k-new", "public_key": public}).json()  # fmt: skip
    body = {"key_id": "k-new", "public_key": public, "nonce": c["nonce"],
            "signature": sign(cli.key, c["canonical"]), "signer_key_id": signer_key}  # fmt: skip
    added = client.post("/v1/phone/keys", headers=cli_auth, json=body)
    assert added.status_code == 403 and added.json()["error"]["code"] == "biometric_required"

    # an approval (CONTRACT-2 §6: signed with Face ID): refused for the device key, accepted for Face ID's
    keys = client.app.state.runtime.autopilot.keys
    subject = {"server": "pisa.example", "device": iphone_id, "approval_id": "a-1", "interrupt_id": "i-1",
               "kind": "email_send", "digest": "0" * 64, "recipients_count": "1"}  # fmt: skip
    for key, key_id, ok in ((cli.key, signer_key, False), (face, "face", True)):
        challenge = client.portal.call(keys.challenge_approval, subject)
        signature = sign(key, challenge["canonical"])
        if ok:
            client.portal.call(keys.verify_approval, subject, challenge["nonce"], signature, key_id)
        else:
            with pytest.raises(PisaError, match="Face ID"):
                client.portal.call(keys.verify_approval, subject, challenge["nonce"], signature, key_id)


# --- P2 ----------------------------------------------------------------------------------------------------
def test_p2_limits_count_ipv6_by_its_64_and_ipv4_by_its_address() -> None:
    assert proxies.limit_key("2001:db8:1:2:aaaa::9") == "2001:db8:1:2::/64"
    assert proxies.limit_key("2001:db8:1:2:bbbb::7") == "2001:db8:1:2::/64"
    assert proxies.limit_key("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert proxies.limit_key("198.51.100.7") == "198.51.100.7"
    assert proxies.limit_key("::ffff:198.51.100.7") == "198.51.100.7"
    assert proxies.limit_key("[2001:db8::1]:443") == "2001:db8::/64"
    assert proxies.limit_key("testclient") == "testclient"


def _from(client: Any, monkeypatch: pytest.MonkeyPatch, peer: str, trusted: str = "") -> None:
    monkeypatch.setattr(client._transport, "client", (peer, 50000))
    monkeypatch.setattr(client.app.state.runtime.settings, "trusted_proxies", trusted)


def _nonce(client: Any, forwarded: str | None = None, **params: str) -> Any:
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    return client.get("/v1/devices/session/nonce", headers=headers, params=params)


def test_p2_a_bound_nonce_can_t_be_starved_by_others_at_the_same_address(
    client: Any, db: psycopg.Connection
) -> None:
    """The proof of concept, inverted: five unauthenticated requests from a shared address exhaust that
    address's unbound nonces, but the user's phone asks with its deviceId and gets one, and uses it."""
    device_id, _, key = pair_v2_key(db)
    phone = Phone(rules.new_token(), "x" * 8)
    phone.key = key
    for _ in range(5):
        assert _nonce(client).status_code == 200
    assert _nonce(client).status_code == 429  # unbound: still 5 per address, as the contract says
    got = [_nonce(client, deviceId=device_id) for _ in range(7)]
    assert all(g.status_code == 200 for g in got)
    bound = db.execute(
        "SELECT count(*) FROM session_nonces WHERE device_id = %s AND used_at IS NULL", (device_id,)
    ).fetchone()
    assert bound == (5,)  # the newest 5 per device; older ones were dropped
    oldest, newest = got[0].json()["nonce"], got[-1].json()["nonce"]
    dropped = {"deviceId": device_id, "nonce": oldest, "signature": phone.sign(device_id, oldest)}
    assert client.post("/v1/devices/session", json=dropped).status_code == 401
    good = {"deviceId": device_id, "nonce": newest, "signature": phone.sign(device_id, newest)}
    assert client.post("/v1/devices/session", json=good).status_code == 200


def test_p2_a_bound_nonce_serves_its_own_device_only(client: Any, db: psycopg.Connection) -> None:
    mine, _, _ = pair_v2_key(db, "iPhone")
    other_id, _, other_key = pair_v2_key(db, "iPad")
    other = Phone(rules.new_token(), "x" * 8)
    other.key = other_key
    nonce = _nonce(client, deviceId=mine).json()["nonce"]
    stolen = {"deviceId": other_id, "nonce": nonce, "signature": other.sign(other_id, nonce)}
    answer = client.post("/v1/devices/session", json=stolen)
    assert answer.status_code == 401 and answer.json()["error"]["code"] == "auth_failed"
    for bad in ("not-a-uuid", "00000000-0000-4000-8000-000000000000"):
        assert _nonce(client, deviceId=bad).status_code == 400
    db.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (mine,))
    assert _nonce(client, deviceId=mine).status_code == 400  # a revoked device gets none


def test_p2_x_forwarded_for_counts_only_from_a_trusted_proxy(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an untrusted peer: whatever it writes in X-Forwarded-For, its nonces are counted under its own address
    _from(client, monkeypatch, "203.0.113.5")
    for i in range(5):
        assert _nonce(client, f"198.51.100.{i}").status_code == 200
    assert _nonce(client, "198.51.100.99").status_code == 429
    # behind a trusted proxy: the forwarded client is counted, each on its own
    _from(client, monkeypatch, "10.0.0.1", "10.0.0.0/8")
    for _ in range(5):
        assert _nonce(client, "198.51.100.20").status_code == 200
    assert _nonce(client, "198.51.100.20").status_code == 429
    assert _nonce(client, "198.51.100.21").status_code == 200


def test_p2_an_ipv6_client_is_counted_by_its_64(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _from(client, monkeypatch, "10.0.0.1", "10.0.0.0/8")
    for i in range(5):
        assert _nonce(client, f"2001:db8:5:6::{i + 1}").status_code == 200
    assert _nonce(client, "2001:db8:5:6:ffff::1").status_code == 429  # the same /64
    assert _nonce(client, "2001:db8:5:7::1").status_code == 200  # the next /64


def test_p2_unbound_nonces_have_a_global_cap_and_bound_ones_pass_it(
    client: Any, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairing, "MAX_UNBOUND_NONCES", 3)
    _from(client, monkeypatch, "10.0.0.1", "10.0.0.0/8")
    for i in range(3):
        assert _nonce(client, f"198.51.100.{i}").status_code == 200
    full = _nonce(client, "198.51.100.50")
    assert full.status_code == 429 and full.json()["error"]["code"] == "rate_limited"
    device_id, _, _ = pair_v2_key(db)
    assert _nonce(client, "198.51.100.50", deviceId=device_id).status_code == 200


# --- P5 ----------------------------------------------------------------------------------------------------
def test_p5_a_stored_key_that_is_not_p256_is_refused_explicitly() -> None:
    def spki(key: Any) -> bytes:
        return key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    nonce, device = rules.new_nonce(), "00000000-0000-4000-8000-000000000001"
    edwards = ed25519.Ed25519PrivateKey.generate()
    signature = base64.urlsafe_b64encode(edwards.sign(rules.session_text(device, nonce))).decode().rstrip("=")
    assert rules.verify_session(spki(edwards), device, nonce, signature) is False
    p384 = ec.generate_private_key(ec.SECP384R1())
    signed = p384.sign(rules.session_text(device, nonce), ec.ECDSA(hashes.SHA256()))
    assert rules.verify_session(spki(p384), device, nonce, rules.b64url(signed)) is False
    with pytest.raises(ValueError, match="P-256"):
        rules.x963(spki(p384))
    p256 = ec.generate_private_key(ec.SECP256R1())
    good = p256.sign(rules.session_text(device, nonce), ec.ECDSA(hashes.SHA256()))
    assert rules.verify_session(spki(p256), device, nonce, rules.b64url(good)) is True
    assert len(rules.x963(spki(p256))) == 65
