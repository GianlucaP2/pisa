# SPDX-License-Identifier: AGPL-3.0-only
"""The first security review's proofs of concept (an independent review; part 1: the push relay), adapted and
inverted: each test makes the reviewer's attack and asserts
that it now fails. Finding ids are the review's (R1 to R12). R5 and R11 are the server's and the contract's
to fix; their tests here pin what the relay itself does about them. Synthetic data only, no network."""

import json
import secrets
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pisa_relay.app import verify_signature
from pisa_relay.storage import Storage, StorageKeyError
from tests.conftest import DAY_MS, NOW, Relay, b64url, signed_headers, transaction, unb64url
from tests.test_appstore import notify

NOW_MS = int(NOW.timestamp() * 1000)
VICTIM_TOKEN = "ab" * 32  # device A: registered under the subscription
OTHER_DEVICE = "c0ffee" * 11 + "c0"  # 64 hex: a device that never registered with this handle
RELAY_DIR = Path(__file__).resolve().parents[1]
CORE = RELAY_DIR.parent / "core"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("no network in these tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def _activity(token: str, **changes: Any) -> dict[str, Any]:
    value = {
        "token": token,
        "event": "update",
        "priority": 10,
        "timestamp": int(NOW.timestamp()),
    }
    return value | changes


# --- R1: a Live Activity push goes only to a token the handle's own app registered --------------------------
def test_r1_a_live_activity_push_to_a_token_the_handle_did_not_register_is_refused(relay: Relay) -> None:
    handle, secret = relay.register(token=VICTIM_TOKEN)
    body = {"handle": handle, "type": "liveactivity", "activity": _activity(OTHER_DEVICE)}
    assert relay.push(handle, secret, body).status_code == 401  # as unknown as a bad signature
    start = _activity(
        OTHER_DEVICE, event="start", attributesType=relay.settings.live_activity_attributes_type
    )
    relay.clock.now += 1
    assert (
        relay.push(handle, secret, {"handle": handle, "type": "liveactivity", "activity": start}).status_code
        == 401
    )
    assert relay.apns.requests == []
    # the app registers its own update token: that one, and only as an update token, is reachable
    assert relay.activity_token(handle, secret, OTHER_DEVICE, "update").status_code == 204
    relay.clock.now += 1
    assert relay.push(handle, secret, body).status_code == 202
    relay.clock.now += 1
    assert (
        relay.push(handle, secret, {"handle": handle, "type": "liveactivity", "activity": start}).status_code
        == 401
    )
    # another handle cannot use it, and cannot register for it without its own secret
    other, other_secret = relay.register(token="ef" * 32)
    body = {"handle": other, "type": "liveactivity", "activity": _activity(OTHER_DEVICE)}
    assert relay.push(other, other_secret, body).status_code == 401
    forged = relay.signed(
        "/v1/register/activity", b"\0" * 32, {"handle": handle, "token": "dd" * 32, "kind": "start"}
    )
    assert forged.status_code == 401
    assert len(relay.apns.requests) == 1


def test_r1_a_foreign_token_gets_the_same_answer_dead_or_alive(relay: Relay) -> None:
    handle, secret = relay.register(token=VICTIM_TOKEN)
    relay.apns.answers.append((410, "Unregistered"))
    body = {"handle": handle, "type": "liveactivity", "activity": _activity(OTHER_DEVICE)}
    assert relay.push(handle, secret, body).status_code == 401  # never 422: Apple is never asked
    assert relay.apns.requests == []


def test_r1_a_handle_keeps_a_handful_of_activity_tokens(relay: Relay) -> None:
    handle, secret = relay.register()
    tokens = [f"{i:02x}" * 32 for i in range(6)]
    for i, token in enumerate(tokens):
        assert relay.activity_token(handle, secret, token, stamp=int(relay.clock.now) + i).status_code == 204
    db = sqlite3.connect(relay.settings.database)
    assert db.execute("SELECT count(*) FROM activity_token").fetchone() == (4,)
    assert OTHER_DEVICE.encode() not in b"".join(
        r[0] for r in db.execute("SELECT token_hash FROM activity_token")
    )


# --- R2: a copied JWS cannot evict the subscriber's phone, and registration is rate-limited -----------------
def test_r2_registrations_from_a_copied_jws_do_not_evict_a_handle_in_use(make_relay: Any) -> None:
    relay: Relay = make_relay(register_per_subscription_hour=1000)
    handle, secret = relay.register(token=VICTIM_TOKEN)
    assert relay.push(handle, secret).status_code == 202  # the victim's phone is in active use today
    relay.clock.now += 5
    answers = [
        relay.try_register(token=secrets.token_hex(32)).status_code
        for _ in range(relay.settings.handles_per_subscription)
    ]
    assert answers == [201] * 9 + [
        409
    ]  # the cap is full of handles used within 30 days: refused, not evicted
    relay.clock.now += 1
    assert relay.push(handle, secret).status_code == 202


def test_r2_registration_is_rate_limited_per_subscription(make_relay: Any) -> None:
    relay: Relay = make_relay()
    answers = [relay.try_register(token=secrets.token_hex(32)).status_code for _ in range(8)]
    assert answers == [201] * 5 + [429] * 3  # five an hour for one subscription
    relay.clock.now += 3600
    assert relay.try_register(token=secrets.token_hex(32)).status_code == 201
    # other subscriptions are not affected: there is no limit across subscriptions (the first security
    # review's re-check, finding N1b)
    others = [
        relay.try_register(token=secrets.token_hex(32), originalTransactionId=str(i)).status_code
        for i in range(20)
    ]
    assert others == [201] * 20


# --- R3: a storage key that does not open the database is an operator error, never "gone" -------------------
def test_r3_a_storage_key_mismatch_never_answers_410(make_relay: Any) -> None:
    first = make_relay()
    handle, secret = first.register()
    with pytest.raises(StorageKeyError):
        make_relay()  # same database file, a different RELAY_STORAGE_KEY: the relay refuses to start
    with pytest.raises(StorageKeyError):
        Storage(first.settings.database, secrets.token_bytes(32))
    assert first.push(handle, secret).status_code == 202  # the right key still works


# --- R4: the pinned Apple root is in the repository ---------------------------------------------------------
def test_r4_the_pinned_apple_root_is_committed_and_not_ignored() -> None:
    root = RELAY_DIR.parent
    if not (root / ".git").exists():
        pytest.skip("not a git checkout (e.g. an archive): the pin's fingerprint test still runs")
    path = "relay/pisa_relay/AppleRootCA-G3.pem"
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", path], capture_output=True, text=True, check=True
    ).stdout
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", path], capture_output=True, text=True)
    assert tracked.strip() == path and ignored.returncode == 1


