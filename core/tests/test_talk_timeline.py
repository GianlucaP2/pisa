"""The live conversation working and visible. The timeline of a session (what happened, event by
event, and who closed it), the phone's `talk.end` reason, a session kept open while the user's voice arrives,
Gemini Live as the user heard it, and every external failure logged with its reason."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.domain.whisper import Kind
from pisa.ports.talker import AskBrain, Closed, OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services import talk
from pisa.services.talk import Thought
from tests.test_brain_loop import calendar  # noqa: F401  (the fixture)
from tests.test_live_talk import FakeLink, FakeTalker, _run, _service, _until
from tests.virtual_time import run_talk

LOUD = (b"\x00\x10" * 160) * 2  # 16-bit PCM with samples at 4096: a voice
QUIET = b"\x00\x00" * 320


def kinds(run: talk.TalkRun) -> list[str]:
    return [e["kind"] for e in run.timeline.events]


def first(run: talk.TalkRun, kind: str) -> dict[str, Any]:
    return next(e for e in run.timeline.events if e["kind"] == kind)


# --- the timeline -------------------------------------------------------------------------------------------


def test_the_timeline_holds_each_event_with_its_offset_and_who_closed_the_session(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "TICK_S", 0.02)
    run, session, link, _ = _run()

    async def go() -> str:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerWords("che ore", final=False))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(AskBrain("c1", "che ore sono"))
        session.queue.put_nowait(OwnerWords("che ore sono?", final=True))
        await _until(lambda: session.answers)
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Domani alle 9 hai il dentista.", final=True))
        session.queue.put_nowait(Speaking(False))
        await _until(lambda: "pisa_said" in kinds(run) and "audio" in kinds(run))
        link.messages.put_nowait({"type": "talk.end", "reason": "press"})
        return await task

    assert run_talk(go()) == "owner"
    seen = kinds(run)
    for kind in ("state", "owner_speaking", "owner_words", "ask_brain", "owner_final", "think", "whisper",
                 "pisa_speaking", "pisa_said", "audio", "close"):  # fmt: skip
        assert kind in seen, kind
    offsets = [e["ms"] for e in run.timeline.events]
    assert offsets == sorted(offsets) and all(isinstance(ms, int) for ms in offsets)
    assert first(run, "ask_brain") == {**first(run, "ask_brain"), "call": "c1", "question": "che ore sono"}
    whisper = first(run, "whisper")
    assert whisper["whisper_kind"] == "say" and whisper["priority"] == "pause" and whisper["call"] == "c1"
    assert whisper["after_final_ms"] >= 0 and whisper["text"] == "Domani alle 9 hai il dentista."
    assert first(run, "owner_final")["text"] == "che ore sono?"
    audio = first(run, "audio")
    assert audio["frames"] == 1 and audio["bytes"] == 960 and audio["peak"] == 0  # FakeLink's one quiet frame
    assert run.timeline.events[-1]["kind"] == "close"
    assert run.timeline.events[-1] == {**run.timeline.events[-1], "reason": "owner", "by": "phone",
                                       "detail": "press"}  # fmt: skip
    assert (run.closed_by, run.close_detail) == ("phone", "press")


def test_who_closed_it_the_talker_with_its_reason_a_failed_part_and_an_unknown_phone_reason() -> None:
    run, session, _, _ = _run()

    async def dropped() -> str:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(Closed("provider", "1011 Internal error encountered."))
        return await task

    assert run_talk(dropped()) == "fallback"
    assert (run.closed_by, run.close_detail) == ("talker", "provider: 1011 Internal error encountered.")

    broken, broken_session, _, _ = _run()

    async def fail(pcm: bytes) -> None:
        raise ConnectionError("received 1008 (policy violation) Quota exceeded")

    broken_session.send_audio = fail  # type: ignore[method-assign]
    assert run_talk(broken.run()) == "error"
    assert broken.closed_by == "error"
    assert broken.close_detail == "pump: ConnectionError: received 1008 (policy violation) Quota exceeded"
    assert first(broken, "failed")["detail"] == broken.close_detail

    odd, _, odd_link, _ = _run()

    async def end() -> str:
        task = asyncio.create_task(odd.run())
        odd_link.messages.put_nowait({"type": "talk.end", "reason": "<script>"})
        return await task

    assert run_talk(end()) == "owner" and (odd.closed_by, odd.close_detail) == ("phone", "")


def test_a_long_session_keeps_its_first_events_and_counts_the_rest(monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "TIMELINE_MAX", 3)
    timeline = talk.Timeline()
    for n in range(5):
        timeline.add("state", state=str(n), text="x" * 500)
    assert [e["state"] for e in timeline.events] == ["0", "1", "2"] and timeline.dropped == 2
    assert len(timeline.events[0]["text"]) == talk.TIMELINE_TEXT


# --- the user's voice keeps the session open (closed idle while the user spoke) ---------------


class VoiceLink(FakeLink):
    """The phone sends the user's voice for as long as the test runs: the talker hears nothing."""

    async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
        while True:
            yield LOUD
            await asyncio.sleep(0.005)


