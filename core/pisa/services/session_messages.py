"""The device session's messages (`session.DeviceSession`): what the shared turn pipeline sends a device
(`Say`, `Card`, `Present`, `Notify` ...) and what the app's frames become before the session handles them
(`Utterance`, `ObjectEvent`, `Presence` ...). Each names its kind in `type`.

They are internal types, never a wire format: the session writes the first as contract 2's AG-UI events and
reads the second from contract 2's frames, validating each frame's values against them (a field out
of range is the app's `invalid_frame`).
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pisa.domain import approvals, locale, sheet
from pisa.domain.drawing import ASK_MAX, graphemes
from pisa.domain.errors import PisaError, ProtocolErrorCode
from pisa.domain.sheet import RitualSheetData

Uuid = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")]
Lang = locale.Tag
Priority = Literal["low", "normal", "urgent"]


def _absent(value: object) -> bool:
    return value is None


def new_id() -> str:
    return str(uuid.uuid4())


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _Message(BaseModel):
    # Fields the session doesn't read are ignored (a frame's body may carry more than it uses).
    model_config = ConfigDict(strict=True, extra="ignore")

    id: Uuid = Field(default_factory=new_id)


# the objects Pisa shows (present), their kinds, how they come forward, and their ids.
ObjectKind = sheet.ObjectKind
ObjectId = Annotated[
    str,
    StringConstraints(pattern=sheet.OBJECT_ID),
]
FocusMode = Literal["take", "offer", "quiet"]
CheckStatus = Literal["verified", "likely", "unknown", "no"]
OptionId = Annotated[str, StringConstraints(pattern=r"^o[1-5]$")]
# a drawing's element: a node n1-n24, an edge e1-e40, a chart's point s1/p1-s4/p60, a
# timeline's item t1-t16
ElementId = Annotated[
    str,
    StringConstraints(
        pattern=r"^(n([1-9]|1[0-9]|2[0-4])|e([1-9]|[1-3][0-9]|40)|s[1-4]/p([1-9]|[1-5][0-9]|60)|t([1-9]|1[0-6]))$"
    ),
]
Section = Literal[
    "briefing", "reminders_quiet", "places_modes", "voice", "companion", "planner", "models", "spend",
    "keys_devices", "connectors", "about_you", "low_priority",
]  # fmt: skip


# the phone's zone (IANA), region (ISO 3166-1) and hour cycle
_TZ = r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$"
Tz = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=_TZ)]
Region = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
HourCycle = Literal["h23", "h12"]


class DeviceContext(_Message):
    """device -> Pisa: the phone's zone, region or hour cycle changed while the socket is open. Pisa answers
    nothing; an absent field keeps the last known."""

    type: Literal["device_context"] = "device_context"
    tz: Tz | None = Field(default=None, exclude_if=_absent)
    region: Region | None = Field(default=None, exclude_if=_absent)
    hour_cycle: HourCycle | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _one_field(self) -> "DeviceContext":
        if self.tz is None and self.region is None and self.hour_cycle is None:
            raise ValueError("device_context needs tz, region or hour_cycle")
        return self


TEXT_MAX = 4000  # the user's words in one turn, spoken or typed (talk.text's too)


class Utterance(_Message):
    type: Literal["utterance"] = "utterance"
    text: Annotated[str, StringConstraints(min_length=1, max_length=TEXT_MAX)]
    lang: Lang
    final: bool
    source: Literal["glasses_mic", "phone_mic", "earbuds_mic", "typed"]
    wake: Literal["hey_pisa", "stem_press", "tap", "reply"] | None = Field(default=None, exclude_if=_absent)
    # the user went on talking after Pisa closed the turn: this utterance's words follow
    # those of the one it continues; Pisa cancels that one's reply and answers the joined words as one turn.
    continues: Uuid | None = Field(default=None, exclude_if=_absent)
    # how they spoke this turn, each against HIS OWN 30-day baseline kept on the phone
    # only (never synced). Observables only, never an emotion; absent on old builds and on typed turns.
    voice: "VoiceObservables | None" = Field(default=None, exclude_if=_absent)


class VoiceObservables(_Strict):
    """The phone's per-turn observables, against their own baseline (each one
    optional)."""

    pace: Literal["slow", "usual", "fast"] | None = Field(default=None, exclude_if=_absent)
    loudness: Literal["soft", "usual", "loud"] | None = Field(default=None, exclude_if=_absent)
    gaps: Literal["few", "usual", "many"] | None = Field(default=None, exclude_if=_absent)
    place: Literal["whisper", "normal", "noisy_place"] | None = Field(default=None, exclude_if=_absent)


Utterance.model_rebuild()


class Say(_Message):
    type: Literal["say"] = "say"
    reply_to: Uuid | None = Field(default=None, exclude_if=_absent)
    stream_id: Uuid
    seq: Annotated[int, Field(ge=0)]
    delta: str
    final: bool
    lang: Lang
    priority: Priority
    # a holding phrase from protocol/holding-phrases.v1.json, played from the app's cache; the
    # answer follows in the same stream.
    holding: bool | None = Field(default=None, exclude_if=_absent)
    holding_id: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = Field(
        default=None, exclude_if=_absent
    )

    @model_validator(mode="after")
    def _holding_id(self) -> "Say":
        if bool(self.holding) != (self.holding_id is not None):
            raise ValueError("a holding say has holding: true and a holding_id (and only then)")
        return self


class Correction(_Strict):
    """A name Pisa fixed in what the user said."""

    model_config = ConfigDict(
        strict=True, extra="forbid", serialize_by_alias=True, validate_by_alias=True, validate_by_name=True
    )
    from_: Annotated[str, StringConstraints(min_length=1, max_length=200)] = Field(alias="from")
    to: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    via: Literal["sound", "spelling", "model"]


class Understood(_Message):
    """What Pisa understood the user meant, before its answer; only when something changed or Pisa asks."""

    type: Literal["understood"] = "understood"
    reply_to: Uuid | None = Field(default=None, exclude_if=_absent)
    meant: Annotated[str, StringConstraints(max_length=2000)]
    raw: Annotated[str, StringConstraints(max_length=2000)]
    corrections: Annotated[list[Correction], Field(max_length=10)]
    ask: Annotated[str, StringConstraints(max_length=200)] | None


class TurnHint(_Message):
    """How likely the user has finished, for a partial transcript (`reply_to`), and how long
    the app should wait at this pause before closing the turn."""

    type: Literal["turn_hint"] = "turn_hint"
    reply_to: Uuid
    p_end: Annotated[float, Field(ge=0, le=1)]
    wait_ms: Annotated[int, Field(ge=0, le=10000)]


class Location(_Message):
    type: Literal["location"] = "location"
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    accuracy_m: Annotated[float, Field(ge=0)]
    measured_at: AwareDatetime


class PlaceEnter(_Message):
    """The phone entered the regions of these place notes (or tasks with a place: `task:<uuid>`)."""

    type: Literal["place_enter"] = "place_enter"
    note_ids: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^(task:)?[0-9a-f-]{36}$")]],
        Field(min_length=1, max_length=20),
    ]


class PlaceCheck(_Strict):
    requirement: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    status: Literal["verified", "likely", "unknown", "no"]
    source: Annotated[str, StringConstraints(max_length=120)]


class PlaceSource(_Strict):
    title: Annotated[str, StringConstraints(max_length=120)]
    url: Annotated[str, StringConstraints(max_length=2000)]


class ShownPlace(_Strict):
    """One place of a `places` card: why it fits and each requirement as checked."""

    place_id: Uuid
    name: Annotated[str, StringConstraints(max_length=120)]
    why: Annotated[str, StringConstraints(max_length=200)]
    checks: Annotated[list[PlaceCheck], Field(max_length=6)]
    distance_m: int | None
    travel_min: int | None
    open_now: bool | None
    rating: float | None
    sources: Annotated[list[PlaceSource], Field(max_length=4)]
    # v2: the map, the first photo, the price, until when, the menu, what fits the user.
    lat: float | None = None
    lon: float | None = None
    photo_count: int = 0
    rating_count: int | None = None
    price_level: int | None = None
    closes_at: str | None = None
    opens_at: str | None = None
    menu_url: Annotated[str, StringConstraints(max_length=2000)] | None = None
    fits: Annotated[list[Annotated[str, StringConstraints(max_length=40)]], Field(max_length=3)] = Field(
        default_factory=list
    )
    different: bool = False  # "something different": unlike the user's usual, labelled; ≤ 1 a card


class SourceLink(_Strict):
    """A page behind a web fact (title, its host, the link)."""

    title: Annotated[str, StringConstraints(max_length=120)]
    url: Annotated[str, StringConstraints(max_length=2000, pattern=r"^https?://")]  # web pages only
    host: Annotated[str, StringConstraints(max_length=255)]


Celsius = Annotated[float, Field(ge=-100, le=70)]
Chance = Annotated[int, Field(ge=0, le=100)]
Condition = Annotated[str, StringConstraints(min_length=1, max_length=60)]


class WeatherNow(_Strict):
    temp_c: Celsius
    condition: Condition


class WeatherHourShown(_Strict):
    time: AwareDatetime
    temp_c: Celsius
    rain_chance: Chance
    condition: Condition


class WeatherShown(_Strict):
    """The forecast get_weather read, as the app's WeatherSummary (catalog core:2) shows it: degrees Celsius,
    chances in percent, at most 24 hours."""

    place: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    date: date
    condition: Condition
    now: WeatherNow | None = None  # the current hour; None for another day
    min_c: Celsius
    max_c: Celsius
    rain_chance: Chance
    hourly: Annotated[list[WeatherHourShown], Field(max_length=24)]


class Card(_Message):
    type: Literal["card"] = "card"
    card_id: Uuid
    title: Annotated[str, StringConstraints(max_length=120)]
    lines: Annotated[list[Annotated[str, StringConstraints(max_length=200)]], Field(max_length=10)]
    kind: Literal["info", "plan", "reminder", "warning", "place", "places", "sources", "weather"]
    place_id: Uuid | None = Field(default=None, exclude_if=_absent)  # kind=place: the place card to open
    # kind=places (show_places): what the user asked and the best 1 to 3 places.
    request: Annotated[str, StringConstraints(max_length=200)] | None = Field(
        default=None, exclude_if=_absent
    )
    places: Annotated[list[ShownPlace], Field(min_length=1, max_length=3)] | None = Field(
        default=None, exclude_if=_absent
    )
    # kind=sources: the links behind a web answer, sent with every one.
    sources: Annotated[list[SourceLink], Field(min_length=1, max_length=6)] | None = Field(
        default=None, exclude_if=_absent
    )

    # kind=weather (get_weather): the forecast, drawn as core:2's WeatherSummary.
    weather: WeatherShown | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _kind_has_its_fields(self) -> "Card":
        if self.kind == "sources" and not self.sources:
            raise ValueError("a sources card needs its sources")
        if self.kind == "weather" and self.weather is None:
            raise ValueError("a weather card needs its forecast")
        return self


class Choice(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
    label: Annotated[str, StringConstraints(min_length=1, max_length=40)]


class Notify(_Message):
    type: Literal["notify"] = "notify"
    notification_id: Uuid
    text: Annotated[str, StringConstraints(max_length=1000)]
    lang: Lang
    kind: Literal["reminder", "briefing", "replan", "alert", "info", "check_in", "done"]
    priority: Priority
    check_in_id: Uuid | None = Field(default=None, exclude_if=_absent)  # kind=check_in
    choices: Annotated[list[Choice], Field(min_length=1, max_length=5)] | None = Field(
        default=None, exclude_if=_absent
    )
    expects_reply: bool | None = Field(default=None, exclude_if=_absent)
    weekly: bool | None = Field(default=None, exclude_if=_absent)  # the weekly check is ready
    # the Sunday card of this ISO week is ready; the app opens GET /v1/ledger/summary?week=
    ledger_week: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-W[0-9]{2}$")] | None = Field(
        default=None, exclude_if=_absent
    )
    # kind=done: what Pisa closed because the user was there (or said so), and until when Undo works.
    task_id: Uuid | None = Field(default=None, exclude_if=_absent)
    event_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None = Field(
        default=None, exclude_if=_absent
    )
    title: Annotated[str, StringConstraints(max_length=200)] | None = Field(default=None, exclude_if=_absent)
    place: Annotated[str, StringConstraints(max_length=200)] | None = Field(default=None, exclude_if=_absent)
    undo_until: AwareDatetime | None = Field(default=None, exclude_if=_absent)
    # Pisa speaking first: `now` is said after the current sentence ("Scusa, un attimo:"),
    # `breakpoint` waits for the end of the exchange.
    urgency: Literal["now", "breakpoint"] | None = Field(default=None, exclude_if=_absent)
    # false = show it (a banner or the notice), don't speak it: Pisa's unprompted voice rate was reached.
    voice: bool | None = Field(default=None, exclude_if=_absent)
    # a ritual's notice; its sheet is read with GET /v1/sheets/{sheet_id} (a sheet, with buttons).
    # `people`, an offer about someone they love (a moment, a thank-you, a wish) with its sheet.
    # `stanotte`, the morning briefing's notice carrying last night's sheet.
    ritual: (
        Literal["ghost_run", "closing", "week_poster", "something_different", "people", "stanotte"] | None
    ) = Field(default=None, exclude_if=_absent)
    sheet_id: Annotated[str, StringConstraints(pattern=r"^sheet:[0-9a-f-]{36}$")] | None = Field(
        default=None, exclude_if=_absent
    )
    # the wrist's line, Pisa's template for the kind (never the text, outside words, a
    # name, an amount); "" at a sensitive place and for health: the watch shows the kind alone. Pisa sends it.
    wrist: Annotated[str, StringConstraints(max_length=60)] | None = Field(default=None, exclude_if=_absent)
    # the answer of a REST turn (POST /v1/turns) that took longer than its wait
    turn_id: Uuid | None = Field(default=None, exclude_if=_absent)
    # about a sensitive place or health: the watch shows the kind, never says it
    sensitive: bool | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _done_fields(self) -> "Notify":
        if self.kind == "done" and (
            self.title is None or self.undo_until is None or (self.task_id is None) == (self.event_id is None)
        ):
            raise ValueError("notify kind=done needs title, undo_until and one of task_id or event_id")
        return self


class SayAudio(_Message):
    """Pisa's spoken voice for a reply (stream_id) or a notify (notification_id)."""

    type: Literal["say_audio"] = "say_audio"
    stream_id: Uuid | None = Field(default=None, exclude_if=_absent)
    notification_id: Uuid | None = Field(default=None, exclude_if=_absent)
    seq: Annotated[int, Field(ge=0)]
    format: Literal["mp3", "aac_adts", "pcm16"]
    sample_rate: Literal[16000, 22050, 24000, 44100, 48000]
    data: Annotated[str, StringConstraints(max_length=400000)]
    final: bool
    sentence_end: bool | None = Field(default=None, exclude_if=_absent)  # this chunk's audio ends a sentence
    complete: bool | None = Field(default=None, exclude_if=_absent)  # final chunk: false = stopped early
    spoken_chars: Annotated[int, Field(ge=0)] | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _has_target(self) -> "SayAudio":
        if self.stream_id is None and self.notification_id is None:
            raise ValueError("say_audio needs a stream_id or a notification_id")
        return self


