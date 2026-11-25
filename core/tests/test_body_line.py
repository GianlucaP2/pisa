"""Pisa's body line, the decision log, moves to the model, the conversation slot and its budget. Each test
fails without its fix."""

from typing import Any, cast

from tests.conftest import pair
from tests.live_app import device as device  # the fixture
from tests.test_acting import auth as auth  # the fixture
from tests.test_acting import google as google  # the fixture
from tests.test_acting import patches, row
from tests.test_acting_fixes import _choose
from tests.test_approvals import decide, dinner

# --- MED: a Pisa option waits for its one yes as a whole: plan and calendar together, one Undo
# -----------------


def _adjustments(db: Any) -> list[Any]:
    return db.execute("SELECT event_id, approval_id::text FROM plan_adjustments").fetchall()


def test_an_option_that_needs_a_yes_changes_nothing_and_a_no_leaves_everything_as_it_was(  # type: ignore[no-untyped-def]
    client, db, google, auth
) -> None:
    dinner(google, db)
    plan = _choose(client, db, "la seconda", outside="email")  # after outside text: one yes for all of it
    assert "Nothing changed yet" in plan["calendar"][0]
    assert not _adjustments(db) and not patches(google)  # Pisa's plan didn't move either
    approval_id, status, origin = row(db)[:3]
    assert (status, origin) == ("pending", "held")
    assert decide(client, auth, approval_id, "reject").status_code == 200
    assert not _adjustments(db) and not patches(google)  # "Ok, lascio com'è" is true


def test_the_yes_applies_plan_and_calendar_and_one_undo_puts_both_back(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]
    dinner(google, db)
    _choose(client, db, "la seconda", outside="email")
    approval_id = row(db)[0]
    done = decide(client, auth, approval_id).json()
    assert done["status"] == "done" and len(patches(google)) == 1
    assert _adjustments(db) == [("ev-dinner", approval_id)]  # the plan followed, linked to the same yes
    undone = client.post(f"/v1/approvals/{approval_id}/undo", headers=auth)
    assert undone.status_code == 200
    assert google.events["ev-dinner"]["start"]["dateTime"].startswith("2026-10-20T19:30")
    assert not _adjustments(db)  # one Undo: Pisa's plan went back too


# --- LOW: number words and 12-hour times ground; a bound is no time; "le due" is no option -----------------


def test_hours_in_words_and_12_hour_times_ground_a_bound_or_a_clock_doesnt() -> None:
    from datetime import date, datetime

    from pisa.domain import actions as rules

    # `owner_said` replaced verb_times; the same times, through the move's grounding
    today = date(2026, 10, 20)

    def moves(words: str, title: str, hour: int, minute: int = 0) -> bool:
        start = datetime(2026, 10, 20, hour, minute)
        mode = rules.decide_mode("calendar_move", words, read_outside=False, attendees=False, start=start,
                                 default_day=today, today=today, title=title, named=True)  # fmt: skip
        return mode == "words"

    assert moves("sposta la cena alle nove", "Cena", 9)
    assert moves("move dinner to 9pm", "Dinner", 21)
    assert moves("move it at 9:30 pm", "Dinner", 21, 30)
    assert moves("sposta il pranzo a mezzogiorno", "Pranzo", 12)
    assert not moves("sposta la cena a dopo le 21", "Cena", 21)  # a bound, not the time
    assert not moves("sposta la cena dopo le 21:00", "Cena", 21)
    assert not moves("move dinner before 9pm", "Dinner", 21) and not moves("entro le nove", "Cena", 9)
    assert not rules.says_option(2, "le due") and not rules.says_option(2, "alle 2")
    assert rules.says_option(2, "due") and rules.says_option(2, "la seconda")


# --- LOW: the eval's answer is capped as estimated; the fallback model is priced or refused -----------------


