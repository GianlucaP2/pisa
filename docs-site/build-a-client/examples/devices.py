"""More than one device: a paired client pairs a new one (no server console needed), lists, revokes and unpairs.

    python devices.py [--state FILE]

Each device has its own key and its own session tokens; they share the conversation and the data. A revoked
device's socket closes with 4003 and its tokens stop working at once.

An offer and its confirmation are unsigned while the server holds no Face ID key; once it holds one they are
signed device operations (`pairing_offer`, `pairing_confirm`), like revoking another device (`revoke_device`),
signed with this client's own device key. See erase.md for signing.
"""

import asyncio
import json
import sys
from typing import Any

import httpx
import websockets
from chat_live import hello
from pair import commitment, new_device_key, pair, parse_qr, sas, transcript
from pisa_example import PisaError, State, arguments, checked, http_client, tls_context, ws_url
from session import Session, new_token


# [guide:offer]
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


# [/guide:offer]


async def new_device(session: Session, server: str, name: str) -> State:
    """The new device's side, in-process here (pair.py is the same code): it pairs with the offer's QR."""
    offer = await offer_new_device(session, name)
    key, device_key = new_device_key()
    qr = parse_qr(offer["qr"])
    async with httpx.AsyncClient(
        base_url=server, verify=tls_context(server, [qr["spki"]]), timeout=30
    ) as http:
        started = asyncio.create_task(pair(http, qr, device_key, name))
        confirmed = await confirm_new_device(session, offer["offerId"])
        await started
    print(f"paired {name}: {confirmed['state']}, device {confirmed['deviceId']}")
    return State(session.state.path, {"server": server, "deviceId": confirmed["deviceId"], "key": _pem(key)})


def _pem(key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


async def main() -> int:
    args = arguments(__doc__ or "").parse_args()
    me = State.load(args.state)
    failures = 0
    async with http_client(me) as http:
        session = Session(http, me)
        tablet = await new_device(session, me.server, "Tablet")
        laptop = await new_device(session, me.server, "Laptop")

        # [guide:list]
        for d in (await session.request("GET", "/v1/devices"))["devices"]:
            print(f"{d['name']:8} {d['platform']:6} last seen {d['last_seen_at']} revoked {d['revoked_at']}"
                  f"{'  (this one)' if d['this_device'] else ''}")  # fmt: skip
        # [/guide:list]

        tablet_token, _ = await new_token(http, tablet)
        headers = {"Authorization": f"Bearer {tablet_token}"}
        async with websockets.connect(
            ws_url(me.server), additional_headers=headers, subprotocols=["pisa.v2"]
        ) as ws:
            await ws.send(json.dumps(hello(tablet.device_id)))
            await ws.recv()  # welcome
            # [guide:revoke]
            await session.request("DELETE", f"/v1/devices/{tablet.device_id}")  # from another device
            try:
                while True:
                    # a last control error device_revoked, then the close
                    await asyncio.wait_for(ws.recv(), 10)
            except websockets.ConnectionClosed as closed:
                code = closed.rcvd.code if closed.rcvd else None
            # [/guide:revoke]
        print(f"the tablet's socket closed {code}")
        failures += code != 4003
        answer = await http.get("/v1/devices", headers=headers)
        print(f"the tablet's token afterwards: HTTP {answer.status_code} {answer.json()['error']['code']}")
        failures += answer.status_code != 401
        try:
            await new_token(http, tablet)
            failures += 1
        except PisaError as err:  # 400 validation_failed at the nonce: "There is no such paired device."
            print(f"the tablet asks for a new token: HTTP {err.status} {err.code}")  # pair again
            failures += (err.status, err.code) != (400, "validation_failed")

        # [guide:unpair]
        laptop_session = Session(http, laptop)
        await laptop_session.request("DELETE", "/v1/devices/me")  # unpair and delete: keys, push, tokens gone
        # [/guide:unpair]
        answer = await http.get("/v1/devices", headers={"Authorization": f"Bearer {laptop_session.token}"})
        print(f"the laptop after unpairing itself: HTTP {answer.status_code}")
        failures += answer.status_code != 401
        checked(await http.get("/health"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
