"""Ring 1 and Ring 2 screens: A2UI 1.0 surfaces in the catalog `urn:pisa:a2ui:catalog:core:1`, and MCP Apps views.

    python screens.py recorded.jsonl [more.jsonl ...]

`Screens` keeps each surface (its components and data model), draws it as text from its `root`, and makes the
frames a client sends back: the acknowledgment (`pisa.surface.ack`) and a tap (an A2UI action). A real client draws
each component with a native widget (screens.md has the table); this one prints.
"""

import copy
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

Out = Callable[[str], None]
CATALOG = "urn:pisa:a2ui:catalog:core:1"


# [guide:surfaces]
class Surface:
    def __init__(self, surface_id: str, catalog: str) -> None:
        self.id, self.catalog = surface_id, catalog
        self.components: dict[str, dict[str, Any]] = {}
        self.data: dict[str, Any] = {}

    def apply(self, op: dict[str, Any]) -> None:
        """One A2UI 1.0 message: createSurface, updateComponents, updateDataModel (deleteSurface: Screens)."""
        if "createSurface" in op:
            body = op["createSurface"]
            self.components = {c["id"]: c for c in body.get("components", [])}
            self.data = copy.deepcopy(body.get("dataModel") or {})
        elif "updateComponents" in op:
            for component in op["updateComponents"].get("components", []):
                self.components[component["id"]] = component
        elif "updateDataModel" in op:
            body = op["updateDataModel"]
            set_pointer(self.data, body.get("path", "/"), body.get("value"))

    def value(self, v: Any) -> Any:
        """A dynamic value: a literal, or `{"@path": "/json/pointer"}` into the surface's data model."""
        if isinstance(v, dict) and "@path" in v:
            return get_pointer(self.data, v["@path"])
        return v


class Screens:
    """Every surface and view on screen, by surface id (the activity's messageId)."""

    def __init__(self, out: Out = print) -> None:
        self.out = out
        self.surfaces: dict[str, Surface] = {}
        self.content: dict[str, dict[str, Any]] = {}  # the activity's content as last received
        self.acks: list[dict[str, Any]] = []  # frames to send: one pisa.surface.ack per activity

    def snapshot(self, surface_id: str, activity: str, content: dict[str, Any]) -> None:
        self.content[surface_id] = copy.deepcopy(content)
        if activity == "mcp-apps":
            self.view(surface_id, content)
        elif activity == "a2ui-surface":
            self.replay(surface_id)
        self.acks.append(surface_ack(surface_id, "shown"))

    def delta(self, surface_id: str, activity: str, patch: list[dict[str, Any]]) -> None:
        """ACTIVITY_DELTA: a JSON Patch on the content; here it appends A2UI operations."""
        content = self.content.setdefault(surface_id, {"a2ui_operations": []})
        for change in patch:
            if change["op"] in ("add", "replace"):
                set_pointer(content, change["path"], change["value"])
        self.replay(surface_id)

    def replay(self, surface_id: str) -> None:
        content = self.content[surface_id]
        for op in content.get("a2ui_operations") or []:
            if "deleteSurface" in op:
                self.surfaces.pop(op["deleteSurface"]["surfaceId"], None)
                self.out(f"[surface {surface_id} removed]")
                return
            if "createSurface" in op:
                catalog = op["createSurface"].get("catalogId", "")
                self.surfaces[surface_id] = Surface(surface_id, catalog)
            if surface_id in self.surfaces:
                self.surfaces[surface_id].apply(op)
        surface = self.surfaces.get(surface_id)
        pisa = content.get("pisa") or {}
        if surface is None or surface.catalog != CATALOG or "root" not in surface.components:
            self.out(f"[{surface_id}] {pisa.get('fallbackText', '')}")  # can't draw it: its fallback text
            return
        self.out(f"[surface {surface_id}, focus {pisa.get('focus')}]")  # take: bring forward; offer; quiet
        for line in draw(surface, "root"):
            self.out("  " + line)

    def view(self, surface_id: str, content: dict[str, Any]) -> None:
        """Ring 2: an MCP Apps view (self-contained HTML). Draw it in a sandboxed web view with no network, or
        show its title, summary and fallback text."""
        pisa = content["pisa"]
        self.out(f"[view {surface_id}] {pisa['title']}: {pisa['summary']}")
        self.out(f"  {pisa['fallbackText']}")

    def focus(self, surface_id: str, mode: str) -> None:
        self.out(f"[bring {surface_id} back to the front ({mode})]")


# [/guide:surfaces]


