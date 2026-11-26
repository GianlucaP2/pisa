# ruff: noqa: E501
"""The second review's findings. Each
test fails when its fix is reverted. Synthetic names, places and times, never theirs."""

import asyncio
import inspect
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql

from pisa.domain import erase as erase_rules
from pisa.domain import retention as retention_rules
from pisa.services import erase, maintenance
from pisa.services.runtime import Runtime
from tests.conftest import pair
from tests.phone_signing import device_headers, register
from tests.seed_any import seed
from tests.test_core_keep import finished


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db, "iPhone")
    return {"Authorization": f"Bearer {token}"}


def _points(db: psycopg.Connection, *ages_days: int) -> None:
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    if device is None:
        device = (pair(db)[0],)
    now = datetime.now(UTC)
    for age in ages_days:
        db.execute(
            "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) VALUES (%s, 45, 9, 5, %s)",
            (device[0], now - timedelta(days=age)),
        )


def _job(
    db: psycopg.Connection,
    family: str,
    before: date,
    created_at: datetime | None = None,
    state: str = "queued",
) -> uuid.UUID:
    erase_id = uuid.uuid4()
    db.execute(
        "INSERT INTO erase_jobs (id, family, before, tz, state, created_at) VALUES (%s, %s, %s, 'Europe/Rome', %s, %s)",
        (erase_id, family, before, state, created_at or datetime.now(UTC)),
    )
    return erase_id


def _state(db: psycopg.Connection, erase_id: uuid.UUID) -> dict[str, Any]:
    row = db.execute(
        "SELECT state, count, tables, children, failure FROM erase_jobs WHERE id = %s", (erase_id,)
    ).fetchone()
    assert row is not None
    return {"state": row[0], "count": row[1], "tables": row[2], "children": row[3], "failure": row[4]}


def _older_than(db: psycopg.Connection, days: int) -> int:
    row = db.execute(
        "SELECT count(*) FROM device_locations WHERE measured_at < now() - %s * interval '1 day'", (days,)
    ).fetchone()
    return int(row[0]) if row else 0


def _clock() -> datetime:
    return datetime.now(UTC)


# --- item 1: two jobs at once queue on one lock; a table is finished only when nothing still qualifies -------


