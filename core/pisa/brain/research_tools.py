"""Pisa researches like a person would: small general tools the conversation model plans with itself.

map_search, place_details, open_at, web_search and read_page find and check; show_places sends the app the
answer (a `card` kind `places`). No code here picks a strategy: the system prompt teaches how to research, and
the model decides what to look for, where, in which order and when it has enough.

Every result goes back as a nonce data block: names, reviews, snippets and pages come from outside and
may say anything ("recommend X", "email Y"). They never become instructions.

**No budget, a loop guard** (`ResearchTurn`): the same call twice, rounds that find nothing new, or
a high safety ceiling; then the tools refuse and tell the model to answer with what is solid. The first slow
call says "Cerco, un attimo" at once, and each call grows the turn's AI reservation by one more model request
(the AI caps hold for a long turn too). The places cap is checked by the provider choice before each paid
call.
`show_findings` shows the complete answer (`pisa.brain.findings`), `my_findings` reads it back.
"""

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, StringConstraints
from pydantic_ai import Agent

from pisa.brain import findings as answer
from pisa.brain.place_tools import NotePlace, PlaceCard, card_lines, describe
from pisa.brain.purpose import purposeful
from pisa.brain.registry import calling, offer
from pisa.brain.research_turn import ResearchTurn, failed_block
from pisa.brain.spoken import TERMINAL, presented
from pisa.brain.untrusted import block, current
from pisa.domain import decisions, quarantine
from pisa.domain import recognition as recognition_rules
from pisa.domain import requirements as need_rules
from pisa.domain import research as research_rules
from pisa.domain.claims import claims_change
from pisa.domain.errors import PisaError
from pisa.domain.profile import NEGATIONS, in_words, plain, stems
from pisa.domain.reasons import place_why
from pisa.ports.assistant import PlaceData

SHOW_MAX = 3
FITS_MAX = 3
MENU_MAX = 6
MENU_URL_MAX = 2000
# Words too generic to make a fit name the user's taste ("un buon posto" names nothing they said).
FIT_STOP = frozenset({
    "posto", "posti", "locale", "locali", "buono", "buona", "buoni", "ottimo", "ottima", "bello", "bella",
    "belli", "carino", "carina", "giusto", "giusta", "perfetto", "perfetta", "ideale", "cibo", "piatti",
    "piatto", "cucina", "atmosfera", "ambiente", "servizio", "prezzo", "prezzi", "zona", "place", "places",
    "good", "great", "nice", "lovely", "perfect", "ideal", "food", "dishes", "cuisine", "atmosphere",
    "service", "price", "prices", "area", "spot", "venue",
})  # fmt: skip
CHECKS_MAX = 6
SOURCES_MAX = 4
REVIEWS_MAX = 5


ATTRIBUTE_WORDS = {
    "outdoorSeating": "outdoor seating", "servesCoffee": "coffee", "servesBreakfast": "breakfast",
    "servesBrunch": "brunch", "servesLunch": "lunch", "servesDinner": "dinner",
    "servesVegetarianFood": "vegetarian food", "servesDessert": "desserts", "servesBeer": "beer",
    "servesWine": "wine", "servesCocktails": "cocktails", "liveMusic": "live music",
    "goodForGroups": "good for groups", "goodForChildren": "good for children", "allowsDogs": "dogs allowed",
    "reservable": "takes reservations", "takeout": "takeaway", "dineIn": "dine-in", "delivery": "delivery",
}  # fmt: skip


def attributes_text(attributes: dict[str, bool]) -> str:
    yes = [ATTRIBUTE_WORDS.get(k, k) for k, v in attributes.items() if v]
    no = [ATTRIBUTE_WORDS.get(k, k) for k, v in attributes.items() if not v]
    parts = []
    if yes:
        parts.append("has " + ", ".join(yes))
    if no:
        parts.append("no " + ", ".join(no))
    return "; ".join(parts)


class Check(BaseModel):
    requirement: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    status: Literal["verified", "likely", "unknown", "no"]
    source: Annotated[str, StringConstraints(max_length=300)] = Field(
        default="",
        description="where it was checked: a url from web_search / read_page, or the map / reviews",
    )


