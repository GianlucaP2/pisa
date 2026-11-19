"""Half an hour of talks: a refused Gemini
resumption sent the talk to economy; "Mi fermo qui, a metà" after all six changes were made; Gemini's
"<!-- No response -->" on the user's cards; "AJ" and "A" each a brain turn."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pisa.domain.whisper import Kind
from pisa.ports.talker import OwnerWords, PisaWords, TalkerConfig, TalkerError
from pisa.services.talk import Thought
from tests.test_live_talk import FakeLink, FakeSession, FakeTalker, _run, _service, _until
from tests.virtual_time import run_talk


class _Forgets(FakeTalker):
    """Gemini, 31 Oct 13:05: a resumption handle it no longer knows is refused (1008)."""

    async def open(self, cfg: TalkerConfig) -> FakeSession:
        self.configs.append(cfg)
        if cfg.resume is not None:
            raise TalkerError("1008 None. BidiGenerateContent session not found or expired", 1008)
        self.sessions.append(FakeSession())
        return self.sessions[-1]


def test_a_refused_resumption_opens_a_fresh_session_never_economy(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore
    from tests.conftest import FAKE_KEY, pair

    device = uuid.UUID(pair(db)[0])
    talker = _Forgets()
    make, service_for = _service(db, talker, datetime.now(UTC))

    async def go() -> None:
        pool = await make()
        async with pool.connection() as conn:
            await KeyStore(SecretStr(secrets_key)).set(conn, "xai", FAKE_KEY)
        service = service_for(pool, SecretStr(secrets_key))
        service._resume[device] = ("old-handle", datetime.now(UTC))

        async def link_for(rate: int) -> Any:
            return FakeLink()

        async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
            return Thought("Ok.", Kind.SAY)

        session_id, _ = await service.open(
            device, trigger="press", lang="it-IT", link_for=link_for, think=think
        )
        assert device not in service._resume  # the stale handle is forgotten
        service.end(session_id, device)
        await asyncio.gather(*service._tasks)
        await pool.close()

    asyncio.run(go())
    assert [c.resume for c in talker.configs] == ["old-handle", None]  # refused, then a fresh session


def test_gemini_s_placeholders_never_reach_the_owner_s_cards() -> None:
    run, session, link, _ = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(PisaWords("<!-- No response ", final=False))
        session.queue.put_nowait(PisaWords("<!-- No response -->", final=True))
        session.queue.put_nowait(PisaWords("[No speech detected]", final=True))
        session.queue.put_nowait(PisaWords("<no speech detected>", final=True))
        session.queue.put_nowait(PisaWords("Sì, ti sento.", final=True))
        await _until(lambda: any(m.get("text") == "Sì, ti sento." for m in link.sent))
        run.end("owner")
        await task

    run_talk(go())
    said = [m["text"] for m in link.sent if m.get("type") == "talk.said"]
    assert said[-1] == "Sì, ti sento." and not any("<" in s or "No speech" in s for s in said)


def test_a_noise_final_starts_no_brain_turn() -> None:
    run, session, _, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        for noise in ("AJ", "A", "e"):
            session.queue.put_nowait(OwnerWords(noise, final=True))
        session.queue.put_nowait(OwnerWords("ok", final=True))  # a yes is never noise
        session.queue.put_nowait(OwnerWords("5", final=True))  # nor a number ("a che ora?" "5")
        await asyncio.sleep(0.3)
        run.end("owner")
        await task

    run_talk(go())
    assert [e["text"] for e in run.timeline.events if e.get("kind") == "noise"] == ["AJ", "A", "e"]
    assert not [w for k, w in seen if k == "think" and w in ("AJ", "A", "e")]


def test_past_the_round_cap_the_model_says_what_it_did_with_no_tool_call() -> None:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from pisa.brain.spoken import MAX_ROUNDS, Loop, Round

    made: list[str] = []
    calls: list[int] = []
    listed: list[list[str]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        # The last call lists the same tools (the prefix) and may call none of them
        none = (info.model_settings or {}).get("tool_choice") == "none"
        calls.append(0 if none else len(info.function_tools))
        listed.append([t.name for t in info.function_tools])
        if not none:  # one write a round, as Luna did (12:38: six writes)
            yield {
                0: DeltaToolCall(
                    name="create_event", json_args='{"title": "Corsa"}', tool_call_id=f"c{len(calls)}"
                )
            }
        else:
            yield "Fatto: corsa, palestra, stampa e costume in calendario."

    loop = Round(lang="it-IT")
    agent = Agent(Loop(FunctionModel(stream_function=stream), loop))

    @agent.tool_plain
    def create_event(title: str) -> str:
        made.append(title)
        return "done"

    async def go() -> str:
        async with agent.run_stream("metti corsa, palestra, stampa, costume") as result:
            return await result.get_output()

    said = run_talk(go())
    assert said == "Fatto: corsa, palestra, stampa e costume in calendario."  # never "Mi fermo qui, a metà"
    assert len(made) == MAX_ROUNDS and calls[-1] == 0 and len(calls) == MAX_ROUNDS + 1
    assert listed[-1] == listed[0] == ["create_event"]  # the same tools listed (the prefix), none callable


def test_a_timeline_event_s_own_time_never_overwrites_its_offset() -> None:
    from pisa.services.talk import Timeline

    timeline = Timeline()
    timeline.started -= 5.0  # five seconds into the session
    timeline.add("first_content", took_ms=420, by="brain")
    timeline.add("reflex", ms=810)  # an old call site: kept, renamed
    (first, reflex) = timeline.events
    assert first["ms"] >= 5000 and first["took_ms"] == 420
    assert reflex["ms"] >= 5000 and reflex["its_ms"] == 810


def test_replay_1103_finish_with_the_same_words_as_text_is_said_once(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    """3 Nov Haiku replay: the model wrote its reply as text and called finish(say=the same) in one response;
    Pisa said the reply twice. The finish sentence is said only when it isn't already the response's text."""
    import json
    from typing import Any

    from pydantic_ai.models.function import DeltaToolCall

    from tests.test_acting import said, socket_turns

    words = "Nessuna allerta per Roma, né oggi né domani."

    class Twice:
        def __init__(self) -> None:
            self.seen = 0

        async def __call__(self, messages: Any, info: Any) -> Any:
            self.seen += 1
            yield words
            yield {0: DeltaToolCall(name="finish", json_args=json.dumps({"say": words}), tool_call_id="f1")}

    model = Twice()
    fake_models.default = model
    (frames,) = socket_turns(client, db, secrets_key, model, "ci sono allerte meteo?")  # type: ignore[arg-type]
    assert " ".join(said(frames).split()) == words
