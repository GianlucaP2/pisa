"""The brain's public-data tools (read-only, no approval): the next departures,
a line's problems, the weather alerts where they are, Rome's ZTL now, the nearest pharmacy in Rome, when to
leave for the next flight by transit; and `keep_skill`, the research's first skills kept on their yes to
Pisa's offer (the path).

Every result is outside data (`public-data`): an agency's words in a nonce block, never instructions;
a write later in the turn needs the user's own words, as after any page or email. A source that fails is
said as such, never guessed."""

import uuid
from collections.abc import Callable
from typing import Any

from pydantic_ai import Agent

from pisa.brain.present_tools import Presenter
from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.registry import offer
from pisa.brain.untrusted import current, data_block
from pisa.domain.public_sources import KIND
from pisa.domain.wishes import lines_of
from pisa.ports.public_data import PublicData, StarterSkills

_WHAT = {
    "next_departures": "the city's live transit feed (predicted departures)",
    "transit_alerts": "the transit agency's service alerts",
    "weather_alerts": "the civil-protection or weather service's alerts",
    "ztl_status": "Rome's ZTL hours (a reviewed table from RSM's page)",
    "nearest_pharmacy": "the regional pharmacy registry (no hours)",
    "airport_departure": "the next flight and the transit time to its airport",
}

# Pisa's own words asking the model to offer once; the closing-offer rule hears only these
OFFER_ONCE = "If they may ask this again, offer once"


def add_public_tools(
    agent: Agent[None, str],
    public: PublicData,
    turn: ProfileTurn,
    starters: StarterSkills | None = None,
    screen: Callable[[], Presenter | None] = lambda: None,
) -> None:
    async def answer(tool: str, args: dict[str, Any]) -> str:
        found = await public.read(tool, args)
        text = str(found.get("summary") or "")
        if found.get("credit"):
            text += f"\n({found['credit']})"
        block = data_block(KIND, text, what=_WHAT[tool])
        out = block.render()
        if not found.get("ok"):
            return out + "\nSay in a few words that Pisa couldn't read it; never guess an answer."
        out += "\nAnswer in one or two short sentences: the facts, no source unless asked."
        if starters is not None and (name := await starters.offer(tool)) is not None:
            out += (
                f'\n{OFFER_ONCE}, in a line, to keep it as a skill named "{name}" '
                "(only their yes to that keeps it: keep_skill)."
            )
        return out

    @offer(agent)
    async def next_departures(to: str = "", line: str = "") -> str:
        """The next buses, trams or trains near the user, predicted by the city's live feed (Rome: RSM/ATAC;
        New York: the MTA subway): "quando passa il prossimo bus per casa?", "il prossimo 64?", "next A
        train?". Read-only.

        Args:
            to: "home" for the ones that go on toward their home; else empty.
            line: a line they named ("64", "A", "metro B"); else empty.
        """
        return await answer("next_departures", {"to": to, "line": line})

    @offer(agent)
    async def transit_alerts(line: str = "") -> str:
        """Problems on the metro, the subway or a line right now (the agency's live alerts): "la metro ha
        problemi?", "ci sono scioperi sulla A?", "is the 7 running?". Read-only.

        Args:
            line: the line they named ("A", "64", "7"); empty for the metro or subway as a whole.
        """
        return await answer("transit_alerts", {"line": line})

    @offer(agent)
    async def weather_alerts() -> str:
        """Official weather alerts where the user is (Italy: the Protezione Civile's bulletin, today and
        tomorrow; the US: the National Weather Service): "ci sono allerte meteo?", "allerta gialla?", "any
        weather warnings?". The forecast itself is get_weather. Read-only."""
        return await answer("weather_alerts", {})

    @offer(agent)
    async def ztl_status(zone: str = "") -> str:
        """Whether Rome's limited-traffic zones (ZTL: Centro Storico, Tridente, Trastevere, San Lorenzo,
        Testaccio) are active now for a car without a permit: "posso entrare nella ZTL adesso?", "la ZTL di
        Trastevere è attiva?". From RSM's published hours; never says they may enter. Read-only.

        Args:
            zone: the zone they named; empty for all.
        """
        return await answer("ztl_status", {"zone": zone})

    @offer(agent)
    async def nearest_pharmacy() -> str:
        """The nearest pharmacies in Rome (the Lazio registry: names and addresses; no hours, no on-duty rota,
        so never say one is open): "dov'è la farmacia più vicina?". Read-only."""
        return await answer("nearest_pharmacy", {})

    @offer(agent)
    async def airport_departure() -> str:
        """When to leave for the next flight by public transport from where the user is (their trips or
        calendar; the airport's buffer; the lines' problems on the way): "quando devo partire per
        l'aeroporto?", "when should I leave for JFK?". Read-only."""
        return await answer("airport_departure", {})

    if starters is None:
        return

    @offer(agent)
    async def keep_skill(name: str) -> str:
        """Keep one of Pisa's offered skills ("bus per casa", "problemi metro", "allerte meteo", "ztl adesso",
        "farmacia vicina", "partenza aeroporto"), ONLY on their yes this turn to your offer naming it ("sì,
        tienila"). Pisa tries it on their data now and shows it; then ask "Lo accendo?" (their yes and Face ID
        turn it on).

        Args:
            name: the name you offered.
        """
        said, sheet = await starters.keep(
            name, turn.words, outside=bool(current().outside_now()), turn_id=turn.turn_id or None,
            device_id=turn.device,
        )  # fmt: skip
        if sheet is None:
            return said
        # its lines as data of what it read, as wish_skill's (said when no screen shows them)
        block = data_block(KIND, lines_of(sheet) or name, what=f"what the trial of {name!r} read")
        said = f"{said}\n{block.render()}"
        p = screen()
        if p is None:
            return said + " (No screen in this turn: say its lines.)"
        object_id = f"sheet:{uuid.uuid4()}"
        if (refused := p.held("keep_skill", "sheet", object_id)) is not None:
            return f"{said} {refused}"
        ack = await p.show("keep_skill", "sheet", object_id, name, "take", data=sheet)
        return f"{said} " + (ack if isinstance(ack, str) else ack.text())
