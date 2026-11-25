"""End-to-end smoke test of a running Pisa, executed INSIDE the api container on the server.

It pairs a throwaway device by QR with the code given as argv[1] (`pisa pair smoke-test --no-wait --json`'s
`uri`), confirms it on the server itself (`pisa pair --confirm <digits>`: it runs where the CLI does), signs a
session token with its device key, exercises REST and the live socket `/v2/live` (hello, ping, one run), and
prints PASS/FAIL lines. It never prints tokens or keys. The caller removes the device afterwards with
everything it made (`pisa devices purge`, from the DEVICE_ID=... line). It asks for no stored briefing (no
"ask", no phrase): those would land in the user's history. With an OpenAI key set, the run costs a fraction of
a cent.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid

import httpx
import websockets

from pisa.client import Client
from pisa.domain import pairing as rules

BASE = os.environ.get("PISA_SMOKE_BASE", "http://127.0.0.1:8000")
results: list[bool] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


async def pair(http: httpx.AsyncClient, code: str) -> tuple[str, str]:
    """A throwaway device (named and typed as `pisa devices purge` takes it) paired with the QR's code and
    confirmed here by its digits; its id and a session token."""
    client = Client.new(BASE)
    qr = rules.parse_qr(code)
    token = rules.token_bytes(qr["token"])
    nonce = rules.new_nonce()
    text = rules.transcript(qr["spki"], client.device_key, nonce)
    begun = (await http.post("/v1/pair/begin")).json()
    body = {"v": 1, "pairingId": begun["pairingId"], "deviceKey": client.device_key, "name": "smoke-test",
            "platform": "other", "clientNonce": nonce, "mac": rules.client_mac(token, text)}
    answer = (await http.post("/v1/pair", json=body)).json()
    check("the server reveals the nonce it committed to",
          rules.reveal_matches(begun["commitment"], str(answer.get("serverNonce"))))
    sas = rules.sas(text, answer["serverNonce"])
    confirmed = subprocess.run(["pisa", "pair", "--confirm", sas], capture_output=True, text=True)
    check("pair by QR, confirmed on the server by its digits", confirmed.returncode == 0, confirmed.stderr[-200:])
    client.data["deviceId"] = answer["deviceId"]
    token = await client.session(http)
    auth = {"Authorization": f"Bearer {token}"}  # the AI disclosure as it is now, consented to (no health)
    revision = (await http.get("/v1/ai/disclosure", headers=auth)).json()["revision"]
    consent = await http.post("/v1/ai/consent", json={"revision": revision, "health": False}, headers=auth)
    check("AI consent recorded for this device", consent.status_code == 204, f"HTTP {consent.status_code}")
    return answer["deviceId"], token


async def main(code: str) -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as http:
        device_id, token = await pair(http, code)
        print(f"DEVICE_ID={device_id}", flush=True)
        check("a session token", bool(token))
        auth = {"Authorization": f"Bearer {token}"}

        r = await http.get("/v1/ai/providers")
        check("REST without a token is refused", r.status_code == 401, f"HTTP {r.status_code}")
        for path in ("/v1/ai/providers", "/v1/ai/slots", "/v1/ai/models?slot=chat.conversation", "/v1/ai/spend"):
            r = await http.get(path, headers=auth)
            check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")
        slots = {s["id"]: s for s in (await http.get("/v1/ai/slots", headers=auth)).json()["slots"]}
        check("chat.conversation model", True, slots["chat.conversation"]["model"])
        models = (await http.get("/v1/ai/models?slot=chat.conversation", headers=auth)).json()
        luna = [m for m in models["models"] if m["ref"] == "openai:gpt-6-luna"]
        check(
            "catalog lists openai:gpt-6-luna as compatible",
            bool(luna) and luna[0]["compatible"],
            f"{len(models['models'])} models, refreshed {models['catalog_refreshed_at']}, EUR/USD {models['usd_to_eur']}",
        )
        spend = (await http.get("/v1/ai/spend", headers=auth)).json()
        check("caps", True, spend["caps"])
        providers = (await http.get("/v1/ai/providers", headers=auth)).json()["providers"]
        key_set = any(p["id"] == "openai" and p["key_set"] for p in providers)
        print(f"INFO  openai key set: {key_set}", flush=True)

        # "Pisa speaks first". No presence and no briefing asked for here: they would land in the user's
        # history (and a presence would use up the day's automatic briefing).
        r = await http.get("/v1/settings", headers=auth)
        settings = r.json() if r.status_code == 200 else {}
        check("GET /v1/settings", "triggers" in settings.get("briefing", {}), settings.get("briefing", {}).get("triggers"))
        r = await http.put(
            "/v1/devices/me/push", headers=auth, json={"mode": "direct", "token": "ab" * 32, "environment": "sandbox"}
        )  # a fake token on this throwaway device (revoked at the end): 204 = the APNs key is installed
        check("APNs key installed (PUT push → 204)", r.status_code == 204, f"HTTP {r.status_code}")
        for path in ("/v1/places/providers", "/v1/places/usage"):
            r = await http.get(path, headers=auth)
            check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")
        configured = [p["id"] for p in (await http.get("/v1/places/providers", headers=auth)).json()["providers"] if p["configured"]]
        print(f"INFO  place providers configured: {configured}", flush=True)
        for provider in configured:  # Apple: token exchange; Google: ids-only search (free); Tavily: 1 credit
            r = await http.post(f"/v1/places/providers/{provider}/test", headers=auth, timeout=30)
            body = r.json()
            check(f"place provider {provider} test", r.status_code == 200 and body.get("ok") is True, body.get("message", body))
        r = await http.get("/v1/plan", headers=auth, timeout=30)  # the user's real plan (coalesced, Apple first)
        plan = r.json() if r.status_code == 200 else {}
        check(
            "GET /v1/plan",
            r.status_code == 200 and "items" in plan and "unplanned" in plan,
            f"{len(plan.get('items', []))} items, routes {plan.get('sources', {}).get('routes')}",
        )

    url = BASE.replace("http", "ws", 1) + "/v2/live"
    async with websockets.connect(url, additional_headers=auth, subprotocols=["pisa.v2"]) as ws:  # type: ignore[list-item]
        hello = {"op": "hello", "contract": {"versions": ["2.0"]}, "agui": {"protocolVersions": ["1.0"]},
                 "rings": [0], "device": {"deviceId": device_id, "appVersion": "smoke", "platform": "other"}}
        await ws.send(json.dumps(hello))
        welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
        check("hello → welcome", welcome.get("control") == "welcome", welcome.get("languages"))
        await ws.send(json.dumps({"op": "ping"}))
        pong = json.loads(await asyncio.wait_for(ws.recv(), 10))
        check("ping → pong", pong.get("control") == "pong")

        run_id = f"run-{uuid.uuid4()}"
        turn = {"threadId": "smoke", "runId": run_id,
                "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "Pisa, che ore sono?"}],
                "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "it-IT"}}}}
        await ws.send(json.dumps({"op": "run", "input": turn}))
        text, last = "", {}
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 60))
            event = frame.get("event", {})
            if frame.get("runId") != run_id:
                continue
            if event.get("type") == "TEXT_MESSAGE_CONTENT":
                text += event["delta"]
            if event.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
                last = event
                break
        if key_set:
            check("spoken reply in Italian (the run's text)", last["type"] == "RUN_FINISHED" and bool(text.strip()),
                  repr(text[:160]))
        else:
            check(
                "no key yet → RUN_ERROR provider_key_missing (real cause named)",
                last.get("type") == "RUN_ERROR" and last.get("code") == "provider_key_missing",
                last.get("message"),
            )
    print(f"SUMMARY  {sum(results)}/{len(results)} passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
