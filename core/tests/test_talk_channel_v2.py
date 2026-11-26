"""The talk's data channel in contract 2 (CONTRACT-2 §3.3), and the voice enrolment's (G5).

Every frame is checked with the conformance kit (`protocol/v2/conformance`): each server frame against the
official AG-UI 1.0 schema, A2UI 1.0 with the catalog `core:1` and `pisa-contract-2.schema.json`; then the
AG-UI lifecycle per thread and `seq` from 1. The talk's v1 messages map as the kit's gap fixtures pair them,
the kit's recorded talk stream comes out of the same v1 messages, a fake talk run's whole channel passes, and
an enrolment goes round on the channel end to end. A channel opened without the subprotocol keeps v1."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pisa_conformance.cli import check_enrol_frames, check_server_frames
from pisa_conformance.validate import (
    check_lifecycle,
    check_seq,
    client_frame_errors,
    enrol_client_frame_errors,
    enrol_server_frame_errors,
    server_frame_errors,
)
from pydantic import SecretStr

from pisa.api import talk_channel
from pisa.api.talk_channel import (
    SUBPROTOCOL,
    EnrolChannel,
    TalkChannel,
    enrol_dialects,
    enrol_frame,
    enrol_message,
    talk_dialects,
)
from pisa.domain.protocol_versions import MCP_APPS
from pisa.domain.whisper import Fresh, Kind
from pisa.infra.keys import KeyStore
from pisa.ports.talker import AskBrain, OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services import a2ui
from pisa.services import voice_id as vid
from pisa.services.talk import Hooks, TalkRun, TalkScreen, Thought
from pisa.services.talk_media import MediaPeer, RefusedChannel
from tests import voice_synth as vs
from tests.test_live_talk import FakeLink, FakeSession, _until
from tests.virtual_time import run_talk

ROOT = Path(__file__).resolve().parents[2]
V2 = ROOT / "protocol" / "v2" / "fixtures"
V1 = ROOT / "protocol" / "fixtures" / "valid"
DEVICE = "00000000-0000-4000-8000-0000000000d5"
THREAD = f"conversation:{DEVICE}"


def channel(thread: str = THREAD, *, mcp_apps: bool = True) -> tuple[TalkChannel, list[dict[str, Any]]]:
    """A talk's channel; `mcp_apps`: the phone declared MCP Apps when it opened the talk (contract 2.4)."""
    frames: list[dict[str, Any]] = []
    ch = TalkChannel(DEVICE, thread, "it-IT", mcp_apps=mcp_apps)
    ch.attach(lambda text: frames.append(json.loads(text)))
    return ch, frames


