"""Pisa's presentation tools: the model decides what to show on the
user's phone, when, and in what focus, and says why.

- Always loaded, in one fixed order (the cached prefix), whatever the device: a device that
  can't show a kind answers `unsupported` and the model says it in words.
- Showing never changes data: no replan, no setting, no approval. Each show is a `show` move in Pisa's
  decision log, its `outcome` the device's acknowledgment, later the user's `object_event`s (seen, chosen…).
- `ref` for what Pisa holds (the plan, an item, the changes, a settings page, a place): the device reads it by
  REST, so the object is the truth. `data` only for what the turn computed (places, compare rows, a note, a
  choice); a compare cell comes only from this turn's tool results, an unchecked one is unknown.
- The device acknowledges within ACK_WAIT_S or the tool goes on as `sent_unconfirmed`. A device without
  the object kinds (no Ring 1 in its hello) gets today's `card` messages (plan, place, places, note) or
  `unsupported`.
"""

import math
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from pisa.brain.place_tools import PlaceCard
from pisa.brain.registry import offer
from pisa.brain.spoken import TERMINAL, Round, presented
from pisa.brain.untrusted import current
from pisa.domain import coverage, decisions, visual
from pisa.domain import layout as layouts
from pisa.domain.sheet import (
    CHIPS,
    DRAWING_TYPES,
    DRAWINGS,
    EDGE_LABEL,
    EDGES,
    GROUP_LABEL,
    GROUPS,
    ITEM_ID,
    ITEMS,
    NODE_DETAIL,
    NODE_LABEL,
    NODES,
    PIE_POINTS,
    POINTS,
    SERIES,
    SHEET_BLOCKS,
    SHEET_IMAGES,
    TABLE_COLUMNS,
    TABLE_ROWS,
    TEXT_MAX,
    SheetData,
    iso,
    moment,
)  # fmt: skip
from pisa.domain.tasks import day_named

Focus = Literal["take", "offer", "quiet"]
Status = Literal["shown", "queued", "deferred", "unavailable", "unsupported", "sent_unconfirmed"]
Section = Literal[
    "briefing", "reminders_quiet", "places_modes", "voice", "companion", "planner", "models", "spend",
    "keys_devices", "connectors", "about_you", "low_priority",
]  # fmt: skip

__all__ = [
    "TERMINAL",
    "Ack",
    "Device",
    "Presenter",
    "Shown",
    "add_present_tools",
    "needs_words",
]
# FACT: the device answers `presented` at once (~200 ms on the tailnet); past this Pisa goes on
ACK_WAIT_S = 0.8
NOTE_LINES = 6


class NodeIn(BaseModel):
    id: str = Field(description="your own short id, for the edges")
    label: str = Field(description="≤ 40 characters")
    detail: str | None = Field(default=None, description="≤ 200, shown on a long press")
    group: str | None = None
    shape: Literal["box", "store", "actor", "note"] = "box"


class EdgeIn(BaseModel):
    model_config = ConfigDict(validate_by_alias=True, validate_by_name=True, serialize_by_alias=True)

    from_: str = Field(alias="from")
    to: str
    label: str | None = Field(default=None, description="≤ 30 characters")
    style: Literal["solid", "dashed"] = "solid"
    arrow: Literal["end", "both", "none"] = "end"


class GroupIn(BaseModel):
    id: str
    label: str


class AxisIn(BaseModel):
    label: str
    kind: Literal["category", "time", "number"] | None = Field(default=None, description="x only")
    unit: str | None = Field(default=None, description="y only")


class PointIn(BaseModel):
    x: str | int | float
    y: int | float


class SeriesIn(BaseModel):
    name: str
    points: list[PointIn]
    derive: Literal["sum", "mean", "delta", "share"] | None = None


class ItemIn(BaseModel):
    at: str = Field(description="ISO date or date-time")
    end: str | None = None
    label: str
    detail: str | None = None


class SheetBlockIn(BaseModel):
    """One block of a sheet, as the model writes it; code keeps what it may show, numbers the
    elements, and lays the diagram out (a `layout` written here is ignored)."""

    type: Literal[
        "text", "image", "link", "list", "fact", "section", "table", "stat", "chips", "divider",
        "diagram", "chart", "timeline",
    ]  # fmt: skip
    text: str | None = None
    url: str | None = None
    alt: str | None = None
    credit: str | None = None
    title: str | None = None
    items: list[str | ItemIn] | None = None
    label: str | None = None
    value: str | int | float | None = None
    unit: str | None = None
    trend: Literal["up", "down", "flat"] | None = None
    columns: list[str] | None = None
    rows: list[list[str | int | float]] | None = None
    style: Literal["flow", "graph", "layers"] | None = None
    direction: Literal["down", "right"] | None = None
    nodes: list[NodeIn] | None = None
    edges: list[EdgeIn] | None = None
    groups: list[GroupIn] | None = None
    mark: Literal["bar", "line", "area", "pie", "point"] | None = None
    x: AxisIn | None = None
    y: AxisIn | None = None
    series: list[SeriesIn] | None = None
    derive: Literal["sum", "mean", "delta", "share"] | None = None
    of: list[str] | None = None


def _host(url: str) -> str:
    return re.sub(r"^www\.", "", url.split("://", 1)[-1].split("/", 1)[0].lower())


def _line(value: Any, limit: int) -> str:
    """A value as one line of plain text (values are text, never markup)."""
    return " ".join(str(value).split())[:limit]


def _table_cell(value: Any) -> str | int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _line(value, 80)
    if isinstance(value, float) and not math.isfinite(value):
        return ""  # NaN or infinity is no number (and no JSON)
    return value


_URL = re.compile(r"https://[^\s<>\"'()\[\]{}]+")


