"""Contract 2's REST (B4): `GET /v1/server` (contract 2's `ServerDiscovery`, against the kit and the
delta), the welcome's features, `PUT`/`GET
/v1/devices/me/push` with relay mode, and `POST /v2/runs` in JSON and SSE
mode, whose runs pass the kit's checks like the socket's."""

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pisa_conformance.cli import discovery_problems, safety_problems
from pisa_conformance.validate import AGUI_ID, check_lifecycle, check_seq, errors

from pisa.api import server as server_api
from tests.conftest import pair_v2, set_key
from tests.test_approvals_v2 import create, ran  # noqa: F401 (a fixture)
from tests.test_live_v2_socket import connect, opened
from tests.test_live_v2_socket import hello as live_hello
from tests.test_openapi_v2_delta import delta_body_errors

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
OPENAPI = json.loads((PROTOCOL / "openapi.json").read_text(encoding="utf-8"))


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --- discovery ----------------------------------------------------------------------------------------
def test_get_server_is_contract_2_s_discovery_without_auth_and_the_same_install_id_each_time(client) -> None:  # type: ignore[no-untyped-def]
    first, again = client.get("/v1/server"), client.get("/v1/server")
    assert first.status_code == 200 and OPENAPI["paths"]["/v1/server"]["get"]["security"] == []  # no auth
    doc = first.json()
    assert discovery_problems(doc) == [] and delta_body_errors("get", "/v1/server", "200", doc) == []
    assert doc["product"] == "pisa" and doc["contract"] == {"versions": ["2.0", "2.1", "2.2", "2.3"]}
    ids = {f["id"] for f in doc["features"]}
    assert {"stop", "approvals.signed"} <= ids and "coder" not in ids
    assert "maps.phone" in ids  # contract 2.3: the phone's own maps, on by default
    assert doc["reach"] == {"scheme": "http", "network": "unknown"}  # the test client's host, "testserver"
    assert doc["serverId"] == again.json()["serverId"] and len(doc["serverId"]) >= 22


def test_the_contract_2_discovery_passes_the_kit_and_shares_the_id_and_features(client, device) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    doc = client.portal.call(server_api.discovery, runtime, "https", "pisa.tail1234.ts.net")
    assert discovery_problems(doc) == []
    assert doc["serverId"] == client.get("/v1/server").json()["serverId"]
    assert doc["reach"] == {"scheme": "https", "network": "tailnet"} and doc["readiness"]["ready"] is True
    assert doc["push"] == {"modes": ["none"], "relayConfigured": False}  # no APNs key in tests
    ids = [f["id"] for f in doc["features"]]
    assert ids[:3] == ["stop", "approvals.signed", "conversation"]
    # the suite turns every experimental feature on: discovery lists each as `experimental.<id>` last
    assert len([i for i in ids if not i.startswith("experimental.")]) == len(server_api.FEATURE_NAMES)
    device_id, token = device
    with connect(client, token) as ws:
        welcome = opened(ws, live_hello(device_id)).got[0]
    assert welcome["features"] == server_api.FEATURES and safety_problems(welcome["features"]) == []
    assert doc["features"][: len(server_api.FEATURES)] == welcome["features"]  # then the experimental ones
    for host, network in (("192.168.1.20", "lan"), ("pisa.local", "lan"), ("100.101.102.103", "tailnet")):
        assert server_api._network(host) == network
    assert server_api._network("pisa.example.org") == "unknown"


