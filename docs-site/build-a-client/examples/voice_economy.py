"""Voice without WebRTC ("economy"): the client recognises speech itself, sends the words, and plays Pisa's voice.

    python voice_economy.py [--state FILE] [--out reply-audio]
    python voice_economy.py --assemble protocol/v2/fixtures/streams/turn_text.jsonl --out reply-audio

Over `/v2/live`: partial transcripts (`pisa.voice.partial`) get end-of-turn hints back (`pisa.voice.turn_hint`);
the final words are an ordinary run with `turn.source` set to the microphone; Pisa's answer comes as text and, when
the server has a voice provider and the hello listed audio formats, as `pisa.voice.audio` chunks.
"""

import asyncio
import base64
import json
import sys
import uuid
import wave
from pathlib import Path
from typing import Any

from chat_live import Live, turn
from pisa_example import State, arguments, http_client
from render import Conversation
from session import Session

# [guide:hello-voice]
VOICE_HELLO: dict[str, Any] = {  # chat_live.hello(device_id, **VOICE_HELLO)
    "extensions": {"pisa.voice": 1, "pisa.voice.turn_hints": 1, "pisa.notice": 1, "pisa.surface": 1},
    # device.audioFormats: the formats this client plays; Pisa's speaker makes mp3, aac_adts or pcm16
    "audio_formats": ("aac_adts", "mp3", "pcm16"),
}
# [/guide:hello-voice]


# [guide:partials]
def partial(text: str, turn_id: str, lang: str = "en-US") -> dict[str, Any]:
    """The words so far, at most 2 a second while the user speaks; the same turnId for the whole utterance."""
    return {
        "op": "custom",
        "name": "pisa.voice.partial",
        "value": {"text": text, "lang": lang, "turnId": turn_id},
    }


def spoken_turn(thread_id: str, text: str, turn_id: str, lang: str = "en-US") -> dict[str, Any]:
    """The final words: a run like a typed one, said into the phone's microphone."""
    body = turn(thread_id, text, lang)
    body["messages"][0]["id"] = turn_id  # the hints' replyTo is this id (lowercased)
    body["forwardedProps"]["pisa"]["turn"] = {
        "source": "phone_mic",  # glasses_mic, phone_mic, earbuds_mic, watch_mic or typed
        "lang": lang,
        "wake": "tap",  # how the listening started: wake_phrase, stem_press, tap, reply, glasses_button, intent
        "voice": {"pace": "usual", "place": "normal"},  # optional, what the recogniser noticed
    }
    return body


# [/guide:partials]


# [guide:audio]
class Speaker:
    """Pisa's voice for one message (messageId) or notice (noticeId): base64 chunks in `seq` order, the last
    one `final`. `complete: false` on the final chunk: speak the text from `spokenChars` on with the device's
    own voice. No audio at all within ~1.5 s of the text: speak the whole text on the device."""

    def __init__(self) -> None:
        self.chunks: dict[str, dict[int, bytes]] = {}
        self.formats: dict[str, tuple[str, int]] = {}
        self.done: dict[str, dict[str, Any]] = {}

    def add(self, value: dict[str, Any]) -> str | None:
        key = value.get("messageId") or value["noticeId"]
        self.chunks.setdefault(key, {})[value["seq"]] = base64.b64decode(value["data"])
        self.formats[key] = (value["format"], value["sampleRate"])  # e.g. aac_adts at 24000 Hz
        if value["final"]:
            self.done[key] = value
            return key  # every chunk is in: play (a real client plays each chunk as it arrives)
        return None

    def save(self, key: str, stem: Path) -> Path:
        audio = b"".join(chunk for _, chunk in sorted(self.chunks[key].items()))
        fmt, rate = self.formats[key]
        if fmt == "pcm16":  # 16-bit little-endian mono PCM at sampleRate
            path = stem.with_suffix(".wav")
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(audio)
            return path
        path = stem.with_suffix({"mp3": ".mp3", "aac_adts": ".aac", "opus": ".opus"}[fmt])
        path.write_bytes(audio)  # mp3 and ADTS frames concatenate into one playable stream
        return path


# [/guide:audio]


async def converse(state: State, out: Path) -> int:
    failures = 0
    async with http_client(state) as http:
        session = Session(http, state)
        live = Live(state.server, await session.bearer(), state.device_id)
        welcome = await live.connect(**VOICE_HELLO)
        print(f"welcome extensions: {sorted(welcome['extensions'])}")
        failures += "pisa.voice.turn_hints" not in welcome["extensions"]
        thread, turn_id, hints = f"voice-{uuid.uuid4()}", str(uuid.uuid4()), []
        screen, speaker = Conversation(), Speaker()
        # [guide:hints]
        for words in ("what's the", "what's the weather tomorrow"):
            await asyncio.sleep(0.5)  # at most 2 partials a second: one sent sooner gets no hint
            await live.send(partial(words, turn_id))
            while True:  # each partial gets one hint, in a short run of its own
                frame = await live.recv()
                event = frame.get("event") or {}
                if event.get("type") == "CUSTOM" and event["name"] == "pisa.voice.turn_hint":
                    hint = event[
                        "value"
                    ]  # {replyTo, pEnd: 0..1 that the user is done, waitMs: how long to wait}
                    hints.append(hint)
                    print(f"after {words!r}: end {hint['pEnd']:.0%}, wait {hint['waitMs']} ms")
                    break
        # End of utterance is the client's call: when the user is silent for the hint's waitMs, send the words.
        # [/guide:hints]
        failures += len(hints) != 2 or hints[0]["replyTo"] != turn_id
        await live.send({"op": "run", "input": spoken_turn(thread, "what's the weather tomorrow", turn_id)})
        while True:
            frame = await live.recv()
            screen.frame(frame)
            event = frame.get("event") or {}
            if event.get("name") == "pisa.voice.audio" and (key := speaker.add(event["value"])):
                print(f"Pisa's voice: {speaker.save(key, out)}")
            if frame.get("threadId") == thread and event.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
                break
        await live.close()
    return failures


def assemble(path: Path, out: Path) -> int:
    """The audio of a recorded stream (the contract's fixture has three aac_adts chunks), saved to a file."""
    speaker = Speaker()
    for line in path.read_text().splitlines():
        event = json.loads(line).get("event") or {}
        if event.get("name") == "pisa.voice.audio" and (key := speaker.add(event["value"])):
            saved = speaker.save(key, out)
            print(f"{key}: {len(speaker.chunks[key])} chunks, {speaker.formats[key]}, saved {saved}")
    return 0 if speaker.done else 1


def main() -> int:
    parser = arguments(__doc__ or "")
    parser.add_argument("--assemble", type=Path, help="a recorded stream to take the audio from")
    parser.add_argument("--out", type=Path, default=Path("reply-audio"))
    args = parser.parse_args()
    if args.assemble:
        return assemble(args.assemble, args.out)
    return 1 if asyncio.run(converse(State.load(args.state), args.out)) else 0


if __name__ == "__main__":
    sys.exit(main())
