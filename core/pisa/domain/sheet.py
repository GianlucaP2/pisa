"""A sheet Pisa composes on the user's phone: text, images (from the pages Pisa read, or a place's
photos), links, lists and facts. The phone renders each block with its own fixed template (fonts, spacing,
boxes), never a page's HTML. The device protocol's `present` object of kind `sheet` carries `SheetData`.
 adds section, table, stat, chips and divider; 24 blocks a sheet; text 600 characters a block. A
phone skips a block type it doesn't know, so an older one shows the rest.
 adds handoff: a big button that opens what Pisa prepared (a page, a call, a message) in its own
app; the user's tap there is the yes.
 (proposed) adds the drawing blocks: diagram, chart and timeline. They are data,
never code: labels are text, numbers are numbers, and a diagram's `layout` is written by CORE's code
(`pisa.domain.layout`), never the model or the phone. At most DRAWINGS of them a sheet. `basis` says where
the sheet stands (Pisa's own knowledge captions "Da quello che so"); `lang` is its language."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pisa.domain import locale
from pisa.domain.lexicon.dates import iso_day_or_none, iso_moment_or_none


def _absent(value: object) -> bool:
    return value is None


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SheetSource(_Strict):
    """A page behind the sheet's facts (title, its host, the link)."""

    title: Annotated[str, StringConstraints(max_length=120)]
    url: Annotated[str, StringConstraints(max_length=2000, pattern=r"^https?://")]
    host: Annotated[str, StringConstraints(max_length=255)]


# the kinds of object Pisa presents (the device protocol's `present`), in the wire's order
ObjectKind = Literal[
    "plan", "place", "places", "compare", "item", "changes", "setting", "note", "choice", "sheet", "drawing"
]  # fmt: skip
OBJECT_KINDS: tuple[ObjectKind, ...] = get_args(ObjectKind)
# what an `item` object shows: one of the user's records, by its kind
ITEM_KINDS = ("task", "event", "email", "draft", "approval", "claim", "thing")
# an object's id is `<kind>:<the record's id>` (an item's kind is its record's kind)
_ID_KINDS = tuple(k for kind in OBJECT_KINDS for k in (ITEM_KINDS if kind == "item" else (kind,)))
ITEM_ID = "^(" + "|".join(ITEM_KINDS) + r"):\S{1,1100}$"
OBJECT_ID = "^(" + "|".join(_ID_KINDS) + r"):\S{1,1100}$"
SHEET_IMAGES = 6
SHEET_BLOCKS = 24
TEXT_MAX = 600  # a text block's characters
HANDOFF_TEXT = 3000  # a handoff's words to copy (a claim letter is ~2,000)
TABLE_COLUMNS, TABLE_ROWS, CHIPS = 6, 20, 12
_HTTPS = r"^https://"


class SheetText(_Strict):
    type: Literal["text"]
    text: Annotated[str, StringConstraints(min_length=1, max_length=TEXT_MAX)]


class SheetImage(_Strict):
    type: Literal["image"]
    url: Annotated[str, StringConstraints(max_length=2000, pattern=_HTTPS)]
    alt: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    credit: Annotated[str, StringConstraints(max_length=120)] | None = Field(default=None, exclude_if=_absent)


class SheetLink(_Strict):
    type: Literal["link"]
    url: Annotated[str, StringConstraints(max_length=2000, pattern=_HTTPS)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    host: Annotated[str, StringConstraints(min_length=1, max_length=255)]


class SheetList(_Strict):
    type: Literal["list"]
    items: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=200)]],
        Field(min_length=1, max_length=8),
    ]


class SheetFact(_Strict):
    type: Literal["fact"]
    label: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=120)]


class SheetSection(_Strict):
    """A heading that opens a part of the sheet."""

    type: Literal["section"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]


Cell = Annotated[str, StringConstraints(max_length=80)] | int | float


class SheetTable(_Strict):
    """Columns (≤6) and rows (≤20) of text or numbers, each row as long as the columns."""

    type: Literal["table"]
    columns: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=40)]],
        Field(min_length=1, max_length=TABLE_COLUMNS),
    ]
    rows: Annotated[
        list[Annotated[list[Cell], Field(min_length=1, max_length=TABLE_COLUMNS)]],
        Field(min_length=1, max_length=TABLE_ROWS),
    ]

    @model_validator(mode="after")
    def _rows_fit(self) -> "SheetTable":
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("every row has as many cells as there are columns")
        return self


