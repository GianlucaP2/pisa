"""Two honesty rails from the default-install audit.

- A client that never declared the phone's actions (a run over plain HTTP) is offered no tool whose result is
  the phone acting (a handoff's sheet, the compose screen, a dial, the maps app), and a "Tap Send" with no
  button shown this turn is held. The reply's opening never comes out cut ('…With "Hi Tom…').
- With reminders on, a promise for later ("I'll remind you when the day starts", "al mattino ci penso io") is
  held unless the turn made a write that fires at its time (remember_to, keep_intention); after an untimed
  task the reply says it is on the list, with no alert at a set time.
"""

import json
from typing import Any

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.domain import abilities, claims
from pisa.domain import talk as form
from pisa.domain import tools as tool_registry
from tests.conftest import FakeModels, pair_v2, set_key
from tests.test_abilities import _ask
from tests.test_live_v2_socket import run

# the audit's reply, as the model wrote it over plain HTTP (its opening is the "on your phone" claim)
AUDIT_REPLY = (
    "I've put a message to Tom on your phone, with \"Hi Tom, I'll be about 10 minutes late. Sorry about "
    "that!\" Tap Send when you're ready."
)
QUOTED = '"Hi Tom, I\'ll be about 10 minutes late. Sorry about that!"'


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


# --- the phrases -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "promised"),
    [
        ("Go to sleep, and I'll remind you when the day starts.", True),
        ("Al mattino ci penso io.", True),
        ("Domattina ci penso io.", True),
        ("Ti avviso domani.", True),
        ("I'll remind you tomorrow.", True),
        ("Ti ricorderò domattina alle 8.", True),
        ("Don't worry, I'll take care of it tomorrow.", True),
        ("Ci penso io.", False),  # no time: about now
        ("Vuoi che te lo ricordi domani?", False),  # a question promises nothing
        ("Non posso ricordartelo domani.", False),
        ("Ti ricordo che domani hai il dentista.", False),  # a reminder now
        ("It's on your list for tomorrow.", False),
    ],
)
def test_a_promise_for_later_is_told_apart(sentence: str, promised: bool) -> None:
    assert abilities.promises_later(sentence) is promised


@pytest.mark.parametrize(
    ("sentence", "tap"),
    [
        ("Tap Send when you're ready.", True),
        ('" Tap Send when you\u2019re ready.', True),
        ("Tocca Invia quando vuoi.", True),
        ("Premi Invia.", True),
        ("Tap the button when you're ready.", True),
        ("Tocca il pulsante quando sei pronto.", True),
        ("Send it when you're ready.", False),
        ("Il messaggio è pronto da copiare.", False),
    ],
)
def test_a_tap_claim_is_told_apart(sentence: str, tap: bool) -> None:
    assert abilities.says_tap(sentence) is tap


def test_the_written_call_filter_leaves_prose_with_quotes_alone() -> None:
    """The cut opening was not the leak filter: `_CALL_SAY` needs a written `finish(`."""
    for piece in [AUDIT_REPLY, *form.split_sentences(AUDIT_REPLY)]:
        assert form.written_call(piece) == piece
    assert form.written_call('All set. finish(say="Hi Tom")') == "All set. Hi Tom"


def test_a_closing_quote_stays_with_its_sentence_and_a_cut_clause_leaves_no_dangling_with() -> None:
    first, second, third = form.split_sentences(AUDIT_REPLY)
    assert second == 'Sorry about that!" ' and third == "Tap Send when you're ready."
    kept, claim = claims.split_screen_claim(first)
    assert claim == "I've put a message to Tom on your phone."
    assert kept.startswith("\"Hi Tom, I'll be about") and not kept.lower().startswith("with")


def test_the_phone_acts_are_declared_tools() -> None:
    assert {"prepare_message", "compose_message", "call_place", "prepare_call"} <= tool_registry.PHONE_ACTS


# --- end to end, a scripted model ------------------------------------------------------------------------


def _calls(messages: Any) -> list[str]:
    return [p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def _scripted(tool: str, args: dict[str, Any], reply: str, offered: list[set[str]] | None = None) -> Any:
    rounds: list[int] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        if offered is not None:
            offered.append({t.name for t in info.function_tools})
        rounds.append(1)
        if len(rounds) == 1 and not _calls(messages):  # once: a withheld tool's call comes back refused
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}
            return
        yield reply

    return model


def test_over_http_no_phone_act_is_offered_and_tap_send_is_held_with_the_opening_whole(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    set_key(db, secrets_key)
    offered: list[set[str]] = []
    fake_models.default = _scripted(
        "prepare_message",
        {"to": "Tom", "text": "Hi Tom, I'll be about 10 minutes late."},
        AUDIT_REPLY,
        offered,
    )
    body = run("Tell Tom I'll be 10 minutes late", lang="en-US")["input"]
    answered = client.post("/v2/runs", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert answered.status_code == 200, answered.text
    events = answered.json()["events"]
    answer = "".join(e.get("delta", "") for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert offered and not any(tool_registry.PHONE_ACTS & names for names in offered)
    assert "Tap Send" not in answer and "on your phone" not in answer
    assert answer.startswith(QUOTED), answer  # the opening whole: never a dangling 'With "Hi Tom…'
    assert abilities.no_button("en-US") in answer
    assert db.execute("SELECT count(*) FROM handoffs").fetchone()[0] == 0


def test_an_untimed_task_does_not_back_a_reminder_promise(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = _scripted(
        "add_task",
        {"title": "Call the bank"},
        "Added it. Go to sleep, and I'll remind you when the day starts.",
    )
    answer = _ask(client, token, device_id, "I need to call the bank, remind me", lang="en-US")
    assert "remind you" not in answer
    assert "It's on your list; you won't get an alert at a set time." in answer
    assert db.execute("SELECT count(*) FROM tasks WHERE title = 'Call the bank'").fetchone()[0] == 1


def test_an_italian_promise_after_an_untimed_task_is_held_in_italian(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = _scripted(
        "add_task", {"title": "Chiamare la banca"}, "Aggiunta. Al mattino ci penso io."
    )
    answer = _ask(client, token, device_id, "devo chiamare la banca")
    assert "ci penso io" not in answer
    assert "L'ho messo in lista; non ti arriverà un avviso a un'ora precisa." in answer


def test_a_timed_promise_passes(client, device, db, secrets_key, fake_models: FakeModels) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = _scripted(
        "remember_to",
        {"what": "chiamare la banca", "condition": "time", "when": "08:00"},
        "Ti ricorderò domattina alle 8.",
    )
    answer = _ask(client, token, device_id, "ricordami domattina alle 8 di chiamare la banca")
    assert "Ti ricorderò domattina alle 8." in answer
    assert "non ti arriverà un avviso" not in answer
