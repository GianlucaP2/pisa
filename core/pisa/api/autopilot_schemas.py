"""REST models of the autopilot: the ledger, stop everything, the vault, earned autonomy. The contract:
protocol/README.md."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from pisa.api.schemas import CountryCode, DateTime
from pisa.api.schemas import IsoDay as Day
from pisa.domain import autonomy, ledger, vault
from pisa.services import phone_keys

LineKind = ledger.Kind
Source = ledger.Source
ActionType = autonomy.ActionType
Level = autonomy.Level
DocKind = vault.Kind
Week = Annotated[str, Field(pattern=r"^\d{4}-W\d{2}$")]
Money = Annotated[str, Field(pattern=r"^\d+\.\d{2}$", description="A decimal string")]


# --- the ledger ---------------------------------------------------------------------------------------------


class LineUndo(BaseModel):
    available: bool = Field(description="POST /v1/ledger/{id}/undo works now")
    until: DateTime | None = Field(description="When the Undo ends; null when it never could, or has no end")


class LedgerLine(BaseModel):
    id: str = Field(description="decision:<uuid>, done:<uuid> or bulk:<uuid> (opaque)")
    at: DateTime
    kind: LineKind
    title: str | None = Field(description="The item's title (the user's data); null when there is none")
    why: str = Field(description="Pisa's reason in the user's language; never an email's text")
    source: Source
    type: ActionType | None = Field(default=None, description="The action type this line counts for")
    undo: LineUndo
    origin: Literal["phone", "watch"] = Field(
        default="phone",
        description='Where they said or decided it; `watch` adds "dall\'orologio" / "from the watch" to '
        "`why`",
    )


class LedgerDay(BaseModel):
    date: Day
    lines: list[LedgerLine] = Field(description="Newest first")


class LedgerTotals(BaseModel):
    lines: int
    undone: int
    by_kind: dict[str, int]
    by_source: dict[str, int]


class LedgerWeek(BaseModel):
    week: Week
    from_: Day = Field(alias="from")
    to: Day
    days: list[LedgerDay] = Field(description="Newest first; 200 lines a page")
    next_page: int | None = Field(
        default=None, description="?page= of the rest of the week; null on the last"
    )
    totals: LedgerTotals

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class LastError(BaseModel):
    on: Day
    what: str | None = Field(default=None, description="The item's title (their own), when Pisa has it")


class Proposal(BaseModel):
    type: ActionType
    level: Level
    kept: int = Field(description="Kept among the actions at the current level")
    undone: int
    days: int = Field(description="The days those actions cover")
    since_on: Day | None = Field(default=None, description="The first action counted")
    bound_pct: int | None = Field(
        default=None, description="The 95% upper bound of the error rate (the rule of three), in %"
    )
    last_error: LastError | None = Field(default=None, description="The type's last undo, if any")
    text: str = Field(default="", description="Pisa's question with its evidence, in the user's language")


class UndoRate(BaseModel):
    type: ActionType
    rung: Level
    kept: int
    undone: int
    rate_pct: int
    bound_pct: int | None


class RubberStamp(BaseModel):
    fast: int = Field(description="Their yeses to Pisa's asks given in under 3 s")
    asked: int
    rate_pct: int


class Surprise(BaseModel):
    undone: int = Field(description="Undos of what Pisa did silently")
    actions: int = Field(description="What Pisa did silently (silent and ledger rungs)")
    per_100: float


class Metrics(BaseModel):
    """The week's three numbers to watch."""

    undo: list[UndoRate] = Field(description="Per type and rung")
    rubber_stamp: RubberStamp
    surprise: Surprise


class Subscription(BaseModel):
    name: str
    amount: Money
    currency: Literal["EUR", "USD", "GBP"]
    period: Literal["monthly", "yearly"]
    last_on: Day
    count: int
    text: str = Field(
        description='Pisa\'s question ("vuoi che ti prepari la disdetta?"); Pisa never contacts a company'
    )


class SheetOut(BaseModel):
    """A sheet (`present` kind `sheet`'s data): the app draws it with its own templates."""

    title: str
    blocks: list[dict[str, object]]


class SkillRef(BaseModel):
    id: str
    name: str
    text: str | None = Field(default=None, description='A withered one: "meteo: la tengo?"')


class SkillsWeek(BaseModel):
    learned: list[SkillRef] = Field(description="Skills turned on this week")
    withered: list[SkillRef] = Field(description="Skills unused for 30 days")


