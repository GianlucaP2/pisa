# ruff: noqa: E501
"""The review's findings. Each test
fails when its fix is reverted. Synthetic names, places and times, never theirs."""

import asyncio
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from pisa.domain import erase as erase_rules
from pisa.domain import retention as retention_rules
from pisa.domain import zone as zone_rules
from pisa.services import erase, maintenance, owner_loop
from tests.conftest import pair
from tests.owner import owner_at
from tests.phone_signing import device_headers, register
from tests.seed_any import seed
from tests.test_core_keep import _locations, finished

ANCIENT = datetime(2016, 1, 1, tzinfo=UTC)
ROME = ZoneInfo("Europe/Rome")


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db, "iPhone")
    return {"Authorization": f"Bearer {token}"}


def _run(client: Any, fn: Any) -> Any:
    async def go() -> Any:
        async with client.app.state.runtime.pool.connection() as conn:
            return await fn(conn)

    return client.portal.call(go)


# --- The night's strip list and the anonymiser name everyone who wrote to them ------------------------


def test_a_recent_sender_is_never_dropped_from_the_strip_list(client, db: psycopg.Connection) -> None:  # type: ignore[no-untyped-def]
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels) "
            "VALUES (%s, %s, %s, %s, 's', %s, '{INBOX}')",
            [
                (
                    f"old{i}",
                    f"t{i}",
                    f"Mittente {i:05d}",
                    f"m{i:05d}@example.org",
                    ANCIENT + timedelta(minutes=i),
                )
                for i in range(2600)
            ],
        )
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels) "
        "VALUES ('new', 'tn', 'Zelinda Nuovissima', 'zelinda@example.org', 's', now(), '{INBOX}')"
    )
    # no ANALYZE: its statistics outlive the reset and changed later tests' plans (equal sent_at rows read in
    # another order: test_money row5, test_two_countries row1); the query orders itself
    names, _ = _run(client, owner_loop.personal_words)
    assert "Zelinda Nuovissima" in names and "zelinda@example.org" in names
    assert {f"Mittente {i:05d}" for i in range(2600)} <= set(names)  # the whole list, never a subset
    assert names.index("Zelinda Nuovissima") < names.index("Mittente 02599")  # recent first
    night = client.app.state.runtime.night
    stripped = _run(client, night.names)
    assert "Zelinda Nuovissima" in stripped and {f"m{i:05d}@example.org" for i in range(2600)} <= set(
        stripped
    )


# --- Their messages_retention also covers the talks' timelines ----------------------------------------


def test_messages_retention_nulls_an_old_talk_timeline(db: psycopg.Connection, app_settings) -> None:  # type: ignore[no-untyped-def]
    from tests.test_core_retention import NOW, _set

    sessions = {}
    for key, days in (("old", 120), ("new", 10)):
        sessions[key] = uuid.uuid4()
        db.execute(
            "INSERT INTO talk_sessions (id, talker, voice, trigger, lang, started_at, timeline) "
            "VALUES (%s, 'xai', 'v', 'press', 'it-IT', %s, %s)",
            (sessions[key], NOW - timedelta(days=days), Jsonb([{"said": "le sue parole"}])),
        )
    _set(db, "90d", NOW - timedelta(days=3))
    counts = asyncio.run(maintenance.prune_history(app_settings, NOW))
    assert counts["talk_timelines"] == 1
    rows = dict(db.execute("SELECT id, timeline IS NOT NULL FROM talk_sessions").fetchall())
    assert rows == {sessions["old"]: False, sessions["new"]: True}  # the session stays, its words go


# --- the erase: helpers ---------------------------------------------------------------------------------------


def _conn(app_settings: Any, fn: Any) -> Any:
    async def go() -> Any:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return await fn(conn)

    return asyncio.run(go())


def _left(db: psycopg.Connection, table: str) -> int:
    row = db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()
    return int(row[0]) if row else 0


