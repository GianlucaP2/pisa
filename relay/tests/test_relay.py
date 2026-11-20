# SPDX-License-Identifier: AGPL-3.0-only
"""The relay's API end to end, against a fake APNs: registration, the signature, the window and replays, rate
limits, the exact payloads Apple gets (nothing but the opaque blob ever leaves), Live Activities by schema,
the 410 path, forgetting, App Store notifications, logs and metrics."""

import base64
import hashlib
import json
import logging
import secrets
import sqlite3
from typing import Any

import jwt
import pytest

from pisa_relay.app import app_from_env, verify_signature
from tests.conftest import APNS_TOKEN, BUNDLE, DAY_MS, NOW, Relay, b64url, signed_headers, transaction

SEALED = b64url(secrets.token_bytes(512))
ACTIVITY_TOKEN = "cd" * 40


def alert(handle: str, **changes: Any) -> dict[str, Any]:
    return {
        "handle": handle,
        "type": "alert",
        "collapse_id": "44444444-4444-4444-8444-444444444444",
        "sealed": SEALED,
    } | changes


def activity(event: str = "update", **changes: Any) -> dict[str, Any]:
    stamp = int(NOW.timestamp())
    value: dict[str, Any] = {
        "token": ACTIVITY_TOKEN,
        "event": event,
        "priority": 10,
        "timestamp": stamp,
    }
    if event == "start":
        value["attributesType"] = "NextStopAttributes"
    if event == "end":
        value["dismissalDate"] = stamp
    return value | changes


# --- registration -------------------------------------------------------------------------------------------
def test_registration_returns_a_random_handle_and_secret_and_keeps_only_a_sealed_hash(relay: Relay) -> None:
    handle, secret = relay.register()
    assert len(base64.urlsafe_b64decode(handle + "==")) == 16 and len(secret) == 32
    other, _ = relay.register(token="ef" * 32)
    assert other != handle
    db = sqlite3.connect(relay.settings.database)
    row = db.execute("SELECT * FROM registration WHERE handle = ?", (handle,)).fetchone()
    stored = b"".join(v if isinstance(v, bytes) else str(v).encode() for v in row)

    assert secret not in stored and hashlib.sha256(secret).digest() not in stored  # sealed at rest
    assert b"2000000000000001" not in stored  # the transaction id only as a keyed hash
    assert row[7] == "2026-11-18T09" and row[8] is None  # the hour, not the time
    assert row[9] == "Production"


@pytest.mark.parametrize(
    "body",
    [
        {"apns_token": "zz", "environment": "production"},
        {"apns_token": APNS_TOKEN, "environment": "development"},
        {"apns_token": APNS_TOKEN, "environment": "production", "extra": 1},
    ],
)
def test_a_malformed_registration_is_400(relay: Relay, body: dict[str, Any]) -> None:
    body = {"signed_transaction": relay.pki.sign(transaction())} | body
    response = relay.client.post("/v1/register", json=body)
    assert (response.status_code, response.json()) == (400, {"error": "malformed"})


def test_a_registration_without_a_valid_subscription_is_403(relay: Relay) -> None:
    for tx in (relay.pki.sign(transaction(expiresDate=int(NOW.timestamp() * 1000) - DAY_MS)), "x.y.z"):
        response = relay.client.post(
            "/v1/register",
            json={"apns_token": APNS_TOKEN, "environment": "production", "signed_transaction": tx},
        )
        assert (response.status_code, response.json()) == (403, {"error": "subscription_not_verified"})
    assert relay.client.post("/v1/register", content=b"x" * 20_000).status_code == 413


