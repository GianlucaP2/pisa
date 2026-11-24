"""Natural mode's talker hears the user's voice: the AI disclosure names it as a processor (only in natural
mode), so a change of talker or of mode changes the revision and needs fresh consent, and a voice talk is
refused (`ai_consent_required`) like a run until the consent covers it. The refusal speaks the user's
language and points at what exists: the app, or `pisa consent` on the server."""

from typing import Any

import psycopg
import pytest
from pisa_conformance.validate import EXT_ID, errors

from pisa.brain.models.providers import PROVIDERS_BY_ID
from pisa.domain import disclosure as rules
from pisa.domain import model_catalog
from tests.assistant_env import set_settings
from tests.conftest import FakeModels, pair_v2, set_key
from tests.test_live_talk import FakeTalker
from tests.test_privacy_v2 import auth, finished

VOICES = {"xai": "eve", "gemini": "Aoede"}


class FakeGemini(FakeTalker):
    """The test talker as Gemini Live: the Google key opens it."""

    id = "gemini"
    name = "Gemini Live"
    key_provider = "google"
    default_voice = "Aoede"
    voices: tuple[str, ...] = ("Aoede",)


def conversation(mode: str, talker: str = "gemini") -> dict[str, Any]:
    return {"conversation": {"mode": mode, "talker": talker, "voices": VOICES}}


def disclosed(client: Any, token: str) -> dict[str, Any]:
    answer = client.get("/v1/ai/disclosure", headers=auth(token))
    assert answer.status_code == 200, answer.text
    return answer.json()


def talkers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in doc["processors"] if p["slot"] == rules.TALKER_SLOT]


def test_the_talker_is_a_processor_in_natural_mode_only(client: Any, db: psycopg.Connection) -> None:
    _, token = pair_v2(db)
    set_settings(db, conversation("economy"))
    economy = disclosed(client, token)
    assert talkers(economy) == []
    set_settings(db, conversation("natural", "gemini"))
    gemini = disclosed(client, token)
    assert errors(gemini, EXT_ID + "#/$defs/AIDisclosure") == []
    assert talkers(gemini) == [{
        "slot": "conversation.talker",
        "provider": {"id": "google", "name": PROVIDERS_BY_ID["google"].name, "kind": "cloud"},
        "model": model_catalog.default_model("talker", "gemini"),
        "dataCategories": list(rules.SLOT_CATEGORIES["conversation.talker"]),
        "features": ["conversation"],
    }]  # fmt: skip
    assert "voice" in talkers(gemini)[0]["dataCategories"]
    set_settings(db, conversation("natural", "xai"))
    xai = disclosed(client, token)
    assert [p["provider"]["id"] for p in talkers(xai)] == ["xai"]
    assert talkers(xai)[0]["model"] == model_catalog.default_model("talker", "xai")
    # each change of mode or talker is a new revision: the app asks again
    assert len({economy["revision"], gemini["revision"], xai["revision"]}) == 3


def test_the_talkers_providers_are_the_connectors_own() -> None:
    from pisa.connectors.talker_gemini import GeminiLive
    from pisa.connectors.talker_xai import GrokVoice

    assert rules.TALKER_PROVIDERS == {
        GeminiLive.id: GeminiLive.key_provider,
        GrokVoice.id: GrokVoice.key_provider,
    }


def test_the_settings_change_tells_the_devices(client: Any, db: psycopg.Connection) -> None:
    """A change of mode or talker from the app notifies like a slot change (the disclosure changed)."""
    _, token = pair_v2(db)
    db.execute("LISTEN pisa_ai_disclosure")
    changed = client.patch("/v1/settings", headers=auth(token), json=conversation("natural"))
    assert changed.status_code == 200, changed.text
    assert [n.channel for n in db.notifies(timeout=2, stop_after=1)] == ["pisa_ai_disclosure"]


