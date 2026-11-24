"""Contract 2's Ring 1 and Ring 2 (CONTRACT-2 §7, §8): what a turn shows, as A2UI 1.0 surfaces in the catalog
`urn:pisa:a2ui:catalog:core:1`, and drawings as MCP Apps views. The weather surface alone (`WeatherSummary`)
is composed in `urn:pisa:a2ui:catalog:core:2` (core:1 and WeatherSummary), for an app that listed core:2.

The converters take the objects the turn pipeline builds (`present` objects with their sheet blocks, `card`s,
sealed drawings: `pisa/services/session_messages.py`, `pisa/domain/sheet.py`) and return A2UI operations, the
activity's text fallback, and MCP Apps content. They are pure: no I/O, no clock. Every value is cut to the
catalog's limits here, so a surface always validates against the catalog (the tests check every kind and every
block type against the official schemas).

Ported from the conformance kit's reference emitter
(`protocol/v2/conformance/src/pisa_conformance/reference/a2ui.py`), fitted to this backend's own shapes.
"""

import hashlib
import re
from typing import Any

from pisa.domain import protocol_versions as versions

CATALOG_ID = versions.A2UI_CATALOG_ID
CATALOG_V2_ID = versions.A2UI_CATALOG_V2_ID
CATALOG_IDS = versions.A2UI_CATALOG_IDS
WEATHER_HOURS_MAX = 24  # WeatherSummary's `hourly`
A2UI_VERSION = versions.A2UI
SURFACE_ID_MAX = 80  # the longest surface id an app's `pisa.surface.ack` may name
FALLBACK_MAX = 1000  # an activity's `pisa.fallbackText`
COMPONENT_FALLBACK_MAX = 400