class Recipient(_Strict):
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    phone: Annotated[str, StringConstraints(max_length=32)] | None = Field(default=None)


class Compose(_Message):
    """A message for the user to send from the phone; their tap on Send is the approval."""

    type: Literal["compose"] = "compose"
    compose_id: Uuid
    app: Literal["messages", "whatsapp"]
    recipient: Recipient
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class Call(_Message):
    type: Literal["call"] = "call"
    call_id: Uuid
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    phone: Annotated[str, StringConstraints(min_length=3, max_length=32)]


class Directions(_Message):
    """Open directions to a place in the phone's maps app."""

    type: Literal["directions"] = "directions"
    directions_id: Uuid
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    mode: Literal["walk", "transit", "drive", "bike"]
    place_id: Uuid | None = Field(default=None, exclude_if=_absent)


class ActionDone(_Message):
    """Pisa changed something on the user's words (or their spoken yes), with no question: the app
    shows it with Undo until `undo_until` (absent: it can't be undone, e.g. a sent email)."""

    type: Literal["action_done"] = "action_done"
    approval_id: Uuid
    kind: approvals.Kind
    summary: Annotated[str, StringConstraints(max_length=500)]
    result: Annotated[str, StringConstraints(max_length=500)]
    undo_until: AwareDatetime | None = Field(default=None, exclude_if=_absent)


