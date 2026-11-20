"""Speech providers: OpenAI's speech endpoint and ElevenLabs' streaming text-to-speech. Both stream
mp3 as it is made; neither logs or returns the text. Errors name the provider, the HTTP status and the action.

- OpenAI: `POST /v1/audio/speech` with `stream_format: "sse"`, so the audio arrives as `speech.audio.delta`
  events and the last event carries the exact token usage (mp3, 24 kHz).
- ElevenLabs: `POST /v1/text-to-speech/{voice_id}/stream` (mp3, 44.1 kHz, 64 kb/s), billed per character.
"""

import base64
import json
import re
from collections.abc import AsyncIterator

import httpx

from pisa.domain import locale, model_catalog
from pisa.domain.voice import PricedBy
from pisa.failures import external_failure
from pisa.logging import get_logger
from pisa.ports.speech import AudioFormat, SpeechError, SpeechProvider, SpeechUsage, Voice

_log = get_logger("pisa.speech")

TIMEOUT = httpx.Timeout(20.0, connect=5.0)
WARM_TIMEOUT = httpx.Timeout(2.0)
BOTH = locale.WIRE_TAGS


# the provider's own words for an empty
# account (OpenAI 429 `insufficient_quota`, ElevenLabs 401 `quota_exceeded`, a 402)
_NO_CREDIT = re.compile(r"\b(?:insufficient_quota|quota_exceeded)\b")  # the codes only


def _failure(name: str, status: int, where: str, reason: object = None) -> SpeechError:
    # the provider's own words in the log (never the key: it travels in a header)
    external_failure(name.lower(), where, status, reason)
    if status == 402 or (status in (401, 403, 429) and _NO_CREDIT.search(str(reason or ""))):
        return SpeechError(
            f"{name} has no credits left (HTTP {status}): top up its account, or pick another voice in "
            "Settings → Voice.",
            status,
            no_credit=True,
        )
    if status in (401, 403):
        return SpeechError(
            f"{name} key rejected (HTTP {status}): add a valid key in Settings → AI providers.", status
        )
    if status == 429:
        return SpeechError(f"{name} {where} is rate-limited or out of credit (HTTP 429).", status)
    return SpeechError(f"{name} {where} failed (HTTP {status}).", status)


def _unreachable(name: str, exc: Exception) -> SpeechError:
    external_failure(name.lower(), "speech", type(exc).__name__, exc)
    return SpeechError(f"{name} speech is unreachable ({exc.__class__.__name__}).")


def _malformed(name: str, exc: Exception) -> SpeechError:
    external_failure(name.lower(), "speech stream", type(exc).__name__, exc)
    return SpeechError(f"{name} speech sent a malformed stream ({exc.__class__.__name__}).")


async def _warm(http: httpx.AsyncClient, url: str) -> None:
    """Open (or keep) the connection to the provider: an unauthenticated HEAD, answered at once and never
    billed. Best effort: a failure only means the first sentence connects as before."""
    try:
        await http.head(url, timeout=WARM_TIMEOUT)
    except httpx.HTTPError:  # not reached now: the first sentence connects by itself
        return
    except Exception as exc:  # anything else is a bug; the first sentence still connects by itself
        _log.warning("speech_warm_failed", cause=exc.__class__.__name__)


