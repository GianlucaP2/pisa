"""Credential-shaped test values, assembled at run time.

A secret scanner (GitHub's, gitleaks, trufflehog) reads the source, not the running test, so the repository
holds no literal of a real credential's shape (tests/test_no_literal_secrets.py checks it). A test that needs
a value of that shape (to check redaction, masking or parsing) builds it here: the same characters at run
time, never as one literal in a file. Every value is synthetic.
"""

import base64
import json
from typing import Any


def _join(*parts: str) -> str:
    return "".join(parts)


def tailscale_key(kind: str, *body: str) -> str:
    """A Tailscale key, `tskey-<kind>-…`."""
    return _join("tskey", "-", kind, "-", *body)


def google_access_token(*body: str) -> str:
    """A Google OAuth access token, `ya29.…`."""
    return _join("ya29", ".", *body)


def google_refresh_token(*body: str) -> str:
    """A Google OAuth refresh token, `1//…`."""
    return _join("1/", "/", *body)


def google_api_key(*body: str) -> str:
    """A Google API key, `AIza…`."""
    return _join("AI", "za", *body)


def google_client_secret(*body: str) -> str:
    """A Google OAuth client secret, `GOCSPX-…`."""
    return _join("GOC", "SPX-", *body)


def openai_key(*body: str) -> str:
    """An OpenAI-style key, `sk-…`."""
    return _join("sk", "-", *body)


def openai_project_key(*body: str) -> str:
    """An OpenAI project key, `sk-proj-…`."""
    return _join("sk", "-proj-", *body)


def tavily_key(*body: str) -> str:
    """A Tavily key, `tvly-…`."""
    return _join("tv", "ly-", *body)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"signature") -> str:
    """A JWS compact token, `<header>.<claims>.<signature>` (each base64url, unpadded): unsigned, the
    signature is the given bytes. The JSON is `json.dumps`'s default form, `{"alg": "RS256", ...}`."""
    return ".".join(
        (_b64url(json.dumps(header).encode()), _b64url(json.dumps(claims).encode()), _b64url(signature))
    )


def pem_private_key(body: str) -> bytes:
    """A PKCS#8 PEM private key around a base64 body (one line)."""
    label = _join("PRIVATE", " KEY")
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n".encode()


def openrouteservice_key(*body: str) -> str:
    """An OpenRouteService API key's shape: hex, its common prefix then the account's part."""
    return _join("5b3ce3597851", "110001cf6248", *body)