# The few words a surface carries that the turn did not write: a ref's fallback, the sources' label.
LABELS: dict[str, dict[str, str]] = {
    "en-US": {
        "plan": "The plan for {date}",
        "place": "A place",
        "item": "An item",
        "changes": "What changed",
        "setting": "A setting",
        "sources": "Sources",
        "pick": "Pick",
        "excluded": "Left out",
        "when": "When",
        "where": "Where",
        "shipping": "Shipping",
        "low_6m": "Lowest in 6 months",
        "open": "Open",
        "closed": "Closed",
        "hours": "Hours",
        "weather": "{place}, {date}: {condition}, {min} to {max} °C, rain chance {rain}%",
    },
    "it-IT": {
        "plan": "Il piano del {date}",
        "place": "Un posto",
        "item": "Un elemento",
        "changes": "Cosa è cambiato",
        "setting": "Un'impostazione",
        "sources": "Fonti",
        "pick": "Scelta",
        "excluded": "Esclusi",
        "when": "Quando",
        "where": "Dove",
        "shipping": "Spedizione",
        "low_6m": "Minimo in 6 mesi",
        "open": "Aperto",
        "closed": "Chiuso",
        "hours": "Orari",
        "weather": "{place}, {date}: {condition}, da {min} a {max} °C, probabilità di pioggia {rain}%",
    },
}
_NO_SPACE = re.compile(r"^\S+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def label(lang: str | None, key: str) -> str:
    return LABELS.get(lang or "", LABELS["en-US"]).get(key) or LABELS["en-US"][key]


def cut(value: Any, limit: int) -> str:
    """A string at most `limit` characters long (an ellipsis marks a cut)."""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _some(value: Any, limit: int) -> str | None:
    """A non-empty string within `limit`, else None (an optional property is then left out)."""
    text = cut(value, limit).strip() if value is not None else ""
    return text or None


def surface_id_for(object_id: str) -> str:
    """The surface id an object goes under: its own id when an app can name it back (≤ 80 characters),
    else a stable digest of it."""
    if len(object_id) <= SURFACE_ID_MAX:
        return object_id
    return "obj:" + hashlib.sha256(object_id.encode()).hexdigest()[:40]


def view_uri(drawing_id: str) -> str:
    return f"ui://pisa/drawing/{drawing_id}"


class SurfaceBuilder:
    """Collects one surface's flat component list; ids are short and stable within the surface."""

    def __init__(self, surface_id: str, lang: str | None = None, catalog_id: str = CATALOG_ID) -> None:
        self.surface_id = surface_id
        self.lang = lang
        self.catalog_id = catalog_id
        self.components: list[dict[str, Any]] = []
        self._n = 0

    def add(self, component: str, *, id: str | None = None, **props: Any) -> str:
        if id is None:
            self._n += 1
            id = f"c{self._n}"
        self.components.append(
            {"id": id, "component": component, **{k: v for k, v in props.items() if v is not None}}
        )
        return id

    def column(self, children: list[str], *, id: str | None = None) -> str:
        return self.add("Column", id=id, children=children)

    def text(self, text: Any, variant: str | None = None, *, id: str | None = None) -> str:
        return self.add("Text", id=id, text=str(text) if text is not None else "", variant=variant)

    def create(self) -> dict[str, Any]:
        if not any(c["id"] == "root" for c in self.components):
            raise ValueError("a surface needs a root component")
        body = {"surfaceId": self.surface_id, "catalogId": self.catalog_id, "components": self.components}
        return {"version": A2UI_VERSION, "createSurface": body}

    def fallback_text(self) -> str:
        return fallback_of(self.components)


def delete_surface(surface_id: str) -> dict[str, Any]:
    return {"version": A2UI_VERSION, "deleteSurface": {"surfaceId": surface_id}}


# ------------------------------------------------------------------------------------------------------
# Fallback text: every component has one (CATALOG.md). It is what glasses, the watch and VoiceOver get, and
# what an app shows for a component it cannot draw.
def component_fallback(c: dict[str, Any]) -> str:
    if "fallback" in c:
        return str(c["fallback"])
    kind = c.get("component")

    def lit(v: Any) -> str:
        return v if isinstance(v, str) else ""

    if kind == "Text":
        t = lit(c.get("text"))
        return f"«{t}» ({c['source']})" if c.get("quoted") and c.get("source") else t
    if kind == "Fact":
        return f"{lit(c.get('label'))}: {lit(c.get('value'))}"
    if kind == "Stat":
        return " ".join(x for x in [lit(c.get("label")), lit(c.get("value")), c.get("unit", "")] if x)
    if kind == "BulletList":
        return "\n".join(c["items"])
    if kind == "Chips":
        return ", ".join(c["items"])
    if kind == "Link":
        return f"{c['title']} ({c['host']})"
    if kind == "Image":
        return str(c["alt"])
    if kind == "Check":
        src = f" ({c['source']})" if c.get("source") else ""
        return f"{c['requirement']}: {c['status']}{src}"
    if kind == "Sources":
        return "Sources: " + ", ".join(i["host"] for i in c["items"])
    if kind == "Button":
        return str(c["label"])
    if kind == "Choice":
        return str(c["question"]) + "".join(f"\n{i + 1}. {o['label']}" for i, o in enumerate(c["options"]))
    if kind == "Section":
        return lit(c.get("title"))
    return ""


def fallback_of(components: list[dict[str, Any]], root: str = "root") -> str:
    by_id = {c["id"]: c for c in components}
    seen: set[str] = set()

    def walk(cid: str) -> list[str]:
        c = by_id.get(cid)
        if c is None or cid in seen:
            return []
        seen.add(cid)
        own = component_fallback(c)
        kids: list[str] = []
        if isinstance(c.get("children"), list):
            kids = c["children"]
        elif "child" in c:
            kids = [c["child"]]
        lines = [own] if own else []
        for k in kids:
            lines += walk(k)
        return lines

    return cut("\n".join(x for x in walk(root) if x), FALLBACK_MAX)


# ------------------------------------------------------------------------------------------------------
# The parts
def _host(url: str) -> str:
    parts = url.split("/")
    return parts[2] if len(parts) > 2 and parts[2] else url


def _https(url: Any) -> str | None:
    """An https URL the catalog's Link and Image take (no spaces), else None."""
    if (
        not isinstance(url, str)
        or not url.startswith("https://")
        or len(url) > 2000
        or not _NO_SPACE.match(url)
    ):
        return None
    return url


def sources_component(b: SurfaceBuilder, sources: list[dict[str, Any]]) -> str | None:
    items = []
    for s in sources[:6]:
        url = s.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url) or len(url) > 2000:
            continue
        host = _some(s.get("host"), 255) or _host(url)[:255]
        items.append({"title": _some(s.get("title"), 120) or host[:120], "url": url, "host": host})
    if not items:
        return None
    return b.add("Sources", items=items)


