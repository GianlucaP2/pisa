"""Pair a new client with a Pisa server from the pairing QR's text (or the 26-character typed code).

    python pair.py 'pisa://pair?v=1&addrs=...' --name "My client" [--server https://...] [--state FILE]

It makes the device key, proves it holds the QR's one-time token (HMAC), checks the server's commitment and MAC,
shows the 6-digit SAS, and waits until the user confirms the same digits on the server (`pisa pair --confirm`) or
on another paired device. The state file it writes is what every other example reads.
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pisa_example import State, arguments, b64url, checked, tls_context


# [guide:qr]
def parse_qr(text: str) -> dict[str, Any]:
    """`pisa://pair?v=1&addrs=<a1>,<a2>&spki=<pin>&token=<token>&exp=<unix seconds>`, or a typed token alone."""
    text = text.strip()
    if not text.startswith("pisa://pair?"):
        return {"token": "".join(c for c in text.upper() if c not in " -")}  # the typed fallback
    query: dict[str, str] = {}
    for part in urlsplit(text).query.split("&"):  # split first: an address's own comma is %2C
        name, _, value = part.partition("=")
        query.setdefault(name, value)
    if query.get("v") != "1":
        raise ValueError("a pairing QR of another version")
    return {
        "v": 1,
        "addrs": [unquote(a) for a in query["addrs"].split(",") if a],
        "spki": unquote(query["spki"]),  # standard base64 with its '=' (percent-encoded in the QR)
        "token": query["token"],  # 16 random bytes, base32 without padding: 26 letters A-Z and digits 2-7
        "exp": int(query["exp"]),
    }


def token_bytes(token: str) -> bytes:
    return base64.b32decode(token.upper() + "======")


# [/guide:qr]


# [guide:pin]
def spki_pin(der_certificate: bytes) -> str:
    """The pin: standard base64 of SHA-256 over the certificate's SubjectPublicKeyInfo (DER)."""
    from cryptography.x509 import load_der_x509_certificate

    public = load_der_x509_certificate(der_certificate).public_key()
    spki = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def pinned_context(host: str, port: int, pins: list[str]) -> ssl.SSLContext:
    """Trust exactly the certificate whose key is pinned, before anything is sent (no CA, no host name)."""
    pem = ssl.get_server_certificate((host, port), timeout=10)
    if spki_pin(ssl.PEM_cert_to_DER_cert(pem)) not in pins:
        raise SystemExit("The server's TLS key is not the one in the pairing code: nothing was sent.")
    context = ssl.create_default_context(cadata=pem)
    context.check_hostname = False
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


# [/guide:pin]


# [guide:transcript]
def transcript(spki: str, device_key: str, client_nonce: str) -> bytes:
    return "\n".join(("pisa-pair/1", spki, device_key, client_nonce)).encode()


def client_mac(token: bytes, text: bytes) -> str:
    return b64url(hmac.new(token, text, hashlib.sha256).digest())


def server_mac(token: bytes, text: bytes, server_nonce: str) -> str:
    return b64url(hmac.new(token, text + b"\n" + server_nonce.encode(), hashlib.sha256).digest())


def commitment(server_nonce: str) -> str:
    return b64url(hashlib.sha256(b"pisa-sas-commit/1\n" + server_nonce.encode()).digest())


def sas(text: bytes, server_nonce: str) -> str:
    digest = hashlib.sha256(b"pisa-sas/1\n" + text + b"\n" + server_nonce.encode()).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


# [/guide:transcript]


# [guide:device-key]
def new_device_key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """A P-256 key made on the device; the server gets its public half as SubjectPublicKeyInfo DER, base64url."""
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return key, b64url(der)


# [/guide:device-key]


# [guide:pair]
async def pair(http: httpx.AsyncClient, qr: dict[str, Any], device_key: str, name: str) -> dict[str, str]:
    token = token_bytes(qr["token"])
    nonce = b64url(secrets.token_bytes(32))
    text = transcript(qr["spki"], device_key, nonce)
    begun = checked(await http.post("/v1/pair/begin"))  # {pairingId, commitment, expiresAt}
    answer = checked(
        await http.post(
            "/v1/pair",
            json={
                "v": 1,
                "pairingId": begun["pairingId"],
                "deviceKey": device_key,
                "name": name[:60],
                "platform": "other",  # ios, watchos, android, web, cli or other
                "clientNonce": nonce,
                "mac": client_mac(token, text),
            },
        )
    )  # {pairingId, deviceId, serverNonce, mac, state}
    # Both checks come before any digits are shown. If either fails: abort, show nothing, start again.
    if not hmac.compare_digest(commitment(answer["serverNonce"]), begun["commitment"]):
        raise SystemExit("The server revealed another nonce than it committed to: not paired.")
    if not hmac.compare_digest(answer["mac"], server_mac(token, text, answer["serverNonce"])):
        raise SystemExit("The server's answer does not prove it holds the pairing code: not paired.")
    return {
        "pairingId": answer["pairingId"],
        "deviceId": answer["deviceId"],
        "sas": sas(text, answer["serverNonce"]),
    }


async def wait_confirmed(http: httpx.AsyncClient, pairing_id: str, timeout_s: float = 300) -> str:
    """`awaiting_confirmation` until the user answers, then `confirmed`, `refused` or `expired`."""
    deadline = time.monotonic() + timeout_s
    while True:
        state = checked(await http.get(f"/v1/pair/{pairing_id}"))["state"]
        if state != "awaiting_confirmation" or time.monotonic() > deadline:
            return state
        await asyncio.sleep(1)


# [/guide:pair]


async def main() -> int:
    parser = arguments(__doc__ or "")
    parser.add_argument("code", help="the QR's pisa://pair?... text, or the typed code")
    parser.add_argument("--name", default="My client")
    parser.add_argument("--server", help="the server's address (default: the QR's first address)")
    args = parser.parse_args()
    qr = parse_qr(args.code)
    if "addrs" not in qr and not args.server:
        raise SystemExit("A typed code needs --server.")
    if "exp" in qr and qr["exp"] < time.time():
        raise SystemExit("This pairing code has expired: make a new one on the server.")
    server = (args.server or qr["addrs"][0]).rstrip("/")
    if "spki" not in qr:  # a typed code carries no pin: take the key the server shows; the MACs check it
        parts = urlsplit(server)
        if parts.scheme != "https":
            raise SystemExit("A typed code needs the server's https address, to pin the key it shows.")
        pem = ssl.get_server_certificate((parts.hostname or "", parts.port or 443), timeout=10)
        qr["spki"] = spki_pin(ssl.PEM_cert_to_DER_cert(pem))
    pins = [qr["spki"]]
    key, device_key = new_device_key()
    state = State(
        Path(args.state),
        {
            "server": server,
            "spki": pins,
            "key": key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ).decode(),
        },
    )
    async with httpx.AsyncClient(base_url=server, verify=tls_context(server, pins), timeout=30) as http:
        started = await pair(http, qr, device_key, args.name)
        print(f"SAS {started['sas']}", flush=True)  # show it as "123 456"; the user compares it on the server
        print(f"Confirm on the server: pisa pair --confirm {started['sas']}", flush=True)
        final = await wait_confirmed(http, started["pairingId"])
    if final != "confirmed":
        print(f"Not paired: {final}", file=sys.stderr)
        return 1
    state.data["deviceId"] = started["deviceId"]
    state.save()
    print(f"Paired as device {started['deviceId']}; state saved in {state.path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