class CostIn(BaseModel):
    amount: Money
    currency: Literal["EUR"]
    ai: Money = Field(description="The AI ledger's settled costs")
    services: Money = Field(
        description="The paid services' recorded calls (maps, places), after free allowances"
    )


class MoneyFound(BaseModel):
    amount: Money
    currency: Literal["EUR", "USD", "GBP"]


class FoundItems(BaseModel):
    claim: int = Field(description="Flight compensations they marked paid")
    refund: int = Field(description="Refunds received")
    subscription: int = Field(description="Subscriptions they cancelled themselves")


class CostFound(BaseModel):
    """The month (of the card's Sunday) to the week's end: what Pisa cost, what it found."""

    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    cost: CostIn
    found: list[MoneyFound] = Field(description="By currency; items with no recorded amount are counted only")
    found_items: FoundItems
    unvalued: int = Field(0, ge=0, description="Items found with no recorded amount (counted, not summed)")
    text: str = Field(max_length=600, description="The card's line, from code")


class Unlock(BaseModel):
    """A type earned autonomy promoted, with the way back."""

    id: str
    type: ActionType
    level: Literal["tell", "silent", "ledger"]
    at: str
    text: str = Field(max_length=600)


class Store(BaseModel):
    """Pisa keeps everything until they delete it: what that costs."""

    bytes: int = Field(ge=0, description="The database's size")
    families: dict[str, int] = Field(
        description="Rows kept per deletion family (messages, places, mail, money, records, memory)"
    )
    offsite_backup_at: DateTime | None = Field(
        default=None, description="The off-site copy's last upload; null while it is not configured"
    )


class CostPerOutcome(BaseModel):
    spent_eur: str = Field(description="The user budget's AI spend in the week, failed calls' cost included")
    yes: int = Field(description="Their yeses to changes Pisa proposed or held, done and not undone")
    tasks_done: int = Field(
        description="Tasks Pisa made (an email, their calendar, Pisa's own) done this week"
    )
    drafts_sent: int = Field(description="Pisa's email drafts they sent this week, not undone")
    accepted: int
    per_accepted_eur: str | None = Field(description="spent_eur / accepted; null when nothing was accepted")


class Interruptions(BaseModel):
    """The week's unprompted items through the gate (counts only)."""

    held: int = Field(description="Held for a breakpoint or the digest")
    passed: int = Field(description="Passed at once")
    never_out: int = Field(description="Held and never got out (the neglect report)")
    holding: int = Field(description="Held now and not yet out (not the week's)")


class Holding(BaseModel):
    """What Pisa holds now for later ("Pisa ne tiene N per dopo"), a count only."""

    holding: int = Field(ge=0, description="Held now and not yet out")


class LedgerSummary(BaseModel):
    week: Week
    counts: dict[str, int] = Field(description="Lines by kind")
    total: int
    undone: int
    mattered: list[LedgerLine] = Field(description="The week's three things that mattered")
    undoable: list[LedgerLine] = Field(description="Lines that can still be undone, at most 10")
    proposals: list[Proposal]
    subscriptions: list[Subscription]
    skills: SkillsWeek | None = Field(default=None, description="What Pisa learned, the withered skills")
    metrics: Metrics | None = Field(default=None, description="Undo, rubber-stamp, surprise rates")
    cost_found: CostFound | None = Field(
        default=None, description='The month\'s "Pisa cost you / found you", only what is recorded'
    )
    unlocks: list[Unlock] = Field(
        default_factory=list, description="Abilities unlocked, announced once in this card"
    )
    cost_per_outcome: CostPerOutcome | None = Field(
        default=None, description="The week's AI spend over the outcomes they accepted (counts only)"
    )
    store: Store | None = Field(default=None, description="The cost of keeping (counts only)")
    interruptions: Interruptions | None = Field(
        default=None, description='The week\'s "trattenute N / passate M"'
    )
    sheet: SheetOut


# --- stop everything ----------------------------------------------------------------------------------------


class StopIn(BaseModel):
    scope: Literal["all"] = "all"


class StopState(BaseModel):
    stopped: bool
    since: DateTime | None = None
    by: Literal["app", "voice"] | None = None
    paused: list[
        Literal[
            "follow_ups",
            "scheduled_pushes",
            "planning",
            "email_tasks",
            "vault_reminders",
            "autonomy",
            "ready_answers",
        ]
    ] = Field(description="What waits while stopped (empty when running)")


# --- the vault ----------------------------------------------------------------------------------------------


class Reminder(BaseModel):
    days_before: Annotated[int, Field(ge=1, le=730)]
    done: bool = False