# --- R5 (the server's): what a Live Activity update carries through the relay -------------------------------
def test_r5_the_relay_forwards_nothing_of_the_day(relay: Relay) -> None:
    handle, secret = relay.register()
    assert (
        relay.activity_token(handle, secret, VICTIM_TOKEN, stamp=int(relay.clock.now) - 1).status_code == 204
    )
    leave_at = int(NOW.timestamp()) + 2 * 3600 + 15 * 60
    leaky = _activity(VICTIM_TOKEN, time=leave_at, progress=0.75)
    assert (
        relay.push(handle, secret, {"handle": handle, "type": "liveactivity", "activity": leaky}).status_code
        == 400
    )
    activity = _activity(VICTIM_TOKEN)
    assert (
        relay.push(
            handle, secret, {"handle": handle, "type": "liveactivity", "activity": activity}
        ).status_code
        == 202
    )
    aps = relay.apns.payloads()[-1]["aps"]
    assert aps == {"timestamp": int(NOW.timestamp()), "event": "update", "content-state": {}}


# --- R6: a JWS from before a refund does not register again; and it must be fresh ---------------------------
def test_r6_a_jws_captured_before_a_refund_does_not_register_after_it(relay: Relay) -> None:
    old_jws = relay.pki.sign(transaction(signedDate=NOW_MS - 300 * DAY_MS))  # signed ten months ago
    recent_jws = relay.pki.sign(
        transaction(signedDate=NOW_MS - DAY_MS)
    )  # signed yesterday, before the refund
    handle, secret = relay.register()
    assert notify(relay, "REFUND") == 200
    assert relay.push(handle, secret).status_code == 410  # the refund ended it...
    for jws in (old_jws, recent_jws):
        again = relay.client.post(
            "/v1/register",
            json={"apns_token": VICTIM_TOKEN, "environment": "production", "signed_transaction": jws},
        )
        assert again.status_code == 403  # ...and neither the old JWS nor a recent one brings it back
    # a renewal after the refund has a later expiry: that one registers
    assert relay.try_register(expiresDate=NOW_MS + 60 * DAY_MS).status_code == 201


