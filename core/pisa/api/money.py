"""The routes: flight claims, refunds promised, subscriptions, the Italian bill watchdog,
warranties, promises, supplies. Pisa finds and drafts; the user sends: nothing here contacts a company. The
contract: protocol/README.md."""

from typing import Any

from fastapi import APIRouter, Response

from pisa.api import money_schemas as s
from pisa.api.deps import DeviceDep, RuntimeDep
from pisa.api.rest import ERROR_RESPONSES
from pisa.domain import locale

router = APIRouter(prefix="/v1/money", tags=["money"], responses=ERROR_RESPONSES)
Lang = locale.Tag


@router.get("/claims", response_model=s.Claims)
async def claims(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """What a disrupted flight of their may be owed (EU261, US DOT), newest flight first."""
    return {"claims": await runtime.money_back.claims()}


@router.patch("/claims/{claim_id}", response_model=s.Claim)
async def patch_claim(claim_id: str, body: s.ClaimPatch, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their word: sent, paid, dismissed; or the delay they know (weighed again)."""
    return await runtime.money_back.patch_claim(claim_id, status=body.status, delay_min=body.delay_min)


@router.get("/claims/{claim_id}/draft", response_model=s.Draft)
async def claim_draft(
    claim_id: str, _: DeviceDep, runtime: RuntimeDep, lang: Lang | None = None
) -> dict[str, Any]:
    """The claim letter for them to send, and the buttons to the airline's site and the public form."""
    return await runtime.money_back.claim_draft(claim_id, lang or runtime.owner.tag)


@router.get("/refunds", response_model=s.Refunds)
async def refunds(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return {"refunds": await runtime.money_back.refunds()}


@router.patch("/refunds/{refund_id}", response_model=s.Refund)
async def patch_refund(
    refund_id: str, body: s.RefundPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    return await runtime.money_back.patch_refund(refund_id, body.status)


@router.get("/subscriptions", response_model=s.Subs)
async def subscriptions(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Their subscriptions from the receipts, flagged (duplicate, unused, price up), with the page to cancel
    them themselves. Pisa never cancels."""
    return {"subscriptions": await runtime.money_back.subscriptions()}


@router.patch("/subscriptions/{sub_id}", response_model=s.Sub)
async def patch_subscription(
    sub_id: str, body: s.SubPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    return await runtime.money_back.patch_subscription(
        sub_id, used=body.used, keep=body.keep, cancelled=body.cancelled
    )


@router.get("/bill_checks", response_model=s.BillChecks)
async def bill_checks(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """Italian utility bills charging consumption over 2 years old (prescrizione biennale)."""
    return {"bill_checks": await runtime.money_back.bill_checks()}


@router.get("/bill_checks/{check_id}/draft", response_model=s.Draft)
async def bill_draft(
    check_id: str, _: DeviceDep, runtime: RuntimeDep, lang: Lang | None = None
) -> dict[str, Any]:
    return await runtime.money_back.bill_draft(check_id, lang or runtime.owner.tag)


@router.patch("/bill_checks/{check_id}", response_model=s.BillCheck)
async def patch_bill_check(
    check_id: str, body: s.BillCheckPatch, _: DeviceDep, runtime: RuntimeDep
) -> dict[str, Any]:
    return await runtime.money_back.patch_bill_check(check_id, body.status)


@router.get("/warranties", response_model=s.Warranties)
async def warranties(_: DeviceDep, runtime: RuntimeDep, q: str | None = None) -> dict[str, Any]:
    """What they bought, with the EU legal guarantee's dates (in force first); `q` names an item."""
    return {"warranties": await runtime.money_back.warranties(q[:80] if q else None)}


@router.post("/warranties", status_code=201, response_model=s.Warranty)
async def add_warranty(body: s.WarrantyIn, _: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    return await runtime.money_back.add_purchase(
        item=body.item,
        purchased_on=body.purchased_on,
        seller=body.seller,
        delivered_on=body.delivered_on,
        region=body.region,
        source="app",
    )


@router.delete("/warranties/{purchase_id}", status_code=204, response_class=Response)
async def delete_warranty(purchase_id: str, _: DeviceDep, runtime: RuntimeDep) -> Response:
    await runtime.money_back.delete_purchase(purchase_id)
    return Response(status_code=204)


@router.get("/promises", response_model=s.Promises)
async def promises(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """The promises in their own sent emails, each a task in the pool."""
    return {"promises": await runtime.money_back.promises()}


@router.get("/supplies", response_model=s.Supplies)
async def supplies(_: DeviceDep, runtime: RuntimeDep) -> dict[str, Any]:
    """What they buy again and again, and when it runs out."""
    return {"supplies": await runtime.money_back.supplies()}
