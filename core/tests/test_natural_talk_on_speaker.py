"""A natural-mode talk in the evening (Gemini Live, the phone on the speaker): Gemini answered
by itself and Pisa's brain said the same again at the pause; "No, no. Calma, calma" never cut Pisa's long
answer; Italian transcribed (and answered) as French; "<no speech>{pause}" and "..." on the cards; "Sì,
grazie" to Pisa's offer to open the voice settings was held."""

import asyncio
from typing import Any

from pisa.domain.whisper import Kind, Priority, Whisper, instructions, talker_rules
from pisa.ports.talker import Audio, OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services.talk import Thought, unmarked
from tests.test_live_talk import CFG, _run, _until
from tests.virtual_time import run_talk

# --- A: one answer per question ----------------------------------------------------------------------------


def _answered(brain: str, talker_said: str, *, call: bool = False) -> list[Whisper]:
    """The user asks; Pisa's voice says `talker_said` by itself; then the brain's plain answer comes."""
    run, session, _, _ = _run(Thought(brain, Kind.SAY))
    gate = asyncio.Event()

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        await gate.wait()  # the brain answers after Pisa's voice spoke
        return Thought(brain, Kind.SAY)

    run.hooks.think = think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("cosa puoi fare per me?", final=True))
        if call:
            from pisa.ports.talker import AskBrain

            session.queue.put_nowait(AskBrain("c1", "cosa può fare Pisa"))
        session.queue.put_nowait(PisaWords(talker_said, final=True))
        await _until(
            lambda: run.timeline.events and any(e["kind"] == "pisa_said" for e in run.timeline.events)
        )
        gate.set()
        await _until(lambda: session.whispers or session.answers)
        run.end("owner")
        await task

    run_talk(go())
    return [*session.whispers, *(w for _, w in session.answers)]


def test_the_brain_s_paraphrase_of_what_pisa_s_voice_said_goes_in_silently() -> None:
    talker = "Posso gestire i tuoi impegni, controllare la tua agenda e ricordarti le cose."
    brain = "Ti aiuto con l'agenda, i promemoria e gli impegni della giornata."
    (said,) = _answered(brain, talker)
    assert said.priority is Priority.SILENT  # never answered twice
    (answer,) = _answered(brain, talker, call=True)  # the talker's own ask_brain call gets it silently too
    assert answer.priority is Priority.SILENT


def test_a_brain_answer_with_a_fact_the_talker_lacked_is_said() -> None:
    (said,) = _answered("Hai la riunione alle 15.", "Posso controllare la tua agenda, se vuoi.")
    assert said.priority is Priority.PAUSE and said.kind is Kind.SAY
    (named,) = _answered("La riunione è con Marco.", "Posso controllare la tua agenda, se vuoi.")
    assert named.priority is Priority.PAUSE
    # a holding phrase is no answer: the brain's plain answer is said
    (after_hold,) = _answered("Ti aiuto con l'agenda e gli impegni.", "Un attimo, controllo.")
    assert after_hold.priority is Priority.PAUSE
    # the other yes or no: "Va bene" then "Non l'ho cancellata" is said
    (no,) = _answered("Non l'ho cancellata: è ancora in agenda.", "Va bene, nessun problema.")
    assert no.priority is Priority.PAUSE


def test_the_talker_s_rules_say_one_holding_phrase_before_pisa_s_answer() -> None:
    # The unusual part said back first; a short holding phrase only when there is none
    assert "say back the unusual part of what they asked" in talker_rules(lang="it-IT")
    assert "a short holding phrase" in talker_rules(lang="it-IT") and "only when there is" in talker_rules(
        lang="it-IT"
    )
    assert "Never an answer, an opinion, an apology, a promise" in talker_rules(lang="it-IT")
    assert "you never answer a question about facts yourself" in talker_rules(
        lang="it-IT"
    )  # the old rules stay


# --- B: the user's words cut Pisa on the speaker
# -------------------------------------------------------------


def _talked_over(partial: str, pisa_line: str = "Allora, domani mattina hai la palestra e poi il dentista"):
    run, session, link, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(Audio(b"\x01\x01"))
        session.queue.put_nowait(PisaWords(pisa_line, final=False))
        await _until(lambda: link.played)
        link.buffered_ms = 4000  # Pisa's long answer still to be heard on the phone
        session.queue.put_nowait(OwnerWords(partial, final=False))
        await asyncio.sleep(0.05)
        session.queue.put_nowait(Audio(b"\x02\x02"))  # the rest of the line Gemini still sends
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    return run, session, link


