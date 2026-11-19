"""Understanding in a conversation turn: the lexicon from Pisa's own tables, the `understood` event before the
answer, the main model given the meant request and the raw one, the small model only for doubts, questions
instead of guesses, the user's own corrections kept on the server, and the iPhone's hints.
Synthetic names only."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.brain.understand import zipf
from pisa.services.lexicon import LexiconService
from tests.assistant_env import add_event, rome
from tests.conftest import Clock, dsn, pair, seed_catalog
from tests.owner import owner_at
from tests.test_learning import place
from tests.test_planner import with_place
from tests.test_research_eval import NOW, Turn, research


@pytest.fixture(autouse=True)
def model_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake small model answers at once, but a loaded CI runner once took 301 ms to schedule it (run 428):
    these tests are about what its answer does, so they give it time. The 300 ms budget itself is
    test_understand.py's slow-model test."""
    from pisa.brain import understand

    monkeypatch.setattr(understand, "UNDERSTAND_S", 5.0)


@pytest.fixture
def week(db: psycopg.Connection) -> dict[str, str]:
    """The user's week: home, a dinner today at a place, a task, a place visited, people who wrote."""
    pair(db)
    seed_catalog(db, [{"ref": "openai:research-model", "input": 1.0, "output": 2.0},
                      {"ref": "openai:confirm-model", "input": 0.1, "output": 0.2}])  # fmt: skip
    db.execute("UPDATE model_slots SET model_ref = 'openai:confirm-model' WHERE id = 'understand.confirm'")
    home = {"lat": 45.46, "lon": 9.19, "name": "Casa Brera", "source": "owner", "confirmed": True}
    db.execute("INSERT INTO app_settings (key, value) VALUES ('profile_home', %s)", (Jsonb(home),))
    gas = place(db, "Knoopfabriek", (45.47, 9.20), "Proefplein 1, 1014 DD Amsterdam, Netherlands")
    dinner = add_event(db, "Cena al Knoopfabriek", rome(2026, 10, 20, 20), minutes=120)
    with_place(db, "calendar_events", dinner, gas)
    knikker = place(db, "Café de Knikker", (45.48, 9.21), "Nepstraat 41, 1017 GB Amsterdam, Netherlands")
    device = db.execute("SELECT id FROM devices LIMIT 1").fetchone()[0]  # type: ignore[index]
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at, "
        "place_id) "
        "VALUES (%s, %s, %s, %s, 45.48, 9.21, 10, %s, %s)",
        (uuid.uuid4(), device, NOW - timedelta(days=3), NOW - timedelta(days=3, hours=-1), NOW, knikker),
    )
    for i, (name, address) in enumerate([
        ("Nino Ferri", "nino@example.com"), ("Jeroen", "jeroen@example.org"),
        ("Giulia Neri", "gn@example.com"),
        ("Giulia Conti", "gc@example.com"), ("Newsletter Bot", "noreply@example.com"),
    ]):  # fmt: skip
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at) "
            "VALUES (%s, %s, %s, %s, 's', %s)",
            (f"m{i}", f"t{i}", name, address, NOW - timedelta(days=i + 1)),
        )
    return {"knoopfabriek": gas, "knikker": knikker}


def turn(
    db: psycopg.Connection, secrets_key: str, words: str, *, confirm: str = "different", **kw: Any
) -> Turn:
    return research(db, secrets_key, words, [], "Ok.", lexicon=True, replies={"confirm-model": confirm}, **kw)


