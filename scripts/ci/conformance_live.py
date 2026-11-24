"""The conformance kit against this server, live. Run from core/ in CI:
`uv run python ../scripts/ci/conformance_live.py`.

It starts `pisa serve-api` on a fresh database (PISA_DATABASE_URL, migrated here), pairs a device by QR
exactly as the CLI's headless client does (`pisa pair --no-wait --json`, the HMAC and SAS,
`pisa pair --confirm <digits>`), signs a session token with its device key, and runs the kit
(`protocol/v2/conformance`):
- `selftest`: every fixture bundled with the kit;
- `discovery`: `GET /v1/server`, contract 2's ServerDiscovery;
- `live`: `/v2/live` with this device's hello and one run (the AI disclosure consented to, but no model chosen
  here: the run ends RUN_ERROR model_not_configured, a valid contract 2 run), every frame checked against the
  official schemas, the AG-UI lifecycle, `seq` and the negotiation.

The server and the pairing are `live_server.py`'s (shared with `e2e_live.py` and `api_fuzz.py`); the port is
PISA_LIVE_PORT, else 8765. Exit status: 0 when every check passed.
"""

import asyncio
import json
import sys
import uuid

from live_server import LiveServer, hello, run

PORT = 8765


async def session(server: LiveServer) -> tuple[str, str]:
    device = await server.pair("conformance")
    await server.consent(device)
    return device.device_id, device.token


def main() -> int:
    with LiveServer(PORT, "pisa-conformance-") as server:
        device_id, token = asyncio.run(session(server))
        work, env = server.work, server.env
        turn = {
            "threadId": "conformance",
            "runId": f"run-{uuid.uuid4()}",
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": "che ore sono?"}],
            "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "it-IT"}}},
        }
        (work / "hello.json").write_text(json.dumps(hello(device_id)))
        (work / "send.jsonl").write_text(json.dumps({"op": "run", "input": turn}) + "\n" + '{"op": "ping"}\n')
        checks = [
            ("pisa-conformance", "selftest"),
            ("pisa-conformance", "discovery", f"{server.base}/v1/server"),
            ("pisa-conformance", "live", f"{server.ws}/v2/live", "--token-env", "PISA_TOKEN",
             "--hello", str(work / "hello.json"), "--send", str(work / "send.jsonl"), "--seconds", "8",
             "--record", str(work / "got.jsonl")),
        ]  # fmt: skip
        failed = [c[1] for c in checks if run(*c, env=env | {"PISA_TOKEN": token}).returncode != 0]
        got = [json.loads(line) for line in (work / "got.jsonl").read_text().splitlines() if line.strip()]
        ended = [
            f["event"]
            for f in got
            if f.get("runId") == turn["runId"] and f["event"]["type"].startswith("RUN_")
        ]
        print(f"the run's lifecycle on the wire: {[e['type'] for e in ended]}", flush=True)
        if [e["type"] for e in ended][-1:] not in (["RUN_FINISHED"], ["RUN_ERROR"]):
            failed.append("the run never ended on the wire")
        print("conformance:", "FAILED " + ", ".join(failed) if failed else "all passed", flush=True)
        if failed:
            server.dump_log()
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
