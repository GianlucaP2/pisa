"""QR pairing, the device key and session tokens, and the server's endpoints (CONTRACT-2 §3.4, §4.3). Pure: no
I/O.

The bytes both sides compute:
- the QR: `pisa://pair?v=1&addrs=<a1>,<a2>&spki=<pin>&token=<token>&exp=<unix seconds>`, each address
  percent-encoded (a comma inside one is `%2C`); `token` is 16 random bytes in base32 without padding (26
  characters, A-Z and 2-7), also the typed fallback;
- the pin: standard base64 (with its `=`) of SHA-256 over the TLS key's SubjectPublicKeyInfo (DER), as
  `$defs/s_pisa_endpoints` writes it;
- the transcript: `pisa-pair/1`, the pin, the device key and the client nonce, each as sent, joined by `\\n`;
- the app's MAC: HMAC-SHA256 keyed by the token's 16 bytes over the transcript; the server's MAC: the same
  over the transcript, `\\n` and the server nonce; both base64url without padding;
- the commitment (`POST /v1/pair/begin`, before the server sees the transcript): base64url of SHA-256 over
  `pisa-sas-commit/1`, `\n` and the server nonce; `POST /v1/pair` reveals the nonce, and the app checks it
  against the commitment before it shows any digits;
- the SAS: SHA-256 over `pisa-sas/1`, `\\n`, the transcript, `\\n` and the server nonce; its first four bytes
  as a big-endian number, modulo 1 000 000, written as 6 digits;
- the session text the device key signs: `pisa-session/1\\n<deviceId>\\n<nonce>` (ECDSA P-256 with SHA-256,
  the signature DER).
Every other binary value is base64url without padding; a value the app sends is read in either base64
alphabet, with or without padding.
"""

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import secrets
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import load_pem_x509_certificate

TOKEN_BYTES = 16
TOKEN_LENGTH = 26  # base32 of 16 bytes, without padding
NONCE_BYTES = 32
MAX_ADDRESSES = 6  # the QR's limit (`$defs/PairingQR`)
PAIR_LABEL = "pisa-pair/1"
SAS_LABEL = "pisa-sas/1"
COMMIT_LABEL = "pisa-sas-commit/1"
SESSION_LABEL = "pisa-session/1"
QR_SCHEME = "pisa://pair"
# a device's own key is named `device-<its device id>` (a client signs under it; the phone-key store names a
# signer device's key so)
DEVICE_KEY_PREFIX = "device-"
POLL_S = 1.0  # how often a side waiting on a pairing (`pisa pair`, a client) looks at its state again
_ULA = ipaddress.ip_network("fc00::/7")  # IPv6 unique local addresses: a home network's own


def device_key_id(device_id: object) -> str:
    """The key id of a device's own key."""
    return f"{DEVICE_KEY_PREFIX}{device_id}"


class PairingInputError(ValueError):
    """A value the app sent is not what the contract says (the message names it)."""


def b64url(raw: bytes) -> str:
    """Base64url without padding: the contract's binary values."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str, what: str) -> bytes:
    """A base64 value as an app may send it: either alphabet, padded or not."""
    clean = text.strip().replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(clean + "=" * (-len(clean) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise PairingInputError(f"{what} is not base64.") from None


def new_token() -> str:
    return base64.b32encode(secrets.token_bytes(TOKEN_BYTES)).decode().rstrip("=")


def token_bytes(token: str) -> bytes:
    """The token's 16 bytes from its 26 characters (typed: any case, spaces and dashes ignored)."""
    clean = "".join(c for c in token.upper() if c not in " -")
    if len(clean) != TOKEN_LENGTH or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in clean):
        raise PairingInputError(f"A pairing token is {TOKEN_LENGTH} letters and digits 2-7.")
    return base64.b32decode(clean + "======")


def new_nonce() -> str:
    return b64url(secrets.token_bytes(NONCE_BYTES))


def transcript(spki: str, device_key: str, client_nonce: str) -> bytes:
    return "\n".join((PAIR_LABEL, spki, device_key, client_nonce)).encode()


