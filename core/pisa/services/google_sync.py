"""Gmail and Calendar sync into the local caches (email_messages, calendars, calendar_events).

Gmail (every 2 min): the first run takes the last 7 days, at most 300 messages; then incremental from the
stored historyId; an expired history (404) means a full resync of the last 7 days.
Calendar (every 5 min): every calendar the user shows (selected), incremental with syncToken (410 → full
resync), window today - 1 day to today + 30 days. The window moves every day, so the first sync of a new day
is a full one (an unchanged event entering the window wouldn't come through the sync token).

A run that can't get its advisory lock is skipped (another process is syncing). Failures are recorded on the
connector (status error + the real cause) and never raised to the scheduler.
"""

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal, LiteralString
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.google.errors import ConnectorError
from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.domain.calendar import CalendarEvent
from pisa.domain.errors import PisaError
from pisa.domain.mail import MailMessage
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.ports.sources import CalendarSource, HistoryExpired, MailSource, SyncTokenExpired
from pisa.services.google import GoogleService, is_off
from pisa.services.reader import note_hidden

FIRST_SYNC_DAYS = 7
FIRST_SYNC_MAX_MESSAGES = 300
FIRST_SYNC_BATCH = 25  # messages per round: a first sync goes on over rounds, within a low Gmail quota
FIRST_SYNC_KEY = "gmail_first_sync"  # {"account", "point"}: the history point a first sync started from
WINDOW_DAYS_BEFORE = 1
WINDOW_DAYS_AFTER = 30
_LOCK_GMAIL = locks.GMAIL_SYNC
_LOCK_CALENDAR = locks.CALENDAR_SYNC
_log = get_logger("pisa.google.sync")


@dataclass(frozen=True)
class SyncResult:
    ran: bool
    changed: int = 0
    error: str | None = None


@contextlib.asynccontextmanager
async def _try_lock(pool: AsyncConnectionPool, key: int) -> AsyncIterator[bool]:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (key,))).fetchone()
        got = bool(row and row[0])
        try:
            yield got
        finally:
            if got:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


async def _record_failure(
    pool: AsyncConnectionPool,
    google: GoogleService,
    source: Literal["gmail", "calendar"],
    exc: ConnectorError | PisaError,
) -> None:
    async with pool.connection() as conn:
        if isinstance(exc, ConnectorError) and exc.auth:
            await google.mark_reconnect(conn, exc.message)  # syncing pauses until the user reconnects
        else:
            await google.record_status(conn, source, error=exc.message)


async def _upsert_messages(
    conn: AsyncConnection, messages: list[MailMessage] | tuple[MailMessage, ...]
) -> None:
    # the hidden runs left out of each new email, counted once (never a resync's)
    known = {
        r[0]
        for r in await (
            await conn.execute(
                "SELECT id FROM email_messages WHERE id = ANY(%s)", ([m.id for m in messages],)
            )
        ).fetchall()
    }
    await note_hidden(
        conn, "mail", sum(m.hidden for m in messages if m.id not in known), datetime.now(UTC).date()
    )
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, to_addresses, "
            "subject, sent_at, "
            "labels, snippet, body) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET labels = EXCLUDED.labels",
            [
                (
                    m.id,
                    m.thread_id,
                    m.sender.name if m.sender else None,
                    m.sender.address if m.sender else None,
                    [str(a) for a in m.to],
                    m.subject,
                    m.sent_at,
                    list(m.labels),
                    m.snippet,
                    m.body,
                )
                for m in messages
            ],
        )
        # the PDFs each email carries (metadata only), for the bills' reader and the accountant
        pdfs = [(m.id, a.part_id, a.filename, a.mime, a.size) for m in messages for a in m.attachments]
        if pdfs:
            await cur.executemany(
                "INSERT INTO email_attachments (email_id, part_id, filename, mime, size) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                pdfs,
            )
        # The people the user writes to (the correspondents, one lookup per email).
        sent = {
            (a.address.strip().lower(), m.sent_at)
            for m in messages
            if "SENT" in m.labels
            for a in m.to
            if "@" in a.address
        }
        if sent:
            await cur.executemany(
                "INSERT INTO email_correspondents (address, last_at) VALUES (%s, %s) ON CONFLICT (address) "
                "DO UPDATE SET last_at = greatest(email_correspondents.last_at, EXCLUDED.last_at)",
                sorted(sent),
            )
        # the invitations each email carries: their events arrived by email (`foreign.OWN_EVENT`)
        invites = sorted({uid for m in messages for uid in m.invites})
        if invites:
            await cur.executemany(
                "INSERT INTO mailed_invites (uid) VALUES (%s) ON CONFLICT DO NOTHING", [(u,) for u in invites]
            )


