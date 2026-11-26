# Approvals

Before Pisa acts outside itself (sends an email, creates, moves or cancels a calendar event), it asks. The
question reaches the client as an AG-UI interrupt; the user's answer goes back as the run that resumes it.

Example: [`approvals.py`](examples/approvals.py). [`vectors.py`](examples/vectors.py) checks its digest, JCS and
canonical text against the contract's approval vectors (`protocol/v2/fixtures/approval_vectors.json`).

## The interrupt

Each approval has its own thread, `pisa-approval:<approvalId>`. A short run on it ends with `RUN_FINISHED`
`outcome: {"type": "interrupt", "interrupts": [...]}` (`$defs/ApprovalInterrupt`):

```json
{"id": "int-33333333-3333-4333-8333-333333333333", "reason": "pisa.approval",
 "message": "Send an email · notifies 1", "expiresAt": "2026-11-18T10:10:00Z",
 "responseSchema": {"type": "object", "required": ["decision", "auth", "digest"], "...": "..."},
 "metadata": {"pisa": {"approvalId": "33333333-3333-4333-8333-333333333333", "kind": "email_send",
   "risk": "R2", "requiresBiometric": true,
   "digest": "305b4add67d0bbc45673988e8944f6f88fabedcc24ac9aa912a2b3457c994bb7",
   "recipientsCount": 1, "undoMinutes": null,
   "detailsRef": "approval:33333333-3333-4333-8333-333333333333", "server": "srv_synthetic0001_Qm7Zr2Xc"}}}
```

(from `protocol/v2/fixtures/streams/approval.jsonl`). The conversation never waits for it: the user may answer
now, later, from a notification, or by voice. `message` is the server's own template (a kind and a count, never
outside text). An approval expires 10 minutes after it was made. A client that was not connected finds the
pending ones with `GET /v1/approvals` (`?status=all` for the last 30 days).

## What to show

`GET /v1/approvals/{approvalId}` gives what to show and what was hashed: `summary`, `details` (up to 8 lines:
what, when, where, who is notified), `preview` (an email's whole text), `payload` (the action exactly as it will
run) and `recipients` (every address it writes to or notifies). Show the summary and details, the preview as
quoted outside text, every recipient, and the time left. Then recompute the digest from `payload` and
`recipients` and **offer Approve only when it equals the interrupt's `digest`**: otherwise what the user sees is
not what would run.

`pisa-approval-digest/2` = `sha256_hex("pisa-approval-digest/2\n" + kind + "\n" + sha256_hex(JCS(payload)) +
"\n" + recipients)`, where JCS is RFC 8785 and `recipients` are lowercased, trimmed, deduplicated, sorted and
joined by `,`. Hash `payload` exactly as the server sent it (parsed, then JCS), never one rebuilt from what you
show: the vectors' `payloadAsSent` cases show what goes wrong otherwise.

<!-- include: examples/approvals.py#jcs -->
```python
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
```

<!-- include: examples/approvals.py#digest -->
```python
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
```

## The decision

A decision is the run that resumes the interrupt, on the approval's thread, with one resume entry; the same
payload (`$defs/ApprovalResume`) is the body of `POST /v1/approvals/{approvalId}/resume` for a client without a
socket:

| Field | Value |
|---|---|
| `decision` | `approve` or `reject` |
| `auth` | `tap`, `voice` or `biometric` |
| `digest` | the interrupt's digest, as the client recomputed it |
| `nonce`, `signature`, `keyId` | only with `auth: biometric` (below) |

<!-- include: examples/approvals.py#resume -->
```python
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
```

- **A reject** needs no signature, ever: `{"decision": "reject", "auth": "tap", "digest"}`. The run ends with
  success and nothing else.
- **A yes on an approval with `requiresBiometric: false`** (a calendar change Pisa proposed): `auth: tap`.
- **A yes on an approval with `requiresBiometric: true`** (every email): `auth: biometric` with a signature.
- `status: "cancelled"` instead of `resolved` lets the interrupt go without deciding: the approval stays pending
  until it is decided elsewhere or expires.

What comes back on the approval's thread: `RUN_STARTED`, then `CUSTOM pisa.action_done {approvalId, kind,
summary, result, undoUntil?}` when the action ran, then `RUN_FINISHED`; or `RUN_ERROR` with one of
`approval_changed` (what was shown is no longer what would run: fetch it again), `approval_expired`,
`approval_decided` (decided elsewhere), `biometric_required` (a yes that needed a signature), `action_failed`
(it ran and failed: show the message). Over REST the refusals are 404 `not_found`, 409 `approval_decided` or
`approval_changed`, 410 `approval_expired`, 401 `biometric_required`; a decision taken answers 200 with the
resuming run's events (or 202 with `runId` when the action is still running after 8 seconds). A resume of an
approval that does not exist starts no run on `/v2/live`: a control error `invalid_frame`.

Decided anywhere else (by voice, on another device, from a notification, by expiry), the server sends
`pisa.approval.state {approvalId, interruptId, state, by}` on the thread: drop the interrupt and never resume
it. Calendar actions can be undone until `undo_until` with `POST /v1/approvals/{approvalId}/undo`.

## Signing a yes

The signed text is `pisa-approval/2`: fixed keys in a fixed order, one per line, joined by `\n`, no trailing
newline. Every value is a server id, an enum, a count, a time or a hex digest; outside text reaches the signature
only through the digest.

```text
pisa-approval/2
server=srv_synthetic0001_Qm7Zr2Xc
device=00000000-0000-4000-8000-0000000000d1
approval_id=33333333-3333-4333-8333-333333333333
interrupt_id=int-1
decision=approve
kind=email_send
digest=5ff647901c4d2155fc5572a5e716e6256e620a5cde22dac535727b5ce9b877f6
recipients_count=1
nonce=bm9uY2UtMQ==
expires_at=2026-11-18T10:05:00Z
```

The server makes the text: `POST /v1/phone/challenge {"action": "approval", "approval_id", "interrupt_id"}`
answers `{nonce, canonical, expires_at, action}`. The client parses `canonical`, checks every line against the
interrupt it is showing (server, its own device id, the approval, the interrupt, `approve`, the kind, the digest
it recomputed, the count), refuses to sign anything else (an extra line, another order, another header), signs
the exact bytes with ECDSA P-256 SHA-256 (DER, standard base64), and resumes with `auth: biometric`, the
challenge's `nonce`, the `signature` and its `keyId`. The nonce is spent at the first try, success or not.

<!-- include: examples/approvals.py#canonical -->
```python
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
```

The key is your client's own device key, the one it registered at pairing, and its `keyId` is
`device-<your device id>`: any client approves with it, no app or Face ID needed (`auth: biometric` is the
contract's word for a signed yes). The challenge is bound to the device that asked for it, and only that
device's key signs it; a revoked device's key signs nothing. (The PISA app signs with its Face ID key instead,
and a watch only rejects.)

<!-- include: examples/approvals.py#sign -->
```python
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
```

## Run it

```text
$ python approvals.py
pending approvals: 0
REST resume of an unknown approval: HTTP 404 not_found
challenge for an unknown approval: HTTP 404 not_found
/v2/live resume of an unknown approval: control error invalid_frame
```

A server with no model and no Google account raises no approvals, so CI exercises the refusals, and the digest,
JCS and canonical text against the vectors (`vectors.py`); the recorded streams `approval.jsonl` and
`approval_failed.jsonl` show a whole round trip.