def client_mac(token: bytes, text: bytes) -> str:
    return b64url(hmac.new(token, text, hashlib.sha256).digest())


def server_mac(token: bytes, text: bytes, server_nonce: str) -> str:
    return b64url(hmac.new(token, text + b"\n" + server_nonce.encode(), hashlib.sha256).digest())


def macs_equal(sent: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(unb64(sent, "mac"), unb64(expected, "mac"))
    except PairingInputError:
        return False


def sas(text: bytes, server_nonce: str) -> str:
    digest = hashlib.sha256(SAS_LABEL.encode() + b"\n" + text + b"\n" + server_nonce.encode()).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


def commitment(server_nonce: str) -> str:
    """The server's commitment to its nonce, sent by `POST /v1/pair/begin` before it sees the app's
    transcript:
    base64url (no padding) of SHA-256 over `pisa-sas-commit/1`, `\n`, serverNonce (CONTRACT-2 §15 S1)."""
    return b64url(hashlib.sha256(COMMIT_LABEL.encode() + b"\n" + server_nonce.encode()).digest())


def reveal_matches(committed: str, server_nonce: str) -> bool:
    """The revealed serverNonce hashes to the commitment: checked before any digits are shown."""
    return hmac.compare_digest(commitment(server_nonce), committed)


def session_text(device_id: str, nonce: str) -> bytes:
    return f"{SESSION_LABEL}\n{device_id}\n{nonce}".encode()


def device_key(text: str) -> tuple[bytes, ec.EllipticCurvePublicKey]:
    """The app's device key: a P-256 public key as SubjectPublicKeyInfo DER, base64. Its DER bytes and the
    key. (An X9.63 point, what `SecKeyCopyExternalRepresentation` gives, is taken too and stored as SPKI.)"""
    raw = unb64(text, "deviceKey")
    try:
        if raw[:1] == b"\x04" and len(raw) == 65:
            key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        else:
            loaded = serialization.load_der_public_key(raw)
            if not isinstance(loaded, ec.EllipticCurvePublicKey) or not isinstance(
                loaded.curve, ec.SECP256R1
            ):
                raise ValueError("not P-256")
            key = loaded
    except ValueError:
        raise PairingInputError("deviceKey must be a P-256 public key (SubjectPublicKeyInfo DER).") from None
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return der, key


def _p256(der: bytes) -> ec.EllipticCurvePublicKey:
    """A DER SubjectPublicKeyInfo that is a P-256 key, checked explicitly (never an `assert`, which
    `python -O` strips). ValueError for anything else."""
    key = serialization.load_der_public_key(der)
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a P-256 public key")
    return key


def x963(der: bytes) -> bytes:
    """A stored device key as the X9.63 point the phone-key store keeps."""
    return _p256(der).public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def verify_session(der: bytes, device_id: str, nonce: str, signature: str) -> bool:
    """The device key's signature over the session text. False for a stored key that is not P-256 too."""
    try:
        key = _p256(der)
        key.verify(unb64(signature, "signature"), session_text(device_id, nonce), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, PairingInputError, ValueError):
        return False
    return True


# --- the server's TLS pins and addresses ------------------------------------------------------------------
def spki_pin(pem: str) -> str:
    """The pin of a PEM certificate or public key: base64 of SHA-256 over its SubjectPublicKeyInfo."""
    data = pem.encode()
    if b"CERTIFICATE" in data:
        public = load_pem_x509_certificate(data).public_key()
    else:
        public = serialization.load_pem_public_key(data)
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


# The pin a server in insecure loopback mode (PISA_INSECURE_LOOPBACK_PAIRING, a developer's machine) puts
# in its QR, its endpoints and the transcript: the hash of a fixed label, which is no TLS key's
# SubjectPublicKeyInfo (finding one would be a SHA-256 preimage), so it can never be mistaken for a pinned
# channel. Such a server is reached over plain http on loopback only; the HMAC under the token and the SAS
# still authenticate pairing.
INSECURE_LOOPBACK_PIN = base64.b64encode(hashlib.sha256(b"pisa-insecure-loopback/1").digest()).decode()


def is_loopback_url(address: str) -> bool:
    """An `http://` URL whose host is a loopback address (127.0.0.0/8, ::1) or `localhost`."""
    try:
        parts = urlsplit(address.strip())
        host = parts.hostname
        parts.port  # noqa: B018 (a bad port raises ValueError)
    except ValueError:
        return False
    if parts.scheme != "http" or not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tls_pem(value: str) -> str | None:
    """A PISA_TLS_CERT given inline (the PEM itself, its newlines real or written `\\n`), else None (a
    path)."""
    text = value.strip()
    if not text.startswith("-----BEGIN "):
        return None
    return text.replace("\\n", "\n") + "\n"


def spki_pin_der(der_certificate: bytes) -> str:
    """The pin of a DER certificate (what a TLS handshake presents)."""
    from cryptography.x509 import load_der_x509_certificate

    public = load_der_x509_certificate(der_certificate).public_key()
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def endpoint(address: str) -> dict[str, Any]:
    """An address of the server as `$defs/s_pisa_endpoints` lists it. `relay://<host>/<handle>` is a blind
    relay; an `http(s)://` URL is LAN (a private, link-local or .local host), IPv6, IPv4, or a host name."""
    parts = urlsplit(address.strip())
    if parts.scheme == "relay":
        handle = parts.path.strip("/")
        if not parts.hostname or not handle:
            raise PairingInputError(f"{address!r}: a relay address is relay://<host>/<handle>.")
        out: dict[str, Any] = {"kind": "relay", "address": parts.hostname, "relayHandle": handle[:64]}
        if parts.port:
            out["port"] = parts.port
        return out
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PairingInputError(f"{address!r} is not an http(s):// or relay:// address.")
    host = parts.hostname
    kind = "host"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_loopback or (ip.version == 4 and ip.is_private) or ip in _ULA:
            kind = "lan"
        else:
            kind = "ipv6" if ip.version == 6 else "ipv4"
    except ValueError:
        if host.endswith((".local", ".lan", ".home.arpa")):
            kind = "lan"
    out = {"kind": kind, "address": host}
    port = parts.port or (443 if parts.scheme == "https" else 80)
    out["port"] = port
    return out


def endpoints(addresses: Sequence[str], pins: Sequence[str]) -> dict[str, Any]:
    """`$defs/s_pisa_endpoints`: the addresses, the current pin and the pre-announced next one, and a revision
    that changes when any of them does."""
    listed = [endpoint(a) for a in addresses][:8]
    revision = hashlib.sha256(
        json.dumps([listed, list(pins)], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {"revision": revision, "endpoints": listed, "spki": list(pins[:2])}


def qr_uri(addresses: Sequence[str], spki: str, token: str, exp: int) -> str:
    addrs = ",".join(quote(a, safe=":/[]@") for a in addresses[:MAX_ADDRESSES])
    return f"{QR_SCHEME}?v=1&addrs={addrs}&spki={quote(spki, safe='')}&token={token}&exp={exp}"


def parse_qr(uri: str) -> dict[str, Any]:
    """`$defs/PairingQR` from the QR's text (or a typed token alone: then only `token` is known)."""
    text = uri.strip()
    if not text.startswith(QR_SCHEME + "?"):
        token_bytes(text)
        return {"token": "".join(c for c in text.upper() if c not in " -")}
    # split before decoding: an address's own comma is %2C
    query: dict[str, str] = {}
    for part in urlsplit(text).query.split("&"):
        name, _, value = part.partition("=")
        query.setdefault(name, value)

    def one(name: str) -> str:
        value = query.get(name)
        if not value:
            raise PairingInputError(f"The pairing QR has no {name}.")
        return value

    if one("v") != "1":
        raise PairingInputError("This pairing QR is of another version: update Pisa.")
    addrs = [unquote(a) for a in one("addrs").split(",") if a]
    token = one("token")
    token_bytes(token)
    try:
        exp = int(one("exp"))
    except ValueError:
        raise PairingInputError("The pairing QR's exp is not a number.") from None
    return {"v": 1, "addrs": addrs, "spki": unquote(one("spki")), "token": token, "exp": exp}