# --- Erasing "records" never resets a cap or a dedupe guard ----------------------------------------


def test_erasing_records_keeps_the_caps_and_the_dedupe_guards(db: psycopg.Connection, app_settings) -> None:  # type: ignore[no-untyped-def]
    tz = app_settings.timezone
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    month = erase_rules.month_start(now, tz)
    for at, status in ((month + timedelta(hours=1), "settled"), (month - timedelta(days=3), "settled")):
        db.execute(
            "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, price_known, trace_id, "
            "created_at) VALUES (%s, 'x', 'x', %s, 0, true, 't', %s)",
            (uuid.uuid4(), status, at),
        )
    recent, old = now - timedelta(hours=20), now - timedelta(days=30)
    for table in ("reminders_sent", "attend_candidates", "walk_recaps", "notifications", "done_by_presence"):
        seed(db, table, recent)
        if table == "done_by_presence":
            db.execute("UPDATE done_by_presence SET event_id = 'e0'")  # one per event
        seed(db, table, old)
    seed(db, "errand_suggestions", now - timedelta(days=5))  # a combination isn't suggested twice a week
    task = seed(db, "tasks", old)
    db.execute("UPDATE tasks SET status = 'open', deleted_at = NULL WHERE id = %s", (task["id"],))
    stuck = seed(db, "morning_thoughts", old)
    db.execute(
        "UPDATE morning_thoughts SET kind = 'stuck_task', ref = %s WHERE id = %s", (task["id"], stuck["id"])
    )
    tables, _ = _conn(app_settings, lambda c: erase.delete(c, "records", now.date(), tz, now))
    assert db.execute("SELECT created_at FROM llm_calls").fetchall() == [(month + timedelta(hours=1),)]
    for table in ("reminders_sent", "attend_candidates", "walk_recaps", "notifications", "done_by_presence"):
        assert _left(db, table) == 1 and tables[table] == 1, table  # the 20-hour-old row stays
    assert _left(db, "errand_suggestions") == 1
    assert _left(db, "morning_thoughts") == 1  # their task is still open: asked once, never again
    db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task["id"],))
    _conn(app_settings, lambda c: erase.delete(c, "records", now.date(), tz, now))
    assert _left(db, "morning_thoughts") == 0


# --- A date after tomorrow is refused (tomorrow is "all of it") ----------------------------------


def test_a_future_before_is_refused(client, db, auth, clock) -> None:  # type: ignore[no-untyped-def]
    private = register(client, auth)
    tomorrow = (clock.now.astimezone(ROME).date() + timedelta(days=2)).isoformat()  # the day after
    preview = client.get(
        "/v1/data/erase/preview", headers=auth, params={"family": "places", "before": tomorrow}
    )
    assert preview.status_code == 400 and preview.json()["error"]["code"] == "validation_failed"
    signed = device_headers(client, auth, private, "erase", f"places:{tomorrow}")
    posted = client.post("/v1/data/erase", headers=signed, json={"family": "places", "before": tomorrow})
    assert posted.status_code == 400 and posted.json()["error"]["code"] == "validation_failed"
    for days in (0, 1):  # today, and tomorrow: all of it
        day = (clock.now.astimezone(ROME).date() + timedelta(days=days)).isoformat()
        ok = client.get("/v1/data/erase/preview", headers=auth, params={"family": "places", "before": day})
        assert ok.status_code == 200


# --- The preview counts exactly what the delete removes, children too, family by family ----------------