def test_the_owner_s_voice_keeps_the_session_open_and_a_deaf_talker_is_noted(monkeypatch: Any) -> None:
    from tests.virtual_time import LoopClock, VirtualTimeLoop

    monkeypatch.setattr(talk, "TICK_S", 0.01)
    monkeypatch.setattr(talk, "IDLE_AFTER_S", 0.05)
    monkeypatch.setattr(talk, "DEAF_AFTER_S", 0.05)
    monkeypatch.setattr(talk, "HAND_END_S", 0.1)
    monkeypatch.setattr(talk, "UNHEARD_SAY_S", 0.3)  # The line after a while of no words
    monkeypatch.setattr(talk, "LAST_FINAL_WAIT_S", 0.1)
    run, session, _, _ = _run()
    run.link = VoiceLink()

    async def go() -> str:
        task = asyncio.create_task(run.run())
        await asyncio.sleep(0.6)  # Never closed idle while the user's voice arrives
        assert not task.done()
        run.end("owner")
        return await task

    # On the loop's virtual clock: a loaded machine's wall stalled the voice past IDLE_AFTER_S now and then
    with monkeypatch.context() as virtual:
        virtual.setattr(talk, "time", LoopClock)
        assert asyncio.run(go(), loop_factory=VirtualTimeLoop) == "owner"
    assert "talker_deaf" not in kinds(run)  # Never while the voice goes on
    assert max(e["peak"] for e in run.timeline.events if e["kind"] == "audio") == 4096
    assert session.audio  # the voice reached the talker: the talker heard nothing of it
    # The deaf talker's turn was ended by hand, again and again; Pisa said once that it can't hear them
    # The close ends their turn once more, their voice still arriving (Gemini sends no partials)
    hand, close = kinds(run).count("hand_end"), kinds(run).count("last_final")
    assert hand >= 2 and close == 1 and hand + close == session.ended_turns
    said = [w.text for w in session.whispers if w.id == "unheard_voice"]
    assert said == ["Non ti sento bene, puoi ripetere?"]

    quiet, _, _, _ = _run()
    assert asyncio.run(quiet.run()) == "idle" and quiet.closed_by == "idle" and quiet.close_detail == ""


def test_a_talker_that_hears_the_owner_is_never_deaf(monkeypatch: Any) -> None:
    from tests.virtual_time import LoopClock, VirtualTimeLoop

    monkeypatch.setattr(talk, "TICK_S", 0.01)
    monkeypatch.setattr(talk, "DEAF_AFTER_S", 0.05)
    monkeypatch.setattr(talk, "time", LoopClock)  # on the loop's virtual clock: a loaded machine's wall
    # stalled past DEAF_AFTER_S between two of the user's words now and then
    run, session, _, _ = _run()
    run.link = VoiceLink()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        for _ in range(20):
            session.queue.put_nowait(OwnerWords("parlo", final=False))
            await asyncio.sleep(0.01)
        run.end("owner")
        await task

    asyncio.run(go(), loop_factory=VirtualTimeLoop)
    assert "talker_deaf" not in kinds(run)


# --- the service stores the timeline; the DELETE's reason ---------------------------------------------------


