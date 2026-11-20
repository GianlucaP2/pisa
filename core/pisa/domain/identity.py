# ruff: noqa: E501
"""Pisa's own identity, step one. Pisa holds an Ed25519 key for itself (never the user's phone
keys: those authorise; this one only identifies) and signs its own outgoing reads with Web Bot Auth:
RFC 9421 HTTP message signatures, a `Signature-Agent` header naming Pisa's URL, a key directory at
`/.well-known/http-message-signatures-directory` and an agent card that describes Pisa with no owner data.


RAIL: what may be signed is decided here, never by the model: a read (GET or HEAD), a UCP catalog search
(exactly `POST …/catalog/search`), and a
UCP cart (one `POST …/checkout-sessions`, which builds a cart the user pays themselves). A checkout's completion,
a payment, an order, any PUT/PATCH/DELETE: never (no action is signed for a purchase yet: the user taps).

Pure: building the signature base and the headers' text; the key itself is used in pisa/services/identity.py.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

Purpose = Literal["read", "ucp_catalog", "ucp_cart"]
TAG = "web-bot-auth"
DIRECTORY_TAG = "http-message-signatures-directory"
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_TYPE = "application/http-message-signatures-directory+json"
CARD_PATH = "/.well-known/agent-card.json"
UCP_PROFILE_PATH = "/.well-known/pisa-ucp-profile.json"
SIGNATURE_TTL_S = 60  # the research's ≤ 60 s
DIRECTORY_MAX_AGE_S = 3600  # a retired key leaves every verifier's cache within the hour
RETIRE_AFTER_S = 2 * DIRECTORY_MAX_AGE_S  # an old key stays published this long after a rotation
UCP_VERSION = "2026-08-25"
LABEL = "sig1"

NAME = "Pisa"
DESCRIPTION = "Pisa, a personal assistant acting for its owner"
# What the card says Pisa does and never does (no owner data: no name, no address, no contact).
CONDUCT = (
    "Reads public pages and merchants' catalogs for one person, at a person's pace.",
    "Never buys, books, sends or pays: it prepares, and its owner confirms with a tap in the merchant's or "
    "the app's own page.",
    "Never masks itself as a browser or a person; every request it signs names this card.",
)

_CHECKOUT_SESSIONS = re.compile(r"^(/[A-Za-z0-9._~%-]+)*/checkout-sessions$")
_CATALOG_SEARCH = re.compile(r"^(/[A-Za-z0-9._~%-]+)*/catalog/search$")
# Words no UCP endpoint's own path may hold (an endpoint like /api/orders/ucp is refused at discovery); the
# signing rail itself rests on the exact paths above, never on this list.
FORBIDDEN = re.compile(r"(complete|payment|pay|order|cancel|confirm|submit|purchase|buy)", re.IGNORECASE)


class NotSignable(ValueError):
    """RAIL: Pisa was asked to sign what it never signs (a purchase, a payment, a non-https URL)."""


@dataclass(frozen=True)
class SignedFor:
    """What a signature covers: the request's method, authority (host[:port]) and path (with the query)."""

    method: str
    authority: str
    path: str


def may_sign(method: str, url: str, purpose: Purpose) -> bool:
    """RAIL: Pisa signs only a read, a catalog search, or a cart's creation; https only. A checkout's completion,
    a payment or an order is never signed, whatever the purpose says."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    method = method.upper()
    if purpose == "read":
        return method in ("GET", "HEAD")
    if purpose == "ucp_catalog":  # a GET read, or exactly a catalog search
        if method == "GET":
            return True
        return method == "POST" and bool(_CATALOG_SEARCH.match(parts.path)) and not parts.query
    if purpose == "ucp_cart":
        return method == "POST" and bool(_CHECKOUT_SESSIONS.match(parts.path)) and not parts.query
    return False


def target(method: str, url: str) -> SignedFor:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    authority = host if parts.port in (None, 443) else f"{host}:{parts.port}"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return SignedFor(method.upper(), authority, path)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(public_raw: bytes) -> dict[str, str]:
    """The public key as a JWK (RFC 8037: OKP, Ed25519), its `kid` the RFC 7638 thumbprint."""
    x = b64url(public_raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": thumbprint(x), "alg": "Ed25519", "use": "sig"}


def thumbprint(x: str) -> str:
    """RFC 7638: the SHA-256 of the required members, in order, no spaces."""
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return b64url(hashlib.sha256(canonical.encode()).digest())


def _sf_string(value: str) -> str:
    """An RFC 8941 sf-string: printable ASCII, `"` and `\\` escaped."""
    if not all(32 <= ord(c) < 127 for c in value):
        raise ValueError("not an sf-string")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def agent_header(agent_url: str) -> str:
    """`Signature-Agent` in the draft's dictionary form: `sig1="https://agent.example"`."""
    return f"{LABEL}={_sf_string(agent_url)}"