# --- push -------------------------------------------------------------------------------------------------
def test_push_relay_mode_is_stored_and_never_echoed(client, device, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    secret, public = bytes(range(32)), bytes(range(32, 64))
    relay = {
        "mode": "relay",
        "handle": "h-synthetic-1",
        "secret": b64url(secret),
        "public_key": b64url(public),
    }
    assert client.get("/v1/devices/me/push", headers=auth(token)).json() == {"mode": "none", "since": None}
    put = client.put("/v1/devices/me/push", headers=auth(token), json=relay)
    assert put.status_code == 204 and put.content == b""
    got = client.get("/v1/devices/me/push", headers=auth(token)).json()
    assert got["mode"] == "relay" and got["since"] and set(got) == {"mode", "since"}
    stored = db.execute("SELECT handle, secret, public_key FROM device_push_relay").fetchone()
    assert (
        stored[0] == "h-synthetic-1" and bytes(stored[2]) == public and bytes(stored[1]) != secret
    )  # sealed
    token_hex = "ab" * 32
    direct = client.put(
        "/v1/devices/me/push", headers=auth(token), json={"mode": "direct", "token": token_hex}
    )
    assert direct.status_code == 204  # kept, though this server has no APNs key: push reads off
    assert client.get("/v1/devices/me/push", headers=auth(token)).json()["mode"] == "direct"
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (0,)  # replaced
    row = db.execute("SELECT apns_environment FROM devices WHERE id = %s", (device_id,)).fetchone()
    assert row == ("production",)
    assert client.put("/v1/devices/me/push", headers=auth(token), json=relay).status_code == 204
    none = client.put("/v1/devices/me/push", headers=auth(token), json={"mode": "none"})
    assert none.status_code == 204
    assert client.get("/v1/devices/me/push", headers=auth(token)).json()["mode"] == "none"
    assert db.execute("SELECT count(*) FROM device_push_relay").fetchone() == (0,)


@pytest.mark.parametrize(
    "change",
    [
        {"handle": "h" * 65},
        {"secret": b64url(bytes(31))},
        {"public_key": b64url(bytes(33))},
        {"secret": base64.urlsafe_b64encode(bytes(32)).decode()},  # padded
        {"extra": 1},
    ],
)
def test_a_bad_relay_registration_is_refused(client, device, change) -> None:  # type: ignore[no-untyped-def]
    relay = {"mode": "relay", "handle": "h-1", "secret": b64url(bytes(32)), "public_key": b64url(bytes(32))}
    refused = client.put("/v1/devices/me/push", headers=auth(device[1]), json=relay | change)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "validation_failed"


# --- POST /v2/runs ----------------------------------------------------------------------------------------
def run_input(text: str, thread: str = "thread-http") -> dict[str, Any]:
    return {
        "threadId": thread,
        "runId": f"run-{uuid.uuid4()}",
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": text}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "en-US"}}},
    }


def kit_problems(thread: str, events: list[dict[str, Any]]) -> list[str]:
    """The kit's checks over a run received over HTTP, framed as the socket would frame it."""
    frames = [
        {"seq": i + 1, "threadId": thread, "runId": events[0]["runId"], "event": e}
        for i, e in enumerate(events)
    ]
    problems = [p for e in events for p in errors(e, AGUI_ID)]
    return problems + check_lifecycle(frames) + check_seq(frames, first=1)


def test_a_run_over_http_in_json_mode_is_the_socket_s_run(
    client, device, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    set_key(db, secrets_key)
    fake_models.default = "It is one o'clock."
    body = run_input("What time is it?")
    answered = client.post("/v2/runs", headers=auth(token), json=body)
    assert answered.status_code == 200, answered.text
    doc = answered.json()
    assert delta_body_errors("post", "/v2/runs", "200", doc) == []
    events = doc["events"]
    assert events[0]["runId"] == body["runId"]
    assert kit_problems("thread-http", events) == []
    assert events[0]["type"] == "RUN_STARTED" and events[-1]["outcome"] == {"type": "success"}
    said = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT").strip()
    assert said == "It is one o'clock."
    roles = db.execute("SELECT role FROM messages ORDER BY id").fetchall()
    assert roles == [("user",), ("assistant",)]  # the same conversation record as the socket's


def test_a_run_over_http_streams_sse_data_lines(client, device, db, secrets_key, fake_models) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    set_key(db, secrets_key)
    fake_models.default = "Fine, thanks."
    body = run_input("Remind me what Lunch is about.")
    headers = auth(token) | {"Accept": "text/event-stream"}
    with client.stream("POST", "/v2/runs", headers=headers, json=body) as streamed:
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        text = "".join(streamed.iter_text())
    blocks = [b for b in text.split("\n\n") if b.strip()]
    assert all(b.startswith("data: ") for b in blocks)
    events = [json.loads(b.removeprefix("data: ")) for b in blocks]
    assert kit_problems("thread-http", events) == []
    assert (
        "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT").strip() == "Fine, thanks."
    )


def test_an_approval_resumed_over_http_and_a_bad_input(client, device, ran) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    device_id, token = device
    approval_id = create(client, device_id)
    shown = client.get(f"/v1/approvals/{approval_id}", headers=auth(token)).json()
    from pisa_conformance.reference import approvals as reference

    digest = reference.digest("calendar_create", shown["payload"], shown["recipients"])
    thread = f"pisa-approval:{approval_id}"
    body = {"threadId": thread, "runId": "run-http-resume", "messages": [],
            "resume": [{"interruptId": f"int-{approval_id}", "status": "resolved",
                        "payload": {"decision": "approve", "auth": "tap", "digest": digest}}]}  # fmt: skip
    answered = client.post("/v2/runs", headers=auth(token), json=body)
    assert answered.status_code == 200, answered.text
    assert delta_body_errors("post", "/v2/runs", "200", answered.json()) == []
    events = answered.json()["events"]
    assert kit_problems(thread, events) == []
    assert [e.get("name") for e in events if e["type"] == "CUSTOM"] == ["pisa.action_done"] and len(ran) == 1
    bad = client.post("/v2/runs", headers=auth(token), json={"threadId": "t", "runId": "r", "messages": []})
    # a run without the user's words is a run that says why; an input without a runId is no run
    assert bad.status_code == 200 and bad.json()["events"][-1]["code"] == "invalid_frame"
    unknown = client.post("/v2/runs", headers=auth(token), json={"threadId": "t", "messages": []})
    assert unknown.status_code == 400


def test_a_run_longer_than_the_wait_is_202_and_goes_on(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models, monkeypatch
) -> None:
    import asyncio

    _, token = device
    set_key(db, secrets_key)

    async def slow(messages, info):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.5)
        yield "Paris is the capital of France."

    fake_models.default = slow
    monkeypatch.setattr("pisa.api.runs.JSON_WAIT_S", 0.1)
    body = run_input("What is the capital of France?")
    accepted = client.post("/v2/runs", headers=auth(token), json=body)
    assert accepted.status_code == 202 and accepted.json() == {"runId": body["runId"]}
    assert delta_body_errors("post", "/v2/runs", "202", accepted.json()) == []
    said = "SELECT trim(content) FROM messages WHERE role = 'assistant'"
    for _ in range(50):  # its words still reach the conversation
        if db.execute(said).fetchone() is not None:
            break
        client.portal.call(asyncio.sleep, 0.1)
    assert db.execute(said).fetchone() == ("Paris is the capital of France.",)
