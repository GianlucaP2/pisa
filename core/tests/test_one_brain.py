"""One brain that thinks while the user talks. The reflex on the user's partial words (a
small fast model, else the rules), speculation under the rails (Pisa's own reads at once, an outside read only
on a stable prefix, never a write), the ready answer checked against Pisa's data, and the shared conversation
state the talker and the main thinker both read and write."""

import asyncio
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from pisa.domain import speculation
from pisa.domain.reflex import RulesRead, rules_read
from pisa.domain.state import ConversationState, Fact, Ready, grounded
from pisa.domain.whisper import Kind, Priority
from pisa.ports.talker import AskBrain, OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services import reflex as reflex_service
from pisa.services.reflex import PisaReads, Reflexed, ReflexService
from pisa.services.talk import Thought
from tests.owner import owner_at
from tests.test_live_talk import _run, _until
from tests.virtual_time import run_talk

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
DEVICE = uuid.UUID(int=7)
AGENDA = "Saturday 2026-10-31. Events: 17:00-18:00 Dentista. Tasks due: no tasks due."


class Reads(PisaReads):
    """Pisa's reads, recorded: what the reflex ran and with what."""

    def __init__(self) -> None:
        super().__init__(None, None, None, owner=owner_at(), clock=lambda: NOW)
        self.ran: list[tuple[str, date, str | None]] = []

    async def run(self, read: str, *, device: uuid.UUID, day: date, place: str | None) -> str | None:
        self.ran.append((read, day, place))
        return {"agenda": AGENDA, "travel": "To Dentista: 12 min (drive, from google)."}.get(read)


class Scripted(ReflexService):
    """The reflex with a scripted read and draft (the model's part), the rest Pisa's own code."""

    def __init__(self, read: RulesRead, draft: str | None = None, by: str = "model") -> None:
        self.reads = Reads()
        super().__init__(None, self.reads, owner=owner_at(), clock=lambda: NOW)
        self.read, self.draft, self.by = read, draft, by

    async def _read(self, words: str, lang: str) -> tuple[RulesRead, str]:
        return self.read, self.by

    async def _answer(self, words: str, data: list[str], lang: str) -> str | None:
        return self.draft


async def passes(service: ReflexService, *words: str, gap: float = 0.0) -> list[Reflexed]:
    seen: list[Reflexed] = []

    async def on_read(r: Reflexed) -> None:
        seen.append(r)

    for said in words:
        service.heard(DEVICE, said, "it-IT", on_read=on_read)
        track = service._tracks.get(DEVICE)  # pyright: ignore[reportPrivateUsage]
        if track is not None and track.task is not None:
            await track.task
        await asyncio.sleep(gap)
    return seen


# --- speculation with the rails -----------------------------------------------------------------------------


