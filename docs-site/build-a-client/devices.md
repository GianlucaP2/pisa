# More than one device

Each device has its own key, its own session tokens and its own push registration; the conversation and the data
are the user's, shared by all of them. One device has one live connection at a time (a newer one replaces it:
close code 4001).

Example: [`devices.py`](examples/devices.py) pairs two more devices from the first one, lists them, revokes one
with its socket open, and lets the other unpair itself.

## Pairing a second device

Besides `pisa pair` on the server, a paired device can offer a pairing code for a new one, so the user needs no
server console:

1. The paired device: `POST /v1/pair/offers {"name"}` → `201 {offerId, state: "open", qr, token, expiresAt}`.
   It shows the QR (and the typed `token`).
2. The new device pairs with it exactly as with a server's QR ([Pairing](pairing.md#the-exchange)) and shows the
   SAS.
3. The paired device polls `GET /v1/pair/offers/{offerId}`: at `awaiting_confirmation` it carries the new
   device's name and the transcript's fields (`spki`, `deviceKey`, `clientNonce`, `commitment`, `serverNonce`).
   It checks the reveal against the commitment, computes the same SAS and shows it.
4. The user compares the two; yes is `POST /v1/pair/offers/{offerId}/confirm {pairingId, sas}`, no is
   `DELETE /v1/pair/offers/{offerId}`. The new device's `GET /v1/pair/{pairingId}` turns `confirmed`.

Only the device that made the offer sees it (404 for any other). An offer body with an unknown field (`signer`
included) is 400 `validation_failed`: a device that may sign device operations is made only on the server.

<!-- include: examples/devices.py#offer -->
```python
async def offer_new_device(session: Session, name: str) -> dict[str, Any]:
    """This device offers a pairing code for a new one: {offerId, qr, token, expiresAt} (10 minutes)."""
    return await session.request("POST", "/v1/pair/offers", json={"name": name})


async def confirm_new_device(session: Session, offer_id: str) -> dict[str, Any]:
    """Once the new device sent its pairing, show the same SAS on this device; the user compares them."""
    while (state := await session.request("GET", f"/v1/pair/offers/{offer_id}"))["state"] == "open":
        await asyncio.sleep(1)
    if state["state"] != "awaiting_confirmation":
        raise SystemExit(f"the offer ended {state['state']}")
    t = state["transcript"]  # {spki, deviceKey, clientNonce, commitment, serverNonce}
    if commitment(t["serverNonce"]) != t["commitment"]:
        raise SystemExit("the server revealed another nonce than it committed to: refuse")
    digits = sas(transcript(t["spki"], t["deviceKey"], t["clientNonce"]), t["serverNonce"])
    print(f"'{state['newDevice']['name']}' shows {digits}? (the user says yes)")
    # A refusal is DELETE /v1/pair/offers/{offerId}.
    return await session.request(
        "POST", f"/v1/pair/offers/{offer_id}/confirm", json={"pairingId": state["pairingId"], "sas": digits}
    )
```

The offer and its confirmation are signed device operations (`pairing_offer`, target the SHA-256 hex of the
request body; `pairing_confirm`, target `<pairingId>:<sas>`) once the server holds a phone key; until then they
pass unsigned ([Deleting data](erase.md#the-delete) shows the signing headers).

## Listing

`GET /v1/devices` → `{devices: [{id, name, platform, created_at, last_seen_at, revoked_at, this_device}]}`.

<!-- include: examples/devices.py#list -->
```python
for d in (await session.request("GET", "/v1/devices"))["devices"]:
    print(f"{d['name']:8} {d['platform']:6} last seen {d['last_seen_at']} revoked {d['revoked_at']}"
          f"{'  (this one)' if d['this_device'] else ''}")  # fmt: skip
```

## Revoking and unpairing

`DELETE /v1/devices/{id}` revokes another device (a signed `revoke_device` operation, target its id, once the
server holds a phone key). At once its live socket gets a control error `device_revoked` and closes with 4003,
its tokens answer 401 `device_revoked`, and its relay registration is forgotten. On the server:
`pisa devices revoke <id>`.

<!-- include: examples/devices.py#revoke -->
```python
await session.request("DELETE", f"/v1/devices/{tablet.device_id}")  # from another device
try:
    while True:
        # a last control error device_revoked, then the close
        await asyncio.wait_for(ws.recv(), 10)
except websockets.ConnectionClosed as closed:
    code = closed.rcvd.code if closed.rcvd else None
```

`DELETE /v1/devices/me` unpairs the device itself: revoked, with its push registration and keys deleted. The last
device may leave too; pairing again then needs the server (`pisa pair`).

<!-- include: examples/devices.py#unpair -->
```python
laptop_session = Session(http, laptop)
await laptop_session.request("DELETE", "/v1/devices/me")  # unpair and delete: keys, push, tokens gone
```

```text
$ python devices.py
'Tablet' shows 570071? (the user says yes)
paired Tablet: confirmed, device 63a14c77-...
'Laptop' shows 382777? (the user says yes)
paired Laptop: confirmed, device 51865180-...
Guide client other  last seen 2026-11-18T21:10:37+00:00 revoked None  (this one)
Tablet   other  last seen None revoked None
Laptop   other  last seen None revoked None
the tablet's socket closed 4003
the tablet's token afterwards: HTTP 401 device_revoked
the tablet asks for a new token: HTTP 400 validation_failed
the laptop after unpairing itself: HTTP 401
```
