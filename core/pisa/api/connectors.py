"""Connectors. Google Sign-In runs on the iPhone; Pisa receives the one-time server
auth code and exchanges it itself. There is no redirect URI or callback page on Pisa.

every source is a connector in one registry (`GET /v1/connectors`). Their switch and their settings
(`PATCH /v1/connectors/{id}`), a credential (`PUT`/`DELETE /v1/connectors/{id}/credential`) and a bank's
consent (`POST /v1/connectors/{id}/consent/code`) are signed by the phone (`device` challenge): ops
`connector`, `connector_credential`, `connector_consent`; the target is `<id>:<SHA-256 hex of the body as
sent>` (the consent's code: of the code; a credential's removal: `<id>:delete`)."""

import re
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response

from pisa.api import connectors_schemas as cs
from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep, running
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import health_days
from pisa.services.connectors import ConnectorsService
from pisa.services.phone_keys import digest

router = APIRouter(prefix="/v1/connectors", tags=["connectors"], responses=ERROR_RESPONSES)
health = APIRouter(prefix="/v1/health", tags=["connectors"], responses=ERROR_RESPONSES)
obligations = APIRouter(prefix="/v1/obligations", tags=["connectors"], responses=ERROR_RESPONSES)


ConnectorsDep = Annotated[
    ConnectorsService,
    running("connectors", "internal", "The connectors' registry isn't running.", "Restart Pisa"),
]


async def _with_google(runtime: Any, out: dict[str, Any]) -> dict[str, Any]:
    if out["id"] == "google":
        out["google"] = await runtime.google.connector()
    return out


# The bank's return address (Enable Banking's `redirect_url`, `https://<Pisa>/v1/connectors/bank/return`): the
# bank's page sends their browser here after their consent; Pisa hands the code and the state, unchanged, to
# the app (`pisa://bank-consent`), whose signed `POST …/consent/code` exchanges them. No session, nothing
# stored, nothing logged; only a code and a state of plain token characters pass, no other parameter.
APP_BANK_RETURN = "pisa://bank-consent"
_RETURN_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")


