"""REST request and response bodies. They also generate protocol/openapi.json."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from pisa.domain import approvals, locale
from pisa.domain import protocol_versions as versions
from pisa.domain import voice as voice_rules
from pisa.domain.ai import Capability
from pisa.domain.erase import Family as EraseFamily
from pisa.domain.errors import ErrorCode, PisaError
from pisa.domain.model_caps import ServerCapability
from pisa.logging import get_logger
from pisa.planner.options import ChangeAction

_log = get_logger("pisa.api")

# RFC 3339 timestamps are sent as strings with their offset; the schema says so (`format: date-time`).
# A country: ISO 3166-1 alpha-2, upper case ("IT", "DE").
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")]
DateTime = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
DAY_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
IsoDay = Annotated[str, Field(pattern=DAY_PATTERN, json_schema_extra={"format": "date"})]


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    action: str
    economy_reason: Literal["setting", "day_guard", "month_guard"] | None = Field(
        default=None,
        description="409 talk_economy: why the talk is in economy: the setting, or half the day's or "
        "the month's AI cap reached",
    )
    provider: str | None = Field(
        default=None,
        description='409 provider_key_missing: the provider whose key is missing ("google" for Gemini)',
    )
    capability: str | None = Field(
        default=None,
        description="409 model_capability_missing: what the use needs and the model lacks, as in a slot's "
        "`needs`",
    )
    model: str | None = Field(
        default=None, description="409 model_capability_missing: the model reference that lacks it"
    )
    slots: list[str] | None = Field(
        default=None,
        description="409 provider_in_use: the feature slots the key still serves; 400 model_incompatible "
        "(a key's replacement): the slots no model of the replacement can serve",
    )
    reason: Literal["paid_ack_required"] | None = Field(
        default=None,
        description="409 place_provider_not_configured: `paid_ack_required` when a paid provider is set up "
        "but waits for the user's OK of its monthly cap (`PUT /v1/connections/paid/{provider}`)",
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


class NewConversation(BaseModel):
    conversation_id: UUID = Field(description="the new conversation (the next turn's)")
    thread_id: str = Field(description="the device's thread, conversation:<deviceId>, the same as before")


class _Discovery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryContract(_Discovery):
    versions: list[str] = Field(min_length=1, description="The contract versions this server speaks")


class DiscoveryA2ui(_Discovery):
    versions: list[str]
    catalogIds: list[str]


class DiscoveryFeature(_Discovery):
    id: str
    version: int


class DiscoveryCheck(_Discovery):
    name: Literal["database", "migrations", "models", "push", "clock"]
    ok: bool


class DiscoveryReadiness(_Discovery):
    ready: bool
    checks: list[DiscoveryCheck] = Field(default_factory=list)


class DiscoveryReach(_Discovery):
    scheme: Literal["https", "http"]
    network: Literal["public", "tailnet", "lan", "unknown"]


class DiscoveryPush(_Discovery):
    modes: list[Literal["relay", "direct", "none"]]
    relayConfigured: bool


class DiscoveryPairing(_Discovery):
    methods: list[Literal["qr_v1", "typed_v1"]]


class DiscoveryAi(_Discovery):
    disclosureRevision: str


class DiscoveryDeprecation(_Discovery):
    what: str
    sunset: date


class ServerDiscovery(_Discovery):
    """Contract 2's `$defs/ServerDiscovery` (CONTRACT-2 §4.1): what this server is and offers, before pairing.
    Nothing personal: no addresses, pins or provider names, only the AI disclosure's revision."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"product": "pisa", "serverId": "srv-a41d77c0e5f3a41d77c0e5", "serverVersion": "0.1.0",
                 "contract": {"versions": list(versions.CONTRACTS)}, "agui": [versions.AGUI],
                 "a2ui": {"versions": [versions.A2UI], "catalogIds": list(versions.A2UI_CATALOG_IDS)},
                 "mcpApps": [versions.MCP_APPS], "features": [{"id": "stop", "version": 1}],
                 "readiness": {"ready": True, "checks": [{"name": "database", "ok": True}]},
                 "reach": {"scheme": "https", "network": "tailnet"},
                 "push": {"modes": ["none"], "relayConfigured": False}}
            ]
        },
    )  # fmt: skip

    product: Literal["pisa"]
    serverId: str = Field(
        description="An opaque random id the server picks once, at install (signed approvals bind it)"
    )
    serverVersion: str
    contract: DiscoveryContract
    agui: list[str]
    a2ui: DiscoveryA2ui
    mcpApps: list[str]
    features: list[DiscoveryFeature] = Field(description="The `/v2/live` welcome's list")
    readiness: DiscoveryReadiness
    reach: DiscoveryReach
    push: DiscoveryPush
    pairing: DiscoveryPairing | None = None
    ai: DiscoveryAi | None = None
    deprecations: list[DiscoveryDeprecation] | None = None  # none announced: absent


class LastTest(BaseModel):
    ok: bool | None  # null: the provider can't be verified for free (Z.ai, Qwen)
    at: DateTime
    message: str


class Provider(BaseModel):
    id: str
    name: str
    type: Literal["builtin", "openai_compatible"] = Field(
        description="builtin: a provider Pisa knows; openai_compatible: a server the user added (vLLM, "
        "SGLang, llama.cpp, Ollama, LM Studio, a LiteLLM proxy...)"
    )
    key_set: bool = Field(description="A server's key is optional: true when one is stored")
    key_last4: str | None
    last_test: LastTest | None
    keys_url: str | None = Field(description="Where to get a key; null for a server")
    base_url: str | None = Field(description="A server's address, the one that ends in /v1; null otherwise")
    ca_bundle: str | None = Field(
        description="A server's CA bundle: a PEM file on Pisa's server that signs its TLS certificate"
    )
    editable: bool = Field(
        description="false for the built-in providers and for the server PISA_OLLAMA_BASE_URL sets"
    )
    read_timeout_s: int | None = Field(
        default=None,
        description="A server's read timeout: the seconds a request may wait for its next byte (a CPU reads "
        "a long prompt for minutes); null for a built-in provider",
    )
    thinking: dict[str, bool] | None = Field(
        default=None,
        description="A server's thinking switch per slot: whether that slot's calls may think (where the "
        "server can turn it off). Off by default for the fast slots (reflex, triage, confirm); null for a "
        "built-in provider",
    )


class ProvidersResponse(BaseModel):
    providers: list[Provider]


class SetKeyRequest(BaseModel):
    key: Annotated[str, Field(min_length=1, max_length=1000)]


ServerId = Annotated[str, Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9-]{1,31}$")]
ServerName = Annotated[str, Field(min_length=1, max_length=100)]
ServerUrl = Annotated[str, Field(min_length=1, max_length=2000)]
CaBundlePath = Annotated[str, Field(min_length=1, max_length=4096)]


ReadTimeout = Annotated[int, Field(ge=10, le=900)]


