"""AG-UI's HTTP binding, `POST /v2/runs` (CONTRACT-2 §3.2): a run without the socket.

The request is an AG-UI `RunAgentInput`; the answer is the run's plain AG-UI 1.0 events, as SSE `data:` lines
(`Accept: text/event-stream`), or collected into one document (`{events}`, the same body as
`POST /v1/approvals/{id}/resume`) within JSON_WAIT_S (agui), else 202 with the run's id.

One pipeline: the run is taken by a device session (`services.session.DeviceSession`) whose frames go to this
request instead of a socket, so a turn, an "ask about this", an A2UI action
(`forwardedProps.pisa.a2uiAction`) or an approval's resume gives exactly the events the socket gives. It is no
device's live session: what Pisa says unasked (notices, an approval's interrupt) never comes here; a headless
client reads those from `GET /v1/changes`, or opens `/v2/live`. A surface shown here waits for no
acknowledgment (HTTP has none). After a 202 the run goes on to its end; its words stay in the conversation.

For its length the run is held in the device registry, beside the device's socket (replacing neither): the
device revoked, the run ends as the socket does, its last frame `device_revoked` (an SSE `data:` line; in JSON
mode the 401 `device_revoked`) and its reply cancelled, the model call too. The session token's hour over, it
ends the same way, without that frame (`session_expired` in JSON mode).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Receive, Scope, Send

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.live import SESSION_EXPIRED, expire_session
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError
from pisa.logging import get_logger
from pisa.services import a2ui
from pisa.services import devices as devices_service
from pisa.services.agui import JSON_WAIT_S, LiveSession
from pisa.services.background import Background
from pisa.services.devices import Device
from pisa.services.runtime import Runtime
from pisa.services.session import DeviceSession, FrameError, Negotiated
from pisa.services.session_registry import SessionRegistry

_log = get_logger("pisa.runs")
_background = Background()
router = APIRouter(prefix="/v2", tags=["live"], responses=ERROR_RESPONSES)

# what a run over HTTP draws: Rings 0 and 1 in the core catalog. No hello negotiated an extension over HTTP:
# the run's `pisa.*` events all go, as on a socket that picked them (an AG-UI client ignores what it
# can't draw).
HTTP_NEGOTIATED = Negotiated(catalogs=[a2ui.CATALOG_ID], rings=[0, 1], mcp_apps=False, extensions=None)


class RunAgentInput(BaseModel):
    """AG-UI 1.0 `RunAgentInput`. `forwardedProps.pisa` follows `$defs/TurnProps`."""

    model_config = ConfigDict(extra="forbid")

    threadId: str = Field(min_length=1, max_length=120)
    runId: str = Field(min_length=1, max_length=80)
    messages: list[dict[str, Any]]
    protocolVersion: str | None = None
    parentRunId: str | None = None
    state: Any = None
    tools: list[dict[str, Any]] | None = None
    context: list[dict[str, Any]] | None = None
    forwardedProps: dict[str, Any] | None = None
    resume: list[dict[str, Any]] | None = None


class _Last:
    """The run's last frame, put on its queue by `_Collected.last` (revoked): sent alone, then the end."""

    __slots__ = ("frame",)

    def __init__(self, text: str) -> None:
        self.frame: dict[str, Any] = json.loads(text)


class _Collected:
    """A run's transport over HTTP: its frames go to the request (the queue attached to its `LiveSession`);
    there is no acknowledgment of a surface to wait for. The run is held in the registry while it lasts
    (`SessionRegistry.register_run`); revoked, its last frame goes alone and the queue ends (None)."""

    acknowledges = False

    def __init__(self, live: LiveSession, registry: SessionRegistry) -> None:
        self.frames: asyncio.Queue[str | _Last | None] = asyncio.Queue()
        self.live = live
        self.registry = registry
        self.ended: str | None = None  # why the run was ended from outside: device_revoked, session_expired
        self.done = False  # the stream sent the run's end (or its last frame)
        self.expiry: asyncio.Task[None] | None = None
        live.attach(self.frames.put_nowait)

    def last(self, text: str) -> None:
        self.live.detach()  # nothing of the run's after it
        last = _Last(text)
        self.ended = str(last.frame.get("code") or "")
        self.frames.put_nowait(last)

    async def close(self, code: int, reason: str) -> None:
        self.live.detach()
        if self.ended is None:
            self.ended = reason
        self.frames.put_nowait(None)  # the stream ends; the session cancels the reply

    def refusal(self) -> PisaError:
        """JSON mode: the answer for a run ended from outside."""
        if self.ended == SESSION_EXPIRED:
            return PisaError(
                "session_expired",
                "The session token expired (they last an hour).",
                "Get a new nonce, sign it with the device key and send POST /v1/devices/session",
            )
        return PisaError("device_revoked", "This device was revoked on the server.", "Pair this device again")

    def finish(self, session: DeviceSession) -> None:
        """The run is over (done, failed, cancelled, the client gone): out of the registry. Idempotent."""
        if self.expiry is not None:
            self.expiry.cancel()
        session.closed = True
        self.live.detach()
        self.registry.unregister_run(session)


def _ended(frame: dict[str, Any], run_id: str) -> bool:
    event = frame.get("event") or {}
    return frame.get("runId") == run_id and event.get("type") in ("RUN_FINISHED", "RUN_ERROR")


