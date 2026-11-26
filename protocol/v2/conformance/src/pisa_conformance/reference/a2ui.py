"""A2UI 1.0 surfaces in the PISA core catalog, built from what a backend turn already computes.

The converters take build 60's v1 object shapes (protocol/schema/pisa-device-protocol.v1.json: present.object,
sheet blocks, K33 drawings, cards) and return A2UI createSurface messages. That makes the v1 fixtures an
executable check of MAPPING.md (tests/test_v1_mapping.py).
"""
from __future__ import annotations

from typing import Any

CATALOG_ID = "urn:pisa:a2ui:catalog:core:1"
A2UI_VERSION = "1.0"


class SurfaceBuilder:
    """Collects a flat A2UI component list; ids are short and stable within the surface."""

    def __init__(self, surface_id: str) -> None:
        self.surface_id = surface_id
        self.components: list[dict[str, Any]] = []
        self._n = 0

    def add(self, component: str, *, id: str | None = None, **props: Any) -> str:
        if id is None:
            self._n += 1
            id = f"c{self._n}"
        self.components.append({"id": id, "component": component, **{k: v for k, v in props.items() if v is not None}})
        return id

    def column(self, children: list[str], *, id: str | None = None) -> str:
        return self.add("Column", id=id, children=children)

    def create(self, data_model: dict[str, Any] | None = None) -> dict[str, Any]:
        if not any(c["id"] == "root" for c in self.components):
            raise ValueError("a surface needs a root component")
        body: dict[str, Any] = {"surfaceId": self.surface_id, "catalogId": CATALOG_ID, "components": self.components}
        if data_model is not None:
            body["dataModel"] = data_model
        return {"version": A2UI_VERSION, "createSurface": body}

    def fallback_text(self) -> str:
        return fallback_of(self.components)


# ------------------------------------------------------------------------------------------------------
# Fallback text: every component has one (CATALOG.md). Used for glasses, the watch, VoiceOver summaries and
# for any component a renderer cannot draw.
def component_fallback(c: dict[str, Any]) -> str:
    kind = c.get("component")
    if "fallback" in c:
        return c["fallback"]
    lit = lambda v: v if isinstance(v, str) else ""
    if kind == "Text":
        t = lit(c.get("text"))
        return f"«{t}» ({c['source']})" if c.get("quoted") and c.get("source") else t
    if kind == "Fact":
        return f"{lit(c.get('label'))}: {lit(c.get('value'))}"
    if kind == "Stat":
        return " ".join(x for x in [lit(c.get("label")), lit(c.get("value")), c.get("unit", "")] if x)
    if kind in ("BulletList",):
        return "\n".join(c["items"])
    if kind == "Chips":
        return ", ".join(c["items"])
    if kind == "Link":
        return f"{c['title']} ({c['host']})"
    if kind == "Image":
        return c["alt"]
    if kind == "Check":
        src = f" ({c['source']})" if c.get("source") else ""
        return f"{c['requirement']}: {c['status']}{src}"
    if kind == "Sources":
        return "Sources: " + ", ".join(i["host"] for i in c["items"])
    if kind == "Button":
        return c["label"]
    if kind == "Choice":
        return c["question"] + "".join(f"\n{i + 1}. {o['label']}" for i, o in enumerate(c["options"]))
    if kind == "Section":
        return lit(c.get("title"))
    return ""


def fallback_of(components: list[dict[str, Any]], root: str = "root") -> str:
    by_id = {c["id"]: c for c in components}

    def walk(cid: str) -> list[str]:
        c = by_id.get(cid)
        if c is None:
            return []
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

    return "\n".join(x for x in walk(root) if x)[:1000]


# ------------------------------------------------------------------------------------------------------
# v1 object -> A2UI
def _ref_fallback(kind: str, ref: dict[str, Any]) -> str:
    return {"plan": "The plan for {date}", "place": "A place", "item": "An item", "changes": "What changed",
            "setting": "A setting"}[kind].format(**{k: v for k, v in ref.items() if isinstance(v, str)})


