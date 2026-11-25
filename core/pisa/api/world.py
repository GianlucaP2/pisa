# ruff: noqa: E501
"""What Pisa publishes about itself (public, no owner data, no auth) and the user's reads.

- `GET /.well-known/http-message-signatures-directory`: Pisa's public keys (JWKS), the response signed by each
  (Web Bot Auth); `GET /.well-known/agent-card.json`: who Pisa is; `GET /.well-known/pisa-ucp-profile.json`: Pisa as a
  UCP platform. These three are the only routes meant to be reachable from the internet (PISA_AGENT_URL).
- `GET /v1/world/identity`, `GET /v1/world/ladder`: the user's (device token).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pisa.api import world_schemas as schemas
from pisa.api.deps import DeviceDep, running
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import identity as rules

public = APIRouter(tags=["world"])
# without PISA_AGENT_URL (or before the identity is set up) Pisa publishes nothing: 404 not_found, in JSON
NOT_PUBLISHED: dict[int | str, dict[str, Any]] = {
    404: {
        "description": "Not published",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }
}
router = APIRouter(prefix="/v1/world", tags=["world"], responses=ERROR_RESPONSES)


class DirectoryResponse(JSONResponse):
    """The key directory's own media type (draft-meunier-http-message-signatures-directory)."""

    media_type = rules.DIRECTORY_TYPE


IdentityDep = Annotated[
    Any, running("identity", "not_found", "Pisa's identity isn't set up in this process.", "Try again later")
]
WorldDep = Annotated[
    Any, running("world", "not_found", "The ladder isn't set up in this process.", "Try again later")
]


@public.get(
    rules.DIRECTORY_PATH, include_in_schema=True, response_class=DirectoryResponse, responses=NOT_PUBLISHED
)
async def key_directory(request: Request, service: IdentityDep) -> JSONResponse:
    """Pisa's signing keys (JWKS, RFC 8037 Ed25519), for sites that verify its Web Bot Auth signatures."""
    identity = service
    authority = (identity.agent_url or "").removeprefix("https://") or (request.url.netloc or "")
    body, headers = await identity.directory(authority)
    return DirectoryResponse(body, headers=headers)


@public.get(rules.CARD_PATH, responses=NOT_PUBLISHED)
async def agent_card(service: IdentityDep) -> JSONResponse:
    """Pisa's agent card: "Pisa, a personal assistant acting for its owner"; how to verify it; no owner data."""
    card = await service.card()
    return JSONResponse(card, headers={"Cache-Control": f"public, max-age={rules.DIRECTORY_MAX_AGE_S}"})


@public.get(rules.UCP_PROFILE_PATH, responses=NOT_PUBLISHED)
async def ucp_profile(service: IdentityDep) -> JSONResponse:
    """Pisa as a UCP platform: the spec's version, what it asks for (a catalog, a cart it hands over), its keys."""
    profile = await service.ucp_profile()
    return JSONResponse(profile, headers={"Cache-Control": "public, max-age=300"})


@router.get("/identity", response_model=schemas.Identity)
async def identity(_: DeviceDep, service: IdentityDep) -> dict[str, Any]:
    """Pisa's own key (its public id only), whether Pisa signs its reads, and where its card is."""
    return await service.status()


@router.get("/ladder", response_model=schemas.Ladder)
async def ladder(_: DeviceDep, service: WorldDep) -> dict[str, Any]:
    """How Pisa reaches each kind of business today: per category and country, the routes best first."""
    return await service.ladder()
