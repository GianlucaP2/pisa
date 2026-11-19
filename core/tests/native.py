"""Test rows that model an event they made themselves in Google Calendar carry what proves it
native (`services/foreign.py` OWN_EVENT): creator and organizer self, a plain event, Google's own iCalUID, and
Google's last change (`updated`, which the sync always stores) before the mail sync read their mail. A row
inserted without them is foreign, like any row the sync has not proven theirs."""

from typing import Any


def native(db: Any, *event_ids: str) -> None:
    db.execute(
        "UPDATE calendar_events SET creator_self = true, organizer_self = true, "
        "event_type = coalesce(event_type, 'default'), "
        "ical_uid = coalesce(recurring_event_id, id) || '@google.com', attendees = nullif(attendees, 0), "
        "updated = coalesce(updated, '2026-01-01T00:00:00Z') "
        "WHERE id = ANY(%s)",
        (list(event_ids),),
    )
