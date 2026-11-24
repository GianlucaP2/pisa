"""Generates protocol/v2/catalog/pisa-core.catalog.v2.json, the PISA A2UI catalog core:2 (run tools/generate.py).

core:2 is core:1 (tools/gen_catalog.py, frozen) plus WeatherSummary: a superset with its own catalogId, beside core:1,
never a change to it (CONTRACT-2 §12). Every core:1 component is here unchanged, so a renderer of core:2 draws every
core:1 surface too. A server composes a surface in core:2 only when the app's hello listed core:2.
"""
import copy
import runpy
from pathlib import Path

V1 = runpy.run_path(str(Path(__file__).resolve().parent / "gen_catalog.py"))
comp, s, FB = V1["comp"], V1["s"], V1["FB"]

C = copy.deepcopy(V1["C"])  # core:1's components, unchanged and in their order
TEMP = {"type": "number", "minimum": -100, "maximum": 70, "description": "Degrees Celsius."}
CHANCE = {"type": "integer", "minimum": 0, "maximum": 100, "description": "Chance of precipitation, percent."}
CONDITION = {"type": "string", "minLength": 1, "maxLength": 60,
             "description": "The condition in words, in the surface's language (\"Light rain\", \"Pioggia debole\")."}
C["WeatherSummary"] = comp("WeatherSummary", "The forecast for one place and one day, as the turn's get_weather read it: the current hour (today only), the day's low, high and rain chance, and up to 24 hours. Temperatures in degrees Celsius, chances in percent; times are RFC 3339 with the place's UTC offset.", {
    "place": s(120), "date": {"type": "string", "format": "date", "description": "The forecast's day (YYYY-MM-DD)."},
    "condition": CONDITION,
    "now": {"type": "object", "additionalProperties": False, "required": ["tempC", "condition"], "description": "The current hour's forecast; absent for another day.",
            "properties": {"tempC": TEMP, "condition": CONDITION}},
    "minC": TEMP, "maxC": TEMP, "rainChance": CHANCE,
    "hourly": {"type": "array", "maxItems": 24, "description": "In time order: today the next 24 hours from the current one, another day its hours.",
               "items": {"type": "object", "additionalProperties": False, "required": ["time", "tempC", "rainChance", "condition"],
                         "properties": {"time": {"type": "string", "format": "date-time", "maxLength": 40}, "tempC": TEMP, "rainChance": CHANCE, "condition": CONDITION}}},
    "fallback": FB}, ["place", "date", "condition", "minC", "maxC", "rainChance", "hourly", "fallback"], "fallback")

cat = copy.deepcopy(V1["CATALOG"])
cat["$id"] = cat["catalogId"] = "urn:pisa:a2ui:catalog:core:2"
cat["title"] = "PISA core catalog, version 2"
cat["description"] = ("core:1 and WeatherSummary. " + V1["CATALOG"]["description"])
cat["components"] = C
cat["$defs"]["anyComponent"] = {"oneOf": [{"$ref": "#/components/" + k} for k in C], "discriminator": {"propertyName": "component"}}
CATALOG = cat