class Cancel(_Message):
    type: Literal["cancel"] = "cancel"
    target_id: Uuid


Route = Literal["speaker", "receiver", "wired", "earbuds", "bluetooth", "car", "glasses", "airplay", "other"]


class DeviceState(_Strict):
    """Where Pisa's words land on this device: the body line (Pisa as an agent)."""

    foreground: bool | None = Field(default=None, exclude_if=_absent)
    locked: bool | None = Field(default=None, exclude_if=_absent)
    route: Route | None = Field(default=None, exclude_if=_absent)
    glasses_worn: bool | None = Field(default=None, exclude_if=_absent)
    companion: bool | None = Field(default=None, exclude_if=_absent)


class Presence(_Message):
    type: Literal["presence"] = "presence"
    event: (
        Literal[
            "glasses_on",
            "glasses_off",
            "app_foreground",
            "app_background",
            "earbuds_connected",
            "earbuds_disconnected",
            "companion_on",
            "companion_off",
        ]
        | None
    ) = None  # contract 2.4: optional (a route change, a lock, with no event of its own: state only)
    state: DeviceState | None = Field(default=None, exclude_if=_absent)


class Error(_Message):
    type: Literal["error"] = "error"
    reply_to: Uuid | None = Field(default=None, exclude_if=_absent)
    code: ProtocolErrorCode
    message: str
    action: str | None = Field(default=None, exclude_if=_absent)
    retryable: bool

    @classmethod
    def from_error(cls, err: PisaError, reply_to: str | None = None) -> "Error":
        return cls(
            reply_to=reply_to,
            code=err.protocol_code,
            message=err.message,
            action=err.action or None,
            retryable=err.retryable,
        )


