"""Contract 2's privacy revision over REST (CONTRACT-2 §3.4, §4.3-§4.5):
- QR pairing: `POST /v1/pair/begin` (the server's commitment), `POST /v1/pair` (the reveal) and
  `GET /v1/pair/{pairingId}` (no auth: the HMAC under the QR's token is the proof, and the user confirms on
  the server side);
- pairing a new device from a paired phone (proposed): `POST /v1/pair/offers` (signed `pairing_offer`),
  `GET` and `DELETE /v1/pair/offers/{offerId}`, `POST /v1/pair/offers/{offerId}/confirm` (signed
  `pairing_confirm`);
- session tokens from the device key: `GET /v1/devices/session/nonce`, `POST /v1/devices/session` (no auth:
  the device key's signature is the proof);
- unpair and delete: `DELETE /v1/devices/me`;
- the server's addresses and TLS pins: `GET /v1/server/endpoints`;
- the AI disclosure and the user's consent: `GET /v1/ai/disclosure`, `POST /v1/ai/consent`.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import proxies as proxies_rules
from pisa.logging import get_logger
from pisa.services import devices, disclosure, endpoints, pairing
from pisa.services.phone_keys import digest

_log = get_logger("pisa.api.pairing")
router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)
NO_AUTH: dict[str, Any] = {"security": []}


@router.post(
    "/pair/begin", status_code=201, response_model=schemas.PairBegin, tags=["pairing"], openapi_extra=NO_AUTH
)
async def pair_begin(request: Request, runtime: RuntimeDep) -> dict[str, Any]:
    """Begin pairing (CONTRACT-2 §4.3 step 3): the server picks its nonce now and sends only its commitment,
    before it can see the app's transcript; `POST /v1/pair` reveals it. No auth and no token. At most 20 begun
    and unused per client address (its IPv6 /64 or IPv4 address; 429 `rate_limited`), and none for an address
    past the backstop (60 failed pairing requests in 10 minutes: 429)."""
    if not endpoints.pins(runtime.settings):
        raise endpoints.not_configured()
    async with runtime.pool.connection() as conn:
        return await pairing.begin(conn, source=_client_address(request, runtime))


@router.post(
    "/pair", status_code=201, response_model=schemas.QrPairResponse, tags=["pairing"], openapi_extra=NO_AUTH
)
async def pair(body: schemas.QrPairRequest, request: Request, runtime: RuntimeDep) -> dict[str, Any]:
    """Pair with the QR's one-time token (CONTRACT-2 §4.3): `pairingId` names the begun pairing (one request
    each), and the request's HMAC must match an open token, which it spends. The answer reveals the server's
    nonce committed to at the begin, with the server's HMAC; both sides then show the SAS, and the user
    confirms on the server (`pisa pair`). Poll `GET /v1/pair/{pairingId}` until `confirmed`. A failed request
    never spends or blocks a token; only a flood is paused: 60 failed requests in 10 minutes from one client
    address (its IPv6 /64 or IPv4 address) give 429 `rate_limited` on `begin` and `pair` until the window
    passes."""
    pins = endpoints.pins(runtime.settings)
    if not pins:
        raise endpoints.not_configured()
    async with runtime.pool.connection() as conn:
        return await pairing.pair(
            conn, runtime.keys, body.model_dump(), pins=pins,
            source=_client_address(request, runtime),
        )  # fmt: skip


@router.post(
    "/pair/offers", status_code=201, response_model=schemas.PairingOffer, tags=["pairing"],
    responses={403: {"model": schemas.ErrorResponse, "description": "biometric_required"}},
)  # fmt: skip
async def pair_offer(
    body: schemas.PairingOfferRequest,
    request: Request,
    device: DeviceDep,
    runtime: RuntimeDep,
    signed: SignedDep,
) -> dict[str, Any]:
    """A paired phone offers to pair a new device (CONTRACT-2 §4.3.1, proposed): a token as `pisa pair` makes
    it, with its QR and typed fallback. The new device pairs with `POST /v1/pair/begin` and `POST /v1/pair` as
    usual; this phone, not the server's CLI, confirms it. Signed device op `pairing_offer`, target the SHA-256
    hex of the body as sent, once Pisa has a Face ID key. There is no `signer` field: a body carrying one is
    400 `validation_failed` (`PairingOfferRequest`), as a key that signs device ops is made only on the server
    (`pisa pair --signer`). A watch offers nothing (WATCH-1: 403 `biometric_required`)."""
    target = digest((await request.body()).decode())
    await runtime.autopilot.keys.guard(
        "pairing_offer", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    pins, addresses = endpoints.pins(runtime.settings), runtime.settings.address_list
    if not pins or not addresses:
        raise endpoints.not_configured()
    async with runtime.pool.connection() as conn:
        return await pairing.offer(
            conn, runtime.keys, device.id, name=body.name, addresses=addresses, pins=pins
        )


@router.get("/pair/offers/{offerId}", response_model=schemas.PairingOffer, tags=["pairing"])
async def pair_offer_state(offerId: str, device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The offer, for the device that made it only: `open`, then the new device's pairing (at
    `awaiting_confirmation` with the transcript's fields: check `serverNonce` against `commitment`, then
    compute
    the SAS and show it beside the new device's), `confirmed` with its device id, `refused` or `expired`."""
    async with runtime.pool.connection() as conn:
        return await pairing.offer_state(conn, offerId, device.id)


