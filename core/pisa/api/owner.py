"""The addresses the user writes from. Pisa learns an alias from the From header of a sent message and asks
once: the mail from a confirmed alias is the user's own words, never a request to them
(`services/owner_identity.py`)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError
from pisa.domain.owner_model import norm
from pisa.services import owner_identity

router = APIRouter(prefix="/v1", tags=["owner"], responses=ERROR_RESPONSES)

NO_ALIAS = ("Pisa knows no such address of yours.", "List the aliases with GET /v1/owner/aliases")


def _entry(row: tuple[str, str | None, object, object]) -> dict[str, object]:
    address, name, seen, confirmed_at = row
    return {
        "address": address,
        "display_name": name,
        "first_seen_at": seen,
        "confirmed": confirmed_at is not None,
    }


@router.get("/owner/aliases", response_model=schemas.OwnerAliases)
async def list_aliases(
    _: DeviceDep,
    runtime: RuntimeDep,
    state: Annotated[
        Literal["pending", "confirmed", "all"], Query(description="which: the ones to confirm (default)")
    ] = "pending",
) -> dict[str, object]:
    """The addresses Pisa saw the user write from, other than their account. A pending one is asked once; a
    confirmed one makes its mail the user's own."""
    async with runtime.pool.connection() as conn:
        account = await owner_identity.google_account(conn)
        rows = await owner_identity.listing(conn, account, state) if account else []
    return {"account": account, "aliases": [_entry(r) for r in rows]}


@router.post(
    "/owner/aliases/{address}/confirm",
    response_model=schemas.OwnerAlias,
    responses={403: {"model": schemas.ErrorResponse, "description": "biometric_required"}},
)
async def confirm_alias(
    address: str, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> dict[str, object]:
    """The user says this address is theirs: the mail already stored from it becomes their own. It widens
    what counts as their words, so it is always signed (`device` challenge, op `alias_confirm`, target the
    address in lowercase): 403 `biometric_required` otherwise (A1 on E1 AL-1)."""
    key = norm(address)
    await runtime.autopilot.keys.guard(
        "alias_confirm", key, signed.nonce, signed.signature, signed.key_id,
        always=True, device_id=signed.device_id,
    )  # fmt: skip
    async with runtime.pool.connection() as conn, conn.transaction():
        account = await owner_identity.google_account(conn)
        if account is None or not await owner_identity.confirm(conn, account, key):
            raise PisaError("not_found", *NO_ALIAS)
        rows = [r for r in await owner_identity.listing(conn, account, "all") if r[0] == key]
    return _entry(rows[0])


@router.post("/owner/aliases/{address}/forget", status_code=204)
async def forget_alias(address: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """The user says this address is not theirs (or no longer): it is forgotten, and the mail it marked as
    theirs is not theirs any more. A POST, not a DELETE: it forgets a guess Pisa made (a sent mail seen again
    asks once more), so it needs no signature."""
    key = norm(address)
    async with runtime.pool.connection() as conn, conn.transaction():
        account = await owner_identity.google_account(conn)
        if account is None or not await owner_identity.forget(conn, account, key):
            raise PisaError("not_found", *NO_ALIAS)
    return Response(status_code=204)