class ServerCreate(BaseModel):
    """An OpenAI-compatible server. Its key, when it needs one, is set afterwards with PUT .../key."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["openai_compatible"]
    id: ServerId = Field(description="The provider part of its models' references (`<id>:<model id>`)")
    name: ServerName | None = Field(default=None, description="Shown in Settings; the id when absent")
    base_url: ServerUrl = Field(description="The address that ends in /v1, e.g. http://192.168.1.20:8000/v1")
    ca_bundle: CaBundlePath | None = Field(
        default=None,
        description="An absolute path, on Pisa's server, to the PEM file of the authority that signed the "
        "server's TLS certificate (a private CA)",
    )
    read_timeout_s: ReadTimeout | None = Field(
        default=None, description="Seconds a request may wait for the server's next byte (default 60)"
    )
    thinking: dict[str, bool] | None = Field(
        default=None, description="Per slot id: whether its calls may think (only the slots given change)"
    )


class ServerUpdate(BaseModel):
    """Only the fields sent change; `ca_bundle: null` removes the bundle; `thinking` changes only the slots it
    names."""

    model_config = ConfigDict(extra="forbid")
    name: ServerName | None = None
    base_url: ServerUrl | None = None
    ca_bundle: CaBundlePath | None = None
    read_timeout_s: ReadTimeout | None = None
    thinking: dict[str, bool] | None = None


class TruncatedPrompt(BaseModel):
    sent_tokens: int = Field(description="Pisa's estimate of the prompt it sent")
    read_tokens: int = Field(description="The prompt tokens the server reported reading")
    at: int = Field(description="When, in Unix seconds")


class ProviderCapabilities(BaseModel):
    model: str = Field(description="The model the answer is about")
    probed_at: DateTime | None = Field(description="null when the model was never probed")
    tools: bool
    json_schema: bool
    json_object: bool
    tool_choice: bool
    vision: bool
    audio: bool
    embeddings: bool
    overrides: dict[ServerCapability, bool] = Field(
        description="Only the capabilities the user set by hand over the probe's"
    )
    features_needing_tools: list[str] = Field(description="Feature ids, as in /v1/server's `features`")
    thinking: bool = Field(default=False, description="The probe saw the model think")
    context_tokens: int | None = Field(
        default=None,
        description="The context the server serves the model with, when it says (llama.cpp's /props, "
        "Ollama's /api/ps or a Modelfile's num_ctx)",
    )
    server: Literal["llama.cpp", "ollama"] | None = Field(
        default=None, description="The server's kind, when the probe could tell"
    )
    truncated: TruncatedPrompt | None = Field(
        default=None,
        description="The last time the server read far fewer prompt tokens than Pisa sent (a silent cut, "
        "Ollama past its context); cleared by a probe that finds a context holding that prompt",
    )


class ProviderTestResponse(BaseModel):
    ok: bool | None  # null: "can't verify" (no free key check for this provider)
    message: str
    models_found: int


class Slot(BaseModel):
    id: str
    name: str
    description: str
    needs: list[Capability]
    model: str | None
    fallback: str | None


class SlotProbe(BaseModel):
    model: str = Field(
        description="The model reference probed: a never-probed model on an OpenAI-compatible server"
    )
    probed: bool = Field(
        description="false: the server could not be reached, so the model stays unprobed (its first use "
        "probes it again); the slot is set all the same"
    )
    capabilities: list[Capability] = Field(description="What the model can do now, in the slots' vocabulary")
    message: str = Field(
        description="The outcome in words, with how to probe by hand when the server was down"
    )


class SlotUpdated(Slot):
    probes: list[SlotProbe] = Field(
        description="The probes run while setting the slot: one per never-probed model on an "
        "OpenAI-compatible server among the model and the fallback; empty otherwise"
    )


class SlotsResponse(BaseModel):
    slots: list[Slot]


class SlotUpdate(BaseModel):
    model: str
    fallback: str | None


class Price(BaseModel):
    input: float
    output: float


class CatalogModel(BaseModel):
    ref: str
    provider: str
    model_id: str
    name: str
    capabilities: list[Capability]
    context_tokens: int | None
    price_eur_per_mtok: Price | None
    price_known: bool
    key_set: bool
    compatible: bool
    est_monthly_eur_for_slot: float | None


class ModelsResponse(BaseModel):
    catalog_refreshed_at: DateTime | None
    usd_to_eur: float
    models: list[CatalogModel]


class Caps(BaseModel):
    daily_eur: Annotated[Decimal, Field(ge=0, le=10_000)]
    monthly_eur: Annotated[Decimal, Field(ge=0, le=100_000)]

    @field_validator("daily_eur", "monthly_eur")
    @classmethod
    def _to_cents(cls, value: Decimal) -> Decimal:
        # Clients send JSON doubles (1.1 may arrive as 1.1000000000000001): round, don't reject.
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class CapsResponse(BaseModel):
    daily_eur: float
    monthly_eur: float


class SlotSpend(BaseModel):
    slot: str
    today_eur: float
    month_eur: float
    calls_today: int


class SpendResponse(BaseModel):
    today_eur: float
    month_eur: float
    caps: CapsResponse
    by_slot: list[SlotSpend]


class LastSync(BaseModel):
    gmail: DateTime | None
    calendar: DateTime | None


class ConnectorClient(BaseModel):
    web_client_id: str | None = Field(description="your own Google project's web client")


class Connector(BaseModel):
    id: str
    name: str
    configured: bool
    status: Literal["not_connected", "connected", "error"]
    account: str | None
    scopes: list[str]
    last_sync: LastSync
    message: str | None
    client: ConnectorClient
    redirect_uri: str | None = Field(
        description="the exact redirect URI to give your web client: this server's https address and "
        "/oauth/google/callback (null: the server has no https address, so Google can't come back)"
    )
    pending_account: str | None = Field(
        description="the Google account a sign-in came back with, waiting for the starting device's yes "
        "(POST /google/confirm) or no (POST /google/cancel); null when none"
    )
    pending_started_here: bool = Field(
        default=False,
        description="true on the device that started the sign-in `pending_account` waits on (it shows the "
        'yes/no); false on every other device (they show "waiting for confirmation on the other device") '
        "and whenever nothing is pending",
    )
    requested_scopes: list[str]
    missing_scopes: list[str] = Field(
        description="Requested but not granted (calendar.events, gmail.compose): reconnect Google "
        "to let Pisa change the calendar or send replies"
    )


# --- Collegamenti (Connections) ---------------------------------------------------------------------------


class PaidAckState(BaseModel):
    cap_cents: int = Field(
        description="the monthly cap the user acknowledged for this provider, in euro cents"
    )
    at: str = Field(description="when (ISO 8601)")


class ConnectionEntry(BaseModel):
    id: str = Field(description="unique: `<category>.<provider>`, e.g. `ai.anthropic`, `maps.google`")
    kind: Literal[
        "ai_provider", "model_server", "voice", "talker", "phone_maps", "place_provider", "web_provider",
        "account", "phone_notify", "push_relay", "apns",
    ]  # fmt: skip
    category: Literal["ai", "voice", "maps", "web", "accounts", "notifications"]
    name: str
    provider: str | None = Field(
        description="the id its own endpoints use (`/v1/ai/providers/{provider}/key`, "
        "`/v1/places/providers/{provider}/key`, `/v1/connections/paid/{provider}`)"
    )
    state: Literal["on", "off", "not_set_up", "error", "needs_reconnect"]
    paid: bool = Field(description="a metered service with a price in Pisa's tables")
    paid_ack: PaidAckState | None = Field(
        description="the user's OK of this paid maps or web provider and its monthly cap; null: none (it is "
        "never called), or not a provider that needs one (the AI providers and voices keep the AI caps)"
    )
    needs_paid_ack: bool = Field(description="set up, but never called until the user's OK (`ack_paid`)")
    month_spent_cents: int | None = Field(
        description="a provider that needs the OK: what it cost this month, in euro cents (every budget)"
    )
    data_out: str = Field(description="what leaves the server and to whom: a fixed phrase, never your data")
    docs: str = Field(description="the docs-site page about it")
    actions: list[Literal["set_key", "delete_key", "connect", "disconnect", "test", "ack_paid"]]
    key_last4: str | None = Field(
        description="the key's last four characters, as its own endpoint shows them"
    )
    slots: list[str] = Field(description="AI: the feature slots whose model or fallback is on this provider")


class ConnectionCategory(BaseModel):
    id: Literal["ai", "voice", "maps", "web", "accounts", "notifications"]
    name: str
    entries: list[ConnectionEntry]


class ConnectionsResponse(BaseModel):
    categories: list[ConnectionCategory] = Field(description="always the six, in this order; an entry of a "
                                                 "feature off on this server is absent")  # fmt: skip
    hard_limit_monthly_cents: int = Field(
        description="the server's hard monthly limit: no paid cap passes it"
    )


class PaidAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cap_cents: StrictInt = Field(
        ge=1,
        le=100_000_000,
        description="the provider's own monthly cap, in euro cents (at most the server's "
        "hard monthly limit, `hard_limit_monthly_cents`)",
    )


class PaidAck(BaseModel):
    cap_cents: int
    at: str


class GoogleCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web_client_id: Annotated[str, Field(min_length=1, max_length=300)]
    web_client_secret: Annotated[str, Field(min_length=1, max_length=300)]


class GoogleSetupStep(BaseModel):
    id: str = Field(
        description="https, client, gmail_api, calendar_api, scopes, paste, connect, maps_key (optional: a "
        "Google Maps API key, not a scope)"
    )
    title: str
    body: str
    url: str | None = Field(description="where to do it (Google Cloud), or null")
    copy_: str | None = Field(alias="copy", description="what to copy into Google Cloud, or null")


class GoogleSetup(BaseModel):
    """How to make your own Google project and web client for Pisa, in your language."""

    redirect_uri: str | None
    scopes: list[str]
    steps: list[GoogleSetupStep]
    legal: list[str]


class GoogleStart(BaseModel):
    auth_url: str = Field(description="Google's consent page for your own web client (state and PKCE inside)")
    redirect_uri: str
    expires_at: str = Field(description="the sign-in must finish before this (ISO 8601)")


class TaskSource(BaseModel):
    kind: Literal["voice", "email", "manual", "calendar"] = Field(
        description="calendar is a reminder of a date from their calendar's yearly events"
    )
    ref: str | None
    label: str | None


class TaskRepeat(BaseModel):
    """Contract Q: completing the task makes the next one, due this much later."""

    model_config = ConfigDict(extra="forbid")

    every: Literal["day", "week", "month"]
    interval: int = Field(ge=1, le=12)


class Task(BaseModel):
    id: str
    title: str
    notes: str | None
    status: Literal["open", "done", "dismissed"]
    due_at: DateTime | None
    due_kind: Literal["none", "date", "datetime"]
    duration_min: int | None
    place_text: str | None
    place_id: str | None = Field(description="The place this task goes to, when resolved")
    priority: Literal[1, 2, 3] = Field(description="1 high, 2 mid, 3 low (never planned or reminded)")
    priority_reason: str | None = Field(default=None, description="Why this priority, in one line (Pisa's)")
    priority_source: Literal["pisa", "owner"] = Field(
        default="pisa", description="owner: the user raised or lowered it (PATCH priority)"
    )
    deadline_source: Literal["owner", "email", "web", "pisa"] | None = Field(
        default=None,
        description="Where due_at, the deadline, came from: the user, an email, a page Pisa read, "
        "or Pisa's own estimate; null with no deadline",
    )
    repeat: TaskRepeat | None
    start_at: DateTime | None = Field(
        default=None,
        description="When to do it (the user's time: a block in the plan; a delivery's window start)",
    )
    country: CountryCode | None = Field(
        default=None,
        description="The one country it can be done in; out of the plan elsewhere",
    )
    private: bool = Field(default=False, description="They marked it private (PATCH private)")
    source: TaskSource
    confidence: float | None
    created_at: DateTime
    updated_at: DateTime


class TasksResponse(BaseModel):
    tasks: list[Task]
    emails: list["InboxEmail"] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="priority=low only: the emails that became no task, newest first",
    )


class TaskCreate(BaseModel):
    title: str
    notes: str | None = None
    due_at: str | None = Field(
        default=None, description="RFC 3339. With due_kind 'date', a date (YYYY-MM-DD) is enough."
    )
    due_kind: Literal["none", "date", "datetime"] | None = None
    duration_min: int | None = None
    place_text: str | None = None
    priority: Literal[1, 2, 3] | None = None
    repeat: TaskRepeat | None = None
    place_id: str | None = Field(
        default=None,
        description="a place Pisa knows (from a places card): the task is there; its name is the place text",
    )


class TasksBulk(BaseModel):
    """Today's edit mode: one change for the selected open tasks, with one Undo for the set."""

    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(min_length=1, max_length=500)
    action: Literal["done", "dismiss", "delete"]


class TasksBulkDone(BaseModel):
    bulk_id: UUID
    count: int = Field(description="The open tasks changed (an id that wasn't open is skipped)")
    undo_until: DateTime = Field(description="POST /v1/tasks/bulk/{bulk_id}/undo until then (24 h), once")


class TasksBulkUndone(BaseModel):
    bulk_id: UUID
    count: int = Field(description="The tasks brought back")


class TaskPatch(BaseModel):
    """Any subset of the fields; null clears notes, due_at (and so the due date), place_text and repeat (the
    series stops)."""

    title: str | None = None
    notes: str | None = None
    status: Literal["open", "done", "dismissed"] | None = None
    due_at: str | None = None
    due_kind: Literal["none", "date", "datetime"] | None = None
    duration_min: int | None = None
    place_text: str | None = None
    priority: Literal[1, 2, 3] | None = None
    repeat: TaskRepeat | None = None
    start_at: str | None = Field(
        default=None,
        description="When to DO it (the app's time for a task): a block at that time in the plan, for "
        "its duration; never a deadline (that is due_at). null clears it.",
    )
    country: CountryCode | None = Field(
        default=None,
        description="The one country it can be done in (out of the plan elsewhere); null: "
        "anywhere. Their word holds: a new title never re-reads it.",
    )
    private: bool | None = Field(
        default=None,
        description='They marked it private: the watch shows "Impegno", never its title',
    )


class TaskDismiss(BaseModel):
    model_config = ConfigDict(extra="forbid")

    never: Literal["sender", "kind"] | None = Field(
        default=None,
        description='A task from an email only: also "never from this sender" or "never this kind" '
        "(kept as the user's rule in About you; dismisses the open email tasks it covers)",
    )


class TaskDismissed(BaseModel):
    task: "Task"
    rule: str | None = Field(description="The rule Pisa keeps (its sentence), when `never` was given")
    rule_id: str | None = Field(
        default=None, description="The rule's id when one was made: DELETE /v1/rules/{id} undoes it"
    )
    also_dismissed: list[str] = Field(description="The other open tasks the rule dismissed (their ids)")


