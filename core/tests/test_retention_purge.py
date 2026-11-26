"""What Pisa read from Gmail is kept short: text 30 days, keys until their tasks closed 30 days, all of it
gone on a disconnect, and what they erase takes it too. Synthetic rows only."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql

from pisa.domain import erase as erase_rules
from pisa.services import erase, maintenance
from tests.gateway_env import run
from tests.google_fake import FakeGoogle
from tests.seed_any import seed
from tests.test_google import google_env

NOW = datetime(2026, 11, 25, 3, 40, tzinfo=UTC)


def days(n: float) -> datetime:
    return NOW - timedelta(days=n)


def message(db: psycopg.Connection, mid: str, fetched: datetime, thread: str = "t1") -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, sent_at, body, new_text, quoted, snippet, fetched_at) "
        "VALUES (%s, %s, %s, 'body text', 'new text', 'quoted text', 'a snippet', %s)",
        (mid, thread, fetched, fetched),
    )


def task(db: psycopg.Connection, title: str, *, status: str = "open", closed: datetime | None = None,
         entity: uuid.UUID | None = None, state: str | None = None) -> uuid.UUID:  # fmt: skip
    tid = uuid.uuid4()
    when = closed or days(60)
    db.execute(
        "INSERT INTO tasks (id, title, status, source_kind, title_key, created_at, updated_at, entity_id, "
        "state) VALUES (%s, %s, %s, 'email', %s, %s, %s, %s, %s)",
        (tid, title, status, title.lower(), days(90), when, entity, state),
    )
    return tid


def entity(db: psycopg.Connection, made: datetime, value: str, source: str = "mail") -> uuid.UUID:
    eid = uuid.uuid4()
    db.execute(
        "INSERT INTO entity (id, kind, issuer, source, created_at, updated_at) "
        "VALUES (%s, 'order', 'shop.example.com', %s, %s, %s)",
        (eid, source, made, made),
    )
    db.execute(
        "INSERT INTO entity_key (entity_id, key_kind, issuer, value, verbatim_source, created_at) "
        "VALUES (%s, 'order_id', 'shop.example.com', %s, %s, %s)",
        (eid, value, value, made),
    )
    return eid


def count(db: psycopg.Connection, table: str, where: str = "true") -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE {}").format(sql.Identifier(table), sql.SQL(where))  # type: ignore[arg-type]
    row = db.execute(query).fetchone()
    return int(row[0]) if row else 0


def test_retention_purge(db: psycopg.Connection, app_settings: Any) -> None:
    # --- text: 31 days after the fetch it goes, 29 days it stays
    message(db, "old", days(31))
    message(db, "new", days(29))
    for mid, made in (("old", days(31)), ("new", days(29))):
        db.execute(
            "INSERT INTO mail_event (id, message_id, kind, quote, created_at) "
            "VALUES (gen_random_uuid(), %s, 'shipped', 'it shipped', %s)",
            (mid, made),
        )
        db.execute(
            "INSERT INTO assertion (subject_kind, subject_id, predicate, valid_from, recorded_at, quote) "
            "VALUES ('entity', %s, 'shipped', %s, %s, 'it shipped')",
            (mid, made, made),
        )
    waiting = task(db, "Wait for shop", state="waiting")
    for made in (days(31), days(29)):
        db.execute(
            "INSERT INTO task_evidence (task_id, message_id, quote, created_at) VALUES (%s, 'old', 'q', %s)",
            (waiting, made),
        )

    # --- keys: an open task keeps them however old; a closed one 30 days; none, 30 days from the thing
    open_old = entity(db, days(200), "KEEP-OPEN")
    task(db, "Open one", entity=open_old)
    waiting_old = entity(db, days(200), "KEEP-WAITING")
    task(db, "Waiting one", entity=waiting_old, state="waiting")
    closed_long = entity(db, days(200), "GO-CLOSED")
    task(db, "Closed long", status="done", closed=days(31), entity=closed_long)
    closed_short = entity(db, days(200), "KEEP-CLOSED-RECENT")
    task(db, "Closed lately", status="done", closed=days(5), entity=closed_short)
    one_open_one_closed = entity(db, days(200), "KEEP-MIXED")
    task(db, "Mixed A", status="done", closed=days(90), entity=one_open_one_closed)
    task(db, "Mixed B", entity=one_open_one_closed)
    cancelled_by_state = entity(db, days(200), "GO-CANCELLED")
    task(
        db, "Cancelled by state", status="open", state="cancelled", closed=days(40), entity=cancelled_by_state
    )
    entity(db, days(40), "GO-NO-TASK")
    entity(db, days(2), "KEEP-NO-TASK-NEW")
    tasks_before = count(db, "tasks")

    counts = asyncio.run(maintenance.purge_mail(app_settings, NOW))

    rows = dict(
        db.execute(
            "SELECT id, body IS NULL AND new_text IS NULL AND quoted IS NULL FROM email_messages"
        ).fetchall()
    )
    assert rows == {"old": True, "new": False}
    assert db.execute("SELECT snippet FROM email_messages WHERE id = 'old'").fetchone() == ("",)  # blanked
    assert db.execute("SELECT count(*) FROM email_messages").fetchone() == (2,)  # the rows stay
    assert dict(db.execute("SELECT message_id, quote IS NULL FROM mail_event").fetchall()) == {
        "old": True,
        "new": False,
    }
    assert dict(db.execute("SELECT subject_id, quote IS NULL FROM assertion").fetchall()) == {
        "old": True,
        "new": False,
    }
    assert count(db, "task_evidence") == 2 and count(db, "task_evidence", "quote IS NULL") == 1
    kept = {r[0] for r in db.execute("SELECT value FROM entity_key").fetchall()}
    assert kept == {"KEEP-OPEN", "KEEP-WAITING", "KEEP-CLOSED-RECENT", "KEEP-MIXED", "KEEP-NO-TASK-NEW"}
    assert count(db, "tasks") == tasks_before  # tasks stay
    assert counts["email_messages"] == 1 and counts["entity_key"] == 3

    # a second run changes nothing; a closed task's keys come back never
    assert asyncio.run(maintenance.purge_mail(app_settings, NOW)) | {} == {
        "email_messages": 0, "mail_event": 0, "task_evidence": 0, "assertion": 0, "entity_key": 0,
    }  # fmt: skip
    # 26 days later the recent close has aged out too; a thing made 2 days ago is still under 30
    asyncio.run(maintenance.purge_mail(app_settings, NOW + timedelta(days=26)))
    assert {r[0] for r in db.execute("SELECT value FROM entity_key").fetchall()} == {
        "KEEP-OPEN", "KEEP-WAITING", "KEEP-MIXED", "KEEP-NO-TASK-NEW"
    }  # fmt: skip
    assert count(db, "tasks") == tasks_before


def test_retention_purge_is_scheduled() -> None:
    from pisa.workers.main import SCHEDULES

    schedule = next(s for s in SCHEDULES if s.name == "purge-mail")
    assert schedule.cron == "0 40 3 * * *"  # nightly, after the 03:30 prune


def test_disconnect_purge(db: psycopg.Connection, secrets_key: str, clock: Any) -> None:
    message(db, "m1", days(1))
    eid = entity(db, days(1), "AB-1042")
    other = entity(db, days(1), "MANUAL-1", source="owner")  # not from Gmail: stays
    tid = task(db, "Ship the parcel", entity=eid, state="waiting")
    db.execute("UPDATE tasks SET intent = 'ship', waiting_until = %s WHERE id = %s", (days(-2), tid))
    event = uuid.uuid4()
    db.execute(
        "INSERT INTO mail_event (id, message_id, entity_id, kind, quote) "
        "VALUES (%s, 'm1', %s, 'shipped', 'q')",
        (event, eid),
    )
    db.execute(
        "INSERT INTO task_evidence (task_id, message_id, mail_event_id, quote) VALUES (%s, 'm1', %s, 'q')",
        (tid, event),
    )
    db.execute(
        "INSERT INTO owner_identity (account, alias) VALUES ('owner@example.org', 'dana@alias.example.net')"
    )
    db.execute("INSERT INTO email_threads (id, first_at, last_at) VALUES ('t1', %s, %s)", (days(2), days(1)))
    db.execute("INSERT INTO orgs (name, domains, kind) VALUES ('Shop', '{shop.example.com}', 'merchant')")
    db.execute(
        "INSERT INTO life_edge (grade, src_kind, src_id, dst_kind, dst_id) "
        "VALUES ('hard', 'entity', %s, 'org', 'x')",
        (str(eid),),
    )
    for source in ("mail", "owner"):
        db.execute(
            "INSERT INTO assertion (subject_kind, subject_id, predicate, valid_from, source, quote) "
            "VALUES ('entity', %s, 'p', %s, %s, 'q')",
            (str(eid), days(1), source),
        )
    story = uuid.uuid4()
    db.execute("INSERT INTO storyline (id, kind, entity_id) VALUES (%s, 'sale', %s)", (story, eid))
    db.execute(
        "INSERT INTO storyline_event (storyline_id, kind, message_id) VALUES (%s, 'sold', 'm1')", (story,)
    )

    async def go() -> None:
        async with google_env(db, secrets_key, FakeGoogle(), clock) as (google, _):
            await google.disconnect()

    run(go)
    for table in ("email_messages", "mail_event", "task_evidence", "owner_identity", "email_threads", "orgs",
                  "life_edge", "storyline", "storyline_event"):  # fmt: skip
        assert count(db, table) == 0, table
    assert {r[0] for r in db.execute("SELECT id FROM entity").fetchall()} == {
        other
    }  # only the Gmail-derived went
    assert count(db, "entity_key") == 1
    assert db.execute("SELECT source FROM assertion").fetchall() == [("owner",)]
    # the task stays, with its own columns; only its link to the thing that went is cleared
    kept = db.execute("SELECT title, state, intent, entity_id FROM tasks WHERE id = %s", (tid,)).fetchone()
    assert kept == (
        "Ship the parcel", "waiting", "ship", None,
    )  # fmt: skip


def test_profile_claims_erase(db: psycopg.Connection, app_settings: Any) -> None:
    tz = app_settings.timezone
    now = datetime.now(UTC)
    old, new = (
        seed(db, "profile_claims", now - timedelta(days=60)),
        seed(db, "profile_claims", now - timedelta(days=1)),
    )
    db.execute(
        "INSERT INTO profile_evidence (id, claim_id, signal, weight, direction, observed_at, summary) "
        "VALUES (gen_random_uuid(), %s, 'said', 1, 1, %s, 'he said so')",
        (old["id"], now - timedelta(days=60)),
    )
    assert any(p.table == "profile_claims" for p in erase_rules.FAMILIES["memory"])
    before = date.today() - timedelta(days=30)

    async def go() -> tuple[dict[str, int], dict[str, int]]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            preview = await erase.count(conn, "memory", before, tz, now)
            await erase.delete(conn, "memory", before, tz, now)
            return preview

    tables, children = asyncio.run(go())
    assert tables["profile_claims"] == 1 and children["profile_evidence"] == 1
    ids = {r[0] for r in db.execute("SELECT id FROM profile_claims").fetchall()}
    assert old["id"] not in ids and new["id"] in ids  # the old one is erased, the newer one is not
    assert count(db, "profile_evidence") == 0  # its evidence went with it


NEW_TABLES = ("owner_identity", "email_threads", "entity", "entity_key", "orgs", "mail_event",
              "task_evidence",
              "life_edge", "assertion", "storyline", "storyline_event")  # fmt: skip


def test_every_new_table_has_an_erase_part(db: psycopg.Connection, app_settings: Any) -> None:
    parts = {p.table for parts in erase_rules.FAMILIES.values() for p in parts}
    # a table that cascades from a Part's table goes with it (the delete counts it as a child)
    cascading = {"mail_event", "entity_key", "storyline_event"}
    assert set(NEW_TABLES) - cascading <= parts and not cascading & parts and "profile_claims" in parts
    decade = datetime.now(UTC) - timedelta(days=3653)
    for table in NEW_TABLES:
        seed(db, table, decade)
    tid = task(db, "Stays")
    assert all(count(db, t) >= 1 for t in NEW_TABLES)

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            await erase.delete(conn, "mail", date.today() + timedelta(days=1), app_settings.timezone)

    asyncio.run(go())
    assert {t: count(db, t) for t in NEW_TABLES if count(db, t)} == {}
    assert count(db, "tasks", f"id = '{tid}'") == 1  # tasks stay