class SheetStat(_Strict):
    """One number that matters, with its label, unit and trend."""

    type: Literal["stat"]
    label: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    unit: Annotated[str, StringConstraints(max_length=16)] | None = Field(default=None, exclude_if=_absent)
    trend: Literal["up", "down", "flat"] | None = Field(default=None, exclude_if=_absent)


class SheetChips(_Strict):
    """Short tags (≤12)."""

    type: Literal["chips"]
    items: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=40)]],
        Field(min_length=1, max_length=CHIPS),
    ]


class SheetDivider(_Strict):
    type: Literal["divider"]


HANDOFF_URL = r"^(https://\S+|tel:\+[1-9][0-9]{5,14}|sms:(\+[1-9][0-9]{5,14})?&body=[A-Za-z0-9%._~-]*)$"
HANDOFF_NOTES = 4


class SheetHandoff(_Strict):
    """What Pisa prepared, behind one button: the phone opens `url` (or dials `phone`) in the official app
    and tells Pisa `handoff_opened`. Pisa builds the URL from its own templates (pisa/domain/handoffs.py)."""

    type: Literal["handoff"]
    handoff_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")]
    kind: Literal["open_url", "call", "message", "map"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    subtitle: Annotated[str, StringConstraints(max_length=120)] | None = Field(
        default=None, exclude_if=_absent
    )
    url: Annotated[str, StringConstraints(max_length=2000, pattern=HANDOFF_URL)] | None = Field(
        default=None, exclude_if=_absent
    )
    phone: Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{5,14}$")] | None = Field(
        default=None, exclude_if=_absent
    )
    text: Annotated[str, StringConstraints(min_length=1, max_length=HANDOFF_TEXT)] | None = Field(
        default=None, exclude_if=_absent
    )
    notes: (
        Annotated[
            list[Annotated[str, StringConstraints(min_length=1, max_length=200)]],
            Field(min_length=1, max_length=HANDOFF_NOTES),
        ]
        | None
    ) = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _opens_something(self) -> "SheetHandoff":
        if self.kind == "call" and self.phone is None and not (self.url or "").startswith("tel:"):
            raise ValueError("a call handoff needs a phone or a tel: url")
        if self.kind != "call" and self.url is None:
            raise ValueError(f"a {self.kind} handoff needs a url")
        if (self.url or "").startswith("tel:") and self.phone is not None and self.url != f"tel:{self.phone}":
            raise ValueError("a call's tel: url and its phone are the same number")
        return self


# the drawing blocks. Ids are the element ids a tap names (`object_event asked`).
DRAWINGS = 2  # drawing blocks a sheet
DRAWING_TYPES = frozenset({"diagram", "chart", "timeline"})
NODES, EDGES, GROUPS = (2, 24), 40, 6
SERIES, POINTS, PIE_POINTS = 4, 60, 8
ITEMS = 16  # a timeline's
NODE_LABEL, NODE_DETAIL, EDGE_LABEL, GROUP_LABEL = 40, 200, 30, 30
_NODE_ID = r"^n([1-9]|1[0-9]|2[0-4])$"
_GROUP_ID = r"^g[1-6]$"


class Box(_Strict):
    """A rectangle in points at scale 1, its origin top left."""

    x: float
    y: float
    w: float
    h: float


class EdgePath(_Strict):
    points: Annotated[list[Annotated[list[float], Field(min_length=2, max_length=2)]], Field(min_length=2)]


class DiagramLayout(_Strict):
    """Written by CORE (`pisa.domain.layout`), never the model. `edges` in the block's `edges` order."""

    width: float
    height: float
    nodes: dict[str, Box]
    edges: list[EdgePath]
    groups: dict[str, Box] = Field(default_factory=dict)


class DiagramNode(_Strict):
    id: Annotated[str, StringConstraints(pattern=_NODE_ID)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=NODE_LABEL)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=NODE_DETAIL)] | None = Field(
        default=None, exclude_if=_absent
    )
    group: Annotated[str, StringConstraints(pattern=_GROUP_ID)] | None = Field(
        default=None, exclude_if=_absent
    )
    shape: Literal["box", "store", "actor", "note"]