def test_a_consent_stops_covering_runs_once_natural_mode_or_the_talker_changes(
    client: Any, db: psycopg.Connection, fake_models: FakeModels, secrets_key: str
) -> None:
    set_key(db, secrets_key)
    set_settings(db, conversation("economy"))
    _, token = pair_v2(db)  # consent for economy mode
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    set_settings(db, conversation("natural", "gemini"))
    before = len(fake_models.seen_messages)
    end = finished(client, token, "ciao")
    assert end["type"] == "RUN_ERROR" and end["code"] == "ai_consent_required", end
    assert end["message"] == rules.refused({"aiRevision": "old"}, "new", False, "it")  # the turn's Italian
    assert len(fake_models.seen_messages) == before  # no model call
    given = client.post(
        "/v1/ai/consent", json={"revision": disclosed(client, token)["revision"], "health": False},
        headers=auth(token),
    )  # fmt: skip
    assert given.status_code == 204
    assert finished(client, token, "ciao")["type"] == "RUN_FINISHED"
    set_settings(db, conversation("natural", "xai"))  # another talker: ask again
    assert finished(client, token, "ciao")["code"] == "ai_consent_required"


@pytest.mark.parametrize("lang", ["it-IT", "en-US"])
def test_a_voice_talk_is_refused_without_covering_consent(
    client: Any, db: psycopg.Connection, secrets_key: str, lang: str
) -> None:
    set_key(db, secrets_key, provider="google")
    set_settings(db, conversation("economy"))
    _, token = pair_v2(db)  # consent for economy mode only
    set_settings(db, conversation("natural", "gemini"))
    body = {"sdp_offer": "v=0", "trigger": "press", "lang": lang}
    refused = client.post("/v1/talk/sessions", headers=auth(token), json=body)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "ai_consent_required"
    assert error["message"] == rules.refused({"aiRevision": "economy's"}, "x", False, lang)
    assert "pisa consent" in error["message"]
    revision = disclosed(client, token)["revision"]
    given = client.post("/v1/ai/consent", json={"revision": revision, "health": False}, headers=auth(token))
    assert given.status_code == 204
    # without health: the talker hears everything said in the talk, so health consent is needed too
    no_health = client.post("/v1/talk/sessions", headers=auth(token), json=body)
    assert no_health.status_code == 409 and no_health.json()["error"]["code"] == "ai_consent_required"
    assert no_health.json()["error"]["message"] == rules.refused({"aiRevision": "x"}, "x", True, lang)
    given = client.post("/v1/ai/consent", json={"revision": revision, "health": True}, headers=auth(token))
    assert given.status_code == 204
    # consent given: the talk goes on to the talker (whose fake offer is then refused as an SDP it can't use)
    client.app.state.runtime.talk._talkers = {"gemini": FakeGemini()}
    after = client.post("/v1/talk/sessions", headers=auth(token), json=body)
    assert after.json()["error"]["code"] != "ai_consent_required", after.text


@pytest.mark.parametrize("lang", ["it", "it-IT", "en", "en-US"])
def test_the_refusal_speaks_the_user_s_language_and_names_what_exists(lang: str) -> None:
    for consent, health in ((None, False), ({"aiRevision": "old"}, False), ({"aiRevision": "r"}, True)):
        why = rules.refused(consent, "r", health, lang)
        assert why is not None and "pisa consent" in why and "Settings, AI" not in why
        assert ("Pisa" in why) or ("IA" in why) or ("AI" in why)
    assert rules.refused(None, "r", False, lang) == rules.REFUSED[lang[:2]]["missing"]


