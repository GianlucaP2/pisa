"""The app's side of `/v2/live` in tests, for the tests of the shared pipeline.

`live(client, token, device_id)` opens the socket with a contract 2 hello and gives a `LiveApp`. The app
speaks contract 2 only (`op` frames, checked against the official schemas as they are sent), and every frame
the server sends is kept for the conformance kit's checks (`check`: the schemas, the AG-UI lifecycle per
thread, `seq`, and nothing sent that the hello did not list).

What the pipeline sent the device is recorded too, at the session's `send` and only when it went out on the
socket (`sent`): its internal messages (`pisa.services.session_messages`) as dicts, with their `type` (`say`,
`present`, `card`, `notify` ...), and each approval whose interrupt went out (`type` `interrupt`). A test of
the pipeline asserts on those (what Pisa said, showed or noticed), a test of the wire on `frames`. Each
`present` the socket writes as a surface is acknowledged with `status` (the app's `pisa.surface.ack`), as the
app does.
"""

import dataclasses
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from pisa_conformance.cli import negotiation_problems
from pisa_conformance.validate import check_lifecycle, check_seq, client_frame_errors, server_frame_errors
from pydantic import BaseModel

from pisa.api.live import EXTENSIONS as live_extensions
from pisa.ports.push import OutgoingApproval
from pisa.services import a2ui, session_messages
from pisa.services.agui import EXTENSION_OF, LiveSession
from pisa.services.devices import Device
from pisa.services.session import DeviceSession, Negotiated
from pisa.services.session_messages import Utterance
from tests.conftest import pair

# what the app handles: every extension whose events the server sends (a hello may list fewer: `extensions`)
EXTENSIONS = {
    "pisa.notice": 1,
    "pisa.surface": 1,
    "pisa.approval": 1,
    "pisa.voice": 1,
    "pisa.directions": 1,
    "pisa.call": 1,
    "pisa.compose": 1,
}
WAKE = {"hey_pisa": "wake_phrase"}  # the session's names → the contract's
Status = str | None | Callable[[dict[str, Any]], str | None]


@pytest.fixture
def device(db: psycopg.Connection) -> tuple[str, str]:
    """A paired device: (its id, a session token)."""
    return pair(db)


def hello(
    device_id: str,
    *,
    ring1: bool = True,
    drawings: bool = False,
    audio_formats: list[str] | None = None,
    turn_hints: bool = False,
    extensions: dict[str, int] | None = None,
    **device: Any,
) -> dict[str, Any]:
    """A contract 2 hello: Ring 1 (every object kind, the catalog `core:1`) unless `ring1` is False, Ring 2
    drawings with `drawings`, the voice formats it plays, partial transcripts with `turn_hints`, the `pisa.*`
    extensions it handles (`EXTENSIONS` unless given); `device` adds to its device facts (tz, region,
    hourCycle)."""
    rings = [0] + ([1] if ring1 else []) + ([2] if drawings else [])
    body: dict[str, Any] = {
        "op": "hello",
        "contract": {"versions": ["2.0"]},
        "agui": {"protocolVersions": ["1.0"]},
        "rings": rings,
        "extensions": (EXTENSIONS if extensions is None else extensions)
        | ({"pisa.voice.turn_hints": 1} if turn_hints else {}),
        "device": {
            "deviceId": device_id,
            "appVersion": "1.0.0",
            "platform": "ios",
            "surfaces": ["phone"],
            "audioFormats": audio_formats or [],
        }
        | device,
    }
    if ring1:
        body["a2ui"] = {"1.0": {"supportedCatalogIds": [a2ui.CATALOG_ID]}}
    if drawings:
        body["mcpApps"] = {
            "protocolVersions": ["2026-01-26"],
            "mimeTypes": ["text/html;profile=mcp-app"],
        }
    return body


def extension_problems(welcome: dict[str, Any], frames: list[dict[str, Any]]) -> list[str]:
    """The kit's negotiation check for the `pisa.*` events (the kit's own checks rings and catalogs only): an
    event whose extension the welcome did not grant is never sent (CONTRACT-2 §4.2)."""
    granted = set(welcome.get("extensions") or {})
    out = []
    for f in frames:
        event = f.get("event") or {}
        extension = EXTENSION_OF.get(event.get("name", "")) if event.get("type") == "CUSTOM" else None
        if extension is not None and extension not in granted:
            out.append(f"seq {f.get('seq')}: {event['name']} sent without {extension} in the welcome")
    return out


