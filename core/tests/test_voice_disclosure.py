"""The server's voice (OpenAI speech, ElevenLabs) hears every sentence Pisa says: the AI disclosure names it
as a processor (`conversation.voice`) while it is the voice that speaks, with the conversation's own data
categories (health included). Choosing a server voice whose provider the device's consent doesn't cover
changes the revision and refuses the next turn (`ai_consent_required`) until consent is given; a provider
already covered asks nothing, and the phone's own voice is no processor. FL-2."""

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from pisa_conformance.validate import EXT_ID, errors

from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.domain import disclosure as rules
from pisa.domain import model_catalog
from pisa.services import disclosure
from tests.assistant_env import set_settings
from tests.conftest import FakeModels, dsn, pair, pair_v2, set_key
from tests.live_app import live
from tests.test_privacy_v2 import auth, finished
from tests.test_voice import ELEVEN_KEY, PLAYS, FakeSpeechAPI

DEVICE = {"provider": "device", "model": None, "voice": None, "speed": 1.0}
OPENAI = {"provider": "openai", "model": None, "voice": "marin", "speed": 1.0}
ELEVEN = {"provider": "elevenlabs", "model": None, "voice": "v-it", "speed": 1.0}


@pytest.fixture(autouse=True)
def speech_on() -> Iterator[None]:
    disclosure.set_speech(True)
    yield
    disclosure.set_speech(True)


def current(db: psycopg.Connection) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await disclosure.current(conn, None)

    return asyncio.run(go())


def voices(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in doc["processors"] if p["slot"] == rules.VOICE_SLOT]


def consent(client: Any, token: str, health: bool = True) -> None:
    revision = client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"]
    given = client.post("/v1/ai/consent", json={"revision": revision, "health": health}, headers=auth(token))
    assert given.status_code == 204, given.text


def test_the_voice_is_a_processor_only_while_a_server_voice_speaks(
    db: psycopg.Connection, secrets_key: str
) -> None:
    set_settings(db, {"voice": {"provider": None, "model": None, "voice": "marin", "speed": 1.0}})
    assert voices(current(db)) == []  # no OpenAI key: a null provider is the phone's voice
    set_key(db, secrets_key)  # now a null provider is OpenAI's voice
    openai = voices(current(db))
    assert openai == [{
        "slot": "conversation.voice",
        "provider": {"id": "openai", "name": PROVIDERS_BY_ID["openai"].name, "kind": "cloud"},
        "model": model_catalog.default_model("speech", "openai"),
        "dataCategories": list(rules.SLOT_CATEGORIES["chat.conversation"]),
        "features": ["conversation"],
    }]  # fmt: skip
    assert errors(current(db), EXT_ID + "#/$defs/AIDisclosure") == []
    set_settings(db, {"voice": OPENAI | {"model": "tts-x"}})
    assert [p["model"] for p in voices(current(db))] == ["tts-x"]  # the chosen model is the one named
    set_settings(db, {"voice": DEVICE})
    assert voices(current(db)) == []  # the phone's own voice
    set_settings(db, {"voice": ELEVEN})
    assert voices(current(db)) == []  # no ElevenLabs key: the phone speaks
    set_key(db, secrets_key, "elevenlabs", ELEVEN_KEY)
    assert [p["provider"]["id"] for p in voices(current(db))] == ["elevenlabs"]
    set_settings(db, {"voice": ELEVEN | {"voice": None}})
    assert voices(current(db)) == []  # ElevenLabs with no voice picked: the phone speaks
    set_settings(db, {"voice": ELEVEN})
    disclosure.set_speech(False)  # the server's voices off (review mode): the phone speaks
    assert voices(current(db)) == []


def test_the_voice_s_categories_are_the_conversation_s_health_included() -> None:
    assert rules.SLOT_CATEGORIES[rules.VOICE_SLOT] == rules.SLOT_CATEGORIES["chat.conversation"]
    assert "health" in rules.SLOT_CATEGORIES[rules.VOICE_SLOT]
    entry = rules.processor(rules.VOICE_SLOT, "openai", "OpenAI", "cloud", "m")
    assert "health" in entry["dataCategories"] and entry["features"] == ["conversation"]