def checked(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The kit's checks on a talk channel: each frame, the lifecycle per thread, and seq from 1."""
    problems = [f"seq {f.get('seq')}: {e}" for f in frames for e in server_frame_errors(f)]
    problems += check_lifecycle([f for f in frames if "event" in f])
    problems += check_seq(frames, 1)
    assert problems == [], problems[:10]
    return frames


def events(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f["event"] for f in frames if "event" in f]


def said(text: str, final: bool) -> dict[str, Any]:
    return {"type": "talk.said", "text": text, "final": final}


def v1_present(name: str) -> dict[str, Any]:
    body = json.loads((V1 / name).read_text())
    message = {"type": "talk.present", "id": body["present_id"], "object": body["object"]}
    return message | {k: body[k] for k in ("purpose", "focus") if body.get(k)}


def gap(name: str) -> dict[str, Any]:
    return json.loads((V2 / "gaps" / "valid" / name).read_text())


# --- the talk's messages as contract 2 ----------------------------------------------------------------------


def test_every_talk_message_becomes_contract_2_frames_that_pass_the_kit() -> None:
    ch, frames = channel()
    ch.out({"type": "talk.state", "state": "listening"})
    ch.out({"type": "talk.gate", "heard": "owner"})
    ch.out({"type": "talk.transcript", "text": "Che", "final": False})
    ch.out({"type": "talk.transcript", "text": "Che treno prendo?", "final": True})
    ch.out({"type": "talk.state", "state": "thinking"})
    for name in sorted(p.name for p in V1.glob("present_*.json")):
        ch.out(v1_present(name))
    ch.out({"type": "talk.state", "state": "speaking"})
    for text, final in (("Il", False), ("Il treno", False), ("Il treno delle 17:10.", True)):
        ch.out(said(text, final))
    ch.out({"type": "talk.dismiss", "id": v1_present("present_choice.json")["id"]})
    ch.out({"type": "talk.transcript", "text": "", "final": True, "ignored": "fragment"})
    ch.out({"type": "talk.transcript", "text": "eh", "final": True, "ignored": "noise"})
    ch.out({"type": "talk.state", "state": "idle"})
    ch.out({"type": "talk.ended", "reason": "owner"})
    ch.out({"type": "talk.state", "state": "idle"})  # after the end: nothing more on the channel
    checked(frames)
    evs = events(frames)
    assert frames[0]["threadId"] == THREAD and {f["threadId"] for f in frames} == {THREAD}
    assert [e["type"] for e in evs].count("RUN_STARTED") == 1  # one exchange, one run
    text = [e["delta"] for e in evs if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert text == ["Il", " treno", " delle 17:10."]  # deltas: only what is new
    start = next(e for e in evs if e["type"] == "TEXT_MESSAGE_START")
    assert start["metadata"] == {"pisa": {"lang": "it-IT", "priority": "normal"}}
    kinds = {e.get("activityType") for e in evs if e["type"] == "ACTIVITY_SNAPSHOT"}
    assert kinds == {"a2ui-surface", "mcp-apps"}  # a drawing is a Ring 2 view
    deleted = [
        op for e in evs if e["type"] == "ACTIVITY_SNAPSHOT" for op in e["content"].get("a2ui_operations", [])
    ]
    assert any("deleteSurface" in op for op in deleted)
    assert evs[-1]["type"] == "RUN_FINISHED" and evs[-1]["result"] == {"talkEnded": "owner"}
    assert evs[-2] == evs[-2] | {"name": "pisa.voice.state", "value": {"state": "idle"}}


def test_the_user_s_next_words_start_the_next_run_and_a_barge_in_cancels_the_one_speaking() -> None:
    ch, frames = channel()
    ch.out({"type": "talk.transcript", "text": "Ciao", "final": True})
    ch.out(said("Ciao!", True))
    ch.out({"type": "talk.transcript", "text": "Che ore", "final": False})  # the next exchange
    ch.out({"type": "talk.transcript", "text": "Che ore sono?", "final": True})
    ch.out(said("Sono le", False))
    ch.out({"type": "talk.transcript", "text": "aspetta", "final": False})  # over Pisa's line: same run
    ch.out({"type": "talk.playout_stop"})  # the gate heard the user over Pisa
    ch.out(said("Sono le 15:40.", True))  # the rest of a cut line is a new run's own message
    ch.out({"type": "talk.ended", "reason": "idle"})
    checked(frames)
    finished = [e for e in events(frames) if e["type"] == "RUN_FINISHED"]
    assert [f["outcome"]["type"] for f in finished] == ["success", "cancelled", "success"]
    assert finished[-1]["result"] == {"talkEnded": "idle"}
    assert len({f["runId"] for f in frames}) == 3


def test_a_talk_that_ends_at_once_still_ends_its_one_run_and_an_unknown_reason_is_an_error() -> None:
    ch, frames = channel()
    ch.out({"type": "talk.ended", "reason": "moon"})
    evs = events(checked(frames))
    assert [e["type"] for e in evs] == ["RUN_STARTED", "RUN_FINISHED"]
    assert evs[-1]["result"] == {"talkEnded": "error"}


def test_the_kit_s_recorded_talk_comes_out_of_the_same_v1_messages() -> None:
    """`streams/talk_gated.jsonl` (G4, G5) from v1's messages: the same events in the same order, the same
    voice values, one run ending with the talk's reason."""
    recorded = [json.loads(line) for line in (V2 / "streams" / "talk_gated.jsonl").read_text().splitlines()]
    choice = {
        "object_id": "talk:choice-1",
        "kind": "choice",
        "revision": 0,
        "data": {
            "question": "Which train?",
            "options": [{"id": "o1", "label": "The 17:10"}, {"id": "o2", "label": "The 17:40"}],
        },
    }
    ch, frames = channel("thread-conversation")
    for message in (
        {"type": "talk.state", "state": "listening"},
        {"type": "talk.gate", "heard": "owner"},
        {"type": "talk.transcript", "text": "Which train should I take?", "final": True},
        {"type": "talk.state", "state": "thinking"},
        {
            "type": "talk.present",
            "id": "show-1",
            "object": choice,
            "purpose": "Pick a train",
            "focus": "offer",
        },
        {"type": "talk.state", "state": "speaking"},
        said("The 17:10 or the 17:40?", True),
        {"type": "talk.gate", "heard": "other"},
        {"type": "talk.state", "state": "idle"},
        {"type": "talk.ended", "reason": "owner"},
    ):
        ch.out(message)
    checked(frames)

    def shape(e: dict[str, Any]) -> tuple[Any, ...]:
        return (
            e["type"],
            e.get("name"),
            e.get("value"),
            e.get("delta"),
            e.get("result"),
            e.get("activityType"),
        )

    assert [shape(e) for e in events(frames)] == [shape(f["event"]) for f in recorded]
    surface = next(e for e in events(frames) if e["type"] == "ACTIVITY_SNAPSHOT")
    assert surface["messageId"] == "talk:choice-1"
    assert surface["content"]["pisa"]["purpose"] == "Pick a train"


@pytest.mark.parametrize(
    "name",
    [
        "g4_talk_ended_owner.json",
        "g4_talk_ended_fallback.json",
        "g4_talk_ended_route.json",
        "g5_voice_gate.json",
        "transcript_ignored_fragment.json",
        "transcript_ignored_language.json",
    ],
)
def test_each_talk_gap_fixture_s_v1_message_maps_to_its_frame(name: str) -> None:
    fx = gap(name)
    ch, frames = channel()
    ch.out(fx["v1"])
    checked(frames)
    want = fx["frame"]["event"]
    got = next(e for e in events(frames) if e["type"] == want["type"] and e["type"] != "RUN_STARTED")
    for key in ("name", "value", "result", "outcome"):
        assert got.get(key) == want.get(key), key


# --- the phone's frames ------------------------------------------------------------------------------------


def test_the_phone_s_frames_become_the_talk_s_messages() -> None:
    ch, frames = channel()
    ch.out({"type": "talk.state", "state": "speaking"})
    ch.out(v1_present("present_choice.json"))
    choice = v1_present("present_choice.json")
    surface = a2ui.surface_id_for(choice["object"]["object_id"])
    run_id = frames[0]["runId"]

    tap = {"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.choose", "surfaceId": surface,
           "sourceComponentId": "root", "timestamp": "2026-11-18T10:00:00Z",
           "context": {"optionId": "o1"}}}}  # fmt: skip
    ask = {"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.ask", "surfaceId": surface,
           "sourceComponentId": "root", "timestamp": "2026-11-18T10:00:00Z",
           "context": {"elementId": "e1", "label": "Milano"}}}}  # fmt: skip
    route = gap("g2_presence_route_airpods.json")
    for frame in (tap, ask, route["frame"], {"op": "cancel", "runId": run_id}):
        assert client_frame_errors(frame) == [], frame
    assert ch.into(tap) == [{"type": "talk.choose", "id": choice["id"], "choice": "o1"}]
    assert ch.into(ask) == [{"type": "talk.ask", "id": choice["id"], "element_id": "e1"}]
    assert ch.into(route["frame"]) == [route["v1"]]  # G2
    assert ch.into({"op": "cancel", "runId": "run-other"}) == []  # not the run in progress
    assert ch.into({"op": "cancel", "runId": run_id}) == [{"type": "talk.stop"}]
    assert ch.into({"op": "cancel", "runId": run_id}) == []  # it ended: once
    assert ch.into({"op": "ping"}) == []
    ack = {"op": "custom", "name": "pisa.surface.ack", "value": {"surfaceId": surface, "status": "shown"}}
    assert ch.into(ack) == []
    for refused in (
        {"op": "run", "input": {}},
        {"op": "custom", "name": "pisa.voice.enrol.finish", "value": {}},  # G5: only on an enrolment
        {"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.undo", "surfaceId": surface}}},
        {"op": "teleport"},
    ):
        assert ch.into(refused) == []
    checked(frames)
    controls = [f for f in frames if "control" in f]
    assert [c["control"] for c in controls] == ["pong", "error", "error", "error", "error"]
    assert {c["code"] for c in controls[1:]} == {"invalid_frame"}
    assert [e["outcome"]["type"] for e in events(frames) if e["type"] == "RUN_FINISHED"] == ["cancelled"]


