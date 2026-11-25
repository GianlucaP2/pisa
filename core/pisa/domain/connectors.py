"""Every source is a connector, parametric, switched in its own Settings section. Pure rules, no I/O.

`REGISTRY` is code's own list: one `Spec` a connector (family, provider, what goes to a new provider, the
user-visible settings and their schema, the credential's fields, the brain tools it feeds). The service
writes one `connectors` row a spec at start; their switch (`enabled`) and their settings live there.

**The state** (`state_of`): their switch off → `off` (no sync, no call, no tool offered); on with something
missing (a key, a login, a consent, the phone's first summary) → `needs_owner`; on with a failing last sync →
`error`, its real cause in `message`; else `on`. A successful sync brings `error` back to `on`.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pisa.domain import locale

Family = Literal["health", "sport", "bank", "admin", "mail", "calendar", "places", "home", "mobility"]
State = Literal["off", "on", "needs_owner", "error"]
STATES: tuple[State, ...] = ("off", "on", "needs_owner", "error")

# What may be missing, as the app names it (Settings shows the action for each)
Missing = Literal[
    "credential",  # a key or a password they enter (write-only)
    "credential_expired",  # the key's own expiry passed (Garmin's Personal API key)
    "consent",  # a consent they give on the provider's page (the bank's SCA)
    "consent_expired",
    "phone_permission",  # the phone's Health permission: no summary has arrived yet
    "google_connect",  # sign in with your own Google client (the browser comes back to this server)
    "google_off",  # admin_mail reads Google's copy: Google is switched off
    "provider_not_built",  # a row with its schema; its provider is the next one to add
]

CONSENT_RENEW_BEFORE = timedelta(days=7)  # PSD2: the renewal reminder, a week before the consent ends
FAILURES_BEFORE_ERROR = 3  # consecutive failing syncs (not auth) before a connector shows `error`
BANK_CONSENT_MAX_DAYS = 180
HEALTH_MAX_DAYS = 30
MONEY_MAX_DAYS = 90
OBLIGATIONS_MAX_DAYS = 400


# --- the user-visible settings, one model a connector (its JSON schema is what the app renders) ------------


class _Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoSettings(_Settings):
    pass


class HealthSettings(_Settings):
    """What the phone sends: each area can be left out (the phone asks Health only for what is on)."""

    sleep: bool = Field(default=True, title="Sleep", description="Duration, bedtime and wake-up time")
    activity: bool = Field(default=True, title="Activity", description="Steps, active calories, minutes")
    workouts: bool = Field(default=True, title="Workouts", description="Type, duration, calories, distance")
    heart: bool = Field(default=True, title="Heart", description="Resting heart rate, HRV, VO2 max")
    medications: bool = Field(
        default=False, title="Medications", description="Only «taken / skipped»: never the medication's name"
    )


class GarminSettings(_Settings):
    activities: bool = Field(default=True, title="Activity")
    sleep: bool = Field(default=True, title="Sleep")
    daily: bool = Field(default=True, title="Daily summary")


class BankSettings(_Settings):
    aspsp_name: str | None = Field(
        default=None,
        max_length=120,
        title="Bank",
        description="The bank's name, as Enable Banking lists it",
    )
    aspsp_country: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        title="Bank's country",
        description="The country's ISO code (IT, DE…); empty: Pisa's country (PISA_COUNTRY)",
    )
    consent_days: int = Field(
        default=90,
        ge=1,
        le=BANK_CONSENT_MAX_DAYS,
        title="Consent duration (days)",
        description="PSD2: 90 to 180 days depending on the bank; then it is renewed on the bank's page",
    )
    history_days: int = Field(default=90, ge=1, le=MONEY_MAX_DAYS, title="Transactions read (days)")
    redirect_url: str | None = Field(
        default=None,
        max_length=300,
        pattern=r"^https://",
        title="Return address",
        description="The one registered in the Enable Banking app: the bank sends you back there after the "
        "consent. It is https://<Pisa's address>/v1/connectors/bank/return: Pisa passes it to the app",
    )


class PecSettings(_Settings):
    imap_host: str | None = Field(
        default=None, max_length=200, pattern=r"^[A-Za-z0-9.-]+$", title="PEC IMAP server"
    )
    imap_port: int = Field(default=993, ge=1, le=65535, title="Port")
    days: int = Field(default=30, ge=1, le=365, title="Days read")


class AdminMailSettings(_Settings):
    pagopa: bool = Field(default=True, title="pagoPA notices")
    send: bool = Field(default=True, title="SEND notifications")
    insurers: bool = Field(default=True, title="Insurers")
    condominium: bool = Field(default=True, title="Condominium")
    ade_inps: bool = Field(default=True, title="Agenzia delle Entrate and INPS")


class AdminCalendarSettings(_Settings):
    """Their own list: which parts of the official calendar are theirs."""

    car: bool = Field(
        default=False, title="Car", description="Road tax, inspection, insurance: the documents' due dates"
    )
    property: bool = Field(default=False, title="Property beyond the main home", description="IMU")
    condominium: bool = Field(default=False, title="Condominium", description="The yearly accounts meeting")
    condominium_year_end: str = Field(
        default="12-31",
        pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$",
        title="Condominium financial year end (MM-DD)",
    )
    isee: bool = Field(default=False, title="ISEE", description="The new DSU from 1 January")
    income_tax: bool = Field(
        default=True, title="Income tax return", description="730, Redditi, advance payments"
    )


class AdeSettings(_Settings):
    years: int = Field(default=2, ge=1, le=5, title="Years of CU read")


# --- the credential's fields (write-only; `secret` ones are sealed, the others are settings-like) ---------


@dataclass(frozen=True)
class CredentialField:
    name: str
    title: str
    secret: bool = True
    kind: Literal["text", "date", "pem"] = "text"


@dataclass(frozen=True)
class DataOut:
    """What goes to a provider that isn't Pisa (shown before they switch the connector on)."""

    provider: str
    what: str
    where: str


