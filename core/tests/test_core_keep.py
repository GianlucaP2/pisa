"""Nothing about them is deleted until they delete it.

1. The 03:30 and 15-minute schedules delete nothing outside PRUNED's list: a decade-old row in every KEPT
   table (and the rows that cascade from them) survives both.
2. They delete by family and age: a preview counts, the delete needs the phone's signature, runs in
   batches, spares newer rows and a message a live row cites; no conversation tool or night job reaches it.
"""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql

from pisa.domain import erase as erase_rules
from pisa.domain import retention as retention_rules
from pisa.services import erase, maintenance, places
from tests.conftest import pair
from tests.phone_signing import device_headers, register
from tests.seed_any import seed
from tests.test_retention import CASCADE, KEPT, PRUNED, migration_tables

PISA_ROOT = Path(__file__).resolve().parents[1] / "pisa"
DECADE = timedelta(days=3653)


def finished(client: Any, auth: dict[str, str], erase_id: str) -> dict[str, Any]:
    """The background deletion, once it settled: its state and counts as the phone reads them."""
    client.portal.call(client.app.state.runtime.erase.settled)
    got = client.get(f"/v1/data/erase/{erase_id}", headers=auth)
    assert got.status_code == 200, got.text
    return got.json()


def _counts(db: psycopg.Connection, tables: set[str]) -> dict[str, int]:
    out = {}
    for table in sorted(tables):
        row = db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()
        out[table] = int(row[0]) if row else 0
    return out


# --- 1. the schedules delete nothing of their
# -----------------------------------------------------------------


def seed_everything(db: psycopg.Connection) -> None:
    """A decade-old row in every KEPT table and every table cascading from one."""
    old = datetime.now(UTC) - DECADE
    for table in sorted(KEPT) + sorted(t for t, parent in CASCADE.items() if parent in KEPT):
        try:
            seed(db, table, old)
        except (
            psycopg.errors.UniqueViolation
        ):  # one row a key, already there (configuration a migration seeds)
            assert db.execute(sql.SQL("SELECT 1 FROM {}").format(sql.Identifier(table))).fetchone(), table


def test_the_schedules_delete_nothing_outside_pruned(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seed_everything(db)
    # The 15-minute maintenance resumes a deletion they signed; none is pending here
    db.execute("UPDATE erase_jobs SET state = 'done'")
    watched = migration_tables() - set(PRUNED)
    before = _counts(db, watched)
    assert all(before[t] >= 1 for t in KEPT), [t for t in KEPT if not before[t]]
    statements: list[str] = []
    real: Any = psycopg.AsyncConnection.execute

    async def spy(self: Any, query: Any, *args: Any, **kw: Any) -> Any:
        statements.append(query.as_string(None) if isinstance(query, sql.Composable) else str(query))
        return await real(self, query, *args, **kw)

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", spy)
    now = datetime.now(UTC)
    asyncio.run(maintenance.prune_history(app_settings, now))  # 03:30
    asyncio.run(maintenance.prune_places(app_settings, now))  # every 15 minutes
    assert _counts(db, watched) == before
    written = {
        m.group(1)
        for q in statements
        if (m := re.match(r"\s*(?:DELETE FROM|UPDATE)\s+\"?(\w+)", q))
    }  # fmt: skip
    assert written and written <= set(PRUNED), sorted(written - set(PRUNED))


def test_a_google_only_place_his_rows_point_at_stays(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    old = datetime.now(UTC) - DECADE
    device, _ = pair(db)
    kept, alone = uuid.uuid4(), uuid.uuid4()
    for place in (kept, alone):
        db.execute(
            "INSERT INTO places (id, name, google_place_id, created_at, updated_at) "
            "VALUES (%s, 'p', %s, %s, %s)",
            (place, place.hex, old, old),
        )
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, lat, lon, accuracy_m, last_seen_at, place_id) "
        "VALUES (%s, %s, %s, 45, 9, 10, %s, %s)",
        (uuid.uuid4(), device, old, old, kept),
    )
    asyncio.run(maintenance.prune_places(app_settings, datetime.now(UTC)))
    assert db.execute("SELECT id FROM places").fetchall() == [(kept,)]  # Google's copy nothing uses goes
    assert db.execute("SELECT place_id FROM stays").fetchone() == (kept,)


