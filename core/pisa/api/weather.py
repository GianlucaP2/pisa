"""The weather: Open-Meteo's forecast for here, a place or a point."""

from typing import Annotated, Any

from fastapi import APIRouter, Query

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, parse_day
from pisa.api.rest import ERROR_RESPONSES

router = APIRouter(prefix="/v1", tags=["weather"], responses=ERROR_RESPONSES)


@router.get("/weather", response_model=schemas.WeatherResponse)
async def weather(
    _: DeviceDep,
    runtime: RuntimeDep,
    near: Annotated[str, Query(description="here, place:<uuid> or <lat>,<lon>")] = "here",
    date_: Annotated[str | None, Query(alias="date", description="YYYY-MM-DD, up to 7 days ahead")] = None,
) -> dict[str, Any]:
    return await runtime.weather.report(near, parse_day(date_))