@dataclass(frozen=True)
class Spec:
    id: str
    name: str
    family: Family
    provider: str
    settings: type[_Settings] = NoSettings
    credential: tuple[CredentialField, ...] = ()
    needs_consent: bool = False
    data_out: DataOut | None = None
    default_enabled: bool = False
    built: bool = True  # False: a row and its schema; the provider is the next one to add
    tools: frozenset[str] = field(default_factory=frozenset)
    # the one country it serves (Italy's PEC, pagoPA, tax calendar, Agenzia delle Entrate): off for a user
    # anywhere else (PISA_COUNTRY), whatever its switch says
    country: str | None = None


def in_reach(s: Spec, country: str | None) -> bool:
    """The connector serves the user's country (`country`: PISA_COUNTRY), or any."""
    return s.country is None or s.country.upper() == (country or "").upper()


HEALTH_TOOLS = frozenset({"health_summary"})
MONEY_TOOLS = frozenset({"money_recent"})
ADMIN_TOOLS = frozenset({"obligations"})
# Google off: Pisa's mail and calendar tools that only reach Google are not offered ("off means off")
GOOGLE_TOOLS = frozenset(
    {
        "search_my_email",
        "read_my_email",
        "draft_reply",
        "draft_email",
        "send_draft",
        "edit_draft",
        "create_event",
        "move_event",
        "cancel_event",
    }
)

_GARMIN_KEY = (
    CredentialField("api_key", "Personal API key (Garmin shows it once)"),
    CredentialField("expires_on", "Key expiry", secret=False, kind="date"),
)
_ENABLE_BANKING = (
    CredentialField("application_id", "Enable Banking application ID", secret=False),
    CredentialField("private_key", "The application's private key (PEM)", kind="pem"),
)
_BANK_OUT = DataOut(
    "Enable Banking (authorised PSD2 provider)",
    "The consent to read the accounts (on the bank's page, with its own authentication); "
    "Pisa receives accounts and transactions, never a payment",
    "EU (Finland)",
)

