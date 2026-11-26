"""Natural talks that can't open say so: every failure to open the talker (a refused handshake, an unknown
model, a key without Live access, a quota, any connector error) is `502 talker_unavailable` with a safe detail
(the provider's code and kind, never the key) and the server's INFO line; the talker's model is the
operator's to set (PISA_TALKER_MODEL_GEMINI / _XAI, over the catalog's); `pisa doctor --live` opens it once
and says OK or the exact refusal. Talkers mocked: no network."""

import asyncio
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from pisa.domain import model_catalog
from pisa.ports.talker import TalkerConfig, TalkerError
from tests.assistant_env import set_settings
from tests.conftest import FAKE_KEY, dsn, pair_v2, set_key
from tests.test_privacy_v2 import auth
from tests.test_talker_disclosure import FakeGemini

NATURAL = {
    "conversation": {"mode": "natural", "talker": "gemini", "voices": {"xai": "eve", "gemini": "Aoede"}},
    "google_ai_tier": "paid",  # Gemini Live only on a paid Google tier
}


class Refusing(FakeGemini):
    """A talker whose connect raises what a connector didn't expect (the key in its words)."""

    async def open(self, cfg: TalkerConfig) -> Any:
        param = "ke" + "y"  # the URL a provider error may quote, with the key in it
        raise RuntimeError(f"handshake failed for wss://example.invalid/live?{param}={cfg.key}")


@pytest.fixture
def talker_models() -> Iterator[None]:
    yield
    model_catalog.set_talker_models({})


