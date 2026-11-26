"""What a client draws for every event the server sends: a terminal stand-in for a conversation screen.

    python render.py recorded.jsonl [more.jsonl ...]

Each line is a `/v2/live` frame (`{"seq", "threadId", "runId", "event"}` or a control frame) or a plain AG-UI
event (what `POST /v2/runs` streams). `chat_live.py` and `chat_runs.py` draw through `Conversation`; run on the
contract's fixtures it shows every event type once.
"""

import json
import sys
from collections.abc import Callable
from typing import Any

from screens import Screens

Out = Callable[[str], None]


# [guide:conversation]
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
        elif name == "pisa.app.open":
            # contract 2.2 (pisa.app): open the app's own screen, then answer at once with pisa.app.result
            self.out(f"[open {v['target']} {v.get('id', '')}] -> pisa.app.result {{ok: true}}")
        elif name == "pisa.app.changed":
            self.out(f"[read again: {', '.join(v['views'])}]")  # GET /v1/briefings/latest for the riepilogo
        elif name in ("pisa.maps.search", "pisa.maps.eta"):
            # contract 2.3 (pisa.maps): ask the phone's own maps, then answer pisa.maps.result (maps.md);
            # never log a coordinate
            what = f"search {v['query']!r}" if name == "pisa.maps.search" else f"eta by {v['mode']}"
            self.out(f"[maps {what}] -> pisa.maps.result {{requestId: {v['requestId'][:8]}…}}")
        elif name in ("pisa.notify.schedule", "pisa.notify.cancel", "pisa.notify.regions"):
            # contract 2.3 (pisa.notify): apply it on the phone, then answer pisa.notify.ack (notify.md)
            count = len(v.get("items") or v.get("ids") or v.get("regions") or [])
            self.out(f"[{name.rsplit('.', 1)[1]} {count}, revision {v['revision']}] -> pisa.notify.ack")

    # [/guide:conversation]

    def on_messages_snapshot(self, e: dict[str, Any], thread: str) -> None:
        pass  # this server does not send it; a client may redraw its history from it

    def on_state_snapshot(self, e: dict[str, Any], thread: str) -> None:
        pass  # this server does not send shared state today

    def on_state_delta(self, e: dict[str, Any], thread: str) -> None:
        pass


def read(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def main(paths: list[str]) -> int:
    for path in paths:
        print(f"== {path}")
        screen = Conversation()
        for item in read(path):
            if "event" in item or "control" in item:
                screen.frame(item)
            else:
                screen.event(item)
        for key, chunks in screen.audio.items():
            print(f"[audio for {key}: {len(chunks)} chunks]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
