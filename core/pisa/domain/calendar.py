"""Calendar events as Pisa sees them."""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    name: str
    access_role: str | None = None  # Google's accessRole: owner | writer | reader | freeBusyReader


@dataclass(frozen=True)
class CalendarEvent:
    calendar_id: str
    id: str
    title: str
    start: datetime  # all-day events: local midnight of the first day
    end: datetime  # all-day events: local midnight after the last day (exclusive)
    all_day: bool
    location: str | None
    status: str  # confirmed | tentative | cancelled
    updated: datetime | None
    start_date: date | None = None  # all-day events only
    end_date: date | None = None  # all-day events only (exclusive)
    attendees: int | None = None  # others invited who haven't declined (a count, never who): scopes
    accepted: bool | None = None  # the user organised or accepted it (lateness is measured only then)
    # the guests they share it with, (email, display name), never themselves, a room or who
    # declined; and what makes it a yearly date (a recurring instance's series, Google's `birthday` events)
    guests: tuple[tuple[str, str], ...] | None = None
    recurring_event_id: str | None = None
    event_type: str | None = None
    # Pisa created it (its `extendedProperties.private` tag, on Pisa's own create)
    made_by_pisa: bool = False
    # Google's `visibility` (default, public, private, confidential): the wrist's flag
    visibility: str | None = None
    # Pisa wrote it after reading outside text (its private tag `pisa_outside`):
    # its title and place may be a stranger's, whatever the approvals still say
    outside: bool = False
    # what proves an event native to their own Google account (`services/foreign.py`):
    # Google's `iCalUID`, and whether its creator and its organizer are the calendar it sits on. None when
    # Google sent no such field: unknown reads as foreign
    ical_uid: str | None = None
    creator_self: bool | None = None
    organizer_self: bool | None = None


@dataclass(frozen=True)
class EventChanges:
    events: tuple[CalendarEvent, ...]  # cancelled ones included: the caller removes them
    next_sync_token: str | None
