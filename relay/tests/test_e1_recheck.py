# SPDX-License-Identifier: AGPL-3.0-only
"""The first security review's re-check (after the fixes): its proofs of concept (N1 to N3), adapted and
inverted, so each asserts that the attack now fails; and the
fixes' own rules (the app secret, the device list, the account key)."""

import base64
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding

from tests.conftest import NOW, Relay, b64url, transaction, unb64url


def _register(relay: Relay, token: str | None = None) -> Any:
    body = {
        "apns_token": token or secrets.token_hex(32),
        "environment": "production",
        "signed_transaction": relay.pki.sign(transaction(signedDate=int(relay.clock.now * 1000))),
    }
    return relay.client.post("/v1/register", json=body)


# --- N1, N1b: nothing an attacker sends can hold every registration at 429 ---------------------------------
ATTEMPTS = 300  # well past any limit across subscriptions the relay used to have (120 a minute)


def _post(relay: Relay, signed_transaction: str) -> int:
    body = {"apns_token": "00" * 32, "environment": "production", "signed_transaction": signed_transaction}
    return relay.client.post("/v1/register", json=body).status_code


def test_n1_junk_requests_do_not_block_registration(relay: Relay) -> None:
    junk = [
        b"{}",
        b'{"apns_token": "' + b"ab" * 32 + b'", "environment": "production", "signed_transaction": "a.b.c"}',
    ]
    for i in range(ATTEMPTS):
        response = relay.client.post(
            "/v1/register", content=junk[i % len(junk)], headers={"content-type": "application/json"}
        )
        assert response.status_code in (400, 403)
    assert _register(relay).status_code == 201  # a real subscriber, a valid fresh transaction


def test_n1b_a_public_root_in_x5c_does_not_block_registration(relay: Relay) -> None:
    root = base64.b64encode(relay.pki.root.public_bytes(Encoding.DER)).decode()  # Apple publishes it
    header = b64url(json.dumps({"alg": "ES256", "x5c": ["AA==", "AA==", root]}).encode())
    junk = f"{header}.{b64url(b'{}')}.{b64url(b'x' * 64)}"  # no Apple leaf, no signature, no transaction
    assert all(_post(relay, junk) == 403 for _ in range(ATTEMPTS))
    assert _register(relay).status_code == 201


def test_n1b_transactions_signed_by_another_root_do_not_block_registration(relay: Relay) -> None:
    from tests.pki import make_pki

    forged = make_pki(NOW).sign(transaction(signedDate=int(relay.clock.now * 1000)))
    assert all(_post(relay, forged) == 403 for _ in range(ATTEMPTS))
    assert _register(relay).status_code == 201


def test_n1b_failed_verifications_do_not_spend_the_subscription_s_hourly_limit(relay: Relay) -> None:
    stale = relay.pki.sign(transaction(signedDate=int(relay.clock.now * 1000) - 30 * 86_400_000))
    # the same subscription, refused before anything is counted
    assert all(_post(relay, stale) == 403 for _ in range(20))
    assert [_register(relay).status_code for _ in range(6)] == [201] * 5 + [429]


# --- N2: only the app binds Live Activity tokens -------------------------------------------------------
FOREIGN = "c0ffee" * 11 + "c0"  # another person's phone's push-to-start token


def _start(relay: Relay, handle: str) -> dict[str, Any]:
    activity = {
        "token": FOREIGN,
        "event": "start",
        "priority": 10,
        "timestamp": int(NOW.timestamp()),
        "attributesType": relay.settings.live_activity_attributes_type,
    }
    return {"handle": handle, "type": "liveactivity", "activity": activity}


def test_n2_the_server_cannot_bind_a_foreign_push_to_start_token_to_its_handle(relay: Relay) -> None:
    handle, secret = relay.register()  # the server received {handle, secret} over PUT /v1/devices/me/push
    body = {"handle": handle, "token": FOREIGN, "kind": "start"}
    assert relay.signed("/v1/register/activity", secret, body).status_code == 401  # the server's secret
    relay.clock.now += 1
    assert relay.push(handle, secret, _start(relay, handle)).status_code == 401
    assert relay.apns.requests == []
    # the app, with its own secret, binds a token of its own phone
    relay.clock.now += 1
    assert relay.app_signed("/v1/register/activity", relay.app_secrets[handle], body).status_code == 204
    relay.clock.now += 1
    assert relay.push(handle, secret, _start(relay, handle)).status_code == 202