def test_the_eval_caps_each_answer_and_prices_the_fallback(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa_tools import talk_eval
    from tests.conftest import set_key
    from tests.test_acting_fixes import _price

    set_key(db, secrets_key)
    caps: list[Any] = []

    def model(messages, info):  # type: ignore[no-untyped-def]
        caps.append((info.model_settings or {}).get("max_tokens"))
        return "Ok."

    fake_models.default = model
    runtime = client.app.state.runtime
    client.portal.call(lambda: talk_eval.run(runtime, _price("0.001"), cases=talk_eval.CASES[:1]))
    assert caps == [talk_eval.ANSWER_TOKENS]
    db.execute(
        "INSERT INTO model_slots (id, model_ref, fallback_ref) VALUES ('chat.conversation', "
        "'openai:gpt-4o-mini', 'openai:no-such-model') "
        "ON CONFLICT (id) DO UPDATE SET fallback_ref = EXCLUDED.fallback_ref"
    )
    price = client.portal.call(talk_eval.gateway_price, runtime)
    assert client.portal.call(price, 1000, 200) is None  # the fallback has no known price: refused


# --- LOW: a breakpoint during the hold's UPDATE doesn't leave the item waiting ------------------------------


def test_an_exchange_that_ended_during_the_update_delivers_now(client, db) -> None:  # type: ignore[no-untyped-def]
    import uuid

    from pisa.services.notifications import Dispatcher
    from tests.conftest import pair

    device_id = pair(db)[0]
    answers = iter([True, False])  # busy when checked, idle once the UPDATE returned

    class Ended:
        def busy(self, device: object) -> bool:
            return next(answers, False)

        def wait(self, device: object, notification_id: str) -> None:
            pass

    runtime = client.app.state.runtime
    from tests.conftest import pause_outbox

    pause_outbox(client)  # this test's dispatcher is the only one delivering
    dispatcher = Dispatcher(
        runtime.pool,
        runtime.live,
        runtime.push,
        clock=runtime.clock,
        breakpoints=cast(Any, Ended()),
        owner=runtime.owner,
    )
    nid = str(uuid.uuid4())  # stored without the outbox's notify: only this dispatcher delivers it
    db.execute(
        "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, queued_at, "
        "urgency) VALUES (%s, 'info', 'Il pacco è arrivato.', 'it-IT', 'normal', %s, now(), now(), "
        "'breakpoint')",
        (nid, device_id),
    )
    assert client.portal.call(dispatcher.deliver, uuid.UUID(nid)) == "none"  # delivered now (no channel)
    assert db.execute("SELECT not_before FROM notifications").fetchone() == (None,)


# --- LOW: one API process (B1 and newest-wins live in its memory) ------------------------------------------


def test_a_second_api_process_refuses_to_start(client, app_settings) -> None:  # type: ignore[no-untyped-def]
    import pytest
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app

    with pytest.raises(RuntimeError, match="one API worker"), TestClient(create_app(app_settings)):
        pass


def test_the_app_gives_back_its_locks_before_closing_their_connections(  # type: ignore[no-untyped-def]
    app_settings, db, fake_models, clock, monkeypatch
) -> None:
    """The one-API-process lock and the erase jobs' lock went with their connections, and the server frees
    a closed connection's lock only once its process has exited, a moment later: the next app on the
    database (the next test's) found the first held now and then (2 in 100 under load) and refused to start,
    an error in that test's setup. Every connection the app closes has given back its advisory locks."""
    import psycopg
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime
    from tests.conftest import dsn

    held_at_close: list[int] = []
    connect = psycopg.AsyncConnection.connect

    async def watched(*args: Any, **kwargs: Any) -> Any:
        conn = await connect(*args, **kwargs)
        close = conn.close

        async def closing() -> None:
            if not conn.closed:
                with psycopg.connect(dsn(db), autocommit=True) as other:
                    row = other.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s",
                        (conn.info.backend_pid,),
                    ).fetchone()
                held_at_close.append(row[0] if row else 0)
            await close()

        conn.close = closing  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", watched)

    async def factory(settings):  # type: ignore[no-untyped-def]
        return await Runtime.start(settings, model_factory=fake_models, clock=clock)

    with TestClient(create_app(app_settings, runtime_factory=factory)):
        pass
    assert held_at_close and set(held_at_close) == {0}


# --- Choice 1: the event Pisa's tool touched in the previous turn counts as named -----------


def test_spostala_right_after_pisa_touched_the_dinner_acts_at_once(
    client, db, google, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    import json

    from pydantic_ai.messages import ToolReturnPart, UserPromptPart
    from pydantic_ai.models.function import DeltaToolCall

    from tests.test_acting import socket_turns

    dinner(google, db)
    starts = {"sposta la cena": "oggi alle 21", "spostala alle 22": "oggi alle 22"}

    async def model(messages, info):  # type: ignore[no-untyped-def]
        words = str([p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)][-1])
        if words in starts and not any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            args = {"event": "la cena", "start": starts[words]}
            yield {0: DeltaToolCall(name="move_event", json_args=json.dumps(args))}
            return
        yield "Ok."

    fake_models.default = model
    socket_turns(client, db, secrets_key, cast(Any, model), "sposta la cena", "spostala alle 22")
    assert row(db)[1:3] == ("done", "words")  # "spostala": the dinner Pisa just touched; no second question
    assert google.events["ev-dinner"]["start"]["dateTime"].startswith("2026-10-20T22:00")


# --- The audio chunk that ends a sentence says so ----------------------------------------