class DiagramEdge(_Strict):
    model_config = ConfigDict(
        strict=True, extra="forbid", serialize_by_alias=True, validate_by_alias=True, validate_by_name=True
    )

    from_: Annotated[str, StringConstraints(pattern=_NODE_ID)] = Field(alias="from")
    to: Annotated[str, StringConstraints(pattern=_NODE_ID)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=EDGE_LABEL)] | None = Field(
        default=None, exclude_if=_absent
    )
    style: Literal["solid", "dashed"]
    arrow: Literal["end", "both", "none"]


class DiagramGroup(_Strict):
    id: Annotated[str, StringConstraints(pattern=_GROUP_ID)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=GROUP_LABEL)]


class SheetDiagram(_Strict):
    """Boxes and arrows (flow, graph, or layers as stacked bands), laid out by CORE. `scroll`: wider than
    a 390 pt phone at the minimum scale: the phone pans it."""

    type: Literal["diagram"]
    style: Literal["flow", "graph", "layers"]
    direction: Literal["down", "right"]
    nodes: Annotated[list[DiagramNode], Field(min_length=NODES[0], max_length=NODES[1])]
    edges: Annotated[list[DiagramEdge], Field(max_length=EDGES)]
    groups: Annotated[list[DiagramGroup], Field(max_length=GROUPS)] | None = Field(
        default=None, exclude_if=_absent
    )
    layout: DiagramLayout
    scroll: bool | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _resolves(self) -> "SheetDiagram":
        nodes = [n.id for n in self.nodes]
        groups = [g.id for g in self.groups or []]
        if len(set(nodes)) != len(nodes) or len(set(groups)) != len(groups):
            raise ValueError("a diagram's ids are unique")
        if any(e.from_ not in nodes or e.to not in nodes for e in self.edges):
            raise ValueError("every edge goes from a node to a node of the diagram")
        if any(n.group is not None and n.group not in groups for n in self.nodes):
            raise ValueError("every node's group is one of the diagram's groups")
        layout = self.layout
        if set(layout.nodes) != set(nodes) or len(layout.edges) != len(self.edges):
            raise ValueError("the layout places every node and every edge, and nothing else")
        if not set(layout.groups) <= set(groups):
            raise ValueError("the layout's groups are the diagram's")
        return self


class ChartAxisX(_Strict):
    label: Annotated[str, StringConstraints(min_length=1, max_length=30)]
    kind: Literal["category", "time", "number"]


class ChartAxisY(_Strict):
    label: Annotated[str, StringConstraints(min_length=1, max_length=30)]
    unit: Annotated[str, StringConstraints(min_length=1, max_length=10)] | None = Field(
        default=None, exclude_if=_absent
    )


class ChartPoint(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^p([1-9]|[1-5][0-9]|60)$")]
    x: Annotated[str, StringConstraints(min_length=1, max_length=40)] | int | float
    y: int | float


class ChartSeries(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^s[1-4]$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=30)]
    points: Annotated[list[ChartPoint], Field(min_length=1, max_length=POINTS)]


_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?)?$")


def iso(value: str) -> str | None:
    """The one ISO form, the one the phone reads: YYYY-MM-DD, YYYY-MM-DDTHH:MM[:SS], with an
    optional Z or ±HH:MM (seconds added before an offset, fractions dropped); None for anything else
    (weeks, ordinals, the basic form, an hour alone)."""
    m = _ISO.match(value.strip())
    if m is None:
        return None
    day, hm, sec, zone = m.groups()
    seconds = f":{sec}" if sec else ":00" if zone else ""  # the phone reads an offset only after seconds
    out = day if hm is None else f"{day}T{hm}{seconds}{zone or ''}"
    if (iso_moment_or_none(out) if "T" in out else iso_day_or_none(out)) is None:
        return None
    return out


def moment(value: str) -> datetime:
    """An ISO value as a naive moment to sort by (an offset turned to UTC; a date at its midnight)."""
    at = datetime.fromisoformat(value) if "T" in value else datetime.fromisoformat(value + "T00:00:00")
    return at.astimezone(UTC).replace(tzinfo=None) if at.tzinfo is not None else at


def is_iso(value: str) -> bool:
    return iso(value) == value