def _bullets(b: SurfaceBuilder, lines: list[Any]) -> str | None:
    items = [cut(x, 200) for x in lines if str(x).strip()][:8]
    return b.add("BulletList", items=items) if items else None


def _chips(b: SurfaceBuilder, items: list[Any]) -> str | None:
    chips = [cut(x, 40) for x in items if str(x).strip()][:8]
    return b.add("Chips", items=chips) if chips else None


def place_summary(b: SurfaceBuilder, p: dict[str, Any], id: str | None = None) -> str:
    checks = [
        {
            "id": f"k{i}",
            "component": "Check",
            "requirement": cut(c["requirement"], 80),
            "status": c["status"],
            **({"source": cut(c["source"], 120)} if c.get("source") else {}),
        }
        for i, c in enumerate(p.get("checks") or [])
    ][:6]
    name = cut(p.get("name") or "-", 120)
    why = _some(p.get("why"), 200) or name
    fb = f"{name}: {why}"
    if p.get("distance_m") is not None:
        fb += f" ({p['distance_m']} m)"
    price = p.get("price_level")
    return b.add(
        "PlaceSummary",
        id=id,
        placeId=str(p["place_id"]).lower(),
        name=name,
        why=why,
        distanceM=p.get("distance_m"),
        travelMin=p.get("travel_min"),
        openNow=p.get("open_now"),
        rating=p.get("rating"),
        ratingCount=p.get("rating_count"),
        priceLevel=price if isinstance(price, int) and 0 <= price <= 4 else None,
        closesAt=_some(p.get("closes_at"), 40),
        opensAt=_some(p.get("opens_at"), 40),
        lat=p.get("lat"),
        lon=p.get("lon"),
        photoCount=p.get("photo_count"),
        fits=[cut(f, 60) for f in p.get("fits") or [] if str(f).strip()][:3] or None,
        different=p.get("different") or None,
        checks=checks or None,
        fallback=cut(fb, COMPONENT_FALLBACK_MAX),
    )


def weather_summary(b: SurfaceBuilder, w: dict[str, Any], id: str | None = None) -> str:
    """get_weather's forecast (the card's `weather`: place, date, condition, min_c, max_c, rain_chance, now,
    hourly) -> a core:2 WeatherSummary; at most 24 hours."""

    def condition(value: Any) -> str:
        return cut(value, 60).strip() or "-"

    now = w.get("now")
    rain = w["rain_chance"]
    fb = label(b.lang, "weather").format(
        place=cut(w["place"], 120), date=w["date"], condition=condition(w["condition"]),
        min=f"{w['min_c']:g}", max=f"{w['max_c']:g}", rain=rain,
    )  # fmt: skip
    return b.add(
        "WeatherSummary",
        id=id,
        place=cut(w["place"], 120).strip() or "-",
        date=w["date"],
        condition=condition(w["condition"]),
        now={"tempC": now["temp_c"], "condition": condition(now["condition"])} if now else None,
        minC=w["min_c"],
        maxC=w["max_c"],
        rainChance=rain,
        hourly=[
            {
                "time": h["time"],
                "tempC": h["temp_c"],
                "rainChance": h["rain_chance"],
                "condition": condition(h["condition"]),
            }
            for h in (w.get("hourly") or [])[:WEATHER_HOURS_MAX]
        ],
        fallback=cut(fb, COMPONENT_FALLBACK_MAX),
    )


def chart_spec(block: dict[str, Any]) -> dict[str, Any]:
    """A sheet's chart block -> the catalog's Vega-Lite subset (one inline table, one mark)."""
    mark = {"bar": "bar", "line": "line", "area": "area", "point": "point", "pie": "arc"}[block["mark"]]
    x, y = block.get("x") or {}, block.get("y") or {}
    xkind = {"time": "temporal", "category": "nominal", "number": "quantitative"}[x.get("kind", "category")]
    values = [
        {"x": pt["x"], "y": pt["y"], "series": s["name"]} for s in block["series"] for pt in s["points"]
    ]
    if mark == "arc":
        encoding: dict[str, Any] = {
            "theta": {"field": "y", "type": "quantitative"},
            "color": {"field": "x", "type": "nominal"},
        }
    else:
        encoding = {"x": {"field": "x", "type": xkind}, "y": {"field": "y", "type": "quantitative"}}
        if title := _some(x.get("label"), 60):
            encoding["x"]["title"] = title
        if title := _some(y.get("label"), 60):
            encoding["y"]["title"] = title
        if len(block["series"]) > 1:
            encoding["color"] = {"field": "series", "type": "nominal"}
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "mark": mark,
        "data": {"values": values[:500]},
        "encoding": encoding,
    }
    if title := _some(block.get("title"), 120):
        spec["title"] = title
    return spec


