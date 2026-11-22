"""The pisa-conformance CLI: recorded streams, discovery, selftest, and live mode against a local scripted server."""
import asyncio
import json
import threading

import pytest

from conftest import V2
from pisa_conformance import cli

STREAMS = V2 / "fixtures" / "streams"


def test_selftest_passes(capsys):
    assert cli.main(["selftest"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_good_stream_passes_and_off_catalog_stream_fails(capsys):
    assert cli.main(["stream", str(STREAMS / "turn_places.jsonl"), str(STREAMS / "approval.jsonl")]) == 0
    assert cli.main(["stream", str(STREAMS / "unknown_component.jsonl")]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_lifecycle_and_seq_breaks_are_reported(tmp_path, capsys):
    frames = [json.loads(line) for line in (STREAMS / "turn_text.jsonl").read_text().splitlines()]
    del frames[0]               # no RUN_STARTED: events outside a run
    frames[3]["seq"] += 5       # a gap in seq
    p = tmp_path / "broken.jsonl"
    p.write_text("".join(json.dumps(f) + "\n" for f in frames))
    assert cli.main(["stream", str(p)]) == 1
    out = capsys.readouterr().out
    assert "outside a run" in out and "seq" in out


def test_enrol_and_client_modes(tmp_path):
    assert cli.main(["stream", "--enrol", str(V2 / "fixtures" / "enrol" / "server_ok.jsonl")]) == 0
    assert cli.main(["stream", "--enrol", str(V2 / "fixtures" / "enrol" / "client_session.jsonl")]) == 0
    p = tmp_path / "client.jsonl"
    p.write_text(json.dumps(cli.default_hello()) + "\n" + json.dumps({"op": "ping"}) + "\n")
    assert cli.main(["stream", "--client", str(p)]) == 0
    p.write_text(json.dumps({"op": "ping"}) + "\n")
    assert cli.main(["stream", "--client", str(p)]) == 1  # hello must come first


def test_discovery_file():
    assert cli.main(["discovery", str(V2 / "examples" / "server.json")]) == 0


def test_negotiation_problems_catch_unasked_ring2_and_catalogs():
    hello = cli.default_hello()
    welcome = {"control": "welcome", "seq": 0, "sessionId": "s", "contract": "2.0", "agui": "1.0",
               "a2ui": {"version": "1.0", "catalogIds": ["urn:pisa:a2ui:catalog:core:9"]}, "mcpApps": {"protocolVersion": "2026-01-26"},
               "rings": [0, 1, 2], "extensions": {"pisa.notice": 2}}
    probs = cli.negotiation_problems(hello, [welcome])
    assert len(probs) == 4  # catalog, Ring 2, pisa.notice 2, and pisa.approval not granted


# ---------- live mode, against a scripted local /v2/live ----------

websockets = pytest.importorskip("websockets")


def _serve(frames, ready, holder, auth_seen):
    async def handler(ws):
        auth_seen.append(ws.request.headers.get("Authorization"))
        hello = json.loads(await ws.recv())
        assert hello["op"] == "hello"
        for f in frames:
            await ws.send(json.dumps(f))
        await asyncio.sleep(0.2)

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0, subprotocols=["pisa.v2"]) as server:
            holder.append(server.sockets[0].getsockname()[1])
            ready.set()
            await asyncio.sleep(5)

    asyncio.run(main())


def _live(frames, monkeypatch, *extra):
    ready, holder, auth = threading.Event(), [], []
    t = threading.Thread(target=_serve, args=(frames, ready, holder, auth), daemon=True)
    t.start()
    assert ready.wait(5)
    monkeypatch.setenv("PISA_TEST_TOKEN", "synthetic-token")
    rc = cli.main(["live", f"ws://127.0.0.1:{holder[0]}/v2/live", "--token-env", "PISA_TEST_TOKEN", "--seconds", "1.5", *extra])
    return rc, auth


WELCOME = {"control": "welcome", "seq": 0, "sessionId": "sess-1", "contract": "2.0", "agui": "1.0",
           "a2ui": {"version": "1.0", "catalogIds": ["urn:pisa:a2ui:catalog:core:1"]}, "mcpApps": None, "rings": [0, 1],
           "extensions": {"pisa.notice": 1, "pisa.approval": 1}}


def test_live_clean_session_passes(monkeypatch, tmp_path):
    frames = [WELCOME] + [json.loads(line) for line in (STREAMS / "proactive.jsonl").read_text().splitlines()]
    rec = tmp_path / "got.jsonl"
    rc, auth = _live(frames, monkeypatch, "--record", str(rec))
    assert rc == 0
    assert auth == ["Bearer synthetic-token"]
    assert len(rec.read_text().splitlines()) == len(frames)


def test_live_ring2_without_negotiation_fails(monkeypatch):
    frames = [WELCOME] + [json.loads(line) for line in (STREAMS / "drawings.jsonl").read_text().splitlines()]
    rc, _ = _live(frames, monkeypatch)
    assert rc == 1


def test_live_refused_hello_fails(monkeypatch):
    err = {"control": "error", "seq": 0, "code": "contract_unsupported", "message": "Update the app", "retryable": False}
    rc, _ = _live([err], monkeypatch)
    assert rc == 1
