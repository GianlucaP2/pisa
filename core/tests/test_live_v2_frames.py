"""Contract 2 on the wire, without a socket: every message the shared pipeline sends, written as AG-UI runs by
`/v2/live`, and every object a turn shows, as A2UI 1.0 (Ring 1) or MCP Apps (Ring 2).

Every frame is checked with the conformance kit (`protocol/v2/conformance`): against the official AG-UI 1.0
schema, A2UI 1.0 with the catalog `core:1`, and `pisa-contract-2.schema.json`; then the AG-UI lifecycle per
thread and `seq`."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pisa_conformance.cli import negotiation_problems
from pisa_conformance.validate import (
    EXT_ID,
    check_lifecycle,
    check_seq,
    client_frame_errors,
    errors,
    server_frame_errors,
)

from pisa.api.live import EXTENSIONS, HelloFrame, negotiate, welcome_body
from pisa.api.runs import HTTP_NEGOTIATED
from pisa.services import a2ui
from pisa.services.agui import EXTENSION_OF, REPLAY_FRAMES, LiveSession
from pisa.services.devices import Device
from pisa.services.session import DeviceSession, Negotiated, replying_to
from pisa.services.session_messages import Say
from tests.live_app import MESSAGES, message_of

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"
# what the app's frames become (the session's messages a device sends), never written by `/v2/live`
FROM_DEVICE = frozenset(
    {"device_context", "utterance", "location", "cancel", "presence", "presented", "object_event",
     "place_enter", "handoff_opened", "drawing.ask"}
)  # fmt: skip
DEVICE = "00000000-0000-4000-8000-0000000000d1"
FULL = Negotiated([a2ui.CATALOG_ID], [0, 1, 2], True, dict(EXTENSIONS), ["aac_adts"])  # every extension


def hello(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "op": "hello",
        "contract": {"versions": ["2.0"]},
        "agui": {"protocolVersions": ["1.0"]},
        "a2ui": {"1.0": {"supportedCatalogIds": [a2ui.CATALOG_ID]}},
        "rings": [0, 1, 2],
        "mcpApps": {
            "protocolVersions": ["2026-01-26"],
            "mimeTypes": ["text/html;profile=mcp-app"],
            "bundledLibraries": ["d3@7.9.0"],
        },
        "extensions": {"pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1, "pisa.voice": 3},
        "device": {
            "deviceId": DEVICE,
            "appVersion": "1.0.0",
            "platform": "ios",
            "audioFormats": ["opus", "mp3"],
        },
    }
    return body | over


def session(negotiated: Negotiated = FULL) -> DeviceSession:
    device = Device(uuid.UUID(DEVICE), "iPhone", "ios", datetime(2026, 11, 18, tzinfo=UTC), None, None)
    return DeviceSession(object(), device, object(), LiveSession(DEVICE), negotiated)  # type: ignore[arg-type]


def checked(frames: list[dict[str, Any]], first_seq: int | None = None) -> list[dict[str, Any]]:
    """The kit's checks on a stream: each frame, the lifecycle per thread, and seq."""
    problems = [f"seq {f.get('seq')}: {e}" for f in frames for e in server_frame_errors(f)]
    problems += check_lifecycle([f for f in frames if "event" in f])
    problems += check_seq(frames, first_seq)
    assert problems == [], problems[:10]
    return frames


def server_fixtures() -> list[Path]:
    found = sorted((FIXTURES / "valid").glob("*.json"))
    found += sorted(FIXTURES.glob("proposed-*/*.json"))
    out = []
    for path in found:
        raw = json.loads(path.read_text())
        if raw.get("type") in MESSAGES and raw["type"] not in FROM_DEVICE:
            out.append(path)
    return out