def test_an_uncovered_voice_refuses_the_turn_and_a_covered_one_does_not(
    client: Any, db: psycopg.Connection, fake_models: FakeModels, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    set_settings(db, {"voice": DEVICE})
    _, token = pair_v2(db, consent=False)
    consent(client, token)  # the phone's voice: the processors without one
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    before = client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"]
    set_settings(db, {"voice": OPENAI})  # a server voice their consent doesn't cover
    assert client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"] != before
    seen = len(fake_models.seen_messages)
    end = finished(client, token, "ciao")
    assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end
    assert len(fake_models.seen_messages) == seen  # no model call, nothing spoken
    refused = client.post("/v1/briefings", headers=auth(token))  # a REST route that reaches a model
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "ai_consent_required"
    consent(client, token)
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    # back to the phone's voice: it sends less, nothing is asked
    set_settings(db, {"voice": DEVICE})
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    # and OpenAI's voice again: already covered, nothing is asked either
    set_settings(db, {"voice": OPENAI})
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    # another provider's voice: not covered
    set_key(db, secrets_key, "elevenlabs", ELEVEN_KEY)
    set_settings(db, {"voice": ELEVEN})
    assert finished(client, token, "ciao")["code"] == "ai_consent_required"


def test_the_phone_s_voice_changes_nothing(
    client: Any, db: psycopg.Connection, fake_models: FakeModels, secrets_key: str
) -> None:
    set_settings(db, {"voice": DEVICE})
    set_key(db, secrets_key)
    _, token = pair_v2(db)
    before = client.get("/v1/ai/disclosure", headers=auth(token)).json()
    assert voices(before) == []
    set_settings(db, {"voice": DEVICE | {"speed": 1.2}})
    assert client.get("/v1/ai/disclosure", headers=auth(token)).json()["revision"] == before["revision"]
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"


def test_choosing_a_server_voice_tells_the_devices(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """A change of voice from the app notifies like a slot change; `tell` asks only a device whose consent
    doesn't cover it."""
    set_key(db, secrets_key)
    set_settings(db, {"voice": DEVICE})
    _, token = pair_v2(db)
    db.execute("LISTEN pisa_ai_disclosure")
    changed = client.patch("/v1/settings", headers=auth(token), json={"voice": {"provider": "openai"}})
    assert changed.status_code == 200, changed.text
    assert [n.channel for n in db.notifies(timeout=2, stop_after=1)] == ["pisa_ai_disclosure"]


def test_tell_asks_only_the_devices_the_voice_is_not_covered_for(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    set_settings(db, {"voice": OPENAI})
    covered_id, covered = pair_v2(db, "covered", consent=False)
    consent(client, covered)  # allowed with OpenAI's voice
    set_settings(db, {"voice": DEVICE})
    uncovered_id, uncovered = pair_v2(db, "uncovered", consent=False)
    consent(client, uncovered)  # allowed with the phone's voice only
    set_settings(db, {"voice": OPENAI})
    db.execute("UPDATE devices SET ai_told = 'old'")

    class Live:
        def __init__(self) -> None:
            self.asked: set[Any] = set()

        def tell(self, name: str, value: dict[str, Any], only: set[Any] | None = None) -> list[Any]:
            assert name == "pisa.ai.disclosure" and voices(value)
            self.asked = set(only or ())
            return list(self.asked)

    listeners = Live()

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await disclosure.tell(conn, None, listeners)

    asyncio.run(go())
    assert {str(d) for d in listeners.asked} == {uncovered_id}
    assert covered_id not in {str(d) for d in listeners.asked}


def test_a_notify_is_not_sent_to_an_uncovered_voice(
    client: Any, db: psycopg.Connection, secrets_key: str, http_handler: Any
) -> None:
    """A notify spoken aloud goes to the server's voice only under a consent that covers it; else the phone
    speaks its text."""
    speech = FakeSpeechAPI()
    http_handler["handler"] = speech
    set_settings(db, {"briefing": {"window": None}, "voice": DEVICE})
    device_id, token = pair(db)  # consent with the phone's voice
    set_key(db, secrets_key)
    set_settings(db, {"voice": OPENAI})
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.custom("pisa.presence", {"event": "companion_on"})
        app.custom("pisa.presence", {"event": "earbuds_connected"})
        app.custom("pisa.presence", {"event": "app_foreground"})
        notify = app.next()
        assert notify["type"] == "notify"
        assert app.ping() == []
        time.sleep(0.3)
        assert app.ping() == []  # no say_audio
    assert speech.texts == []
