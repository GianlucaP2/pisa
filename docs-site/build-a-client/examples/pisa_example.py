"""What every example of the client guide shares: the client's state file, base64, and Pisa's error envelope.

The state file is what a client keeps after pairing: the server's address, the pins it trusts, the device id and
the device's private key. Keep it like a password (mode 0600 here): the key is the device.

Needs: Python 3.11+, `pip install httpx websockets cryptography` (and `aiortc` for the talk, `pyhpke` for push).
"""

import argparse
import base64
import binascii
import json
import os
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

DEFAULT_STATE = "pisa-client.json"


def b64url(raw: bytes) -> str:
    """Base64url without padding: how contract 2 writes binary values."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    """A base64 value in either alphabet, padded or not (what the server accepts, and what a client should)."""
    clean = text.strip().replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(clean + "=" * (-len(clean) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"not base64: {text[:20]}...") from None


class State:
    """The client's state file: server, pins, device id and private key."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path, self.data = path, data

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        return cls(path, json.loads(path.read_text()))

    def save(self) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(self.data, out, indent=2)

    @property
    def server(self) -> str:
        return self.data["server"]

    @property
    def device_id(self) -> str:
        return self.data["deviceId"]

    @property
    def key(self) -> ec.EllipticCurvePrivateKey:
        key = serialization.load_pem_private_key(self.data["key"].encode(), password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)
        return key


def ws_url(server: str) -> str:
    """The `/v2/live` address of a server (`https` becomes `wss`)."""
    parts = urlsplit(server)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}/v2/live"


def tls_context(server: str, pins: list[str]) -> ssl.SSLContext | bool:
    """Over https: a TLS context that trusts exactly the certificate whose key the pairing QR pinned (see
    pair.py). Over plain http there is nothing to pin: allowed only for a server on this machine."""
    from pair import pinned_context  # the pin check lives with pairing, where the guide explains it

    parts = urlsplit(server)
    if parts.scheme == "https":
        return pinned_context(parts.hostname or "", parts.port or 443, pins)
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing plain http to another machine: use the https address from the QR.")
    return True


def http_client(state: State) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=state.server, verify=tls_context(state.server, state.data.get("spki") or []), timeout=30
    )


# [guide:error-envelope]
class PisaError(Exception):
    """Pisa's error envelope: `{"error": {"code", "message", "action"}}`, with the HTTP status."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        error = body.get("error") or {}
        self.status = status
        self.code = str(error.get("code") or f"http_{status}")
        self.message = str(error.get("message") or "")
        self.action = str(error.get("action") or "")
        self.extra = {k: v for k, v in error.items() if k not in ("code", "message", "action")}
        super().__init__(f"HTTP {status} {self.code}: {self.message}")


def checked(response: httpx.Response) -> Any:
    """The JSON body of a 2xx answer (None for 204); PisaError for anything else."""
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise PisaError(response.status_code, body if isinstance(body, dict) else {})
    return None if response.status_code == 204 or not response.content else response.json()


# [/guide:error-envelope]


def arguments(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--state", default=os.environ.get("PISA_STATE", DEFAULT_STATE), help="the client's state file"
    )
    return parser
