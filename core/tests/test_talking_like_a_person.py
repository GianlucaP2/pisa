"""Pisa talks like a person, on the server: the first audio within about a second (a holding
phrase, a progress line, the reply after an action said from its result), the rules that are code ("eh?",
no filler, no greeting but the day's first, no "C'è altro?", "no, alle 9", times as people say them), when
Pisa speaks first (`urgency`, held for breakpoints, "dopo"), and the eval set. Each fails without its fix."""

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.actions_fake import ActionsGoogle, connect
from tests.assistant_env import rome, set_settings
from tests.conftest import deadline, pair
from tests.test_acting import Model, patches, row, said, socket_turns

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"


@pytest.fixture
def google(http_handler, db, secrets_key) -> ActionsGoogle:  # type: ignore[no-untyped-def]
    fake = ActionsGoogle()
    http_handler["handler"] = fake
    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)
    return fake


def slow(text: str, after_s: float):  # type: ignore[no-untyped-def]
    """A model whose first words come `after_s` seconds after it is asked."""

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        await asyncio.sleep(after_s)
        yield text

    return reply


# --- holding phrases (rule 2) ------------------------------------------------------------------------------


def test_the_holding_list_is_the_shared_one_short_and_in_both_languages() -> None:
    from pisa.domain import holding

    shared = json.loads((PROTOCOL / "holding-phrases.v1.json").read_text())["phrases"]
    assert [(p["id"], p["lang"], p["text"], p["use"]) for p in shared] == [
        (p.id, p.lang, p.text, p.use) for p in holding.PHRASES
    ]
    assert all(len(p.text.split()) <= 4 for p in holding.PHRASES)
    assert {(p.lang, p.use) for p in holding.PHRASES} == {
        ("it-IT", "any"), ("it-IT", "lookup"), ("en-US", "any"), ("en-US", "lookup"),
        ("it-IT", "slow"), ("en-US", "slow"),  # Past the turn budget
    }  # fmt: skip
    first = holding.pick("it-IT", "any")
    assert holding.pick("it-IT", "any", last=first.id).id != first.id  # never twice in a row