class PlanRef(_Strict):
    date: date
    options_id: Uuid | None
    # no plan stored for that day: the device shows its agenda (GET /v1/agenda); sent only when true
    agenda: bool = Field(default=False, exclude_if=lambda v: not v)


class PlaceRef(_Strict):
    place_id: Uuid
    detail: Literal["card", "full"]


class ItemRef(_Strict):
    ref: Annotated[
        str, StringConstraints(pattern=r"^(task|event|email|draft|approval|claim|thing):\S{1,1100}$")
    ]


class ChangesRef(_Strict):
    since: AwareDatetime | None


class SettingRef(_Strict):
    section: Section


class PlacesData(_Strict):
    title: Annotated[str, StringConstraints(max_length=120)]
    request: Annotated[str, StringConstraints(max_length=200)]
    places: Annotated[list[ShownPlace], Field(min_length=1, max_length=3)]


class CompareCell(_Strict):
    status: CheckStatus
    value: Annotated[str, StringConstraints(max_length=80)]
    source: Annotated[str, StringConstraints(max_length=120)]


class CompareRow(_Strict):
    place_id: Uuid
    name: Annotated[str, StringConstraints(max_length=120)]
    cells: Annotated[list[CompareCell], Field(min_length=1, max_length=6)]


class CompareData(_Strict):
    """2-4 places side by side; each row's cells follow `criteria` in order."""

    title: Annotated[str, StringConstraints(max_length=120)]
    criteria: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=40)]],
        Field(min_length=1, max_length=6),
    ]
    rows: Annotated[list[CompareRow], Field(min_length=2, max_length=4)]


