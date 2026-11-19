"""The user's `messages_retention` setting, its preview and its nightly
prune (cited messages stay, a day's grace after a change), and the closed list of routine tables.
The table-by-table rule list is `test_retention.py`."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.domain import retention as rules
from pisa.services import maintenance, retention
from pisa.services.maintenance import prune_history
from tests.conftest import pair
from tests.phone_signing import device_headers, register

NOW = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)  # 03:30 in Rome
TZ = "Europe/Rome"


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def _conversation(db: psycopg.Connection) -> str:
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    if device is None:
        pair(db)
        device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()
    assert device is not None
    row = db.execute(
        "INSERT INTO conversations (id, device_id) VALUES (gen_random_uuid(), %s) RETURNING id", (device[0],)
    ).fetchone()
    assert row is not None
    return str(row[0])


def _message(db: psycopg.Connection, conversation: str, days: float, now: datetime = NOW) -> int:
    row = db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) "
        "VALUES (%s, 'user', 'x', 'it-IT', %s) RETURNING id",
        (conversation, now - timedelta(days=days)),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _set(db: psycopg.Connection, value: str, changed: datetime | None) -> None:
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb({"messages_retention": value}),),
    )
    if changed is not None:
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (retention.CHANGED_KEY, Jsonb(changed.isoformat())),
        )


def _left(db: psycopg.Connection) -> set[int]:
    return {int(r[0]) for r in db.execute("SELECT id FROM messages").fetchall()}


# --- the rules (pure) --------------------------------------------------------------------------------------


def test_the_cutoff_waits_for_the_first_0330_a_day_after_a_change() -> None:
    assert rules.messages_cutoff("forever", None, NOW, TZ) is None
    assert rules.messages_cutoff("1y", None, NOW, TZ) == NOW - timedelta(days=365)
    # changed at 22:30 Rome: tonight's 03:30 is 5 h later, the next one 29 h later
    changed = NOW - timedelta(hours=5)
    assert rules.messages_cutoff("90d", changed, NOW, TZ) is None
    tomorrow = NOW + timedelta(days=1)
    assert rules.messages_cutoff("90d", changed, tomorrow, TZ) == tomorrow - timedelta(days=90)
    # changed at 03:31 Rome: a day later is 03:31, so tomorrow's 03:30 run is too early: two nights
    changed = NOW + timedelta(minutes=1)
    assert rules.messages_cutoff("90d", changed, NOW + timedelta(days=1, seconds=30), TZ) is None
    late = NOW + timedelta(days=1, minutes=5)  # tomorrow's run, started late: a day passed, not its night
    assert rules.messages_cutoff("90d", changed, late, TZ) is None
    assert rules.messages_cutoff("90d", changed, NOW + timedelta(days=2), TZ) is not None


def test_applies_after_is_the_first_0330_at_least_a_day_after_the_change() -> None:
    rome = ZoneInfo(TZ)
    for changed, expected in (
        (datetime(2026, 11, 19, 22, 30, tzinfo=rome), datetime(2026, 11, 21, 3, 30, tzinfo=rome)),
        (datetime(2026, 11, 20, 3, 29, tzinfo=rome), datetime(2026, 11, 21, 3, 30, tzinfo=rome)),
        (datetime(2026, 11, 20, 3, 30, tzinfo=rome), datetime(2026, 11, 21, 3, 30, tzinfo=rome)),
        (datetime(2026, 11, 20, 3, 31, tzinfo=rome), datetime(2026, 11, 22, 3, 30, tzinfo=rome)),
        (datetime(2027, 3, 27, 12, 0, tzinfo=rome), datetime(2027, 3, 29, 3, 30, tzinfo=rome)),  # DST night
    ):
        after = rules.first_prune_after(changed.astimezone(UTC), TZ)
        assert after == expected.astimezone(UTC), changed
        assert after - changed >= timedelta(days=1)
        # the copy is true: the prune deletes on that run, never on the one before
        assert rules.messages_cutoff("90d", changed, after, TZ) is not None
        assert rules.messages_cutoff("90d", changed, after - timedelta(days=1), TZ) is None
    assert rules.messages_applies_after("90d", "forever", None, NOW, TZ) == rules.first_prune_after(NOW, TZ)
    assert rules.messages_applies_after("forever", "forever", None, NOW, TZ) == NOW


# --- the setting, through the API --------------------------------------------------------------------------


def _db_now(db: psycopg.Connection) -> datetime:
    row = db.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0]


def _changed(db: psycopg.Connection) -> dict[str, Any]:
    row = db.execute("SELECT value FROM app_settings WHERE key = %s", (retention.CHANGED_KEY,)).fetchone()
    assert row is not None
    return row[0]


def test_the_setting_defaults_to_forever_and_takes_the_three_values(client, auth, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NOW - timedelta(days=30)  # the app's clock is not the change's: the database's now() is
    assert client.get("/v1/settings", headers=auth).json()["messages_retention"] == "forever"
    before = _db_now(db)
    body = client.patch("/v1/settings", headers=auth, json={"messages_retention": "90d"}).json()
    assert body["messages_retention"] == "90d"
    changed = _changed(db)
    device = db.execute("SELECT id FROM devices").fetchone()
    assert device is not None
    assert {k: changed[k] for k in ("value", "previous", "device_id")} == {
        "value": "90d", "previous": "forever", "device_id": str(device[0]),
    }  # fmt: skip
    assert before <= datetime.fromisoformat(changed["at"]) <= _db_now(db)  # the grace starts at now()
    bad = client.patch("/v1/settings", headers=auth, json={"messages_retention": "30d"})
    assert bad.status_code == 400
    assert bad.json()["error"]["message"] == "messages_retention must be forever, 1y or 90d."
    # the same value again is no change: the grace isn't restarted
    client.patch("/v1/settings", headers=auth, json={"messages_retention": "90d", "language": "en-US"})
    assert _changed(db) == changed


def test_the_preview_counts_what_would_go_and_says_when(client, auth, db) -> None:  # type: ignore[no-untyped-def]
    now = _db_now(db)
    conversation = _conversation(db)
    for days in (10, 100, 200, 400):
        _message(db, conversation, days, now)
    cited = _message(db, conversation, 500, now)  # a topic still queued keeps it
    db.execute(
        "INSERT INTO pisa_topics (id, message_id, name, opened_at, updated_at) VALUES (%s, %s, 'x', %s, %s)",
        (uuid.uuid4(), cited, now, now),
    )
    got = client.get("/v1/settings/messages_retention/preview", params={"value": "90d"}, headers=auth)
    assert got.status_code == 200
    body = got.json()
    assert {k: body[k] for k in ("value", "messages")} == {"value": "90d", "messages": 3}  # 100, 200, 400
    after = datetime.fromisoformat(body["applies_after"])
    rome = after.astimezone(ZoneInfo(TZ))
    assert (rome.hour, rome.minute) == (3, 30)  # the prune's run
    assert timedelta(days=1) <= after - now < timedelta(days=2)
    year = client.get("/v1/settings/messages_retention/preview", params={"value": "1y"}, headers=auth)
    assert year.json()["messages"] == 1
    forever = client.get("/v1/settings/messages_retention/preview", params={"value": "forever"}, headers=auth)
    assert forever.json()["messages"] == 0
    assert _left(db) and len(_left(db)) == 5  # a preview deletes nothing
    bad = client.get("/v1/settings/messages_retention/preview", params={"value": "30d"}, headers=auth)
    assert bad.status_code in (400, 422)


def test_changing_it_twice_restarts_the_wait_from_the_second_change(client, auth, db, app_settings) -> None:  # type: ignore[no-untyped-def]
    conversation = _conversation(db)
    real = _db_now(db)
    old = _message(db, conversation, 400, real)
    client.patch("/v1/settings", headers=auth, json={"messages_retention": "1y"})
    first = _changed(db)
    db.execute(  # the first change was made 30 h ago: its 03:30 comes before a day after the second
        "UPDATE app_settings SET value = jsonb_set(value, '{at}', to_jsonb(%s::text)) WHERE key = %s",
        ((real - timedelta(hours=30)).isoformat(), retention.CHANGED_KEY),
    )
    client.patch("/v1/settings", headers=auth, json={"messages_retention": "90d"})
    second = _changed(db)
    assert (first["value"], second["value"], second["previous"]) == ("1y", "90d", "1y")
    at = datetime.fromisoformat(second["at"])
    preview = client.get("/v1/settings/messages_retention/preview", params={"value": "90d"}, headers=auth)
    applies = datetime.fromisoformat(preview.json()["applies_after"])
    assert applies == rules.first_prune_after(at, TZ) and applies - at >= timedelta(days=1)
    # the run the first change would have opened deletes nothing; the one the preview names does
    first_run = rules.first_prune_after(real - timedelta(hours=30), TZ)
    assert first_run < applies
    assert asyncio.run(prune_history(app_settings, first_run))["messages"] == 0
    assert old in _left(db)
    assert asyncio.run(prune_history(app_settings, applies))["messages"] == 1
    assert old not in _left(db)


# --- A shorter value is signed by the phone (irreversible); a longer one isn't --------------------


def test_a_shorter_retention_needs_face_id_a_longer_one_doesn_t(client, db) -> None:  # type: ignore[no-untyped-def]
    _, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    private = register(client, auth)  # Pisa has a Face ID key now

    def refused(response: Any) -> bool:
        return response.status_code == 403 and response.json()["error"]["code"] == "biometric_required"

    assert refused(client.patch("/v1/settings", headers=auth, json={"messages_retention": "1y"}))
    assert client.get("/v1/settings", headers=auth).json()["messages_retention"] == "forever"
    wrong = device_headers(client, auth, private, "messages_retention", "90d")  # signed for another value
    assert refused(client.patch("/v1/settings", headers=wrong, json={"messages_retention": "1y"}))
    signed = device_headers(client, auth, private, "messages_retention", "1y")
    ok = client.patch("/v1/settings", headers=signed, json={"messages_retention": "1y"})
    assert ok.status_code == 200 and ok.json()["messages_retention"] == "1y"
    assert refused(client.patch("/v1/settings", headers=auth, json={"messages_retention": "90d"}))
    # longer, or forever: no signature
    assert (
        client.patch("/v1/settings", headers=auth, json={"messages_retention": "forever"}).status_code == 200
    )
    assert client.patch("/v1/settings", headers=auth, json={"language": "en-US"}).status_code == 200
    assert _changed(db)["previous"] == "1y"


# --- the nightly prune -------------------------------------------------------------------------------------


def test_forever_deletes_nothing(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    conversation = _conversation(db)
    _message(db, conversation, 2000)
    counts = asyncio.run(prune_history(app_settings, NOW))
    assert counts["messages"] == 0 and len(_left(db)) == 1


def test_nothing_goes_the_night_he_changes_it(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    conversation = _conversation(db)
    old = _message(db, conversation, 200)
    _set(db, "90d", NOW - timedelta(hours=5))  # changed at 22:30 last night
    assert asyncio.run(prune_history(app_settings, NOW))["messages"] == 0
    assert old in _left(db)
    tomorrow = NOW + timedelta(days=1)
    assert asyncio.run(prune_history(app_settings, tomorrow))["messages"] == 1
    assert old not in _left(db)


def test_old_messages_go_what_pisa_learned_stays(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    """A live citing row keeps its message; a closed or used one keeps its own text, the pointer
    goes (0090, ON DELETE SET NULL). The prune says how many the live citations kept."""
    conversation = _conversation(db)
    recent = _message(db, conversation, 30)
    old = _message(db, conversation, 120)
    msg = {k: _message(db, conversation, 120) for k in (
        "topic_queued", "topic_active", "topic_monthly", "topic_closed", "unknown_open", "unknown_answered",
        "problem_live", "problem_killed", "problem_none", "fragment",
    )}  # fmt: skip
    for key in ("topic_queued", "topic_active", "topic_monthly", "topic_closed"):
        db.execute(
            "INSERT INTO pisa_topics (id, message_id, name, state, opened_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uuid.uuid4(), msg[key], key, key.removeprefix("topic_"), NOW, NOW),
        )
    for key, state in (("unknown_open", "open"), ("unknown_answered", "answered")):
        db.execute(
            "INSERT INTO pisa_unknowns (id, source, ref, question, about, message_id, state, opened_at, "
            "updated_at) VALUES (%s, 'his_question', %s, %s, 'world', %s, %s, %s, %s)",
            (uuid.uuid4(), str(msg[key]), key, msg[key], state, NOW, NOW),
        )
    for key, idea in (("problem_live", "ready"), ("problem_killed", "killed"), ("problem_none", None)):
        problem = uuid.uuid4()
        db.execute(
            "INSERT INTO pisa_idea_problems (id, source, ref, message_id, statement, created_at, touched_at) "
            "VALUES (%s, 'his_words', %s, %s, %s, %s, %s)",
            (problem, str(msg[key]), msg[key], key, NOW, NOW),
        )
        if idea is not None:
            db.execute(
                "INSERT INTO pisa_ideas (id, night, problem_id, statement, mechanism, market, kind, cycle, "
                "state, created_at, updated_at) "
                "VALUES (%s, %s, %s, 'x', 'x', 'it', 'software', 1, %s, %s, %s)",
                (uuid.uuid4(), NOW.date(), problem, idea, NOW, NOW),
            )
    db.execute(
        "INSERT INTO pisa_idea_fragments (id, message_id, text, said_at) VALUES (%s, %s, 'fragment', %s)",
        (uuid.uuid4(), msg["fragment"], NOW),
    )
    claim = uuid.uuid4()  # a memory: their quoted words, no message id
    db.execute(
        "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, support, "
        "against, confidence, state, sensitive, hard, created_at, updated_at, last_evidence_at) VALUES "
        "(%s, 'like', 'x', '{}', 1, '{}', 'said', 1, 0, 0.9, 'active', false, false, %s, %s, %s)",
        (claim, NOW, NOW, NOW),
    )
    _set(db, "90d", NOW - timedelta(days=3))
    counts = asyncio.run(prune_history(app_settings, NOW))
    live = {"topic_queued", "topic_active", "topic_monthly", "unknown_open", "problem_live"}
    assert counts["messages_kept_cited"] == len(live)
    assert counts["messages"] == 1 + len(msg) - len(live)
    assert _left(db) == {recent} | {msg[k] for k in live}
    assert old not in _left(db)
    # every citing row is still there, with its own text; the gone ones lost only the pointer
    for table, column in (("pisa_topics", "name"), ("pisa_unknowns", "question"),
                          ("pisa_idea_problems", "statement"), ("pisa_idea_fragments", "text")):  # fmt: skip
        rows = dict(db.execute(f"SELECT {column}, message_id FROM {table}").fetchall())
        for text, message_id in rows.items():
            assert (message_id is not None) == (text in live), (table, text)
    assert db.execute("SELECT count(*) FROM pisa_ideas").fetchone() == (2,)
    assert db.execute("SELECT count(*) FROM profile_claims WHERE id = %s", (claim,)).fetchone() == (1,)


def _aged_out(db: psycopg.Connection) -> int:
    """A message 200 days old under 90d (changed 3 days ago): tonight's prune removes it."""
    said = _message(db, _conversation(db), 200)
    _set(db, "90d", NOW - timedelta(days=3))
    return said


