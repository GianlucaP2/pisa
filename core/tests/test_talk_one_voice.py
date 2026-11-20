"""One voice, one answer, no echo. The five patterns of a day's talks (Gemini
Live, AirPods), each as a synthetic talk scenario with the timings seen: two voices, Pisa's echo taken for
their voice and words, "non ho sentito" after the answer, the talker's own cause and promise, and their yes to
the last question. Every word here is synthetic."""

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from pisa.domain.whisper import Kind, Priority, Whisper
from pisa.ports.talker import AskBrain, OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services import talk
from pisa.services.talk import Thought
from tests.test_live_talk import FakeLink, _run, _until
from tests.test_talk_timeline import LOUD, QUIET, first, kinds
from tests.virtual_time import run_talk

PISA_LINE = "Certo, un attimo: ho segnato la spesa come fatta."


class Phone(FakeLink):
    """The phone's audio keeps flowing (20 ms frames): `voice` loud (the user speaks) or quiet (they don't;
    only Pisa's own voice may come back, under the phone's echo cancellation)."""

    def __init__(self, voice: bool) -> None:
        super().__init__()
        self.voice = voice

    async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
        while True:
            yield LOUD if self.voice else QUIET
            await asyncio.sleep(0.005)


def _talking(voice: bool) -> tuple[talk.TalkRun, Any, Phone, list[Any]]:
    run, session, _, seen = _run()
    phone = Phone(voice)
    run.link = phone
    return run, session, phone, seen


async def _pisa_speaking(run: talk.TalkRun, session: Any, phone: Phone, line: str = PISA_LINE) -> None:
    session.queue.put_nowait(Speaking(True))
    session.queue.put_nowait(PisaWords(line, final=False))
    phone.buffered_ms = 800  # Pisa's line is playing on the phone
    await _until(lambda: run._pisa_line == line)  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(1.05)  # the phone's audio has flowed a whole second (BARGE_VOICE_S)


# --- P2: a barge-in needs their voice; a final that is Pisa's own line is not their words
# --------------------------


def test_the_talker_s_start_of_speech_with_no_voice_in_the_phone_s_audio_never_cuts_pisa() -> None:
    """AirPods: `barge_in by='voice'` three times in 10 s right after "Fatto: …" started, their voice
    silent. The phone's audio carried no voice: Pisa's own voice came back; Pisa's line plays on."""
    run, session, phone, _ = _talking(voice=False)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await _pisa_speaking(run, session, phone)
        session.queue.put_nowait(OwnerSpeaking(True))  # Gemini's `interrupted` on Pisa's own voice
        await _until(lambda: "barge_in" in kinds(run))
        run.end("owner")
        await task

    run_talk(go())
    assert first(run, "barge_in")["by"] == "echo"
    assert phone.cleared == 0  # Pisa's line was never cut on the phone

    spoken, spoken_session, spoken_phone, _ = _talking(voice=True)  # their voice in the phone's audio

    async def barge() -> None:
        task = asyncio.create_task(spoken.run())
        await _pisa_speaking(spoken, spoken_session, spoken_phone)
        spoken_session.queue.put_nowait(OwnerSpeaking(True))
        await _until(lambda: "barge_in" in kinds(spoken))
        spoken.end("owner")
        await task

    run_talk(barge())
    assert first(spoken, "barge_in")["by"] == "voice" and spoken_phone.cleared == 1


@pytest.mark.parametrize("echo", ["Certo.", "Attimi.", "Danke."])
def test_one_word_after_a_barge_in_that_is_pisa_s_own_line_is_no_turn(echo: str) -> None:
    """Pisa's line came back as his one-word finals ("certo", "attimi", a word in another language);
    each became a turn (and "certo", a bare yes, answered a held question). They are Pisa's echo: dropped."""
    run, session, phone, seen = _talking(voice=True)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await _pisa_speaking(run, session, phone)
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerWords(echo, final=True))
        await _until(lambda: "echo" in kinds(run) or "think" in kinds(run))
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert first(run, "echo")["text"] == echo
    assert "owner_final" not in kinds(run) and not [s for s in seen if s[0] == "think"]


