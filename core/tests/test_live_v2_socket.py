"""`GET /v2/live`, contract 2's socket, end to end with a fake model: the handshake, a turn as an AG-UI run
(its words, tool steps and surfaces), taps, barge-in, failures, notices, a resume that replays exactly once,
on the shared pipeline.

Every stream a test records is checked with the conformance kit: each frame against the official schemas,
the AG-UI lifecycle per thread, `seq`, and the negotiation (nothing is sent that the hello did not list)."""

import json
import uuid
from typing import Any

import pytest
from pisa_conformance.cli import negotiation_problems
from pisa_conformance.validate import check_lifecycle, check_seq, client_frame_errors, server_frame_errors
from starlette.websockets import WebSocketDisconnect

from pisa.services import a2ui
from tests.conftest import FakeModels, pair_v2, set_key
from tests.live_app import extension_problems
from tests.test_interface_tools import SHOW_PLAN, TWO, _calls


def hello(device_id: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "op": "hello",
        "contract": {"versions": ["2.0"]},
        "agui": {"protocolVersions": ["1.0"]},
        "a2ui": {"1.0": {"supportedCatalogIds": [a2ui.CATALOG_ID]}},
        "rings": [0, 1],
        "extensions": {"pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1, "pisa.voice": 1},
        "device": {"deviceId": device_id, "appVersion": "1.0.0", "platform": "ios", "surfaces": ["phone"]},
    }
    return body | over


def run(text: str, *, thread: str = "thread-1", lang: str = "it-IT", **pisa: Any) -> dict[str, Any]:
    run_id = f"run-{uuid.uuid4()}"
    body = {
        "threadId": thread,
        "runId": run_id,
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": text}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": lang}} | pisa},
    }
    return {"op": "run", "input": body}


class App:
    """The app's side of one socket: what it sent and everything it received, for the kit's checks."""

    def __init__(self, ws: Any, hello_frame: dict[str, Any]) -> None:
        self.ws, self.hello, self.got = ws, hello_frame, []  # type: ignore[var-annotated]

    def send(self, frame: dict[str, Any]) -> None:
        assert client_frame_errors(frame) == [], client_frame_errors(frame)
        self.ws.send_text(json.dumps(frame))

    def receive(self) -> dict[str, Any]:
        frame = self.ws.receive_json()
        self.got.append(frame)
        return frame

    def until(self, done: Any) -> list[dict[str, Any]]:
        frames = []
        while True:
            frame = self.receive()
            frames.append(frame)
            if done(frame):
                return frames

    def run_ends(self, run_id: str) -> list[dict[str, Any]]:
        return self.until(
            lambda f: f.get("runId") == run_id and f["event"]["type"] in ("RUN_FINISHED", "RUN_ERROR")
        )

    def check(self) -> None:
        """The kit's checks over the whole recorded stream."""
        problems = [f"seq {f.get('seq')}: {e}" for f in self.got for e in server_frame_errors(f)]
        problems += check_lifecycle([f for f in self.got if "event" in f])
        problems += check_seq(self.got)
        problems += negotiation_problems(self.hello, self.got)
        problems += extension_problems(self.got[0], self.got)
        assert problems == [], problems[:10]


def connect(client: Any, token: str) -> Any:
    return client.websocket_connect(
        "/v2/live", headers={"Authorization": f"Bearer {token}"}, subprotocols=["pisa.v2"]
    )


def opened(ws: Any, frame: dict[str, Any]) -> App:
    app = App(ws, frame)
    app.send(frame)
    welcome = app.receive()
    assert welcome["control"] == "welcome", welcome
    return app