class InboxEmail(BaseModel):
    id: str
    subject: str
    sender: str = Field(description="The sender's name, else address")
    sender_address: str | None
    sent_at: DateTime
    kind: str | None = Field(
        description="security, storage, auto_renewal, developer, contact_check (account notices), else the "
        "triage category: what never this kind would cover"
    )
    why: str = Field(description="Why it's not a task, in the user's language")
    detail: str | None = Field(description="The triage's one sentence about it, when there is one")


class InboxResponse(BaseModel):
    emails: list[InboxEmail] = Field(description="Newest first, 50 at most")


class ChangeUndo(BaseModel):
    method: Literal["POST", "PATCH"]
    path: str = Field(description="The REST call that undoes it (the same Undo as everywhere else)")
    body: dict[str, Any] | None = Field(description="Its body (PATCH: the task back open); null for POST")
    until: DateTime | None = Field(description="Undo works until then; null: while it is still so")


class Change(BaseModel):
    """One of Pisa's own changes (GET /v1/changes), with its reason and its Undo while it lasts."""

    id: str = Field(description="Stable: decision:<uuid>, done:<uuid> or bulk:<uuid>")
    kind: Literal["task_dismissed", "task_done", "event_done", "calendar", "plan", "tasks_bulk"]
    at: DateTime
    title: str | None = Field(description="The item's title (the user's own data); null for a set of tasks")
    why: str = Field(description='Why, in Pisa\'s words ("Scaduto e senza risposta da 7 giorni…")')
    by: Literal["owner", "pisa"] = Field(
        description="owner: the user's words or yes; pisa: Pisa's own initiative"
    )
    task_id: str | None = None
    event_id: str | None = None
    approval_id: str | None = None
    undo: ChangeUndo | None = Field(description="null: it can no longer be undone here")


class ChangesResponse(BaseModel):
    changes: list[Change] = Field(description="Newest first, 50 at most")


class EmailItem(BaseModel):
    """One email by id (an `email:<id>` object), as Pisa's views read it; never its body."""

    id: str
    subject: str
    sender: str = Field(description="The sender's name, else address")
    sender_address: str | None
    sent_at: DateTime
    gist: str | None = Field(description="Pisa's one line on what it is, when the triage made one")
    task_id: str | None = Field(description="The task it became, when it became one")
    kind: str | None = Field(description="As in the inbox, when it became no task; else null")
    why: str | None = Field(description="Why it's not a task, when it isn't; else null")
    detail: str | None = Field(description="The triage's one sentence about it, when there is one")


class DraftItem(BaseModel):
    """One of Pisa's drafts by id (a `draft:<id>` object): the whole text the user approves."""

    id: str
    subject: str
    to: list[str]
    cc: list[str]
    outsiders: list[str] = Field(description="Recipients who are not in the thread (the app marks them)")
    body: str
    version: int = Field(description="+1 on each edit; an approval sends the version it showed")
    updated_at: DateTime
    sent_at: DateTime | None
    approval_id: str | None = Field(description="The approval waiting for the user's yes, if any")


class AgendaEvent(BaseModel):
    id: str
    title: str
    start: DateTime
    end: DateTime
    all_day: bool
    location: str | None
    calendar: str
    place_id: str | None = Field(description="The place this event is at, when resolved")


class Event(BaseModel):
    """An event as the plan sees it."""

    id: str
    calendar: str
    title: str
    start: DateTime
    end: DateTime
    all_day: bool
    location: str | None
    place_id: str | None
    kind: Literal["fixed", "window", "soft"] = Field(
        description="fixed: never moved, late after its start; window: arrive any time between its start and "
        "its end minus min_stay_min (a festival); soft: a plan made in Pisa"
    )
    kind_source: Literal["inferred", "owner"]
    min_stay_min: int | None = Field(description="window only: the minimum stay, in minutes")
    attended_at: DateTime | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="The user was there (10 minutes within its time ± 1 h) or said so; absent "
        "otherwise. POST /v1/events/{id}/undo-attended within 24 h",
    )


class EventsResponse(BaseModel):
    timezone: str
    events: list[Event]


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fixed", "window", "soft"] | None = Field(description="null: Pisa infers the kind again")
    min_stay_min: int | None = Field(default=None, ge=0, le=1440, description="window only")


class AgendaDay(BaseModel):
    date: str
    events: list[AgendaEvent]
    tasks_due: list[Task]


class AgendaResponse(BaseModel):
    timezone: str
    days: list[AgendaDay]


class Triggers(BaseModel):
    glasses_on: bool
    app_open: bool
    phrase: bool
    time: bool


class Span(BaseModel):
    start: str = Field(description="HH:MM, the user's time zone")
    end: str = Field(description="HH:MM, the user's time zone; may be earlier than start (crosses midnight)")


class BriefingSettings(BaseModel):
    triggers: Triggers
    phrases: list[str]
    time: str = Field(description="HH:MM, used only when triggers.time is on")
    window: Span | None
    repeat_after_min: int | None
    sections: list[Literal["agenda", "tasks", "email", "weather"]]
    max_words: int


class ReminderSettings(BaseModel):
    event_lead_min: int
    task_lead_min: int


class QuietSettings(BaseModel):
    hours: Span | None
    until: DateTime | None


class PlacesSettings(BaseModel):
    routes: list[Literal["apple", "google"]] = Field(description="Travel times: order of preference")
    search: list[Literal["apple", "google"]] = Field(description="Place search: order of preference")
    details: list[Literal["google"]] = Field(description="Place details (hours, phone, reviews); [] = off")
    web: list[Literal["tavily"]] = Field(description="Web research; [] = off")
    default_mode: Literal["drive", "transit", "walk", "bike"] = Field(
        description="The first of `modes`, as routed (car and taxi are drive); setting it puts that mode "
        "first"
    )
    monthly_cap_eur: float = Field(ge=0, le=100, description="0 = no paid calls; free providers keep working")
    modes: list[Literal["walk", "transit", "bike", "car", "taxi"]] = Field(
        description="The travel modes the user allows, in order of preference (transit includes "
        "ferries). A walk longer than walk_max_min goes by the next allowed mode"
    )
    walk_max_min: int = Field(ge=10, le=180, description="The longest walk Pisa plans (default 45)")


class PlannerSettings(BaseModel):
    day: Span = Field(description="The hours Pisa may place tasks in (may cross midnight)")
    buffer_min: int = Field(ge=0, le=60, description="Slack around fixed events and between legs")
    default_task_min: int = Field(ge=5, le=240, description="A task's duration when it has none")
    leave_now: bool = Field(description='Send "leave now" reminders')
    leave_lead_min: int = Field(ge=0, le=30, description="How early the leave-now reminder comes")
    check_ins: bool = Field(description="Ask about missed and visited items")


class WeatherDay(BaseModel):
    min_c: float
    max_c: float
    precip_mm: float
    precip_prob: int
    wind_kmh: float
    summary: str = Field(description="From the WMO code, in the user's language")


class WeatherHour(BaseModel):
    time: DateTime
    temp_c: float
    precip_prob: int
    summary: str


class WeatherResponse(BaseModel):
    label: str
    date: str
    day: WeatherDay
    hours: list[WeatherHour] = Field(description="The next 24 hours for today, the whole day otherwise")


class VoiceOption(BaseModel):
    id: str
    name: str
    languages: list[locale.Tag]
    preview_text: str = Field(description="A sentence to try it with, in the user's language")


class VoicesResponse(BaseModel):
    voices: list[VoiceOption]


class VoicePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=voice_rules.PREVIEW_MAX)] | None = Field(
        default=None, description="null: a fixed sentence in the user's language"
    )


class VoiceSettings(BaseModel):
    """Pisa's spoken voice. provider null in a PATCH = OpenAI when its key is set, else the phone's."""

    provider: Literal["openai", "elevenlabs", "device"]
    model: str | None = Field(description="null: the provider's default speech model")
    voice: str | None = Field(
        description="The provider's voice id (GET /v1/voice/voices); null: its default (OpenAI marin; "
        "ElevenLabs has none: the phone speaks until one is picked; device: the phone's own)"
    )
    speed: float = Field(ge=0.8, le=1.3)


class ProfileSettings(BaseModel):
    something_different: bool = Field(
        description="About 1 places card in 5 may show one place unlike the user's usual, labelled"
    )


TALK_ROUTES = frozenset({"speaker", "receiver", "airpods", "headphones", "car", "glasses"})


class TalkMcpApps(BaseModel):
    """Contract 2.4: the MCP Apps the phone renders in a talk, the same shape as the live hello's
    `mcpApps`."""

    model_config = ConfigDict(extra="ignore")

    protocolVersions: list[Annotated[str, StringConstraints(max_length=40)]] = Field(max_length=10)
    mimeTypes: list[Annotated[str, StringConstraints(max_length=80)]] = Field(max_length=10)
    bundledLibraries: list[Annotated[str, StringConstraints(max_length=80)]] = Field(
        default_factory=list, max_length=20
    )


class TalkSessionCreate(BaseModel):
    """The phone's WebRTC offer for one conversation. A newer phone's field is
    ignored (its names logged) and an unknown `route` is none: never a 400 that drops the talk to economy."""

    model_config = ConfigDict(extra="ignore")

    sdp_offer: str = Field(min_length=1, max_length=100_000, description="The phone's SDP offer (Opus, one "
                           "data channel labelled \"pisa\")")  # fmt: skip
    trigger: Literal["press", "wake", "intent", "notice", "proactive", "glasses"] = Field(
        description='press: any press (presence, AirPods); wake: only "Hey Pisa"; intent: '
        "App Intents; notice: "
        "Pisa's own start; proactive: a talk.ask_to_open (with ask_id); glasses: the glasses' "
        'capture button or "Hey Meta, start Pisa"'
    )
    lang: locale.Tag
    first_text: str | None = Field(
        default=None, max_length=2000, description="The words said in one breath with the wake phrase, "
        "recognised on the phone: the user's first final words"
    )  # fmt: skip
    route: Literal["speaker", "receiver", "airpods", "headphones", "car", "glasses"] | None = Field(
        default=None,
        description="Where the talk's audio is at the start (talk.route then follows each change)",
    )  # fmt: skip
    ask_id: UUID | None = Field(
        default=None, description="The talk.ask_to_open this talk answers: it starts with Pisa's line"
    )
    mcpApps: TalkMcpApps | None = Field(
        default=None,
        description="Contract 2.4: the MCP Apps this phone renders in the talk (as the live hello's "
        "mcpApps). Without it (or with no version and MIME type Pisa speaks) a drawing comes as a note "
        "card with its title and summary, never as an mcp-apps view",
    )
    purpose: Literal["talk", "enrol"] = Field(
        default="talk",
        description="enrol records their voice's enrolment over the talk's own path (no talker, no "
        "model money; the data channel carries enrol.* messages); `route` is required then",
    )

    @model_validator(mode="before")
    @classmethod
    def _newer_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and (unknown := sorted(str(k) for k in data if k not in cls.model_fields)):
            _log.info("talk_session_fields_ignored", fields=unknown[:20])  # the names only, never values
        return data

    @field_validator("route", mode="before")
    @classmethod
    def _known_route(cls, value: Any) -> Any:
        return value if isinstance(value, str) and value in TALK_ROUTES else None


