"""Approvals: what a client shows before the user says yes, the digest it recomputes, and the decision it sends.

    python approvals.py [--state FILE]

A yes on an approval with `requiresBiometric: true` is signed with this client's own device key (key id
`device-<its device id>`); a reject, and a yes on an approval that does not require a signature, need no
signature at all.

Live, it lists the approvals (none can be raised on a server without a model or Google) and shows the refusals a
client gets for an approval that does not exist. `vectors.py` checks `jcs`, `approval_digest` and
`canonical_text` against the contract's approval vectors.
"""

import asyncio
import base64
import hashlib
import json
import math
import sys
import uuid
from typing import Any

import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pisa_example import PisaError, State, arguments, http_client, ws_url
from session import Session


# [guide:jcs]
def jcs(value: Any) -> str:
    """RFC 8785 JSON Canonicalization: sorted keys (by UTF-16 code units), no spaces, ECMAScript numbers."""
    if value is None or isinstance(value, bool):
        return {None: "null", True: "true", False: "false"}[value]
    if isinstance(value, int) and abs(value) < 2**53:
        return str(value)
    if isinstance(value, int | float):
        return _number(float(value))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + jcs(v) for k, v in items) + "}"
    raise TypeError(type(value))


def _number(value: float) -> str:
    """ECMAScript Number::toString of a finite double (the digits from Python's shortest repr)."""
    if not math.isfinite(value):
        raise ValueError("JCS has no NaN or Infinity")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + _number(-value)
    mantissa, _, exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    n = len(whole) + int(exponent or 0) - (len(whole + fraction) - len(digits))
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    return digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))


# [/guide:jcs]


# [guide:digest]
def approval_digest(kind: str, payload: dict[str, Any], recipients: list[str]) -> str:
    """`pisa-approval-digest/2`: what the interrupt's `metadata.pisa.digest` must equal."""
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
    normalised = ",".join(sorted({r.strip().lower() for r in recipients if r.strip()}))
    return sha("\n".join(["pisa-approval-digest/2", kind, sha(jcs(payload)), normalised]))


async def may_offer_approve(session: Session, interrupt: dict[str, Any]) -> bool:
    """Offer Approve only when what you show hashes to the interrupt's digest."""
    meta = interrupt["metadata"]["pisa"]
    detail = await session.request("GET", f"/v1/approvals/{meta['approvalId']}")
    # Show: detail["summary"], detail["details"] (up to 8 lines), detail["preview"] (an email's whole text, as
    # quoted outside text), detail["recipients"], and when it expires (interrupt["expiresAt"]).
    return approval_digest(meta["kind"], detail["payload"], detail["recipients"]) == meta["digest"]


# [/guide:digest]


# [guide:canonical]
CANONICAL_KEYS = ("server", "device", "approval_id", "interrupt_id", "decision", "kind", "digest",
                  "recipients_count", "nonce", "expires_at")  # fmt: skip


def canonical_text(fields: dict[str, Any]) -> str:
    """`pisa-approval/2`: the exact bytes signed for a yes. The server sends this text as the challenge's
    `canonical`; a client parses it, checks every line against the interrupt it shows, and refuses anything
    else (an extra line, another order, another header)."""
    return "\n".join(["pisa-approval/2", *(f"{k}={fields[k]}" for k in CANONICAL_KEYS)])


def parse_canonical(text: str) -> dict[str, str]:
    lines = text.split("\n")
    if lines[0] != "pisa-approval/2" or [line.partition("=")[0] for line in lines[1:]] != list(
        CANONICAL_KEYS
    ):
        raise ValueError("not a pisa-approval/2 text: do not sign it")
    return dict(line.partition("=")[::2] for line in lines[1:])


# [/guide:canonical]