REGISTRY: tuple[Spec, ...] = (
    Spec("google", "Google (Gmail + Calendar)", "mail", "google", default_enabled=True, tools=GOOGLE_TOOLS),
    Spec(
        "apple_health",
        "Apple Health",
        "health",
        "apple",
        settings=HealthSettings,
        default_enabled=True,
        tools=HEALTH_TOOLS,
    ),
    Spec(
        "garmin",
        "Garmin",
        "sport",
        "garmin",
        settings=GarminSettings,
        credential=_GARMIN_KEY,
        data_out=DataOut(
            "Garmin",
            "The Personal API key, to download your Garmin data (read-only)",
            "Garmin Connect",
        ),
        tools=HEALTH_TOOLS,
    ),
    Spec(
        "bank",
        "Bank (read-only)",
        "bank",
        "enable_banking",
        settings=BankSettings,
        credential=_ENABLE_BANKING,
        needs_consent=True,
        data_out=_BANK_OUT,
        tools=MONEY_TOOLS,
    ),
    Spec(
        "bank_2",
        "Second bank (read-only)",
        "bank",
        "enable_banking",
        settings=BankSettings,
        credential=_ENABLE_BANKING,
        needs_consent=True,
        data_out=_BANK_OUT,
        tools=MONEY_TOOLS,
    ),
    Spec(
        "bank_3",
        "Third bank (read-only)",
        "bank",
        "enable_banking",
        settings=BankSettings,
        credential=_ENABLE_BANKING,
        needs_consent=True,
        data_out=_BANK_OUT,
        tools=MONEY_TOOLS,
    ),
    Spec(
        "admin_mail",
        "Radar: mail",
        "admin",
        "pisa",
        settings=AdminMailSettings,
        default_enabled=True,
        tools=ADMIN_TOOLS,
        country="IT",
    ),
    Spec(
        "admin_pec",
        "Radar: PEC",
        "admin",
        "imap",
        settings=PecSettings,
        credential=(
            CredentialField("username", "PEC address", secret=False),
            CredentialField("password", "PEC app password"),
        ),
        default_enabled=True,  # on; their own mailbox, no data_out gate
        tools=ADMIN_TOOLS,
        country="IT",
    ),
    Spec("admin_documents", "Radar: documents", "admin", "pisa", default_enabled=True, tools=ADMIN_TOOLS),
    Spec(
        "admin_calendar",
        "Radar: calendar",
        "admin",
        "pisa",
        settings=AdminCalendarSettings,
        default_enabled=True,
        tools=ADMIN_TOOLS,
        country="IT",
    ),
    Spec(
        "ade_cassetto",
        "Agenzia delle Entrate: tax drawer",
        "admin",
        "agenzia_entrate",
        settings=AdeSettings,
        credential=(
            CredentialField("username", "Tax code (Fisconline)", secret=False),
            CredentialField("password", "Password Fisconline"),
            CredentialField("pin", "PIN Fisconline"),
        ),
        data_out=DataOut(
            "Agenzia delle Entrate",
            "The Fisconline credentials, to read the CUs (read-only)",
            "agenziaentrate.gov.it",
        ),
        tools=ADMIN_TOOLS,
        country="IT",
    ),
    # more sport platforms, built off; each is a provider behind `ports.sources.SportProvider`
    Spec(
        "polar",
        "Polar",
        "sport",
        "polar",
        credential=(CredentialField("token", "Polar AccessLink token"),),
        data_out=DataOut("Polar", "The token, to read your workouts", "Polar Flow"),
        built=False,
        tools=HEALTH_TOOLS,
    ),
    Spec(
        "withings",
        "Withings",
        "health",
        "withings",
        credential=(CredentialField("token", "Withings token"),),
        data_out=DataOut("Withings", "The token, to read weight and sleep", "Withings"),
        built=False,
        tools=HEALTH_TOOLS,
    ),
)
# --- the connector's words in each language ---------------------------------------------------------------
# Every user-visible string above (a name, a field's title or description, a credential field, what goes to a
# provider) is written in English; Italian comes from this table when the language is Italian (PISA_LANGUAGE,
# `locale.pick`). A string missing here is shown in English.
WORDS: dict[str, dict[str, str]] = {
    "en": {},
    "it": {
        "Sleep": "Sonno",
        "Duration, bedtime and wake-up time": "Durata, ora di letto e di risveglio",
        "Activity": "Attività",
        "Steps, active calories, minutes": "Passi, calorie attive, minuti",
        "Workouts": "Allenamenti",
        "Type, duration, calories, distance": "Tipo, durata, calorie, distanza",
        "Heart": "Cuore",
        "Resting heart rate, HRV, VO2 max": "Frequenza a riposo, HRV, VO2 max",
        "Medications": "Farmaci",
        "Only «taken / skipped»: never the medication's name": "Solo «presa / saltata»: "
        "mai il nome del farmaco",
        "Daily summary": "Riepilogo giornaliero",
        "Bank": "Banca",
        "The bank's name, as Enable Banking lists it": "Il nome della banca, come lo elenca Enable Banking",
        "Bank's country": "Paese della banca",
        "The country's ISO code (IT, DE…); empty: Pisa's country (PISA_COUNTRY)": "Il codice ISO del paese "
        "(IT, DE…); vuoto: il paese di Pisa (PISA_COUNTRY)",
        "Consent duration (days)": "Durata del consenso (giorni)",
        "PSD2: 90 to 180 days depending on the bank; then it is renewed on the bank's page": "PSD2: da 90 a "
        "180 giorni secondo la banca; poi si rinnova sulla pagina della banca",
        "Transactions read (days)": "Movimenti letti (giorni)",
        "Return address": "Indirizzo di ritorno",
        "PEC IMAP server": "Server IMAP della PEC",
        "Port": "Porta",
        "Days read": "Giorni letti",
        "pagoPA notices": "Avvisi pagoPA",
        "SEND notifications": "Notifiche SEND",
        "Insurers": "Assicurazioni",
        "Condominium": "Condominio",
        "Agenzia delle Entrate and INPS": "Agenzia delle Entrate e INPS",
        "Car": "Auto",
        "Road tax, inspection, insurance: the documents' due dates": "Bollo, revisione, RC: le "
        "scadenze dei documenti",
        "Property beyond the main home": "Immobili oltre la prima casa",
        "The yearly accounts meeting": "L'assemblea dei conti annuali",
        "Condominium financial year end (MM-DD)": "Fine dell'esercizio del condominio (MM-GG)",
        "The new DSU from 1 January": "La DSU nuova dal 1° gennaio",
        "Income tax return": "Dichiarazione dei redditi",
        "730, Redditi, advance payments": "730, Redditi, acconti",
        "Years of CU read": "Anni di CU letti",
        "Personal API key (Garmin shows it once)": "Chiave Personal API (Garmin la mostra una volta)",
        "Key expiry": "Scadenza della chiave",
        "Enable Banking application ID": "ID dell'applicazione Enable Banking",
        "The application's private key (PEM)": "Chiave privata dell'applicazione (PEM)",
        "Enable Banking (authorised PSD2 provider)": "Enable Banking (fornitore PSD2 autorizzato)",
        "EU (Finland)": "UE (Finlandia)",
        "The Personal API key, to download your Garmin data (read-only)": "La chiave Personal API, per "
        "scaricare i tuoi dati Garmin (sola lettura)",
        "Bank (read-only)": "Banca (sola lettura)",
        "Second bank (read-only)": "Seconda banca (sola lettura)",
        "Third bank (read-only)": "Terza banca (sola lettura)",
        "Radar: mail": "Radar: la posta",
        "Radar: PEC": "Radar: la PEC",
        "PEC address": "Indirizzo PEC",
        "PEC app password": "Password per le app della PEC",
        "Radar: documents": "Radar: i documenti",
        "Radar: calendar": "Radar: il calendario",
        "Agenzia delle Entrate: tax drawer": "Agenzia delle Entrate: cassetto fiscale",
        "Tax code (Fisconline)": "Codice fiscale (Fisconline)",
        "The Fisconline credentials, to read the CUs (read-only)": "Le credenziali Fisconline, per leggere "
        "le CU (sola lettura)",
        "Polar AccessLink token": "Token Polar AccessLink",
        "The token, to read your workouts": "Il token, per leggere i tuoi allenamenti",
        "Withings token": "Token Withings",
        "The token, to read weight and sleep": "Il token, per leggere peso e sonno",
        "The one registered in the Enable Banking app: the bank sends you back there after the consent. It "
        "is https://<Pisa's address>/v1/connectors/bank/return: Pisa passes it to the app": "Quello "
        "registrato nell'app Enable Banking: la banca vi rimanda dopo il consenso. È "
        "https://<l'indirizzo di Pisa>/v1/connectors/bank/return: Pisa lo passa all'app",
        "The consent to read the accounts (on the bank's page, with its own authentication); Pisa receives "
        "accounts and transactions, never a payment": "Il consenso alla lettura dei conti (sulla pagina "
        "della banca, con la sua autenticazione); Pisa riceve conti e movimenti, mai un pagamento",
    },
}


