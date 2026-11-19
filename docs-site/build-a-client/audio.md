# Audio

Pisa talks in two ways, and a client picks one per conversation:

| | **Economy** | **Natural** |
|---|---|---|
| Who recognises speech | the client (the platform's recogniser, or your own) | the server's live voice model |
| What goes up | text: partial transcripts, then the final words as a run | the microphone, as an Opus track over WebRTC |
| What comes back | the answer as text, plus Pisa's voice as audio chunks on `/v2/live` | Pisa's voice on the same WebRTC peer, and text and screens on its data channel |
| Transport | `/v2/live` only | WebRTC (`POST /v1/talk/sessions`), beside `/v2/live` |
| End of utterance | the client decides, helped by turn hints | the server decides |
| Needs on the server | a voice provider for Pisa's voice (optional: without one the client speaks the text itself) | a talker (xAI Grok Voice or Gemini Live) with its key, natural mode on, inside the budget |

**There is no way to send raw audio over `/v2/live` or REST.** A client that wants the server to hear the user's
voice must use WebRTC. A client without WebRTC uses economy: it recognises speech on the device.

Examples: [`voice_economy.py`](examples/voice_economy.py), [`talk_webrtc.py`](examples/talk_webrtc.py).

## Economy: on-device recognition over `/v2/live`

### The hello

List `pisa.voice` and `pisa.voice.turn_hints` among the extensions, and the audio formats the client can play in
`device.audioFormats`:

<!-- include: examples/voice_economy.py#hello-voice -->
```python
VOICE_HELLO: dict[str, Any] = {  # chat_live.hello(device_id, **VOICE_HELLO)
    "extensions": {"pisa.voice": 1, "pisa.voice.turn_hints": 1, "pisa.notice": 1, "pisa.surface": 1},
    # device.audioFormats: the formats this client plays; Pisa's speaker makes mp3, aac_adts or pcm16
    "audio_formats": ("aac_adts", "mp3", "pcm16"),
}
```

Pisa's speaker makes `mp3`, `aac_adts` or `pcm16`; it picks from the client's list. With no formats listed, or
without `pisa.voice` granted, the server synthesises nothing (and spends nothing): the client speaks the text
with its own voice.

### While the user speaks: partials and turn hints

While listening, send the recogniser's words so far as `pisa.voice.partial`, at most two a second (a partial
sent sooner than 0.5 s after the last one gets no hint), with one `turnId` for the whole utterance. Partials
are never stored. Each one answered gets a `pisa.voice.turn_hint` back, in a short run of its own:
`{replyTo: <turnId>, pEnd: 0..1, waitMs: 0..5000}`, how likely the user is done and how long to wait in silence
before treating the utterance as over.

<!-- include: examples/voice_economy.py#partials -->
```python
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
```

<!-- include: examples/voice_economy.py#hints -->
```python
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
```