class IceServer(BaseModel):
    urls: list[str] | str
    username: str | None = None
    credential: str | None = None


class TalkSessionCreated(BaseModel):
    session_id: UUID
    sdp_answer: str = Field(description="Pisa's SDP answer, every ICE candidate included (no trickle)")
    ice_servers: list[IceServer] = Field(
        description="The ICE servers Pisa uses (empty: host candidates only)"
    )


class ConversationVoices(BaseModel):
    xai: str = Field(description="Grok Voice's voice (a built-in id, e.g. eve, or a custom voice id)")
    gemini: str = Field(description="Gemini Live's prebuilt voice (e.g. Aoede, Kore, Puck)")


class ConversationSettings(BaseModel):
    """How Pisa talks. natural: the talker (a live voice model, WebRTC, POST /v1/talk/sessions); economy:
    today's cascade (the phone's recognizer, Pisa's sentences, the per-sentence voice)."""

    mode: Literal["natural", "economy"]
    talker: Literal["xai", "gemini"] = Field(description="Grok Voice (xAI key) or Gemini Live (Google key)")
    voices: ConversationVoices


class GuardianRule(BaseModel):
    """One of their rules over Pisa's interruptions. `always`: it interrupts them, even in a
    busy block; `never`: it waits for the digest. It names a kind, one of their people, or both."""

    mode: Literal["always", "never"]
    kind: (
        Literal[
            "leave", "event", "reminder", "approval", "alert", "check_in", "done", "replan", "briefing",
            "ask", "ritual", "person", "card", "info",
        ]
        | None
    ) = Field(default=None, description="What it is (Pisa's own kinds of notices)")  # fmt: skip
    person_id: UUID | None = Field(default=None, description="One of their people (GET /v1/people)")


class GuardianSettings(BaseModel):
    rules: list[GuardianRule] = Field(
        max_length=50,
        description='Their rules; the ones they say in words ("interrompimi sempre per Marta") '
        "are kept as their claims and apply too",
    )


class Settings(BaseModel):
    language: locale.Tag
    briefing: BriefingSettings
    reminders: ReminderSettings
    quiet: QuietSettings
    places: PlacesSettings
    planner: PlannerSettings
    voice: VoiceSettings
    profile: ProfileSettings
    conversation: ConversationSettings
    guardian: GuardianSettings = Field(
        default_factory=lambda: GuardianSettings(rules=[]),
        description="Their rules over when Pisa interrupts them",
    )
    messages_retention: Literal["forever", "1y", "90d"] = Field(
        description="How long the raw conversation messages are kept (forever, a year, 90 days); "
        "what Pisa learned from them stays. A shorter value needs the phone's signature "
        "(op `messages_retention`) "
        "and applies from the first 03:30 at least a day after the change: "
        "GET /v1/settings/messages_retention/preview says how many messages will go, and when"
    )


class EraseIn(BaseModel):
    """Their deletion by family and age (signed: `device` challenge, op `erase`)."""

    family: EraseFamily = Field(
        description="messages: conversations; places: GPS points, visits, stays, place notes, residency, "
        "trips; mail: Pisa's copies of their email and its logs; money: the records; records: "
        "Pisa's records about them; "
        "memory: Pisa's memory (episodes, hunches, nights); learn: Pisa Learn's sources and "
        "its answers from them"
    )
    before: date = Field(description="Rows older than this day (their zone) go")


class ErasePreview(BaseModel):
    """What a deletion removes (the preview) or removed (the delete): counts only."""

    family: EraseFamily
    before: date
    count: int = Field(
        ge=0,
        description="Rows in all, with their children; a conversation message a live row cites stays",
    )
    tables: dict[str, int] = Field(description="Per table")
    children: dict[str, int] = Field(
        default_factory=dict,
        description="The rows that go with them, per table (cascading children; mail: the gift ideas "
        "Pisa read in those emails)",
    )


class EraseJob(BaseModel):
    """A deletion's state and counts (never content). The signed POST answers it at once (202), `queued`."""

    erase_id: str = Field(description="GET /v1/data/erase/{erase_id} for its state")
    family: EraseFamily
    before: date
    state: Literal["queued", "running", "done", "failed"]
    count: int | None = Field(
        ge=0, description="What qualified when the job first ran, children included (null while queued)"
    )
    erased: int = Field(ge=0, description="Rows gone so far, children included")
    tables: dict[str, int] = Field(description="Per table, gone so far")
    children: dict[str, int] = Field(description="The rows that went with them, per table")
    failure: str | None = Field(description="A failed job's reason: the error's kind, never content")
    created_at: AwareDatetime
    finished_at: AwareDatetime | None


class EraseJobs(BaseModel):
    """Their latest deletions, newest first (states and counts only)."""

    jobs: list[EraseJob]


class MessagesRetentionPreview(BaseModel):
    """What a `messages_retention` value would remove, before they pick it."""

    value: Literal["forever", "1y", "90d"]
    messages: int = Field(
        ge=0,
        description="How many messages are older than the value today and would go (a message a live topic, "
        "open unknown or live idea's problem cites stays)",
    )
    applies_after: DateTime = Field(
        description="The nightly prune that first deletes under this value: the first 03:30 (Pisa's zone) at "
        "least a day after the change"
    )


class RiepilogoItem(BaseModel):
    """One thing the current plan still holds for today (an event or a task), or an open task it couldn't
    place."""

    kind: Literal["event", "task"]
    title: str
    start: DateTime | None
    task_id: str | None = None
    event_id: str | None = None


class RiepilogoView(BaseModel):
    """The riepilogo as a view of the current plan, derived on every read by code (no model call)."""

    summary: str | None = Field(
        description="The morning's summary line: its tone, naming none of the day's items"
    )
    items: list[RiepilogoItem] = Field(description="What the current plan still holds for today, in order")
    also: list[RiepilogoItem] = Field(description="Open tasks the plan couldn't place")
    updated_at: DateTime


class Briefing(BaseModel):
    id: str
    day: Annotated[str, Field(json_schema_extra={"format": "date"}, description="The briefing day")]
    trigger: Literal["glasses_on", "app_open", "phrase", "time", "ask"]
    text: str = Field(
        description="GET /v1/briefings/latest, today's: the riepilogo, the morning's summary line and what "
        "is left today (from `view`); otherwise the briefing as composed"
    )
    lang: locale.Tag
    created_at: DateTime
    morning: str | None = Field(default=None, description="The briefing as composed (GET latest only)")
    view: RiepilogoView | None = Field(
        default=None, description="Today's riepilogo as a view of the current plan (GET latest only)"
    )


class P8Key(BaseModel):
    """An Apple .p8 key pasted in the app: its text and its key id."""

    key: Annotated[str, Field(min_length=1, max_length=1000)]
    key_id: Annotated[str, Field(min_length=1, max_length=20)]


class PushKeyStatus(BaseModel):
    configured: bool
    key_id: str | None
    set_at: DateTime | None


class DeviceInfo(BaseModel):
    id: str
    name: str
    platform: str
    created_at: DateTime
    last_seen_at: DateTime | None
    revoked_at: DateTime | None
    this_device: bool


class DevicesResponse(BaseModel):
    devices: list[DeviceInfo]


Base64Url = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=200)]


class PushRelay(BaseModel):
    """Contract 2: notices go through the push relay, sealed to the device (CONTRACT-2 §9)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["relay"]
    handle: Annotated[str, Field(min_length=1, max_length=64)] = Field(
        description="The relay's opaque handle"
    )
    secret: Base64Url = Field(description="The secret the relay shares with this device: 32 bytes, base64url")
    public_key: Base64Url = Field(description="The device's X25519 public key: 32 bytes raw, base64url")


class PushDirect(BaseModel):
    """Contract 2: notices go to APNs from this server, with the device's token."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["direct"]
    token: Annotated[str, Field(min_length=1, max_length=400, description="The APNs device token, in hex")]
    environment: Literal["production", "sandbox"] = "production"


class PushNone(BaseModel):
    """Contract 2: no notices pushed to this device."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none"]


PushMode = Annotated[PushRelay | PushDirect | PushNone, Field(discriminator="mode")]


class PushState(BaseModel):
    mode: Literal["relay", "direct", "none"]
    since: DateTime | None = Field(description="When this mode was set; null when it never was")


# --- contract 2's privacy revision (CONTRACT-2 §3.4, §4.3-§4.5) ------------------------------------------

B64 = Annotated[str, Field(pattern=r"^[A-Za-z0-9+/=_-]{8,400}$")]


class QrPairRequest(BaseModel):
    """`$defs/PairRequest`: sent over the channel pinned to the QR's `spki`."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    pairingId: Annotated[str, Field(min_length=1, max_length=80)] = Field(
        description="From POST /v1/pair/begin: the server committed to its nonce under it"
    )
    deviceKey: B64 = Field(description="The device key: P-256, SubjectPublicKeyInfo DER, base64")
    name: Annotated[str, Field(min_length=1, max_length=60)]
    platform: Literal["ios", "watchos", "android", "web", "cli", "other"]
    clientNonce: B64
    mac: B64 = Field(
        description="HMAC-SHA256 under the token over 'pisa-pair/1' | spki | deviceKey | clientNonce"
    )


class QrPairResponse(BaseModel):
    """`$defs/PairResponse`."""

    model_config = ConfigDict(extra="forbid")

    pairingId: str
    deviceId: str
    serverNonce: str
    mac: str = Field(description="HMAC-SHA256 under the token over the transcript and serverNonce")
    state: Literal["awaiting_confirmation", "confirmed", "refused", "expired"]


class PairBegin(BaseModel):
    """`$defs/PairBegin`: the server's commitment to its nonce, made before it sees the app's transcript."""

    model_config = ConfigDict(extra="forbid")

    pairingId: str
    commitment: str = Field(description="base64url of SHA-256('pisa-sas-commit/1\\n' || serverNonce)")
    expiresAt: DateTime


class PairingOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=60)] | None = Field(
        default=None, description="A label for the new device"
    )

    @model_validator(mode="before")
    @classmethod
    def _no_signer(cls, data: Any) -> Any:
        """CONTRACT-2 §4.3.1: there is no `signer` field. A key that signs device ops is host-only (`pisa pair
        --signer` on the server): a body carrying `signer`, whatever its value, is 400
        `validation_failed`."""
        if isinstance(data, dict) and "signer" in data:
            raise PisaError(
                "validation_failed",
                "A device paired from a phone cannot sign device ops: a signer is made only on the server.",
                "Leave signer out, or pair this device with `pisa pair <name> --signer` on the server",
            )
        return data


class NewDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    platform: str


class OfferTranscript(BaseModel):
    """What the offering phone needs to check the commitment and compute the SAS itself."""

    model_config = ConfigDict(extra="forbid")

    spki: str
    deviceKey: str
    clientNonce: str
    commitment: str
    serverNonce: str


def _none(value: object) -> bool:
    return value is None