class Abroad(BaseModel):
    where: str
    on: Day


class Document(BaseModel):
    id: str
    kind: DocKind
    country: CountryCode | None = Field(
        default=None, description="null: not said, and no default for its kind"
    )
    number_last4: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{4}$")] | None = None
    expires_on: Day
    issued_on: Day | None = None
    notes: Annotated[str, Field(max_length=300)] | None = None
    reminders: list[Reminder]
    task_id: str | None = Field(
        default=None, description="The renewal task in the pool, once a reminder fired"
    )
    abroad: Abroad | None = Field(
        default=None, description="Where they'll be (the other country) in the window"
    )
    warning: str | None = Field(
        default=None,
        description="A CIE whose notes say it is a paper card: no longer valid for travel abroad",
    )
    created_at: DateTime
    updated_at: DateTime


class Aire(BaseModel):
    """Whether they are registered with AIRE; null until they say (Pisa asks once, never assumes)."""

    registered: bool | None = None
    asked_on: Day | None = Field(default=None, description="When Pisa asked it (once)")


class AireIn(BaseModel):
    registered: bool | None = Field(description="null forgets it (Pisa may ask again)")


class Documents(BaseModel):
    documents: list[Document] = Field(description="Soonest expiry first")
    aire: Aire = Field(default_factory=Aire)


class DocumentIn(BaseModel):
    """RAIL: never a full number: `number` is refused (400), `number_last4` is exactly 4 characters."""

    model_config = ConfigDict(extra="allow")

    kind: DocKind
    country: CountryCode | None = Field(
        default=None, description="Default: the issuer for a one-country kind (cie: IT), else PISA_COUNTRY"
    )
    number_last4: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{4}$")] | None = None
    expires_on: Day
    issued_on: Day | None = None
    notes: Annotated[str, Field(max_length=300)] | None = Field(default=None, description="Never a number")
    reminders: Annotated[list[Reminder], Field(min_length=1, max_length=6)] | None = Field(
        default=None, description="Days before the expiry; default by kind"
    )


