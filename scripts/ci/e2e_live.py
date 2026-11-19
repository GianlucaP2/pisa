"""The headless end-to-end suite against this server, live. Run from core/ in CI:
`uv run python ../scripts/ci/e2e_live.py`.

It starts `pisa serve-api` on PISA_DATABASE_URL (`live_server.py`: migrated, offline, the port
PISA_LIVE_PORT else 8767) and drives it as the app does, with the CLI's headless client (`pisa.client.Client`)
for pairing and sessions:
1. discovery: `GET /v1/server` against the kit's ServerDiscovery checks; `/health`;
2. QR pairing: the client's HMAC under the QR's token, the server's commitment and MAC checked, the SAS
   confirmed on the server (`pisa pair --confirm`), a session token signed with the device key; REST without
   it is 401;
3. consent: a run before it ends `RUN_ERROR ai_consent_required`; the disclosure as it is now, consented to;
4. `POST /v2/runs`, as SSE and as one JSON document: no model is chosen on a CI server (the tests fake the
   model in process, the server has no scripted one), so the documented run is
   `RUN_ERROR model_not_configured`;
5. `/v2/live`: hello and welcome, ping and pong, one run to its end;
6. approvals: none can be raised without a model (they come from a model's tool call or a Google
   write-through), so the round-trip is the refusal path: the Approvals list is empty, and a resume of an
   unknown approval is a 404 over REST and, over `/v2/live`, a control error `invalid_frame` with no run;
7. push registration in relay mode, then none;
8. a second device paired with its socket open; listed, revoked from the first, and its socket closes 4003;
   its session token is refused afterwards;
9. erase (D-138, audit M5): unsigned it is refused; signed with the client's own device key (no iPhone, no
   Face ID key) every family's deletion is accepted.

The server runs in insecure loopback mode (PISA_INSECURE_LOOPBACK_PAIRING: no certificate, plain http on
127.0.0.1), the documented developer path (audit M3); `conformance_live.py` pairs with a TLS pin.

Every frame the server sends is checked by the conformance kit (the official AG-UI and A2UI schemas and
CONTRACT-2's, `seq`, the lifecycle, the negotiation), every frame sent by the kit's client-frame checks, every
`/v2/runs` event against AG-UI 1.0. Exit status: 0 when every check passed.
"""

import asyncio
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import websockets
from live_server import LiveServer, Paired, hello
from pisa_conformance.cli import discovery_problems, negotiation_problems
from pisa_conformance.validate import (
    AGUI_ID,
    EXT_ID,
    check_lifecycle,
    check_seq,
    client_frame_errors,
    errors,
    server_frame_errors,
)

PORT = 8767
TIMEOUT_S = 30
results: list[bool] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip(), flush=True)
    return ok


def turn(thread: str, text: str = "che ore sono?") -> dict[str, Any]:
    return {
        "threadId": thread,
        "runId": f"run-{uuid.uuid4()}",
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": text}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "it-IT"}}},
    }


def event_errors(event: dict[str, Any]) -> list[str]:
    """A `/v2/runs` event: plain AG-UI 1.0, and a CUSTOM one also CONTRACT-2's."""
    errs = errors(event, AGUI_ID)
    if event.get("type") == "CUSTOM":
        errs += errors(event, EXT_ID + "#/$defs/ServerCustomEvent")
    return errs


class Socket:
    """One `/v2/live` connection: every frame both ways checked by the conformance kit."""

    def __init__(self, server: LiveServer, device: Paired, name: str) -> None:
        self.server, self.device, self.name = server, device, name
        self.frames: list[dict[str, Any]] = []
        self.hello = hello(device.device_id)
        self.ws: Any = None

    async def __aenter__(self) -> "Socket":
        self.ws = await websockets.connect(
            f"{self.server.ws}/v2/live",
            additional_headers=self.device.auth,
            subprotocols=["pisa.v2"],  # type: ignore[list-item]
        )
        await self.send(self.hello)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.ws.close()

    async def send(self, frame: dict[str, Any]) -> None:
        problems = client_frame_errors(frame)
        check(f"[{self.name}] the {frame['op']} frame sent is contract 2's", not problems, problems[:3])
        await self.ws.send(json.dumps(frame))

    async def recv(self) -> dict[str, Any]:
        frame = json.loads(await asyncio.wait_for(self.ws.recv(), TIMEOUT_S))
        self.frames.append(frame)
        problems = server_frame_errors(frame)
        if problems:
            check(f"[{self.name}] frame seq {frame.get('seq')} is contract 2's", False, problems[:3])
        return frame

    async def until(self, done: Any) -> dict[str, Any]:
        while True:
            frame = await self.recv()
            if done(frame):
                return frame

    async def run(self, body: dict[str, Any]) -> dict[str, Any]:
        """The run's last event (RUN_FINISHED or RUN_ERROR)."""
        await self.send({"op": "run", "input": body})
        frame = await self.until(
            lambda f: (
                f.get("runId") == body["runId"]
                and f.get("event", {}).get("type") in ("RUN_FINISHED", "RUN_ERROR")
            )
        )
        return frame["event"]

    def verdict(self) -> None:
        problems = check_seq(self.frames) + check_lifecycle(self.frames)
        problems += negotiation_problems(self.hello, self.frames)
        check(
            f"[{self.name}] seq, lifecycle and negotiation over {len(self.frames)} frames",
            not problems,
            problems[:5],
        )


