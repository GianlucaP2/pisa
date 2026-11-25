# ruff: noqa: F811  (pytest fixtures imported by name)
"""The fixes from the first security review's re-check of findings M3 and M5, each the inverse of the
re-check's proof of concept:

- M5-1 (Medium): a key vouched (`add_key`) by a device key is stored as kind `device`, never `biometric`, and
  keeps a device key's limits; a watch never approves, sets an autonomy level, offers a pairing, vouches for
  or gets a key, whatever key it holds; a vouched key records its voucher (`vouched_by`), goes when the
  voucher is revoked, and signs nothing while the voucher is revoked by any means;
- M3-1 (Low): the loopback guard refuses the headers proxies, tunnels and CDNs add beyond the first three;
- M3-2 (Low): the container's gateway counts as this machine only with PISA_INSECURE_LOOPBACK_GATEWAY."""

import asyncio
import hashlib
import json
import uuid
from typing import Any

import psycopg
import pytest

from pisa.api import loopback
from pisa.domain import pairing as rules
from pisa.services.devices import revoke_device
from tests.conftest import pair_v2_key
from tests.phone_signing import challenge, device_headers, new_key, register, sign
from tests.test_headless_signing import auth, erase, key_of, make_approval, ran, signed_resume  # noqa: F401
from tests.test_pair_cli import on_db
from tests.test_privacy_v2 import Phone, app_settings, send, session, tls  # noqa: F401 (fixtures)


def refused(response: Any, status: int = 403) -> bool:
    return response.status_code == status and response.json()["error"]["code"] == "biometric_required"


def _offer(client: Any, token: str, key: Any, signer_id: str, name: str) -> Any:
    text = json.dumps({"name": name})
    target = hashlib.sha256(text.encode()).hexdigest()
    headers = device_headers(client, auth(token), key, "pairing_offer", target, key_id=key_of(signer_id))
    return client.post(
        "/v1/pair/offers", content=text, headers=headers | {"content-type": "application/json"}
    )


