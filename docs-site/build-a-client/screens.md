# Screens: Ring 1 surfaces and Ring 2 views

Besides words, Pisa shows things: two cafés with their opening hours, a choice between two trains, a chart of the
week's temperatures. A client draws them in one of three rings, and says in its `hello` which it can draw:

| Ring | What | The client draws it with |
|---|---|---|
| 0 | the client's own screens for things the server holds (a task, an event, an approval, the plan) | its own code, from REST |
| 1 | **A2UI surfaces**: a tree of components from Pisa's catalog `urn:pisa:a2ui:catalog:core:1` | native widgets |
| 2 | **MCP Apps views**: a self-contained HTML page (a diagram, an interactive chart) | a sandboxed web view |

A client that lists only Ring 0 gets text only. Ring 1 needs `"rings": [0, 1]` and the catalog in
`a2ui."1.0".supportedCatalogIds`; Ring 2 needs `2` in `rings` and `"mcpApps": {"protocolVersions":
["2026-01-26"], "mimeTypes": ["text/html;profile=mcp-app"], "bundledLibraries": [...]}`. Over `POST /v2/runs`
the server assumes Rings 0 and 1.

Example: [`screens.py`](examples/screens.py) keeps the surfaces, draws them as text, and makes the frames a
client sends back. CI runs it over every fixture stream.

## A surface arrives

An `ACTIVITY_SNAPSHOT` with `activityType: "a2ui-surface"`; its `messageId` is the surface id, its `content`
(`$defs/A2UISurfaceActivity`):

```json
{"a2ui_operations": [{"version": "1.0", "createSurface": {
    "surfaceId": "places:demo-1", "catalogId": "urn:pisa:a2ui:catalog:core:1",
    "components": [
      {"id": "root", "component": "Column", "children": ["c1", "c2"]},
      {"id": "c1", "component": "Text", "text": "Two cafés nearby", "variant": "headline"},
      {"id": "c2", "component": "PlaceSummary", "placeId": "11111111-1111-4111-8111-111111111111",
       "name": "Café Alpha", "openNow": true, "closesAt": "19:00", "travelMin": 5, "why": "Quiet, open until 19:00",
       "fallback": "Café Alpha: Quiet, open until 19:00 (350 m)"}]}}],
 "pisa": {"focus": "take", "fallbackText": "Two cafés nearby\nCafé Alpha: Quiet, open until 19:00 (350 m)",
          "purpose": "Two cafés", "lang": "en-US"}}
```

(shortened from `protocol/v2/fixtures/streams/turn_places.jsonl`.)

- `a2ui_operations`: A2UI 1.0 messages, applied in order: `createSurface {surfaceId, catalogId, components,
  dataModel?}`, `updateComponents {surfaceId, components}` (replace by `id`), `updateDataModel {surfaceId,
  path, value}` (a JSON Pointer into the data model), `deleteSurface {surfaceId}`.
- Components are a flat list; `children` (a list of ids) and `child` (one id) make the tree; drawing starts at
  `root`.