def test_r6_a_stale_transaction_is_refused(relay: Relay) -> None:
    assert relay.try_register(signedDate=NOW_MS - 8 * DAY_MS).status_code == 403
    assert relay.try_register(signedDate=NOW_MS + 3600 * 1000).status_code == 403  # from the future
    assert relay.try_register(signedDate=NOW_MS - 6 * DAY_MS).status_code == 201


# --- R7: the same request twice is the same push, sent once -------------------------------------------------
def test_r7_a_second_identical_wake_in_the_same_second_is_accepted_and_sent_once(relay: Relay) -> None:
    handle, secret = relay.register()
    assert relay.push(handle, secret).status_code == 202
    assert relay.push(handle, secret).status_code == 202  # a legitimate second wake: not a 401
    assert len(relay.apns.requests) == 1


# --- R8: the rate limit comes before the replay set ---------------------------------------------------------
def test_r8_requests_past_the_rate_limit_do_not_fill_the_replay_table(make_relay: Any) -> None:
    relay: Relay = make_relay(push_per_minute=1)
    handle, secret = relay.register()
    for i in range(200):
        raw = json.dumps({"handle": handle, "type": "background", "collapse_id": f"x{i}"}).encode()
        relay.client.post("/v1/push", content=raw, headers=signed_headers(secret, int(relay.clock.now), raw))
    db = sqlite3.connect(relay.settings.database)
    (rows,) = db.execute("SELECT count(*) FROM accepted_signature").fetchone()
    assert rows == 1 and len(relay.apns.requests) == 1


# --- R9: subscription references are per StoreKit environment -----------------------------------------------
def test_r9_a_sandbox_notification_does_not_end_a_production_subscription_with_the_same_id(
    make_relay: Any,
) -> None:
    relay: Relay = make_relay(storekit_environments="Production,Sandbox")
    handle, secret = relay.register(originalTransactionId="2000000000000001", environment="production")
    payload = {
        "notificationType": "EXPIRED",
        "notificationUUID": "2" * 8 + "-2222-4222-8222-" + "2" * 12,
        "version": "2.0",
        "signedDate": NOW_MS,
        "data": {
            "bundleId": "com.example.pisa",
            "environment": "Sandbox",
            "signedTransactionInfo": relay.pki.sign(
                transaction(
                    environment="Sandbox",
                    originalTransactionId="2000000000000001",
                    expiresDate=NOW_MS + 31 * DAY_MS,
                )
            ),
        },
    }
    assert (
        relay.client.post(
            "/v1/appstore/notifications", json={"signedPayload": relay.pki.sign(payload)}
        ).status_code
        == 200
    )
    assert relay.push(handle, secret).status_code == 202
    db = sqlite3.connect(relay.settings.database)
    assert db.execute("SELECT storekit_environment FROM registration").fetchall() == [("Production",)]


# --- R10: Apple's billing grace period ----------------------------------------------------------------------
def renewal(**changes: Any) -> dict[str, Any]:
    value = {
        "originalTransactionId": "2000000000000001",
        "autoRenewProductId": "com.example.pisa.plus.monthly",
        "environment": "Production",
        "signedDate": NOW_MS,
        "gracePeriodExpiresDate": NOW_MS + 14 * DAY_MS,
    }
    return value | changes


def test_r10_a_subscriber_in_billing_grace_registers_with_the_renewal_info(relay: Relay) -> None:
    expired = NOW_MS - 2 * DAY_MS  # Apple still grants access: billing grace until 14 days from now
    assert relay.try_register(expiresDate=expired).status_code == 403
    assert (
        relay.try_register(expiresDate=expired, renewal=renewal(originalTransactionId="9")).status_code == 403
    )
    assert (
        relay.try_register(expiresDate=expired, renewal=renewal(gracePeriodExpiresDate=None)).status_code
        == 403
    )
    response = relay.try_register(expiresDate=expired, renewal=renewal())
    assert response.status_code == 201
    handle = response.json()["handle"]
    secret = unb64url(response.json()["secret"])
    relay.clock.now += 10 * 86_400
    assert relay.push(handle, secret).status_code == 202  # still in grace
    relay.clock.now += 6 * 86_400
    assert relay.push(handle, secret).status_code == 410  # grace and the relay's day are over