def _offer_and_pair(client: Any, token: str, key: Any, signer_id: str, name: str) -> tuple[Phone, str]:
    """`signer_id`'s device key signs the offer and the confirm; a new device pairs with it."""
    made = _offer(client, token, key, signer_id, name)
    assert made.status_code == 201, made.text
    offer = made.json()
    new = Phone(offer["token"], rules.parse_qr(offer["qr"])["spki"], name=name, platform="cli")
    paired = send(client, new).json()
    t = client.get(f"/v1/pair/offers/{offer['offerId']}", headers=auth(token)).json()["transcript"]
    sas = rules.sas(rules.transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    headers = device_headers(client, auth(token), key, "pairing_confirm", f"{paired['pairingId']}:{sas}",
                             key_id=key_of(signer_id))  # fmt: skip
    confirmed = client.post(f"/v1/pair/offers/{offer['offerId']}/confirm",
                            json={"pairingId": paired["pairingId"], "sas": sas}, headers=headers)  # fmt: skip
    assert confirmed.status_code == 200, confirmed.text
    return new, paired["deviceId"]


def _vouch(
    client: Any, voucher_token: str, voucher_key: Any, signer_key_id: str, for_token: str, key_id: str
) -> Any:
    private, public = new_key()
    made = challenge(client, auth(voucher_token), action="add_key", key_id=key_id, public_key=public)
    body = {"key_id": key_id, "public_key": public, "nonce": made["nonce"],
            "signature": sign(voucher_key, made["canonical"]), "signer_key_id": signer_key_id}  # fmt: skip
    return client.post("/v1/phone/keys", headers=auth(for_token), json=body), private


def _owner_with_face_id(client: Any, db: psycopg.Connection) -> tuple[str, str, Any]:
    iphone_id, iphone_token, _ = pair_v2_key(db, "iPhone", "ios")
    face = register(client, auth(iphone_token), "face")  # every op now needs a signature
    return iphone_id, iphone_token, face


# --- M5-1 ---------------------------------------------------------------------------------------------------
def test_m5_1_a_watch_cannot_bring_in_a_device_nor_vouch_for_one(
    client: Any, db: psycopg.Connection, ran: list[dict[str, Any]]
) -> None:
    """The PoC's path 1, inverted: the watch's key can't offer a pairing, and can't vouch for a key."""
    _owner_with_face_id(client, db)
    watch_id, watch_token, watch_key = pair_v2_key(db, "Watch", "watchos")
    assert refused(_offer(client, watch_token, watch_key, watch_id, "rogue"))
    unsigned = client.post("/v1/pair/offers", json={"name": "rogue"}, headers=auth(watch_token))
    assert refused(unsigned)  # not even on the unsigned path
    _, rogue_token, _ = pair_v2_key(db, "rogue", "cli")  # a device the attacker got in some other way
    answer, _ = _vouch(client, watch_token, watch_key, key_of(watch_id), rogue_token, "rogue-face")
    assert refused(answer)
    assert db.execute("SELECT count(*) FROM phone_keys WHERE key_id = 'rogue-face'").fetchone() == (0,)
    assert ran == []


def test_m5_1_a_device_key_s_vouch_makes_a_device_key_not_a_face_id_key(
    client: Any, db: psycopg.Connection, ran: list[dict[str, Any]]
) -> None:
    _owner_with_face_id(client, db)
    cli_id, cli_token, cli_key = pair_v2_key(db, "CLI", "cli")
    rogue_dev, rogue_id = _offer_and_pair(client, cli_token, cli_key, cli_id, "rogue")
    rogue_token = session(client, rogue_dev, rogue_id).json()["token"]
    answer, rogue_key = _vouch(client, cli_token, cli_key, key_of(cli_id), rogue_token, "rogue-face")
    assert answer.status_code == 201, answer.text
    stored = db.execute(
        "SELECT kind, vouched_by::text FROM phone_keys WHERE key_id = 'rogue-face'"
    ).fetchone()
    assert stored == ("device", cli_id)
    # a device key's limits: only for its own device
    other_id, other_token, _ = pair_v2_key(db, "Laptop", "cli")
    assert refused(erase(client, other_token, rogue_key, "rogue-face"))
    approval_id = make_approval(client, other_id)
    resumed, _ = signed_resume(client, other_token, rogue_key, "rogue-face", approval_id)
    assert refused(resumed, 401) and ran == []
    # and it vouches for no key of its own device
    again, _ = _vouch(client, rogue_token, rogue_key, "rogue-face", rogue_token, "rogue-face-2")
    assert refused(again)


def test_m5_1_a_watch_gets_no_key_and_approves_with_none(
    client: Any, db: psycopg.Connection, ran: list[dict[str, Any]]
) -> None:
    """The PoC's path 2, inverted: a CLI can't vouch a key onto a watch; a watch holding a Face ID key (an old
    row) still approves nothing, nor sets an autonomy level: WATCH-1 by the requesting device's platform."""
    _owner_with_face_id(client, db)
    cli_id, cli_token, cli_key = pair_v2_key(db, "CLI", "cli")
    watch_id, watch_token, _ = pair_v2_key(db, "Watch", "watchos")
    answer, _ = _vouch(client, cli_token, cli_key, key_of(cli_id), watch_token, "watch-face")
    assert refused(answer)
    private, public = new_key()
    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, device_id, created_at, kind) VALUES ('watch-face', "
        "decode(%s, 'base64'), %s, now(), 'biometric')",
        (public, watch_id),
    )
    approval_id = make_approval(client, watch_id)
    resumed, _ = signed_resume(client, watch_token, private, "watch-face", approval_id)
    assert refused(resumed, 401) and ran == []
    made = challenge(client, auth(watch_token), action="autonomy", type="calendar_create", level="tell")
    body = {"level": "tell", "nonce": made["nonce"], "signature": sign(private, made["canonical"]),
            "key_id": "watch-face"}  # fmt: skip
    assert refused(client.post("/v1/autonomy/calendar_create", headers=auth(watch_token), json=body))