def test_an_open_bet_never_vanishes_with_an_aged_out_message(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    said = _aged_out(db)
    bet = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_bets (id, message_id, claim, resolver, by_day, pisa_p, created_at) "
        "VALUES (%s, %s, 'domani piove', 'rain', %s, 0.4, %s)",
        (bet, said, NOW.date() + timedelta(days=30), NOW - timedelta(days=200)),
    )
    assert asyncio.run(prune_history(app_settings, NOW))["messages"] == 1
    assert said not in _left(db)
    row = db.execute("SELECT claim, state, message_id FROM pisa_bets WHERE id = %s", (bet,)).fetchone()
    assert row == ("domani piove", "open", None)  # their sentence stays; only the pointer went


def test_a_held_swarm_request_never_vanishes_with_an_aged_out_message(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    said = _aged_out(db)
    idea, ask = uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_ideas (id, night, statement, mechanism, market, kind, cycle, state, created_at, "
        "updated_at) VALUES (%s, %s, 'x', 'x', 'it', 'software', 1, 'kept', %s, %s)",
        (idea, NOW.date(), NOW, NOW),
    )
    db.execute(
        "INSERT INTO pisa_swarm_asks (id, idea_id, message_id, created_at) VALUES (%s, %s, %s, %s)",
        (ask, idea, said, NOW - timedelta(days=200)),
    )
    assert asyncio.run(prune_history(app_settings, NOW))["messages"] == 1
    assert said not in _left(db)
    row = db.execute("SELECT state, message_id FROM pisa_swarm_asks WHERE id = %s", (ask,)).fetchone()
    assert row == ("held", None)


def test_a_turn_he_went_on_with_takes_its_bet_and_swarm_request(client, db) -> None:  # type: ignore[no-untyped-def]
    """The `drop_after` removes the turn's messages; a bet or a swarm request linked
    during that turn goes with them (SET NULL would leave a stray open bet or a held request that can later
    start paid jobs). One linked before the mark stays."""
    device_id, _ = pair(db)
    conversation = str(
        db.execute(
            "INSERT INTO conversations (id, device_id) VALUES (gen_random_uuid(), %s) RETURNING id",
            (device_id,),
        ).fetchone()[0]  # type: ignore[index]
    )
    now = _db_now(db)
    earlier = _message(db, conversation, 0.01, now)
    turn = _message(db, conversation, 0, now)
    idea = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_ideas (id, night, statement, mechanism, market, kind, cycle, state, created_at, "
        "updated_at) VALUES (%s, %s, 'x', 'x', 'it', 'software', 1, 'kept', now(), now())",
        (idea, now.date()),
    )
    for message, claim in ((earlier, "earlier"), (turn, "this turn")):
        db.execute(
            "INSERT INTO pisa_bets (id, message_id, claim, resolver, by_day, pisa_p, created_at) "
            "VALUES (%s, %s, %s, 'rain', current_date, 0.5, now())",
            (uuid.uuid4(), message, claim),
        )
    db.execute(
        "INSERT INTO pisa_swarm_asks (id, idea_id, message_id, created_at) VALUES (%s, %s, %s, now())",
        (uuid.uuid4(), idea, turn),
    )
    conversations = client.app.state.runtime.conversations
    assert client.portal.call(conversations.drop_after, uuid.UUID(device_id), earlier) == 1
    assert _left(db) == {earlier}
    assert db.execute("SELECT claim FROM pisa_bets").fetchall() == [("earlier",)]
    assert db.execute("SELECT count(*) FROM pisa_swarm_asks").fetchone() == (0,)


def test_every_table_citing_a_message_is_known_to_the_prune(db) -> None:  # type: ignore[no-untyped-def]
    """A new foreign key to `messages` must be in `CITING`, with its delete rule as the database has it; the
    four the ruling names are SET NULL (0090), each live one written out in the prune's SQL."""
    rows = db.execute(
        "SELECT c.conrelid::regclass::text, a.attname, c.confdeltype FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.contype = 'f' AND c.confrelid = 'messages'::regclass"
    ).fetchall()
    rule = {"n": "set_null", "c": "cascade"}
    assert {(t, col): rule.get(kind, kind) for t, col, kind in rows} == retention.CITING
    assert set(retention.CITING.values()) == {"set_null"}  # 0090: none goes with their message any more
    for table, sql in retention.LIVE.items():
        assert f"FROM {table} c" in sql and sql in retention.GOING  # pyright: ignore[reportPrivateUsage]


def test_a_device_s_deletion_still_takes_what_came_from_its_messages(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    """The smoke device's purge (`devices.purge_device`) removes what Pisa kept from its words."""
    from pisa.infra.keys import KeyStore
    from pisa.services.devices import PURGE_NAME, PURGE_PLATFORM, purge_device

    device_id, _ = pair(db, PURGE_NAME, PURGE_PLATFORM)
    conversation = str(
        db.execute(
            "INSERT INTO conversations (id, device_id) VALUES (gen_random_uuid(), %s) RETURNING id",
            (device_id,),
        ).fetchone()[0]  # type: ignore[index]
    )
    said = _message(db, conversation, 1, _db_now(db))
    db.execute(
        "INSERT INTO pisa_topics (id, message_id, name, opened_at, updated_at) "
        "VALUES (%s, %s, 'x', now(), now())",
        (uuid.uuid4(), said),
    )
    db.execute(
        "INSERT INTO pisa_idea_fragments (id, message_id, text, said_at) VALUES (%s, %s, 'x', now())",
        (uuid.uuid4(), said),
    )
    db.execute(
        "INSERT INTO pisa_bets (id, message_id, claim, resolver, by_day, pisa_p, created_at) "
        "VALUES (%s, %s, 'x', 'rain', current_date, 0.5, now())",
        (uuid.uuid4(), said),
    )

    async def purge() -> None:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            await purge_device(conn, uuid.UUID(device_id), datetime.now(UTC), keys=KeyStore(None), relay=None)

    asyncio.run(purge())
    assert db.execute("SELECT count(*) FROM pisa_topics").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM pisa_idea_fragments").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM pisa_bets").fetchone() == (0,)
    assert _left(db) == set()


# --- The clocks disagree: no prune runs -------------------------------------------------------------


def test_every_prune_skips_when_the_app_clock_is_an_hour_off(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    conversation = _conversation(db)
    real = _db_now(db)
    old = _message(db, conversation, 400, real)
    _set(db, "90d", real - timedelta(days=3))
    _routine(db, timedelta(days=800))
    monkeypatch.setattr(maintenance, "_app_clock", lambda: datetime.now(UTC) + timedelta(hours=1, minutes=5))
    assert asyncio.run(prune_history(app_settings, real)) == {}
    # nothing pruned; their signed deletion still resumes, its own step (the core-keep merge)
    assert asyncio.run(maintenance.prune_places(app_settings, real)) == {"erase_jobs": 0}
    assert old in _left(db) and db.execute("SELECT count(*) FROM pisa_curious_sources").fetchone() == (1,)
    monkeypatch.setattr(maintenance, "_app_clock", lambda: datetime.now(UTC) + timedelta(minutes=50))
    assert asyncio.run(prune_history(app_settings, real))["messages"] == 1  # within the hour: it runs


# --- Batched deletes; the indexes the prune reads by ------------------------------------------------


def test_the_prune_deletes_in_batches(db, app_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    conversation = _conversation(db)
    for _ in range(5):
        _message(db, conversation, 200)
    for days in (800, 801, 802):
        _routine(db, timedelta(days=days))
    _set(db, "90d", NOW - timedelta(days=3))
    monkeypatch.setattr(rules, "BATCH", 2)
    statements: list[str] = []
    real = psycopg.AsyncConnection.execute

    async def spy(self: Any, query: Any, *args: Any, **kw: Any) -> Any:
        statements.append(str(query))
        return await real(self, query, *args, **kw)

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", spy)
    counts = asyncio.run(prune_history(app_settings, NOW))
    assert counts["messages"] == 5 and counts["pisa_curious_sources"] == 3 and _left(db) == set()
    assert sum(q.startswith("DELETE FROM messages") for q in statements) == 3  # 2 + 2 + 1
    assert sum(q.startswith("DELETE FROM pisa_curious_sources") for q in statements) == 2  # 2 + 1
    assert all("LIMIT %s" in q for q in statements if q.startswith("DELETE FROM messages"))


def test_the_prune_s_indexes_exist(db) -> None:  # type: ignore[no-untyped-def]
    rows = db.execute("SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'public'").fetchall()
    defs = {(t, d.split(" USING btree ")[-1]) for t, d in rows}
    for table, column in (
        ("messages", "created_at"),
        ("pisa_unknowns", "message_id"),
        ("pisa_idea_problems", "message_id"),
        ("pisa_bets", "message_id"),
        ("pisa_swarm_asks", "message_id"),
        ("pisa_explicit", "message_id"),
    ):
        assert (table, f"({column})") in defs, table


# --- the routine tables: The closed list goes by age; everything else is kept -------------------------


def _routine(db: psycopg.Connection, age: timedelta) -> None:
    """One row of each routine table, `age` old."""
    at = NOW - age
    day = at.date()
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, price_known, trace_id, "
        "created_at) "
        "VALUES (%s, 'x', 'x', 'settled', 0, true, 't', %s)",
        (uuid.uuid4(), at),
    )
    db.execute(
        "INSERT INTO pair_tokens (id, label, created_at, expires_at) VALUES (%s, 'x', %s, %s)",
        (uuid.uuid4(), at, at + timedelta(minutes=10)),
    )
    db.execute(
        "INSERT INTO task_bulk (id, action, items, origin, created_at) VALUES (%s, 'done', '[]', 'app', %s)",
        (uuid.uuid4(), at),
    )
    db.execute(
        "INSERT INTO errand_suggestions (day, combo_key, text, created_at) VALUES (%s, 'k', 'x', %s)",
        (day, at),
    )
    db.execute(
        "INSERT INTO walk_recaps (day, walk_start, walk_end, items, created_at) VALUES (%s, %s, %s, 1, %s)",
        (day, at, at, at),
    )
    db.execute(
        "INSERT INTO morning_thoughts (id, day, kind, ref, created_at) "
        "VALUES (%s, %s, 'welcome_back', %s, %s)",
        (uuid.uuid4(), day, uuid.uuid4(), at),
    )
    db.execute("INSERT INTO pisa_nights (night, started_at, status) VALUES (%s, %s, 'done')", (day, at))
    db.execute(
        "INSERT INTO pisa_meter (month, computed_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (day.replace(day=1), at),
    )
    db.execute(
        "INSERT INTO pisa_curious_sources (host, reliable, checked_on) VALUES (%s, true, %s)",
        (f"{uuid.uuid4().hex}.example", day),
    )
    db.execute(
        "INSERT INTO pisa_sent_mail (gmail_message_id, sent_at) VALUES (%s, %s)", (uuid.uuid4().hex, at)
    )


ROUTINE = ("pair_tokens", "pisa_curious_sources")  # Security material, a public cache
KEPT_NOW = (
    "llm_calls", "task_bulk", "errand_suggestions", "walk_recaps", "morning_thoughts", "pisa_nights",
    "pisa_meter", "pisa_sent_mail",
)  # fmt: skip  # were routine limits; kept until they delete
AGE: dict[str, timedelta] = {"pair_tokens": timedelta(days=7), "pisa_curious_sources": timedelta(days=90)}


@pytest.mark.parametrize("table", ROUTINE)
def test_each_routine_table_goes_after_its_limit(db, app_settings, table) -> None:  # type: ignore[no-untyped-def]
    limit = AGE[table]
    _routine(db, limit - timedelta(days=2))
    _routine(db, limit + timedelta(days=40))
    counts: dict[str, Any] = asyncio.run(prune_history(app_settings, NOW))
    assert counts[table] == 1, table
    assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,), table


def test_the_former_routine_tables_are_kept(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    _routine(db, timedelta(days=3650))
    counts: dict[str, Any] = asyncio.run(prune_history(app_settings, NOW))
    for table in KEPT_NOW:
        assert table not in counts, table
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,), table


def test_pair_tokens_open_or_recent_stay(db, app_settings) -> None:  # type: ignore[no-untyped-def]
    kept, gone = uuid.uuid4(), uuid.uuid4()
    db.execute(  # expired 3 days ago, never used: kept until a week past
        "INSERT INTO pair_tokens (id, label, created_at, expires_at) VALUES (%s, 'x', %s, %s)",
        (kept, NOW - timedelta(days=3), NOW - timedelta(days=3)),
    )
    db.execute(  # expired 9 days ago: goes
        "INSERT INTO pair_tokens (id, label, created_at, expires_at) VALUES (%s, 'x', %s, %s)",
        (gone, NOW - timedelta(days=9), NOW - timedelta(days=9)),
    )
    assert asyncio.run(prune_history(app_settings, NOW))["pair_tokens"] == 1
    assert db.execute("SELECT id FROM pair_tokens").fetchall() == [(kept,)]
