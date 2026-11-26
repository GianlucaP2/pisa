"""Collegamenti (Connections): every outside service Pisa can reach, in six categories, and the user's OK of a
paid maps or web provider (`domain.connections`).

`GET /v1/connections` is read-only: each entry's `actions` are the existing endpoints of its own kind (an AI
key: `PUT`/`DELETE /v1/ai/providers/{provider}/key`; a place key: `PUT`/`DELETE
/v1/places/providers/{provider}/key`; the APNs key: `PUT`/`DELETE /v1/push/key`; Google:
`/v1/connectors/google/start` and `DELETE /v1/connectors/google`; `test`: the kind's `POST .../test`).
`ack_paid` is `PUT /v1/connections/paid/{provider}` here, signed."""

from typing import Any

from fastapi import APIRouter, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep, feature
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import connections as rules
from pisa.services import connections

router = APIRouter(prefix="/v1/connections", tags=["connections"], responses=ERROR_RESPONSES)


@router.get("", response_model=schemas.ConnectionsResponse)
async def overview(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Every outside service in six categories (AI models, Voice, Maps & places, Web research, Accounts,
    Notifications): its state, whether it costs money and the user's OK of it, what leaves the server and to
    whom, its docs page and the actions a client may offer. A feature off on this server is absent."""
    return await connections.overview(runtime)


@router.put(
    "/paid/{provider}",
    response_model=schemas.PaidAck,
    responses={403: {"model": schemas.ErrorResponse, "description": "biometric_required"}},
    dependencies=[feature("places")],
)
async def acknowledge(
    provider: str, body: schemas.PaidAckRequest, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> dict[str, Any]:
    """The user's OK of a paid maps or web provider (`google`, `tavily`) and its own monthly cap in cents (at
    most the server's hard monthly limit). Until it, the provider is never called. Always signed, a fresh
    install too (`device` op `paid_ack`, target `<provider>:<cap_cents>`, e.g. `google:500`): Face ID, or
    this client's own device key. A new OK replaces the old one; deleting the provider's key keeps it
    (`DELETE` here withdraws it)."""
    rules.check_ack(
        provider, body.cap_cents, runtime.place_providers.hard_monthly_cents()
    )  # before the nonce
    await runtime.autopilot.keys.guard(
        "paid_ack", rules.ack_target(provider, body.cap_cents), signed.nonce, signed.signature, signed.key_id,
        always=True, device_id=signed.device_id,
    )  # fmt: skip
    return await runtime.place_providers.acknowledge(provider, body.cap_cents, signed.device_id)


@router.delete("/paid/{provider}", status_code=204, dependencies=[feature("places")])
async def withdraw(provider: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """The OK withdrawn (no signature: it only stops spending): the provider is not called until a new one."""
    await runtime.place_providers.withdraw(provider)
    return Response(status_code=204)
