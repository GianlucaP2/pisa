"""The autopilot's routes: the ledger and the Sunday card, stop everything, the vault and
the bills, earned autonomy with the phone's signature. The contract: protocol/README.md."""

from typing import Any

from fastapi import APIRouter, Response

from pisa.api import autopilot_schemas as s
from pisa.api.deps import DeviceDep, LangDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.services import guardian_tick, phone_keys

router = APIRouter(prefix="/v1", tags=["autopilot"], responses=ERROR_RESPONSES)


# --- the ledger and the Sunday card -------------------------------------------------------------------------


@router.get("/ledger", response_model=s.LedgerWeek)
async def ledger_week(
    _: DeviceDep, runtime: RuntimeDep, week: str | None = None, page: int = 0
) -> dict[str, Any]:
    """Everything Pisa did in an ISO week (default: this one), by day, newest first: why, from where, its
    Undo. 200 lines a page; `next_page` names the next one (null on the last)."""
    return await runtime.autopilot.ledger.week(week, max(0, page))


@router.get("/ledger/summary", response_model=s.LedgerSummary)
async def ledger_summary(_: DeviceDep, runtime: RuntimeDep, week: str | None = None) -> dict[str, Any]:
    """The Sunday card: counts, the three things that mattered, what can still be undone, Pisa's autonomy
    proposals, the subscriptions seen, and the sheet the app draws."""
    return await runtime.autopilot.ledger.summary(week)


