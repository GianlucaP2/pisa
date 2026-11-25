"""pisa-conformance: check a PISA server's contract 2 output.

    pisa-conformance stream FILE.jsonl [...]       a recorded /v2/live session (server frames, one JSON per line)
    pisa-conformance stream --enrol FILE.jsonl     a recorded enrolment data channel (G5)
    pisa-conformance stream --client FILE.jsonl    app frames (hello, run, custom...)
    pisa-conformance live wss://HOST/v2/live --token-env PISA_TOKEN [--seconds 10] [--hello FILE] [--record OUT]
    pisa-conformance discovery FILE.json|https://HOST/v1/server
    pisa-conformance selftest                      every bundled fixture, as a check of this installation

Server frames are checked against the official AG-UI 1.0 schema, A2UI 1.0 with the PISA catalog core:1, and
pisa-contract-2.schema.json; then the AG-UI lifecycle per thread and the session's seq. Exit status 0 when clean,
1 when anything failed, 2 on usage or connection errors.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from . import CONTRACT, __version__
from .paths import EXAMPLES, FIXTURES, ROOT
from .validate import (EXT_ID, check_lifecycle, check_seq, client_frame_errors, enrol_client_frame_errors, enrol_server_frame_errors,
                       errors, server_frame_errors)


class Report:
    def __init__(self, label: str) -> None:
        self.label, self.frames, self.problems = label, 0, []  # type: ignore[var-annotated]

    def add(self, where: str, errs: Iterable[str]) -> None:
        self.problems += [f"{where}: {e}" for e in errs]

    def print(self, out=None, limit: int = 50) -> bool:
        out = out or sys.stdout  # looked up at call time, so redirection works
        ok = not self.problems
        print(f"{'PASS' if ok else 'FAIL'} {self.label}: {self.frames} frames, {len(self.problems)} problems", file=out)
        for p in self.problems[:limit]:
            print("  " + p, file=out)
        if len(self.problems) > limit:
            print(f"  ... and {len(self.problems) - limit} more", file=out)
        return ok


def _read_jsonl(path: Path) -> list[Any]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{i}: not JSON: {e}")
    return out


def check_server_frames(frames: list[dict[str, Any]], label: str, *, first_seq: int | None = None) -> Report:
    r = Report(label)
    r.frames = len(frames)
    for i, f in enumerate(frames):
        if not isinstance(f, dict):
            r.add(f"frame {i}", ["not a JSON object"])
            continue
        r.add(f"frame {i} (seq {f.get('seq')})", server_frame_errors(f))
    events = [f for f in frames if isinstance(f, dict) and "event" in f and "threadId" in f]
    r.add("lifecycle", check_lifecycle(events))
    r.add("seq", check_seq([f for f in frames if isinstance(f, dict)], first_seq))
    return r


def check_enrol_frames(frames: list[dict[str, Any]], label: str) -> Report:
    r = Report(label)
    r.frames = len(frames)
    for i, f in enumerate(frames):
        r.add(f"frame {i}", enrol_server_frame_errors(f) if "event" in f else enrol_client_frame_errors(f))
    server = [f for f in frames if "event" in f]
    r.add("seq", check_seq(server, 1 if server else None))
    return r


def check_client_frames(frames: list[dict[str, Any]], label: str) -> Report:
    r = Report(label)
    r.frames = len(frames)
    for i, f in enumerate(frames):
        r.add(f"frame {i} ({f.get('op') if isinstance(f, dict) else '?'})", client_frame_errors(f))
    if frames and frames[0].get("op") != "hello":
        r.add("frame 0", ["the first app frame must be hello"])
    return r


def cmd_stream(a: argparse.Namespace) -> int:
    ok = True
    for p in a.files:
        frames = _read_jsonl(Path(p))
        if a.enrol:
            rep = check_enrol_frames(frames, p)
        elif a.client:
            rep = check_client_frames(frames, p)
        else:
            rep = check_server_frames(frames, p)
        ok &= rep.print()
    return 0 if ok else 1


def _fetch_json(src: str, token: str | None = None) -> Any:
    if src.startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(src, headers={"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (a URL the operator typed)
            return json.loads(resp.read().decode("utf-8"))
    return json.loads(Path(src).read_text(encoding="utf-8"))


REQUIRED_FEATURES = ("stop", "approvals.signed")  # always on: never optional, paywalled or deprecated


def safety_problems(features: Any) -> list[str]:
    """The safety features every server offers (stated plainly; the schema refuses the same documents)."""
    if not isinstance(features, list):
        return ["no features list"]
    out = []
    by_id = {f.get("id"): f for f in features if isinstance(f, dict)}
    for fid in REQUIRED_FEATURES:
        f = by_id.get(fid)
        if f is None:
            out.append(f"required safety feature {fid} is missing")
        elif "entitlement" in f or "deprecated" in f:
            out.append(f"safety feature {fid} must not be paywalled or deprecated")
    return out


def discovery_problems(doc: Any) -> list[str]:
    errs = errors(doc, EXT_ID + "#/$defs/ServerDiscovery")
    if isinstance(doc, dict):
        errs += safety_problems(doc.get("features"))
        if "serverId" not in doc:
            errs.append("serverId is missing (an opaque random id picked at install)")
    return errs


def cmd_discovery(a: argparse.Namespace) -> int:
    doc = _fetch_json(a.source)
    r = Report(f"discovery {a.source}")
    r.frames = 1
    r.add("GET /v1/server", discovery_problems(doc))
    return 0 if r.print() else 1


def default_hello() -> dict[str, Any]:
    """A conservative hello: Rings 0 and 1, core:1, no Ring 2, no audio. Synthetic device id."""
    return {"op": "hello", "contract": {"versions": ["2.0"]}, "agui": {"protocolVersions": ["1.0"]},
            "a2ui": {"1.0": {"supportedCatalogIds": ["urn:pisa:a2ui:catalog:core:1"]}}, "rings": [0, 1],
            "extensions": {"pisa.notice": 1, "pisa.surface": 1, "pisa.approval": 1},
            "device": {"deviceId": "00000000-0000-4000-8000-00000000c0f0", "appVersion": f"pisa-conformance/{__version__}",
                       "platform": "cli", "surfaces": ["none"], "audioFormats": []}}


async def _live(url: str, token: str | None, hello: dict[str, Any], seconds: float, max_frames: int,
                send: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        import websockets
    except ImportError:
        raise SystemExit("live mode needs the 'live' extra: pip install 'pisa-conformance[live]'")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    frames: list[dict[str, Any]] = []
    async with websockets.connect(url, subprotocols=["pisa.v2"], additional_headers=headers, open_timeout=15, max_size=2**23) as ws:
        await ws.send(json.dumps(hello))
        sent_rest = False
        deadline = time.monotonic() + seconds
        while len(frames) < max_frames:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), left)
            except TimeoutError:
                break
            except websockets.ConnectionClosed:
                break
            if isinstance(raw, bytes):
                continue  # binary audio frames (pisa.voice.audio.binary) are outside the JSON checks
            frames.append(json.loads(raw))
            if not sent_rest and frames[0].get("control") == "welcome":
                for f in send:
                    await ws.send(json.dumps(f))
                sent_rest = True
    return frames


def cmd_live(a: argparse.Namespace) -> int:
    token = os.environ.get(a.token_env) if a.token_env else None
    if a.token_env and not token:
        print(f"error: ${a.token_env} is empty", file=sys.stderr)
        return 2
    hello = json.loads(Path(a.hello).read_text()) if a.hello else default_hello()
    send = _read_jsonl(Path(a.send)) if a.send else []
    pre = Report("app frames")
    pre.frames = 1 + len(send)
    pre.add("hello", client_frame_errors(hello))
    for i, f in enumerate(send):
        pre.add(f"send {i}", client_frame_errors(f))
    if pre.problems:
        pre.print()
        return 2
    try:
        frames = asyncio.run(_live(a.url, token, hello, a.seconds, a.max_frames, send))
    except (OSError, TimeoutError) as e:
        print(f"error: cannot reach {a.url}: {e}", file=sys.stderr)
        return 2
    if a.record:
        Path(a.record).write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames), encoding="utf-8")
    r = check_server_frames(frames, f"live {a.url}")
    if not frames:
        r.add("session", ["no frame received"])
    elif frames[0].get("control") == "error":
        r.add("session", [f"the server refused the hello: {frames[0].get('code')}: {frames[0].get('message')}"])
    elif frames[0].get("control") != "welcome":
        r.add("session", ["the first server frame must be welcome"])
    else:
        r.add("negotiation", negotiation_problems(hello, frames))
    return 0 if r.print() else 1


def negotiation_problems(hello: dict[str, Any], frames: list[dict[str, Any]]) -> list[str]:
    """The server never sends what was not negotiated (CONTRACT-2 §4.2)."""
    w, out = frames[0], []
    listed = set(hello.get("a2ui", {}).get("1.0", {}).get("supportedCatalogIds", []))
    picked = set((w.get("a2ui") or {}).get("catalogIds", []))
    if not picked <= listed:
        out.append(f"welcome picked catalogs the app did not list: {sorted(picked - listed)}")
    if 2 not in hello.get("rings", []) and w.get("mcpApps") is not None:
        out.append("welcome offers Ring 2 to an app that did not list it")
    if "features" in w:
        out += [f"welcome: {p}" for p in safety_problems(w["features"])]
    if "pisa.approval" in hello.get("extensions", {}) and "pisa.approval" not in (w.get("extensions") or {}):
        out.append("welcome did not grant pisa.approval, which the app listed: approvals are always on")
    for name, major in (w.get("extensions") or {}).items():
        if major > hello.get("extensions", {}).get(name, 0):
            out.append(f"welcome grants {name} {major} beyond the app's")
    for f in frames[1:]:
        ev = f.get("event") or {}
        if ev.get("activityType") == "mcp-apps" and w.get("mcpApps") is None:
            out.append(f"seq {f.get('seq')}: a Ring 2 view was sent without Ring 2")
        if ev.get("activityType") == "a2ui-surface":
            for op in (ev.get("content") or {}).get("a2ui_operations", []):
                cid = (op.get("createSurface") or {}).get("catalogId")
                if cid and cid not in picked:
                    out.append(f"seq {f.get('seq')}: surface in un-negotiated catalog {cid}")
    return out


def cmd_selftest(a: argparse.Namespace) -> int:
    ok = True
    print(f"pisa-conformance {__version__}, data at {ROOT}")
    for p in sorted((FIXTURES / "streams").glob("*.jsonl")):
        rep = check_server_frames(_read_jsonl(p), f"streams/{p.name}")
        expected_bad = p.stem == "unknown_component"  # deliberately off-catalog: an app must fall back
        if expected_bad:
            ok &= bool(rep.problems)
            print(f"{'PASS' if rep.problems else 'FAIL'} streams/{p.name}: refused as expected (off-catalog component)")
        else:
            ok &= rep.print()
    for p in sorted((FIXTURES / "enrol").glob("server_*.jsonl")):
        ok &= check_enrol_frames(_read_jsonl(p), f"enrol/{p.name}").print()
    ok &= check_enrol_frames(_read_jsonl(FIXTURES / "enrol" / "client_session.jsonl"), "enrol/client_session.jsonl").print()
    for kind, want in (("valid", True), ("invalid", False)):
        for p in sorted((FIXTURES / "gaps" / kind).glob("*.json")):
            fx = json.loads(p.read_text(encoding="utf-8"))
            check = {"client_frame": client_frame_errors, "server_frame": server_frame_errors,
                     "enrol_server_frame": enrol_server_frame_errors, "enrol_client_frame": enrol_client_frame_errors}[fx["check"]]
            good = not check(fx["frame"])
            if good != want:
                ok = False
                print(f"FAIL gaps/{kind}/{p.name}")
    print(f"{'PASS' if ok else 'FAIL'} gaps: {len(list((FIXTURES / 'gaps').rglob('*.json')))} fixtures")
    from .reference import pairing_bytes as PB
    pv = json.loads((FIXTURES / "pairing" / "vectors.json").read_text(encoding="utf-8"))
    pa, pin = pv["pairing"], pv["pairing"]["inputs"]
    tr = PB.transcript(pa["spkiPin"], pin["deviceKey"], pin["clientNonce"])
    tok = bytes.fromhex(pin["tokenHex"])
    pairing_ok = (PB.app_mac(tok, tr) == pa["appMac"] and PB.server_mac(tok, tr, pin["serverNonce"]) == pa["serverMac"]
                  and PB.sas(tr, pin["serverNonce"]) == pa["sas"] and PB.parse_qr(pa["qr"])["spki"] == pa["spkiPin"]
                  and PB.reveal_matches(pa["commitment"], pin["serverNonce"])
                  and not PB.reveal_matches(pa["revealMismatch"]["commitment"], pa["revealMismatch"]["revealedServerNonce"])
                  and all(PB.open_push(bytes.fromhex(pv["push"]["recipientPrivateKeyHex"]), pv["push"]["aad"], c["sealed"])
                          == json.loads(c["plaintext"]) for c in pv["push"]["cases"])
                  and PB.relay_headers(bytes.fromhex(pv["relay"]["secretHex"]), pv["relay"]["timestamp"], pv["relay"]["body"].encode())
                  == pv["relay"]["headers"])
    print(f"{'PASS' if pairing_ok else 'FAIL'} pairing/vectors.json: QR, transcript, MACs, commitment and reveal, SAS, relay signature, sealed pushes")
    ok &= pairing_ok
    dev = json.loads((FIXTURES / "device_ops" / "model_server.json").read_text(encoding="utf-8"))
    from .reference.device_ops import canonical, model_server_target
    bad = [c["name"] for c in dev["cases"]
           if c["challenge"]["target"] != model_server_target((json.loads(c["body"]).get("id") or c["path"].rsplit("/", 1)[1]), c["body"].encode())
           or c["canonical"] != canonical("model_server", c["challenge"]["target"], c["nonce"], c["expires_at"])]
    print(f"{'PASS' if not bad else 'FAIL'} device_ops/model_server.json: {len(dev['cases'])} vectors")
    ok &= not bad
    for name in ("server.json", "server-minimal.json"):
        disc = discovery_problems(json.loads((EXAMPLES / name).read_text()))
        print(f"{'PASS' if not disc else 'FAIL'} examples/{name}")
        ok &= not disc
    n = 0
    for kind, want in (("valid", True), ("invalid", False)):
        for p in sorted((FIXTURES / "discovery" / kind).glob("*.json")):
            n += 1
            if (not discovery_problems(json.loads(p.read_text(encoding="utf-8"))["document"])) != want:
                ok = False
                print(f"FAIL discovery/{kind}/{p.name}")
    print(f"PASS discovery: {n} fixtures" if ok else f"checked discovery: {n} fixtures")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pisa-conformance", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"pisa-conformance {__version__} (contract {CONTRACT})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stream", help="check recorded frames (JSON Lines)")
    s.add_argument("files", nargs="+")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--enrol", action="store_true", help="an enrolment data channel (pisa.voice.enrol.*)")
    g.add_argument("--client", action="store_true", help="app -> server frames")
    s.set_defaults(fn=cmd_stream)
    lv = sub.add_parser("live", help="connect to a live /v2/live endpoint and check what it sends")
    lv.add_argument("url", help="ws:// or wss:// URL of /v2/live")
    lv.add_argument("--token-env", help="name of the environment variable holding the session token (never pass the token itself)")
    lv.add_argument("--hello", help="a hello frame (JSON file); default: a conservative Ring 0+1 hello")
    lv.add_argument("--send", help="app frames (JSON Lines) to send after welcome, e.g. a run")
    lv.add_argument("--seconds", type=float, default=10.0)
    lv.add_argument("--max-frames", type=int, default=500)
    lv.add_argument("--record", help="write what was received to this JSON Lines file")
    lv.set_defaults(fn=cmd_live)
    d = sub.add_parser("discovery", help="check a GET /v1/server document (file or URL)")
    d.add_argument("source")
    d.set_defaults(fn=cmd_discovery)
    st = sub.add_parser("selftest", help="check every bundled fixture")
    st.set_defaults(fn=cmd_selftest)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
