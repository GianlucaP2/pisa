"""The user: "it started to answer all my answers one by one starting from the very first and
looping".
The talk path thought on every final, in order: words said while Pisa thought became a backlog of answers,
each cut by the next; and Pisa's own voice heard back became the user's turns. The latest words win; an echo
is dropped."""

import asyncio
from typing import Any

from pisa.domain.whisper import Kind, Priority
from pisa.ports.talker import OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services.talk import Thought, _in_order
from tests.test_live_talk import _run, _until
from tests.virtual_time import run_talk


def test_words_said_while_pisa_thinks_are_one_turn_never_a_backlog_of_answers() -> None:
    run, session, _, seen = _run()
    first_done = asyncio.Event()
    asked: list[str] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        asked.append(words)
        if len(asked) == 1:
            await first_done.wait()  # 23:03: the first answer took 24 s
            return Thought("Aggiungo il Macro a mezzanotte?", Kind.SAY)
        return Thought("Ti sento. Il Macro a mezzanotte aspetta il tuo sì.", Kind.SAY)

    run.hooks.think = think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("mi metti il Macro a mezzanotte", final=True))
        await _until(lambda: asked)
        for again in ("mi senti?", "con la", "funziona?"):  # said while Pisa thinks
            session.queue.put_nowait(OwnerWords(again, final=True))
        await asyncio.sleep(0.05)
        first_done.set()
        await _until(lambda: len(asked) == 2)
        await _until(lambda: len(session.whispers) >= 2)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert len(asked) == 2  # one thought for the three finals said while Pisa thought, not three
    assert asked[1] == "mi senti? … con la … funziona?"
    said = [w for w in session.whispers if w.kind is not Kind.HOLD and w.priority is not Priority.SILENT]
    assert [w.text for w in said] == ["Ti sento. Il Macro a mezzanotte aspetta il tuo sì."]  # the latest only
    stale = [w for w in session.whispers if w.text == "Aggiungo il Macro a mezzanotte?"]
    assert stale and stale[0].priority is Priority.SILENT  # the talker knows it; it isn't said after
    assert [e for e in run.timeline.events if e.get("kind") == "final_folded"]
    del seen


def test_a_done_answered_late_is_still_said() -> None:
    run, session, _, _ = _run()
    first_done = asyncio.Event()
    asked: list[str] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        asked.append(words)
        if len(asked) == 1:
            await first_done.wait()
            return Thought("Fatto: dentista domani alle 17.", Kind.DONE)
        return Thought("Sì, ti sento.", Kind.SAY)

    run.hooks.think = think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("metti il dentista domani alle 17", final=True))
        await _until(lambda: asked)
        session.queue.put_nowait(OwnerWords("mi senti?", final=True))
        await asyncio.sleep(0.05)
        first_done.set()
        await _until(lambda: len(asked) == 2 and len(session.whispers) >= 2)
        run.end("owner")
        await task

    run_talk(go())
    done = [w for w in session.whispers if w.kind is Kind.DONE]
    assert done and done[0].priority is not Priority.SILENT  # what Pisa did is always said


def _echo_run() -> tuple[Any, Any, list[str]]:
    run, session, _, _ = _run()
    asked: list[str] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        asked.append(words)
        return Thought("Hai già fatto colazione oggi? Rispondi sì o no.", Kind.ASK)

    run.hooks.think = think
    return run, session, asked


def test_pisa_s_own_voice_heard_back_is_never_the_owner_s_words() -> None:
    run, session, asked = _echo_run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("fammi una domanda a cui posso rispondere sì o no", final=True))
        await _until(lambda: asked)
        # cut short
        session.queue.put_nowait(PisaWords("Ti va di fare una passeggiata più tardi?", final=True))
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(
            PisaWords("Non ho capito: intendi dire che hai già fatto colazione?", final=False)
        )
        session.queue.put_nowait(OwnerSpeaking(True))  # Pisa's voice from the speaker: a "barge-in"
        session.queue.put_nowait(
            PisaWords("Non ho capito: intendi dire che hai già fatto colazione?", final=True)
        )
        session.queue.put_nowait(Speaking(False))
        # 11:57: the words Gemini then heard
        echoed = "Ti va di Non ho capito, intendi dire che hai già fatto colazione?"
        session.queue.put_nowait(OwnerWords(echoed, final=True))
        session.queue.put_nowait(OwnerWords("Non ho capito", final=True))
        session.queue.put_nowait(OwnerWords("Dimmi pure.", final=False))
        await asyncio.sleep(0.1)
        session.queue.put_nowait(OwnerWords("Sì, alle otto con mia sorella.", final=True))  # the user's own
        await _until(lambda: len(asked) == 2)
        # a bare yes, even right after Pisa's "sì o no", is always the user's
        session.queue.put_nowait(PisaWords("Rispondi sì o no.", final=True))
        await asyncio.sleep(0.05)
        assert not run._echo("Sì.")  # pyright: ignore[reportPrivateUsage]
        assert run._echo("Rispondi sì o no.")  # pyright: ignore[reportPrivateUsage]
        run.end("owner")
        await task

    run_talk(go())
    assert asked[1:] == ["Sì, alle otto con mia sorella."]  # the echoes started no turn
    assert len([e for e in run.timeline.events if e.get("kind") == "echo"]) == 2


def test_the_owner_s_own_new_words_are_never_taken_for_an_echo() -> None:
    run, session, asked = _echo_run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("fammi una domanda", final=True))
        await _until(lambda: asked)
        session.queue.put_nowait(PisaWords("Hai già fatto colazione oggi? Rispondi sì o no.", final=True))
        session.queue.put_nowait(OwnerWords("no, ho fatto colazione alle otto con mia sorella", final=True))
        await _until(lambda: len(asked) == 2)
        run.end("owner")
        await task

    run_talk(go())
    assert asked[1] == "no, ho fatto colazione alle otto con mia sorella"
    assert _in_order(["hai", "fatto", "colaz"], ["hai", "già", "fatto", "colazione"]) == 3
