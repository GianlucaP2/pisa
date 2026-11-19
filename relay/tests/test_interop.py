# SPDX-License-Identifier: AGPL-3.0-only
"""Interop: the Pisa server's own sender (`core/pisa/domain/push_seal.py`, `core/pisa/connectors/
push_relay.py`, `core/pisa/domain/live_activity.py`), unchanged, against this relay.

A notice sealed by the server's code is accepted and reaches Apple byte for byte, and opens on the "phone"
with the device's key; a background wake and a Live Activity update pass; a replay and an old timestamp are
refused; a forgotten handle reads as `token_invalid` on the server, which then drops the registration.
The relay checks what CONTRACT-2 §9 pins (`Content-Type: application/json`, the two `X-Pisa-Relay-*` headers);
the server's sender sends exactly that."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tests.conftest import NOW, Relay, unb64url

CORE = Path(__file__).resolve().parents[2] / "core"
if not (CORE / "pisa" / "domain" / "push_seal.py").exists():
    pytest.skip("the Pisa server's code (core/) is not beside the relay", allow_module_level=True)
sys.path.insert(0, str(CORE))

from pisa.connectors.push_relay import RelayClient  # noqa: E402
from pisa.domain import live_activity, push_seal  # noqa: E402

RELAY = "https://relay.example.net"
NOTICE = {
    "noticeId": "44444444-4444-4444-8444-444444444444",
    "kind": "reminder",
    "priority": "normal",
    "lang": "it-IT",
    "text": "Tra 15 minuti: Dentista (Via Immaginaria 1).",
    "privacy": "health",
    "genericText": "Un promemoria da Pisa",
}
APNS_META = {"category": "NOTICE", "threadId": "reminder", "interruption": "time-sensitive"}


def device_keys() -> tuple[bytes, bytes]:
    private = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    return (
        private.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()),
        private.public_key().public_bytes(raw, serialization.PublicFormat.Raw),
    )


def send(relay: Relay, secret: bytes, body: bytes, clock: Any = None) -> Any:
    async def go() -> Any:
        transport = httpx.ASGITransport(app=relay.client.app)
        async with httpx.AsyncClient(transport=transport) as http:
            return await RelayClient(http, RELAY, clock=clock or relay.clock).send(secret, body)

    return asyncio.run(go())


def test_a_notice_sealed_by_the_server_reaches_apple_unchanged_and_opens_on_the_phone(relay: Relay) -> None:
    handle, secret = relay.register(language="it")
    private, public = device_keys()
    sealed = push_seal.seal(push_seal.plaintext(NOTICE, APNS_META, sent_at=NOW), public, handle)
    body = push_seal.relay_request(handle, "alert", sealed=sealed, collapse_id=NOTICE["noticeId"])
    assert send(relay, secret, body).status == "sent"
    (request,) = relay.apns.requests
    payload = json.loads(request.content)
    assert payload["sealed"] == sealed and len(unb64url(sealed)) == 512
    assert payload["aps"] == {"alert": {"title": "Pisa", "body": "Nuovo avviso"}, "mutable-content": 1}
    assert request.headers["apns-collapse-id"] == NOTICE["noticeId"]
    for personal in (b"Dentista", b"Roma", b"promemoria", b"health", b"reminder"):
        assert personal not in request.content  # nothing of the notice leaves but the blob
    opened = push_seal.open_sealed(payload["sealed"], private, handle)
    assert opened == {"v": 2, "notice": NOTICE, "sentAt": "2026-11-18T09:00:00Z", "apns": APNS_META}


def test_a_long_notice_s_2_kb_blob_passes_too(relay: Relay) -> None:
    handle, secret = relay.register()
    private, public = device_keys()
    long_notice = push_seal.plaintext(NOTICE | {"text": "x" * 1500}, sent_at=NOW)
    sealed = push_seal.seal(long_notice, public, handle)
    assert send(relay, secret, push_seal.relay_request(handle, "alert", sealed=sealed)).status == "sent"
    assert len(unb64url(relay.apns.payloads()[0]["sealed"])) == 2048
    assert (
        push_seal.open_sealed(relay.apns.payloads()[0]["sealed"], private, handle)["notice"]["text"]
        == "x" * 1500
    )


def test_a_background_wake_and_a_live_activity_update_from_the_server_pass(relay: Relay) -> None:
    handle, secret = relay.register()
    assert send(relay, secret, push_seal.relay_request(handle, "background")).status == "sent"
    now = NOW
    stamp = int(now.timestamp())
    for kind in ("start", "update"):  # the app tells the relay its own Live Activity tokens
        relay.clock.now += 1
        assert relay.activity_token(handle, secret, "cd" * 40, kind).status_code == 204
    statuses = []
    for i, event in enumerate(("start", "update", "end")):  # built the way the server's tick builds it
        activity = live_activity.relay_activity("cd" * 40, event, 10, stamp + i)
        # no progress, no time, no stale date (finding R5 of the first security review)
        assert set(activity) <= live_activity.RELAY_FIELDS
        body = push_seal.relay_request(handle, "liveactivity", activity=activity)
        relay.clock.now += 1
        statuses.append(send(relay, secret, body).status)
    assert relay.apns.payloads()[0] == {"aps": {"content-available": 1}}
    assert statuses == ["sent"] * 3
    start, update, end = (p["aps"] for p in relay.apns.payloads()[1:])
    assert update == {
        "timestamp": stamp + 1,
        "event": "update",
        "content-state": {},
    }  # relay.liveActivityApsRelayMode
    assert start == {
        "timestamp": stamp,
        "event": "start",
        "content-state": {},
        "attributes-type": live_activity.ATTRIBUTES_TYPE,
        "attributes": {},
        "input-push-token": 1,
    }
    assert end == {"timestamp": stamp + 2, "event": "end", "content-state": {}, "dismissal-date": stamp + 2}


def test_replays_old_timestamps_and_forgotten_handles_as_the_server_sees_them(relay: Relay) -> None:
    handle, secret = relay.register()
    body = push_seal.relay_request(handle, "background")
    stamp = int(relay.clock.now)
    assert send(relay, secret, body, clock=lambda: stamp).status == "sent"
    # the same request again (the same second, the same body): accepted, and Apple gets it once
    assert send(relay, secret, body, clock=lambda: stamp).status == "sent"
    assert len(relay.apns.requests) == 1
    assert send(relay, secret, body, clock=lambda: stamp - 301).status == "failed"

    # the app forgets itself (Unpair and delete); the server's next push hears it and drops the registration
    forget = json.dumps({"handle": handle}).encode()
    headers = push_seal.relay_headers(secret, stamp + 1, forget)
    assert relay.client.request("DELETE", "/v1/register", content=forget, headers=headers).status_code == 204
    assert send(relay, secret, body, clock=lambda: stamp + 2).status == "token_invalid"


def test_the_server_and_the_relay_agree_on_the_protocol_s_constants() -> None:
    from pisa_relay import app

    assert push_seal.REPLAY_WINDOW_S == app.REPLAY_WINDOW_S
    assert (push_seal.TIMESTAMP_HEADER, push_seal.SIGNATURE_HEADER) == (
        app.TIMESTAMP_HEADER,
        app.SIGNATURE_HEADER,
    )
    assert push_seal.BUCKETS == (512, 2048)
    assert live_activity.ATTRIBUTES_TYPE == "NextStopAttributes"
    assert datetime.now(UTC).year >= 2026