def test_his_own_one_word_answer_and_stop_still_count() -> None:
    """Never too much: his "sì" to a line that asked him, and his "basta", are his (his voice was there)."""
    for line, word in (("Posso segnare la spesa come fatta?", "Sì."), (PISA_LINE, "Basta.")):
        run, session, phone, _ = _talking(voice=True)

        async def go(r: talk.TalkRun = run, s: Any = session, p: Phone = phone, ln: str = line,
                     w: str = word) -> None:  # fmt: skip
            task = asyncio.create_task(r.run())
            await _pisa_speaking(r, s, p, ln)
            s.queue.put_nowait(OwnerSpeaking(True))
            s.queue.put_nowait(OwnerWords(w, final=True))
            await _until(lambda: "owner_final" in kinds(r) or "echo" in kinds(r))
            r.end("owner")
            await task

        run_talk(go())
        assert "echo" not in kinds(run) and first(run, "owner_final")["text"] == word, word


# --- P3: "didn't hear you" only when nothing of theirs came
# ----------------------------------------------------


def test_a_call_made_as_his_words_begin_waits_for_their_final(monkeypatch: Any) -> None:
    """ask_brain as his words began (+0 s), his final at +3.9 s, ASK_WAIT_S 3 s: Pisa sent "ask them to
    repeat" and then answered. While their words arrive (their voice in the phone's audio), the call waits for
    them (scaled: 0.3 s, 0.39 s)."""
    monkeypatch.setattr(talk, "ASK_WAIT_S", 0.3)
    run, session, _, _ = _talking(voice=True)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerWords("ricordami di", final=False))  # +0 s
        session.queue.put_nowait(AskBrain("c1", "un promemoria"))
        session.queue.put_nowait(OwnerSpeaking(False))  # Gemini: the talker's call ends their speech
        await asyncio.sleep(0.39)
        session.queue.put_nowait(OwnerWords("ricordami di chiamare Anna domani", final=True))  # +3.9 s
        await _until(lambda: session.answers)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert [w.id for _, w in session.answers] == ["w1"]  # the brain's answer, never "unheard"
    assert session.answers[0][1].text == "Domani alle 9 hai il dentista."


def test_with_nothing_of_his_the_call_still_hears_unheard(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "ASK_WAIT_S", 0.1)
    run, session, _, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(AskBrain("c1", "cosa ha detto"))
        await _until(lambda: session.answers)
        run.end("owner")
        await task

    run_talk(go())
    assert [w.id for _, w in session.answers] == ["unheard"]


# --- P4: the talker says holds and Pisa's whispers only
# --------------------------------------------------------


def _slow(run: talk.TalkRun, thought: Thought) -> None:
    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        await asyncio.sleep(0.3)  # the brain still works while the talker speaks
        return thought

    run.hooks = dataclasses.replace(run.hooks, think=think)


def test_the_talker_s_own_cause_and_promise_are_taken_back_once() -> None:
    run, session, _, _ = _run()
    _slow(run, Thought("Ho segnato la spesa come fatta.", Kind.DONE))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("segna la spesa come fatta", final=True))
        session.queue.put_nowait(
            PisaWords("Ho avuto un problema col sistema vocale che ha interrotto la risposta.", final=True)
        )
        session.queue.put_nowait(
            PisaWords("Cercherò di risolvere questo malfunzionamento subito.", final=True)
        )
        await _until(lambda: kinds(run).count("own_words") == 2)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    owns = [e["own"] for e in run.timeline.events if e["kind"] == "own_words"]
    assert owns == ["cause", "promise"] and run.incidents == 2
    fixes = [w for w in session.whispers if w.id == "own"]
    assert len(fixes) == 1  # once per final words
    assert fixes[0].kind is Kind.CORRECT and fixes[0].priority is Priority.NOW