class PairingOffer(BaseModel):
    """A paired phone's offer to pair a new device (CONTRACT-2 §4.3.1, proposed)."""

    model_config = ConfigDict(extra="forbid")

    offerId: str
    state: Literal["open", "awaiting_confirmation", "confirmed", "refused", "expired"]
    qr: str | None = Field(default=None, exclude_if=_none, description="The pisa://pair link (when made)")
    token: str | None = Field(default=None, exclude_if=_none, description="The typed fallback (when made)")
    expiresAt: DateTime
    pairingId: str | None = Field(default=None, exclude_if=_none)
    deviceId: str | None = Field(default=None, exclude_if=_none, description="When confirmed")
    newDevice: NewDevice | None = Field(default=None, exclude_if=_none)
    transcript: OfferTranscript | None = Field(
        default=None, exclude_if=_none, description="awaiting_confirmation only"
    )


class PairingConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairingId: str
    sas: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class SessionNonce(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(description="32 random bytes, base64url; single use, 60 s")
    expires_at: DateTime


class SessionRequest(BaseModel):
    """`$defs/SessionRequest`: the device key signs 'pisa-session/1\\n<deviceId>\\n<nonce>'."""

    model_config = ConfigDict(extra="forbid")

    deviceId: str
    nonce: B64
    signature: B64 = Field(description="ECDSA P-256 SHA-256, DER, base64url")


class SessionToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(description="The bearer for REST and /v2/live: 32 random bytes, base64url")
    expires_at: DateTime


class Endpoint(BaseModel):
    kind: Literal["lan", "ipv6", "ipv4", "host", "relay"]
    address: str
    port: int | None = Field(default=None, exclude_if=lambda v: v is None)
    relayHandle: str | None = Field(default=None, exclude_if=lambda v: v is None)


class Endpoints(BaseModel):
    """`$defs/s_pisa_endpoints`: the same value as the `pisa.endpoints` event."""

    model_config = ConfigDict(extra="forbid")

    revision: str
    endpoints: list[Endpoint]
    spki: list[str] = Field(
        description="The current SPKI pin, then the next one when a rotation is announced"
    )


class AIProvider(BaseModel):
    id: str
    name: str
    kind: Literal["cloud", "self_hosted", "on_server"]
    region: str | None = Field(default=None, exclude_if=lambda v: v is None)


class AIProcessor(BaseModel):
    slot: str
    provider: AIProvider
    model: str
    dataCategories: list[str]
    features: list[str] | None = Field(default=None, exclude_if=lambda v: v is None)


class AIDisclosure(BaseModel):
    """`$defs/AIDisclosure`."""

    model_config = ConfigDict(extra="forbid")

    revision: str
    processors: list[AIProcessor]
    healthRequiresSeparateConsent: Literal[True]


class AIConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{16,64}$")]
    health: bool


# --- locations ------------------------------------------------------------------------

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class LocationFix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fix"]
    lat: Latitude
    lon: Longitude
    accuracy_m: Annotated[float, Field(ge=0)]
    measured_at: AwareDatetime


class LocationVisit(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a visit has no measured_at: its arrival counts

    kind: Literal["visit"]
    lat: Latitude
    lon: Longitude
    accuracy_m: Annotated[float, Field(ge=0)]
    arrived_at: AwareDatetime
    departed_at: AwareDatetime | None = None


class LiveActivityToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(description="The ActivityKit push token, hex")
    kind: Literal["update", "start"] = Field(
        description="update: the running activity's; start: push-to-start"
    )


class DiagnosticsUpload(BaseModel):
    """Contract X: one MetricKit diagnostic payload (`MXDiagnosticPayload.jsonRepresentation()`)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["crash", "hang", "cpu", "disk"]
    app_version: Annotated[str, Field(min_length=1, max_length=40, description='e.g. "0.1.0 (5)"')]
    os_version: Annotated[str, Field(min_length=1, max_length=40, description='e.g. "26.3"')]
    payload: dict[str, Any] = Field(description="≤ 1 MB; only call stacks and exception types are kept")


Seconds = Annotated[float, Field(ge=0, le=7 * 86_400)]
Kilobytes = Annotated[float, Field(ge=0, le=1e9)]


class LocationSeconds(BaseModel):
    """MetricKit's cumulative location time by accuracy (seconds)."""

    model_config = ConfigDict(extra="ignore")

    best: Seconds | None = None
    ten_m: Seconds | None = None
    hundred_m: Seconds | None = None
    km: Seconds | None = None
    three_km: Seconds | None = None


class CompanionSession(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: AwareDatetime  # a time, never text
    minutes: Annotated[float, Field(ge=0, le=1440)]
    battery_pct_per_h: Annotated[float, Field(ge=0, le=100)] | None = None


class DayMetrics(BaseModel):
    """One day of the phone's own numbers (MetricKit's `MXMetricPayload`, mapped by the app). Numbers only:
    any other key is dropped."""

    model_config = ConfigDict(extra="ignore")

    day: date
    foreground_s: Seconds | None = None
    background_audio_s: Seconds | None = None
    background_location_s: Seconds | None = None
    location_s: LocationSeconds | None = None
    cpu_s: Seconds | None = None
    gpu_s: Seconds | None = None
    cell_up_kb: Kilobytes | None = None
    cell_down_kb: Kilobytes | None = None
    wifi_up_kb: Kilobytes | None = None
    wifi_down_kb: Kilobytes | None = None
    companion: Annotated[list[CompanionSession], Field(max_length=48)] = Field(default_factory=list)


class MetricsUpload(BaseModel):
    """The battery numbers: one a day per device (a second upload for a day replaces it), kept 90
    days."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["metrics"]
    app_version: Annotated[str, Field(min_length=1, max_length=40)]
    os_version: Annotated[str, Field(min_length=1, max_length=40)]
    payload: DayMetrics


class AppEvent(BaseModel):
    """One app event (shared contract): allowlisted fields only; anything else is dropped."""

    event: Literal[
        "companion_refused",
        "companion_retry",
        "companion_recovered",
        "companion_stopped",
        "notification_dismissed",
        "notification_opened",
        "call_started",
        "call_ended",
        "activity",
        "focus_on",
        "focus_off",
        "microphone_mode",
    ]
    code: str | None = Field(default=None, description='e.g. "!int"; at most 64 characters are kept')
    site: str | None = Field(
        default=None,
        description="the function name; CallKit, motion.<previous>, Focus, push for the guardian's; the "
        "route for microphone_mode; at most 64 characters are kept",
    )
    state: str | None = Field(
        default=None,
        description="the companion state; on/off, automotive/walking/stationary, opened for the guardian's; "
        "the microphone mode for microphone_mode; at most 64 characters are kept",
    )
    background: bool | None = None
    at: DateTime
    notification_id: UUID | None = Field(
        default=None,
        description="notification_dismissed / notification_opened: the notice the user dismissed "
        "or opened; required there",
    )


class AppEventsUpload(BaseModel):
    """App events on the diagnostics upload: kept until they delete them, a day's cap per device and family
    (companion 50, notifications 50, the guardian's breakpoints 100, the microphone mode 20; more are dropped,
    and so is one dated after now), never text the user said or Pisa's replies."""

    appEvents: list[AppEvent] = Field(max_length=200)


class LocationsUpload(BaseModel):
    locations: Annotated[
        list[Annotated[LocationFix | LocationVisit, Field(discriminator="kind")]],
        Field(min_length=1, max_length=200),
    ]


# --- places ---------------------------------------------------------------------


class PlaceTest(BaseModel):
    ok: bool | None
    at: DateTime
    message: str | None


class PlaceProvider(BaseModel):
    id: Literal["apple", "google", "tavily", "osm", "openrouteservice"]
    name: str
    configured: bool
    key_last4: str | None
    capabilities: list[Literal["routes", "search", "details", "web"]]
    last_test: PlaceTest | None


class PlaceProvidersResponse(BaseModel):
    providers: list[PlaceProvider]


class PlaceKey(BaseModel):
    key: Annotated[str, Field(min_length=1, max_length=1000)]  # apple: the .p8 text
    key_id: Annotated[str, Field(min_length=1, max_length=20)] | None = None  # apple only


class PlaceTestResult(BaseModel):
    ok: bool
    message: str


class PlaceUsageItem(BaseModel):
    provider: str
    sku: str
    calls: int
    free_calls: int = Field(description="This month's calls that were free (≤ calls)")
    free_allowance: int | None = Field(description="The SKU's free calls per month; null = no paid tier")
    unit_eur: float
    cost_eur: float


class PlaceUsage(BaseModel):
    month: str
    cap_eur: float
    spent_eur: float
    items: list[PlaceUsageItem]


class PlaceSummary(BaseModel):
    id: str
    name: str
    category: str | None
    address: str | None
    lat: float | None
    lon: float | None
    distance_m: int | None
    open_now: bool | None
    rating: float | None
    rating_count: int | None
    attribution: str | None = Field(
        default=None,
        description="The credit its data's licence asks to be shown with it "
        '("© OpenStreetMap contributors"); null when none',
    )


class PlaceSearchResponse(BaseModel):
    results: list[PlaceSummary]


class OpeningPeriod(BaseModel):
    day: int = Field(ge=1, le=7, description="1 = Monday … 7 = Sunday")
    open: str
    close: str


class PlaceHours(BaseModel):
    weekly: list[OpeningPeriod]
    open_now: bool | None
    next_change: DateTime | None
    note: str | None


class PlaceReview(BaseModel):
    author: str | None
    rating: float | None
    text: str
    published_at: str | None
    source: str


class InsightSource(BaseModel):
    title: str
    url: str


class PlaceInsights(BaseModel):
    text: str
    sources: list[InsightSource]
    fetched_at: DateTime


class PlaceTravel(BaseModel):
    from_: Literal["here"] = Field(alias="from")
    mode: Literal["drive", "transit", "walk", "bike"]
    minutes: int
    traffic_minutes: int | None
    provider: str
    at: DateTime


class PlaceLinks(BaseModel):
    apple_maps: str
    google_maps: str


class PlaceRelated(BaseModel):
    kind: Literal["event", "task"]
    id: str
    title: str
    when: DateTime | None


class PlaceSources(BaseModel):
    search: str | None
    details: str | None
    fetched_at: DateTime
    skipped: list[str] = Field(description="Providers skipped for this answer, and why")


class Place(BaseModel):
    id: str
    apple_id: str | None = Field(description="Apple Maps place id, for MapKit's own place card")
    name: str
    category: str | None
    address: str | None
    lat: float | None
    lon: float | None
    phone: str | None
    website: str | None
    hours: PlaceHours | None
    rating: float | None
    rating_count: int | None
    price_level: int | None
    summary: str | None
    attributes: dict[str, bool] = Field(
        default_factory=dict,
        description='What the place offers, as Google says it ("outdoorSeating": true); empty when unknown',
    )
    reviews: list[PlaceReview]
    insights: PlaceInsights | None
    travel: PlaceTravel | None
    links: PlaceLinks
    related: list[PlaceRelated]
    attribution: str | None = Field(
        default=None,
        description="The credit shown with the place when OpenStreetMap's data named it (\"© OpenStreetMap "
        'contributors", ODbL); null when none',
    )
    sources: PlaceSources


class ResearchRequest(BaseModel):
    question: Annotated[str, Field(max_length=300)] | None = None


class TravelResponse(BaseModel):
    minutes: int
    traffic_minutes: int | None
    distance_m: int | None
    mode: Literal["drive", "transit", "walk", "bike"]
    provider: str


# --- the plan --------------------------------------------------------------------------------


class PlanFrom(BaseModel):
    lat: float
    lon: float
    label: str = Field(description='"here", or the place of the first fixed event when no fresh location')


class PlanOverlap(BaseModel):
    event_id: str
    title: str


class PlanBand(BaseModel):
    lo_min: int = Field(description="their 20th percentile, in minutes")
    hi_min: int = Field(description="their 80th percentile, in minutes")


class PlanItem(BaseModel):
    kind: Literal["event", "travel", "task", "free"]
    private: bool | None = Field(
        default=None,
        description="A calendar event marked private or confidential, or a task they marked "
        'private: the watch shows "Impegno" and never speaks it',
    )
    sensitive: bool | None = Field(
        default=None,
        description="At a sensitive place (health, worship, politics): the watch shows "
        '"Impegno" and never speaks it',
    )
    start: DateTime
    end: DateTime
    title: str | None = None
    event_id: str | None = None
    event_ids: list[str] | None = Field(
        default=None,
        description="event: its one calendar event's id (overlapping events are separate items; the list "
        "stays for older apps)",
    )
    overlaps: list["PlanOverlap"] | None = Field(
        default=None,
        description="event: the other events this one overlaps, with their ids and titles; omitted when "
        "none (a run inside a grocery wait: the wait is overlapped on purpose)",
    )
    task_id: str | None = None
    place_id: str | None = None
    why: str | None = Field(default=None, description="task: one deterministic sentence, no model")
    mode: Literal["drive", "transit", "walk", "bike", "taxi"] | None = Field(
        default=None, description='travel: "drive" is by car (settings places.modes "car")'
    )
    minutes: int | None = None
    provider: str | None = None
    to_place_id: str | None = None
    leave_at: DateTime | None = None
    rain: bool | None = Field(
        default=None, description="travel: true when the rain rule switched this walk to transit"
    )
    status: Literal["planned", "ongoing", "done", "visited", "missed", "skipped"] | None = Field(
        default=None, description="event and task: what really happened, from where the user was"
    )
    adjusted: bool | None = Field(
        default=None, description="event and task: true when an adjustment moved or changed it"
    )
    event_kind: Literal["fixed", "window", "soft"] | None = Field(
        default=None,
        description="event: fixed (never moved), window (arrive any time between its start and its "
        "end minus min_stay_min), soft (a plan made in Pisa)",
    )
    kind_source: Literal["inferred", "owner"] | None = Field(
        default=None, description="event: Pisa inferred the kind, or the user set it (PATCH /v1/events/{id})"
    )
    min_stay_min: int | None = Field(default=None, description="event, window only: the minimum stay")
    arrive_at: DateTime | None = Field(
        default=None,
        description="event, window only: when the plan has the user arrive, when later than its start "
        "(the item then starts there)",
    )
    moved_from: DateTime | None = Field(
        default=None,
        description="event, soft only: Pisa moved it within its day so a stop fits; its start in the "
        "calendar, which keeps it (the item's `why` says so)",
    )
    scheduled_by: Literal["owner", "email"] | None = Field(
        default=None,
        description="task: who gave it this time: the user (their time, or Plan it) or an email's "
        "window; Pisa never times a task by itself",
    )
    band: PlanBand | None = Field(
        default=None,
        description="task/travel: their usual spread around the minutes Pisa planned, from the "
        "night's planned-against-actual score; absent until five items of the kind were scored",
    )


class Unplanned(BaseModel):
    task_id: str
    title: str
    reason: str


class TodoItem(BaseModel):
    """An open task that matters today, with no time (Pisa never gives one by itself)."""

    task_id: str
    title: str
    due_at: DateTime | None = Field(default=None, description="its deadline, when it has one")
    deadline_source: Literal["owner", "email", "web", "pisa"] | None = Field(
        default=None, description="where the deadline came from (with due_at)"
    )
    priority: Literal[1, 2, 3]
    why: str | None = Field(default=None, description="why it matters, in one line (Pisa's)")


class PlanSources(BaseModel):
    routes: str | None
    skipped: list[str]


class Plan(BaseModel):
    date: str
    generated_at: DateTime
    from_: PlanFrom | None = Field(alias="from")
    items: list[PlanItem]
    unplanned: list[Unplanned]
    todo: list[TodoItem] = Field(
        default_factory=list,
        description="The few open tasks that matter today, by deadline then priority (no time)",
    )
    sources: PlanSources


# --- plan options and adjustments -------------------------------------------------------


class PlanTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    task_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _one(self) -> "PlanTarget":
        if (self.task_id is None) == (self.event_id is None):
            raise ValueError("target needs exactly one of event_id and task_id")
        return self


class PlanOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PlanTarget | None = None
    goal: Literal["fit", "alternative", "reorganize"] = "fit"


class PlanInsert(BaseModel):
    """ "Plan it" with an intent: a task, or a place (made a task), before / after / instead of an event,
    or when it fits."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    place_id: str | None = None
    when: Literal["before", "after", "instead", "fits"]
    relative_to: str | None = Field(
        default=None, description="An event id; null: the next event of the day (ignored for fits)"
    )
    duration_min: int | None = Field(default=None, ge=5, le=480)


class PlanChange(BaseModel):
    action: ChangeAction
    event_id: str | None = None
    task_id: str | None = None
    start: DateTime | None = None
    end: DateTime | None = None
    place_id: str | None = None
    duration_min: int | None = None


class PlanEffects(BaseModel):
    late_min: int
    extra_travel_min: int
    dropped: list[str]


class PlanOption(BaseModel):
    id: str
    summary: str
    why: str
    changes: list[PlanChange]
    effects: PlanEffects
    plan: Plan


class PlanOptions(BaseModel):
    options_id: str
    expires_at: DateTime
    target_title: str
    options: list[PlanOption]


class ApplyOption(BaseModel):
    option_id: Annotated[str, Field(min_length=1, max_length=8)]


class Adjustment(BaseModel):
    id: str
    date: str
    action: ChangeAction
    event_id: str | None = None
    task_id: str | None = None
    title: str
    start: DateTime | None
    end: DateTime | None
    place_id: str | None
    duration_min: int | None
    created_at: DateTime
    source: Literal["voice", "app", "check_in"]
    option_ref: str = Field(
        description="Shared by the changes of one option: undoing one of them undoes them all"
    )


class ManualAdjustment(BaseModel):
    """Contract Q: a change from the app, without options. move needs start; a task keeps its duration, an
    event its length."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    event_id: str | None = None
    action: Literal["move", "skip"]
    start: DateTime | None = None


class AdjustmentsResponse(BaseModel):
    adjustments: list[Adjustment]


# --- check-ins and the day so far -------------------------------------------------------


class CheckInChoice(BaseModel):
    id: Annotated[str, Field(pattern="^[a-z_]{1,32}$")]
    label: Annotated[str, Field(max_length=40)]


class CheckIn(BaseModel):
    id: str
    date: str
    kind: Literal["missed", "confirm"]
    event_id: str | None = None
    task_id: str | None = None
    title: str
    text: str
    choices: list[CheckInChoice]
    created_at: DateTime
    answered_at: DateTime | None
    answer: str | None


class CheckInsResponse(BaseModel):
    check_ins: list[CheckIn]


class CheckInAnswer(BaseModel):
    choice: Annotated[str, Field(min_length=1, max_length=32)]


class CheckInAnswered(BaseModel):
    check_in: CheckIn
    plan: Plan | None
    options: PlanOptions | None


class DayStay(BaseModel):
    arrived_at: DateTime
    departed_at: DateTime | None
    lat: float
    lon: float
    place_id: str | None
    name: str | None


class DayItem(BaseModel):
    kind: Literal["event", "task"]
    event_id: str | None = None
    task_id: str | None = None
    title: str
    status: Literal["planned", "ongoing", "done", "visited", "missed", "skipped"]


class Day(BaseModel):
    date: str
    stays: list[DayStay]
    items: list[DayItem]


# --- approvals ---------------------------------------------------------------


class Approval(BaseModel):
    id: str
    kind: approvals.Kind
    summary: str
    details: list[str] = Field(description="At most 8 lines: what, when, where, who is notified")
    created_at: DateTime
    expires_at: DateTime = Field(description="10 minutes after creation")
    status: Literal["pending", "approved", "rejected", "expired", "done", "failed"] = Field(
        description="expired also for an email's question replaced by the read-back of a newer version "
        "(B6; `result` says so)"
    )
    result: str | None = Field(description="done: what was done; failed: the cause")
    undo_until: DateTime | None = Field(description="Calendar kinds: POST …/undo works until then (24 h)")
    requires_biometric: bool = Field(description="Approve only with auth=biometric (Face ID)")
    origin: Literal["words", "proposal", "held"] = Field(
        description="words = done at once on the user's own words (no question); proposal = Pisa's "
        "proposal, one yes (other attendees, or an option chosen); held = not in the user's words or after "
        "outside text, one yes"
    )
    why: "ApprovalWhy | None" = Field(
        default=None,
        description='Why Pisa did or asked it, for the Changes screen\'s "perché"; null when '
        "it wasn't Pisa's move in a conversation (a tap on a card) or for changes made before",
    )
    tier: Literal[0, 1, 2] = Field(
        default=1,
        description="0 information; 1 send, book, contact (one yes); 2 money or Pisa's own code "
        "(Face ID, signed by the phone's key)",
    )
    verified: Literal["verified", "not_verified"] | None = Field(
        default=None,
        description="After it ran, whether the provider's own state confirmed it (read back); "
        "null before it ran",
    )


class ApprovalWhy(BaseModel):
    purpose: str | None = Field(
        description="Pisa's one line, in the user's language: what the user gets (null when Pisa gave none)"
    )
    provenance: Literal["owner_words", "pisa_proposal", "outside_text", "pisa_initiative"] = Field(
        description="Set by code, not by the model: owner_words = from the user's own words; pisa_proposal = "
        "Pisa's idea, offered for a yes; outside_text = the turn had read an email, a page or an invite; "
        "pisa_initiative = Pisa did it unasked (reversible, the user's own things only)"
    )


class ApprovalDetail(Approval):
    preview: str | None = Field(
        description="email_send: the whole text that will be sent (the details show its start); "
        "null otherwise"
    )
    card: "YesCard" = Field(description="The phone's card, the same facts Pisa reads out")
    payload: dict[str, Any] = Field(
        description="Contract 2: the action exactly as the server will run it, as hashed by "
        "`pisa-approval-digest/2` (an email's draft_id, version, to, cc, subject, body and attachments' "
        "signs; a calendar change's fields)"
    )
    recipients: list[str] = Field(
        description="Contract 2: every address it writes to or notifies, exactly as hashed (lowercased, "
        "trimmed, once each, sorted)"
    )


class RunAccepted(BaseModel):
    """The run goes on past 8 s: its events reach the device on /v2/live; a headless client reads what it
    changed over REST."""

    runId: str = Field(description="The run still going")


class CollectedRun(BaseModel):
    """A run collected into one document: the JSON mode of the AG-UI HTTP binding (CONTRACT-2 §3.2). The
    run's id is in its RUN_STARTED."""

    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]] = Field(
        description="Plain AG-UI 1.0 events, RUN_STARTED to RUN_FINISHED or RUN_ERROR"
    )


class YesCard(BaseModel):
    """The one card the phone shows for an outward act (who, what, when, how much, what can't be
    undone), the hash its yes is bound to, its tier and, after it ran, the read-back."""

    what: str
    who: str | None = Field(
        description="Whom it reaches (an email's recipients; who a calendar change tells)"
    )
    when: str | None
    cost: str | None = Field(description="The amount as stated, with its currency; null when nothing is paid")
    reversible: bool
    irreversible_line: str | None = Field(description="What can't be undone, in the user's language")
    data_out: list[str] = Field(description="Every address it writes to or notifies, lower case, sorted")
    details: list[str]
    tier: Literal[0, 1, 2]
    face_id: bool = Field(description="Tier 2: approved only with the signature")
    action_hash: str = Field(description="64 lowercase hex: the digest; the yes is bound to it")
    verified: Literal["verified", "not_verified"] | None


class ApprovalsResponse(BaseModel):
    approvals: list[Approval]


# --- the user's profile -------------------------------------------------------------------


class ProfileEvidence(BaseModel):
    summary: str
    at: DateTime


class ProfileScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: Literal["weekday", "weekend"] | None = None
    company: Literal["alone", "others"] | None = None
    where: Literal["home", "travel"] | None = None
    time: Literal["morning", "afternoon", "evening", "night"] | None = None


class ProfileClaim(BaseModel):
    id: str
    kind: Literal[
        "like", "dislike", "routine", "constraint", "goal", "style", "mobility", "source", "sensitivity"
    ]
    statement: str
    scope: dict[str, str | None] = Field(
        description="day (weekday/weekend), company (alone/others), where (home/travel), time "
        "(morning/afternoon/evening/night): all four keys, null means any"
    )
    provenance: Literal["said", "observed", "inferred", "confirmed"]
    confidence: float = Field(description="0 to 1: support / (support + against + 2); confirmed ≥ 0.8")
    support: float = Field(
        description="the evidence for it, decayed (what was said counts 1, a hard limit 3)"
    )
    evidence_count: int = Field(description='how many pieces of evidence ("seen N times")')
    state: Literal["candidate", "active", "asking", "retired"] = Field(
        description="candidate: not used yet; active: used; asking: in the weekly check; retired: not true"
    )
    buffer_min: int | None = Field(
        None,
        description="lateness claims: how many minutes earlier Pisa has you leave for that kind of event "
        "(you said yes in the weekly check); null: none. PATCH buffer_off turns it off",
    )
    sensitive: bool
    hard: bool = Field(description="a limit Pisa applies as a rule")
    evidence: list[ProfileEvidence] = Field(description="why Pisa thinks so, newest first")
    updated_at: DateTime
    created_at: DateTime | None = Field(
        default=None, description="When Pisa saved it (with provenance and evidence: where it came from)"
    )


class WordRule(BaseModel):
    """One of their interruption rules they said in words (`rule_from_words`), with its
    claim; forgetting it is DELETE /v1/profile/claims/{claim_id}."""

    claim_id: UUID
    mode: Literal["always", "never"]
    kind: str | None = Field(default=None, description="the gate's kind it names, if any")
    person_id: UUID | None = Field(default=None, description="one of their people it names, if any")


class ProfileRules(BaseModel):
    rules: list[ProfileClaim] = Field(
        description="Pisa's own rules (hard limits, email rules), each with its source and date"
    )
    interruptions: list[WordRule] = Field(
        default_factory=list,
        description="The interruption rules they said in words (Settings' own are in guardian.rules)",
    )


class Travelling(BaseModel):
    since: str = Field(description="the first night away (YYYY-MM-DD)")
    city: str | None


class SpeechHints(BaseModel):
    hints: list[Annotated[str, StringConstraints(max_length=80)]] = Field(
        max_length=100, description="the recogniser's contextual strings, best first"
    )
    updated_at: DateTime = Field(description="when the names were last gathered from Pisa's tables")


class HomePlace(BaseModel):
    name: str
    lat: float
    lon: float


class Home(BaseModel):
    place: HomePlace | None = Field(description="null: Pisa doesn't know where the user lives")
    source: Literal["owner", "learned"] | None = Field(
        description="owner: the user set it; learned: Pisa's guess from where the phone sleeps (21+ nights)"
    )
    confirmed: bool = Field(description='false: show it as a guess ("I think you live in …")')


class HomeSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str | None = Field(default=None, max_length=64, description="a place Pisa knows")
    query: str | None = Field(default=None, min_length=2, max_length=200, description="the user's words")

    @model_validator(mode="after")
    def _one(self) -> "HomeSet":
        if (self.place_id is None) == (self.query is None):
            raise ValueError("Give exactly one of place_id or query.")
        return self


class Profile(BaseModel):
    claims: list[ProfileClaim]
    off_limits: list[str] = Field(description="topics Pisa never learns")
    paused_until: DateTime | None
    first_talk: Literal["not_started", "in_progress", "done"]
    travelling: Travelling | None = Field(description="while the user sleeps more than 50 km from home")
    home: Home


class WeeklyItem(BaseModel):
    id: str
    kind: Literal["confirm", "changed", "new", "expiring", "buffer"] = Field(
        description="buffer: answer yes or no; the others: right, wrong or forget"
    )
    statement: str = Field(description="the claim, or for a buffer the question")
    why: str = Field(description="the evidence in one line")
    evidence: list[ProfileEvidence]


class Weekly(BaseModel):
    week: str = Field(description="the Sunday it is offered from, 18:00 in the user's time zone (YYYY-MM-DD)")
    items: list[WeeklyItem] = Field(description="at most 3; empty most weeks is fine")


class WeeklyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["right", "wrong", "forget", "yes", "no"]


class WeeklyAnswered(BaseModel):
    answer: str


class ProfileClaimPatch(BaseModel):
    """The user's correction: provenance becomes confirmed."""

    model_config = ConfigDict(extra="forbid")

    statement: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    scope: ProfileScope | None = None
    hard: bool | None = None
    buffer_off: Literal[True] | None = Field(
        None,
        description="stop leaving earlier for this kind of event (a lateness claim with buffer_min): the "
        "claim stays, and the buffer isn't offered again",
    )


class ProfilePause(BaseModel):
    until: AwareDatetime


class OffLimits(BaseModel):
    topic: Annotated[str, Field(min_length=1, max_length=80)]


class FirstTalkStarted(BaseModel):
    first_talk: Literal["in_progress"]
    text: str = Field(description="Pisa's first line, also sent as a notify that expects a reply")


class PlaceOutcome(BaseModel):
    """What the user did with a places card."""

    card_id: str
    place_id: str
    action: Literal["go", "plan", "call", "website", "details", "dismissed"]


# --- the detail sheet ------------------------------------------------------------------------------


class Attribution(BaseModel):
    name: str | None = Field(description="who took the photo; show it with the photo")
    uri: str | None


class PlaceDetailsReview(BaseModel):
    author: str | None
    rating: float | None
    relative_time: str | None = Field(description='the provider\'s words, e.g. "2 settimane fa"')
    text: str = Field(description="at most 300 characters")
    author_uri: str | None = Field(default=None, description="the author's profile: link the name to it")


class ReviewSummary(BaseModel):
    text: str
    source: Literal["google", "pisa"] = Field(
        description="google: Google's own summary; pisa: Pisa's research"
    )
    sources: list[dict[str, Any]] = Field(description="Pisa's sources (title, url) when source is pisa")
    disclosure: str | None = Field(
        default=None, description='google: show it with the text ("Summarized with Gemini")'
    )
    flag_uri: str | None = Field(default=None, description="google: the link to report the summary")
    uri: str | None = Field(default=None, description="google: the reviews on Google Maps")


class PlaceDescription(BaseModel):
    """Google's AI description of the place (generativeSummary): about the place, not its reviews."""

    text: str
    disclosure: str = Field(description='show it with the text ("Summarized with Gemini")')
    flag_uri: str | None = Field(description="the link to report the description")


class PlaceMenu(BaseModel):
    url: str
    highlights: list[str] = Field(description="up to 6 dishes Pisa read on the page")
    source: str = Field(description="the page Pisa read")


class PlacePhotoRef(BaseModel):
    index: int
    attribution: Attribution


class PlaceDetails(BaseModel):
    id: str
    name: str
    hours: PlaceHours | None
    phone: str | None
    website: str | None
    review_summary: ReviewSummary | None
    description: PlaceDescription | None = None
    reviews: list[PlaceDetailsReview]
    menu: PlaceMenu | None
    apple_place_id: str | None = Field(description="for MapKit's own place card (iOS 18+)")
    photos: list[PlacePhotoRef] = Field(description="GET …/photos/{index} for each one's URL")


class PlacePhoto(BaseModel):
    url: str = Field(description="short-lived: load it now, don't store it")
    expires_at: DateTime
    attribution: Attribution


class View(BaseModel):
    """One of Pisa's own views (a recipe of read-only reads and sheet blocks; data, never code)."""

    id: str
    name: str
    description: str
    provenance: Literal["said"]
    reads: int
    blocks: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    shown_at: AwareDatetime | None = None


class Views(BaseModel):
    views: list[View]


# --- trips and the residency counter ------------------------------------------------------------------------


class TripLeg(BaseModel):
    kind: Literal["flight", "train", "car", "hotel", "other"]
    ref: str | None = Field(
        default=None, description="the flight or train number, the hotel's or rental's name"
    )
    at: DateTime = Field(description="in the leg's own zone")
    until: DateTime | None = None
    place: str | None = None
    tz: str | None = None
    confirmation_last4: str | None = Field(default=None, description="a booking code's last 4 only")


class TripProposal(BaseModel):
    ref: str | None = None
    at: DateTime
    until: DateTime | None = None
    email_id: str


class TripChecklistItem(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    done: bool = False
    by: Literal["pisa", "owner"] = "pisa"


class Trip(BaseModel):
    id: str
    title: str
    from_city: str | None
    to_city: str | None
    from_country: str | None = None
    to_country: str | None = None
    depart_at: DateTime
    arrive_at: DateTime
    tz_from: str | None
    tz_to: str | None
    status: Literal["planned", "cancelled"]
    doubt: Literal["unverified_sender", "someone_else"] | None = Field(
        default=None,
        description="a trip Pisa isn't sure of ('da confermare'): no tasks, no landing, no evidence",
    )
    proposals: list[TripProposal] = Field(default_factory=list)
    legs: list[TripLeg]
    checklist: list[TripChecklistItem]
    source_email_ids: list[str]


class Trips(BaseModel):
    trips: list[Trip]


class TripChecklistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200)
    done: bool = False


class TripPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool | None = Field(
        default=None, description="true: a trip 'da confermare' is their (tasks made)"
    )
    apply_change: str | None = Field(
        default=None, description="a proposal's email_id: its new times applied (Pisa's tasks follow)"
    )
    title: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["planned", "cancelled"] | None = None
    checklist: list[TripChecklistIn] | None = Field(default=None, max_length=30)


ResidencyCountry = Annotated[str, Field(pattern=r"^([A-Z]{2}|other)$")]


class ResidencyRule(BaseModel):
    rule: str
    country: str
    count: float
    threshold: float
    note: str = Field(description="always closed by 'è un conteggio, non una consulenza'")


class ResidencyEvidence(BaseModel):
    day: str
    country: str
    source: Literal["phone_region", "trip", "calendar", "his_words"]


class Residency(BaseModel):
    year: int
    days: dict[str, int] = Field(
        description="Days per country (ISO 3166-1 alpha-2; every rule's country listed, 0 without a day), "
        "'other' for a place with no country code, and 'unknown': the days nothing says"
    )
    by_rule: list[ResidencyRule] = Field(
        description="Each rule's count. Empty when no rule is on: the rules are app_settings.residency_rules;"
        " without them only a user in Italy (PISA_COUNTRY=IT) has one, Italy's 183 days (US tests if named)"
    )
    evidence: list[ResidencyEvidence]
    note: str


class ResidencyDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str = Field(pattern=DAY_PATTERN)
    country: ResidencyCountry | None = Field(default=None, description="ISO 3166-1 alpha-2, or other")
    countries: list[ResidencyCountry] | None = Field(
        default=None, min_length=1, max_length=3, description="a travel day in two countries"
    )


class ResidencyDayOut(BaseModel):
    day: str
    country: str
    countries: list[str]
    source: Literal["his_words"]


class RitualAt(BaseModel):
    on: bool
    at: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="HH:MM, the user's local time")


