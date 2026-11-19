"""Contract 2's approvals, end to end (CONTRACT-2 §6): the interrupt on the approval's own thread of
`/v2/live`, `GET /v1/approvals/{id}`'s `payload` and `recipients` (the phone recomputes the digest from
them), the `pisa-approval/2` challenge, the resume over the socket and over REST
(`POST /v1/approvals/{id}/resume`, with the shapes of protocol/v2/openapi-v2-delta.json), and
`pisa.approval.state` when it is decided elsewhere or expires.

Every recorded stream is checked with the conformance kit (schemas, lifecycle per thread, `seq`, negotiation),
and every digest and canonical text against the kit's reference implementation."""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from pisa_conformance.reference import approvals as reference
from pisa_conformance.validate import AGUI_ID, check_lifecycle, errors

from pisa.domain import approval_sign
from pisa.services import approval_runs
from pisa.services.approvals import Outcome
from tests.conftest import pair_v2
from tests.phone_signing import register, sign
from tests.test_live_v2_socket import App, connect, opened
from tests.test_live_v2_socket import hello as live_hello
from tests.test_openapi_v2_delta import delta_body_errors

PAYLOAD = {"calendar": "primary", "title": "Lunch", "start": "2026-10-21T12:30:00+02:00", "lang": "en-US"}


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


@pytest.fixture
def ran(client: Any) -> list[dict[str, Any]]:
    """The calendar_create executor, faked: what it was asked to run."""
    done: list[dict[str, Any]] = []

    async def execute(payload: dict[str, Any]) -> Outcome:
        done.append(payload)
        return Outcome("Added Lunch on Wednesday at 12:30.")

    async def verify(payload: dict[str, Any], outcome: Outcome) -> bool:
        return True

    client.app.state.runtime.approvals.register("calendar_create", execute, verify=verify)
    return done


def create(
    client: Any, device_id: str, *, biometric: bool = False, payload: dict[str, Any] | None = None
) -> str:
    approvals = client.app.state.runtime.approvals
    view = client.portal.call(
        lambda: approvals.create(
            "calendar_create",
            "Add «Lunch» on Wednesday?",
            ["Wednesday 12:30"],
            payload or PAYLOAD,
            requires_biometric=biometric,
            device_id=uuid.UUID(device_id),
            lang="en-US",
        )
    )
    return str(view["id"])


def auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} | extra


