"""REST endpoints. Every endpoint except pairing needs `Authorization: Bearer <token>`, a session token
(contract 2, `POST /v1/devices/session`)."""

import base64
import binascii
import json
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep
from pisa.domain import app_events as app_event_rules
from pisa.domain.diagnostics import PAYLOAD_MAX_BYTES
from pisa.domain.errors import PisaError
from pisa.domain.model_caps import ServerCapability
from pisa.domain.push_contract import APNS_ENVIRONMENTS
from pisa.domain.stays import Fix, Visit, time_problem
from pisa.services import app_events, diagnostics, stays
from pisa.services.devices import end, list_devices
from pisa.services.phone_keys import digest
from pisa.services.push import apns_key_status, delete_apns_key, set_apns_key

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": schemas.ErrorResponse} for status in (400, 401, 402, 404, 409, 429, 500, 502)
}

router = APIRouter(prefix="/v1", responses=ERROR_RESPONSES)


@router.get("/ai/providers", response_model=schemas.ProvidersResponse, tags=["ai"])
async def providers(_: DeviceDep, runtime: RuntimeDep) -> dict[str, object]:
    return {"providers": await runtime.ai.providers()}


@router.put("/ai/providers/{provider_id}/key", status_code=204, tags=["ai"])
async def set_key(
    provider_id: str, body: schemas.SetKeyRequest, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> Response:
    """Signed by the phone (`device` challenge, op `provider_key`, target
    `<provider_id>:<sha256 hex of the key>`) once Pisa has a Face ID key; 403 `biometric_required`
    otherwise."""
    target = f"{provider_id}:{digest(body.key)}"
    await runtime.autopilot.keys.guard(
        "provider_key", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    await runtime.ai.set_key(provider_id, body.key)
    return Response(status_code=204)


@router.delete("/ai/providers/{provider_id}/key", status_code=204, tags=["ai"])
async def delete_key(
    provider_id: str,
    _: DeviceDep,
    runtime: RuntimeDep,
    replace_with: Annotated[
        str | None,
        Query(
            max_length=200,
            description="another provider with a key (or a model server): the slots on this provider move to "
            "its model for each, in the same transaction as the deletion",
        ),
    ] = None,
) -> Response:
    """While a feature slot's model or fallback is on this provider: 409 `provider_in_use` with `slots`, and
    nothing changes, unless `replace_with` names the provider that takes them over (each slot gets its model
    that can serve the slot; a fallback on the deleted provider is dropped). A slot no model of the
    replacement can serve: 400 `model_incompatible` with `slots`, nothing changed."""
    await runtime.ai.delete_key(provider_id, replace_with)
    return Response(status_code=204)


@router.post("/ai/providers/{provider_id}/test", response_model=schemas.ProviderTestResponse, tags=["ai"])
async def test_key(provider_id: str, _: DeviceDep, runtime: RuntimeDep) -> schemas.ProviderTestResponse:
    result = await runtime.ai.test(provider_id)
    return schemas.ProviderTestResponse(
        ok=result.ok, message=result.message, models_found=result.models_found
    )


@router.post("/ai/providers", status_code=201, response_model=schemas.Provider, tags=["ai"])
async def create_server(
    body: schemas.ServerCreate, request: Request, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> dict[str, object]:
    """Add an OpenAI-compatible server. Signed by the phone (`device` op `model_server`, target
    `<id>:<SHA-256 hex of the body as sent>`) once Pisa has a Face ID key: its address is where the
    user's words go."""
    target = f"{body.id}:{digest((await request.body()).decode())}"
    await runtime.autopilot.keys.guard(
        "model_server", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    return await runtime.ai.create_server(
        body.id, body.name, body.base_url, body.ca_bundle,
        read_timeout_s=body.read_timeout_s, thinking=body.thinking,
    )  # fmt: skip


@router.patch("/ai/providers/{provider_id}", response_model=schemas.Provider, tags=["ai"])
async def update_server(
    provider_id: str,
    body: schemas.ServerUpdate,
    request: Request,
    _: DeviceDep,
    runtime: RuntimeDep,
    signed: SignedDep,
) -> dict[str, object]:
    """Change a server the user added. Signed as its creation (`device` op `model_server`, target
    `<id>:<SHA-256 hex of the body as sent>`). A new address forgets the probe's results."""
    target = f"{provider_id}:{digest((await request.body()).decode())}"
    await runtime.autopilot.keys.guard(
        "model_server", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    return await runtime.ai.update_server(provider_id, body.model_dump(exclude_unset=True))


@router.delete("/ai/providers/{provider_id}", status_code=204, tags=["ai"])
async def delete_server(provider_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Remove a server the user added, with its key, its probes and its models."""
    await runtime.ai.delete_server(provider_id)
    return Response(status_code=204)


ModelQuery = Annotated[
    str | None,
    Query(max_length=500, description="The model; optional when the server serves only one"),
]


@router.get(
    "/ai/providers/{provider_id}/capabilities", response_model=schemas.ProviderCapabilities, tags=["ai"]
)
async def capabilities(
    provider_id: str, _: DeviceDep, runtime: RuntimeDep, model: ModelQuery = None
) -> dict[str, object]:
    """What a model on an OpenAI-compatible server can do: the probe's results with the user's overrides
    applied. A model never probed reports every capability false, with `probed_at` null."""
    return await runtime.ai.capabilities(provider_id, model)


@router.post("/ai/providers/{provider_id}/probe", response_model=schemas.ProviderCapabilities, tags=["ai"])
async def probe(
    provider_id: str, _: DeviceDep, runtime: RuntimeDep, model: ModelQuery = None
) -> dict[str, object]:
    """Probe a model: one tiny real request per capability, each with a strict pass test (a minute or more
    on a CPU server). Answers as GET .../capabilities."""
    return await runtime.ai.probe(provider_id, model)


@router.put(
    "/ai/providers/{provider_id}/capabilities/overrides",
    response_model=schemas.ProviderCapabilities,
    tags=["ai"],
)
async def set_overrides(
    provider_id: str,
    body: dict[ServerCapability, bool],
    _: DeviceDep,
    runtime: RuntimeDep,
    model: ModelQuery = None,
) -> dict[str, object]:
    """Replace the model's overrides: each capability sent is set by hand over the probe's result; one left
    out follows the probe again. Answers as GET .../capabilities."""
    return await runtime.ai.set_overrides(provider_id, model, {str(k): v for k, v in body.items()})


@router.get("/ai/slots", response_model=schemas.SlotsResponse, tags=["ai"])
async def slots(_: DeviceDep, runtime: RuntimeDep) -> dict[str, object]:
    return {"slots": await runtime.ai.slots()}


@router.put("/ai/slots/{slot_id}", response_model=schemas.SlotUpdated, tags=["ai"])
async def update_slot(
    slot_id: str, body: schemas.SlotUpdate, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, object]:
    return await runtime.ai.update_slot(slot_id, body.model, body.fallback)


@router.get("/ai/models", response_model=schemas.ModelsResponse, tags=["ai"])
async def models(_: DeviceDep, runtime: RuntimeDep, slot: str | None = None) -> dict[str, object]:
    return await runtime.ai.models(slot)


@router.get("/ai/spend", response_model=schemas.SpendResponse, tags=["ai"])
async def spend(_: DeviceDep, runtime: RuntimeDep) -> dict[str, object]:
    return await runtime.ai.spend()


@router.put("/ai/caps", response_model=schemas.CapsResponse, tags=["ai"])
async def set_caps(body: schemas.Caps, _: DeviceDep, runtime: RuntimeDep) -> dict[str, float]:
    return await runtime.ai.set_caps(body.daily_eur, body.monthly_eur)


@router.put("/devices/me/push", status_code=204, tags=["devices"])
async def register_push(body: schemas.PushMode, device: DeviceDep, runtime: RuntimeDep) -> Response:
    """How notices reach this device; a new mode replaces the old registration. Contract 2 (CONTRACT-2 §9):
    `relay` stores the relay's `handle` (at most 64 characters), `secret` (32 bytes) and the device's X25519
    `public_key` (32 bytes raw), both base64url without padding; `direct` the APNs `token` (`environment`
    production or sandbox); `none` deletes both. 204
    whenever the request is valid: a token is kept while the server has no APNs key (push reads off in
    readyz and discovery until it has)."""
    if isinstance(body, schemas.PushRelay):
        secret, public_key = _b64url(body.secret, "secret"), _b64url(body.public_key, "public_key")
        await runtime.push.register_relay(device.id, body.handle, secret, public_key)
    elif isinstance(body, schemas.PushDirect):
        # the schema's token limit is looser than the push contract's: the service enforces the contract
        await runtime.push.register(device.id, body.token, APNS_ENVIRONMENTS[body.environment])
    else:
        await runtime.push.unregister(device.id)
    return Response(status_code=204)


def _b64url(value: str, what: str) -> bytes:
    """32 bytes, base64url without padding (the relay's values)."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise PisaError(
            "validation_failed", f"{what} must be 32 bytes, base64url without padding.", "Fix the request"
        )
    return raw


@router.get("/devices/me/push", response_model=schemas.PushState, tags=["devices"])
async def push_state(device: DeviceDep, runtime: RuntimeDep) -> dict[str, object]:
    """This device's push mode and since when; never its secret, token or key."""
    state = await runtime.push.mode(device.id)
    since = state["since"]
    return {"mode": state["mode"], "since": since.isoformat() if since is not None else None}


@router.delete("/devices/me/push", status_code=204, tags=["devices"])
async def unregister_push(device: DeviceDep, runtime: RuntimeDep) -> Response:
    await runtime.push.unregister(device.id)
    return Response(status_code=204)


@router.post("/devices/me/locations", status_code=204, tags=["devices"])
async def upload_locations(body: schemas.LocationsUpload, device: DeviceDep, runtime: RuntimeDep) -> Response:
    """Fixes and iPhone visits sent while the WebSocket was down. Repeats are ignored; a visit's
    departure may come in a later upload of the same visit. A time more than 30 days old or more than 5
    minutes ahead refuses the whole upload, naming the item."""
    now = runtime.clock()
    for i, x in enumerate(body.locations):
        times = (
            {"measured_at": x.measured_at}
            if isinstance(x, schemas.LocationFix)
            else {"arrived_at": x.arrived_at, "departed_at": x.departed_at}
        )
        for field, at in times.items():
            problem = at and time_problem(at, now)
            if problem:
                raise PisaError(
                    "validation_failed",
                    f"The time in locations[{i}].{field} {problem}.",
                    "Send only locations from the last 30 days, with the device's clock right",
                )
    fixes = [
        Fix(x.lat, x.lon, x.accuracy_m, x.measured_at)
        for x in body.locations
        if isinstance(x, schemas.LocationFix)
    ]
    visits = [
        Visit(x.lat, x.lon, x.accuracy_m, x.arrived_at, x.departed_at)
        for x in body.locations
        if isinstance(x, schemas.LocationVisit)
    ]
    async with runtime.pool.connection() as conn:
        await stays.save(conn, device.id, fixes, visits)
    return Response(status_code=204)


DIAGNOSTICS_BODY_MAX = PAYLOAD_MAX_BYTES + 4096  # the payload's 1 MB plus the envelope (kind, versions)
_DIAGNOSTICS_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    schemas.DiagnosticsUpload.model_json_schema(),
                    schemas.AppEventsUpload.model_json_schema(),
                    schemas.MetricsUpload.model_json_schema(),
                ]
            }
        }
    },
}


@router.post(
    "/devices/me/diagnostics",
    status_code=204,
    tags=["devices"],
    openapi_extra={"requestBody": _DIAGNOSTICS_BODY},
)
async def upload_diagnostics(request: Request, device: DeviceDep, runtime: RuntimeDep) -> Response:
    """A MetricKit diagnostic payload, as iOS delivers it (on the next launch): the payload's
    shape or one bare diagnostic. Only call stacks and exception types are kept, for 30 days; anything else
    in the payload is dropped. A body over the limit is refused before it is parsed. Or `{"appEvents": […]}`:
    the companion's refusals, retries, recoveries and stops, allowlisted fields only. Or `kind:
    "metrics"`: one day of the phone's battery numbers, numbers only, one a day per device."""
    raw = bytearray()
    declared = request.headers.get("content-length", "")
    if not (declared.isdigit() and int(declared) > DIAGNOSTICS_BODY_MAX):
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > DIAGNOSTICS_BODY_MAX:
                break
    if len(raw) > DIAGNOSTICS_BODY_MAX or (declared.isdigit() and int(declared) > DIAGNOSTICS_BODY_MAX):
        raise PisaError(
            "validation_failed",
            f"The diagnostic upload is over {PAYLOAD_MAX_BYTES // 1_000_000} MB; at most 1 MB is accepted.",
            "Send the payload's call stacks only",
        )
    try:
        parsed = json.loads(bytes(raw))
    except ValueError:
        parsed = None
    if (
        isinstance(parsed, dict)
        and parsed.get("kind") == "events"
        and isinstance(parsed.get("payload"), dict)
        and set(parsed["payload"]) == {"appEvents"}
    ):
        parsed = parsed["payload"]  # TestFlight 0.1.0 (11) wraps them like a diagnostic
    if isinstance(parsed, dict) and set(parsed) == {"appEvents"}:
        # App events: sanitised like the diagnostics; an event outside the allowlist is dropped.
        items = parsed["appEvents"]
        if not isinstance(items, list) or len(items) > app_event_rules.BATCH_MAX:
            raise PisaError(
                "validation_failed",
                f"The app events must be a list of at most {app_event_rules.BATCH_MAX}.",
                "Send the events in smaller batches",
            )
        async with runtime.pool.connection() as conn:
            await app_events.store(
                conn, device.id, items, now=runtime.clock(), timezone=runtime.settings.timezone
            )
        return Response(status_code=204)
    if isinstance(parsed, dict) and parsed.get("kind") == "metrics":
        try:
            metrics = schemas.MetricsUpload.model_validate_json(bytes(raw))
        except ValidationError as err:
            raise RequestValidationError(err.errors(include_url=False)) from None
        numbers = metrics.payload.model_dump(mode="json", exclude_none=True)
        async with runtime.pool.connection() as conn:
            await diagnostics.store_metrics(
                conn, device.id, metrics.payload.day, numbers, app_version=metrics.app_version,
                os_version=metrics.os_version, now=runtime.clock(),
            )  # fmt: skip
        return Response(status_code=204)
    try:
        body = schemas.DiagnosticsUpload.model_validate_json(bytes(raw))
    except ValidationError as err:
        raise RequestValidationError(err.errors(include_url=False)) from None
    async with runtime.pool.connection() as conn:
        await diagnostics.store(
            conn, device.id, kind=body.kind, app_version=body.app_version, os_version=body.os_version,
            payload=body.payload, now=runtime.clock(),
        )  # fmt: skip
    return Response(status_code=204)


@router.put("/devices/me/live-activity", status_code=204, tags=["devices"])
async def set_live_activity(
    body: schemas.LiveActivityToken, device: DeviceDep, runtime: RuntimeDep
) -> Response:
    """Contract T: the running activity's push token (`update`) or the push-to-start token (`start`)."""
    await runtime.live_activity.set_token(device.id, body.token, body.kind)
    return Response(status_code=204)


@router.delete("/devices/me/live-activity", status_code=204, tags=["devices"])
async def delete_live_activity(
    kind: Annotated[Literal["update", "start"], Query()],
    device: DeviceDep,
    runtime: RuntimeDep,
    reason: Annotated[
        Literal["dismissed", "ended"],
        Query(description="update: dismissed by the user (off for the rest of the day) or ended by iOS"),
    ] = "dismissed",
) -> Response:
    await runtime.live_activity.delete_token(device.id, kind, reason)
    return Response(status_code=204)


# --- keys and devices from the app ------------------------------------------------------------------


@router.get("/push/key", response_model=schemas.PushKeyStatus, tags=["devices"])
async def push_key(_: DeviceDep, runtime: RuntimeDep) -> dict[str, object]:
    async with runtime.pool.connection() as conn:
        return await apns_key_status(conn)


@router.put("/push/key", status_code=204, tags=["devices"])
async def set_push_key(body: schemas.P8Key, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """The APNs .p8 text and its key id, pasted in the app (stored Fernet-encrypted; never returned)."""
    async with runtime.pool.connection() as conn:
        await set_apns_key(conn, runtime.keys, body.key_id, body.key)
    return Response(status_code=204)


@router.delete("/push/key", status_code=204, tags=["devices"])
async def delete_push_key(_: DeviceDep, runtime: RuntimeDep) -> Response:
    async with runtime.pool.connection() as conn:
        await delete_apns_key(conn)
    return Response(status_code=204)


@router.get("/devices", response_model=schemas.DevicesResponse, tags=["devices"])
async def devices(device: DeviceDep, runtime: RuntimeDep) -> dict[str, object]:
    async with runtime.pool.connection() as conn:
        rows = await list_devices(conn)
    return {
        "devices": [
            {
                "id": str(d.id),
                "name": d.name,
                "platform": d.platform,
                "created_at": d.created_at.isoformat(),
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
                "this_device": d.id == device.id,
            }
            for d in rows
        ]
    }


@router.delete("/devices/{device_id}", status_code=204, tags=["devices"])
async def revoke(device_id: uuid.UUID, device: DeviceDep, runtime: RuntimeDep, signed: SignedDep) -> Response:
    """This device: no signature. Another one: signed by the phone (`device` challenge, op
    `revoke_device`, target its id) once Pisa has a Face ID key."""
    if device_id != device.id:
        target = str(device_id)
        await runtime.autopilot.keys.guard(
            "revoke_device", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
        )
    async with runtime.pool.connection() as conn:
        # revoked, then the relay forgets its handle (best effort)
        await end(conn, [device_id], keys=runtime.push.keys, relay=runtime.push.relay, how="from_app")
    return Response(status_code=204)