def params(*, created: int, expires: int, keyid: str, nonce: str, tag: str, covered: tuple[str, ...]) -> str:
    """The signature's parameters (`@signature-params`): the covered components, then created, expires,
    keyid, alg, nonce, tag."""
    components = " ".join(covered)
    return (
        f"({components});created={created};expires={expires};keyid={_sf_string(keyid)};"
        f'alg="ed25519";nonce={_sf_string(nonce)};tag={_sf_string(tag)}'
    )


def base(lines: list[tuple[str, str]], signature_params: str) -> str:
    """RFC 9421 §2.5: one `"<component>": <value>` line each, then `"@signature-params": …`, no final newline."""
    out = [f"{name}: {value}" for name, value in lines]
    out.append(f'"@signature-params": {signature_params}')
    return "\n".join(out)


def request_base(
    signed: SignedFor, agent_url: str, *, created: int, keyid: str, nonce: str
) -> tuple[str, str]:
    """(the signature base, the `Signature-Input` member's value) for one of Pisa's requests: the authority, the
    method, the path and the `Signature-Agent` member."""
    covered = ('"@authority"', '"@method"', '"@path"', f'"signature-agent";key="{LABEL}"')
    sp = params(
        created=created, expires=created + SIGNATURE_TTL_S, keyid=keyid, nonce=nonce, tag=TAG, covered=covered
    )
    path_only = signed.path.split("?", 1)[0]
    lines = [
        ('"@authority"', signed.authority),
        ('"@method"', signed.method),
        ('"@path"', path_only),
        (f'"signature-agent";key="{LABEL}"', _sf_string(agent_url)),
    ]
    return base(lines, sp), sp


def directory_base(authority: str, *, created: int, keyid: str, nonce: str) -> tuple[str, str]:
    """The key directory's own response signature (the draft: signed by each key it lists, over the
    request's authority; tag http-message-signatures-directory)."""
    covered = ('"@authority";req',)
    sp = params(
        created=created,
        expires=created + SIGNATURE_TTL_S * 5,
        keyid=keyid,
        nonce=nonce,
        tag=DIRECTORY_TAG,
        covered=covered,
    )
    return base([('"@authority";req', authority)], sp), sp


def user_agent(agent_url: str | None) -> str:
    """Honest: Pisa says what it is on every read, signed or not (never a browser's string)."""
    card = f"; +{agent_url.rstrip('/')}{CARD_PATH}" if agent_url else ""
    return f"Pisa/1.0 (personal assistant acting for its owner{card})"


def agent_card(agent_url: str, kids: list[str]) -> dict[str, object]:
    """Pisa's card at `/.well-known/agent-card.json` (A2A's fields, and the Signature Agent Card's): who Pisa is
    and how to verify it; no owner data (no name, place, contact or account)."""
    root = agent_url.rstrip("/")
    return {
        "name": NAME,
        "description": DESCRIPTION,
        "url": root,
        "version": "1.0",
        "category": "direct",  # Cloudflare's taxonomy: one narrow operator (a single person's assistant)
        "purpose": "personal-assistant",
        "conduct": list(CONDUCT),
        "keys": {"directory": root + DIRECTORY_PATH, "kids": kids},
        "ucp_profile": root + UCP_PROFILE_PATH,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [],
        "defaultInputModes": [],
        "defaultOutputModes": [],
    }


def ucp_profile(agent_url: str, keys: list[dict[str, str]]) -> dict[str, object]:
    """Pisa as a UCP platform (`UCP-Agent: profile="…"`): the version, what it asks merchants for (catalog and a
    cart it hands to the user) and its signing keys."""
    return {
        "ucp": {
            "version": UCP_VERSION,
            "capabilities": [
                {"name": "dev.ucp.shopping.catalog", "version": UCP_VERSION},
                {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION, "mode": "handoff"},
            ],
        },
        "platform": {"name": NAME, "description": DESCRIPTION, "url": agent_url.rstrip("/")},
        "keys": keys,
    }


def agent_url_ok(url: str | None) -> str | None:
    """Pisa's public URL (PISA_AGENT_URL): plain https on a real host, no path beyond `/`, no query; else None."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or "." not in host
        or parts.username
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or not url.isascii()
    ):
        return None
    return f"https://{parts.netloc}"
