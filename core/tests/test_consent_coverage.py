"""A consent covers a later AI disclosure that sends the same or less (rc.5: the app switched the
conversation to economy, which drops the natural talker, and every run then asked for consent again). Only a
new or changed processor, model or data category asks again."""

from typing import Any

import psycopg

from pisa.domain import disclosure as rules
from tests.assistant_env import set_settings
from tests.conftest import FakeModels, pair_v2, set_key
from tests.test_privacy_v2 import auth, finished
from tests.test_talker_disclosure import conversation, disclosed


def p(slot: str, model: str, *cats: str, provider: str = "openai") -> dict[str, Any]:
    return {"slot": slot, "provider": {"id": provider, "name": provider, "kind": "cloud"}, "model": model,
            "dataCategories": list(cats)}  # fmt: skip


def test_rules_coverage_is_the_same_or_less() -> None:
    allowed = [p("chat.conversation", "m1", "conversation", "mail"), p("conversation.talker", "t1", "voice",
               "conversation", provider="google")]  # fmt: skip
    assert rules.covers(allowed, allowed)
    assert rules.covers(allowed, allowed[:1])  # natural → economy: the talker goes
    assert rules.covers(allowed, [p("chat.conversation", "m1", "conversation")])  # a category fewer
    assert not rules.covers(allowed[:1], allowed)  # economy → natural: a processor more
    assert not rules.covers(allowed, [p("chat.conversation", "m2", "conversation")])  # another model
    assert not rules.covers(
        allowed, [p("chat.conversation", "m1", "conversation", "health")]
    )  # a category more
    assert not rules.covers(allowed, [*allowed, p("intake.triage", "m1", "mail")])  # a slot's model added
    assert not rules.covers(None, allowed[:1])  # nothing recorded: the revision decides, as before


def consent(client: Any, token: str, *, health: bool = True) -> None:
    given = client.post(
        "/v1/ai/consent", json={"revision": disclosed(client, token)["revision"], "health": health},
        headers=auth(token),
    )  # fmt: skip
    assert given.status_code == 204, given.text


def test_natural_to_economy_keeps_the_consent_and_economy_to_natural_asks(
    client: Any, db: psycopg.Connection, fake_models: FakeModels, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    set_settings(db, conversation("natural", "gemini"))
    _, token = pair_v2(db, consent=False)
    consent(client, token)  # natural: the talker allowed too
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    set_settings(db, conversation("economy"))  # the talker goes: less is sent
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    set_settings(db, conversation("natural", "gemini"))  # back to what was allowed
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"

    set_settings(db, conversation("economy"))
    _, other = pair_v2(db, "iPad", consent=False)
    consent(client, other)  # allowed in economy only
    set_settings(db, conversation("natural", "gemini"))  # the talker comes: more is sent
    end = finished(client, other, "ciao")
    assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end
    assert "`" not in end["message"]  # plain text: the app shows it as written


def test_a_changed_slot_model_asks_again(
    client: Any, db: psycopg.Connection, fake_models: FakeModels, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    set_settings(db, conversation("economy"))
    _, token = pair_v2(db, consent=False)
    consent(client, token)
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    db.execute("UPDATE model_slots SET fallback_ref = 'openai:gpt-4.1-mini' WHERE id = 'intake.triage'")
    end = finished(client, token, "ciao")
    assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end


def test_the_refusals_are_plain_text() -> None:
    for lang in ("en", "it"):
        for line in rules.REFUSED[lang].values():
            assert "`" not in line
