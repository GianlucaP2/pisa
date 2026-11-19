"""A conversation with the talker. The phone sends its WebRTC offer and gets Pisa's answer; the
audio and the "pisa" data channel then run on that peer (protocol/README.md "CONV-1"). The phone's live socket
still carries the presentation objects and cards Pisa's brain shows while it talks (moves them to the
channel). The channel speaks contract 2 and must be opened with the subprotocol `pisa.v2` (`talk_channel`);
one opened without it is refused.

409 `talk_economy` (the setting, or the budget guard), 409 `provider_key_missing`, 402 `budget_exceeded` and
502 `talker_unavailable` mean: talk in economy, today's path (`OPEN_ERRORS` lists every error). Logs carry ids
only.
"""

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response

from pisa.api import schemas, talk_channel
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import wrist
from pisa.domain.errors import PisaError
from pisa.domain.voice_gate import RATE
from pisa.services.talk import Link
from pisa.services.talk_media import MediaPeer, MediaUnreachable, ice_servers, voice_net
from pisa.services.talk_turns import TalkTurns

router = APIRouter(prefix="/v1/talk", tags=["talk"], responses=ERROR_RESPONSES)


def _errors(description: str) -> dict[str, object]:
    return {"model": schemas.ErrorResponse, "description": description}


# every error `POST /v1/talk/sessions` answers, by status (the `{"error"}` envelope)
OPEN_ERRORS: dict[int | str, dict[str, object]] = {
    400: _errors(
        "validation_failed: a body out of the schema, an enrolment without its route, or a WebRTC offer Pisa "
        "can't answer (send a new offer)"
    ),
    401: _errors("auth_failed, session_expired or device_revoked: the session token"),
    402: _errors("budget_exceeded: a spending cap refuses the talk's first minute; talk in economy"),
    409: _errors(
        "talk_economy: natural mode is off, or the budget guard is on (`economy_reason`: setting, day_guard, "
        "month_guard); provider_key_missing: the talker's provider has no key (`provider`); "
        "voice_consent_missing or voice_cohort_missing: an enrolment that can't start yet. The first two: "
        "talk in economy"
    ),
    500: _errors("internal: a failure on the server (the database unreachable, enrolment not available)"),
    502: _errors("talker_unavailable: the talker's provider refused or is unreachable; talk in economy"),
}


@router.post("/sessions", status_code=201, response_model=schemas.TalkSessionCreated, responses=OPEN_ERRORS)
async def open_session(
    body: schemas.TalkSessionCreate, request: Request, device: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """A conversation starts (a press, the wake word, an App Intent or a notice): the talker's session opens
    and Pisa answers the phone's WebRTC offer. One per device: a new one ends the previous.

    When natural mode can't run now it answers 409 `talk_economy` (the setting, or the budget guard: see
    `economy_reason`), 409 `provider_key_missing` (no key for the talker's provider), 402 `budget_exceeded`
    (a cap refuses the first minute) or 502 `talker_unavailable`: the app talks in economy. An enrolment
    (`purpose: enrol`) answers 409 `voice_consent_missing` or `voice_cohort_missing` until it can start.
    400 `validation_failed` for a body or an offer Pisa can't use; 401 for the session token."""
    servers = ice_servers(runtime.settings.talk_ice_servers)
    net = voice_net(runtime.settings)
    answer: list[str] = []
    if body.purpose == "enrol":  # their voice's enrolment over this same path; no talker
        if body.route is None:
            raise PisaError("validation_failed", "An enrolment needs its route.", "Send the route with it")
        await runtime.voice_id.can_enrol()  # their consent and the cohort first: never a peer for nothing
        peer = MediaPeer(servers, RATE, talk_channel.enrol_dialects(), net)
        try:
            sdp = await peer.answer(body.sdp_offer)
            session_id = await runtime.talk.enrol(device.id, body.route, peer)
        except BaseException:
            await asyncio.shield(peer.close())
            raise
        return {"session_id": session_id, "sdp_answer": sdp, "ice_servers": servers}

    turns = TalkTurns(
        runtime, request.app.state.sessions, device.id, body.lang, watch=wrist.origin.get() == "watch"
    )  # the talk opened from the watch (relayed by the phone)
    dialects = talk_channel.talk_dialects(str(device.id), turns.conversation_thread(), body.lang)

    async def link_for(rate: int) -> Link:
        peer = MediaPeer(servers, rate, dialects, net)
        try:
            answer.append(await peer.answer(body.sdp_offer))
        except BaseException as err:  # a cancelled POST closes its peer too
            if not isinstance(err, Exception):
                await asyncio.shield(peer.close())
                raise
            await peer.close()
            if isinstance(err, MediaUnreachable):  # no way in for the phone's audio: voice unavailable
                raise PisaError(
                    "talk_economy",
                    "Voice can't reach this server now (no free media port).",
                    "Pisa talks in economy for now",
                ) from None
            raise PisaError(
                "validation_failed",
                "The WebRTC offer couldn't be answered (an SDP Pisa can't use).",
                "Send a new offer",
            ) from None
        return peer

    line = await runtime.talk.opened_for(device.id, body.ask_id) if body.ask_id is not None else ""
    opening = turns.opening(body.route, line, body.ask_id)

    session_id, _ = await runtime.talk.open(
        device.id, trigger=body.trigger, lang=body.lang, link_for=link_for, think=turns.think,
        first_text=body.first_text or "", opening=opening,
    )  # fmt: skip
    return {"session_id": session_id, "sdp_answer": answer[0], "ice_servers": servers}


@router.delete("/sessions/{session_id}", status_code=204)
async def end_session(
    session_id: uuid.UUID, device: DeviceDep, runtime: RuntimeDep, reason: str | None = None
) -> Response:
    """The user ends the conversation (`talk.ended` reason `owner` on the channel). 404 when it isn't this
    device's live session. `reason` (optional): why the phone ended it (`press`, `call`, `route`,
    `background`, `idle`, `error`), kept in the session's timeline; any other value is ignored."""
    if not runtime.talk.end(session_id, device.id, reason):
        raise PisaError(
            "not_found", "There is no live talk session with that id on this device.", "Start a new one"
        )
    return Response(status_code=204)