class RitualOn(BaseModel):
    on: bool


class RitualPerWeek(BaseModel):
    on: bool
    per_week: int = Field(ge=0, le=7, description="At most this many ideas a week (2 by default)")


class Rituals(BaseModel):
    """Pisa's rituals, each can be turned off (also by voice, "spegni il Ghost Run")."""

    ghost_run: RitualAt = Field(description="tomorrow rehearsed (22:00): one problem, one fix, one yes")
    one_thing: RitualOn = Field(description="the morning briefing is the one thing that matters today")
    closing: RitualAt = Field(description='"Chiudiamo?": the day in a minute (21:30), Undo per line')
    week_poster: RitualOn = Field(description="Sunday 19:00: the shape of the week")
    something_different: RitualPerWeek = Field(description="An idea as a question, a few times a week")
    errands: RitualOn = Field(description='One errand combo a day at most ("passi da X e Y?")')
    walk_recap: RitualOn = Field(description="What they said on a walk, in 20 seconds, when it ends")
    life_map: RitualOn = Field(description="The Map of the Life, each season (a sheet they ask for)")
    stanotte: RitualOn = Field(
        description="The morning sheet, off until Pisa opens it after 7 nights in shadow; they can turn it "
        "off"
    )
    knock: RitualOn = Field(description='"Posso dirti una cosa?", at most once a morning, on their "dimmi"')
    tragitto: RitualOn = Field(description='"Vuoi il tragitto?", once a trip on foot or by transit')
    quarto: RitualOn = Field(
        description="Il quarto d'ora, off until they turn it on: a 15-minute walk talk, twice a week"
    )
    consiglio: RitualOn = Field(
        description="Il consiglio, off until they turn it on: Pisa as CEO to its chairman, Sunday, 15 min"
    )
    prova: RitualOn = Field(
        description="La prova generale, off until they turn it on: next week's collisions, one by one"
    )


