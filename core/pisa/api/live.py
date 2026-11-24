"""Contract 2's live socket, `GET /v2/live` (CONTRACT-2 §3, §4.2, §5, §7, §8): the transport of a device
session (`services.session.DeviceSession`).

The socket speaks AG-UI 1.0 events framed as `{"seq", "threadId", "runId"?, "event"}`, `pisa.*` extension
events, and control frames; the app speaks `op` frames (`hello`, `run`, `cancel`, `a2ui`, `custom`, `ping`).
Here: the handshake, the socket's writer and reader, and what an app frame Pisa can't take is on the wire
(`invalid_frame`, `internal`). What a frame does, and every frame Pisa sends, is the session's.

Handshake: the session token (CONTRACT-2 §4.3) is checked on connect, then
the first frame must be `hello` within HELLO_TIMEOUT_S. The server answers `welcome` with only what the app
listed. A `hello` that resumes a session (`resume {sessionId, lastSeq}`) gets every frame after `lastSeq` once
(`replayed: true`), or `replayed: false` and a new session when they are no longer kept.

The privacy revision (CONTRACT-2 §3.4, §4.3-§4.5): a session ends with its token, an hour after it was made
(close 4003, reason `session_expired`: the app signs a new nonce and reconnects); a revoked device is told
`device_revoked`; a turn's run needs the user's AI consent (the session checks it before any AI call); and a
change of the AI disclosure or of the server's endpoints since the device was last told reaches it on connect
(the session's `catch_up`).
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.websockets import WebSocketDisconnect

from pisa.api.server import FEATURES
from pisa.db.errors import UNREACHABLE, database_unreachable
from pisa.degrade import degrades, fails_closed
from pisa.domain import protocol_versions as versions
from pisa.domain.errors import PisaError
from pisa.logging import get_logger
from pisa.services import devices as devices_service
from pisa.services import presence
from pisa.services.agui import LiveSession
from pisa.services.background import Background
from pisa.services.devices import authenticate, bearer_token
from pisa.services.runtime import Runtime
from pisa.services.session import (
    CLOSE_AUTH_FAILED,
    LANGUAGES,
    DeviceSession,
    FrameError,
    Negotiated,
    control_error,
    failure,
)
from pisa.services.session_registry import SessionRegistry

_log = get_logger("pisa.live")
_background = Background()
router = APIRouter()

SUBPROTOCOL = versions.SUBPROTOCOL
CONTRACT = versions.CONTRACT
AGUI = versions.AGUI
MCP_APPS = versions.MCP_APPS
HELLO_TIMEOUT_S = 10.0
SESSION_EXPIRED = "session_expired"  # the close reason when the session token's hour is over
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_INTERNAL = 1011
CLOSE_HELLO_TIMEOUT = 4008
CATALOGS = list(versions.A2UI_CATALOG_IDS)  # the A2UI catalogs this server composes in, in its order

# the pisa.* extensions this server speaks, with their majors (CONTRACT-2 §5.2); `pisa.voice.turn_hints` only
# answers an app that sends partial transcripts
EXTENSIONS = {
    "pisa.voice": 1,
    "pisa.voice.turn_hints": 1,
    "pisa.presence": 1,
    "pisa.location": 1,
    "pisa.notice": 1,
    "pisa.directions": 1,
    "pisa.call": 1,
    "pisa.compose": 1,
    "pisa.approval": 1,
    "pisa.surface": 1,
    "pisa.app": 1,  # contract 2.2: the app's own screens (`pisa.app.open`, `pisa.app.changed`)
}
AUDIO_FORMATS = ("mp3", "aac_adts", "pcm16")  # the voice formats Pisa's speaker makes


# --- the app's frames -------------------------------------------------------------------------------------
class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Versions(_Frame):
    versions: list[str] = Field(min_length=1)


class _AguiVersions(_Frame):
    protocolVersions: list[str] = Field(min_length=1)


class _McpApps(_Frame):
    protocolVersions: list[str]
    mimeTypes: list[str]
    bundledLibraries: list[str] = Field(default_factory=list, max_length=20)


class _DeviceFacts(_Frame):
    deviceId: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    ]
    appVersion: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    platform: Literal["ios", "watchos", "android", "web", "cli", "other"]
    surfaces: list[Literal["phone", "glasses_display", "watch", "none"]] = Field(default_factory=list)
    audioFormats: list[Literal["opus", "aac_adts", "mp3", "pcm16"]] = Field(default_factory=list)
    wearable: dict[str, Any] | None = None
    tz: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    region: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    hourCycle: Literal["h12", "h23"] | None = None


class _Resume(_Frame):
    sessionId: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    lastSeq: int = Field(ge=0)


class HelloFrame(_Frame):
    """`$defs/Hello`: what the app speaks and draws (CONTRACT-2 §4.2)."""

    op: Literal["hello"]
    contract: _Versions
    agui: _AguiVersions
    a2ui: dict[str, Any] | None = None
    mcpApps: _McpApps | None = None
    rings: list[Literal[0, 1, 2]] = Field(min_length=1)
    extensions: dict[str, int] = Field(default_factory=dict)
    device: _DeviceFacts
    entitlements: list[str] = Field(default_factory=list, max_length=8)
    resume: _Resume | None = None


def negotiate(hello: HelloFrame) -> Negotiated | str:
    """The server's picks from the app's lists (CONTRACT-2 §4.2); a refusal's message when they share no
    contract or AG-UI version."""
    shared = [v for v in versions.CONTRACTS if v in hello.contract.versions]
    if not shared:
        return f"This server speaks contract {' or '.join(versions.CONTRACTS)}; update the app."
    if AGUI not in hello.agui.protocolVersions:
        return f"This server speaks AG-UI {AGUI}; update the app."
    offered = (hello.a2ui or {}).get("1.0", {})
    listed = offered.get("supportedCatalogIds", []) if isinstance(offered, dict) else []
    catalogs = [c for c in CATALOGS if c in listed] if 1 in hello.rings else []
    mcp = hello.mcpApps
    mcp_apps = (
        2 in hello.rings
        and mcp is not None
        and MCP_APPS in mcp.protocolVersions
        and "text/html;profile=mcp-app" in mcp.mimeTypes
    )
    rings = [r for r in sorted(set(hello.rings)) if r == 0 or (r == 1 and catalogs) or (r == 2 and mcp_apps)]
    extensions = {k: min(v, EXTENSIONS[k]) for k, v in hello.extensions.items() if k in EXTENSIONS and v >= 1}
    # Pisa's voice back is `pisa.voice.audio`: without `pisa.voice` the app speaks the text itself
    formats = (
        [f for f in hello.device.audioFormats if f in AUDIO_FORMATS] if "pisa.voice" in extensions else []
    )
    return Negotiated(catalogs, rings, mcp_apps, extensions, formats, contract=shared[-1])


def welcome_body(negotiated: Negotiated, session_id: str, replayed: bool) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "contract": negotiated.contract,
        "agui": AGUI,
        "a2ui": {"version": "1.0", "catalogIds": negotiated.catalogs} if negotiated.catalogs else None,
        "mcpApps": {"protocolVersion": MCP_APPS} if negotiated.mcp_apps else None,
        "rings": negotiated.rings,
        "extensions": negotiated.extensions,
        "features": FEATURES,
        "languages": list(LANGUAGES),
        "replayed": replayed,
    }


# --- the socket -------------------------------------------------------------------------------------------
class SocketTransport:
    """A session's socket: what the session sends goes out in order (one writer), and what the app sends is
    read here and handed to the session (`serve`)."""

    acknowledges = True  # the app acknowledges each surface it shows (`pisa.surface.ack`)

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.session: DeviceSession | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None

    def open(self) -> None:
        """The socket's writer: what this connection sends goes out in order."""
        if self._pump is None:
            self._pump = asyncio.create_task(self._write())

    def start(self, session: DeviceSession, replay: list[str], replayed: bool) -> None:
        """Welcome the app, replay what it missed, then send live frames (one step: nothing comes between)."""
        self.open()
        live = session.live
        live.attach(self._queue.put_nowait)
        last = live.seq - len(replay)
        live.welcome(welcome_body(session.negotiated, live.session_id, replayed), seq=last)
        for text in replay:
            self._queue.put_nowait(text)

    async def _write(self) -> None:
        while (text := await self._queue.get()) is not None:
            try:
                await self.ws.send_text(text)
            except (WebSocketDisconnect, RuntimeError):
                if self.session is not None:
                    self.session.closed = True
                    self.session.live.detach(self._queue.put_nowait)
                return

    async def drain(self) -> None:
        if self.session is not None:
            self.session.live.detach(self._queue.put_nowait)
        self._queue.put_nowait(None)
        if self._pump is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._pump), 5)
            except (TimeoutError, asyncio.CancelledError):
                self._pump.cancel()

    def last(self, text: str) -> None:
        if self.session is not None:
            self.session.live.detach(self._queue.put_nowait)
        self._queue.put_nowait(text)

    async def close(self, code: int, reason: str) -> None:
        await self.drain()
        try:
            await self.ws.close(code=code, reason=reason)
        except RuntimeError:
            pass

    async def serve(self, session: DeviceSession) -> None:
        """Read the app's frames until the socket closes; each goes to the session."""
        while not session.closed:
            try:
                event = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                break
            if event["type"] == "websocket.disconnect":
                break
            text = event.get("text")
            if text is None:
                session.live.control(
                    "error", **control_error("invalid_frame", "Binary frames are not supported.")
                )
                continue
            try:
                frame = json.loads(text)
                if not isinstance(frame, dict):
                    raise FrameError("A frame is a JSON object.")
                await session.frame(frame)
            except (FrameError, json.JSONDecodeError) as err:
                message = (
                    str(err) if isinstance(err, FrameError) else f"The frame is not valid JSON ({err.msg})."
                )
                session.live.control("error", **control_error("invalid_frame", message))
            except Exception as exc:
                err = failure(
                    _log, exc, "frame_failed", "Pisa failed to handle the frame (trace {trace}).",
                    device_id=str(session.device.id),
                )  # fmt: skip
                session.live.control("error", **control_error("internal", err.message, err.retryable))


