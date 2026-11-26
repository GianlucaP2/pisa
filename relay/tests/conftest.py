# SPDX-License-Identifier: AGPL-3.0-only
"""The relay under test: a test App Store PKI pinned in place of Apple's root, a fake APNs (an httpx mock
transport that records every request), a clock the test moves, and helpers that sign like a Pisa server."""

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pydantic import SecretStr

from pisa_relay.apns import Apns
from pisa_relay.app import create_app
from pisa_relay.settings import Settings
from tests.pki import Pki, make_pki

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
BUNDLE = "com.example.pisa"
PRODUCT = "com.example.pisa.plus.monthly"
APNS_TOKEN = "ab" * 32
DAY_MS = 86_400_000
REPO = Path(__file__).resolve().parents[2]
# The contract's own vectors (CONTRACT-2 §9), read where the contract keeps them: never a copy that can drift.
VECTORS: dict[str, Any] = json.loads(
    (REPO / "protocol" / "v2" / "fixtures" / "pairing" / "vectors.json").read_text()
)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signed_headers(secret: bytes, stamp: int, body: bytes, context: bytes = b"") -> dict[str, str]:
    """What the Pisa server sends (relay/PROTOCOL.md): HMAC-SHA256 under sha256(secret). With `context`, what
    the app sends: the same, over `pisa-relay-app/1\\n<METHOD> <path>\\n` first."""
    key = hashlib.sha256(secret).digest()
    mac = hmac.new(key, context + str(stamp).encode() + b"\n" + body, hashlib.sha256).digest()
    return {
        "X-Pisa-Relay-Timestamp": str(stamp),
        "X-Pisa-Relay-Signature": b64url(mac),
        "Content-Type": "application/json",
    }


def transaction(**changes: Any) -> dict[str, Any]:
    """A subscription transaction as the App Store signs it (the fields the relay reads, and a few others)."""
    now_ms = int(NOW.timestamp() * 1000)
    value = {
        "transactionId": "2000000000000002",
        "originalTransactionId": "2000000000000001",
        "bundleId": BUNDLE,
        "productId": PRODUCT,
        "purchaseDate": now_ms - DAY_MS,
        "expiresDate": now_ms + 30 * DAY_MS,
        "type": "Auto-Renewable Subscription",
        "environment": "Production",
        "signedDate": now_ms,
    }
    return value | changes


def app_headers(app_secret: bytes, stamp: int, method: str, path: str, body: bytes) -> dict[str, str]:
    return signed_headers(app_secret, stamp, body, b"pisa-relay-app/1\n" + f"{method} {path}\n".encode())


class Clock:
    def __init__(self) -> None:
        self.now = NOW.timestamp()

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeApns:
    """Answers as APNs would; `answers` queues (status, reason) for the next requests, else 200."""

    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    answers: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, reason = self.answers.pop(0) if self.answers else (200, "")
        return httpx.Response(status, json={"reason": reason} if reason else None)

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@dataclass
class Relay:
    client: TestClient
    apns: FakeApns
    clock: Clock
    pki: Pki
    settings: Settings
    app_secrets: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def register(
        self,
        *,
        token: str = APNS_TOKEN,
        environment: str = "production",
        language: str | None = None,
        **tx: Any,
    ) -> tuple[str, bytes]:
        response = self.try_register(token=token, environment=environment, language=language, **tx)
        assert response.status_code == 201, response.text
        data = response.json()
        self.app_secrets[data["handle"]] = unb64url(data["app_secret"])
        return data["handle"], unb64url(data["secret"])

    def try_register(
        self,
        *,
        token: str = APNS_TOKEN,
        environment: str = "production",
        language: str | None = None,
        renewal: dict[str, Any] | None = None,
        **tx: Any,
    ) -> httpx.Response:
        tx.setdefault("signedDate", int(self.clock.now * 1000))  # StoreKit signs a fresh one
        body: dict[str, Any] = {
            "apns_token": token,
            "environment": environment,
            "signed_transaction": self.pki.sign(transaction(**tx)),
        }
        if language:
            body["language"] = language
        if renewal is not None:
            body["signed_renewal_info"] = self.pki.sign(renewal)
        return self.client.post("/v1/register", json=body)

    def signed(
        self,
        path: str,
        secret: bytes,
        body: dict[str, Any],
        *,
        method: str = "POST",
        stamp: int | None = None,
    ) -> httpx.Response:
        raw = json.dumps(body).encode()
        stamp = int(self.clock.now) if stamp is None else stamp
        return self.client.request(method, path, content=raw, headers=signed_headers(secret, stamp, raw))

    def app_signed(
        self,
        path: str,
        app_secret: bytes,
        body: dict[str, Any],
        *,
        method: str = "POST",
        stamp: int | None = None,
    ) -> httpx.Response:
        """A request only the app signs (with its app secret, over the method and path too)."""
        raw = json.dumps(body).encode()
        stamp = int(self.clock.now) if stamp is None else stamp
        headers = app_headers(app_secret, stamp, method, path, raw)
        return self.client.request(method, path, content=raw, headers=headers)

    def activity_token(
        self, handle: str, _secret: bytes, token: str, kind: str = "update", *, stamp: int | None = None
    ) -> httpx.Response:
        """The app tells the relay a Live Activity token of its own (push-to-start or an activity's), signed
        with the handle's app secret (the server's secret, the second argument, cannot do it)."""
        body = {"handle": handle, "token": token, "kind": kind}
        return self.app_signed("/v1/register/activity", self.app_secrets[handle], body, stamp=stamp)

    def push(
        self, handle: str, secret: bytes, body: dict[str, Any] | None = None, *, stamp: int | None = None
    ) -> httpx.Response:
        raw = json.dumps(body or {"handle": handle, "type": "background"}).encode()
        stamp = int(self.clock.now) if stamp is None else stamp
        return self.client.post("/v1/push", content=raw, headers=signed_headers(secret, stamp, raw))


def p8_key() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def settings_for(tmp_path: Path, **changes: Any) -> Settings:
    values: dict[str, Any] = {
        "database": str(tmp_path / "relay.sqlite3"),
        "storage_key": SecretStr(b64url(secrets.token_bytes(32))),
        "bundle_id": BUNDLE,
        "product_ids": f"{PRODUCT},com.example.pisa.plus.yearly",
        "apns_key_id": "TESTKEY123",
        "apns_team_id": "TESTTEAM12",
    }
    return Settings(**(values | changes))


@pytest.fixture
def make_relay(tmp_path: Path) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(**changes: Any) -> Relay:
        clock, fake, pki = Clock(), FakeApns(), make_pki(NOW)
        settings = settings_for(tmp_path, **changes)
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        apns = Apns(http, p8_key(), settings.apns_key_id, settings.apns_team_id, BUNDLE, clock=clock)
        client = TestClient(create_app(settings, apns, root=pki.root, clock=clock)).__enter__()
        clients.append(client)
        return Relay(client, fake, clock, pki, settings)

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def relay(make_relay: Any) -> Relay:
    return make_relay()
