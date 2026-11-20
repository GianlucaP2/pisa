"""Things v1: hard-key links between emails, tasks and events (`pisa.domain.things`), and the
whole thing for Pisa's `thing` tool. Logs carry ids and counts only.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from pisa.domain import things as rules
from pisa.logging import get_logger

_log = get_logger("pisa.things")
READ_EMAILS = 6  # the newest emails of a thing Pisa reads


async def link_email(
    conn: AsyncConnection,
    *,
    message_id: str,
    thread_id: str | None,
    keys: list[dict[str, Any]],
    subject: str | None,
    gist: dict[str, Any] | None,
    now: datetime,
    sender: str | None = None,
) -> uuid.UUID | None:
    """The thing this email belongs to (made when it carries a number that names one), or None."""
    row = await (
        await conn.execute(
            "SELECT thing_id FROM thing_items WHERE item_kind = 'email' AND item_ref = %s", (message_id,)
        )
    ).fetchone()
    if row is not None:
        return row[0]
    # RAIL: a number names a thing only for its sender's domain: two shops' "Fattura 00042",
    # or a stranger quoting the user's order number, never join (or move the date of) another sender's thing.
    domain = (sender or "").rsplit("@", 1)[-1].lower() or "?"
    typed = [(kind, f"{domain}|{value}") for kind, value in rules.typed_keys(keys)]
    hard = [key for _, key in typed]
    thing: uuid.UUID | None = None
    how = "key"
    if hard:
        # a key kept before kinds were takes its kind from the email that names it
        for kind, key in typed:
            await conn.execute("UPDATE thing_keys SET kind = %s WHERE key = %s AND kind IS NULL", (kind, key))
        named = await (
            await conn.execute(
                "SELECT DISTINCT thing_id FROM thing_keys WHERE key = ANY(%s) ORDER BY thing_id", (hard,)
            )
        ).fetchall()
        held = [r[0] for r in named]
        if (lowest := rules.anchor(typed)) is not None:
            # an email naming two orders goes with its lowest order's thing (made
            # if none holds it), never with whichever was read first
            row = await (
                await conn.execute("SELECT thing_id FROM thing_keys WHERE key = %s", (lowest,))
            ).fetchone()
            thing = row[0] if row else None
        elif len(held) > 1:
            theirs = await (
                await conn.execute("SELECT kind, key FROM thing_keys WHERE thing_id = ANY(%s)", (held,))
            ).fetchall()
            if rules.joinable(typed, [(k, v) for k, v in theirs]):
                thing = await _one_of(conn, held, now)
            else:  # the thing that holds the email's lowest key, never the oldest
                row = await (
                    await conn.execute(
                        "SELECT thing_id FROM thing_keys WHERE key = ANY(%s) ORDER BY key LIMIT 1", (hard,)
                    )
                ).fetchone()
                thing = row[0] if row else None
        elif held:
            thing = held[0]
    if thing is None and thread_id and rules.anchor(typed) is None:
        # an email naming several orders belongs with its lowest order's thing, never a thread's
        found = await (
            await conn.execute(
                "SELECT ti.thing_id FROM thing_items ti JOIN email_messages m ON m.id = ti.item_ref "
                "WHERE ti.item_kind = 'email' AND m.thread_id = %s ORDER BY m.sent_at, m.id LIMIT 1",
                (thread_id,),
            )
        ).fetchone()
        if found is not None and await _other_order(conn, found[0], typed):
            found = None  # the merge: one thread, two orders (a shop's same subject): never one thing
        thing, how = (found[0], "thread") if found else (None, how)
    if thing is None:
        if not hard:
            return None  # no number, no thread: not a thing (v1 never guesses)
        thing, how = uuid.uuid4(), "first"
        title = rules.title(subject, (gist or {}).get("what"))
        await conn.execute(
            "INSERT INTO things (id, title, created_at, updated_at) VALUES (%s, %s, %s, %s)",
            (thing, title, now, now),
        )
    await _add(conn, thing, "email", message_id, how, now)
    if thread_id and not await _thread_split(conn, thread_id, thing):
        # an email of this thread read before the one with the number (no number, no
        # thing then) joins now, as it would have had the numbered one come first. Never when the thread
        # already holds another thing's email (two orders in one thread: a keyless email is no one's)
        await conn.execute(
            "INSERT INTO thing_items (thing_id, item_kind, item_ref, linked_by, created_at) "
            "SELECT %s, 'email', m.id, 'thread', %s FROM email_messages m WHERE m.thread_id = %s "
            "AND m.id <> %s AND m.intake_status = 'done' AND m.intake_category IS DISTINCT FROM "
            "'promo_or_spam' ON CONFLICT DO NOTHING",
            (thing, now, thread_id, message_id),
        )
    if how != "first":  # the thing is named by its earliest email, whichever came first
        earliest = await (
            await conn.execute(
                "SELECT m.id FROM thing_items ti JOIN email_messages m ON m.id = ti.item_ref WHERE "
                "ti.thing_id = %s AND ti.item_kind = 'email' ORDER BY m.sent_at, m.id LIMIT 1",
                (thing,),
            )
        ).fetchone()
        if earliest is not None and earliest[0] == message_id:
            await conn.execute(
                "UPDATE things SET title = %s WHERE id = %s",
                (rules.title(subject, (gist or {}).get("what")), thing),
            )
    # its keys are this thing's, except one of a kind it already holds another of (an invoice naming two
    # orders: the other order stays a thing of its own)
    # one key per class (order, booking, ticket and reference are one class; a key of
    # unknown kind is order-class)
    have: dict[str, str] = {}
    for k, v in await (
        await conn.execute("SELECT kind, key FROM thing_keys WHERE thing_id = %s ORDER BY key", (thing,))
    ).fetchall():
        have.setdefault(rules.key_class(k), v)
    for kind, key in sorted(typed, key=lambda t: t[1]):
        if have.setdefault(rules.key_class(kind), key) != key:
            continue
        await conn.execute(
            "INSERT INTO thing_keys (key, thing_id, kind) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (key, thing, kind),
        )
    await _link_events(conn, thing, keys, (gist or {}).get("where"), now)
    await conn.execute("UPDATE things SET updated_at = %s WHERE id = %s", (now, thing))
    _log.info("thing_linked", thing_id=str(thing), by=how)
    return thing


async def _other_order(conn: AsyncConnection, thing: uuid.UUID, typed: list[tuple[str, str]]) -> bool:
    """The thread's thing already holds an order-class number other than this email's (the merge)."""
    mine = {v for k, v in typed if rules.key_class(k) == "id"}
    if not mine:
        return False
    theirs = {
        v
        for k, v in await (
            await conn.execute("SELECT kind, key FROM thing_keys WHERE thing_id = %s", (thing,))
        ).fetchall()
        if rules.key_class(k) == "id"
    }
    return bool(theirs) and not (theirs & mine)


async def _thread_split(conn: AsyncConnection, thread_id: str, thing: uuid.UUID) -> bool:
    """Some email of this thread already belongs to another thing (the merge)."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM thing_items ti JOIN email_messages m ON m.id = ti.item_ref WHERE ti.item_kind = "
            "'email' AND m.thread_id = %s AND ti.thing_id <> %s LIMIT 1",
            (thread_id, thing),
        )
    ).fetchone()
    return row is not None