**End of utterance is the client's decision.** A simple rule: after the last partial, wait `waitMs` of silence
(the latest hint's), then send the final words. Without hints (the extension not granted), use your recogniser's
own end-of-speech. Measured on a server with no model (the hints come from the server's own rules):

```text
after "what's the": end 0%, wait 3000 ms
after "what's the weather tomorrow": end 100%, wait 1200 ms
```

### The final words

An ordinary run, with `turn.source` naming the microphone (`phone_mic`, `earbuds_mic`, `watch_mic`,
`glasses_mic`) and the user message's `id` equal to the partials' `turnId`. The server may also send
`pisa.voice.understood` (`{replyTo, meant, raw, corrections: [{from, to}], ask}`) when it read the words
differently from how they were heard: show `meant`, keep `raw` available.

### Pisa's voice back: `pisa.voice.audio`

When the server has a voice provider and the client listed formats, each answer's text is followed by its audio
in `CUSTOM` events named `pisa.voice.audio` (`$defs/s_pisa_voice_audio`):

| Field | Meaning |
|---|---|
| `messageId` or `noticeId` | the text message (`TEXT_MESSAGE_START.messageId`) or the notice it voices |
| `seq` | 0, 1, 2... per message: play in this order |
| `format` | `mp3`, `aac_adts` or `pcm16` (16-bit little-endian mono) here; `opus` exists in the contract |
| `sampleRate` | 16000, 22050, 24000, 44100 or 48000 |
| `data` | the chunk, standard base64 (up to 400 000 characters) |
| `final` | the last chunk of this message |
| `sentenceEnd`, `complete`, `spokenChars` | on the final chunk, `complete: false` means synthesis stopped early: speak the text from character `spokenChars` on with the device's own voice |

The contract also names an extension `pisa.voice.audio.binary` (the same chunks as binary WebSocket frames);
this server does not grant it, so audio always arrives as these JSON events.

Chunks of one message concatenate into one playable stream (mp3 and ADTS frames are self-delimiting; pcm16 needs
a WAV header or a raw player). Play each chunk as it arrives. If no audio comes within about 1.5 seconds of the
text, speak the text on the device. A barge-in (`op: cancel`) stops the run: stop playback too.

<!-- include: examples/voice_economy.py#audio -->
```python
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
```

The contract's recorded stream `turn_text.jsonl` ([Text chat](text-chat.md#what-a-real-answer-looks-like))
carries three `aac_adts` chunks at 24000 Hz; `python voice_economy.py --assemble <stream> --out reply` writes
them to `reply.aac`.

## Natural: WebRTC

### The peer

The client makes a WebRTC peer with:

- one **audio track**: the microphone, with the platform's echo cancellation on (Pisa must never hear itself).
  WebRTC encodes it as **Opus** (48 kHz on the wire, 20 ms frames); the server resamples it to what its voice
  model needs (16 kHz for the voice gate). There is no other encoding or chunking to choose;
- one **data channel**, label `pisa`, subprotocol **`pisa.v2`**. A channel opened without that subprotocol is
  closed at once.

Make the offer with every ICE candidate gathered (no trickle ICE):

<!-- include: examples/talk_webrtc.py#offer -->
```python
async def offer() -> tuple[Any, Any, str]:
    """A peer with the microphone and the "pisa" data channel; its offer with every ICE candidate (no trickle)."""
    from aiortc import RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    peer = RTCPeerConnection()
    # A real client sends its microphone with echo cancellation on, e.g. aiortc.contrib.media.MediaPlayer
    # ("default", format="pulse") on Linux. AudioStreamTrack is silence: enough to open the talk.
    peer.addTrack(AudioStreamTrack())
    channel = peer.createDataChannel("pisa", protocol="pisa.v2")  # without pisa.v2 the server closes it
    await peer.setLocalDescription(await peer.createOffer())  # aiortc gathers every candidate here
    return peer, channel, peer.localDescription.sdp
```

### Opening the talk

`POST /v1/talk/sessions` with the offer → `201 {session_id, sdp_answer, ice_servers}`. Apply the answer; the
audio and the channel connect. `trigger` is `press`, `wake`, `intent`, `notice`, `proactive` (with `ask_id`, to
answer a `pisa.ask_to_open`) or `glasses`; `lang` is `it-IT` or `en-US`; `route` is where the audio plays
(`speaker`, `receiver`, `airpods`, `headphones`, `car`, `glasses`): on earbuds, glasses and in the car Pisa
speaks shorter. `first_text` carries words said in one breath with a wake phrase. One talk per device: a new
one ends the previous.

<!-- include: examples/talk_webrtc.py#open -->
```python
async def open_talk(session: Session, sdp: str, lang: str = "en-US") -> dict[str, Any]:
    """POST /v1/talk/sessions: 201 {session_id, sdp_answer, ice_servers}."""
    return await session.request(
        "POST",
        "/v1/talk/sessions",
        json={
            "sdp_offer": sdp,
            "trigger": "press",  # press, wake, intent, notice, proactive (with ask_id) or glasses
            "lang": lang,  # it-IT or en-US
            "route": "speaker",  # speaker, receiver, airpods, headphones, car or glasses
            # "first_text": "words said with the wake phrase, recognised on the device",
        },
    )
```

When the server can't talk now it answers 409 `talk_economy` (natural mode is off, or half the day's or month's
AI cap is spent: `economy_reason` says which), 409 `provider_key_missing` (no key for the talker; `provider`
names it), 402 `budget_exceeded`, or 502 `talker_unavailable`. Talk in economy instead:

<!-- include: examples/talk_webrtc.py#fallback -->
```python
try:
    talk = await open_talk(session, sdp)
except PisaError as err:
    if err.code in ECONOMY:  # not now: talk in economy over /v2/live (voice_economy.py)
        print(f"natural voice is off ({err.code}: {err.message}): talk in economy")
        return 0
    raise
```

The peer needs a network path: with no ICE servers configured on the server (`PISA_TALK_ICE_SERVERS`, empty by
default) only host candidates work, so the client must reach the server directly (same network, or a VPN).
`ice_servers` in the answer says what the server uses; configure the same TURN server on the client.

### The data channel

It carries the same frames as `/v2/live`, with its own `seq` from 1: AG-UI event frames on the conversation's
thread, and `pong` and `error` control frames. Each exchange is one run: the user's next words after Pisa
answered start the next run. What arrives:

| Event | Meaning |
|---|---|
| `CUSTOM pisa.voice.state` `{state}` | `listening`, `thinking`, `speaking`, `idle`: drive the orb |
| `CUSTOM pisa.voice.transcript` `{text, final, ignored?}` | the user's words as the server heard them; `text` is the whole line so far. `ignored` (`noise`, `echo`, `language`, `fragment`) on a final: clear the live words and keep listening |
| `CUSTOM pisa.voice.gate` `{heard}` | `owner`, `other`, `none`: whose voice the voice gate hears, at most twice a second |
| `TEXT_MESSAGE_*` | what Pisa says, as text (the voice itself is the audio track) |
| `ACTIVITY_SNAPSHOT` | a screen ([Screens](screens.md)) |
| `RUN_FINISHED` with `outcome: cancelled` | Pisa stopped talking (a barge-in) |
| `RUN_FINISHED` with `result: {"talkEnded": reason}` | the talk is over and the channel closes. `idle` (nobody spoke), `owner` (the user, or a newer talk), `budget`, `error`, `fallback` (carry on in economy over `/v2/live`), `route` (reopen at once on the new route) |

The client sends `ping`, `cancel` (a touch barge-in: Pisa stops at once; speaking over Pisa does the same on its
own), `a2ui` taps (`pisa.choose`, `pisa.ask`; never a yes on an approval), and `pisa.presence` with
`state.route` when the audio route changes. No `hello` and no `run`: those go on `/v2/live`.

<!-- include: examples/talk_webrtc.py#channel -->
```python
def wire(peer: Any, channel: Any, screen: Conversation, reply_path: Path) -> Any:
    """The data channel speaks /v2/live's frames (its own seq from 1); Pisa's voice is the remote audio
    track. Returns the recorder of Pisa's voice (start it, and stop it at the end)."""
    from aiortc.contrib.media import MediaRecorder

    recorder = MediaRecorder(str(reply_path))  # a real client plays the track instead

    @channel.on("open")
    def _open() -> None:
        channel.send(json.dumps({"op": "ping"}))

    @channel.on("message")
    def _message(text: str) -> None:
        screen.frame(json.loads(text))  # pisa.voice.state, .transcript, .gate, TEXT_MESSAGE_*, surfaces...

    @peer.on("track")
    def _track(track: Any) -> None:  # Pisa's voice: Opus at 48 kHz, 20 ms frames; play it as it comes
        recorder.addTrack(track)

    return recorder


CHANNEL_FRAMES = [  # what a client sends on the talk's channel (no hello, no run: those go on /v2/live)
    {"op": "ping"},
    {"op": "cancel", "runId": "run-of-the-exchange-in-progress"},  # a touch barge-in: Pisa stops talking
    # the audio route changed: speaker, receiver, airpods, headphones, car or glasses
    {"op": "custom", "name": "pisa.presence",
     "value": {"event": "earbuds_connected", "state": {"route": "airpods"}}},
    {"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.choose", "surfaceId": "talk:choice-1",
     "sourceComponentId": "c1", "timestamp": "2026-11-18T10:00:00Z", "context": {"optionId": "early"}}}},
]  # fmt: skip
```

`DELETE /v1/talk/sessions/{session_id}?reason=press` ends the talk (`reason`: `press`, `call`, `route`,
`background`, `idle` or `error`).

### Voice enrolment

`POST /v1/talk/sessions` with `purpose: "enrol"` and a `route` records the user's voice for the voice gate over
the same path, after the user's voice consent (409 `voice_consent_missing` without it). Its data channel carries
only `pisa.voice.enrol.*` frames, outside any run (`$defs/EnrolServerFrame`, `$defs/EnrolClientFrame`;
recorded in `protocol/v2/fixtures/enrol/`).

## What CI exercises, and what it doesn't

The examples run against a server with no model, no voice provider and no talker key. CI exercises: the
economy hello and its granted extensions, two partials and their turn hints, a spoken run (to
`model_not_configured`), the WebRTC offer from aiortc and the server's `provider_key_missing` answer, the
assembly of the recorded stream's audio chunks, and the talk channel's client frames against the conformance
kit. It does not exercise: synthesised audio arriving live, a WebRTC session that connects, media in either
direction, the data channel's server frames live, or enrolment. Those formats are checked against the contract's
recorded streams (`talk_gated.jsonl`, `enrol/*.jsonl`) by `pisa-conformance selftest`.