def test_the_server_logs_each_talk_opened_or_refused(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """The API has no access log: a refused natural talk said nothing on the server. One INFO line each,
    with no user data and no SDP."""
    import asyncio

    from aiortc import RTCPeerConnection
    from structlog.testing import capture_logs

    async def offer() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        pc.createDataChannel("pisa")
        await pc.setLocalDescription(await pc.createOffer())
        sdp = pc.localDescription.sdp
        await pc.close()
        return sdp

    _, token = pair_v2(db, health=True)
    body = {"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT", "first_text": "ciao Pisa segreto"}
    real = asyncio.run(offer())
    with capture_logs() as logs:
        economy = client.post("/v1/talk/sessions", headers=auth(token), json=body)
        set_settings(db, conversation("natural", "gemini"))
        keyless = client.post("/v1/talk/sessions", headers=auth(token), json=body)
        set_key(db, secrets_key, provider="google")
        unconsented = client.post("/v1/talk/sessions", headers=auth(token), json=body)
        revision = disclosed(client, token)["revision"]
        client.post("/v1/ai/consent", json={"revision": revision, "health": True}, headers=auth(token))
        client.app.state.runtime.talk._talkers = {"gemini": FakeGemini()}
        made = client.post("/v1/talk/sessions", headers=auth(token), json=body | {"sdp_offer": real})
    codes = [r.json()["error"]["code"] for r in (economy, keyless, unconsented)]
    assert codes == ["talk_economy", "provider_key_missing", "ai_consent_required"]
    assert made.status_code == 201, made.text
    talk = [log for log in logs if log["event"] in ("talk_session_refused", "talk_session_opened")]
    assert [(log["event"], log.get("code")) for log in talk] == [
        ("talk_session_refused", "talk_economy"), ("talk_session_refused", "provider_key_missing"),
        ("talk_session_refused", "ai_consent_required"), ("talk_session_opened", None),
    ]  # fmt: skip
    assert all(log["log_level"] == "info" for log in talk)
    assert (talk[-1]["talker"], talk[-1]["mode"]) == ("gemini", "natural")
    assert "segreto" not in str(talk) and "v=0" not in str(talk)
    client.delete(f"/v1/talk/sessions/{made.json()['session_id']}", headers=auth(token))


def test_the_talk_s_refusal_says_what_to_do_in_the_talk_s_language(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    set_key(db, secrets_key, provider="google")
    set_settings(db, conversation("economy"))
    _, token = pair_v2(db)
    set_settings(db, conversation("natural", "gemini"))
    for lang, words in (("it-IT", "Permettilo nell'app"), ("en-US", "Allow it in the app")):
        body = {"sdp_offer": "v=0", "trigger": "press", "lang": lang}
        error = client.post("/v1/talk/sessions", headers=auth(token), json=body).json()["error"]
        assert error["code"] == "ai_consent_required"
        assert rules.refused_action(lang) in str(error) and words in rules.refused_action(lang)
        assert "pisa consent" in rules.refused_action(lang)


def test_the_talker_categories_are_voice_plus_the_conversation_s() -> None:
    assert rules.SLOT_CATEGORIES[rules.TALKER_SLOT] == ("voice", *rules.SLOT_CATEGORIES["chat.conversation"])


def test_pisa_never_asks_to_open_a_talk_the_consent_would_refuse(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    """`talk.ask_to_open` only for a talk that can open: a talk their consent doesn't cover would be refused
    (409 `ai_consent_required`) the moment the phone opened it."""
    import uuid

    from tests.conftest import talk_naturally
    from tests.test_talk_body import _notice, _wearing

    set_key(db, secrets_key, "xai")
    device_id, _ = pair_v2(db)
    talk_naturally(
        db, consent=False
    )  # natural with Grok Voice: the consent of the pairing no longer covers it
    _wearing(db, device_id)
    runtime = client.app.state.runtime

    def ask() -> Any:
        return client.portal.call(
            runtime.talk.may_ask,
            uuid.UUID(device_id),
            uuid.UUID(_notice(db, device_id)),
            "leave_now",
            "urgent",
        )

    assert ask() is None
    talk_naturally(db)  # their consent, given again
    assert ask() is not None


def test_a_frame_the_server_never_sends_fails_the_test_never_hangs(
    client: Any, db: psycopg.Connection
) -> None:
    """The app's side of `/v2/live` in tests reads with a deadline: Starlette's own receive waits forever."""
    import time

    from tests.live_app import live, receive_within

    device_id, token = pair_v2(db)
    with live(client, token, device_id) as app:
        started = time.monotonic()
        with pytest.raises(AssertionError, match=r"no frame within 0\.5 s"):
            receive_within(app.ws, 0.5)
        assert time.monotonic() - started < 10