class OpenAISpeech:
    id = "openai"
    name = "OpenAI"
    format: AudioFormat = "mp3"
    sample_rate = 24_000
    priced_by: PricedBy = "tokens"
    voices_need_key = False

    @property
    def default_model(self) -> str:
        return model_catalog.default_model("speech", "openai")

    default_voice: str | None = "marin"
    url = "https://api.openai.com/v1/audio/speech"
    # the speech model's built-in voices: each speaks Italian and English.
    VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage",
              "shimmer", "verse")  # fmt: skip

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def warm(self) -> None:
        await _warm(self._http, self.url)

    async def stream(
        self, text: str, *, key: str, model: str, voice: str, speed: float, usage: SpeechUsage
    ) -> AsyncIterator[bytes]:
        body = {"model": model, "input": text, "voice": voice, "response_format": "mp3", "speed": speed,
                "stream_format": "sse"}  # fmt: skip
        usage.characters += len(text)
        try:
            async with self._http.stream(
                "POST", self.url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise _failure(self.name, response.status_code, "speech", response.text)
                usage.accepted = True
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    async for chunk in response.aiter_bytes():  # plain audio: no usage given
                        usage.audio_bytes += len(chunk)
                        yield chunk
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if line[5:].strip() == "[DONE]":  # the stream's end marker, not JSON
                        return  # read as malformed, the phone would speak the reply again
                    try:
                        event = json.loads(line[5:].strip() or "{}")
                        if not isinstance(event, dict):
                            raise TypeError("an SSE event is not an object")
                        chunk = (
                            base64.b64decode(event.get("audio") or "", validate=True)
                            if event.get("type") == "speech.audio.delta"
                            else None
                        )
                    except (ValueError, TypeError) as exc:  # bad JSON or base64 (binascii.Error is one)
                        raise _malformed(self.name, exc) from None
                    if chunk is not None:
                        usage.audio_bytes += len(chunk)
                        yield chunk
                    elif event.get("type") == "speech.audio.done":
                        tokens = event.get("usage") or {}
                        usage.input_tokens = tokens.get("input_tokens")
                        usage.output_tokens = tokens.get("output_tokens")
                        return  # the audio is complete: nothing after it can spoil it
        except httpx.HTTPError as exc:
            raise _unreachable(self.name, exc) from None

    async def voices(self, key: str | None) -> list[Voice]:
        return [Voice(v, v.capitalize(), BOTH) for v in self.VOICES]

    async def check_key(self, key: str) -> str:
        return "OpenAI keys are checked in Settings → AI providers → OpenAI."


class ElevenLabsSpeech:
    id = "elevenlabs"
    name = "ElevenLabs"
    format: AudioFormat = "mp3"
    sample_rate = 44_100
    priced_by: PricedBy = "characters"
    voices_need_key = True

    @property
    def default_model(self) -> str:
        return model_catalog.default_model("speech", "elevenlabs")

    default_voice: str | None = None  # an account's voices are its own: the user picks one
    base = "https://api.elevenlabs.io/v1"
    SPEED = (0.7, 1.2)  # ElevenLabs' range; Pisa's setting (0.8-1.3) is clamped to it

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def warm(self) -> None:
        await _warm(self._http, self.base)

    async def stream(
        self, text: str, *, key: str, model: str, voice: str, speed: float, usage: SpeechUsage
    ) -> AsyncIterator[bytes]:
        body = {"text": text, "model_id": model,
                "voice_settings": {"speed": min(max(speed, self.SPEED[0]), self.SPEED[1])}}  # fmt: skip
        url = f"{self.base}/text-to-speech/{voice}/stream"
        try:
            async with self._http.stream(
                "POST", url, params={"output_format": "mp3_44100_64"}, json=body,
                headers={"xi-api-key": key}, timeout=TIMEOUT,
            ) as response:  # fmt: skip
                if response.status_code != 200:
                    await response.aread()
                    raise _failure(self.name, response.status_code, "speech", response.text)
                usage.accepted = True
                billed = response.headers.get("character-cost")
                usage.characters += int(billed) if billed and billed.isdigit() else len(text)
                async for chunk in response.aiter_bytes():
                    usage.audio_bytes += len(chunk)
                    yield chunk
        except httpx.HTTPError as exc:
            raise _unreachable(self.name, exc) from None

    async def voices(self, key: str | None) -> list[Voice]:
        if not key:
            raise SpeechError("No ElevenLabs key is set: add it in Settings → AI providers.")
        try:
            response = await self._http.get(
                f"{self.base}/voices", headers={"xi-api-key": key}, timeout=TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise _unreachable(self.name, exc) from None
        if response.status_code != 200:
            raise _failure(self.name, response.status_code, "voice list", response.text)
        out = []
        for v in response.json().get("voices") or []:
            codes = {str(lang.get("language") or "")[:2] for lang in v.get("verified_languages") or []}
            languages = tuple(code for code in BOTH if code[:2] in codes) or BOTH  # multilingual otherwise
            out.append(Voice(str(v["voice_id"]), str(v.get("name") or v["voice_id"]), languages))
        return out

    async def check_key(self, key: str) -> str:
        try:
            response = await self._http.get(f"{self.base}/user", headers={"xi-api-key": key}, timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise _unreachable(self.name, exc) from None
        if response.status_code != 200:
            raise _failure(self.name, response.status_code, "key check", response.text)
        return "The ElevenLabs key works."


def speech_providers(http: httpx.AsyncClient) -> dict[str, SpeechProvider]:
    """The speech adapters, by provider id."""
    return {"openai": OpenAISpeech(http), "elevenlabs": ElevenLabsSpeech(http)}
