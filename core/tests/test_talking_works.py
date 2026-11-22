"""Talking works: Pisa's speed (the reflex on the final words, where the time goes), Pisa going deaf,
confirmations, the last words, and the leftovers."""

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage

from pisa.domain import speculation
from pisa.domain.reflex import rules_read
from pisa.domain.whisper import Kind, Priority
from pisa.ports.talker import OwnerWords, PisaWords, Speaking
from pisa.services.conversation import turn_parts
from pisa.services.reflex import PisaReads, Reflexed, ReflexService
from pisa.services.talk import Thought
from tests.owner import owner_at
from tests.test_live_talk import _run, _until
from tests.virtual_time import run_talk

NOW = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)
DEVICE = uuid.UUID(int=11)
AGENDA = "Monday 2026-11-02. Events: 15:00-16:00 Dentista. Tasks due: no tasks due."


# --- The reflex on the final words (natural mode: no partial words) -------------------------------------


class Reads(PisaReads):
    def __init__(self) -> None:
        super().__init__(None, None, None, owner=owner_at(), clock=lambda: NOW)
        self.ran: list[tuple[str, date]] = []

    async def run(self, read: str, *, device: uuid.UUID, day: date, place: str | None) -> str | None:
        self.ran.append((read, day))
        found = {"agenda": AGENDA, "plan": "Today in Pisa's plan: 15:00 Dentista.", "weather": "Weather: 18°"}
        return found.get(read)


class Drafting(ReflexService):
    """Pisa's reflex with the draft scripted (the model's part); the rules, the reads and the rail are
    Pisa's."""

    def __init__(self, draft: str | None) -> None:
        self.reads = Reads()
        super().__init__(object(), self.reads, owner=owner_at(), clock=lambda: NOW)  # type: ignore[arg-type]
        self.draft = draft
        self.drafted: list[list[str]] = []

    async def _answer(
        self, words: str, data: list[str], lang: str, *, budget_s: float | None = None
    ) -> str | None:
        self.drafted.append(data)
        return self.draft


def _final(service: ReflexService, words: str) -> tuple[str | None, list[Reflexed]]:
    seen: list[Reflexed] = []

    async def on_read(r: Reflexed) -> None:
        seen.append(r)

    return run_talk(service.final(DEVICE, words, "it-IT", on_read=on_read)), seen


def test_a_question_s_final_words_get_pisa_s_ready_answer_from_its_own_data() -> None:
    service = Drafting("Il dentista è alle 15:00.")
    ready, (read,) = _final(service, "a che ora ho il dentista oggi?")
    assert ready == "Il dentista è alle 15:00." and read.by == "final" and read.ready == ready
    assert [r for r, _ in service.reads.ran] == ["agenda"]  # what the words name, read at once
    # the main thinker finds those reads (the shared state, as on partial words)
    assert "15:00-16:00 Dentista" in service.state(DEVICE).for_brain(NOW, "a che ora ho il dentista oggi?")


def test_the_final_pass_never_says_a_time_pisa_s_data_lacks_and_never_answers_a_change() -> None:
    wrong = Drafting("Il dentista è alle 16:30.")
    assert _final(wrong, "a che ora ho il dentista oggi?")[0] is None  # RAIL: grounded or nothing
    change = Drafting("Fatto, alle 18.")
    assert _final(change, "spostami il dentista alle 18")[0] is None
    assert change.reads.ran == [] and change.drafted == []  # a change is the brain's: no read, no call
    chat = Drafting("Ciao!")
    assert _final(chat, "grazie mille Pisa")[0] is None and chat.drafted == []


def test_a_question_that_names_no_read_reads_the_day_and_the_forecast_only_on_final_words() -> None:
    service = Drafting(None)
    _final(service, "cosa mi resta adesso?")
    assert [r for r, _ in service.reads.ran] == ["agenda", "plan", "tasks"]  # the day's own data
    assert "weather" in rules_read("che tempo fa domani?").reads
    assert not speculation.may_read("weather") and speculation.may_read(
        "weather", final=True
    )  # an outside read: on the final words only, even on a stable prefix
    weather = Drafting(None)
    _final(weather, "che tempo fa domani?")
    assert ("weather", date(2026, 11, 3)) in weather.reads.ran


