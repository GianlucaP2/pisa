# Events: what the client receives, and what it draws

Every server event is a plain AG-UI 1.0 event that validates against the official schema on its own
(`protocol/v2/vendor/ag-ui-1.0/schema.json`); Pisa's own events are AG-UI `CUSTOM` events named `pisa.*`. On
`/v2/live` and a talk's data channel each one is wrapped in a frame `{"seq", "threadId", "runId", "event"}`; over
`POST /v2/runs` it comes bare.

**Ignore what you don't know.** A newer server may send an event type or a `pisa.*` name your client has never
seen: skip it, never fail. Within a run, the events obey AG-UI's lifecycle: `RUN_STARTED` first, one of
`RUN_FINISHED` or `RUN_ERROR` last, each text message and step opened before it is used and closed before the
run ends.

Example: [`render.py`](examples/render.py) draws every event (as text), and runs over every fixture stream in CI.

<!-- include: examples/render.py#conversation -->
```python
class Conversation:
    """One conversation screen. Each AG-UI event type has one handler; an unknown one is ignored, never an error
    (AG-UI and contract 2 grow by adding events)."""

    def __init__(self, out: Out = print) -> None:
        self.out = out
        self.text: dict[str, str] = {}  # messageId -> its words so far (one bubble each)
        self.screens = Screens(out)  # Ring 1 surfaces and Ring 2 views, by surface id
        self.interrupts: dict[str, dict[str, Any]] = {}  # approvals waiting for the user, by approval id
        self.audio: dict[str, list[bytes]] = {}  # pisa.voice.audio chunks, by messageId or noticeId

    def frame(self, frame: dict[str, Any]) -> None:
        if "control" in frame:  # welcome, pong, error: the socket's own frames, outside any run
            if frame["control"] == "error":
                self.out(f"[socket error] {frame['code']}: {frame['message']}")
            return
        self.event(frame["event"], frame.get("threadId", ""))

    def event(self, event: dict[str, Any], thread: str = "") -> None:
        handler = getattr(self, "on_" + str(event.get("type", "")).lower(), None)
        if handler is not None:
            handler(event, thread)

    # --- a run: one turn of Pisa's, from start to end -------------------------------------------------------
    def on_run_started(self, e: dict[str, Any], thread: str) -> None:
        if thread.startswith("conversation") or not thread.startswith("pisa-"):
            self.out("[thinking]")  # a typing indicator until the first words or RUN_FINISHED

    def on_run_finished(self, e: dict[str, Any], thread: str) -> None:
        outcome = (e.get("outcome") or {}).get("type", "success")
        if outcome == "interrupt":  # an approval: Ring 0, the app's own sheet (approvals.py)
            for interrupt in e["outcome"]["interrupts"]:
                approval = ((interrupt.get("metadata") or {}).get("pisa") or {}).get("approvalId")
                if interrupt.get("reason") == "pisa.approval" and approval:
                    self.interrupts[approval] = interrupt
                self.out(f"[approval] {interrupt.get('message', '')} (until {interrupt.get('expiresAt')})")
        elif outcome == "cancelled":
            self.out("[stopped]")  # keep what was said; nothing more comes for this run
        if isinstance(e.get("result"), dict) and "talkEnded" in e["result"]:
            self.out(f"[talk ended: {e['result']['talkEnded']}]")

    def on_run_error(self, e: dict[str, Any], thread: str) -> None:
        self.out(f"[error {e.get('code')}] {e['message']}")  # see errors.md for what each code asks of you

    # --- Pisa's words ------------------------------------------------------------------------------------
    def on_text_message_start(self, e: dict[str, Any], thread: str) -> None:
        self.text[e["messageId"]] = ""
        meta = (e.get("metadata") or {}).get("pisa") or {}
        if meta.get(
            "holding"
        ):  # "Let's see…": a holding line while a slow turn works; replaced by the answer
            self.out("[holding line]")

    def on_text_message_content(self, e: dict[str, Any], thread: str) -> None:
        # append, and redraw the bubble
        self.text[e["messageId"]] = self.text.get(e["messageId"], "") + e["delta"]

    def on_text_message_end(self, e: dict[str, Any], thread: str) -> None:
        self.out(f"Pisa: {self.text.get(e['messageId'], '')}")

    def on_text_message_chunk(self, e: dict[str, Any], thread: str) -> None:  # start+content+end in one
        if e.get("delta"):
            self.out(f"Pisa: {e['delta']}")

    # --- what Pisa does while it answers -------------------------------------------------------------------
    def on_step_started(self, e: dict[str, Any], thread: str) -> None:
        self.out(f"[working: {e['stepName']}]")  # a quiet progress line ("Looking up places…")

    def on_step_finished(self, e: dict[str, Any], thread: str) -> None:
        pass  # clear the progress line

    def on_tool_call_start(self, e: dict[str, Any], thread: str) -> None:
        pass  # the server's own tool (same name as the step): nothing for the client to run

    # --- screens: Ring 1 (A2UI) and Ring 2 (MCP Apps) -------------------------------------------------------
    def on_activity_snapshot(self, e: dict[str, Any], thread: str) -> None:
        self.screens.snapshot(e["messageId"], e["activityType"], e["content"])

    def on_activity_delta(self, e: dict[str, Any], thread: str) -> None:
        self.screens.delta(e["messageId"], e["activityType"], e["patch"])

    # --- pisa.* extensions -------------------------------------------------------------------------------
    def on_custom(self, e: dict[str, Any], thread: str) -> None:
        name, v = e["name"], e["value"]
        if name == "pisa.notice":
            undo = " [Undo]" if v.get("undo") else ""
            choices = " ".join(f"[{c['label']}]" for c in (v.get("checkIn") or {}).get("choices", []))
            self.out(f"[notice {v['kind']}/{v['priority']}] {v.get('title', '')} {v['text']}{undo} {choices}")
        elif name == "pisa.voice.audio":
            key = v.get("messageId") or v.get("noticeId")
            # see voice_economy.py: decode, and play in seq order
            self.audio.setdefault(key, []).append(v["data"])
        elif name == "pisa.voice.state":
            self.out(f"[voice {v['state']}]")  # listening, thinking, speaking, idle: the orb
        elif name == "pisa.voice.gate":
            pass  # owner, other, none: a subtle hint on the orb, at most twice a second
        elif name == "pisa.voice.transcript":
            if v.get("ignored"):
                self.out(f"[heard, ignored: {v['ignored']}]")  # clear the live words and keep listening
            else:
                self.out(f"You{'' if v['final'] else ' (…)'}: {v['text']}")
        elif name == "pisa.voice.turn_hint":
            self.out(f"[turn hint: end {v['pEnd']:.0%}, wait {v['waitMs']} ms]")  # voice_economy.py
        elif name == "pisa.voice.understood":
            if v["meant"] != v["raw"]:
                self.out(f"[understood: {v['meant']!r} (heard {v['raw']!r})]")
        elif name == "pisa.ask_to_open":
            self.out(f"[asks to talk: {v['line']}] [Talk] [Not now]")
        elif name == "pisa.directions":
            self.out(f"[directions to {v['name']} by {v['mode']}] [Open maps]")
        elif name == "pisa.call":
            self.out(f"[call {v['name']} {v['phone']}?] [Call]")  # only after the user's tap
        elif name == "pisa.compose":
            # the user sends it
            self.out(f"[message to {v['recipient']['name']}: {v['text']}] [Open {v['app']}]")
        elif name == "pisa.action_done":
            self.out(f"[done] {v['summary']}: {v['result']}")
        elif name == "pisa.approval.state":
            self.interrupts.pop(v["approvalId"], None)  # decided elsewhere: drop it, never resume it
            self.out(f"[approval {v['state']} by {v.get('by', 'someone else')}]")
        elif name == "pisa.ai.disclosure":
            self.out("[the AI disclosure changed: ask the user again (consent.py)]")
        elif name == "pisa.endpoints":
            self.out(f"[server addresses or TLS pins changed: store revision {v['revision']}]")
        elif name == "pisa.surface.focus":
            self.screens.focus(v["surfaceId"], v["mode"])
        elif name == "pisa.entitlement.required":
            self.out(f"[{v['feature']} needs {v['level']}]")
```

