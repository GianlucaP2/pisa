"""Google Calendar as a CalendarSource (Calendar API v3, scope calendar.readonly).

Calls (base https://www.googleapis.com/calendar/v3):
- GET /users/me/calendarList?pageToken → items[].id, summary, summaryOverride, selected, deleted, accessRole
- GET /calendars/{calendarId}/events?singleEvents=true&maxResults=2500 and either timeMin+timeMax (full sync)
  or syncToken (incremental), &pageToken → items[].id, status, summary, start/end{dateTime|date}, location,
  updated, attendees[], recurringEventId, eventType, iCalUID, creator.self, organizer.self; nextSyncToken on
  the last page; 410 = the sync token
  expired (SyncTokenExpired)
"""

from datetime import date, datetime, time
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pisa.connectors.google.api import CALENDAR_BASE, GoogleApi
from pisa.domain.calendar import CalendarEvent, CalendarInfo, EventChanges
from pisa.domain.owner import Owner
from pisa.ports.sources import SyncTokenExpired

BASE = CALENDAR_BASE


def _when(value: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, date | None]:
    if value.get("dateTime"):
        return datetime.fromisoformat(str(value["dateTime"])), None
    day = date.fromisoformat(str(value["date"]))
    return datetime.combine(day, time.min, tzinfo=tz), day


def parse_event(calendar_id: str, item: dict[str, Any], tz: ZoneInfo) -> CalendarEvent:
    status = str(item.get("status", "confirmed"))
    updated = datetime.fromisoformat(str(item["updated"])) if item.get("updated") else None
    if status == "cancelled" and not item.get("start"):
        epoch = datetime.fromtimestamp(0, tz=tz)
        return CalendarEvent(calendar_id, str(item["id"]), "", epoch, epoch, False, None, status, updated)
    start, start_day = _when(item.get("start") or {}, tz)
    end, end_day = _when(item.get("end") or item.get("start") or {}, tz)
    location = item.get("location")
    return CalendarEvent(
        calendar_id=calendar_id,
        id=str(item["id"]),
        title=str(item.get("summary") or "(no title)"),
        start=start,
        end=end,
        all_day=start_day is not None,
        location=str(location) if location else None,
        status=status,
        updated=updated,
        start_date=start_day,
        end_date=end_day,
        attendees=_others(item.get("attendees")),
        accepted=_accepted(item),
        guests=_guests(item.get("attendees")),
        recurring_event_id=str(item["recurringEventId"]) if item.get("recurringEventId") else None,
        event_type=str(item["eventType"]) if item.get("eventType") else None,
        made_by_pisa=made_by_pisa(item),
        visibility=str(item["visibility"]) if item.get("visibility") else None,
        outside=from_outside(item),
        ical_uid=str(item["iCalUID"])[:2048] if item.get("iCalUID") else None,
        creator_self=_is_self(item.get("creator")),
        organizer_self=_is_self(item.get("organizer")),
    )


def _is_self(person: Any) -> bool | None:
    """`creator.self` / `organizer.self` ("whether [it] corresponds to the calendar on which
    this copy of the event appears"; Google omits `self` when false). None when Google sent no such person."""
    if not isinstance(person, dict):
        return None
    return person.get("self") is True


def from_outside(item: dict[str, Any]) -> bool:
    """Pisa wrote this event after reading outside text (`extendedProperties.private.pisa_outside`,
    services/actions.py `OUTSIDE_TAG`): its title and place are outside text. Only a private property Pisa
    itself wrote carries it; a forged one could only mark an event as outside, never clear the mark."""
    private = (item.get("extendedProperties") or {}).get("private") or {}
    return isinstance(private, dict) and private.get("pisa_outside") == "1"


def made_by_pisa(item: dict[str, Any]) -> bool:
    """Pisa created this event. Pisa tags what it creates and what it moves alike
    (`extendedProperties.private {"pisa": "1", "pisa_action": <approval id>}`, services/actions.py `_tag`);
    only a create's event id is its approval's own hex, so an event of their that Pisa moved stays theirs."""
    private = (item.get("extendedProperties") or {}).get("private") or {}
    if not isinstance(private, dict) or private.get("pisa") != "1":
        return False
    action = str(private.get("pisa_action") or "").replace("-", "").lower()
    return bool(action) and action == str(item.get("id") or "").lower()


def _guests(attendees: Any) -> tuple[tuple[str, str], ...] | None:
    """The others invited who haven't declined, (email, display name), for the people list."""
    if not isinstance(attendees, list) or not attendees:
        return None
    return (
        tuple(
            (str(a["email"]).lower()[:200], str(a.get("displayName") or "")[:120])
            for a in attendees
            if isinstance(a, dict)
            and a.get("email")
            and not a.get("self")
            and not a.get("resource")
            and a.get("responseStatus") != "declined"
        )
        or None
    )


def _accepted(item: dict[str, Any]) -> bool:
    """The user organised it, or accepted the invite; an event without guests is their own."""
    organizer = item.get("organizer")
    if isinstance(organizer, dict) and organizer.get("self"):
        return True
    attendees = item.get("attendees")
    if not isinstance(attendees, list) or not attendees:
        return True
    me = next((a for a in attendees if isinstance(a, dict) and a.get("self")), None)
    return me is not None and me.get("responseStatus") == "accepted"


def _others(attendees: Any) -> int | None:
    """How many others are invited and haven't declined ("with others"); None without a guest list."""
    if not isinstance(attendees, list) or not attendees:
        return None
    return sum(
        1
        for a in attendees
        if isinstance(a, dict)
        and not a.get("self")
        and not a.get("resource")
        and a.get("responseStatus") != "declined"
    )


class GoogleCalendarSource:
    def __init__(self, api: GoogleApi, *, owner: Owner) -> None:
        self._api = api
        self._owner = owner

    async def calendars(self) -> list[CalendarInfo]:
        found: list[CalendarInfo] = []
        page: str | None = None
        while True:
            params = {"pageToken": page} if page else None
            body = await self._api.json(f"{BASE}/users/me/calendarList", params)
            for item in body.get("items") or []:
                if isinstance(item, dict) and item.get("selected") is True and not item.get("deleted"):
                    name = item.get("summaryOverride") or item.get("summary") or item.get("id")
                    role = item.get("accessRole")
                    found.append(CalendarInfo(str(item["id"]), str(name), str(role) if role else None))
            page = body.get("nextPageToken")
            if not page:
                return found

    async def events(
        self, calendar_id: str, *, sync_token: str | None, time_min: datetime, time_max: datetime
    ) -> EventChanges:
        events: list[CalendarEvent] = []
        page: str | None = None
        next_token: str | None = None
        url = f"{BASE}/calendars/{quote(calendar_id, safe='')}/events"
        while True:
            params: dict[str, str] = {"singleEvents": "true", "maxResults": "2500"}
            if sync_token:
                params["syncToken"] = sync_token
            else:
                params["timeMin"] = time_min.isoformat()
                params["timeMax"] = time_max.isoformat()
            if page:
                params["pageToken"] = page
            response = await self._api.get(url, params)
            if response.status_code == 410:
                raise SyncTokenExpired(calendar_id)
            if response.status_code == 404:
                return EventChanges((), None)  # the calendar was removed meanwhile
            body = response.json()
            events += [parse_event(calendar_id, item, self._owner.zone) for item in body.get("items") or []]
            page = body.get("nextPageToken")
            next_token = body.get("nextSyncToken") or next_token
            if not page:
                return EventChanges(tuple(events), next_token)