def test_the_talker_s_own_confirmation_is_corrected_when_nothing_was_done() -> None:
    """ "Sì, ho appena confermato il promemoria…" before the brain answered: no `done` came: corrected."""
    run, session, _, _ = _run(Thought("Vuoi che ti ricordi la spesa alle 18?", Kind.ASK))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("ricordami la spesa", final=True))
        session.queue.put_nowait(PisaWords("Sì, ho appena confermato il promemoria per stasera.", final=True))
        await _until(lambda: any(w.id == "fix" for w in session.whispers))
        run.end("owner")
        await task

    run_talk(go())
    assert first(run, "grounding")["done_without_done"] is True


def test_own_words_carried_by_a_whisper_or_his_words_pass() -> None:
    from pisa.domain.whisper import own_words

    given = [Whisper(Kind.SAY, Priority.PAUSE, "Il calendario ha un problema: ti avviserò alle 9.", id="w1")]
    assert own_words("Il calendario ha un problema: ti avviserò alle 9.", given) is None
    assert own_words("Che problema?", [], owner_words="ho un problema con la sveglia") is None
    assert own_words("Un attimo, controllo.", []) is None
    assert own_words("Ti farò sapere appena posso.", []) == "promise"


# --- P5: a line is said once; their yes answers the last question
# ---------------------------------------------


def test_a_whole_line_started_again_for_the_same_words_is_cut() -> None:
    """ "Non ci sono riuscito" three times, once per barge-in. A whole line Pisa already said for these
    words, said whole again with no new whisper since, is cut there; the phone hears no more of it."""
    run, session, link, _ = _run()
    line = "Non ci sono riuscito."  # 4 words (exactly LINE_AGAIN_WORDS)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("segna la spesa come fatta", final=True))
        await _until(lambda: "whisper" in kinds(run))  # the brain's answer came before Pisa's line
        session.queue.put_nowait(PisaWords(line, final=True))
        await _until(lambda: "pisa_said" in kinds(run))
        session.queue.put_nowait(Speaking(True))
        link.buffered_ms = 300
        session.queue.put_nowait(PisaWords("Non ci sono", final=False))
        session.queue.put_nowait(PisaWords(line, final=False))
        await _until(lambda: "pisa_again" in kinds(run))
        run.end("owner")
        await task

    run_talk(go())
    assert session.stopped == 1 and link.cleared == 1

    retried, retried_session, _, _ = _run()  # a line cut off (`interrupted`) is said whole again

    async def again() -> None:
        task = asyncio.create_task(retried.run())
        retried_session.queue.put_nowait(OwnerWords("segna la spesa come fatta", final=True))
        retried_session.queue.put_nowait(PisaWords("Non ci sono riuscito a", final=True, interrupted=True))
        retried_session.queue.put_nowait(PisaWords(line, final=False))
        await _until(lambda: retried._pisa_line == line)  # pyright: ignore[reportPrivateUsage]
        retried.end("owner")
        await task

    run_talk(again())
    assert "pisa_again" not in kinds(retried) and retried_session.stopped == 0