def test_in_a_talk_the_final_words_ready_answer_is_said_first_and_the_brain_s_goes_in_silently() -> None:
    run, session, _, seen = _run(Thought("Il dentista è alle 15:00.", Kind.SAY))
    brain = asyncio.Event()
    think = run.hooks.think

    async def slow(words: str, trace: str, yes: bool) -> Thought:
        await brain.wait()
        return await think(words, trace, yes)

    async def ready_final(words: str) -> str | None:
        return "Il dentista è alle 15:00." if "dentista" in words else None

    run.hooks.think, run.hooks.ready_final = slow, ready_final

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho il dentista oggi?", final=True))  # no partials
        await _until(lambda: session.whispers)  # the reflex's answer, while the brain still works
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Il dentista è alle 15:00.", final=True))
        session.queue.put_nowait(Speaking(False))
        await _until(lambda: any(e["kind"] == "pisa_said" for e in run.timeline.events))
        brain.set()
        await _until(lambda: len(session.whispers) == 2)
        run.end("owner")
        await task

    run_talk(go())
    ready, brain_answer = session.whispers
    assert ready.id == "final1" and ready.priority is Priority.PAUSE
    assert brain_answer.priority is Priority.SILENT  # Pisa's voice said it already: never twice
    (exchange,) = [e for name, e in seen if name == "exchange"]
    assert exchange.first_content_by == "reflex"
    assert next(e for e in run.timeline.events if e["kind"] == "ready_final")["ready"] is True


def test_a_ready_answer_later_than_the_brain_s_is_never_said() -> None:
    run, session, _, _ = _run(Thought("Il dentista è alle 15:00.", Kind.SAY))
    release = asyncio.Event()

    async def ready_final(words: str) -> str | None:
        await release.wait()
        return "Il dentista è alle 15:00."

    run.hooks.ready_final = ready_final

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho il dentista oggi?", final=True))
        await _until(lambda: session.whispers)  # the brain first
        release.set()
        await _until(lambda: any(e["kind"] == "ready_final" for e in run.timeline.events))
        run.end("owner")
        await task

    run_talk(go())
    assert [w.id for w in session.whispers] == ["w1"]
    assert next(e for e in run.timeline.events if e["kind"] == "ready_final")["late"] is True


# --- Where the time goes --------------------------------------------------------------------------------


def test_the_brain_s_parts_are_its_rounds_with_their_tokens_and_its_tools() -> None:
    responses = [
        ModelResponse([TextPart("x")], usage=RequestUsage(input_tokens=19000, output_tokens=40)),
        ModelResponse([TextPart("y")], usage=RequestUsage(input_tokens=19500, output_tokens=30)),
    ]
    parts = turn_parts("t1", 0.25, [2100, 1800], responses, [("get_agenda", 0.12)])
    assert parts == {
        "trace": "t1", "context_ms": 250, "model_ms": 3900, "tools_ms": 120,
        "rounds": [  # Each round's prompt-cache counts (read, written) beside its tokens
            {"ms": 2100, "in": 19000, "out": 40, "cr": 0, "cw": 0},
            {"ms": 1800, "in": 19500, "out": 30, "cr": 0, "cw": 0},
        ],
        "tools": [{"name": "get_agenda", "ms": 120}],
    }  # fmt: skip