## AG-UI events

"Sent" says whether this server sends the event today; a client must accept every one of them anyway.

| Event | Sent | What to draw |
|---|---|---|
| `RUN_STARTED` | yes | a typing or thinking indicator on the thread's screen |
| `RUN_FINISHED`, `outcome.type: success` | yes | remove the indicator |
| `RUN_FINISHED`, `outcome.type: cancelled` | yes | stop drawing and speaking for this run; keep what was shown |
| `RUN_FINISHED`, `outcome.type: interrupt` | yes | on an approval's thread: the approval sheet ([Approvals](approvals.md)) |
| `RUN_FINISHED`, `result.talkEnded` | yes | on a talk's channel: the talk is over ([Audio](audio.md#the-data-channel)) |
| `RUN_ERROR {code, message}` | yes | the message as Pisa's words, and what the code asks of you ([Errors](errors.md)) |
| `TEXT_MESSAGE_START {messageId, role, metadata.pisa}` | yes | a new bubble. `metadata.pisa`: `lang`, `priority` (`low`, `normal`, `urgent`), `holding: true` for a holding line ("Let's see…") that the answer follows (a message of its own, with its own id: never part of the user's bubble or the answer's), `urgency` (`now`: interrupt; `breakpoint`: at the next pause) |
| `TEXT_MESSAGE_CONTENT {messageId, delta}` | yes | append to the bubble (stream it) |
| `TEXT_MESSAGE_END {messageId}` | yes | the bubble is complete (speak it now, if the client speaks text itself) |
| `STEP_STARTED {stepName}`, `STEP_FINISHED` | yes | a quiet progress line while a tool works (`search_places`...), cleared when it ends |
| `TOOL_CALL_START {toolCallId, toolCallName}`, `TOOL_CALL_END` | yes | nothing to run: the server's own tool, the same as the step |
| `ACTIVITY_SNAPSHOT {messageId, activityType, content}` | yes | a screen: `a2ui-surface` (Ring 1) or `mcp-apps` (Ring 2) ([Screens](screens.md)) |
| `ACTIVITY_DELTA {messageId, activityType, patch}` | in the contract | a JSON Patch on that screen's content ([Screens](screens.md#updates)) |
| `CUSTOM {name, value}` | yes | the `pisa.*` events below |
| `TOOL_CALL_ARGS`, `TOOL_CALL_RESULT`, `TOOL_CALL_CHUNK` | no | nothing |
| `TEXT_MESSAGE_CHUNK` | no | a whole message in one event: a bubble |
| `STATE_SNAPSHOT`, `STATE_DELTA`, `MESSAGES_SNAPSHOT` | no | nothing (no shared state today; the history is read over REST) |
| `REASONING_*`, `SUBAGENT_*`, `RAW` | no | nothing |