def test_his_yes_to_a_held_change_makes_it_when_the_model_asks_the_tool_again(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    """Pisa asked "Posso segnare «…» come fatto?" (a change held after outside text), he said "Sì.", the
    model called mark_done again, and it was refused as "waiting for your answer to another question": the
    question waiting was that very change. Their yes is for it: it is made."""
    from tests.assistant_env import add_task
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play
    from tests.test_talking_works import _history_with_a_page  # pyright: ignore[reportPrivateUsage]

    device = pair(db)
    task = add_task(db, "Spesa al mercato")
    _history_with_a_page(db, client, device)  # an earlier turn read a page: a change waits for their yes
    done = [("mark_done", {"what": "Spesa al mercato"})]
    model = Script({"ok": [done], "Sì.": [done]})
    fake_models.default = model
    (frames,), _, _ = play(client, db, secrets_key, model, "ok", device=device)
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("open",)  # held
    asked = "".join(f.get("delta") or "" for f in frames if f["type"] == "say")
    assert "Posso segnare «Spesa al mercato» come fatto?" in asked
    play(client, db, secrets_key, model, "Sì.", device=device)
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("done",)


def test_a_held_question_is_said_whole_in_a_talk() -> None:
    """On AirPods Pisa's voice says one sentence of a reply; a held write's question ("Vuoi che ricordi…?") is
    Pisa's question and is said whole (a yes answers only what Pisa said aloud)."""
    from pisa.services.conversation import ConversationService, TurnState

    device = uuid.uuid4()
    service = ConversationService.__new__(ConversationService)
    service._last_turn = {device: TurnState(uuid.uuid4(), "mi piace il sushi", profile_asked=True)}  # pyright: ignore[reportPrivateUsage]
    assert service.asks_owner(device)


# --- P1: one voice -----------------------------------------------------------------------------------------


class _Run:
    def __init__(self) -> None:
        self.timeline = talk.Timeline()
        self.said: list[tuple[str, str]] = []

    async def proactive(self, text: str, speak: str) -> None:
        self.said.append((text, speak))

    def link_silent(self) -> bool:
        return False  # the talk's audio is flowing


def test_a_spoken_utterance_during_a_talk_is_never_answered_by_a_second_voice(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, monkeypatch
) -> None:
    """19 `voice.speak` rows during talks. The phone's own recognizer words reach Pisa on the socket while
    a talk is open: not answered there (no `say` words, no audio), only an empty final reply."""
    from tests.conftest import pair, set_key
    from tests.live_app import live
    from tests.test_brain_loop import Script

    set_key(db, secrets_key)
    device_id, token = pair(db)
    model = Script({"che ore sono": []})
    fake_models.default = model
    fake = _Run()
    monkeypatch.setattr(client.app.state.runtime.talk, "run_for", lambda device: fake)
    with live(client, token, device_id) as app:
        app.say("che ore sono", lang="it-IT", source="earbuds_mic")
        reply = app.next()
    assert reply["type"] == "say" and reply["final"] and reply["delta"] == ""
    assert model.calls == 0
    assert [e["kind"] for e in fake.timeline.events] == ["ws_utterance"]


def test_a_notice_during_a_talk_is_said_in_the_talk_never_by_a_second_voice(monkeypatch: Any) -> None:
    """A notice Pisa would say aloud (companion mode with AirPods) while a talk is open: said in the talk, its
    `notify` shown with `voice: false`, and no server audio for it."""
    from pisa.ports.push import OutgoingNotification
    from pisa.services import session_registry

    class Voice:
        async def spoken_aloud(self, device_id: uuid.UUID) -> bool:
            return True

    class Session:
        closed = False
        audio_formats = frozenset({"pcm_s16le"})
        last_unprompted: Any = None

        def __init__(self) -> None:
            self.device = SimpleNamespace(id=uuid.uuid4())
            self.runtime = SimpleNamespace(pool=None, clock=lambda: None)
            self.sent: list[Any] = []
            self.run = _Run()

        def talking(self) -> Any:
            return self.run

        async def send(self, message: Any) -> bool:
            self.sent.append(message)
            return True

    async def marked(*args: Any) -> None:
        return None

    monkeypatch.setattr(session_registry.notifications, "mark_spoken", marked)
    spoken: list[Any] = []

    async def speak(self: Any, voice: Any, session: Any, notification: Any) -> None:
        spoken.append(notification.id)

    monkeypatch.setattr(session_registry.SessionRegistry, "_speak", speak)
    registry = session_registry.SessionRegistry(voice=Voice())  # type: ignore[arg-type]
    session = Session()
    registry._live[session.device.id] = session  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    notice = OutgoingNotification(str(uuid.uuid4()), "info", "Fatto: segnata la spesa.", "it-IT", "normal")

    async def go() -> bool:
        sent = await registry.notify(session.device.id, notice)
        await asyncio.sleep(0.01)
        return sent

    assert run_talk(go())
    assert session.run.said == [("Fatto: segnata la spesa.", "notice")]
    (notify,) = session.sent
    assert notify.voice is False and spoken == []


# --- the report: the five patterns, counted per session -----------------------------------------------------
