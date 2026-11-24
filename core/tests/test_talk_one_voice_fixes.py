"""The review's findings, each as the scenario the reviewer reproduced. Every word
here is synthetic."""

import asyncio
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from pisa.domain.claims import claim, talker_claims_change
from pisa.domain.whisper import Kind, Priority, Whisper, own_words
from pisa.ports.talker import AskBrain, OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services import talk
from pisa.services.talk import Thought
from tests.test_live_talk import FakeLink, _run, _until
from tests.test_talk_one_voice import Phone, _pisa_speaking, _talking
from tests.test_talk_timeline import QUIET, first, kinds
from tests.virtual_time import run_talk

# --- 1: the repeat guard never cuts the brain's real answer ------------------------------------------

WHOLE = "Domani hai la riunione con Marco."
LONGER = "Domani hai la riunione con Marco alle 10 e il dentista."


def _said_then(run: talk.TalkRun, session: Any, link: FakeLink, whisper_after: bool) -> Any:
    """Pisa said WHOLE for their words; the brain's whisper came after it (or before); then Pisa's next line
    grows, partial by partial, into LONGER."""

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("cosa ho domani", final=True))
        if not whisper_after:
            await _until(lambda: "whisper" in kinds(run))
        session.queue.put_nowait(PisaWords(WHOLE, final=True))
        await _until(lambda: "pisa_said" in kinds(run))
        if whisper_after:
            await _until(lambda: "whisper" in kinds(run))
        session.queue.put_nowait(Speaking(True))
        link.buffered_ms = 300
        for partial in ("Domani hai la riunione", "Domani hai la riunione con Marco", LONGER):
            session.queue.put_nowait(PisaWords(partial, final=False))
        await _until(lambda: run._pisa_line == LONGER)  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    return go


@pytest.mark.parametrize("whisper_after", [True, False])
def test_a_longer_answer_that_begins_like_a_whole_line_is_never_cut(whisper_after: bool) -> None:
    """The reviewer: "Domani hai la riunione con Marco." said whole, then the brain's whisper has Pisa say
    "Domani hai la riunione con Marco alle 10 e il dentista": Pisa was cut at "riunione" and the answer lost.
    A longer line that begins the same way is never "said again", whisper or not."""
    run, session, link, _ = _run()
    if whisper_after:
        _slow(run, Thought(LONGER, Kind.SAY), 0.2)
    run_talk(_said_then(run, session, link, whisper_after)())
    assert "pisa_again" not in kinds(run)
    assert session.stopped == 0 and link.cleared == 0


def test_the_same_whole_line_after_a_correction_is_never_cut() -> None:
    """A line carrying a correction given since the whole line was said is said, even word for word."""
    run, session, link, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("cosa ho domani", final=True))
        await _until(lambda: "whisper" in kinds(run))
        session.queue.put_nowait(PisaWords(WHOLE, final=True))
        await _until(lambda: "pisa_said" in kinds(run))
        await run._whisper(  # pyright: ignore[reportPrivateUsage]
            Whisper(Kind.CORRECT, Priority.NOW, WHOLE, id="fix", replaces="talker")
        )
        session.queue.put_nowait(Speaking(True))
        link.buffered_ms = 300
        session.queue.put_nowait(PisaWords(WHOLE, final=False))
        await _until(lambda: run._pisa_line == WHOLE)  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert "pisa_again" not in kinds(run) and session.stopped == 0


# --- 2: their yes makes the held change as confirm_remember would ----------------------------------


def test_his_yes_to_a_held_add_task_keeps_its_outside_text_mark(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    """After a page was read, add_task is held; "Sì."; the model calls add_task again. The task is stored as
    confirm_remember stores it: its title came from outside text (`source_kind` 'email')."""
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play
    from tests.test_talking_works import _history_with_a_page  # pyright: ignore[reportPrivateUsage]

    device = pair(db)
    _history_with_a_page(db, client, device)
    add = [("add_task", {"title": "Comprare il bagnoschiuma"})]
    model = Script({"ok": [add], "Sì.": [add]})
    fake_models.default = model
    play(client, db, secrets_key, model, "ok", device=device)
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)  # held
    play(client, db, secrets_key, model, "Sì.", device=device)
    row = db.execute("SELECT title, source_kind, source_ref FROM tasks").fetchone()
    assert row == ("Comprare il bagnoschiuma", "email", None)