The user's own message is the client's: it draws the bubble when it sends the turn. The server never sends
it back (no user `TEXT_MESSAGE_*`, no `MESSAGES_SNAPSHOT`); `pisa.voice.transcript` is only a talk's live
words.

## `pisa.*` events the server sends

List in the `hello` every extension you handle (`pisa.notice`, `pisa.voice`, `pisa.approval`,
`pisa.surface`, `pisa.directions`, `pisa.call`, `pisa.compose`, `pisa.presence`, `pisa.location`). The server
never sends an event whose extension the `welcome` did not grant: `pisa.voice.*` and `pisa.ask_to_open` need
`pisa.voice` (`pisa.voice.turn_hint` needs `pisa.voice.turn_hints`), `pisa.action_done` and
`pisa.approval.state` need `pisa.approval`, `pisa.surface.focus` needs `pisa.surface`, and the others their
own. What the user must not lose comes as Pisa's words instead: without `pisa.notice` a notice is a text
message on the notices thread, without `pisa.approval` an approved action's outcome is one on its approval's
thread, and without `pisa.directions`, `pisa.call` or `pisa.compose` Pisa says the place, the number or the
message in its answer. Without `pisa.voice` no voice comes back: the client speaks the text itself.
`pisa.ai.disclosure`, `pisa.endpoints` and `pisa.entitlement.required` belong to no extension: always sent.
Over `POST /v2/runs` nothing is negotiated, and a run's events all come. Field lists: `$defs/s_pisa_*` in
the schema.

