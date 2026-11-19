"""Text chat without a socket: `POST /v2/runs`, as a server-sent event stream and as one JSON document.

    python chat_runs.py "What's on today?" [--state FILE]

The request is an AG-UI RunAgentInput (chat_live.turn); the answer is the run's plain AG-UI 1.0 events.
"""

import asyncio
import json
import sys
import uuid
from typing import Any

from chat_live import turn
from pisa_example import PisaError, State, arguments, checked, http_client
from render import Conversation
from session import Session


# [guide:sse]
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


# [/guide:sse]


# [guide:json]
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


# [/guide:json]


async def main() -> int:
    parser = arguments(__doc__ or "")
    parser.add_argument("text", nargs="?", default="What's on today?")
    args = parser.parse_args()
    state = State.load(args.state)
    thread = f"chat-{uuid.uuid4()}"
    failures = 0
    async with http_client(state) as http:
        session = Session(http, state)
        screen = Conversation()
        for name, way in (("SSE", run_streamed), ("JSON", run_collected)):
            last = await way(session, turn(thread, args.text), screen)
            print(f"{name}: the run ended {last and last['type']} {last and last.get('code', '')}")
            failures += not last or last["type"] not in ("RUN_FINISHED", "RUN_ERROR")
        # [guide:a2ui-over-http]
        # A tap on a surface, without a socket: the A2UI action (screens.tap_action) rides
        # forwardedProps.pisa.a2uiAction, on the conversation's thread, with no messages and no resume.
        action = {"version": "1.0", "action": {"name": "pisa.choose", "surfaceId": "places:demo-1",
                  "sourceComponentId": "c2", "timestamp": "2026-11-18T10:00:00Z", "context": {"optionId": "o1"}}}  # fmt: skip
        tap = {"threadId": thread, "runId": f"run-{uuid.uuid4()}", "messages": [],
               "forwardedProps": {"pisa": {"a2uiAction": action}}}  # fmt: skip
        # [/guide:a2ui-over-http]
        last = await run_collected(session, tap, screen)
        print(f"a2uiAction over HTTP: the run ended {last and last['type']} {last and last.get('code', '')}")
        failures += not last or last["type"] not in ("RUN_FINISHED", "RUN_ERROR")
        try:  # a surface id the server can't map to anything it showed is refused before any run
            await run_collected(session, tap | {"runId": f"run-{uuid.uuid4()}", "forwardedProps": {"pisa": {
                "a2uiAction": action | {"action": action["action"] | {"surfaceId": "nothing-here"}}}}}, screen)  # fmt: skip
            failures += 1
        except PisaError as err:
            print(f"a2uiAction on an unknown surface: HTTP {err.status} {err.code}")
            failures += (err.status, err.code) != (400, "validation_failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