class RitualsPatch(BaseModel):
    """Any part of the rituals; what is left out stays as it is."""

    model_config = ConfigDict(extra="forbid")

    ghost_run: dict[str, Any] | None = None
    one_thing: dict[str, Any] | None = None
    closing: dict[str, Any] | None = None
    week_poster: dict[str, Any] | None = None
    something_different: dict[str, Any] | None = None
    errands: dict[str, Any] | None = None
    walk_recap: dict[str, Any] | None = None
    life_map: dict[str, Any] | None = None
    stanotte: dict[str, Any] | None = None
    knock: dict[str, Any] | None = None
    tragitto: dict[str, Any] | None = None
    quarto: dict[str, Any] | None = None
    consiglio: dict[str, Any] | None = None
    prova: dict[str, Any] | None = None


class PlaceNote(BaseModel):
    """A note pinned to a place (or, `id` `task:<uuid>`, an open task with a place: it fires the same
    way and is deleted with the task)."""

    id: str
    text: str
    lat: float
    lon: float
    radius_m: int = Field(ge=50, le=500)
    place_name: str | None = None
    created_at: AwareDatetime
    last_fired_at: AwareDatetime | None = None
    task_id: str | None = None


class PlaceNotes(BaseModel):
    notes: list[PlaceNote]


class PlaceNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: int = Field(default=150, ge=50, le=500)
    place_name: str | None = Field(default=None, max_length=200)


class PlaceEnter(BaseModel):
    """The regions the phone entered (the same as the WebSocket's `place_enter`)."""

    model_config = ConfigDict(extra="forbid")

    note_ids: list[str] = Field(min_length=1, max_length=20)


class PlaceEntered(BaseModel):
    fired: list[str] = Field(description="the notes that fired now (each at most once in 6 hours)")
    text: str | None = Field(description="what Pisa said (a notice to the device), null when nothing fired")


class RitualSheet(BaseModel):
    """A ritual's sheet as the `present` object (kind sheet, blocks, Pisa's buttons in
    `actions`)."""

    object_id: str
    kind: Literal["sheet"]
    revision: int
    data: dict[str, Any]


class SheetActionDone(BaseModel):
    said: str = Field(description="what happened, in one line (the app may show or speak it)")


# --- people, and the care circle ----------------------------------------------------------------------------


class GiftIdea(BaseModel):
    id: int
    text: str
    from_: Literal["owner", "email"] = Field(
        alias="from", description="owner: their words; email: the person's own words to them, kept as data"
    )
    at: AwareDatetime

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class PersonDate(BaseModel):
    id: int
    kind: Literal["birthday", "anniversary", "name_day", "other"]
    on: str = Field(description="MM-DD, or YYYY-MM-DD when the year is known")
    remind_days_before: list[int]
    source: Literal["owner", "calendar"]


class PersonClock(BaseModel):
    """Their hour now and whether it is a decent hour to call them."""

    at: AwareDatetime = Field(description="Their time now, with their zone's offset")
    call: Literal["good", "early", "late"] = Field(
        description="good: 9:00-21:30 there (10:00 at their weekend); early/late: not now"
    )
    call_from: AwareDatetime | None = Field(
        default=None, description="When it next is a decent hour there (their offset); null when it is now"
    )
    line: str = Field(
        description='Pisa\'s own sentence: "Da mamma sono le 23:40: domani, dalle 9 loro (le 15 tue)?"'
    )


class Person(BaseModel):
    """Someone the user loves, as Pisa may know them (from their mail, their calendar and their words)."""

    id: str
    name: str
    relation: str | None = None
    birthday: str | None = Field(default=None, description="The birthday's `on`, for a list row")
    tz: str | None = Field(default=None, description="An IANA zone, from their words or the person's city")
    city: str | None = None
    emails: list[str]
    last_contact_at: AwareDatetime | None = None
    notes_count: int = Field(description="The notes they gave about the person, gift ideas included")
    gift_ideas: list[GiftIdea]
    dates: list[PersonDate]
    orbit: bool = Field(description="false: they said Pisa shouldn't point out they haven't heard from them")
    source: Literal["owner", "email", "calendar"]
    clock: PersonClock | None = Field(
        default=None,
        description="Their hour now and the call verdict; null when tz is unknown",
    )


class People(BaseModel):
    people: list[Person]
    orbits: bool = Field(
        description="false: Pisa points out nobody they haven't heard from (their words, or the app)"
    )


class PersonPatch(BaseModel):
    """Any subset; their corrections win over every sync. Null clears relation, tz, city or birthday."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    relation: str | None = None
    tz: str | None = Field(default=None, description="An IANA zone (America/New_York)")
    city: str | None = None
    birthday: str | None = Field(default=None, description="MM-DD or YYYY-MM-DD")
    orbit: bool | None = None
    hidden: bool | None = Field(default=None, description="true: out of the list and of every line Pisa says")


class CareMember(BaseModel):
    person_id: str
    name: str
    shares: Literal["location", "calendar_busy", "nothing"]
    since: AwareDatetime


class Care(BaseModel):
    """Only what they entered; nothing is collected from anyone in this slice."""

    members: list[CareMember]


class CareMemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_id: UUID
    shares: Literal["location", "calendar_busy", "nothing"]


class CarePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    members: list[CareMemberIn] = Field(max_length=20)


class Orbits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orbits: bool = Field(description="false: Pisa points out nobody they haven't heard from")


class PersonMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into: UUID = Field(description="The person who stays")


# --- their voice
# ----------------------------------------------------------------------------------------------


class VoiceConsent(BaseModel):
    version: str = Field(min_length=1, max_length=40, description="The consent text's version they agreed to")


class VoiceConsentGiven(BaseModel):
    consent_at: datetime


class VoiceRoute(BaseModel):
    route: str
    enrolled_at: datetime
    samples_s: float = Field(description="Seconds of their speech in the prints")
    ok: bool = Field(description="The route's bench met its target: the gate is on for it")


class VoiceProfile(BaseModel):
    routes: list[VoiceRoute]
    consent_at: datetime | None
    gate: Literal["on", "off"] = Field(
        description="on: their consent, a route enrolled, the server's switch on"
    )


class VoiceScriptBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    block: Literal["A", "B", "C", "D", "E"]
    title: str
    language: Literal["it", "en"]
    sentences: list[str] = Field(description="The sentences to read (C: the prompts to speak about)")
    optional: bool
    min_voiced_s: float


class VoiceScript(BaseModel):
    lang: Literal["it", "en"]
    blocks: list[VoiceScriptBlock]
    mini: list[VoiceScriptBlock] = Field(description="A new route's first talk: blocks A and C, short")
    test: list[str] = Field(
        description="The test's 6 sentences, read once after the blocks (enrol.test start/end "
        "around their reading); the server scores them, clean and in babble it mixes itself"
    )


class VoiceDeleted(BaseModel):
    deleted: int = Field(description="Rows removed (prints, recordings, consent)")
