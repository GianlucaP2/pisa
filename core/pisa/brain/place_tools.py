"""The conversation agent's place tools: where_am_i, travel_time, detour, get_weather. All read-only,
no approval. Finding and checking places and pages is research_tools'; these count against the same
turn budget.

Every result goes back to the agent as a nonce data block: place names, reviews, web summaries and
event titles come from outside. When a tool's answer is about one place, it is noted, and after the reply Pisa
sends the app a `card` kind `place` for it (unless the turn showed a `places` card).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic_ai import Agent

from pisa.brain.registry import calling, offer
from pisa.brain.research_turn import ResearchTurn, failed_block
from pisa.brain.untrusted import block
from pisa.domain.errors import PisaError
from pisa.domain.lexicon import travel
from pisa.logging import get_logger
from pisa.ports.assistant import PlaceData

MODE_WORDS = travel.MODE_WORDS["en-US"]
CARD_MODE = {"drive": "drive", "transit": "by transit", "walk": "walk", "bike": "by bike"}


_log = get_logger("pisa.place_tools")


async def resolved(places: PlaceData, place: str) -> tuple[tuple[float, float], str, str | None] | str:
    """(position, name, place id) of the place the user named, or the class of what stopped the lookup (the
    tool says "the place wasn't found (<class>)"). A refusal (PisaError) is logged by its code; anything else
    (a provider's failure, a bug) by its class: never the place, never a URL."""
    try:
        return await places.resolve(place)
    except PisaError as exc:
        _log.info("place_not_resolved", code=exc.code)
        return exc.__class__.__name__
    except Exception as exc:
        _log.warning("place_resolve_failed", cause=exc.__class__.__name__)
        return exc.__class__.__name__


@dataclass(frozen=True)
class PlaceCard:
    place_id: str
    title: str
    lines: list[str]


NotePlace = Callable[[PlaceCard], None]
# get_weather's forecast to the app (`weather_summary`), as core:2's WeatherSummary; False when not shown
ShowWeather = Callable[[dict[str, Any]], Awaitable[bool]]
WEATHER_HOURS_MAX = 24


def _local(moment: str | None, tz: ZoneInfo) -> str:
    return f"{datetime.fromisoformat(moment).astimezone(tz):%a %H:%M}" if moment else ""


def card_lines(card: dict[str, Any], tz: ZoneInfo) -> list[str]:
    """The short lines of a place card (English, like the app's screens)."""
    lines: list[str] = []
    hours = card.get("hours")
    if hours and hours.get("open_now") is not None:
        change = hours.get("next_change")
        at = f"{datetime.fromisoformat(change).astimezone(tz):%H:%M}" if change else None
        if hours["open_now"]:
            lines.append(f"Open until {at}" if at else "Open now")
        else:
            lines.append(f"Closed, opens at {at}" if at else "Closed")
    travel = card.get("travel")
    if travel:
        minutes = travel.get("traffic_minutes") or travel["minutes"]
        lines.append(f"{minutes} min {CARD_MODE.get(travel['mode'], travel['mode'])}")
    if card.get("rating") is not None:
        count = card.get("rating_count")
        lines.append(f"{card['rating']} ({count:,} reviews)" if count else f"{card['rating']}")
    if card.get("address"):
        lines.append(str(card["address"])[:200])
    return lines[:10]


def describe(card: dict[str, Any], tz: ZoneInfo) -> str:
    """The facts of a place card, as plain lines for the agent."""
    parts = [f"Name: {card['name']}"]
    for label, key in (("Category", "category"), ("Address", "address"), ("Phone", "phone"),
                       ("Website", "website"), ("Summary", "summary")):  # fmt: skip
        if card.get(key):
            parts.append(f"{label}: {card[key]}")
    hours = card.get("hours")
    if hours:
        state = {True: "open now", False: "closed now", None: "hours known"}[hours.get("open_now")]
        change = f", changes {_local(hours['next_change'], tz)}" if hours.get("next_change") else ""
        week = "; ".join(f"day {p['day']} {p['open']}-{p['close']}" for p in hours.get("weekly") or [])
        parts.append(f"Hours: {state}{change}. Week (1 = Monday): {week or 'unknown'}.")
        if hours.get("note"):
            parts.append(f"Hours note: {hours['note']}")
    if card.get("rating") is not None:
        parts.append(f"Rating: {card['rating']} from {card.get('rating_count') or '?'} reviews")
    if card.get("price_level") is not None:
        parts.append(f"Price level: {card['price_level']} of 4")
    travel = card.get("travel")
    if travel:
        traffic = f" ({travel['traffic_minutes']} with traffic)" if travel.get("traffic_minutes") else ""
        parts.append(f"From here: {travel['minutes']} min {MODE_WORDS[travel['mode']]}{traffic}")
    for review in (card.get("reviews") or [])[:3]:
        parts.append(f"Review ({review.get('rating')}): {str(review['text'])[:300]}")
    if card.get("insights"):
        parts.append(f"Pisa's web research: {card['insights']['text']}")
    for item in (card.get("related") or [])[:5]:
        parts.append(f"Related {item['kind']}: {item['title']} {_local(item.get('when'), tz)}".rstrip())
    return "\n".join(parts)


def add_place_tools(
    agent: Agent[None, str],
    places: PlaceData,
    timezone: str,
    note: NotePlace,
    research: ResearchTurn | None = None,
    *,
    weather: ShowWeather | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    tz = ZoneInfo(timezone)
    places_data = places  # walk_times' own argument is named `places`

    async def gate(args: dict[str, Any] | None = None) -> str | None:
        return await research.gate(calling(), args) if research is not None else None

    def seen(result: str) -> str:
        return research.seen(result) if research is not None else result

    def _note(card: dict[str, Any]) -> None:
        note(PlaceCard(card["id"], str(card["name"])[:120], card_lines(card, tz)))

    @offer(agent)
    async def where_am_i() -> str:
        """Where the user is now (address and how fresh the location is), and the next event today with a
        place: where they are going and why."""
        if (refused := await gate()) is not None:
            return refused
        try:
            here = await places.where()
        except PisaError as err:
            return failed_block("Couldn't tell where the user is", err)
        if here is None:
            return "No device has sent a location yet: Pisa doesn't know where the user is."
        text = (
            f"Address: {here['address'] or 'unknown'}\nCoordinates: {here['lat']:.5f}, {here['lon']:.5f}\n"
            f"Location age: {here['age_min']} min (accuracy {here['accuracy_m'] or '?'} m)"
        )
        if here.get("next"):
            nxt = here["next"]
            text += f"\nNext: {nxt['title']} at {_local(nxt['when'], tz)}, place: {nxt['place']}"
        return seen(block("location", text, "where the user is, and their next appointment"))

    @offer(agent)
    async def travel_time(
        destination: str,
        mode: Literal["drive", "transit", "walk", "bike"] | None = None,
        depart_at: str | None = None,
        arrive_by: str | None = None,
        origin: str | None = None,
    ) -> str:
        """How long it takes to get to a place, from where the user is now or from anywhere else, with
        traffic when driving.

        Args:
            destination: a place id, a place name, an address, today's event or task title, or "lat,lon".
            mode: drive, transit, walk or bike; omit for the user's default.
            depart_at: YYYY-MM-DDTHH:MM (local time) to leave later; omit for now.
            arrive_by: YYYY-MM-DDTHH:MM (local time) to arrive by (public transport).
            origin: where from: "here" (the default), a place, an address, or today's event or task title.
        """
        if (
            refused := await gate(
                {
                    "destination": destination,
                    "mode": mode,
                    "depart_at": depart_at,
                    "arrive_by": arrive_by,
                    "origin": origin,
                },
            )
        ) is not None:
            return refused
        try:
            leave = _moment(depart_at, tz)
            arrive = _moment(arrive_by, tz)
            result = await places.travel(destination, mode, leave, arrive, origin)
        except ValueError:
            return "depart_at and arrive_by must be YYYY-MM-DDTHH:MM."
        except PisaError as err:
            return failed_block("No travel time", err)
        traffic = f", {result['traffic_minutes']} min with traffic" if result.get("traffic_minutes") else ""
        distance = f", {result['distance_m'] / 1000:.1f} km" if result.get("distance_m") else ""
        start = f"From {result['from']} to" if result.get("from") else "To"
        text = (
            f"{start} {result['name']}: {result['minutes']} min {MODE_WORDS[result['mode']]}{traffic}"
            f"{distance} (from {result['provider']})."
        )
        if research is not None:  # a travel time found is a check made
            research.facts.add(("travel", str(result.get("place_id") or destination), result["mode"]))
        if result.get("place_id"):
            # The card from what is already known: no second route, no paid reviews.
            travel = {k: result.get(k) for k in ("minutes", "traffic_minutes", "mode")}
            _note({"id": result["place_id"], "name": result["name"], "travel": travel})
            if research is not None and (origin or "here").strip().lower() in ("here", "qui"):
                research.note_travel(result["place_id"], result["minutes"])
        return seen(block("travel", text, "the travel time"))

    @offer(agent)
    async def walk_times(
        places: list[str], mode: Literal["walk", "drive", "transit", "bike"] = "walk"
    ) -> str:
        """How long from where the user is now to many places, in ONE call (a table's distance column):
        give every candidate's id at once, never one call a place.

        Args:
            places: place ids from map_search or place_details (up to 12).
            mode: walk (default), drive, transit or bike.
        """
        if (refused := await gate({"places": sorted(places), "mode": mode})) is not None:
            return refused
        try:
            got = await places_data.travel_many(places, mode)
        except PisaError as err:
            return failed_block("No travel times", err)
        found = research.places if research is not None else {}
        lines = []
        for t in got["times"]:
            name = found.get(t["place_id"], {}).get("name") or t["place_id"]
            if t["minutes"] is None:
                lines.append(f"{name}: no {MODE_WORDS[got['mode']]} route found")
                continue
            meters = f", {t['distance_m'] / 1000:.1f} km" if t.get("distance_m") else ""
            lines.append(f"{name}: {t['minutes']} min {MODE_WORDS[got['mode']]}{meters} (id {t['place_id']})")
            if research is not None:  # each time found is a check made
                research.facts.add(("travel", t["place_id"], got["mode"]))
            if research is not None and got["mode"] == "walk":
                research.note_travel(t["place_id"], t["minutes"])
        if not lines:
            return seen(
                block("travel", "No travel time: none of these is a place found this turn.", "outcome")
            )
        provider = got.get("provider") or "the map"
        return seen(block("travel", "\n".join(lines), f"travel times from where the user is ({provider})"))

    @offer(agent)
    async def detour(
        origin: str,
        via: str,
        destination: str,
        mode: Literal["drive", "transit", "walk", "bike"] | None = None,
    ) -> str:
        """The extra minutes of a stop on the way ("se passo dal tabaccaio andando al museo?").

        Args:
            origin: where from: "here", a place, an address, or today's event or task title.
            via: the stop.
            destination: where to, in the same forms.
            mode: drive, transit, walk or bike; omit for the user's default.
        """
        if (
            refused := await gate({"origin": origin, "via": via, "destination": destination, "mode": mode})
        ) is not None:
            return refused
        try:
            r = await places.detour(origin, via, destination, mode)
        except PisaError as err:
            return failed_block("No detour time", err)
        how = MODE_WORDS[r["mode"]]
        text = (
            f"A stop at {r['via']} on the way from {r['from']} to {r['to']} adds {r['extra_min']} min {how}: "
            f"{r['to_stop_min']} + {r['onward_min']} min instead of {r['direct_min']} (from {r['provider']})."
        )
        return seen(block("travel", text, "the travel times of a detour"))

    @offer(agent)
    async def get_weather(where: str | None = None, when: str | None = None) -> str:
        """The weather forecast ("piove domani ad Amsterdam?", "che tempo fa al Spiraal venerdì?").

        Args:
            where: "here" (default), a place, today's event or task title, or a city.
            when: today (default), tomorrow, a weekday, or YYYY-MM-DD; up to 7 days ahead.
        """
        if (refused := await gate({"where": where, "when": when})) is not None:
            return refused
        try:
            w = await places.weather(where, when)
        except PisaError as err:
            return failed_block("No forecast", err)
        if weather is not None:  # the app draws it too; the model's text is the same either way
            try:
                await weather(weather_summary(w, clock()))
            except Exception as exc:  # never the turn's failure: the words still carry the forecast
                _log.warning("weather_card_failed", cause=exc.__class__.__name__)
        return seen(block("weather", describe_weather(w), "a weather forecast from Open-Meteo"))


def weather_summary(w: dict[str, Any], now: datetime) -> dict[str, Any]:
    """get_weather's forecast as the app's WeatherSummary reads it (`session_messages.WeatherShown`): the
    day's condition, low, high and rain chance, the hour holding `now` (none on another day), at most 24
    hours."""
    d = w["day"]
    current = next(
        (h for h in w["hours"]
         if (start := datetime.fromisoformat(h["time"])) <= now < start + timedelta(hours=1)),
        None,
    )  # fmt: skip
    return {
        "place": str(w["label"]).strip()[:120] or w["date"],
        "date": w["date"],
        "condition": d["summary"],
        "now": {"temp_c": current["temp_c"], "condition": current["summary"]} if current else None,
        "min_c": d["min_c"],
        "max_c": d["max_c"],
        "rain_chance": d["precip_prob"],
        "hourly": [
            {
                "time": h["time"],
                "temp_c": h["temp_c"],
                "rain_chance": h["precip_prob"],
                "condition": h["summary"],
            }
            for h in w["hours"][:WEATHER_HOURS_MAX]
        ],
    }


def describe_weather(w: dict[str, Any]) -> str:
    """One line for the day, then the hours (every third one, to keep it short)."""
    d = w["day"]
    lines = [
        f"{w['label']}, {w['date']}: {d['summary']}, {d['min_c']:g} to {d['max_c']:g} °C, rain chance "
        f"{d['precip_prob']}% ({d['precip_mm']:g} mm), wind up to {d['wind_kmh']:g} km/h."
    ]
    lines += [
        f"{h['time'][11:16]} {h['summary']}, {h['temp_c']:g} °C, rain {h['precip_prob']}%"
        for h in w["hours"][::3]
    ]
    return "\n".join(lines)


def _moment(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value.strip())
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)