async def _start(
    body: RunAgentInput, device: Device, runtime: Runtime, registry: SessionRegistry
) -> tuple[DeviceSession, _Collected]:
    """The run started in a session of its own, held in the registry; its frames arrive on the transport's
    queue. 400 for an input it can't take."""
    live = LiveSession(str(device.id), clock_ms=lambda: int(runtime.clock().timestamp() * 1000))
    run = _Collected(live, registry)
    session = DeviceSession(run, device, runtime, live, HTTP_NEGOTIATED)
    registry.register_run(session)
    pisa = (body.forwardedProps or {}).get("pisa")
    action = pisa.get("a2uiAction") if isinstance(pisa, dict) else None
    try:
        async with runtime.pool.connection() as conn:  # revoked since its token was checked: no run
            if (await devices_service.revoked(conn, [device.id])).get(device.id, True):
                raise run.refusal()
        if device.session_expires_at is not None:
            run.expiry = asyncio.create_task(expire_session(session, device.session_expires_at))
        if isinstance(action, dict):
            if body.resume or body.threadId.startswith("pisa-"):
                raise FrameError("An A2UI action runs on the conversation's thread, without resume.")
            message = action if "version" in action else {"version": "1.0", "action": action}
            await session.action(message, run_id=body.runId, thread=body.threadId)
        else:
            await session.run(body.model_dump(exclude_none=True))
    except FrameError as err:
        session.cancel_all()
        run.finish(session)
        raise PisaError("validation_failed", str(err), "Fix the run's input") from None
    except BaseException:
        session.cancel_all()
        run.finish(session)
        raise
    return session, run


async def _frames(run: _Collected, run_id: str) -> AsyncIterator[dict[str, Any]]:
    """The run's frames, in order, up to its end; or up to its last frame (`{"last": frame}`, revoked), or
    to the queue's end (the session closed)."""
    while True:
        item = await run.frames.get()
        if item is None:
            return
        if isinstance(item, _Last):
            yield {"last": item.frame}
            return
        frame = json.loads(item)
        if frame.get("runId") != run_id or "event" not in frame:
            continue  # another run's (a reply it superseded) or a control frame: not this run's
        yield frame
        if _ended(frame, run_id):
            return


@router.post(
    "/runs",
    response_model=schemas.CollectedRun,
    responses={
        200: {
            "description": "text/event-stream: SSE `data:` lines of plain AG-UI 1.0 events. "
            "application/json: the run collected within 8 s.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        202: {"model": schemas.RunAccepted, "description": "JSON mode: the run is still going after 8 s"},
    },
)
async def run_agent(body: RunAgentInput, request: Request, device: DeviceDep, runtime: RuntimeDep) -> Any:
    """Run the agent (AG-UI's HTTP binding, CONTRACT-2 §3.2): the same run as `op: run` on `/v2/live` (a
    turn, an "ask about this", a resume of an approval's interrupt) or, with `forwardedProps.pisa.a2uiAction`,
    `op: a2ui`. `Accept: text/event-stream` streams it (closing the request cancels it); otherwise 200 with
    `{events}` within 8 s, or 202 with `runId`. The bearer is a session token (`POST /v1/devices/session`); a
    turn needs the user's AI consent, or it ends with `RUN_ERROR ai_consent_required` (CONTRACT-2 §4.4)."""
    registry: SessionRegistry = request.app.state.sessions
    session, run = await _start(body, device, runtime, registry)
    if "text/event-stream" in request.headers.get("accept", ""):
        return _RunStream(session, run, body.runId)
    events: list[dict[str, Any]] = []

    async def collect() -> None:
        async for frame in _frames(run, body.runId):
            if "event" in frame:
                events.append(frame["event"])

    task = asyncio.create_task(collect())
    task.add_done_callback(lambda _: run.finish(session))  # on every path: out of the registry
    try:
        done, _ = await asyncio.wait({task}, timeout=JSON_WAIT_S)
    except BaseException:  # the request went away: the run goes on to its end, as after a 202
        _background.keep(task)
        raise
    if not done:  # it goes on to its end; its words stay in the conversation
        _background.keep(task)
        return JSONResponse({"runId": body.runId}, status_code=202)
    run.finish(session)
    if run.ended is not None:
        raise run.refusal()
    return {"events": events}


def _data(body: dict[str, Any]) -> str:
    return "data: " + json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n\n"


async def _sse(run: _Collected, run_id: str) -> AsyncIterator[str]:
    async for frame in _frames(run, run_id):
        if "last" in frame:  # revoked: `device_revoked`, the same frame the socket gets, and nothing after
            run.done = True
            yield _data(frame["last"])
            return
        run.done = _ended(frame, run_id)
        yield _data(frame["event"])
    run.done = run.done or run.ended is not None


class _RunStream(StreamingResponse):
    """The run as SSE. However the response ends (the run's end, revoked, the client gone, the stream never
    started), the run leaves the registry, and is cancelled unless it ended."""

    def __init__(self, session: DeviceSession, run: _Collected, run_id: str) -> None:
        super().__init__(_sse(run, run_id), media_type="text/event-stream")
        self._session, self._run = session, run

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._run.done:  # the client went away: the run is cancelled, as a barge-in
                _log.info("run_stream_closed", device_id=str(self._session.device.id))
                self._session.cancel_all()
            self._run.finish(self._session)
