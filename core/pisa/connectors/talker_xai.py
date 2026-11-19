"""Grok Voice (xAI), Pisa's default talker (it uses the xAI key Pisa already has). Its realtime API is
OpenAI-Realtime-compatible over a WebSocket (`wss://api.x.ai/v1/realtime?model=…`, docs.x.ai voice agent
guide; the event shapes as Pipecat's xAI service sends them): `session.update` once, the user's audio as
`input_audio_buffer.append`, Pisa's audio as `response.output_audio.delta`, the user's words from
the transcription model (`…input_audio_transcription.updated` / `.completed`), Pisa's words as
`response.output_audio_transcript.*`, and `ask_brain` as a function call.

Whispers: every whisper is a system item
(`conversation.item.create`); NOW cancels the response being spoken and asks for a new one at once; PAUSE asks
for one when the talker is idle; SILENT adds the item only. An ask_brain answer is the call's
`function_call_output` with the same priorities.

The message mapping is pure (`session_update`, `whisper_events`, `answer_events`, `parse`), so it is tested
without a socket. Never logs the words or the audio.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import websockets
from websockets.asyncio.client import ClientConnection

from pisa.domain import model_catalog
from pisa.domain.whisper import Priority, Whisper, framed
from pisa.failures import external_failure
from pisa.logging import get_logger
from pisa.ports.talker import (
    ASK_BRAIN_NAME,
    ASK_BRAIN_PARAMETERS,
    AskBrain,
    Audio,
    Closed,
    OwnerSpeaking,
    OwnerWords,
    PisaWords,
    Speaking,
    TalkerConfig,
    TalkerError,
    TalkerEvent,
    ask_brain_description,
)

URL = "wss://api.x.ai/v1/realtime"
RATE = 24_000  # PCM in and out (Grok takes 8 to 48 kHz)
_log = get_logger("pisa.talker.xai")

ASK_BRAIN = {
    "type": "function",
    "name": ASK_BRAIN_NAME,
    "description": ask_brain_description("a whisper"),
    "parameters": ASK_BRAIN_PARAMETERS,
}


def session_update(cfg: TalkerConfig) -> dict[str, Any]:
    """The session's whole configuration, sent once after connecting."""
    return {
        "type": "session.update",
        "session": {
            "instructions": cfg.instructions,
            "voice": cfg.voice,
            # with the user-voice gate on, xAI's own voice detection is off ("null for
            # manual turns", docs.x.ai voice agent guide): Pisa commits their turns and asks for the answer
            "turn_detection": {"type": None} if cfg.client_activity else {"type": "server_vad"},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "transcription": {  # streams the user's words as they are said
                        "model": model_catalog.default_model("transcribe", "xai"),
                        "language_hint": cfg.lang[:2],
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": RATE}},
            },
            "tools": [ASK_BRAIN],
        },
    }


def _system_item(text: str) -> dict[str, Any]:
    return {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]},
    }


def _respond(priority: Priority, responding: bool, owner_speaking: bool = False) -> list[dict[str, Any]]:
    """NOW: cancel what is being said, then answer; PAUSE: answer now when idle (else after the current
    response: the caller holds it); SILENT: nothing. While the user is speaking nothing new is created
    (9): server VAD answers their turn, and the item is already in the context."""
    if priority is Priority.NOW:
        cancel = [{"type": "response.cancel"}] if responding else []
        return cancel + ([] if owner_speaking else [{"type": "response.create"}])
    if priority is Priority.PAUSE and not responding and not owner_speaking:
        return [{"type": "response.create"}]
    return []


def whisper_events(w: Whisper, *, responding: bool, owner_speaking: bool = False) -> list[dict[str, Any]]:
    """A whisper as client events (NOW cancels first: the item must land after the cancel)."""
    respond = _respond(w.priority, responding, owner_speaking)
    cancel = [e for e in respond if e["type"] == "response.cancel"]
    rest = [e for e in respond if e["type"] != "response.cancel"]
    return [*cancel, _system_item(framed(w)), *rest]


def answer_events(
    call_id: str, w: Whisper, *, responding: bool, owner_speaking: bool = False
) -> list[dict[str, Any]]:
    """The brain's answer to an ask_brain call: its output item, then a response as its priority says."""
    respond = _respond(w.priority, responding, owner_speaking)
    cancel = [e for e in respond if e["type"] == "response.cancel"]
    rest = [e for e in respond if e["type"] != "response.cancel"]
    output = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"whisper": framed(w)}),
        },
    }
    return [*cancel, output, *rest]


