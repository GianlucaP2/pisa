"""The Memory screen. Their days as cards with place and people pills; tap for the
sources (pointers only); "dimentica" and "correggi"; the "Stanotte" strip of what the night wrote."""

from typing import Annotated, Any

from fastapi import APIRouter, Query

from pisa.api import memory_schemas as s
from pisa.api.deps import NOT_A_DAY, DeviceDep, parse_day, running
from pisa.api.rest import ERROR_RESPONSES
from pisa.api.schemas import DAY_PATTERN

router = APIRouter(prefix="/v1/memory", tags=["memory"], responses=ERROR_RESPONSES)


EpisodesDep = Annotated[
    Any, running("episodes", "not_found", "Pisa's memory is not running.", "Try again in a minute")
]


@router.get("/days", response_model=s.MemoryDays)
async def days(
    _: DeviceDep,
    service: EpisodesDep,
    since: str | None = Query(default=None, pattern=DAY_PATTERN),
    until: str | None = Query(default=None, pattern=DAY_PATTERN),
) -> dict[str, Any]:
    """Their days, newest first (the last 14 by default, at most 31): each episode a card with its place and
    people."""
    return await service.days(parse_day(since, NOT_A_DAY), parse_day(until, NOT_A_DAY))


@router.get("/stanotte", response_model=s.MemoryNight)
async def stanotte(_: DeviceDep, service: EpisodesDep) -> dict[str, Any]:
    """The "Stanotte" strip: what the last night wrote."""
    return await service.stanotte()


@router.get("/episodes/{episode_id}", response_model=s.MemoryEpisodeDetail)
async def episode(episode_id: str, _: DeviceDep, service: EpisodesDep) -> dict[str, Any]:
    """One card and its sources: pointers by kind and id (an email's sender class, their own event title)."""
    return await service.episode(episode_id)


@router.post("/episodes/{episode_id}/correct", response_model=s.MemoryEpisodeDetail)
async def correct(
    episode_id: str, body: s.MemoryCorrect, _: DeviceDep, service: EpisodesDep
) -> dict[str, Any]:
    """ "Correggi": who they were with. Their links (`by_owner`); the night never changes them."""
    return await service.correct_people(episode_id, body.people)


@router.post("/episodes/{episode_id}/forget", response_model=s.MemoryForgotten)
async def forget(episode_id: str, _: DeviceDep, service: EpisodesDep) -> dict[str, Any]:
    """ "Dimentica": the episode and its links go; Pisa keeps only that something was forgotten."""
    return await service.forget_card(episode_id)
