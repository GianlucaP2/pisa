"""Changes to the user's Google Calendar (scope calendar.events), made by Pisa's actions (on the
user's words, or after their one yes).

Calls (base https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events):
- GET /{eventId} → the event (organizer.self, attendees[].email/self/displayName, start, end, location, etag…)
- POST ?sendUpdates=all|none → the new event
- PATCH /{eventId}?sendUpdates=all|none → the event (start/end to move it; status confirmed to restore it)
- DELETE /{eventId}?sendUpdates=all|none → 204; 404/410 = already gone (fine)

a PATCH or DELETE carries `If-Match: <etag>` of the event Pisa read, so a change made in Google meanwhile
answers 412 and nothing is overwritten; what Pisa creates or moves is tagged in `extendedProperties.private`
(`pisa` = 1, `pisa_action` = the approval's id), so Pisa can tell its changes from the user's.
"""

from typing import Any
from urllib.parse import quote

from pisa.connectors.google.api import CALENDAR_BASE, GoogleApi
from pisa.connectors.google.errors import ConnectorError

BASE = f"{CALENDAR_BASE}/calendars"


class GoogleCalendarWriter:
    def __init__(self, api: GoogleApi) -> None:
        self._api = api

    @staticmethod
    def _url(calendar_id: str, event_id: str | None = None) -> str:
        url = f"{BASE}/{quote(calendar_id, safe='')}/events"
        return f"{url}/{quote(event_id, safe='')}" if event_id else url

    async def event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        response = await self._api.get(self._url(calendar_id, event_id))
        if response.status_code in (404, 410):
            return None
        body = response.json()
        return body if isinstance(body, dict) else None

    async def insert(self, calendar_id: str, body: dict[str, Any], send_updates: str) -> dict[str, Any]:
        return await self._api.send_json(
            "POST", self._url(calendar_id), params={"sendUpdates": send_updates}, body=body
        )

    async def patch(
        self,
        calendar_id: str,
        event_id: str,
        body: dict[str, Any],
        send_updates: str,
        etag: str | None = None,
    ) -> dict[str, Any]:
        return await self._api.send_json(
            "PATCH",
            self._url(calendar_id, event_id),
            params={"sendUpdates": send_updates},
            body=body,
            headers={"If-Match": etag} if etag else None,
        )

    async def delete(
        self, calendar_id: str, event_id: str, send_updates: str, etag: str | None = None
    ) -> None:
        response = await self._api.request(
            "DELETE",
            self._url(calendar_id, event_id),
            params={"sendUpdates": send_updates},
            headers={"If-Match": etag} if etag else None,
        )
        if response.status_code == 412:
            raise ConnectorError("The event changed in Google Calendar meanwhile (HTTP 412).", 412)
        if response.status_code >= 400 and response.status_code not in (404, 410):
            raise ConnectorError(f"Google Calendar refused the delete (HTTP {response.status_code}).")