def parse(event: dict[str, Any]) -> list[TalkerEvent]:
    """A server event as the port's events (unknown ones are dropped)."""
    kind = event.get("type", "")
    if kind == "response.output_audio.delta":
        return [Audio(base64.b64decode(event.get("delta") or ""))]
    if kind == "conversation.item.input_audio_transcription.updated":  # the words so far (Grok's name)
        return [OwnerWords(str(event.get("transcript") or ""), final=False)]
    if kind == "conversation.item.input_audio_transcription.delta":  # OpenAI Realtime's name: a delta
        return [OwnerWords(str(event.get("delta") or ""), final=False, delta=True)]
    if kind == "conversation.item.input_audio_transcription.completed":
        return [OwnerWords(str(event.get("transcript") or ""), final=True)]
    if kind == "response.output_audio_transcript.delta":
        return [PisaWords(str(event.get("delta") or ""), final=False)]
    if kind == "response.output_audio_transcript.done":
        return [PisaWords(str(event.get("transcript") or ""), final=True)]
    if kind == "response.function_call_arguments.done" and event.get("name") == ASK_BRAIN_NAME:
        try:
            question = str(json.loads(event.get("arguments") or "{}").get("question") or "")
        except ValueError:
            question = ""
        return [AskBrain(str(event.get("call_id") or ""), question)]
    if kind == "input_audio_buffer.speech_started":
        return [OwnerSpeaking(True)]
    if kind == "input_audio_buffer.speech_stopped":
        return [OwnerSpeaking(False)]
    if kind == "response.output_audio.done":
        return [Speaking(False)]
    if kind == "error":
        error = event.get("error") or {}
        _log.warning("talker_error", provider="xai", code=error.get("code"), kind=error.get("type"))
        # xAI's own words for it
        external_failure(
            "grok_voice", "session", error.get("code") or error.get("type"), error.get("message")
        )
    return []