def test_the_same_device_re_registering_replaces_its_handle_and_a_subscription_has_a_cap(
    make_relay: Any,
) -> None:
    relay: Relay = make_relay(handles_per_subscription=3, register_per_subscription_hour=100)
    year = int(NOW.timestamp() * 1000) + 365 * DAY_MS
    old, old_secret = relay.register(expiresDate=year)
    new, new_secret = relay.register(expiresDate=year)
    assert relay.push(old, old_secret).status_code == 410
    assert relay.push(new, new_secret).status_code == 202
    others = [relay.register(token=f"{i:02x}" * 32, expiresDate=year) for i in range(2)]
    refused = relay.try_register(token="03" * 32, expiresDate=year)  # all three used within 30 days
    assert (refused.status_code, refused.json()) == (409, {"error": "too_many_devices"})
    assert relay.push(new, new_secret).status_code == 202
    relay.clock.now += 31 * 86_400
    assert relay.push(*others[0]).status_code == 202  # in use; the other two idle for 31 days
    relay.register(token="03" * 32, expiresDate=year)
    assert relay.push(new, new_secret).status_code == 410  # the least recently used idle one made room
    assert relay.push(*others[1], stamp=int(relay.clock.now) + 1).status_code == 202


# --- authentication -----------------------------------------------------------------------------------------
def test_signature_window_and_replay(relay: Relay) -> None:
    handle, secret = relay.register()
    body = json.dumps({"handle": handle, "type": "background"}).encode()
    stamp = int(relay.clock.now)
    headers = signed_headers(secret, stamp, body)
    assert relay.client.post("/v1/push", content=body, headers=headers).status_code == 202
    # the same request again: accepted, never sent twice (two identical wakes in one second are one wake)
    assert relay.client.post("/v1/push", content=body, headers=headers).status_code == 202
    # the same MAC, spelled with other base64url padding bits in its last character, is the same request
    sig = headers["X-Pisa-Relay-Signature"]
    alias = (
        sig[:-1]
        + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[
            ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(sig[-1]) ^ 1)
        ]
    )
    assert (
        relay.client.post(
            "/v1/push", content=body, headers=headers | {"X-Pisa-Relay-Signature": alias}
        ).status_code
        == 202
    )
    assert relay.push(handle, b"\x00" * 32, stamp=stamp + 1).status_code == 401  # another secret
    assert relay.push(handle, secret, stamp=stamp - 301).status_code == 401  # too old
    assert relay.push(handle, secret, stamp=stamp + 301).status_code == 401  # too far ahead
    assert relay.push(handle, secret, stamp=stamp - 300).status_code == 202  # the window's edge
    changed = body.replace(b'"background"', b'"background", "collapse_id": "x"')
    assert (
        relay.client.post(
            "/v1/push", content=changed, headers=signed_headers(secret, stamp + 2, body)
        ).status_code
        == 401
    )
    for bad in (
        {"X-Pisa-Relay-Timestamp": "+1"},
        {"X-Pisa-Relay-Timestamp": ""},
        {"X-Pisa-Relay-Signature": "x"},
    ):
        fresh = signed_headers(secret, stamp + 3, body)
        assert relay.client.post("/v1/push", content=body, headers=fresh | bad).status_code == 401
    assert len(relay.apns.requests) == 2


def test_an_unknown_handle_is_410_and_a_malformed_body_400_before_anything_reaches_apple(
    relay: Relay,
) -> None:
    assert relay.push("nobody", secrets.token_bytes(32)).status_code == 410
    handle, secret = relay.register()
    for body in (
        {"handle": handle, "type": "alert"},  # no blob
        alert(handle, sealed=b64url(secrets.token_bytes(600))),  # not one of the two sizes
        alert(handle, sealed=SEALED + "!"),
        {"handle": handle, "type": "background", "sealed": SEALED},
        {"handle": handle, "type": "background", "text": "hello"},
        {"handle": handle, "type": "silent"},
        alert(handle, collapse_id="Dentist at 3"),
        alert(handle, collapse_id="x" * 65),
        {"handle": "h" * 65, "type": "background"},
    ):
        assert relay.push(handle, secret, body).status_code == 400, body
    raw = b'{"handle": "' + handle.encode() + b'", "type": "alert", "sealed": "' + b"A" * 9000 + b'"}'
    assert (
        relay.client.post(
            "/v1/push", content=raw, headers=signed_headers(secret, int(relay.clock.now), raw)
        ).status_code
        == 413
    )
    assert relay.apns.requests == []