def test_every_table_pointing_at_a_place_is_known_to_the_prune(db) -> None:  # type: ignore[no-untyped-def]
    rows = db.execute(
        "SELECT conrelid::regclass::text FROM pg_constraint WHERE contype = 'f' "
        "AND confrelid = 'places'::regclass AND confdeltype = 'n'"
    ).fetchall()
    assert {r[0] for r in rows} == set(places.PLACE_REFERRERS)
    source = (PISA_ROOT / "services" / "places.py").read_text()
    for table in places.PLACE_REFERRERS:
        assert f"SELECT 1 FROM {table} r WHERE r.place_id = p.id" in source, table


# --- 2. they delete, Pisa never does ------------------------------------------------------------------


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db, "iPhone")
    return {"Authorization": f"Bearer {token}"}


def _refused(response: Any) -> bool:
    return response.status_code == 403 and response.json()["error"]["code"] == "biometric_required"


def _locations(db: psycopg.Connection, *ages_days: int) -> None:
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    assert device is not None
    now = datetime.now(UTC)
    for age in ages_days:
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
            "VALUES (%s, 45, 9, 5, %s)",
            (device[0], now - timedelta(days=age)),
        )


def test_no_conversation_tool_or_night_job_reaches_the_deletion() -> None:
    """Only the API's route imports the deletion service; the brain's tools and the night never name it."""
    importers = sorted(
        str(p.relative_to(PISA_ROOT)) for p in PISA_ROOT.rglob("*.py")
        if re.search(
            r"\bpisa\.services\.erase\b|from pisa\.services import (?:[\w, ]*\b)?erase\b", p.read_text()
        )
    )  # fmt: skip
    assert importers == [
        "services/ledger.py",
        "services/maintenance.py",
        "services/runtime.py",
    ], importers
    # resumes their signed jobs, starts none
    maintenance = (PISA_ROOT / "services" / "maintenance.py").read_text()
    assert set(re.findall(r"erase\.(\w+)", maintenance)) == {"run_pending"}
    ledger = (PISA_ROOT / "services" / "ledger.py").read_text()  # the Sunday card counts, never deletes
    assert set(re.findall(r"erase\.(\w+)", ledger)) == {"store_size", "kept", "offsite_backup_at"}
    for path in PISA_ROOT.rglob("*.py"):
        text = path.read_text()
        if "runtime.erase" in text or ".erase.erase(" in text or ".erase.start(" in text:
            where = path.relative_to(PISA_ROOT).as_posix()
            if where == "api/app.py":  # the API's start resumes a deletion a restart interrupted
                assert set(re.findall(r"runtime\.erase\.(\w+)", text)) == {"resume"}, path
                continue
            assert where == "api/erase.py", path


def test_an_unsigned_deletion_is_refused_even_before_a_face_id_key(client, db, auth) -> None:  # type: ignore[no-untyped-def]
    _locations(db, 400)
    body = {"family": "places", "before": (date.today() - timedelta(days=30)).isoformat()}
    assert _refused(client.post("/v1/data/erase", headers=auth, json=body))  # no key yet: still refused
    private = register(client, auth)
    assert _refused(client.post("/v1/data/erase", headers=auth, json=body))
    wrong = device_headers(client, auth, private, "erase", f"mail:{body['before']}")  # another family
    assert _refused(client.post("/v1/data/erase", headers=wrong, json=body))
    assert db.execute("SELECT count(*) FROM device_locations").fetchone() == (1,)