def connect(client: Any, token: str) -> Any:
    return client.websocket_connect(
        "/v2/live", headers={"Authorization": f"Bearer {token}"}, subprotocols=["pisa.v2"]
    )


@contextmanager
def live(
    client: Any, token: str, device_id: str, *, status: Status = "shown", **options: Any
) -> Iterator["LiveApp"]:
    """The app on `/v2/live`, welcomed (`hello`'s options), its pipeline's sends recorded."""
    sent: list[dict[str, Any]] = []
    original, original_approval = DeviceSession.send, DeviceSession.approval

    async def recording(session: DeviceSession, message: Any) -> bool:
        went = await original(session, message)  # writes its frames before it returns: recorded first
        if went:
            sent.append(message.model_dump(mode="json", by_alias=True))
        return went

    async def interrupting(session: DeviceSession, approval: OutgoingApproval) -> bool:
        went = await original_approval(session, approval)
        if went:  # the approval's interrupt, on its own thread
            sent.append({"type": "interrupt", "approval_id": approval.id} | dataclasses.asdict(approval))
        return went

    with (
        patch.object(DeviceSession, "send", recording),
        patch.object(DeviceSession, "approval", interrupting),
        connect(client, token) as ws,
    ):
        app = LiveApp(ws, hello(device_id, **options), sent, status)
        app.open()
        yield app


