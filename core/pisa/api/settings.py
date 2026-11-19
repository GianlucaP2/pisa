"""Settings: GET the whole object, PATCH any subset (objects merge, arrays and null replace)."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["settings"], responses=ERROR_RESPONSES)


@router.get("/settings", response_model=schemas.Settings)
async def get_settings(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.settings_service.get()


@router.patch("/settings", response_model=schemas.Settings)
async def patch_settings(
    patch: Annotated[dict[str, Any], Body(description="Any subset of the settings object")],
    device: DeviceDep,
    runtime: RuntimeDep,
    signed: SignedDep,
) -> dict[str, Any]:
    """A shorter `messages_retention` (irreversible) is signed by the phone (`device` challenge, op
    `messages_retention`, target the new value: `1y` or `90d`) once Pisa has a Face ID key, like a device's
    deletion. A longer one, or `forever`, needs no signature."""

    async def guard(value: str) -> None:
        await runtime.autopilot.keys.guard(
            "messages_retention",
            value,
            signed.nonce,
            signed.signature,
            signed.key_id,
            device_id=signed.device_id,
        )

    return await runtime.settings_service.patch(patch, device_id=device.id, signed=guard)


@router.get("/settings/messages_retention/preview", response_model=schemas.MessagesRetentionPreview)
async def preview_messages_retention(
    value: Annotated[Literal["forever", "1y", "90d"], Query(description="The value they are about to pick")],
    _: DeviceDep,
    runtime: RuntimeDep,
) -> dict[str, Any]:
    """Before they pick a shorter value, how many messages it would remove and from when."""
    return await runtime.settings_service.messages_preview(value)