def ref_string(kind: str, ref: dict[str, Any]) -> str:
    if kind == "plan":
        return f"plan:{ref['date']}"
    if kind == "place":
        return f"place:{ref['place_id']}"
    if kind == "item":
        return ref["ref"]
    if kind == "changes":
        return f"changes:{ref['since'] or 'all'}"
    if kind == "setting":
        return f"setting:{ref['section']}"
    raise ValueError(kind)


def place_summary(b: SurfaceBuilder, p: dict[str, Any], id: str | None = None) -> str:
    checks = [{"id": f"k{i}", "component": "Check", "requirement": c["requirement"], "status": c["status"],
               **({"source": c["source"]} if c.get("source") else {})} for i, c in enumerate(p.get("checks", []))]
    fb = f"{p['name']}: {p['why']}"
    if p.get("distance_m") is not None:
        fb += f" ({p['distance_m']} m)"
    return b.add("PlaceSummary", id=id, placeId=p["place_id"], name=p["name"], why=p["why"],
                 distanceM=p.get("distance_m"), travelMin=p.get("travel_min"), openNow=p.get("open_now"),
                 rating=p.get("rating"), ratingCount=p.get("rating_count"), priceLevel=p.get("price_level"),
                 closesAt=p.get("closes_at"), opensAt=p.get("opens_at"), lat=p.get("lat"), lon=p.get("lon"),
                 photoCount=p.get("photo_count"), fits=p.get("fits") or None, different=p.get("different"),
                 checks=checks or None, fallback=fb[:400])


def sources_component(b: SurfaceBuilder, sources: list[dict[str, Any]]) -> str:
    items = []
    for s in sources:
        host = s.get("host") or s["url"].split("/")[2]
        items.append({"title": s["title"], "url": s["url"], "host": host})
    return b.add("Sources", items=items)


def chart_from_k33(block: dict[str, Any]) -> dict[str, Any]:
    """K33 chart block -> the catalog's Vega-Lite subset (one inline table, one mark)."""
    mark = {"bar": "bar", "line": "line", "area": "area", "point": "point", "pie": "arc"}[block["mark"]]
    xkind = {"time": "temporal", "category": "nominal", "number": "quantitative"}[block.get("x", {}).get("kind", "category")]
    values = [{"x": pt["x"], "y": pt["y"], "series": s["name"]} for s in block["series"] for pt in s["points"]]
    multi = len(block["series"]) > 1
    if mark == "arc":
        enc = {"theta": {"field": "y", "type": "quantitative"}, "color": {"field": "x", "type": "nominal"}}
    else:
        enc = {"x": {"field": "x", "type": xkind, "title": block.get("x", {}).get("label", "")[:60] or None},
               "y": {"field": "y", "type": "quantitative", "title": block.get("y", {}).get("label", "")[:60] or None}}
        enc = {k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in enc.items()}
        if multi:
            enc["color"] = {"field": "series", "type": "nominal"}
    spec = {"$schema": "https://vega.github.io/schema/vega-lite/v6.json", "mark": mark, "data": {"values": values}, "encoding": enc}
    if block.get("title"):
        spec["title"] = block["title"]
    return spec


def _chart_fallback(block: dict[str, Any]) -> str:
    parts = []
    for s in block["series"]:
        pts = ", ".join(f"{p['x']}: {p['y']}" for p in s["points"][:12])
        parts.append(f"{s['name']}: {pts}")
    unit = block.get("y", {}).get("unit")
    return ("; ".join(parts) + (f" ({unit})" if unit else ""))[:400]