def interrupt_of(frames: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The frame that ends the interrupt's run, and its interrupt."""
    end = next(f for f in frames if f.get("event", {}).get("outcome", {}).get("type") == "interrupt")
    return end, end["event"]["outcome"]["interrupts"][0]


def ended(app: App, thread: str) -> list[dict[str, Any]]:
    return app.until(
        lambda f: f.get("threadId") == thread and f["event"]["type"] in ("RUN_FINISHED", "RUN_ERROR")
    )


def resume_frame(
    approval_id: str, payload: dict[str, Any] | None, status: str = "resolved"
) -> dict[str, Any]:
    entry: dict[str, Any] = {"interruptId": f"int-{approval_id}", "status": status}
    if payload is not None:
        entry["payload"] = payload
    run_input = {"threadId": f"pisa-approval:{approval_id}", "runId": f"run-{uuid.uuid4()}", "messages": []}
    return {"op": "run", "input": run_input | {"resume": [entry]}}


def test_an_approval_is_an_interrupt_on_its_own_thread_and_the_phone_can_recompute_its_digest(  # type: ignore[no-untyped-def]
    client, device, ran, db
) -> None:
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        approval_id = create(client, device_id)
        thread = f"pisa-approval:{approval_id}"
        frames = ended(app, thread)
        app.check()
    end, interrupt = interrupt_of(frames)
    assert {f["threadId"] for f in frames} == {thread} and [f["event"]["type"] for f in frames] == [
        "RUN_STARTED",
        "RUN_FINISHED",
    ]
    meta = interrupt["metadata"]["pisa"]
    assert interrupt["id"] == f"int-{approval_id}" and interrupt["reason"] == "pisa.approval"
    assert interrupt["message"] == "Add an event"  # templated: no title, no outside text
    assert meta["approvalId"] == approval_id and meta["requiresBiometric"] is False
    assert meta["detailsRef"] == f"approval:{approval_id}" and meta["recipientsCount"] == 0
    server = db.execute("SELECT value FROM app_meta WHERE key = 'server_id'").fetchone()[0]
    assert meta["server"] == server
    shown = client.get(f"/v1/approvals/{approval_id}", headers=auth(token)).json()
    assert shown["payload"] == {k: v for k, v in PAYLOAD.items()} and shown["recipients"] == []
    recomputed = reference.digest("calendar_create", shown["payload"], shown["recipients"])
    assert recomputed == meta["digest"] == interrupt["responseSchema"]["properties"]["digest"]["const"]
    assert end["event"]["outcome"]["interrupts"] == [interrupt] and ran == []


def test_a_tap_resume_on_the_socket_runs_it_and_reports_action_done(client, device, ran, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        approval_id = create(client, device_id)
        thread = f"pisa-approval:{approval_id}"
        first, _ = interrupt_of(ended(app, thread))
        digest = interrupt_of([first])[1]["metadata"]["pisa"]["digest"]
        frame = resume_frame(approval_id, {"decision": "approve", "auth": "tap", "digest": digest})
        app.send(frame)
        frames = ended(app, thread)
        again = resume_frame(approval_id, {"decision": "approve", "auth": "tap", "digest": digest})
        app.send(again)
        refused = ended(app, thread)
        app.check()
    started = frames[0]["event"]
    assert started["type"] == "RUN_STARTED" and started["runId"] == frame["input"]["runId"]
    assert started["parentRunId"] == first["runId"]
    done = [f["event"] for f in frames if f["event"]["type"] == "CUSTOM"]
    assert [e["name"] for e in done] == ["pisa.action_done"]  # no pisa.approval.state for its own decision
    assert done[0]["value"]["approvalId"] == approval_id
    assert done[0]["value"]["result"] == "Added Lunch on Wednesday at 12:30."
    assert frames[-1]["event"]["outcome"] == {"type": "success"} and len(ran) == 1
    assert refused[-1]["event"]["type"] == "RUN_ERROR" and refused[-1]["event"]["code"] == "approval_decided"
    assert db.execute("SELECT status FROM approvals").fetchone() == ("done",)


def test_a_cancelled_resume_decides_nothing_and_a_reject_runs_nothing(client, device, ran, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        approval_id = create(client, device_id)
        thread = f"pisa-approval:{approval_id}"
        ended(app, thread)
        app.send(resume_frame(approval_id, None, status="cancelled"))
        let_go = ended(app, thread)
        assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",)
        app.send(resume_frame(approval_id, {"decision": "reject", "auth": "tap", "digest": "0" * 64}))
        rejected = ended(app, thread)
        app.check()
    assert let_go[-1]["event"]["outcome"] == {"type": "success"} and len(let_go) == 2
    assert rejected[-1]["event"]["outcome"] == {"type": "success"} and len(rejected) == 2
    assert ran == [] and db.execute("SELECT status FROM approvals").fetchone() == ("rejected",)


def test_face_id_approve_with_the_v2_challenge_signed_byte_for_byte(client, device, ran) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    key = register(client, auth(token))
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        approval_id = create(client, device_id, biometric=True)
        thread = f"pisa-approval:{approval_id}"
        _, interrupt = interrupt_of(ended(app, thread))
        meta = interrupt["metadata"]["pisa"]
        tap = {"decision": "approve", "auth": "tap", "digest": meta["digest"]}
        app.send(resume_frame(approval_id, tap))
        refused = ended(app, thread)
        made = client.post(
            "/v1/phone/challenge",
            headers=auth(token),
            json={"action": "approval", "approval_id": approval_id, "interrupt_id": interrupt["id"]},
        )
        assert made.status_code == 200, made.text
        challenge = made.json()
        assert set(challenge) == {"action", "nonce", "canonical", "expires_at"}
        mine = reference.canonical(
            server=meta["server"], device=device_id, approval_id=approval_id, interrupt_id=interrupt["id"],
            decision="approve", kind="calendar_create", digest_hex=meta["digest"], recipients_count=0,
            nonce=challenge["nonce"], expires_at=challenge["expires_at"],
        )  # fmt: skip
        assert challenge["canonical"].encode() == mine.encode()  # the phone's own reconstruction
        signed = tap | {
            "auth": "biometric",
            "nonce": challenge["nonce"],
            "signature": sign(key, mine),
            "keyId": "k1",
        }
        app.send(resume_frame(approval_id, signed))
        frames = ended(app, thread)
        app.check()
    assert (
        refused[-1]["event"]["type"] == "RUN_ERROR" and refused[-1]["event"]["code"] == "biometric_required"
    )
    assert [e["event"]["name"] for e in frames if e["event"]["type"] == "CUSTOM"] == ["pisa.action_done"]
    assert len(ran) == 1


def test_a_signature_is_refused_for_other_bytes_or_another_device_and_its_nonce_is_spent(  # type: ignore[no-untyped-def]
    client, device, ran, db
) -> None:
    device_id, token = device
    key = register(client, auth(token))
    approval_id = create(client, device_id, biometric=True)
    body = {"action": "approval", "approval_id": approval_id, "interrupt_id": f"int-{approval_id}"}
    challenge = client.post("/v1/phone/challenge", headers=auth(token), json=body).json()
    digest = approval_sign.parse_canonical(challenge["canonical"])["digest"]
    payload = {"decision": "approve", "auth": "biometric", "digest": digest, "nonce": challenge["nonce"],
               "keyId": "k1", "signature": sign(key, challenge["canonical"] + " ")}  # fmt: skip
    url = f"/v1/approvals/{approval_id}/resume"
    first = client.post(url, headers=auth(token), json=payload)
    good = payload | {"signature": sign(key, challenge["canonical"])}
    spent = client.post(url, headers=auth(token), json=good)  # the right bytes, but the nonce is spent
    other = pair_v2(db, "Other phone")[1]
    fresh = client.post("/v1/phone/challenge", headers=auth(token), json=body).json()
    elsewhere = client.post(  # issued to the first device, sent by another
        url,
        headers=auth(other),
        json=good | {"nonce": fresh["nonce"], "signature": sign(key, fresh["canonical"])},
    )
    for refused in (first, spent, elsewhere):
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "biometric_required"
    assert ran == []
    last = client.post("/v1/phone/challenge", headers=auth(token), json=body).json()
    ok = client.post(
        url,
        headers=auth(token),
        json=good | {"nonce": last["nonce"], "signature": sign(key, last["canonical"])},
    )
    assert ok.status_code == 200 and len(ran) == 1


def test_rest_resume_answers_with_the_settled_shapes(client, device, ran, clock) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    approval_id = create(client, device_id)
    url = f"/v1/approvals/{approval_id}/resume"
    digest = client.get(f"/v1/approvals/{approval_id}", headers=auth(token)).json()
    digest = reference.digest("calendar_create", digest["payload"], digest["recipients"])
    tap = {"decision": "approve", "auth": "tap", "digest": digest}
    unknown = client.post(f"/v1/approvals/{uuid.uuid4()}/resume", headers=auth(token), json=tap)
    changed = client.post(url, headers=auth(token), json=tap | {"digest": "1" * 64})
    done = client.post(url, headers=auth(token), json=tap)
    twice = client.post(url, headers=auth(token), json=tap)
    late_id = create(client, device_id, payload=PAYLOAD | {"title": "Dinner"})
    clock.now += timedelta(minutes=11)
    late = client.post(f"/v1/approvals/{late_id}/resume", headers=auth(token), json=tap)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
    assert (changed.status_code, changed.json()["error"]["code"]) == (409, "approval_changed")
    assert (twice.status_code, twice.json()["error"]["code"]) == (409, "approval_decided")
    assert (late.status_code, late.json()["error"]["code"]) == (410, "approval_expired")
    assert done.status_code == 200, done.text
    body = done.json()
    assert delta_body_errors("post", "/v1/approvals/{id}/resume", "200", body) == []
    events = body["events"]
    run_id = events[0]["runId"]
    assert [e["type"] for e in events] == ["RUN_STARTED", "CUSTOM", "RUN_FINISHED"]
    assert all(errors(e, AGUI_ID) == [] for e in events)
    framed = [{"seq": i + 1, "threadId": f"pisa-approval:{approval_id}", "runId": run_id, "event": e}
              for i, e in enumerate(events)]  # fmt: skip
    assert check_lifecycle(framed) == [] and events[-1]["runId"] == run_id
    assert events[1]["name"] == "pisa.action_done" and len(ran) == 1


def test_decided_elsewhere_or_expired_reaches_the_socket_as_approval_state(
    client, device, ran, clock
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        by_notification = create(client, device_id)
        thread = f"pisa-approval:{by_notification}"
        ended(app, thread)
        tap = {"decision": "reject", "auth": "tap", "digest": "0" * 64}
        answered = client.post(
            f"/v1/approvals/{by_notification}/resume",
            headers=auth(token, **{"X-Pisa-Origin": "notification"}),
            json=tap,
        )
        assert answered.status_code == 200, answered.text
        told = ended(app, thread)
        by_watch = create(client, device_id, payload=PAYLOAD | {"title": "Tea"})
        ended(app, f"pisa-approval:{by_watch}")
        client.post(f"/v1/approvals/{by_watch}/resume", headers=auth(token, **{"X-Pisa-Origin": "watch"}),
                    json=tap)  # fmt: skip
        watched = ended(app, f"pisa-approval:{by_watch}")
        lapsing = create(client, device_id, payload=PAYLOAD | {"title": "Coffee"})
        ended(app, f"pisa-approval:{lapsing}")
        clock.now += timedelta(minutes=11)
        assert client.portal.call(client.app.state.runtime.approvals.expire) == 1
        lapsed = ended(app, f"pisa-approval:{lapsing}")
        app.check()

    def state(frames: list[dict[str, Any]]) -> dict[str, Any]:
        (event,) = [f["event"] for f in frames if f["event"]["type"] == "CUSTOM"]
        assert event["name"] == "pisa.approval.state"
        return event["value"]

    assert state(told) == {"approvalId": by_notification, "interruptId": f"int-{by_notification}",
                           "state": "rejected", "by": "notification"}  # fmt: skip
    assert state(watched)["by"] == "watch" and state(watched)["state"] == "rejected"
    assert state(lapsed) == {"approvalId": lapsing, "interruptId": f"int-{lapsing}", "state": "expired",
                             "by": "server"}  # fmt: skip
    assert ran == []


def test_the_watch_cannot_approve_over_rest_resume(client, device, ran) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    approval_id = create(client, device_id)
    shown = client.get(f"/v1/approvals/{approval_id}", headers=auth(token)).json()
    tap = {"decision": "approve", "auth": "tap",
           "digest": reference.digest("calendar_create", shown["payload"], shown["recipients"])}  # fmt: skip
    refused = client.post(
        f"/v1/approvals/{approval_id}/resume", headers=auth(token, **{"X-Pisa-Origin": "watch"}), json=tap
    )
    assert refused.status_code == 400 and ran == []


def test_an_approved_action_that_fails_ends_its_run_action_failed(client, device, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.errors import PisaError

    async def refuse(payload: dict[str, Any]) -> Outcome:
        raise PisaError("connector_not_connected", "The calendar refused the change.", "")

    client.app.state.runtime.approvals.register("calendar_create", refuse)
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        approval_id = create(client, device_id)
        thread = f"pisa-approval:{approval_id}"
        _, interrupt = interrupt_of(ended(app, thread))
        tap = {"decision": "approve", "auth": "tap", "digest": interrupt["metadata"]["pisa"]["digest"]}
        app.send(resume_frame(approval_id, tap))
        frames = ended(app, thread)
        app.check()  # the kit refuses a RUN_ERROR code outside ApprovalErrorCode on an approval thread
    assert frames[-1]["event"]["type"] == "RUN_ERROR" and frames[-1]["event"]["code"] == "action_failed"
    assert frames[-1]["event"]["message"] == "The calendar refused the change."
    assert db.execute("SELECT status FROM approvals").fetchone() == ("failed",)


def test_a_frame_that_cannot_be_a_resume_starts_no_run(client, device, ran) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    with connect(client, token) as ws:
        app = opened(ws, live_hello(device_id))
        approval_id = create(client, device_id)
        ended(app, f"pisa-approval:{approval_id}")
        no_digest = resume_frame(approval_id, {"decision": "approve", "auth": "tap"})
        ws.send_text(json.dumps(no_digest))  # not a valid client frame: sent as is
        bad = app.receive()
        app.send(resume_frame(str(uuid.uuid4()), {"decision": "reject", "auth": "tap", "digest": "0" * 64}))
        unknown = app.receive()
        app.check()
    for refused in (bad, unknown):
        assert refused["control"] == "error" and refused["code"] == "invalid_frame"
    assert ran == []


def test_the_kit_s_action_failed_stream_and_gap_fixtures_pass() -> None:

    from pisa_conformance.paths import data_root
    from pisa_conformance.validate import check_seq, server_frame_errors

    v2 = data_root()
    lines = (v2 / "fixtures" / "streams" / "approval_failed.jsonl").read_text(encoding="utf-8").splitlines()
    frames = [json.loads(line) for line in lines if line.strip()]
    assert [p for f in frames for p in server_frame_errors(f)] == []
    assert check_lifecycle(frames) == [] and check_seq(frames) == []
    assert frames[-1]["event"]["code"] == "action_failed"
    good = json.loads((v2 / "fixtures" / "gaps" / "valid" / "approval_action_failed.json").read_text())
    bad = json.loads((v2 / "fixtures" / "gaps" / "invalid" / "approval_error_unknown_code.json").read_text())
    assert server_frame_errors(good["frame"]) == [] and server_frame_errors(bad["frame"]) != []
    assert approval_runs.ERROR_CODES == set(
        json.loads((v2 / "schema" / "pisa-contract-2.schema.json").read_text())["x-pisa-approval-error-codes"]
    )
