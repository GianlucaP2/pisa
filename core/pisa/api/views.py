"""Pisa's own views in Settings ("Viste di Pisa"): the list and delete. A view is saved only by the
user's words in a talk (the brain's `save_view`); the app lists and deletes them."""

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel

from pisa.api import schemas
from pisa.api.autopilot_schemas import SheetOut
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError

router = APIRouter(prefix="/v1", tags=["views"], responses=ERROR_RESPONSES)


class OwnerReport(BaseModel):
    week: str
    sheet: SheetOut


@router.get("/views", response_model=schemas.Views)
async def list_views(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Pisa's own views, newest first: name, what it shows, how many reads and blocks, when it was saved and
    last shown, and where it came from (always the user's own words: `said`). At most 50."""
    return {"views": await runtime.views.listed()}


@router.get("/views/report", response_model=OwnerReport)
async def owner_report(
    _: DeviceDep, runtime: RuntimeDep, lang: LangDep, week: str | None = None
) -> dict[str, Any]:
    """The user loop: Pisa's built-in view "report", the week's talk report as a sheet
    (first words by route, clean turns, the spend per turn against the daily cap, the replay's pass^3).
    Numbers only, made by code. The latest week, or `week=YYYY-Www`; 404 `not_found` before the first
    report."""
    loop = runtime.autopilot.owner_loop
    sheet = await loop.sheet(lang, week)
    if sheet is None:
        raise PisaError("not_found", "There is no report for that week yet.", "Ask again after Sunday")
    return {"week": (await loop.latest(week) or {}).get("week"), "sheet": sheet}


@router.delete("/views/{view_id}", status_code=204, response_class=Response)
async def delete_view(view_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Delete one of Pisa's views (it can't be shown again; Pisa may save it again on the user's words)."""
    await runtime.views.delete(view_id)
    return Response(status_code=204)