def block_to_components(b: SurfaceBuilder, block: dict[str, Any]) -> str | None:
    t = block["type"]
    if t == "text":
        return b.add("Text", text=block["text"])
    if t == "image":
        return b.add("Image", url=block["url"], alt=block["alt"], credit=block.get("credit"))
    if t == "link":
        return b.add("Link", url=block["url"], title=block["title"], host=block["host"])
    if t == "list":
        return b.add("BulletList", items=block["items"])
    if t == "fact":
        return b.add("Fact", label=block["label"], value=block["value"])
    if t == "section":
        return b.add("Text", text=block["title"], variant="headline")
    if t == "table":
        rows = [[(c if isinstance(c, (str, int, float)) or c is None else c.get("text", "")) for c in r] for r in block["rows"]]
        fb = "; ".join(" | ".join(str(x) for x in r if x is not None) for r in rows[:6])
        return b.add("Table", columns=block["columns"], rows=rows, fallback=fb[:400] or "A table")
    if t == "stat":
        return b.add("Stat", label=block["label"], value=block["value"], unit=block.get("unit"), trend=block.get("trend"))
    if t == "chips":
        return b.add("Chips", items=block["items"])
    if t == "divider":
        return b.add("Divider")
    if t == "handoff":
        fb = f"{block['title']}" + (f" ({block['subtitle']})" if block.get("subtitle") else "")
        return b.add("Handoff", handoffId=block["handoff_id"], kind=block["kind"], title=block["title"],
                     subtitle=block.get("subtitle"), url=block.get("url"), phone=block.get("phone"),
                     text=block.get("text"), notes=block.get("notes"), fallback=fb[:400])
    if t == "chart":
        return b.add("Chart", spec=chart_from_k33(block), unit=block.get("y", {}).get("unit"), fallback=_chart_fallback(block))
    if t == "diagram":
        nodes = [{k: v for k, v in n.items() if k in ("id", "label", "shape", "group")} for n in block["nodes"]]
        edges = [{k: v for k, v in e.items() if k in ("from", "to", "label", "style", "arrow")} for e in block["edges"]]
        groups = [{"id": g["id"], "label": g["label"]} for g in block.get("groups", [])]
        style = {"flow": "flow"}.get(block.get("style", "flow"), "structure")
        fb = " → ".join(n["label"] for n in nodes)[:400]
        return b.add("Diagram", style=style, direction=block.get("direction", "down"), nodes=nodes, edges=edges,
                     groups=groups or None, fallback=fb)
    if t == "timeline":
        items = [{"id": it.get("id", f"t{i}"), "at": str(it.get("at", it.get("when", ""))) or "-", "title": it.get("title", it.get("label", ""))[:120] or "-",
                  **({"detail": it["detail"][:200]} if it.get("detail") else {})} for i, it in enumerate(block["items"])]
        return b.add("Timeline", items=items, fallback="; ".join(f"{i['at']} {i['title']}" for i in items)[:400])
    if t == "map":
        pins = [{k: v for k, v in p.items() if k in ("id", "name", "lat", "lon", "badge", "pick")} for p in block["pins"]]
        return b.add("Map", pins=pins, fallback=", ".join(p["name"] for p in pins)[:400])
    if t == "findings":
        cards = []
        for row in block["rows"]:
            kids = [b.add("Text", text=row["name"], variant="headline")]
            chips = [x for x in [("Pick" if row.get("pick") else None), row.get("walk"), row.get("price"),
                                 (f"★ {row['rating']}" if row.get("rating") is not None else None)] if x]
            if chips:
                kids.append(b.add("Chips", items=[str(c)[:40] for c in chips]))
            if row.get("why"):
                kids.append(b.add("Text", text=row["why"]))
            if row.get("pros"):
                kids.append(b.add("BulletList", items=[f"+ {p}" for p in row["pros"]][:8]))
            if row.get("cons"):
                kids.append(b.add("BulletList", items=[f"− {c}" for c in row["cons"]][:8]))
            cards.append(b.add("Card", child=b.column(kids)))
        return b.column(cards)
    if t == "product":
        kids = [b.add("Text", text=block["title"], variant="headline"),
                b.add("Fact", label=block.get("shop", "Price"), value=f"{block['price']} {block.get('currency', '')}".strip())]
        if block.get("url"):
            kids.append(b.add("Link", url=block["url"], title=block["title"], host=block["url"].split("/")[2]))
        return b.add("Card", child=b.column(kids))
    return None  # unknown v1 block: skipped, as build 60 does