async def sync_gmail(
    pool: AsyncConnectionPool,
    google: GoogleService,
    *,
    source: MailSource | None = None,
    now: datetime | None = None,
) -> SyncResult:
    now = now or datetime.now(UTC)
    async with _try_lock(pool, _LOCK_GMAIL) as got:
        if not got:
            return SyncResult(ran=False)
        async with pool.connection() as conn:
            account = await google.account(conn)
            off = await is_off(conn)  # switched off in Settings: no sync
        if account is None or account.needs_reconnect or off:
            return SyncResult(ran=False)
        mail = source or google.gmail()
        try:
            changed = await _gmail_round(pool, mail, account.gmail_history_id, account.account or "")
        except (ConnectorError, PisaError) as exc:
            await _record_failure(pool, google, "gmail", exc)
            _log.warning("gmail_sync_failed", cause=exc.message)
            return SyncResult(ran=True, error=exc.message)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE connector_accounts SET gmail_synced_at = %s WHERE connector = 'google'", (now,)
            )
            await google.record_status(conn, "gmail", error=None)
        _log.info("gmail_synced", changed=changed)
        return SyncResult(ran=True, changed=changed)


async def _gmail_round(
    pool: AsyncConnectionPool, mail: MailSource, history_id: str | None, account: str
) -> int:
    if history_id is not None:
        try:
            changes = await mail.changes_since(history_id)
        except HistoryExpired:
            _log.info("gmail_history_expired_resync")
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE connector_accounts SET gmail_history_id = NULL WHERE connector = 'google'"
                )
                await delete_setting(conn, FIRST_SYNC_KEY)
        else:
            async with pool.connection() as conn, conn.transaction():
                await _upsert_messages(conn, changes.added)
                for change in changes.relabelled:
                    await conn.execute(
                        "UPDATE email_messages SET labels = %s WHERE id = %s",
                        (list(change.labels), change.message_id),
                    )
                if changes.deleted:
                    await conn.execute(
                        "DELETE FROM email_messages WHERE id = ANY(%s)", (list(changes.deleted),)
                    )
                await _set_history(conn, changes.history_id)
            return len(changes.added) + len(changes.relabelled) + len(changes.deleted)
    return await _first_sync_round(pool, mail, account)


async def _first_sync_round(pool: AsyncConnectionPool, mail: MailSource, account: str) -> int:
    """One batch of the first sync (the last 7 days): what earlier rounds stored is not fetched again, and
    the history point taken by the first round becomes the incremental starting point once all is in."""
    async with pool.connection() as conn:
        started = await get_setting(conn, FIRST_SYNC_KEY)
        known = await (await conn.execute("SELECT id FROM email_messages")).fetchall()
    if isinstance(started, dict) and started.get("account") == account and started.get("point"):
        point = str(started["point"])
    else:  # taken first, so nothing that arrives during the rounds is missed
        point = await mail.history_point()
        async with pool.connection() as conn:
            await set_setting(conn, FIRST_SYNC_KEY, Jsonb({"account": account, "point": point}))
    messages = await mail.recent(
        days=FIRST_SYNC_DAYS,
        limit=FIRST_SYNC_MAX_MESSAGES,
        skip=frozenset(row[0] for row in known),
        fetch=FIRST_SYNC_BATCH,
    )
    async with pool.connection() as conn, conn.transaction():
        await _upsert_messages(conn, messages)
        if len(messages) < FIRST_SYNC_BATCH:  # all in (a message deleted meanwhile may end it early)
            await _set_history(conn, point)
            await delete_setting(conn, FIRST_SYNC_KEY)
    return len(messages)


async def _set_history(conn: AsyncConnection, history_id: str) -> None:
    await conn.execute(
        "UPDATE connector_accounts SET gmail_history_id = %s WHERE connector = 'google'", (history_id,)
    )


def calendar_window(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    """[today - 1 day, today + 31 days) at local midnight: the last included day is today + 30."""
    tz = ZoneInfo(timezone)
    today = now.astimezone(tz).date()
    start = datetime.combine(today - timedelta(days=WINDOW_DAYS_BEFORE), time.min, tzinfo=tz)
    end = datetime.combine(today + timedelta(days=WINDOW_DAYS_AFTER + 1), time.min, tzinfo=tz)
    return start, end


async def sync_calendar(
    pool: AsyncConnectionPool,
    google: GoogleService,
    *,
    timezone: str,
    source: CalendarSource | None = None,
    now: datetime | None = None,
) -> SyncResult:
    now = now or datetime.now(UTC)
    async with _try_lock(pool, _LOCK_CALENDAR) as got:
        if not got:
            return SyncResult(ran=False)
        async with pool.connection() as conn:
            account = await google.account(conn)
            off = await is_off(conn)  # switched off in Settings: no sync
        if account is None or account.needs_reconnect or off:
            return SyncResult(ran=False)
        calendar = source or google.calendar()
        try:
            changed = await _calendar_round(pool, calendar, calendar_window(now, timezone), now)
        except (ConnectorError, PisaError) as exc:
            await _record_failure(pool, google, "calendar", exc)
            _log.warning("calendar_sync_failed", cause=exc.message)
            return SyncResult(ran=True, error=exc.message)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE connector_accounts SET calendar_synced_at = %s WHERE connector = 'google'", (now,)
            )
            await google.record_status(conn, "calendar", error=None)
        _log.info("calendar_synced", changed=changed)
        return SyncResult(ran=True, changed=changed)


