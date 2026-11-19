"""The first skills from the research (section 5: 2, 3, 4, then 6, 7,
9), as recipes ready to keep. When the user asks one of these questions, Pisa answers with the public read
and offers once to keep it as a skill; on their yes to that offer (this turn, Pisa's own last words naming it)
the recipe goes through the own path: the trial on their data, then "Lo accendo?", their yes and Face ID. A
starter is data, never code: the recipe over the read-only allowlist (`views.PUBLIC_READS`). No I/O."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Starter:
    name: str  # the skill's name, as Pisa offers it
    tool: str  # the public read it is built on
    made_from: str  # the wish it stands for (their yes to Pisa's offer of it)
    checks: tuple[dict[str, str], ...]
    recipe: dict[str, Any]
    country: str | None = None  # the one country whose feed it reads (Rome's ZTL, Lazio's pharmacies)


def _sheet(tool: str, args: dict[str, str], each: str, block: dict[str, Any]) -> dict[str, Any]:
    """One read; its summary line, one block per item, the source's credit (CC BY and BY-SA ask for it)."""
    return {
        "reads": [{"tool": tool, "args": args}],
        "blocks": [
            {"type": "text", "text": "{{0.summary}}"},
            {"type": "each", "from": f"{{{{0.{each}}}}}", "block": block},
            {"type": "text", "text": "{{0.credit}}"},
        ],
    }


STARTERS: tuple[Starter, ...] = (
    Starter(
        "bus per casa", "next_departures",
        "vorrei che tu potessi dirmi quando passa il prossimo bus per casa",
        ({"input": "the next departures toward home", "shows": "text"},
         {"input": "each departure: line, direction, time", "shows": "fact"}),
        _sheet("next_departures", {"to": "home"}, "departures",
               {"type": "fact", "label": "{{item.line}} → {{item.toward}}", "value": "{{item.at}}"}),
    ),
    Starter(
        "problemi metro", "transit_alerts",
        "vorrei che tu potessi dirmi se la metro ha problemi",
        ({"input": "the alerts active now on the metro", "shows": "text"},
         {"input": "how many alerts", "has": "alert"}),
        _sheet("transit_alerts", {}, "alerts",
               {"type": "fact", "label": "{{item.lines}}", "value": "{{item.text}}"}),
    ),
    Starter(
        "allerte meteo", "weather_alerts",
        "vorrei che tu potessi dirmi se ci sono allerte meteo dove sono",
        ({"input": "today's and tomorrow's alert level where they are", "shows": "text"},
         {"input": "the alert level said, even when there is none", "has": "alert"}),
        _sheet("weather_alerts", {}, "alerts", {"type": "text", "text": "{{item.text}}"}),
    ),
    Starter(
        "ztl adesso", "ztl_status",
        "vorrei che tu potessi dirmi se la ZTL è attiva adesso",
        ({"input": "each Rome ZTL, active or not now", "shows": "text"},
         {"input": "the Centro Storico zone", "has": "Centro Storico"}),
        _sheet("ztl_status", {}, "zones", {"type": "text", "text": "{{item.name}}"}), "IT",
    ),
    Starter(
        "farmacia vicina", "nearest_pharmacy",
        "vorrei che tu potessi dirmi qual è la farmacia più vicina",
        ({"input": "the nearest pharmacies with their address", "shows": "fact"},
         {"input": "hours are not promised", "has": "unknown"}),
        _sheet("nearest_pharmacy", {}, "pharmacies",
               {"type": "fact", "label": "{{item.name}}", "value": "{{item.address}}"}), "IT",
    ),
    Starter(
        "partenza aeroporto", "airport_departure",
        "vorrei che tu potessi dirmi quando partire per l'aeroporto",
        ({"input": "when to leave for the next flight", "shows": "text"},
         {"input": "by public transport", "has": "public transport"}),
        {"reads": [{"tool": "airport_departure", "args": {}}],
         "blocks": [{"type": "text", "text": "{{0.summary}}"},
                    {"type": "fact", "label": "Esci alle", "value": "{{0.leave_at}}"}]},
    ),
)  # fmt: skip


def offered(country: str | None) -> tuple[Starter, ...]:
    """The starters this install offers: one that reads a single country's feed only for a user in that
    country (PISA_COUNTRY)."""
    return tuple(s for s in STARTERS if s.country is None or s.country.upper() == (country or "").upper())


def for_tool(tool: str, country: str | None) -> Starter | None:
    return next((s for s in offered(country) if s.tool == tool), None)


def named(name: str, country: str | None) -> Starter | None:
    key = " ".join(name.casefold().split())
    return next((s for s in offered(country) if s.name == key), None)
