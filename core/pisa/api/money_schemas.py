"""REST models: money that comes back, promises, things that run out. The contract:
protocol/README.md."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pisa.api.schemas import DateTime
from pisa.api.schemas import IsoDay as Day

Money = Annotated[str, Field(pattern=r"^\d+\.\d{2}$", description="A decimal string")]
Currency = Literal["EUR", "USD", "GBP"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(BaseModel):
    id: str
    flight: str
    place: str | None = None
    scheduled_at: DateTime
    disruption: Literal["delay", "cancellation", "denied_boarding"]
    delay_min: int | None = None
    notice_days: int | None = None
    rule: Literal["eu261", "us_dot", "none"]
    status: Literal["possible", "eligible", "not_eligible", "sent", "paid", "dismissed"]
    reason: str
    why: str
    distance_km: int | None = None
    amount: Money | None = None
    currency: Literal["EUR"] | None = None
    deadline_on: Day | None = None
    source: Literal["email", "said"]
    task_id: str | None = None
    created_at: DateTime
    updated_at: DateTime


class Claims(BaseModel):
    claims: list[Claim]


class ClaimPatch(_Strict):
    status: Literal["sent", "paid", "dismissed"] | None = None
    delay_min: Annotated[int, Field(ge=0, le=4320)] | None = None


class Draft(BaseModel):
    lang: str
    subject: str
    text: str
    buttons: list[dict[str, Any]] = Field(description="`handoff` blocks (kind open_url)")


class Refund(BaseModel):
    id: str
    merchant: str
    amount: Money | None = None
    currency: Currency | None = None
    promised_on: Day
    expected_by: Day
    status: Literal["waiting", "overdue", "received", "dismissed"]
    source_email_id: str | None = None
    task_id: str | None = None


class Refunds(BaseModel):
    refunds: list[Refund]


class RefundPatch(_Strict):
    status: Literal["received", "dismissed"]


class Cancel(BaseModel):
    url: str | None = None
    host: str | None = None
    steps: str


class Sub(BaseModel):
    id: str
    name: str
    service: str | None = None
    amount: Money
    currency: Currency
    period: Literal["monthly", "yearly"]
    first_on: Day
    last_on: Day
    count: int
    flags: list[Literal["duplicate", "unused", "price_up"]]
    duplicate_of: str | None = None
    last_use_on: Day | None = None
    cancelled_on: Day | None = Field(default=None, description="The day they said they cancelled it")
    cancel: Cancel | None = None
    text: str


class Subs(BaseModel):
    subscriptions: list[Sub]


class SubPatch(_Strict):
    used: bool = False
    keep: bool = False
    cancelled: bool = Field(
        default=False, description="They cancelled it themselves (Pisa never cancels): counted as found"
    )


class BillCheck(BaseModel):
    id: str
    supplier: str
    utility: Literal["luce", "gas", "acqua"]
    issued_on: Day
    period_from: Day | None = None
    period_to: Day | None = None
    amount: Money | None = None
    old_amount: Money | None = None
    currency: Literal["EUR"] | None = None
    reason: Literal["older_than_2_years", "stated_by_supplier"]
    status: Literal["flagged", "contested", "dismissed"]
    source_email_id: str | None = None
    task_id: str | None = None


class BillChecks(BaseModel):
    bill_checks: list[BillCheck]


class BillCheckPatch(_Strict):
    status: Literal["contested", "dismissed"]


class Warranty(BaseModel):
    id: str
    item: str
    seller: str | None = None
    purchased_on: Day
    delivered_on: Day | None = None
    price: Money | None = None
    currency: Currency | None = None
    region: Literal["EU", "US"]
    presumption_until: Day | None = None
    ends_on: Day | None = None
    action_until: Day | None = None
    in_warranty: bool
    source: Literal["email", "said", "app"]


class Warranties(BaseModel):
    warranties: list[Warranty]


class WarrantyIn(_Strict):
    item: Annotated[str, Field(min_length=2, max_length=160)]
    purchased_on: Day
    seller: Annotated[str, Field(max_length=120)] | None = None
    delivered_on: Day | None = None
    region: Literal["EU", "US"] | None = None


class Promise(BaseModel):
    id: str
    what: str
    to: str
    said_on: Day
    due_on: Day | None = None
    task_id: str | None = None


class Promises(BaseModel):
    promises: list[Promise]


class Supply(BaseModel):
    id: str
    item: str
    seller: str | None = None
    count: int
    every_days: int
    last_on: Day
    next_on: Day
    task_id: str | None = None
    handoff: dict[str, Any] | None = Field(default=None, description="A `handoff` block")


class Supplies(BaseModel):
    supplies: list[Supply]