def test_the_owner_s_words_over_pisa_cut_pisa_on_the_speaker() -> None:
    run, session, link = _talked_over("no no calma")
    assert link.cleared == 1 and session.stopped == 1  # the phone and the talker stop
    assert b"\x02\x02" not in link.played  # what was left of the line isn't played
    assert [e["by"] for e in run.timeline.events if e["kind"] == "barge_in"] == ["words"]
    assert any(m == {"type": "talk.state", "state": "listening"} for m in link.sent)
    run, session, link = _talked_over("basta")
    assert link.cleared == 1


def test_pisa_s_own_line_heard_back_cuts_nothing() -> None:
    run, session, link = _talked_over("domani mattina hai")
    assert link.cleared == 0 and session.stopped == 0
    assert not [e for e in run.timeline.events if e["kind"] == "barge_in"]
    _, _, link = _talked_over("ok")  # one word that isn't a stop
    assert link.cleared == 0


def test_a_cut_by_gemini_s_voice_is_not_cut_again_by_the_words() -> None:
    run, session, link = _run()[:3]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(Audio(b"\x01\x01"))
        await _until(lambda: link.played)
        link.buffered_ms = 4000
        session.queue.put_nowait(OwnerSpeaking(True))  # Gemini's VAD fired
        session.queue.put_nowait(OwnerWords("no no calma", final=False))
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert [e["by"] for e in run.timeline.events if e["kind"] == "barge_in"] == ["voice"]
    assert link.cleared == 1 and session.stopped == 0


# --- C: the session's language -----------------------------------------------------------------------------


def test_gemini_transcribes_in_the_session_s_language() -> None:
    from pisa.connectors import talker_gemini as G

    config = G.connect_config(CFG)
    assert config.input_audio_transcription is not None
    assert config.input_audio_transcription.language_codes == ["it-IT"]


def test_pisa_answers_in_the_session_s_language_whatever_the_transcript_reads() -> None:
    from pisa.brain.conversation import language_instruction
    from pisa.domain.whisper import Fresh

    rule = language_instruction("it-IT")
    assert rule.startswith("The user spoke Italian (it-IT): answer in Italian, even when")
    assert "switch only if the user asks" in rule
    talker = instructions(Fresh("adesso"), "", "it-IT", pisa_lang="it-IT")
    assert "Speak only the session's language" in talker and "Speak Italian (tu)." in talker


# --- D: Gemini's markup on the cards -----------------------------------------------------------------------


def test_gemini_s_markup_never_reaches_the_cards() -> None:
    assert unmarked("<no speech>{pause} Ho aggiunto il latte.").strip() == "Ho aggiunto il latte."
    assert unmarked("Allora {…} vediamo.") == "Allora  vediamo."
    assert unmarked("...") == "" and unmarked(" … ") == "" and unmarked("<no speech> ...") == ""
    assert unmarked("Sono le 9... e poi?") == "Sono le 9... e poi?"  # dots inside a line stay
    run, session, link = _run()[:3]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(PisaWords("...", final=True))
        await _until(lambda: any(m["type"] == "talk.said" for m in link.sent))
        run.end("owner")
        await task

    run_talk(go())
    assert [m["text"] for m in link.sent if m["type"] == "talk.said"] == [""]


# --- E: Pisa's own settings show after outside text
# -----------------------------------------------------------


def test_show_setting_shows_after_an_email_was_read_other_shows_stay_held() -> None:
    from pisa.brain.untrusted import data_block, watch_turn
    from pisa.domain import decisions
    from tests.test_interface_tools import _Phone, _tools, _turn_with

    async def go(tool: str, args: dict[str, Any]) -> tuple[str, _Phone]:
        phone = _Phone()
        presenter = _turn_with("sì, grazie", phone)
        with decisions.collecting():
            watch_turn()
            data_block("email", "Marco: ci vediamo domani", what="an email")
            said = await _tools(presenter)[tool](**args)
            return said, phone

    said, phone = run_talk(go("show_setting", {"section": "voice"}))
    assert not said.startswith("Not shown") and len(phone.sent) == 1
    for tool, args in (("show_plan", {}), ("show_note", {"title": "Nota", "lines": ["riga"]})):
        said, phone = run_talk(go(tool, args))
        assert said.startswith("Not shown") and phone.sent == []
