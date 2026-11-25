"""One predicate for the titles a stranger controls,
used by every reader that decides whether Pisa's own lists hold outside text: the
turn's foreign titles and the day-context (`AssistantService.foreign_titles`, `foreign_in`), the agenda's per-
event mark (the brain's `foreign_titles(view)`), and the earned-autonomy gate
(`ActionsService._invites_near`).

An event is their own only on proof (`OWN_EVENT`, below).
A stranger controls an event's title when it has other
attendees (an invite), when Gmail made it from an email (`event_type = 'fromGmail'`, no attendees: the sender
wrote it), when it sits on a calendar they don't own (shared or subscribed, or not synced yet: `access_role`
other than 'owner', NULL included), when it came in as a mailed invitation (its iCalUID is the sender's, or
an invitation in their mail names it), or when Pisa made it after reading outside text (the event's own
`outside` mark, set when Pisa writes it and read back from its Google tag at every sync, never a join on the
approval, which a re-sync, an id change or a retention change can break).
A task's, when it was made from an email (`source_kind = 'email'`, open, done or dismissed: a task Pisa adds
after outside text, on their yes, is one too). SQL fragments over `calendar_events e JOIN calendars c` and
`tasks t`; no parameters.
"""

from typing import Any

from psycopg import AsyncConnection

# an event is their own only on positive evidence (fail-closed). It must sit on a calendar they
# own, carry no outside mark, and either (a native event needs NO guest list at all, so an iMIP
# invite listing them to RSVP, or one whose guests all declined, is never native: others' events too)
# 1. Pisa made it (`made_by_pisa`: Pisa's private tag on its own create, or Pisa's executed create approval;
# the outside mark is the ledger's and the approvals', `google_sync._PISA_OUTSIDE`), or
# 2. it is native to their own Google account: they created and organise it (`creator.self`,
#    `organizer.self`), it is a plain event (`default`: not Gmail's `fromGmail`, nor a birthday, focus time,
#    out of office, working place), and its `iCalUID` is the one Google gives its own events:
#    `<id>@google.com`, where the id is the event's (or, for an instance, its series'). A mailed invite
#    (text/calendar) keeps its sender's UID, so even one whose ORGANIZER is their own address, with no other
#    attendee, is not theirs.
#    And it did not arrive by email: an event that arrived only by email is foreign unless they created it in
#    Calendar (the first term) or Pisa did. No invitation Pisa has read in their mail names its UID
#    (`mailed_invites`), whatever id Google gave it: a sender who picks a UID of Google's own form
#    (`<id>@google.com`) cannot pass as native. Deleting one email keeps its UID; erasing their mail (the
#    `mail` family) erases the UIDs but first marks their events `mailed`, which keeps them foreign.
# Everything else is foreign, NULL included: rows not yet re-read since migration 0111 are foreign until the
# next sync fills them.
# While Gmail is connected, an event Google changed after the last mail sync may have come in a mail Pisa has
# not read yet: not theirs until that mail is read. Mail outside the sync window, or trashed before
# Pisa first read it, leaves no trace: SECURITY.md names that limit.
OWN_EVENT = (
    "(c.access_role IS NOT DISTINCT FROM 'owner' AND NOT e.outside "
    "AND ((e.made_by_pisa AND coalesce(e.attendees, 0) = 0) "
    "OR (e.attendees IS NULL AND e.creator_self IS TRUE AND e.organizer_self IS TRUE "
    "AND e.event_type IS NOT DISTINCT FROM 'default' "
    "AND e.ical_uid IS NOT DISTINCT FROM coalesce(e.recurring_event_id, e.id) || '@google.com' "
    "AND NOT EXISTS (SELECT 1 FROM mailed_invites mi WHERE mi.uid = e.ical_uid) AND NOT e.mailed "
    "AND NOT EXISTS (SELECT 1 FROM connector_accounts a WHERE a.connector = 'google' "
    "AND 'https://www.googleapis.com/auth/gmail.readonly' = ANY (a.scopes) "  # oauth.SCOPE_GMAIL
    "AND (a.gmail_synced_at IS NULL OR e.updated IS NULL OR e.updated > a.gmail_synced_at)))))"
)
# Never NULL: every term of OWN_EVENT is NOT NULL, EXISTS, or compared with IS [NOT DISTINCT]
FOREIGN_EVENT = f"(NOT {OWN_EVENT})"
FOREIGN_TASK = "(t.source_kind = 'email')"
EVENTS = "calendar_events e JOIN calendars c ON c.id = e.calendar_id"
# the window whose foreign titles a turn knows (`AssistantService.foreign_titles`): every list
# a tool can make (an agenda of up to 14 days, a find weeks out, a plan) names only events inside it
TITLES_BEHIND_DAYS = 31
TITLES_AHEAD_DAYS = 400


async def event_foreign(conn: AsyncConnection, event_ids: list[str]) -> bool:
    """Any of these events has a title or a place a stranger controls, or none of them is
    known any more (a stale plan item whose event left Pisa's copy fails closed)."""
    ids = [str(i) for i in event_ids if i]
    if not ids:
        return False
    row = await (
        await conn.execute(
            f"SELECT coalesce(bool_or({FOREIGN_EVENT}), true) FROM {EVENTS} WHERE e.id = ANY(%s)",  # noqa: S608
            (ids,),
        )
    ).fetchone()
    return bool(row and row[0])


async def task_foreign(conn: AsyncConnection, task_ids: list[str]) -> bool:
    """Any of these tasks was made from an email."""
    ids = [str(i) for i in task_ids if i]
    if not ids:
        return False
    row = await (
        await conn.execute(
            f"SELECT bool_or({FOREIGN_TASK}) FROM tasks t WHERE t.id::text = ANY(%s)",  # noqa: S608
            (ids,),
        )
    ).fetchone()
    return bool(row and row[0])


async def item_foreign(conn: AsyncConnection, item: dict[str, Any]) -> bool:
    """A plan item (or any dict naming `event_id`, `event_ids`, `task_id`) whose title a
    stranger controls."""
    events = [*([item["event_id"]] if item.get("event_id") else []), *(item.get("event_ids") or [])]
    tasks = [item["task_id"]] if item.get("task_id") else []
    return await event_foreign(conn, events) or await task_foreign(conn, tasks)