def test_speculation_pisa_s_own_reads_on_partial_words_an_outside_read_only_on_a_stable_prefix(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(speculation, "STABLE_S", 0.15)
    read = RulesRead("question", "dal dentista", ["agenda", "travel", "send"], "tomorrow", "Dentista")
    service = Scripted(read)
    first, later = asyncio.run(
        passes(
            service, "quanto ci metto ad arrivare", "quanto ci metto ad arrivare dal dentista domani", gap=0.2
        )
    )
    # the first partial: Pisa's own agenda at once; the travel time (a paid call) waits; an unknown read never
    assert first.reads == ("agenda",) and set(first.held) == {"travel", "send"} and not first.stable
    # A paid read waits for the final words, even on a stable prefix (the place may be
    # still being said); only Pisa's own reads ran on the partial words
    assert [r for r, _, _ in service.reads.ran] == ["agenda"]
    assert later.stable and "travel" in later.held and "send" in later.held
    assert service.reads.ran[0][1] == date(2026, 10, 31)


def test_other_words_are_read_again_and_a_short_partial_says_nothing_yet() -> None:
    service = Scripted(RulesRead("question", "", ["agenda"], "today"))
    asyncio.run(passes(service, "cosa ho"))  # two words: nothing yet
    assert service.reads.ran == [] and DEVICE not in service._tracks  # pyright: ignore[reportPrivateUsage]
    asyncio.run(passes(service, "cosa ho oggi pomeriggio", "e domani invece cosa ho"))
    assert [r for r, _, _ in service.reads.ran] == ["agenda", "agenda"]  # other words: their own read


# --- the ready answer ---------------------------------------------------------------------------------------


def test_a_ready_answer_is_kept_only_when_pisa_s_data_holds_its_times(monkeypatch: Any) -> None:
    monkeypatch.setattr(speculation, "STABLE_S", 0.05)
    words = ("a che ora ho il dentista", "a che ora ho il dentista domani")
    right = Scripted(
        RulesRead("question", "l'ora del dentista", ["agenda"], "tomorrow"), "Domani alle 17:00."
    )
    asyncio.run(passes(right, *words, gap=0.1))
    state = right.state(DEVICE)
    assert state.ready is not None and state.ready.text == "Domani alle 17:00."
    assert "agenda: " + AGENDA in state.for_brain(NOW, words[1]) and "Domani alle 17:00." in state.for_brain(
        NOW, words[1]
    )
    wrong = Scripted(
        RulesRead("question", "l'ora del dentista", ["agenda"], "tomorrow"), "Domani alle 16:30."
    )
    asyncio.run(passes(wrong, *words, gap=0.1))
    assert wrong.state(DEVICE).ready is None  # RAIL: a time Pisa's data doesn't hold is never said
    rules = Scripted(RulesRead("question", "", ["agenda"], "tomorrow"), "Domani alle 17:00.", by="rules")
    asyncio.run(passes(rules, *words, gap=0.1))
    assert rules.state(DEVICE).ready is None  # the rules read the words; only the model drafts
    change = Scripted(RulesRead("change", "", ["agenda"], "tomorrow"), "Domani alle 17:00.")
    asyncio.run(passes(change, "spostami il dentista", "spostami il dentista domani alle 18", gap=0.1))
    assert change.state(DEVICE).ready is None  # a change is the main thinker's, on the final words
    assert grounded("Alle 17.00, 12 minuti", ["17:00-18:00 Dentista", "12 min"]) and not grounded(
        "alle 9", []
    )


# --- the reflex model, its budget, the rules ----------------------------------------------------------------


class Gateway:
    def __init__(self, delay: float, out: Any) -> None:
        self.delay, self.out, self.slots = delay, out, []

    async def run(self, slot: str, call: Any, **_: Any) -> Any:
        self.slots.append(slot)
        await asyncio.sleep(self.delay)
        return self.out


def test_the_reflex_model_reads_the_words_within_its_budget_else_the_rules() -> None:
    from pisa.brain.reflex import ReflexRead

    out = ReflexRead(intent="question", goal="il dentista", reads=["agenda"], day="tomorrow")
    fast = ReflexService(Gateway(0.0, out), Reads(), owner=owner_at(), clock=lambda: NOW, budget_s=0.2)  # type: ignore[arg-type]
    read, by = run_talk(fast._read("a che ora ho il dentista", "it-IT"))  # pyright: ignore[reportPrivateUsage]
    assert by == "model" and read.reads == ["agenda"] and read.goal == "il dentista"
    slow = ReflexService(Gateway(0.5, out), Reads(), owner=owner_at(), clock=lambda: NOW, budget_s=0.1)  # type: ignore[arg-type]

    async def timed() -> tuple[Any, float]:  # on the loop's clock: a loaded machine's wall overran the window
        loop = asyncio.get_running_loop()
        started = loop.time()
        got = await slow._read("domani ho il dentista?", "it-IT")  # pyright: ignore[reportPrivateUsage]
        return got, loop.time() - started

    (read, by), took = run_talk(timed())
    # A read past the window is the model's, cut ("slow"); the rules read the words
    assert by == "slow" and read.reads == ["agenda"] and read.day == "tomorrow"
    assert took < 0.4  # the turn never waits on a slow reflex


def test_the_reflex_s_two_calls_build_against_a_model() -> None:
    from pydantic_ai.models.test import TestModel

    from pisa.brain import reflex

    read, _ = asyncio.run(reflex.read(TestModel(), "cosa ho domani", "it-IT", "Friday 2026-10-30 16:00"))
    assert read.intent in ("question", "change", "chat", "unclear")
    answer, _ = asyncio.run(reflex.answer(TestModel(), "cosa ho domani", [AGENDA], "it-IT", "Friday 16:00"))
    assert answer is None or isinstance(answer, str)


def test_the_rules_read_italian_and_english() -> None:
    assert rules_read("domani ho il dentista?") == RulesRead(
        "question", "domani ho il dentista", ["agenda"], "tomorrow"
    )
    assert rules_read("quali attività ho ancora").reads == ["tasks"]
    assert rules_read("metti la cena domani alle 20").intent == "change"
    assert rules_read("what's next on my plan").reads == ["plan"]
    assert rules_read("ciao").intent == "unclear"


# --- the shared conversation state --------------------------------------------------------------------------


def test_pisa_s_voice_lines_and_promises_are_in_the_state_and_the_brain_reads_each_line_once() -> None:
    state = ConversationState()
    state.pisa_said("Ti avviso alle 5 per la call.", "voice", NOW)
    state.pisa_said("Va bene.", "voice", NOW)
    seen = state.for_brain(NOW, "e la cena?")
    assert (
        '"Ti avviso alle 5 per la call." / "Va bene."' in seen and 'Pisa promised: "Ti avviso alle 5' in seen
    )
    state.brain_read()
    again = state.for_brain(NOW, "e la cena?")
    assert "voice already said" not in again and "Pisa promised" in again  # the promise stays two hours
    assert "Pisa promised" not in state.for_brain(NOW + timedelta(hours=3), "e la cena?")
    state.set_waiting(["Spostare la cena alle 21"])
    # C9: what waits and Pisa's data are data in «…» for the talker
    assert "Waiting for the user's yes (NOT done): «Spostare la cena alle 21»" in state.for_talker(NOW)


PIZZA = "Tra venti minuti ti ricordo di tirare fuori la pizza dal forno."


def test_a_new_conversation_starts_without_the_old_one_s_promises() -> None:
    """Quality audit §24 f12-it-1: after the driver's reset (a new conversation, the reminder cancelled), a
    small-talk turn said "Tra sedici minuti ti ricordo… pizza": the device's state still held the old
    conversation's promise. A new conversation starts without what Pisa said or promised in the one before;
    the reflex's reads for the words being said stay."""
    state = ConversationState()
    state.begin("c1")
    state.pisa_said(PIZZA, "brain", NOW)
    state.add_fact(Fact("agenda", AGENDA, NOW, "ciao come stai"))
    assert "pizza" in state.for_brain(NOW, "ciao come stai")
    state.begin("c1")  # the same conversation: kept
    assert "Pisa promised" in state.for_brain(NOW, "ciao come stai")
    state.begin("c2")
    seen = state.for_brain(NOW, "ciao come stai")
    assert "pizza" not in seen and "Pisa promised" not in seen and AGENDA in seen
    assert "pizza" not in state.for_talker(NOW)


@pytest.mark.parametrize(
    ("line", "live", "kept"),
    [
        (PIZZA, ["tirare fuori la pizza dal forno"], True),  # still to come
        (PIZZA, [], False),  # cancelled or fired: no reminder holds it
        (PIZZA, ["chiamare il dentista"], False),
        ("I'll remind you to take the pizza out at 18:36.", [], False),
        ("Ti faccio sapere appena arriva.", [], True),  # no reminder: nothing stored backs or denies it
        # E1 PR-1: one shared word ("pizza", "domani") with an unrelated reminder holds nothing
        (PIZZA, ["comprare la pizza surgelata"], False),
        ("Ti ricordo domani di chiamare Marco.", ["domani alle 9 chiamare il dentista"], False),
        ("Ti ricordo domani di chiamare Marco.", ["chiamare Marco"], True),
        (
            "Ho aggiunto il latte. Domani ti ricordo di comprarlo al mercato.",
            ["comprare il latte al mercato"],
            True,
        ),
        # E1 PR-2: every reminding phrasing the promise pattern stores is checked
        ("Te lo ricorderò.", [], False),
        ("Te lo ricorderò.", ["pagare la bolletta"], True),
        ("Ti avviso alle 18 per la bolletta.", [], False),
        ("Ti avviso alle 18 per la bolletta.", ["pagare la bolletta"], True),
        ("Ti mando un promemoria per la bolletta stasera.", [], False),
        ("I'll let you know at 6 about the bill.", [], False),
        ("Ti avviso appena arriva.", [], True),  # no time: no reminder
    ],
)
def test_a_reminder_promised_is_pending_only_while_one_still_to_come_holds_it(
    line: str, live: list[str], kept: bool
) -> None:
    state = ConversationState()
    state.pisa_said(line, "brain", NOW)
    state.begin("c1", live)
    assert ("Pisa promised" in state.for_brain(NOW, "ciao")) is kept
    assert ("You promised" in state.for_talker(NOW)) is kept


@pytest.mark.parametrize("cancelled", [True, False])
def test_a_cancelled_reminder_is_absent_from_the_turn_s_instructions(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, cancelled
) -> None:
    """The row the reset cancelled (`cancelled_at`): the turn's instructions never hold it as pending. A
    reminder still to come stays."""
    from tests.conftest import pair
    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    device = pair(db)
    runtime = client.app.state.runtime
    now = runtime.clock()
    db.execute(
        "INSERT INTO pisa_promises (id, what, condition, due_at, lang, provenance, created_at, cancelled_at) "
        "VALUES (%s, 'tirare fuori la pizza dal forno', 'time', %s, 'it-IT', 'owner_words', %s, %s)",
        (uuid.uuid4(), now + timedelta(minutes=16), now - timedelta(minutes=4), now if cancelled else None),
    )
    runtime.reflex.state(uuid.UUID(device[0])).pisa_said(PIZZA, "brain", now)
    model = Heard()
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, "Ciao! Come stai oggi?", device=device)  # type: ignore[arg-type]
    assert ("pizza" in model.instructions[0]) is not cancelled


