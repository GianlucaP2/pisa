"""REST models: the connectors' registry (Settings → Connectors), the phone's health summary, the
admin radar's list. Credentials are write-only: no response model has a field for one."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pisa.api import schemas
from pisa.domain.connectors import Family, Missing, State


class CredentialField(BaseModel):
    name: str
    title: str
    secret: bool = Field(description="Shown as a password field; every field is write-only all the same")
    kind: Literal["text", "date", "pem"]


class CredentialStatus(BaseModel):
    fields: list[CredentialField]
    set: bool = Field(description="A credential is stored (sealed); its value is never returned")
    set_at: str | None
    expires_on: str | None = Field(description="The key's own expiry, when it has one (Garmin)")


class Consent(BaseModel):
    expires_at: str = Field(description="PSD2: when the bank's consent ends")
    renew_from: str | None = Field(description="From when Pisa reminds them to renew it (7 days before)")


class DataOut(BaseModel):
    provider: str
    what: str
    where: str


class SyncInfo(BaseModel):
    at: str | None
    counts: dict[str, int] = Field(description="Counts only (days, transactions, obligations, …)")


class ConnectorOut(BaseModel):
    id: str
    name: str
    family: Family
    provider: str
    state: State = Field(
        description="off: switched off (no sync, no call, no tool); needs_owner: on, something of their "
        "missing (`missing`); error: on, the last syncs failed (`message`); on"
    )
    enabled: bool = Field(description="Their switch")
    missing: list[Missing]
    message: str | None = Field(description="The real cause of `error`, and the action")
    settings: dict[str, Any]
    settings_schema: dict[str, Any] = Field(description="JSON Schema of `settings` (the app renders it)")
    credential: CredentialStatus | None = Field(description="None: this connector takes no credential")
    needs_consent: bool = Field(description="A consent on the provider's page (the bank's SCA)")
    consent: Consent | None
    data_out: DataOut | None = Field(
        description="Where their data goes when it is on: shown before switching on (PATCH `data_out_seen`)"
    )
    data_out_seen: bool
    last_sync: SyncInfo
    tools: list[str] = Field(description="The brain tools it feeds (found by search_tools)")
    built: bool = Field(description="False: the row and its settings exist; its provider is the next to add")
    google: schemas.Connector | None = Field(
        default=None, description="google only: the Google connection's own detail"
    )


class ConnectorsOut(BaseModel):
    connectors: list[ConnectorOut]


class ConnectorPatch(BaseModel):
    """Signed by the phone (`device` op `connector`, target `<id>:<SHA-256 hex of this body as sent>`)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    settings: dict[str, Any] | None = Field(
        default=None, description="A change, validated by settings_schema"
    )
    data_out_seen: bool = Field(
        default=False, description="The app showed `data_out` before this switch-on (required once)"
    )


class CredentialIn(BaseModel):
    """Signed (`device` op `connector_credential`, target `<id>:<SHA-256 hex of this body as sent>`)."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[Annotated[str, Field(max_length=40)], Annotated[str, Field(max_length=8000)]]


class ConsentStart(BaseModel):
    url: str = Field(description="The bank's consent page: the phone opens it")
    state: str = Field(description="Sent back with the bank's code")


class ConsentCode(BaseModel):
    """Signed (`device` op `connector_consent`, target `<id>:<SHA-256 hex of code>`)."""

    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(min_length=1, max_length=2000)]
    state: Annotated[str, Field(min_length=1, max_length=200)]


class HealthSummaryAck(BaseModel):
    day: str
    state: State


class ObligationOut(BaseModel):
    id: str
    kind: str
    what: str
    due_on: str | None
    state: Literal["upcoming", "ready", "done", "overdue", "flagged"]
    source: Literal["mail", "pec", "vault", "calendar", "bank", "ade"]
    penalty: str | None
    channel: str | None
    amount_as_printed: str | None = Field(description="Copied from the notice as printed; never computed")
    rule_source_url: str | None
    last_verified: str | None


class FlaggedOut(BaseModel):
    id: str
    source: str
    why: str | None
    at: str


class ObligationsOut(BaseModel):
    window_days: int
    items: list[ObligationOut]
    flagged: int = Field(description="Messages flagged as phishing-like in the window: shown, never tracked")
    flagged_recent: list[FlaggedOut]
