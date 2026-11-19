"""Conversation service: history window, new conversation after silence, the time tool, sentence splitting."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.brain.conversation import SentenceSplitter
from pisa.services.conversation import ConversationService
from tests.conftest import FAKE_KEY, Clock, FakeModels, dsn, pair
from tests.gateway_env import gateway, run
from tests.owner import owner_at


def converse(db, secrets_key, fake, clock, texts):  # type: ignore[no-untyped-def]
    device_id = uuid.UUID(pair(db)[0])
    out: list[list[tuple[str, bool]]] = []

    async def go() -> None:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            service = ConversationService(pool, gw, owner=owner_at(), clock=clock)
            for text, advance in texts:
                clock.now += advance
                chunks: list[tuple[str, bool]] = []

                async def emit(delta: str, final: bool, chunks: list[tuple[str, bool]] = chunks) -> None:
                    chunks.append((delta, final))

                await service.respond(device_id, text, "it-IT", emit, trace_id="t")
                out.append(chunks)

    run(go)
    return out


def test_history_window_and_new_conversation_after_30_minutes(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake, clock = FakeModels(), Clock(datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
    turns = [(f"domanda {i}", timedelta(minutes=1)) for i in range(12)]
    turns.append(("dopo una pausa", timedelta(minutes=31)))
    converse(db, secrets_key, fake, clock, turns)

    def user_texts(messages: Any) -> list[str]:
        return [
            str(getattr(p, "content", ""))
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if getattr(p, "part_kind", "") == "user-prompt"
        ]

    eleventh: Any = fake.seen_messages[11]  # 11 earlier exchanges = 22 messages: the last 20 are read
    # The last 3 exchanges word for word, the 7 before them as Pisa's summary (one data block).
    assert len([m for m in eleventh if isinstance(m, ModelResponse)]) == 3
    texts = user_texts(eleventh)
    assert texts[0].startswith("Earlier in this conversation") and "the user: domanda 1" in texts[0]
    assert "domanda 0" not in texts[0] and texts[1:] == ["domanda 8", "domanda 9", "domanda 10", "domanda 11"]
    after_pause: Any = fake.seen_messages[12]
    assert user_texts(after_pause) == ["dopo una pausa"]
    assert db.execute("SELECT count(*) FROM conversations").fetchone() == (2,)
    assert db.execute("SELECT count(*) FROM messages").fetchone() == (26,)


def test_get_current_time_tool_uses_europe_rome(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake, clock = FakeModels(), Clock(datetime(2026, 10, 20, 22, 30, tzinfo=UTC))  # 00:30 on the 21st in Rome
    tool_results: list[str] = []

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            yield {0: DeltaToolCall(name="get_current_time", json_args="{}")}
            return
        tool_results.append(str(returns[-1].content))
        yield "Sono le 00:30."

    fake.default = reply
    chunks = converse(db, secrets_key, fake, clock, [("che ore sono?", timedelta())])[0]
    assert tool_results == ["Wednesday 2026-10-21 00:30 Europe/Rome"]
    assert chunks == [("Sono le 00:30.", True)]


def test_sentence_splitter_keeps_text_and_waits_for_boundaries() -> None:
    splitter = SentenceSplitter()
    deltas = ["Alle 13", " pranzo. Poi", " parti alle 13:40", ". Costa 3.5 euro! Ok?", " Va", " bene…\nFine"]
    sentences = [s for d in deltas for s in splitter.feed(d)]
    assert sentences == [
        "Alle 13 pranzo. ",
        "Poi parti alle 13:40. ",
        "Costa 3.5 euro! ",
        "Ok? ",
        "Va bene…\n",
    ]
    assert splitter.rest() == "Fine"
    assert "".join(sentences) + "Fine" == "".join(deltas)