def test_n2_an_app_signature_is_bound_to_its_route_and_is_not_a_server_signature(relay: Relay) -> None:
    from tests.conftest import app_headers, signed_headers

    handle, _ = relay.register()
    app_secret = relay.app_secrets[handle]
    stamp = int(relay.clock.now)
    listing = json.dumps({"handle": handle}).encode()
    headers = app_headers(app_secret, stamp, "POST", "/v1/register/devices", listing)
    # the device list's signature, replayed to DELETE /v1/register (the same body): refused
    assert relay.client.request("DELETE", "/v1/register", content=listing, headers=headers).status_code == 401
    # the app secret in the server's construction (no route) is not accepted for the app's routes
    token = json.dumps({"handle": handle, "token": FOREIGN, "kind": "update"}).encode()
    plain = signed_headers(app_secret, stamp, token)
    assert relay.client.post("/v1/register/activity", content=token, headers=plain).status_code == 401
    # and the app secret never signs a push
    raw = json.dumps({"handle": handle, "type": "background"}).encode()
    assert (
        relay.client.post("/v1/push", content=raw, headers=signed_headers(app_secret, stamp, raw)).status_code
        == 401
    )
    assert relay.client.post("/v1/register/devices", content=listing, headers=headers).status_code == 200


def test_n2_either_secret_ends_the_handle(relay: Relay) -> None:
    by_app, _ = relay.register()
    by_server, server_secret = relay.register(token="ef" * 32)
    body = {"handle": by_app}
    assert (
        relay.app_signed("/v1/register", relay.app_secrets[by_app], body, method="DELETE").status_code == 204
    )
    body = {"handle": by_server}
    assert relay.signed("/v1/register", server_secret, body, method="DELETE").status_code == 204
    assert (
        relay.push(by_app, b"\0" * 32).status_code == 410
        and relay.push(by_server, server_secret).status_code == 410
    )


# --- N3: a copied transaction cannot lock the subscriber out ------------------------------------------------
def _fill(relay: Relay, **tx: Any) -> list[tuple[str, bytes]]:
    """The attacker, with a copy of the subscriber's transaction, fills the free slots, paced."""
    made: list[tuple[str, bytes]] = []
    while True:
        for _ in range(relay.settings.register_per_subscription_hour):
            response = relay.try_register(token=secrets.token_hex(32), **tx)
            if response.status_code != 201:
                return made
            made.append((response.json()["handle"], b""))
        relay.clock.now += 3600


def test_n3_without_the_account_key_copies_in_use_wait_for_idleness_not_for_order(relay: Relay) -> None:
    """A purchase without an appAccountToken: the subscriber's phone sees the copies but cannot remove them
    while they are in use (which phone came first decides nothing, finding N3b of the first security review);
    once they have been idle for 30 days they go, by removal or by the next registration's eviction."""
    year: dict[str, Any] = {"expiresDate": int(NOW.timestamp() * 1000) + 365 * 86_400_000}
    phone, _ = relay.register(token="ab" * 32, **year)  # the subscriber's phone, registered first
    relay.clock.now += 3600
    copies = _fill(relay, **year)
    assert len(copies) == relay.settings.handles_per_subscription - 1
    relay.clock.now += 20 * 86_400
    assert relay.try_register(token="cd" * 32, **year).status_code == 409  # three weeks later: a new phone
    listing = relay.app_signed("/v1/register/devices", relay.app_secrets[phone], {"handle": phone})
    assert listing.status_code == 200
    devices = listing.json()["devices"]
    assert len(devices) == 10 and sum(d["this_device"] for d in devices) == 1
    assert all(len(d["id"]) == 16 and d["id"] not in {phone, *(h for h, _ in copies)} for d in devices)
    assert all(len(d["created"]) == 10 and d["environment"] == "production" for d in devices)
    copy_ids = [d["id"] for d in devices if not d["this_device"]]
    refused = relay.app_signed(
        "/v1/register/devices/remove", relay.app_secrets[phone], {"handle": phone, "device": copy_ids[0]}
    )
    assert (refused.status_code, refused.json()) == (403, {"error": "not_allowed"})
    relay.clock.now += 10 * 86_400  # the copies, never used, are now idle for 30 days
    removed = relay.app_signed(
        "/v1/register/devices/remove", relay.app_secrets[phone], {"handle": phone, "device": copy_ids[0]}
    )
    assert removed.status_code == 204
    assert relay.try_register(token="cd" * 32, **year).status_code == 201


def test_n3b_an_older_copied_registration_cannot_remove_the_subscriber_s_newly_registered_phone(
    relay: Relay,
) -> None:
    attacker, _ = relay.register(token=secrets.token_hex(32))  # a copy of the transaction, registered first
    relay.clock.now += 2 * 3600
    phone, phone_secret = relay.register(token="ab" * 32)  # the subscriber's new phone, or a re-install
    listed = relay.app_signed("/v1/register/devices", relay.app_secrets[attacker], {"handle": attacker})
    target = next(d for d in listed.json()["devices"] if not d["this_device"])
    relay.clock.now += 1
    removed = relay.app_signed(
        "/v1/register/devices/remove",
        relay.app_secrets[attacker],
        {"handle": attacker, "device": target["id"]},
    )
    assert (removed.status_code, removed.json()) == (403, {"error": "not_allowed"})
    assert relay.push(phone, phone_secret).status_code == 202
    own = next(d for d in listed.json()["devices"] if d["this_device"])
    itself = relay.app_signed(
        "/v1/register/devices/remove",
        relay.app_secrets[attacker],
        {"handle": attacker, "device": own["id"]},
        stamp=int(relay.clock.now) + 1,
    )
    assert itself.status_code == 204  # anyone may remove itself