def test_a_tap_never_decides_an_approval_on_the_talk_s_channel() -> None:
    ch, frames = channel()
    item = {"object_id": "approval:a1", "kind": "item", "revision": 0,
            "ref": {"kind": "approval", "ref": "approval:7d1c1ad0-0000-4000-8000-000000000001"}}  # fmt: skip
    ch.out({"type": "talk.present", "id": "show-1", "object": item, "focus": "take"})
    checked(frames)
    surface = a2ui.surface_id_for("approval:a1")
    yes = {"op": "a2ui", "message": {"version": "1.0", "action": {"name": "pisa.choose", "surfaceId": surface,
           "context": {"optionId": "yes"}}}}  # fmt: skip
    assert ch.into(yes) == []


# --- a fake talk run, recorded on a contract 2 channel ----------------------------------------------------


class ChannelLink(FakeLink):
    """The fake phone with a dialect in between: the talk's messages out through it, the phone's frames in."""

    def __init__(self, dialect: Any) -> None:
        super().__init__()
        self.dialect = dialect
        self.frames: list[dict[str, Any]] = []
        dialect.attach(lambda text: self.frames.append(json.loads(text)))

    def send(self, message: dict[str, Any]) -> None:
        super().send(message)
        self.dialect.out(message)

    def phone(self, frame: dict[str, Any]) -> None:
        for message in self.dialect.into(frame):
            self.messages.put_nowait(message)