class ShownPlace(BaseModel):
    place_id: str = Field(description="the id from map_search")
    why: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    checks: list[Check] = Field(default_factory=list, max_length=CHECKS_MAX)
    sources: list[str] = Field(default_factory=list, description="urls read for this place", max_length=8)
    fits: list[Annotated[str, StringConstraints(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=FITS_MAX,
        description='what in the user\'s profile it matches, in a few words ("quiet", "not a chain")',
    )  # fmt: skip
    menu_url: str | None = Field(
        default=None, max_length=MENU_URL_MAX, description="the menu page, only one you read with read_page"
    )
    menu_highlights: list[Annotated[str, StringConstraints(min_length=1, max_length=60)]] = Field(
        default_factory=list, max_length=MENU_MAX, description="dishes named on that menu page"
    )
    different: bool = Field(
        default=False,
        description="something different: unlike the user's usual, only when the instructions say this "
        "card may have one; `why` says how it differs",
    )


class Point(BaseModel):
    text: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    source: Annotated[str, StringConstraints(min_length=1, max_length=2000)] = Field(
        description="where it comes from: a url from this turn's web_search / read_page, or the place's id "
        "for its reviews and attributes (place_details)"
    )


class Finding(BaseModel):
    place_id: str = Field(description="the id from map_search or place_details")
    why: Annotated[str, StringConstraints(min_length=1, max_length=200)] = Field(
        description="why Pisa picks it for them now: where they are, the hour, their taste, their day"
    )
    pros: list[Point] = Field(default_factory=list, max_length=answer.POINTS_MAX)
    cons: list[Point] = Field(default_factory=list, max_length=answer.POINTS_MAX)


def add_research_tools(
    agent: Agent[None, str],
    data: PlaceData,
    timezone: str,
    research: ResearchTurn,
    note: NotePlace,
    clock: Callable[[], datetime],
) -> None:
    tz = ZoneInfo(timezone)

    @offer(agent)
    async def map_search(
        query: str,
        near: str | None = None,
        radius_m: int | None = None,
        open_now: bool = False,
        provider: Literal["apple", "google"] | None = None,
        saying: str | None = None,
    ) -> str:
        """Places on the map for a query, at most 6, with what the map knows: distance, open now, rating and
        count, price level and Google's attributes when present (outdoor seating, coffee, breakfast,
        vegetarian food…). One query is one kind of place: search again with other words, the local
        language or another provider when it finds nothing fitting.

        Args:
            query: what to look for, in plain words ("terrazza ristorante", "specialty coffee", "bakkerij").
            near: "here" for around the user now, or a place, address, city or today's event; omit for
                anywhere (biased to where the user was last).
            radius_m: only places within this distance of `near`.
            open_now: only places open now (some providers can't filter: then their hours decide).
            provider: "apple" or "google" for that map only; omit for the user's order. Google has the
                ratings and attributes.
            saying: optional: a few words Pisa says if this takes long, in the user's language ("Leggo
                le recensioni del secondo"); what you are doing, never a result.
        """
        research.doing(saying)
        if (
            refused := await research.gate(
                calling(),
                {
                    "query": query,
                    "near": near,
                    "radius_m": radius_m,
                    "open_now": open_now,
                    "provider": provider,
                },
            )
        ) is not None:
            return refused
        try:
            found = await research.bounded(
                data.map_search(query, near=near, radius_m=radius_m, open_now=open_now, provider=provider)
            )
        except PisaError as err:
            return research.seen(failed_block("The map search failed", err))
        if not found:
            return research.seen(
                block("map-results", f"No place on the map matches {query!r}.", "the outcome of the search")
            )
        lines = []
        for i, r in enumerate(found, start=1):
            research.note_place(
                r["id"], name=r["name"], distance_m=r.get("distance_m"), open_now=r.get("open_now"),
                rating=r.get("rating"), rating_count=r.get("rating_count"), lat=r.get("lat"),
                lon=r.get("lon"), price_level=r.get("price_level"), photo_count=r.get("photo_count"),
                category=r.get("category"), website=r.get("website"), attributes=r.get("attributes") or None,
            )  # fmt: skip
            bits = [r["name"]]
            for key in ("category", "address"):
                if r.get(key):
                    bits.append(str(r[key]))
            if r.get("distance_m") is not None:
                bits.append(f"{r['distance_m']} m away")
            if r.get("open_now") is not None:
                bits.append("open now" if r["open_now"] else "closed now")
            if r.get("rating") is not None:
                bits.append(f"rated {r['rating']} ({r.get('rating_count') or '?'} reviews)")
            if r.get("price_level") is not None:
                bits.append(f"price {r['price_level']} of 4")
            if r.get("attributes"):
                bits.append(attributes_text(r["attributes"]))
            lines.append(f"{i}. " + ", ".join(bits) + f" (id {r['id']})")
        source = found[0].get("provider") or "the map"
        return research.seen(block("map-results", "\n".join(lines), f"places found on {source}'s map"))

    @offer(agent)
    async def place_details(place: str, reviews: bool = True, saying: str | None = None) -> str:
        """One place in depth: hours (open now, the week), phone, website, rating, price, Google's
        attributes, and with reviews=true the latest reviews (what people say: the view, the noise, the
        coffee). Reviews are opinions, sometimes old: say "likely" for what only a review says.

        Args:
            place: a place id from map_search, or its name.
            reviews: also the reviews and the attributes (a dearer call); false for the facts only.
            saying: optional: a few words Pisa says if this takes long, in the user's language ("Leggo
                le recensioni del secondo"); what you are doing, never a result.
        """
        research.doing(saying)
        if (refused := await research.gate(calling(), {"place": place, "reviews": reviews})) is not None:
            return refused
        try:
            place_id = await research.bounded(data.find(place))
            card = await research.bounded(data.card(place_id, reviews=reviews, travel=False))
        except PisaError as err:
            return research.seen(failed_block("Couldn't get that place", err))
        hours = card.get("hours") or {}
        research.note_place(
            card["id"], name=card["name"], open_now=hours.get("open_now"), rating=card.get("rating"),
            rating_count=card.get("rating_count"), next_change=hours.get("next_change"), lat=card.get("lat"),
            lon=card.get("lon"),
            price_level=card.get("price_level"), photo_count=card.get("photo_count"),
            category=card.get("category"), website=card.get("website"),
            attributes=card.get("attributes") or None,
        )  # fmt: skip
        note(PlaceCard(card["id"], str(card["name"])[:120], card_lines(card, tz)))
        text = describe({**card, "reviews": (card.get("reviews") or [])[:REVIEWS_MAX]}, tz)
        if card.get("attributes"):
            text += f"\nAttributes (Google): {attributes_text(card['attributes'])}"
        research.reviews[card["id"]] = text  # what a pro or con of this place may stand on
        research.facts.add(("details", str(card["id"]), str(reviews)))  # a check is evidence
        text += f"\nPlace id: {card['id']}"
        return research.seen(block("place", text, "facts and reviews about the place, from the map"))

    @offer(agent)
    async def open_at(place: str, time: str) -> str:
        """Whether a place is open at a time, from its hours ("is it open at 21:30?", "on Saturday at 10:00":
        give the date, not the day's name).

        Args:
            place: a place id from map_search, or its name.
            time: HH:MM (today) or YYYY-MM-DDTHH:MM (local time).
        """
        if (refused := await research.gate(calling(), {"place": place, "time": time})) is not None:
            return refused
        try:
            at = _at(time, clock().astimezone(tz))
        except ValueError:
            return "time must be HH:MM or YYYY-MM-DDTHH:MM."
        try:
            place_id = await data.find(place)
            state = await data.open_at(place_id, at)
        except PisaError as err:
            return research.seen(failed_block("Couldn't tell", err))
        when = f"{at:%a %d %b %H:%M}"
        research.facts.add(("open", place.strip(), at.isoformat()))
        research.opening[(str(place_id), at.isoformat())] = state  # a check made, kept
        if not state["known"]:
            text = f"{state['name']}: its hours are unknown, so Pisa can't tell whether it is open {when}."
        else:
            change = state.get("next_change")
            after = f" until {change:%a %H:%M}" if state["open"] and change else ""
            then = f"; it opens {change:%a %H:%M}" if not state["open"] and change else ""
            text = f"{state['name']} is {'open' if state['open'] else 'closed'} on {when}{after}{then}."
        return research.seen(block("hours", text, "a place's opening at a time"))

    @offer(agent)
    async def web_search(
        query: str,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        recent_days: int | None = None,
        language: str | None = None,
        saying: str | None = None,
    ) -> str:
        """Search the web: the results (title, url, snippet), at most 6, for you to read and weigh. Use it for
        named events, facts, products, and for places: the city's guides, local blogs and magazines, public
        social posts (search them, e.g. include_domains=["instagram.com"]), menus. read_page reads a result.

        Args:
            query: the search, in the words a local would use (in the local language for a city abroad).
            include_domains: only these sites (e.g. ["gamberorosso.it", "timeout.com"]).
            exclude_domains: never these sites (e.g. ["tripadvisor.com"] for less touristy).
            recent_days: only pages from the last N days (events, news, openings).
            language: the language of the pages you want, as a code ("it", "nl", "en"): pages from that
                country rank higher. Write the query in that language too.
            saying: optional: a few words Pisa says if this takes long, in the user's language ("Leggo
                le recensioni del secondo"); what you are doing, never a result.
        """
        research.doing(saying)
        if (
            refused := await research.gate(
                calling(),
                {
                    "query": query,
                    "include_domains": include_domains,
                    "exclude_domains": exclude_domains,
                    "recent_days": recent_days,
                    "language": language,
                },
            )
        ) is not None:
            return refused
        try:
            results = await research.bounded(data.web_search(
                query, include_domains=include_domains, exclude_domains=exclude_domains,
                recent_days=recent_days, language=language,
            ))  # fmt: skip
        except PisaError as err:
            return research.seen(failed_block("The web search failed", err))
        if not results:
            return research.seen(
                block("web-results", f"The web search found nothing for {query!r}.", "the outcome")
            )
        lines = []
        results = [r for r in results if _is_web(r["url"])]  # a result's address is the page's own word
        for i, r in enumerate(results, start=1):
            research.urls[r["url"]] = r["title"]
            research.snippets[r["url"]] = f"{r['title']} {r['snippet']}"
            lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
        return research.seen(block("web-results", "\n".join(lines), "web search results"))

    @offer(agent)
    async def read_page(url: str, saying: str | None = None) -> str:
        """Read one web page (its text, trimmed): a guide, an article, a menu, an event page. Only urls from
        web_search or ones the user gave.

        Args:
            url: the page's full address.
            saying: optional: a few words Pisa says if this takes long, in the user's language ("Leggo
                le recensioni del secondo"); what you are doing, never a result.
        """
        research.doing(saying)
        if (why_not := research.may_read(url)) is not None:
            return f"Not read: {why_not} Use an address from web_search's results."
        if (refused := await research.gate(calling(), {"url": url})) is not None:
            return refused
        try:
            # their words go to the reader as their own block: it returns what answers them
            page = await research.bounded(data.read_page(url, research.said or None))
        except PisaError as err:
            return research.seen(failed_block("Couldn't read the page", err))
        if page is None:
            return research.seen(block("web-page", f"The page {url} couldn't be read.", "the outcome"))
        research.urls.setdefault(page["url"], page["url"])
        research.urls.setdefault(url, research.urls[page["url"]])
        research.read.update({url, page["url"]})
        research.pages[url] = research.pages[page["url"]] = page["text"]  # a menu's dishes come only from it
        # the model reads the page's reader fields, never its text (code keeps the text
        # for its own checks: a dish, a quote, a link must be in it)
        host = urlsplit(page["url"]).hostname or page["url"]
        fields = page.get("fields") or quarantine.read(
            "web-page", page["text"], source=host, title=page["url"]
        )
        current().note_fields(fields, research.lang)  # its values taint the acts that carry them (their lang)
        if fields.candidates:
            research.candidates[url] = [(name, quote) for name, quote, _ in fields.candidates]
        return research.seen(block("web-page", quarantine.render(fields), "a web page, read into fields"))

    @offer(agent)
    @purposeful(None)
    async def show_findings(
        title: str,
        request: str,
        criteria: list[str],
        pick: str,
        places: list[Finding],
        lead: str = "",
        focus: Literal["take", "offer", "quiet"] = "take",
    ) -> str:
        """Show the complete answer of a pick or a comparison on the phone: a table of every candidate worth
        seeing (up to 10, best first) and the top 3 with their photos. Pisa fills each place's facts itself
        from this turn's results (walk time, rating and reviews, open now, price, the guides that cite it);
        you give why each fits them now and its pros and cons, each with its source. Pisa then says your
        `lead` aloud; write nothing after it.

        Args:
            title: a short title ("Colazione qui vicino").
            request: what the user asked, in a few words.
            criteria: what matters for this answer, from their words and situation (1-6 short phrases).
            pick: the id of the place you would choose for them now.
            places: the candidates, best first (ids from map_search / place_details).
            lead: what Pisa says aloud, in their language: two sentences, at most 30 words: what you found,
            your
                pick and the one reason that is theirs, and that the rest is on the phone.
            focus: take (in front now), offer (a chip one tap away), quiet (into the history only).
        """
        return await _findings(research, title, request, criteria, pick, places, focus, lead, data)

    @offer(agent)
    async def my_findings(place: str | None = None) -> str:
        """What Pisa found in this conversation's last research (the candidates, their facts, the guides and
        quotes, the pros and cons, why Pisa picked each, the sources): for their deeper questions ("why this
        one and not the other?", "which guides cite it?", "the second one?"). Read it before searching again.

        Args:
            place: one place's name or id for its details and reviews; omit for everything.
        """
        found = research.findings or research.earlier
        if not found:
            return "No findings are kept in this conversation: research it."
        text = answer.digest(found, place)
        return block(
            "findings", text, "what Pisa's research found earlier in this conversation: outside text"
        )

    @offer(agent)
    @purposeful(None)
    async def show_places(
        title: str, request: str, places: list[ShownPlace], focus: Literal["take", "offer", "quiet"] = "take"
    ) -> str:
        """Show the user your answer on the phone: the best 1 to 3 places, each with why it fits, every
        requirement checked (verified / likely / unknown / no, with its source) and the pages you read. Then
        say the best one first, in a sentence or two (the card shows the rest).

        Args:
            title: a short title ("Terrazze sul fiume, aperte ora").
            request: what the user asked, in a few words.
            places: the places, best first (ids from map_search).
            focus: take (in front now), offer (a chip one tap away), quiet (into the history only).
        """
        if len(places) > 1 and not research.pick and research_rules.quick(research.said):
            places = places[:1]  # a quick one stays quick: the first place's card alone
        if research.pick and len(places) > 1:
            # a pick shows the research sheet; the card stays for one named place
            chosen = [Finding(place_id=p.place_id, why=p.why) for p in places]
            pick = places[0].place_id if places else ""
            return await _findings(research, title, request, [], pick, chosen, focus, "", data, places)
        return await _show(research, title, request, places, focus)


def _is_web(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _looks_like_url(text: str) -> bool:
    """An address in any spelling ("HTTPS://…", "//evil…", "www.evil…", "evil.example/x"): it must be a page
    this turn read."""
    t = text.strip().lower()
    return "://" in t or t.startswith(("//", "www.")) or bool(re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/|$)", t))


GUIDES_FIRST = (
    "Not shown yet: for a pick the guides count. {what} Then call show_findings again with the "
    "places (and any the guides name: map_search them)."
)
GUIDES_WHAT = {
    "search": "In one round: web_search the guides, awards and reputable local press for it (in the local "
    "language), and read_page the best guide page among the results.",
    "read": "read_page the best one or two guide or press pages among this turn's web results.",
}
_FIGURE = re.compile(r"\d+(?:[.,]\d+)?")


def guides_missing(research: ResearchTurn) -> str | None:
    """Before a pick is shown, Pisa asks
    once for the guides when no guide page was read and one could be (no web call yet, or results not read);
    never after the loop guard stopped the research."""
    if not research.pick or research.guide_read() or research.guides_asked or research.stopped is not None:
        return None
    if not research.web_tried:
        return GUIDES_WHAT["search"]
    # a place's own menu page read is no guide; a guide among the results is asked for
    unread = [u for u in research.urls if u not in research.read and not research.own_page(u)]
    return GUIDES_WHAT["read"] if unread else None


CANDIDATES_MIN = 3  # the places seen before a pick
WIDEN_FIRST = (
    "Not shown yet: only {n} place(s) seen this turn, too few to pick from. In one round: map_search again "
    "with another query or a wider area (and the places the pages read name). Then call show_findings again."
)


def too_few(research: ResearchTurn) -> int | None:
    """Before a pick
    is shown, Pisa asks once for more candidates when this turn saw fewer than CANDIDATES_MIN places; never
    after the loop guard stopped the research. The count, or None."""
    if not research.pick or research.widen_asked or research.stopped is not None:
        return None
    return len(research.places) if len(research.places) < CANDIDATES_MIN else None


async def _walk_times(research: ResearchTurn, data: PlaceData | None, ids: list[str]) -> None:
    """The minutes on foot of the places about to be shown that this turn has none for, in ONE
    call (the why column says them: "a 5 minuti a piedi"). A refusal or a failure leaves the cells empty."""
    missing = [p for p in dict.fromkeys(ids) if p in research.places
               and research.places[p].get("travel_min") is None][:12]  # fmt: skip
    if data is None or not missing or research.stopped is not None:
        return
    if await research.gate("walk_times", {"places": sorted(missing), "mode": "walk"}) is not None:
        return
    try:
        got = await research.bounded(data.travel_many(missing, "walk"))
    except PisaError:
        return
    for t in got.get("times") or []:
        if t.get("minutes") is not None and got.get("mode", "walk") == "walk":
            research.facts.add(("travel", t["place_id"], "walk"))
            research.note_travel(t["place_id"], t["minutes"])


def _traced(said: str, research: ResearchTurn, rows: list[dict[str, Any]]) -> bool:
    """Every figure of the model's lead is one this turn's tools returned (a row's facts, a page, a result, a
    review) or their situation's: never a number nobody gave."""
    seen = " ".join([
        *(" ".join(str(v) for v in r.values() if isinstance(v, str | int | float)) for r in rows),
        *(" ".join(r.get("now") or []) for r in rows), *research.pages.values(), *research.snippets.values(),
        *research.reviews.values(), *(f"{n} {q}" for c in research.candidates.values() for n, q in c),
        *(str(v) for f in research.places.values() for v in f.values()),
    ])  # fmt: skip
    known = {x.replace(",", ".") for x in _FIGURE.findall(seen)}
    return all(x.replace(",", ".") in known for x in _FIGURE.findall(said))


def spoken_lead(research: ResearchTurn, lead: str, rows: list[dict[str, Any]]) -> str:
    """What Pisa says once the sheet is up: the model's lead when it fits (whole
    sentences, two at most, LEAD_WORDS, the pick named, their language, every figure traced), else Pisa's own
    from code's facts. The lead names the pick's recognition when it has one, never counts
    what Pisa found, never names a place a hard requirement excluded, and says the distance the card shows."""
    pick = rows[0]["name"]
    said = research_rules.lead(lead, pick, research.lang)
    awards = list(rows[0].get("recognition") or [])
    facts = research.places.get(rows[0].get("place_id", ""), {})
    if said is not None and (
        claims_change(said)
        or not _traced(said, research, rows)
        or research_rules.counts(said)
        or (awards and not any(_names_award(said, a) for a in awards))
        or any(answer.key(e["name"]) and f" {answer.key(e['name'])} " in f" {answer.key(said)} "
               for e in research.excluded)
        or not research_rules.same_distance(said, facts.get("travel_min"), facts.get("distance_m"))
    ):  # fmt: skip
        # a lead that claims a change is held by the claim rail and
        # said as "Non l'ho ancora fatto: non ho cambiato niente.", naming nothing: Pisa says its own lead
        said = None
    if said is not None:
        return research_rules.tidy(said)
    found = max(len(research.places), len(rows))
    named = next((a for a in awards if not a["award"].startswith("n. ")), awards[0] if awards else None)
    recognition = recognition_rules.said(named, research.lang) if named is not None else ""
    return research_rules.own_lead(found, pick, list(rows[0].get("now") or []), research.lang, recognition)


def _names_award(said: str, item: dict[str, Any]) -> bool:
    """The lead names the recognition: its award ("Tre Lune", "una stella") or its guide."""
    text = f" {answer.key(said)} "
    award, guide = answer.key(str(item.get("award") or "")), answer.key(str(item.get("guide") or ""))
    stars = "stell" in text or " star" in text
    return bool(
        (award and f" {award} " in text)
        or (guide and f" {guide} " in text)
        or ("★" in str(item.get("award")) and stars)
    )


FILL_CHECKED = 6  # places seen beyond the model's that are checked too (the table's filler)
ALL_FAILED = (
    "Not shown: every place fails what they asked, checked against its source ({reasons}). Search for places "
    "that meet it (open then, within the budget), then call show_findings again with them."
)


async def _opening(
    research: ResearchTurn, data: PlaceData | None, place_id: str, at: datetime, now: bool
) -> dict[str, Any] | None:
    """Open at the asked time: the map's open-now for now, else (or when the map didn't say) its hours for
    that time, once a turn per place and time (a check the model made is reused)."""
    facts = research.places.get(place_id) or {}
    if now and facts.get("open_now") is not None:
        return {"known": True, "open": facts["open_now"], "next_change": facts.get("next_change")}
    if (state := research.opening.get((place_id, at.isoformat()))) is not None:
        return state
    if (
        data is None
        or await research.gate("open_at", {"place": place_id, "time": at.isoformat()}) is not None
    ):
        return None
    try:
        state = await research.bounded(data.open_at(place_id, at))
    except PisaError:
        return None
    research.facts.add(("open", place_id, at.isoformat()))
    research.opening[(place_id, at.isoformat())] = state
    return state


async def _check_requirements(research: ResearchTurn, data: PlaceData | None, ids: list[str]) -> None:
    """Each finalist against every hard requirement their words
    carry, from this turn's evidence (the map's hours for the asked time, the pages and reviews read), by
    code."""
    ask = research.asked
    if ask is None:
        return
    lang = research.lang
    todo = [p for p in dict.fromkeys(ids) if p in research.places and p not in research.verdicts]
    states = await asyncio.gather(*(_opening(research, data, p, ask.at, not ask.named) for p in todo))
    for pid, state in zip(todo, states, strict=True):
        facts = research.places[pid]
        evidence = answer.evidence_of(research, pid)
        outcomes = [need_rules.opening(state, ask, lang)]
        if ask.budget is not None:
            outcomes.append(need_rules.budget(ask, evidence, lang))
        if (party := need_rules.party(ask, dict(facts.get("attributes") or {}), lang)) is not None:
            outcomes.append(party)
        if ask.seated:
            outcomes.append(need_rules.seated(dict(facts.get("attributes") or {}), evidence, lang))
        if ask.work:
            outcomes.append(need_rules.work(evidence, lang))
        research.verdicts[pid] = need_rules.Verdict(outcomes)


_STATUS: dict[bool | None, Literal["verified", "no", "unknown"]] = {
    True: "verified",
    False: "no",
    None: "unknown",
}


def _row_checks(row: dict[str, Any], research: ResearchTurn) -> list[Check]:
    """The card's checks for one place: its recognition (verified, the guide's page) and each
    hard requirement as code checked it."""
    out: list[Check] = []
    for item in row.get("recognition") or []:
        out.append(
            Check(requirement=recognition_rules.chip(item)[:80], status="verified", source=item["url"])
        )
    for c in row.get("checks") or []:
        source = c.get("source") or ""
        if source and source != "map" and source not in research.urls:
            source = ""
        out.append(Check(requirement=str(c["why"])[:80] or c["need"], status=_STATUS[c.get("ok")],
                         source=source))  # fmt: skip
    return out[:CHECKS_MAX]


async def _findings(
    research: ResearchTurn,
    title: str,
    request: str,
    criteria: list[str],
    pick: str,
    chosen: list[Finding],
    focus: str,
    lead: str = "",
    places: PlaceData | None = None,
    card_places: list[ShownPlace] | None = None,
) -> str:
    """The top places as the `places` card (photos credited, the Maps link: the
    phone's view), then the sheet with the whole table; every cell from this turn's results (`findings`).
    the guides asked for once on a pick, the walk times filled, and the lead Pisa says."""
    if (what := guides_missing(research)) is not None:
        research.guides_asked = True

        async def held() -> str:  # the model never showed it again: the map's answer is better than none
            return await _findings(
                research, title, request, criteria, pick, chosen, focus, lead, places, card_places
            )

        research.pending = held
        return GUIDES_FIRST.format(what=what)
    if card_places is None and (seen := too_few(research)) is not None:  # the sheet asked for (show_findings)
        research.widen_asked = True

        async def fuller() -> str:  # the model never showed it again: the sheet it asked for, as it was
            return await _findings(
                research, title, request, criteria, pick, chosen, focus, lead, places, card_places
            )

        research.pending = fuller
        return WIDEN_FIRST.format(n=seen)
    research.pending = None
    await _walk_times(research, places, [c.place_id.strip() for c in chosen] + list(research.places))
    ids = [c.place_id.strip() for c in chosen if c.place_id.strip() in research.places]
    now = research.now or (research.local_now() if research.local_now is not None else None)
    if now is not None:  # every hard requirement checked before the pick
        research.asked = need_rules.asked(research.said, now)
        others = [p for p in research.places if p not in ids][:FILL_CHECKED]
        await _check_requirements(research, places, [*ids, *others])
        failing = [p for p in ids if (v := research.verdicts.get(p)) is not None and v.failed is not None]
        if ids and len(failing) == len(ids) and not research.all_failed_said and research.stopped is None:
            # a rail's miss goes back to the model: every finalist fails what they asked; it searches
            # again
            research.all_failed_said = True

            async def again() -> str:
                return await _findings(
                    research, title, request, criteria, pick, chosen, focus, lead, places, card_places
                )

            research.pending = again
            reasons = "; ".join(
                f"{research.places[p].get('name')}: {research.verdicts[p].failed.why}"  # type: ignore[union-attr]
                for p in failing
            )
            return ALL_FAILED.format(reasons=reasons)
    data, rows, notes = answer.compose(research, title, criteria, pick, chosen, research.lang)
    if data is None:
        return "Nothing shown: " + " ".join(notes) + " Use ids from map_search."
    research.criteria = [c for c in criteria if c.strip()]
    if sources := research.sources():
        data["sources"] = sources
    top = [
        ShownPlace(
            place_id=r["place_id"],
            why=r["why"][:200] or r["name"][:200],
            sources=[c["url"] for c in r["cited_by"]][:SOURCES_MAX],
            checks=_row_checks(r, research),
        )
        for r in rows[: answer.TOP]
    ]
    if card_places:  # the model's own card (its checks, menu, "something different") stays,
        whys = {r["place_id"]: r["why"][:200] for r in rows}  # with the same why as its row
        by_id = {r["place_id"]: r for r in rows}
        top = [p.model_copy(update={
            "why": whys.get(p.place_id.strip()) or p.why,
            "checks": [*_row_checks(by_id[p.place_id.strip()], research), *p.checks][:CHECKS_MAX]
            if p.place_id.strip() in by_id else p.checks,
        }) for p in card_places if p.place_id.strip() in by_id]  # fmt: skip
        top = top or [ShownPlace(place_id=r["place_id"], why=r["why"][:200] or r["name"][:200])
                      for r in rows[: answer.TOP]]  # fmt: skip
    card = await _show(research, title, request, top, "offer" if focus == "take" else focus)
    if research.show_sheet is None:
        status = "unavailable"
        presented(research.loop, "show_findings", status)
    else:
        status = await research.show_sheet(data, focus, decisions.intended() or "")
    research.shown = research.shown or status in TERMINAL
    if status in TERMINAL and research.on_shown is not None:
        research.on_shown()  # the spoken lead counts from here
    research.findings = answer.kept(research, rows, criteria, rows[0]["place_id"], request)
    said = {
        "shown": "The table is on the user's phone",
        "queued": "The table waits on the phone",
        "deferred": "The table waits on the phone with an unseen dot",
    }.get(status, "The table isn't on a screen the user sees: say the best two or three places")
    if status in TERMINAL:  # Pisa says the lead itself, the model adds nothing
        research.lead = spoken_lead(research, lead, rows)
        then = f"Pisa says aloud: «{research.lead}» Write nothing more: the screen has the rest."
    else:
        then = "Now say the best two or three places, in a few sentences."
    return " ".join([f"{said}: {len(rows)} places, {rows[0]['name']} first.", card, *notes, then]).strip()


async def _show(
    research: ResearchTurn, title: str, request: str, chosen: list[ShownPlace], focus: str = "take"
) -> str:
    """The `places` card from what this turn found: only places it saw, only sources it read."""
    notes: list[str] = []
    shown: list[dict[str, Any]] = []
    for item in chosen:
        facts = research.places.get(item.place_id.strip())
        if facts is None:
            notes.append(
                f"{item.place_id[:40]!r} wasn't found by map_search or place_details this turn: left out."
            )
            continue
        if len(shown) == SHOW_MAX:
            notes.append(f"Only the best {SHOW_MAX} are shown: {facts['name']} left out.")
            continue
        checks = []
        for check in item.checks[:CHECKS_MAX]:
            source, status = check.source.strip(), check.status
            if _looks_like_url(source) and source not in research.urls:
                # A source this turn never read: not a source (never invent).
                notes.append(
                    f"{source[:80]} wasn't read this turn: {check.requirement!r} is shown as unknown."
                )
                source, status = "", "unknown"
            if source in research.urls and len(source) > 120:
                source = research.urls[source][:120] or source  # a long read url: its title, not a cut link
            checks.append({"requirement": check.requirement, "status": status, "source": source[:120]})
        sources = [
            {"title": research.urls[url][:120] or url[:120], "url": url}
            for url in dict.fromkeys(u.strip() for u in item.sources)
            if url in research.urls
        ][:SOURCES_MAX]
        dropped = [u for u in item.sources if u.strip() not in research.urls]
        if dropped:
            notes.append(f"{len(dropped)} source(s) for {facts['name']} weren't read this turn: left out.")
        fits = [f for f in item.fits if _fits_taste(f, research.taste, research.avoid)][:FITS_MAX]
        different = item.different and _different_allowed(
            research, facts["name"], item.why, shown, notes, str(facts.get("category") or "")
        )
        if len(fits) < len(item.fits[:FITS_MAX]):
            notes.append(
                f"`fits` name only what is in the user's profile: the others for {facts['name']} left out."
            )
        menu_url = (item.menu_url or "").strip() or None
        if menu_url is not None and menu_url not in research.read:
            notes.append(f"The menu of {facts['name']} wasn't read this turn (read_page): no menu shown.")
            menu_url = None
        dishes: list[str] = []
        if menu_url is not None:
            # Only dishes the menu page says: the model can't add one it imagined.
            page = research.pages.get(menu_url, "")
            dishes = [d.strip() for d in item.menu_highlights if in_words(d, page)]
            if len(dishes) < len(item.menu_highlights):
                notes.append(
                    f"Dishes not on the menu page of {facts['name']} were left out: name them as written."
                )
        if menu_url is not None and research.save_menu is not None:
            await research.save_menu(item.place_id.strip(), menu_url, dishes)
        change = facts.get("next_change")
        shown.append({
            "place_id": item.place_id.strip(),
            "name": str(facts.get("name", ""))[:120],
            # the model's line, in their language, after code's reasons from their situation
            # (their next event, who is with them, their tastes, the minutes on foot)
            "why": answer.for_him(research, item.place_id.strip(), facts,
                                place_why(item.why, research.lang), research.lang,
                                None if research.asked is not None and research.asked.named
                                else facts.get("open_now"))[1][:200],  # as the row
            "checks": checks,
            "distance_m": facts.get("distance_m"),
            "travel_min": facts.get("travel_min"),
            "open_now": facts.get("open_now"),
            "rating": facts.get("rating"),
            "rating_count": facts.get("rating_count"),
            "sources": sources,
            # where, photos, price, until when, the menu, what fits the user.
            "lat": facts.get("lat"),
            "lon": facts.get("lon"),
            "photo_count": facts.get("photo_count") or 0,
            "price_level": facts.get("price_level"),
            "closes_at": change if facts.get("open_now") is True else None,
            "opens_at": change if facts.get("open_now") is False else None,
            "menu_url": menu_url,
            "fits": fits,
            "different": different,
        })  # fmt: skip
    if not shown:
        return "Nothing shown: " + " ".join(notes) + " Use ids from map_search."
    card = {
        "title": " ".join(title.split())[:120] or "Places",
        "request": " ".join(request.split())[:200],
        "places": shown,
        "lines": [f"{p['name']}: {p['why']}"[:200] for p in shown],
    }
    if research.show is None:
        presented(research.loop, "show_places", "unavailable")
        status = "unavailable"
    else:
        status = await research.show(card, focus, decisions.intended() or "")
    research.shown = research.shown or status in TERMINAL
    said = {
        "shown": "The card is on the user's phone.",
        "queued": "The card waits on the phone: it is there when the user opens Pisa.",
        "deferred": "The card waits on the phone with an unseen dot.",
    }.get(status, "The card isn't on a screen the user sees: say the places.")
    return " ".join([f"Shown: {', '.join(p['name'] for p in shown)}.", said, *notes])


_FIT_STOP = {w[:5] for w in FIT_STOP}


def _different_allowed(
    research: ResearchTurn,
    name: str,
    why: str,
    shown: list[dict[str, Any]],
    notes: list[str],
    category: str = "",
) -> bool:
    """ "something different": at most one place per card, about 1 card in 5 (Pisa decides which, and
    only while the user leaves it on), never something the user avoids: by its name, its `why`, and its
    category, which carries the cuisine ("sushi_restaurant"), without a negation."""
    if not research.different_allowed:
        notes.append(f"No 'something different' on this card: {name} is shown as a usual pick.")
        return False
    if any(p.get("different") for p in shown):
        notes.append(f"Only one 'something different' per card: {name} is shown as a usual pick.")
        return False
    if any(not _fits_taste(text, [], research.avoid) and _fit_stems(text) & _avoided(research) for text in (
        name, why, category.replace("_", " ")
    )):  # fmt: skip
        notes.append(f"{name} names something the user avoids: never 'something different'.")
        return False
    return True


def _avoided(research: ResearchTurn) -> set[str]:
    return {s for claim in research.avoid for s in _fit_stems(claim)}


def _fit_stems(text: str) -> set[str]:
    return stems(text) - _FIT_STOP


def _fits_taste(fit: str, taste: list[str], avoid: list[str]) -> bool:
    """A fit names something in the user's profile ("why it fits the user", never invented): a
    content word of it, not a generic one ("posto", "good"), shares its stem with a word of a claim ("non
    una catena" ~ "Niente catene"). The subject of a dislike or a hard limit only with a negation
    ("catena famosa" never fits "Niente catene")."""
    named = _fit_stems(fit)
    if not named:
        return False
    negated = bool(NEGATIONS & set(plain(fit).split()))
    if not negated and any(named & _fit_stems(claim) for claim in avoid):
        return False
    return any(named & _fit_stems(claim) for claim in [*taste, *avoid])


def _at(value: str, now: datetime) -> datetime:
    """HH:MM today (tomorrow when it was more than an hour ago) or an ISO time, in the user's zone."""
    value = value.strip()
    tz = now.tzinfo
    if len(value) <= 5 and ":" in value:
        hours, minutes = value.split(":")
        moment = now.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)
        return moment if moment >= now - timedelta(hours=1) else moment + timedelta(days=1)
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)