- A value written `{"@path": "/changes/0/title"}` is read from the surface's data model.
- `pisa.focus`: `take` (bring it to the front now), `offer` (show it beside the words), `quiet` (put it where it
  belongs, no fuss). `pisa.fallbackText`: what to show or say where the surface can't be drawn (a watch, a
  screen reader, an unknown component). `pisa.expiresAt`: remove it from view at that time, even with no
  `deleteSurface` (keep it in the history). `pisa.basis`: `data` (from the user's data) or `knowledge` (the
  model's general knowledge). `pisa.replyTo`: the turn it answers.

<!-- include: examples/screens.py#surfaces -->
```python
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
```

### Updates

An `ACTIVITY_DELTA` on the same surface carries a JSON Patch on the content; Pisa uses it to append A2UI
operations (`{"op": "add", "path": "/a2ui_operations/-", "value": {"updateDataModel": ...}}`). Apply the patch,
then the new operations (`protocol/v2/fixtures/streams/today_section.jsonl`).

## The catalog, mapped to widgets

The 24 components of `core:1` (`protocol/v2/catalog/pisa-core.catalog.v1.json` has every property). A component
the client doesn't know, or can't draw here, is replaced by its `fallback` text when it has one; failing that,
the whole surface by `pisa.fallbackText` (`unknown_component.jsonl` is that case).

| Component | Properties | A native client draws |
|---|---|---|
| `Column` | `children`, `spacing` | a vertical stack (SwiftUI `VStack`, Compose `Column`, CSS flex column) |
| `Row` | `children` | a horizontal stack that wraps to a column when narrow |
| `Card` | `child`, `tone` (`plain`, `info`, `warning`, `done`) | a grouped box in the app's card style |
| `Section` | `title`, `child` | a titled group |
| `Divider` | | a thin rule |
| `Text` | `text`, `variant` (`title`, `headline`, `body`, `caption`, `footnote`), `quoted`, `source` | a label in that text style. `quoted: true` is outside text (an email, a page, a person's words): draw it as a quotation, never as Pisa's own voice |
| `Fact` | `label`, `value` | a label and value on one line |
| `Stat` | `label`, `value`, `unit`, `trend` (`up`, `down`, `flat`) | a big number with its unit and a trend arrow |
| `BulletList` | `items` (up to 8) | a bulleted list |
| `Chips` | `items` (up to 8) | small capsules |
| `Link` | `url` (https), `title`, `host` | a link that opens in an in-app browser |
| `Image` | `url` (https), `alt`, `credit` | an image, fetched without cookies |
| `Check` | `requirement`, `status` (`verified`, `likely`, `unknown`, `no`), `source` | a checklist row with a status mark |
| `Sources` | `items: [{title, url, host}]` | the links behind an answer, small |
| `Table` | `columns`, `rows` (cells: text, number, or `{text, status}`), `fallback` | a small table |
| `Chart` | `spec` (a strict subset of Vega-Lite v6: one inline data table, one mark, x/y/color/theta), `unit`, `fallback` | a native chart (Swift Charts, a chart library) |
| `Diagram` | `style` (`flow`, `structure`), `direction`, `nodes`, `edges`, `groups`, `fallback` | a laid-out box-and-arrow diagram |
| `Timeline` | `items: [{id, at, title, detail}]`, `fallback` | dated rows in order |
| `Map` | `pins: [{id, name, lat, lon, badge, pick}]`, `fallback` | a native map with pins |
| `PlaceSummary` | `placeId`, `name`, `openNow`, `opensAt`, `closesAt`, `distanceM`, `travelMin`, `rating`, `priceLevel`, `why`, `checks`... `fallback` | a place card, decision facts first; photos and details from REST by `placeId` |
| `Ref` | `ref` (`plan:`, `place:`, `task:`, `event:`, `email:`, `draft:`, `approval:`, `claim:`, `thing:`, `changes:`, `setting:`), `detail` (`card`, `full`), `fallback` | the client's own Ring 0 screen for that object, fetched over REST |
| `Button` | `label`, `style` (`primary`, `plain`, `undo`), `action` | a button; never an approval |
| `Choice` | `question`, `options: [{id: o1..o5, label, detail}]` | a question with 2 to 5 answers to tap (the user may also answer by voice) |
| `Handoff` | `handoffId`, `kind` (`open_url`, `call`, `message`, `map`), `title`, `subtitle`, `url`, `phone`, `text`, `notes`, `fallback` | a prepared act the system carries out after the user's tap |

<!-- include: examples/screens.py#widgets -->
```python
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
```

## Taps

The client acknowledges every screen at once with `pisa.surface.ack` (`status`: `shown`, `queued`, `deferred`,
`unavailable`, `unsupported`; `surface`: `phone`, `glasses_display`, `watch`, `none`), and may report `seen`,
`expanded`, `kept` and `closed` with `pisa.surface.event` (ids only).

A tap is an A2UI 1.0 action (`renderer_to_agent.json`): `{"version": "1.0", "action": {name, surfaceId,
sourceComponentId, timestamp, context}}`. The names are Pisa's closed list:

| Action | Context | From |
|---|---|---|
| `pisa.choose` | `{optionId}` | a `Choice` option, or a `Button` that picks one |
| `pisa.ask` | `{elementId, label}` | "ask about this" on an element |
| `pisa.undo` | `{undoId}` | an Undo button |
| `pisa.act` | `{action: go, call, plan_it, open or details, ref}` | an act on an object |
| `pisa.sheet_action` | `{actionId}` | a ritual sheet's button |
| `pisa.handoff_opened` | `{handoffId}` | after the system opened a `Handoff` |

A `Button`'s `action.event` holds the name and the context (values in it may be `{"@path"}` bindings: resolve
them). On `/v2/live` the client sends `{"op": "a2ui", "message": <the action>}`; a choice or a question starts
a turn, whose answer arrives as a run on the conversation's thread.

<!-- include: examples/screens.py#actions -->
```python
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
```

Over HTTP, the action rides a run's `forwardedProps.pisa.a2uiAction`, on the conversation's thread, with no
messages and no `resume`. A surface id the server can't map to anything it showed is refused with 400
`validation_failed`; a choice no longer open ends the run `RUN_ERROR` `invalid_message`.

<!-- include: examples/chat_runs.py#a2ui-over-http -->
```python
# A tap on a surface, without a socket: the A2UI action (screens.tap_action) rides
# forwardedProps.pisa.a2uiAction, on the conversation's thread, with no messages and no resume.
action = {"version": "1.0", "action": {"name": "pisa.choose", "surfaceId": "places:demo-1",
          "sourceComponentId": "c2", "timestamp": "2026-11-18T10:00:00Z", "context": {"optionId": "o1"}}}  # fmt: skip
tap = {"threadId": thread, "runId": f"run-{uuid.uuid4()}", "messages": [],
       "forwardedProps": {"pisa": {"a2uiAction": action}}}  # fmt: skip
```

## Ring 2: MCP Apps views

An `ACTIVITY_SNAPSHOT` with `activityType: "mcp-apps"`; `messageId` is the view's `resourceUri`
(`ui://pisa/drawing/<id>`). Its `content` (`$defs/McpAppActivity`, from `drawings.jsonl`):

```json
{"resourceUri": "ui://pisa/drawing/77777777-7777-4777-8777-777777777777",
 "mimeType": "text/html;profile=mcp-app",
 "html": "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src pisa-bundle:; style-src 'unsafe-inline'\"></head><body><div id=\"m\"></div><script src=\"pisa-bundle://lib/mermaid.min.js\"></script></body></html>",
 "toolInput": {"source": "graph LR; A-->B; B-->C"},
 "csp": {}, "permissions": {},
 "pisa": {"title": "How the pieces talk", "summary": "Three steps: ask, query, answer.",
          "fallbackText": "Three steps: ask, query, answer.", "bundle": ["mermaid@12.2.0"],
          "kind": "flow", "basis": "data", "focus": "take"}}
```

Drawing it, as an MCP Apps host (MCP Apps 2026-01-26):

- Load `html` in a sandboxed web view with **no network at all** (`csp` domains are always empty, `permissions`
  always empty: no camera, microphone, location or clipboard).
- The libraries in `pisa.bundle` (`d3@7.9.0`, `mermaid@12.2.0`, `vega@6.4.0`, `vega-lite@6.5.0`,
  `vega-embed@7.3.0`, `vega-interpreter@2.3.2`) are loaded from `pisa-bundle://lib/...`: the client ships them and
  serves that scheme itself. List the ones you ship in `mcpApps.bundledLibraries`.
- Deliver `toolInput` to the page as the MCP Apps `ui/notifications/tool-input` notification.
- "Ask about this": when the page sends `ui/message` after the user's tap, start a turn with
  `forwardedProps.pisa.ask = {"source": "mcp-apps", "surfaceId": <resourceUri>, "label": <the element's text>}`.
- Can't draw it (a watch, no web view): show `pisa.title`, `pisa.summary` and `pisa.fallbackText`.