async def _one_of(conn: AsyncConnection, held: list[uuid.UUID], now: datetime) -> uuid.UUID:
    """An email whose numbers name two things (the order's and the parcel's, the two read
    before the email that carries both) makes them one, as reading it first would have: the one whose
    earliest email came first keeps its id and title; the other's emails, tasks, events and numbers join it.
    Only a hard number of the same sender's domain joins things (never a model's guess)."""
    if len(held) == 1:
        return held[0]
    ordered = [
        r[0]
        for r in await (
            await conn.execute(
                "SELECT t.id FROM things t LEFT JOIN thing_items ti ON ti.thing_id = t.id AND ti.item_kind = "
                "'email' LEFT JOIN email_messages m ON m.id = ti.item_ref WHERE t.id = ANY(%s) GROUP BY t.id "
                "ORDER BY min(m.sent_at) NULLS LAST, t.id",
                (held,),
            )
        ).fetchall()
    ]
    keep, gone = ordered[0], ordered[1:]
    await conn.execute("UPDATE thing_items SET thing_id = %s WHERE thing_id = ANY(%s)", (keep, gone))
    await conn.execute("UPDATE thing_keys SET thing_id = %s WHERE thing_id = ANY(%s)", (keep, gone))
    await conn.execute(
        "UPDATE episode_links SET target_id = %s WHERE target_kind = 'thing' AND target_id = ANY(%s)",
        (str(keep), [str(g) for g in gone]),
    )
    await conn.execute("DELETE FROM things WHERE id = ANY(%s)", (gone,))
    await conn.execute("UPDATE things SET updated_at = %s WHERE id = %s", (now, keep))
    _log.info("things_merged", thing_id=str(keep), merged=len(gone))
    return keep


