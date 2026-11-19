"""Contract 2's Ring 1 and Ring 2 (B2): every object and sheet block a turn can show, as A2UI 1.0
components of the catalog `core:1`, and drawings as MCP Apps views. Every output is checked against the
official schemas with the conformance kit (`protocol/v2/conformance`); the v1 fixtures of every `present` kind
are converted through the socket in `test_live_v2_frames.py`."""

import json
import uuid
from pathlib import Path
from typing import Any, get_args

import pytest
from pisa_conformance.validate import EXT_ID, errors

from pisa.domain.sheet import SheetBlock, SheetData
from pisa.services import a2ui
from pisa.services.session_messages import Present

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures"


# --- every object and block a turn can show ----------------------------------------------------------------
def _block(kind: str) -> dict[str, Any]:
    layout = {
        "width": 300,
        "height": 200,
        "nodes": {"n1": {"x": 0, "y": 0, "w": 80, "h": 40}, "n2": {"x": 0, "y": 100, "w": 80, "h": 40}},
        "edges": [{"points": [[40, 40], [40, 100]]}],
    }
    row = {
        "id": "r1",
        "kind": "place",
        "name": "Café Alpha",
        "pick": True,
        "why": "Quiet",
        "walk": "5 min",
        "rating": "4.5",
        "price": "€€",
        "open_at_asked": {"ok": True, "hours": "8-19", "source": "map"},
        "recognition": [{"guide": "Guide", "award": "Pick", "year": 2026, "url": "https://example.org/g"}],
        "lat": 45.0,
        "lon": 9.0,
        "pros": ["calm"],
        "cons": ["small"],
        "url": "https://example.org/a",
        "slots": {
            "day": "2026-11-20",
            "times": ["19:00", "20:00"],
            "url": "https://example.org/b",
            "source": "https://example.org/b",
        },
    }
    return {
        "text": {"type": "text", "text": "Two places fit."},
        "image": {
            "type": "image",
            "url": "https://example.org/p.jpg",
            "alt": "A street",
            "credit": "Example",
        },
        "link": {"type": "link", "url": "https://example.org/x", "title": "A page", "host": "example.org"},
        "list": {"type": "list", "items": ["one", "two"]},
        "fact": {"type": "fact", "label": "Open", "value": "until 19:00"},
        "section": {"type": "section", "title": "Nearby"},
        "table": {"type": "table", "columns": ["Name", "Min"], "rows": [["Alpha", 5], ["Beta", 2.5]]},
        "stat": {"type": "stat", "label": "Steps", "value": "8,400", "unit": "steps", "trend": "up"},
        "chips": {"type": "chips", "items": [f"tag {n}" for n in range(12)]},
        "divider": {"type": "divider"},
        "handoff": {
            "type": "handoff",
            "handoff_id": "AAAAAAAA-0000-4000-8000-000000000001",
            "kind": "call",
            "title": "Call the café",
            "phone": "+390200005678",
            "notes": ["Ask for a table"],
        },
        "diagram": {
            "type": "diagram",
            "style": "layers",
            "direction": "right",
            "groups": [{"id": "g1", "label": "Back"}],
            "nodes": [
                {"id": "n1", "label": "App", "shape": "actor"},
                {"id": "n2", "label": "Server", "shape": "store", "group": "g1", "detail": "x"},
            ],
            "edges": [{"from": "n1", "to": "n2", "style": "dashed", "arrow": "end", "label": "asks"}],
            "layout": layout,
        },
        "chart": {
            "type": "chart",
            "mark": "pie",
            "title": "Share",
            "x": {"label": "Kind", "kind": "category"},
            "y": {"label": "Count", "unit": "n"},
            "series": [
                {
                    "id": "s1",
                    "name": "All",
                    "points": [{"id": "p1", "x": "a", "y": 2}, {"id": "p2", "x": "b", "y": 3}],
                }
            ],
        },
        "timeline": {
            "type": "timeline",
            "items": [
                {"id": "t1", "at": "2026-11-18", "label": "Start"},
                {
                    "id": "t2",
                    "at": "2026-11-19T10:00",
                    "end": "2026-11-19T12:00",
                    "label": "Talk",
                    "detail": "Room 2",
                },
            ],
        },
        "findings": {
            "type": "findings",
            "rows": [
                row,
                {
                    "id": "r2",
                    "kind": "event",
                    "name": "Concert",
                    "at": "2026-11-21T21:00",
                    "venue": "Hall",
                    "age": "18+",
                },
            ],
        },
        "map": {
            "type": "map",
            "pins": [{"id": "r1", "name": "Café Alpha", "lat": 45.0, "lon": 9.0, "pick": True, "badge": "1"}],
        },
        "product": {
            "type": "product",
            "id": "p1",
            "title": "Kettle",
            "price": 39.9,
            "currency": "EUR",
            "shop": "Example Shop",
            "shipping": 4.5,
            "low_6m": 35.0,
            "url": "https://example.org/k",
            "source": "https://example.org/k",
            "image_url": "https://example.org/k.jpg",
        },
    }[kind]


BLOCK_TYPES = [get_args(m.model_fields["type"].annotation)[0] for m in get_args(get_args(SheetBlock)[0])]


@pytest.mark.parametrize("kind", BLOCK_TYPES)
def test_every_sheet_block_is_a_catalog_component(kind: str) -> None:
    blocks = [_block(kind)]
    if kind == "map":
        blocks.insert(0, _block("findings"))  # a pin names a row of the sheet
    data = SheetData.model_validate(
        {
            "title": "A sheet",
            "blocks": blocks,
            "lang": "en-US",
            "sources": [{"title": "", "url": "https://example.org/s", "host": ""}],
        }
    )
    present = Present.model_validate(
        {
            "present_id": str(uuid.uuid4()),
            "reply_to": None,
            "purpose": "the answer",
            "focus": "take",
            "lang": "en-US",
            "object": {
                "object_id": f"sheet:{uuid.uuid4()}",
                "kind": "sheet",
                "revision": 1,
                "data": data.model_dump(mode="json", by_alias=True),
            },
        }
    )
    body = present.model_dump(mode="json", by_alias=True)
    content = a2ui.present_content(body, a2ui.surface_id_for(body["object"]["object_id"]))
    assert content is not None
    assert errors(content, EXT_ID + "#/$defs/A2UISurfaceActivity") == []
    components = content["a2ui_operations"][0]["createSurface"]["components"]
    assert len(components) > 2, kind  # the title, the root, and the block's own


def test_long_object_ids_get_a_short_surface_id_and_every_kind_is_mapped() -> None:
    long = "email:" + "x" * 300
    assert a2ui.surface_id_for(long) != long and len(a2ui.surface_id_for(long)) <= a2ui.SURFACE_ID_MAX
    kinds = {json.loads(p.read_text())["object"]["kind"] for p in (FIXTURES / "valid").glob("present_*.json")}
    from pisa.services.session_messages import ObjectKind

    assert kinds == set(get_args(ObjectKind))  # each kind is in the fixture test above


def test_a_drawing_keeps_its_seal_and_becomes_an_mcp_apps_view() -> None:
    for path in sorted((FIXTURES / "drawings").glob("*.json")):
        drawing = json.loads(path.read_text())
        content = a2ui.mcp_app_from_drawing(drawing, "take")
        assert errors(content, EXT_ID + "#/$defs/McpAppActivity") == [], path.name
        assert content["html"] == drawing["html"] and content["toolInput"] == drawing["data"]
        assert content["csp"] == {} and content["permissions"] == {}