class NoteData(_Strict):
    title: Annotated[str, StringConstraints(max_length=120)]
    lines: Annotated[
        list[Annotated[str, StringConstraints(max_length=200)]], Field(min_length=1, max_length=6)
    ]
    kind: Literal["info", "warning"]
    sources: Annotated[list[SourceLink], Field(min_length=1, max_length=6)] | None = Field(
        default=None, exclude_if=_absent
    )


# a drawing Pisa wrote, run sealed on the phone (no network, no storage, no navigation:
# the CSP Pisa put first in its <head>, each inline script allowed by its hash, and the web view's own
# blocks). The page reads `data` as window.PISA_DATA; it loads only the bundled `libraries` (pisa-bundle://lib/<name>.min.js,
# the app's pinned versions). Its only way out is "ask Pisa about this" (`drawing.ask`).
DrawingKind = Literal["flow", "schema", "chart", "comparison", "timeline", "explorer", "other"]
LibraryName = Literal["d3", "mermaid", "vega", "vega-lite", "vega-embed", "vega-interpreter"]


class DrawingLibrary(_Strict):
    name: LibraryName
    version: Annotated[str, StringConstraints(pattern=r"^[0-9]+(\.[0-9]+){1,3}$", max_length=20)]


class DrawingData(_Strict):
    drawing_id: Uuid
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    kind: DrawingKind
    html: Annotated[str, StringConstraints(min_length=1, max_length=200_400)]  # the page + Pisa's CSP meta
    data: dict[str, Any]
    libraries: Annotated[list[DrawingLibrary], Field(max_length=6)]
    # "knowledge" when Pisa drew what it knows (no data fetched this turn): the phone captions it
    # "Da quello che so" / "From what I know"; absent when it was drawn from this turn's results
    basis: Literal["knowledge"] | None = Field(default=None, exclude_if=_absent)