def test_his_yes_to_a_held_task_change_is_no_label_of_his(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    """The gated task tools after his yes run with no label: the idea was outside text's."""
    from tests.assistant_env import add_task
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play
    from tests.test_talking_works import _history_with_a_page  # pyright: ignore[reportPrivateUsage]

    device = pair(db)
    task = add_task(db, "Spesa al mercato", priority=2)
    _history_with_a_page(db, client, device)
    urgent = [("set_task_priority", {"task_ref": "Spesa al mercato", "priority": 1})]
    model = Script({"ok": [urgent], "Sì.": [urgent]})
    fake_models.default = model
    play(client, db, secrets_key, model, "ok", device=device)
    assert db.execute("SELECT priority FROM tasks WHERE id = %s", (task,)).fetchone() == (2,)  # held
    play(client, db, secrets_key, model, "Sì.", device=device)
    row = db.execute("SELECT priority, priority_source FROM tasks WHERE id = %s", (task,)).fetchone()
    assert row == (1, "pisa")


# --- 3: their one-word answers are their ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "word"), [("Vuoi chiamare Marco o Nino?", "Marco."), ("Alle nove o alle dieci?", "Dieci.")]
)
def test_a_one_word_answer_to_pisa_s_choice_question_is_his(line: str, word: str) -> None:
    run, session, phone, _ = _talking(voice=True)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await _pisa_speaking(run, session, phone, line)
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerWords(word, final=True))
        await _until(lambda: "owner_final" in kinds(run) or "echo" in kinds(run))
        run.end("owner")
        await task

    run_talk(go())
    assert "echo" not in kinds(run) and first(run, "owner_final")["text"] == word


def test_a_quiet_voice_s_yes_to_pisa_s_question_is_his() -> None:
    """His voice never reached VOICE_PEAK (the barge-in read as echo), then his "Sì." to the line that asked
    them (a word of that very line): theirs. A word of a line that asked nothing ("Certo.") is still Pisa's
    echo."""
    cases = (
        ("La segno come fatta, sì o no?", "Sì.", True),
        ("Preferisci la palestra o la piscina?", "Piscina.", True),
        (None, "Certo.", False),
    )
    for line, word, his in cases:
        run, session, phone, _ = _talking(voice=False)

        async def go(r: talk.TalkRun = run, s: Any = session, p: Phone = phone, ln: str | None = line,
                     w: str = word) -> None:  # fmt: skip
            task = asyncio.create_task(r.run())
            await (_pisa_speaking(r, s, p, ln) if ln else _pisa_speaking(r, s, p))
            s.queue.put_nowait(OwnerSpeaking(True))
            await _until(lambda: "barge_in" in kinds(r))
            s.queue.put_nowait(OwnerWords(w, final=True))
            await _until(lambda: "owner_final" in kinds(r) or "echo" in kinds(r))
            r.end("owner")
            await task

        run_talk(go())
        assert first(run, "barge_in")["by"] == "echo"
        assert ("owner_final" in kinds(run)) is his and ("echo" in kinds(run)) is not his, word


# --- 4: the wait after a stop; the 12 s from their last words ---------------------------------------


def test_after_a_stop_the_call_waits_through_silence_and_an_empty_final(monkeypatch: Any) -> None:
    """talk.stop, then ask_brain, then silence past ASK_WAIT_S, then an empty final from noise: the call still
    waits, and their next words answer it."""
    monkeypatch.setattr(talk, "ASK_WAIT_S", 0.1)
    run, session, link, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        link.messages.put_nowait({"type": "talk.stop"})
        await _until(lambda: "barge_in" in kinds(run))
        session.queue.put_nowait(AskBrain("c1", "cosa vuole"))
        await asyncio.sleep(0.15)  # 3 s of silence, scaled
        session.queue.put_nowait(OwnerWords("", final=True))  # noise
        await asyncio.sleep(0.15)
        assert not session.answers
        session.queue.put_nowait(OwnerWords("cosa ho domani", final=True))
        await _until(lambda: session.answers)
        run.end("owner")
        await task

    run_talk(go())
    assert [w.id for _, w in session.answers] == ["w1"]


def test_a_long_dictation_is_never_cut_by_unheard(monkeypatch: Any) -> None:
    """His partial words go on past ASK_WORDS_WAIT_S from the call: the cap counts from his last words."""
    monkeypatch.setattr(talk, "ASK_WAIT_S", 0.3)
    monkeypatch.setattr(talk, "ASK_WORDS_WAIT_S", 0.5)
    run, session, _, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("scrivi a Anna", final=False))
        session.queue.put_nowait(AskBrain("c1", "un messaggio"))
        words = "scrivi a Anna"
        for n in range(10):  # 1 s of dictation, a new partial every 0.1 s
            await asyncio.sleep(0.1)
            words += f" parola{n}"
            session.queue.put_nowait(OwnerWords(words, final=False))
        session.queue.put_nowait(OwnerWords(words, final=True))
        await _until(lambda: session.answers)
        run.end("owner")
        await task

    run_talk(go())
    assert [w.id for _, w in session.answers] == ["w1"]