def _chart_fallback(block: dict[str, Any]) -> str:
    parts = []
    for s in block["series"]:
        pts = ", ".join(f"{p['x']}: {p['y']}" for p in s["points"][:12])
        parts.append(f"{s['name']}: {pts}")
    unit = (block.get("y") or {}).get("unit")
    return cut("; ".join(parts) + (f" ({unit})" if unit else ""), COMPONENT_FALLBACK_MAX) or "-"


def _cell(value: Any) -> Any:
    if value is None or (isinstance(value, int | float) and not isinstance(value, bool)):
        return value
    if isinstance(value, dict):
        out = {"text": cut(value.get("text", ""), 120), "status": value["status"]}
        if value.get("source"):
            out["source"] = cut(value["source"], 120)
        return out
    return cut(value, 120)


def _cell_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return "" if value is None else str(value)


def _table(b: SurfaceBuilder, columns: list[Any], rows: list[list[Any]], fallback: str | None = None) -> str:
    cols = [cut(c, 40) for c in columns][:7]
    body = [[_cell(c) for c in r][: len(cols)] for r in rows][:30]
    fb = fallback or "; ".join(" | ".join(_cell_text(x) for x in r if x is not None) for r in body[:6])
    return b.add("Table", columns=cols, rows=body, fallback=cut(fb, COMPONENT_FALLBACK_MAX) or "-")


def _finding_card(b: SurfaceBuilder, row: dict[str, Any]) -> str:
    """One research finding: its name, its facts as chips, why, pros and cons, and its link."""
    kids = [b.text(cut(row["name"], 120), "headline")]
    open_at = row.get("open_at_asked") or {}
    ok = open_at.get("ok")
    opened = None if ok is None else label(b.lang, "open" if ok else "closed")
    chips = [
        label(b.lang, "pick") if row.get("pick") else None,
        row.get("walk"),
        row.get("price"),
        f"★ {row['rating']}" if row.get("rating") else None,
        row.get("age"),
        opened,
    ]
    chips += [f"{r['guide']}: {r['award']}" for r in row.get("recognition") or []]
    if cid := _chips(b, [c for c in chips if c]):
        kids.append(cid)
    if row.get("at"):
        kids.append(b.add("Fact", label=label(b.lang, "when"), value=cut(row["at"], 120)))
    if row.get("venue"):
        kids.append(b.add("Fact", label=label(b.lang, "where"), value=cut(row["venue"], 120)))
    if open_at.get("hours"):
        kids.append(b.add("Fact", label=label(b.lang, "hours"), value=cut(open_at["hours"], 120)))
    if row.get("why"):
        kids.append(b.text(cut(row["why"], 200)))
    if row.get("pros") and (cid := _bullets(b, [f"+ {p}" for p in row["pros"]])):
        kids.append(cid)
    if row.get("cons") and (cid := _bullets(b, [f"- {c}" for c in row["cons"]])):
        kids.append(cid)
    slots = row.get("slots")
    if slots and (cid := _chips(b, [f"{slots['day']} {t}" for t in slots["times"]])):
        kids.append(cid)
    if url := _https(row.get("url")):
        kids.append(b.add("Link", url=url, title=cut(row["name"], 120), host=_host(url)[:255]))
    return b.add("Card", child=b.column(kids))