def test_a_recorded_talk_from_a_fake_talk_run_passes_the_kit() -> None:
    session, seen = FakeSession(), []
    link = ChannelLink(TalkChannel(DEVICE, THREAD, "it-IT"))

    async def think(words: str, trace: str, yes_by_code: bool, origin: str = "words") -> Thought:
        return Thought("Domani alle 9 hai il dentista.", Kind.SAY)

    async def fresh() -> Fresh:
        return Fresh("adesso")

    async def meter() -> bool:
        return True

    async def exchange(e: Any) -> None:
        seen.append(e)

    async def chosen(object_id: str, option: str) -> str | None:
        seen.append(("chosen", object_id, option))
        return "il primo"

    hooks = Hooks(think=think, fresh=fresh, meter=meter, exchange=exchange, chosen=chosen)
    screen = TalkScreen(link, frozenset(), "it-IT")
    run = TalkRun(uuid.uuid4(), session, link, hooks, "it-IT", Fresh("adesso"), screen=screen)
    choice = v1_present("present_choice.json")

    async def go() -> str:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerSpeaking(True))
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(AskBrain("c1", "cosa ho domani"))
        session.queue.put_nowait(OwnerWords("cosa ho", final=False))
        session.queue.put_nowait(OwnerWords("cosa ho domani?", final=True))
        await _until(lambda: session.answers)
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Domani alle 9", final=False))
        session.queue.put_nowait(PisaWords("Domani alle 9 hai il dentista.", final=True))
        session.queue.put_nowait(Speaking(False))
        await _until(lambda: any(m["type"] == "talk.said" for m in link.sent))
        await screen.present({"present_id": choice["id"], "object": choice["object"], "focus": "offer"})
        surface = a2ui.surface_id_for(choice["object"]["object_id"])
        option = choice["object"]["data"]["options"][0]["id"]
        link.phone({"op": "a2ui", "message": {"version": "1.0", "action": {
            "name": "pisa.choose", "surfaceId": surface, "context": {"optionId": option}}}})  # fmt: skip
        await _until(lambda: any(s[0] == "chosen" for s in seen if isinstance(s, tuple)))
        session.queue.put_nowait(Speaking(True))
        session.queue.put_nowait(PisaWords("Va bene, il primo", final=False))
        speaking = [f for f in link.frames if "Va bene" in f.get("event", {}).get("delta", "")]
        await _until(lambda: speaking or speaking.extend(
            f for f in link.frames if "Va bene" in f.get("event", {}).get("delta", "")))  # fmt: skip
        link.buffered_ms = 400  # Pisa is speaking on the phone: the user touches to stop it
        link.phone({"op": "cancel", "runId": speaking[0]["runId"]})
        await _until(lambda: session.stopped)
        link.phone({"op": "ping"})
        run.end("owner")
        return await task

    assert run_talk(go()) == "owner"
    frames = checked(link.frames)
    evs = events(frames)
    assert ("chosen", choice["object"]["object_id"], choice["object"]["data"]["options"][0]["id"]) in seen
    outcomes = [e["outcome"]["type"] for e in evs if e["type"] == "RUN_FINISHED"]
    assert "cancelled" in outcomes and outcomes[-1] == "success"
    assert evs[-1]["result"] == {"talkEnded": "owner"}
    names = {e.get("name") for e in evs}
    assert {"pisa.voice.state", "pisa.voice.transcript"} <= names
    assert any(e["type"] == "ACTIVITY_SNAPSHOT" for e in evs)
    assert any(f.get("control") == "pong" for f in frames)
    # the same run spoke v1 to a v1 phone: every message went through the one pipeline
    assert [m["type"] for m in link.sent][-1] == "talk.ended"