async def _calendar_round(
    pool: AsyncConnectionPool, calendar: CalendarSource, window: tuple[datetime, datetime], now: datetime
) -> int:
    time_min, time_max = window
    shown = await calendar.calendars()
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM calendars WHERE NOT (id = ANY(%s))", ([c.id for c in shown],))
        stored = {
            r[0]: (r[1], r[2])
            for r in await (
                await conn.execute("SELECT id, sync_token, window_start FROM calendars")
            ).fetchall()
        }
    changed = 0
    for info in shown:
        token, window_start = stored.get(info.id, (None, None))
        full = token is None or window_start != time_min.date()
        events = None
        if not full:
            try:
                events = await calendar.events(
                    info.id, sync_token=token, time_min=time_min, time_max=time_max
                )
            except SyncTokenExpired:
                _log.info("calendar_sync_token_expired_resync", calendar=info.id)
                full = True
        if full:
            events = await calendar.events(info.id, sync_token=None, time_min=time_min, time_max=time_max)
        assert events is not None
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO calendars (id, name, sync_token, window_start, synced_at, access_role) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, sync_token = EXCLUDED.sync_token, "
                "window_start = EXCLUDED.window_start, synced_at = EXCLUDED.synced_at, "
                "access_role = EXCLUDED.access_role",
                (info.id, info.name, events.next_sync_token, time_min.date(), now, info.access_role),
            )
            owners: list[tuple[str, str | None, int | None, datetime | None]] = []
            if full:
                # The user's kind and minimum stay and "attended" are Pisa's own data: kept
                # across the full resync, which happens every day.
                owners = await (
                    await conn.execute(
                        "SELECT id, kind_owner, min_stay_owner, attended_at FROM calendar_events "
                        "WHERE calendar_id = %s AND (kind_owner IS NOT NULL OR min_stay_owner IS NOT NULL "
                        "OR attended_at IS NOT NULL)",
                        (info.id,),
                    )
                ).fetchall()
                # the outside mark too: once outside text, always (whatever the tag, a full
                # resync never reads a marked event back as their own)
                marked = [
                    str(r[0])
                    for r in await (
                        await conn.execute(
                            "SELECT id FROM calendar_events WHERE calendar_id = %s AND outside", (info.id,)
                        )
                    ).fetchall()
                ]
                await conn.execute("DELETE FROM calendar_events WHERE calendar_id = %s", (info.id,))
            for event in events.events:
                changed += 1
                if event.status == "cancelled" or event.end <= time_min or event.start >= time_max:
                    await conn.execute(
                        "DELETE FROM calendar_events WHERE calendar_id = %s AND id = %s", (info.id, event.id)
                    )
                else:
                    await _upsert_event(conn, event)
            if full and marked:
                await conn.execute(
                    "UPDATE calendar_events SET outside = true WHERE calendar_id = %s AND id = ANY(%s)",
                    (info.id, marked),
                )
            for event_id, kind, min_stay, attended in owners:
                await conn.execute(
                    "UPDATE calendar_events SET kind_owner = %s, min_stay_owner = %s, attended_at = %s "
                    "WHERE calendar_id = %s AND id = %s",
                    (kind, min_stay, attended, info.id, event_id),
                )
    return changed


# Pisa's own create, from its approval (the planner's MADE_BY_PISA join). A
# Pisa move rewrites the event's tag with the move's approval, and the daily full resync re-inserts every
# event: the create's approval keeps it Pisa's whatever the tag says now.
_PISA_CREATED: LiteralString = (
    "EXISTS (SELECT 1 FROM approvals a WHERE a.kind = 'calendar_create' AND a.status = 'done' "
    "AND a.payload->'body'->>'id' = %s)"
)