def test_m5_1_revoking_the_voucher_revokes_what_it_vouched_for(client: Any, db: psycopg.Connection) -> None:
    """The PoC's path 3, inverted: revoked by SQL (as the PoC does), the voucher's key signs nothing; revoked
    as the server revokes, the vouched key is deleted."""
    _owner_with_face_id(client, db)
    cli_id, cli_token, cli_key = pair_v2_key(db, "CLI", "cli")
    rogue_dev, rogue_id = _offer_and_pair(client, cli_token, cli_key, cli_id, "rogue")
    rogue_token = session(client, rogue_dev, rogue_id).json()["token"]
    answer, rogue_key = _vouch(client, cli_token, cli_key, key_of(cli_id), rogue_token, "rogue-face")
    assert answer.status_code == 201
    assert erase(client, rogue_token, rogue_key, "rogue-face").status_code == 202  # for its own device
    db.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (cli_id,))
    assert refused(erase(client, rogue_token, rogue_key, "rogue-face", "mail"))
    db.execute("UPDATE devices SET revoked_at = NULL WHERE id = %s", (cli_id,))
    on_db(db, lambda c: revoke_device(c, uuid.UUID(cli_id)))
    assert db.execute("SELECT count(*) FROM phone_keys WHERE key_id = 'rogue-face'").fetchone() == (0,)
    assert refused(erase(client, rogue_token, rogue_key, "rogue-face", "places"))


def test_m5_1_a_face_id_key_s_vouch_is_still_a_face_id_key(client: Any, db: psycopg.Connection) -> None:
    iphone_id, iphone_token, face = _owner_with_face_id(client, db)
    _, new_token, _ = pair_v2_key(db, "New iPhone", "ios")
    answer, _ = _vouch(client, iphone_token, face, "face", new_token, "face-new")
    assert answer.status_code == 201, answer.text
    stored = db.execute("SELECT kind, vouched_by::text FROM phone_keys WHERE key_id = 'face-new'").fetchone()
    assert stored == ("biometric", iphone_id)


# --- M5-2 (Low) and Info 3, from the re-check of 60b126537 --------------------------------------------
def test_m5_2_a_vouched_device_key_never_vouches(client: Any, db: psycopg.Connection) -> None:
    """The PoC (`test_m5r_a_vouched_device_key_still_vouches_and_its_grandchild_outlives_the_first_voucher`)
    inverted: K_B, which A's device key vouched for, can't vouch for K_C on C."""
    _owner_with_face_id(client, db)
    a_id, a_token, a_key = pair_v2_key(db, "A", "cli")
    _, b_token, _ = pair_v2_key(db, "B", "cli")
    _, c_token, _ = pair_v2_key(db, "C", "cli")
    first, kb = _vouch(client, a_token, a_key, key_of(a_id), b_token, "k-b")
    assert first.status_code == 201, first.text
    second, _ = _vouch(client, b_token, kb, "k-b", c_token, "k-c")
    assert refused(second)
    assert db.execute("SELECT count(*) FROM phone_keys WHERE key_id = 'k-c'").fetchone() == (0,)


def test_m5_2_revoking_a_voucher_follows_the_chain_down(client: Any, db: psycopg.Connection) -> None:
    """A's device key vouches K_B on B; B's own device key vouches K_C on C. A revoked by SQL: K_C signs
    nothing (its voucher's voucher is revoked); A revoked as the server revokes: K_B and K_C are deleted."""
    _owner_with_face_id(client, db)
    a_id, a_token, a_key = pair_v2_key(db, "A", "cli")
    b_id, b_token, b_key = pair_v2_key(db, "B", "cli")
    _, c_token, _ = pair_v2_key(db, "C", "cli")
    first, _ = _vouch(client, a_token, a_key, key_of(a_id), b_token, "k-b")
    second, kc = _vouch(client, b_token, b_key, key_of(b_id), c_token, "k-c")
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert erase(client, c_token, kc, "k-c").status_code == 202  # for its own device
    db.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (a_id,))
    assert refused(erase(client, c_token, kc, "k-c", "mail"))  # two hops up, by any means
    db.execute("UPDATE devices SET revoked_at = NULL WHERE id = %s", (a_id,))
    on_db(db, lambda c: revoke_device(c, uuid.UUID(a_id)))
    left = db.execute("SELECT key_id FROM phone_keys WHERE key_id IN ('k-b', 'k-c')").fetchall()
    assert left == []
    assert refused(erase(client, c_token, kc, "k-c", "places"))