# --- what Apple gets ----------------------------------------------------------------------------------------
def test_an_alert_is_a_generic_banner_with_the_sealed_blob_and_nothing_else(relay: Relay) -> None:
    handle, secret = relay.register()
    assert relay.push(handle, secret, alert(handle)).status_code == 202
    (request,) = relay.apns.requests
    assert str(request.url) == f"https://api.push.apple.com/3/device/{APNS_TOKEN}"
    assert request.headers["apns-topic"] == BUNDLE and request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert request.headers["apns-collapse-id"] == "44444444-4444-4444-8444-444444444444"
    assert json.loads(request.content) == {
        "aps": {"alert": {"title": "Pisa", "body": "New notice"}, "mutable-content": 1},
        "sealed": SEALED,
    }
    bearer = request.headers["authorization"].removeprefix("bearer ")
    assert jwt.get_unverified_header(bearer) == {"alg": "ES256", "kid": "TESTKEY123", "typ": "JWT"}
    assert jwt.decode(bearer, options={"verify_signature": False}) == {
        "iss": "TESTTEAM12",
        "iat": int(NOW.timestamp()),
    }


def test_the_banner_is_in_the_device_s_language_and_the_sandbox_goes_to_the_sandbox(relay: Relay) -> None:
    handle, secret = relay.register(language="it-IT", environment="sandbox")
    other, other_secret = relay.register(token="ef" * 32, language="fr")
    relay.push(handle, secret, alert(handle))
    relay.push(other, other_secret, alert(other))
    first, second = relay.apns.payloads()
    assert first["aps"]["alert"] == {"title": "Pisa", "body": "Nuovo avviso"}
    assert second["aps"]["alert"] == {"title": "Pisa", "body": "New notice"}
    assert relay.apns.requests[0].url.host == "api.sandbox.push.apple.com"


def test_a_background_wake_carries_no_blob(relay: Relay) -> None:
    handle, secret = relay.register()
    assert relay.push(handle, secret).status_code == 202
    (request,) = relay.apns.requests
    assert json.loads(request.content) == {"aps": {"content-available": 1}}
    assert request.headers["apns-push-type"] == "background" and request.headers["apns-priority"] == "5"