class ChoiceOption(_Strict):
    id: OptionId
    label: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    detail: Annotated[str, StringConstraints(max_length=80)] | None = Field(default=None, exclude_if=_absent)


class ChoiceData(_Strict):
    question: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    options: Annotated[list[ChoiceOption], Field(min_length=2, max_length=5)]


class _Object(_Strict):
    object_id: ObjectId
    revision: Annotated[int, Field(ge=0)]


class PlanObject(_Object):
    kind: Literal["plan"] = "plan"
    ref: PlanRef


class PlaceObject(_Object):
    kind: Literal["place"] = "place"
    ref: PlaceRef


class ItemObject(_Object):
    kind: Literal["item"] = "item"
    ref: ItemRef


class ChangesObject(_Object):
    kind: Literal["changes"] = "changes"
    ref: ChangesRef


class SettingObject(_Object):
    kind: Literal["setting"] = "setting"
    ref: SettingRef


class PlacesObject(_Object):
    kind: Literal["places"] = "places"
    data: PlacesData


class CompareObject(_Object):
    kind: Literal["compare"] = "compare"
    data: CompareData


class NoteObject(_Object):
    kind: Literal["note"] = "note"
    data: NoteData


class ChoiceObject(_Object):
    kind: Literal["choice"] = "choice"
    data: ChoiceData


class SheetObject(_Object):
    kind: Literal["sheet"] = "sheet"
    data: RitualSheetData  # a ritual's sheet may carry Pisa's buttons (`actions`)


class DrawingObject(_Object):
    kind: Literal["drawing"] = "drawing"
    data: DrawingData


# `ref`: what Pisa holds (the device reads it by REST: the truth, not a copy); `data`: what the turn computed.
PresentObject = Annotated[
    PlanObject | PlaceObject | ItemObject | ChangesObject | SettingObject | PlacesObject | CompareObject
    | NoteObject | ChoiceObject | SheetObject | DrawingObject,
    Field(discriminator="kind"),
]  # fmt: skip


class Present(_Message):
    """Show one object (to a device that draws its kind); answered by `Presented` (`pisa.surface.ack`)."""

    model_config = ConfigDict(strict=True, extra="ignore", serialize_by_alias=True, validate_by_alias=True,
                              validate_by_name=True)  # fmt: skip
    type: Literal["present"] = "present"
    present_id: Uuid
    reply_to: Uuid | None
    object_: PresentObject = Field(alias="object")
    purpose: Annotated[str, StringConstraints(max_length=120)]
    focus: FocusMode
    highlight: Annotated[str, StringConstraints(max_length=200)] | None = Field(
        default=None, exclude_if=_absent
    )
    lang: Lang
    expires_at: AwareDatetime | None = Field(default=None, exclude_if=_absent)