def test_the_main_thinker_reads_the_shared_state_after_the_cached_prefix(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from pisa.brain.conversation import system_prompt
    from tests.conftest import pair
    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    device = pair(db)
    runtime = client.app.state.runtime
    state = runtime.reflex.state(uuid.UUID(device[0]))
    words = "a che ora ho il dentista domani"
    now = runtime.clock()
    state.add_fact(Fact("agenda", AGENDA, now, words))
    state.set_ready(Ready("Domani alle 17:00.", words, now))
    state.pisa_said("Un attimo, controllo e ti dico.", "voice", now)
    model = Heard()
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, words, "grazie", device=device)  # type: ignore[arg-type]
    first, second = model.instructions[0], model.instructions[-1]
    assert first.startswith(system_prompt(lang="it-IT"))  # the cached prefix stays byte-identical
    shared = first[first.index("The conversation now") :]
    assert (
        "<day-context-" in shared
        and AGENDA in shared
        and 'reflex drafted from it: "Domani alle 17:00."' in shared
    )
    assert '"Un attimo, controllo e ti dico."' in shared and "Pisa promised" in shared
    # the next turn: other words (no reads for them), Pisa's voice line already read; Pisa's reply is in the
    # state
    assert AGENDA not in second and "voice already said" not in second
    assert any(s.by == "brain" for s in state.said)