def test_noise_partials_that_never_close_are_nothing_of_his(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "ASK_WAIT_S", 0.1)
    monkeypatch.setattr(talk, "ASK_WORDS_WAIT_S", 5.0)
    run, session, _, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("mh", final=False))
        session.queue.put_nowait(AskBrain("c1", "cosa"))
        await _until(lambda: session.answers, within=1.0)
        run.end("owner")
        await task

    run_talk(go())
    assert [w.id for _, w in session.answers] == ["unheard"]


# --- 5: own_words never corrects Pisa's legitimate lines ---------------------------------------------


def _given(text: str) -> list[Whisper]:
    return [Whisper(Kind.SAY, Priority.PAUSE, text, id="w1")]


@pytest.mark.parametrize(
    ("said", "given"),
    [
        ("Non c\u2019è problema, ci penso io.", ""),  # curly apostrophe
        ("Non è un problema.", ""),
        ("Lo controllerò e ti avviserò.", "Lo controllo e ti avviso appena cambia."),  # the whisper's tense
        ("Ti dirò, il ristorante è aperto fino alle 22.", ""),
        ("I will remind you at nine.", "I'll remind you at nine."),
        ("Il server ha avuto dei problemi, riprovo tra poco.", "Il server ha avuto un problema: riprovo."),
    ],
)
def test_own_words_never_takes_back_a_legitimate_line(said: str, given: str) -> None:
    assert own_words(said, _given(given) if given else []) is None


def test_its_own_cause_and_promise_are_still_found() -> None:
    assert own_words("Cercherò di risolvere questo malfunzionamento subito.", []) == "promise"
    assert own_words("Ho avuto un problema col sistema vocale.", []) == "cause"
    assert own_words("I will fix it right away.", _given("I'll remind you at nine.")) == "promise"


# --- 6: the new claim verbs are the talker's only --------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "Ho confermato che il negozio è aperto fino alle 20.",
        "I've confirmed the shop is open until 8.",
        "Ho completato la ricerca.",
        "Ho registrato un calo delle vendite a ottobre.",
    ],
)
def test_the_brain_s_rail_reads_these_as_on_main(said: str) -> None:
    """`claim` serves the brain's own claim rail: these report what Pisa found, never a change (as on
    main)."""
    assert claim(said) is None


def test_the_talker_s_grounding_keeps_its_own_claims() -> None:
    assert talker_claims_change("Sì, ho appena confermato il promemoria per stasera.")
    assert talker_claims_change("I've just confirmed the reminder.")
    assert talker_claims_change("Ho spostato la cena alle 21.")  # main's claims stand for it too
    assert not talker_claims_change("Ti confermo l'orario?")


# --- 7: the thresholds -------------------------------------------------------------------------------


class Jittery(FakeLink):
    """The phone's audio over a cellular link: quiet frames with 0.3 s gaps."""

    async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
        while True:
            for _ in range(5):
                yield QUIET
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)


def test_cellular_jitter_still_counts_as_the_phone_s_audio_flowing() -> None:
    run, session, _, _ = _run()
    phone = Jittery()
    run.link = phone

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Certo, un attimo: ho segnato la spesa come fatta.", final=False))
        phone.buffered_ms = 800
        await asyncio.sleep(1.3)
        session.queue.put_nowait(OwnerSpeaking(True))
        await _until(lambda: "barge_in" in kinds(run))
        run.end("owner")
        await task

    run_talk(go())
    assert first(run, "barge_in")["by"] == "echo" and phone.cleared == 0


def test_his_voice_just_after_the_talker_s_start_of_speech_makes_the_barge_in_his() -> None:
    """BARGE_VOICE_S counts from their voice's first frame: the talker's start came first, their voice reached
    VOICE_PEAK a moment later: their barge-in, Pisa's line is cut."""
    run, session, phone, _ = _talking(voice=False)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await _pisa_speaking(run, session, phone)
        session.queue.put_nowait(OwnerSpeaking(True))
        await _until(lambda: "barge_in" in kinds(run))
        phone.voice = True
        await _until(lambda: phone.cleared == 1)
        run.end("owner")
        await task

    run_talk(go())
    barges = [e for e in run.timeline.events if e["kind"] == "barge_in"]
    assert [b["by"] for b in barges] == ["echo", "voice"] and barges[1]["late"] is True


