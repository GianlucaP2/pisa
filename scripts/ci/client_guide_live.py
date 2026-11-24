"""The client guide's examples (`docs-site/build-a-client/examples/`), run against this server, live. Run from
core/ in CI: `uv run python ../scripts/ci/client_guide_live.py`.

Each example runs as its own process, exactly as a reader runs it (`python <example>.py --state <file>`), so the
guide's code is the code under test:
1. offline: the contract's vectors (`vectors.py`), every fixture stream drawn (`render.py`, `screens.py`), the
   voice chunks of a recorded reply assembled (`voice_economy.py --assemble`), and the frames a client sends on a
   talk's data channel checked by the conformance kit (`talk_webrtc.py --client-frames`);
2. pairing: the server makes a QR (`pisa pair --no-wait --json`), `pair.py` pairs from its text and prints the
   SAS, the server confirms it (`pisa pair --confirm`);
3. the rest in the guide's order, the device revoking and unpairing others last. `chat_live.py` records its
   frames, which the kit then checks (`pisa-conformance stream`, server and client frames).

The server is `live_server.py`'s (migrated, offline, the QR's pin a throwaway key); no model is chosen on it, so
every turn ends RUN_ERROR model_not_configured, which the examples handle. Exit status: 0 when every step passed.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from live_server import LiveServer, run

PORT = 8769
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs-site" / "build-a-client" / "examples"
FIXTURES = ROOT / "protocol" / "v2" / "fixtures"
STEP_TIMEOUT_S = 120
results: list[tuple[str, bool, float]] = []


def step(name: str, *args: str, env: dict[str, str], cwd: Path) -> bool:
    began = time.monotonic()
    print(f"\n=== {name}: {' '.join(args)}", flush=True)
    try:
        done = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=STEP_TIMEOUT_S)
        print(done.stdout, end="", flush=True)
        print(done.stderr, end="", file=sys.stderr, flush=True)
        ok = done.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
        print(f"timed out after {STEP_TIMEOUT_S} s", flush=True)
    results.append((name, ok, time.monotonic() - began))
    print(f"--- {'PASS' if ok else 'FAIL'} {name} ({time.monotonic() - began:.1f} s)", flush=True)
    return ok


def example(name: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, str(EXAMPLES / name), *args)


def offline(env: dict[str, str], work: Path) -> None:
    streams = sorted(str(p) for p in (FIXTURES / "streams").glob("*.jsonl"))
    events = sorted(str(p) for p in (FIXTURES / "events").glob("*.jsonl"))
    step("the contract's vectors", *example("vectors.py"), env=env, cwd=work)
    step("every fixture drawn", *example("render.py", *streams, *events), env=env, cwd=work)
    step("every surface drawn, its taps and acks", *example("screens.py", *streams), env=env, cwd=work)
    step("a recorded reply's voice assembled", *example("voice_economy.py", "--assemble",
         str(FIXTURES / "streams" / "turn_text.jsonl"), "--out", str(work / "reply")), env=env, cwd=work)  # fmt: skip
    frames = work / "talk-client-frames.jsonl"
    step("the talk channel's client frames written", *example("talk_webrtc.py", "--client-frames", str(frames)),
         env=env, cwd=work)  # fmt: skip
    # A talk's channel takes no hello, so `pisa-conformance stream --client` (which wants one first) does not fit
    # it: each frame is checked on its own with the kit's library.
    check = (
        "import json, sys; from pisa_conformance.validate import client_frame_errors as e; "
        "bad = [(f, e(f)) for f in map(json.loads, open(sys.argv[1])) if e(f)]; print(bad or 'all valid'); "
        "sys.exit(1 if bad else 0)"
    )
    step("the talk channel's client frames are contract 2's", sys.executable, "-c", check, str(frames),
         env=env, cwd=work)  # fmt: skip


def pair(server: LiveServer, env: dict[str, str], work: Path) -> bool:
    began = time.monotonic()
    made = run("pisa", "pair", "guide", "--no-wait", "--json", env=server.env, quiet=True)
    uri = json.loads(made.stdout.strip().splitlines()[-1])["uri"]
    print("\n=== pairing: pair.py with the QR's text, then pisa pair --confirm", flush=True)
    child = subprocess.Popen(
        [*example("pair.py", uri, "--server", server.base, "--name", "Guide client")],
        env=env, cwd=work, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert child.stdout is not None
    confirmed = False
    for line in child.stdout:
        print(line, end="", flush=True)
        if line.startswith("SAS ") and not confirmed:
            confirmed = run("pisa", "pair", "--confirm", line.split()[1], env=server.env).returncode == 0
    ok = child.wait(timeout=60) == 0 and confirmed
    results.append(("pairing (pair.py)", ok, time.monotonic() - began))
    print(f"--- {'PASS' if ok else 'FAIL'} pairing", flush=True)
    return ok


def live(server: LiveServer, env: dict[str, str], work: Path) -> None:
    step("session tokens", *example("session.py"), env=env, cwd=work)
    step("consent", *example("consent.py"), env=env, cwd=work)
    step("POST /v2/runs (SSE and JSON)", *example("chat_runs.py"), env=env, cwd=work)
    token = subprocess.run(example("session.py", "--print-token"), env=env, cwd=work, capture_output=True,
                           text=True, check=True).stdout.strip()  # fmt: skip
    step("POST /v2/runs with curl", "bash", str(EXAMPLES / "chat_runs.sh"),
         env=env | {"PISA_SERVER": server.base, "PISA_TOKEN": token}, cwd=work)  # fmt: skip
    step("/v2/live: hello, a run, cancel, resume", *example("chat_live.py", "--record", str(work / "live")),
         env=env, cwd=work)  # fmt: skip
    for n in (1, 2):  # the first connection, and the one that resumed it
        step(f"connection {n}'s server frames are contract 2's", "pisa-conformance", "stream",
             str(work / f"live-{n}.server.jsonl"), env=env, cwd=work)  # fmt: skip
        step(f"connection {n}'s client frames are contract 2's", "pisa-conformance", "stream", "--client",
             str(work / f"live-{n}.client.jsonl"), env=env, cwd=work)  # fmt: skip
    step(
        "voice in economy: partials, turn hints, a spoken turn",
        *example("voice_economy.py"),
        env=env,
        cwd=work,
    )
    step("natural voice: a WebRTC offer, and the fallback", *example("talk_webrtc.py"), env=env, cwd=work)
    step("approvals: refusals, and no challenge for an unknown one", *example("approvals.py"), env=env, cwd=work)
    step("push registration and /v1/changes", *example("push.py"), env=env, cwd=work)
    step("erase: refused unsigned, done signed with the device key", *example("erase.py"), env=env, cwd=work)
    step("errors", *example("errors.py"), env=env, cwd=work)
    step("devices: offer, confirm, list, revoke, unpair", *example("devices.py"), env=env, cwd=work)


def main() -> int:
    began = time.monotonic()
    with LiveServer(PORT, "pisa-client-guide-") as server:
        work = server.work / "client"
        work.mkdir()
        env = os.environ | {"PISA_STATE": str(work / "pisa-client.json"), "PYTHONDONTWRITEBYTECODE": "1"}
        offline(env, work)
        if pair(server, env, work):
            live(server, env, work)
        failed = [name for name, ok, _ in results if not ok]
        print("\n=== the client guide's examples", flush=True)
        for name, ok, seconds in results:
            print(f"{'PASS' if ok else 'FAIL'}  {seconds:5.1f} s  {name}", flush=True)
        print(
            f"client guide: {len(results) - len(failed)}/{len(results)} passed in {time.monotonic() - began:.0f} s"
        )
        if failed:
            server.dump_log()
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