async def _link_events(
    conn: AsyncConnection, thing: uuid.UUID, keys: list[dict[str, Any]], where: str | None, now: datetime
) -> None:
    days, words = rules.dates(keys), rules.link_words(where)  # never a city's or a street's word
    if not days or not words:
        return
    for day in days:
        rows = await (
            await conn.execute(
                "SELECT DISTINCT ON (id) id, title, location FROM calendar_events "
                "WHERE coalesce(start_date, (starts_at AT TIME ZONE 'UTC')::date) BETWEEN %s AND %s",
                (day - timedelta(days=1), day + timedelta(days=1)),
            )
        ).fetchall()
        for event_id, title, location in rows:
            if rules.same_venue(words, title or "", location):
                await _add(conn, thing, "event", event_id, "date_place", now)


async def _add(conn: AsyncConnection, thing: uuid.UUID, kind: str, ref: str, how: str, now: datetime) -> None:
    await conn.execute(
        "INSERT INTO thing_items (thing_id, item_kind, item_ref, linked_by, created_at) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (thing, kind, ref, how, now),
    )


async def link_task(conn: AsyncConnection, thing: uuid.UUID, task_id: str, now: datetime) -> None:
    await _add(conn, thing, "task", task_id, "source", now)


async def open_task(conn: AsyncConnection, thing: uuid.UUID) -> str | None:
    """RAIL: one task per thing: its open one, if any."""
    row = await (
        await conn.execute(
            "SELECT t.id::text FROM thing_items ti JOIN tasks t ON t.id::text = ti.item_ref "
            "WHERE ti.thing_id = %s AND ti.item_kind = 'task' AND t.status = 'open' LIMIT 1",
            (thing,),
        )
    ).fetchone()
    return row[0] if row else None


async def find(conn: AsyncConnection, ref: str) -> uuid.UUID | None:
    """The thing the user means: a number it carries, else the newest whose title or tasks name its words."""
    key = " ".join(ref.split()).upper()
    row = await (
        await conn.execute(
            "SELECT thing_id FROM thing_keys WHERE split_part(key, '|', 2) = %s LIMIT 1", (key,)
        )
    ).fetchone()
    if row is not None:
        return row[0]
    words = sorted(rules.venue_words(ref))
    if not words:
        return None
    rows = await (
        await conn.execute(
            "SELECT th.id, lower(th.title || ' ' || coalesce(string_agg(t.title, ' '), '')) FROM things th "
            "LEFT JOIN thing_items ti ON ti.thing_id = th.id AND ti.item_kind = 'task' "
            "LEFT JOIN tasks t ON t.id::text = ti.item_ref GROUP BY th.id "
            "ORDER BY th.updated_at DESC LIMIT 200"
        )
    ).fetchall()
    for thing, text in rows:
        if all(w in text for w in words):
            return thing
    return None


async def read(conn: AsyncConnection, thing: uuid.UUID, timezone: str) -> dict[str, Any]:
    """The thing whole: its emails (newest first, each as its gist), its tasks and its events."""
    tz = ZoneInfo(timezone)
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute("SELECT title FROM things WHERE id = %s", (thing,))
    head = await cur.fetchone()
    await cur.execute(
        "SELECT m.subject, m.sent_at, m.intake_gist, m.from_name, m.from_address FROM thing_items ti "
        "JOIN email_messages m ON m.id = ti.item_ref WHERE ti.thing_id = %s AND ti.item_kind = 'email' "
        "ORDER BY m.sent_at DESC LIMIT %s",
        (thing, READ_EMAILS),
    )
    emails = [
        {"when": r["sent_at"].astimezone(tz).strftime("%a %d %b %H:%M") if r["sent_at"] else None,
         "subject": r["subject"], "gist": r["intake_gist"], "from_name": r["from_name"],
         "from_address": r["from_address"]}
        for r in await cur.fetchall()
    ]  # fmt: skip
    await cur.execute(
        "SELECT t.title, t.status, t.due_at FROM thing_items ti JOIN tasks t ON t.id::text = ti.item_ref "
        "WHERE ti.thing_id = %s AND ti.item_kind = 'task' AND t.status <> 'deleted'",
        (thing,),
    )
    tasks = [{"title": r["title"], "status": r["status"],
              "due": r["due_at"].astimezone(tz).strftime("%a %d %b") if r["due_at"] else None}
             for r in await cur.fetchall()]  # fmt: skip
    await cur.execute(
        "SELECT DISTINCT ON (e.id) e.title, e.starts_at, e.location FROM thing_items ti "
        "JOIN calendar_events e ON e.id = ti.item_ref WHERE ti.thing_id = %s AND ti.item_kind = 'event'",
        (thing,),
    )
    events = [{"title": r["title"], "location": r["location"],
               "start": r["starts_at"].astimezone(tz).strftime("%a %d %b %H:%M") if r["starts_at"] else None}
              for r in await cur.fetchall()]  # fmt: skip
    return {"title": head["title"] if head else "", "emails": emails, "tasks": tasks, "events": events}
