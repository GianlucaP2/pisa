"""The fix at the merge: Grok sends one utterance's final transcript several
times, repeated or grown ("Ciao" … "Ciao Ilung, mi suggeri…" → "Ciao Ilunga, mi suggerisci un film per
stasera?" three times): each was a full brain turn, three web searches for one question
(~520k input tokens, €0.45). A repeated final is dropped; a final that grows the previous one before its
thought arrived replaces it."""

import asyncio
from typing import Any

from pisa.domain.whisper import Kind
from pisa.ports.talker import AskBrain, OwnerWords
from pisa.services.talk import Thought, _grows
from tests.test_live_talk import _run, _until
from tests.virtual_time import run_talk

FILM = "Ciao Ilunga, mi suggerisci un film per stasera?"


def _finals(link: Any) -> list[str]:
    return [m["text"] for m in link.sent if m.get("type") == "talk.transcript" and m.get("final")]


def test_a_repeated_final_is_one_exchange_and_one_thought() -> None:
    run, session, link, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        for _ in range(3):
            session.queue.put_nowait(OwnerWords(FILM, final=True))
        await _until(lambda: session.whispers)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert [s for s in seen if s[0] == "think"] == [("think", FILM)]
    assert _finals(link) == [FILM]  # the phone makes one card


def test_a_final_that_grows_the_one_being_thought_about_replaces_it() -> None:
    run, session, _, _ = _run()
    started, release = asyncio.Event(), asyncio.Event()
    thoughts: list[str] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        thoughts.append(words)
        if len(thoughts) == 1:
            started.set()
            await release.wait()  # still thinking when the grown final comes
        return Thought(f"Per «{words}»: Knives Out.", Kind.SAY)

    run.hooks.think = think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(AskBrain("c1", "un film"))
        session.queue.put_nowait(OwnerWords("Ciao Ilung, mi suggeri", final=True))
        await started.wait()
        session.queue.put_nowait(OwnerWords(FILM, final=True))
        await _until(lambda: session.answers or session.whispers)
        release.set()
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert thoughts == ["Ciao Ilung, mi suggeri", FILM]
    said = [w.text for _, w in session.answers] + [w.text for w in session.whispers]
    assert said == [f"Per «{FILM}»: Knives Out."]  # only the grown words' thought, to the waiting call
    assert session.answers and session.answers[0][0] == "c1"


def test_what_grows_a_final() -> None:
    def words(t: str) -> list[str]:
        return t.casefold().replace(",", " ").replace("?", " ").split()

    assert _grows(words("Ciao Ilung, mi suggeri"), words(FILM))
    assert _grows(words("Ciao"), words(FILM))
    assert not _grows(words("Fammi una domanda"), words("Sì, oggi è venerdì"))
    assert not _grows(words(FILM), words(FILM))  # a repeat is dropped, not grown