def sheet_blocks(
    blocks: list[SheetBlockIn] | list[dict[str, Any]],
    read: list[dict[str, str]],
    texts: list[str] | None = None,
    *,
    results: list[str],
    lang: str,
    phone: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """RAIL: the blocks Pisa may show. An image or a link only over https and from a host
    this turn read (its pages' own images; a place's photos come with show_places), at most SHEET_IMAGES
    images; the rest as written, cut to the protocol's lengths; at most SHEET_BLOCKS. (blocks, what was left
    out and why).

    an exact address, never a host's: a link is a page read this turn, an image an
    address written in such a page's own text (`texts`): the phone fetches an image at once, so a query the
    model made up (the user's data in it, for a page that asked) never leaves.

    every number shown (a chart's points, a stat, a table's numeric
    cells) traces to `results`, this turn's tool results, or is a declared `derive` CORE computes from traced
    ones: an untraced point drops its series, an untraced stat or cell its block. A diagram is laid out here
    (it must show every label whole), at most DRAWINGS drawings, nothing approval-like."""
    corpus = visual.Corpus(results)
    raw_blocks = [
        b.model_dump(exclude_none=True, by_alias=True) if isinstance(b, BaseModel) else dict(b)
        for b in blocks
    ]
    traced = _traced_points(raw_blocks, corpus)
    drawings = 0
    exact = {s["url"] for s in read if s.get("url")}
    # the addresses the pages write, whole (never a substring of one)
    written = {m.rstrip(".,;:!?") for text in texts or [] for m in _URL.findall(text)}
    out: list[dict[str, Any]] = []
    dropped: list[str] = []
    images = 0
    for b in raw_blocks:
        kind = b.get("type")
        if len(out) >= SHEET_BLOCKS:
            dropped.append(f"blocks past the {SHEET_BLOCKS}th")
            break
        if kind in DRAWING_TYPES:
            if drawings >= DRAWINGS:
                dropped.append(f"a {kind} past the {DRAWINGS}nd drawing")
                continue
            drawn, why = _drawing(b, corpus, phone)
            dropped += why
            if drawn is not None:
                drawings += 1
                out.append(drawn)
            continue
        if kind == "text" and str(b.get("text") or "").strip():
            out.append({"type": "text", "text": _line(b["text"], TEXT_MAX)})
        elif kind in ("image", "link"):
            url = str(b.get("url") or "").strip()
            found = url in exact or (kind == "image" and url in written)
            if not url.startswith("https://") or not found:
                dropped.append(f"{kind} {url[:60]!r}: not from a page read this turn (its exact address)")
                continue
            if kind == "image":
                if images >= SHEET_IMAGES:
                    dropped.append("an image past the sixth")
                    continue
                images += 1
                image: dict[str, Any] = {
                    "type": "image",
                    "url": url,
                    "alt": _line(b.get("alt") or "", 200) or "immagine",
                }
                if b.get("credit"):
                    image["credit"] = _line(b["credit"], 120)
                out.append(image)
            else:
                title = _line(b.get("title") or _host(url), 120)
                out.append({"type": "link", "url": url, "title": title, "host": _host(url)})
        elif kind in ("list", "chips") and b.get("items"):
            limit, size = (8, 200) if kind == "list" else (CHIPS, 40)
            items = [_line(i, size) for i in b["items"] if str(i).strip()][:limit]
            if items:
                out.append({"type": kind, "items": items})
            else:
                dropped.append(f"an empty {kind}")
        elif kind == "fact" and b.get("label") and str(b.get("value") or "").strip():
            out.append({"type": "fact", "label": _line(b["label"], 40), "value": _line(b["value"], 120)})
        elif kind == "section" and str(b.get("title") or "").strip():
            out.append({"type": "section", "title": _line(b["title"], 120)})
        elif kind == "stat" and b.get("label") and b.get("derive"):
            values = [traced.get(ref) for ref in b.get("of") or []]
            result = visual.derive(str(b["derive"]), [v for v in values if v is not None])
            if result is None or None in values or not values:
                dropped.append(
                    f"stat {_line(b['label'], 40)!r}: its derive needs traced chart points (s1/p1…)"
                )
                continue
            stat: dict[str, Any] = {
                "type": "stat", "label": _line(b["label"], 40), "value": visual.written(result, lang)
            }  # fmt: skip
            if b.get("unit"):
                stat["unit"] = _line(b["unit"], 16)
            if b.get("trend") in ("up", "down", "flat"):
                stat["trend"] = b["trend"]
            out.append(stat)
        elif (
            kind == "stat"
            and b.get("label")
            and str(b.get("value") if b.get("value") is not None else "").strip()
        ):
            if not visual.traces(b["value"], corpus):
                dropped.append(f"stat {_line(b['label'], 40)!r}: its number isn't in this turn's results")
                continue
            stat = {
                "type": "stat",
                "label": _line(b["label"], 40),
                "value": _line(b["value"], 40),
            }
            if b.get("unit"):
                stat["unit"] = _line(b["unit"], 16)
            if b.get("trend") in ("up", "down", "flat"):
                stat["trend"] = b["trend"]
            out.append(stat)
        elif kind == "table" and b.get("columns") and b.get("rows"):
            columns = [_line(c, 40) or "-" for c in b["columns"]][:TABLE_COLUMNS]
            rows = [
                [_table_cell(c) for c in row][: len(columns)]
                for row in b["rows"][:TABLE_ROWS]
                if isinstance(row, list)
            ]
            rows = [
                row + [""] * (len(columns) - len(row)) for row in rows if any(str(c).strip() for c in row)
            ]

            def unsourced(c: Any) -> bool:
                return bool(
                    ((isinstance(c, int | float) and not isinstance(c, bool)) or visual.numeric_text(c))
                    and not visual.traces(c, corpus)
                ) or (  # a quantity inside a cell's text ("circa 500 €") too
                    isinstance(c, str)
                    and not visual.numeric_text(c)
                    and not visual.quantities_trace(c, corpus)
                )

            # a cell with no source stays empty; the rest of the table is still their answer
            numbered = sum(
                1
                for row in rows
                for c in row
                if (isinstance(c, int | float) and not isinstance(c, bool))
                or (isinstance(c, str) and any(ch.isdigit() for ch in c))
            )
            untraced = sum(1 for row in rows for c in row if unsourced(c))
            rows = [["" if unsourced(c) else c for c in row] for row in rows]
            rows = [row for row in rows if any(str(c).strip() for c in row)]
            if numbered and untraced >= numbered:  # not one of its numbers traced: no table, only labels left
                dropped.append(f"a table: {untraced} number(s) not in this turn's results")
                rows = []
            elif untraced:
                dropped.append(f"a table: {untraced} cell(s) emptied, not in this turn's results")
            if not rows and not (numbered and untraced >= numbered):
                dropped.append("an empty table")
            if rows:
                out.append({"type": "table", "columns": columns, "rows": rows})
        elif kind == "divider":
            if out and out[-1]["type"] != "divider":
                out.append({"type": "divider"})
        else:
            dropped.append(f"an empty {kind}")
    while out and out[-1]["type"] == "divider":
        out.pop()
    return out, dropped


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _traced_points(blocks: list[dict[str, Any]], corpus: visual.Corpus) -> dict[str, float]:
    """The charts' points whose y this turn's results carry, as "s<k>/p<j>" (series k, point j, by position,
    as the sheet numbers them): what a stat's `derive` may name. The first chart's, when two name one."""
    out: dict[str, float] = {}
    for b in blocks:
        if b.get("type") != "chart":
            continue
        for k, series in enumerate(b.get("series") or [], 1):
            for j, point in enumerate(series.get("points") or [], 1):
                y = point.get("y") if isinstance(point, dict) else None
                if _finite(y) and visual.traces(y, corpus):
                    out.setdefault(f"s{k}/p{j}", float(y))  # type: ignore[arg-type]
    return out


def _drawing(
    b: dict[str, Any], corpus: visual.Corpus, phone: float | None = None
) -> tuple[dict[str, Any] | None, list[str]]:
    """One drawing block as Pisa may show it, and what was left out of it and why; `phone`: the width a
    diagram is laid out for (the hello's `surface.width`)."""
    kind = b["type"]
    if kind == "diagram":
        return _diagram(b, corpus, phone)
    if kind == "chart":
        return _chart(b, corpus)
    return _timeline(b, corpus)


def _diagram(
    b: dict[str, Any], corpus: visual.Corpus, phone: float | None = None
) -> tuple[dict[str, Any] | None, list[str]]:
    """Ids numbered n1…, g1… by position (the model's own ids only link edges to nodes); a node labelled like
    an approval goes, with its edges; edges to nowhere and loops go; laid out by code."""
    left: list[str] = []
    groups: list[dict[str, Any]] = []
    group_ids: dict[str, str] = {}
    for g in (b.get("groups") or [])[:GROUPS]:
        label = _line(g.get("label") or "", GROUP_LABEL)
        if not label or visual.approval_like(label):
            left.append("a diagram group whose label reads like an approval" if label else "an unnamed group")
            continue
        gid = f"g{len(groups) + 1}"
        group_ids[str(g.get("id"))] = gid
        groups.append({"id": gid, "label": label})
    nodes: list[dict[str, Any]] = []
    node_ids: dict[str, str] = {}
    for n in b.get("nodes") or []:
        label = _line(n.get("label") or "", NODE_LABEL)
        if not label:
            continue
        if visual.approval_like(label) or visual.approval_like(n.get("detail")):
            left.append("a diagram node that reads like an approval or a payment")
            continue
        if not visual.quantities_trace(f"{label} {n.get('detail') or ''}", corpus):
            left.append("a diagram node with a number this turn's results don't carry")
            continue
        if len(nodes) >= NODES[1]:
            left.append(f"diagram nodes past the {NODES[1]}th")
            break
        nid = f"n{len(nodes) + 1}"
        node_ids.setdefault(str(n.get("id")), nid)
        node: dict[str, Any] = {"id": nid, "label": label, "shape": n.get("shape") or "box"}
        if n.get("detail") and str(n["detail"]).strip():
            node["detail"] = _line(n["detail"], NODE_DETAIL)
        if (group := group_ids.get(str(n.get("group")))) is not None:
            node["group"] = group
        nodes.append(node)
    if len(nodes) < NODES[0]:
        return None, [*left, "a diagram with fewer than 2 nodes"]
    edges: list[dict[str, Any]] = []
    unlinked = 0
    for e in b.get("edges") or []:
        a, z = node_ids.get(str(e.get("from"))), node_ids.get(str(e.get("to")))
        if a is None or z is None or a == z:
            unlinked += 1
            continue
        if len(edges) >= EDGES:
            left.append(f"diagram edges past the {EDGES}th")
            break
        edge: dict[str, Any] = {"from": a, "to": z, "style": e.get("style") or "solid",
                                "arrow": e.get("arrow") or "end"}  # fmt: skip
        label = _line(e.get("label") or "", EDGE_LABEL)
        if label and not visual.approval_like(label):
            edge["label"] = label
        elif label:
            left.append("an edge label that reads like an approval")
        edges.append(edge)
    if unlinked:
        left.append(f"{unlinked} diagram edge(s) to a node it doesn't have")
    used = {n.get("group") for n in nodes}
    diagram: dict[str, Any] = {
        "type": "diagram", "style": b.get("style") or "flow", "direction": b.get("direction") or "down",
        "nodes": nodes, "edges": edges,
    }  # fmt: skip
    if kept := [g for g in groups if g["id"] in used]:
        diagram["groups"] = kept
    try:  # the direction follows the phone's width (top to bottom on a portrait phone)
        diagram["layout"], diagram["direction"] = layouts.fitted(diagram, phone)
    except layouts.Uncut as err:
        return None, [*left, f"a diagram: {err}"]
    if not layouts.fits_phone(diagram["layout"]["width"], layouts.phone_width(phone)):
        diagram["scroll"] = True  # the render check: too wide at the minimum scale: the phone pans it
    return diagram, left


def _plain(label: Any, default: str) -> str:
    text = _line(label or default, 30) or default
    return default if visual.approval_like(text) else text


def _chart(b: dict[str, Any], corpus: visual.Corpus) -> tuple[dict[str, Any] | None, list[str]]:
    """A series stays only when every one of its numbers is in this turn's results; its `derive` is
    computed here. Series s1…, points p1… by position."""
    left: list[str] = []
    x_in, y_in = b.get("x") or {}, b.get("y") or {}
    kind = x_in.get("kind") or "category"
    series: list[dict[str, Any]] = []
    for k, s in enumerate((b.get("series") or [])[:SERIES], 1):
        name = _line(s.get("name") or "", 30) or f"Serie {k}"
        if visual.approval_like(name):
            left.append("a chart series named like an approval")
            continue
        raw = (s.get("points") or [])[:POINTS]
        untraced = [
            p for p in raw
            if not _finite(p.get("y")) or not visual.traces(p["y"], corpus)
            or (kind != "time" and isinstance(p.get("x"), int | float)
                and not (_finite(p.get("x")) and visual.traces(p["x"], corpus)))
            or (kind == "number" and not isinstance(p.get("x"), int | float))
            or (isinstance(p.get("x"), str) and (visual.approval_like(p["x"])
                or not visual.quantities_trace(p["x"], corpus)))
        ]  # fmt: skip
        if untraced or not raw:
            left.append(f"chart series {name!r}: {len(untraced)} number(s) not in this turn's results")
            continue
        ys = [float(p["y"]) for p in raw]
        if s.get("derive") in visual.DERIVES:
            derived = visual.derive_series(str(s["derive"]), ys)
            raw = [p for p, d in zip(raw, derived, strict=True) if d is not None]
            ys = [d for d in derived if d is not None]
        points = []
        for j, (p, y) in enumerate(zip(raw, ys, strict=True), 1):
            x = p["x"] if _finite(p["x"]) else _line(p["x"], 40)
            if kind == "time":
                x = iso(x) if isinstance(x, str) else None
                if x is None:
                    break
            points.append({"id": f"p{j}", "x": x, "y": visual.rounded(y) if s.get("derive") else p["y"]})
        if len(points) != len(raw) or not points:
            left.append(f"chart series {name!r}: an x that isn't an ISO date")
            continue
        series.append({"id": f"s{k}", "name": name, "points": points})
    mark = b.get("mark") or "bar"
    if mark == "pie" and series and any(p["y"] < 0 for p in series[0]["points"]):
        return None, [*left, "a pie with a negative slice"]
    if mark == "pie" and (len(series) != 1 or len(series[0]["points"]) > PIE_POINTS):
        return None, [*left, f"a pie: one series of at most {PIE_POINTS} points"]
    if not series:
        return None, [*left, "a chart with no series left"]
    x_axis = {"label": _plain(x_in.get("label"), "x"), "kind": kind}
    y_axis: dict[str, Any] = {"label": _plain(y_in.get("label"), "y")}
    if y_in.get("unit") and not visual.approval_like(str(y_in["unit"])):
        y_axis["unit"] = _line(y_in["unit"], 10)
    chart: dict[str, Any] = {"type": "chart", "mark": mark, "x": x_axis, "y": y_axis, "series": series}
    title = _line(b.get("title") or "", 80)
    if title and not visual.approval_like(title):
        chart["title"] = title
    return chart, left


def _timeline(b: dict[str, Any], corpus: visual.Corpus) -> tuple[dict[str, Any] | None, list[str]]:
    left: list[str] = []
    items: list[dict[str, Any]] = []
    for raw in (b.get("items") or [])[:ITEMS]:
        if not isinstance(raw, dict):
            continue
        label = _line(raw.get("label") or "", 60)
        at, end = iso(str(raw.get("at") or "")) or "", str(raw.get("end") or "").strip()
        if end:
            end = iso(end) or "!"
        if end and end != "!" and moment(end) < moment(at or "0001-01-01"):
            end = "!"
        if not label or not at or end == "!":
            left.append("a timeline item with no label or no ISO date")
            continue
        if visual.approval_like(label) or visual.approval_like(raw.get("detail")):
            left.append("a timeline item that reads like an approval")
            continue
        if not visual.quantities_trace(f"{label} {raw.get('detail') or ''}", corpus):
            left.append("a timeline item with a number this turn's results don't carry")
            continue
        item: dict[str, Any] = {"id": "", "at": at, "label": label}
        if end:
            item["end"] = end
        if raw.get("detail") and str(raw["detail"]).strip():
            item["detail"] = _line(raw["detail"], 200)
        items.append(item)
    if not items:
        return None, [*left, "a timeline with no dated item"]
    items.sort(key=lambda i: moment(i["at"]))
    for n, item in enumerate(items, 1):
        item["id"] = f"t{n}"
    return {"type": "timeline", "items": items}, left


# a drawing that can't
# be drawn never goes as an empty card; the model is told why, and says it or shows the table
LOST_DRAWING = (
    "Say in words that it can't be drawn and why, or show the table its numbers are in (a table block)."
)
# what a sheet shows besides words: with none of it left, the drawing that was asked was the answer
_CONTENT = frozenset({"image", "list", "fact", "table", "stat", "chips", *DRAWING_TYPES})


def _drawing_lost(
    asked: list[SheetBlockIn] | list[dict[str, Any]], built: list[dict[str, Any]]
) -> str | None:
    """A sheet whose every drawing was left out and that holds nothing else to see (only its title, text,
    sections or links): what was lost, else None."""
    kinds = [b.type if isinstance(b, BaseModel) else dict(b).get("type") for b in asked]
    lost = [k for k in kinds if k in DRAWING_TYPES]
    if not lost or any(b["type"] in _CONTENT for b in built):
        return None
    return f"the {lost[0]} can't be drawn and nothing else on the sheet shows the answer"


def sheet_basis(
    blocks: list[dict[str, Any]], results: list[str], earlier: list[str] | None = None
) -> visual.Basis | None:
    """The sheet's basis by code (None: tools, the field left out). `earlier`: the numbers of sheets Pisa
    showed before: a source a drawing's labels may come from, never a read of this turn
    (a sheet of what Pisa knows, after a priced table, is still "Da quello che so")."""
    return visual.basis(blocks, [*results, *(earlier or [])], bool(results))


def for_phone(
    blocks: list[dict[str, Any]], draws: frozenset[str] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """A drawing block only to a phone whose hello lists it (`present.blocks`); otherwise the same
    answer as a table or a list, made by code."""
    out: list[dict[str, Any]] = []
    left: list[str] = []
    for b in blocks:
        if b["type"] in DRAWING_TYPES and b["type"] not in (draws or frozenset()):
            plain, cut = visual.fallback(b)
            out.append(plain)
            if cut:
                left.append(cut)
        else:
            out.append(b)
    return out, left


CHOICES = (2, 5)
COMPARE_PLACES = (2, 4)
COMPARE_TITLE = {"it-IT": "Confronto", "en-US": "Comparison"}
CRITERIA_MAX = 6
# the switch turned on once the app supports it: `ref.agenda` on a plan object for a day
# with no plan stored. Build 31's strict PlanRef drops the whole `present` on an unknown key,
# so it stays unsent until the phone decodes it; meanwhile the phone falls back to the day's agenda itself.
SEND_PLAN_AGENDA = False
_ITEM = re.compile(ITEM_ID)
# RAIL: what the user's words must say for a show in a turn that read outside text: they asked to
# see, list, compare or choose (a tap on Pisa's own choice is theirs too).
# RAIL: only an explicit ask to display, never everyday words ("guarda se Marco mi ha
# scritto", "look at the email", "is it open?"), which an email could ride on.
_SEE = re.compile(
    r"\[tap[^\]]*\]|\b(mostr\w*|fammi vedere|fammel[aoie] vedere|fammi scegliere|sul telefono|sullo schermo|"
    r"apri le impostazioni|show me|show it|let me see|let me choose|on (?:the|my) (?:screen|phone)|"
    r"open (?:the )?settings)\b",
    re.IGNORECASE,
)
# The kinds whose content isn't a search's answer the user asked for: outside text can't put them up. Pisa's
# own settings page carries no outside text, so a "Sì, grazie" to Pisa's offer to open it is never held:
# "setting" shows after an email read too (focus_object and close_object never pass here: they show nothing
# new).
_NEEDS_WORDS = frozenset({"plan", "item", "changes", "note", "choice"})
_ACK_WORDS = {
    "shown": "Shown on the {surface}, in front.",
    "queued": "Queued: the app isn't in front (locked, or in the background); there when "
    "the user opens Pisa.",
    "deferred": "Waiting with an unseen dot: the user is reading or editing something on the phone.",
    "unavailable": "Not shown: no screen in use (glasses only, or no phone connected). Say it in words.",
    "unsupported": "Not shown: this app can't show a {kind}. Say it in words.",
    "sent_unconfirmed": "Sent to the phone, not confirmed in time: don't say it is on the screen.",
}


def needs_words(kind: str, words: str) -> bool:
    """In a turn that read outside text, does showing this kind need the user's words asking to see?"""
    return kind in _NEEDS_WORDS and not _SEE.search(words)


@dataclass(frozen=True)
class Ack:
    """What a presentation tool tells the model: the device's status for the object."""

    status: Status
    object_id: str
    kind: str
    surface: str = "none"

    def text(self) -> str:
        surface = "glasses' display" if self.surface == "glasses_display" else "phone"
        line = _ACK_WORDS[self.status].format(surface=surface, kind=self.kind)
        return f"{line} (object {self.object_id})"


class Device(Protocol):
    """The user's device on the socket this turn (the gateway's session)."""

    kinds: frozenset[str]  # the object kinds it shows; empty: an app that only knows `card`
    # the drawing blocks it draws; None: none (a
    # drawing goes as a table or a list). Read with getattr: a device may not carry it.

    async def present(self, message: dict[str, Any]) -> tuple[str, str] | None:
        """Send `present`; the device's (status, surface), or None when no `presented` came in time."""
        ...

    async def focus(
        self, present_id: str, object_id: str, highlight: str | None
    ) -> tuple[str, str] | None: ...

    async def close(self, object_id: str) -> bool: ...

    async def card(self, kind: str, title: str, lines: list[str]) -> bool:
        """Today's `card` (an app without `present`): plan, info or warning."""
        ...


@dataclass
class Shown:
    """An object Pisa showed to a device: for focus, close, and a tap on a choice."""

    kind: str
    revision: int
    lang: str
    options: dict[str, str] = field(default_factory=dict)  # a choice's option id → label
    present_id: str = ""  # its latest show: the key of its move in Pisa's log
    at: datetime | None = None  # when it was last shown
    expires_at: datetime | None = None  # a choice answered later is refused
    # "options:<id>": the choice offers Pisa's plan options (a tap grounds only those)
    link: str | None = None
    # a sheet's data as Pisa sent it (its drawings' elements: what a tap names), and
    # whether the turn that showed it read outside text (a tapped label is then quoted as data)
    data: dict[str, Any] | None = None
    outside: bool = False
    closed: bool = False  # the phone closed it (object_event closed): a tap on it answers nothing


OBJECTS_KEPT = timedelta(hours=24)  # FACT: an object older than a day is gone from the phone's history too
# a sheet's numbers trace a later drawing only within this conversation (as
# conversation.NEW_CONVERSATION_AFTER starts a new one after 30 minutes' silence), never a day-old sheet's
EARLIER_SHEETS = timedelta(minutes=30)
OBJECTS_MAX = 50


def prune(objects: dict[str, Shown], now: datetime) -> None:
    """Forget objects older than OBJECTS_KEPT, and the oldest past OBJECTS_MAX."""
    for object_id in [k for k, v in objects.items() if v.at is not None and now - v.at > OBJECTS_KEPT]:
        del objects[object_id]
    while len(objects) > OBJECTS_MAX:
        del objects[min(objects, key=lambda k: objects[k].at or now)]


@dataclass
class Cards:
    """Today's `card` messages, for an app without `present` (the fallback)."""

    place: Callable[[PlaceCard], Awaitable[None]] | None = None
    places: Callable[[dict[str, Any]], Awaitable[bool]] | None = None
    plan: Callable[[date], Awaitable[bool]] | None = None
    note: Callable[[str, list[str], str], Awaitable[bool]] | None = None


@dataclass
class Presenter:
    """One turn's showing: the device, the user's words, what this turn found, and the device's objects."""

    device: Device | None
    words: str
    lang: str
    today: date
    objects: dict[str, Shown]
    reply_to: str | None = None
    cards: Cards = field(default_factory=Cards)
    noted: Callable[[], dict[str, PlaceCard]] = dict
    facts: Callable[[], dict[str, dict[str, Any]]] = dict  # this turn's place facts (map_search, details)
    sources: Callable[[], list[dict[str, str]]] = list  # the web pages this turn read (title, url, host)
    pages: Callable[[], list[dict[str, str]]] = list  # only the pages read_page read
    page_texts: Callable[[], list[str]] = list  # those pages' own text
    # this turn's reads (`DrawingTurn.reads`: the sheet's basis) and the one
    # corpus a shown number traces to (`DrawingTurn.corpus`: those reads and their words)
    results: Callable[[], list[str]] = list
    corpus: Callable[[], list[str]] = list
    plain: bool = False  # they said "disegna meno": a drawing goes as its table or list, by code
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    showed: list[str] = field(default_factory=list)  # the objects this turn put on a screen
    on_screen: list[str] = field(default_factory=list)  # of those, the ones the phone said it shows now
    # Pisa's stored plan options by their id: {option id: (summary, why)}, None when expired
    stored_options: Callable[[str], Awaitable[dict[str, tuple[str, str]] | None]] | None = None
    # whether a day has a stored plan (today's is made on the phone's GET): else its agenda shows
    has_plan: Callable[[date], Awaitable[bool]] | None = None
    loop: Round | None = None  # the turn's loop: what this turn showed counts for `finish`

    def held(self, tool: str, kind: str, object_id: str) -> str | None:
        """RAIL: outside text never shows by itself: in a turn that read some, Pisa's own lists and
        free text go up only when the user's words asked to see something (what a search the user asked for
        found, places and compare, answers their words)."""
        if not current().outside() or not needs_words(kind, self.words):
            return None
        decisions.note("show", tool, "outside_text", ref=object_id, rail="R1 outside text shows nothing",
                       outcome="held")  # fmt: skip
        presented(self.loop, tool, "held")
        return (
            "Not shown: this turn read outside text (an email, a page, an invite) and the user didn't ask "
            "to see anything. Say it in words; show it if the user asks."
        )

    async def show(
        self,
        tool: str,
        kind: str,
        object_id: str,
        purpose: str,
        focus: Focus,
        *,
        ref: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        highlight: str | None = None,
        expires_at: datetime | None = None,
        options: dict[str, str] | None = None,
        fallback: Callable[[], Awaitable[bool]] | None = None,
        rail: str | None = None,
        link: str | None = None,
        keep: dict[str, Any] | None = None,
        revision: int | None = None,
    ) -> Ack:
        """Show one object: `present` to a device that shows this kind, a card to an app without `present`,
        else unsupported; recorded as a `show` move with the acknowledgment as its outcome. `keep`: the
        server's copy of a sheet (a tap on its element is grounded from it). `revision`:
        the caller's own count (a drawing's redraws), else from the clock."""
        now = self.clock()
        prune(self.objects, now)
        # from the clock (milliseconds), so a restart never shows a revision again
        before = self.objects.get(object_id)
        if revision is None:
            revision = max(int(now.timestamp() * 1000), before.revision + 1 if before is not None else 0)
        present_id = str(uuid.uuid4())
        if self.device is not None and kind in self.device.kinds:
            body: dict[str, Any] = {"object_id": object_id, "kind": kind, "revision": revision}
            if kind == "sheet" and data is not None:  # what this phone draws
                keep = data if keep is None else keep
                data = visual.answers_for_phone(data, getattr(self.device, "blocks", None), self.lang)
            body |= {"ref": ref} if ref is not None else {"data": data}
            message: dict[str, Any] = {
                "present_id": present_id, "reply_to": self.reply_to, "object": body,
                "purpose": " ".join(purpose.split())[:120], "focus": focus, "lang": self.lang,
            }  # fmt: skip
            if highlight:
                message["highlight"] = highlight[:200]
            if expires_at is not None:
                message["expires_at"] = expires_at.isoformat()
            answer = await self.device.present(message)
            ack = _ack(answer, object_id, kind)
        elif (self.device is None or not self.device.kinds) and fallback is not None:
            ack = Ack("shown" if await fallback() else "unavailable", object_id, kind, "phone")
        elif self.device is None:
            ack = Ack("unavailable", object_id, kind)
        else:
            ack = Ack("unsupported", object_id, kind)
        if ack.status not in ("unavailable", "unsupported"):
            self.objects[object_id] = Shown(
                kind, revision, self.lang, dict(options or {}), present_id, now, expires_at, link,
                keep if keep is not None else data if kind == "sheet" else None, bool(current().outside()),
            )  # fmt: skip
            self.showed.append(object_id)
            if ack.status == "shown":
                self.on_screen.append(object_id)
        said = decisions.provenance(said=True, outside=bool(current().outside()))
        decisions.note("show", tool, said, ref=object_id, rail=rail, outcome=ack.status, key=present_id)
        presented(self.loop, tool, ack.status)
        return ack

    def place(self, ref: str) -> tuple[str, str] | None:
        """(place_id, name) of the one place this turn found that `ref` names (its id or its name)."""
        wanted = ref.strip()
        found = {p.place_id: p.title for p in self.noted().values() if wanted in (p.place_id, p.title)}
        found |= {
            pid: str(f.get("name", "")) for pid, f in self.facts().items() if wanted in (pid, f.get("name"))
        }
        if not found:
            low = wanted.casefold()
            found = {p.place_id: p.title for p in self.noted().values() if low and low in p.title.casefold()}
            found |= {pid: str(f.get("name", "")) for pid, f in self.facts().items()
                      if low and low in str(f.get("name", "")).casefold()}  # fmt: skip
        return next(iter(found.items())) if len(found) == 1 else None


class ChoiceOption(BaseModel):
    """One option of ask_choice."""

    id: str = Field(description='"o1" to "o5"')
    label: str = Field(description="≤ 40 characters, what the user would say")
    detail: str | None = Field(default=None, description="≤ 80 characters, optional")


def _ack(answer: tuple[str, str] | None, object_id: str, kind: str) -> Ack:
    """The device's answer, or sent_unconfirmed when it didn't come within ACK_WAIT_S."""
    if answer is None:
        return Ack("sent_unconfirmed", object_id, kind)
    return Ack(cast(Status, answer[0]), object_id, kind, answer[1])


def _criterion(name: str) -> str | None:
    """Which fact of a place a criterion asks for (the user's words too)."""
    low = name.casefold()
    for fact, words in (
        ("distance", ("distan", "vicin", "lontan", "close", "far", "minut", "walk", "piedi")),
        ("open", ("apert", "open", "orari", "until", "fino", "chiud", "close at")),
        ("price", ("prezz", "price", "cost", "econom", "cheap", "caro", "expensive")),
        ("rating", ("voto", "rating", "recension", "review", "stell", "star")),
    ):
        if any(w in low for w in words):
            return fact
    return None


def _cell(fact: str | None, facts: dict[str, Any], origin: str) -> dict[str, str]:
    """One compare cell from this turn's facts about the place; what wasn't checked is unknown."""
    unknown = {"status": "unknown", "value": "", "source": ""}
    if fact == "distance" and origin == "here":
        meters, minutes = facts.get("distance_m"), facts.get("travel_min")
        if meters is None and minutes is None:
            return unknown
        value = " · ".join(v for v in (f"{meters} m" if meters is not None else "",
                                       f"{minutes} min" if minutes is not None else "") if v)  # fmt: skip
        return {"status": "verified", "value": value, "source": "Google"}
    if fact == "open" and facts.get("open_now") is not None:
        change = str(facts.get("next_change") or "")
        until = f" ({'until' if facts['open_now'] else 'opens'} {change[11:16]})" if len(change) >= 16 else ""
        value = ("open" if facts["open_now"] else "closed") + until
        return {"status": "verified", "value": value, "source": "Google (hours)"}
    if fact == "price" and facts.get("price_level") is not None:
        return {"status": "verified", "value": "€" * max(1, int(facts["price_level"])), "source": "Google"}
    if fact == "rating" and facts.get("rating") is not None:
        count = facts.get("rating_count")
        value = f"{facts['rating']}" + (f" ({count})" if count else "")
        return {"status": "verified", "value": value, "source": "Google"}
    return unknown


def _day(value: str, today: date) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return day_named(value, today)


def add_present_tools(agent: Agent[None, str], screen: Callable[[], Presenter | None]) -> None:
    """The presentation toolset, always loaded (`screen()`: this turn's presenter, None outside a turn)."""

    def turn() -> Presenter:
        found = screen()
        if found is None:
            raise RuntimeError("a presentation tool ran outside a turn")
        return found

    def done(ack: Ack | str) -> str:
        return ack if isinstance(ack, str) else ack.text()

    @offer(agent)
    async def show_plan(
        day: str = "today", focus: Focus = "offer", highlight: str | None = None,
        options_id: str | None = None, purpose: str = "",
    ) -> str:  # fmt: skip
        """Show the day's plan (Pisa's own plan, as on Today) on the user's phone. Use it when a look beats a
        sentence: several moves, a new order, travel and leave-at times, or options to compare.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            day: "today", "tomorrow", a weekday or YYYY-MM-DD.
            focus: take (in front now), offer (a chip one tap away), quiet (into the history only).
            highlight: an item's id or title to point at.
            options_id: show Pisa's latest options next to the current plan; nothing changes until
            apply_option.
        """
        p = turn()
        when = _day(day, p.today)
        if when is None:
            return "Not shown: name the day as today, tomorrow, a weekday or YYYY-MM-DD."
        if options_id is not None and not re.fullmatch(r"[0-9a-fA-F-]{36}", options_id.strip()):
            return "Not shown: options_id is the id Pisa's options gave."
        object_id = f"plan:{when.isoformat()}"
        if (refused := p.held("show_plan", "plan", object_id)) is not None:
            return refused
        plan_card = p.cards.plan
        ref: dict[str, Any] = {
            "date": when.isoformat(),
            "options_id": options_id.strip() if options_id else None,
        }
        # a day with no plan stored shows its agenda (GET /v1/agenda), never just its date
        agenda = when != p.today and p.has_plan is not None and not await p.has_plan(when)
        if agenda and SEND_PLAN_AGENDA:
            ref["agenda"] = True
        with decisions.intending(purpose):
            ack = await p.show(
                "show_plan", "plan", object_id, purpose, focus, ref=ref, highlight=highlight,
                fallback=(lambda: plan_card(when)) if plan_card else None,
            )  # fmt: skip
        return done(ack) + (" No plan for that day yet: its agenda is shown." if agenda else "")

    @offer(agent)
    async def show_place(
        place: str, detail: Literal["card", "full"] = "card", focus: Focus = "offer", purpose: str = ""
    ) -> str:
        """Show ONE place you looked up this turn (its photo, how far, open until, why it fits); `full` opens
        the map, photos, reviews and menu. Several places: show_places. Not a place you didn't look up.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            place: the place's id or name as the tool result gave it.
            detail: card, or full.
            focus: take, offer or quiet.
        """
        p = turn()
        found = p.place(place)
        if found is None:
            return "Not shown: name one place this turn looked up, as its result named it."
        place_id, name = found
        card = p.noted().get(place_id) or PlaceCard(place_id, name, [])
        send = p.cards.place

        async def fallback() -> bool:
            if send is None:
                return False
            await send(card)
            return True

        with decisions.intending(purpose):
            ack = await p.show("show_place", "place", f"place:{place_id}", purpose, focus,
                               ref={"place_id": place_id, "detail": detail}, fallback=fallback)  # fmt: skip
        return done(ack)

    # show_places is the research toolset's: registered there, in this place of the order.

    @offer(agent)
    async def compare_places(
        place_ids: list[str],
        criteria: list[str],
        origin: Literal["here", "next_event"] = "here",
        focus: Focus = "take",
        purpose: str = "",
    ) -> str:
        """Compare 2-4 places side by side on what matters to this request (criteria: "distance", "open
        until", "price", "rating", or the user's words). Every cell comes from this turn's tool results; an
        unchecked cell shows as unknown. Never claim "the closest of all" unless the search covered that.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            place_ids: the places' ids from map_search or place_details this turn.
            criteria: 1-6 short criteria.
            origin: distances from here or from the next event.
            focus: take, offer or quiet.
        """
        p = turn()
        facts = p.facts()
        ids = list(dict.fromkeys(i.strip() for i in place_ids))
        missing = [i for i in ids if i not in facts]
        if missing:
            return f"Not shown: {len(missing)} place(s) weren't found by this turn's tools. Use their ids."
        if not COMPARE_PLACES[0] <= len(ids) <= COMPARE_PLACES[1]:
            return "Not shown: compare 2 to 4 places."
        named = [" ".join(c.split())[:40] for c in criteria if c.strip()][:CRITERIA_MAX]
        outside = bool(current().outside())
        if outside:  # RAIL: after outside text, only criteria Pisa reads from its own facts
            named = [c for c in named if _criterion(c) is not None]
        if not named:
            return "Not shown: name at least one criterion."
        rows = [{"place_id": i, "name": str(facts[i].get("name", ""))[:120],
                 "cells": [_cell(_criterion(c), facts[i], origin) for c in named]} for i in ids]  # fmt: skip
        fixed = COMPARE_TITLE.get(p.lang, COMPARE_TITLE["en-US"])
        title = (
            fixed if outside else " ".join(purpose.split())[:120] or fixed
        )  # no free text after outside text
        data = {"title": title, "criteria": named, "rows": rows}
        note = p.cards.note

        async def fallback() -> bool:
            lines = [f"{r['name']}: " + " · ".join(c["value"] or "?" for c in r["cells"]) for r in rows]
            return note is not None and await note(title, lines, "info")

        with decisions.intending(purpose):
            ack = await p.show("compare_places", "compare", f"compare:{uuid.uuid4()}", purpose, focus,
                               data=data, fallback=fallback)  # fmt: skip
        unknown = sum(c["status"] == "unknown" for r in rows for c in r["cells"])
        return done(ack) + (f" {unknown} cell(s) unknown: not checked this turn." if unknown else "")

    @offer(agent)
    async def show_item(
        ref: str, focus: Focus = "offer", highlight: str | None = None, purpose: str = ""
    ) -> str:
        """Show one of the user's things: "task:<id>", "event:<calendar>/<id>", "email:<id>", "draft:<id>",
        "approval:<id>", "claim:<id>" (About you), "thing:<id>". For a draft or an approval, the full text the
        user is about to approve.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            ref: the item, as above, with the id a tool gave.
            focus: take, offer or quiet.
            highlight: a part to point at.
        """
        p = turn()
        item = ref.strip()
        if not _ITEM.match(item):
            return 'Not shown: ref is "task:<id>", "event:<calendar>/<id>", "email:<id>", "draft:<id>"….'
        if (refused := p.held("show_item", "item", item)) is not None:
            return refused
        with decisions.intending(purpose):
            return done(await p.show("show_item", "item", item, purpose, focus, ref={"ref": item},
                                     highlight=highlight))  # fmt: skip

    @offer(agent)
    async def show_changes(since: str | None = None, focus: Focus = "offer", purpose: str = "") -> str:
        """Show what Pisa did or proposed (Changes): each with its why and its Undo while it lasts.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            since: an ISO date-time to start from, or nothing for the recent ones.
            focus: take, offer or quiet.
        """
        p = turn()
        start: datetime | None = None
        if since:
            try:
                start = datetime.fromisoformat(since.strip())
            except ValueError:
                return "Not shown: since is an ISO date-time, or leave it out."
            start = start if start.tzinfo else start.replace(tzinfo=UTC)
        object_id = f"changes:{start.date().isoformat() if start else 'recent'}"
        if (refused := p.held("show_changes", "changes", object_id)) is not None:
            return refused
        ref = {"since": start.isoformat() if start else None}
        with decisions.intending(purpose):
            return done(await p.show("show_changes", "changes", object_id, purpose, focus, ref=ref))

    @offer(agent)
    async def show_setting(
        section: Section, highlight: str | None = None, focus: Focus = "take", purpose: str = ""
    ) -> str:
        """Open a settings page on the phone, pointing at one setting (highlight: "walk_max_min",
        "speak_briefing_on_phone"…). It only shows: a setting changes only with its own tool and the user's
        words, never by showing it.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            section: the settings page.
            highlight: the setting's key to point at.
            focus: take, offer or quiet.
        """
        p = turn()
        object_id = f"setting:{section}"
        if (refused := p.held("show_setting", "setting", object_id)) is not None:
            return refused
        with decisions.intending(purpose):
            return done(await p.show("show_setting", "setting", object_id, purpose, focus,
                                     ref={"section": section}, highlight=highlight))  # fmt: skip

    @offer(agent)
    async def show_note(
        title: str, lines: list[str], kind: Literal["info", "warning"] = "info", purpose: str = "",
        focus: Focus = "offer",
    ) -> str:  # fmt: skip
        """A short readable note (at most 6 lines) when a list is easier to read than to hear: three options,
        a checklist, an address. A web fact carries the pages this turn read as its sources. Never for
        a table ("come tabella", "as a table"): that is show_sheet with a table block.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            title: a few words.
            lines: 1-6 short lines.
            kind: info or warning.
            focus: take, offer or quiet.
        """
        p = turn()
        kept = [" ".join(line.split())[:200] for line in lines if line.strip()][:NOTE_LINES]
        if not kept:
            return "Not shown: a note needs at least one line."
        object_id = f"note:{uuid.uuid4()}"
        if (refused := p.held("show_note", "note", object_id)) is not None:
            return refused
        heading = " ".join(title.split())[:120]
        data: dict[str, Any] = {"title": heading, "lines": kept, "kind": kind}
        if sources := p.sources()[:6]:
            data["sources"] = sources
        note = p.cards.note
        with decisions.intending(purpose):
            ack = await p.show("show_note", "note", object_id, purpose, focus, data=data,
                               fallback=(lambda: note(heading, kept, kind)) if note else None)  # fmt: skip
        return done(ack) + (f" {len(lines) - len(kept)} line(s) left out." if len(kept) < len(lines) else "")

    @offer(agent)
    async def show_sheet(
        title: str,
        blocks: list[SheetBlockIn],
        purpose: str = "",
        focus: Focus = "take",
        replaces: str | None = None,
    ) -> str:
        """Compose a sheet on the user's phone when an answer is better seen than heard: a
        research answer (the best matches, their pictures, links and facts), "fammi vedere"; and whenever the
        user asks for a table ("come tabella", "in tabella", "as a table"): a table block. The phone draws
        each block with its own template. Images only from the pages you read this turn (their preview
        images) or a place's photos, at most 6; links only to pages you read this turn. Draw a
        structure or a flow (diagram), ≥ 3 numbers from this turn's results (chart), dated items (timeline);
        never for a one-fact answer; then say one sentence.

        Args:
            purpose: what the user gets from seeing it, one line in their language.
            title: a few words.
            blocks: 1-24 blocks, in order: text (text), image (url, alt, credit), link (url, title), list
                (items, 1-8), fact (label, value), section (title), table (columns ≤6, rows ≤20 of text or
                numbers), stat (label, value, unit, trend up/down/flat), chips (items ≤12), divider; at most
                2 of: diagram (style, direction, nodes, edges, groups), chart (mark, title, x, y, series),
                timeline (items with at). How to draw: the drawing skill.
            focus: take, offer or quiet.
            replaces: the object id of a sheet you showed, to redraw it in place ("dal lato del database").
        """
        p = turn()
        before = (replaces or "").strip()
        if before and (p.objects.get(before) is None or p.objects[before].kind != "sheet"):
            return "Not shown: replaces names a sheet you showed (its object id, from that show's result)."
        results = p.results()
        # the numbers of a sheet Pisa showed this
        # conversation traced when it was shown, so its chart may draw them again
        # only this conversation's sheets (shown within EARLIER_SHEETS), never a day's worth of them
        now = p.clock()
        earlier = [
            n
            for shown in p.objects.values()
            if shown.kind == "sheet" and shown.at is not None and now - shown.at <= EARLIER_SHEETS
            for n in visual.shown_numbers(shown.data)
        ]
        built, dropped = sheet_blocks(
            blocks,
            p.pages(),
            p.page_texts(),
            results=[*p.corpus(), *earlier],
            lang=p.lang,
            phone=getattr(p.device, "width", None),
        )  # hosts of pages read, never a mere result
        if not built:
            return (
                "Not shown: a sheet needs at least one block Pisa can show (" + "; ".join(dropped[:3]) + ")."
            )
        if (empty := _drawing_lost(blocks, built)) is not None:
            return f"Not shown: {empty} (" + "; ".join(dropped[:3]) + "). " + LOST_DRAWING
        lang = p.lang if p.lang in ("it-IT", "en-US") else "en-US"
        data: dict[str, Any] = {
            "title": " ".join(title.split())[:120] or "Pisa",
            "blocks": built,
            "lang": lang,
        }
        if (basis := sheet_basis(built, results, earlier)) is not None:
            data["basis"] = basis  # by code: "Da quello che so" on the phone
        if sources := p.sources()[:6]:
            data["sources"] = sources
        try:
            SheetData.model_validate(data)
        except ValueError as err:
            return f"Not shown: {str(err)[:300]}"
        drawn = sum(b["type"] in DRAWING_TYPES for b in built)
        draws = None if p.plain else getattr(p.device, "blocks", None)  # words only, by code
        sent, cut = for_phone(built, draws)  # the fallback, by code
        dropped += cut
        object_id = before or f"sheet:{uuid.uuid4()}"
        if (refused := p.held("show_sheet", "sheet", object_id)) is not None:
            return refused
        with decisions.intending(purpose):
            ack = await p.show(
                "show_sheet", "sheet", object_id, purpose, focus, data={**data, "blocks": sent}, keep=data
            )
        left = f" Left out: {'; '.join(dropped[:4])}." if dropped else ""
        line = " Say one sentence: what it shows and the one thing to notice." if drawn else ""
        return done(ack) + left + (line if ack.status in TERMINAL else "")

    @offer(agent)
    async def ask_choice(
        question: str,
        options: list[ChoiceOption],
        focus: Focus = "take",
        expires_min: int = 10,
        options_id: str | None = None,
        purpose: str = "",
    ) -> str:
        """Put a question with 2-5 options on the phone, answerable by tap or voice. Say the question too (at
        most 3 options aloud). The answer comes back as the user's own next message ("[tap …] o2: …"). Not
        for approvals of actions that reach other people: the action tool itself asks those.

        Args:
            purpose: what the user gets from it, one line in their language.
            question: the question, as you say it.
            options: 2-5 options, ids "o1" to "o5".
            focus: take, offer or quiet.
            expires_min: minutes the question stays open (1-60).
            options_id: when the options are Pisa's plan options (plan_options), their id, with the same
                option ids: a tap then chooses that option.
        """
        p = turn()
        # no choice put to them when their words already decide ("sono in ritardo di un'ora,
        # sistemami il pomeriggio"): the change is made, one question only for what waits for their yes
        if (decided := coverage.no_choice(p.words)) is not None:
            return f"Not shown: {decided}"
        ids = [o.id.strip() for o in options]
        if (
            not CHOICES[0] <= len(options) <= CHOICES[1]
            or len(set(ids)) != len(ids)
            or any(not re.fullmatch(r"o[1-5]", i) for i in ids)
        ):
            return 'Not shown: 2 to 5 options, each with its own id "o1" to "o5".'
        object_id = f"choice:{uuid.uuid4()}"
        if (refused := p.held("ask_choice", "choice", object_id)) is not None:
            return refused
        linked = (options_id or "").strip()
        if linked and not re.fullmatch(r"[0-9a-fA-F-]{36}", linked):
            return "Not shown: options_id is the id Pisa's options gave."
        listed = [{"id": i, "label": " ".join(o.label.split())[:40]} | (
            {"detail": " ".join(o.detail.split())[:80]} if o.detail and o.detail.strip() else {}
        ) for i, o in zip(ids, options, strict=True)]  # fmt: skip
        if linked and p.stored_options is not None:
            # a tap on a linked option applies Pisa's stored option: its words are Pisa's, not the
            # model's (a label saying one thing over an id doing another).
            stored = await p.stored_options(linked)
            if stored is None or any(i not in stored for i in ids):
                return "Not shown: those options expired or don't have these ids; ask plan_options again."
            listed = [{"id": i, "label": " ".join(stored[i][0].split())[:40]}
                      | ({"detail": " ".join(stored[i][1].split())[:80]} if stored[i][1].strip() else {})
                      for i in ids]  # fmt: skip
        data = {"question": " ".join(question.split())[:200], "options": listed}
        expires = p.clock() + timedelta(minutes=min(max(expires_min, 1), 60))
        with decisions.intending(purpose):
            ack = await p.show(
                "ask_choice", "choice", object_id, purpose, focus, data=data, expires_at=expires,
                options={o["id"]: o["label"] for o in listed}, link=f"options:{linked}" if linked else None,
            )  # fmt: skip
        return done(ack)

    @offer(agent)
    async def focus_object(object_id: str, highlight: str | None = None, purpose: str = "") -> str:
        """Bring an object you already showed back in front, or point at a part of it ("guarda la seconda").

        Args:
            object_id: the object, as a show's result named it.
            purpose: what the user gets from looking again, one line in their language.
            highlight: a part to point at (an item, an option id, a place id).
        """
        p = turn()
        shown = p.objects.get(object_id.strip())
        if shown is None:
            return "Not focused: name an object you showed, as its result named it."
        if p.device is None or not p.device.kinds:
            ack = Ack("unavailable" if p.device is None else "unsupported", object_id, shown.kind)
        else:
            answer = await p.device.focus(str(uuid.uuid4()), object_id.strip(), highlight)
            ack = _ack(answer, object_id, shown.kind)
        said = decisions.provenance(said=True, outside=bool(current().outside()))
        with decisions.intending(purpose):
            decisions.note("show", "focus_object", said, ref=object_id, outcome=ack.status)
        presented(p.loop, "focus_object", ack.status)
        return ack.text()

    @offer(agent)
    async def close_object(object_id: str) -> str:
        """Take an object you showed off the screen (it stays in the history).

        Args:
            object_id: the object, as a show's result named it.
        """
        p = turn()
        shown = p.objects.pop(object_id.strip(), None)
        if shown is None:
            return "Nothing closed: name an object you showed."
        sent = p.device is not None and bool(p.device.kinds) and await p.device.close(object_id.strip())
        status: Status = "shown" if sent else "unavailable"
        said = decisions.provenance(said=True, outside=bool(current().outside()))
        decisions.note("show", "close_object", said, ref=object_id, outcome="closed" if sent else status)
        presented(p.loop, "close_object", status)
        return f"Closed: {object_id}." if sent else f"Not closed: no screen that shows it. ({object_id})"
