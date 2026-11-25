"""A device's conversations: a new one at the user's word (`POST /v1/conversations/new`), and all of them
deleted (`DELETE /v1/conversations/{threadId}`).

The server keeps one thread id per device, `conversation:<deviceId>` (CONTRACT-2 §3.1), holding its
conversations over time: a new one after 30 minutes of silence, or at `POST /v1/conversations/new`. The thread
ids a client gives its own runs all land in it. Only the device that owns it deletes it (403
`other_device` for another's; 404 `not_found` for a thread no device owns), always signed: a `device`
challenge, op `conversation`, target the thread id, by a Face ID key or the device's own key
(`device-<its id>`), even on a fresh install. What goes: its messages and what came from them, its REST turns
and its talks (`services.retention.forget_device_conversation`); its open talk ends first; and, as at
`POST /new`, the short-lived state the server holds in memory for it (`start_fresh`, security review CD-1).
Tasks, reminders and Memoria stay. Deleting it again answers 204 too."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response

from pisa.api import schemas
from pisa.api.deps import DeviceDep, RuntimeDep, SignedDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain.errors import PisaError
from pisa.services import retention
from pisa.services.runtime import Runtime
from pisa.services.session_registry import SessionRegistry

router = APIRouter(prefix="/v1/conversations", tags=["conversations"], responses=ERROR_RESPONSES)

THREAD_PREFIX = "conversation:"
OP = "conversation"


def _unknown() -> PisaError:
    return PisaError(
        "not_found",
        "There is no such conversation thread: the server keeps one per device, conversation:<deviceId>.",
        "Use this device's thread, conversation:<its device id>",
    )


def device_of(thread_id: str) -> uuid.UUID | None:
    """The device a server conversation thread names (`conversation:<deviceId>`), or None."""
    if not thread_id.startswith(THREAD_PREFIX):
        return None
    try:
        return uuid.UUID(thread_id.removeprefix(THREAD_PREFIX))
    except ValueError:
        return None


def start_fresh(runtime: Runtime, device_id: uuid.UUID) -> None:
    """What the server holds in memory for the device's conversation, forgotten: the conversation service's
    short-lived state (its last turn, what Pisa last said, objects and taps, the reading, the reflex's lines
    and facts) and its live socket's (the words said before, the last final). `POST /new` and the DELETE both
    run it, so a new conversation and a deleted one can't drift apart (security review CD-1)."""
    runtime.conversations.forget_short_lived(device_id)
    live = runtime.live
    session = live.session(device_id) if isinstance(live, SessionRegistry) else None
    if session is not None:
        session.new_conversation()


@router.post("/new", response_model=schemas.NewConversation)
async def new(device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Start a new conversation for this device now (the app's "Nuova conversazione"): the next turn reaches
    the model with none of the earlier turns, as after 30 minutes of silence. Nothing is deleted, and nothing
    needs a signature; long-term memory (Memoria, episodes, tasks, reminders) is unchanged. A second call
    before any turn answers the same conversation."""
    conversation_id = await runtime.conversations.new_conversation(device.id)
    start_fresh(runtime, device.id)
    return {"conversation_id": conversation_id, "thread_id": f"{THREAD_PREFIX}{device.id}"}


@router.delete(
    "/{thread_id}",
    status_code=204,
    responses={403: {"model": schemas.ErrorResponse}},
)
async def delete(
    thread_id: Annotated[
        str, Path(max_length=120, description="the device's thread, conversation:<deviceId>")
    ],
    device: DeviceDep,
    runtime: RuntimeDep,
    signed: SignedDep,
) -> Response:
    """Delete this device's conversations (`conversation:<its device id>`, every one): their messages and
    what was derived from them, its REST turns and its talks (an open one ends first), and what the server
    held in memory for them, as at `POST /new`. Tasks, reminders and Memoria stay. Always signed
    (`device` challenge, op `conversation`, target the thread id in lowercase): 403 `biometric_required`
    otherwise. Another device's thread: 403 `other_device`; a thread no device owns: 404 `not_found`. Again:
    204."""
    owner = device_of(thread_id)
    if owner is None:
        raise _unknown()
    async with runtime.pool.connection() as conn:
        found = await (await conn.execute("SELECT 1 FROM devices WHERE id = %s", (owner,))).fetchone()
    if found is None:
        raise _unknown()
    if owner != device.id:
        raise PisaError(
            "other_device",
            "This conversation belongs to another device: only that device can delete it.",
            "Delete it from the device that had the conversation",
        )
    await runtime.autopilot.keys.guard(
        OP, f"{THREAD_PREFIX}{owner}", signed.nonce, signed.signature, signed.key_id,
        always=True, device_id=signed.device_id,
    )  # fmt: skip
    await runtime.talk.conversation_deleted(owner)  # closed and stored before its rows go
    await runtime.conversations.settle()  # a cancelled reply still being saved lands before the delete
    async with runtime.pool.connection() as conn:
        await retention.forget_device_conversation(conn, owner)
    start_fresh(runtime, owner)
    return Response(status_code=204)
