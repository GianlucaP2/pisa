# Text chat

A turn is an [AG-UI 1.0](https://docs.ag-ui.com) run: the client sends a `RunAgentInput` with the user's message,
the server answers with a stream of events that starts with `RUN_STARTED` and ends with `RUN_FINISHED` or
`RUN_ERROR`. Two ways carry it:

- **`POST /v2/runs`**: one HTTP request per turn, the events as server-sent events or one JSON document. Simple;
  but only that turn's events come back.
- **`/v2/live`**: one WebSocket for the whole session. Turns, and everything Pisa says unasked (notices,
  approvals, a changed disclosure), on one connection, with resume after a dropped network.

A client that is open on screen uses `/v2/live`; a widget, a shortcut or a script can use `POST /v2/runs`.

Examples: [`consent.py`](examples/consent.py), [`chat_runs.py`](examples/chat_runs.py),
[`chat_runs.sh`](examples/chat_runs.sh), [`chat_live.py`](examples/chat_live.py).

## Consent first

Before any turn reaches an AI provider, the user must have seen which providers get which data and said yes. A
turn without it ends at once, before any AI call, with `RUN_ERROR` code `ai_consent_required`.

`GET /v1/ai/disclosure` answers `{revision, processors: [{slot, provider: {id, name, kind, region?}, model,
dataCategories, features?}], healthRequiresSeparateConsent: true}` (`$defs/AIDisclosure`). `kind` is `cloud`,
`self_hosted` or `on_server`; `dataCategories` are among `conversation`, `mail`, `calendar`, `places`,
`location`, `people`, `health`, `finance`, `documents`, `voice`. Show every processor, then record the yes with
`POST /v1/ai/consent {revision, health}` (204). Health is its own question, never pre-ticked. In natural mode
the disclosure lists the talker (slot `conversation.talker`, `dataCategories` with `voice`): it hears everything
said in a talk, so opening a natural talk needs `health: true` too, and without it the talk is refused with
`ai_consent_required`. Say so where you ask: an economy talk and typed turns don't need it. While an OpenAI or
ElevenLabs voice is the chosen voice (`settings.voice`, with its key set), the disclosure lists it too (slot
`conversation.voice`, the conversation's `dataCategories`): every sentence Pisa says goes to it to be spoken.
Choosing such a voice can change the revision; the phone's own voice (`device`) is no processor.

<!-- include: examples/consent.py#disclosure -->
```python
async def ask_consent(session: Session, health: bool = False) -> dict[str, Any]:
    """Show which providers get which data, then record the user's yes for this revision."""
    disclosure = await session.request("GET", "/v1/ai/disclosure")
    for p in disclosure["processors"]:  # one per feature slot: show all of them before asking
        provider = p["provider"]  # {id, name, kind: cloud | self_hosted | on_server, region?}
        print(
            f"{p['slot']}: {provider['name']} ({provider['kind']}) {p['model']} gets {', '.join(p['dataCategories'])}"
        )
    # health is asked separately (healthRequiresSeparateConsent is always true): never pre-ticked
    await session.request(
        "POST", "/v1/ai/consent", json={"revision": disclosure["revision"], "health": health}
    )
    return disclosure


def with_consent(body: dict[str, Any], revision: str, health: bool = False) -> dict[str, Any]:
    """Or carry it in each run: forwardedProps.pisa.consent wins over the recorded one."""
    body["forwardedProps"]["pisa"]["consent"] = {"aiRevision": revision, "health": health}
    return body
```

The revision changes whenever a provider, a model or a data category does. Then a recorded consent no longer
counts (`POST /v1/ai/consent` with an old revision answers 409 `ai_consent_required`), and a connected client
gets `pisa.ai.disclosure` with the new document: ask again.

## The turn

<!-- include: examples/chat_live.py#turn -->
```python
def turn(thread_id: str, text: str, lang: str = "en-US") -> dict[str, Any]:
    """An AG-UI RunAgentInput: one user message. `forwardedProps.pisa.turn` says how it was said."""
    message_id = str(uuid.uuid4())
    return {
        "threadId": thread_id,  # yours: one per conversation; never one starting with "pisa-"
        "runId": f"run-{uuid.uuid4()}",  # yours, new for every run
        "messages": [{"id": message_id, "role": "user", "content": text}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": lang}}},
    }
```

| Field | Rules |
|---|---|
| `threadId` | the client's own, 1 to 120 characters, one per conversation. Ids starting with `pisa-` are the server's |
| `runId` | the client's own, 1 to 80 characters, new for every run |
| `messages` | AG-UI messages; the last `user` message is the turn. Its `content` is a string (or text parts). Its `id`, a UUID, lets later turns refer to it |
| `forwardedProps.pisa.turn` | `source` (`typed`, `phone_mic`, `earbuds_mic`, `watch_mic`, `glasses_mic`) and `lang` (`en-US` or `it-IT`) are required; `wake`, `origin`, `continues` (an earlier turn's message id this one finishes) and `voice` are optional (`$defs/TurnProps`) |
| `forwardedProps.pisa.consent` | optional `{aiRevision, health}`: the consent for this run, over the recorded one |

The server keeps the conversation: send only the new message, not the history.

## Over HTTP: `POST /v2/runs`

With `Accept: text/event-stream` the answer is a stream of `data: <event JSON>` lines, a blank line after each;
closing the request cancels the run.

<!-- include: examples/chat_runs.py#sse -->
```python
async def run_streamed(session: Session, body: dict[str, Any], screen: Conversation) -> dict[str, Any]:
    """`Accept: text/event-stream`: one `data: <event JSON>` line per event, a blank line between. Closing the
    request cancels the run."""
    headers = await session.bearer() | {"Accept": "text/event-stream"}
    last: dict[str, Any] = {}
    async with session.http.stream("POST", "/v2/runs", json=body, headers=headers) as answer:
        if answer.status_code != 200:
            await answer.aread()
            checked(answer)  # 400 validation_failed, 401...: the error envelope
        async for line in answer.aiter_lines():
            if line.startswith("data:"):
                last = json.loads(line[5:])
                screen.event(last, body["threadId"])
    return last
```

Without it, the answer is `200 {"events": [...]}` when the run ends within 8 seconds, else `202 {"runId"}`: the
run goes on, and its words stay in the conversation.

<!-- include: examples/chat_runs.py#json -->
```python
async def run_collected(
    session: Session, body: dict[str, Any], screen: Conversation
) -> dict[str, Any] | None:
    """`Accept: application/json`: 200 `{"events": [...]}` when the run ends within 8 s, else 202 `{"runId"}`
    (the run goes on; its words stay in the conversation, read them later from GET /v1/changes or /v2/live)."""
    answer = await session.http.post("/v2/runs", json=body, headers=await session.bearer())
    collected = checked(answer)
    if answer.status_code == 202:
        print(f"still running: {collected['runId']}")
        return None
    for event in collected["events"]:
        screen.event(event, body["threadId"])
    return collected["events"][-1]
```

With curl:

<!-- include: examples/chat_runs.sh#curl -->
```bash
curl -sS -N "$PISA_SERVER/v2/runs" \
  -H "Authorization: Bearer $PISA_TOKEN" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"threadId": "curl-chat", "runId": "run-curl-1",
       "messages": [{"id": "m1", "role": "user", "content": "What is on today?"}],
       "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "en-US"}}}}'
# data: {"type":"RUN_STARTED","timestamp":...,"threadId":"curl-chat","runId":"run-curl-1","protocolVersion":"1.0"}
# data: {"type":"TEXT_MESSAGE_START",...}    ... and so on, to RUN_FINISHED or RUN_ERROR

curl -sS "$PISA_SERVER/v2/runs" \
  -H "Authorization: Bearer $PISA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"threadId": "curl-chat", "runId": "run-curl-2",
       "messages": [{"id": "m2", "role": "user", "content": "And tomorrow?"}]}'
# {"events": [{"type": "RUN_STARTED", ...}, ..., {"type": "RUN_FINISHED", ...}]}
```

A request the server can't take answers with the error envelope (400 `validation_failed`, 401...). A run it
takes but refuses (no user message, a thread that is the server's) is a run that ends `RUN_ERROR`
`invalid_frame`: handle both.

## Over the live socket: `/v2/live`

Connect with the WebSocket subprotocol `pisa.v2` and the session token as `Authorization: Bearer`. The first
frame must be `hello`, within 10 seconds; the server answers `welcome` with what it picked from the client's
lists, and never sends anything it did not pick.

<!-- include: examples/chat_live.py#hello -->
```python
def hello(device_id: str, audio_formats: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    """The first frame, within 10 s of connecting: what this client speaks and draws. The server picks from
    these lists only, and never sends what was not picked."""
    device = {
        "deviceId": device_id,  # the token's device: another one is refused (4003)
        "appVersion": "0.1.0",
        "platform": "other",  # ios, watchos, android, web, cli or other
        "tz": "Europe/London",  # optional: where the user is now (also region "GB", hourCycle "h23")
    }
    if audio_formats:  # the voice formats it plays (voice_economy.py); none: Pisa sends no audio
        device["audioFormats"] = list(audio_formats)
    return {
        "op": "hello",
        "contract": {"versions": ["2.0"]},
        "agui": {"protocolVersions": ["1.0"]},
        "a2ui": {"1.0": {"supportedCatalogIds": ["urn:pisa:a2ui:catalog:core:1"]}},  # Ring 1
        "rings": [0, 1],  # add 2 with "mcpApps" to get MCP Apps views
        "extensions": {"pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1, "pisa.voice": 1},
        "device": device,
        **extra,  # "resume": {"sessionId", "lastSeq"} after a dropped connection
    }
```

The `welcome` (`$defs/Welcome`):

```json
{"control": "welcome", "seq": 0, "sessionId": "sess-e222901f-...", "contract": "2.0", "agui": "1.0",
 "a2ui": {"version": "1.0", "catalogIds": ["urn:pisa:a2ui:catalog:core:1"]}, "mcpApps": null,
 "rings": [0, 1], "extensions": {"pisa.voice": 1, "pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1},
 "features": [{"id": "stop", "version": 1}, ...], "languages": ["it-IT", "en-US"], "replayed": false}
```

What the client sends is one JSON object per text frame, each with an `op` (`$defs/ClientFrame`):

| Frame | What |
|---|---|
| `{"op": "hello", ...}` | first, once |
| `{"op": "run", "input": <RunAgentInput>}` | a turn, or the decision on an approval ([Approvals](approvals.md)) |
| `{"op": "cancel", "runId"}` | stop a run (barge-in) |
| `{"op": "a2ui", "message": <A2UI action>}` | a tap on a surface ([Screens](screens.md#taps)) |
| `{"op": "custom", "name": "pisa.*", "value"}` | the client's events: `pisa.presence`, `pisa.location`, `pisa.place_enter`, `pisa.device_context`, `pisa.surface.ack`, `pisa.surface.event`, `pisa.voice.partial` ([Events](events.md#what-the-client-sends)) |
| `{"op": "ping"}` | answered `{"control": "pong", "seq"}` |

What the server sends is either an event frame `{"seq", "threadId", "runId", "event": <AG-UI event>}` or a
control frame `{"control": "welcome" | "pong" | "error", "seq", ...}`. `seq` grows by one per frame, control
frames included. One socket carries one AG-UI stream per `threadId`, each with one run at a time:

| Thread | What runs on it |
|---|---|
| the client's own (`chat-...` here) | the client's turns and Pisa's answers |
| `conversation:<deviceId>` | the server's default conversation thread, used until the client's first run names its own (turn hints for partials sent before any run arrive here) |
| `pisa-notices:<deviceId>` | what Pisa says unasked: one short run per notice, `pisa.ai.disclosure`, `pisa.endpoints` |
| `pisa-approval:<approvalId>` | one approval: its interrupt, then the run that decides it |

<!-- include: examples/chat_live.py#socket -->
```python
class Live:
    """One `/v2/live` connection: subprotocol `pisa.v2`, the session token as a bearer, hello first."""

    def __init__(
        self, server: str, headers: dict[str, str], device_id: str, record: Path | None = None
    ) -> None:
        self.url, self.headers, self.device_id = ws_url(server), headers, device_id
        self.session_id: str | None = None
        self.last_seq = 0  # the last seq seen: a resume replays what came after it
        self.record, self.connections = record, 0
        self.ws: Any = None

    async def connect(self, **hello_extra: Any) -> dict[str, Any]:
        protocols: Any = ["pisa.v2"]  # the subprotocol is required
        self.ws = await websockets.connect(self.url, additional_headers=self.headers, subprotocols=protocols)
        self.connections += 1
        if self.session_id is not None:  # a reconnect: ask for what was missed
            hello_extra.setdefault("resume", {"sessionId": self.session_id, "lastSeq": self.last_seq})
        await self.send(hello(self.device_id, **hello_extra))
        welcome = await self.recv()
        if (
            welcome.get("control") != "welcome"
        ):  # contract_unsupported, auth_failed...: the socket closes next
            raise RuntimeError(f"refused: {welcome}")
        self.session_id = welcome["sessionId"]
        return welcome  # rings, extensions, a2ui and mcpApps: what the server will send; replayed: true/false

    async def send(self, frame: dict[str, Any]) -> None:
        self._keep("client", frame)
        await self.ws.send(json.dumps(frame))

    async def recv(self, timeout: float = 30) -> dict[str, Any]:
        frame = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        self.last_seq = frame.get("seq", self.last_seq)
        self._keep("server", frame)
        return frame

    async def run(self, body: dict[str, Any], screen: Conversation) -> dict[str, Any]:
        """Send a run and draw its frames until it ends; the last event (RUN_FINISHED or RUN_ERROR). Frames of
        other threads (notices, approvals) arrive in between: draw them too."""
        await self.send({"op": "run", "input": body})
        while True:
            frame = await self.recv()
            screen.frame(frame)
            event = frame.get("event") or {}
            if frame.get("runId") == body["runId"] and event.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
                return event

    async def close(self) -> None:
        await self.ws.close()

    def _keep(self, side: str, frame: dict[str, Any]) -> None:
        if self.record is not None:
            path = self.record.parent / f"{self.record.name}-{self.connections}.{side}.jsonl"
            with path.open("a") as out:
                out.write(json.dumps(frame) + "\n")
```

<!-- include: examples/chat_live.py#run -->
```python
screen = Conversation()
ended = await live.run(turn(thread, args.text), screen)
if ended["type"] == "RUN_ERROR" and ended["code"] == "ai_consent_required":
    print("Ask for consent first: consent.py")  # then send the turn again
elif ended["type"] == "RUN_ERROR" and ended["code"] == "model_not_configured":
    print("The server has no model for conversation yet: `pisa models set` on the server.")
```

## Cancel

`{"op": "cancel", "runId"}` (or closing a `POST /v2/runs` stream) ends the run at once with `RUN_FINISHED`
`outcome: {"type": "cancelled"}`; nothing more is sent for it. Keep what was already shown. A new turn while one
is in progress also ends the earlier one `cancelled`: the user moved on.

<!-- include: examples/chat_live.py#cancel -->
```python
body = turn(thread, "Tell me about my week")
await live.send({"op": "run", "input": body})
# barge-in: a stop button, or the user speaks
await live.send({"op": "cancel", "runId": body["runId"]})
while (frame := await live.recv()).get("runId") != body["runId"] or frame["event"]["type"] not in (
    "RUN_FINISHED", "RUN_ERROR",
):  # fmt: skip
    screen.frame(frame)
screen.frame(frame)  # RUN_FINISHED outcome cancelled (or the run's end, if it was already over)
```

## Reconnect and resume

The server keeps the last frames of a session (512 frames, up to 8 MB). After a dropped connection, a `hello`
with `"resume": {"sessionId", "lastSeq"}` gets a `welcome` with `replayed: true` and then every frame after
`lastSeq`, once; `replayed: false` means they are gone: a new session, so refresh what is on screen over REST.
A token that expired meanwhile: sign a new one first ([Pairing](pairing.md#refresh-expiry-revocation)).

<!-- include: examples/chat_live.py#resume -->
```python
await live.close()  # the network dropped
welcome = await live.connect()  # the same session id and the last seq seen
print(f"reconnected: replayed={welcome['replayed']}")  # false: a new session, start fresh
```

One device has one live connection: a second one replaces the first, which gets a control error `replaced` and
close code 4001. Don't reconnect a replaced socket.

## What a real answer looks like

A server with a model answers a turn like this (the contract's recorded stream
`protocol/v2/fixtures/streams/turn_text.jsonl`, with Pisa's voice as three audio chunks):

<!-- include: ../../protocol/v2/fixtures/streams/turn_text.jsonl -->
```json
{"event": {"protocolVersion": "1.0", "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "threadId": "thread-conversation", "timestamp": 1763460000040, "type": "RUN_STARTED"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 1, "threadId": "thread-conversation"}
{"event": {"messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "metadata": {"pisa": {"lang": "it-IT", "priority": "normal"}}, "role": "assistant", "timestamp": 1763460000080, "type": "TEXT_MESSAGE_START"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 2, "threadId": "thread-conversation"}
{"event": {"delta": "Domani a Testville ", "messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "timestamp": 1763460000120, "type": "TEXT_MESSAGE_CONTENT"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 3, "threadId": "thread-conversation"}
{"event": {"delta": "ci sono 14 gradi, ", "messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "timestamp": 1763460000160, "type": "TEXT_MESSAGE_CONTENT"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 4, "threadId": "thread-conversation"}
{"event": {"delta": "con sole al mattino.", "messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "timestamp": 1763460000200, "type": "TEXT_MESSAGE_CONTENT"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 5, "threadId": "thread-conversation"}
{"event": {"messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "timestamp": 1763460000240, "type": "TEXT_MESSAGE_END"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 6, "threadId": "thread-conversation"}
{"event": {"name": "pisa.voice.audio", "timestamp": 1763460000280, "type": "CUSTOM", "value": {"data": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "final": false, "format": "aac_adts", "messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "sampleRate": 24000, "seq": 0}}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 7, "threadId": "thread-conversation"}
{"event": {"name": "pisa.voice.audio", "timestamp": 1763460000320, "type": "CUSTOM", "value": {"data": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB", "final": false, "format": "aac_adts", "messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "sampleRate": 24000, "seq": 1}}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 8, "threadId": "thread-conversation"}
{"event": {"name": "pisa.voice.audio", "timestamp": 1763460000360, "type": "CUSTOM", "value": {"complete": true, "data": "AgICAgICAgICAgICAgICAgICAgICAgIC", "final": true, "format": "aac_adts", "messageId": "msg-c1067ca4-bb89-5dd9-a9b3-611bde498590", "sampleRate": 24000, "seq": 2}}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 9, "threadId": "thread-conversation"}
{"event": {"outcome": {"type": "success"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "threadId": "thread-conversation", "timestamp": 1763460000400, "type": "RUN_FINISHED"}, "runId": "run-9d6ba167-e9ec-5aa3-a93c-7951b40c836a", "seq": 10, "threadId": "thread-conversation"}
```

A slow first turn may start with a holding line, a message whose `metadata.pisa.holding` is true ("Let's
see…"), before the answer. A turn that uses tools adds `STEP_STARTED`/`TOOL_CALL_START`... and maybe a screen
(`protocol/v2/fixtures/streams/turn_places.jsonl`). What to draw for each: [Events](events.md).

On a server with no model chosen yet, every turn ends like this, which is what the examples meet in CI:

```text
data: {"type":"RUN_STARTED","timestamp":1795036197883,"threadId":"curl-chat","runId":"run-curl-1","protocolVersion":"1.0"}

data: {"type":"RUN_ERROR","timestamp":1795036197905,"message":"No model is chosen for the conversation (chat.conversation).","code":"model_not_configured"}
```

Show the message; the server's owner chooses a model (`pisa models set`, [AI providers](../ai-providers.md)).