def block_to_components(b: SurfaceBuilder, block: dict[str, Any]) -> str | None:
    """One sheet block -> its components (the id of the outermost); None for a block nothing shows."""
    t = block.get("type")
    if t == "text":
        return b.text(block["text"])
    if t == "image":
        url = _https(block.get("url"))
        if url is None:
            return b.text(block["alt"], "caption")
        return b.add(
            "Image", url=url, alt=cut(block["alt"], 200) or "-", credit=_some(block.get("credit"), 120)
        )
    if t == "link":
        url = _https(block.get("url"))
        if url is None:
            return b.text(block["title"])
        host = _some(block.get("host"), 255) or _host(url)[:255]
        return b.add("Link", url=url, title=cut(block["title"], 120) or host[:120], host=host)
    if t == "list":
        return _bullets(b, block["items"])
    if t == "fact":
        return b.add("Fact", label=str(block["label"]), value=str(block["value"]))
    if t == "section":
        return b.text(block["title"], "headline")
    if t == "table":
        return _table(b, block["columns"], block["rows"])
    if t == "stat":
        return b.add(
            "Stat",
            label=str(block["label"]),
            value=str(block["value"]),
            unit=_some(block.get("unit"), 20),
            trend=block.get("trend"),
        )
    if t == "chips":
        return _chips(b, block["items"])
    if t == "divider":
        return b.add("Divider")
    if t == "handoff":
        fb = str(block["title"]) + (f" ({block['subtitle']})" if block.get("subtitle") else "")
        notes = [cut(n, 200) for n in block.get("notes") or [] if str(n).strip()][:4]
        return b.add(
            "Handoff",
            handoffId=str(block["handoff_id"]).lower(),
            kind=block["kind"],
            title=cut(block["title"], 120),
            subtitle=_some(block.get("subtitle"), 120),
            url=block.get("url"),
            phone=block.get("phone"),
            text=_some(block.get("text"), 3000),
            notes=notes or None,
            fallback=cut(fb, COMPONENT_FALLBACK_MAX),
        )
    if t == "chart":
        unit = _some((block.get("y") or {}).get("unit"), 20)
        return b.add("Chart", spec=chart_spec(block), unit=unit, fallback=_chart_fallback(block))
    if t == "diagram":
        nodes = [
            {
                "id": n["id"],
                "label": cut(n["label"], 60),
                "shape": n.get("shape", "box"),
                **({"group": n["group"]} if n.get("group") else {}),
            }
            for n in block["nodes"]
        ][:40]
        edges = [
            {
                "from": e.get("from", e.get("from_")),
                "to": e["to"],
                **{k: e[k] for k in ("label", "style", "arrow") if e.get(k)},
            }
            for e in block["edges"]
        ][:80]
        groups = [{"id": g["id"], "label": cut(g["label"], 60)} for g in block.get("groups") or []][:10]
        style = "flow" if block.get("style", "flow") == "flow" else "structure"
        fb = " → ".join(n["label"] for n in nodes)
        return b.add(
            "Diagram",
            style=style,
            direction=block.get("direction", "down"),
            nodes=nodes,
            edges=edges,
            groups=groups or None,
            fallback=cut(fb, COMPONENT_FALLBACK_MAX) or "-",
        )
    if t == "timeline":
        items = []
        for i, it in enumerate(block["items"][:30]):
            at = str(it.get("at") or "-")
            if it.get("end"):
                at = f"{at}/{it['end']}"
            items.append(
                {
                    "id": cut(it.get("id") or f"t{i + 1}", 40),
                    "at": cut(at, 40),
                    "title": cut(it.get("label") or it.get("title") or "-", 120),
                    **({"detail": cut(it["detail"], 200)} if it.get("detail") else {}),
                }
            )
        fb = "; ".join(f"{i['at']} {i['title']}" for i in items)
        return b.add("Timeline", items=items, fallback=cut(fb, COMPONENT_FALLBACK_MAX) or "-")
    if t == "map":
        pins = [
            {
                "id": p["id"],
                "name": cut(p["name"], 120),
                "lat": p["lat"],
                "lon": p["lon"],
                **({"badge": cut(p["badge"], 60)} if p.get("badge") else {}),
                **({"pick": True} if p.get("pick") else {}),
            }
            for p in block["pins"]
        ][:12]
        return b.add(
            "Map", pins=pins, fallback=cut(", ".join(p["name"] for p in pins), COMPONENT_FALLBACK_MAX)
        )
    if t == "findings":
        return b.column([_finding_card(b, row) for row in block["rows"]])
    if t == "product":
        price = f"{block['price']:.2f} {block.get('currency', '')}".strip()
        kids = [b.text(cut(block["title"], 120), "headline")]
        if image := _https(block.get("image_url")):
            kids.append(b.add("Image", url=image, alt=cut(block["title"], 200)))
        kids.append(b.add("Fact", label=cut(block.get("shop") or "-", 120), value=price))
        if block.get("shipping") is not None:
            kids.append(b.add("Fact", label=label(b.lang, "shipping"), value=f"{block['shipping']:.2f}"))
        if block.get("low_6m") is not None:
            kids.append(b.add("Fact", label=label(b.lang, "low_6m"), value=f"{block['low_6m']:.2f}"))
        if url := _https(block.get("url")):
            kids.append(b.add("Link", url=url, title=cut(block["title"], 120), host=_host(url)[:255]))
        return b.add("Card", child=b.column(kids))
    return None  # a block type this catalog has no form for: skipped


