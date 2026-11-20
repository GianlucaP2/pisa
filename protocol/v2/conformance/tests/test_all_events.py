from pisa_conformance.reference.all_events import agui_events, pisa_events
from pisa_conformance.paths import SCHEMA
from pisa_conformance.validate import AGUI_ID, EXT_ID, errors

OFFICIAL_TYPES = 31


def test_every_agui_type_is_covered_and_valid():
    evs = agui_events()
    assert len({e["type"] for e in evs}) == OFFICIAL_TYPES
    for e in evs:
        assert errors(e, AGUI_ID) == [], e["type"]


def test_every_pisa_server_event_is_covered_and_valid():
    import json
    schema = json.loads(SCHEMA.read_text())
    evs = pisa_events()
    assert {e["name"] for e in evs} == set(schema["x-pisa-server-events"])
    for e in evs:
        assert errors(e, AGUI_ID) == [], e["name"]
        assert errors(e, EXT_ID + "#/$defs/ServerCustomEvent") == [], e["name"]
