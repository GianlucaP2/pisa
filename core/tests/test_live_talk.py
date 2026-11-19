"""Pisa talks through a live voice model. The whisper core, the two talkers' message mapping
(from their own wire types), the run's rails and lifecycle with a fake talker and a fake phone, the service
(economy, the guard, the ledger, the measures), and the REST contract."""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pisa.domain.whisper import Fresh, Kind, Priority, Whisper, framed, grounding, instructions
from pisa.ports.talker import (
    AskBrain,
    Audio,
    Closed,
    OwnerSpeaking,
    OwnerWords,
    PisaWords,
    Speaking,
    TalkerConfig,
    TalkerError,
    TalkerEvent,
)
from pisa.services import talk
from pisa.services.talk import Exchange, Hooks, TalkRun, Thought
from tests.owner import owner_at
from tests.virtual_time import run_talk

CFG = TalkerConfig(key="sk-test-xxxx", voice="eve", lang="it-IT", instructions="rules")

# --- the whisper core ---------------------------------------------------------------------------------------


def test_a_whisper_is_framed_by_code_and_fresh_seeds_the_talker() -> None:
    w = Whisper(Kind.DONE, Priority.PAUSE, "Spostato alle 17.", id="w1")
    assert framed(w) == (
        "[w1 · Done by Pisa (you may say it was done) · at your next pause, or as the answer you owe] "
        "Spostato alle 17."
    )
    with pytest.raises(ValueError, match="names the whisper"):
        Whisper(Kind.CORRECT, Priority.NOW, "anzi")
    fresh = Fresh("martedì 29 ottobre, 15:40", next_item="Dentista alle 17", approvals=("Mando la mail?",))
    assert fresh.text() == (
        "Now: martedì 29 ottobre, 15:40. Next: «Dentista alle 17». Waiting for the user's yes: «Mando la "
        "mail?»."
    )
    assert fresh.whisper().priority is Priority.SILENT
    text = instructions(fresh, "x" * 5000, "it-IT", pisa_lang="it-IT")
    assert (
        "Speak Italian (tu)." in text and "x" * 1200 in text and "x" * 1201 not in text
    )  # the day's summary: ≤ ~300 tokens


def test_grounding_flags_a_done_never_given_and_a_fact_from_nowhere() -> None:
    fresh = Fresh("martedì, 15:40", next_item="Dentista alle 17")
    given = [Whisper(Kind.FACT, Priority.PAUSE, "Il Bar Centrale è aperto fino alle 2.")]
    assert grounding("Il dentista è alle 17.", given, "", fresh).ok
    assert grounding("Il Bar Centrale chiude alle 2.", given, "", fresh).ok
    bad = grounding("Fatto, l'ho spostato alle 19.", given, "spostalo", fresh)
    assert bad.done_without_done and bad.unsupported == ("19",)
    done = [Whisper(Kind.DONE, Priority.PAUSE, "Spostato alle 19.")]
    assert grounding("Fatto, l'ho spostato alle 19.", done, "", fresh).ok


# --- Grok Voice (xAI): the wire messages --------------------------------------------------------------------


def test_grok_voice_session_and_whispers_on_the_wire() -> None:
    from pisa.connectors import talker_xai as X

    update = X.session_update(CFG)["session"]
    assert update["voice"] == "eve" and update["turn_detection"] == {"type": "server_vad"}
    assert update["audio"]["input"] == {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": "grok-transcribe", "language_hint": "it"},
    }
    assert [t["name"] for t in update["tools"]] == ["ask_brain"]  # its only tool: the talker never acts
    now = Whisper(Kind.CORRECT, Priority.NOW, "Anzi, è chiuso.", replaces="w1")
    assert [e["type"] for e in X.whisper_events(now, responding=True)] == [
        "response.cancel", "conversation.item.create", "response.create",
    ]  # fmt: skip
    pause = Whisper(Kind.FACT, Priority.PAUSE, "Aperto fino alle 2.")
    assert [e["type"] for e in X.whisper_events(pause, responding=False)] == [
        "conversation.item.create", "response.create",
    ]  # fmt: skip
    assert [e["type"] for e in X.whisper_events(pause, responding=True)] == ["conversation.item.create"]
    silent = Whisper(Kind.FACT, Priority.SILENT, "contesto")
    item = X.whisper_events(silent, responding=False)
    assert len(item) == 1 and item[0]["item"]["role"] == "system"
    answer = X.answer_events("c1", pause, responding=False)
    assert answer[0]["item"]["type"] == "function_call_output" and answer[0]["item"]["call_id"] == "c1"
    assert json.loads(answer[0]["item"]["output"]) == {"whisper": framed(pause)}
    assert answer[1] == {"type": "response.create"}