def test_the_service_stores_the_timeline_and_the_delete_s_reason(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore
    from tests.conftest import FAKE_KEY, pair

    device = uuid.UUID(pair(db)[0])
    talker = FakeTalker()
    make, service_for = _service(db, talker, datetime.now(UTC))

    async def go() -> None:
        pool = await make()
        async with pool.connection() as conn:
            await KeyStore(SecretStr(secrets_key)).set(conn, "xai", FAKE_KEY)
        service = service_for(pool, SecretStr(secrets_key))

        async def link_for(rate: int) -> Any:
            return FakeLink()

        async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
            return Thought("Ok.", Kind.SAY)

        session_id, _ = await service.open(
            device, trigger="press", lang="it-IT", link_for=link_for, think=think
        )
        fake = talker.sessions[0]
        fake.queue.put_nowait(OwnerWords("ciao", final=True))
        await _until(lambda: fake.whispers or fake.answers)
        assert service.end(session_id, device, "call")
        await asyncio.gather(*service._tasks)
        await pool.close()

    asyncio.run(go())
    timeline, by, detail = db.execute(
        "SELECT timeline, closed_by, close_detail FROM talk_sessions"
    ).fetchone()
    assert (by, detail) == ("phone", "call")
    assert timeline["dropped"] == 0 and timeline["events"][-1]["kind"] == "close"
    assert any(e["kind"] == "owner_final" and e["text"] == "ciao" for e in timeline["events"])


def test_the_rest_delete_takes_the_phone_s_reason(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    _, token = pair(db)
    response = client.delete(
        f"/v1/talk/sessions/{uuid.uuid4()}?reason=call", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404  # no live session: the reason changes nothing else


# --- R5: every external failure logged with its reason ------------------------------------------------------


def test_a_failure_s_reason_never_carries_a_key_or_a_query_string() -> None:
    from pisa.failures import reason_of

    assert reason_of(None) is None and len(reason_of("x" * 900) or "") == 400
    text = (
        # fake keys, for the redaction (gitleaks reads them as keys)
        "Invalid key=AIzaSyA1234567890abcdefghijklmn "  # gitleaks:allow
        "at https://maps.googleapis.com/v1/places?key=AIzaSyA12 "
        '{"api_key": "abcd1234"} Authorization: Bearer ya29.a0AfH6SMBxyz sk-proj-abcdefghijk12345 secret-1234'
    )
    clean = reason_of(text, "secret-1234") or ""
    for leak in ("AIza", "abcd1234", "ya29", "sk-proj", "secret-1234", "?key"):
        assert leak not in clean, leak
    assert "https://maps.googleapis.com/v1/places?…" in clean and "Invalid key=****" in clean
    assert reason_of('Unknown name "proactivity" at "setup"') == 'Unknown name "proactivity" at "setup"'


def test_one_redaction_for_logs_source_errors_and_google_reasons() -> None:
    import httpx

    from pisa.connectors.google.errors import google_reason
    from pisa.failures import redact

    # a known secret and a key-like token (fake), both masked by the one helper
    said = redact("login refused for hunter-2222, token: fake-token-0000", "hunter-2222")
    assert said == "login refused for ****, token: ****"
    body = {"error": {"message": "Bad request, key=AIzaFakeFakeFakeFakeFakeFake0"}}  # gitleaks:allow
    assert google_reason(httpx.Response(400, json=body)) == "Bad request, key=****"
    plain = {"error": {"message": "Gmail API has not been used in project 123."}}
    assert google_reason(httpx.Response(403, json=plain)) == "Gmail API has not been used in project 123"


def test_maps_web_weather_and_speech_failures_log_the_provider_s_reason() -> None:
    import httpx
    from structlog.testing import capture_logs

    from pisa.connectors.google_maps import GoogleMaps
    from pisa.connectors.open_meteo import OpenMeteo
    from pisa.connectors.speech import ElevenLabsSpeech
    from pisa.connectors.tavily import Tavily
    from pisa.ports.places import PlaceProviderError
    from pisa.ports.speech import SpeechError
    from pisa.ports.weather import WeatherError

    def reply(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "googleapis" in host:
            return httpx.Response(
                403, json={"error": {"message": "Places API (New) has not been used in project 42"}}
            )
        if "tavily" in host:
            return httpx.Response(400, json={"detail": {"error": "Query is too long."}})
        if "open-meteo" in host:
            return httpx.Response(502, text="Bad Gateway")
        if "elevenlabs" in host:
            return httpx.Response(401, json={"detail": {"status": "invalid_api_key"}})
        return httpx.Response(403, json={"reason": "InvalidProviderToken"})

    async def meter(*args: Any) -> None:
        return None

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            calls = (
                GoogleMaps(http, "AIzaSyKEYKEYKEYKEYKEYKEY12", meter).search(
                    "bar", near=None, open_now=False, limit=3, lang="it-IT"
                ),
                Tavily(http, "tvly-KEYKEYKEYKEY", meter).search("bar", max_results=1),
                OpenMeteo(http).forecast(52.0, 4.0, "Europe/Rome"),
                ElevenLabsSpeech(http).check_key("el-KEYKEYKEYKEY"),
            )
            for call in calls:
                try:
                    await call
                except (PlaceProviderError, WeatherError, SpeechError):
                    pass

    with capture_logs() as logs:
        asyncio.run(go())
    seen = {e["provider"]: e for e in logs if e["event"] == "external_failure"}
    assert seen["google_maps"]["status"] == 403 and seen["google_maps"]["op"] == "POST /v1/places:searchText"
    assert "has not been used in project 42" in seen["google_maps"]["reason"]
    assert seen["tavily"]["status"] == 400 and "Query is too long" in seen["tavily"]["reason"]
    assert seen["open_meteo"]["status"] == 502 and seen["open_meteo"]["reason"] == "Bad Gateway"
    assert seen["elevenlabs"]["status"] == 401 and "invalid_api_key" in (seen["elevenlabs"]["reason"] or "")
    for provider in seen.values():
        assert "KEYKEY" not in str(provider)


def test_a_failed_model_call_logs_the_provider_s_own_reason_without_the_key(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.exceptions import ModelHTTPError
    from structlog.testing import capture_logs

    from pisa.domain.errors import PisaError
    from tests.conftest import FAKE_KEY, FakeModels, dsn
    from tests.gateway_env import gateway, run
    from tests.test_ledger import ask

    fake = FakeModels()
    body = {"error": {"message": f"Rate limit reached for gpt-6-luna; key {FAKE_KEY} on tokens per min"}}
    fake.replies["gpt-6-luna"] = ModelHTTPError(429, "gpt-6-luna", body=body)

    async def go() -> None:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            try:
                await gw.run("chat.conversation", ask, prompt_chars=40, max_output_tokens=100)
            except PisaError:
                pass

    with capture_logs() as logs:
        run(go)
    (failure,) = [e for e in logs if e["event"] == "external_failure"]
    assert failure["provider"] == "openai" and failure["status"] == 429
    assert failure["op"] == "model chat.conversation" and "Rate limit reached" in failure["reason"]
    assert FAKE_KEY not in str(failure)


def test_a_failed_google_call_and_a_gemini_close_carry_google_s_reason() -> None:
    import httpx
    from google.genai import errors as genai_errors
    from structlog.testing import capture_logs

    from pisa.connectors.google.api import GoogleApi
    from pisa.connectors.google.errors import ConnectorError
    from pisa.connectors.talker_gemini import GeminiLiveSession

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "Calendar usage limits exceeded."}})

    async def token(force: bool) -> str:
        return "ya29.secret-token"

    class Dying:
        async def receive(self) -> AsyncIterator[Any]:
            raise genai_errors.APIError(
                1011, {"message": "Internal error encountered.", "status": "INTERNAL"}
            )
            yield None

    async def go() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            try:
                await GoogleApi(http, token, service="Google Calendar").get(
                    "https://www.googleapis.com/calendar/v3/calendars/primary/events?q=dentista"
                )
            except ConnectorError:
                pass
        session = GeminiLiveSession(Dying(), contextlib.AsyncExitStack())
        return [event async for event in session.events()]

    with capture_logs() as logs:
        events = asyncio.run(go())
    failures = {e["provider"]: e for e in logs if e["event"] == "external_failure"}
    google = failures["google_calendar"]
    assert google["status"] == 403 and google["op"] == "GET /calendar/v3/calendars/primary/events"
    assert "Calendar usage limits exceeded" in google["reason"] and "ya29" not in str(google)
    assert failures["gemini_live"]["status"] == 1011
    assert events == [Closed("provider", "1011 Internal error encountered.")]


# --- (d) the brain's latency on the voice path: the smallest request that answers ---------------------------


class Measured:
    """A conversation model that keeps each request's parts, in characters: the instructions (the system
    prompt and the turn's own lines) and the tools offered."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        import json

        tools = {t.name: len(json.dumps(t.parameters_json_schema)) + len(t.description or "")
                 for t in info.function_tools}  # fmt: skip
        self.requests.append({"instructions": len(info.instructions or ""), "tools": sum(tools.values()),
                              "names": set(tools)})  # fmt: skip
        yield "Alle 17:30 hai la palestra."


def test_d_a_voice_turn_offers_the_core_tools(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain import tool_search
    from tests.conftest import pair
    from tests.test_acting import socket_turns

    model = Measured()
    fake_models.default = model
    device = pair(db)
    socket_turns(client, db, secrets_key, model, "che ora ho la palestra?", device=device)  # type: ignore[arg-type]
    economy = model.requests[-1]
    conversations = client.app.state.runtime.conversations

    async def voice() -> str:
        async def emit(delta: str, final: bool) -> None:
            return None

        return await conversations.respond(uuid.UUID(device[0]), "che ora ho la palestra?", "it-IT", emit,
                                           trace_id=uuid.uuid4().hex, voice=True)  # fmt: skip

    client.portal.call(voice)
    spoken = model.requests[-1]
    assert spoken["names"] <= tool_search.VOICE_ALWAYS | {"search_tools"}
    assert tool_search.VOICE_ALWAYS < tool_search.ALWAYS
    assert spoken["tools"] < economy["tools"] * 0.6  # The cards' schemas are found on demand
    print(f"\neconomy: instructions {economy['instructions']}, tools {economy['tools']} chars "
          f"({len(economy['names'])}); voice: instructions {spoken['instructions']}, tools {spoken['tools']} "
          f"chars ({len(spoken['names'])})")  # fmt: skip


def test_d_a_voice_turn_restores_half_the_history() -> None:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from pisa.brain import history
    from pisa.brain.conversation import Turn
    from pisa.services.conversation import ConversationService

    msgs: list[Any] = [ModelRequest(parts=[UserPromptPart("cerca ristoranti")])]
    for r in range(3):
        calls = [ToolCallPart("read_page", {}, tool_call_id=f"c{r}{k}") for k in range(3)]
        msgs.append(ModelResponse(parts=list(calls)))
        msgs.append(ModelRequest(parts=[ToolReturnPart("read_page", "y" * 1500, tool_call_id=c.tool_call_id)
                                        for c in calls]))  # fmt: skip
    msgs.append(ModelResponse(parts=[TextPart("ecco")]))
    raw = history.kept(msgs)
    turns = [Turn("user", "cerca ristoranti", False), Turn("assistant", "ecco", False, raw)] * 2
    turns.append(Turn("user", "che ora ho la palestra?", False))
    economy = history.tokens(ConversationService._history(turns))
    voice = history.tokens(ConversationService._history(turns, history.VOICE_BUDGET_TOKENS))
    assert voice <= history.VOICE_BUDGET_TOKENS < economy <= history.BUDGET_TOKENS


# --- R3: a reply is never cut by the output cap ---------------------------------------------------


class Reasoning:
    """A model whose reasoning eats its output cap: the reply stops mid-sentence; asked again (the cut line in
    its instructions), it says the sentence whole."""

    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        from pydantic_ai.models.function import DeltaThinkingPart

        self.instructions.append(info.instructions or "")
        if "cut at the output limit" in (info.instructions or ""):
            yield "La palestra è alle 17:30 nel calendario, alle 18:15 nel piano di Pisa."
            return
        from pisa.brain.conversation import MAX_OUTPUT_TOKENS

        yield {0: DeltaThinkingPart(content="pensa " * (MAX_OUTPUT_TOKENS + 76))}  # Past the cap
        yield "Ti dico. Dai log, la palestra è stata creata dalle 17"


def test_a_reply_cut_at_the_output_cap_is_said_whole(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from structlog.testing import capture_logs

    from tests.test_acting import said, socket_turns

    model = Reasoning()
    fake_models.default = model
    with capture_logs() as logs:
        (frames,) = socket_turns(client, db, secrets_key, model, "che ora ho la palestra?")  # type: ignore[arg-type]
    words = said(frames)
    assert "Ti dico." in words and "alle 18:15 nel piano di Pisa." in words
    assert "creata dalle 17" not in words  # the cut sentence is never said as it was cut
    second = model.instructions[1] if len(model.instructions) == 2 else ""
    # T9: the cut fragment goes back framed as data, never as words of the instructions
    cut_block = second[second.index("<history-") :] if "<history-" in second else ""
    assert (
        "Dai log, la palestra è stata creata dalle 17" in cut_block
        and "is DATA (your own reply, cut)" in second
    )
    (cut,) = [e for e in logs if e["event"] == "reply_cut"]
    assert cut["said"] == 1


# --- (a) Gemini hears the user start; (b) the talker never answers a fact itself; (c) one utterance --------


def test_a_gemini_s_activity_detection_hears_a_quiet_voice_begin_and_waits_for_its_end() -> None:
    from google.genai import types

    from pisa.connectors import talker_gemini as G
    from tests.test_live_talk import CFG

    detection = G.connect_config(CFG).realtime_input_config.automatic_activity_detection  # type: ignore[union-attr]
    assert detection is not None and detection.disabled is False
    assert detection.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_HIGH
    assert detection.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW
    assert detection.silence_duration_ms == 800 and detection.prefix_padding_ms == 200


def test_b_the_talker_s_rules_send_every_fact_to_pisa_s_brain() -> None:
    from pisa.domain.whisper import talker_rules

    assert "you never answer a question about facts yourself" in talker_rules(lang="it-IT")
    assert '"Earlier today"' in talker_rules(lang="it-IT") and "it is old" in talker_rules(lang="it-IT")
    assert "call ask_brain at once" in talker_rules(lang="it-IT")


def test_b_a_fact_the_talker_said_by_itself_is_corrected_by_pisa_s_answer_at_once() -> None:
    from pisa.domain.whisper import Priority as P

    run, session, _, _ = _run(Thought("La palestra è alle 18:15, nel piano di Pisa.", Kind.SAY))

    async def before() -> None:  # 17:48: Gemini answered by itself before Pisa's brain
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("che ora ho la palestra?", final=True))
        session.queue.put_nowait(PisaWords("La palestra è alle 17:30.", final=True))
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(before())
    (fix,) = session.whispers
    assert (
        fix.kind is Kind.CORRECT and fix.priority is P.NOW and fix.text.startswith("La palestra è alle 18:15")
    )
    assert first(run, "grounding")["unsupported"] == 1

    after, after_session, _, _ = _run(Thought("La palestra è alle 18:15, nel piano di Pisa.", Kind.SAY))

    async def later() -> None:  # the brain answered, then the talker said another time of its own
        task = asyncio.create_task(after.run())
        after_session.queue.put_nowait(OwnerWords("che ora ho la palestra?", final=True))
        await _until(lambda: after_session.whispers)
        after_session.queue.put_nowait(PisaWords("Alle 17:30, ricordi?", final=True))
        await _until(lambda: len(after_session.whispers) == 2)
        after.end("owner")
        await task

    run_talk(later())
    answer, again = after_session.whispers
    assert answer.kind is Kind.SAY and again.kind is Kind.CORRECT and again.priority is P.NOW
    assert again.text == answer.text and again.replaces == "talker"


def test_b_a_talker_that_said_the_brain_s_own_answer_first_is_not_corrected() -> None:
    from pisa.domain.whisper import Priority as P

    run, session, _, _ = _run(Thought("La palestra è alle 18:15.", Kind.SAY))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("che ora ho la palestra?", final=True))
        session.queue.put_nowait(PisaWords("Alle 18:15 la palestra.", final=True))
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(go())
    (said,) = session.whispers
    assert said.priority is P.SILENT  # it already said Pisa's answer: never twice


def test_c_the_owner_s_words_twice_in_one_final_are_said_once() -> None:
    from pisa.connectors.talker_gemini import joined
    from pisa.services.talk import one_utterance

    assert (
        one_utterance("Che ora ho l'appuntamento? Che ora ho l'appuntamento?") == "Che ora ho l'appuntamento?"
    )
    assert one_utterance("Che ora ho? Che ora ho l'appuntamento dal dentista?") == (
        "Che ora ho l'appuntamento dal dentista?"
    )
    assert one_utterance("Che ora ho l'appuntamento? Che ora ho") == "Che ora ho l'appuntamento?"
    for kept in ("no no", "sì sì, va bene", "metti la call alle 5, metti la call alle 6",
                 "a che ora ho la call, a che ora ho la palestra"):  # fmt: skip
        assert one_utterance(kept) == kept
    # Gemini's pieces are deltas; a piece that restates the words so far (sent whole again) replaces them
    assert joined("Che ora", " ho l'appuntamento?") == "Che ora ho l'appuntamento?"
    assert joined("Che ora ho l'appuntamento?", " Che ora ho l'appuntamento?") == "Che ora ho l'appuntamento?"
    assert joined("sì", " sì, va bene") == "sì sì, va bene"


def test_c_one_final_with_the_words_twice_reaches_the_brain_once() -> None:
    run, session, _, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(
            OwnerWords("che ora ho l'appuntamento? Che ora ho l'appuntamento?", final=True)
        )
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(go())
    assert ("think", "Che ora ho l'appuntamento?") in seen  # the later, whole form


# --- R4: the words of a turn that got no answer count for the next turn's writes ------------------


class Hangs:
    """The first words get no answer (the model never comes back: the user speaks again, a 68 s turn);
    the next ones put the event in the calendar, citing the first words."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        import json

        from pydantic_ai.messages import ToolReturnPart, UserPromptPart
        from pydantic_ai.models.function import DeltaToolCall

        words = [str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart)][-1]
        if words.startswith("domani alle 10"):
            self.started.set()
            await asyncio.sleep(60)
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            yield "Fatto."
            return
        args = {"title": "Palestra", "start": "domani alle 10", "owner_said": "domani alle 10 palestra"}
        yield {0: DeltaToolCall(name="create_event", json_args=json.dumps(args))}


def test_an_unanswered_turn_s_words_count_for_the_next_turn_s_write(  # type: ignore[no-untyped-def]
    client,
    db,
    calendar,  # noqa: F811  (the fixture)
    fake_models,
    secrets_key,
    clock,
) -> None:
    from pisa.brain.untrusted import watch_turn
    from tests.conftest import pair, set_key

    set_key(db, secrets_key)
    model = Hangs()
    fake_models.default = model
    device = uuid.UUID(pair(db)[0])
    conversations = client.app.state.runtime.conversations

    async def emit(delta: str, final: bool) -> None:
        return None

    async def go() -> str:
        watch_turn()
        first = asyncio.create_task(conversations.respond(device, "domani alle 10 palestra", "it-IT", emit,
                                                          trace_id=uuid.uuid4().hex))  # fmt: skip
        await asyncio.wait_for(model.started.wait(), 60)  # bounds a hang only: 10 s ran out under load
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        return await conversations.respond(device, "mettilo in calendario", "it-IT", emit,
                                           trace_id=uuid.uuid4().hex)  # fmt: skip

    client.portal.call(go)
    titles = [e.get("summary") for e in calendar.events.values()]
    assert "Palestra" in titles  # acted on the user's own words, though they came a turn before
    pending = db.execute("SELECT count(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
    assert pending == 0


# --- R1: the plan never moves the user's event; a task said with its time is a block at that time -------


@pytest.mark.usefixtures("owner_plans_every_task")
def test_an_event_pisa_made_at_the_owner_s_time_never_moves_for_a_stop(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from psycopg.types.json import Jsonb

    from tests.assistant_env import rome
    from tests.test_plan_insert import at, items
    from tests.test_soft_events import day, lunch_before_the_meeting

    ids = day(db, "soft")
    db.execute("UPDATE calendar_events SET kind_owner = NULL WHERE id = %s", (ids["coffee"],))
    db.execute(  # Pisa made it, at the user's time (the gym at 17:30, said by the user)
        "INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at, origin) "
        "VALUES (gen_random_uuid(), 'calendar_create', 'Caffè con Marta', %s, 'done', now(), now(), 'words')",
        (Jsonb({"body": {"id": ids["coffee"]}}),),
    )
    plan = lunch_before_the_meeting(db, secrets_key, ids)
    events = items(plan, "event") if "items" in plan else []
    coffee = [i for i in events if i["title"] == "Caffè con Marta"]
    assert all("moved_from" not in i and at(i["start"]) == rome(2026, 10, 24, 12) for i in coffee)


def test_a_task_said_with_its_own_time_is_a_block_at_that_time(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome, set_settings
    from tests.test_plan_insert import at, items, service

    set_settings(db, {"language": "it-IT", "places": {"default_mode": "walk"}})

    async def add(planner: Any, options: Any) -> Any:
        tasks = planner._tasks
        await tasks.create(title="Prepararmi", source_kind="voice", start_at="2026-10-24T19:00",
                           duration_min=45)  # fmt: skip
        await tasks.create(title="Comprare il pane", source_kind="voice", due_at="2026-10-24T19:00")
        return await planner.plan()

    plan = service(db, secrets_key, add)
    timed = next(i for i in items(plan, "task") if i["title"] == "Prepararmi")
    assert at(timed["start"]) == rome(2026, 10, 24, 19) and at(timed["end"]) == rome(2026, 10, 24, 19, 45)
    assert timed["why"] == "All'ora che hai detto."
    row = db.execute(
        "SELECT due_kind, expected_from IS NOT NULL FROM tasks WHERE title = 'Prepararmi'"
    ).fetchone()
    assert row == ("datetime", True)
    bread = db.execute("SELECT expected_from FROM tasks WHERE title = 'Comprare il pane'").fetchone()
    assert bread == (None,)  # a deadline stays a deadline


# --- R2: the brain sees today's plan as the user sees it ---------------------------------------------------


class Heard:
    """A model that keeps each request's instructions and answers plainly."""

    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        self.instructions.append(info.instructions or "")
        yield "Alle 18:15 nel piano di Pisa; nel calendario alle 17:30."


def test_the_brain_reads_today_s_plan_at_the_times_the_owner_sees(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock
) -> None:
    import json

    from tests.assistant_env import rome
    from tests.test_acting import socket_turns

    clock.now = rome(2026, 10, 30, 17, 48)  # 17:48: "che ora ho la palestra?"
    plan = {"items": [
        {"kind": "event", "title": "Riunione", "start": rome(2026, 10, 30, 9).isoformat(),
         "end": rome(2026, 10, 30, 10).isoformat()},
        {"kind": "event", "title": "Palestra", "start": rome(2026, 10, 30, 18, 15).isoformat(),
         "end": rome(2026, 10, 30, 19, 15).isoformat(), "moved_from": rome(2026, 10, 30, 17, 30).isoformat()},
        {"kind": "task", "title": "Prepararmi", "start": rome(2026, 10, 30, 19).isoformat(),
         "end": rome(2026, 10, 30, 19, 45).isoformat()},
    ]}  # fmt: skip
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
        "'2026-10-30', %s, 'test', 'f', %s)",
        (clock.now, json.dumps(plan)),
    )
    model = Heard()
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, "che ora ho la palestra?")  # type: ignore[arg-type]
    seen = model.instructions[0]
    assert (
        "Today in Pisa's plan, as the user sees it in Today: 18:15-19:15 Palestra "
        "(moved by Pisa from 17:30" in seen
    )
    assert "19:00-19:45 Prepararmi" in seen and "Riunione" not in seen  # what is over is left out
    assert "answer with the time in Today in Pisa's plan above" in seen