class SheetChart(_Strict):
    """Numbers drawn (Swift Charts). Every number traces to this turn's tool results."""

    type: Literal["chart"]
    mark: Literal["bar", "line", "area", "pie", "point"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = Field(
        default=None, exclude_if=_absent
    )
    x: ChartAxisX
    y: ChartAxisY
    series: Annotated[list[ChartSeries], Field(min_length=1, max_length=SERIES)]

    @model_validator(mode="after")
    def _fits(self) -> "SheetChart":
        if len({s.id for s in self.series}) != len(self.series):
            raise ValueError("a chart's series ids are unique")
        for s in self.series:
            if len({p.id for p in s.points}) != len(s.points):
                raise ValueError("a series' point ids are unique")
            for p in s.points:
                if isinstance(p.y, float) and p.y != p.y:
                    raise ValueError("a point's y is a number")
                if self.x.kind == "time" and not (isinstance(p.x, str) and is_iso(p.x)):
                    raise ValueError("a time axis' x values are ISO dates or date-times")
                if self.x.kind == "number" and not isinstance(p.x, int | float):
                    raise ValueError("a number axis' x values are numbers")
        if self.mark == "pie" and any(p.y < 0 for p in self.series[0].points):
            raise ValueError("a pie's slices are never negative")
        if self.mark == "pie" and (len(self.series) != 1 or len(self.series[0].points) > PIE_POINTS):
            raise ValueError(f"a pie has one series of at most {PIE_POINTS} points")
        return self


class TimelineItem(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^t([1-9]|1[0-6])$")]
    at: Annotated[str, StringConstraints(min_length=10, max_length=40)]
    end: Annotated[str, StringConstraints(min_length=10, max_length=40)] | None = Field(
        default=None, exclude_if=_absent
    )
    label: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = Field(
        default=None, exclude_if=_absent
    )


class SheetTimeline(_Strict):
    """Dated items on a rail, sorted by `at`."""

    type: Literal["timeline"]
    items: Annotated[list[TimelineItem], Field(min_length=1, max_length=ITEMS)]

    @model_validator(mode="after")
    def _sorted(self) -> "SheetTimeline":
        if len({i.id for i in self.items}) != len(self.items):
            raise ValueError("a timeline's ids are unique")
        if not all(is_iso(i.at) and (i.end is None or is_iso(i.end)) for i in self.items):
            raise ValueError("a timeline's at and end are ISO dates or date-times")
        ats = [moment(i.at) for i in self.items]
        if ats != sorted(ats):
            raise ValueError("a timeline's items are sorted by at")
        if any(i.end is not None and moment(i.end) < moment(i.at) for i in self.items):
            raise ValueError("a timeline item's end is never before its at")
        return self


# the answer blocks. Every value is code's,
# from this turn's tool results; a value the turn didn't see is absent. A phone that doesn't
# draw a type (Ring 1 in its hello: `PHONE_BLOCKS`) gets the same answer as tables, lists and links
# (`pisa.domain.visual.answers_for_phone`).
ANSWER_TYPES = frozenset({"findings", "map", "product"})
PHONE_BLOCKS = DRAWING_TYPES | ANSWER_TYPES  # the block types a phone may list in its hello
FINDING_ROWS = 10  # the research table's ROWS_MAX
RECOGNITION_MAX = 4
CITED_MAX = 3
PROS_MAX = 3
SLOT_TIMES = 8
EXCLUDED_MAX = 6
PINS_MAX = 12
PRODUCTS_MAX = 6
_ROW_ID = r"^r([1-9]|10)$"
_PRODUCT_ID = r"^p[1-6]$"
_SOURCE = r"^(map|https://\S+)$"
_DAY = r"^\d{4}-\d{2}-\d{2}$"
_CLOCK = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
Https = Annotated[str, StringConstraints(max_length=2000, pattern=_HTTPS)]


class OpenAtAsked(_Strict):
    """Open at the time they asked about (else now): `ok` null when no source said."""

    ok: bool | None
    hours: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = Field(
        default=None, exclude_if=_absent
    )
    source: Annotated[str, StringConstraints(max_length=2000, pattern=_SOURCE)]


class Recognition(_Strict):
    """A guide's award, from a guide's page read this turn that names the place (its url)."""

    guide: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    award: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    year: Annotated[int, Field(ge=1900, le=2100)] | None = Field(default=None, exclude_if=_absent)
    url: Https


class Slots(_Strict):
    """Read-only availability (booking stays an approval with Face ID; a slot is never a button)."""

    day: Annotated[str, StringConstraints(pattern=_DAY)]
    times: Annotated[
        list[Annotated[str, StringConstraints(pattern=_CLOCK)]], Field(min_length=1, max_length=SLOT_TIMES)
    ]
    url: Https
    source: Https

    @model_validator(mode="after")
    def _sorted(self) -> "Slots":
        if self.times != sorted(set(self.times)):
            raise ValueError("a slot's times are sorted and distinct")
        return self


Short = Annotated[str, StringConstraints(min_length=1, max_length=40)]
Point = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class FindingRow(_Strict):
    id: Annotated[str, StringConstraints(pattern=_ROW_ID)]
    kind: Literal["place", "event"]
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    place_id: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = Field(
        default=None, exclude_if=_absent
    )
    pick: Literal[True] | None = Field(default=None, exclude_if=_absent)
    why: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = Field(
        default=None, exclude_if=_absent
    )
    walk: Short | None = Field(default=None, exclude_if=_absent)
    rating: Short | None = Field(default=None, exclude_if=_absent)
    price: Short | None = Field(default=None, exclude_if=_absent)
    open_at_asked: OpenAtAsked | None = Field(default=None, exclude_if=_absent)
    recognition: Annotated[list[Recognition], Field(min_length=1, max_length=RECOGNITION_MAX)] | None = Field(
        default=None, exclude_if=_absent
    )
    cited_by: (
        Annotated[
            list[Annotated[str, StringConstraints(min_length=1, max_length=255)]],
            Field(min_length=1, max_length=CITED_MAX),
        ]
        | None
    ) = Field(default=None, exclude_if=_absent)
    lat: Annotated[float, Field(ge=-90, le=90)] | None = Field(default=None, exclude_if=_absent)
    lon: Annotated[float, Field(ge=-180, le=180)] | None = Field(default=None, exclude_if=_absent)
    pros: Annotated[list[Point], Field(min_length=1, max_length=PROS_MAX)] | None = Field(
        default=None, exclude_if=_absent
    )
    cons: Annotated[list[Point], Field(min_length=1, max_length=PROS_MAX)] | None = Field(
        default=None, exclude_if=_absent
    )
    slots: Slots | None = Field(default=None, exclude_if=_absent)
    at: Annotated[str, StringConstraints(min_length=10, max_length=40)] | None = Field(
        default=None, exclude_if=_absent
    )
    venue: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = Field(
        default=None, exclude_if=_absent
    )
    age: Short | None = Field(default=None, exclude_if=_absent)
    url: Https | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def _fits(self) -> "FindingRow":
        if (self.lat is None) != (self.lon is None):
            raise ValueError("a row's lat and lon come together")
        if self.at is not None and not is_iso(self.at):
            raise ValueError("a row's at is an ISO date-time")
        if self.kind == "place" and (self.at or self.venue or self.age):
            raise ValueError("at, venue and age are an event's")
        return self


class SheetFindings(_Strict):
    """The candidates as rows, each with its card (the pick first)."""

    type: Literal["findings"]
    rows: Annotated[list[FindingRow], Field(min_length=1, max_length=FINDING_ROWS)]

    @model_validator(mode="after")
    def _rows(self) -> "SheetFindings":
        if len({r.id for r in self.rows}) != len(self.rows):
            raise ValueError("a findings block's row ids are unique")
        picks = [i for i, r in enumerate(self.rows) if r.pick]
        if picks not in ([], [0]):
            raise ValueError("one pick at most, and it is the first row")
        return self


class Pin(_Strict):
    id: Annotated[str, StringConstraints(pattern=r"^(r([1-9]|10)|p[1-6])$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    badge: Short | None = Field(default=None, exclude_if=_absent)
    pick: Literal[True] | None = Field(default=None, exclude_if=_absent)


class SheetMap(_Strict):
    """Every candidate pinned; a tap on a pin opens its row's card."""

    type: Literal["map"]
    pins: Annotated[list[Pin], Field(min_length=1, max_length=PINS_MAX)]

    @model_validator(mode="after")
    def _unique(self) -> "SheetMap":
        if len({p.id for p in self.pins}) != len(self.pins):
            raise ValueError("a map's pin ids are unique")
        return self


class SheetProduct(_Strict):
    """One offer, every number from this turn's comparator or shop pages. It never
    buys."""

    type: Literal["product"]
    id: Annotated[str, StringConstraints(pattern=_PRODUCT_ID)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    image_url: Https | None = Field(default=None, exclude_if=_absent)
    price: Annotated[float, Field(ge=0)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    shop: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    shipping: Annotated[float, Field(ge=0)] | None = Field(default=None, exclude_if=_absent)
    low_6m: Annotated[float, Field(ge=0)] | None = Field(default=None, exclude_if=_absent)
    url: Https
    source: Https


class Excluded(_Strict):
    """A finalist dropped for a hard requirement checked against a source ("chiuso il martedì")."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    why: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    source: Annotated[str, StringConstraints(max_length=2000, pattern=_SOURCE)]


SheetBlock = Annotated[
    SheetText
    | SheetImage
    | SheetLink
    | SheetList
    | SheetFact
    | SheetSection
    | SheetTable
    | SheetStat
    | SheetChips
    | SheetDivider
    | SheetHandoff
    | SheetDiagram
    | SheetChart
    | SheetTimeline
    | SheetFindings
    | SheetMap
    | SheetProduct,
    Field(discriminator="type"),
]


class SheetData(_Strict):
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    blocks: Annotated[list[SheetBlock], Field(min_length=1, max_length=SHEET_BLOCKS)]
    sources: Annotated[list[SheetSource], Field(min_length=1, max_length=6)] | None = Field(
        default=None, exclude_if=_absent
    )
    # set by code. Absent: tools. `knowledge`: a diagram from Pisa's own knowledge ("Da quello
    # che so"); `mixed`: such a diagram beside traced data. A chart is never knowledge.
    basis: Literal["tools", "knowledge", "mixed"] | None = Field(default=None, exclude_if=_absent)
    lang: locale.Tag | None = Field(default=None, exclude_if=_absent)
    # the finalists dropped for a hard requirement, with a findings block only
    excluded: Annotated[list[Excluded], Field(min_length=1, max_length=EXCLUDED_MAX)] | None = Field(
        default=None, exclude_if=_absent
    )

    @model_validator(mode="after")
    def _drawings(self) -> "SheetData":
        if sum(b.type in DRAWING_TYPES for b in self.blocks) > DRAWINGS:
            raise ValueError(f"at most {DRAWINGS} drawing blocks (diagram, chart, timeline) a sheet")
        if self.basis == "knowledge" and any(b.type == "chart" for b in self.blocks):
            raise ValueError("a chart is never Pisa's knowledge: its numbers come from this turn's tools")
        self._answers()
        return self

    def _answers(self) -> None:
        """One findings block, one map, at most six products; every pin names a row or a
        product of this sheet; `excluded` only beside a findings block."""
        kinds = [b.type for b in self.blocks]
        if kinds.count("findings") > 1 or kinds.count("map") > 1:
            raise ValueError("at most one findings block and one map a sheet")
        products = [b for b in self.blocks if isinstance(b, SheetProduct)]
        if len(products) > PRODUCTS_MAX:
            raise ValueError(f"at most {PRODUCTS_MAX} product blocks a sheet")
        if len({p.id for p in products}) != len(products):
            raise ValueError("a sheet's product ids are unique")
        ids = {p.id for p in products} | {
            r.id for b in self.blocks if isinstance(b, SheetFindings) for r in b.rows
        }
        for b in self.blocks:
            if isinstance(b, SheetMap) and any(p.id not in ids for p in b.pins):
                raise ValueError("every pin names a row or a product of this sheet")
        if self.excluded is not None and "findings" not in kinds:
            raise ValueError("excluded goes with a findings block")


SHEET_ACTIONS = 8


class SheetAction(_Strict):
    """A button on a ritual's sheet (the Ghost Run's one yes, Chiudiamo's Undo per line). Its effect is
    Pisa's, stored on the server; the phone posts its id (`POST /v1/sheets/{id}/actions/{action_id}`)."""

    id: Annotated[str, StringConstraints(pattern=r"^a[1-8]$")]
    label: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    style: Literal["primary", "plain", "undo"]


class RitualSheetData(SheetData):
    """A ritual's sheet: a sheet with Pisa's own buttons. Only code makes them, never the model (a
    composed sheet is `SheetData`, with no actions)."""

    actions: Annotated[list[SheetAction], Field(min_length=1, max_length=SHEET_ACTIONS)] | None = Field(
        default=None, exclude_if=_absent
    )