@pytest.mark.parametrize("family", sorted(erase_rules.FAMILIES))
def test_the_preview_equals_the_delete(
    db: psycopg.Connection, app_settings, family: erase_rules.Family
) -> None:  # type: ignore[no-untyped-def]
    old = datetime(2016, 3, 1, 12, tzinfo=UTC)
    tz = app_settings.timezone
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    for part in erase_rules.FAMILIES[family]:
        seed(db, part.table, old)
        for (child,) in db.execute(
            "SELECT conrelid::regclass::text FROM pg_constraint WHERE contype = 'f' AND confdeltype = 'c' "
            "AND confrelid = %s::regclass AND conrelid <> confrelid",
            (part.table,),
        ).fetchall():
            seed(db, child, old)  # with a parent of its own, as old
    if family == "mail":
        email = db.execute("SELECT id FROM email_messages LIMIT 1").fetchone()
        assert email is not None
        person = seed(db, "people", old)
        db.execute(
            "INSERT INTO person_notes (person_id, kind, text, source, source_ref, at) "
            "VALUES (%s, 'gift', 'un libro', 'email', %s, %s)",
            (person["id"], f"{email[0]}#0", old),
        )
        db.execute(  # A mail's waiting-for goes with its mail
            "INSERT INTO intentions (id, kind, state, what, source, source_ref, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'waiting_for', 'open', 'il contratto', 'received_mail', %s, %s, %s)",
            (email[0], old, old),
        )
    before = now.date()
    counted = _conn(app_settings, lambda c: erase.count(c, family, before, tz, now))
    gone = _conn(app_settings, lambda c: erase.delete(c, family, before, tz, now))
    assert gone == counted, family
    tables, children = gone
    assert sum(tables.values()) >= 1, tables
    for child in children:
        assert children[child] >= 1 and _left(db, child) == 0, child
    if family == "mail":
        assert children["person_notes"] == 1 and children["email_attachments"] >= 1
        assert children["intentions"] == 1


# --- The delete runs in the background; a retry needs no second Face ID ------------------------------