# the mark a row takes when written, whatever Google's copy says now:
# - Pisa's create whose approval says outside text was behind it (`after_outside`, or an outside value it
#   carried, `payload.outside`; any status: a `failed` create may have reached Google), event or series;
# - the durable ledger `event_outside` (migration 0110): every event or series id ever marked, kept outside
#   the sync window, so a row written fresh (back in the window, a new instance) keeps the mark;
# - a marked copy Pisa already holds: the same event on another of their calendars (a move), or its series.
# Its parameters: `_outside_params(e)`.
_PISA_OUTSIDE: LiteralString = (
    "(EXISTS (SELECT 1 FROM approvals a WHERE a.kind = 'calendar_create' "
    "AND (coalesce((a.payload->>'after_outside')::boolean, false) OR a.payload ? 'outside') "
    "AND a.payload->'body'->>'id' IN (%s, %s)) "
    "OR EXISTS (SELECT 1 FROM event_outside l WHERE l.event_id IN (%s, %s)) "
    "OR EXISTS (SELECT 1 FROM calendar_events o WHERE o.outside AND (o.id IN (%s, %s) "
    "OR o.recurring_event_id = %s)) "
    # the backfill's rule at every write: Pisa's (its tag, an id or a series in Pisa's 32-hex shape,
    # or an approval's) and no clean approval proves it theirs. Clean: no outside text behind it, `done` or
    # still `approved` (a sync can land while Pisa's own create is executing)
    "OR ((%s OR %s ~ '^[0-9a-f]{32}$' OR %s ~ '^[0-9a-f]{32}$' "
    "OR EXISTS (SELECT 1 FROM approvals a WHERE a.kind = 'calendar_create' "
    "AND (a.payload->'body'->>'id' IN (%s, %s) OR replace(a.id::text, '-', '') IN (%s, %s)))) "
    "AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.kind = 'calendar_create' "
    "AND a.status IN ('approved', 'done') AND NOT coalesce((a.payload->>'after_outside')::boolean, false) "
    "AND NOT (a.payload ? 'outside') AND a.payload->'body'->>'id' IN (%s, %s))))"
)


def _outside_params(e: CalendarEvent) -> tuple[str | bool, ...]:
    """`_PISA_OUTSIDE`'s parameters: the event's id and its series' (its own id when it has none), and whether
    its tag says Pisa made it."""
    series = e.recurring_event_id or e.id
    return (e.id, series, e.id, series, e.id, series, series,
            e.made_by_pisa, e.id, series, e.id, series, e.id, series, e.id, series)  # fmt: skip


async def _upsert_event(conn: AsyncConnection, e: CalendarEvent) -> None:
    await conn.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, "
        "start_date, end_date, "
        "location, status, updated, attendees, accepted, guests, recurring_event_id, event_type, visibility, "
        "made_by_pisa, outside, ical_uid, creator_self, organizer_self) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s OR " + _PISA_CREATED + ", %s OR " + _PISA_OUTSIDE + ", %s, %s, %s) "
        "ON CONFLICT (calendar_id, id) DO UPDATE SET title = EXCLUDED.title, starts_at = EXCLUDED.starts_at, "
        "ends_at = EXCLUDED.ends_at, all_day = EXCLUDED.all_day, start_date = EXCLUDED.start_date, "
        "end_date = EXCLUDED.end_date, location = EXCLUDED.location, status = EXCLUDED.status, "
        "updated = EXCLUDED.updated, attendees = EXCLUDED.attendees, accepted = EXCLUDED.accepted, "
        "guests = EXCLUDED.guests, recurring_event_id = EXCLUDED.recurring_event_id, "
        "event_type = EXCLUDED.event_type, visibility = EXCLUDED.visibility, "
        # Google's word on where it came from, as Google says it now (services/foreign.py)
        "ical_uid = EXCLUDED.ical_uid, creator_self = EXCLUDED.creator_self, "
        "organizer_self = EXCLUDED.organizer_self, "
        # once Pisa's, Pisa's (a backfilled row keeps it)
        "made_by_pisa = calendar_events.made_by_pisa OR EXCLUDED.made_by_pisa, "
        # once outside text, always: a re-sync never clears the mark
        "outside = calendar_events.outside OR EXCLUDED.outside",
        (
            e.calendar_id,
            e.id,
            e.title,
            e.start,
            e.end,
            e.all_day,
            e.start_date,
            e.end_date,
            e.location,
            e.status,
            e.updated,
            e.attendees,
            e.accepted,
            Jsonb([{"email": a, "name": n} for a, n in e.guests]) if e.guests else None,
            e.recurring_event_id,
            e.event_type,
            e.visibility,  # private / confidential: never named on the wrist
            e.made_by_pisa,
            e.id,
            e.outside,
            *_outside_params(e),
            e.ical_uid,
            e.creator_self,
            e.organizer_self,
        ),
    )
