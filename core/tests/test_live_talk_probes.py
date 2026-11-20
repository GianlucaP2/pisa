"""The reviewer's probes, kept as tests (R8, Gemini finishing a filler turn mid-sentence,
and a quoted "I've moved" are left for the real check and the builder)."""

import asyncio
from typing import Any

import pytest

from pisa.domain.claims import claims_change
from pisa.domain.whisper import Fresh, Kind, Priority, Whisper, grounding
from pisa.ports.talker import AskBrain, OwnerSpeaking, OwnerWords, PisaWords
from pisa.services import talk
from pisa.services.talk import TalkRun, Thought, _says
from tests.test_live_talk import _run, _until
from tests.test_live_talk_stop import _audio, _gemini, _heard, _turn_done
from tests.virtual_time import run_talk


def _seq(thoughts):
    it = iter(thoughts)
    seen = []

    async def think(words, trace, y):
        seen.append(words)
        t = next(it)
        if isinstance(t, tuple):
            await t[1].wait()
            t = t[0]
        return t

    return think, seen


# ---- N2: early basis lets a false "fatto" pass while this exchange's thought is an ASK --------------------
def test_early_false_fatto_with_earlier_done_but_thought_is_ask() -> None:
    gate = asyncio.Event()
    run, s, _, _ = _run()
    think, _seen = _seq(
        [
            Thought("Ho aggiunto il latte alla spesa.", Kind.DONE),
            (Thought("Sposto la cena alle 21?", Kind.ASK), gate),
        ]
    )
    run.hooks.think = think

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(OwnerWords("aggiungi il latte", final=True))
        await _until(lambda: s.whispers)
        s.queue.put_nowait(OwnerWords("sposta la cena alle 21", final=True))
        s.queue.put_nowait(
            PisaWords("Fatto, l'ho spostata alle 21.", final=True)
        )  # a lie: said before the brain
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.1)
        run.end("owner")
        await t

    run_talk(go())
    assert any(w.kind is Kind.CORRECT for w in s.whispers), "false 'fatto' passed: brain is asking for a yes"


# ---- N2: a whisper with an ordinary 'fatto' (not a claim of Pisa) launders the talker's false claim
# ----------
@pytest.mark.parametrize(
    "text",
    [
        "Oggi hai fatto 8000 passi. Metto la palestra domani alle 7?",
        "Il volo è sul 31. Sposto la cena al 30?",
    ],
)
def test_whisper_mentioning_done_launders(text: str) -> None:
    w = Whisper(Kind.ASK, Priority.PAUSE, text, id="w1")
    g = grounding("Fatto, messo.", [w], current=[w])
    assert g.done_without_done, f"whisper '{text}' counted as Pisa's done"


# ---- N2: whisper-says-done negations (should NOT count as done) -----------------------------------------
@pytest.mark.parametrize(
    "text", ["Non l'ho fatto.", "Posso farlo io.", "It's not done yet.", "Non è ancora fatto."]
)
def test_negated_whisper_not_done(text: str) -> None:
    w = Whisper(Kind.SAY, Priority.PAUSE, text, id="w1")
    assert grounding("Fatto.", [w], current=[w]).done_without_done


# ---- talker claim with a 'non' elsewhere in the sentence escapes the check ------------------------------
@pytest.mark.parametrize(
    "said",
    ["Fatto, non ti preoccupare.", "L'ho spostata, nessun problema, non pensarci.", "Done, no problem."],
)
def test_talker_claim_with_negation_word_escapes(said: str) -> None:
    assert claims_change(said), f"'{said}' not seen as a done claim"


# ---- N2: English claim deferred and judged ---------------------------------------------------------------
def test_english_claim_corrected() -> None:
    run, s, _, _ = _run(Thought("Do you want me to add milk?", Kind.ASK))
    run.lang = "en-US"

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(OwnerWords("add milk", final=True))
        s.queue.put_nowait(PisaWords("Yes, I added it.", final=True))
        await asyncio.sleep(0.15)
        run.end("owner")
        await t

    run_talk(go())
    assert any(w.kind is Kind.CORRECT for w in s.whispers)


# ---- N4: echo filler silences the brain's answer --------------------------------------------------------
@pytest.mark.parametrize(
    "filler,thought",
    [
        ("Vediamo domani…", "Sì, domani piove."),
        ("Controllo il meteo di domani.", "Domani piove."),
        ("Fammi vedere se domani piove.", "No, domani non piove."),
    ],
)
def test_echo_filler(filler: str, thought: str) -> None:
    assert not _says(filler, thought), f"filler '{filler}' counts as answering '{thought}'"


