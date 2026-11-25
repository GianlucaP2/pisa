# Deleting data

The user can delete one family of their data older than a day: `messages` (conversations), `places` (GPS
points, visits, stays, place notes, residency, trips), `mail` (Pisa's copies of email), `money`, `records`,
`memory` (Pisa's memory, Memoria's standing instructions and notes too), `health`, `voice` or `learn`. See [Privacy](../privacy.md) for what each holds.

Example: [`erase.py`](examples/erase.py).

## Preview

`GET /v1/data/erase/preview?family=<family>&before=<YYYY-MM-DD>` counts what would go now, per table (counts
only; `before` is a day in the user's zone). No signature.

<!-- include: examples/erase.py#preview -->
```python
async def preview(session: Session, family: str, before: str) -> dict[str, Any]:
    """How many rows would go now, per table: counts only (`before` is a day, YYYY-MM-DD, in the user's zone)."""
    return await session.request("GET", "/v1/data/erase/preview", params={"family": family, "before": before})
```

## The delete

`POST /v1/data/erase {family, before}` → `202` with the job `{erase_id, state: "queued", ...}`. It runs in the
background; `GET /v1/data/erase/{erase_id}` follows its state (`queued`, `running`, `done`, `failed`) and counts,
and `GET /v1/data/erase` lists the latest jobs (a client that lost the 202 finds its own there). The same family
and day while a job is queued or running answers that job, not a second one. Tomorrow means all of it: every
row up to the moment of the request, today's too. A day after tomorrow: 400 `validation_failed`.

It always needs a signed device operation, carried in three headers: get the text to sign with
`POST /v1/phone/challenge {"action": "device", "op": "erase", "target": "<family>:<before>"}` →
`{nonce, canonical, expires_at}`, where `canonical` is

```text
pisa-device
op=erase
target=messages:2026-01-01
nonce=0ND6Jy9zSby1QOXYM_335Be3jPWJbknf
expires_at=2026-11-18T22:05:38.799864+01:00
```

then send `X-Pisa-Nonce: <nonce>`, `X-Pisa-Signature: <ECDSA P-256 SHA-256 over canonical, DER, standard
base64>`, `X-Pisa-Key-Id: device-<your device id>` with the request. The key is your client's own device key,
the one it registered at pairing: no other key and no app is needed. The nonce works once and for 5 minutes,
and a revoked device's key signs nothing.

<!-- include: examples/erase.py#sign -->
```python
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
```

<!-- include: examples/erase.py#erase -->
```python
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
```

## A device's conversations

`DELETE /v1/conversations/conversation:<your device id>` → `204` deletes this device's conversations, every
one: their messages and what Pisa derived from them (the topics, questions and idea fragments they opened, the values its
talks read from outside, its research findings and drawings), its REST turns and its talks. It is not a family
delete: no day, and nothing else goes. Tasks, reminders and Memoria (standing instructions and notes) stay, as
do places and everything another device said.

The server keeps one thread id per device, `conversation:<deviceId>`, holding its conversations over time (a new
one after 30 minutes of silence, or at `POST /v1/conversations/new`, [Text chat](text-chat.md#a-new-conversation));
the thread ids a client gives its own runs (`chat-...`) all land in it, so this is the id to send. Only that device can delete it: another
device's thread is 403 `other_device`, a thread no device has is 404 `not_found`. Deleting it again is a `204`
too.

It is always signed, like the delete above: `POST /v1/phone/challenge {"action": "device", "op":
"conversation", "target": "conversation:<your device id>"}` (the device id in lowercase), then the three
headers with the request. `pisa client delete-conversation` does it for the command-line client.

```text
pisa-device
op=conversation
target=conversation:5b0c3f2a-1d4e-4f7a-8b6c-5d4e3f2a1b0c
nonce=0ND6Jy9zSby1QOXYM_335Be3jPWJbknf
expires_at=2026-11-18T22:05:38.799864+01:00
```

## Run it

```text
$ python erase.py
messages: 0 rows before 2026-01-01
...
learn: 0 rows before 2026-01-01
unsigned erase: HTTP 403 biometric_required
signed erase: queued (5b0c...)
the job: done, 0 rows
```

The other signed device operations (`provider_key`, `model_server`, `connector`, `revoke_device`,
`pairing_offer`, `pairing_confirm`...) use the same challenge and headers with your device key,
with their own `op` and `target` (each endpoint's description names them). Unlike the deletes (and the paid OK,
`paid_ack`), those are checked
only once the server holds a Face ID key (the PISA app's); until then they pass unsigned.
