"""Generates protocol/v2/catalog/pisa-core.catalog.v1.json, the PISA A2UI catalog core:1 (run tools/generate.py).

core:1 is FROZEN (protocol/v2/FROZEN.json): a catalog never changes after release. A new component means a new
generator for core:2, a superset with a new catalogId, beside this one (CONTRACT-2 §12).
"""
CT = "https://a2ui.org/specification/v1_0/common_types.json#/$defs/"
DS = {"$ref": CT + "DynamicString"}
CHILD = {"$ref": CT + "Child"}
CHILDREN = {"$ref": CT + "ChildList"}
ACTION = {"$ref": CT + "Action"}
FB = {"type": "string", "minLength": 1, "maxLength": 400,
      "description": "Plain text said or shown where this component cannot be drawn (unknown component, glasses, watch, VoiceOver summary). Required."}
def s(maxlen, minlen=1): return {"type": "string", "minLength": minlen, "maxLength": maxlen}
UUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
HTTPS = {"type": "string", "pattern": "^https://\\S+$", "maxLength": 2000}
STATUS = {"type": "string", "enum": ["verified", "likely", "unknown", "no"]}

def comp(name, desc, props, required, fallback_rule):
    p = {"component": {"const": name}}
    p.update(props)
    return {"type": "object", "description": desc, "x-pisa-fallback": fallback_rule,
            "properties": p, "required": ["component"] + required}

C = {}
C["Column"] = comp("Column", "Children stacked vertically.", {"children": CHILDREN, "spacing": {"type": "string", "enum": ["none", "tight", "normal", "loose"], "default": "normal"}}, ["children"], "children's fallbacks, one per line")
C["Row"] = comp("Row", "Children side by side; wraps to a Column below the compact width.", {"children": CHILDREN}, ["children"], "children's fallbacks joined by ' · '")
C["Card"] = comp("Card", "A grouped surface with the app's card style.", {"child": CHILD, "tone": {"type": "string", "enum": ["plain", "info", "warning", "done"], "default": "plain"}}, ["child"], "child's fallback")
C["Section"] = comp("Section", "A titled group.", {"title": DS, "child": CHILD}, ["title", "child"], "title, then child's fallback")
C["Divider"] = comp("Divider", "A thin rule.", {}, [], "nothing")
C["Text"] = comp("Text", "Text. quoted=true marks outside text (an email, a page, a person's words): drawn as a quotation, never in the app's own voice (D-013).", {
    "text": DS, "variant": {"type": "string", "enum": ["title", "headline", "body", "caption", "footnote"], "default": "body"},
    "quoted": {"type": "boolean", "default": False}, "source": {"type": "string", "maxLength": 120, "description": "Where quoted text came from (host or kind)."}}, ["text"], "text (quoted text prefixed by its source)")
C["Fact"] = comp("Fact", "A label and a value on one line.", {"label": DS, "value": DS}, ["label", "value"], "'label: value'")
C["Stat"] = comp("Stat", "A big number with a label.", {"label": DS, "value": DS, "unit": s(20), "trend": {"type": "string", "enum": ["up", "down", "flat"]}}, ["label", "value"], "'label value unit'")
C["BulletList"] = comp("BulletList", "Up to 8 short lines.", {"items": {"type": "array", "minItems": 1, "maxItems": 8, "items": s(200)}}, ["items"], "items, one per line")
C["Chips"] = comp("Chips", "Small labels.", {"items": {"type": "array", "minItems": 1, "maxItems": 8, "items": s(40)}}, ["items"], "items joined by ', '")
C["Link"] = comp("Link", "An https link; opening it asks Ring 0 (an in-app Safari view).", {"url": HTTPS, "title": s(120), "host": s(255)}, ["url", "title", "host"], "'title (host)'")
C["Image"] = comp("Image", "An https image from a page the server read this turn, or a place photo. Fetched by the app without cookies.", {"url": HTTPS, "alt": s(200), "credit": s(120)}, ["url", "alt"], "alt")
C["Check"] = comp("Check", "One requirement as checked: verified, likely, unknown or no, with its source.", {"requirement": s(80), "status": STATUS, "source": {"type": "string", "maxLength": 120}}, ["requirement", "status"], "'requirement: status (source)'")
C["Sources"] = comp("Sources", "The links behind an answer.", {"items": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "object", "additionalProperties": False, "required": ["title", "url", "host"], "properties": {"title": s(120), "url": {"type": "string", "pattern": "^https?://\\S+$", "maxLength": 2000}, "host": s(255)}}}}, ["items"], "'Sources: ' + hosts")
C["Table"] = comp("Table", "A small table; cells are text, numbers, or {text, status} (a checked value).", {"columns": {"type": "array", "minItems": 1, "maxItems": 7, "items": s(40, 0)},
    "rows": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "array", "minItems": 1, "maxItems": 7, "items": {"oneOf": [{"type": ["string", "number", "null"], "maxLength": 120}, {"type": "object", "additionalProperties": False, "required": ["text", "status"], "properties": {"text": s(120, 0), "status": STATUS, "source": {"type": "string", "maxLength": 120}}}]}}}, "fallback": FB}, ["columns", "rows", "fallback"], "fallback")