class GrokVoiceSession:
    input_rate = RATE
    output_rate = RATE
    cancels_on_activity = True  # their turn's start cancels the reply under way

    def __init__(self, ws: ClientConnection, *, client_activity: bool = False) -> None:
        self._ws = ws
        self._client = client_activity
        self._responding = False
        self._held: list[dict[str, Any]] = []  # a PAUSE's response.create, sent when the talker is idle
        self._speaking = False
        self._owner_speaking = False  # server VAD: between speech_started and speech_stopped
        self._said = ""  # Pisa's words so far in this response
        self._heard = ""  # the user's words so far, when the partials come as deltas
        self._item = ""  # Pisa's message being spoken (for conversation.item.truncate)

    async def _send(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            await self._ws.send(json.dumps(event))

    async def send_audio(self, pcm: bytes) -> None:
        await self._send([{"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}])

    async def whisper(self, w: Whisper) -> None:
        events = whisper_events(w, responding=self._responding, owner_speaking=self._owner_speaking)
        await self._send(events)
        self._hold_if_busy(w)

    async def answer(self, call_id: str, w: Whisper) -> None:
        await self._send(
            answer_events(call_id, w, responding=self._responding, owner_speaking=self._owner_speaking)
        )
        self._hold_if_busy(w)

    def _hold_if_busy(self, w: Whisper) -> None:
        if w.priority is Priority.PAUSE and self._responding and not self._owner_speaking:
            self._held = [{"type": "response.create"}]  # one answer at the next pause is enough

    async def stop(self) -> None:
        self._held = []  # a barge-in by touch: nothing held is said after it either
        if self._responding:
            await self._send([{"type": "response.cancel"}])

    async def activity_start(self) -> None:
        """Their turn starts: what the buffer held is dropped (nothing of anyone else's), Pisa's answer
        under way is cancelled (Pisa's playout already stopped on the phone), and their audio follows."""
        self._held = []
        self._owner_speaking = True
        events: list[dict[str, Any]] = [{"type": "input_audio_buffer.clear"}]
        if self._responding:
            events.insert(0, {"type": "response.cancel"})
        await self._send(events)

    async def activity_end(self) -> None:
        """Their turn ends: the buffer is committed (transcribed) and answered."""
        self._owner_speaking = False
        await self._send([{"type": "input_audio_buffer.commit"}, {"type": "response.create"}])

    async def end_turn(self) -> None:
        """Grok's server VAD ends the user's turn itself (it transcribes as they speak):
        nothing to do by hand. With the gate on, the gate's end is the turn's."""
        if self._client:
            await self.activity_end()

    async def truncate(self, audio_end_ms: int) -> None:
        """Barge-in: the model's record of what Pisa said ends where the user stopped hearing it."""
        if self._item:
            cut = {"type": "conversation.item.truncate", "item_id": self._item, "content_index": 0,
                   "audio_end_ms": max(0, audio_end_ms)}  # fmt: skip
            await self._send([cut])

    async def events(self) -> AsyncIterator[TalkerEvent]:
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                kind = event.get("type", "")
                if kind == "response.created":
                    self._responding = True
                elif (
                    kind == "response.output_item.added"
                    and (event.get("item") or {}).get("type") == "message"
                ):
                    self._item = str((event.get("item") or {}).get("id") or "")
                elif kind == "input_audio_buffer.speech_started":
                    self._owner_speaking = True
                    self._held = []  # the user took the floor: VAD's answer to them reads the item
                elif kind == "input_audio_buffer.speech_stopped":
                    self._owner_speaking = False
                elif kind == "response.done":
                    self._responding = False
                    if self._speaking:
                        self._speaking = False
                        yield Speaking(False)
                    cut = (event.get("response") or {}).get("status") == "cancelled"
                    held, self._held = self._held, []
                    if held and not cut and not self._owner_speaking:  # never over the user
                        await self._send(held)
                for out in parse(event):
                    if isinstance(out, Audio) and not self._speaking:
                        self._speaking = True
                        yield Speaking(True)
                    if isinstance(out, Speaking):
                        self._speaking = out.on
                    if isinstance(out, OwnerWords) and out.delta:
                        self._heard += out.text
                        out = OwnerWords(self._heard, False)
                    elif isinstance(out, OwnerWords):
                        self._heard = "" if out.final else out.text
                    if isinstance(out, PisaWords):  # the port gives the words so far, never a delta
                        self._said = out.text if out.final else self._said + out.text
                        out = PisaWords(self._said, out.final)
                        if out.final:
                            self._said = ""
                    yield out
        except websockets.ConnectionClosed as exc:
            yield Closed("provider", f"closed ({exc.code})")
            return
        except (OSError, ValueError) as exc:
            yield Closed("error", exc.__class__.__name__)
            return
        yield Closed("provider", "closed")

    async def close(self) -> None:
        await self._ws.close()


class GrokVoice:
    id = "xai"
    name = "Grok Voice"
    key_provider = "xai"
    default_voice = "eve"
    # Candidates from xAI's built-in roster (`eve` is xAI's default; the rest as the voice agent guide names
    # them, to verify against `GET /v1/tts/voices`). Grok Voice lists Italian for each; the user picks by
    # ear, and a custom voice id works too.
    voices: tuple[str, ...] = ("eve", "ara", "leo", "rex", "sal")
    usd_per_min = 0.08  # FACT: Grok Voice, $0.08 a minute (+ $0.004 per text input)

    def __init__(self, url: str = URL) -> None:
        self._url = url

    async def open(self, cfg: TalkerConfig) -> GrokVoiceSession:
        model = model_catalog.default_model("talker", "xai")  # the model catalog's Grok Voice model
        uri = f"{self._url}?model={quote(model, safe='')}"
        try:
            ws = await asyncio.wait_for(
                websockets.connect(uri, additional_headers={"Authorization": f"Bearer {cfg.key}"}), timeout=10
            )
        except websockets.InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise TalkerError(
                    f"xAI key rejected by Grok Voice (HTTP {status}): add a valid key in Settings → AI "
                    "providers.",
                    status,
                ) from None
            body = exc.response.body.decode(errors="replace") if exc.response.body else None
            raise TalkerError(f"Grok Voice refused the session (HTTP {status}).", status, body) from None
        except (OSError, TimeoutError) as exc:
            raise TalkerError(
                f"Grok Voice is unreachable ({exc.__class__.__name__}).", None, str(exc)
            ) from None
        session = GrokVoiceSession(ws, client_activity=cfg.client_activity)
        await session._send([session_update(cfg)])
        return session
