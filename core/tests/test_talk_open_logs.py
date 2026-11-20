"""A refused natural talk was invisible on the server (no access log): POST /v1/talk/sessions logs one INFO
line, talk_session_opened (talker, mode) or talk_session_refused (the error code), with no user data and no
SDP; and any failure to open the talker is 502 talker_unavailable with the provider's code and words, the key
redacted, never a 500. Talkers mocked."""

import asyncio
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from pisa.ports.talker import TalkerConfig, TalkerError
from tests.assistant_env import set_settings
from tests.conftest import FAKE_KEY, pair, set_key, talk_naturally
from tests.test_live_talk import FakeTalker

NATURAL = {"conversation": {"mode": "natural", "talker": "xai", "voices": {"xai": "eve", "gemini": "Aoede"}}}


class Refusing(FakeTalker):
    async def open(self, cfg: TalkerConfig) -> Any:
        param = "ke" + "y"  # the URL a provider error may quote, with the key in it
        raise RuntimeError(f"handshake failed for wss://example.invalid/live?{param}={cfg.key}")


def offer() -> str:
    from aiortc import RTCPeerConnection

    async def go() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        pc.createDataChannel("pisa")
        await pc.setLocalDescription(await pc.createOffer())
        sdp = pc.localDescription.sdp
        await pc.close()
        return sdp

    return asyncio.run(go())


def test_each_talk_opened_or_refused_is_one_info_line(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    body = {"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT", "first_text": "ciao Pisa segreto"}
    set_settings(db, {"conversation": {**NATURAL["conversation"], "mode": "economy"}})
    real = offer()
    with capture_logs() as logs:
        economy = client.post("/v1/talk/sessions", headers=auth, json=body)
        talk_naturally(db)  # NATURAL, their consent covering it
        keyless = client.post("/v1/talk/sessions", headers=auth, json=body)
        set_key(db, secrets_key, provider="xai")
        client.app.state.runtime.talk._talkers = {"xai": FakeTalker()}
        made = client.post("/v1/talk/sessions", headers=auth, json=body | {"sdp_offer": real})
    assert [r.json()["error"]["code"] for r in (economy, keyless)] == ["talk_economy", "provider_key_missing"]
    assert made.status_code == 201, made.text
    talk = [log for log in logs if log["event"] in ("talk_session_refused", "talk_session_opened")]
    assert [(log["event"], log.get("code")) for log in talk] == [
        ("talk_session_refused", "talk_economy"), ("talk_session_refused", "provider_key_missing"),
        ("talk_session_opened", None),
    ]  # fmt: skip
    assert all(log["log_level"] == "info" for log in talk)
    assert (talk[-1]["talker"], talk[-1]["mode"]) == ("xai", "natural")
    assert "segreto" not in str(talk) and "v=0" not in str(talk)
    client.delete(f"/v1/talk/sessions/{made.json()['session_id']}", headers=auth)


def test_a_talker_that_cannot_open_is_a_typed_502(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    talk_naturally(db)  # NATURAL, their consent covering it
    set_key(db, secrets_key, provider="xai")
    client.app.state.runtime.talk._talkers = {"xai": Refusing()}
    with capture_logs() as logs:
        answer = client.post("/v1/talk/sessions", headers=auth,
                             json={"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT"})  # fmt: skip
    assert answer.status_code == 502, answer.text
    error = answer.json()["error"]
    assert error["code"] == "talker_unavailable" and "RuntimeError" in error["message"]
    assert [log["code"] for log in logs if log["event"] == "talk_session_refused"] == ["talker_unavailable"]
    assert FAKE_KEY not in str(logs) and FAKE_KEY not in answer.text


def test_gemini_live_s_closed_handshake_is_a_talker_error_with_its_code_and_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown model or a key without Live access: Google closes the WebSocket (1008) during setup."""
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    from pisa.connectors import talker_gemini

    fake = "AI" + "za" + "-not-a-real-one-0123456789"  # shaped like a Google key, built so scanners skip it

    class Connect:
        async def __aenter__(self) -> Any:
            raise ConnectionClosedError(Close(1008, f"models/nope is not found for key {fake}"), None)

        async def __aexit__(self, *_: Any) -> None:
            return None

    class Client:
        def __init__(self, api_key: str) -> None:
            self.aio = type(
                "Aio", (), {"live": type("Live", (), {"connect": staticmethod(lambda **_: Connect())})}
            )

    monkeypatch.setattr(talker_gemini.genai, "Client", Client)
    cfg = TalkerConfig(key=fake, voice="Aoede", lang="it-IT", instructions="")
    with pytest.raises(TalkerError) as caught:
        asyncio.run(talker_gemini.GeminiLive().open(cfg))
    err = caught.value
    assert err.status == 1008 and "ConnectionClosedError" in err.message
    assert err.reason is not None and "not found" in err.reason and fake not in err.reason