# [guide:widgets]
def draw(surface: Surface, cid: str, depth: int = 0) -> list[str]:
    """One component as lines of text. Each branch says what a native client draws."""
    c = surface.components.get(cid)
    if c is None:
        return []
    v, kind = surface.value, c.get("component")
    children = [c["child"]] if "child" in c else c.get("children") or []
    kids = [line for child in children if isinstance(child, str) for line in draw(surface, child, depth + 1)]
    if kind in ("Column", "Row"):  # a vertical / horizontal stack (Row wraps to a column when narrow)
        return kids
    if kind == "Card":  # a grouped box; tone plain, info, warning or done
        return [f"┌ {c.get('tone', 'plain')}", *("│ " + k for k in kids), "└"]
    if kind == "Section":  # a titled group
        return [f"## {v(c.get('title'))}", *kids]
    if kind == "Divider":
        return ["────"]
    if kind == "Text":  # variant title, headline, body, caption, footnote; quoted = outside words, as a quote
        text = str(v(c.get("text")))
        return [f"> {text}  ({c.get('source', 'quoted')})" if c.get("quoted") else text]
    if kind == "Fact":
        return [f"{v(c['label'])}: {v(c['value'])}"]
    if kind == "Stat":  # a big number, with its unit and trend arrow
        trend = {"up": " ↑", "down": " ↓", "flat": " →"}.get(c.get("trend", ""), "")
        return [f"{v(c['value'])}{c.get('unit', '')}{trend}  {v(c['label'])}"]
    if kind == "BulletList":
        return ["• " + item for item in c["items"]]
    if kind == "Chips":
        return [" ".join(f"({item})" for item in c["items"])]
    if kind == "Link":  # opens in an in-app browser
        return [f"🔗 {c.get('title', c['url'])} <{c['url']}>"]
    if kind == "Image":  # fetched without cookies, only https
        return [f"[image: {c['alt']}]"]
    if kind == "Check":
        mark = {"verified": "✓", "likely": "~", "unknown": "?", "no": "✗"}[c["status"]]
        return [f"{mark} {c['requirement']}" + (f" ({c['source']})" if c.get("source") else "")]
    if kind == "Sources":
        return [f"source: {item['title']} ({item['host']})" for item in c["items"]]
    if kind == "Choice":  # a tap sends pisa.choose {optionId}; the user may also answer by voice
        return [c["question"], *(f"  [{o['id']}] {o['label']}" for o in c["options"])]
    if kind == "Button":  # style primary, plain, undo; a tap sends its action (tap_action)
        return [f"[{c['label']}]"]
    if kind == "PlaceSummary":  # decision facts first; photos and details from REST by placeId
        opened = "open" if c.get("openNow") else "closed"
        return [f"📍 {c['name']} · {opened} · {c.get('travelMin', '?')} min · {c.get('why', '')}"]
    if kind == "Ref":  # something the server holds: fetch it over REST and draw it (task:, event:, plan:...)
        return [f"[{c['ref']}] {c['fallback']}"]
    # Table, Chart, Diagram, Timeline, Map, Handoff and anything newer: draw it natively when you can, else its
    # fallback text (every one of these carries one)
    return [c.get("fallback") or f"[{kind}]"]


# [/guide:widgets]


# [guide:actions]
def surface_ack(surface_id: str, status: str, surface: str = "phone") -> dict[str, Any]:
    """Sent at once for every a2ui-surface or mcp-apps activity: shown, queued, deferred, unavailable or
    unsupported; on phone, glasses_display, watch or none."""
    return {"op": "custom", "name": "pisa.surface.ack",
            "value": {"surfaceId": surface_id, "status": status, "surface": surface}}  # fmt: skip


def tap_action(surface: Surface, component_id: str, option_id: str | None = None) -> dict[str, Any]:
    """The user tapped a Button (its action) or a Choice's option (`pisa.choose`): an A2UI 1.0 action, sent
    as `{"op": "a2ui", "message": ...}` on /v2/live, or as `forwardedProps.pisa.a2uiAction` on POST /v2/runs."""
    c = surface.components[component_id]
    if c["component"] == "Choice":
        name, context = "pisa.choose", {"optionId": option_id}
    else:
        event = c["action"]["event"]
        name, context = event["name"], {k: surface.value(x) for k, x in (event.get("context") or {}).items()}
    return {
        "version": "1.0",
        "action": {
            "name": name,
            "surfaceId": surface.id,
            "sourceComponentId": component_id,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "context": context,
        },
    }


# [/guide:actions]


def _parts(pointer: str) -> list[str]:
    return [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]] if pointer != "/" else []


def get_pointer(doc: Any, pointer: str) -> Any:
    for part in _parts(pointer):
        doc = doc[int(part)] if isinstance(doc, list) else doc.get(part) if isinstance(doc, dict) else None
    return doc


def set_pointer(doc: Any, pointer: str, value: Any) -> None:
    parts = _parts(pointer)
    for part in parts[:-1]:
        doc = doc[int(part)] if isinstance(doc, list) else doc.setdefault(part, {})
    last = parts[-1]
    if isinstance(doc, list):
        doc.append(value) if last == "-" else doc.__setitem__(int(last), value)
    else:
        doc[last] = value


def main(paths: list[str]) -> int:
    for path in paths:
        print(f"== {path}")
        screens = Screens()
        with open(path, encoding="utf-8") as lines:
            for line in lines:
                frame = json.loads(line) if line.strip() else {}
                event = frame.get("event", frame)
                if event.get("type") == "ACTIVITY_SNAPSHOT":
                    screens.snapshot(event["messageId"], event["activityType"], event["content"])
                elif event.get("type") == "ACTIVITY_DELTA":
                    screens.delta(event["messageId"], event["activityType"], event["patch"])
        for surface in screens.surfaces.values():
            for cid, c in surface.components.items():
                if c.get("component") in ("Button", "Choice"):
                    option = c["options"][0]["id"] if c["component"] == "Choice" else None
                    print("tap ->", json.dumps({"op": "a2ui", "message": tap_action(surface, cid, option)}))
        for ack in screens.acks:
            print("ack ->", json.dumps(ack))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
