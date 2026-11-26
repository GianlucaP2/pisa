"""The contract (rule_id, nearby without a query, continuation),
pickups and expiring services, the email situation reading, and the end-of-turn hint.
Each with a test that fails without its fix."""

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest

from pisa.services.session_messages import Utterance
from tests.assistant_env import add_task, rome, set_settings
from tests.conftest import dsn, pair

# --- review fixes -----------------------------------------------------------------------------------------


def test_13b_wordfreq_is_warmed_at_startup_off_the_event_loop(
    app_settings, fake_models, clock, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.brain import understand
    from pisa.services.runtime import Runtime

    warmed: list[str] = []
    monkeypatch.setattr(understand, "warm", lambda: warmed.append(threading.current_thread().name))

    async def factory(settings):  # type: ignore[no-untyped-def]
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        return await Runtime.start(settings, model_factory=fake_models, http_transport=transport, clock=clock)

    with TestClient(create_app(app_settings, runtime_factory=factory)) as client:
        client.get("/v1/health")
        for _ in range(50):
            if warmed:
                break
            threading.Event().wait(0.05)
    assert warmed and warmed[0] != threading.main_thread().name  # in a worker thread, not on the loop


def test_13b_a_correspondent_is_one_lookup_kept_as_mail_syncs(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.mail import Address, MailMessage
    from pisa.services.google_sync import _upsert_messages

    sent = MailMessage(
        "s1", "t1", Address("Owner", "owner@example.com"),
        (Address("Studio Notarile", "Studio@Notaio.example"),),
        "Atto", datetime(2026, 10, 20, 9, tzinfo=UTC), ("SENT",), "", "",
    )  # fmt: skip

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await _upsert_messages(conn, [sent])
            # The sent email itself isn't read again: the lookup is the correspondents set.
            await conn.execute("DELETE FROM email_messages")

    asyncio.run(go())
    assert db.execute("SELECT address FROM email_correspondents").fetchall() == [("studio@notaio.example",)]


def test_13b_a_shop_stay_nearer_the_cafe_next_door_still_closes_the_shop_task(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_done import done_notes, tick
    from tests.test_follow import fixes, track
    from tests.test_planner import place, with_place

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    owner = pair(db)[0]
    shop = (52.363, 4.958)
    cafe, stay = (shop[0] + 0.00027, shop[1]), (shop[0] + 0.00018, shop[1])  # café 30 m north, stay 20 m
    ferramenta = place(db, "Ferramenta", shop)
    place(db, "Caffè", cafe)
    task = add_task(db, "Comprare le viti")
    with_place(db, "tasks", task, ferramenta)
    track(db, owner, fixes(stay, rome(2026, 10, 21, 10), rome(2026, 10, 21, 10, 20)))
    linked = db.execute("SELECT p.name FROM stays s JOIN places p ON p.id = s.place_id").fetchall()
    assert linked == [("Caffè",)]  # 10 m from the café, 20 m from the shop
    assert tick(db, secrets_key, rome(2026, 10, 21, 10, 20)) == 1
    assert [t for t, _ in done_notes(db)] == ["Fatto: Comprare le viti ✓"]


def test_13b_a_failed_lexicon_rebuild_is_logged_not_left_unretrieved() -> None:
    from structlog.testing import capture_logs

    from pisa.services.conversation import ConversationService

    convo = ConversationService.__new__(ConversationService)

    async def boom() -> None:
        raise RuntimeError("database gone")

    async def go() -> None:
        task = asyncio.ensure_future(boom())
        convo._rebuilds = {task}
        task.add_done_callback(convo._rebuild_done)
        await asyncio.sleep(0.01)

    with capture_logs() as logs:
        asyncio.run(go())
    assert [e["cause"] for e in logs if e["event"] == "lexicon_rebuild_failed"] == ["RuntimeError"]
    assert convo._rebuilds == set()


# --- the contract: rule_id, nearby without a query, continuation --------------------------------------------


def test_contract_a_never_rule_has_an_id_and_delete_undoes_it(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.mail_world import add_email

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    add_email(db, "b1", "Rinnovo VPS", "billing@ovh.example")
    task = add_task(db, "Rinnova il VPS")
    db.execute("UPDATE tasks SET source_kind = 'email', source_ref = 'b1' WHERE id = %s", (task,))
    out = client.post(f"/v1/tasks/{task}/dismiss", headers=auth, json={"never": "sender"})
    assert out.status_code == 200
    rule_id = out.json()["rule_id"]
    assert rule_id and db.execute("SELECT id::text FROM profile_claims").fetchone() == (rule_id,)
    assert client.delete(f"/v1/rules/{rule_id}", headers=auth).status_code == 204
    assert db.execute("SELECT count(*) FROM profile_claims").fetchone() == (0,)
    # A rule already undone is 204 again, not "no such rule".
    assert client.delete(f"/v1/rules/{rule_id}", headers=auth).status_code == 204
    assert client.delete("/v1/rules/not-an-id", headers=auth).status_code == 404
    # A dismiss without never has no rule.
    plain = client.post(f"/v1/tasks/{add_task(db, 'Altro')}/dismiss", headers=auth, json={"never": None})
    assert plain.json()["rule_id"] is None


def test_contract_nearby_without_a_query_is_the_nearest_known_places(client, db, http_handler) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import place

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    calls: list[str] = []
    http_handler["handler"] = lambda request: calls.append(str(request.url)) or httpx.Response(500)
    here = (45.516, 9.0300)
    for n in range(25):  # 25 known places, ~11 m apart northwards
        place(db, f"Posto {n:02d}", (here[0] + 0.0001 * n, here[1]))
    place(db, "Lontano", (45.443, 9.189))  # ~15 km: outside the search
    found = client.get("/v1/places/search", headers=auth, params={"near": "45.516,9.0300"})
    assert found.status_code == 200, found.text
    results = found.json()["results"]
    assert [r["name"] for r in results] == [f"Posto {n:02d}" for n in range(20)]
    assert [r["distance_m"] for r in results] == sorted(r["distance_m"] for r in results)
    assert calls == []  # no provider call
    five = client.get("/v1/places/search", headers=auth, params={"near": "45.516,9.0300", "limit": 5})
    assert len(five.json()["results"]) == 5
    assert client.get("/v1/places/search", headers=auth).status_code == 400  # neither q nor near
    short = client.get("/v1/places/search", headers=auth, params={"q": "a", "near": "45.516,9.0300"})
    assert short.status_code == 400  # q keeps its 2-200 rule


def test_contract_a_continuation_is_answered_as_one_turn(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import UserPromptPart

    from tests.conftest import set_key
    from tests.live_app import live

    device_id, token = pair(db)
    set_key(db, secrets_key)
    heard: list[str] = []

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        prompts = [str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        heard.append(prompts[-1].split("\n\n[Pisa's understanding")[0])
        if len(heard) == 1:
            yield "Va bene. "
            await asyncio.sleep(30)
            yield "mai."
        else:
            yield "Farmacia e poi tabaccaio."

    fake_models.default = reply
    with live(client, token, device_id) as app:
        first = Utterance(text="Prima andiamo in farmacia", lang="it-IT", final=True, source="phone_mic")
        app.utterance(first)
        assert app.next()["reply_to"] == first.id
        more = Utterance(
            text="e poi passiamo dal tabaccaio",
            lang="it-IT",
            final=True,
            source="phone_mic",
            continues=first.id,
        )
        app.utterance(more)
        rest = list(app.reply())
    assert "".join(c["delta"] for c in rest) == "Farmacia e poi tabaccaio."
    assert heard[-1] == "Prima andiamo in farmacia e poi passiamo dal tabaccaio"
    history = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    assert [r for r, _ in history] == ["user", "assistant"]  # the cut-off turn left the history
    assert history[0][1].startswith("Prima andiamo in farmacia e poi passiamo dal tabaccaio")
    # The app sends the whole turn in `text`: the earlier words are not added twice.
    heard.clear()
    with live(client, token, device_id) as app:
        first = Utterance(text="Compra il latte", lang="it-IT", final=True, source="phone_mic")
        app.utterance(first)
        assert app.next()["reply_to"] == first.id
        whole = Utterance(
            text="compra il latte, e il pane",
            lang="it-IT",
            final=True,
            source="phone_mic",
            continues=first.id,
        )
        app.utterance(whole)
        list(app.reply())
    assert heard[-1] == "compra il latte, e il pane"


# --- pickups and services to renew by hand are mid, not low (the sweep's dry run) ---------------


# --- the email situation reading -----------------------------------------------------------

CLIENT = ("Marta Bianchi", "marta@cliente.example")


def mail(db: psycopg.Connection, mid: str, thread: str, sender: tuple[str, str], subject: str, body: str,
         hours_ago: int, labels: str = "{INBOX}") -> None:  # fmt: skip
    from tests.mail_world import OLD_NOW as NOW

    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, body) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            mid,
            thread,
            sender[0],
            sender[1],
            subject,
            NOW - timedelta(hours=hours_ago),
            labels,
            body[:80],
            body,
        ),
    )


def read(tone: tuple[str, str | None], history: tuple[str, str | None], stakes: tuple[str, str | None],
         implications: tuple[str, str | None]) -> dict[str, Any]:  # fmt: skip
    return {name: {"value": v, "quote": q}
            for name, (v, q) in zip(("tone", "history", "stakes", "implications"),
                                    (tone, history, stakes, implications), strict=True)}  # fmt: skip


def triaged(db: psycopg.Connection, secrets_key: str, model: Any) -> dict[str, tuple[Any, ...]]:
    from tests.mail_world import service_call

    service_call(db, secrets_key, model, lambda i, t: i.run())
    return {
        mid: (priority, reading)
        for mid, priority, reading in db.execute(
            "SELECT id, intake_priority, intake_reading FROM email_messages"
        )
    }


# --- the end-of-turn hint --------------------------------------------------------------------


def test_turn_rules_are_the_shared_table_the_app_uses() -> None:
    import json
    from pathlib import Path

    from pisa.domain import turn

    table = json.loads((Path(__file__).resolve().parents[2] / "protocol" / "turn-rules.v1.json").read_text())
    assert table["version"] == 2  # the table these rules mirror
    assert table["waits_ms"] == turn.WAITS_MS and table["p_end"] == turn.P_END
    for lang, rules in table["languages"].items():
        mine = turn.RULES[lang]
        assert set(rules["unfinished_last"]) == mine.unfinished_last
        assert set(rules["question_first"]) == mine.question_first
        assert set(rules["command_first"]) == mine.command_first
        assert set(rules["openers"]) == mine.openers  # The whole algorithm
    assert tuple(table["unfinished_endings"]) == turn.UNFINISHED_ENDINGS
    assert (table["question_min_words"], table["command_min_words"]) == (
        turn.QUESTION_MIN_WORDS,
        turn.COMMAND_MIN_WORDS,
    )


def test_turn_rules_give_d053_waits() -> None:
    from pisa.services.turn_hint import rules_hint

    def wait(text: str, lang: str = "it-IT") -> int:
        return rules_hint(text, lang).wait_ms

    # A trailing conjunction, preposition, article or filler: 3 s, whatever the first word.
    assert (
        wait("trovami un posto per")
        == wait("allora ho una fame e")
        == wait("I need to call the", "en-US")
        == 3000
    )
    assert wait("") == 3000  # nothing yet
    # A complete question or command: 1 s.
    assert (
        wait("che ore sono") == wait("trovami un posto per cena") == wait("book two tickets", "en-US") == 1200
    )
    # Otherwise 1.8 s; punctuation and case don't matter (the same words on the phone and here).
    assert wait("oggi in ufficio il capo") == wait("Oggi in ufficio, il capo!") == 1800
    # Each language its own table: "e" ends nothing in English, "the" nothing in Italian.
    assert wait("I saw the car e", "en-US") == 1800 and wait("ho visto the", "it-IT") == 1800
    assert rules_hint("che ore sono", "it-IT").p_end == 1.0 and rules_hint("ma", "it-IT").p_end == 0.0


def test_turn_hint_answers_a_partial_within_150_ms_never_replies_stores_or_logs_its_words(client, db) -> None:  # type: ignore[no-untyped-def]
    from structlog.testing import capture_logs

    from tests.live_app import live

    device_id, token = pair(db)
    words = "prenotami un tavolo da Gino per domani sera"
    turn_id = str(uuid.uuid4())
    with capture_logs() as logs, live(client, token, device_id, turn_hints=True) as app:
        app.partial(words, turn_id)
        hint = app.next()
        # Two more within half a second: the contract's 2 a second; they get no hint.
        app.partial(words + " verso", turn_id)
        app.partial(words + " verso le", turn_id)
        assert app.ping() == []  # nothing before the pong: no hint, no reply
        (wire,) = [
            f["event"]["value"]
            for f in app.frames
            if f.get("event", {}).get("name") == "pisa.voice.turn_hint"
        ]
        app.check()
    assert hint["type"] == "turn_hint" and hint["reply_to"] == turn_id and wire["replyTo"] == turn_id
    assert (hint["p_end"], hint["wait_ms"]) == (1.0, 1200)  # a complete command (1.2 s)
    # Never a reply, never stored, never logged (its length only).
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert words not in repr(logs) and "prenotami" not in repr(logs)
    # Within 150 ms by what was decided, not by the wall (a loaded machine's): the rules answered it, with
    # nothing awaited but the word table; a model gets at most its budget (the next test, on a virtual clock).
    assert any(
        e.get("event") == "turn_hint" and e.get("chars") == len(words) and e.get("source") == "rules"
        for e in logs
    )


def test_turn_hint_is_off_unless_the_hello_asks(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    import time

    from tests.conftest import set_key
    from tests.live_app import live

    device_id, token = pair(db)
    set_key(db, secrets_key)  # a reply, if a partial ever started one, would be answered and stored
    asked: list[str] = []

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        asked.append("reply")
        yield "Domani piove."

    fake_models.default = reply
    with live(client, token, device_id) as app:  # no pisa.voice.turn_hints in the hello
        app.partial("che tempo fa domani", str(uuid.uuid4()))
        answer = app.ping()
        time.sleep(0.3)
        (refused,) = [f for f in app.frames if f.get("control") == "error"]
    assert answer == []  # no hint, no reply
    assert refused["code"] == "invalid_frame" and "pisa.voice.turn_hints" in refused["message"]
    assert asked == [] and db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_turn_hint_a_model_can_take_the_rules_place_within_the_budget() -> None:
    from pisa.services.turn_hint import BUDGET_S, TurnHintService
    from tests.virtual_time import VirtualTimeLoop

    assert BUDGET_S < 0.15  # a model's share of the 150 ms answer

    async def quick() -> None:
        # The model's own answer within its budget (a budget no loaded machine runs out: only a hang's bound)
        service = TurnHintService(lambda text, lang: (0.93, 600), budget_s=60)
        hint = await service.hint("che ore sono", "it-IT")
        assert (hint.p_end, hint.wait_ms, hint.source) == (0.93, 600, "model")
        service.close()

    asyncio.run(quick())

    # A model that hasn't answered when the budget runs out: the rules, at the budget. On a virtual clock, so
    # the budget is spent exactly (the wall's on a loaded machine overran 150 ms now and then).
    answer = threading.Event()
    asked: list[str] = []

    def slow(text: str, lang: str) -> tuple[float, int]:
        asked.append(text)
        answer.wait(30)
        return 0.9, 600

    async def late() -> None:
        loop = asyncio.get_running_loop()
        service = TurnHintService(slow)
        started = loop.time()
        hint = await service.hint("trovami un posto per cena", "it-IT")
        assert loop.time() - started == pytest.approx(BUDGET_S)
        assert (hint.source, hint.wait_ms) == ("rules", 1200)
        # While the model is still busy with that one, the next partial doesn't queue behind it: the rules at
        # once, and the model is never asked
        started = loop.time()
        assert (await service.hint("allora ho una fame e", "it-IT")).source == "rules"
        assert loop.time() - started == 0
        answer.set()
        service.close()

    asyncio.run(late(), loop_factory=VirtualTimeLoop)
    assert asked == ["trovami un posto per cena"]

    def broken(text: str, lang: str) -> tuple[float, int]:
        raise RuntimeError("model gone")

    async def failing() -> None:
        service = TurnHintService(broken, budget_s=60)
        assert (await service.hint("allora ho una fame e", "it-IT")).wait_ms == 3000
        service.close()

    asyncio.run(failing())
