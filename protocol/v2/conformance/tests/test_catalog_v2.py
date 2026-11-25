"""core:2: core:1 unchanged plus WeatherSummary, judged by the catalog a surface's createSurface names."""
import copy
import json

import pytest

from conftest import V2
from pisa_conformance.validate import CORE_1, CORE_2, server_frame_errors, surface_catalog, surface_errors

V1 = json.loads((V2 / "catalog" / "pisa-core.catalog.v1.json").read_text())
CAT2 = json.loads((V2 / "catalog" / "pisa-core.catalog.v2.json").read_text())


def hour(n, temp=14.5, chance=10):
    return {"time": f"2026-11-22T{n:02d}:00:00+01:00", "tempC": temp, "rainChance": chance, "condition": "Partly cloudy"}


def weather(**over):
    c = {"id": "root", "component": "WeatherSummary", "place": "Testville", "date": "2026-11-22",
         "condition": "Light rain", "now": {"tempC": 12.0, "condition": "Overcast"}, "minC": 8.5, "maxC": 15.0,
         "rainChance": 60, "hourly": [hour(n) for n in range(10, 24)],
         "fallback": "Testville, 2026-11-22: Light rain, 8.5 to 15 °C, rain chance 60%."}
    c.update(over)
    return c


def surface(component, catalog=CORE_2):
    return {"a2ui_operations": [{"version": "1.0", "createSurface": {
        "surfaceId": "card:weather-1", "catalogId": catalog, "components": [component]}}],
        "pisa": {"focus": "offer", "fallbackText": component.get("fallback", "-")}}


def test_core_2_is_core_1_and_weather_summary():
    assert CAT2["catalogId"] == CAT2["$id"] == CORE_2
    assert {k: v for k, v in CAT2["components"].items() if k != "WeatherSummary"} == V1["components"]
    assert list(CAT2["components"]) == [*V1["components"], "WeatherSummary"]
    refs = [r["$ref"] for r in CAT2["$defs"]["anyComponent"]["oneOf"]]
    assert refs == ["#/components/" + k for k in CAT2["components"]]
    ws = CAT2["components"]["WeatherSummary"]
    assert ws["properties"]["hourly"]["maxItems"] == 24
    assert set(ws["required"]) == {"component", "place", "date", "condition", "minC", "maxC", "rainChance", "hourly", "fallback"}


def test_a_weather_summary_validates_in_core_2_only():
    content = surface(weather())
    assert surface_catalog(content) == CORE_2
    assert surface_errors(content) == []
    assert surface_errors(surface(weather(), CORE_1)) != []  # not a core:1 component
    assert surface_errors(surface(weather(now=None) | {})) != []  # null is not absent
    another_day = weather(date="2026-11-23")
    del another_day["now"]
    assert surface_errors(surface(another_day)) == []  # no current hour for another day


def test_a_core_1_surface_is_a_core_2_surface():
    text = {"id": "root", "component": "Text", "text": "Hello"}
    assert surface_errors(surface(text, CORE_1)) == surface_errors(surface(text, CORE_2)) == []


@pytest.mark.parametrize("bad", [
    {"hourly": [hour(n % 24) for n in range(25)]},  # at most 24 hours
    {"rainChance": 101},
    {"hourly": [hour(10, chance=-1)]},
    {"minC": "8"},  # a number, not text
    {"date": "22/11/2026"},
    {"hourly": [hour(10) | {"time": "10:00"}]},  # RFC 3339
    {"hourly": [hour(10) | {"wind": 3}]},  # nothing else per hour
    {"now": {"tempC": 12.0}},
    {"fallback": ""},
])
def test_a_wrong_weather_summary_is_refused(bad):
    assert surface_errors(surface(weather(**bad))) != []


def test_missing_required_fields_are_refused():
    for key in ("place", "date", "condition", "minC", "maxC", "rainChance", "hourly", "fallback"):
        c = copy.deepcopy(weather())
        del c[key]
        assert surface_errors(surface(c)) != [], key


def test_a_frame_carrying_it_is_judged_by_its_catalog():
    def frame(content):
        return {"seq": 5, "threadId": "pisa-main", "event": {"type": "ACTIVITY_SNAPSHOT", "messageId": "card:weather-1",
                "activityType": "a2ui-surface", "content": content, "replace": True}}
    assert server_frame_errors(frame(surface(weather()))) == []
    assert server_frame_errors(frame(surface(weather(), CORE_1))) != []
