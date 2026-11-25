"""A talk is heard once and kept as heard.

- A talk's turn shows on the talk's channel, in its own run (the talk's turn id, the talk card's), never
  streamed on `/v2/live` too: what Pisa understood, a card, a phone action.
- The user's talk.end (or the DELETE) is the end: their last words are never answered after it, even when the
  phone's peer closed first. After a real loss of the peer, the answer goes once into the talk's own turn when
  the app is open, and is a notice only when it is closed. A DELETE for a talk that already ended is answered
  as done.
- What was heard is what the chat stores: when the brain's reply goes in silently (the talker had answered),
  the turn's stored reply is the talker's spoken lines; a correction stays its own message.
- One utterance the talker split on a trailing connector ("Scusa… e poi") is one turn.
- `pisa.voice.talk.text`: words typed into the open talk go to the talker as the user's text turn; a typed run
  during a talk is never answered in text only.

Synthetic words only."""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pisa_conformance.validate import EXT_ID, check_lifecycle, check_seq, errors, server_frame_errors

from pisa.api.talk_channel import TalkChannel
from pisa.domain.whisper import Kind, Priority
from pisa.ports.talker import AskBrain, OwnerWords, PisaWords
from pisa.services import talk
from pisa.services.session import FrameError
from pisa.services.session_messages import TEXT_MAX, Utterance
from pisa.services.talk import Opening, TalkScreen, Thought
from pisa.services.talk_turns import turn_senders
from tests.owner import owner_at
from tests.test_live_talk import FakeLink, _run, _until
from tests.test_live_v2_frames import DEVICE, session
from tests.virtual_time import run_talk

THREAD = f"conversation:{DEVICE}"
SOURCE = {"title": "Pagina", "url": "https://example.org/a", "host": "example.org"}