@router.get("/guardian/holding", response_model=s.Holding)
async def guardian_holding(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """What Pisa holds now and has not yet let out: a count, light enough to read every
    few minutes; it changes nothing."""
    async with runtime.pool.connection() as conn:
        return {"holding": await guardian_tick.holding(conn)}


@router.post("/ledger/{line_id}/undo", response_model=s.LedgerLine)
async def ledger_undo(line_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The line's own Undo while it lasts (409 `undo_expired` after, or when it never had one)."""
    return await runtime.autopilot.ledger.undo(line_id)


# --- stop everything ----------------------------------------------------------------------------------------


@router.get("/stop", response_model=s.StopState)
async def stop_state(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.autopilot.stops.state()


@router.post("/stop", response_model=s.StopState)
async def stop(body: s.StopIn, _: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    """Pause Pisa's own initiative ("Pisa, ferma tutto"); their own requests still work. Idempotent."""
    return await runtime.autopilot.stops.stop("app", lang)


@router.post("/stop/resume", response_model=s.StopState)
async def resume(_: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    return await runtime.autopilot.stops.resume("app", lang)


# --- the vault and the bills --------------------------------------------------------------------------------


@router.get("/vault/documents", response_model=s.Documents)
async def documents(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    vault = runtime.autopilot.vault
    return {"documents": await vault.documents(), "aire": await vault.aire()}


@router.put("/vault/aire", response_model=s.Aire)
async def set_aire(body: s.AireIn, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their answer about AIRE from the app (null forgets it)."""
    return await runtime.autopilot.vault.set_aire(body.registered, "app")


@router.post("/vault/documents", status_code=201, response_model=s.Document)
async def add_document(
    body: s.DocumentIn, _: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    """Keep a document with its expiry; never a full number (`number` is refused: `number_last4` only)."""
    fields = body.model_dump(exclude_none=True, mode="json")
    return await runtime.autopilot.vault.add(fields, provenance="app", lang=lang)


@router.patch("/vault/documents/{document_id}", response_model=s.Document)
async def update_document(
    document_id: str, body: s.DocumentPatch, _: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    return await runtime.autopilot.vault.update(
        document_id, body.model_dump(exclude_unset=True, mode="json"), lang
    )


@router.delete("/vault/documents/{document_id}", status_code=204, response_class=Response)
async def delete_document(document_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    """Its open renewal task is dismissed (they can reopen it)."""
    await runtime.autopilot.vault.delete(document_id)
    return Response(status_code=204)


@router.get("/vault/bills", response_model=s.Bills)
async def bills(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Bills Pisa recognised in their email (subject and snippet only), newest first."""
    return {"bills": await runtime.autopilot.money.bills()}


# --- earned autonomy ----------------------------------------------------------------------------------------


@router.get("/autonomy", response_model=s.AutonomyList)
async def autonomy(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.autopilot.autonomy.listed()


@router.post("/autonomy/{action_type}/ask", response_model=s.Autonomy)
async def autonomy_ask(action_type: str, _: DeviceDep, runtime: RuntimeDep, lang: LangDep) -> dict[str, Any]:
    """Back to `ask` for this kind: no signature (asking more is never a risk)."""
    return await runtime.autopilot.autonomy.ask_signless(action_type, lang)


@router.post("/autonomy/{action_type}", response_model=s.Autonomy)
async def set_autonomy(
    action_type: str, body: s.AutonomySet, device: DeviceDep, runtime: RuntimeDep, lang: LangDep
) -> dict[str, Any]:
    """Change a level: only with a signature over an `autonomy` challenge, by a Face ID key or this device's
    own key (`device-<device id>`); 403 `biometric_required` otherwise. `email_send` stays at ask (400)."""
    return await runtime.autopilot.autonomy.set_signed(
        action_type,
        body.level,
        body.nonce,
        body.signature,
        body.key_id,
        lang,
        device_id=device.id,
    )


@router.post("/phone/challenge", response_model=s.Challenge)
async def phone_challenge(body: s.ChallengeIn, device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """What the phone signs after Face ID: an autonomy level, or a new key (once one exists). An approval with
    `approval_id` and `interrupt_id` gets contract 2's `pisa-approval/2` text, bound to this device
    (CONTRACT-2 §6.2)."""
    return await runtime.autopilot.keys.challenge_for(
        phone_keys.ChallengeRequest(**body.model_dump()),
        device.id,
        skill=runtime.autopilot.skills.challenge,
        approval_subject=runtime.approvals.challenge_subject,
    )


@router.post("/phone/keys", status_code=201, response_model=s.PhoneKey)
async def phone_key(body: s.PhoneKeyIn, device: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Register the phone's P-256 key (shared with). The first needs nothing; later ones need a signature
    from a registered key over an `add_key` challenge."""
    return await runtime.autopilot.keys.register(
        body.key_id,
        body.public_key,
        body.label,
        device_id=device.id,
        platform=device.platform,
        nonce=body.nonce,
        signature=body.signature,
        signer_key_id=body.signer_key_id,
    )


# --- skills -------------------------------------------------------------------------------------------------


@router.get("/skills", response_model=s.Skills)
async def skills(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Pisa's skills, newest first: what each does (their sentence), its status, its checks passed, its
    use."""
    return {"skills": await runtime.autopilot.skills.listed()}


@router.get("/skills/{skill_id}", response_model=s.SkillDetail)
async def skill(skill_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """One skill with its checks and its trial's sheet ("Lo accendo?")."""
    return await runtime.autopilot.skills.get(skill_id)


@router.post("/skills/{skill_id}/enable", response_model=s.Skill)
async def enable_skill(
    skill_id: str, body: s.SkillEnable, device: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    """On only with a signature over a `skill` challenge, by a Face ID key or this device's own key
    (`device-<device id>`; 403 `biometric_required` otherwise), and only a view whose trial passed every
    check (400)."""
    return await runtime.autopilot.skills.enable(
        skill_id, body.nonce, body.signature, body.key_id, device_id=device.id
    )


@router.post("/skills/{skill_id}/disable", response_model=s.Skill)
async def disable_skill(skill_id: str, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.autopilot.skills.disable(skill_id)


@router.delete("/skills/{skill_id}", status_code=204, response_class=Response)
async def delete_skill(skill_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    await runtime.autopilot.skills.delete(skill_id)
    return Response(status_code=204)
