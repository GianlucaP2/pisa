"""Google "server-side access": the iPhone signs in with the official Google Sign-In SDK
(serverClientID = Pisa's web client) and sends Pisa a one-time server auth code; Pisa exchanges it for a
refresh token. No redirect URI or callback page exists on Pisa.

- Exchange: POST https://oauth2.googleapis.com/token with grant_type=authorization_code, the code, the **web**
  client id + secret and redirect_uri="" (Google's documented server-side flow for iOS).
- Refresh: Google's official `google-auth` library (`google.oauth2.credentials.Credentials.refresh`), over
  Pisa's httpx client.
- Account: the `email` claim of the ID token that comes with the exchange. It arrives straight from Google's
  token endpoint over TLS, so its signature needn't be checked (Google's guidance for this case).
- Revoke: POST https://oauth2.googleapis.com/revoke (best effort).
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from google.auth import jwt
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport import Request as GoogleRequest
from google.auth.transport import Response as GoogleResponse
from google.oauth2.credentials import Credentials

from pisa.connectors.google.errors import ConnectorError, google_reason

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 (an endpoint, not a secret)
CALLBACK_PATH = "/oauth/google/callback"  # on the user's own server: the web client's one redirect URI
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"
SCOPE_CALENDAR_APP = "https://www.googleapis.com/auth/calendar.app.created"
SCOPE_CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"  # change the user's events
SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"  # drafts, and send them
READ_SCOPES = (SCOPE_GMAIL, SCOPE_CALENDAR, SCOPE_CALENDAR_APP)  # without these Pisa refuses the connection
ACTION_SCOPES = (SCOPE_CALENDAR_EVENTS, SCOPE_GMAIL_COMPOSE)  # without these Pisa only reads (missing_scopes)
SCOPES = READ_SCOPES + ACTION_SCOPES  # what the app asks for


@dataclass(frozen=True)
class Tokens:
    access_token: str
    expires_in: int
    refresh_token: str | None
    scopes: tuple[str, ...]
    email: str | None = None


# --- google-auth over httpx ------------------------------------------------------------------------------


class _Response(GoogleResponse):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.content


class HttpxRequest(GoogleRequest):
    """google-auth's transport interface on a synchronous httpx client (same transport as Pisa's async
    one)."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> GoogleResponse:
        try:
            response = self._client.request(method, url, content=body, headers=headers, timeout=timeout or 20)
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Google's token service is unreachable ({exc.__class__.__name__})"
            ) from None
        return _Response(response)


# --- exchange, refresh, revoke ---------------------------------------------------------------------------


def _email(id_token: object) -> str | None:
    if not isinstance(id_token, str):
        return None
    try:
        claims = jwt.decode(id_token, verify=False)
    except ValueError:
        return None
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge (RFC 7636)."""
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    """Google's consent page for this server's own web client: offline access (a refresh token), the consent
    asked again (so the refresh token comes back), every scope Pisa asks for, PKCE S256."""
    from urllib.parse import urlencode

    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(query)}"


async def exchange_server_auth_code(
    http: httpx.AsyncClient,
    *,
    web_client_id: str,
    web_client_secret: str,
    code: str,
    redirect_uri: str = "",
    code_verifier: str | None = None,
) -> Tokens:
    try:
        response = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": web_client_id,
                "client_secret": web_client_secret,
                "redirect_uri": redirect_uri,
                **({"code_verifier": code_verifier} if code_verifier else {}),
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise ConnectorError(f"Google's token service is unreachable ({exc.__class__.__name__}).") from None
    if response.status_code >= 400:
        reason = google_reason(response)
        if reason.startswith("invalid_grant"):
            raise ConnectorError(
                "Google rejected the code: invalid_grant (codes are single-use and expire in minutes).",
                response.status_code,
                auth=True,
            )
        detail = reason or f"HTTP {response.status_code}"
        raise ConnectorError(f"Google rejected the code: {detail}.", response.status_code, auth=True)
    body: Any = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise ConnectorError("Google answered the code exchange without an access token.")
    expires, refresh, scope = body.get("expires_in"), body.get("refresh_token"), body.get("scope")
    return Tokens(
        access_token=body["access_token"],
        expires_in=expires if isinstance(expires, int) else 3600,
        refresh_token=refresh if isinstance(refresh, str) else None,
        scopes=tuple(scope.split()) if isinstance(scope, str) else (),
        email=_email(body.get("id_token")),
    )


async def refresh_access_token(
    http: httpx.Client, *, web_client_id: str, web_client_secret: str, refresh_token: str
) -> Tokens:
    """Refresh with google-auth. Raises ConnectorError(auth=True) when Google refuses the refresh token
    (e.g. `invalid_grant`: revoked, or expired after 7 days while the Google app is in Testing mode)."""
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URL,
        client_id=web_client_id,
        client_secret=web_client_secret,
    )
    try:
        await asyncio.to_thread(credentials.refresh, HttpxRequest(http))
    except RefreshError as exc:
        reason = str(exc.args[0]) if exc.args else "refused"
        code = reason.split(":", 1)[0].strip() or "refused"
        raise ConnectorError(f"Google refused the saved authorization ({code}).", auth=True) from None
    except TransportError as exc:
        raise ConnectorError(f"{str(exc).rstrip('.')}.") from None
    expires_in = 3600
    if credentials.expiry is not None:  # google-auth keeps a naive UTC expiry
        expires_in = max(0, int((credentials.expiry.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()))
    return Tokens(
        access_token=str(credentials.token),
        expires_in=expires_in,
        refresh_token=None,
        scopes=tuple(credentials.granted_scopes or ()),
    )


async def revoke(http: httpx.AsyncClient, token: str) -> bool:
    """Best effort: True when Google confirmed the revocation."""
    try:
        response = await http.post(REVOKE_URL, data={"token": token}, timeout=10)
    except httpx.HTTPError:
        return False
    return response.status_code == 200
