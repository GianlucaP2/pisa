"""Deleting the user's data by family and age.

    python erase.py [--state FILE]

The preview needs no signature; the delete always needs a signed device operation (`erase`), which this client
signs with its own device key, the key it registered at pairing (key id `device-<its device id>`). Unsigned, the
server answers 403 `biometric_required`.
"""

import asyncio
import base64
import sys
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pisa_example import PisaError, State, arguments, http_client
from session import Session

FAMILIES = ("messages", "places", "mail", "money", "records", "memory", "health", "voice", "learn")


# [guide:preview]
async def preview(session: Session, family: str, before: str) -> dict[str, Any]:
    """How many rows would go now, per table: counts only (`before` is a day, YYYY-MM-DD, in the user's zone)."""
    return await session.request("GET", "/v1/data/erase/preview", params={"family": family, "before": before})


# [/guide:preview]


# [guide:sign]
async def signed_headers(session: Session, op: str, target: str) -> dict[str, str]:
    """A signed device operation's three headers: the server's challenge text for (op, target), signed with this
    client's own device key (ECDSA P-256 SHA-256, DER, standard base64) under its key id `device-<device id>`.
    The nonce works once, for 5 minutes."""
    challenge = await session.request(
        "POST", "/v1/phone/challenge", json={"action": "device", "op": op, "target": target}
    )
    expected = f"pisa-device\nop={op}\ntarget={target}\n"
    if not challenge["canonical"].startswith(expected):  # sign only the operation you asked for
        raise ValueError("the challenge is not for this operation: do not sign it")
    signature = session.state.key.sign(challenge["canonical"].encode(), ec.ECDSA(hashes.SHA256()))
    return {
        "X-Pisa-Nonce": challenge["nonce"],
        "X-Pisa-Signature": base64.b64encode(signature).decode(),
        "X-Pisa-Key-Id": f"device-{session.state.device_id}",
    }


# [/guide:sign]


# [guide:erase]
async def erase(session: Session, family: str, before: str, signed: dict[str, str]) -> dict[str, Any]:
    """202 with the job {erase_id, state: queued, ...}; follow it with GET /v1/data/erase/{erase_id}.
    `signed`: the X-Pisa-Nonce, X-Pisa-Signature and X-Pisa-Key-Id headers over the challenge's text
    (POST /v1/phone/challenge {"action": "device", "op": "erase", "target": "<family>:<before>"})."""
    headers = await session.bearer() | signed
    answer = await session.http.post(
        "/v1/data/erase", json={"family": family, "before": before}, headers=headers
    )
    if answer.status_code != 202:
        raise PisaError(answer.status_code, answer.json())
    return answer.json()


# [/guide:erase]


async def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    state = State.load(args.state)
    async with http_client(state) as http:
        session = Session(http, state)
        for family in FAMILIES:
            counted = await preview(session, family, "2026-01-01")
            print(f"{family}: {counted['count']} rows before 2026-01-01")
        try:
            await erase(session, "messages", "2026-01-01", signed={})
            return 1
        except PisaError as err:
            print(f"unsigned erase: HTTP {err.status} {err.code}")
            if err.code != "biometric_required":
                return 1
        job = await erase(
            session, "messages", "2026-01-01", await signed_headers(session, "erase", "messages:2026-01-01")
        )
        print(f"signed erase: {job['state']} ({job['erase_id']})")
        for _ in range(50):  # the job runs in the background
            job = await session.request("GET", f"/v1/data/erase/{job['erase_id']}")
            if job["state"] in ("done", "failed"):
                break
            await asyncio.sleep(0.2)
        print(f"the job: {job['state']}, {job['erased']} rows")
        return 0 if job["state"] == "done" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