# --- 8: a talk whose audio stopped is no open talk ---------------------------------------------------


def test_a_talk_whose_audio_stopped_past_its_idle_timeout_ends_and_the_phone_is_answered() -> None:
    from pisa.services.session import DeviceSession

    run, _, _, _ = _run()
    device = uuid.uuid4()
    session = SimpleNamespace(
        runtime=SimpleNamespace(talk=SimpleNamespace(run_for=lambda d: run if d == device else None)),
        device=SimpleNamespace(id=device),
    )
    run._frame_at = time.monotonic() - 1.0  # pyright: ignore[reportPrivateUsage]
    assert DeviceSession.talking(session) is run  # type: ignore[arg-type]
    run._frame_at = time.monotonic() - talk.LINK_SILENT_S - 1  # pyright: ignore[reportPrivateUsage]
    assert DeviceSession.talking(session) is None  # type: ignore[arg-type]
    assert (run.reason, run.closed_by) == ("fallback", "peer") and "link_silent" in kinds(run)


def _slow(run: talk.TalkRun, thought: Thought, delay: float) -> None:
    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        await asyncio.sleep(delay)
        return thought

    run.hooks = dataclasses.replace(run.hooks, think=think)


# --- The merge (the review's fixes) ---------------------------------------------------------------


def test_a_silent_whisper_does_not_reopen_a_line_already_said() -> None:
    """Review M1: a SILENT whisper (a fact for the talker's memory, nothing to say) moved "the talker last got
    words to say", so the same whole line said again after it was no longer cut. Only spoken kinds count."""
    run, session, link, _ = _run()
    line = "Non ci sono riuscito."

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("segna la spesa come fatta", final=True))
        await _until(lambda: "whisper" in kinds(run))
        session.queue.put_nowait(PisaWords(line, final=True))
        await _until(lambda: "pisa_said" in kinds(run))
        await run._whisper(  # pyright: ignore[reportPrivateUsage]
            Whisper(Kind.FACT, Priority.SILENT, "La spesa è nella lista.", id="fact")
        )
        session.queue.put_nowait(Speaking(True))
        link.buffered_ms = 300
        session.queue.put_nowait(PisaWords("Non ci sono", final=False))
        session.queue.put_nowait(PisaWords(line, final=False))
        await _until(lambda: "pisa_again" in kinds(run))
        run.end("owner")
        await task

    run_talk(go())
    assert session.stopped == 1 and link.cleared == 1


@pytest.mark.parametrize(
    ("said", "brain"),
    [
        ("Cercherò il numero di Marco.", "Cerco il numero di Marco."),
        ("Proverò a chiamarlo dopo.", "Provo a chiamarlo dopo."),
        ("Te lo dirò appena arriva.", "Te lo dico appena arriva."),
        ("Lo farò subito.", "Lo faccio subito."),
        ("Controllerò e ti avviserò.", "Controllo e ti avviso."),
        ("I will call Marco later.", "I'm calling Marco later."),
    ],
)
def test_the_brain_s_words_in_the_future_tense_are_its_own(said: str, brain: str) -> None:
    """Review M2: the stem compare missed Italian futures ("Cercherò" vs "Cerco", "farò" vs "faccio") and so
    took back Pisa's own promise. The future's ending and the irregular verbs are folded to the same stem."""
    given = [Whisper(Kind.SAY, Priority.PAUSE, brain, id="w1")]
    assert own_words(said, given) is None


def test_an_invented_cause_or_promise_is_still_taken_back() -> None:
    given = [Whisper(Kind.SAY, Priority.PAUSE, "Cerco il numero di Marco.", id="w1")]
    assert own_words("Ho avuto un problema con il server, scusa.", given) == "cause"
    assert own_words("Ti farò sapere appena posso.", given) == "promise"


@pytest.mark.parametrize(
    "said",
    [
        "Ho confermato che il negozio è aperto fino alle 20.",
        "I've confirmed that the shop is open until 8.",
        "Ho completato la ricerca: il negozio chiude alle 20.",
        "Registrato un calo del 3% sul conto.",
    ],
)
def test_a_verb_reporting_a_fact_is_no_change_claimed(said: str) -> None:
    """Review L3: "Ho confermato che…" reports what the brain found; it changed nothing."""
    assert talker_claims_change(said) is False


def test_a_change_claimed_is_still_caught() -> None:
    assert talker_claims_change("Sì, ho appena confermato il promemoria.") is True
    assert talker_claims_change("Fatto, ho registrato la spesa.") is True