@router.delete("/pair/offers/{offerId}", status_code=204, tags=["pairing"])
async def pair_offer_withdraw(offerId: str, device: DeviceDep, runtime: RuntimeDep) -> Response:
    """Withdraw the offer, or refuse the device that scanned it. Unsigned: refusing grants nothing."""
    async with runtime.pool.connection() as conn:
        await pairing.withdraw(conn, offerId, device.id)
    return Response(status_code=204)


@router.post(
    "/pair/offers/{offerId}/confirm", response_model=schemas.PairingOffer, tags=["pairing"],
    responses={403: {"model": schemas.ErrorResponse, "description": "biometric_required"}},
)  # fmt: skip
async def pair_offer_confirm(
    offerId: str,
    body: schemas.PairingConfirm,
    device: DeviceDep,
    runtime: RuntimeDep,
    signed: SignedDep,
) -> dict[str, Any]:
    """Confirm the new device after comparing the SAS. Signed device op `pairing_confirm`, target
    `<pairingId>:<sas>`, so Face ID approves exactly the device whose digits the user compared. 409 when the
    pairing is not this offer's one awaiting confirmation, or the digits are not the server's."""
    target = f"{body.pairingId}:{body.sas}"
    await runtime.autopilot.keys.guard(
        "pairing_confirm", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    async with runtime.pool.connection() as conn:
        return await pairing.confirm_offer(conn, offerId, device.id, body.pairingId, body.sas)


@router.get(
    "/pair/{pairingId}", response_model=schemas.QrPairResponse, tags=["pairing"], openapi_extra=NO_AUTH
)
async def pairing_state(pairingId: str, runtime: RuntimeDep) -> dict[str, Any]:
    """The pairing's state: `awaiting_confirmation` until the user answers on the server, then `confirmed` or
    `refused`; `expired` after 5 minutes without an answer."""
    async with runtime.pool.connection() as conn:
        return await pairing.state(conn, pairingId)


def _client_address(request: Request, runtime: Any) -> str:
    """Who the per-address limits count: the client's address, believed from X-Forwarded-For only behind a
    proxy the operator trusts (PISA_TRUSTED_PROXIES), keyed by its /64 for IPv6."""
    peer = request.client.host if request.client else ""
    return proxies_rules.limit_key(
        proxies_rules.client_address(
            peer, request.headers.get("x-forwarded-for"), runtime.settings.proxy_networks
        )
    )


@router.get(
    "/devices/session/nonce", response_model=schemas.SessionNonce, tags=["pairing"], openapi_extra=NO_AUTH
)
async def session_nonce(
    request: Request,
    runtime: RuntimeDep,
    device_id: Annotated[
        uuid.UUID | None,
        Query(
            alias="deviceId",
            description="The device the nonce is for (CONTRACT-2 §4.3 step 7; the app always sends it). "
            "The nonce then serves only that device, and the newest 5 per device stay outstanding (an older "
            "one is dropped): no limit by client address, which everyone behind one NAT or proxy shares. 400 "
            "for a device that is not paired.",
        ),
    ] = None,
) -> dict[str, str]:
    """32 random bytes for the device key to sign: single use, 60 s. With `deviceId`, bound to that device
    (the newest 5 per device); without it, at most 5 outstanding per client address and 10,000 in all (429
    past either)."""
    async with runtime.pool.connection() as conn:
        bound = None if device_id is None else str(device_id)
        return await pairing.session_nonce(conn, _client_address(request, runtime), device_id=bound)


@router.post("/devices/session", response_model=schemas.SessionToken, tags=["pairing"], openapi_extra=NO_AUTH)
async def session(body: schemas.SessionRequest, runtime: RuntimeDep) -> dict[str, str]:
    """A 1 h session token for REST and `/v2/live`, for the device key's signature over
    `pisa-session/1\\n<deviceId>\\n<nonce>`. The nonce is spent even when this fails. 401 `auth_failed` for a
    nonce or signature that does not hold, 401 `device_revoked` for a revoked device."""
    async with runtime.pool.connection() as conn:
        return await pairing.create_session(conn, body.deviceId, body.nonce, body.signature)


@router.delete("/devices/me", status_code=204, tags=["devices"])
async def unpair(device: DeviceDep, runtime: RuntimeDep) -> Response:
    """Unpair and delete (CONTRACT-2 §4.5): this device is revoked (its sessions end, its live socket closes
    with `device_revoked`), and its approval keys, push registration and Live Activity tokens are deleted. The
    last device can leave too: pairing again needs the server (`pisa pair`)."""
    async with runtime.pool.connection() as conn:
        # unpaired, then the relay forgets the handle (best effort)
        await devices.end(conn, [device.id], keys=runtime.push.keys, relay=runtime.push.relay, how="unpair")
    return Response(status_code=204)


@router.get("/server/endpoints", response_model=schemas.Endpoints, tags=["discovery"])
async def server_endpoints(device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The server's addresses and SPKI pins, current and next (CONTRACT-2 §3.4): the same value as the
    `pisa.endpoints` event. 404 when the server has none configured (PISA_ADDRESSES, PISA_TLS_CERT)."""
    doc = endpoints.current(runtime.settings)
    async with runtime.pool.connection() as conn:
        await devices.endpoints_told(conn, device.id, doc["revision"])
    return doc


@router.get("/ai/disclosure", response_model=schemas.AIDisclosure, tags=["ai"])
async def ai_disclosure(device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Which AI providers and models get which of the user's data, per feature slot (CONTRACT-2 §4.4).
    `revision` changes whenever a provider, model or data category does: the app asks for consent again."""
    async with runtime.pool.connection() as conn:
        doc = await disclosure.current(conn, runtime.settings.ollama_base_url)
        await disclosure.told(conn, device.id, doc["revision"])
    return doc


@router.post("/ai/consent", status_code=204, tags=["ai"])
async def ai_consent(body: schemas.AIConsent, device: DeviceDep, runtime: RuntimeDep) -> Response:
    """Record the user's consent on this device for REST-started work (the watch, Shortcuts): the disclosure
    `revision` they allowed and whether health may go to AI. 409 `ai_consent_required` when the revision is no
    longer current. A run's own `forwardedProps.pisa.consent` wins over it."""
    async with runtime.pool.connection() as conn:
        await disclosure.record(
            conn, device.id, body.revision, body.health, runtime.settings.ollama_base_url, runtime.clock()
        )
    return Response(status_code=204)