# ---- N4: talker's wrong guess sharing the topic words silences the right answer --------------------------
@pytest.mark.parametrize(
    "said,thought",
    [
        ("Domani alle 11 hai il dentista.", "Domani alle 9 hai il dentista."),
        ("Sì, domani piove.", "No, domani non piove."),
    ],
)
def test_wrong_answer_counts(said: str, thought: str) -> None:
    assert not _says(said, thought)


# ---- N4: correct paraphrase counts -----------------------------------------------------------------------
def test_paraphrase_counts() -> None:
    assert _says("Domani alle 10 hai il dentista.", "Hai il dentista domani alle dieci.")


# ---- after a stop the talker calls first, owner says 'sì' to Pisa's pending question -----------------------
def test_after_stop_call_then_bare_yes_to_pisa(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "ADDRESSED_WAIT_S", 0.1)
    run, s, link, seen = _run(Thought("Mando la mail a Marco?", Kind.ASK))

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(OwnerWords("scrivi a marco", final=True))
        await _until(lambda: s.whispers)
        link.messages.put_nowait({"type": "talk.stop"})
        await _until(lambda: s.stopped)
        s.queue.put_nowait(AskBrain("c1", "aspetto"))
        await asyncio.sleep(0.05)
        s.queue.put_nowait(OwnerSpeaking(True))
        s.queue.put_nowait(OwnerSpeaking(False))
        s.queue.put_nowait(OwnerWords("sì", final=True))
        await asyncio.sleep(0.3)
        run.end("owner")
        await t

    run_talk(go())
    thinks = [x[1] for x in seen if x[0] == "think"]
    assert "sì" in thinks, f"yes dropped; answers={[(c, w.kind.value) for c, w in s.answers]}"


# ---- after a stop, session idles: run ends, call task cancelled ------------------------------------------
def test_after_stop_idle_ends(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "IDLE_AFTER_S", 0.3)
    monkeypatch.setattr(talk, "TICK_S", 0.05)
    run, s, link, _ = _run()

    async def go():
        t = asyncio.create_task(run.run())
        link.messages.put_nowait({"type": "talk.stop"})
        await _until(lambda: s.stopped)
        s.queue.put_nowait(AskBrain("c1", "x"))
        r = await asyncio.wait_for(t, 2)
        return r, len(run._asking)

    r, left = run_talk(go())
    assert r == "idle" and left == 0


# ---- N1 Gemini: owner pauses mid-sentence, Gemini says 'Ok', owner continues: one utterance --------------
def test_gemini_pause_mid_sentence(monkeypatch: Any) -> None:
    session = _gemini(
        [_heard("metti la call con Marco"), _audio(), _heard(" alle quattro"), _audio(), _turn_done()]
    )
    run0, _, link, seen = _run()
    run = TalkRun(talk.uuid.uuid4(), session, link, run0.hooks, "it-IT", Fresh("adesso"))

    async def go():
        t = asyncio.create_task(run.run())
        await asyncio.sleep(0.3)
        run.end("owner")
        await t

    run_talk(go())
    thinks = [x[1] for x in seen if x[0] == "think"]
    assert thinks == ["metti la call con Marco alle quattro"], thinks


# ---- N1 Gemini: 'sì' to someone else (no audio from Gemini, then a finished transcript) is dropped + HOLD --
def test_gemini_yes_elsewhere(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "ADDRESSED_WAIT_S", 0.1)
    session = _gemini([_heard("sì", True)])
    run0, _, link, seen = _run()
    run = TalkRun(talk.uuid.uuid4(), session, link, run0.hooks, "it-IT", Fresh("adesso"))
    run._calls.append(("c1", 1))

    async def go():
        t = asyncio.create_task(run.run())
        await asyncio.sleep(0.4)
        run.end("owner")
        await t

    run_talk(go())
    thinks = [x[1] for x in seen if x[0] == "think"]
    sent = session._session.sent
    assert not thinks and any(x[0] == "tool" and x[1] == "c1" for x in sent), (thinks, sent)


# ---- N7: shutdown cancel not logged as error -------------------------------------------------------------
def test_normal_end_reason_kept(caplog: Any) -> None:
    run, _s, link, _ = _run()

    async def go():
        t = asyncio.create_task(run.run())
        await asyncio.sleep(0.05)
        link.messages.put_nowait({"type": "talk.end"})
        return await t

    assert run_talk(go()) == "owner"


# ---- N7: an _asked task failing is not seen ---------------------------------------------------------------
def test_asked_task_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "ASK_WAIT_S", 0.05)
    run, s, _link, _ = _run()

    async def broken(call_id, w):
        raise OSError("gone")

    s.answer = broken

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(AskBrain("c1", "x"))
        return await asyncio.wait_for(t, 1)

    assert run_talk(go()) == "error"