def test_grok_voice_events_become_the_port_s() -> None:
    from pisa.connectors import talker_xai as X

    pcm = b"\x01\x02"
    assert X.parse({"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()}) == [
        Audio(pcm)
    ]
    assert X.parse(
        {"type": "conversation.item.input_audio_transcription.updated", "transcript": "sposta"}
    ) == [OwnerWords("sposta", final=False)]
    assert X.parse(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "sposta la"}
    ) == [OwnerWords("sposta la", final=True)]
    call = {"type": "response.function_call_arguments.done", "name": "ask_brain", "call_id": "c9",
            "arguments": json.dumps({"question": "sposta la cena"})}  # fmt: skip
    assert X.parse(call) == [AskBrain("c9", "sposta la cena")]
    assert X.parse({"type": "input_audio_buffer.speech_started"}) == [OwnerSpeaking(True)]
    assert (
        X.parse({"type": "response.function_call_arguments.done", "name": "other", "arguments": "{}"}) == []
    )


def test_a_grok_voice_session_holds_a_pause_until_the_talker_is_idle_and_gives_whole_words() -> None:
    from pisa.connectors.talker_xai import GrokVoiceSession

    class Socket:
        def __init__(self, incoming: list[dict[str, Any]]) -> None:
            self.incoming, self.sent = incoming, []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw)["type"])

        def __aiter__(self) -> "Socket":
            return self

        async def __anext__(self) -> str:
            if not self.incoming:
                raise StopAsyncIteration
            return json.dumps(self.incoming.pop(0))

    socket = Socket([
        {"type": "response.created"},
        {"type": "response.output_audio_transcript.delta", "delta": "Ciao, "},
        {"type": "response.output_audio_transcript.delta", "delta": "come va?"},
        {"type": "response.output_audio_transcript.done", "transcript": "Ciao, come va?"},
        {"type": "response.done"},
    ])  # fmt: skip
    session = GrokVoiceSession(socket)  # type: ignore[arg-type]

    async def go() -> list[TalkerEvent]:
        out: list[TalkerEvent] = []
        async for event in session.events():
            if isinstance(event, PisaWords) and event.text == "Ciao, ":  # mid-response: a PAUSE whisper waits
                await session.whisper(Whisper(Kind.FACT, Priority.PAUSE, "Aperto fino alle 2."))
            out.append(event)
        return out

    events = run_talk(go())
    assert [e.text for e in events if isinstance(e, PisaWords)] == [
        "Ciao, ",
        "Ciao, come va?",
        "Ciao, come va?",
    ]
    assert socket.sent == ["conversation.item.create", "response.create"]  # the answer after response.done
    assert isinstance(events[-1], Closed)


# --- Gemini Live: the SDK's own types -----------------------------------------------------------------------