def test_the_brain_s_parts_reach_the_timeline_and_the_turn_s_measures() -> None:
    rounds = [{"ms": 1500, "in": 9000, "out": 20}]
    parts = {"trace": "t", "context_ms": 200, "rounds": rounds, "model_ms": 1500, "tools": [], "tools_ms": 0}
    run, session, _, seen = _run(Thought("Alle 15.", Kind.SAY, parts=parts))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho il dentista?", final=True))
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(go())
    event = next(e for e in run.timeline.events if e["kind"] == "brain_parts")
    assert event["model_ms"] == 1500 and "trace" not in event
    (exchange,) = [e for name, e in seen if name == "exchange"]
    assert exchange.parts is not None and exchange.parts["rounds"][0]["in"] == 9000


def test_the_parts_are_stored_with_the_turn(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.talk import Exchange, TalkService
    from tests.test_live_talk import _service

    session_id = uuid.uuid4()
    db.execute(
        "INSERT INTO talk_sessions (id, talker, voice, trigger, lang, started_at) "
        "VALUES (%s, 'gemini', 'Aoede', 'press', 'it-IT', now())",
        (session_id,),
    )
    make, _ = _service(db, None, NOW)  # type: ignore[arg-type]
    rounds = [{"ms": 900, "in": 8000, "out": 12}]
    parts: dict[str, Any] = {"context_ms": 10, "rounds": rounds, "model_ms": 900}

    async def go() -> None:
        pool = await make()
        service = TalkService(pool, None, {}, owner=owner_at(), clock=lambda: NOW)  # type: ignore[arg-type]
        await service._store_exchange(session_id, Exchange(1, NOW, 0.0, parts=parts))  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    (stored,) = db.execute("SELECT parts FROM talk_turns").fetchone()
    assert stored == parts


# --- Pisa goes deaf ----------------------------------------------------------------------------------------


def test_on_the_speaker_the_owner_s_turn_ends_eagerly_elsewhere_as_before() -> None:
    from google.genai import types

    from pisa.connectors import talker_gemini
    from pisa.ports.talker import TalkerConfig

    def detection(route: str) -> Any:
        cfg = TalkerConfig(key="k", voice="Aoede", lang="it-IT", instructions="", route=route)
        return talker_gemini.connect_config(cfg).realtime_input_config.automatic_activity_detection  # type: ignore[union-attr]

    speaker, airpods = detection("speaker"), detection("airpods")
    # start LOW on the speaker (Pisa's own voice), but its end HIGH and a shorter silence (never ended)
    assert speaker.start_of_speech_sensitivity is types.StartSensitivity.START_SENSITIVITY_LOW
    assert speaker.end_of_speech_sensitivity is types.EndSensitivity.END_SENSITIVITY_HIGH
    assert speaker.silence_duration_ms == talker_gemini.SPEAKER_SILENCE_MS < talker_gemini.SILENCE_MS
    assert airpods.end_of_speech_sensitivity is types.EndSensitivity.END_SENSITIVITY_LOW
    assert airpods.silence_duration_ms == talker_gemini.SILENCE_MS


def test_a_voice_the_talker_transcribes_is_never_ended_by_hand(monkeypatch: Any) -> None:
    from pisa.services import talk
    from tests.test_talk_timeline import VoiceLink

    monkeypatch.setattr(talk, "TICK_S", 0.01)
    monkeypatch.setattr(talk, "DEAF_AFTER_S", 0.05)
    monkeypatch.setattr(talk, "HAND_END_S", 0.15)
    run, session, _, _ = _run()
    run.link = VoiceLink()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        for _ in range(12):  # the user speaks on, and the talker hears them: words keep coming
            session.queue.put_nowait(OwnerWords("allora domani devo", final=False))
            await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    hand_ends = [e for e in run.timeline.events if e["kind"] == "hand_end"]  # The close's own
    assert not hand_ends and not [w for w in session.whispers if w.id == "unheard_voice"]  # end_turn is W10's


# --- Confirmations --------------------------------------------------------------------------------------


def _history_with_a_page(db: Any, client: Any, device: Any) -> None:
    """An earlier turn of the talk read a web page (outside text), restored whole by the history."""
    from psycopg.types.json import Jsonb
    from pydantic_ai.messages import ModelRequest, TextPart, ToolCallPart, ToolReturnPart

    from pisa.brain import history
    from pisa.brain.untrusted import data_block

    page = data_block("page", "Bagnoschiuma in offerta da Tigotà fino a domenica.", what="a web page")
    read = ToolReturnPart("read_page", f"{page.rule}\n{page.text}", tool_call_id="p1")
    raw = history.kept(
        [
            ModelRequest(parts=[]),
            ModelResponse(parts=[ToolCallPart("read_page", {}, tool_call_id="p1")]),
            ModelRequest(parts=[read]),
            ModelResponse(parts=[TextPart("Da Tigotà è in offerta.")]),
        ]
    )
    conversation, now = uuid.uuid4(), client.app.state.runtime.clock()
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, device[0], now, now),
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'user', "
        "'dove trovo il bagnoschiuma in offerta', 'it-IT', %s)",
        (conversation, now),
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at, model_messages) VALUES "
        "(%s, 'assistant', 'Da Tigotà è in offerta.', 'it-IT', %s, %s)",
        (conversation, now, Jsonb(raw)),
    )