def test_economy_s_partial_words_reach_the_reflex(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair, set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    reflex = client.app.state.runtime.reflex
    with live(client, token, device_id, turn_hints=True) as app:  # partial transcripts (economy)
        app.partial("domani ho il dentista", str(uuid.uuid4()))
        for _ in range(300):
            if reflex.state(uuid.UUID(device_id)).goal:
                break
            time.sleep(0.01)
    state = reflex.state(uuid.UUID(device_id))
    assert state.goal and state.goal_words == "domani ho il dentista"


# --- the talk: Pisa's voice and the shared state
# ---------------------------------------------------------------


def _ready(text: str) -> Any:
    return lambda words: text if "dentista" in words else None


def test_the_ready_answer_is_said_at_the_final_and_the_brain_s_same_answer_goes_in_silently() -> None:
    run, session, _, seen = _run(Thought("Domani alle 17:00 hai il dentista.", Kind.SAY))
    run.hooks.ready = _ready("Domani alle 17:00 hai il dentista.")
    brain = asyncio.Event()
    think = run.hooks.think

    async def slow(words: str, trace: str, yes: bool) -> Thought:
        await brain.wait()
        return await think(words, trace, yes)

    run.hooks.think = slow

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(AskBrain("c1", "a che ora è il dentista"))
        session.queue.put_nowait(OwnerWords("a che ora ho il dentista domani?", final=True))
        await _until(lambda: session.answers)  # the reflex's answer, while the brain still works
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Domani alle 17:00 hai il dentista.", final=True))
        session.queue.put_nowait(Speaking(False))
        await _until(lambda: any(e["kind"] == "pisa_said" for e in run.timeline.events))
        brain.set()
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(go())
    ((call, ready),) = session.answers
    assert (
        call == "c1" and ready.id == "reflex1" and ready.kind is Kind.SAY and ready.priority is Priority.PAUSE
    )
    (brain_answer,) = session.whispers
    assert brain_answer.priority is Priority.SILENT  # Pisa's voice already said it: not twice
    (exchange,) = [e for name, e in seen if name == "exchange"]
    assert exchange.first_content_by == "reflex" and exchange.first_content_ms is not None
    assert next(e for e in run.timeline.events if e["kind"] == "first_content")["by"] == "reflex"
    assert run.incidents == 0


def test_a_brain_faster_than_pisa_s_voice_never_says_the_ready_answer_twice() -> None:
    run, session, _, _ = _run(Thought("Domani alle 17:00 hai il dentista.", Kind.SAY))
    run.hooks.ready = _ready("Domani alle 17:00 hai il dentista.")

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho il dentista domani?", final=True))
        # the ready answer, then the brain's: before Pisa spoke
        await _until(lambda: len(session.whispers) == 2)
        run.end("owner")
        await task

    run_talk(go())
    ready, brain = session.whispers
    assert ready.id == "reflex1" and ready.priority is Priority.PAUSE
    assert brain.priority is Priority.SILENT  # the same answer: Pisa's voice has it, never twice


def test_a_call_after_the_ready_answer_gets_it_at_once_and_a_bare_yes_never_does() -> None:
    run, session, _, _ = _run()
    run.hooks.ready = _ready("Domani alle 17:00 hai il dentista.")
    brain = asyncio.Event()

    async def slow(words: str, trace: str, yes: bool) -> Thought:
        await brain.wait()
        return Thought("Domani alle 17:00 hai il dentista.", Kind.SAY)

    run.hooks.think = slow

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho il dentista domani?", final=True))
        await _until(lambda: session.whispers)  # no call yet: at the pause
        session.queue.put_nowait(AskBrain("c2", "dentista"))
        await _until(lambda: session.answers)  # the call that came after: the ready answer, at once
        brain.set()
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert session.whispers[0].id == "reflex1" and session.whispers[0].priority is Priority.PAUSE
    assert session.answers[0] == ("c2", session.whispers[0])

    yes_run, yes_session, _, _ = _run()
    yes_run.hooks.ready = lambda words: "Domani alle 17:00 hai il dentista."

    async def yes() -> None:
        task = asyncio.create_task(yes_run.run())
        yes_session.queue.put_nowait(OwnerWords("sì", final=True))
        await asyncio.sleep(0.1)
        yes_run.end("owner")
        await task

    run_talk(yes())
    assert not any(w.id.startswith("reflex") for w in yes_session.whispers)  # a yes is code's


def test_pisa_s_voice_may_state_what_the_shared_state_holds() -> None:
    def said_before_the_brain(supports: list[str]) -> int:
        run, session, _, _ = _run()
        run.hooks.supports = lambda: supports
        brain = asyncio.Event()

        async def slow(words: str, trace: str, yes: bool) -> Thought:
            await brain.wait()
            return Thought("Domani alle 17:00 hai il dentista.", Kind.SAY)

        run.hooks.think = slow

        async def go() -> None:
            task = asyncio.create_task(run.run())
            session.queue.put_nowait(OwnerWords("a che ora ho il dentista domani?", final=True))
            session.queue.put_nowait(PisaWords("Il dentista è alle 17:00.", final=True))
            await _until(lambda: any(e["kind"] == "pisa_said" for e in run.timeline.events))
            await asyncio.sleep(0.05)
            brain.set()
            await asyncio.sleep(0.05)
            run.end("owner")
            await task

        run_talk(go())
        return run.incidents

    assert said_before_the_brain([AGENDA]) == 0  # Pisa's own data the reflex read: Pisa's voice may say it
    assert said_before_the_brain([]) == 1  # the same time out of nowhere: an incident


def test_the_reflex_s_reads_reach_pisa_s_voice_as_one_silent_whisper_and_its_lines_reach_the_state() -> None:
    run, session, _, _ = _run()
    state = ConversationState()
    state.add_fact(Fact("agenda", AGENDA, NOW, "domani ho il dentista"))
    run.hooks.state = lambda: state.for_talker(NOW)
    lines: list[tuple[str, bool]] = []
    run.hooks.said = lambda text, backed, retried: lines.append((text, backed))
    read = Reflexed("domani ho il dentista", "model", 180, "question", ("agenda",), (), False, None)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.reflexed(read)
        await run.reflexed(read)  # the same state: not again
        session.queue.put_nowait(PisaWords("Ti dico subito.", final=True))
        await _until(lambda: lines)
        run.end("owner")
        await task

    run_talk(go())
    (whisper,) = session.whispers
    assert whisper.kind is Kind.FACT and whisper.priority is Priority.SILENT and AGENDA in whisper.text
    event = next(e for e in run.timeline.events if e["kind"] == "reflex")
    assert event == {**event, "by": "model", "took_ms": 180, "intent": "question", "reads": ["agenda"]}
    assert lines == [("Ti dico subito.", False)]


def test_the_talk_service_shares_one_state_per_device_with_the_main_thinker() -> None:
    from pisa.services.talk import Hooks, TalkService

    service = ReflexService(None, Reads(), owner=owner_at(), clock=lambda: NOW)
    talk = TalkService(None, None, {}, owner=owner_at(), clock=lambda: NOW, reflex=service)  # type: ignore[arg-type]
    run, _, _, _ = _run()
    hooks: Hooks = run.hooks
    talk._shared(hooks, run, DEVICE, "it-IT")  # pyright: ignore[reportPrivateUsage]
    hooks.said("Ti ricordo la call alle 5.", False, False)
    state = service.state(DEVICE)
    assert state.said[-1].by == "voice" and state.promised
    state.set_ready(Ready("Domani alle 17:00.", "a che ora ho il dentista", NOW))
    assert hooks.ready("a che ora ho il dentista domani?") == "Domani alle 17:00."
    assert hooks.ready("e la cena?") is None  # other words: not their answer


def test_the_turn_s_first_content_time_is_stored(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.talk import Exchange, TalkService
    from tests.test_live_talk import _service

    session_id = uuid.uuid4()
    db.execute(
        "INSERT INTO talk_sessions (id, talker, voice, trigger, lang, started_at) "
        "VALUES (%s, 'gemini', 'Aoede', 'press', 'it-IT', now())",
        (session_id,),
    )
    make, _ = _service(db, None, NOW)  # type: ignore[arg-type]

    async def go() -> None:
        pool = await make()
        service = TalkService(pool, None, {}, owner=owner_at(), clock=lambda: NOW)  # type: ignore[arg-type]
        e = Exchange(
            1, NOW, 0.0, first_content_ms=420, first_content_by="reflex", reflex_ms=210, reflex_by="slow"
        )
        await service._store_exchange(session_id, e)  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    row = db.execute(
        "SELECT first_content_ms, first_content_by, reflex_ms, reflex_by FROM talk_turns"
    ).fetchone()
    assert row == (420, "reflex", 210, "slow")


@pytest.mark.parametrize("words", ["", "sì"])
def test_nothing_ready_for_no_words(words: str) -> None:
    assert not speculation.follows("a che ora ho il dentista", words)


def test_reflex_logs_never_carry_the_words() -> None:
    import structlog

    service = Scripted(RulesRead("question", "", ["agenda"], "tomorrow"))
    with structlog.testing.capture_logs() as logs:
        run_talk(passes(service, "a che ora ho il dentista domani"))
    (line,) = [entry for entry in logs if entry["event"] == "reflex"]
    assert "dentista" not in str(line) and line["chars"] == len("a che ora ho il dentista domani")
    assert reflex_service.TARGET_MS == 300