def events(frames: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [f["event"] for f in frames if "event" in f and f["event"]["type"] == kind]


def said(frames: list[dict[str, Any]]) -> str:
    return "".join(e["delta"] for e in events(frames, "TEXT_MESSAGE_CONTENT")).strip()


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


def test_handshake_welcomes_with_only_what_the_app_listed_and_pings(client, device) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        welcome = app.got[0]
        assert welcome["seq"] == 0 and welcome["replayed"] is False and welcome["sessionId"]
        assert (
            welcome["a2ui"] == {"version": "1.0", "catalogIds": [a2ui.CATALOG_ID]}
            and welcome["mcpApps"] is None
        )
        assert welcome["rings"] == [0, 1] and welcome["languages"] == ["it-IT", "en-US"]
        app.send({"op": "ping"})
        assert app.receive() == {"control": "pong", "seq": 1}
        app.check()


@pytest.mark.parametrize(
    ("change", "code", "close"),
    [
        (lambda h: h | {"contract": {"versions": ["3.0"]}}, "contract_unsupported", 1002),
        (lambda h: {"op": "hello"}, "invalid_frame", 1002),
        (lambda h: h | {"device": h["device"] | {"deviceId": str(uuid.uuid4())}}, "auth_failed", 4003),
    ],
)
def test_a_hello_the_server_cannot_take_is_refused_by_name(client, device, change, code, close) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        ws.send_text(json.dumps(change(hello(device_id))))
        refused = ws.receive_json()
        assert (
            refused["control"] == "error" and refused["code"] == code and server_frame_errors(refused) == []
        )
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == close


def test_a_bad_token_is_auth_failed_and_no_hello_is_4008(client, device, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    with connect(client, "not-a-token") as ws:
        assert ws.receive_json()["code"] == "auth_failed"
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 4003
    monkeypatch.setattr("pisa.api.live.HELLO_TIMEOUT_S", 0.2)
    with connect(client, device[1]) as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_text()
    assert closed.value.code == 4008


def test_a_turn_is_the_app_s_run_with_pisa_s_words(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = "Sono le tredici. Hai pranzo alle 13:30!"
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run("Pisa, che ore sono?")
        app.send(turn)
        frames = app.run_ends(turn["input"]["runId"])
        app.check()
    started = events(frames, "RUN_STARTED")[0]
    assert started["threadId"] == "thread-1" and started["runId"] == turn["input"]["runId"]
    assert started["protocolVersion"] == "1.0"
    assert events(frames, "RUN_FINISHED")[0]["outcome"] == {"type": "success"}
    assert said(frames) == "Sono le tredici. Hai pranzo alle 13:30!"
    start = events(frames, "TEXT_MESSAGE_START")[0]
    assert start["metadata"] == {"pisa": {"lang": "it-IT", "priority": "normal"}}
    roles = db.execute("SELECT role, trim(content) FROM messages ORDER BY id").fetchall()
    assert roles == [
        ("user", "Pisa, che ore sono?"),
        ("assistant", "Sono le tredici. Hai pranzo alle 13:30!"),
    ]


def test_tool_steps_and_surfaces_ride_the_run_and_the_ack_is_the_show_s_outcome(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    note = [("show_note", {"title": "Nota", "lines": ["una riga"], "purpose": "la nota"})]
    fake_models.default = _calls(SHOW_PLAN + note, "Ecco.")
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run("fammi vedere il piano e una nota")
        app.send(turn)
        frames: list[dict[str, Any]] = []
        while not frames or frames[-1].get("event", {}).get("type") != "RUN_FINISHED":
            frame = app.receive()
            frames.append(frame)
            if frame.get("event", {}).get("type") == "ACTIVITY_SNAPSHOT":
                ack = {"surfaceId": frame["event"]["messageId"], "status": "shown", "surface": "phone"}
                app.send({"op": "custom", "name": "pisa.surface.ack", "value": ack})
        app.check()
    kinds = [e["type"] for e in events(frames, "STEP_STARTED") + events(frames, "TOOL_CALL_START")]
    assert kinds.count("STEP_STARTED") == 2 and kinds.count("TOOL_CALL_START") == 2
    names = [e["toolCallName"] for e in events(frames, "TOOL_CALL_START")]
    assert names == ["show_plan", "show_note"]
    assert not events(frames, "TOOL_CALL_RESULT")  # results stay on the server
    surfaces = events(frames, "ACTIVITY_SNAPSHOT")
    refs = [
        c["component"]
        for s in surfaces
        for c in s["content"]["a2ui_operations"][0]["createSurface"]["components"]
    ]
    assert "Ref" in refs and "BulletList" in refs
    assert all(s["content"]["pisa"]["replyTo"] == turn["input"]["messages"][0]["id"] for s in surfaces)
    outcomes = dict(db.execute("SELECT tool, outcome FROM pisa_decisions WHERE move = 'show'").fetchall())
    assert outcomes == {"show_plan": "shown", "show_note": "shown"}  # the app's ack is the show's outcome


def test_a_tap_on_a_choice_is_the_user_s_next_turn_in_a_run_of_its_own(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    ask = [("ask_choice", {"question": "Dove andiamo?", "options": TWO})]
    fake_models.default = _calls(("Dove andiamo? ", ask), "Vada per B.")
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run("dove andiamo?")
        app.send(turn)
        frames = app.run_ends(turn["input"]["runId"])
        surface = events(frames, "ACTIVITY_SNAPSHOT")[0]
        choice = surface["content"]["a2ui_operations"][0]["createSurface"]["components"][0]
        assert choice["component"] == "Choice"
        action = {
            "name": "pisa.choose",
            "surfaceId": surface["messageId"],
            "sourceComponentId": "root",
            "timestamp": "2026-10-20T11:00:00Z",
            "context": {"optionId": "o2"},
        }
        app.send({"op": "a2ui", "message": {"version": "1.0", "action": action}})
        tap = app.until(lambda f: f.get("event", {}).get("type") == "RUN_FINISHED")
        app.check()
    assert said(tap) == "Vada per B." and tap[0]["event"]["type"] == "RUN_STARTED"
    assert tap[0]["runId"] != turn["input"]["runId"]


def test_cancel_ends_the_run_cancelled_and_nothing_more_comes(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    import asyncio

    device_id, token = device
    set_key(db, secrets_key)

    async def slow(messages, info):  # type: ignore[no-untyped-def]
        yield "Prima frase. "
        await asyncio.sleep(30)
        yield "mai."

    fake_models.default = slow
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run("raccontami")
        app.send(turn)
        app.until(lambda f: f.get("event", {}).get("type") == "TEXT_MESSAGE_CONTENT")
        app.send({"op": "cancel", "runId": turn["input"]["runId"]})
        ended = app.run_ends(turn["input"]["runId"])
        app.send({"op": "ping"})
        after = app.receive()
        app.check()
    assert ended[-1]["event"]["outcome"] == {"type": "cancelled"}
    assert after["control"] == "pong"  # nothing more for that run


def test_a_new_turn_supersedes_the_reply_in_progress(client, device, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    device_id, token = device
    set_key(db, secrets_key)
    calls = 0

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "Prima risposta. "
            await asyncio.sleep(30)
        yield "Seconda risposta."

    fake_models.default = reply
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        first, second = run("raccontami"), run("lascia stare, che ore sono?")
        app.send(first)
        app.until(lambda f: f.get("event", {}).get("type") == "TEXT_MESSAGE_CONTENT")
        app.send(second)
        frames = app.run_ends(second["input"]["runId"])
        app.check()
    first_end = [f["event"] for f in frames if f.get("runId") == first["input"]["runId"]][-1]
    assert first_end["type"] == "RUN_FINISHED" and first_end["outcome"] == {"type": "cancelled"}
    assert said([f for f in frames if f.get("runId") == second["input"]["runId"]]) == "Seconda risposta."


def test_a_turn_s_failure_is_its_run_s_error(client, device) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:  # no provider key set
        app = opened(ws, hello(device_id))
        turn = run("ciao")
        app.send(turn)
        frames = app.run_ends(turn["input"]["runId"])
        app.send({"op": "run", "input": {"threadId": "thread-1", "runId": "run-empty", "messages": []}})
        refused = app.run_ends("run-empty")
        ws.send_text(json.dumps({"op": "nonsense"}))
        bad = app.receive()
        app.check()
    error = frames[-1]["event"]
    assert error["type"] == "RUN_ERROR" and error["code"] == "provider_key_missing"
    assert refused[-1]["event"]["code"] == "invalid_frame"
    assert bad["control"] == "error" and bad["code"] == "invalid_frame"


def test_app_events_reach_the_session_s_handlers(client, device, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        location = {"lat": 45.413, "lon": 9.199, "accuracyM": 12.5, "measuredAt": "2026-10-20T11:00:00Z"}
        app.send({"op": "custom", "name": "pisa.location", "value": location})
        app.send(
            {
                "op": "custom",
                "name": "pisa.presence",
                "value": {"event": "app_foreground", "state": {"foreground": True, "route": "airpods"}},
            }
        )
        app.send({"op": "ping"})
        assert app.receive()["control"] == "pong"
        app.check()
        state = db.execute(
            "SELECT app, state->>'route' FROM device_presence WHERE device_id = %s", (device_id,)
        ).fetchone()
        assert state == ("foreground", "earbuds")  # AirPods: the session's earbuds
    row = db.execute("SELECT device_id::text, lat, lon, accuracy_m FROM device_locations").fetchone()
    assert row == (device_id, 45.413, 9.199, 12.5)


def test_a_presence_with_no_event_stores_its_state_alone(client, device, db) -> None:  # type: ignore[no-untyped-def]
    """Contract 2.4 (build 67 review 2): `event` is optional. A route change with no event of its own
    sends state alone: the state is kept, the app's field is unchanged, no error frame."""
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        app.send({"op": "custom", "name": "pisa.presence", "value": {"event": "app_foreground"}})
        app.send({"op": "custom", "name": "pisa.presence", "value": {"state": {"route": "car"}}})
        app.send({"op": "ping"})
        assert app.receive()["control"] == "pong"  # no error before it
        app.check()
        state = db.execute(
            "SELECT app, state->>'route' FROM device_presence WHERE device_id = %s", (device_id,)
        ).fetchone()
        assert state == ("foreground", "car")


def test_a_notice_is_a_short_run_on_the_notices_thread(client, device) -> None:  # type: ignore[no-untyped-def]
    from pisa.ports.push import OutgoingNotification

    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        registry = client.app.state.sessions
        session = registry.session(uuid.UUID(device_id))
        notice = OutgoingNotification(
            id=str(uuid.uuid4()),
            kind="reminder",
            text="Il dentista alle 15.",
            lang="it-IT",
            priority="normal",
            wrist="Promemoria",
        )
        assert client.portal.call(registry.send_notify, session, notice)
        frames = app.until(lambda f: f.get("event", {}).get("type") == "RUN_FINISHED")
        app.check()
    assert {f["threadId"] for f in frames} == {f"pisa-notices:{device_id}"}
    value = events(frames, "CUSTOM")[0]["value"]
    assert (
        value["noticeId"] == notice.id
        and value["text"] == "Il dentista alle 15."
        and value["wrist"] == "Promemoria"
    )


def test_a_hello_without_pisa_notice_never_gets_one_but_the_notice_s_words(client, device) -> None:  # type: ignore[no-untyped-def]
    """CONTRACT-2 §4.2: what the welcome did not grant is never sent. The notice still reaches the user, as
    Pisa's words on the notices thread (the contract says nothing of a fallback; the user never loses it)."""
    from pisa.ports.push import OutgoingNotification

    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id, extensions={"pisa.surface": 1, "pisa.approval": 1}))
        assert app.got[0]["extensions"] == {"pisa.surface": 1, "pisa.approval": 1}
        registry = client.app.state.sessions
        session = registry.session(uuid.UUID(device_id))
        notice = OutgoingNotification(
            id=str(uuid.uuid4()),
            kind="reminder",
            text="Il dentista alle 15.",
            lang="it-IT",
            priority="normal",
        )
        assert client.portal.call(registry.send_notify, session, notice)
        frames = app.until(lambda f: f.get("event", {}).get("type") == "RUN_FINISHED")
        app.check()
    assert {f["threadId"] for f in frames} == {f"pisa-notices:{device_id}"}
    assert events(frames, "CUSTOM") == [] and said(frames) == "Il dentista alle 15."
    start = events(frames, "TEXT_MESSAGE_START")[0]
    assert start["messageId"] == f"notice:{notice.id}" and start["metadata"]["pisa"]["lang"] == "it-IT"


def test_a_resume_replays_what_the_app_missed_exactly_once(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = "Prima frase. Seconda frase."
    with connect(client, token) as ws:
        first = opened(ws, hello(device_id))
        turn = run("ciao")
        first.send(turn)
        frames = first.run_ends(turn["input"]["runId"])
        first.check()
    session_id = first.got[0]["sessionId"]
    seen = frames[2]["seq"]  # the app lost everything after its third frame
    with connect(client, token) as ws:
        again = opened(ws, hello(device_id, resume={"sessionId": session_id, "lastSeq": seen}))
        welcome = again.got[0]
        assert welcome["replayed"] is True and welcome["sessionId"] == session_id and welcome["seq"] == seen
        replayed = [again.receive() for _ in frames[3:]]
        assert replayed == frames[3:]  # each frame once, in order
        again.send({"op": "ping"})
        pong = again.receive()
        assert pong == {"control": "pong", "seq": frames[-1]["seq"] + 1}  # the session's seq goes on
    # what the app saw, as one stream: up to its last frame, then the resumed socket's
    seen_by_app = App(None, again.hello)
    seen_by_app.got = first.got[: first.got.index(frames[2]) + 1] + again.got[1:]
    seen_by_app.check()
    with connect(client, token) as ws:  # a session it cannot resume: a new one, the app reloads over REST
        fresh = opened(ws, hello(device_id, resume={"sessionId": "sess-unknown", "lastSeq": 3}))
        assert fresh.got[0]["replayed"] is False and fresh.got[0]["sessionId"] != session_id
        assert fresh.got[0]["seq"] == 0


def test_a_newer_connection_replaces_the_older_with_a_named_error(client, device) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as old:
        older = opened(old, hello(device_id))
        with connect(client, token) as new:
            opened(new, hello(device_id))
            replaced = older.receive()
            assert replaced["control"] == "error" and replaced["code"] == "replaced"
            with pytest.raises(WebSocketDisconnect) as closed:
                old.receive_text()
            assert closed.value.code == 4001
        older.check()