def test_m5_2_a_cycle_of_vouches_ends(client: Any, db: psycopg.Connection) -> None:
    """A vouches a key on B and B one on A: revoking A deletes both, and the cascade ends."""
    _owner_with_face_id(client, db)
    a_id, a_token, a_key = pair_v2_key(db, "A", "cli")
    b_id, b_token, b_key = pair_v2_key(db, "B", "cli")
    one, _ = _vouch(client, a_token, a_key, key_of(a_id), b_token, "on-b")
    two, _ = _vouch(client, b_token, b_key, key_of(b_id), a_token, "on-a")
    assert one.status_code == 201 and two.status_code == 201, two.text
    on_db(db, lambda c: revoke_device(c, uuid.UUID(a_id)))
    assert db.execute("SELECT count(*) FROM phone_keys WHERE key_id IN ('on-a', 'on-b')").fetchone() == (0,)


def test_info_3_a_watch_turns_on_no_skill() -> None:
    from pisa.services import phone_keys

    watch = uuid.uuid4()
    face = phone_keys._Key(b"", phone_keys.BIOMETRIC, watch, "watchos")  # pyright: ignore[reportPrivateUsage]
    assert "skill" in phone_keys.WATCH_REFUSED
    assert phone_keys._refusal(face, "skill", watch, "watchos") == "a watch only rejects"  # pyright: ignore[reportPrivateUsage]
    phone = phone_keys._Key(b"", phone_keys.BIOMETRIC, watch, "ios")  # pyright: ignore[reportPrivateUsage]
    assert phone_keys._refusal(phone, "skill", watch, "ios") is None  # pyright: ignore[reportPrivateUsage]


# --- M3-1, M3-2 ---------------------------------------------------------------------------------------------
def _status(peer: str, headers: list[tuple[bytes, bytes]], extra: frozenset[str] = frozenset()) -> int:
    seen: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(200)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            seen.append(message["status"])

    scope = {"type": "http", "client": (peer, 5555), "headers": headers, "method": "GET", "path": "/"}
    asyncio.run(loopback.LoopbackOnly(app, extra)(scope, receive, send))
    return seen[0]


@pytest.mark.parametrize(
    "name",
    [b"via", b"x-forwarded-host", b"x-forwarded-proto", b"cf-connecting-ip", b"true-client-ip",
     b"x-client-ip",
     b"fastly-client-ip", b"Via", b"forwarded", b"x-forwarded-for", b"x-real-ip"],
)  # fmt: skip
def test_m3_1_a_proxy_s_headers_are_refused_even_from_loopback(name: bytes) -> None:
    assert _status("127.0.0.1", [(name, b"203.0.113.9")]) == 403
    assert _status("127.0.0.1", []) == 200


def test_m3_2_the_gateway_is_this_machine_only_when_the_owner_says_so(
    app_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loopback, "in_container", lambda: True)
    monkeypatch.setattr(loopback, "default_gateway", lambda: "172.18.0.1")
    plain = app_settings.model_copy(update={"insecure_loopback_pairing": True})
    assert loopback.allowed_peers(plain) == frozenset()
    assert _status("172.18.0.1", [], loopback.allowed_peers(plain)) == 403
    said = plain.model_copy(update={"insecure_loopback_gateway": True})
    assert loopback.allowed_peers(said) == frozenset({"172.18.0.1"})
    assert _status("172.18.0.1", [], loopback.allowed_peers(said)) == 200
