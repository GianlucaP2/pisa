"""Places: provider keys, status and usage; search, the place card, research and travel
times."""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep, SignedDep, require_ai_consent
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError
from pisa.services.phone_keys import digest
from pisa.services.places import NEARBY_MAX, From, PlacesService, parse_coord

router = APIRouter(prefix="/v1/places", tags=["places"], responses=ERROR_RESPONSES)
Mode = Literal["drive", "transit", "walk", "bike"]


def _origin(value: str, what: str) -> From:
    return "here" if value == "here" else parse_coord(value, what)


def _moment(value: str | None, what: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise PisaError(
            "validation_failed", f"{what} must be an RFC 3339 date-time with its offset.", "Fix the request"
        )
    return moment


# --- provider keys, status and usage ------------------------------------------------------------------------


@router.get("/providers", response_model=schemas.PlaceProvidersResponse)
async def providers(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return {"providers": await runtime.place_providers.statuses()}


@router.put("/providers/{provider_id}/key", status_code=204)
async def set_key(
    provider_id: str, body: schemas.PlaceKey, _: DeviceDep, runtime: RuntimeDep, signed: SignedDep
) -> Response:
    """Signed by the phone (`device` op `place_key`, target `<provider_id>:<SHA-256
    hex of the key>`) once Pisa has a Face ID key."""
    target = f"{provider_id}:{digest(body.key)}"
    await runtime.autopilot.keys.guard(
        "place_key", target, signed.nonce, signed.signature, signed.key_id, device_id=signed.device_id
    )
    await runtime.place_providers.set_key(provider_id, body.key, body.key_id)
    return Response(status_code=204)


@router.delete("/providers/{provider_id}/key", status_code=204)
async def delete_key(provider_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    await runtime.place_providers.delete_key(provider_id)
    return Response(status_code=204)


@router.post("/providers/{provider_id}/test", response_model=schemas.PlaceTestResult)
async def test_key(provider_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.place_providers.test(provider_id)


@router.get("/usage", response_model=schemas.PlaceUsage)
async def usage(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.place_providers.usage()


# --- search, the place card, research and travel times ------------------------------------------------------


@router.get("/search", response_model=schemas.PlaceSearchResponse)
async def search(
    _: DeviceDep,
    runtime: RuntimeDep,
    q: Annotated[
        str | None, Query(description="What to look for; omit it for the places nearest `near`")
    ] = None,
    near: Annotated[str | None, Query(description="here, or <lat>,<lon>")] = None,
    open_now: bool = False,
    limit: Annotated[
        int | None, Query(ge=1, le=20, description="5 by default; 10 at most with q, 20 without")
    ] = None,
) -> dict[str, Any]:
    """With `q`, a provider search (2 to 200 characters). Without it, the places Pisa knows nearest to
    `near`, by distance, at most 20: no provider call."""
    origin = _origin(near, "near") if near is not None else None
    if q is None:
        if origin is None:
            raise PisaError("validation_failed", "Give q, or near for the places nearby.", "Fix the request")
        return {"results": await runtime.places.nearby(origin, limit or NEARBY_MAX, open_now=open_now)}
    return {"results": await runtime.places.search(q, near=origin, open_now=open_now, limit=limit or 5)}


@router.get("/{place_id}", response_model=schemas.Place, response_model_by_alias=True)
async def place(place_id: str, _: DeviceDep, runtime: RuntimeDep, reviews: bool = True) -> dict[str, Any]:
    """`reviews=false` for lists (Today): Google's reviews are its most expensive tier."""
    return await runtime.places.card(PlacesService.parse_id(place_id), reviews=reviews)


@router.get("/{place_id}/details", response_model=schemas.PlaceDetails)
async def details(place_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The detail sheet: hours, phone, website, what the reviews say (Google's summary, else Pisa's
    with its sources), up to 5 review excerpts, the menu as Pisa read it, Apple's place id, the photos'
    attributions."""
    return await runtime.places.detail_sheet(PlacesService.parse_id(place_id))


@router.get("/{place_id}/photos/{index}", response_model=schemas.PlacePhoto)
async def photo(
    place_id: str,
    index: int,
    _: DeviceDep,
    runtime: RuntimeDep,
    w: Annotated[int, Query(ge=100, le=1600, description="the width in pixels")] = 800,
) -> dict[str, Any]:
    """A short-lived URL of the photo (Google's): the key stays on the server, no copy is kept.
    Show the attribution with it. Counts in the places cap (Place Photo)."""
    return await runtime.places.photo(PlacesService.parse_id(place_id), index, w)


@router.post("/{place_id}/research", response_model=schemas.Place, response_model_by_alias=True)
async def research(
    place_id: str,
    device: DeviceDep,
    runtime: RuntimeDep,
    lang: LangDep,
    body: Annotated[schemas.ResearchRequest | None, Body()] = None,
) -> dict[str, Any]:
    question = body.question if body else None
    # a model answers: 409 `ai_consent_required` until the user's AI consent covers it (health too, when the
    # question touches it), before any call (FL-1)
    await require_ai_consent(runtime, device, lang, question)
    return await runtime.places.research(
        PlacesService.parse_id(place_id), question.strip() if question else None
    )


@router.get("/{place_id}/travel", response_model=schemas.TravelResponse)
async def travel(
    place_id: str,
    _: DeviceDep,
    runtime: RuntimeDep,
    mode: Mode | None = None,
    from_: Annotated[str, Query(alias="from", description="here, or <lat>,<lon>")] = "here",
    depart_at: str | None = None,
    arrive_by: str | None = None,
) -> dict[str, Any]:
    return await runtime.places.travel(
        PlacesService.parse_id(place_id),
        mode=mode,
        origin=_origin(from_, "from"),
        depart_at=_moment(depart_at, "depart_at"),
        arrive_by=_moment(arrive_by, "arrive_by"),
    )
