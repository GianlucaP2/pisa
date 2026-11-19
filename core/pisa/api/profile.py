"""The user's profile: what Pisa knows, why, and the user's edits; the first talk; what the
user did with a places card."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, running
from pisa.api.portrait_schemas import Portrait, PortraitLineAnswer
from pisa.api.rest import ERROR_RESPONSES
from pisa.services import guardian, outcomes

router = APIRouter(prefix="/v1", tags=["profile"], responses=ERROR_RESPONSES)


@router.get("/profile", response_model=schemas.Profile)
async def get_profile(
    _: DeviceDep,
    runtime: RuntimeDep,
    include: Annotated[
        Literal["retired"] | None, Query(description="retired: also the claims Pisa no longer believes")
    ] = None,
) -> dict[str, Any]:
    """Every claim Pisa believes (candidate, active, asking), with its evidence; the off-limits topics; the
    pause; the first talk's state."""
    return await runtime.profile.overview(include_retired=include == "retired")


PortraitDep = Annotated[
    Any, running("portrait", "not_found", "The portrait is not running.", "Try again in a minute")
]


@router.get("/profile/portrait", response_model=Portrait)
async def get_portrait(_: DeviceDep, service: PortraitDep) -> dict[str, Any]:
    """The portrait at the top of "Su di te", written by Pisa from their active claims (never candidates,
    never sensitive ones), in their language; empty `themes` when Pisa knows too little."""
    return await service.get()


@router.post("/profile/portrait/lines", response_model=Portrait)
async def answer_portrait_line(
    body: PortraitLineAnswer, _: DeviceDep, service: PortraitDep
) -> dict[str, Any]:
    """Their answer on one line: `wrong` ("Non è vero") retires its claims by them; `correct` with their words
    keeps them as their (the line's first claim, confirmed) and retires the others. Returns the portrait."""
    return await service.correct(body.claim_ids, body.answer, body.words)


@router.get("/profile/rules", response_model=schemas.ProfileRules)
async def get_rules(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Pisa's own rules about the user in the open: the hard limits it applies and the email rules
    (never from a sender or a kind), each with where it came from (provenance, evidence) and when it was
    saved; and the interruption rules they said in words, each with its claim. Forgetting one: DELETE
    /v1/profile/claims/{id}."""
    profile = await runtime.profile.overview()
    rules = [c for c in profile["claims"] if c["hard"] or c["kind"] == "source"]
    async with runtime.pool.connection() as conn:
        said = await guardian.word_rules(conn)
    interruptions = [
        {"claim_id": claim_id, "mode": r.mode, "kind": r.kind, "person_id": r.person_id}
        for claim_id, r in said
    ]
    return {"rules": rules, "interruptions": interruptions}


@router.put("/profile/home", response_model=schemas.Home)
async def set_home(body: schemas.HomeSet, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The user's home: a place Pisa knows, or the user's words looked up once. It replaces a learned
    one; the next night more than 50 km from it starts a trip."""
    return await runtime.profile.set_home(place_id=body.place_id, query=body.query)


@router.delete("/profile/home", status_code=204)
async def forget_home(_: DeviceDep, runtime: RuntimeDep) -> Response:
    """Forget it: nothing is scoped home or travel and no trip goes on until a new home; Pisa doesn't learn
    one again from the nights before."""
    await runtime.profile.forget_home()
    return Response(status_code=204)


@router.post("/profile/home/confirm", response_model=schemas.Home)
async def confirm_home(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """ "Yes, I live there": the learned home becomes the user's to keep (never moved by Pisa)."""
    return await runtime.profile.confirm_home()


@router.get("/profile/weekly", response_model=schemas.Weekly)
async def weekly(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The weekly check: at most 3 things to confirm, offered Sunday from 18:00 (the user's time)."""
    return await runtime.learning.weekly()


@router.post("/profile/weekly/{item_id}", response_model=schemas.WeeklyAnswered)
async def weekly_answer(
    item_id: str, body: schemas.WeeklyAnswer, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """right (confirmed), wrong (retired), forget (gone, a tombstone stays); a buffer: yes or no."""
    return await runtime.learning.answer(item_id, body.answer)


@router.patch("/profile/claims/{claim_id}", response_model=schemas.ProfileClaim)
async def edit_claim(
    claim_id: str, body: schemas.ProfileClaimPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """The user's correction: the claim becomes `confirmed`; `buffer_off` alone: no more leaving earlier."""
    scope = body.scope.model_dump(exclude_none=True) if body.scope is not None else None
    return await runtime.profile.edit(
        claim_id, statement=body.statement, scope=scope, hard=body.hard, buffer_off=bool(body.buffer_off)
    )


@router.delete("/profile/claims/{claim_id}", status_code=204)
async def forget_claim(claim_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Forget: the claim and its evidence go; a tombstone without content stays."""
    await runtime.profile.forget(claim_id)
    return Response(status_code=204)


@router.post("/profile/pause", status_code=204)
async def pause(body: schemas.ProfilePause, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Pause learning until then: no new claims, no outcomes kept."""
    await runtime.profile.pause(body.until)
    return Response(status_code=204)


@router.delete("/profile/pause", status_code=204)
async def resume(_: DeviceDep, runtime: RuntimeDep) -> Response:
    await runtime.profile.pause(None)
    return Response(status_code=204)


@router.post("/profile/off_limits", response_model=list[str])
async def add_off_limits(body: schemas.OffLimits, _: DeviceDep, runtime: RuntimeDep) -> list[str]:
    """Pisa never learns about the topic; claims it covers are forgotten now. Returns every off-limits
    topic."""
    return await runtime.profile.off_limits(body.topic)


@router.post("/profile/first_talk", response_model=schemas.FirstTalkStarted)
async def start_first_talk(device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The app's "Start getting to know each other": Pisa begins the first talk on this device."""
    return await runtime.profile.start_talk(device.id)


@router.post("/places/outcomes", status_code=204, tags=["places"])
async def place_outcome(body: schemas.PlaceOutcome, device: DeviceDep, runtime: RuntimeDep) -> Response:
    """What the user did with a places card. Nothing is kept while learning is paused."""
    async with runtime.pool.connection() as conn:
        await outcomes.app_action(
            conn, card_id=body.card_id, place_id=body.place_id, action=body.action, device_id=device.id,
            now=runtime.clock(),
        )  # fmt: skip
    return Response(status_code=204)