class LiveApp:
    def __init__(
        self, ws: Any, hello_frame: dict[str, Any], sent: list[dict[str, Any]], status: Status
    ) -> None:
        self.ws, self.hello, self.sent, self.status = ws, hello_frame, sent, status
        self.frames: list[dict[str, Any]] = []
        self.welcome: dict[str, Any] = {}
        self.thread = "thread-1"  # the app's conversation thread
        self._acked: set[int] = set()  # the recorded `present`s acknowledged (their index in `sent`)
        self.cursor = 0  # the next message of `sent` the test reads

    # --- the wire ---------------------------------------------------------------------------------------
    def open(self) -> None:
        self.send(self.hello)
        self.welcome = self.receive()
        assert self.welcome.get("control") == "welcome", self.welcome

    def send(self, frame: dict[str, Any]) -> None:
        assert client_frame_errors(frame) == [], client_frame_errors(frame)
        self.ws.send_text(json.dumps(frame))

    def receive(self) -> dict[str, Any]:
        frame = self.ws.receive_json()
        self.frames.append(frame)
        event = frame.get("event", {})
        if event.get("type") == "ACTIVITY_SNAPSHOT" and event.get("activityType") in (
            "a2ui-surface",
            "mcp-apps",
        ):
            self._ack(event["messageId"])
        return frame

    def until(self, done: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        frames = []
        while True:
            frame = self.receive()
            frames.append(frame)
            if done(frame):
                return frames

    def run_ends(self, run_id: str) -> list[dict[str, Any]]:
        return self.until(lambda f: f.get("runId") == run_id and _ends(f))

    def run_ended_since(self, index: int, thread: str | None = None) -> dict[str, Any]:
        """The first run end on the conversation (or on `thread`) among the frames from `index` on, reading
        the socket until there is one. The conversation is any thread that is not the server's (`pisa-*`):
        before the app's first run names its thread, the server's runs go on its default one."""

        def on(frame: dict[str, Any]) -> bool:
            named = str(frame.get("threadId") or "")
            return named == thread if thread else bool(named) and not named.startswith("pisa-")

        while True:
            for frame in self.frames[index:]:
                if on(frame) and _ends(frame):
                    return frame
            index = len(self.frames)
            self.receive()

    def next_run_ends(self, thread: str | None = None) -> list[dict[str, Any]]:
        """Frames until a run on the conversation thread (or `thread`) ends."""
        on = thread or self.thread
        return self.until(lambda f: f.get("threadId") == on and _ends(f))

    def check(self) -> None:
        """The kit's checks over everything this socket received."""
        problems = [f"seq {f.get('seq')}: {e}" for f in self.frames for e in server_frame_errors(f)]
        problems += check_lifecycle([f for f in self.frames if "event" in f])
        problems += check_seq(self.frames)
        problems += negotiation_problems(self.hello, self.frames)
        problems += extension_problems(self.welcome, self.frames)
        assert problems == [], problems[:10]

    # --- what the app sends -----------------------------------------------------------------------------
    def say(
        self,
        text: str,
        *,
        lang: str = "it-IT",
        source: str = "typed",
        message_id: str | None = None,
        thread: str | None = None,
        **turn: Any,
    ) -> dict[str, Any]:
        """The user's words as a run on the conversation thread (`turn`: wake, continues, voice, origin); the
        run's input (its `runId`, and `messages[0]["id"]`: the id Pisa's reply answers)."""
        if "wake" in turn:
            turn["wake"] = WAKE.get(turn["wake"], turn["wake"])
        body = {
            "threadId": thread or self.thread,
            "runId": f"run-{uuid.uuid4()}",
            "messages": [{"id": message_id or str(uuid.uuid4()), "role": "user", "content": text}],
            "forwardedProps": {"pisa": {"turn": {"source": source, "lang": lang} | turn}},
        }
        self.send({"op": "run", "input": body})
        return body

    def utterance(self, utterance: Utterance) -> dict[str, Any]:
        """A final `Utterance` of the session's, said as the app says it."""
        assert utterance.final, "a partial transcript is `partial`"
        turn: dict[str, Any] = {}
        if utterance.wake:
            turn["wake"] = utterance.wake
        if utterance.continues:
            turn["continues"] = utterance.continues
        if utterance.voice is not None:
            turn["voice"] = utterance.voice.model_dump(mode="json")
        return self.say(
            utterance.text, lang=utterance.lang, source=utterance.source, message_id=utterance.id, **turn
        )

    def partial(self, text: str, turn_id: str, lang: str = "it-IT") -> None:
        self.custom("pisa.voice.partial", {"text": text, "lang": lang, "turnId": turn_id})

    def custom(self, name: str, value: dict[str, Any]) -> None:
        self.send({"op": "custom", "name": name, "value": value})

    def cancel(self, run_id: str) -> None:
        self.send({"op": "cancel", "runId": run_id})

    def action(self, name: str, object_id: str, **context: Any) -> None:
        """An A2UI action (`pisa.choose`, `pisa.ask`, `pisa.act` ...) on the surface showing `object_id`."""
        action = {
            "name": name,
            "surfaceId": self.surface_of(object_id),
            "sourceComponentId": "root",
            "timestamp": "2026-10-20T11:00:00Z",
            "context": context,
        }
        self.send({"op": "a2ui", "message": {"version": "1.0", "action": action}})

    def object_event(self, event: dict[str, Any]) -> None:
        """An `ObjectEvent` of the session's (`object_id`, `event`, ...) as the app sends it: a choice, a tap
        on a drawing's element and an act are A2UI actions, what it did with a surface
        `pisa.surface.event`."""
        object_id, kind = event["object_id"], event["event"]
        if kind == "chosen":
            self.action("pisa.choose", object_id, optionId=event["option_id"])
        elif kind == "asked":
            self.action("pisa.ask", object_id, elementId=event["element_id"])
        elif kind == "acted" and event["action"] == "undo":
            self.action("pisa.undo", object_id)
        elif kind == "acted":
            self.action("pisa.act", object_id, action=event["action"])
        else:
            self.custom("pisa.surface.event", {"surfaceId": self.surface_of(object_id), "event": kind})

    # --- what the pipeline sent -------------------------------------------------------------------------
    # A cursor reads `sent` in order, as the app reads its socket: `next` is the next message the pipeline
    # sent (the socket is read until there is one), `ping` what came before the pong.
    def mark(self) -> int:
        return len(self.sent)

    def since(self, mark: int) -> list[dict[str, Any]]:
        return list(self.sent[mark:])

    def next(self) -> dict[str, Any]:
        while self.cursor >= len(self.sent):
            self.receive()
        self.cursor += 1
        return self.sent[self.cursor - 1]

    def until_sent(self, done: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        """The messages from the cursor up to the first that `done` takes."""
        out = [self.next()]
        while not done(out[-1]):
            out.append(self.next())
        return out

    def next_of(self, kind: str) -> list[dict[str, Any]]:
        """The messages from the cursor up to the next of `kind`."""
        return self.until_sent(lambda m: m["type"] == kind)

    def reply(self, reply_to: str | None = None) -> list[dict[str, Any]]:
        """The messages from the cursor up to a reply's end: its final `say` or an `error` (of `reply_to`,
        when given)."""

        def ends(m: dict[str, Any]) -> bool:
            if reply_to is not None and m.get("reply_to") != reply_to:
                return False
            return m["type"] == "error" or (m["type"] == "say" and m["final"])

        return self.until_sent(ends)

    def ping(self) -> list[dict[str, Any]]:
        """A ping, and every frame until its pong (whatever the app sent before it has been handled): the
        messages the pipeline sent meanwhile, from the cursor."""
        self.send({"op": "ping"})
        self.until(lambda f: f.get("control") == "pong")
        out, self.cursor = self.sent[self.cursor :], len(self.sent)
        return out

    def turn(self, text: str, **say: Any) -> list[dict[str, Any]]:
        """The user's words, and what the pipeline sent until their run ended (the cursor moves past it)."""
        mark = self.mark()
        body = self.say(text, **say)
        self.run_ends(body["runId"])
        self.cursor = len(self.sent)
        return self.since(mark)

    def surface_of(self, object_id: str) -> str:
        for message in reversed(self.sent):
            if message["type"] == "present" and message["object"]["object_id"] == object_id:
                return _surface(message)
        return a2ui.surface_id_for(object_id)

    def _ack(self, surface_id: str) -> None:
        for i in range(len(self.sent) - 1, -1, -1):
            message = self.sent[i]
            if message["type"] == "present" and i not in self._acked and _surface(message) == surface_id:
                self._acked.add(i)
                answer = self.status(message) if callable(self.status) else self.status
                if answer is not None:
                    surface = "phone" if answer in ("shown", "queued", "deferred") else "none"
                    self.custom(
                        "pisa.surface.ack", {"surfaceId": surface_id, "status": answer, "surface": surface}
                    )
                return


def _ends(frame: dict[str, Any]) -> bool:
    return frame.get("event", {}).get("type") in ("RUN_FINISHED", "RUN_ERROR")


def _surface(present: dict[str, Any]) -> str:
    obj = present["object"]
    if obj["kind"] == "drawing":
        return f"ui://pisa/drawing/{obj['data']['drawing_id']}"
    return a2ui.surface_id_for(obj["object_id"])


FULL = Negotiated([a2ui.CATALOG_ID], [0, 1, 2], True, dict(live_extensions), ["mp3"])  # every extension


def wire_errors(message: Any) -> list[str]:
    """One of the session's messages written by `/v2/live` (every ring negotiated), and the conformance kit's
    findings on the frames it became: the schemas, the AG-UI lifecycle and `seq`. [] when it is valid
    contract 2, or when the wire has nothing to write for it."""
    device_id = "00000000-0000-4000-8000-0000000000d1"
    device = Device(uuid.UUID(device_id), "iPhone", "ios", datetime(2026, 10, 20, tzinfo=UTC), None, None)
    session = DeviceSession(object(), device, object(), LiveSession(device_id), FULL)  # type: ignore[arg-type]
    session._write_message(message)
    frames = session.live.kept()
    problems = [f"seq {f.get('seq')}: {e}" for f in frames for e in server_frame_errors(f)]
    problems += check_lifecycle([f for f in frames if "event" in f])
    return problems + check_seq(frames, 1 if frames else None)


def said(messages: list[dict[str, Any]]) -> str:
    """The words of the `say`s among `messages`, as the app hears them."""
    return "".join(m["delta"] for m in messages if m["type"] == "say").strip()


def of_type(messages: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [m for m in messages if m["type"] == kind]


def _message_classes() -> dict[str, type[BaseModel]]:
    out: dict[str, type[BaseModel]] = {}
    for value in vars(session_messages).values():
        if isinstance(value, type) and issubclass(value, BaseModel) and "type" in value.model_fields:
            out[value.model_fields["type"].default] = value
    return out


MESSAGES = _message_classes()  # each of the session's messages by its `type`


def message_of(raw: str | dict[str, Any]) -> Any:
    """One of the session's messages from its JSON (a fixture's, a frame's values), by its `type`; a value out
    of its range raises pydantic's ValidationError, as `/v2/live` refuses the frame (`invalid_frame`)."""
    body = json.loads(raw) if isinstance(raw, str) else raw
    return MESSAGES[body["type"]].model_validate_json(json.dumps(body))