def test_live_activity_updates_carry_an_empty_content_state(relay: Relay) -> None:
    handle, secret = relay.register()
    stamp = int(NOW.timestamp())
    assert relay.activity_token(handle, secret, ACTIVITY_TOKEN, "start", stamp=stamp - 2).status_code == 204
    assert relay.activity_token(handle, secret, ACTIVITY_TOKEN, "update", stamp=stamp - 1).status_code == 204
    for i, event in enumerate(("start", "update", "end")):
        body = {"handle": handle, "type": "liveactivity", "activity": activity(event)}
        assert relay.push(handle, secret, body, stamp=stamp + i).status_code == 202
    start, update, end = relay.apns.requests
    assert str(update.url) == f"https://api.push.apple.com/3/device/{ACTIVITY_TOKEN}"
    assert update.headers["apns-topic"] == f"{BUNDLE}.push-type.liveactivity"
    assert update.headers["apns-push-type"] == "liveactivity" and update.headers["apns-priority"] == "10"
    state: dict[str, Any] = {}  # CONTRACT-2 relay.liveActivityApsRelayMode: nothing of the user's day
    assert json.loads(start.content) == {
        "aps": {
            "timestamp": stamp,
            "event": "start",
            "content-state": state,
            "attributes-type": "NextStopAttributes",
            "attributes": {},
            "input-push-token": 1,
        }
    }
    assert json.loads(update.content) == {
        "aps": {"timestamp": stamp, "event": "update", "content-state": state}
    }
    assert json.loads(end.content)["aps"] == {
        "timestamp": stamp,
        "event": "end",
        "content-state": {},
        "dismissal-date": stamp,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Dinner at Marco's"},
        # relay mode carries no progress and no time (finding R5 of the first security review)
        {"progress": 0.5},
        {"time": 1_800_000_000},
        {"contentState": {}},
        {"staleDate": 1_800_000_000},  # a stale date is a time of the user's day too
        {"timestamp": True},
        {"priority": 7},
        {"token": "not hex"},
        {"attributesType": "NextStopAttributes"},  # only with start
        {"dismissalDate": 1},  # only with end
        {"contentState": {"title": "x"}},
    ],
)
def test_a_live_activity_update_with_anything_but_numbers_is_refused(
    relay: Relay, changes: dict[str, Any]
) -> None:
    handle, secret = relay.register()
    body = {"handle": handle, "type": "liveactivity", "activity": activity("update", **changes)}
    assert relay.push(handle, secret, body).status_code == 400
    start = {"handle": handle, "type": "liveactivity", "activity": activity("start", attributesType="Other")}
    assert relay.push(handle, secret, start, stamp=int(relay.clock.now) + 1).status_code == 400
    assert relay.apns.requests == []