def say(text: str, lang: str) -> str:
    """A connector string in `lang`."""
    return locale.pick(WORDS, lang).get(text, text)


BY_ID: dict[str, Spec] = {s.id: s for s in REGISTRY}
BANKS = tuple(s.id for s in REGISTRY if s.family == "bank")
HEALTH_SOURCES = ("apple_health", "garmin")


def spec(connector_id: str) -> Spec | None:
    return BY_ID.get(connector_id)


def schema(s: Spec, lang: str) -> dict[str, Any]:
    """The settings' JSON schema, as the app renders it: each field's title and description in `lang`."""
    out = s.settings.model_json_schema()
    for prop in out.get("properties", {}).values():
        for key in ("title", "description"):
            if isinstance(prop.get(key), str):
                prop[key] = say(prop[key], lang)
    return out


def defaults(s: Spec) -> dict[str, Any]:
    return s.settings().model_dump()


def validate_settings(s: Spec, current: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """Their change over the current settings, validated by the connector's own schema. ValueError: the
    first field that is wrong, named."""
    try:
        return s.settings.model_validate({**defaults(s), **current, **change}).model_dump()
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "settings"
        raise ValueError(f"{where}: {first['msg']}") from None


def validate_credential(s: Spec, fields: dict[str, str]) -> tuple[dict[str, str], date | None]:
    """The credential's fields: exactly the spec's, each non-empty. Returns (every text field, sealed
    together: write-only, and the key's expiry). ValueError names the field."""
    names = {f.name for f in s.credential}
    if not s.credential:
        raise ValueError(f"{s.name} takes no credential")
    if extra := sorted(set(fields) - names):
        raise ValueError(f"unknown field: {extra[0]}")
    sealed: dict[str, str] = {}
    expires: date | None = None
    for f in s.credential:
        value = (fields.get(f.name) or "").strip()
        if not value:
            raise ValueError(f"{f.name}: missing")
        if len(value) > 8000:
            raise ValueError(f"{f.name}: too long")
        if f.kind == "date":
            try:
                expires = date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{f.name}: a date as YYYY-MM-DD") from None
        elif f.kind == "pem" and "PRIVATE KEY" not in value:
            raise ValueError(f"{f.name}: a PEM private key")
        else:
            sealed[f.name] = value
    return sealed, expires


@dataclass(frozen=True)
class Facts:
    """What the state is decided from (the row, read by the service)."""

    enabled: bool
    has_secret: bool
    secret_expires_on: date | None
    consent_expires_at: datetime | None
    failures: int
    google_status: str | None = None  # google and admin_mail: not_connected / connected / error
    google_enabled: bool = True  # admin_mail: Google's own switch
    phone_seen: bool = False  # apple_health only: a summary arrived


def missing(s: Spec, f: Facts, now: datetime, today: date) -> list[Missing]:
    out: list[Missing] = []
    if not s.built:
        return ["provider_not_built"]
    if s.id == "google":
        return ["google_connect"] if f.google_status in (None, "not_connected") else []
    if s.id == "admin_mail":  # the radar reads Google's copy of their mail, so it needs Google
        if not f.google_enabled:
            return ["google_off"]
        if f.google_status in (None, "not_connected"):
            return ["google_connect"]
    if s.id == "apple_health" and not f.phone_seen:
        out.append("phone_permission")
    if s.credential:
        if not f.has_secret:
            out.append("credential")
        elif f.secret_expires_on is not None and f.secret_expires_on < today:
            out.append("credential_expired")
    if s.needs_consent:
        if f.consent_expires_at is None:
            out.append("consent")
        elif f.consent_expires_at <= now:
            out.append("consent_expired")
    return out


def state_of(s: Spec, f: Facts, now: datetime, today: date, country: str | None) -> State:
    if not f.enabled or not in_reach(s, country):
        return "off"
    if missing(s, f, now, today):
        return "needs_owner"
    if s.id == "google":
        return "error" if f.google_status == "error" else "on"
    return "error" if f.failures >= FAILURES_BEFORE_ERROR else "on"


def renew_from(consent_expires_at: datetime | None) -> datetime | None:
    """PSD2: when Pisa reminds them to renew the bank's consent (a week before it ends)."""
    return consent_expires_at - CONSENT_RENEW_BEFORE if consent_expires_at else None


# a connector they haven't connected yet (`needs_owner`) feeds its tools nothing, and an empty read
# ("no email matches") would be said as "nothing found". Its tools answer that it isn't connected instead: by
# connector, the name Pisa says and every tool that depends on it (show_email reads Google's mail too).
UNCONNECTED_SAYS: dict[str, tuple[str, frozenset[str]]] = {
    "google": ("Google", GOOGLE_TOOLS | {"show_email"}),
}


def tools_unconnected(states: dict[str, State]) -> dict[str, str]:
    """The brain tools whose connector waits for them to connect it (`needs_owner`, the state the connectors
    service decides): tool -> the connector's name, for a result that says it isn't connected."""
    out: dict[str, str] = {}
    for connector, (name, tools) in UNCONNECTED_SAYS.items():
        if states.get(connector) == "needs_owner":
            out |= dict.fromkeys(tools, name)
    return out


def tools_off(states: dict[str, State]) -> frozenset[str]:
    """The brain tools no connector that is on feeds: never offered (off means off)."""
    fed: set[str] = set()
    every: set[str] = set()
    for s in REGISTRY:
        every |= s.tools
        if states.get(s.id) not in (None, "off") and s.built:
            fed |= s.tools
    return frozenset(every - fed)