class DocumentPatch(BaseModel):
    """Any subset of the fields; null clears number_last4, issued_on and notes. kind, country, expires_on and
    reminders can be changed, never cleared (null is a 400)."""

    model_config = ConfigDict(extra="allow")

    kind: DocKind | SkipJsonSchema[None] = None
    country: CountryCode | SkipJsonSchema[None] = None
    number_last4: Annotated[str, Field(pattern=r"^[A-Za-z0-9]{4}$")] | None = None
    expires_on: Day | SkipJsonSchema[None] = None
    issued_on: Day | None = None
    notes: Annotated[str, Field(max_length=300)] | None = None
    reminders: Annotated[list[Reminder], Field(min_length=1, max_length=6)] | SkipJsonSchema[None] = None

    @field_validator("kind", "country", "expires_on", "reminders", mode="before")
    @classmethod
    def _never_cleared(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("it can be changed, never cleared")
        return value


class Bill(BaseModel):
    id: str
    from_: str = Field(alias="from")
    subject: str
    amount: Money | None
    currency: Literal["EUR", "USD", "GBP"] | None
    due_on: Day | None
    paid: bool
    source_email_id: str
    task_id: str | None = None
    payee: str | None = Field(
        default=None, max_length=120, description="The company the bill's PDF names (outside text)"
    )
    iban_last4: str | None = Field(
        default=None, pattern=r"^[0-9A-Z]{4}$", description="The IBAN's last 4, never the rest"
    )
    source: Literal["email", "pdf"] = Field(
        default="email",
        description="pdf when the bill's PDF was read (its facts completed the email's)",
    )

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class Bills(BaseModel):
    bills: list[Bill] = Field(description="Newest first, at most 50")


# --- autonomy -----------------------------------------------------------------------------------------------


class AutonomyProposal(BaseModel):
    level: Level
    kept: int
    undone: int
    days: int
    since_on: Day | None = Field(default=None, description="The first action counted")
    bound_pct: int | None = Field(
        default=None, description="The 95% upper bound of the error rate (the rule of three), in %"
    )
    last_error: LastError | None = Field(default=None, description="The type's last undo, if any")


class Autonomy(BaseModel):
    type: ActionType
    level: Level
    kept: int = Field(description="Kept in the last 30 days")
    undone: int = Field(description="Undone in the last 30 days")
    since: DateTime = Field(description="When the level was set")
    locked: bool = Field(description="true: stays at ask (a send to other people)")
    proposal: AutonomyProposal | None = None


class AutonomyList(BaseModel):
    types: list[Autonomy]
    stopped: bool = Field(description="While stopped every level acts as ask")
    proposals_from: DateTime | None = Field(
        default=None, description="After a stop, Pisa proposes nothing until then (14 days)"
    )


class AutonomySet(BaseModel):
    level: Level
    nonce: Annotated[str, Field(min_length=1, max_length=200)]
    signature: Annotated[str, Field(min_length=1, max_length=400, description="DER, base64")]
    key_id: Annotated[str, Field(min_length=1, max_length=128)]


class ChallengeIn(BaseModel):
    action: Literal["autonomy", "add_key", "skill", "approval", "device"]
    approval_id: str | None = Field(default=None, description="approval: the approval to approve")
    interrupt_id: Annotated[str, Field(min_length=1, max_length=80)] | None = Field(
        default=None,
        description="approval (required): the approval's interrupt (`int-<approval_id>`); the canonical is "
        "`pisa-approval/2` (server, this device, the interrupt, the digest of `pisa-approval-digest/2`)",
    )
    op: phone_keys.DeviceOp | None = Field(default=None, description="device: the user action")
    target: Annotated[str, Field(min_length=1, max_length=200)] | None = Field(
        default=None,
        description="device: what it acts on (revoke_device: the device id; "
        "provider_key: `<provider_id>:<SHA-256 hex of the key>`; google_connect: the SHA-256 hex of the "
        "server auth code; google_disconnect: `google`; "
        "place_key: `<provider_id>:<SHA-256 hex of the key>`; google_credentials: the SHA-256 hex of "
        "`<web_client_id>\\n<web_client_secret>\\n<ios_client_id>`; messages_retention: the shorter value, "
        "`1y` or `90d`; erase: `<family>:<YYYY-MM-DD>`; connector: `<id>:<SHA-256 hex of the PATCH "
        "body as sent>`; connector_credential: `<id>:<SHA-256 hex of the PUT body as sent>` or "
        "`<id>:delete`; "
        "connector_consent: `<id>:start` or `<id>:<SHA-256 hex of the bank's code>`; model_server: "
        "`<server id>:<SHA-256 hex of the POST or PATCH body as sent>`)",
    )
    skill_id: str | None = None
    type: ActionType | None = None
    level: Level | None = None
    key_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    public_key: Annotated[str, Field(min_length=1, max_length=400)] | None = None


class Challenge(BaseModel):
    nonce: str
    canonical: str = Field(description="What the phone shows and signs (UTF-8) after Face ID")
    expires_at: DateTime
    action: Literal["autonomy", "add_key", "skill", "approval", "device"]


class PhoneKeyIn(BaseModel):
    key_id: Annotated[str, Field(min_length=1, max_length=128)]
    public_key: Annotated[str, Field(min_length=1, max_length=400, description="P-256, X9.63, base64")]
    label: Annotated[str, Field(max_length=80)] | None = None
    nonce: str | None = Field(default=None, description="Once a key exists: an add_key challenge's nonce")
    signature: str | None = None
    signer_key_id: str | None = None


class PhoneKey(BaseModel):
    key_id: str
    label: str | None
    created_at: DateTime


# --- skills -------------------------------------------------------------------------------------------------


class SkillSchedule(BaseModel):
    at: Annotated[str, Field(pattern=r"^\d{2}:\d{2}$")]
    days: Literal["daily", "weekdays", "weekends"]


class Skill(BaseModel):
    id: str
    name: str
    kind: Literal["view", "tool"]
    status: Literal["proposed", "trial", "on", "withered", "off"]
    made_from: str = Field(description="The user's own sentence")
    tests_passed: int
    tests_total: int
    last_used_at: DateTime | None
    uses_30d: int
    schedule: SkillSchedule | None = None
    created_at: DateTime
    enabled_at: DateTime | None


class Skills(BaseModel):
    skills: list[Skill] = Field(description="Newest first, at most 30")


class SkillCheck(BaseModel):
    input: str
    shows: str | None = None
    has: str | None = None
    passed: bool


class SkillTrial(BaseModel):
    at: DateTime
    sheet: SheetOut


class SkillDetail(Skill):
    checks: list[SkillCheck]
    trial: SkillTrial | None = None


class SkillEnable(BaseModel):
    nonce: Annotated[str, Field(min_length=1, max_length=200)]
    signature: Annotated[str, Field(min_length=1, max_length=400, description="DER, base64")]
    key_id: Annotated[str, Field(min_length=1, max_length=128)]