async def runs(
    http: httpx.AsyncClient, device: Paired, body: dict[str, Any], sse: bool
) -> list[dict[str, Any]]:
    """`POST /v2/runs`: its events, as SSE `data:` lines or one JSON document."""
    accept = "text/event-stream" if sse else "application/json"
    answer = await http.post("/v2/runs", json=body, headers=device.auth | {"Accept": accept})
    check(f"POST /v2/runs ({accept}) answers 200", answer.status_code == 200, f"HTTP {answer.status_code}")
    if sse:
        lines = [line.removeprefix("data:").strip() for line in answer.text.splitlines()]
        events = [json.loads(x) for x in lines if x.startswith("{")]
    else:
        events = answer.json().get("events", [])
    problems = [p for e in events for p in event_errors(e)]
    check(f"every /v2/runs event is AG-UI 1.0 ({len(events)})", bool(events) and not problems, problems[:3])
    types = [e["type"] for e in events]
    ended = types[-1:] in (["RUN_FINISHED"], ["RUN_ERROR"])
    check("the run opens and ends", types[:1] == ["RUN_STARTED"] and ended, types)
    return events


async def suite(server: LiveServer) -> None:
    async with httpx.AsyncClient(base_url=server.base, timeout=TIMEOUT_S) as http:
        # 1. discovery
        check("GET /health", (await http.get("/health")).status_code == 200)
        answer = await http.get("/v1/server")
        problems = discovery_problems(answer.json()) if answer.status_code == 200 else [answer.status_code]
        check("GET /v1/server is contract 2's ServerDiscovery", not problems, problems[:3])

        # 2. QR pairing (Client.pair checks the commitment and the server's MAC before it shows the SAS)
        device = await server.pair("e2e")
        check("paired by QR, confirmed by its SAS, a session token", bool(device.token), device.device_id)
        check("REST without a token is 401", (await http.get("/v1/devices")).status_code == 401)

        # 3. consent
        events = await runs(http, device, turn("e2e-runs"), sse=False)
        check(
            "before consent a run ends ai_consent_required",
            events[-1].get("code") == "ai_consent_required",
            events[-1],
        )
        await server.consent(device)
        check("AI consent recorded", True)

        # 4. /v2/runs (no model on a CI server: the documented run error)
        for sse in (True, False):
            events = await runs(http, device, turn("e2e-runs"), sse=sse)
            check(
                "no model chosen: RUN_ERROR model_not_configured",
                events[-1].get("type") == "RUN_ERROR" and events[-1].get("code") == "model_not_configured",
                events[-1],
            )

        # 5. /v2/live
        async with Socket(server, device, "live") as live:
            welcome = await live.recv()
            check(
                "hello → welcome",
                welcome.get("control") == "welcome",
                {k: welcome.get(k) for k in ("contract", "rings")},
            )
            await live.send({"op": "ping"})
            check("ping → pong", (await live.until(lambda f: "control" in f)).get("control") == "pong")
            ended = await live.run(turn("e2e-live"))
            check(
                "a run on /v2/live ends RUN_ERROR model_not_configured",
                ended.get("type") == "RUN_ERROR" and ended.get("code") == "model_not_configured",
                ended,
            )

            # 6. approvals: the refusal path (none can be raised without a model)
            listed = await http.get("/v1/approvals", headers=device.auth)
            check("GET /v1/approvals", listed.status_code == 200, listed.json())
            unknown = str(uuid.uuid4())
            decision = {"decision": "reject", "auth": "tap", "digest": "0" * 64}
            answer = await http.post(f"/v1/approvals/{unknown}/resume", json=decision, headers=device.auth)
            code = answer.json().get("error", {}).get("code")
            check(
                "resume of an unknown approval over REST: 404 not_found",
                answer.status_code == 404 and code == "not_found",
                f"HTTP {answer.status_code} {code}",
            )
            body = {
                "threadId": f"pisa-approval:{unknown}",
                "runId": f"run-{uuid.uuid4()}",
                "messages": [],
                "resume": [{"interruptId": f"int-{unknown}", "status": "resolved", "payload": decision}],
            }
            await live.send(
                {"op": "run", "input": body}
            )  # an unknown approval starts no run (CONTRACT-2 §6.1)
            refused = await live.until(lambda f: f.get("control") == "error")
            check(
                "resume of an unknown approval over /v2/live: control error invalid_frame, no run",
                refused.get("code") == "invalid_frame"
                and not any(f.get("threadId") == body["threadId"] for f in live.frames),
                {k: refused.get(k) for k in ("code", "message")},
            )
            live.verdict()

        # 7. push registration
        relay = {
            "mode": "relay",
            "handle": f"e2e-{uuid.uuid4().hex[:12]}",
            "secret": base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
            "public_key": base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
        }
        for body in (relay, {"mode": "none"}):
            answer = await http.put("/v1/devices/me/push", json=body, headers=device.auth)
            state = (await http.get("/v1/devices/me/push", headers=device.auth)).json()
            check(
                f"push mode {body['mode']}: 204, then read back",
                answer.status_code == 204 and state.get("mode") == body["mode"],
                state,
            )

        # 8. a second device: listed, revoked, its socket closed 4003
        second = await server.pair("e2e-second")
        await server.consent(second)
        async with Socket(server, second, "second") as other:
            check("the second device's hello → welcome", (await other.recv()).get("control") == "welcome")
            devices = (await http.get("/v1/devices", headers=device.auth)).json()["devices"]
            ids = {d["id"]: d for d in devices}
            check(
                "GET /v1/devices lists both, this one marked",
                second.device_id in ids
                and ids[device.device_id]["this_device"]
                and not ids[second.device_id]["this_device"],
                [d["name"] for d in devices],
            )
            answer = await http.delete(f"/v1/devices/{second.device_id}", headers=device.auth)
            check("DELETE /v1/devices/{id} → 204", answer.status_code == 204, f"HTTP {answer.status_code}")
            began, code = time.monotonic(), None
            try:
                while True:
                    await other.recv()
            except websockets.ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd else None
            except TimeoutError:
                pass
            check(
                "the revoked device's socket closes 4003",
                code == 4003,
                f"code {code} after {time.monotonic() - began:.1f} s",
            )
            other.verdict()
        answer = await http.get("/v1/devices", headers=second.auth)
        check(
            "the revoked device's session token is refused",
            answer.status_code == 401,
            f"HTTP {answer.status_code}",
        )
        devices = (await http.get("/v1/devices", headers=device.auth)).json()["devices"]
        revoked = next((d for d in devices if d["id"] == second.device_id), None)
        check(
            "the revoked device shows revoked (or is gone)",
            revoked is None or revoked["revoked_at"] is not None,
        )

        # 9. erase, signed with the client's own device key
        today = datetime.now(ZoneInfo(server.env["PISA_TIMEZONE"])).date()
        body = {"family": "messages", "before": today.isoformat()}
        answer = await http.post("/v1/data/erase", json=body, headers=device.auth)
        code = answer.json().get("error", {}).get("code")
        check(
            "erase unsigned: 403 biometric_required",
            answer.status_code == 403 and code == "biometric_required",
        )
        families = ("messages", "places", "mail", "money", "records", "memory", "health", "voice", "learn")
        async with device.client.http() as own:
            answers = [await device.client.erase(own, family, today) for family in families]
        check(
            "erase every family, signed with the client's device key: 202 each",
            all(a.status_code == 202 for a in answers),
            [a.status_code for a in answers],
        )


def main() -> int:
    began = time.monotonic()
    with LiveServer(PORT, "pisa-e2e-", insecure_loopback=True) as server:
        try:
            asyncio.run(suite(server))
        except Exception as exc:  # a crash is a failure, reported with what came before it
            check("the suite ran to its end", False, f"{exc.__class__.__name__}: {exc}")
        print(f"e2e: {sum(results)}/{len(results)} passed in {time.monotonic() - began:.0f} s", flush=True)
        if not all(results):
            server.dump_log()
        return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