def test_a_slow_answer_gets_a_holding_phrase_first_and_it_is_not_in_the_history(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from pisa.domain.holding import PHRASES

    client.app.state.runtime.conversations.hold_after_s = 0.7
    fake_models.default = slow("Sono le tredici.", 1.5)
    (frames,) = socket_turns(client, db, secrets_key, Model("x", []), "che ore sono?")
    first = frames[0]
    assert first["type"] == "say" and first["holding"] is True and first["final"] is False
    assert first["holding_id"] in {p.id for p in PHRASES} and first["delta"].strip() == "Vediamo…"
    assert said(frames).endswith("Sono le tredici.")
    saved = db.execute("SELECT content FROM messages WHERE role = 'assistant'").fetchone()
    assert saved == ("Sono le tredici.",)


def test_a_fast_answer_gets_no_holding_phrase(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    # The answer comes before the timer: 5 s, not 0.7 s, because a loaded CI runner took 0.69 s for this
    # fixture turn alone (run 560).
    client.app.state.runtime.conversations.hold_after_s = 5.0
    fake_models.default = "Sono le tredici."
    (frames,) = socket_turns(client, db, secrets_key, Model("x", []), "che ore sono?")
    assert not [f for f in frames if f.get("holding")]


def test_long_work_says_the_step_once_after_six_seconds(
    client, db, secrets_key, fake_models, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    from pisa.services import conversation

    monkeypatch.setattr(conversation, "PROGRESS_AFTER_S", 1.5)  # 6 s in production
    client.app.state.runtime.conversations.hold_after_s = 0.3
    said: list[asyncio.Event] = []
    line = conversation.holding.progress_line

    def progress(lang: str, tool: str) -> str:
        said[0].set()
        return line(lang, tool)

    monkeypatch.setattr(conversation.holding, "progress_line", progress)

    async def model(messages, info):  # type: ignore[no-untyped-def]
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            said.append(asyncio.Event())
            yield {0: DeltaToolCall(name="get_tasks", json_args=json.dumps({}))}
            return
        # (CI under load) the long work lasts until the step was said, a condition, not 2.5 s
        await asyncio.wait_for(said[0].wait(), 30)
        yield "Sole tutto il giorno."

    fake_models.default = model
    (frames,) = socket_turns(client, db, secrets_key, Model("x", []), "che tempo fa?")
    deltas = [f["delta"].strip() for f in frames if f["type"] == "say"]
    assert deltas.count("Ci sto ancora lavorando.") == 1  # a step without its own line: the general one
    assert deltas.index("Ci sto ancora lavorando.") < deltas.index("Sole tutto il giorno.")


def test_a_step_that_starts_after_the_window_still_gets_its_line(
    client, db, secrets_key, fake_models, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # Long work whose first tool starts after the progress window (a slow first round; a loaded runner) still
    # hears the step once: the timer waits for a step instead of looking only once.
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    from pisa.services import conversation

    monkeypatch.setattr(conversation, "PROGRESS_AFTER_S", 1.0)
    client.app.state.runtime.conversations.hold_after_s = 0.3
    said: list[asyncio.Event] = []
    line = conversation.holding.progress_line

    def progress(lang: str, tool: str) -> str:
        said[0].set()
        return line(lang, tool)

    monkeypatch.setattr(conversation.holding, "progress_line", progress)

    async def model(messages, info):  # type: ignore[no-untyped-def]
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            said.append(asyncio.Event())
            await asyncio.sleep(1.2)  # past the window (0.6 s after the utterance) before any step
            yield {0: DeltaToolCall(name="get_tasks", json_args=json.dumps({}))}
            return
        await asyncio.wait_for(said[0].wait(), 15)
        yield "Sole tutto il giorno."

    fake_models.default = model
    (frames,) = socket_turns(client, db, secrets_key, Model("x", []), "che tempo fa?")
    deltas = [f["delta"].strip() for f in frames if f["type"] == "say"]
    assert deltas.count("Ci sto ancora lavorando.") == 1


# --- the reply after an action: no second model round (rule 4); the model's finish(say=…) ----------


def test_an_action_is_said_from_its_result_with_one_model_call(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    finish = ("finish", {"say": "Aggiunto: «Comprare il latte»."})
    model = Model("segnati il latte", [("add_task", {"title": "Comprare il latte"}), finish])
    fake_models.default = model
    (frames,) = socket_turns(client, db, secrets_key, model, "segnati il latte")
    assert said(frames) == "Aggiunto: «Comprare il latte»." and model.seen == 1  # every result was done
    calls = db.execute("SELECT count(*) FROM llm_calls WHERE slot = 'chat.conversation'").fetchone()
    assert calls == (1,)  # nothing billed for a model call that didn't happen


def test_a_refused_action_goes_back_to_the_model(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    model = Model("completa il pane", [("complete_task", {"task_ref": "pane"})])  # no such task
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, "completa il pane")
    assert model.seen == 2  # "No open task matches": the model words the question


# --- the rules that are code -------------------------------------------------------------------------------


def test_eh_says_the_last_reply_again_shorter_with_no_model_call(
    client, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    model = Model("x", [])
    fake_models.default = model
    model_text = "Domani alle 10 hai il dentista, poi alle 15 la riunione con Marco e il team del progetto."

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        model.seen += 1
        yield model_text

    fake_models.default = reply
    _, again = socket_turns(client, db, secrets_key, model, "cosa ho domani?", "eh?")
    assert model.seen == 1 and said(again) == "Domani alle 10 hai il dentista."


def test_no_filler_no_greeting_after_the_first_exchange_no_anything_else(
    client, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    fake_models.default = "Ciao! Certamente! Domani piove. C'è altro?"
    first, second = socket_turns(client, db, secrets_key, Model("x", []), "piove domani?", "e dopodomani?")
    assert said(first) == "Ciao! Domani piove. "  # the day's first exchange may greet
    assert said(second) == "Domani piove. "


def test_no_alle_22_corrects_the_time_pisa_just_said(client, db, google, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_approvals import dinner

    dinner(google, db)
    model = Model("sposta la cena alle 21", [("move_event", {"event": "la cena", "start": "oggi alle 21"})])
    fake_models.default = model
    done, fixed = socket_turns(client, db, secrets_key, model, "sposta la cena alle 21", "no, alle 22")
    assert said(done).startswith("Ho spostato «Cena» a oggi, martedì 20, alle 21.")
    # The move's result goes back to the model (2 calls); the correction is code's (none)
    assert said(fixed).startswith("Ho spostato «Cena» a oggi, martedì 20, alle 22.") and model.seen == 2
    assert google.events["ev-dinner"]["start"]["dateTime"].startswith("2026-10-20T22:00")
    assert len(patches(google)) == 2 and row(db)[1:3] == ("done", "words")


def test_times_as_people_say_them() -> None:
    from pisa.domain.actions import spoken_clock, spoken_when

    today = date(2026, 10, 20)
    # Every date with its weekday ("giovedì 30" leaves no doubt)
    assert spoken_when(rome(2026, 10, 20, 21), today, "it-IT", "Europe/Rome") == "oggi, martedì 20, alle 21"
    assert (
        spoken_when(rome(2026, 10, 21, 9, 30), today, "it-IT", "Europe/Rome")
        == "domani, mercoledì 21, alle 9 e mezza"
    )
    assert spoken_when(rome(2026, 10, 23, 13, 15), today, "it-IT", "Europe/Rome") == "venerdì 23 alle 13 e 15"
    assert spoken_when(rome(2026, 11, 3, 1), today, "it-IT", "Europe/Rome") == "martedì 3 novembre all'una"
    assert (
        spoken_when(rome(2026, 10, 20, 21, 30), today, "en-US", "Europe/Rome")
        == "today, Tuesday 20, at 9:30 pm"
    )
    assert spoken_clock(12, 0, "en-US") == "at noon"


# --- when Pisa speaks first (rules 29-30)
# ---------------------------------------------------------------------


def test_leave_now_is_urgent_and_a_reminder_for_later_waits() -> None:
    from pisa.domain import talk

    assert talk.urgency("leave_now") == "now" and talk.urgency("info", "urgent") == "now"
    assert talk.urgency("reminder", minutes_left=10) == "now"
    assert talk.urgency("reminder", minutes_left=60) == "breakpoint"
    assert talk.urgency("briefing") == "breakpoint" and talk.urgency("check_in") == "breakpoint"
    assert talk.interrupting("Esci ora: 12 minuti a piedi.", "it-IT").startswith("Scusa, un attimo: ")
    assert talk.defers("dopo") and talk.defers("Non ora.") and not talk.defers("dopo cena vado")


def _queue(client: Any, kind: Any, text: str, urgency: str | None = None, device: Any = None) -> str:
    from pisa.services.notifications import enqueue

    runtime = client.app.state.runtime

    async def go() -> str:
        async with runtime.pool.connection() as conn:
            nid, _ = await enqueue(
                conn, kind=kind, text=text, lang="it-IT", now=runtime.clock(), timezone="Europe/Rome",
                urgency=urgency, device_id=device,  # type: ignore[arg-type]
            )  # fmt: skip
        return str(nid)

    return client.portal.call(go)


def _deliver(client: Any, nid: str) -> Any:
    import uuid

    from pisa.services.notifications import Dispatcher

    runtime = client.app.state.runtime
    from tests.conftest import pause_outbox

    pause_outbox(client)  # this test's dispatcher is the only one delivering
    dispatcher = Dispatcher(
        runtime.pool, runtime.live, runtime.push, clock=runtime.clock, unasked=runtime.conversations.unasked,
        breakpoints=runtime.live, owner=runtime.owner
    )  # fmt: skip
    return client.portal.call(dispatcher.deliver, uuid.UUID(nid))


def test_now_opens_with_scusa_and_carries_its_urgency(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.live_app import live

    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        nid = _queue(client, "reminder", "Esci ora: 12 minuti a piedi per Cena.", "now", device_id)
        _deliver(client, nid)
        frame = app.next()
    assert frame["type"] == "notify" and frame["urgency"] == "now"
    assert frame["text"] == "Scusa, un attimo: Esci ora: 12 minuti a piedi per Cena."
    assert db.execute("SELECT urgency FROM notifications").fetchone() == ("now",)


def test_a_breakpoint_item_waits_for_the_end_of_a_meeting(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    db.execute("INSERT INTO calendars (id, name, access_role) VALUES ('cal', 'Lavoro', 'owner')")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) VALUES "
        "('cal', 'm1', 'Riunione budget', %s, %s, false, 'confirmed')",
        (clock.now - timedelta(minutes=10), clock.now + timedelta(minutes=50)),
    )
    nid = _queue(client, "info", "Il pacco è arrivato.")
    held = _until(db, "SELECT not_before, delivered_via FROM notifications", lambda: _deliver(client, nid))
    assert held == (clock.now + timedelta(minutes=50), None)  # held: not during the meeting
    clock.now += timedelta(minutes=51)
    done = _until(db, "SELECT delivered_via FROM notifications", lambda: _deliver(client, nid))
    assert done == ("none",)  # no device; not expired early


def _until(db: Any, sql: str, deliver: Any, within_s: float = 60.0) -> Any:
    """The row once a delivery got through: the app's own outbox sweep may hold the row when ours tries, so
    ours tries again (as the next minute's sweep would). It waits on that condition; the
    deadline only stops a test that would never pass (a loaded CI box gave up after 5 s)."""
    import time

    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        deliver()
        got = db.execute(sql).fetchone()
        if got is not None and got[0] is not None:
            return got
        time.sleep(0.05)
    return db.execute(sql).fetchone()


def test_a_breakpoint_item_waits_while_the_owner_talks_to_pisa(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    started = asyncio.Event()

    async def talking(messages, info):  # type: ignore[no-untyped-def]
        started.set()
        await asyncio.sleep(1.0)
        yield "Fatto il conto."

    fake_models.default = talking
    with deadline(30), live(client, token, device_id) as app:
        app.say("quanto fa", lang="it-IT", source="phone_mic")
        client.portal.call(started.wait)
        nid = _queue(client, "info", "Il pacco è arrivato.", device=device_id)
        _deliver(client, nid)
        kinds = []
        while "notify" not in kinds:
            frame = app.next()
            kinds.append("final" if frame["type"] == "say" and frame["final"] else frame["type"])
    assert kinds.index("final") < kinds.index("notify")  # after the reply, never over it


def test_dopo_puts_it_off_once(client, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    """ "Dopo" puts it off once, stored: undelivered again in the database until the end of
    the next exchange (or DEFER_S, by the sweep); a second "dopo" is no put-off."""
    from tests.conftest import set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    fake_models.default = "Sono le tredici."
    device_id, token = pair(db)
    with deadline(30), live(client, token, device_id) as app:
        _deliver(client, _queue(client, "info", "Il pacco è arrivato.", device=device_id))
        assert app.next()["type"] == "notify"
        app.say("dopo", lang="it-IT", source="phone_mic")
        reply = app.next()
        assert reply["type"] == "say" and reply["delta"] == "Ok, dopo." and reply["final"] is True
        stored = db.execute("SELECT delivered_via, put_off_at IS NOT NULL, not_before FROM notifications")
        assert stored.fetchone()[:2] == (None, True)  # type: ignore[index]
        app.say("che ore sono?", lang="it-IT", source="phone_mic")
        kinds = []
        while "notify" not in kinds:
            frame = app.next()
            kinds.append("final" if frame["type"] == "say" and frame["final"] else frame["type"])
            if frame["type"] == "notify":
                assert frame["text"] == "Il pacco è arrivato."
        assert kinds.index("final") < kinds.index("notify")  # back at the end of the next exchange
        app.say("dopo", lang="it-IT", source="phone_mic")
        again = [app.next()]
        while not (again[-1]["type"] == "say" and again[-1]["final"]):
            again.append(app.next())
        assert "Ok, dopo." not in [f.get("delta") for f in again]  # once only: Pisa answers it as words


def test_a_held_item_survives_the_socket_closing_before_the_breakpoint(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, clock
) -> None:
    from pisa.services.notifications import BUSY_HOLD
    from tests.conftest import set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    started, stop = asyncio.Event(), asyncio.Event()

    async def talking(messages, info):  # type: ignore[no-untyped-def]
        started.set()
        await stop.wait()
        yield "Fatto."

    fake_models.default = talking
    with live(client, token, device_id) as app:
        app.say("quanto fa", lang="it-IT", source="phone_mic")
        client.portal.call(started.wait)
        nid = _queue(client, "info", "Il pacco è arrivato.", device=device_id)
        # The app's own outbox sweep may hold the row when ours tries (CI run 584): try until it is held.
        held = _until(
            db, "SELECT not_before, delivered_via FROM notifications", lambda: _deliver(client, nid)
        )
        assert held == (clock.now + BUSY_HOLD, None)  # stored, not in the socket's memory
    client.portal.call(stop.set)
    clock.now += BUSY_HOLD + timedelta(seconds=1)
    assert _until(db, "SELECT delivered_via FROM notifications", lambda: _deliver(client, nid)) == ("none",)


# --- the prompt: shorter, and conversational ---------------------------------------------------------------


def test_the_prompt_is_shorter_and_the_research_guide_only_where_it_helps() -> None:
    from pisa.brain.conversation import RESEARCH_GUIDE, self_block, system_prompt
    from pisa.domain import bible

    # was 8662 characters; the body line adds Pisa's self block (about 2.5k) in front, cached with the rest;
    # the voice bible adds its rules (about 1k) at the end, cached too
    rules = len(bible.rules(lang="it-IT"))
    assert (
        len(system_prompt(lang="it-IT")) - len(self_block(lang="it-IT")) - rules < 1500
        and len(self_block(lang="it-IT")) < 2900
    )  # Prompt v2
    assert rules < 1200  # the bible stays one short page
    assert "How to research" not in system_prompt(lang="it-IT")
    assert "How to research" in RESEARCH_GUIDE
    from pisa.brain.skills import load

    prompt = " ".join(
        (system_prompt(lang="it-IT") + "".join(s.body for s in load())).split()
    )  # the prompt, the skills
    for rule in ('Italian "tu"', "The answer or what you did first", "At most three options aloud",
                 "the most specific question", "Disagree softly", "one clause that matches it"):  # fmt: skip
        assert rule in prompt, rule
    # The profile hold names events again (the about-me skill)
    assert "a page, an email or an event: Pisa asks first" in prompt


def test_the_research_guide_goes_only_to_turns_that_look_things_up(
    client, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        seen.append(info.instructions or "")
        yield "Ok."

    fake_models.default = model
    socket_turns(
        client, db, secrets_key, Model("x", []), "sposta la palestra a domani", "trova un bar aperto"
    )
    # A quick request gets no guide; a find request has it from its first request
    move, find = seen
    assert "How to research" not in move and "How to research" in find


def test_the_research_guide_joins_once_a_research_tool_is_called(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall

    seen: list[str] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        seen.append(info.instructions or "")
        if not any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            yield {0: DeltaToolCall(name="map_search", json_args=json.dumps({"query": "Bistrot dell'Ansa"}))}
            return
        yield "Ok."

    fake_models.default = model
    socket_turns(client, db, secrets_key, Model("x", []), "Com'è il Bistrot dell'Ansa?")
    first, *found, after = seen  # the words name no research ("Com'è"): the tool call brings the guide
    assert "How to research" not in first and "How to research" in after
    assert len(found) == 1 and "How to research" not in found[0]  # Finding map_search is no research


# --- the eval set (rules 10-28): code where the rule is code, the real cheap model behind a flag ------------

# (language, the user's words, what a careless model says, what Pisa must say): through the conversation.
CODE_CASES: list[tuple[str, str, str, str]] = [
    ("it-IT", "piove domani?", "Certamente! Domani piove.", "Domani piove."),
    ("it-IT", "e sabato?", "Assolutamente. Sabato sole.", "Sabato sole."),
    ("it-IT", "a che ora è il dentista?", "Ciao! Alle 10.", "Alle 10."),
    ("it-IT", "che faccio stasera?", "Buonasera! Hai la cena alle 20.", "Hai la cena alle 20."),
    ("it-IT", "quanto ci metto?", "Venti minuti a piedi. C'è altro?", "Venti minuti a piedi. "),
    ("it-IT", "ok grazie", "Figurati. Posso aiutarti in altro?", "Figurati. "),
    ("it-IT", "e il treno?", "Perfetto! Parte alle 18.", "Parte alle 18."),
    ("en-US", "what time is it?", "Certainly! It's 1 pm.", "It's 1 pm."),
    ("en-US", "is it raining?", "Hi! No rain today. Anything else?", "No rain today. "),
    ("en-US", "when is lunch?", "Great question. At 1:30 pm.", "At 1:30 pm."),
    ("it-IT", "e il meteo?", "Salve! Nuvoloso, 18 gradi.", "Nuvoloso, 18 gradi."),
    ("it-IT", "chi c'è alla riunione?", "Certo! Marco e Anna.", "Marco e Anna."),
    ("it-IT", "quando parte il volo?", "Ottima domanda. Alle 7 e 40.", "Alle 7 e 40."),
    # ("Te la segno fatta" with no tool called is a claimed change: the rail's case, test_night_commands)
    ("it-IT", "ho finito la spesa", "Perfetto! Ottimo lavoro. Serve altro?", "Ottimo lavoro. "),
    ("it-IT", "mi ricordi il pacco?", "Assolutamente! Domani alle 9.", "Domani alle 9."),
    ("en-US", "who's at the meeting?", "Of course! Marco and Anna.", "Marco and Anna."),
    (
        "en-US",
        "did I finish the shopping?",
        "Absolutely. Yes, this morning. Is there anything else?",
        "Yes, this morning. ",
    ),
    ("en-US", "what's the weather?", "Good morning! Cloudy, 18 degrees.", "Cloudy, 18 degrees."),
]


@pytest.mark.parametrize(("lang", "words", "careless", "expected"), CODE_CASES)
def test_eval_code_rules(client, db, secrets_key, fake_models, lang, words, careless, expected) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    fake_models.default = "Ok."  # a first exchange: the case is not the day's first, so no greeting
    with live(client, token, device_id) as app:
        app.say("ciao", lang=lang, source="phone_mic")
        list(app.reply())
        fake_models.default = careless
        app.say(words, lang=lang, source="phone_mic")
        got = "".join(f.get("delta") or "" for f in app.reply())
    assert got.strip() == expected.strip() and not violations(got, lang)


def violations(reply: str, lang: str, *, asked_long: bool = False, after_yes: bool = False) -> list[str]:
    """The rules the eval holds a reply to (numbers only, no judgement): pisa/domain/talk.py's, shared with
    the real-model eval job."""
    from pisa.domain import talk

    return talk.violations(reply, lang, asked_long=asked_long, after_yes=after_yes)


def test_eval_every_sentence_the_code_says_follows_the_rules() -> None:
    """Pisa's own sentences (templates, holding phrases, repairs, interjections): tu, no filler, short."""
    from pisa.brain import spoken
    from pisa.domain import actions, holding, talk

    lines = [v for lang in ("it-IT", "en-US") for v in actions.WORDS[lang].values()]
    lines += list(spoken.STOPPED.values())
    lines += [p.text for p in holding.PHRASES] + list(talk.LATER.values()) + list(talk.NOW_PREFIX.values())
    for line in lines:
        assert not violations(line.format_map(_Blank()), "it-IT"), line


class _Blank(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return "X"


REPAIR_CASES = [  # rule 10: the repair names the candidates
    ("Quale? 1) Sposto «Cena» a mar 20 ott alle 21:00?; 2) Sposto «Palestra» a mar 20 ott alle 21:00?",
     ["Cena", "Palestra"]),
    ("Which one? 1) Move «Dinner» to Tue 20 Oct at 21:00?; 2) Move «Gym» to Tue 20 Oct at 21:00?",
     ["Dinner", "Gym"]),
]  # fmt: skip


@pytest.mark.parametrize(("repair", "names"), REPAIR_CASES)
def test_eval_a_repair_is_specific(repair, names) -> None:  # type: ignore[no-untyped-def]
    assert all(n in repair for n in names) and "non ho capito" not in repair.lower()


EH_CASES = [
    ("Ho trovato tre posti vicino. Il primo è il Bar Roma, a 200 metri. Vuoi le indicazioni?",
     "Vuoi le indicazioni?"),
    ("Sole tutto il giorno, massima 22.", "Sole tutto il giorno, massima 22."),
    ("Certo! Il treno parte alle 18 dal binario 5, ma oggi c'è sciopero e potrebbe tardare.",
     "Il treno parte alle 18 dal binario 5."),
    ("Ciao! Hai tre cose oggi.", "Hai tre cose oggi."),
    ("Ho spostato «Cena» a oggi alle 21. Se vuoi lo annullo.", "Ho spostato «Cena» a oggi alle 21."),
    ("Sposto «Cena» a oggi alle 21 e avviso Paola?", "Sposto «Cena» a oggi alle 21 e avviso Paola?"),
    ("Perfetto. Il dentista è domani alle 10.", "Il dentista è domani alle 10."),
    ("You have three things today: the dentist at 10, lunch with Marco and a call at 4 pm with the team.",
     "You have three things today."),
]  # fmt: skip


@pytest.mark.parametrize(("reply", "again"), EH_CASES)
def test_eval_eh_is_the_key_sentence_again(reply, again) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import talk

    assert (
        talk.is_huh("eh?") and talk.is_huh("Cosa?") and talk.is_huh("sorry?") and not talk.is_huh("cosa fai?")
    )
    assert talk.again(reply) == again and talk.word_count(talk.again(reply)) <= 12


# The real cheap model, by hand (PISA_EVAL_REAL=1 with a key in the environment), or on the server as the
# maintenance job `conversation-eval` through Pisa's gateway (pisa_tools/talk_eval.py: the same cases and
# rules). Off in CI: no paid calls outside the ledgers; numbers only.
def _real_cases() -> list[Any]:
    from pisa_tools.talk_eval import CASES

    return CASES


@pytest.mark.skipif(os.environ.get("PISA_EVAL_REAL") != "1", reason="the real model eval runs by hand (paid)")
@pytest.mark.parametrize("case", _real_cases(), ids=lambda c: c.name)
def test_eval_real_model(case) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import talk
    from tests.eval_real import ask_real

    reply = ask_real(case)
    assert not talk.violations(reply, case.lang, asked_long=case.asked_long, check=case.check), reply


# --- the latency report: close → first audio, by reply kind, before and after (modelled) --------------------


def test_first_audio_by_reply_kind_before_and_after(client, db, google, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    """A model with a provider's shape of latency (0.45 s + 0.12 s per 1,000 prompt tokens to the first
    token, 0.25 s per sentence, the same again for each round after a tool) runs the fixture turns twice:
    as before (no holding phrase, a second model round after an action, the research guide in every
    turn, the yes through the model) and after. The table goes to PISA_LATENCY_REPORT when set."""
    from tests.latency_report import run_report

    table = run_report(client, db, google, secrets_key, fake_models)
    for kind, (before, after) in table.items():
        # Deterministic under load: the modelled waits are seeded and every one of them is longer than the
        # 0.7 s timer, and the loop runs timers in deadline order, so what comes first doesn't depend on load.
        assert not before["held"], kind  # before: no holding phrase
        if kind == "research":  # Said back at once, in both runs, before any model round
            assert not after["held"], kind  # the words said back come before the timer even starts
        else:  # rule 2: the first audio is the holding phrase at 0.7 s, never the wait for the model
            assert after["held"], kind
        assert after["model_rounds"] <= before["model_rounds"], kind  # rule 4, B1: never more rounds
    # the action and its words: two rounds. (Its "before" run took 9: the research guide it forces on every
    # turn also cut the tools to research's own, move_event too; the cache rule keeps a turn's tools whole.)
    assert table["action"][1]["model_rounds"] <= 2
    # the handoff's table, run by hand on an idle machine: the numbers
    if os.environ.get("PISA_LATENCY_REPS"):
        for kind, (before, after) in table.items():
            assert after["p50"] <= 1.0 and after["p95"] <= 1.1, kind  # rule 2: first audio within ~1 s
            assert after["p50"] < before["p50"] or max(after["p50"], before["p50"]) <= 0.05, kind
    if path := os.environ.get("PISA_LATENCY_REPORT"):
        Path(path).write_text(json.dumps(table, indent=2) + "\n")
