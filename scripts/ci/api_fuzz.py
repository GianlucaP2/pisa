"""Schemathesis against this server, live. Run from core/ in CI: `uv run python ../scripts/ci/api_fuzz.py`.

It starts `pisa serve-api` on PISA_DATABASE_URL and pairs a device (`live_server.py`, as the conformance step
does), consents to the AI disclosure, then runs Schemathesis over `protocol/openapi.json` (the document
`scripts/ci/gen_openapi.py` writes) with that device's session token, checking:
- `no_server_fault` (`fuzz_hooks.py`): no 5xx; the one exception is a 502 that says which upstream failed,
  the documented answer when a provider is unreachable, as every provider is here;
- `status_code_conformance`: every status is one the operation documents;
- `content_type_conformance` and `response_schema_conformance`: every answer is the documented shape.

Bounded for CI: PISA_FUZZ_EXAMPLES test cases per operation (default 50), a fixed seed (PISA_FUZZ_SEED) so a
failure reproduces, and the examples, coverage and fuzzing phases (no stateful phase: the document has no
links).
The server reaches nothing outside the machine (`offline_serve.py`): a made-up provider key the fuzzer stores
is never sent to the provider.

Exit status: Schemathesis's (0 when every check passed).
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from live_server import LiveServer

PORT = 8766
OPENAPI = Path(__file__).resolve().parents[2] / "protocol" / "openapi.json"
HOOKS = Path(__file__).with_name("fuzz_hooks.py")
CHECKS = "no_server_fault,status_code_conformance,content_type_conformance,response_schema_conformance"

# The only operations not fuzzed, each because it cannot run safely or headlessly here. A route that merely
# fails is never excluded: it is a bug to fix. Keyed by operation id (`POST /v1/data/erase` is
# `erase_v1_data_erase_post`).
EXCLUDED: dict[str, str] = {
    "erase_v1_data_erase_post": "erases everything (the fuzzer's own device and session included)",
    "unpair_v1_devices_me_delete": "unpairs the fuzzer's own device: every later request would be 401",
}


def main() -> int:
    examples = os.environ.get("PISA_FUZZ_EXAMPLES", "50")
    seed = os.environ.get("PISA_FUZZ_SEED", "20261118")
    with LiveServer(PORT, "pisa-fuzz-") as server:

        async def device() -> str:
            paired = await server.pair("schemathesis")
            await server.consent(paired)
            return paired.token

        token = asyncio.run(device())
        excluded = [arg for operation in EXCLUDED for arg in ("--exclude-operation-id", operation)]
        print("excluded:", *(f"  {o}: {why}" for o, why in EXCLUDED.items()), sep="\n", flush=True)
        command = [
            "schemathesis", "run", str(OPENAPI), "--url", server.base,
            "--header", f"Authorization: Bearer {token}",
            "--checks", CHECKS, "--phases", "examples,coverage,fuzzing",
            "--max-examples", examples, "--seed", seed, "--workers", "4", "--request-timeout", "30",
            "--output-truncate", "true", "--no-color", "--generation-database", "none",
            *excluded,
        ]  # fmt: skip
        print("$ schemathesis run ...", flush=True)  # the command carries the token: not printed
        began = time.monotonic()
        env = server.env | {"NO_COLOR": "1", "SCHEMATHESIS_HOOKS": str(HOOKS)}
        done = subprocess.run(command, env=env, cwd=server.work, timeout=900)  # its crash cache: thrown away
        print(f"schemathesis: exit {done.returncode} in {time.monotonic() - began:.0f} s", flush=True)
        if done.returncode != 0:
            server.dump_log()
        return done.returncode


if __name__ == "__main__":
    sys.exit(main())