# --- the enrolment's channel (G5) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "g5_enrol_quality.json",
        "g5_enrol_test_result.json",
        "g5_enrol_done_ok.json",
        "g5_enrol_done_missing.json",
        "g5_enrol_done_consent.json",
    ],
)
def test_each_enrolment_gap_fixture_s_v1_message_maps_to_its_frame(name: str) -> None:
    fx = gap(name)
    frame = enrol_frame(fx["v1"], fx["frame"]["seq"])
    assert frame is not None and enrol_server_frame_errors(frame) == []
    assert frame["seq"] == fx["frame"]["seq"]
    assert {k: v for k, v in frame["event"].items() if k != "timestamp"} == fx["frame"]["event"]


@pytest.mark.parametrize(
    "name",
    [
        "g5_enrol_client_block.json",
        "g5_enrol_client_test.json",
        "g5_enrol_client_finish.json",
        "g5_enrol_client_cancel.json",
    ],
)
def test_each_enrolment_client_fixture_becomes_its_v1_message(name: str) -> None:
    fx = gap(name)
    assert enrol_client_frame_errors(fx["frame"]) == []
    assert enrol_message(fx["frame"]) == fx["v1"]


def test_an_enrolment_frame_never_carries_a_reason_it_cant_and_a_verdict_not_reliable_says_so() -> None:
    weak = {"type": "enrol.done", "route": "airpods", "ok": False, "owner_false_reject": 0.4,
            "strangers_per_min": 0.0, "by_snr": {"clean": 0.9, "10": 0.6, "5": 0.5, "0": 0.3}}  # fmt: skip
    frame = enrol_frame(weak, 1)
    assert frame is not None and enrol_server_frame_errors(frame) == []
    assert frame["event"]["value"]["reason"] == "not_reliable"
    assert enrol_frame({"type": "talk.state", "state": "idle"}, 1) is None
    assert enrol_message({"op": "custom", "name": "pisa.presence", "value": {}}) is None
    assert enrol_message({"op": "ping"}) is None