def _ref_string(kind: str, ref: dict[str, Any]) -> str:
    if kind == "plan":
        return f"plan:{ref['date']}"
    if kind == "place":
        return f"place:{str(ref['place_id']).lower()}"
    if kind == "item":
        return str(ref["ref"])
    if kind == "changes":
        return f"changes:{ref.get('since') or 'all'}"
    if kind == "setting":
        return f"setting:{ref['section']}"
    raise ValueError(f"unmapped ref kind {kind}")


def _ref_fallback(kind: str, ref: dict[str, Any], lang: str | None) -> str:
    return label(lang, kind).format(date=ref.get("date", ""))


def object_to_surface(
    obj: dict[str, Any], surface_id: str, lang: str | None = None
) -> tuple[dict[str, Any], str] | None:
    """A `present` object (the session's `Present`) -> (createSurface, its fallback text). A drawing returns
    None: it is a Ring 2 view (`mcp_app_from_drawing`)."""
    kind = obj["kind"]
    b = SurfaceBuilder(surface_id, lang)
    if "ref" in obj:
        b.add(
            "Ref",
            id="root",
            ref=_ref_string(kind, obj["ref"]),
            detail=obj["ref"].get("detail", "card"),
            fallback=_ref_fallback(kind, obj["ref"], lang),
        )
        return b.create(), b.fallback_text()
    data = obj["data"]
    if kind == "note":
        kids = [b.text(data["title"] or "-", "headline")]
        if cid := _bullets(b, data["lines"]):
            kids.append(cid)
        if data.get("sources") and (cid := sources_component(b, data["sources"])):
            kids.append(cid)
        b.add(
            "Card", id="root", child=b.column(kids), tone="warning" if data["kind"] == "warning" else "info"
        )
    elif kind == "choice":
        options = [
            {
                "id": o["id"],
                "label": cut(o["label"], 40),
                **({"detail": cut(o["detail"], 80)} if o.get("detail") else {}),
            }
            for o in data["options"]
        ]
        b.add("Choice", id="root", question=cut(data["question"], 200), options=options)
    elif kind == "places":
        kids = [b.text(data["title"] or "-", "headline")] + [place_summary(b, p) for p in data["places"]]
        b.column(kids, id="root")
    elif kind == "compare":
        criteria = data["criteria"]
        rows = [
            [r["name"]]
            + [{"text": c["value"], "status": c["status"], "source": c.get("source")} for c in r["cells"]]
            for r in data["rows"]
        ]
        fb = "; ".join(
            f"{r['name']}: " + ", ".join(f"{criteria[i]} {c['value']}" for i, c in enumerate(r["cells"]))
            for r in data["rows"]
        )
        kids = [b.text(data["title"] or "-", "headline"), _table(b, ["", *criteria], rows, fb)]
        b.column(kids, id="root")
    elif kind == "sheet":
        kids = [b.text(data["title"], "title")]
        for block in data["blocks"]:
            if (cid := block_to_components(b, block)) is not None:
                kids.append(cid)
        if data.get("excluded") and (
            cid := _bullets(
                b, [f"{label(lang, 'excluded')}: {e['name']}: {e['why']}" for e in data["excluded"]]
            )
        ):
            kids.append(cid)
        if data.get("sources") and (cid := sources_component(b, data["sources"])):
            kids.append(cid)
        if data.get("actions"):
            buttons = [
                b.add(
                    "Button",
                    label=cut(a["label"], 40),
                    style=a["style"],
                    action={"event": {"name": "pisa.sheet_action", "context": {"actionId": a["id"]}}},
                )
                for a in data["actions"]
            ]
            kids.append(b.add("Row", children=buttons))
        b.column(kids, id="root")
    elif kind == "drawing":
        return None
    else:
        raise ValueError(f"unmapped object kind {kind}")
    return b.create(), b.fallback_text()


