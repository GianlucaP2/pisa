"""Their intentions and what others owe them (the waiting-for list), and the resumption packet.

- `GET /v1/intentions?kind=&state=` the list (open and proposed by default);
- `GET /v1/intentions/{id}` the resumption packet;
- `POST /v1/intentions/{id}/confirm` their yes to a proposed promise; `/done` ("fatto"); `/archive`.
There is no DELETE: nothing is deleted, only archived; their deletion by category stays in Settings.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from pisa.api.deps import DeviceDep, LangDep, running
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import intentions as intention_rules
from pisa.domain.reasons import intention_why
from pisa.services.intentions import IntentionsService

router = APIRouter(prefix="/v1/intentions", tags=["intentions"], responses=ERROR_RESPONSES)
Kind = intention_rules.Kind
State = intention_rules.State


class IntentionOut(BaseModel):
    id: str
    kind: Kind
    state: State = Field(
        description="proposed: a promise from their mail or dictation, waiting for their yes"
    )
    what: str | None
    why: str | None
    next_step: str | None
    last_step: str | None
    who: str | None = Field(description="waiting-for: who owes it; a promise: to whom")
    cue: Literal["time", "place", "event", "none"]
    cue_event: Literal["meeting_end", "arrive_home", "call_end"] | None
    cue_ref: str | None
    repeat: dict[str, Any] | None
    due_on: str | None
    task_id: str | None
    place_note_id: str | None
    source: Literal["voice", "sent_mail", "dictation", "received_mail"] = Field(
        description="received_mail: another person's words, shown as data, never an action"
    )
    asked: Literal["what", "next_step"] | None
    nudged_at: str | None
    created_at: str


class IntentionsOut(BaseModel):
    intentions: list[IntentionOut]


class PacketOut(BaseModel):
    """The resumption packet: what they need to restart it in one look."""

    id: str
    kind: Kind
    state: State
    what: str | None
    why: str | None
    last_step: str | None
    next_step: str | None
    who: str | None
    cue: str = Field(description="the cue in their language: a time, a place, an event, or none")
    due_on: str | None
    thread: dict[str, str] | None = Field(description="{id, subject}: the mail it came from")
    task: dict[str, Any] | None = Field(description="{id, title, due_at}: the pool task that carries it")
    document: dict[str, Any] | None = Field(description="{id, kind, expires_on}: a vault document")
    source: Literal["voice", "sent_mail", "dictation", "received_mail"]
    created_at: str
    lines: list[str] = Field(description="the packet as short lines, in their language")


LoopsDep = Annotated[
    IntentionsService, running("loops", "internal", "The intentions service isn't running.", "Restart Pisa")
]


@router.get("", response_model=IntentionsOut)
async def intentions(
    _: DeviceDep,
    service: LoopsDep,
    lang: LangDep,
    kind: Annotated[Kind | None, Query()] = None,
    state: Annotated[list[State] | None, Query()] = None,
) -> dict[str, Any]:
    """Their intentions and the waiting-for list (open and proposed unless `state` says otherwise)."""
    states = tuple(state) if state else ("proposed", "open")
    # an intention's `why` is the model's: in their language (LangDep)
    listed = await service.listed(kind, states)
    return {"intentions": [i | {"why": intention_why(i["why"], i["kind"], lang)} for i in listed]}


@router.get("/{intention_id}", response_model=PacketOut)
async def packet(intention_id: str, _: DeviceDep, service: LoopsDep, lang: LangDep) -> dict[str, Any]:
    return await service.packet(intention_id, lang)


@router.post("/{intention_id}/confirm", response_model=PacketOut)
async def confirm(intention_id: str, _: DeviceDep, service: LoopsDep, lang: LangDep) -> dict[str, Any]:
    """Their yes to a promise Pisa proposed from their sent mail or their dictation: open, in the pool."""
    return await service.confirm(intention_id, lang)


@router.post("/{intention_id}/done", response_model=PacketOut)
async def done(intention_id: str, _: DeviceDep, service: LoopsDep, lang: LangDep) -> dict[str, Any]:
    """ "Fatto" (a repeating one goes on); a waiting-for item arrived."""
    return await service.done(intention_id, lang)


@router.post("/{intention_id}/archive", response_model=PacketOut)
async def archive(intention_id: str, _: DeviceDep, service: LoopsDep, lang: LangDep) -> dict[str, Any]:
    """Put away, never deleted."""
    return await service.archive(intention_id, lang)
