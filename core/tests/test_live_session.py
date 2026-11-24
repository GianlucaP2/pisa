"""The device session on `/v2/live`: a reply streamed sentence by sentence, partial transcripts, a turn's
failure named, cancel, a revocation from another process, and frames the session can't take named
(the handshake, replacement, turns, taps and resume are `test_live_v2_socket.py`)."""

import asyncio
import json
import time
import uuid

import psycopg
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import FAKE_KEY, FakeModels, dsn, set_key
from tests.live_app import device as device  # the fixture
from tests.live_app import live, of_type, said


@pytest.mark.parametrize(
    ("lang", "text", "reply"),
    [
        ("it-IT", "Pisa, che ore sono?", "Sono le tredici. Hai pranzo alle 13:30!"),
        ("en-US", "Pisa, what time is it?", "It is one pm. Lunch is at 13:30!"),
    ],
)
def test_conversation_streams_sentences(
    client, device, db, secrets_key, fake_models: FakeModels, lang, text, reply
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = reply
    with live(client, token, device_id) as app:
        turn = app.say(text, lang=lang, source="glasses_mic")
        chunks = app.reply()
        app.run_ends(turn["runId"])
        app.check()
    utterance_id = turn["messages"][0]["id"]
    assert all(c["type"] == "say" for c in chunks), chunks
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))
    assert {c["stream_id"] for c in chunks} == {chunks[0]["stream_id"]}
    assert all(c["reply_to"] == utterance_id and c["lang"] == lang for c in chunks)
    assert [c["final"] for c in chunks] == [False] * (len(chunks) - 1) + [True]
    assert said(chunks) == reply
    non_final = [c["delta"] for c in chunks[:-1]]
    assert non_final[0].strip() == reply.split(". ")[0] + "."  # one chunk per sentence
    # The model got the language instruction, and the key reached only the model client.
    assert fake_models.calls == [("openai", "gpt-6-luna", FAKE_KEY)]
    instructions = fake_models.seen_messages[0][-1].instructions  # type: ignore[index]
    assert ("Italian" if lang == "it-IT" else "English") in instructions
    row = db.execute(
        "SELECT status, input_tokens > 0, output_tokens > 0, cost_eur > 0 FROM llm_calls "
        "WHERE slot <> 'voice.speak'"
    ).fetchone()
    assert row == ("settled", True, True, True)
    roles = db.execute("SELECT role, content FROM messages ORDER BY id").fetchall()
    # what was streamed, as stored: no space at the reply's edges
    assert roles == [("user", text), ("assistant", "".join(c["delta"] for c in chunks).strip())]


def test_a_partial_transcript_is_never_a_turn(client, device, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with live(client, token, device_id, turn_hints=True) as app:
        app.partial("Pisa, che", str(uuid.uuid4()))
        assert of_type(app.ping(), "say") == []
        app.check()
    assert db.execute("SELECT count(*) FROM messages").fetchone() == (0,)


def test_missing_key_is_reported(client, device) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with live(client, token, device_id) as app:
        turn = app.say("ciao")
        ended = app.run_ends(turn["runId"])
        app.check()
    (err,) = app.sent
    assert err["type"] == "error" and err["code"] == "provider_key_missing"
    assert "OpenAI" in err["message"] and "Settings → AI providers" in err["action"]
    assert ended[-1]["event"]["type"] == "RUN_ERROR" and ended[-1]["event"]["code"] == "provider_key_missing"


def test_cancel_stops_the_stream_and_the_model_call(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)

    async def slow(messages, info):  # type: ignore[no-untyped-def]
        yield "Prima frase. "
        yield "Seconda "
        await asyncio.sleep(30)
        yield "mai."

    fake_models.default = slow
    with live(client, token, device_id) as app:
        turn = app.say("raccontami")
        first = app.next_of("say")[-1]
        assert first["delta"] == "Prima frase. " and not first["final"]
        app.cancel(turn["runId"])
        ended = app.run_ends(turn["runId"])
        after = app.ping()
        app.check()
    assert ended[-1]["event"]["outcome"] == {"type": "cancelled"}
    assert after == []  # no more chunks for that reply, not even a final one
    for _ in range(50):
        row = db.execute("SELECT status, error_code FROM llm_calls WHERE slot <> 'voice.speak'").fetchone()
        if row and row[0] != "reserved":
            break
        asyncio.run(asyncio.sleep(0.05))
    assert row == ("settled", "cancelled")


def test_revoke_from_another_process_closes_the_live_socket_with_4003(client, device, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.devices import revoke_device

    device_id, token = device
    with live(client, token, device_id) as app:

        async def revoke() -> None:  # like `pisa devices revoke`, from another process's connection
            async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
                await revoke_device(conn, uuid.UUID(device_id))

        started = time.monotonic()
        asyncio.run(revoke())
        err = app.receive()
        assert err["control"] == "error" and err["code"] == "device_revoked"
        assert err["message"] == "This device was revoked on the server."
        with pytest.raises(WebSocketDisconnect) as closed:
            app.ws.receive_text()
        assert closed.value.code == 4003
        assert time.monotonic() - started < 5


def test_frames_the_session_cannot_take_get_named_errors(client, device) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with live(client, token, device_id) as app:
        app.ws.send_text("{not json")
        bad_json = app.receive()
        assert bad_json["control"] == "error" and bad_json["code"] == "invalid_frame"
        assert "not valid JSON" in bad_json["message"]
        turn = {
            "threadId": "thread-1",
            "runId": "run-es",
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "x"}],
            "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "es-ES"}}},
        }
        app.ws.send_text(json.dumps({"op": "run", "input": turn}))  # a lang the session doesn't speak
        refused = app.run_ends("run-es")[-1]["event"]
        assert refused["type"] == "RUN_ERROR" and refused["code"] == "invalid_frame"
        assert "lang" in refused["message"]
        app.ws.send_text(json.dumps(app.hello))
        again = app.receive()
        assert again["control"] == "error" and "already said hello" in again["message"]