def card_to_surface(
    card: dict[str, Any], surface_id: str, lang: str | None = None
) -> tuple[dict[str, Any], str]:
    """A `card` (the session's `Card`) -> a surface."""
    kind = card["kind"]
    if kind == "place" and card.get("place_id"):
        made = object_to_surface(
            {"kind": "place", "ref": {"place_id": card["place_id"], "detail": "card"}}, surface_id, lang
        )
        assert made is not None
        return made
    if kind == "places" and card.get("places"):
        data = {"title": card["title"], "request": card.get("request", ""), "places": card["places"]}
        made = object_to_surface({"kind": "places", "data": data}, surface_id, lang)
        assert made is not None
        return made
    if kind == "weather" and card.get("weather"):  # core:2: the session sends it only where core:2 was picked
        b = SurfaceBuilder(surface_id, lang, CATALOG_V2_ID)
        weather_summary(b, card["weather"], id="root")
        return b.create(), b.fallback_text()
    b = SurfaceBuilder(surface_id, lang)
    kids = [b.text(card["title"] or "-", "headline")]
    if card.get("lines") and (cid := _bullets(b, card["lines"])):
        kids.append(cid)
    if kind == "sources" and card.get("sources") and (cid := sources_component(b, card["sources"])):
        kids.append(cid)
    b.add("Card", id="root", child=b.column(kids), tone="warning" if kind == "warning" else "plain")
    return b.create(), b.fallback_text()


def surface_pisa(
    fallback: str,
    *,
    focus: str = "offer",
    purpose: str | None = None,
    reply_to: str | None = None,
    lang: str | None = None,
    basis: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """The activity's `pisa` block (`$defs/A2UISurfaceActivity`)."""
    out: dict[str, Any] = {"focus": focus, "fallbackText": cut(fallback, FALLBACK_MAX).strip() or "-"}
    if p := _some(" ".join((purpose or "").split()), 120):
        out["purpose"] = p
    if reply_to:
        out["replyTo"] = reply_to[:80]
    if lang:
        out["lang"] = lang
    if basis:
        out["basis"] = basis
    if expires_at:
        out["expiresAt"] = expires_at
    return out


def present_content(present: dict[str, Any], surface_id: str) -> dict[str, Any] | None:
    """A `present` message (the session's `Present`, as JSON) -> the a2ui-surface activity's content; None
    for a drawing."""
    obj = present["object"]
    lang = present.get("lang")
    made = object_to_surface(obj, surface_id, lang)
    if made is None:
        return None
    op, fallback = made
    data = obj.get("data") or {}
    basis = "knowledge" if obj["kind"] == "sheet" and data.get("basis") in ("knowledge", "mixed") else None
    pisa = surface_pisa(
        fallback or present.get("purpose") or "-",
        focus=present["focus"],
        purpose=present.get("purpose"),
        reply_to=present.get("reply_to"),
        lang=lang,
        basis=basis,
        expires_at=present.get("expires_at"),
    )
    return {"a2ui_operations": [op], "pisa": pisa}


def mcp_app_from_drawing(
    data: dict[str, Any], focus: str = "take", expires_at: str | None = None
) -> dict[str, Any]:
    """A sealed drawing -> an MCP Apps view (Ring 2, `$defs/McpAppActivity`). The page is the one the server
    checked and sealed, unchanged; the host adds its own wall (CONTRACT-2 §8). `data` reaches the view as
    `ui/notifications/tool-input`."""
    summary = cut(data["summary"], 400) or "-"
    pisa: dict[str, Any] = {
        "title": cut(data["title"], 120) or "-",
        "summary": summary,
        "fallbackText": summary,
        "bundle": [f"{lib['name']}@{lib['version']}" for lib in data.get("libraries", [])][:6],
        "kind": data["kind"],
        "basis": data.get("basis") or "data",
        "focus": focus,
    }
    if expires_at:
        pisa["expiresAt"] = expires_at
    return {
        "resourceUri": view_uri(str(data["drawing_id"])),
        "mimeType": "text/html;profile=mcp-app",
        "html": data["html"],
        "toolInput": data["data"],
        "csp": {},
        "permissions": {},
        "pisa": pisa,
    }


def is_uuid(value: str) -> bool:
    return bool(_UUID.match(value))