VL_FIELD = {"type": "object", "additionalProperties": False, "required": ["field", "type"], "properties": {
    "field": s(40), "type": {"type": "string", "enum": ["quantitative", "temporal", "nominal", "ordinal"]}, "title": s(60), "aggregate": {"type": "string", "enum": ["sum", "mean", "count", "min", "max"]}}}
C["Chart"] = comp("Chart", "A chart drawn natively (Swift Charts). The wire format is a strict subset of Vega-Lite v6: one inline data table, one mark, x/y/color/theta encodings, no transforms, no URLs, no expressions. Anything beyond the subset is a Ring 2 view.", {
    "spec": {"type": "object", "additionalProperties": False, "required": ["mark", "data", "encoding"], "properties": {
        "$schema": {"const": "https://vega.github.io/schema/vega-lite/v6.json"},
        "title": s(120),
        "mark": {"type": "string", "enum": ["bar", "line", "area", "point", "arc", "rule"]},
        "data": {"type": "object", "additionalProperties": False, "required": ["values"], "properties": {"values": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "object", "maxProperties": 8, "additionalProperties": {"type": ["string", "number", "boolean", "null"]}}}}},
        "encoding": {"type": "object", "additionalProperties": False, "properties": {"x": VL_FIELD, "y": VL_FIELD, "color": VL_FIELD, "theta": VL_FIELD}}}},
    "unit": s(20), "fallback": FB}, ["spec", "fallback"], "fallback")
C["Diagram"] = comp("Diagram", "A flow or structure diagram laid out natively (build 60's K33 diagram). Mermaid sources are Ring 2.", {
    "style": {"type": "string", "enum": ["flow", "structure"]}, "direction": {"type": "string", "enum": ["down", "right"], "default": "down"},
    "nodes": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "object", "additionalProperties": False, "required": ["id", "label"], "properties": {"id": {"type": "string", "pattern": "^[a-z0-9_]{1,24}$"}, "label": s(60), "shape": {"type": "string", "enum": ["box", "store", "actor", "note"], "default": "box"}, "group": {"type": "string", "pattern": "^[a-z0-9_]{1,24}$"}}}},
    "edges": {"type": "array", "maxItems": 80, "items": {"type": "object", "additionalProperties": False, "required": ["from", "to"], "properties": {"from": s(24), "to": s(24), "label": s(40), "style": {"type": "string", "enum": ["solid", "dashed"]}, "arrow": {"type": "string", "enum": ["end", "both", "none"]}}}},
    "groups": {"type": "array", "maxItems": 10, "items": {"type": "object", "additionalProperties": False, "required": ["id", "label"], "properties": {"id": s(24), "label": s(60)}}},
    "fallback": FB}, ["style", "nodes", "edges", "fallback"], "fallback")