def object_to_surface(obj: dict[str, Any], surface_id: str) -> tuple[dict[str, Any], str] | None:
    """v1 present.object -> (createSurface, fallbackText). drawing returns None (Ring 2, see mcp_app_from_drawing)."""
    kind = obj["kind"]
    b = SurfaceBuilder(surface_id)
    if "ref" in obj:
        b.add("Ref", id="root", ref=ref_string(kind, obj["ref"]), detail=obj["ref"].get("detail", "card"),
              fallback=_ref_fallback(kind, obj["ref"]))
        return b.create(), b.fallback_text()
    data = obj["data"]
    if kind == "note":
        kids = [b.add("Text", text=data["title"], variant="headline"), b.add("BulletList", items=data["lines"])]
        if data.get("sources"):
            kids.append(sources_component(b, data["sources"]))
        b.add("Card", id="root", child=b.column(kids), tone="warning" if data["kind"] == "warning" else "info")
    elif kind == "choice":
        b.add("Choice", id="root", question=data["question"],
              options=[{k: v for k, v in o.items() if k in ("id", "label", "detail")} for o in data["options"]])
    elif kind == "places":
        kids = [b.add("Text", text=data["title"], variant="headline")] + [place_summary(b, p) for p in data["places"]]
        b.column(kids, id="root")
    elif kind == "compare":
        cols = [""] + data["criteria"]
        rows = [[r["name"]] + [{"text": c["value"], "status": c["status"], **({"source": c["source"]} if c.get("source") else {})} for c in r["cells"]] for r in data["rows"]]
        fb = "; ".join(f"{r['name']}: " + ", ".join(f"{data['criteria'][i]} {c['value']}" for i, c in enumerate(r["cells"])) for r in data["rows"])
        kids = [b.add("Text", text=data["title"], variant="headline"), b.add("Table", columns=cols, rows=rows, fallback=fb[:400])]
        b.column(kids, id="root")
    elif kind == "sheet":
        kids = [b.add("Text", text=data["title"], variant="title")]
        for block in data["blocks"]:
            cid = block_to_components(b, block)
            if cid:
                kids.append(cid)
        if data.get("sources"):
            kids.append(sources_component(b, data["sources"]))
        if data.get("actions"):
            buttons = [b.add("Button", label=a["label"], style=a["style"],
                             action={"event": {"name": "pisa.sheet_action", "context": {"actionId": a["id"]}}}) for a in data["actions"]]
            kids.append(b.add("Row", children=buttons))
        b.column(kids, id="root")
    elif kind == "drawing":
        return None
    else:
        raise ValueError(f"unmapped v1 object kind {kind}")
    return b.create(), b.fallback_text()


def card_to_surface(card: dict[str, Any], surface_id: str) -> tuple[dict[str, Any], str]:
    """v1 `card` message -> surface (the pre-present fallback path, still sent by build 60's server)."""
    kind = card["kind"]
    if kind == "place":
        return object_to_surface({"kind": "place", "ref": {"place_id": card["place_id"], "detail": "card"}}, surface_id)  # type: ignore[return-value]
    if kind == "places":
        return object_to_surface({"kind": "places", "data": {"title": card["title"], "request": card.get("request", ""), "places": card["places"]}}, surface_id)  # type: ignore[return-value]
    b = SurfaceBuilder(surface_id)
    kids = [b.add("Text", text=card["title"], variant="headline")]
    if card.get("lines"):
        kids.append(b.add("BulletList", items=card["lines"][:8]))
    if kind == "sources" and card.get("sources"):
        kids.append(sources_component(b, card["sources"]))
    b.add("Card", id="root", child=b.column(kids), tone="warning" if kind == "warning" else "plain")
    return b.create(), b.fallback_text()


def mcp_app_from_drawing(data: dict[str, Any], focus: str = "take") -> dict[str, Any]:
    """v1 sealed drawing (D-103) -> an MCP Apps view (Ring 2). The HTML keeps its sealed CSP meta; the host
    adds its own wall (CONTRACT-2 §7). Libraries move from j-bundle:// to the host's pisa-bundle:// scheme."""
    html = data["html"].replace("j-bundle://", "pisa-bundle://")
    return {"resourceUri": f"ui://pisa/drawing/{data['drawing_id']}", "mimeType": "text/html;profile=mcp-app",
            "html": html, "toolInput": data["data"], "csp": {}, "permissions": {},
            "pisa": {"title": data["title"], "summary": data["summary"], "fallbackText": data["summary"],
                     "bundle": [f"{lib['name']}@{lib['version']}" for lib in data.get("libraries", [])],
                     "kind": data["kind"], "basis": data.get("basis", "data"), "focus": focus}}
