"""An authorized Google REST client: bearer token, one retry with a fresh token on 401, waits and retries
on Google's rate limits (429, or 403 "rate limit" / "quota exceeded" per minute), named errors."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from pisa.connectors.google.errors import ConnectorError, google_reason
from pisa.failures import external_failure

TokenProvider = Callable[[bool], Awaitable[str]]
"""(force_refresh) → a valid access token."""

# the Google APIs Pisa calls, each base once (the readers and the writers share them)
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
RATE_LIMIT_WAITS_S = (2.0, 5.0, 15.0)  # Google's per-user limits are per second/minute: wait, then retry


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    reason = google_reason(response).lower()
    return "rate limit" in reason or "quota exceeded" in reason or "ratelimitexceeded" in reason


class GoogleApi:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token: TokenProvider,
        *,
        service: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._token = token
        self._service = service  # "Gmail" | "Google Calendar", for messages
        self._sleep = sleep

    async def get(
        self, url: str, params: dict[str, Any] | list[tuple[str, str]] | None = None
    ) -> httpx.Response:
        """GET with authorization. Returns the response for 2xx and for 404/410 (callers decide what they
        mean); raises ConnectorError for everything else."""
        return await self.request("GET", url, params=params)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Any method, with the same token refresh, rate-limit waits and named errors as `get`. Returns the
        response for 2xx and for 404/410. `headers`: extra ones (If-Match)."""
        try:
            return await self._request(method, url, params=params, body=body, headers=headers)
        except ConnectorError as err:
            # every failed Google call in the log, with Google's own reason (never the token)
            service = "gmail" if self._service == "Gmail" else "google_calendar"
            external_failure(service, f"{method} {httpx.URL(url).path}", err.status, err.message)
            raise

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None,
        body: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        response = await self._send(method, url, params, body, force=False, headers=headers)
        for wait in RATE_LIMIT_WAITS_S:
            if not _rate_limited(response):
                break
            retry_after = response.headers.get("Retry-After", "")
            await self._sleep(float(retry_after) if retry_after.isdigit() else wait)
            response = await self._send(method, url, params, body, force=False, headers=headers)
        if response.status_code == 401:
            response = await self._send(method, url, params, body, force=True, headers=headers)
            if response.status_code == 401:
                raise ConnectorError(
                    f"Google rejected Pisa's access to {self._service} (HTTP 401).", 401, auth=True
                )
        if response.status_code < 400 or response.status_code in (404, 410):
            return response
        reason = google_reason(response)
        detail = f": {reason}" if reason else ""
        if response.status_code == 429:
            raise ConnectorError(f"Google throttled the {self._service} calls (HTTP 429{detail}).", 429)
        if response.status_code == 403:
            raise ConnectorError(f"Google refused the {self._service} call (HTTP 403{detail}).", 403)
        raise ConnectorError(
            f"The {self._service} call failed (HTTP {response.status_code}{detail}).", response.status_code
        )

    async def send_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """A write (or read) whose answer is a JSON object; 404/410 raise ConnectorError with the status."""
        response = await self.request(method, url, params=params, body=body, headers=headers)
        if response.status_code >= 400:
            raise ConnectorError(
                f"The {self._service} call failed (HTTP {response.status_code}).", response.status_code
            )
        if not response.content:
            return {}
        try:
            answer = response.json()
        except ValueError:
            raise ConnectorError(f"{self._service} returned a response that is not JSON.") from None
        if not isinstance(answer, dict):
            raise ConnectorError(f"{self._service} returned an unexpected response.")
        return answer

    async def json(
        self, url: str, params: dict[str, Any] | list[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        response = await self.get(url, params)
        if response.status_code >= 400:
            raise ConnectorError(
                f"The {self._service} call failed (HTTP {response.status_code}).", response.status_code
            )
        try:
            body = response.json()
        except ValueError:
            raise ConnectorError(f"{self._service} returned a response that is not JSON.") from None
        if not isinstance(body, dict):
            raise ConnectorError(f"{self._service} returned an unexpected response.")
        return body

    async def _send(
        self,
        method: str,
        url: str,
        params: Any,
        body: dict[str, Any] | None,
        *,
        force: bool,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._token(force)
        try:
            return await self._http.request(
                method,
                url,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{self._service} is unreachable ({exc.__class__.__name__}).") from None