class ChannelLink(FakeLink):
    """The phone's side of a talk whose channel speaks contract 2 (a `TalkChannel`, as `MediaPeer` has)."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, Any]] = []
        self.channel = TalkChannel(DEVICE, THREAD, "it-IT")
        self.channel.attach(lambda text: self.frames.append(json.loads(text)))

    def send(self, message: dict[str, Any]) -> None:
        super().send(message)
        self.channel.out(message)

    def turn_id(self) -> str | None:
        return self.channel.turn_id()


def _kit(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problems = [f"seq {f.get('seq')}: {e}" for f in frames for e in server_frame_errors(f)]
    problems += check_lifecycle([f for f in frames if "event" in f])
    problems += check_seq(frames, 1)
    assert problems == [], problems[:10]
    return frames


# --- 1. a talk's turn is shown once, in its own turn -------------------------------------------------------


def test_a_talk_s_turn_shows_in_its_own_run_on_the_talk_s_channel_never_on_the_live_socket() -> None:
    live = session()
    run, _, _, _ = _run()
    link = ChannelLink()
    run.link = link
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    link.send({"type": "talk.transcript", "text": "Portami da Marco e chiamalo", "final": True})
    turn = run.turn_id()
    assert turn is not None and turn == link.channel.turn_id()
    senders = turn_senders(live, run)

    async def go() -> list[bool]:
        return [
            await senders["understood"](
                {"meant": "da Marco", "raw": "da marco", "corrections": [], "ask": None}
            ),
            await senders["directions"](
                {
                    "directions_id": str(uuid.uuid4()),
                    "name": "Casa di Marco",
                    "lat": 41.9,
                    "lon": 12.5,
                    "mode": "walk",
                }
            ),
            await senders["call_phone"](
                {"call_id": str(uuid.uuid4()), "name": "Marco", "phone": "+39060000000"}
            ),
            await senders["sources_card"](
                {
                    "title": "Fonti",
                    "lines": ["Una pagina"],
                    "sources": [SOURCE],
                }
            ),
        ]

    assert asyncio.run(go()) == [True, True, True, True]
    assert live.live.kept() == []  # nothing of the talk's turn on /v2/live
    link.send({"type": "talk.ended", "reason": "owner"})
    frames = _kit(link.frames)
    events = [f for f in frames if "event" in f]
    # every frame of the turn is in the talk's run: the id the app's talk card knows
    assert {f["runId"] for f in events} == {turn}
    customs = {f["event"]["name"]: f["event"]["value"] for f in events if f["event"]["type"] == "CUSTOM"}
    assert customs["pisa.voice.understood"]["replyTo"] == turn
    assert {"pisa.directions", "pisa.call"} <= set(customs)
    assert any(f["event"]["type"] == "ACTIVITY_SNAPSHOT" for f in events)  # the sources' card


def test_a_talk_s_turn_keeps_to_what_the_app_negotiated() -> None:
    from pisa.services.session import Negotiated

    # no Ring 1, no pisa.voice, no phone actions
    bare = session(Negotiated([], [0], False, {"pisa.notice": 1}, []))
    run, _, _, _ = _run()
    link = ChannelLink()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    senders = turn_senders(bare, run)
    assert senders["directions"] is None and senders["call_phone"] is None  # not offered to the brain

    async def go() -> list[bool]:
        return [
            await senders["understood"]({"meant": "x", "raw": "y", "corrections": [], "ask": None}),
            await senders["sources_card"](
                {
                    "title": "Fonti",
                    "lines": ["a"],
                    "sources": [SOURCE],
                }
            ),
        ]

    assert asyncio.run(go()) == [False, False]
    assert link.frames == [] and bare.live.kept() == []


# --- 2. the user's own end is the end; a lost peer's answer goes once ---------------------------------------


def _service(pool: Any) -> talk.TalkService:
    return talk.TalkService(pool, None, {}, owner=owner_at(), clock=lambda: datetime.now(UTC))  # type: ignore[arg-type]


async def _pool(db: Any) -> Any:
    from pisa.db.pool import open_pool
    from tests.conftest import dsn

    pool = await open_pool(dsn(db), max_size=4)
    await pool.wait()
    return pool


def _notices(db: Any) -> list[tuple[str, str]]:
    return db.execute("SELECT kind, text FROM notifications ORDER BY created_at").fetchall()


def test_talk_end_then_a_peer_close_answers_no_last_words(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    monkeypatch.setattr(talk, "PEER_END_GRACE_S", 0.05)
    device = uuid.UUID(pair(db)[0])
    run, session_, _, seen = _run()
    thought: list[str] = []

    async def go() -> None:
        pool = await _pool(db)
        service = _service(pool)
        service.runs[run.session_id] = run
        service._device_of[run.session_id] = device  # pyright: ignore[reportPrivateUsage]
        run.hooks.after = lambda words, t: service._after(  # pyright: ignore[reportPrivateUsage]
            device,
            "it-IT",
            run.hooks.think,
            words,
            t,
            run=run,
            in_card=lambda *a: thought.append("card") or True,
        )
        task = asyncio.create_task(run.run())
        session_.queue.put_nowait(OwnerWords("e ricordami di chiamare Marco", final=False))
        await _until(lambda: any(e["kind"] == "owner_words" for e in run.timeline.events))
        assert service.end(run.session_id, device, "press")  # talk.end (the DELETE) ...
        run.end(*talk.peer_gone(run.link))  # ... then the phone's peer closes
        await task
        await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    assert run.reason == "owner" and run.closed_by == "phone"
    assert _notices(db) == [] and thought == []  # no notice, nothing written in the card
    assert not any(name == "think" for name, _ in seen)  # their last words weren't even thought on


def test_a_peer_lost_before_the_talk_end_still_answers_no_last_words(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    monkeypatch.setattr(talk, "PEER_END_GRACE_S", 0.2)
    device = uuid.UUID(pair(db)[0])
    run, _, _, _ = _run()
    run.end(*talk.peer_gone(run.link))  # the app closed its peer first ...
    cards: list[Any] = []

    async def think(words: str, trace: str, yes: bool) -> Thought:
        return Thought("Ti ricordo di chiamare Marco alle 18.", Kind.DONE)

    async def go() -> None:
        pool = await _pool(db)
        service = _service(pool)
        service.runs[run.session_id] = run
        service._device_of[run.session_id] = device  # pyright: ignore[reportPrivateUsage]
        service._after(device, "it-IT", think, "e ricordami di chiamare Marco", None, run=run,  # pyright: ignore[reportPrivateUsage]
                       in_card=lambda *a: cards.append(a) or True)  # fmt: skip
        await asyncio.sleep(0.05)
        assert service.end(run.session_id, device, "press")  # ... and its talk.end arrives just after
        await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    assert _notices(db) == [] and cards == []


@pytest.mark.parametrize("app_open", [True, False], ids=["app-open", "app-closed"])
def test_a_real_peer_loss_answers_the_last_words_once(db, monkeypatch, app_open) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    monkeypatch.setattr(talk, "PEER_END_GRACE_S", 0.01)
    device = uuid.UUID(pair(db)[0])
    run, _, _, _ = _run()
    run.end(*talk.peer_gone(run.link))  # no talk.end: the peer failed
    run.last_turn = "run-of-the-talk-s-turn"
    cards: list[tuple[Any, ...]] = []

    async def think(words: str, trace: str, yes: bool) -> Thought:
        return Thought(
            "Ti ricordo di chiamare Marco alle 18.", Kind.DONE, correction="Correzione: non l'ho segnato."
        )

    async def go() -> None:
        pool = await _pool(db)
        service = _service(pool)
        service._after(device, "it-IT", think, "e ricordami di chiamare Marco", None, run=run,  # pyright: ignore[reportPrivateUsage]
                       in_card=lambda *a: cards.append(a) or app_open)  # fmt: skip
        await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    said = "Ti ricordo di chiamare Marco alle 18.\nCorrezione: non l'ho segnato."
    assert cards == [("run-of-the-talk-s-turn", said, "it-IT")]  # into the talk's own turn, once
    if app_open:
        assert _notices(db) == []  # never also a notice while the app is open
    else:
        assert _notices(db) == [("info", said)]  # the app is closed: the push as today


def test_the_answer_goes_into_the_talk_s_turn_on_the_open_live_socket() -> None:
    live = session()
    assert (
        live.talk_answer(THREAD, "run-of-the-talk-s-turn", "Ti ricordo di chiamare Marco.", "it-IT") is False
    )
    frames = _kit(live.live.kept())  # not attached in this test: written all the same, kept for a resume
    events = [f for f in frames if "event" in f]
    assert {f["runId"] for f in events} == {"run-of-the-talk-s-turn"} and {f["threadId"] for f in events} == {
        THREAD
    }
    content = [f["event"]["delta"] for f in events if f["event"]["type"] == "TEXT_MESSAGE_CONTENT"]
    assert "".join(content) == "Ti ricordo di chiamare Marco."


def test_the_delete_is_idempotent_for_the_device_s_own_ended_talk(db) -> None:  # type: ignore[no-untyped-def]
    run, _, _, _ = _run()
    device, other = uuid.uuid4(), uuid.uuid4()

    async def go() -> list[bool]:
        pool = await _pool(db)
        service = _service(pool)
        service.runs[run.session_id] = run
        service._device_of[run.session_id] = device  # pyright: ignore[reportPrivateUsage]
        out = [service.end(run.session_id, other), service.end(run.session_id, device)]
        # stored and gone, as the run's end leaves it
        service.runs.pop(run.session_id)
        service._device_of.pop(run.session_id)  # pyright: ignore[reportPrivateUsage]
        service._ended[run.session_id] = (device, time.monotonic())  # pyright: ignore[reportPrivateUsage]
        out += [service.end(run.session_id, device), service.end(run.session_id, other)]
        out.append(service.end(uuid.uuid4(), device))  # no such talk
        await pool.close()
        return out

    assert asyncio.run(go()) == [False, True, True, False, False]


# --- 3. the chat stores what was heard ----------------------------------------------------------------------


def _heard_run(thought: Thought) -> tuple[Any, Any, list[tuple[str, list[str]]], asyncio.Event]:
    run, talker, _, _ = _run(thought)
    release = asyncio.Event()
    think = run.hooks.think
    stored: list[tuple[str, list[str]]] = []

    async def slow(words: str, trace: str, yes: bool, origin: str = "words") -> Thought:
        await release.wait()  # the talker answers by itself first
        return await think(words, trace, yes)

    async def spoken(trace: str, lines: list[str]) -> None:
        stored.append((trace, list(lines)))

    run.hooks.think = slow
    run.hooks.spoken = spoken
    return run, talker, stored, release


def test_a_reply_that_goes_in_silently_is_stored_as_the_talker_said_it() -> None:
    run, talker, stored, release = _heard_run(Thought("Domani alle 9 hai il dentista.", Kind.SAY))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        talker.queue.put_nowait(OwnerWords("ho qualcosa domani?", final=True))
        await _until(lambda: any(e["kind"] == "think" for e in run.timeline.events))
        talker.queue.put_nowait(PisaWords("Domani alle 9 hai il dentista.", final=True))
        await _until(lambda: any(e["kind"] == "pisa_said" for e in run.timeline.events))
        release.set()
        await _until(lambda: stored)
        talker.queue.put_nowait(
            PisaWords("Vuoi che te lo ricordi?", final=True)
        )  # it goes on for those words
        await _until(lambda: len(stored) == 2)
        run.end("owner")
        await task

    run_talk(go())
    trace = next(e["trace"] for e in run.timeline.events if e["kind"] == "think")
    whisper = talker.whispers[-1]
    assert whisper.priority is Priority.SILENT  # the brain's reply went in silently ...
    # ... so the turn's stored reply is what was heard, kept up to date as the voice goes on
    assert stored == [
        (trace, ["Domani alle 9 hai il dentista."]),
        (trace, ["Domani alle 9 hai il dentista.", "Vuoi che te lo ricordi?"]),
    ]


def test_a_reply_said_aloud_keeps_its_stored_reply_and_a_correction_stays_apart() -> None:
    said_aloud, talker, stored, release = _heard_run(Thought("Domani alle 9 hai il dentista.", Kind.SAY))

    async def aloud() -> None:
        task = asyncio.create_task(said_aloud.run())
        talker.queue.put_nowait(OwnerWords("ho qualcosa domani?", final=True))
        await _until(lambda: any(e["kind"] == "think" for e in said_aloud.timeline.events))
        release.set()  # nothing said yet: the brain's reply is said
        await _until(lambda: talker.whispers and talker.whispers[-1].kind is Kind.SAY)
        said_aloud.end("owner")
        await task

    run_talk(aloud())
    assert stored == []  # what is stored is what is said: nothing to replace

    fix = "Correzione: non l'ho segnato."
    run, talker, stored, release = _heard_run(Thought("L'ho segnato per domani.", Kind.SAY, correction=fix))

    async def corrected() -> None:
        task = asyncio.create_task(run.run())
        talker.queue.put_nowait(OwnerWords("segna il dentista domani", final=True))
        await _until(lambda: any(e["kind"] == "think" for e in run.timeline.events))
        talker.queue.put_nowait(PisaWords("L'ho segnato per domani.", final=True))
        await _until(lambda: any(e["kind"] == "pisa_said" for e in run.timeline.events))
        release.set()
        await _until(lambda: stored)
        talker.queue.put_nowait(PisaWords(fix, final=True))  # the correction, said as its own line
        await _until(lambda: any(w.text == fix for w in talker.whispers))
        await asyncio.sleep(0.1)
        run.end("owner")
        await task

    run_talk(corrected())
    assert all(fix not in lines for _, lines in stored)  # the correction is its own message, never the reply
    assert stored[-1][1] == ["L'ho segnato per domani."]


def test_the_conversation_replaces_a_talk_turn_s_stored_reply_and_keeps_its_correction(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from tests.test_said_is_stored import FALSE_CLAIM, _play, _scenario, _stored

    lang, words, false, answer, fix = FALSE_CLAIM[0]
    _play(client, db, fake_models, secrets_key, clock, http_handler,
          _scenario(lang, [{"owner": words, "voice": True}], [[false]], {}))  # fmt: skip
    assert [m.strip() for m in _stored(db)] == [answer, fix]
    conversations = client.app.state.runtime.conversations
    ((device, turn),) = conversations._last_turn.items()  # pyright: ignore[reportPrivateUsage]
    heard = "La cena di domani è con i cugini, alle otto."
    assert client.portal.call(conversations.spoken, device, turn.trace_id, heard) is True
    assert _stored(db) == [heard, fix]  # what was heard, then its correction, apart
    assert client.portal.call(conversations.spoken, device, "another-trace", "x") is False
    assert client.portal.call(conversations.spoken, uuid.uuid4(), turn.trace_id, "x") is False
    assert _stored(db) == [heard, fix]


# --- 4. one utterance split on a connector is one turn ------------------------------------------------------


def test_words_ending_on_a_connector_wait_for_the_rest_and_are_one_turn() -> None:
    run, talker, _, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        talker.queue.put_nowait(OwnerWords("Scusa… E poi", final=True))
        talker.queue.put_nowait(AskBrain("c1", "e poi?"))
        await asyncio.sleep(talk.CONNECTOR_WAIT_S - 0.5)  # still waiting for the rest
        assert not any(name == "think" for name, _ in seen)
        talker.queue.put_nowait(OwnerWords("mettimi il dentista domani alle 9", final=True))
        await _until(lambda: any(name == "think" for name, _ in seen), within=5)
        await asyncio.sleep(1)
        run.end("owner")
        await task

    run_talk(go())
    assert [w for name, w in seen if name == "think"] == ["Scusa… E poi … mettimi il dentista domani alle 9"]
    assert len(talker.answers) == 1  # the talker's call answered once, by the one thought


def test_the_wait_goes_on_while_the_user_is_still_saying_the_rest() -> None:
    """The talker split one utterance 8 s apart: their voice kept arriving, so the first words waited."""
    run, talker, _, seen = _run()

    async def voice() -> None:
        for _ in range(16):  # their voice in the phone's audio, every half second for 8 s
            await asyncio.sleep(0.5)
            run._voice_at = talk.time.monotonic()  # pyright: ignore[reportPrivateUsage]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        talker.queue.put_nowait(OwnerWords("Scusa, una cosa e poi", final=True))
        speaking = asyncio.create_task(voice())
        await asyncio.sleep(8)
        talker.queue.put_nowait(OwnerWords("spostami la palestra a giovedì", final=True))
        await speaking
        await _until(lambda: any(name == "think" for name, _ in seen), within=5)
        run.end("owner")
        await task

    run_talk(go())
    assert [w for name, w in seen if name == "think"] == [
        "Scusa, una cosa e poi … spostami la palestra a giovedì"
    ]


@pytest.mark.parametrize(
    ("words", "lang", "trails"),
    [
        ("Scusa… E poi", "it-IT", True),
        ("e poi", "it-IT", True),
        ("I need milk and", "en-US", True),
        ("Book it and then", "en-US", True),
        ("E poi?", "it-IT", False),
        ("Scusa.", "it-IT", False),
        ("ehm", "it-IT", False),
        ("Che tempo fa domani", "it-IT", False),
    ],
)
def test_a_trailing_connector_is_the_turn_rules_unfinished_last_word(
    words: str, lang: str, trails: bool
) -> None:
    assert talk._trails(words, lang) is trails  # pyright: ignore[reportPrivateUsage]


def test_the_connector_wait_is_the_economy_path_s() -> None:
    from pisa.services.session import FRAGMENT_WAIT_S

    assert talk.CONNECTOR_WAIT_S == FRAGMENT_WAIT_S


# --- 5. talk.text -----------------------------------------------------------------------------------------


class _Talks:
    def __init__(self, run: Any) -> None:
        self.run = run

    def run_for(self, device_id: uuid.UUID) -> Any:
        return self.run


class _Runtime:
    def __init__(self, run: Any) -> None:
        self.talk = _Talks(run)


def _talking(run: Any, consent: str | None = None) -> Any:
    live = session()
    live.runtime = _Runtime(run)  # type: ignore[assignment]

    async def refused(*args: Any) -> str | None:
        return consent

    live.consent_refused = refused  # type: ignore[method-assign]
    return live


def _frame(talk_id: str, text: str) -> dict[str, Any]:
    return {"op": "custom", "name": "pisa.voice.talk.text", "value": {"id": talk_id, "text": text}}


def test_text_typed_into_the_open_talk_is_his_turn_and_the_talker_s_to_answer() -> None:
    run, talker, link, seen = _run()
    live = _talking(run)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await asyncio.sleep(0.05)
        await live.frame(_frame(str(run.session_id).upper(), "Mettimi il dentista domani alle 9"))
        await _until(lambda: any(name == "think" for name, _ in seen))
        run.end("owner")
        await task

    run_talk(go())
    assert talker.texts == ["Mettimi il dentista domani alle 9"]  # the talker answers it by voice
    # his bubble, in the talk's turn; the brain's turn on his words (stored as his message, as any)
    assert {
        "type": "talk.transcript",
        "text": "Mettimi il dentista domani alle 9",
        "final": True,
    } in link.sent
    assert [w for name, w in seen if name == "think"] == ["Mettimi il dentista domani alle 9"]
    assert live.live.kept() == []  # nothing answered on /v2/live


@pytest.mark.parametrize("case", ["unknown", "closed", "empty", "too_long", "no_talk"])
def test_talk_text_is_refused_for_a_talk_that_isn_t_open_or_words_out_of_bounds(case: str) -> None:
    run, talker, _, _ = _run()
    talk_id, text = str(run.session_id), "Ciao"
    if case == "unknown":
        talk_id = str(uuid.uuid4())
    elif case == "closed":
        run.end("owner")
    elif case == "empty":
        text = "   "
    elif case == "too_long":
        text = "a" * (TEXT_MAX + 1)
    live = _talking(None if case == "no_talk" else run)
    with pytest.raises(FrameError) as refused:
        asyncio.run(live.frame(_frame(talk_id, text)))
    assert ("isn't open" in str(refused.value)) == (case in ("unknown", "closed", "no_talk"))
    assert talker.texts == []


def test_talk_text_is_refused_without_the_user_s_ai_consent_or_the_voice_extension() -> None:
    from pisa.services.session import Negotiated

    run, talker, _, _ = _run()
    live = _talking(run, consent="Pisa needs your consent first.")
    asyncio.run(live.frame(_frame(str(run.session_id), "Ciao")))
    (frame,) = live.live.kept()
    assert frame["control"] == "error" and frame["code"] == "ai_consent_required"
    assert errors(frame, EXT_ID + "#/$defs/ServerControl") == []
    plain = _talking(run)
    plain.negotiated = Negotiated([], [0], False, {"pisa.notice": 1}, [])
    with pytest.raises(FrameError, match=r"pisa\.voice"):
        asyncio.run(plain.frame(_frame(str(run.session_id), "Ciao")))
    assert talker.texts == []


def test_a_typed_turn_during_a_talk_goes_into_the_talk_never_answered_in_text_only() -> None:
    run, talker, _, seen = _run()
    live = _talking(run)
    typed = Utterance(id=str(uuid.uuid4()), text="E la palestra?", lang="it-IT", final=True, source="typed")

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await asyncio.sleep(0.05)
        await live.handle(typed)
        await _until(lambda: any(name == "think" for name, _ in seen))
        run.end("owner")
        await task

    run_talk(go())
    assert talker.texts == ["E la palestra?"]  # the talker answers it by voice
    assert not live._replies  # pyright: ignore[reportPrivateUsage]  # no text-only reply under way
    events = [f["event"] for f in live.live.kept() if "event" in f]
    assert not any(e["type"] == "TEXT_MESSAGE_CONTENT" for e in events)  # nothing said in text


def test_the_talkers_take_typed_words_as_the_user_s_own_turn() -> None:
    from pisa.connectors.talker_gemini import owner_turn
    from pisa.connectors.talker_xai import owner_text_events

    content = owner_turn("Ciao")
    assert content.role == "user" and content.parts and content.parts[0].text == "Ciao"  # never a whisper
    events = owner_text_events("Ciao", responding=True)
    assert [e["type"] for e in events] == ["response.cancel", "conversation.item.create", "response.create"]
    assert events[1]["item"] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Ciao"}],
    }
    assert [e["type"] for e in owner_text_events("Ciao", responding=False)] == [
        "conversation.item.create",
        "response.create",
    ]


def test_discovery_lists_talk_text() -> None:
    from pisa.api.server import FEATURES

    assert {"id": "talk.text", "version": 1} in FEATURES