def test_the_preview_counts_what_the_signed_delete_removes(client, db, auth) -> None:  # type: ignore[no-untyped-def]
    # their day, as Pisa counts it (their zone): the cutoff is the first instant of `before` in Rome. The
    # boundary rows sit a minute either side of it, so the test holds at any hour and across a DST change (a
    # row "31 days ago" fell after Rome's midnight from 22:00 UTC while 7 Oct was still summer time: 6 Nov
    # 2026)
    before_day = datetime.now(ZoneInfo("Europe/Rome")).date() - timedelta(days=30)
    cut = datetime.combine(before_day, time.min, ZoneInfo("Europe/Rome")).astimezone(UTC)
    _locations(db, 3650, 400, 2)
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    assert device is not None
    for at in (cut - timedelta(minutes=1), cut + timedelta(minutes=1)):  # the first goes, the second stays
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
            "VALUES (%s, 45, 9, 5, %s)",
            (device[0], at),
        )
    old = datetime.now(UTC) - timedelta(days=500)
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, lat, lon, accuracy_m, last_seen_at) "
        "VALUES (%s, %s, %s, 45, 9, 10, %s)",
        (uuid.uuid4(), device[0], old, old),
    )
    private = register(client, auth)
    before = before_day.isoformat()
    preview = client.get(
        "/v1/data/erase/preview", headers=auth, params={"family": "places", "before": before}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["count"] == 4
    assert preview.json()["tables"]["device_locations"] == 3 and preview.json()["tables"]["stays"] == 1
    signed = device_headers(client, auth, private, "erase", f"places:{before}")
    started = client.post("/v1/data/erase", headers=signed, json={"family": "places", "before": before})
    assert started.status_code == 202, started.text
    done = finished(client, auth, started.json()["erase_id"])
    assert done["count"] == preview.json()["count"]  # Counted inside the job
    assert done["state"] == "done" and done["erased"] == preview.json()["count"]
    assert done["tables"] == preview.json()["tables"]
    # the newer rows survive: the one a minute after their day began, and the recent one
    left = sorted(r[0] for r in db.execute("SELECT measured_at FROM device_locations").fetchall())
    assert left[0] == cut + timedelta(minutes=1) and left[1] > datetime.now(UTC) - timedelta(days=3)
    assert len(left) == 2
    assert db.execute("SELECT count(*) FROM stays").fetchone() == (0,)


def test_the_delete_runs_in_batches_and_spares_rows_from_that_day(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pair(db)
    tz = app_settings.timezone
    cut = date(2026, 3, 10)
    first = erase_rules.cutoff(cut, tz)
    _locations(db)
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    assert device is not None
    for at in (first - timedelta(days=9), first - timedelta(days=1), first - timedelta(minutes=1), first):
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
            "VALUES (%s, 45, 9, 5, %s)",
            (device[0], at),
        )
    monkeypatch.setattr(retention_rules, "BATCH", 2)

    async def go() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return (await erase.delete(conn, "places", cut, tz))[0]

    assert asyncio.run(go())["device_locations"] == 3  # 2 + 1: two statements
    assert db.execute("SELECT measured_at FROM device_locations").fetchall() == [(first,)]  # 00:00 Rome stays


def test_a_cited_message_follows_the_safe_rule(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    """A message a live row cites stays (a topic still queued); the rest older than the date go."""
    pair(db)
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    assert device is not None
    conversation = db.execute(
        "INSERT INTO conversations (id, device_id) VALUES (gen_random_uuid(), %s) RETURNING id", (device[0],)
    ).fetchone()
    assert conversation is not None
    old = datetime.now(UTC) - timedelta(days=800)
    ids = []
    for _ in range(3):
        row = db.execute(
            "INSERT INTO messages (conversation_id, role, content, lang, created_at) "
            "VALUES (%s, 'user', 'x', 'it-IT', %s) RETURNING id",
            (conversation[0], old),
        ).fetchone()
        assert row is not None
        ids.append(int(row[0]))
    topic = seed(db, "pisa_topics", old)
    db.execute(
        "UPDATE pisa_topics SET message_id = %s, state = 'queued' WHERE id = %s", (ids[0], topic["id"])
    )
    cut = date.today() - timedelta(days=1)

    async def go() -> tuple[dict[str, int], dict[str, int]]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            counted = (await erase.count(conn, "messages", cut, app_settings.timezone))[0]
            return counted, (await erase.delete(conn, "messages", cut, app_settings.timezone))[0]

    counted, gone = asyncio.run(go())
    assert counted["messages"] == gone["messages"] == 2
    assert db.execute("SELECT id FROM messages").fetchall() == [(ids[0],)]


def test_an_email_s_gift_idea_goes_with_its_email(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    """The note: an email-sourced gift idea lives as long as its email row (now: until he deletes it)."""
    old = datetime.now(UTC) - timedelta(days=800)
    db.execute(
        "INSERT INTO email_messages (id, thread_id, sent_at, body) VALUES ('m-old', 't', %s, 'x')", (old,)
    )
    person = seed(db, "people", old)
    db.execute(
        "INSERT INTO person_notes (person_id, kind, text, source, source_ref, at) "
        "VALUES (%s, 'gift', 'un libro', 'email', 'm-old#0', %s)",
        (person["id"], old),
    )

    async def go() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            tables, children = await erase.delete(conn, "mail", date.today(), app_settings.timezone)
            return tables | children

    gone = asyncio.run(go())
    assert gone["email_messages"] == 1 and gone["person_notes"] == 1
    assert db.execute("SELECT count(*) FROM email_messages").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM person_notes").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM people").fetchone() == (1,)  # the person is theirs: kept


def test_erasing_their_mail_erases_the_invitation_uids_read_in_it(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    old = datetime.now(UTC) - timedelta(days=800)
    db.execute("INSERT INTO mailed_invites (uid, seen_at) VALUES ('old@example.com', %s)", (old,))
    db.execute("INSERT INTO mailed_invites (uid) VALUES ('today@example.com')")

    async def go() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return (await erase.delete(conn, "mail", date.today(), app_settings.timezone))[0]

    assert asyncio.run(go())["mailed_invites"] == 1
    assert db.execute("SELECT uid FROM mailed_invites").fetchall() == [("today@example.com",)]


def test_a_reserved_call_and_a_running_night_stay(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    at = datetime.now(UTC) - timedelta(days=800)
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, price_known, trace_id, "
        "created_at) VALUES (%s, 'x', 'x', 'reserved', 0, true, 't', %s)",
        (uuid.uuid4(), at),
    )
    db.execute("INSERT INTO pisa_nights (night, started_at) VALUES (%s, %s)", (at.date(), at))

    async def go(family: str) -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return (await erase.delete(conn, family, date.today(), app_settings.timezone))[0]

    assert asyncio.run(go("records"))["llm_calls"] == 0
    assert asyncio.run(go("memory"))["pisa_nights"] == 0


def test_every_family_table_exists_and_its_clause_runs(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            for family in erase_rules.FAMILIES:
                assert set((await erase.count(conn, family, date.today(), app_settings.timezone))[0]) == {
                    p.table for p in erase_rules.FAMILIES[family]
                }

    asyncio.run(go())


def test_an_unknown_family_names_the_ones_there_are(client, auth) -> None:  # type: ignore[no-untyped-def]
    bad = client.get("/v1/data/erase/preview", headers=auth, params={"family": "all", "before": "2026-01-01"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_failed"


def test_the_reads_a_prune_kept_small_have_their_indexes(db) -> None:  # type: ignore[no-untyped-def]
    """The scans that relied on a prune (intake's thread and sender, people's runs, the briefing's
    mail, their sent mail, the skill garden) read by an index now."""
    rows = db.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()
    names = {r[0] for r in rows}
    for name in (
        "email_messages_thread", "email_messages_sender", "email_messages_fetched",
        "email_messages_intake_at",
        "email_messages_sent_by_him", "pisa_skill_uses_used_at",
    ):  # fmt: skip
        assert name in names, name
    plan = db.execute(
        "EXPLAIN SELECT to_addresses FROM email_messages WHERE 'SENT' = ANY(labels) ORDER BY sent_at DESC "
        "LIMIT 200"
    ).fetchall()
    assert any("email_messages_sent_by_him" in line for (line,) in plan), plan


# --- 4. the cost of keeping -----------------------------------------------------------------------


def test_the_sunday_card_shows_the_store_and_asks_for_the_offsite_copy(client, db, auth) -> None:  # type: ignore[no-untyped-def]
    _locations(db, 1, 2, 3)
    db.execute("ANALYZE")  # The card reads the planner's estimates, exact once analysed
    summary = client.get("/v1/ledger/summary", headers=auth)
    assert summary.status_code == 200, summary.text
    store = summary.json()["store"]
    assert store["bytes"] > 0 and store["families"]["places"] == 3 and store["offsite_backup_at"] is None
    assert set(store["families"]) == set(erase_rules.FAMILIES)
    texts = [b.get("text", "") for b in summary.json()["sheet"]["blocks"]]
    assert any(t.startswith("Il tuo archivio: ") and "luoghi e GPS 3" in t for t in texts), texts
    assert any("backup fuori sede non è attivo" in t for t in texts), texts
    # backup_offsite.sh records its upload: the ask stops
    db.execute("INSERT INTO app_meta (key, value) VALUES ('offsite_backup_at', to_jsonb(now())::text)")
    again = client.get("/v1/ledger/summary", headers=auth).json()
    assert again["store"]["offsite_backup_at"] is not None
    assert not any("backup fuori sede" in b.get("text", "") for b in again["sheet"]["blocks"])