# --- the endpoint -----------------------------------------------------------------------------------------
def _sessions(ws: WebSocket) -> dict[uuid.UUID, LiveSession]:
    """Each device's latest contract 2 session, kept past its socket for a resume."""
    state = ws.app.state
    if not hasattr(state, "live_sessions"):
        state.live_sessions = {}
    return state.live_sessions


async def _refuse(ws: WebSocket, code: str, message: str, close: int) -> None:
    await ws.send_text(json.dumps({"control": "error", "seq": 0, **control_error(code, message)}))
    await ws.close(code=close, reason=message[:120])


@router.websocket("/v2/live")
async def live_socket(ws: WebSocket) -> None:
    runtime: Runtime = ws.app.state.runtime
    registry: SessionRegistry = ws.app.state.sessions
    await ws.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in ws.scope.get("subprotocols", []) else None)
    try:
        async with runtime.pool.connection() as conn:
            device = await authenticate(conn, bearer_token(ws.headers.get("authorization")))
    except PisaError as err:
        if err.code == "session_expired":  # not auth_failed: the app keeps its keys and signs a new nonce
            await ws.close(code=CLOSE_AUTH_FAILED, reason=SESSION_EXPIRED)
            return
        code = "device_revoked" if err.code == "device_revoked" else "auth_failed"
        await _refuse(ws, code, err.message, CLOSE_AUTH_FAILED)
        return
    except UNREACHABLE as exc:  # the guard can't decide: refused
        fails_closed(_log, "device_auth", exc)
        await _refuse(ws, "internal", database_unreachable().message, CLOSE_INTERNAL)
        return
    try:
        event = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT_S)
    except TimeoutError:
        await ws.close(code=CLOSE_HELLO_TIMEOUT, reason=f"no hello within {HELLO_TIMEOUT_S:g} s")
        return
    if event["type"] == "websocket.disconnect":
        return
    try:
        hello = HelloFrame.model_validate_json(event.get("text") or event.get("bytes") or b"")
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "frame"
        await _refuse(
            ws,
            "invalid_frame",
            f"The first frame must be a valid hello ({where}: {first['msg']}).",
            CLOSE_PROTOCOL_ERROR,
        )
        return
    if hello.device.deviceId != str(device.id):
        await _refuse(
            ws,
            "auth_failed",
            "The hello names a different device than the one this token was issued to.",
            CLOSE_AUTH_FAILED,
        )
        return
    negotiated = negotiate(hello)
    if isinstance(negotiated, str):
        await _refuse(ws, "contract_unsupported", negotiated, CLOSE_PROTOCOL_ERROR)
        return
    facts = hello.device
    if facts.tz or facts.region or facts.hourCycle:  # where the phone is, on every connect
        # never fails the hello; the last known zone stays
        with degrades(_log, "device_context", device_id=str(device.id)):
            await runtime.zone.update(
                tz=facts.tz,
                region=facts.region,
                hour_cycle=facts.hourCycle,
                device_id=device.id,
                now=runtime.clock(),
            )

    sessions = _sessions(ws)
    live, replay = sessions.get(device.id), None
    if live is not None and hello.resume is not None and hello.resume.sessionId == live.session_id:
        replay = live.replay_after(hello.resume.lastSeq)
    if replay is None:
        live = LiveSession(str(device.id), clock_ms=lambda: int(runtime.clock().timestamp() * 1000))
        sessions[device.id] = live
    assert live is not None
    socket = SocketTransport(ws)
    session = socket.session = DeviceSession(socket, device, runtime, live, negotiated)
    await registry.register(session)  # a connection it replaces is closed first (`replaced`)
    expiry: asyncio.Task[None] | None = None
    try:
        async with runtime.pool.connection() as conn:
            if (await devices_service.revoked(conn, [device.id])).get(device.id, True):
                socket.open()
                await session.revoke()
                return
            await presence.reset(conn, device.id, runtime.clock())
        _log.info(
            "live_connected",
            device_id=str(device.id),
            session_id=live.session_id,
            app_version=facts.appVersion,
            replayed=replay is not None,
            rings=negotiated.rings,
        )
        socket.start(session, replay or [], replay is not None)
        await session.catch_up()
        if device.session_expires_at is not None:
            expiry = asyncio.create_task(expire_session(session, device.session_expires_at))
        await socket.serve(session)
    finally:
        if expiry is not None:
            expiry.cancel()
        session.closed = True
        session.cancel_all()
        session.end_notice_audio()
        await socket.drain()
        _log.info("live_disconnected", device_id=str(device.id), session_id=live.session_id)
        if registry.unregister(session):
            reset = _background.start(_reset_presence(runtime, device.id))
            await asyncio.shield(reset)


async def expire_session(session: DeviceSession, at: datetime) -> None:
    """The session token's hour is over: the socket closes 4003 `session_expired` (CONTRACT-2 §5); a run over
    HTTP (`/v2/runs`) ends, its reply cancelled."""
    await asyncio.sleep(max(0.0, (at - datetime.now(UTC)).total_seconds()))
    _log.info("live_session_expired", device_id=str(session.device.id))
    await session.close(CLOSE_AUTH_FAILED, SESSION_EXPIRED)


async def _reset_presence(runtime: Runtime, device_id: uuid.UUID) -> None:
    with degrades(_log, "presence_reset", device_id=str(device_id)):
        async with runtime.pool.connection() as conn:
            await presence.reset(conn, device_id, runtime.clock())