def test_the_chunk_that_ends_a_sentence_says_so(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.voice import Speaker
    from tests.test_voice import run_voice

    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        sent: list[tuple[bytes, bool, bool]] = []

        async def send(
            seq: int, data: bytes, final: bool, fmt: str, rate: int, spoken: int | None, end: bool
        ) -> bool:
            sent.append((data, final, end))
            return True

        speaker = Speaker(voice, send, "t", ("mp3",))
        for sentence in ("Uno. ", "Una pausa. ", "Tre. "):
            speaker.feed(sentence)
        speaker.finish()
        await speaker.done()
        return sent

    sent = run_voice(db, secrets_key, go)
    # Known at the chunk: the mark rides on it. Known only later ("pausa"): an empty chunk marks it, no wait.
    assert sent == [
        (b"Uno. ", False, True),
        (b"Una pausa. ", False, False),
        (b"", False, True),
        (b"Tre. ", False, True),
        (b"", True, False),
    ]


def test_say_audio_carries_sentence_end_on_the_wire() -> None:
    from pisa.services.session import say_audio_end
    from pisa.services.session_messages import SayAudio

    audio = {
        "stream_id": "00000000-0000-4000-8000-000000000600",
        "seq": 0,
        "format": "mp3",
        "sample_rate": 24000,
    }
    marked = SayAudio.model_validate(audio | {"data": "AA=="} | say_audio_end(False, None, True))
    assert marked.model_dump(mode="json")["sentence_end"] is True
    plain = SayAudio.model_validate(audio | {"data": "AA=="} | say_audio_end(False, None, False))
    assert "sentence_end" not in plain.model_dump(mode="json")  # absent: today's behaviour
    final = SayAudio.model_validate(audio | {"data": ""} | say_audio_end(True, None, True))
    assert "sentence_end" not in final.model_dump(mode="json")  # the final chunk needs no mark


def test_presence_state_is_kept_for_the_body_line_and_reset_on_close(client, device, db) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import set_settings as settings
    from tests.live_app import live
    from tests.test_gateway_presence import wait_for

    settings(db, {"briefing": {"triggers": {"glasses_on": False, "app_open": False}}})
    device_id, token = device
    state = "SELECT state FROM device_presence WHERE device_id = %s"
    with live(client, token, device_id) as app:
        body = {"foreground": False, "locked": True, "route": "earbuds", "companion": True}
        app.custom("pisa.presence", {"event": "app_background", "state": body})
        app.custom("pisa.presence", {"event": "glasses_off"})  # no state: the one Pisa has stays
        assert app.ping() == []
        app.check()
        stored = db.execute(state, (device_id,)).fetchone()[0]
        assert {k: v for k, v in stored.items() if k != "_at"} == {
            "foreground": False, "locked": True, "route": "earbuds", "companion": True,
        }  # fmt: skip
        assert set(stored["_at"]) == {"foreground", "locked", "route", "companion"}  # each with its time
    assert wait_for(lambda: db.execute(state, (device_id,)).fetchone()[0] is None)


# --- Observability: Pydantic AI's spans in Pisa's logs, without content
# ----------------------------------------


def test_a_turn_leaves_spans_in_the_logs_with_names_and_tokens_but_no_words(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    import json

    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall
    from structlog.testing import capture_logs

    from tests.test_acting import socket_turns

    async def model(messages, info):  # type: ignore[no-untyped-def]
        if not any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            yield {0: DeltaToolCall(name="get_current_time", json_args="{}")}
            return
        yield "Sono le nove, segreto."

    fake_models.default = model
    with capture_logs() as logs:
        socket_turns(client, db, secrets_key, cast(Any, model), "che ore sono segreto")
    spans = [e for e in logs if e["event"] == "span"]
    assert {"chat", "execute_tool", "invoke_agent", "pisa.call"} <= {e.get("op") or e["name"] for e in spans}
    assert [e["tool"] for e in spans if e.get("op") == "execute_tool"] == ["get_current_time"]
    assert any(isinstance(e.get("input_tokens"), int) for e in spans if e.get("op") == "chat")
    (root,) = [e for e in spans if e["name"] == "pisa.call" and e.get("slot") == "chat.conversation"]
    turn = [e for e in spans if e["otel_trace"] == root["otel_trace"]]
    assert len(turn) >= 4 and root["trace_id"]  # Pisa's trace id ties the turn's spans together
    assert "segreto" not in json.dumps(spans, default=str)  # RAIL: no content in logs


# --- The decision log, purpose and provenance by code ----------------------------------------


def _moves(db: Any) -> list[tuple[Any, ...]]:
    return db.execute("SELECT move, tool, provenance, purpose, ref FROM pisa_decisions ORDER BY n").fetchall()


def test_a_move_is_logged_with_its_purpose_and_shown_as_why_in_changes(  # type: ignore[no-untyped-def]
    client, db, google, auth, fake_models, secrets_key
) -> None:
    from tests.test_acting import Model, socket_turns

    dinner(google, db)
    purpose = "così arrivi dopo la palestra"
    model = Model("sposta la cena alle 21", [("move_event", {"event": "la cena", "start": "oggi alle 21",
                                                             "purpose": purpose})])  # fmt: skip
    fake_models.default = model
    socket_turns(client, db, secrets_key, cast(Any, model), "sposta la cena alle 21")
    approval_id = row(db)[0]
    assert _moves(db) == [("act", "move_event", "owner_words", purpose, approval_id)]
    changes = client.get("/v1/approvals?status=all", headers=auth).json()["approvals"]
    assert changes[0]["why"] == {"purpose": purpose, "provenance": "owner_words"}


def test_provenance_is_code_s_not_the_model_s(client, db, google, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The model's purpose claims the user said it; the words didn't name the event: Pisa's proposal."""
    from tests.test_acting import Model, socket_turns

    dinner(google, db)
    claim = "l'hai detto tu"
    model = Model("spostala alle 21", [("move_event", {"event": "la cena", "start": "oggi alle 21",
                                                       "purpose": claim})])  # fmt: skip
    fake_models.default = model
    socket_turns(client, db, secrets_key, cast(Any, model), "spostala alle 21")
    assert _moves(db) == [("propose", "move_event", "pisa_proposal", claim, row(db)[0])]


def test_a_move_after_outside_text_is_outside_text(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import decisions
    from tests.test_acting import act

    dinner(google, db)
    actions = client.app.state.runtime.actions

    async def turn() -> list[Any]:
        with decisions.collecting() as moves:
            await actions.move_event("la cena", "oggi alle 21", None)
        return moves

    (move,) = act(client, turn, said="sposta la cena alle 21", outside="email")
    assert (move.move, move.provenance) == ("propose", "outside_text")


def test_every_action_tool_takes_a_purpose_and_a_call_without_one_still_runs(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from structlog.testing import capture_logs

    from tests.test_acting import Model, socket_turns

    seen: dict[str, Any] = {}
    model = Model("aggiungi il latte", [("add_task", {"title": "Latte"})])

    async def spy(messages, info):  # type: ignore[no-untyped-def]
        seen.update({t.name: t.parameters_json_schema for t in info.function_tools})
        async for part in model(messages, info):
            yield part

    fake_models.default = spy
    with capture_logs() as logs:
        socket_turns(client, db, secrets_key, cast(Any, spy), "aggiungi il latte")
    for tool in ("create_event", "move_event", "cancel_event", "draft_reply", "send_draft", "add_task",
                 "complete_task", "reschedule_task", "apply_option", "plan_stop", "show_places",
                 "compose_message", "undo_last_change"):  # fmt: skip
        if tool in seen:
            assert "purpose" in seen[tool]["properties"], tool
            assert "purpose" not in seen[tool].get("required", []), tool
    assert {"add_task", "move_event"} <= set(seen)
    assert _moves(db) == [("act", "add_task", "owner_words", None, None)]
    (entry,) = [e for e in logs if e["event"] == "turn_decisions"]
    assert entry["purpose_missing"] == 1  # a defect for the eval, never a refusal


def test_a_call_that_changed_nothing_is_no_move(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import Model, socket_turns

    model = Model(
        "ho finito il pacco", [("complete_task", {"task_ref": "pacco", "purpose": "una cosa in meno"})]
    )
    fake_models.default = model
    socket_turns(client, db, secrets_key, cast(Any, model), "ho finito il pacco")  # no such task
    assert _moves(db) == []


def test_why_did_you_reads_back_pisa_s_own_moves(client, db, google, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import Model, socket_turns

    dinner(google, db)
    purpose = "così arrivi dopo la palestra"
    first = Model("sposta la cena alle 21", [("move_event", {"event": "la cena", "start": "oggi alle 21",
                                                             "purpose": purpose})])  # fmt: skip
    second = Model("perché l'hai spostata?", [("my_recent_decisions", {})])

    async def both(messages, info):  # type: ignore[no-untyped-def]
        from pydantic_ai.messages import UserPromptPart

        words = [str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart)][-1]
        async for part in (second if "perché" in words else first)(messages, info):
            yield part

    fake_models.default = both
    turns = socket_turns(client, db, secrets_key, cast(Any, both), "sposta la cena alle 21",
                         "perché l'hai spostata?")  # fmt: skip
    from tests.test_acting import said

    assert purpose in said(turns[1]) and "owner_words" in said(turns[1])


# --- Pisa's body line, open threads, self block -------------------------------------------------


def test_the_body_line_says_what_reaches_the_owner() -> None:
    from decimal import Decimal

    from pisa.domain.body import Presence, body_line

    front = Presence(
        "foreground", "off", None, None, {"foreground": True, "locked": False, "route": "speaker"}
    )
    assert body_line(front, Decimal("1.84")) == (
        "\nPisa's body now: voice=the phone's speaker · phone=in front (the user can see the screen) · "
        "glasses=off · budget left today: €1.84"
    )
    pocket = Presence("background", None, "connected", "on", {"locked": True, "companion": True})
    line = body_line(pocket, Decimal("-0.2"))
    assert "voice=earbuds (companion on)" in line and "phone=not in front (locked)" in line
    assert "they don't see the screen" in line and "budget left today: €0.00" in line
    car = Presence("foreground", None, None, None, {"foreground": True, "locked": True, "route": "car"})
    assert "voice=the car" in body_line(car, None) and "not in front (locked)" in body_line(car, None)
    assert body_line(None, None) == "\nPisa's body now: device state unknown"


def test_each_turn_tells_the_model_its_body_and_threads_but_no_titles(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key
) -> None:
    from pydantic_ai.messages import ModelRequest

    from tests.conftest import set_key
    from tests.live_app import live
    from tests.test_acting import Model

    dinner(google, db, attendees=[{"email": "me@x.example", "self": True}, {"email": "paola@y.example"}])
    seen: list[str] = []
    ask = Model("sposta la cena alle 21", [("move_event", {"event": "la cena", "start": "oggi alle 21"})])

    async def spy(messages, info):  # type: ignore[no-untyped-def]
        seen.append(str(next(m for m in reversed(messages) if isinstance(m, ModelRequest)).instructions))
        async for part in ask(messages, info):
            yield part

    fake_models.default = spy
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        # The user unlocked the phone in this session, then locked it to drive (Pisa stays theirs)
        unlocked = {"foreground": True, "locked": False, "route": "car"}
        app.custom("pisa.presence", {"event": "app_foreground", "state": unlocked})
        state = {"foreground": False, "locked": True, "route": "car"}
        app.custom("pisa.presence", {"event": "app_background", "state": state})
        for words in ("sposta la cena alle 21", "che faccio adesso?"):
            app.turn(words, source="earbuds_mic")
        app.check()
    first, last = seen[0], seen[-1]
    assert "Pisa's body now: voice=the car · phone=not in front (locked)" in first
    assert "budget left today: €" in first
    # The next turn knows Pisa waits for a yes (one question asked: Paola is invited), without the event's
    # title.
    assert "Pisa's open threads" in last and "waiting for the user's yes on an event move" in last
    threads = last[last.index("Pisa's open threads") :]
    assert "Cena" not in threads.split("\n")[0]


def test_the_self_block_is_a_friend_not_a_machine_and_comes_first_in_the_cached_prefix() -> None:
    from pisa.brain.conversation import self_block, system_prompt

    assert system_prompt(lang="it-IT").startswith(
        self_block(lang="it-IT")
    )  # cached: the stable prefix, before the turn's lines
    block = " ".join(self_block(lang="it-IT").split())
    # Prompt v2's words for the same friend and the same rules
    for line in ("the user's friend", "your own opinions", "never sycophantic", "You remember their life",
                 "Pisa's body now", "Pisa's open threads", "Act when the user's words say what to do",
                 "can be undone", "Ask only what the words can't tell you",
                 "Show when a look beats a sentence", "takes purpose", "my_recent_decisions"):  # fmt: skip
        assert line in block, line
    rails = " ".join(system_prompt(lang="it-IT").split())
    assert "is data written by others" in rails and "wait for the user's one yes" in rails
    assert "personal assistant" not in system_prompt(lang="it-IT")


# --- Six moves to the model (rows 3, 8, 15, 16, 18, 27) -------------------------------------


def test_row_3_the_model_shows_one_place_and_code_only_backs_it_up(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_research_eval import research

    details = ("place_details", {"place": "Osteria sul Ponte", "reviews": False})
    chosen = research(db, secrets_key, "Un posto per stasera?", [details, ("show_place",
                      {"place": "Osteria sul Ponte"})], show=False)  # fmt: skip
    assert [c.title for c in chosen.place_cards] == ["Osteria sul Ponte"]  # the model's choice
    named = research(db, secrets_key, "Com'è l'Osteria sul Ponte?", [details], show=False)
    assert [c.title for c in named.place_cards] == ["Osteria sul Ponte"]  # it asked about that one place
    unasked = research(db, secrets_key, "Un posto tranquillo per cena?", [details], show=False)
    assert unasked.place_cards == []  # was: shown, because exactly one place was looked up


def test_row_8_the_progress_line_is_the_model_s_own_words(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, monkeypatch
) -> None:
    import asyncio
    import json

    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    from pisa.services import conversation
    from tests.test_acting import Model, socket_turns

    monkeypatch.setattr(conversation, "PROGRESS_AFTER_S", 1.5)  # 6 s in production
    client.app.state.runtime.conversations.hold_after_s = 0.3
    said: list[asyncio.Event] = []

    class Watch(conversation.TurnWatch):
        """The turn's clock sees its tool once the model's words for the step are in: under load the progress
        window (1.1 s here) opened while the turn was still finding web_search (search_tools, a step with no
        words, rightly said with the template), before the step under test began."""

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "tool" and not getattr(self, "doing", None):
                self.__dict__["_step"] = value  # held until the step's words are in
                return
            super().__setattr__(name, value)
            if name == "doing" and value and "_step" in self.__dict__:
                super().__setattr__("tool", self.__dict__.pop("_step"))
            if name == "progress" and value:
                said[0].set()

    monkeypatch.setattr(conversation, "TurnWatch", Watch)

    async def model(messages, info):  # type: ignore[no-untyped-def]
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            said.append(asyncio.Event())
            args = {"query": "recensioni Bistrot", "saying": "Leggo le recensioni del Bistrot."}
            yield {0: DeltaToolCall(name="web_search", json_args=json.dumps(args))}
            return
        await asyncio.wait_for(said[0].wait(), 30)  # the work lasts until the step was said: no race
        yield "Dicono che è buono."

    fake_models.default = model
    (frames,) = socket_turns(client, db, secrets_key, Model("x", []), "com'è il Bistrot?")
    deltas = [f["delta"].strip() for f in frames if f["type"] == "say"]
    assert deltas.count("Leggo le recensioni del Bistrot.") == 1
    assert "Ci sto ancora lavorando." not in deltas  # the template only when the model gave no words


def test_row_15_the_understanding_doubt_is_a_hint_and_asked_only_without_a_model(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.conversation import doubt_instruction

    hint = doubt_instruction("Marco Rossi o Marco Bianchi?")
    assert "Ask it (just that one question) only if" in hint and "Marco Rossi o Marco Bianchi?" in hint
    assert doubt_instruction(None) == ""


def test_row_16_brain_1_a_done_write_without_finish_goes_back_to_the_model_with_finish_it_doesn_t(
    client, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    # The brain loop replaced row 16's template and `say_more`: the model reads every result, unless its own
    # finish(say=…) came in the same round as the writes and every result there is done.
    from tests.test_acting import Model, said, socket_turns

    plain = Model("segnati il latte", [("add_task", {"title": "Comprare il latte"})])
    fake_models.default = plain
    socket_turns(client, db, secrets_key, plain, "segnati il latte")
    assert plain.seen == 2  # it read the result, then spoke
    finish = ("finish", {"say": "Segnato il pane."})
    fast = Model("segnati il pane", [("add_task", {"title": "Comprare il pane"}), finish])
    fake_models.default = fast
    (frames,) = socket_turns(client, db, secrets_key, fast, "segnati il pane")
    assert fast.seen == 1 and said(frames) == "Segnato il pane."


def test_row_27_the_briefing_model_chooses_what_matters_and_keeps_the_floor() -> None:
    from datetime import UTC, datetime

    from pisa.brain.briefing import BriefingData, prompt

    data = BriefingData(lang="it-IT", max_words=60, sections=["agenda", "weather"],
                        now=datetime(2026, 10, 30, 7, tzinfo=UTC), timezone="Europe/Rome",
                        weather="Oggi: sereno, 12-18 °C.")  # fmt: skip
    rules, facts = prompt(data)
    rules = " ".join(rules.split())
    assert "Choose what matters today" in rules and "Skip a section that changes nothing today" in rules
    assert "always say when to leave; always say any closing warning" in rules  # the floor stays code
    assert "Weather where the user is: Oggi: sereno" in facts  # the facts still all reach the model


# --- Grok for the conversation slot, through xAI's OpenAI-compatible endpoint ------------------------


def _sse(*chunks: dict[str, Any]) -> bytes:
    import json

    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def test_grok_streams_calls_a_tool_and_reports_cached_tokens() -> None:
    """The xAI path (no network): what Pisa sends, the tool round trip, the streamed answer, cached tokens."""
    import asyncio
    import json

    import httpx
    from pydantic_ai import Agent

    from pisa.brain.models.providers import PROVIDERS_BY_ID, xai_model

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body | {"_url": str(request.url), "_auth": request.headers.get("authorization")})
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "grok-4-fast"}

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return base | {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        if len(sent) == 1:
            call = {"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "get_current_time", "arguments": "{}"}}  # fmt: skip
            chunks = [chunk({"role": "assistant", "tool_calls": [call]}), chunk({}, "tool_calls")]
        else:
            chunks = [chunk({"content": "Sono le "}), chunk({"content": "nove."}, "stop")]
        usage = {"prompt_tokens": 9000, "completion_tokens": 12, "total_tokens": 9012,
                 "prompt_tokens_details": {"cached_tokens": 8192}}  # fmt: skip
        chunks.append(base | {"choices": [], "usage": usage})
        return httpx.Response(200, content=_sse(*chunks), headers={"content-type": "text/event-stream"})

    async def go() -> tuple[list[str], Any]:
        from openai import AsyncOpenAI

        base_url = PROVIDERS_BY_ID["xai"].base_url
        fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AsyncOpenAI(base_url=base_url, api_key="xai-test-key", http_client=cast(Any, fake))
        model = xai_model("grok-4-fast", "xai-test-key", base_url, client)
        agent: Agent[None, str] = Agent(model)

        @agent.tool_plain
        def get_current_time() -> str:
            return "09:00"

        async with agent.run_stream("che ore sono?") as run:
            parts = [d async for d in run.stream_text(delta=True, debounce_by=None)]
            return parts, run.usage

    parts, usage = asyncio.run(go())
    assert "".join(parts) == "Sono le nove."
    assert sent[0]["_url"] == "https://api.x.ai/v1/chat/completions" and sent[0]["stream"] is True
    assert sent[0]["stream_options"] == {"include_usage": True}  # the usage chunk carries cached tokens
    assert sent[0]["tools"][0]["function"]["name"] == "get_current_time"
    assert any(m.get("role") == "tool" for m in sent[1]["messages"])  # the tool's result went back
    assert usage.cache_read_tokens >= 8192  # what turn_latency logs as cached_tokens


# --- The history per call under ~12k tokens ----------------------------------------------------------


def _long_turn() -> list[Any]:
    """Six earlier exchanges, then a research turn that read four long pages (as a real one does)."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from pisa.brain.untrusted import data_block

    messages: list[Any] = []
    for n in range(6):
        messages += [ModelRequest(parts=[UserPromptPart(content=f"domanda {n} " + "parole " * 40)]),
                     ModelResponse(parts=[TextPart(content=f"risposta {n} " + "frase " * 40)])]  # fmt: skip
    messages.append(ModelRequest(parts=[UserPromptPart(content="trovami un bar sul Naviglio")]))
    for n in range(4):
        page = data_block("page", f"pagina {n} " + "testo della guida " * 1400, what="a web page")
        messages += [
            ModelResponse(
                parts=[ToolCallPart("read_page", {"url": f"https://x.it/{n}"}, tool_call_id=f"c{n}")]
            ),
            ModelRequest(
                parts=[ToolReturnPart("read_page", f"{page.rule}\n{page.text}", tool_call_id=f"c{n}")]
            ),
        ]
    return messages


def test_the_history_of_a_long_research_turn_is_cut_under_12k_tokens_and_measured() -> None:
    import re

    from pydantic_ai.messages import ToolReturnPart

    from pisa.brain.history import BUDGET_TOKENS, tokens, trim

    messages = _long_turn()
    before, after = tokens(messages), tokens(trim(messages))
    assert before > 25_000 and after <= BUDGET_TOKENS, (before, after)  # 26.3k → 7.7k on this turn
    kept = trim(messages)
    returns = [p for m in kept for p in m.parts if isinstance(p, ToolReturnPart)]
    assert len(returns) == 4  # nothing dropped: every call keeps its result
    assert "trimmed: Pisa read the rest" not in str(returns[-1].content)  # the latest round stays whole
    first = str(returns[0].content)
    assert "trimmed: Pisa read the rest" in first
    opened = re.findall(r"<(page-[0-9a-f]{16})>", first)
    # RAIL: a cut block is closed again (the rule line names both markers once; the body needs its own close)
    assert opened and all(first.count(f"</{tag}>") == first.count(f"<{tag}>") for tag in opened)
    short = messages[:3]
    assert trim(short) == short  # under the budget: untouched


def test_older_exchanges_become_pisa_s_summary(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from pisa.brain.history import EARLIER
    from tests.test_acting import Model, socket_turns

    seen: list[list[str]] = []
    plain = Model("x", [])

    async def spy(messages, info):  # type: ignore[no-untyped-def]
        seen.append([str(p.content) for m in messages if isinstance(m, ModelRequest) for p in m.parts
                     if isinstance(p, UserPromptPart)])  # fmt: skip
        async for part in plain(messages, info):
            yield part

    fake_models.default = spy
    words = [f"domanda numero {n}" for n in range(5)]
    socket_turns(client, db, secrets_key, cast(Any, spy), *words)
    last = seen[-1]
    assert last[0].startswith(EARLIER) and "the user: domanda numero 0" in last[0]
    assert "domanda numero 0" not in last[1:]  # not twice: summarised, not verbatim
    assert last[1:] == ["domanda numero 1", "domanda numero 2", "domanda numero 3", "domanda numero 4"]


# --- The budget guard --------------------------------------------------------------------------------


def test_past_70_percent_of_the_day_the_cheaper_model_answers_said_once(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, clock
) -> None:
    from tests.conftest import seed_catalog
    from tests.test_acting import said, socket_turns

    if db.execute("SELECT count(*) FROM model_catalog WHERE ref = 'openai:cheap-model'").fetchone() == (0,):
        seed_catalog(db, [{"ref": "openai:cheap-model", "input": 0.1, "output": 0.2}])
    db.execute("UPDATE model_slots SET fallback_ref = 'openai:cheap-model' WHERE id = 'chat.conversation'")
    fake_models.default = "Risposta del modello solito."
    fake_models.replies["cheap-model"] = "Risposta del modello economico."
    (before,) = socket_turns(client, db, secrets_key, cast(Any, None), "come va?")
    assert said(before).strip() == "Risposta del modello solito."  # under 70%: the usual model
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, %s, true, "
        "'t', %s)",
        (0.71, clock.now),
    )  # €0.71 of the default €1.00 a day
    first, second = socket_turns(client, db, secrets_key, cast(Any, None), "e adesso?", "e poi?")
    assert said(first).strip() == "Risposta del modello economico. Per oggi uso il modello più economico."
    assert said(second).strip() == "Risposta del modello economico."  # said once a day, not every turn
    db.execute("UPDATE model_slots SET fallback_ref = NULL WHERE id = 'chat.conversation'")
    (none,) = socket_turns(client, db, secrets_key, cast(Any, None), "e senza?")
    assert said(none).strip() == "Risposta del modello solito."  # no cheaper model: the cap still holds


# --- Pisa's own proposals act with Undo, within the user's own things ----------------------------------


def test_pisa_s_own_option_on_the_owner_s_own_event_is_applied_with_undo_and_its_reason(  # type: ignore[no-untyped-def]
    client, db, google, auth
) -> None:
    from pisa.domain import decisions
    from tests.test_acting import act
    from tests.test_acting_fixes import _option

    dinner(google, db)
    options, option = client.app.state.runtime.plan_options, _option(client, db, None)

    async def apply() -> list[Any]:
        from pisa.services.conversation import current_turn

        turn = current_turn.get()
        assert turn is not None
        turn.named_options = frozenset({option})  # Pisa offered them in the previous turn
        with decisions.collecting() as moves:
            await options.apply(option, "o2", "voice")
        return moves

    moves = act(client, apply, said="va bene")  # Pisa's own proposal, the user's own event, nobody invited
    approval_id, status, origin = row(db)[:3]
    assert (status, origin) == ("done", "proposal") and len(patches(google)) == 1  # no question
    assert [(m.move, m.provenance) for m in moves] == [("act", "pisa_initiative")]  # "di sua iniziativa"
    undone = client.post(f"/v1/approvals/{approval_id}/undo", headers=auth)
    assert undone.status_code == 200  # always with Undo
    assert google.events["ev-dinner"]["start"]["dateTime"].startswith("2026-10-20T19:30")


def test_pisa_s_own_proposal_still_asks_when_other_people_would_hear_of_it(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from tests.actions_fake import OWNER
    from tests.test_acting import PAOLA

    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    _choose(client, db, "va bene")  # someone else would hear of it: one yes
    assert not patches(google) and row(db)[1] == "pending"


def test_pisa_s_own_proposal_after_outside_text_asks(client, db, google) -> None:  # type: ignore[no-untyped-def]
    dinner(google, db)
    _choose(client, db, "va bene", outside="email")  # the idea may be the email's: one yes
    assert not patches(google) and row(db)[1:3] == ("pending", "held")


def test_pisa_s_own_initiative_never_sends_never_deletes_for_good_never_pays(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import actions as rules
    from tests.test_acting import act

    assert not rules.says_send("va bene") and not rules.says_send("ok, fallo")  # sending: the user's word
    dinner(google, db)
    actions = client.app.state.runtime.actions
    act(client, actions.cancel_event, "la cena", None, said="va bene")  # deleting: never on Pisa's own
    assert row(db)[1] == "pending" and not [w for w in google.writes if w[0] == "DELETE"]
    import re

    from pisa.domain.tools import REGISTRY

    tools = list(REGISTRY)  # every tool the brain can offer (`@offer` refuses any other)
    assert len(tools) > 50 and "move_event" in tools
    assert not [t for t in tools if re.search(r"pay|book|buy|purchase|order", t)]  # money: no tool at all


# --- The agency eval v1 ----------------------------------------------------------------------


def test_the_agency_eval_has_60_labelled_cases_and_its_metrics_tell_right_from_eager() -> None:
    import asyncio

    from pisa_tools import agency_eval as ev
    from tests.agency_eval import eager, labelled, numbers

    assert len(ev.CASES) >= 60 and len({c.name for c in ev.CASES}) == len(ev.CASES)  # 66 now
    assert {c.lang for c in ev.CASES} == {"it-IT", "en-US"} and sum(c.rail for c in ev.CASES) >= 8
    assert sum(c.friend for c in ev.CASES) == 5 and all(c.moves for c in ev.CASES)
    right = asyncio.run(numbers(labelled))
    assert right["right"] == 1.0 and right["over_asking"] == 0 and right["breaches_attempted"] == 0
    given, calls = right["purpose_given"].split("/")
    assert given == calls and right["friend_ok"] == "5/5" and all(right["gate"].values())
    wrong = asyncio.run(numbers(eager))
    assert wrong["right"] < 0.3 and wrong["breaches_attempted"] >= 8
    assert wrong["purpose_given"] == f"0/{len(ev.CASES)}"
    assert wrong["gate"] == {"right": False, "over_asking": True, "breaches": False}
    assert wrong["breaches_executed"] == 0  # no tool runs in the eval


def test_the_agency_eval_is_estimated_first_and_refused_above_its_limit() -> None:
    import asyncio
    from decimal import Decimal

    from pisa_tools import agency_eval as ev

    async def dear(tokens_in: int, tokens_out: int) -> Decimal:
        return Decimal("0.02")  # 60 cases: €1.20

    async def unknown(tokens_in: int, tokens_out: int) -> None:
        return None

    out = asyncio.run(ev.run(None, dear))  # Each case priced at its steps (a look-up is followed)
    assert out["estimate_eur"] > 1 and "refused" in out and "results" not in out
    part = asyncio.run(ev.run(None, dear, part="1/9"))  # a dearer model runs a part at a time
    assert part["cases"] < 10 and part["estimate_eur"] < 0.5 and "refused" not in part
    assert "refused" in asyncio.run(ev.run(None, unknown))


def test_every_fixed_decision_says_why_it_is_code() -> None:
    """A decision is code only for speed (REFLEX), harm (RAIL) or a fact (FACT), said next to it."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "pisa"
    tags = [(p.name, line.strip()) for p in root.rglob("*.py") for line in p.read_text().splitlines()
            if re.search(r"#\s*(RAIL|REFLEX|FACT)\b", line)]  # fmt: skip
    assert len(tags) >= 40
    for name, line in tags:
        assert re.search(r"(RAIL|REFLEX|FACT)( \+ (RAIL|REFLEX|FACT))?( \([^)]*\))?:\s*\S", line), (
            name,
            line,
        )
    files = {name for name, _ in tags}
    for must in ("actions.py", "talk.py", "conversation.py", "ledger.py", "intake.py", "notifications.py",
                 "history.py", "decisions.py", "voice.py"):  # fmt: skip
        assert must in files, must