def test_a_talker_that_cannot_open_is_a_typed_502_with_the_server_s_line(
    client: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    set_key(db, secrets_key, provider="google")
    set_settings(db, NATURAL)
    _, token = pair_v2(db, health=True)  # consent for natural mode with Gemini Live (health too)
    client.app.state.runtime.talk._talkers = {"gemini": Refusing()}
    with capture_logs() as logs:
        answer = client.post("/v1/talk/sessions", headers=auth(token),
                             json={"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT"})  # fmt: skip
    assert answer.status_code == 502, answer.text
    error = answer.json()["error"]
    assert error["code"] == "talker_unavailable" and "RuntimeError" in error["message"]
    refused = [log for log in logs if log["event"] == "talk_session_refused"]
    assert refused == [{"event": "talk_session_refused", "log_level": "info", "code": "talker_unavailable",
                        "purpose": "talk"}]  # fmt: skip
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


def test_the_talker_model_is_the_operator_s_over_the_catalog_s(
    talker_models: None, client: Any, db: psycopg.Connection
) -> None:
    from pisa.settings import Settings

    shipped = model_catalog.default_model("talker", "gemini")
    configured = Settings(_env_file=None, timezone="Europe/London", talker_model_gemini=" gemini-live-test ")  # type: ignore[call-arg]
    assert configured.talker_models == {"gemini": " gemini-live-test ", "xai": ""}
    model_catalog.set_talker_models(configured.talker_models)
    assert model_catalog.default_model("talker", "gemini") == "gemini-live-test"
    assert model_catalog.default_model("talker", "xai") == "grok-voice-latest"  # empty: the catalog's
    set_settings(db, NATURAL)
    _, token = pair_v2(db, health=True)
    doc = client.get("/v1/ai/disclosure", headers=auth(token)).json()
    assert [p["model"] for p in doc["processors"] if p["slot"] == "conversation.talker"] == [
        "gemini-live-test"
    ]
    model_catalog.set_talker_models({})
    assert model_catalog.default_model("talker", "gemini") == shipped


def test_a_gemini_talk_on_the_tailscale_setup_offers_the_sidecar_s_address_in_the_range(
    client: Any, db: psycopg.Connection, secrets_key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The tailscale profile's voice, at the API: PISA_ICE_HOST empty, the address read from the file the
    sidecar writes (PISA_ICE_HOST_FILE), the media on the UDP range; Gemini Live mocked."""
    from aiortc import RTCPeerConnection

    from tests.test_talker_disclosure import FakeGemini
    from tests.test_voice_net import candidates, free_range

    set_key(db, secrets_key, provider="google")
    set_settings(db, NATURAL)
    _, token = pair_v2(db, health=True)
    runtime = client.app.state.runtime
    runtime.talk._talkers = {"gemini": FakeGemini()}
    ip = tmp_path / "ip"
    ip.write_text("127.0.0.1\n")  # the tailnet address in production (100.64.0.x); loopback here
    ports = free_range(3)
    update = {"voice_udp_ports": f"{ports[0]}-{ports[-1]}", "ice_host": "", "ice_host_file": str(ip)}
    monkeypatch.setattr(runtime, "settings", runtime.settings.model_copy(update=update))

    async def offer() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        pc.createDataChannel("pisa")
        await pc.setLocalDescription(await pc.createOffer())
        sdp = pc.localDescription.sdp
        await pc.close()
        return sdp

    with capture_logs() as logs:
        body = {"sdp_offer": asyncio.run(offer()), "trigger": "press", "lang": "it-IT"}
        made = client.post("/v1/talk/sessions", headers=auth(token), json=body)
    assert made.status_code == 201, made.text
    hosts = [(host, port) for host, port, kind in candidates(made.json()["sdp_answer"]) if kind == "host"]
    assert hosts and all(host == "127.0.0.1" and port in ports for host, port in hosts), hosts
    opened = [log for log in logs if log["event"] == "talk_session_opened"]
    assert [(log["talker"], log["mode"]) for log in opened] == [("gemini", "natural")]
    client.delete(f"/v1/talk/sessions/{made.json()['session_id']}", headers=auth(token))


# --- pisa doctor --live -------------------------------------------------------------------------------------


class Opens(FakeGemini):
    def __init__(self) -> None:
        super().__init__()
        self.opened: list[TalkerConfig] = []

    async def open(self, cfg: TalkerConfig) -> Any:
        self.opened.append(cfg)
        return await super().open(cfg)


class Refuses(FakeGemini):
    async def open(self, cfg: TalkerConfig) -> Any:
        raise TalkerError("Gemini Live refused the session (ConnectionClosedError 1008).", 1008,
                          f"models/x is not found ({cfg.key})")  # fmt: skip


def check(app_settings: Any, db: psycopg.Connection, talker: Any) -> tuple[str, dict[str, object]]:
    from pisa.cli_doctor import talker_check

    settings = app_settings.model_copy(update={"database_url": dsn(db)})

    async def go() -> tuple[str, dict[str, object]]:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await talker_check(conn, settings, {"gemini": talker, "xai": talker})

    return asyncio.run(go())


def test_doctor_live_opens_the_talker_once_and_says_ok(
    app_settings: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    from pisa.domain import guide

    talker = Opens()
    assert check(app_settings, db, talker)[0] == "talker_off"  # economy: nothing opened, nothing spent
    set_settings(db, NATURAL)
    key, values = check(app_settings, db, talker)
    assert key == "talker_no_key" and values["provider"] == "google"
    set_key(db, secrets_key, provider="google")
    key, values = check(app_settings, db, talker)
    assert key == "talker_ok" and len(talker.opened) == 1 and talker.opened[0].voice == "Aoede"
    assert values["model"] == model_catalog.default_model("talker", "gemini")
    assert "ha aperto e chiuso" in guide.doctor_text(key, "it", **values)


def test_doctor_live_says_the_exact_refusal_without_the_key(
    app_settings: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    from pisa.domain import guide

    set_settings(db, NATURAL)
    set_key(db, secrets_key, provider="google")
    key, values = check(app_settings, db, Refuses())
    assert key == "talker_failed" and values["code"] == 1008
    line = guide.doctor_text(key, "en", **values)
    assert "1008" in line and "not found" in line and FAKE_KEY not in line


def test_pisa_doctor_live_exits_1_on_a_refusal(
    app_settings: Any, db: psycopg.Connection, secrets_key: str, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from pisa import cli, cli_doctor

    set_settings(db, NATURAL)
    set_key(db, secrets_key, provider="google")
    configured = app_settings.model_copy(update={"database_url": dsn(db)})
    monkeypatch.setattr("pisa.cli.get_settings", lambda: configured)
    monkeypatch.setattr(cli_doctor, "default_talkers", lambda: {"gemini": Refuses()})
    code = cli.main(["doctor", "--live"])
    err = capsys.readouterr().err
    assert code == 1 and "1008" in err and FAKE_KEY not in err
