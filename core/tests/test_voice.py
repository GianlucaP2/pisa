"""Pisa's voice (contract V): replies and notifies as `say_audio`, sentence by sentence and in order;
cancel; a provider error is silent for the device; the caps; the settings, voices, preview and the
ElevenLabs key; prices in the ledger and first_audio_s in the latency log."""

import asyncio
import base64
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from pisa.db.pool import open_pool
from pisa.domain import voice as rules
from pisa.infra import ledger
from pisa.infra.keys import KeyStore
from pisa.ports.speech import SpeechError, SpeechUsage, Voice
from pisa.services.voice import Speaker, VoiceService
from tests.assistant_env import set_settings
from tests.conftest import FAKE_KEY, Clock, FakeModels, dsn, pair, set_key
from tests.live_app import LiveApp, live
from tests.owner import owner_at

ELEVEN_KEY = "el-test-xxxxxxxxxxxxxxxx"
PLAYS = ["mp3", "aac_adts"]  # the hello's device.audioFormats of a build that plays Pisa's voice


class FakeSpeechAPI:
    """OpenAI's speech endpoint (SSE) and ElevenLabs' API, as httpx handlers. Each sentence's audio is
    b"<text>|1" then b"<text>|2"; a sentence in `slow` waits that long first; one in `fail` gets HTTP 500."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.bodies: list[dict[str, Any]] = []  # the JSON bodies sent to the providers
        self.slow: dict[str, float] = {}
        self.fail: set[str] = set()
        self.malformed: set[str] = set()  # an SSE line that is not JSON
        self.done_marker = False  # end the stream with "data: [DONE]", as OpenAI's streams do
        self.started: list[str] = []
        self.finished: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.elevenlabs.io":
            return self._eleven(request)
        assert (host, path) == ("api.openai.com", "/v1/audio/speech"), request.url
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        body = json.loads(request.content)
        text = body["input"]
        self.bodies.append(body)
        self.texts.append(text)
        self.started.append(text)
        await asyncio.sleep(next((s for k, s in self.slow.items() if k in text), 0))
        self.finished.append(text)
        if any(k in text for k in self.fail):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        events: list[dict[str, Any]] = [
            {"type": "speech.audio.delta", "audio": base64.b64encode(f"{text}|{i}".encode()).decode()}
            for i in (1, 2)
        ]
        events.append({"type": "speech.audio.done", "usage": {"input_tokens": 10, "output_tokens": 250}})
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        if self.done_marker:
            sse += "data: [DONE]\n\n"
        if any(k in text for k in self.malformed):
            sse = "data: {not json\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    def _eleven(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("xi-api-key") != ELEVEN_KEY:
            return httpx.Response(401, json={"detail": {"status": "invalid_api_key"}})
        if request.url.path == "/v1/user":
            return httpx.Response(200, json={"subscription": {"tier": "starter"}})
        if request.url.path == "/v1/voices":
            return httpx.Response(200, json={"voices": [
                {"voice_id": "v-it", "name": "Giulia", "verified_languages": [{"language": "it"}]},
                {"voice_id": "v-any", "name": "Rachel", "verified_languages": []},
            ]})  # fmt: skip
        body = json.loads(request.content)
        self.texts.append(body["text"])
        self.bodies.append(body)
        return httpx.Response(200, content=b"ID3" + body["text"].encode(), headers={"character-cost": "12"})


def messages_until_done(app: LiveApp, *, audio: bool = True) -> list[dict[str, Any]]:
    """Frames of one reply until both its last `say` and (when audio is expected) its last `say_audio`."""
    out: list[dict[str, Any]] = []
    said = heard = False
    while not (said and (heard or not audio)):
        message = app.next()
        out.append(message)
        said |= message["type"] == "say" and message["final"]
        heard |= message["type"] == "say_audio" and message["final"]
    return out


def audio_of(frames: list[dict[str, Any]]) -> bytes:
    return b"".join(base64.b64decode(f["data"]) for f in frames if f["type"] == "say_audio")


@pytest.fixture
def speech(http_handler) -> FakeSpeechAPI:  # type: ignore[no-untyped-def]
    fake = FakeSpeechAPI()
    http_handler["handler"] = fake
    return fake


# --- replies over the socket ------------------------------------------------------------------------------


def test_each_sentence_is_spoken_in_order(client, db, secrets_key, fake_models: FakeModels, speech) -> None:  # type: ignore[no-untyped-def]
    """The first sentence is slow at the provider: the second is already asked for meanwhile, and the audio
    still goes out in order, the last chunk final."""
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Prima frase. Seconda frase. Terza."
    speech.slow["Prima"] = 0.4
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("dimmi", lang="it-IT", source="typed")
        frames = messages_until_done(app)
    says = [f for f in frames if f["type"] == "say"]
    audio = [f for f in frames if f["type"] == "say_audio"]
    sentences = [s["delta"] for s in says if s["delta"]]
    assert speech.texts and set(speech.texts) == set(sentences)
    assert speech.finished.index(sentences[1]) < speech.finished.index(sentences[0])  # not one after another
    assert audio_of(frames) == b"".join(f"{s}|1{s}|2".encode() for s in sentences)
    assert [f["seq"] for f in audio] == list(range(len(audio)))
    assert [f["final"] for f in audio] == [False] * (len(audio) - 1) + [True]
    assert audio[-1]["data"] == "" and "complete" not in audio[-1]  # an empty chunk closes it (no hold-back)
    assert {f["stream_id"] for f in audio} == {says[0]["stream_id"]}
    assert {(f["format"], f["sample_rate"]) for f in audio} == {("mp3", 24000)}
    assert speech.bodies[0] | {"input": "x"} == {
        "model": "gpt-4o-mini-tts", "input": "x", "voice": "marin", "response_format": "mp3", "speed": 1.0,
        "stream_format": "sse",
    }  # fmt: skip
    rows = db.execute(
        "SELECT model_ref, status, input_tokens, output_tokens, cost_eur FROM llm_calls WHERE slot = %s",
        (rules.SLOT,),
    ).fetchall()
    assert len(rows) == len(sentences) and {r[:4] for r in rows} == {
        ("openai:gpt-4o-mini-tts", "settled", 10, 250)
    }
    assert all(r[4] > 0 for r in rows)  # $0.60 / $12 per 1M tokens, in euros


def test_cancel_stops_the_audio(client, db, secrets_key, fake_models: FakeModels, speech) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    set_key(db, secrets_key)

    async def slow(messages, info):  # type: ignore[no-untyped-def]
        yield "Prima frase. "
        await asyncio.sleep(30)
        yield "mai."

    fake_models.default = slow
    speech.slow["Prima"] = 30  # the provider never answers before the cancel, however busy the machine
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        turn = app.say("raccontami", lang="it-IT", source="typed")
        first = app.next()
        assert first["type"] == "say"
        for _ in range(200):  # the sentence reached the provider (its row is reserved): then cancel
            if speech.started:
                break
            time.sleep(0.01)
        app.cancel(turn["runId"])
        assert app.ping() == []
        time.sleep(0.3)
        assert app.ping() == []  # no say_audio in between
    row = None
    for _ in range(50):
        row = db.execute("SELECT status, error_code FROM llm_calls WHERE slot = %s", (rules.SLOT,)).fetchone()
        if row and row[0] != "reserved":
            break
        time.sleep(0.05)
    assert row == ("failed", "cancelled")


def test_a_provider_error_means_no_audio_and_no_error(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Una frase. Un'altra."
    speech.fail.add("Una frase")
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("dimmi", lang="it-IT", source="typed")
        frames = messages_until_done(app, audio=False)
        failed: list[tuple[str]] = []
        # The speaker settles its rows in the background (after its one retry), and nothing on the socket
        # says when: wait for them with the socket open, since closing it cancels the speaker.
        for _ in range(400):
            failed = db.execute(
                "SELECT error_code FROM llm_calls WHERE slot = %s AND status = 'failed'", (rules.SLOT,)
            ).fetchall()
            if failed:
                break
            time.sleep(0.05)
        frames += app.ping()
    assert {f["type"] for f in frames} == {"say"}
    assert not any(f["type"] in ("say_audio", "error") for f in frames)  # nor while Pisa tried
    assert ("provider_error",) in failed


def test_no_key_or_the_device_voice_means_no_audio(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    set_key(db, secrets_key)
    set_settings(db, {"voice": {"provider": "device"}})
    fake_models.default = "Ciao."
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("ciao", lang="it-IT", source="typed")
        frames = messages_until_done(app, audio=False)
        assert app.ping() == []
    assert speech.texts == [] and not any(f["type"] == "say_audio" for f in frames)


def test_a_notify_is_spoken_too(client, db, secrets_key, speech) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"window": None}})
    device_id, token = pair(db)
    set_key(db, secrets_key)
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.custom("pisa.presence", {"event": "companion_on"})  # spoken in the AirPods (contract P)
        app.custom("pisa.presence", {"event": "earbuds_connected"})
        app.custom("pisa.presence", {"event": "app_foreground"})  # a briefing, as notify
        notify = app.next()
        assert notify["type"] == "notify"
        frames = []
        while not (frames and frames[-1]["final"]):
            frames.append(app.next())
    assert {f["type"] for f in frames} == {"say_audio"}
    assert {f["notification_id"] for f in frames} == {notify["notification_id"]}
    assert "stream_id" not in frames[0]
    assert audio_of(frames).startswith(speech.texts[0].encode())


# --- the service: caps, first audio, ElevenLabs -----------------------------------------------------------


class Provider:
    """A speech provider in memory."""

    id = "openai"
    name = "OpenAI"
    format = "mp3"
    sample_rate = 24000
    default_model = "gpt-4o-mini-tts"
    default_voice = "marin"
    priced_by = "tokens"
    voices_need_key = False

    def __init__(self) -> None:
        self.calls = 0
        self.seen: dict[str, int] = {}
        self.warmed = 0

    async def warm(self) -> None:
        self.warmed += 1

    async def stream(self, text, *, key, model, voice, speed, usage: SpeechUsage) -> AsyncIterator[bytes]:  # type: ignore[no-untyped-def]
        self.calls += 1
        self.seen[text] = self.seen.get(text, 0) + 1
        usage.characters += len(text)
        if "errore" in text or ("instabile" in text and self.seen[text] == 1):
            raise SpeechError("OpenAI speech failed (HTTP 500).", 500)
        if "rotto" in text:
            raise RuntimeError("not a SpeechError")  # anything at all: the reply still ends
        usage.accepted = True  # the provider said 200: it bills from here
        yield text.encode()
        if "lento" in text:
            await asyncio.sleep(30)
        if "pausa" in text:
            await asyncio.sleep(0.05)  # the sentence's end comes after its audio

    async def voices(self, key: str | None) -> list[Voice]:
        return []

    async def check_key(self, key: str) -> str:
        return "ok"


def run_voice(db: psycopg.Connection, secrets_key: str, action: Any) -> Any:
    async def go() -> Any:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        keys = KeyStore(SecretStr(secrets_key))
        provider = Provider()
        try:
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            voice = VoiceService(pool, keys, {"openai": provider}, owner=owner_at())  # type: ignore[dict-item]
            return await action(voice, pool, provider)
        finally:
            await pool.close()

    return asyncio.run(go())


Sent = list[tuple[int, bytes, bool, int | None]]
Marks = list[bool]


async def speak(
    voice: VoiceService, *sentences: str, formats: tuple[str, ...] = ("mp3",)
) -> tuple[Sent, Speaker]:
    sent: Sent = []

    async def send(
        seq: int, data: bytes, final: bool, fmt: str, rate: int, spoken: int | None, end: bool = False
    ) -> bool:
        sent.append((seq, data, final, spoken))
        return True

    speaker = Speaker(voice, send, "t", formats)
    for s in sentences:
        speaker.feed(s)
    speaker.finish()
    await speaker.done()
    return sent, speaker


def test_over_a_cap_no_audio_and_one_notice_a_day(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        async with pool.connection() as conn:
            await ledger.set_caps(
                conn, ledger.Caps(daily_eur=Decimal("0.0000001"), monthly_eur=Decimal("20"))
            )
        first, _ = await speak(voice, "Ciao. ")
        second, _ = await speak(voice, "Ancora. ")
        return first, second, provider.calls

    first, second, calls = run_voice(db, secrets_key, go)
    assert first == second == [] and calls == 0
    notices = db.execute("SELECT kind, text FROM notifications").fetchall()
    assert notices == [("info", rules.CAP_NOTICE["it-IT"])]


def test_the_last_chunk_is_final_and_an_error_ends_the_audio(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        whole, speaker = await speak(voice, "Uno. ", "Due. ")
        broken, _ = await speak(voice, "Uno. ", "errore. ", "Tre. ")
        return whole, speaker.first_audio_at, broken

    whole, first_audio, broken = run_voice(db, secrets_key, go)
    assert whole == [(0, b"Uno. ", False, None), (1, b"Due. ", False, None), (2, b"", True, None)]
    assert first_audio is not None
    # What was said stands; "errore" failed twice, so the phone speaks from character 5 on.
    assert broken == [(0, b"Uno. ", False, None), (1, b"", True, 5)]


def test_turn_latency_has_first_audio(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from structlog.testing import capture_logs

    from pisa.services.conversation import ConversationService
    from tests.gateway_env import gateway

    models = FakeModels()
    models.default = "Ciao."
    device = uuid.UUID(pair(db)[0])

    async def go():  # type: ignore[no-untyped-def]
        async with gateway(dsn(db), secrets_key, models, Clock(datetime.now(UTC))) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "openai", FAKE_KEY)
            voice = VoiceService(pool, keys, {"openai": Provider()}, owner=owner_at())  # type: ignore[dict-item]

            async def send(
                seq: int, data: bytes, final: bool, fmt: str, rate: int, spoken: int | None, end: bool
            ) -> bool:
                return True

            speaker = Speaker(voice, send, "t", ("mp3",))

            async def emit(delta: str, final: bool) -> None:
                speaker.feed(delta)
                if final:
                    speaker.finish()

            service = ConversationService(pool, gw, owner=owner_at())
            await service.respond(device, "ciao", "it-IT", emit, trace_id="t", speaker=speaker)

    with capture_logs() as logs:
        asyncio.run(go())
    (entry,) = [e for e in logs if e["event"] == "turn_latency"]
    assert entry["first_audio_s"] is not None and entry["first_audio_s"] <= entry["total_s"]


def test_prices() -> None:
    usd = rules.cost_usd("tokens", "gpt-4o-mini-tts", 100, 25, 1250)  # a minute of speech
    assert usd == Decimal("0.000015") + Decimal("0.015")
    assert rules.cost_usd("characters", "eleven_flash_v2_5", 1000, None, None) == Decimal("0.05")
    assert rules.cost_usd("characters", "eleven_multilingual_v2", 1000, None, None) == Decimal("0.10")


# --- settings, voices, preview, the ElevenLabs key --------------------------------------------------------


def test_voice_settings(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    assert client.get("/v1/settings", headers=auth).json()["voice"]["provider"] == "device"  # no OpenAI key
    set_key(db, secrets_key)
    assert client.get("/v1/settings", headers=auth).json()["voice"]["provider"] == "openai"
    for bad, message in (
        ({"speed": 1.5}, "voice.speed must be a number from 0.8 to 1.3."),
        ({"provider": "apple"}, "voice.provider must be openai, elevenlabs or device "
                                "(null: OpenAI when its key is set)."),
        ({"voice": ""}, "voice.voice must be null (the provider's default) or the provider's voice id, "
                        "1-80 characters."),
    ):  # fmt: skip
        response = client.patch("/v1/settings", headers=auth, json={"voice": bad})
        assert response.status_code == 400 and response.json()["error"]["message"] == message
    ok = client.patch("/v1/settings", headers=auth, json={"voice": {"provider": "elevenlabs", "speed": 1.2}})
    # H6: a new provider without a voice gets its default (null), never the old provider's voice id.
    assert ok.json()["voice"] == {"provider": "elevenlabs", "model": None, "voice": None, "speed": 1.2}


def test_voices_and_preview(client, db, secrets_key, speech) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    set_key(db, secrets_key)
    openai = client.get("/v1/voice/voices", headers=auth, params={"provider": "openai"}).json()["voices"]
    assert openai[0] == {"id": "marin", "name": "Marin", "languages": ["it-IT", "en-US"],
                         "preview_text": rules.PREVIEW_TEXT["it-IT"]}  # fmt: skip
    assert client.get("/v1/voice/voices", headers=auth, params={"provider": "device"}).json() == {
        "voices": []
    }
    missing = client.get("/v1/voice/voices", headers=auth, params={"provider": "elevenlabs"})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "provider_key_missing"
    audio = client.post("/v1/voice/preview", headers=auth, json={"text": None})
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
    assert audio.content == f"{rules.PREVIEW_TEXT['it-IT']}|1{rules.PREVIEW_TEXT['it-IT']}|2".encode()
    long = client.post("/v1/voice/preview", headers=auth, json={"text": "x" * (rules.PREVIEW_MAX + 1)})
    assert long.status_code == 400
    speech.fail.add("rompi")
    broken = client.post("/v1/voice/preview", headers=auth, json={"text": "rompi"})
    assert broken.status_code == 502 and broken.json()["error"]["code"] == "provider_error"
    set_settings(db, {"voice": {"provider": "elevenlabs"}})
    no_key = client.post("/v1/voice/preview", headers=auth, json={"text": "ciao"})
    assert no_key.status_code == 409 and no_key.json()["error"]["message"] == "No ElevenLabs key is set."


def test_the_elevenlabs_key_and_voice(client, db, secrets_key, speech) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    assert (
        client.put(
            "/v1/ai/providers/elevenlabs/key", headers=auth, json={"key": "el-wrong-xxxxxxxxxx"}
        ).status_code
        == 204
    )
    bad = client.post("/v1/ai/providers/elevenlabs/test", headers=auth).json()
    assert bad["ok"] is False and bad["message"].startswith("ElevenLabs key rejected (HTTP 401)")
    assert (
        client.put("/v1/ai/providers/elevenlabs/key", headers=auth, json={"key": ELEVEN_KEY}).status_code
        == 204
    )
    assert client.post("/v1/ai/providers/elevenlabs/test", headers=auth).json()["ok"] is True
    voices = client.get("/v1/voice/voices", headers=auth, params={"provider": "elevenlabs"}).json()["voices"]
    assert [(v["id"], v["languages"]) for v in voices] == [("v-it", ["it-IT"]), ("v-any", ["it-IT", "en-US"])]
    set_settings(db, {"voice": {"provider": "elevenlabs", "voice": "v-it", "speed": 1.3}})
    audio = client.post("/v1/voice/preview", headers=auth, json={"text": "Ciao Giulia"})
    assert audio.content == b"ID3Ciao Giulia"
    assert speech.bodies[-1] == {
        "text": "Ciao Giulia",
        "model_id": "eleven_flash_v2_5",
        "voice_settings": {"speed": 1.2},
    }
    row = db.execute("SELECT model_ref, input_tokens, cost_eur > 0 FROM llm_calls").fetchone()
    assert row == ("elevenlabs:eleven_flash_v2_5", 12, True)  # billed characters, from the provider
    assert client.delete("/v1/ai/providers/elevenlabs/key", headers=auth).status_code == 204


# --- H1, H2, H3, M9, M10, the cap notice --------------------------------------------------


def test_no_audio_for_a_device_that_cannot_play_it(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    """H3: builds that don't list audio_formats, or not the provider's (mp3), get no say_audio, and nothing is
    synthesised or paid for."""
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Ciao."
    for formats in (None, ["pcm16"]):
        with live(client, token, device_id, audio_formats=formats) as app:
            app.say("ciao", lang="it-IT", source="typed")
            frames = messages_until_done(app, audio=False)
            assert app.ping() == []
        assert not any(f["type"] == "say_audio" for f in frames)
    assert speech.texts == []
    assert db.execute("SELECT count(*) FROM llm_calls WHERE slot = %s", (rules.SLOT,)).fetchone() == (0,)


def test_a_notify_shown_as_a_banner_is_not_synthesised(client, db, secrets_key, speech) -> None:  # type: ignore[no-untyped-def]
    """H3: without companion mode + earbuds or the glasses, a notify is a banner: no audio."""
    set_settings(db, {"briefing": {"window": None}})
    device_id, token = pair(db)
    set_key(db, secrets_key)
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.custom("pisa.presence", {"event": "companion_on"})  # companion mode, but no earbuds
        app.custom("pisa.presence", {"event": "app_foreground"})
        assert app.next()["type"] == "notify"
        time.sleep(0.3)
        assert app.ping() == []  # no say_audio in between
    assert speech.texts == []


def test_the_streams_end_marker_is_not_a_failure(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    """Seen in use: OpenAI ends its stream with "data: [DONE]", which isn't JSON. It read as a
    malformed stream after the whole audio, so the final chunk said complete: false, spoken_chars 0, and the
    phone spoke every reply a second time with its own voice."""
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Era dalle 14:30 alle 16:00."
    speech.done_marker = True
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("a che ora?", lang="it-IT", source="typed")
        frames = messages_until_done(app)
    last = [f for f in frames if f["type"] == "say_audio"][-1]
    assert last["final"] and "complete" not in last and "spoken_chars" not in last
    assert audio_of(frames) == b"".join(f"{t}|1{t}|2".encode() for t in speech.texts)  # all of it, once
    statuses = db.execute("SELECT status FROM llm_calls WHERE slot = %s", (rules.SLOT,)).fetchall()
    assert statuses == [("settled",)]


def test_a_malformed_stream_ends_the_audio_not_the_reply(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    """H1: a bad SSE line is a SpeechError; the reply ends, the audio closes, the row is settled."""
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Prima. Guasta. Terza."
    speech.malformed.add("Guasta")
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("dimmi", lang="it-IT", source="typed")
        frames = messages_until_done(app)
    last = [f for f in frames if f["type"] == "say_audio"][-1]
    assert (last["final"], last["complete"], last["spoken_chars"]) == (True, False, len("Prima. "))
    assert not any(f["type"] == "error" for f in frames)
    statuses = db.execute(
        "SELECT status, error_code FROM llm_calls WHERE slot = %s ORDER BY created_at", (rules.SLOT,)
    ).fetchall()
    assert ("reserved", None) not in statuses and ("settled", "provider_error") in statuses


def test_any_error_in_synthesis_still_ends_the_audio(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """H1: not a SpeechError (a bug, the database): the speaker still ends, and no row stays reserved."""

    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        return (await asyncio.wait_for(speak(voice, "Uno. ", "rotto. ", "Tre. "), 5))[0]

    sent = run_voice(db, secrets_key, go)
    assert sent == [(0, b"Uno. ", False, None), (1, b"", True, 5)]
    rows = db.execute("SELECT status, error_code FROM llm_calls ORDER BY created_at").fetchall()
    assert ("reserved", None) not in rows and ("failed", "internal") in rows


def test_a_reply_that_fails_midway_closes_its_audio(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    """H2: the model fails after a sentence: the device gets the error, and the audio of what was said still
    ends with its final chunk (the speaker's task doesn't leak)."""
    device_id, token = pair(db)
    set_key(db, secrets_key)

    async def broken(messages, info):  # type: ignore[no-untyped-def]
        yield "Prima frase. "
        raise RuntimeError("the model broke")

    fake_models.default = broken
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("dimmi", lang="it-IT", source="typed")
        frames: list[dict[str, Any]] = []
        while not (
            any(f["type"] == "error" for f in frames)
            and any(f["type"] == "say_audio" and f["final"] for f in frames)
        ):
            frames.append(app.next())
    assert audio_of(frames) == b"Prima frase. |1Prima frase. |2"


def test_audio_accepted_by_the_provider_is_billed_even_when_cancelled(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M9: after the provider's 200 the sentence is settled at its price (here the estimate), not at 0."""

    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        async def send(
            seq: int, data: bytes, final: bool, fmt: str, rate: int, spoken: int | None, end: bool = False
        ) -> bool:
            return True

        speaker = Speaker(voice, send, "t", ("mp3",))
        speaker.feed("Un racconto lento. ")
        for _ in range(100):
            if provider.calls:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        speaker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await speaker.done()

    run_voice(db, secrets_key, go)
    for _ in range(50):
        row = db.execute("SELECT status, error_code, cost_eur FROM llm_calls").fetchone()
        if row and row[0] != "reserved":
            break
        time.sleep(0.05)
    assert row is not None and row[:2] == ("settled", "cancelled") and row[2] > 0