@router.get("/bank/return", include_in_schema=False)
async def bank_return(code: str | None = None, state: str | None = None) -> Response:
    passed = {k: v for k, v in (("code", code), ("state", state)) if v and _RETURN_TOKEN.match(v)}
    if "code" not in passed:
        passed = {"error": "denied", **({"state": passed["state"]} if "state" in passed else {})}
    return Response(
        status_code=302,
        headers={
            "Location": f"{APP_BANK_RETURN}?{urlencode(passed)}",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("", response_model=cs.ConnectorsOut)
async def connectors(_: DeviceDep, runtime: RuntimeDep, service: ConnectorsDep) -> dict[str, object]:
    """Every connector, its state and its settings' schema (Settings → Connectors)."""
    return {"connectors": [await _with_google(runtime, c) for c in await service.all()]}


@router.get("/google", response_model=schemas.Connector)
async def google(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The Google connection's own detail, as the list's `google` field."""
    return await runtime.google.connector()


@router.put("/google/credentials", status_code=204)
async def google_credentials(
    body: schemas.GoogleCredentials, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> Response:
    """Signed (`device` op `google_credentials`, target the SHA-256 hex of
    `<web_client_id>\\n<web_client_secret>\\n<ios_client_id>`) once Pisa has a Face ID key."""
    target = digest(f"{body.web_client_id}\n{body.web_client_secret}\n{body.ios_client_id}")
    await runtime.autopilot.keys.guard(
        "google_credentials",
        target,
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
    )
    await runtime.google.set_credentials(body.web_client_id, body.web_client_secret, body.ios_client_id)
    return Response(status_code=204)


@router.post("/google/code", response_model=schemas.Connector)
async def google_code(
    body: schemas.ServerAuthCode, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> dict[str, Any]:
    """Exchange the server auth code from Google Sign-In; on success a first sync starts at once.
    Signed by the phone (`device` challenge, op `google_connect`, target the SHA-256 hex of the code)
    once Pisa has a Face ID key; 403 `biometric_required` otherwise."""
    target = digest(body.server_auth_code)
    await runtime.autopilot.keys.guard(
        "google_connect", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    return await runtime.google.connect(body.server_auth_code)


@router.delete("/google", status_code=204)
async def google_disconnect(_: DeviceDep, runtime: RuntimeDep, signed: SignedDep) -> Response:
    """Signed (`device` challenge, op `google_disconnect`, target `google`) once Pisa
    has a key."""
    await runtime.autopilot.keys.guard(
        "google_disconnect",
        "google",
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
    )
    await runtime.google.disconnect()
    return Response(status_code=204)


@router.get("/{connector_id}", response_model=cs.ConnectorOut)
async def connector(
    connector_id: str, _: DeviceDep, runtime: RuntimeDep, service: ConnectorsDep
) -> dict[str, Any]:
    return await _with_google(runtime, await service.get(connector_id))


@router.patch("/{connector_id}", response_model=cs.ConnectorOut)
async def change(
    connector_id: str,
    body: cs.ConnectorPatch,
    request: Request,
    _: DeviceDep,
    runtime: RuntimeDep,
    service: ConnectorsDep,
    signed: SignedDep,
) -> dict[str, Any]:
    """Their switch and their settings. Signed (`device` op `connector`, target `<id>:<SHA-256 hex of the body
    as sent>`) once Pisa has a Face ID key. Switching on a connector with `data_out` needs `data_out_seen`
    once."""
    service.spec(connector_id)  # 404 before the signature is spent
    target = f"{connector_id}:{digest((await request.body()).decode())}"
    await runtime.autopilot.keys.guard(
        "connector", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    out = await service.patch(
        connector_id, enabled=body.enabled, settings=body.settings, data_out_seen=body.data_out_seen
    )
    return await _with_google(runtime, out)


@router.put("/{connector_id}/credential", response_model=cs.ConnectorOut)
async def set_credential(
    connector_id: str,
    body: cs.CredentialIn,
    request: Request,
    _: DeviceDep,
    runtime: RuntimeDep,
    service: ConnectorsDep,
    signed: SignedDep,
) -> dict[str, Any]:
    """Their key or password, write-only (sealed; never returned or logged). Signed (`device` op
    `connector_credential`, target `<id>:<SHA-256 hex of the body as sent>`)."""
    service.spec(connector_id)
    target = f"{connector_id}:{digest((await request.body()).decode())}"
    await runtime.autopilot.keys.guard(
        "connector_credential",
        target,
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
    )
    return await service.set_credential(connector_id, body.fields)


@router.delete("/{connector_id}/credential", response_model=cs.ConnectorOut)
async def delete_credential(
    connector_id: str, _: DeviceDep, runtime: RuntimeDep, service: ConnectorsDep, signed: SignedDep
) -> dict[str, Any]:
    """Removes the credential (and a bank's consent with it). Signed (op `connector_credential`, target
    `<id>:delete`)."""
    service.spec(connector_id)
    await runtime.autopilot.keys.guard(
        "connector_credential",
        f"{connector_id}:delete",
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
    )
    return await service.delete_credential(connector_id)


@router.post("/{connector_id}/consent", response_model=cs.ConsentStart)
async def consent_start(
    connector_id: str, _: DeviceDep, runtime: RuntimeDep, service: ConnectorsDep, signed: SignedDep
) -> dict[str, Any]:
    """A bank's consent page (PSD2, read-only): the phone opens `url`; the bank sends it back with a code.
    Signed (op `connector_consent`, target `<id>:start`)."""
    service.spec(connector_id)
    await runtime.autopilot.keys.guard(
        "connector_consent",
        f"{connector_id}:start",
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
    )
    return await service.consent_start(connector_id)


@router.post("/{connector_id}/consent/code", response_model=cs.ConnectorOut)
async def consent_code(
    connector_id: str,
    body: cs.ConsentCode,
    _: DeviceDep,
    runtime: RuntimeDep,
    service: ConnectorsDep,
    signed: SignedDep,
) -> dict[str, Any]:
    """The bank's code after their consent: the session and its accounts. Signed (op `connector_consent`,
    target `<id>:<SHA-256 hex of code>`)."""
    service.spec(connector_id)
    await runtime.autopilot.keys.guard(
        "connector_consent",
        f"{connector_id}:{digest(body.code)}",
        signed.nonce,
        signed.signature,
        signed.key_id,
        device_id=signed.device_id,
    )
    return await service.consent_code(connector_id, body.code, body.state)


@health.post("/summary", response_model=cs.HealthSummaryAck)
async def health_summary(
    body: health_days.DaySummary, _: DeviceDep, service: ConnectorsDep
) -> dict[str, Any]:
    """The phone's daily summary from Apple Health (derived numbers only). Any other field (a raw
    sample, a medication's name, cycle data, a place) is refused (400). Off: 409 `connector_off`, nothing
    kept. The same day sent again replaces it."""
    return await service.health_summary_in(body)


@obligations.get("", response_model=cs.ObligationsOut)
async def list_obligations(
    _: DeviceDep,
    runtime: RuntimeDep,
    service: ConnectorsDep,
    window_days: Annotated[
        int, Query(ge=1, le=400, description="Due from today to this many days ahead")
    ] = 90,
) -> dict[str, Any]:
    """The admin radar: what is due in the window (overdue ones of the last 60 days too); flagged messages
    counted and listed apart, never tracked."""
    out = await service.obligations(window_days, runtime.owner.tag)
    return out | {"flagged_recent": await service.flagged()}