C["Timeline"] = comp("Timeline", "Dated items in order.", {"items": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object", "additionalProperties": False, "required": ["id", "at", "title"], "properties": {"id": s(40), "at": s(40), "title": s(120), "detail": s(200)}}}, "fallback": FB}, ["items", "fallback"], "fallback")
C["Map"] = comp("Map", "Pins on a native map.", {"pins": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "object", "additionalProperties": False, "required": ["id", "name", "lat", "lon"], "properties": {"id": s(40), "name": s(120), "lat": {"type": "number", "minimum": -90, "maximum": 90}, "lon": {"type": "number", "minimum": -180, "maximum": 180}, "badge": s(60), "pick": {"type": "boolean"}}}}, "fallback": FB}, ["pins", "fallback"], "fallback")
C["PlaceSummary"] = comp("PlaceSummary", "One place the turn found: the decision facts first; photos and details come from REST by placeId.", {
    "placeId": UUID, "name": s(120), "why": s(200), "distanceM": {"type": ["integer", "null"]}, "travelMin": {"type": ["integer", "null"]},
    "openNow": {"type": ["boolean", "null"]}, "closesAt": s(40), "opensAt": s(40), "rating": {"type": ["number", "null"]}, "ratingCount": {"type": ["integer", "null"]},
    "priceLevel": {"type": "integer", "minimum": 0, "maximum": 4}, "lat": {"type": ["number", "null"]}, "lon": {"type": ["number", "null"]},
    "photoCount": {"type": "integer", "minimum": 0}, "fits": {"type": "array", "maxItems": 3, "items": s(60)}, "different": {"type": "boolean"},
    "checks": {"type": "array", "maxItems": 6, "items": {"$ref": "#/components/Check"}, "description": "Inline Check objects (their component key included)."},
    "fallback": FB}, ["placeId", "name", "why", "fallback"], "fallback")
C["Ref"] = comp("Ref", "Something the server holds (plan, place, task, event, email, draft, approval, claim, thing, changes, setting): Ring 0 draws it from REST, so the object is the truth, not a copy.", {
    "ref": {"type": "string", "pattern": "^(plan:\\d{4}-\\d{2}-\\d{2}|place:[0-9a-f-]{36}|(task|event|email|draft|approval|claim|thing):\\S{1,1100}|changes:(all|\\S{1,40})|setting:[a-z_]{1,40})$"},
    "detail": {"type": "string", "enum": ["card", "full"], "default": "card"}, "fallback": FB}, ["ref", "fallback"], "fallback")
C["Button"] = comp("Button", "A tap. Never an approval: approvals are Ring 0 (interrupts). Action names are the closed list pisa.*.", {"label": s(40), "style": {"type": "string", "enum": ["primary", "plain", "undo"], "default": "plain"}, "action": ACTION}, ["label", "action"], "label")
C["Choice"] = comp("Choice", "A question answerable by tap or voice; a tap sends action pisa.choose with optionId.", {"question": s(200), "options": {"type": "array", "minItems": 2, "maxItems": 5, "items": {"type": "object", "additionalProperties": False, "required": ["id", "label"], "properties": {"id": {"type": "string", "pattern": "^o[1-5]$"}, "label": s(40), "detail": s(80)}}}}, ["question", "options"], "question, then '1. label' lines")
C["Handoff"] = comp("Handoff", "The app hands a prepared act to the system (open a page, call, message, map) after the user's tap.", {"handoffId": UUID, "kind": {"type": "string", "enum": ["open_url", "call", "message", "map"]}, "title": s(120), "subtitle": s(120),
    "url": {"type": "string", "maxLength": 2000, "pattern": "^(https://\\S+|tel:\\+[1-9][0-9]{5,14}|sms:(\\+[1-9][0-9]{5,14})?&body=[A-Za-z0-9%._~-]*)$"}, "phone": {"type": "string", "pattern": "^\\+[1-9][0-9]{5,14}$"}, "text": s(3000), "notes": {"type": "array", "maxItems": 4, "items": s(200)}, "fallback": FB}, ["handoffId", "kind", "title", "fallback"], "fallback")

cat = {
  "$schema": "https://a2ui.org/specification/v1_0/catalog_definition.json",
  "$id": "urn:pisa:a2ui:catalog:core:1",
  "protocolVersion": "1.0",
  "catalogId": "urn:pisa:a2ui:catalog:core:1",
  "title": "PISA core catalog, version 1",
  "description": "Small primitives the PISA app draws natively (Ring 1). Additive only: a new component means a new catalog version with a new catalogId. Every component has a text fallback (x-pisa-fallback says where it comes from); a renderer that cannot draw a component shows that text.",
  "instructions": "Compose with Column, Row, Card and Section. Outside text (emails, pages, people's words) goes only in Text with quoted=true. Never build an approval from components: approvals are interrupts. Actions: pisa.choose {optionId}, pisa.ask {elementId,label}, pisa.sheet_action {actionId}, pisa.handoff_opened {handoffId}, pisa.undo {undoId}, pisa.act {action: go|call|plan_it|open|details, ref}.",
  "components": C,
  "functions": {},
  "$defs": {
    "anyComponent": {"oneOf": [{"$ref": "#/components/" + k} for k in C], "discriminator": {"propertyName": "component"}},
    "anyFunction": {"not": {}}
  }
}
CATALOG = cat

