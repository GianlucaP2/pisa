"""Text chat over the live socket, `/v2/live`: hello, a turn, its streamed events, a cancel, and a resume.

    python chat_live.py "What's on today?" [--state FILE] [--record frames]

`--record frames` writes every frame both ways, one pair of files per connection (`frames-1.server.jsonl`,
`frames-1.client.jsonl`, ...), for the conformance kit: `pisa-conformance stream frames-1.server.jsonl` and
`pisa-conformance stream --client frames-1.client.jsonl`.
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import websockets
from pisa_example import State, arguments, http_client, ws_url
from render import Conversation
from session import Session


# [guide:hello]
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


# [/guide:hello]


# [guide:turn]
def turn(thread_id: str, text: str, lang: str = "en-US") -> dict[str, Any]:
    """An AG-UI RunAgentInput: one user message. `forwardedProps.pisa.turn` says how it was said."""
    message_id = str(uuid.uuid4())
    return {
        "threadId": thread_id,  # yours: one per conversation; never one starting with "pisa-"
        "runId": f"run-{uuid.uuid4()}",  # yours, new for every run
        "messages": [{"id": message_id, "role": "user", "content": text}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": lang}}},
    }


# [/guide:turn]


# [guide:socket]
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


# [/guide:socket]


async def main() -> int:
    parser = arguments(__doc__ or "")
    parser.add_argument("text", nargs="?", default="What's on today?")
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    state = State.load(args.state)
    thread = f"chat-{uuid.uuid4()}"
    failures = 0
    async with http_client(state) as http:
        session = Session(http, state)
        live = Live(state.server, await session.bearer(), state.device_id, args.record)
        welcome = await live.connect()
        print(
            f"welcome: contract {welcome['contract']}, rings {welcome['rings']}, session {welcome['sessionId']}"
        )
        await live.send({"op": "ping"})
        print(f"ping -> {(await live.recv())['control']}")

        # [guide:run]
        screen = Conversation()
        ended = await live.run(turn(thread, args.text), screen)
        if ended["type"] == "RUN_ERROR" and ended["code"] == "ai_consent_required":
            print("Ask for consent first: consent.py")  # then send the turn again
        elif ended["type"] == "RUN_ERROR" and ended["code"] == "model_not_configured":
            print("The server has no model for conversation yet: `pisa models set` on the server.")
        # [/guide:run]
        failures += ended["type"] not in ("RUN_FINISHED", "RUN_ERROR")

        # [guide:cancel]
        body = turn(thread, "Tell me about my week")
        await live.send({"op": "run", "input": body})
        # barge-in: a stop button, or the user speaks
        await live.send({"op": "cancel", "runId": body["runId"]})
        while (frame := await live.recv()).get("runId") != body["runId"] or frame["event"]["type"] not in (
            "RUN_FINISHED", "RUN_ERROR",
        ):  # fmt: skip
            screen.frame(frame)
        screen.frame(frame)  # RUN_FINISHED outcome cancelled (or the run's end, if it was already over)
        # [/guide:cancel]
        failures += frame["event"]["type"] not in ("RUN_FINISHED", "RUN_ERROR")

        # [guide:resume]
        await live.close()  # the network dropped
        welcome = await live.connect()  # the same session id and the last seq seen
        print(f"reconnected: replayed={welcome['replayed']}")  # false: a new session, start fresh
        # [/guide:resume]
        failures += welcome["replayed"] is not True
        await live.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
