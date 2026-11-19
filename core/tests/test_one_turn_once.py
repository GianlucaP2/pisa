"""One turn, its words once (the owner's first conversation: every message twice, Pisa's filler glued to one).

- On the wire (contract 2, `docs-site/build-a-client/text-chat.md` and `events.md`): the server never sends
  the user's own message back (no user TEXT_MESSAGE, no MESSAGES_SNAPSHOT, no transcript of a typed turn); the
  holding line ("Vediamo…") is Pisa's message of its own, with an id of its own, and the answer the next one.
- Self-hearing: the phone's recognizer kept listening while Pisa said its holding phrase and sent the user's
  words again with Pisa's glued after them ("Cosa devo fare oggi? Dunque."). That is no second turn."""

import asyncio
import uuid
from typing import Any

import pytest

from pisa.domain import self as self_rules
from tests.conftest import FakeModels, pair_v2, set_key
from tests.test_live_v2_socket import connect, events, hello, opened, run

QUESTION = "Cosa devo fare oggi?"


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


def _slow(answer: str, calls: list[int]) -> Any:
    async def reply(messages, info):  # type: ignore[no-untyped-def]
        calls.append(1)
        await asyncio.sleep(1.2)  # past the holding phrase's wait (0.3 s here)
        yield answer

    return reply


def _user_text_in(frames: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Every event that carries the user's words back, whatever its field."""
    return [f for f in frames if "event" in f and text in str(f["event"])]


def test_a_typed_turn_never_sends_the_user_s_message_and_the_holding_line_is_its_own_message(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    calls: list[int] = []
    fake_models.default = _slow("Ciao, sono qui.", calls)
    client.app.state.runtime.conversations.hold_after_s = 0.3
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run(QUESTION)
        app.send(turn)
        frames = app.run_ends(turn["input"]["runId"])
        app.check()
    kinds = [f["event"]["type"] for f in frames]
    assert kinds == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",  # the holding line
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",  # the answer
        "RUN_FINISHED",
    ]  # fmt: skip
    starts = events(frames, "TEXT_MESSAGE_START")
    assert all(s["role"] == "assistant" for s in starts)  # never a user message
    holding, answer = starts
    assert holding["metadata"]["pisa"]["holding"] is True and "holding" not in answer["metadata"]["pisa"]
    # each message has its own id: a UUID, unrelated to the answer's or the user's
    user_id = turn["input"]["messages"][0]["id"]
    assert uuid.UUID(holding["messageId"]) and holding["messageId"] not in (answer["messageId"], user_id)
    assert answer["messageId"] not in holding["messageId"] and user_id not in answer["messageId"]
    contents = events(frames, "TEXT_MESSAGE_CONTENT")
    assert [c["messageId"] for c in contents] == [holding["messageId"], answer["messageId"]]
    assert contents[0]["delta"] == "Vediamo…"  # the phrase alone: nothing of theirs, nothing of the answer
    assert _user_text_in(frames, QUESTION) == []  # the user's words never come back
    assert not [f for f in frames if f["event"].get("name") == "pisa.voice.understood"]
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'user'").fetchone() == (1,)


def test_their_words_again_with_pisa_s_holding_phrase_glued_on_are_one_turn(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    """Spoken: "Cosa devo fare oggi?", Pisa's holding phrase, then the recognizer's "Cosa devo fare oggi?
    <phrase>.": the second run ends with no words and the first is answered once."""
    device_id, token = device
    set_key(db, secrets_key)
    calls: list[int] = []
    fake_models.default = _slow("Oggi niente di urgente.", calls)
    client.app.state.runtime.conversations.hold_after_s = 0.3
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        first = run(QUESTION)
        first["input"]["forwardedProps"]["pisa"]["turn"]["source"] = "phone_mic"
        app.send(first)
        got = app.until(lambda f: (f.get("event") or {}).get("metadata", {}).get("pisa", {}).get("holding"))
        phrase = next(e["delta"] for e in events(app.until(lambda f: True), "TEXT_MESSAGE_CONTENT"))
        again = run(f"{QUESTION} {phrase.rstrip('…')}.")  # "Cosa devo fare oggi? Vediamo."
        again["input"]["forwardedProps"]["pisa"]["turn"]["source"] = "phone_mic"
        app.send(again)
        ended = app.run_ends(again["input"]["runId"])
        rest = app.run_ends(first["input"]["runId"]) if not any(
            f.get("runId") == first["input"]["runId"] and f["event"]["type"] == "RUN_FINISHED" for f in ended
        ) else []  # fmt: skip
        app.check()
    frames = got + ended + rest
    second = [f["event"] for f in frames if f.get("runId") == again["input"]["runId"]]
    assert [e["type"] for e in second] == ["RUN_STARTED", "RUN_FINISHED"]  # no words, no second answer
    first_events = [f["event"] for f in app.got if "event" in f and f.get("runId") == first["input"]["runId"]]
    assert first_events[-1]["outcome"] == {"type": "success"}  # never superseded by the echo
    assert (
        len(events([f for f in app.got if f.get("runId") == first["input"]["runId"]], "TEXT_MESSAGE_END"))
        == 2
    )
    # one turn: their words stored once, one answer
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'user'").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM messages WHERE role = 'assistant'").fetchone() == (1,)


def test_the_exact_case_dunque_is_an_echo_and_new_words_are_a_turn() -> None:
    saying = ["Dunque… "]  # the holding phrase, the reply under way
    assert self_rules.repeat_of("Cosa devo fare oggi? Dunque.", QUESTION, saying) == "echo"
    assert self_rules.repeat_of("cosa devo fare oggi", QUESTION, saying) == "same"
    # the start of Pisa's answer glued on is its echo too
    assert self_rules.repeat_of(f"{QUESTION} Oggi hai", QUESTION, ["Dunque… Oggi hai due cose."]) == "echo"
    # their own new words after the same start are a turn
    assert self_rules.repeat_of(f"{QUESTION} E domani?", QUESTION, saying) is None
    assert self_rules.repeat_of("E domani?", QUESTION, saying) is None
    assert self_rules.REPEAT_WITHIN_S == self_rules.ECHO_AFTER_S


def test_over_post_v2_runs_a_typed_turn_has_no_second_user_message_either(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    """The same run without the socket (JSON mode): the events carry no user message and no snapshot."""
    _, token = device
    set_key(db, secrets_key)
    fake_models.default = _slow("Domani sole.", [])
    client.app.state.runtime.conversations.hold_after_s = 0.3
    body = run("che tempo fa domani?")["input"]
    answered = client.post("/v2/runs", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert answered.status_code == 200, answered.text
    got = answered.json()["events"]
    assert not [e for e in got if e["type"] in ("MESSAGES_SNAPSHOT", "TEXT_MESSAGE_CHUNK")]
    starts = [e for e in got if e["type"] == "TEXT_MESSAGE_START"]
    assert [s["role"] for s in starts] == ["assistant", "assistant"]  # the holding line, the answer
    assert len({s["messageId"] for s in starts}) == 2
    assert not [e for e in got if "che tempo fa domani" in str(e)]