def test_spoken_chars_counts_the_text_as_the_phone_holds_it(
    client, db, secrets_key, fake_models: FakeModels, speech
) -> None:  # type: ignore[no-untyped-def]
    """The phone trims each say delta, drops empty ones and joins them with one space: blank lines in the
    reply must not move where it resumes (they did: the count ran ahead by one per extra whitespace char)."""
    device_id, token = pair(db)
    set_key(db, secrets_key)
    fake_models.default = "Prima.\n\n\nGuasta. Terza."
    speech.malformed.add("Guasta")
    with live(client, token, device_id, audio_formats=PLAYS) as app:
        app.say("dimmi", lang="it-IT", source="typed")
        frames = messages_until_done(app)
    said = [f["delta"].strip() for f in frames if f["type"] == "say" and f["delta"].strip()]
    phone_text = " ".join(said)
    last = [f for f in frames if f["type"] == "say_audio"][-1]
    assert last["complete"] is False
    assert phone_text[last["spoken_chars"] :].startswith("Guasta.")


def test_a_failed_sentence_is_retried_once(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """M10: the first try fails, the second speaks: the audio is complete."""

    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        sent, _ = await speak(voice, "Uno. ", "instabile. ")
        return sent, provider.seen

    sent, seen = run_voice(db, secrets_key, go)
    assert sent == [(0, b"Uno. ", False, None), (1, b"instabile. ", False, None), (2, b"", True, None)]
    assert seen["instabile. "] == 2


def test_the_cap_notice_goes_once_under_concurrency(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        await asyncio.gather(*(voice.cap_notice() for _ in range(4)))

    run_voice(db, secrets_key, go)
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (1,)


def test_a_malformed_sse_line_is_a_speech_error() -> None:
    from pisa.connectors.speech import OpenAISpeech

    async def go() -> str:
        sse = 'data: {"type": "speech.audio.delta", "audio": "%%%"}\n\n'
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        )
        async with httpx.AsyncClient(transport=transport) as http:
            usage = SpeechUsage()
            with pytest.raises(SpeechError) as err:
                async for _ in OpenAISpeech(http).stream(
                    "x", key="sk-test-xxxx", model="m", voice="marin", speed=1.0, usage=usage
                ):
                    pass
            assert usage.accepted
            return err.value.message

    assert asyncio.run(go()) == "OpenAI speech sent a malformed stream (Error)."


def test_the_iphone_voice_and_a_provider_change(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """H6: the app saves the Device voice with voice "" (or null); switching provider resets the voice and
    model unless the same patch gives them; null on OpenAI speaks with marin."""
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    set_key(db, secrets_key)
    url = "/v1/settings"
    device = client.patch(url, headers=auth, json={"voice": {"provider": "device", "voice": ""}})
    assert device.status_code == 200 and device.json()["voice"]["voice"] is None
    both = client.patch(url, headers=auth, json={"voice": {"provider": "elevenlabs", "voice": "v-it"}})
    assert both.json()["voice"] | {"speed": 1.0} == {
        "provider": "elevenlabs", "model": None, "voice": "v-it", "speed": 1.0
    }  # fmt: skip
    back = client.patch(url, headers=auth, json={"voice": {"provider": "openai"}})
    assert back.json()["voice"]["voice"] is None and back.json()["voice"]["model"] is None
    same = client.patch(url, headers=auth, json={"voice": {"provider": "openai", "speed": 1.1}})
    assert same.json()["voice"]["voice"] is None  # unchanged provider: nothing reset
    picked = client.patch(url, headers=auth, json={"voice": {"voice": "cedar"}})
    assert picked.json()["voice"]["voice"] == "cedar"
    kept = client.patch(url, headers=auth, json={"voice": {"provider": "openai"}})
    assert kept.json()["voice"]["voice"] == "cedar"


def test_a_null_voice_speaks_with_the_providers_default(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"voice": {"provider": "openai", "voice": None}})

    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        return await voice.config()

    cfg = run_voice(db, secrets_key, go)
    assert cfg is not None and cfg.voice == "marin"