# --- Apple's answers ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "reason"), [(410, "Unregistered"), (410, "ExpiredToken"), (400, "BadDeviceToken")]
)
def test_a_token_apple_no_longer_knows_ends_the_handle_and_the_server_hears_410(
    relay: Relay, status: int, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    handle, secret = relay.register()
    relay.apns.answers.append((status, reason))
    with caplog.at_level(logging.INFO):
        assert relay.push(handle, secret, alert(handle)).status_code == 410
    assert relay.push(handle, secret, stamp=int(relay.clock.now) + 1).status_code == 410
    assert len(relay.apns.requests) == 1
    assert handle not in caplog.text and APNS_TOKEN not in caplog.text and reason in caplog.text


def test_a_dead_live_activity_token_does_not_end_the_handle(relay: Relay) -> None:
    handle, secret = relay.register()
    assert (
        relay.activity_token(handle, secret, ACTIVITY_TOKEN, stamp=int(relay.clock.now) - 1).status_code
        == 204
    )
    relay.apns.answers.append((410, "Unregistered"))
    body = {"handle": handle, "type": "liveactivity", "activity": activity()}
    assert relay.push(handle, secret, body).status_code == 422  # its own token: no oracle for others
    assert relay.push(handle, secret, stamp=int(relay.clock.now) + 1).status_code == 202
    assert relay.push(handle, secret, body, stamp=int(relay.clock.now) + 2).status_code == 401  # forgotten


def test_other_apple_failures_are_502_and_keep_the_handle(relay: Relay) -> None:
    handle, secret = relay.register()
    relay.apns.answers += [
        (403, "InvalidProviderToken"),
        (500, "InternalServerError"),
        (400, "PayloadTooLarge"),
        (410, "SomethingNew"),  # a 410 ends the handle only with the push contract's reasons
    ]
    for i in range(4):
        assert relay.push(handle, secret, stamp=int(relay.clock.now) + i).status_code == 502
    assert relay.push(handle, secret, stamp=int(relay.clock.now) + 4).status_code == 202


# --- limits -------------------------------------------------------------------------------------------------
def test_rate_limits_per_minute_and_per_day(make_relay: Any) -> None:
    relay: Relay = make_relay(push_per_minute=3, push_per_day=5)
    handle, secret = relay.register()
    other, other_secret = relay.register(token="ef" * 32)
    stamp = int(relay.clock.now)
    assert [relay.push(handle, secret, stamp=stamp + i).status_code for i in range(4)] == [202, 202, 202, 429]
    assert relay.push(other, other_secret).status_code == 202  # per handle
    relay.clock.now += 60
    stamp = int(relay.clock.now)
    assert [relay.push(handle, secret, stamp=stamp + i).status_code for i in range(3)] == [202, 202, 429]
    relay.clock.now += 86_400
    assert relay.push(handle, secret).status_code == 202
    assert len(relay.apns.requests) == 7


def test_an_expired_subscription_ends_the_handle_after_the_grace(relay: Relay) -> None:
    handle, secret = relay.register(expiresDate=int(NOW.timestamp() * 1000) + DAY_MS)
    relay.clock.now += 2 * 86_400 - 60  # past the expiry, within the day of grace
    assert relay.push(handle, secret).status_code == 202
    relay.clock.now += 120
    assert relay.push(handle, secret).status_code == 410


def test_sweeping_forgets_expired_subscriptions_signatures_and_counters(relay: Relay) -> None:
    handle, secret = relay.register(expiresDate=int(NOW.timestamp() * 1000) + DAY_MS)
    relay.push(handle, secret)
    storage = relay.client.app.state.storage  # type: ignore[attr-defined]
    db = sqlite3.connect(relay.settings.database)
    counts = (
        "SELECT (SELECT count(*) FROM registration), (SELECT count(*) FROM accepted_signature), "
        "(SELECT count(*) FROM usage)"
    )
    assert db.execute(counts).fetchone() == (1, 1, 3)  # two push counters, the subscription's hour
    storage.sweep(relay.clock.now + 3 * 86_400, relay.settings.expiry_grace_s)
    assert db.execute(counts).fetchone() == (0, 0, 0)


# --- forgetting ---------------------------------------------------------------------------------------------
def test_the_app_forgets_itself_with_a_signed_delete(relay: Relay) -> None:
    handle, secret = relay.register()
    body = json.dumps({"handle": handle}).encode()
    stamp = int(relay.clock.now)
    forged = signed_headers(b"\x01" * 32, stamp, body)
    assert relay.client.request("DELETE", "/v1/register", content=body, headers=forged).status_code == 401
    headers = signed_headers(secret, stamp, body)
    assert relay.client.request("DELETE", "/v1/register", content=body, headers=headers).status_code == 204
    assert relay.client.request("DELETE", "/v1/register", content=body, headers=headers).status_code == 410
    assert relay.push(handle, secret, stamp=stamp + 1).status_code == 410


# --- logs and metrics ---------------------------------------------------------------------------------------
def test_nothing_of_a_request_reaches_the_logs_and_metrics_are_counters(
    make_relay: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from pydantic import SecretStr

    relay: Relay = make_relay(metrics_token=SecretStr("metrics-token"))
    with caplog.at_level(logging.DEBUG):
        handle, secret = relay.register()
        relay.push(handle, secret, alert(handle))
        relay.push(handle, secret, alert(handle), stamp=int(relay.clock.now) - 1000)
        relay.apns.answers.append((500, "InternalServerError"))
        relay.push(handle, secret, alert(handle), stamp=int(relay.clock.now) + 1)
    for secret_text in (handle, b64url(secret), SEALED, APNS_TOKEN, "2000000000000001"):
        assert secret_text not in caplog.text
    assert relay.client.get("/metrics").status_code == 404
    text = relay.client.get("/metrics", headers={"authorization": "Bearer metrics-token"}).text
    assert 'pisa_relay_push{type="alert",status="202"} 1' in text
    assert 'pisa_relay_push{type="alert",status="401"} 1' in text
    assert 'pisa_relay_push{type="alert",status="502"} 1' in text
    assert 'pisa_relay_register{status="201"} 1' in text
    assert handle not in text and relay.client.get("/healthz").json() == {"ok": True}


def test_a_refusal_echoes_nothing_of_the_request(relay: Relay) -> None:
    handle, secret = relay.register()
    for response in (
        relay.push(handle, secret, alert(handle, collapse_id="Dentist at 3")),
        relay.push(handle, b"\x00" * 32),
        relay.push("unknown-handle", secret),
    ):
        assert response.content == b""


def test_the_app_builds_from_the_environment_with_an_http2_client(tmp_path: Any, monkeypatch: Any) -> None:
    from tests.conftest import p8_key

    key = tmp_path / "AuthKey_TEST.p8"
    key.write_bytes(p8_key())
    env = {
        "RELAY_DATABASE": str(tmp_path / "r.sqlite3"),
        "RELAY_STORAGE_KEY": b64url(secrets.token_bytes(32)),
        "RELAY_BUNDLE_ID": BUNDLE,
        "RELAY_PRODUCT_IDS": "a,b",
        "RELAY_APNS_KEY_PATH": str(key),
        "RELAY_APNS_KEY_ID": "TESTKEY123",
        "RELAY_APNS_TEAM_ID": "TESTTEAM12",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = app_from_env()
    assert app.state.storage is not None
    monkeypatch.setenv("RELAY_STORAGE_KEY", "short")
    with pytest.raises(ValueError, match="32 random bytes"):
        app_from_env()


# --- CONTRACT-2 §9 ------------------------------------------------------------------------------------------
def test_the_contract_s_relay_vector_verifies() -> None:
    """protocol/v2/fixtures/pairing/vectors.json, `relay` (independent of this code and of the server's)."""
    secret = bytes.fromhex("5ec2e7" + "00" * 29)  # the vector's test secret
    key = hashlib.sha256(secret).digest()
    assert key.hex() == "29e754e7182136842bef82858805a2998f8f69df8a8e4fbad416adec78649c26"
    body = b'{"handle":"h-7Qm2Xc9LkP4tVb","type":"alert","collapse_id":"n-1","sealed":"..."}'
    signature = "dXZnIIEtYaq1SN6G5XAGBK2mrnu4zAOLOPHAovfBv_s"
    stamp = 1763460000
    assert verify_signature(key, str(stamp), signature, body, stamp + 300) is not None
    assert verify_signature(key, str(stamp), signature, body, stamp + 301) is None  # windowSeconds 300
    assert verify_signature(key, str(stamp + 1), signature, body, stamp) is None
    assert verify_signature(key, str(stamp), signature, body + b" ", stamp) is None


def test_the_body_must_be_declared_json_and_header_names_match_in_any_case(relay: Relay) -> None:
    handle, secret = relay.register()
    body = json.dumps({"handle": handle, "type": "background"}).encode()
    stamp = int(relay.clock.now)
    for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
        headers = signed_headers(secret, stamp, body) | {"Content-Type": content_type}
        assert relay.client.post("/v1/push", content=body, headers=headers).status_code == 400
    lower = {k.lower(): v for k, v in signed_headers(secret, stamp, body).items()}
    lower["content-type"] = "application/json; charset=utf-8"
    assert relay.client.post("/v1/push", content=body, headers=lower).status_code == 202
    assert len(relay.apns.requests) == 1


def test_a_storage_key_that_does_not_open_the_database_is_refused_at_start_and_500_never_410(
    make_relay: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from pisa_relay.storage import Storage, StorageKeyError

    relay: Relay = make_relay()
    handle, secret = relay.register()
    with pytest.raises(StorageKeyError):
        Storage(relay.settings.database, secrets.token_bytes(32))  # a typo, a rotation, a restore
    db = sqlite3.connect(relay.settings.database)
    db.execute("UPDATE registration SET secret_hash = ? WHERE handle = ?", (b"\0" * 60, handle))
    db.commit()
    with caplog.at_level(logging.ERROR):
        assert relay.push(handle, secret).status_code == 500  # a fault of the relay, never "gone"
    assert "RELAY_STORAGE_KEY" in caplog.text and relay.apns.requests == []