| Name | Value | What to draw or do |
|---|---|---|
| `pisa.notice` | `{noticeId, kind, priority, lang, text, title?, urgency?, voice?, sensitive?, checkIn?, undo?, place?, refs?, surfaceId?, privacy?, genericText?, wrist?}` | a notification card. `kind`: `reminder`, `briefing`, `replan`, `alert`, `info`, `check_in`, `done`, `turn_answer`, `weekly`, `ledger_week`. `checkIn.choices`: buttons, answered with `POST /v1/check-ins/{checkInId}/answer {choice}`, whatever the `kind`: an offer from Pisa rides a check-in too (an `info` notice whose one choice, `conosciamoci`, starts the first talk; its answer is a `confirm` check-in about no task or event, with no plan or options), and no notice carries choices outside `checkIn`. `undo`: an Undo button until `until`. `privacy` health or finance: show `genericText` on a lock screen. `voice` false: show it, don't say it |
| `pisa.voice.audio` | audio chunks | play them ([Audio](audio.md#pisas-voice-back-pisavoiceaudio)) |
| `pisa.voice.state` | `{state}` | the voice orb: `listening`, `thinking`, `speaking`, `idle` |
| `pisa.voice.gate` | `{heard}` | a subtle hint: `owner`, `other`, `none` |
| `pisa.voice.transcript` | `{text, final, ignored?}` | the user's words, live; a final without `ignored` becomes their message |
| `pisa.voice.turn_hint` | `{replyTo, pEnd, waitMs}` | nothing to draw: when to end the utterance ([Audio](audio.md#while-the-user-speaks-partials-and-turn-hints)) |
| `pisa.voice.understood` | `{replyTo, meant, raw, corrections, ask}` | under the user's bubble, what Pisa understood when it differs from what was heard (spoken turns only; never a second bubble) |
| `pisa.ask_to_open` | `{why, line, lang, noticeId?}` | Pisa asks to start a voice talk: "Talk" opens one with `trigger: proactive` |
| `pisa.directions` | `{directionsId, name, lat, lon, mode, placeId?}` | open the maps app with directions (`walk`, `transit`...) |
| `pisa.call` | `{callId, name, phone}` | a Call button: dial only after the user's tap |
| `pisa.compose` | `{composeId, app, recipient: {name, phone?}, text}` | open a prepared message; the user sends it |
| `pisa.action_done` | `{approvalId, kind, summary, result, undoUntil?}` | an approved action ran: a confirmation, with Undo until `undoUntil` |
| `pisa.approval.state` | `{approvalId, interruptId, state, by?}` | an approval was decided elsewhere or expired: drop its sheet and never resume it |
| `pisa.ai.disclosure` | the AI disclosure | the providers changed: ask for consent again ([Text chat](text-chat.md#consent-first)) |
| `pisa.endpoints` | `{revision, endpoints, spki}` | store the server's new addresses and pins ([Pairing](pairing.md#the-pin)) |
| `pisa.surface.focus` | `{surfaceId, mode, highlight?}` | bring a screen already shown back in front |
| `pisa.entitlement.required` | `{feature, level}` | the feature needs an entitlement the client did not report |

## What the client sends

`{"op": "custom", "name", "value"}` frames on `/v2/live` (`$defs/c_pisa_*`):

| Name | Value | When |
|---|---|---|
| `pisa.presence` | `{event, state?: {foreground, locked, route, glassesWorn, companion}}` | right after `welcome` and on each change; `event` is `app_foreground`, `app_background`, `earbuds_connected`, `earbuds_disconnected`, `glasses_on`, `glasses_off`, `companion_on`, `companion_off` |
| `pisa.location` | `{lat, lon, accuracyM, measuredAt}` | when the user allows it: weather, travel times, places |
| `pisa.place_enter` | `{noteIds}` | the user arrived where notes are pinned |
| `pisa.device_context` | `{tz?, region?, hourCycle?}` | the device's zone or region changed |
| `pisa.surface.ack` | `{surfaceId, status, surface, reason?}` | at once for every screen ([Screens](screens.md#taps)) |
| `pisa.surface.event` | `{surfaceId, event}` | `seen`, `expanded`, `kept`, `closed`: ids only |
| `pisa.voice.partial` | `{text, lang, turnId}` | partial transcripts ([Audio](audio.md)) |

Locations sent while the socket was down go up with `POST /v1/devices/me/locations`.

## Every event, once

The contract's fixtures hold every AG-UI event type (`protocol/v2/fixtures/events/agui_all_types.jsonl`) and
every `pisa.*` server event (`pisa_server_events.jsonl`). Run your renderer over them; `render.py` draws:

```text
[voice speaking]
You (…): Hello there
[turn hint: end 80%, wait 300 ms]
[understood: 'the station' (heard 'the nation')]
[notice done/normal]  Done: parcel [Undo]
[asks to talk: Time to go.] [Talk] [Not now]
[directions to Example Station by walk] [Open maps]
[call Example Clinic +15550100100?] [Call]
[message to Sam: On my way] [Open messages]
[done] Email sent: Sent to 1 person
[approval approved by voice]
[the AI disclosure changed: ask the user again (consent.py)]
[server addresses or TLS pins changed: store revision r2]
[bring places:demo-1 back to the front (take)]
[push.relay needs pisa.pro]
```