def test_gemini_live_config_and_whispers_from_the_sdk_types() -> None:
    from google.genai import types

    from pisa.connectors import talker_gemini as G

    config = G.connect_config(TalkerConfig(**{**CFG.__dict__, "voice": "Aoede", "resume": "h-1"}))
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Aoede"  # type: ignore[union-attr]
    assert config.speech_config.language_code == "it-IT"  # type: ignore[union-attr]
    (decl,) = config.tools[0].function_declarations  # type: ignore[index, union-attr, misc]
    assert decl.name == "ask_brain" and decl.behavior == types.Behavior.NON_BLOCKING
    assert config.session_resumption.handle == "h-1"  # type: ignore[union-attr]
    assert config.context_window_compression.sliding_window is not None  # type: ignore[union-attr]
    assert config.input_audio_transcription is not None and config.output_audio_transcription is not None
    for priority, scheduling in ((Priority.NOW, "INTERRUPT"), (Priority.PAUSE, "WHEN_IDLE"),
                                 (Priority.SILENT, "SILENT")):  # fmt: skip
        response = G.function_response("c1", Whisper(Kind.FACT, priority, "x"))
        assert response.scheduling == types.FunctionResponseScheduling(scheduling) and response.id == "c1"
        assert response.will_continue is False
    assert G.client_turn(Whisper(Kind.FACT, Priority.SILENT, "x"))[1] is False
    assert G.client_turn(Whisper(Kind.DONE, Priority.PAUSE, "x"))[1] is True


def test_gemini_live_messages_become_the_port_s() -> None:
    from google.genai import types

    from pisa.connectors import talker_gemini as G

    message = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="che ore", finished=False),
            model_turn=types.Content(
                parts=[types.Part(inline_data=types.Blob(data=b"\x00\x01", mime_type="audio/pcm"))]
            ),
            output_transcription=types.Transcription(text="Sono le"),
        ),
        tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id="c2", name="ask_brain", args={"question": "che ore sono"})]
        ),
        session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle="h-2", resumable=True),
    )
    events = G.parse(message)
    assert OwnerWords("che ore", final=False) in events and Audio(b"\x00\x01") in events
    assert PisaWords("Sono le", final=False) in events and AskBrain("c2", "che ore sono") in events
    assert any(getattr(e, "handle", None) == "h-2" for e in events)
    done = G.parse(types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True)))
    assert PisaWords("", final=True) in done and Speaking(False) in done
    cut = G.parse(types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True)))
    # buffer dropped
    assert cut[0] == OwnerSpeaking(True) and PisaWords("", final=True, interrupted=True) in cut


# --- the run: a fake talker, a fake phone -------------------------------------------------------------------


class FakeSession:
    input_rate = 24000
    output_rate = 24000

    def __init__(self) -> None:
        self.queue: asyncio.Queue[TalkerEvent | None] = asyncio.Queue()
        self.whispers: list[Whisper] = []
        self.answers: list[tuple[str, Whisper]] = []
        self.audio: list[bytes] = []
        self.truncated: list[int] = []
        self.stopped = self.closed = self.ended_turns = 0

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def whisper(self, w: Whisper) -> None:
        self.whispers.append(w)

    async def answer(self, call_id: str, w: Whisper) -> None:
        self.answers.append((call_id, w))

    async def stop(self) -> None:
        self.stopped += 1

    async def truncate(self, audio_end_ms: int) -> None:
        self.truncated.append(audio_end_ms)

    async def end_turn(self) -> None:
        self.ended_turns += 1

    async def activity_start(self) -> None:
        return None

    async def activity_end(self) -> None:
        return None

    async def events(self) -> AsyncIterator[TalkerEvent]:
        while (event := await self.queue.get()) is not None:
            yield event

    async def close(self) -> None:
        self.closed += 1


class FakeLink:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.played: list[bytes] = []
        self.cleared = 0
        self.closed = 0
        self.buffered_ms = 0  # what is still to be heard on the phone
        self.on_closed: Any = None

    async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
        yield b"\x00\x00" * 480  # one frame of the user's audio, then the line stays quiet
        await asyncio.Event().wait()

    def play(self, pcm: bytes) -> None:
        self.played.append(pcm)

    def clear(self) -> None:
        self.cleared += 1
        self.played.clear()
        self.buffered_ms = 0

    @property
    def playing(self) -> bool:
        return self.buffered_ms > 0

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed += 1