def user_prompt(t: Turn, db: psycopg.Connection) -> str:
    """What the model read for the user's turn: built from their stored words and
    Pisa's `meant` beside them (the stored row holds only their words)."""
    from pisa.domain.understand import as_read

    row = db.execute(
        "SELECT content, meant, corrections FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return as_read(row[0], row[1], row[2]) if row else ""


def test_the_lexicon_comes_from_js_tables_and_follows_them(db, week) -> None:  # type: ignore[no-untyped-def]
    async def go(action: Any) -> Any:
        from pisa.db.pool import open_pool

        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            return await action(LexiconService(pool, owner=owner_at(), clock=Clock(NOW)))
        finally:
            await pool.close()

    async def build(service: LexiconService) -> tuple[dict[str, Any], dict[str, Any]]:
        _, entries, _ = await service.current()
        first = {e.name: (e.kind, e.why) for e in entries}
        db.execute(
            "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at) "
            "VALUES ('m9', 't9', 'Sofie de Vries', 'sofie@example.org', 's', %s)",
            (NOW - timedelta(hours=1),),
        )
        _, again, _ = await service.current()  # a new sender: rebuilt on change
        return first, {e.name: (e.kind, e.why) for e in again}

    first, after = asyncio.run(go(build))
    assert first["Casa Brera"] == ("place", "home")
    assert first["Cena al Knoopfabriek"] == ("event", "today") and first["Knoopfabriek"] == ("place", "today")
    assert first["Café de Knikker"] == ("place", "visited")
    assert (
        first["Nino Ferri"] == ("person", "contact") and "Newsletter Bot" not in first
    )  # automated: not a name
    assert "Sofie de Vries" not in first and after["Sofie de Vries"] == ("person", "contact")
    hints = asyncio.run(go(lambda s: s.hints(zipf)))
    assert hints["hints"][:3] == ["Casa Brera", "Cena al Knoopfabriek", "Knoopfabriek"]
    assert "Knikker" in hints["hints"] and hints["updated_at"].startswith("2026-10-20")


def test_the_hints_endpoint(client, db) -> None:  # type: ignore[no-untyped-def]
    token = pair(db)[1]
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at) "
        "VALUES ('m1', 't1', 'Paola Marini', 'paola@example.com', 's', now())"
    )
    got = client.get("/v1/speech/hints", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 200, got.text
    assert (
        "Paola Marini" in got.json()["hints"] and len(got.json()["hints"]) <= 100 and got.json()["updated_at"]
    )


def test_a_misheard_name_is_fixed_before_the_answer(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]
    # A name from an email is someone else's writing: the small model confirms it, never the rules alone (the
    # fix at the merge), so Pisa says it back.
    assert turn(db, secrets_key, "Ehm, chiama nino ferry").understood[0]["corrections"] == []
    t = turn(db, secrets_key, "Ehm, chiama nino ferry", confirm="same")
    assert t.understood == [
        {**t.understood[0], "meant": "chiama Nino Ferri", "raw": "Ehm, chiama nino ferry", "ask": None,
         "corrections": [{"from": "nino ferry", "to": "Nino Ferri", "via": "model"}]}
    ]  # fmt: skip
    prompt = user_prompt(t, db)  # what the model got, as the history keeps it
    assert (
        prompt.startswith("chiama Nino Ferri\n\n[Pisa's understanding")
        and "«Ehm, chiama nino ferry»" in prompt
    )
    assert "by name" in prompt


def test_nothing_changed_nothing_sent(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]
    t = turn(db, secrets_key, "Che ore sono?")
    assert t.understood == [] and user_prompt(t, db) == "Che ore sono?"


def test_the_grey_zone_asks_the_small_model(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]
    same = turn(db, secrets_key, "chiama ierun", confirm="same")
    assert same.understood[0]["corrections"] == [{"from": "ierun", "to": "Jeroen", "via": "model"}]
    assert "Say Jeroen by name in your answer" in user_prompt(same, db)
    spent = db.execute("SELECT count(*) FROM llm_calls WHERE slot = 'understand.confirm'").fetchone()
    assert spent == (1,)  # metered, like every model call
    different = turn(db, secrets_key, "chiama ierun", confirm="different")
    assert different.understood == [] and user_prompt(different, db) == "chiama ierun"


def test_two_names_alike_get_a_question_not_a_guess(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]
    """The doubt reaches the model as a hint; the model asks it
    or settles it from the context. Pisa asks it by itself only when there is no model (next test)."""
    t = turn(db, secrets_key, "chiama giulia")
    assert (
        t.understood[0]["ask"] == "Intendi Giulia Conti o Giulia Neri?"
        and t.understood[0]["corrections"] == []
    )
    assert "Pisa's understanding step has a doubt" in t.instructions
    assert "Intendi Giulia Conti o Giulia Neri?" in t.instructions and t.reply == "Ok."  # the model answered


def test_without_a_model_the_doubt_is_asked_by_pisa_itself(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]
    db.execute("UPDATE model_slots SET model_ref = NULL, fallback_ref = NULL WHERE id = 'chat.conversation'")
    t = turn(db, secrets_key, "chiama giulia")
    assert t.reply == "Intendi Giulia Conti o Giulia Neri?"  # REFLEX: no model, Pisa's own one question


def test_the_owners_correction_is_kept_on_the_server(db, secrets_key, week) -> None:  # type: ignore[no-untyped-def]
    first = turn(db, secrets_key, "chiama nino ferry", confirm="same")
    again = turn(db, secrets_key, "No, ho detto Nino Ferry", device=first.device)
    rows = db.execute("SELECT heard, meant, lang, via FROM understand_corrections").fetchall()
    assert rows == [("nino ferry", "Nino Ferry", "it-IT", "model")]
    assert again.understood == []  # their words stand as said