def test_the_kit_s_enrolment_and_voice_fixtures_pass() -> None:
    for path in sorted((V2 / "enrol").glob("*.jsonl")):
        frames = [json.loads(line) for line in path.read_text().splitlines()]
        assert check_enrol_frames(frames, path.name).problems == [], path.name
    talk = [json.loads(line) for line in (V2 / "streams" / "talk_gated.jsonl").read_text().splitlines()]
    assert check_server_frames(talk, "talk_gated", first_seq=1).problems == []
    checks = {
        "server_frame": server_frame_errors,
        "client_frame": client_frame_errors,
        "enrol_server_frame": enrol_server_frame_errors,
        "enrol_client_frame": enrol_client_frame_errors,
    }
    for kind, want in (("valid", True), ("invalid", False)):
        for path in sorted((V2 / "gaps" / kind).glob("*.json")):
            if not path.name.startswith(("g4_", "g5_", "transcript_")):
                continue
            fx = json.loads(path.read_text())
            assert (checks[fx["check"]](fx["frame"]) == []) is want, path.name


def test_the_enrolment_channel_replays_the_kit_s_client_session_in_order() -> None:
    ch = EnrolChannel()
    frames: list[dict[str, Any]] = []
    ch.attach(lambda text: frames.append(json.loads(text)))
    client = [json.loads(line) for line in (V2 / "enrol" / "client_session.jsonl").read_text().splitlines()]
    messages = [m for frame in client for m in ch.into(frame)]
    assert [m["type"] for m in messages] == ["enrol.block"] * 10 + ["enrol.test"] * 2 + ["enrol.finish"]
    assert messages[0] == {"type": "enrol.block", "block": "A", "state": "start"}
    for line in (V2 / "enrol" / "server_ok.jsonl").read_text().splitlines():
        event: dict[str, Any] = json.loads(line)["event"]
        name = str(event["name"]).removeprefix("pisa.voice.")  # the v1 type: enrol.quality, ...
        snake = {
            "voicedS": "voiced_s",
            "snrDb": "snr_db",
            "acceptedOwner": "accepted_owner",
            "ownerTotal": "owner_total",
            "acceptedStrangers": "accepted_strangers",
            "strangersTotal": "strangers_total",
            "bySnr": "by_snr",
            "ownerFalseReject": "owner_false_reject",
            "strangersPerMin": "strangers_per_min",
        }
        ch.out({"type": name, **{snake.get(str(k), str(k)): v for k, v in event["value"].items()}})
    assert check_enrol_frames(frames, "replayed").problems == []
    recorded = [json.loads(line) for line in (V2 / "enrol" / "server_ok.jsonl").read_text().splitlines()]
    assert [(f["seq"], f["event"]["name"], f["event"]["value"]) for f in frames] == [
        (f["seq"], f["event"]["name"], f["event"]["value"]) for f in recorded
    ]


