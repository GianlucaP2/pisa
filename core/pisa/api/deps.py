"""Request dependencies: the runtime and the authenticated device."""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, overload

from fastapi import Depends, Header, Query, Request

from pisa.domain import experimental, wrist
from pisa.domain.errors import ErrorCode, PisaError
from pisa.domain.locale import Tag
from pisa.domain.owner import OwnerContext
from pisa.services import disclosure
from pisa.services.devices import Device, authenticate, bearer_token
from pisa.services.runtime import Runtime

NOTIFICATION_ORIGIN = "notification"
DateQuery = Annotated[str | None, Query(alias="date", description="YYYY-MM-DD; default today")]
NOT_YYYY_MM_DD = ("The date must be YYYY-MM-DD.", "Fix the request")
NOT_A_DAY = ("{value} is not a day.", "Fix the request and try again")


@overload
def parse_day(value: str, wording: tuple[str, str] = ...) -> date: ...
@overload
def parse_day(value: str | None, wording: tuple[str, str] = ...) -> date | None: ...
def parse_day(value: str | None, wording: tuple[str, str] = NOT_YYYY_MM_DD) -> date | None:
    """A request's day (None when not given). A string that is not a calendar day ("2026-02-30" included)
    is a 400 `validation_failed` with the route's own wording; `{value}` in it names what was sent."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        message, action = wording
        raise PisaError("validation_failed", message.format(value=value), action) from None


def from_notification(request: Request) -> bool:
    """`X-Pisa-Origin: notification`: the phone sends a notification action's answer (Approve, Undo)."""
    return wrist.parse(request.headers.get(wrist.ORIGIN_HEADER)) == NOTIFICATION_ORIGIN


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def feature(*ids: str) -> Any:
    """A router's gate: 404 `not_found` unless one of these experimental features is on (`PISA_EXPERIMENTAL`).
    An off feature's REST routes don't exist, as its tools and connectors don't (E1 X-1)."""

    async def gate(runtime: RuntimeDep) -> None:
        if not experimental.any_on(ids, runtime.enabled):
            raise PisaError(
                "not_found", "There is no such feature on this server.", "Check PISA_EXPERIMENTAL"
            )

    return Depends(gate)


def running(slot: str, code: ErrorCode, message: str, action: str) -> Any:
    """A dependency on the runtime's service in `slot`; when it isn't running in this process, the route's own
    error (its code, message and action)."""

    def service(runtime: RuntimeDep) -> Any:
        found = getattr(runtime, slot)
        if found is None:
            raise PisaError(code, message, action)
        return found

    return Depends(service)


async def owner_context(runtime: RuntimeDep) -> OwnerContext:
    """The user's context for this request: their Settings language, their zone now, their settings."""
    return await runtime.owner.context(runtime.pool)


OwnerDep = Annotated[OwnerContext, Depends(owner_context)]


async def owner_lang(context: OwnerDep) -> Tag:
    """Their Settings language now: what a reply's words are in."""
    return context.language


LangDep = Annotated[Tag, Depends(owner_lang)]


async def current_device(request: Request, runtime: RuntimeDep) -> Device:
    """The device of the bearer, a session token (CONTRACT-2 §3.1, §4.3)."""
    async with runtime.pool.connection() as conn:
        device = await authenticate(conn, bearer_token(request.headers.get("authorization")))
    # `X-Pisa-Origin: watch` when the phone relays the watch (a Reject, a check-in's answer, a
    # thumb, stop); every route reads it (not repeated per route in openapi).
    # An async dependency runs in the request's own context: what the route writes carries the origin.
    origin = wrist.parse(request.headers.get(wrist.ORIGIN_HEADER))
    if (
        origin == NOTIFICATION_ORIGIN
    ):  # contract 2: a notification's action, sent by the phone (CONTRACT-2 §9)
        origin = "phone"
    if origin not in wrist.ORIGINS:
        raise PisaError(
            "validation_failed",
            f"{wrist.ORIGIN_HEADER} must be phone, watch or notification.",
            f"Send {wrist.ORIGIN_HEADER}: watch for a call relayed from the watch, or leave it out",
        )
    wrist.origin.set(origin)
    return device


DeviceDep = Annotated[Device, Depends(current_device)]


async def require_ai_consent(runtime: Runtime, device: Device, lang: str, words: str | None = None) -> None:
    """A REST route that reaches a model or an embedding asks this first, before any AI call or queued work:
    409 `ai_consent_required` unless the device's recorded AI consent covers the disclosure as it is now (and
    health, when `words` touch it), the same check a run, the live session and a talk pass (CONTRACT-2
    §4.4, `services.disclosure.require`)."""
    async with runtime.pool.connection() as conn:
        await disclosure.require(conn, device.id, words, runtime.settings.ollama_base_url, lang)


@dataclass(frozen=True)
class Signed:
    """A signature over a `device` challenge, sent in headers (the routes keep their bodies; a DELETE has
    none), and the device sending it (whose own key, `device-<its id>`, may sign). Missing parts are None: the
    guard refuses them once Pisa has a Face ID key."""

    nonce: str | None
    signature: str | None
    key_id: str | None
    device_id: uuid.UUID | None = None


async def signed_headers(
    device: DeviceDep,
    x_pisa_nonce: Annotated[
        str | None, Header(max_length=200, description="a `device` challenge's nonce")
    ] = None,
    x_pisa_signature: Annotated[
        str | None, Header(max_length=400, description="its signature, base64")
    ] = None,
    x_pisa_key_id: Annotated[str | None, Header(max_length=128, description="the signing key")] = None,
) -> Signed:
    return Signed(x_pisa_nonce, x_pisa_signature, x_pisa_key_id, device.id)


SignedDep = Annotated[Signed, Depends(signed_headers)]