def test_r10_did_fail_to_renew_in_grace_keeps_the_handles(relay: Relay) -> None:
    handle, secret = relay.register(expiresDate=NOW_MS + DAY_MS)
    payload = {
        "notificationType": "DID_FAIL_TO_RENEW",
        "subtype": "GRACE_PERIOD",
        "version": "2.0",
        "signedDate": NOW_MS,
        "data": {
            "bundleId": "com.example.pisa",
            "environment": "Production",
            "signedTransactionInfo": relay.pki.sign(transaction(expiresDate=NOW_MS + DAY_MS)),
            "signedRenewalInfo": relay.pki.sign(renewal(gracePeriodExpiresDate=NOW_MS + 16 * DAY_MS)),
        },
    }
    assert (
        relay.client.post(
            "/v1/appstore/notifications", json={"signedPayload": relay.pki.sign(payload)}
        ).status_code
        == 200
    )
    relay.clock.now += 10 * 86_400
    assert relay.push(handle, secret).status_code == 202
    assert notify(relay, "GRACE_PERIOD_EXPIRED", expiresDate=NOW_MS + DAY_MS) == 200
    assert relay.push(handle, secret, stamp=int(relay.clock.now) + 1).status_code == 410


# --- R11 (the contract's): the relay cannot see when a sealed notice was made -------------------------------
def test_r11_the_relay_forwards_the_blob_unchanged_and_adds_no_time_of_its_own(relay: Relay) -> None:
    """The fix (`sentAt` in the plaintext, the extension's seen-set) is on the server and the app. The relay
    adds nothing a replayed blob could be told apart by, and cannot open one: the payload is the blob."""
    handle, secret = relay.register()
    sealed = b64url(secrets.token_bytes(512))
    assert (
        relay.push(handle, secret, {"handle": handle, "type": "alert", "sealed": sealed}).status_code == 202
    )
    assert (
        set(relay.apns.payloads()[-1]) == {"aps", "sealed"} and relay.apns.payloads()[-1]["sealed"] == sealed
    )


# --- R12 (accepted): an unknown handle answers 410 before any HMAC work -------------------------------------
def test_r12_unknown_handle_answers_410_live_handle_401(relay: Relay) -> None:
    handle, _ = relay.register()
    raw = json.dumps({"handle": handle, "type": "background"}).encode()
    wrong = signed_headers(b"\0" * 32, int(relay.clock.now), raw)
    assert relay.client.post("/v1/push", content=raw, headers=wrong).status_code == 401  # live
    raw = json.dumps({"handle": "x" * 22, "type": "background"}).encode()
    assert (
        relay.client.post("/v1/push", content=raw, headers=wrong).status_code == 410
    )  # unknown (CONTRACT-2 §9)


# --- conformance (positive): the contract's vectors agree with the relay and the server's sealer ------------
def test_vectors_relay_signature_and_push_seal() -> None:
    from tests.conftest import VECTORS as vectors

    r = vectors["relay"]
    key = bytes.fromhex(r["keyHex"])
    sig = r["headers"]["X-Pisa-Relay-Signature"]
    assert (
        verify_signature(
            key, str(r["timestamp"]), sig, r["body"].encode(), r["timestamp"] + r["windowSeconds"]
        )
        is not None
    )
    assert (
        verify_signature(
            key, str(r["timestamp"]), sig, r["body"].encode(), r["timestamp"] + r["windowSeconds"] + 1
        )
        is None
    )
    if not (CORE / "pisa" / "domain" / "push_seal.py").exists():
        pytest.skip("the Pisa server's code (core/) is not beside the relay")
    sys.path.insert(0, str(CORE))
    from pisa.domain import push_seal

    p = vectors["push"]
    for case in p["cases"]:
        opened = push_seal.open_sealed(case["sealed"], bytes.fromhex(p["recipientPrivateKeyHex"]), p["aad"])
        assert opened == json.loads(case["plaintext"])