def test_an_enrolment_goes_round_on_a_contract_2_channel(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The app's enrolment frames in, the enrolment's verdicts out, end to end with fake models and generated
    voices: every frame the kit's, seq from 1, no thread and no run."""
    from tests.test_voice_id_db import _pool

    owner = vs.speaker(1)
    link = ChannelLink(EnrolChannel())

    async def go() -> str:
        pool = await _pool(db)
        service = vid.VoiceIdService(pool, KeyStore(SecretStr(secrets_key)), vs.fake_models)
        await service.store.consent("2026-11-08", None)
        emb = vs.SpectralEmbedder()
        await vs.save_cohort(
            pool, "spectral", [("it", emb.embed(vs.say(vs.speaker(900 + i), 2.0, 0))) for i in range(40)]
        )
        await vs.save_babble(pool, [("it,fr", 4, vs.say(vs.speaker(700 + i), 8.0, 0)) for i in range(3)])
        e = await service.start_enrolment(uuid.uuid4(), "airpods", link.send)
        audio: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def stream() -> Any:
            while (pcm := await audio.get()) is not None:
                yield pcm

        async def close() -> None:
            return None

        task = asyncio.create_task(vid.run_enrolment(e, stream, link.messages, close))
        for block in ("A", "B", "C", "D"):
            link.phone(
                {
                    "op": "custom",
                    "name": "pisa.voice.enrol.block",
                    "value": {"block": block, "state": "start"},
                }
            )
            await asyncio.sleep(0.01)
            for k in range(14 if block == "C" else 10):
                audio.put_nowait(
                    vid.float_to_pcm(vs.sentence(owner, ord(block) * 100 + k, seconds=2.5, pad=0.5))
                )
            await _until(lambda: audio.empty())
            await asyncio.sleep(0.01)
            link.phone(
                {"op": "custom", "name": "pisa.voice.enrol.block", "value": {"block": block, "state": "end"}}
            )
            await _until(lambda b=block: any(f["event"]["value"].get("block") == b for f in link.frames), 20)
        audio.put_nowait(vid.float_to_pcm(np.zeros(16_000, dtype=np.float32)))
        link.phone({"op": "custom", "name": "pisa.voice.enrol.finish", "value": {}})
        outcome = await asyncio.wait_for(task, 60)
        await pool.close()
        return outcome

    assert asyncio.run(go()) == "done"
    assert check_enrol_frames(link.frames, "enrolment").problems == []
    names = [f["event"]["name"] for f in link.frames]
    assert names == ["pisa.voice.enrol.quality"] * 4 + ["pisa.voice.enrol.done"]
    done = link.frames[-1]["event"]["value"]
    # the fake models leave the user above the guard: prints made, the gate can't rely on them
    assert done["ok"] is False and done["reason"] == "not_reliable" and "ownerFalseReject" in done
    # the contract's four conditions; with no 60-second check, quiet is the +10 dB rate (a lower bound)
    assert set(done["bySnr"]) == {"clean", "10", "5", "0"} and done["bySnr"]["clean"] == done["bySnr"]["10"]
    (v1,) = [m for m in link.sent if m["type"] == "enrol.done"]
    assert "-5" in v1["by_snr"] and "reason" not in v1  # v1 as it was, plus its quiet rate
    assert all("threadId" not in f and "runId" not in f for f in link.frames)


def test_the_live_socket_refuses_an_enrolment_frame() -> None:
    """G5: `pisa.voice.enrol.*` belongs to an enrolment's channel only, never to `/v2/live`."""
    from pisa.services.session import FrameError
    from tests.test_live_v2_frames import session

    fx = json.loads((V2 / "gaps" / "invalid" / "g5_enrol_client_on_live_socket.json").read_text())
    with pytest.raises(FrameError):
        asyncio.run(session().frame(fx["frame"]))


# --- the channel's dialect is chosen by its subprotocol -------------------------------------------------


def test_the_subprotocol_chooses_the_dialect_and_no_other_is_spoken() -> None:
    talk = talk_dialects(DEVICE, THREAD, "it-IT")
    assert isinstance(talk(SUBPROTOCOL), TalkChannel) and talk("") is None and talk("pisa.v9") is None
    enrol = enrol_dialects()
    assert isinstance(enrol(SUBPROTOCOL), EnrolChannel) and enrol("") is None
    refused, sent = RefusedChannel(), []  # what a channel without `pisa.v2` gets: nothing either way
    refused.attach(sent.append)
    refused.out({"type": "talk.state", "state": "idle"})
    assert sent == [] and refused.into({"type": "talk.stop"}) == []


@pytest.mark.parametrize("subprotocol", ["", SUBPROTOCOL])
def test_a_real_webrtc_channel_speaks_what_the_phone_opened_it_with(subprotocol: str) -> None:
    """A phone's peer and the server's, on this machine: messages sent before the channel opens wait for it,
    then go out in contract 2 when the phone opened it with `pisa.v2`, and the phone's frames come in through
    it. A channel opened without the subprotocol is refused: closed, nothing sent, nothing read."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    async def go() -> tuple[list[Any], list[dict[str, Any]], str]:
        server = MediaPeer([], 16_000, talk_dialects(DEVICE, THREAD, "it-IT"))
        server.send({"type": "talk.state", "state": "listening"})  # before the channel: kept
        phone = RTCPeerConnection()
        phone.addTransceiver("audio", direction="sendrecv")
        dc = phone.createDataChannel("pisa", protocol=subprotocol)
        got: list[Any] = []
        dc.on("message", lambda m: got.append(json.loads(m)))
        await phone.setLocalDescription(await phone.createOffer())
        answer = await server.answer(phone.localDescription.sdp)
        await phone.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        for _ in range(500):
            if (dc.readyState == "open" and got) or (not subprotocol and dc.readyState == "closed"):
                break
            await asyncio.sleep(0.02)
        server.send({"type": "talk.state", "state": "thinking"})
        if subprotocol:
            dc.send(json.dumps({"op": "ping"}))
        for _ in range(250):
            if len(got) >= 4 or not subprotocol:
                break
            await asyncio.sleep(0.02)
        state = dc.readyState
        heard = []
        while not server.messages.empty():
            heard.append(server.messages.get_nowait())
        await phone.close()
        await server.close()
        return got, heard, state

    got, heard, state = asyncio.run(go())
    if subprotocol:
        checked(got)
        assert [f.get("control") or f["event"].get("name") or f["event"]["type"] for f in got] == [
            "RUN_STARTED", "pisa.voice.state", "pisa.voice.state", "pong",
        ][: len(got)] and len(got) == 4  # fmt: skip
        assert heard == []
    else:
        assert got == [] and heard == [] and state == "closed"


@pytest.mark.parametrize("code", ["budget_exceeded", "validation_failed", "internal", "provider_error"])
def test_an_enrolment_ended_by_an_error_s_code_says_error(code: str) -> None:
    frame = enrol_frame({"type": "enrol.done", "route": "speaker", "ok": False, "reason": code}, 3)
    assert frame is not None
    assert frame["event"]["value"]["reason"] == "error"
    assert enrol_server_frame_errors(frame) == []


def test_not_reliable_without_its_whole_report_says_error() -> None:
    message = {"type": "enrol.done", "route": "speaker", "ok": False, "owner_false_reject": 0.2}
    frame = enrol_frame(message, 4)
    assert frame is not None
    value = frame["event"]["value"]
    assert value["reason"] == "error" and "ownerFalseReject" not in value
    assert enrol_server_frame_errors(frame) == []


def test_not_reliable_keeps_its_report() -> None:
    by_snr = {"clean": 0.9, "10": 0.85, "5": 0.8, "0": 0.7, "-5": 0.5}
    message = {"type": "enrol.done", "route": "speaker", "ok": False, "owner_false_reject": 0.2,
               "strangers_per_min": 0.4, "by_snr": by_snr}  # fmt: skip
    frame = enrol_frame(message, 5)
    assert frame is not None
    value = frame["event"]["value"]
    assert value["reason"] == "not_reliable" and set(value["bySnr"]) == {"clean", "10", "5", "0"}
    assert enrol_server_frame_errors(frame) == []


def test_a_drawing_for_a_phone_with_no_mcp_apps_is_a_note_card() -> None:
    """Build 67 review 3 (contract 2.4): the talk's mcp-apps views follow what the phone declared on
    POST /v1/talk/sessions (`mcpApps`). Nothing declared: the drawing comes as a ring-1 note, its title
    and summary, never as a view; the other shows are unchanged."""
    ch, frames = channel(mcp_apps=False)
    drawing = v1_present("present_drawing.json")
    ch.out(drawing)
    checked(frames)
    snaps = [e for e in events(frames) if e["type"] == "ACTIVITY_SNAPSHOT"]
    assert [e.get("activityType") for e in snaps] == ["a2ui-surface"]
    text = json.dumps(snaps[0]["content"], ensure_ascii=False)
    data = drawing["object"]["data"]
    assert data["title"] in text and data["summary"][:40] in text
    assert "text/html" not in text and data["html"][:30] not in text


def test_the_talk_session_negotiates_mcp_apps_as_the_hello_does() -> None:
    assert talk_channel.mcp_apps_negotiated([MCP_APPS], ["text/html;profile=mcp-app"]) is True
    assert talk_channel.mcp_apps_negotiated(None, None) is False
    assert talk_channel.mcp_apps_negotiated(["2025-01-01"], ["text/html;profile=mcp-app"]) is False
    assert talk_channel.mcp_apps_negotiated([MCP_APPS], ["text/html"]) is False
