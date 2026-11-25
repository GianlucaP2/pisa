"""A fake Google (OAuth, Gmail, Calendar) answering with the recorded-shape JSON in tests/data/google/."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from tests.support.fake_secrets import google_access_token, google_client_secret, google_refresh_token, jwt

DATA = Path(__file__).parent / "data" / "google"
WEB_CLIENT_ID = "123-web.apps.googleusercontent.com"
CLIENT_SECRET = google_client_secret("test-planted-secret")  # a fake; tests assert it never leaks
ACCESS_TOKEN = google_access_token("test-access-1")  # the exchange's access token
REFRESHED_ACCESS_TOKEN = google_access_token("test-access-2")  # the refresh's
REFRESH_TOKEN = google_refresh_token("test-refresh-token")
# The exchange's ID token: unsigned (kid "test", the signature is the bytes "signature"), for the owner.
ID_TOKEN = jwt(
    {"alg": "RS256", "kid": "test", "typ": "JWT"},
    {
        "iss": "https://accounts.google.com",
        "aud": WEB_CLIENT_ID,
        "azp": "456-ios.apps.googleusercontent.com",
        "sub": "1234567890",
        "email": "owner@example.org",
        "email_verified": True,
        "iat": 1760950000,
        "exp": 1760953600,
    },
)
# The token responses' credentials, filled in at load: the recorded files hold only their other fields.
_TOKENS = {
    "token_exchange.json": {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "id_token": ID_TOKEN,
    },
    "token_exchange_partial_scopes.json": {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "id_token": ID_TOKEN,
    },
    "token_refresh.json": {"access_token": REFRESHED_ACCESS_TOKEN},
}
SERVER_AUTH_CODE = "4/0AVG-server-auth-code"  # a fake one-time code
USED_CODE = "4/0AVG-already-used"


def load(name: str) -> Any:
    data = json.loads((DATA / name).read_text())
    return data | _TOKENS[name] if name in _TOKENS else data


class FakeGoogle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, list[str]]]] = []
        self.token_file = "token_exchange.json"
        self.refresh_file = "token_refresh.json"
        self.refresh_status = 200
        self.refresh_error_file = "token_invalid_grant.json"
        self.history_expired = False
        self.calendar_gone = False
        self.fail_next_message = False  # the next messages.get fails (a server error)
        self.revoked: list[str] = []
        self.forms: list[dict[str, list[str]]] = []

    def paths(self, host: str) -> list[str]:
        return [path for h, path, _ in self.calls if h == host]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        params = parse_qs(request.url.query.decode())
        self.calls.append((host, path, params))
        if host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode(), keep_blank_values=True)
            self.forms.append(form)
            if path == "/token":
                if form["grant_type"] == ["authorization_code"]:
                    if form["code"] == [USED_CODE]:
                        return httpx.Response(400, json=load("token_invalid_grant.json"))
                    return httpx.Response(200, json=load(self.token_file))
                if self.refresh_status != 200:
                    return httpx.Response(self.refresh_status, json=load(self.refresh_error_file))
                return httpx.Response(200, json=load(self.refresh_file))
            if path == "/revoke":
                self.revoked.append(form["token"][0])
                return httpx.Response(200)
        assert request.headers["authorization"].startswith("Bearer " + google_access_token()), (
            "Google calls carry the access token"
        )
        if host == "gmail.googleapis.com":
            base = "/gmail/v1/users/me"
            if path == f"{base}/profile":
                return httpx.Response(200, json=load("gmail_profile.json"))
            if path == f"{base}/messages":
                return httpx.Response(200, json=load("gmail_list.json"))
            if path.startswith(f"{base}/messages/"):
                if self.fail_next_message:
                    self.fail_next_message = False
                    return httpx.Response(500, json={"error": {"code": 500, "message": "Backend Error"}})
                message_id = path.rsplit("/", 1)[1].removeprefix("m-")
                file = DATA / f"gmail_message_{message_id}.json"
                return httpx.Response(200, json=load(file.name)) if file.exists() else httpx.Response(404)
            if path == f"{base}/history":
                if self.history_expired:
                    return httpx.Response(404, json=load("gmail_history_expired.json"))
                return httpx.Response(200, json=load("gmail_history.json"))
        if host == "www.googleapis.com":
            if path == "/calendar/v3/users/me/calendarList":
                return httpx.Response(200, json=load("calendar_list.json"))
            if path.endswith("/events"):
                holidays = "holiday" in path
                if "syncToken" in params:
                    if self.calendar_gone:
                        return httpx.Response(410, json=load("calendar_gone.json"))
                    if holidays:
                        return httpx.Response(200, json={"items": [], "nextSyncToken": "sync-token-hol"})
                    return httpx.Response(200, json=load("calendar_events_incremental.json"))
                return httpx.Response(
                    200,
                    json=load("calendar_events_holidays.json" if holidays else "calendar_events_full.json"),
                )
        raise httpx.ConnectError(f"unexpected call {request.method} {request.url}", request=request)