# [guide:sign]
async def signed_yes(session: Session, interrupt: dict[str, Any], server_id: str) -> dict[str, Any]:
    """The decision for a yes that needs a signature: the server's `pisa-approval/2` text, checked line by line
    against the interrupt shown, signed with this client's own device key."""
    meta = interrupt["metadata"]["pisa"]
    challenge = await session.request(
        "POST", "/v1/phone/challenge",
        json={"action": "approval", "approval_id": meta["approvalId"], "interrupt_id": interrupt["id"]},
    )  # fmt: skip
    fields = parse_canonical(challenge["canonical"])
    mine = {
        "server": server_id, "device": session.state.device_id, "approval_id": meta["approvalId"],
        "interrupt_id": interrupt["id"], "decision": "approve", "kind": meta["kind"], "digest": meta["digest"],
        "recipients_count": str(meta["recipientsCount"]), "nonce": challenge["nonce"],
        "expires_at": challenge["expires_at"],
    }  # fmt: skip
    if canonical_text(mine) != challenge["canonical"] or fields != mine:
        raise ValueError("the server's text is not the approval shown: do not sign it")
    signature = session.state.key.sign(challenge["canonical"].encode(), ec.ECDSA(hashes.SHA256()))
    return {"decision": "approve", "auth": "biometric", "digest": meta["digest"], "nonce": challenge["nonce"],
            "signature": base64.b64encode(signature).decode(), "keyId": f"device-{session.state.device_id}"}  # fmt: skip


# [/guide:sign]


# [guide:resume]
def resume_run(interrupt: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """The run that answers an approval's interrupt on `/v2/live` (`op: run`) or `POST /v2/runs`.
    `decision` is `{"decision": "approve"|"reject", "auth": "tap"|"voice"|"biometric", "digest"}`, plus
    `nonce`, `signature` and `keyId` for `biometric`. The same object is the body of
    `POST /v1/approvals/{approvalId}/resume`."""
    approval_id = interrupt["metadata"]["pisa"]["approvalId"]
    return {
        "threadId": f"pisa-approval:{approval_id}",
        "runId": f"run-{uuid.uuid4()}",
        "messages": [],
        "resume": [{"interruptId": interrupt["id"], "status": "resolved", "payload": decision}],
    }


# [/guide:resume]


async def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    state = State.load(args.state)
    failures = 0
    async with http_client(state) as http:
        session = Session(http, state)
        pending = await session.request("GET", "/v1/approvals")  # ?status=all for the last 30 days
        print(f"pending approvals: {len(pending['approvals'])}")
        unknown = str(uuid.uuid4())
        reject = {"decision": "reject", "auth": "tap", "digest": "0" * 64}
        try:
            await session.request("POST", f"/v1/approvals/{unknown}/resume", json=reject)
            failures += 1
        except PisaError as err:
            print(f"REST resume of an unknown approval: HTTP {err.status} {err.code}")
            failures += (err.status, err.code) != (404, "not_found")
        interrupt = {"id": f"int-{unknown}", "metadata": {"pisa": {"approvalId": unknown}}}
        try:  # no challenge is made for an approval that does not exist
            await signed_yes(session, interrupt, "unused")
            failures += 1
        except PisaError as err:
            print(f"challenge for an unknown approval: HTTP {err.status} {err.code}")
            failures += (err.status, err.code) != (404, "not_found")
        headers = await session.bearer()
    async with websockets.connect(
        ws_url(state.server), additional_headers=headers, subprotocols=["pisa.v2"]
    ) as ws:
        from chat_live import hello

        await ws.send(json.dumps(hello(state.device_id)))
        json.loads(await ws.recv())  # welcome
        await ws.send(json.dumps({"op": "run", "input": resume_run(interrupt, reject)}))
        while "control" not in (frame := json.loads(await asyncio.wait_for(ws.recv(), 10))):
            pass
        print(f"/v2/live resume of an unknown approval: control {frame['control']} {frame.get('code')}")
        failures += frame.get("code") != "invalid_frame"
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
