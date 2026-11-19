"""A `/v2/runs` run in flight when its device is revoked (or its session token's hour ends) ends as the
device's `/v2/live` socket does: a final `device_revoked`, nothing after, its model call cancelled. The run is
held in the registry for its length, beside the device's socket (E1 R12-1)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import dsn, set_key
from tests.live_app import device as device  # the fixture
from tests.live_app import live

NEVER = "Never said."


def run_input(text: str) -> dict[str, Any]:
    return {
        "threadId": "thread-http",
        "runId": f"run-{uuid.uuid4()}",
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": text}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "en-US"}}},
    }


def auth(token: str, sse: bool = False) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} | ({"Accept": "text/event-stream"} if sse else {})


def sse_blocks(text: str) -> list[dict[str, Any]]:
    blocks = [b for b in text.split("\n\n") if b.strip()]
    assert all(b.startswith("data: ") for b in blocks)
    return [json.loads(b.removeprefix("data: ")) for b in blocks]


async def revoke(db: psycopg.Connection, device_id: str) -> None:
    """Like `pisa devices revoke`, from another connection: the app hears it on its LISTEN."""
    from pisa.services.devices import revoke_device

    async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
        await revoke_device(conn, uuid.UUID(device_id))


def model(seen: dict[str, Any], *, before: Any = None) -> Any:
    """A model that says a first sentence, runs `before()` (the revoke), then waits; its cancel is seen."""

    async def stream(messages, info):  # type: ignore[no-untyped-def]
        yield "First words. "
        seen["called"] = True
        if (called := seen.get("on_call")) is not None:
            called.set()
        try:  # the cancel can land during the revoke itself: the listener may hear it before `before` returns
            if before is not None:
                await before()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        seen["completed"] = True
        yield NEVER

    return stream


def registry(client: Any) -> Any:
    return client.app.state.sessions


def settled(client: Any, db: psycopg.Connection, seen: dict[str, Any], device_id: str) -> None:
    """The model call was cancelled (never completed), and the run left the registry."""
    row = None
    for _ in range(100):
        row = db.execute("SELECT status, error_code FROM llm_calls WHERE slot <> 'voice.speak'").fetchone()
        if (
            seen.get("cancelled")
            and row
            and row[0] != "reserved"
            and not registry(client).runs(uuid.UUID(device_id))
        ):
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert seen.get("cancelled") and not seen.get("completed")
    assert row == ("settled", "cancelled")
    assert registry(client).runs(uuid.UUID(device_id)) == set()
    said = db.execute("SELECT content FROM messages WHERE role = 'assistant'").fetchall()
    assert all(NEVER not in r[0] for r in said)


def test_a_revoke_during_an_sse_run_ends_it_with_device_revoked_and_nothing_after(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    seen: dict[str, Any] = {}
    fake_models.default = model(seen, before=lambda: revoke(db, device_id))
    with client.stream(
        "POST", "/v2/runs", headers=auth(token, sse=True), json=run_input("Hello?")
    ) as streamed:
        assert streamed.status_code == 200
        frames = sse_blocks("".join(streamed.iter_text()))
    last = frames[-1]
    assert last["control"] == "error" and last["code"] == "device_revoked"
    assert last["message"] == "This device was revoked on the server."
    assert all("control" not in f for f in frames[:-1])  # the run's AG-UI events, then the one last frame
    assert frames[0]["type"] == "RUN_STARTED"
    assert not any(f.get("type") in ("RUN_FINISHED", "RUN_ERROR") for f in frames)
    assert NEVER not in json.dumps(frames)
    settled(client, db, seen, device_id)
    again = client.post("/v2/runs", headers=auth(token), json=run_input("Still there?"))
    assert again.status_code == 401 and again.json()["error"]["code"] == "device_revoked"


def test_a_revoke_during_a_202_background_run_cancels_its_model_call(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models, monkeypatch
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    seen: dict[str, Any] = {}

    async def later() -> None:
        await asyncio.sleep(0.3)  # after the 202
        await revoke(db, device_id)

    fake_models.default = model(seen, before=later)
    monkeypatch.setattr("pisa.api.runs.JSON_WAIT_S", 0.1)
    body = run_input("What is the capital of France?")
    accepted = client.post("/v2/runs", headers=auth(token), json=body)
    assert accepted.status_code == 202 and accepted.json() == {"runId": body["runId"]}
    settled(client, db, seen, device_id)


def test_a_revoke_during_a_json_run_is_the_401_device_revoked(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models, monkeypatch
) -> None:
    # the run can end only through the revoke: under load, the 8 s answer-or-202 wait could pass first
    monkeypatch.setattr("pisa.api.runs.JSON_WAIT_S", 60.0)
    device_id, token = device
    set_key(db, secrets_key)
    seen: dict[str, Any] = {}
    fake_models.default = model(seen, before=lambda: revoke(db, device_id))
    answered = client.post("/v2/runs", headers=auth(token), json=run_input("Hello?"))
    assert answered.status_code == 401 and answered.json()["error"]["code"] == "device_revoked"
    settled(client, db, seen, device_id)


def test_the_live_socket_and_a_concurrent_run_both_get_device_revoked(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    seen: dict[str, Any] = {}
    held: dict[str, Any] = {}

    async def look_then_revoke() -> None:
        sessions = registry(client)
        held["socket"] = sessions.session(uuid.UUID(device_id))
        held["runs"] = len(sessions.runs(uuid.UUID(device_id)))
        await revoke(db, device_id)

    fake_models.default = model(seen, before=look_then_revoke)
    with live(client, token, device_id) as app:
        socket = registry(client).session(uuid.UUID(device_id))
        assert socket is not None
        headers = auth(token, sse=True)
        with client.stream("POST", "/v2/runs", headers=headers, json=run_input("Hello?")) as streamed:
            frames = sse_blocks("".join(streamed.iter_text()))
        err = app.receive()
        assert err["control"] == "error" and err["code"] == "device_revoked"  # not `replaced`
        with pytest.raises(WebSocketDisconnect) as closed:
            app.ws.receive_text()
        assert closed.value.code == 4003
    assert held["socket"] is socket and held["runs"] == 1  # the run did not evict the socket
    assert frames[-1]["control"] == "error" and frames[-1]["code"] == "device_revoked"
    settled(client, db, seen, device_id)


def test_an_ordinary_run_leaves_the_registry_when_it_ends_and_when_it_fails(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    during: list[int] = []

    async def said(messages, info):  # type: ignore[no-untyped-def]
        during.append(len(registry(client).runs(uuid.UUID(device_id))))
        yield "It is one o'clock."

    fake_models.default = said
    answered = client.post("/v2/runs", headers=auth(token), json=run_input("What time is it?"))
    assert answered.status_code == 200 and answered.json()["events"][-1]["type"] == "RUN_FINISHED"
    with client.stream("POST", "/v2/runs", headers=auth(token, sse=True), json=run_input("And now?")) as s:
        streamed = sse_blocks("".join(s.iter_text()))
    assert streamed[-1]["type"] == "RUN_FINISHED"
    assert during == [1, 1] and registry(client).runs(uuid.UUID(device_id)) == set()

    async def fails(messages, info):  # type: ignore[no-untyped-def]
        during.append(len(registry(client).runs(uuid.UUID(device_id))))
        raise RuntimeError("the provider fell over")
        yield ""

    fake_models.default = fails
    failed = client.post("/v2/runs", headers=auth(token), json=run_input("What time is it?"))
    assert failed.status_code == 200 and failed.json()["events"][-1]["type"] == "RUN_ERROR"
    with client.stream("POST", "/v2/runs", headers=auth(token, sse=True), json=run_input("And now?")) as s:
        streamed = sse_blocks("".join(s.iter_text()))
    assert streamed[-1]["type"] == "RUN_ERROR"
    action = {"forwardedProps": {"pisa": {"a2uiAction": {"name": "tap"}}}}
    resume = {"resume": [{"interruptId": "i", "status": "resolved"}]}  # an action never resumes: 400
    bad = client.post("/v2/runs", headers=auth(token), json=run_input("x") | action | resume)
    assert bad.status_code == 400
    assert during == [1, 1, 1, 1] and registry(client).runs(uuid.UUID(device_id)) == set()


def test_a_session_token_ending_mid_run_ends_the_run(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models, monkeypatch
) -> None:
    from pisa.api import live as live_api

    device_id, token = device
    set_key(db, secrets_key)
    real = live_api.expire_session
    called = asyncio.Event()
    seen: dict[str, Any] = {"on_call": called}

    async def soon(session, at):  # type: ignore[no-untyped-def]
        await called.wait()  # the token's hour, over once the model is called
        called.clear()
        await real(session, datetime.now(UTC) - timedelta(seconds=1))

    monkeypatch.setattr("pisa.api.runs.expire_session", soon)
    fake_models.default = model(seen)
    with client.stream(
        "POST", "/v2/runs", headers=auth(token, sse=True), json=run_input("Hello?")
    ) as streamed:
        frames = sse_blocks("".join(streamed.iter_text()))
    assert frames[0]["type"] == "RUN_STARTED" and all("control" not in f for f in frames)
    assert not any(f.get("type") in ("RUN_FINISHED", "RUN_ERROR") for f in frames)
    settled(client, db, seen, device_id)
    seen.clear()
    seen["on_call"] = called
    answered = client.post("/v2/runs", headers=auth(token), json=run_input("Hello again?"))
    assert answered.status_code == 401 and answered.json()["error"]["code"] == "session_expired"  # JSON mode
    for _ in range(100):
        if seen.get("cancelled") and not registry(client).runs(uuid.UUID(device_id)):
            break
        client.portal.call(asyncio.sleep, 0.05)
    assert seen.get("cancelled") and not seen.get("completed")
    assert registry(client).runs(uuid.UUID(device_id)) == set()