def test_the_erase_answers_202_and_its_state_is_read_back(client, db, auth, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _locations(db, 400, 401, 402, 403, 404, 2)
    monkeypatch.setattr(retention_rules, "BATCH", 2)  # three batches, then one that takes nothing
    private = register(client, auth)
    before = (date.today() - timedelta(days=30)).isoformat()
    signed = device_headers(client, auth, private, "erase", f"places:{before}")
    started = client.post("/v1/data/erase", headers=signed, json={"family": "places", "before": before})
    assert started.status_code == 202 and started.json()["state"] in {"queued", "running", "done"}
    done = finished(client, auth, started.json()["erase_id"])
    assert done["count"] == 5  # Item 2: counted inside the job
    assert done["state"] == "done" and done["erased"] == 5 and done["tables"]["device_locations"] == 5
    assert done["family"] == "places" and done["before"] == before and done["finished_at"]
    assert _left(db, "device_locations") == 1
    # the nonce is spent: the same signed request again is refused (a replay never deletes twice)
    again = client.post("/v1/data/erase", headers=signed, json={"family": "places", "before": before})
    assert again.status_code == 403 and again.json()["error"]["code"] == "biometric_required"
    assert client.get(f"/v1/data/erase/{uuid.uuid4()}", headers=auth).status_code == 404


# --- pisa_meter's month that holds the day stays; a trip in progress stays ---------------------------------


def test_the_month_holding_the_day_and_a_trip_in_progress_stay(db: psycopg.Connection, app_settings) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    for month in (date(2026, 10, 1), date(2026, 11, 1)):
        db.execute("INSERT INTO pisa_meter (month, computed_at) VALUES (%s, now())", (month,))
    _conn(app_settings, lambda c: erase.delete(c, "memory", date(2026, 11, 15), app_settings.timezone, now))
    assert db.execute("SELECT month FROM pisa_meter").fetchall() == [(date(2026, 11, 1),)]
    away, home = uuid.uuid4(), uuid.uuid4()
    for trip, depart, back in (
        (away, now - timedelta(days=30), None),
        (home, now - timedelta(days=60), now - timedelta(days=40)),
    ):
        db.execute(
            "INSERT INTO trips (id, title, depart_at, arrive_at, back_at, created_at, updated_at) "
            "VALUES (%s, 'Lisbona', %s, %s, %s, %s, %s)",
            (trip, depart, depart + timedelta(hours=3), back, depart, depart),
        )
    _conn(app_settings, lambda c: erase.delete(c, "places", now.date(), app_settings.timezone, now))
    assert db.execute("SELECT id FROM trips").fetchall() == [(away,)]  # still away: never deleted


# --- The cutoff is the followed zone's --------------------------------------------------------------------


def test_the_cutoff_follows_his_zone(client, db, auth, clock) -> None:  # type: ignore[no-untyped-def]
    """Rome's start-up default vs the phone's Kiritimati (UTC+14): a point between their two midnights."""
    before = clock.now.astimezone(ROME).date() - timedelta(days=3)
    rome_midnight = erase_rules.cutoff(before, "Europe/Rome")
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    assert device is not None
    db.execute(
        "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) VALUES (%s, 45, 9, 5, %s)",
        (device[0], rome_midnight - timedelta(hours=5)),
    )

    def counted() -> int:
        got = client.get(
            "/v1/data/erase/preview", headers=auth, params={"family": "places", "before": before.isoformat()}
        )
        assert got.status_code == 200, got.text
        return int(got.json()["tables"]["device_locations"])

    assert counted() == 1
    client.app.state.runtime.owner.place = zone_rules.PhoneContext("Pacific/Kiritimati")
    try:
        assert counted() == 0  # Kiritimati's midnight came 13 hours before Rome's
    finally:
        client.app.state.runtime.owner.place = None


# --- The Sunday card's counts are the planner's estimates ---------------------------------------------


def test_the_kept_rows_are_estimates_not_counts() -> None:
    import inspect

    source = inspect.getsource(erase.kept).split('"""')[2]  # the code, after its docstring
    assert "count(" not in source and "reltuples" in source and "pg_stat_user_tables" in source


# --- The hunch gate is the one helper (psic…, fisioterap… too) ---------------------------------------


@pytest.mark.parametrize(
    "seen",
    [
        "martedì 17:00, Studio Psicologia Rossi",
        "giovedì 18:30, Fisioterapia Centro",
        "domenica, Chiesa di San Marco",
    ],
)
def test_a_hunch_from_any_sensitive_stay_is_refused(client, db, seen: str) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    rituals = client.app.state.runtime.rituals
    said = client.portal.call(
        lambda: rituals.note_hunch("Il martedì pomeriggio passi sempre in centro?", [seen], uuid.UUID(device))
    )
    assert said.startswith("Not stored") and db.execute("SELECT count(*) FROM hunches").fetchone() == (0,)


# --- The health recall answers only their own visit question -------------------------------------------


@pytest.mark.parametrize(
    ("words", "place", "category"),
    [
        ("Marco è stato in ospedale ieri?", "Ospedale Maggiore", "hospital"),
        ("ho pranzato col dottor Bianchi ieri?", "Studio Medico Bianchi", "doctor"),
        ("ho preso le medicine ieri?", "Farmacia Aurora", "pharmacy"),
        ("did Marco go to the hospital yesterday?", "Ospedale Maggiore", "hospital"),
    ],
)
def test_a_question_that_is_not_his_visit_falls_through(
    client, db, clock, words: str, place: str, category: str
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_core_health import _place, _stay

    device, _ = pair(db)
    pid = _place(db, place, category)
    clock.now = datetime(2026, 11, 12, 18, 0, tzinfo=ROME).astimezone(UTC)
    _stay(
        db,
        device,
        pid,
        datetime(2026, 11, 11, 10, 0, tzinfo=ROME),
        datetime(2026, 11, 11, 10, 40, tzinfo=ROME),
    )
    svc = client.app.state.runtime.episodes
    lang = "en-US" if words.startswith("did") else "it-IT"
    got = client.portal.call(lambda: svc.recall("x", words=words, lang=lang))
    say = str(got.get("say") or "")
    assert place not in say, got  # never the stay: not their own visit question
    assert not say.startswith(("Sì: ", "Yes: ", "Non ho traccia di una sosta", "I have no stay")), (
        got
    )  # nor its branch
    first_person = {
        "Marco è stato in ospedale ieri?": "ieri sono stato in ospedale?",
        "ho pranzato col dottor Bianchi ieri?": "ieri sono andato dal dottor Bianchi?",
        "ho preso le medicine ieri?": "ieri sono passato in farmacia?",
        "did Marco go to the hospital yesterday?": "did I go to the hospital yesterday?",
    }[words]
    mine = client.portal.call(lambda: svc.recall("x", words=first_person, lang=lang))
    assert mine["answer"] == "one" and place in mine["say"], mine  # their own: answered


# --- The places prune's referrers are indexed; a link or a claim by text keeps the place ----------------


def test_a_google_only_place_an_episode_link_or_a_claim_points_at_stays(
    db: psycopg.Connection, app_settings
) -> None:  # type: ignore[no-untyped-def]
    from pisa.services import places

    old = datetime.now(UTC) - timedelta(days=3653)
    linked, claimed, alone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for place in (linked, claimed, alone):
        db.execute(
            "INSERT INTO places (id, name, google_place_id, created_at, updated_at) VALUES (%s, 'p', %s, %s, %s)",
            (place, place.hex, old, old),
        )
    episode = seed(db, "episodes", old)
    db.execute(
        "INSERT INTO episode_links (episode_id, target_kind, target_id, role, source) "
        "VALUES (%s, 'place', %s, 'at', 'code')",
        (episode["id"], str(linked)),
    )
    claim = seed(db, "profile_claims", old)
    db.execute("UPDATE profile_claims SET subject_key = %s WHERE id = %s", (f"place:{claimed}", claim["id"]))
    asyncio.run(maintenance.prune_places(app_settings, datetime.now(UTC)))
    assert {r[0] for r in db.execute("SELECT id FROM places").fetchall()} == {linked, claimed}
    for table in places.PLACE_REFERRERS:  # each referrer's place_id has its index (0091)
        indexed = db.execute(
            "SELECT 1 FROM pg_indexes WHERE tablename = %s AND indexdef ~ '\\(place_id\\)'", (table,)
        ).fetchone()
        assert indexed, table


# --- The per-utterance and per-scan reads use an index, never a scan of the table -----------------------


def _plan(db: psycopg.Connection, query: str, params: tuple[Any, ...] = ()) -> str:
    with db.transaction():
        db.execute("SET LOCAL enable_seqscan = off")
        rows = db.execute(sql.SQL("EXPLAIN ") + sql.SQL(query), params).fetchall()  # type: ignore[arg-type]
    return "\n".join(str(r[0]) for r in rows)


def test_the_ready_version_the_undo_scan_and_the_different_count_use_indexes(db: psycopg.Connection) -> None:
    import re

    from pisa.services import ready

    text = Path(ready.__file__).read_text()
    version = re.search(r"async def _version.*?execute\(\n(.*?)\n            \)", text, re.S)
    assert version is not None
    query = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', version.group(1)))
    plan = _plan(db, query, (datetime.now(UTC) - timedelta(days=2),) * query.count("%s"))
    for table in ("approvals", "pisa_promises", "plans"):
        assert f"Seq Scan on {table}" not in plan, plan
    plan = _plan(
        db,
        "SELECT a.id FROM approvals a WHERE a.undone_at IS NOT NULL AND a.executed_at IS NOT NULL "
        "AND a.undone_at - a.executed_at <= interval '2 minutes' AND a.undone_at >= now() - interval '1 day'",
    )
    assert "Seq Scan on approvals" not in plan, plan
    plan = _plan(
        db,
        "SELECT count(*) FROM pisa_decisions WHERE move = 'show' AND tool = 'show_places' AND n > "
        "coalesce((SELECT max(n) FROM pisa_decisions WHERE rail = 'something different'), 0)",
    )
    assert "Seq Scan on pisa_decisions" not in plan, plan


def test_the_migrations_readme_asks_for_concurrent_indexes() -> None:

    readme = (Path(__file__).resolve().parents[1] / "pisa" / "db" / "migrations" / "README.md").read_text()
    assert "CONCURRENTLY" in readme and "kept table" in readme


# --- the guard, again: the other scheduled deleters delete nothing of theirs either -------------------------------


def test_the_other_scheduled_deleters_delete_nothing_of_his(
    db: psycopg.Connection, app_settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """people_offers.plan (hourly), stays.refresh (every upload) and the Google sync's own deletes (Gmail's
    deletions mirrored, the calendar list's) run over a decade-old row in every KEPT table: only what Gmail
    said they deleted goes; every other statement that writes targets PRUNED, the sync's bookkeeping, the
    stays of the last day it rebuilds, or the calendar mirror."""
    from psycopg_pool import AsyncConnectionPool

    from pisa.domain.calendar import CalendarInfo, EventChanges
    from pisa.domain.mail import MailChanges
    from pisa.services import google_sync, stays
    from pisa.services.people_offers import PeopleOffers
    from tests.test_core_keep import seed_everything
    from tests.test_retention import PRUNED, migration_tables

    seed_everything(db)
    db.execute("INSERT INTO email_messages (id, thread_id, sent_at, body) VALUES ('m-gone', 't', now(), 'x')")
    mirror = {"calendar_events"}  # the calendar's window, mirrored from Google, where their calendar lives
    watched = migration_tables() - set(PRUNED) - mirror
    before = {t: _left(db, t) for t in sorted(watched)}
    statements: list[str] = []
    real: Any = psycopg.AsyncConnection.execute

    async def spy(self: Any, query: Any, *args: Any, **kw: Any) -> Any:
        statements.append(query.as_string(None) if isinstance(query, sql.Composable) else str(query))
        return await real(self, query, *args, **kw)

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", spy)
    now = datetime.now(UTC)
    calendars = [CalendarInfo(str(r[0]), "c") for r in db.execute("SELECT id FROM calendars").fetchall()]
    devices = [r[0] for r in db.execute("SELECT id FROM devices").fetchall()]

    class Mail:
        async def changes_since(self, history_point: str) -> MailChanges:
            return MailChanges(history_id="h2", deleted=("m-gone",))

    class Calendar:
        async def calendars(self) -> list[CalendarInfo]:
            return calendars

        async def events(self, calendar_id: str, **_: Any) -> EventChanges:
            return EventChanges(events=(), next_sync_token="t2")

    async def go() -> None:
        async with AsyncConnectionPool(
            app_settings.database_url,
            connection_class=psycopg.AsyncConnection,
            kwargs={"autocommit": True},
            open=False,
        ) as pool:
            async with pool.connection() as conn:
                await PeopleOffers(pool, owner=owner_at(app_settings.timezone)).plan(conn, now)
                for device in devices:
                    await stays.refresh(conn, device, now)
            await google_sync._gmail_round(pool, Mail(), "h1", "him@example.org")  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
            await google_sync._calendar_round(
                pool, Calendar(), (now - timedelta(days=7), now + timedelta(days=30)), now
            )  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]

    asyncio.run(go())
    after = {t: _left(db, t) for t in sorted(watched)}
    assert after == before | {"email_messages": before["email_messages"] - 1}, {
        t: (before[t], after[t]) for t in before if before[t] != after[t]
    }
    allowed = (
        set(PRUNED) | mirror | {"email_messages", "stays", "app_settings", "connector_accounts", "calendars"}
    )
    written = {m.group(1) for q in statements if (m := re.match(r"\s*(?:DELETE FROM|UPDATE)\s+\"?(\w+)", q))}
    assert written <= allowed, sorted(written - allowed)