@pytest.mark.parametrize("path", server_fixtures(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_message_the_pipeline_sends_is_valid_contract_2(path: Path) -> None:
    """Each message the pipeline can send (samples of each in the protocol's fixtures) goes out as frames
    that the kit passes."""
    message = message_of(path.read_text())
    s = session()
    stream = getattr(message, "stream_id", None)
    if message.type == "say_audio" and stream:  # a reply's audio goes in that reply's run
        s._turn(stream, "t", "run-1")
        s.reply_started(stream)
        s._streams[stream] = stream
    sent = s._write_message(message)
    if message.type == "say_audio" and stream:
        s.reply_ended(stream, cancelled=False)
    frames = checked(s.live.kept(), 1 if s.live.kept() else None)
    assert sent and frames, message.type
    assert not s.live.runs  # every run it opened is closed


def test_every_message_the_session_sends_is_covered() -> None:
    covered = {json.loads(p.read_text())["type"] for p in server_fixtures()}
    assert set(MESSAGES) - FROM_DEVICE == covered


def test_a_reply_is_one_run_with_its_words_steps_and_what_it_showed() -> None:
    s = session()
    utterance_id, stream = str(uuid.uuid4()), str(uuid.uuid4())
    s._turn(utterance_id, "thread-1", "run-app-1", "msg-app-1")
    s.reply_started(utterance_id)
    token = replying_to.set(utterance_id)
    steps = s.turn_senders(utterance_id)["steps"]
    import asyncio

    asyncio.run(steps("started", "c1", "web_search"))
    asyncio.run(steps("finished", "c1", "web_search"))
    s._write_message(
        Say(
            reply_to=utterance_id,
            stream_id=stream,
            seq=0,
            delta="Cerco ",
            final=False,
            lang="it-IT",
            priority="normal",
            holding=True,
            holding_id="h1",
        )
    )
    for n, (delta, final) in enumerate([("Ecco. ", False), ("Due posti.", False), ("", True)]):
        s._write_message(
            Say(
                reply_to=utterance_id,
                stream_id=stream,
                seq=n + 1,
                delta=delta,
                final=final,
                lang="it-IT",
                priority="normal",
            )
        )
    present = message_of((FIXTURES / "valid" / "present_choice.json").read_text())
    s._write_message(present)
    replying_to.reset(token)
    s.reply_ended(utterance_id, cancelled=False)
    frames = checked(s.live.kept(), 1)
    events = [f["event"] for f in frames]
    assert {f["threadId"] for f in frames} == {"thread-1"} and {f["runId"] for f in frames} == {"run-app-1"}
    kinds = [e["type"] for e in events]
    assert kinds[0] == "RUN_STARTED" and kinds[-1] == "RUN_FINISHED"
    assert kinds[1:5] == ["STEP_STARTED", "TOOL_CALL_START", "TOOL_CALL_END", "STEP_FINISHED"]
    holding = [
        e for e in events if e["type"] == "TEXT_MESSAGE_START" and e["metadata"]["pisa"].get("holding")
    ]
    answer = [e for e in events if e["type"] == "TEXT_MESSAGE_START" and e["messageId"] == stream]
    assert len(holding) == 1 and len(answer) == 1  # the holding line is its own message (G7)
    said = "".join(
        e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT" and e["messageId"] == stream
    )
    assert said == "Ecco. Due posti."
    surface = next(e for e in events if e["type"] == "ACTIVITY_SNAPSHOT")
    assert surface["activityType"] == "a2ui-surface" and surface["content"]["pisa"]["replyTo"]


def test_a_superseded_reply_ends_cancelled_before_the_next_starts_and_sends_nothing_more() -> None:
    s = session()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    s._turn(first, "t", "run-1")
    s._turn(second, "t", "run-2")
    s.reply_started(first)
    s.reply_started(second)
    s._write_message(
        Say(
            reply_to=first,
            stream_id=str(uuid.uuid4()),
            seq=0,
            delta="old",
            final=True,
            lang="it-IT",
            priority="normal",
        )
    )
    s.reply_ended(first, cancelled=True)
    s.reply_ended(second, cancelled=False)
    events = [(f["runId"], f["event"]["type"], f["event"].get("outcome")) for f in checked(s.live.kept(), 1)]
    assert events == [
        ("run-1", "RUN_STARTED", None),
        ("run-1", "RUN_FINISHED", {"type": "cancelled"}),
        ("run-2", "RUN_STARTED", None),
        ("run-2", "RUN_FINISHED", {"type": "success"}),
    ]


def test_a_short_run_waits_for_the_conversation_s_run_in_progress() -> None:
    """An echo's empty answer while Pisa is still answering: its run comes after, never inside or across."""
    s = session()
    answering, echo = str(uuid.uuid4()), str(uuid.uuid4())
    s._turn(answering, "t", "run-1")
    s._turn(echo, "t", "run-echo")
    s.reply_started(answering)
    s._write_message(
        Say(
            reply_to=echo,
            stream_id=str(uuid.uuid4()),
            seq=0,
            delta="",
            final=True,
            lang="it-IT",
            priority="normal",
        )
    )
    assert [f["runId"] for f in s.live.kept()] == ["run-1"]
    s.reply_ended(answering, cancelled=False)
    runs = [(f["runId"], f["event"]["type"]) for f in checked(s.live.kept(), 1)]
    assert runs == [
        ("run-1", "RUN_STARTED"),
        ("run-1", "RUN_FINISHED"),
        ("run-echo", "RUN_STARTED"),
        ("run-echo", "RUN_FINISHED"),
    ]


def test_a_turn_s_failure_ends_its_run_and_one_with_no_run_is_a_control_error() -> None:
    from pisa.domain.errors import PisaError
    from pisa.services.session_messages import Error

    s = session()
    turn = str(uuid.uuid4())
    s._turn(turn, "t", "run-1")
    s.reply_started(turn)
    s._write_message(
        Error.from_error(PisaError("budget_exceeded", "Over budget.", "Raise it"), reply_to=turn)
    )
    s._write_message(
        Error.from_error(PisaError("invalid_message", "Bad location."), reply_to=str(uuid.uuid4()))
    )
    frames = checked(s.live.kept(), 1)
    assert frames[1]["event"] == {
        "type": "RUN_ERROR",
        "timestamp": frames[1]["event"]["timestamp"],
        "message": "Over budget.",
        "code": "budget_exceeded",
    }
    assert frames[2]["control"] == "error" and frames[2]["code"] == "invalid_frame"


def test_a_notice_and_its_audio_go_on_the_notices_thread_and_never_wait() -> None:
    s = session()
    notify = message_of((FIXTURES / "valid" / "notify_check_in.json").read_text())
    audio = json.loads((FIXTURES / "valid" / "say_audio.json").read_text())
    audio.pop("stream_id", None)
    audio |= {"notification_id": notify.notification_id, "final": False}  # type: ignore[attr-defined]
    first = message_of(json.dumps(audio))
    last = message_of(json.dumps(audio | {"seq": 1, "final": True}))
    s._write_message(notify)
    s._write_message(first)
    s._write_message(message_of((FIXTURES / "valid" / "notify.json").read_text()))  # ends the audio's run
    s._write_message(last)
    frames = checked(s.live.kept(), 1)
    assert {f["threadId"] for f in frames} == {f"pisa-notices:{DEVICE}"}
    names = [f["event"].get("name") for f in frames if f["event"]["type"] == "CUSTOM"]
    assert names == ["pisa.notice", "pisa.voice.audio", "pisa.notice", "pisa.voice.audio"]
    assert not s.live.runs


# --- negotiation -------------------------------------------------------------------------------------------
def test_welcome_picks_only_what_the_app_listed() -> None:
    picked = negotiate(HelloFrame.model_validate(hello()))
    assert isinstance(picked, Negotiated)
    welcome = {"control": "welcome", "seq": 0, **welcome_body(picked, "sess-1", False)}
    assert errors(welcome, EXT_ID + "#/$defs/Welcome") == []
    assert negotiation_problems(hello(), [welcome]) == []
    assert picked.extensions == {"pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1, "pisa.voice": 1}
    assert picked.audio_formats == ["mp3"] and picked.mcp_apps and picked.rings == [0, 1, 2]
    plain = hello(rings=[0, 1], a2ui={"1.0": {"supportedCatalogIds": ["urn:other:catalog"]}})
    plain.pop("mcpApps")
    assert client_frame_errors(plain) == []
    words_only = negotiate(HelloFrame.model_validate(plain))
    assert isinstance(words_only, Negotiated)
    assert words_only.catalogs == [] and words_only.rings == [0] and not words_only.mcp_apps  # Ring 1 is off
    welcome = {"control": "welcome", "seq": 0, **welcome_body(words_only, "sess-2", False)}
    assert (
        welcome["a2ui"] is None
        and welcome["mcpApps"] is None
        and negotiation_problems(plain, [welcome]) == []
    )
    assert isinstance(negotiate(HelloFrame.model_validate(hello(contract={"versions": ["3.0"]}))), str)
    # contract 2.1 is additive: an app of 2.0 is served 2.0, one that lists 2.1 is served it
    for listed, served in ((["2.0"], "2.0"), (["2.0", "2.1"], "2.1"), (["2.1"], "2.1")):
        app = hello(contract={"versions": listed})
        minor = negotiate(HelloFrame.model_validate(app))
        assert isinstance(minor, Negotiated) and minor.contract == served
        welcome = {"control": "welcome", "seq": 0, **welcome_body(minor, "sess-3", False)}
        assert welcome["contract"] == served and negotiation_problems(app, [welcome]) == []


def test_without_ring_1_nothing_is_drawn_and_without_ring_2_no_drawing() -> None:
    words = session(Negotiated([], [0], False, {}))
    assert words.screen.kinds == frozenset() and words.screen.blocks is None
    assert not words._write_message(message_of((FIXTURES / "valid" / "present_note.json").read_text()))
    assert not words._write_message(message_of((FIXTURES / "valid" / "card.json").read_text()))
    native = session(Negotiated([a2ui.CATALOG_ID], [0, 1], False, {}))
    assert "drawing" not in native.screen.kinds and "sheet" in native.screen.kinds
    assert not native._write_message(message_of((FIXTURES / "valid" / "present_drawing.json").read_text()))
    assert words.live.kept() == [] and native.live.kept() == []


# each server message that is one `pisa.*` event: its fixture, the event, its extension
EVENT_FIXTURES = {
    "understood.json": ("pisa.voice.understood", "pisa.voice"),
    "turn_hint.json": ("pisa.voice.turn_hint", "pisa.voice.turn_hints"),
    "talk_ask_to_open.json": ("pisa.ask_to_open", "pisa.voice"),
    "say_audio.json": ("pisa.voice.audio", "pisa.voice"),
    "focus.json": ("pisa.surface.focus", "pisa.surface"),
    "directions.json": ("pisa.directions", "pisa.directions"),
    "call.json": ("pisa.call", "pisa.call"),
    "compose.json": ("pisa.compose", "pisa.compose"),
    "notify_check_in.json": ("pisa.notice", "pisa.notice"),
    "action_done.json": ("pisa.action_done", "pisa.approval"),
}


def _event_message(fixture: str) -> Any:
    raw = json.loads((FIXTURES / "valid" / fixture).read_text())
    raw.pop("stream_id", None)  # a notice's audio: no reply's run to find
    if raw["type"] == "say_audio":
        raw |= {"notification_id": str(uuid.uuid4()), "final": True}
    return message_of(raw)


def test_the_extension_map_covers_every_pisa_event_the_server_sends() -> None:
    """Each `pisa.*` server event belongs to one of the contract's extensions, but the three of Ring 0."""
    contract = json.loads((FIXTURES.parent / "v2" / "schema" / "pisa-contract-2.schema.json").read_text())
    ring0 = {"pisa.ai.disclosure", "pisa.endpoints", "pisa.entitlement.required"}
    assert set(EXTENSION_OF) == set(contract["x-pisa-server-events"]) - ring0
    assert set(EXTENSION_OF.values()) <= set(contract["x-pisa-extensions"]) and set(EXTENSIONS) >= set(
        EXTENSION_OF.values()
    )


@pytest.mark.parametrize("fixture", sorted(EVENT_FIXTURES))
def test_an_event_goes_only_when_the_welcome_granted_its_extension(fixture: str) -> None:
    """CONTRACT-2 §4.2: what the welcome did not pick is never sent. Granted, the event goes; not granted,
    no event and no run, but a notice and an action's outcome, which come as Pisa's words instead."""
    name, extension = EVENT_FIXTURES[fixture]
    granted = session(Negotiated([a2ui.CATALOG_ID], [0, 1], False, {extension: 1}))
    assert granted._write_message(_event_message(fixture))
    events = [f["event"] for f in checked(granted.live.kept(), 1)]
    assert [e["name"] for e in events if e["type"] == "CUSTOM"] == [name]
    others = {k: 1 for k in EXTENSIONS if k != extension}
    bare = session(Negotiated([a2ui.CATALOG_ID], [0, 1], False, others))
    sent = bare._write_message(_event_message(fixture))
    frames = checked(bare.live.kept(), 1) if bare.live.kept() else []
    assert not [f for f in frames if f["event"]["type"] == "CUSTOM"]
    texts = [f["event"] for f in frames if f["event"]["type"] == "TEXT_MESSAGE_CONTENT"]
    if name in ("pisa.notice", "pisa.action_done"):
        message = _event_message(fixture)
        words = message.text if name == "pisa.notice" else message.summary
        assert sent and words in "".join(e["delta"] for e in texts)
        assert {f["threadId"] for f in frames} == (
            {f"pisa-notices:{DEVICE}"} if name == "pisa.notice" else {f"pisa-approval:{message.approval_id}"}
        )
    else:
        assert not sent and frames == []
    assert not bare.live.runs and not granted.live.runs


def test_without_an_action_s_extension_the_brain_gets_no_sender_for_it() -> None:
    """No `pisa.directions`, `pisa.call` or `pisa.compose`: the brain's tool says it can't open it here, and
    Pisa says it in words; never a screen promised that the app won't open."""
    full = session().turn_senders(None)
    bare = session(Negotiated([a2ui.CATALOG_ID], [0, 1], False, {"pisa.notice": 1})).turn_senders(None)
    assert all(full[k] is not None for k in ("directions", "compose", "call_phone"))
    assert all(bare[k] is None for k in ("directions", "compose", "call_phone"))
    # each phone action goes with the extension the hello granted, never with "a socket" alone
    assert {"pisa.directions", "pisa.call", "pisa.compose"} <= set(full["phone_grants"])
    assert bare["phone_grants"] == frozenset({"pisa.notice"})
    # a run over HTTP listed none: its events still go, but no phone action is counted as the client's
    http = session(HTTP_NEGOTIATED).turn_senders(None)
    assert http["compose"] is not None and http["phone_grants"] is None
    assert http["app_action"] is None and bare["app_action"] is None  # pisa.app: a socket that picked it


def test_without_pisa_voice_no_audio_format_is_picked() -> None:
    plain = hello(extensions={"pisa.notice": 1})
    picked = negotiate(HelloFrame.model_validate(plain))
    assert isinstance(picked, Negotiated) and picked.audio_formats == []
    voiced = negotiate(HelloFrame.model_validate(hello()))
    assert isinstance(voiced, Negotiated) and voiced.audio_formats == ["mp3"]


# --- the replay buffer -------------------------------------------------------------------------------------
def test_a_resume_replays_every_frame_after_the_last_seen_once() -> None:
    live = LiveSession(DEVICE)
    sent: list[str] = []
    live.attach(sent.append)
    for n in range(10):
        live.control("pong") if n % 3 else live.start_run("t").finish()
    assert live.seq == len(sent) == 14
    assert [json.loads(t) for t in live.replay_after(10) or []] == [json.loads(t) for t in sent[10:]]
    assert live.replay_after(14) == [] and live.replay_after(15) is None
    for _ in range(REPLAY_FRAMES + 5):
        live.control("pong")
    assert live.replay_after(3) is None  # no longer kept: the app reloads over REST
    kept = live.replay_after(live.seq - 10) or []
    assert [json.loads(t)["seq"] for t in kept] == list(range(live.seq - 9, live.seq + 1))


# --- the kit's recorded streams, replayed through this server's emitter (CONTRACT-2 §10, conformance 2) -----
STREAMS = sorted((FIXTURES.parent / "v2" / "fixtures" / "streams").glob("*.jsonl"))


def _replay(frames: list[dict[str, Any]]) -> LiveSession:
    """Each recorded event, made again by `LiveSession` and `RunWriter` (the same calls the socket makes)."""
    live = LiveSession(DEVICE, clock_ms=lambda: 0)
    tools: dict[str, dict[str, Any]] = {}
    for frame in frames:
        e = frame["event"]
        t = e["type"]
        if t == "RUN_STARTED":
            live.start_run(
                e["threadId"], e["runId"], parent_run_id=e.get("parentRunId"), input_=e.get("input")
            )
            continue
        w = live.runs[frame["threadId"]]
        if t == "TEXT_MESSAGE_START":
            w.text_start(e["messageId"], role=e.get("role", "assistant"), **e["metadata"]["pisa"])
        elif t == "TEXT_MESSAGE_CONTENT":
            w.text_content(e["messageId"], e["delta"])
        elif t == "TEXT_MESSAGE_END":
            w.text_end(e["messageId"])
        elif t == "TOOL_CALL_START":
            tools[e["toolCallId"]] = {
                "name": e["toolCallName"],
                "args": [],
                "parent": e.get("parentMessageId"),
            }
        elif t == "TOOL_CALL_ARGS":
            tools[e["toolCallId"]]["args"].append(e["delta"])
        elif t == "TOOL_CALL_END":
            call = tools.pop(e["toolCallId"])
            w.tool_call(e["toolCallId"], call["name"], call["args"], call["parent"])
        elif t == "TOOL_CALL_RESULT":
            w.tool_result(e["toolCallId"], e["content"], e["messageId"])
        elif t == "STEP_STARTED":
            w.step_started(e["stepName"])
        elif t == "STEP_FINISHED":
            w.step_finished(e["stepName"])
        elif t == "ACTIVITY_SNAPSHOT" and e["activityType"] == "a2ui-surface":
            w.surface(e["messageId"], e["content"], e.get("replace"))
        elif t == "ACTIVITY_SNAPSHOT":
            w.mcp_app(e["messageId"], e["content"])
        elif t == "ACTIVITY_DELTA":
            w.surface_patch(e["messageId"], e["patch"])
        elif t == "CUSTOM":
            w.custom(e["name"], e["value"])
        elif t == "RUN_FINISHED":
            w.finish(e.get("outcome"), e.get("result"))
        elif t == "RUN_ERROR":
            w.error(e["message"], e.get("code", ""))
        else:
            raise AssertionError(f"no RunWriter call makes {t}")
    return live


def _bare(frame: dict[str, Any]) -> dict[str, Any]:
    return {**frame, "event": {k: v for k, v in frame["event"].items() if k != "timestamp"}}


@pytest.mark.parametrize("path", STREAMS, ids=lambda p: p.name)
def test_the_kit_s_recorded_streams_come_out_of_this_server_s_emitter(path: Path) -> None:
    recorded = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    events_only = [f for f in recorded if "event" in f]
    made = _replay(events_only).kept()
    assert [_bare(f) for f in made] == [_bare(f) for f in events_only]  # the same frames, seq included
    if path.stem == "unknown_component":  # deliberately off the catalog: the kit refuses it, as the app must
        assert any(server_frame_errors(f) for f in made)
    else:
        checked(made, 1)


def test_the_kit_passes_its_own_selftest() -> None:
    from pisa_conformance.cli import main

    assert main(["selftest"]) == 0


# --- core:2: get_weather's WeatherSummary ------------------------------------------------------------------
CORE_2 = [a2ui.CATALOG_ID, a2ui.CATALOG_V2_ID]
FORECAST: dict[str, Any] = {  # synthetic: what get_weather hands the app (`place_tools.weather_summary`)
    "place": "Testville",
    "date": "2026-11-18",
    "condition": "Light rain",
    "now": {"temp_c": 11.0, "condition": "Drizzle"},
    "min_c": 9.2,
    "max_c": 12.6,
    "rain_chance": 80,
    "hourly": [
        {"time": f"2026-11-18T{h:02d}:00:00+01:00", "temp_c": 11.0, "rain_chance": 80, "condition": "Drizzle"}
        for h in range(13, 24)
    ],
}


def test_weather_is_a_core_2_surface_only_for_an_app_that_picked_core_2() -> None:
    picked = negotiate(HelloFrame.model_validate(hello(a2ui={"1.0": {"supportedCatalogIds": CORE_2}})))
    assert isinstance(picked, Negotiated) and picked.catalogs == CORE_2
    assert welcome_body(picked, "s", False)["a2ui"] == {"version": "1.0", "catalogIds": CORE_2}
    only_1 = negotiate(HelloFrame.model_validate(hello()))
    assert isinstance(only_1, Negotiated) and only_1.catalogs == [a2ui.CATALOG_ID]
    assert session(only_1).turn_senders(None)["weather_card"] is None  # the words carry the forecast
    assert session(HTTP_NEGOTIATED).turn_senders(None)["weather_card"] is None  # HTTP: core:1

    s = session(picked)
    s.live.attach([].append)
    import asyncio

    assert asyncio.run(s.turn_senders(None)["weather_card"](FORECAST)) is True
    frames = checked(s.live.kept(), 1)  # the kit judges the surface by core:2, the catalog it names
    assert negotiation_problems(hello(a2ui={"1.0": {"supportedCatalogIds": CORE_2}}), [
        welcome_body(s.negotiated, "s", False), *frames
    ]) == []  # fmt: skip
    (snapshot,) = [f["event"] for f in frames if f["event"]["type"] == "ACTIVITY_SNAPSHOT"]
    (made,) = snapshot["content"]["a2ui_operations"]
    assert made["createSurface"]["catalogId"] == a2ui.CATALOG_V2_ID
    (root,) = made["createSurface"]["components"]
    assert root == {
        "id": "root",
        "component": "WeatherSummary",
        "place": "Testville",
        "date": "2026-11-18",
        "condition": "Light rain",
        "now": {"tempC": 11.0, "condition": "Drizzle"},
        "minC": 9.2,
        "maxC": 12.6,
        "rainChance": 80,
        "hourly": [
            {
                "time": f"2026-11-18T{h:02d}:00:00+01:00",
                "tempC": 11.0,
                "rainChance": 80,
                "condition": "Drizzle",
            }
            for h in range(13, 24)
        ],
        "fallback": "Testville, 2026-11-18: Light rain, 9.2 to 12.6 °C, rain chance 80%",
    }
    assert snapshot["content"]["pisa"]["fallbackText"] == root["fallback"]
    # a weather card never reaches an app without core:2, even when something tries to send one
    bare = session(only_1)
    card = bare.turn_senders(None)["card"]  # any sender: the session's own check is what's tested
    assert card is not None
    from pisa.services.session_messages import Card, WeatherShown

    weather = Card(
        card_id=str(uuid.uuid4()), title="Testville", lines=[], kind="weather",
        weather=WeatherShown.model_validate(FORECAST, strict=False),
    )  # fmt: skip
    assert bare._write_message(weather) is False and bare.live.kept() == []


def test_a_weather_card_holds_at_most_24_hours() -> None:
    from pydantic import ValidationError

    from pisa.services.session_messages import WeatherShown

    too_many = FORECAST | {"hourly": FORECAST["hourly"] * 3}
    with pytest.raises(ValidationError):
        WeatherShown.model_validate(too_many, strict=False)
    with pytest.raises(ValidationError):
        WeatherShown.model_validate(FORECAST | {"rain_chance": 101}, strict=False)
    another_day = WeatherShown.model_validate(FORECAST | {"now": None}, strict=False)
    op, fallback = a2ui.card_to_surface(
        {"kind": "weather", "title": "Testville", "weather": another_day.model_dump(mode="json")},
        "card:w",
        "it-IT",
    )
    (root,) = op["createSurface"]["components"]
    assert (
        "now" not in root
        and fallback == "Testville, 2026-11-18: Light rain, da 9.2 a 12.6 °C, probabilità di pioggia 80%"
    )