def test_n3_a_later_copy_cannot_remove_the_subscriber_s_phone_in_use(relay: Relay) -> None:
    phone, phone_secret = relay.register(token="ab" * 32)
    relay.clock.now += 3600
    copy, _ = relay.register(token="ef" * 32)
    mine = relay.app_signed("/v1/register/devices", relay.app_secrets[copy], {"handle": copy}).json()[
        "devices"
    ]
    target = next(d["id"] for d in mine if not d["this_device"])
    removed = relay.app_signed(
        "/v1/register/devices/remove",
        relay.app_secrets[copy],
        {"handle": copy, "device": target},
        stamp=int(relay.clock.now) + 1,
    )
    assert (removed.status_code, removed.json()) == (403, {"error": "not_allowed"})
    assert relay.push(phone, phone_secret).status_code == 202
    unknown = relay.app_signed(
        "/v1/register/devices/remove",
        relay.app_secrets[copy],
        {"handle": copy, "device": "A" * 16},
        stamp=int(relay.clock.now) + 2,
    )
    assert unknown.status_code == 404


ACCOUNT_KEY = bytes(range(32))
ACCOUNT_VECTOR = "55ba834e-40eb-8f96-88a3-4ac1fd42cf59"  # SHA-256(label + key)[:16] as UUIDv8


def _bound(**changes: Any) -> dict[str, Any]:
    from pisa_relay.app import account_token

    return {"appAccountToken": account_token(ACCOUNT_KEY).upper()} | changes  # Apple's UUIDs: any case


def test_n3_a_purchase_bound_to_the_app_s_account_key_needs_the_key_to_register(relay: Relay) -> None:
    def register(account_key: bytes | None, token: str) -> Any:
        body = {
            "apns_token": token,
            "environment": "production",
            "signed_transaction": relay.pki.sign(
                transaction(signedDate=int(relay.clock.now * 1000), **_bound())
            ),
        }
        if account_key is not None:
            body["account_key"] = b64url(account_key)
        return relay.client.post("/v1/register", json=body)

    for key in (None, b"\1" * 32):  # a copy of the transaction, without the key or with a guess
        response = register(key, secrets.token_hex(32))
        assert (response.status_code, response.json()) == (403, {"error": "account_not_verified"})
    for _ in range(relay.settings.register_per_subscription_hour):  # the copier cannot burn the hourly limit
        assert register(None, secrets.token_hex(32)).status_code == 403
    assert register(ACCOUNT_KEY, "ab" * 32).status_code == 201  # the subscriber's phones share the key


def test_n3_an_account_bound_phone_may_remove_an_older_one(relay: Relay) -> None:
    old = relay.client.post(
        "/v1/register",
        json={
            "apns_token": "ab" * 32,
            "environment": "production",
            "account_key": b64url(ACCOUNT_KEY),
            "signed_transaction": relay.pki.sign(
                transaction(signedDate=int(relay.clock.now * 1000), **_bound())
            ),
        },
    )
    relay.clock.now += 3600
    new = relay.client.post(
        "/v1/register",
        json={
            "apns_token": "cd" * 32,
            "environment": "production",
            "account_key": b64url(ACCOUNT_KEY),
            "signed_transaction": relay.pki.sign(
                transaction(signedDate=int(relay.clock.now * 1000), **_bound())
            ),
        },
    )
    handle, app_secret = new.json()["handle"], unb64url(new.json()["app_secret"])
    devices = relay.app_signed("/v1/register/devices", app_secret, {"handle": handle}).json()["devices"]
    target = next(d["id"] for d in devices if not d["this_device"])
    assert (
        relay.app_signed(
            "/v1/register/devices/remove",
            app_secret,
            {"handle": handle, "device": target},
            stamp=int(relay.clock.now) + 1,
        ).status_code
        == 204
    )
    assert relay.push(old.json()["handle"], unb64url(old.json()["secret"])).status_code == 410


def test_n3_the_account_token_derivation_is_fixed() -> None:
    """The app sets `appAccountToken` at purchase from its account key; this vector pins the derivation."""
    from pisa_relay.app import account_token

    assert account_token(ACCOUNT_KEY) == ACCOUNT_VECTOR