def _run(
    thought: Thought | None = None, meter: bool = True
) -> tuple[TalkRun, FakeSession, FakeLink, list[Any]]:
    session, link, seen = FakeSession(), FakeLink(), []

    async def think(words: str, trace: str, yes_by_code: bool, origin: str = "words") -> Thought:
        seen.append(("think", words))
        if origin != "words":  # A tap in a drawing comes flagged, never by its words
            seen.append(("origin", origin))
        seen.append(("yes_by_code", yes_by_code))
        return thought or Thought("Domani alle 9 hai il dentista.", Kind.SAY)

    async def fresh() -> Fresh:
        return Fresh("adesso")

    async def metered() -> bool:
        return meter

    async def exchange(e: Exchange) -> None:
        seen.append(("exchange", e))

    hooks = Hooks(think=think, fresh=fresh, meter=metered, exchange=exchange)
    return TalkRun(uuid.uuid4(), session, link, hooks, "it-IT", Fresh("adesso")), session, link, seen


async def _until(check: Any, within: float = 2.0) -> None:
    for _ in range(int(within / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


def test_the_brain_answers_the_talker_s_call_in_a_whisper_and_the_phone_sees_both_sides() -> None:
    run, session, link, seen = _run()

    async def go() -> str:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(
            AskBrain("c1", "cosa ho domani")
        )  # the talker asks first: it waits for the words
        session.queue.put_nowait(OwnerWords("cosa ho domani?", final=True))
        await _until(lambda: session.answers)
        session.queue.put_nowait(Audio(b"\x01\x01"))
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Domani alle 9 hai il dentista.", final=True))
        session.queue.put_nowait(Speaking(False))
        await _until(lambda: any(m["type"] == "talk.said" for m in link.sent))
        link.messages.put_nowait({"type": "talk.end"})
        return await task

    assert run_talk(go()) == "owner"
    ((call_id, whisper),) = session.answers
    assert call_id == "c1" and whisper.kind is Kind.SAY and whisper.priority is Priority.PAUSE
    assert ("think", "cosa ho domani?") in seen  # the user's own words, never the talker's paraphrase
    types = [m["type"] for m in link.sent]
    assert types[0] == "talk.state" and "talk.transcript" in types and types[-1] == "talk.ended"
    assert {m["state"] for m in link.sent if m["type"] == "talk.state"} >= {"listening", "speaking", "idle"}
    assert link.played == [b"\x01\x01"] and session.audio  # Pisa's voice out, the user's in
    ((_, exchange),) = [s for s in seen if s[0] == "exchange"]
    assert exchange.whisper_kind == "say" and exchange.asked_brain and exchange.whisper_ms is not None
    assert exchange.first_sound_ms is not None
    assert session.closed == link.closed == 1 and run.incidents == 0


def test_done_is_always_said_a_plain_answer_already_given_goes_in_silently() -> None:
    async def go(thought: Thought, talker_answered: bool, said: str = "Sono le 15:40.") -> list[Whisper]:
        run, session, _, _ = _run(thought)
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("che ore sono?", final=True))
        if talker_answered:  # the talker answered from Fresh before the brain came back
            session.queue.put_nowait(PisaWords(said, final=True))
        await _until(lambda: session.whispers)
        run.end("owner")
        await task
        return session.whispers

    (said,) = run_talk(go(Thought("Sono le 15:40.", Kind.SAY), talker_answered=True))
    assert said.priority is Priority.SILENT and said.kind is Kind.FACT  # never answered twice
    # A different number isn't the answer: the brain's is said
    (fixed,) = run_talk(go(Thought("Sono le 15:40.", Kind.SAY), talker_answered=True, said="Sono le tre."))
    # A number word ("tre") corrects nothing aloud (only digits do): Pisa's answer is said
    assert fixed.priority is Priority.PAUSE and fixed.kind is Kind.SAY
    (done,) = run_talk(go(Thought("Spostata alle 21.", Kind.DONE), talker_answered=True))
    assert done.kind is Kind.DONE and done.priority is Priority.PAUSE  # RAIL: Pisa's action is always said
    (asked,) = run_talk(go(Thought("Mando la mail a Marco?", Kind.ASK), talker_answered=False))
    assert asked.kind is Kind.ASK and asked.priority is Priority.PAUSE


def test_a_fatto_said_without_a_done_is_corrected_at_once_and_counted() -> None:
    run, session, _, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(PisaWords("Fatto, l'ho spostato.", final=True))
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(go())
    (fix,) = session.whispers
    assert fix.kind is Kind.CORRECT and fix.priority is Priority.NOW and run.incidents == 1


def test_barge_in_by_touch_the_idle_close_the_budget_and_a_dropped_talker(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "TICK_S", 0.01)
    run, session, link, _ = _run()

    async def touch() -> str:
        task = asyncio.create_task(run.run())
        link.buffered_ms = 400  # Pisa is speaking on the phone
        link.messages.put_nowait({"type": "talk.stop"})
        await _until(lambda: session.stopped)
        run.end("owner")
        return await task

    # Pisa stops at once, its queued voice dropped
    assert run_talk(touch()) == "owner" and link.cleared == 1

    monkeypatch.setattr(talk, "IDLE_AFTER_S", 0.05)
    idle, _, idle_link, _ = _run()
    assert run_talk(idle.run()) == "idle"
    assert idle_link.sent[-1] == {"type": "talk.ended", "reason": "idle"}

    monkeypatch.setattr(talk, "IDLE_AFTER_S", 30.0)
    monkeypatch.setattr(talk, "METER_EVERY_S", 0.03)
    over, _, _, _ = _run(meter=False)
    assert run_talk(over.run()) == "budget"

    dropped, dropped_session, _, _ = _run()

    async def drop() -> str:
        task = asyncio.create_task(dropped.run())
        dropped_session.queue.put_nowait(Closed("provider", "closed (1006)"))
        return await task

    assert run_talk(drop()) == "fallback"  # the app's economy path takes the turn


# --- the service: economy, the guard, the ledger, the measures ----------------------------------------------


class FakeTalker:
    id = "xai"
    name = "Grok Voice"
    key_provider = "xai"
    default_voice = "eve"
    voices: tuple[str, ...] = ("eve",)
    usd_per_min = 0.08

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sessions: list[FakeSession] = []
        self.configs: list[TalkerConfig] = []

    async def open(self, cfg: TalkerConfig) -> FakeSession:
        if self.fail:
            raise TalkerError("Grok Voice refused the session (HTTP 503).", 503)
        self.configs.append(cfg)
        self.sessions.append(FakeSession())
        return self.sessions[-1]


def _service(db: Any, talker: FakeTalker, now: datetime, *, tick: bool = False) -> Any:
    """`tick`: the clock moves a microsecond each time it is read, so rows it stamps keep the order they were
    made in (a frozen clock tied them, and the table's physical order decided)."""
    import itertools

    ticks = itertools.count()

    def clock() -> datetime:
        return now + timedelta(microseconds=next(ticks)) if tick else now

    from pisa.db.pool import open_pool
    from pisa.infra.keys import KeyStore
    from tests.conftest import dsn

    async def make() -> Any:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        return pool

    return make, lambda pool, secrets: talk.TalkService(
        pool, KeyStore(secrets), {"xai": talker}, owner=owner_at(), clock=clock
    )


def test_economy_by_setting_by_the_guard_or_without_a_key_refuses_natural(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.domain.errors import PisaError
    from tests.assistant_env import set_settings
    from tests.conftest import FAKE_KEY, pair

    device = uuid.UUID(pair(db)[0])
    now = datetime(2026, 11, 4, 10, tzinfo=UTC)
    make, service_for = _service(db, FakeTalker(), now)

    async def attempt() -> str:
        pool = await make()
        try:
            service = service_for(pool, SecretStr(secrets_key))

            async def link_for(rate: int) -> Any:
                return FakeLink()

            async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
                return Thought("", Kind.SAY)

            try:
                await service.open(device, trigger="press", lang="it-IT", link_for=link_for, think=think)
            except PisaError as err:
                return f"{err.code}: {err.message}"
            return "opened"
        finally:
            await pool.close()

    assert asyncio.run(attempt()).startswith("provider_key_missing: Grok Voice needs the xai key")
    from pisa.infra.keys import KeyStore

    async def key() -> None:
        pool = await make()
        async with pool.connection() as conn:
            await KeyStore(SecretStr(secrets_key)).set(conn, "xai", FAKE_KEY)
        await pool.close()

    asyncio.run(key())
    set_settings(
        db, {"conversation": {"mode": "economy", "talker": "xai", "voices": {"xai": "eve", "gemini": "Kore"}}}
    )
    assert asyncio.run(attempt()).startswith("talk_economy: Conversation mode is economy")
    set_settings(
        db, {"conversation": {"mode": "natural", "talker": "xai", "voices": {"xai": "eve", "gemini": "Kore"}}}
    )
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'x:y', 'settled', 0, 0.5, true, 't', "
        "%s)",
        (now - timedelta(minutes=5),),
    )  # €0.50 of the default €1 day: the guard (50%)
    assert asyncio.run(attempt()).startswith("talk_economy: Today's AI spend passed 50% of a cap")


def test_a_session_is_metered_measured_and_closed_with_its_reason(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.domain.errors import PisaError
    from pisa.infra.keys import KeyStore
    from tests.conftest import FAKE_KEY, pair

    device = uuid.UUID(pair(db)[0])
    now = datetime.now(UTC)
    db.execute(
        "INSERT INTO approvals (id, kind, summary, payload, status, device_id, created_at, expires_at) "
        "VALUES (gen_random_uuid(), 'email_send', 'Mando la mail a Marco?', '{}', 'pending', %s, %s, %s)",
        (device, now, now + timedelta(minutes=5)),
    )
    talker = FakeTalker()
    make, service_for = _service(db, talker, now, tick=True)  # the two calls keep the order they were made

    async def go() -> tuple[Any, Any]:
        pool = await make()
        async with pool.connection() as conn:
            await KeyStore(SecretStr(secrets_key)).set(conn, "xai", FAKE_KEY)
        service = service_for(pool, SecretStr(secrets_key))
        link = FakeLink()

        async def link_for(rate: int) -> Any:
            assert rate == 24000
            return link

        async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
            return Thought("Ok.", Kind.SAY)

        session_id, _ = await service.open(
            device, trigger="wake", lang="it-IT", link_for=link_for, think=think
        )
        fake = talker.sessions[0]
        fake.queue.put_nowait(OwnerSpeaking(False))
        fake.queue.put_nowait(OwnerWords("ciao", final=True))
        await _until(lambda: fake.whispers or fake.answers)
        assert not service.end(session_id, uuid.uuid4())  # another device's DELETE ends nothing
        assert service.end(session_id, device)
        await asyncio.gather(*service._tasks)
        failing = service_for(pool, SecretStr(secrets_key))
        failing._talkers = {"xai": FakeTalker(fail=True)}
        try:
            await failing.open(device, trigger="press", lang="it-IT", link_for=link_for, think=think)
        except PisaError as err:
            refused = err
        await pool.close()
        return refused, talker.configs[0]

    refused, cfg = asyncio.run(go())
    assert refused.code == "talker_unavailable" and "HTTP 503" in refused.message
    assert (
        "Mando la mail a Marco?" in cfg.instructions and "You are Pisa's voice" in cfg.instructions
    )  # Fresh, rules
    row = db.execute(
        "SELECT talker, voice, trigger, end_reason, seconds IS NOT NULL, cost_eur IS NOT NULL "
        "FROM talk_sessions"
    ).fetchone()
    assert row == ("xai", "eve", "wake", "owner", True, True)
    assert db.execute("SELECT n, whisper_kind FROM talk_turns").fetchall() == [(1, "say")]
    calls = db.execute("SELECT slot, status FROM llm_calls ORDER BY created_at").fetchall()
    assert calls == [("voice.talk", "settled"), ("voice.talk", "failed")]  # metered; a refused open costs 0


def test_done_comes_only_from_code_s_record_of_the_turn(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from tests.conftest import pair

    device = uuid.UUID(pair(db)[0])
    now = datetime.now(UTC)
    make, service_for = _service(db, FakeTalker(), now)
    db.execute(
        "INSERT INTO pisa_decisions (at, trace_id, move, tool, provenance) "
        "VALUES (%s, 'acted', 'act', 'move_event', 'owner_words')",
        (now,),
    )
    db.execute(
        "INSERT INTO pisa_decisions (at, trace_id, move, tool, provenance, outcome) "
        "VALUES (%s, 'waiting', 'act', 'move_event', 'owner_words', 'pending')",
        (now,),
    )
    db.execute(
        "INSERT INTO pisa_decisions (at, trace_id, move, tool, provenance) "
        "VALUES (%s, 'asked', 'ask', 'ask_owner', 'owner_words')",
        (now,),
    )

    async def go() -> list[Kind]:
        pool = await make()
        service = service_for(pool, SecretStr(secrets_key))
        kinds = [
            await service.kind_of(device, "acted", now),
            await service.kind_of(device, "waiting", now),  # an act still pending is no "done"
            await service.kind_of(device, "asked", now),
            await service.kind_of(device, "none", now),  # the reply's words ("Fatto?") prove nothing
        ]
        await pool.close()
        return kinds

    assert asyncio.run(go()) == [Kind.DONE, Kind.SAY, Kind.ASK, Kind.SAY]


# --- the REST contract --------------------------------------------------------------------------------------


def test_the_rest_contract_economy_and_a_real_webrtc_offer(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from aiortc import RTCPeerConnection

    from tests.assistant_env import set_settings
    from tests.conftest import pair, set_key

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    set_settings(
        db, {"conversation": {"mode": "economy", "talker": "xai", "voices": {"xai": "eve", "gemini": "Kore"}}}
    )
    refused = client.post("/v1/talk/sessions", headers=auth, json={"sdp_offer": "v=0", "trigger": "press",
                                                                    "lang": "it-IT"})  # fmt: skip
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "talk_economy"
    set_settings(
        db, {"conversation": {"mode": "natural", "talker": "xai", "voices": {"xai": "eve", "gemini": "Kore"}}}
    )
    keyless = client.post("/v1/talk/sessions", headers=auth, json={"sdp_offer": "v=0", "trigger": "press",
                                                                    "lang": "it-IT"})  # fmt: skip
    assert keyless.status_code == 409 and keyless.json()["error"]["code"] == "provider_key_missing"
    set_key(db, secrets_key, provider="xai")
    talker = FakeTalker()
    client.app.state.runtime.talk._talkers = {"xai": talker}

    async def offer() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        pc.createDataChannel("pisa")
        await pc.setLocalDescription(await pc.createOffer())
        sdp = pc.localDescription.sdp
        await pc.close()
        return sdp

    sdp = asyncio.run(offer())
    made = client.post("/v1/talk/sessions", headers=auth, json={"sdp_offer": sdp, "trigger": "press",
                                                                "lang": "it-IT"})  # fmt: skip
    assert made.status_code == 201, made.text
    body = made.json()
    assert (
        body["sdp_answer"].startswith("v=0")
        and "a=candidate" in body["sdp_answer"]
        and body["ice_servers"] == []
    )
    ended = client.delete(f"/v1/talk/sessions/{body['session_id']}", headers=auth)
    assert ended.status_code == 204
    again = client.delete(f"/v1/talk/sessions/{uuid.uuid4()}", headers=auth)
    assert again.status_code == 404


def test_every_error_opening_a_talk_answers_is_in_the_openapi_document() -> None:
    """`POST /v1/talk/sessions` documents each error it answers under its status in the generated document."""
    from pathlib import Path

    from pisa.domain.errors import PisaError

    doc = json.loads((Path(__file__).resolve().parents[2] / "protocol" / "openapi.json").read_text())
    responses = doc["paths"]["/v1/talk/sessions"]["post"]["responses"]
    codes = (
        "validation_failed", "auth_failed", "session_expired", "device_revoked", "budget_exceeded",
        "talk_economy", "provider_key_missing", "voice_consent_missing", "voice_cohort_missing", "internal",
        "talker_unavailable",
    )  # fmt: skip
    for code in codes:
        status = str(PisaError(code, "-").http_status)
        assert code in responses[status]["description"], (code, status)
        assert responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