def test_after_an_earlier_search_the_owner_s_own_request_is_done_without_a_yes(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from tests.assistant_env import add_task
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    device = pair(db)
    task = add_task(db, "Comprare il bagnoschiuma")
    _history_with_a_page(db, client, device)  # "dove trovo il bagnoschiuma in offerta" and a page read
    words = "spostalo a domani"
    model = Script({words: [[("reschedule_task", {"task_ref": "bagnoschiuma", "when": "tomorrow"})]]})
    fake_models.default = model
    play(client, db, secrets_key, model, words, device=device)
    # "non so per quale motivo tu hai bisogno del sì": the page was read in an earlier turn, the
    # owner named the task then and commands now
    (due,) = db.execute("SELECT due_at FROM tasks WHERE id = %s", (task,)).fetchone()
    assert due is not None


def test_a_change_the_owner_s_words_don_t_name_still_waits_and_asked_again_it_is_done(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    device = pair(db)
    _history_with_a_page(db, client, device)
    add = [("add_task", {"title": "Comprare il bagnoschiuma da Tigotà"})]
    model = Script({"sì aggiungilo": [add], "ti ho detto aggiungilo": [add]})
    fake_models.default = model
    play(client, db, secrets_key, model, "sì aggiungilo", device=device)
    # the page's words, not theirs: one yes
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
    play(client, db, secrets_key, model, "ti ho detto aggiungilo", device=device)
    # Their words never name it: the page's task still waits
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_the_change_pisa_asked_about_earlier_asked_again_naming_it_is_done(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from tests.assistant_env import add_task
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    device = pair(db)
    task = add_task(db, "Comprare il bagnoschiuma")
    _history_with_a_page(db, client, device)
    # the day comes from the page, not from their words: Pisa asks; asked again naming the task, it is their
    # yes
    move = [("reschedule_task", {"task_ref": "bagnoschiuma", "when": "2026-10-25"})]
    model = Script({"sposta il bagnoschiuma": [move], "ti ho detto di spostare il bagnoschiuma": [move]})
    fake_models.default = model
    play(client, db, secrets_key, model, "sposta il bagnoschiuma", device=device)
    assert db.execute("SELECT due_at FROM tasks WHERE id = %s", (task,)).fetchone() == (None,)
    play(client, db, secrets_key, model, "ti ho detto di spostare il bagnoschiuma", device=device)
    assert db.execute("SELECT due_at FROM tasks WHERE id = %s", (task,)).fetchone() != (None,)


def test_a_held_change_s_question_never_says_none() -> None:
    from pisa.brain.follow_tools import change_words

    title = "«OVHcloud»"
    assert change_words("it-IT", "reschedule", title=title, when="none") == "togliere la data a «OVHcloud»"
    assert change_words("en-US", "reschedule", title=title, when="None") == "clear the date of «OVHcloud»"
    assert "none" not in change_words("it-IT", "stop", what="«caffè»", when="None").casefold()


def test_one_approval_one_question_code_s_alone() -> None:
    from pisa.services.conversation import _without_questions  # pyright: ignore[reportPrivateUsage]

    reply = "Ho trovato il bagnoschiuma. Posso aggiungere il task «Bagnoschiuma»? Va bene."
    assert _without_questions(reply) == "Ho trovato il bagnoschiuma. Va bene."


def test_a_repeat_counts_only_when_the_owner_asks_it_himself() -> None:
    from pisa.domain.actions import asks_it

    assert (
        asks_it("ti ho detto aggiungilo") and asks_it("mettilo nei compiti") and asks_it("spostalo a domani")
    )
    assert not asks_it("che tempo fa domani?") and not asks_it("ok grazie mille, e poi?")
    # A yes alone names nothing; a no or a negation is never a request
    assert not asks_it("fallo") and not asks_it("ok grazie") and not asks_it("sì")
    assert not asks_it("no, non cancellarlo") and not asks_it("no, non aggiungerlo, aggiungi invece il latte")


# --- The last words; W11; the leftovers -----------------------------------------------------------


def test_a_thought_under_way_when_the_talk_closes_is_answered_after_it() -> None:
    run, session, _, _ = _run(Thought("Fatto: dentista domani alle 17.", Kind.DONE))
    release = asyncio.Event()
    think = run.hooks.think
    after: list[tuple[str, Any]] = []

    async def slow(words: str, trace: str, yes: bool) -> Thought:
        await release.wait()
        return await think(words, trace, yes)

    run.hooks.think = slow
    run.hooks.after = lambda words, thought: after.append((words, thought))

    async def go() -> Thought:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("mettimi il dentista domani alle 17", final=True))
        await _until(lambda: any(e["kind"] == "think" for e in run.timeline.events))
        run.end("owner", "phone", "press")  # the user closes while Pisa thinks
        await task
        ((words, thought),) = after
        assert words == "mettimi il dentista domani alle 17" and thought is not None
        release.set()
        return await thought  # never cancelled with the talk: its answer goes as a notice

    assert run_talk(go()).text == "Fatto: dentista domani alle 17."


def test_partial_words_when_the_press_came_are_answered_after_the_talk() -> None:
    run, session, _, _ = _run()
    after: list[tuple[str, Any]] = []
    run.hooks.after = lambda words, thought: after.append((words, thought))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("e ricordami di chiamare Marco", final=False))
        await _until(lambda: any(e["kind"] == "owner_words" for e in run.timeline.events))
        run.end("owner", "phone", "press")
        await task

    run_talk(go())
    assert after == [("e ricordami di chiamare Marco", None)]
    noise, session2, _, _ = _run()
    heard: list[Any] = []
    noise.hooks.after = lambda words, thought: heard.append(words)

    async def cough() -> None:
        task = asyncio.create_task(noise.run())
        session2.queue.put_nowait(OwnerWords("eh", final=False))
        await asyncio.sleep(0.05)
        noise.end("owner")
        await task

    run_talk(cough())
    assert heard == []  # a cough is no last word


def test_the_last_words_answer_goes_as_a_notice(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.talk import TalkService
    from tests.conftest import pair
    from tests.test_live_talk import _service

    device = uuid.UUID(str(pair(db)[0]))
    make, _ = _service(db, None, NOW)  # type: ignore[arg-type]

    async def think(words: str, trace: str, yes: bool) -> Thought:
        return Thought("Ti ricordo di chiamare Marco alle 18.", Kind.DONE)

    async def go() -> None:
        pool = await make()
        service = TalkService(pool, None, {}, owner=owner_at(), clock=lambda: NOW)  # type: ignore[arg-type]
        service._after(device, "it-IT", think, "e ricordami di chiamare Marco", None)  # pyright: ignore[reportPrivateUsage]
        await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    assert db.execute("SELECT kind, text, device_id FROM notifications").fetchall() == [
        ("info", "Ti ricordo di chiamare Marco alle 18.", device)
    ]


def test_the_not_done_line_is_in_the_first_person() -> None:
    from pisa.domain.whisper import NOT_DONE

    assert "ci sto ancora lavorando" in NOT_DONE["it-IT"] and "Pisa ci sta" not in NOT_DONE["it-IT"]
    assert "I'm still working" in NOT_DONE["en-US"]


def test_words_cut_mid_phrase_wait_for_the_rest(monkeypatch: Any) -> None:
    from pisa.services import talk

    monkeypatch.setattr(talk, "DANGLING_WAIT_S", 1.0)
    run, session, _, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("aggiungi un task chiamandolo", final=True))
        await asyncio.sleep(0.1)
        session.queue.put_nowait(OwnerWords("rinnova OVHcloud", final=True))
        await _until(lambda: any(name == "think" for name, _ in seen))
        run.end("owner")
        await task

    run_talk(go())
    assert [w for name, w in seen if name == "think"] == ["aggiungi un task chiamandolo … rinnova OVHcloud"]
    monkeypatch.setattr(talk, "DANGLING_WAIT_S", 0.05)
    alone, session2, _, seen2 = _run()

    async def cut() -> None:
        task = asyncio.create_task(alone.run())
        session2.queue.put_nowait(OwnerWords("aggiungi un task chiamandolo", final=True))
        await _until(lambda: any(name == "think" for name, _ in seen2))
        alone.end("owner")
        await task

    run_talk(cut())
    assert [w for name, w in seen2 if name == "think"] == ["aggiungi un task chiamandolo…"]  # Pisa asks
    assert not talk._dangling("aggiungi un task per la palestra.")  # pyright: ignore[reportPrivateUsage]


def test_one_request_proposed_twice_is_one_approval(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.approvals import ApprovalsService
    from tests.test_live_talk import _service

    make, _ = _service(db, None, NOW)  # type: ignore[arg-type]

    async def go() -> list[str]:
        pool = await make()
        approvals = ApprovalsService(pool, None, owner=owner_at(), clock=lambda: NOW)  # type: ignore[arg-type]
        ids = []
        for _ in range(2):  # the same question twice in one turn (two calls of one round)
            view = await approvals.create("calendar_cancel", "Cancello «Pranzo»?", [], {})
            ids.append(view["id"])
        other = await approvals.create("calendar_cancel", "Cancello «Cena»?", [], {})
        await pool.close()
        return [*ids, other["id"]]

    first, again, other = asyncio.run(go())
    assert first == again != other
    assert db.execute("SELECT count(*) FROM approvals").fetchone() == (2,)


def test_the_owner_s_words_close_the_delivery_s_task_and_tomorrow_s_wait(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """ "il pacco Amazon è arrivato", and the delivery stayed as tomorrow's event and an open task."""
    from tests.assistant_env import add_event, add_task, rome, set_settings
    from tests.conftest import pair
    from tests.test_done import run

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    pair(db)
    now = rome(2026, 10, 31, 18, 0)
    add_task(db, "Pacco Amazon")
    add_event(db, "Consegna pacco Amazon (resta a casa)", rome(2026, 11, 1, 9, 0), minutes=240)
    add_event(db, "Cena da Marco", rome(2026, 11, 1, 20, 0))
    out = run(db, secrets_key, now, lambda done, follow, env: done.mark("pacco Amazon"))
    assert {d["title"] for d in out["done"]} == {"Pacco Amazon", "Consegna pacco Amazon (resta a casa)"}
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)
    attended = db.execute("SELECT title FROM calendar_events WHERE attended_at IS NOT NULL").fetchall()
    assert attended == [("Consegna pacco Amazon (resta a casa)",)]  # never tomorrow's dinner