def test_two_overlapping_jobs_both_finish_and_nothing_older_remains(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _points(db, 900, 800, 700, 600, 500, 400, 120, 110, 100, 5)
    monkeypatch.setattr(retention_rules, "BATCH", 2)
    today = datetime.now(UTC).date()
    a = _job(db, "places", today - timedelta(days=300))  # takes the six oldest
    b = _job(db, "places", today - timedelta(days=50))  # the same rows and three more
    real = erase._progress
    holding, release = asyncio.Event(), asyncio.Event()

    def held(conn: Any, erase_id: uuid.UUID) -> Any:
        write = real(conn, erase_id)

        async def go(tables: dict[str, int], children: dict[str, int]) -> None:
            await write(tables, children)
            if erase_id == a and not holding.is_set():  # A's first batch: deleted, not committed yet
                holding.set()
                await release.wait()

        return go

    monkeypatch.setattr(erase, "_progress", held)

    async def both() -> str:
        first = asyncio.create_task(erase.run(app_settings.database_url, a, _clock))
        await holding.wait()
        second = asyncio.create_task(erase.run(app_settings.database_url, b, _clock))
        await asyncio.sleep(0.5)  # B had all the time to run over A's locked rows
        b_meanwhile = _state(db, b)["state"]
        release.set()
        await asyncio.gather(first, second)
        return b_meanwhile

    assert asyncio.run(both()) == "queued"  # one job at a time: B waited for A's lock
    assert _state(db, a)["state"] == "done" and _state(db, b)["state"] == "done"
    assert _older_than(db, 50) == 0 and _older_than(db, 1) == 1
    assert _state(db, a)["tables"]["device_locations"] + _state(db, b)["tables"]["device_locations"] == 9


def test_a_table_is_finished_only_when_nothing_still_qualifies(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A sweep that met rows another writer held (and gave back) sweeps again until the count is 0."""
    _points(db, 900, 800, 700, 600)
    monkeypatch.setattr(retention_rules, "BATCH", 2)
    real_batch, served = erase._batch, {"n": 0}

    def first_sweep_misses(part: Any, columns: list[str], after: bool) -> Any:
        served["n"] += 1
        if part.table == "device_locations" and served["n"] == 2:  # the first sweep's second batch: nothing
            return sql.SQL(
                "SELECT NULL, NULL WHERE false AND %s IS NOT NULL AND %s IS NOT NULL AND %s IS NOT NULL"
            )
        return real_batch(part, columns, after)

    monkeypatch.setattr(erase, "_batch", first_sweep_misses)

    async def go() -> Any:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return await erase.delete(conn, "places", datetime.now(UTC).date(), "Europe/Rome")

    tables, _ = asyncio.run(go())
    assert tables["device_locations"] == 4 and _older_than(db, 1) == 0


# --- item 2: the POST answers at once; the same family and day is the same job; the list finds it -----------


def test_the_post_answers_at_once_the_same_day_is_the_same_job_and_the_list_finds_it(
    client, db, auth, clock
) -> None:  # type: ignore[no-untyped-def]
    _points(db, 400, 401, 2)
    private = register(client, auth)
    before = (clock.now.date() - timedelta(days=30)).isoformat()  # the server's day, never the machine's
    with psycopg.connect(str(client.app.state.runtime.pool.conninfo), autocommit=True) as other:
        other.execute(erase.LOCK)  # another job runs: this one queues
        first = client.post(
            "/v1/data/erase",
            headers=device_headers(client, auth, private, "erase", f"places:{before}"),
            json={"family": "places", "before": before},
        )
        assert first.status_code == 202, first.text
        assert first.json()["state"] == "queued" and first.json()["count"] is None  # counted inside the job
        again = client.post(  # the phone lost the 202 and signs again
            "/v1/data/erase",
            headers=device_headers(client, auth, private, "erase", f"places:{before}"),
            json={"family": "places", "before": before},
        )
        assert again.status_code == 202 and again.json()["erase_id"] == first.json()["erase_id"]
        listed = client.get("/v1/data/erase", headers=auth)
        assert listed.status_code == 200, listed.text
        jobs = listed.json()["jobs"]
        assert [j["erase_id"] for j in jobs] == [first.json()["erase_id"]] and jobs[0]["state"] == "queued"
        assert set(jobs[0]) == {
            "erase_id", "family", "before", "state", "count", "erased", "tables", "children", "failure",
            "created_at", "finished_at",
        }  # fmt: skip
        other.execute("SELECT pg_advisory_unlock_all()")
    done = finished(client, auth, first.json()["erase_id"])
    assert done["state"] == "done" and done["count"] == 2 and done["erased"] == 2
    assert db.execute("SELECT count(*) FROM erase_jobs").fetchone() == (1,)
    third = client.post(  # done: a new deletion of the same day is a new job
        "/v1/data/erase",
        headers=device_headers(client, auth, private, "erase", f"places:{before}"),
        json={"family": "places", "before": before},
    )
    assert third.json()["erase_id"] != first.json()["erase_id"]
    finished(client, auth, third.json()["erase_id"])


# --- item 3: a resumed job adds to what it took; its guards are those of the moment they asked -----------------


def test_a_killed_job_resumed_counts_every_row_that_was_there(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _points(db, 900, 800, 700, 600, 500, 400, 300)
    monkeypatch.setattr(retention_rules, "BATCH", 2)
    erase_id = _job(db, "places", datetime.now(UTC).date() - timedelta(days=30))
    real, calls = erase._progress, {"n": 0}

    def dies_at_the_third(conn: Any, job: uuid.UUID) -> Any:
        write = real(conn, job)

        async def go(tables: dict[str, int], children: dict[str, int]) -> None:
            calls["n"] += 1
            if calls["n"] == 3:  # the process dies with the third batch deleted, not yet committed
                raise asyncio.CancelledError
            await write(tables, children)

        return go

    monkeypatch.setattr(erase, "_progress", dies_at_the_third)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(erase.run(app_settings.database_url, erase_id, _clock))
    killed = _state(db, erase_id)
    assert killed["state"] == "running" and killed["tables"]["device_locations"] == 4 and killed["count"] == 7
    monkeypatch.setattr(erase, "_progress", real)
    assert asyncio.run(erase.run_pending(app_settings.database_url)) == 1
    resumed = _state(db, erase_id)
    assert resumed["state"] == "done" and resumed["count"] == 7
    assert resumed["tables"]["device_locations"] == 7 and _older_than(db, 30) == 0


def test_a_resumed_job_keeps_the_guards_of_the_moment_he_asked(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    asked = datetime.now(UTC) - timedelta(days=5)
    for at in (asked - timedelta(days=1), asked - timedelta(days=30)):
        seed(db, "notifications", at)  # the push caps count the one a day before they asked
    erase_id = _job(db, "records", asked.date(), asked, state="running")  # interrupted, resumed 5 days later
    assert asyncio.run(erase.run_pending(app_settings.database_url)) == 1
    assert _state(db, erase_id)["state"] == "done"
    left = db.execute("SELECT created_at FROM notifications").fetchall()
    assert left == [(asked - timedelta(days=1),)]


# --- item 4: never stuck in `running`; the maintenance resumes; the shutdown never waits -------------------


def test_whatever_raises_the_job_is_failed_with_the_class_only(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    erase_id = _job(db, "places", datetime.now(UTC).date())

    async def boom(*_: Any, **__: Any) -> Any:
        raise ValueError("Via Segreta 1, the pharmacy")

    monkeypatch.setattr(erase, "count", boom)  # it raises before any delete, outside its handler of old
    asyncio.run(erase.run(app_settings.database_url, erase_id, _clock))
    state = _state(db, erase_id)
    assert state["state"] == "failed" and state["failure"] == "ValueError"
    assert "Segreta" not in str(db.execute("SELECT row_to_json(e) FROM erase_jobs e").fetchone())


def test_the_15_minute_maintenance_resumes_an_interrupted_job(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    _points(db, 400, 2)
    erase_id = _job(db, "places", datetime.now(UTC).date() - timedelta(days=30), state="running")
    counts = asyncio.run(maintenance.prune_places(app_settings, datetime.now(UTC)))
    assert counts["erase_jobs"] == 1 and _state(db, erase_id)["state"] == "done" and _older_than(db, 30) == 0


def test_the_shutdown_cancels_the_job_and_leaves_it_for_the_resume(client, db, auth, clock) -> None:  # type: ignore[no-untyped-def]
    _points(db, 400)
    private = register(client, auth)
    before = (clock.now.date() - timedelta(days=30)).isoformat()  # the server's day, never the machine's
    runtime = client.app.state.runtime
    with psycopg.connect(str(runtime.pool.conninfo), autocommit=True) as other:
        other.execute(erase.LOCK)  # the job waits on the lock: it would never finish
        started = client.post(
            "/v1/data/erase",
            headers=device_headers(client, auth, private, "erase", f"places:{before}"),
            json={"family": "places", "before": before},
        )
        assert started.status_code == 202

        async def close() -> None:
            await asyncio.wait_for(runtime.erase.close(), 3)

        client.portal.call(close)  # returns at once, never waits on the job
        assert _state(db, uuid.UUID(started.json()["erase_id"]))["state"] == "queued"
    assert "self.erase.close()" in inspect.getsource(Runtime.close)
    assert "erase.settled" not in inspect.getsource(Runtime.close)


# --- item 5: keyset batches -----------------------------------------------------------------------------


def test_the_batches_are_keyset(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _points(db, 900, 800, 700, 600, 500)
    monkeypatch.setattr(retention_rules, "BATCH", 2)
    seen: list[tuple[str, tuple[Any, ...]]] = []
    real: Any = psycopg.AsyncConnection.execute

    async def spy(self: Any, query: Any, params: Any = None, **kw: Any) -> Any:
        text = query.as_string(None) if isinstance(query, sql.Composable) else str(query)
        cur = await real(self, query, params, **kw)
        if "FOR UPDATE OF m" in text and '"device_locations"' in text:
            seen.append((text, tuple(params or ())))
        return cur

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", spy)

    async def go() -> Any:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return await erase.delete(conn, "places", datetime.now(UTC).date(), "Europe/Rome")

    tables, _ = asyncio.run(go())
    assert tables["device_locations"] == 5
    assert len(seen) == 4  # 2 + 2 + 1, then the sweep's end
    assert ") > (" not in seen[0][0] and all(") > (" in q for q, _ in seen[1:])
    lasts = [p[1] for _, p in seen[1:]]  # the last key of the batch before
    assert lasts == sorted(lasts) and len(set(lasts)) == 3


# --- item 6: the idea asks keep a week; each guard is named after its read ----------------------------------


def test_the_idea_asks_keep_a_week_and_each_guard_is_named(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    for age in (5, 9):
        row = seed(db, "ritual_asks", now - timedelta(days=age))
        db.execute("UPDATE ritual_asks SET kind = 'idea' WHERE id = %s", (row["id"],))

    async def go() -> Any:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            return await erase.delete(conn, "records", now.date(), app_settings.timezone, now)

    asyncio.run(go())
    left = db.execute("SELECT created_at FROM ritual_asks").fetchall()
    assert left == [(now - timedelta(days=5),)]  # the week's count of idea asks stays right
    guards = {p.table: p.guard for p in erase_rules.FAMILIES["records"] if p.guard is not None}
    assert guards == {
        "notifications": erase_rules.PUSH_CAPS,
        "reminders_sent": erase_rules.REMINDER_KEYS,
        "done_by_presence": erase_rules.PRESENCE_TODAY,
        "attend_candidates": erase_rules.ATTEND_CAP,
        "ritual_asks": erase_rules.IDEA_WEEK,
        "pisa_cue_asks": erase_rules.CUES_TODAY,
        "walk_recaps": erase_rules.WALK_RECAP_DAY,
        "morning_thoughts": erase_rules.MORNING_THOUGHT_DAY,
        "errand_suggestions": erase_rules.ERRANDS_WEEK,
    }
    assert erase_rules.IDEA_WEEK >= timedelta(days=7) and erase_rules.ERRANDS_WEEK >= timedelta(days=7)
    assert not hasattr(erase_rules, "GUARD_KEPT")


# --- item 7: the first-person visit question, the verb next to the place word -----------------------------

_KIND_PLACE = {
    "pharmacy": ("Farmacia Centrale", "pharmacy"),
    "hospital": ("Ospedale San Carlo", "hospital"),
    "dentist": ("Studio Dentistico Blu", "dentist"),
    "doctor": ("Studio Medico Verdi", "doctor"),
}
# (their words, their language, the kind they ask about: None when it is no visit of theirs, the stay that is there)
VISITS: tuple[tuple[str, str, str | None, str], ...] = (
    ("mi sono fermato in farmacia ieri?", "it-IT", "pharmacy", "pharmacy"),
    ("a che ora sono arrivato in ospedale?", "it-IT", "hospital", "hospital"),
    ("a che ora sono uscito dal dentista?", "it-IT", "dentist", "dentist"),
    ("quanto sono rimasto dal medico?", "it-IT", "doctor", "doctor"),
    ("siamo stati in ospedale ieri?", "it-IT", "hospital", "hospital"),
    ("ho visto il dottore ieri?", "it-IT", "doctor", "doctor"),
    ("when did I leave the hospital?", "en-US", "hospital", "hospital"),
    ("did I make it to the pharmacy?", "en-US", "pharmacy", "pharmacy"),
    ("ik ben naar de huisarts gegaan?", "nl-NL", "doctor", "doctor"),
    ("mia madre è stata in ospedale mentre io ero a Roma?", "it-IT", None, "hospital"),
    ("dove ero quando Marco era in ospedale?", "it-IT", None, "hospital"),
    ("I was wondering if Marco went to the hospital yesterday?", "en-US", None, "hospital"),
    ("sono andato a pranzo col dottor Bianchi ieri?", "it-IT", None, "doctor"),
    ("sono andato a comprare le medicine?", "it-IT", None, "pharmacy"),
)


@pytest.mark.parametrize(("words", "lang", "kind", "there"), VISITS)
def test_his_own_visit_question_is_answered_and_any_other_falls_through(
    client, db, clock, words: str, lang: str, kind: str | None, there: str
) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.health import visit_kinds
    from tests.test_core_health import _place, _stay

    rome = erase_rules.ZoneInfo("Europe/Rome")
    place, category = _KIND_PLACE[there]
    device, _ = pair(db)
    pid = _place(db, place, category)
    clock.now = datetime(2026, 11, 12, 18, 0, tzinfo=rome).astimezone(UTC)
    _stay(
        db,
        device,
        pid,
        datetime(2026, 11, 11, 10, 0, tzinfo=rome),
        datetime(2026, 11, 11, 10, 40, tzinfo=rome),
    )
    got = client.portal.call(lambda: client.app.state.runtime.episodes.recall("x", words=words, lang=lang))
    say = str(got.get("say") or "")
    if kind is not None:
        assert visit_kinds(words) == {kind}
        assert got["answer"] == "one" and place in say, got
    else:
        assert visit_kinds(words) == set()
        assert place not in say, got
        assert not say.startswith(("Sì: ", "Yes: ", "Non ho traccia di una sosta", "I have no stay")), got


@pytest.mark.parametrize(
    ("words", "kind"),
    [
        ("sono stato dallo psicologo ieri?", "psychologist"),
        ("sono andata dalla psicologa?", "psychologist"),
        ("sono passato dal fisioterapista?", "physiotherapist"),
        ("sono andata dalla ginecologa ieri?", "specialist"),
        ("sono stato dal ginecologo?", "specialist"),
        ("sono stato dal cardiologo?", "specialist"),
        ("did I go to the dermatologist?", "specialist"),
    ],
)
def test_the_specialists_are_health_places(words: str, kind: str) -> None:
    from pisa.domain.health import KIND_SAID, health_place, visit_kinds

    assert visit_kinds(words) == {kind} and kind in KIND_SAID
    assert health_place(words, None)


# --- item 9: the reasons the phone shows are in their language -----------------------------------------------

ENGLISH = "The first listed service expires within 15 days and must be renewed by hand."


def dsn_of(db: psycopg.Connection) -> str:
    from tests.conftest import dsn

    return dsn(db)


# --- item 10: app_meta values are JSON; the replay's p95 reads only its own keys -----------------------------