PresentStatus = Literal["shown", "queued", "deferred", "unavailable", "unsupported"]


class Presented(_Message):
    """The device's acknowledgment of a `Present` (same present_id)."""

    type: Literal["presented"] = "presented"
    present_id: Uuid
    object_id: ObjectId
    status: PresentStatus
    surface: Literal["phone", "glasses_display", "none"]
    reason: Annotated[str, StringConstraints(max_length=200)] | None = Field(default=None, exclude_if=_absent)


class ObjectEvent(_Message):
    """What the user did with an object Pisa showed: ids only, never content."""

    type: Literal["object_event"] = "object_event"
    object_id: ObjectId
    event: Literal["seen", "expanded", "kept", "closed", "chosen", "acted", "asked"]
    option_id: OptionId | None = Field(default=None, exclude_if=_absent)
    # `asked`, a tap on a drawing's element: its id (a node, an edge e<n>, a point s1/p3, a
    # timeline item), never its label
    element_id: ElementId | None = Field(default=None, exclude_if=_absent)
    action: Literal["go", "call", "plan_it", "undo", "open", "details"] | None = Field(
        default=None, exclude_if=_absent
    )

    @model_validator(mode="after")
    def _event_has_its_field(self) -> "ObjectEvent":
        if (self.event == "chosen") != (self.option_id is not None):
            raise ValueError("option_id goes with chosen, and only with it")
        if (self.event == "acted") != (self.action is not None):
            raise ValueError("action goes with acted, and only with it")
        if (self.event == "asked") != (self.element_id is not None):
            raise ValueError("element_id goes with asked, and only with it")
        return self


class FocusObject(_Message):
    """Bring a shown object back in front, or point at a part of it (`pisa.surface.focus`)."""

    type: Literal["focus"] = "focus"
    present_id: Uuid
    object_id: ObjectId
    highlight: Annotated[str, StringConstraints(max_length=200)] | None = Field(
        default=None, exclude_if=_absent
    )


class CloseObject(_Message):
    type: Literal["close"] = "close"
    object_id: ObjectId


class HandoffOpened(_Message):
    """device -> Pisa: the user tapped a handoff's button and its URL opened. Pisa answers nothing."""

    type: Literal["handoff_opened"] = "handoff_opened"
    handoff_id: Uuid
    object_id: ObjectId


class DrawingAsk(_Message):
    """device -> Pisa: "ask Pisa about this", after the user's tap on a drawing Pisa showed.
    `label` (1-120 user-perceived characters) is the page's text: the element's `data-pisa-ask` value or the
    label the page passed to `Pisa.ask`: the page wrote it, so Pisa treats it as outside text,
    quarantined, grounding nothing. Pisa answers it as their turn marked as coming from the drawing: it may
    read and answer, never approve, send or change anything. Pisa's reply answers this message's id
    (`reply_to`)."""

    type: Literal["drawing.ask"] = "drawing.ask"
    drawing_id: Uuid
    # counted as the phone counts it (grapheme clusters); the schema caps code points at 960
    label: Annotated[str, StringConstraints(min_length=1, max_length=960)]

    @field_validator("label")
    @classmethod
    def _label_length(cls, value: str) -> str:
        if len(graphemes(value)) > ASK_MAX:
            raise ValueError(f"label is longer than {ASK_MAX} characters")
        return value


class TalkAskToOpen(_Message):
    """Pisa has something to say first (the user's AirPods or glasses on, no talk open): the phone
    opens a talk for it (`trigger: proactive`, `ask_id` = this id), or shows `line` as a time-sensitive
    notice."""

    type: Literal["talk.ask_to_open"] = "talk.ask_to_open"
    why: Literal["leave_now", "reply_waiting", "approval_lapsing", "check_in"]
    line: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    lang: Lang
    # the item's own notify (voice false): the phone shows one alert, not two
    notification_id: Uuid | None = Field(default=None, exclude_if=_absent)
