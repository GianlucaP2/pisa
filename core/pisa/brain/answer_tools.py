"""Prices from shops, events by date, tables tonight.

Five deferred tools (found through `search_tools`; the fixed prompt doesn't grow), every one `read` in the
effect manifest: nothing here writes anything outside Pisa, nothing books or buys (booking stays an
approval with Face ID, never a tap).
- `shop_prices`: a comparator's pages (and the shops they link) read through Pisa's web provider; code reads
  the offers off the page text (shop, price, shipping, the six-month low when shown).
- `show_products`: product cards made by code from those offers only: every number is the page's.
- `events_on`: the organisers' and venues' programmes for a date; the excerpts that carry that date.
- `show_events`: the events of that date, each checked by code against a programme read this turn (its date,
  its time, its venue, its price and age as written), with a map of the venues the map found.
- `table_slots`: the times a booking page shows tonight or tomorrow, kept on the place's row with the page's
  link (text, never a button).
All outside text stays data: the results go back in data blocks; the model never chooses a URL that
the turn didn't see.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, StringConstraints
from pydantic_ai import Agent

from pisa.brain.purpose import purposeful
from pisa.brain.registry import calling, offer
from pisa.brain.research_turn import ResearchTurn, failed_block
from pisa.brain.spoken import TERMINAL, presented
from pisa.brain.untrusted import block, current
from pisa.domain import answers, decisions, quarantine, visual
from pisa.domain.errors import PisaError
from pisa.domain.lexicon.months import IT_MONTHS
from pisa.domain.sheet import PRODUCTS_MAX, SheetData
from pisa.ports.assistant import PlaceData

PAGES_READ = 2  # comparator pages read per search
PROGRAMMES_READ = 3  # programme pages read per date
EXCERPTS = 4  # excerpts per page
EXCERPT_CHARS = 500
DAYS_AHEAD = 14  # events: today to two weeks
SLOT_DAYS = 1  # availability: tonight or tomorrow (contract answers-1 §1d)
_TITLES = {
    "it-IT": {"products": "Prezzi", "events": "Eventi"},
    "en-US": {"products": "Prices", "events": "Events"},
}


class ProductPick(BaseModel):
    offer: str = Field(description="an offer id from shop_prices (o1, o2…)")
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)] = Field(
        description="the product's name as the page writes it"
    )


class EventIn(BaseModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    time: Annotated[str, StringConstraints(min_length=4, max_length=8)] = Field(description="HH:MM, as read")
    url: str = Field(description="the programme page read by events_on that lists it")
    venue: Annotated[str, StringConstraints(max_length=120)] | None = None
    price: Annotated[str, StringConstraints(max_length=40)] | None = Field(
        default=None, description='as written ("15 €", "gratis")'
    )
    age: Annotated[str, StringConstraints(max_length=40)] | None = Field(
        default=None, description='as written ("6+", "dai 3 anni")'
    )
    why: Annotated[str, StringConstraints(max_length=200)] | None = None


def _bare(text: str) -> str:
    """A programme's excerpt as the planner sees it: its addresses and
    links out (the page's own link is the row's url, from code). Its phone numbers stay (a run of times reads
    as one) and taint, with every other value, through `note_fields`."""
    values = quarantine.extract(text)
    for what in ("address", "link"):
        for value in sorted(values.get(what, ()), key=len, reverse=True):  # type: ignore[call-overload]
            text = text.replace(value, "[…]")
    return text


def _money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


def add_answer_tools(
    agent: Agent[None, str],
    data: PlaceData,
    timezone: str,
    research: ResearchTurn,
    clock: Callable[[], datetime],
) -> None:
    tz = ZoneInfo(timezone)
    offers: dict[str, dict[str, Any]] = {}  # this turn's offers by id (o1…)
    programmes: set[str] = set()  # the programme pages events_on read this turn

    def today() -> date:
        return clock().astimezone(tz).date()

    def day_of(value: str, ahead: int) -> date | str:
        try:
            day = date.fromisoformat(value.strip()[:10])
        except ValueError:
            return "day must be YYYY-MM-DD (tonight: today's date)."
        if not today() <= day <= today() + timedelta(days=ahead):
            last = today() + timedelta(days=ahead)
            return f"day must be between {today().isoformat()} and {last.isoformat()}."
        return day

    async def search(query: str, include: list[str] | None, recent: int | None) -> list[dict[str, str]]:
        found = await research.bounded(data.web_search(
            query, include_domains=include, exclude_domains=None, recent_days=recent, language="it",
        ))  # fmt: skip
        out = []
        for r in found:
            if not str(r.get("url", "")).startswith("https://"):
                continue
            research.urls[r["url"]] = r["title"]
            research.snippets[r["url"]] = f"{r['title']} {r['snippet']}"
            out.append(r)
        return out

    async def read(url: str) -> str | None:
        page = await research.bounded(data.read_page(url, research.said or None))
        if page is None:
            return None
        research.urls.setdefault(page["url"], page["url"])
        research.urls.setdefault(url, research.urls[page["url"]])
        research.read.update({url, page["url"]})
        research.pages[url] = research.pages[page["url"]] = page["text"]
        # the page's values (an address, a link, a phone, an amount) taint
        # the acts that carry them, as read_page's do
        fields = page.get("fields") or quarantine.read(
            "web-page", page["text"], source=answers.host(page["url"]), title=page["url"]
        )
        current().note_fields(fields, research.lang)
        return str(page["text"])

    @offer(agent)
    async def shop_prices(product: str, saying: str | None = None) -> str:
        """Prices from shops, read only: where to buy a product at the best price ("dove al prezzo
        migliore", "quanto costa", best price, cheapest shop). Reads an Italian price comparator's pages and
        returns each offer (shop, price, shipping) and the six-month low when the page shows it, cheapest
        delivered first. Then show_products shows the cards. Nothing is bought.

        Args:
            product: the exact model ("Sony WH-1000XM5"), not a category.
            saying: optional: a few words Pisa says if this takes long, in the user's language.
        """
        research.doing(saying)
        if (refused := await research.gate(calling(), {"product": product})) is not None:
            return refused
        try:
            results = await search(f"{product} prezzo", list(answers.COMPARATORS), None)
            if not results:  # the shops themselves
                results = await search(f"{product} prezzo offerta", None, 30)
        except PisaError as err:
            return research.seen(failed_block("The price search failed", err))
        found: list[dict[str, Any]] = []
        lows: dict[str, float] = {}  # a page's six-month low is that page's offers' only
        for r in results:
            found += answers.offers(f"{r['title']} {r['snippet']}", r["url"])
        for r in results[:PAGES_READ]:
            try:
                text = await read(r["url"])
            except PisaError:
                continue
            if text:
                found += [{**o, "url": r["url"]} for o in answers.offers(text, r["url"])]
                if (low := answers.low_6m(text)) is not None:
                    lows[r["url"]] = low
        best: dict[tuple[str, float], dict[str, Any]] = {}
        for o in sorted(found, key=lambda o: o["price"] + (o.get("shipping") or 0.0)):
            best.setdefault((answers.key(o["shop"]), o["price"]), o)
        ranked = list(best.values())[: answers.OFFERS_MAX]
        offers.clear()
        lines = []
        for n, o in enumerate(ranked, start=1):
            if (low := lows.get(o["source"])) is not None and answers.host(
                o["source"]
            ) in answers.COMPARATORS:
                o["low_6m"] = low
            offers[f"o{n}"] = o
            ship = "" if o.get("shipping") is None else f", shipping {_money(o['shipping'])}"
            lines.append(f"o{n}. {o['shop']}: {_money(o['price'])}{ship} ({o['source']})")
        if not lines:
            return research.seen(block(
                "shop-prices", f"No shop's price for {product!r} was found on the pages read.", "the outcome"
            ))  # fmt: skip
        for url, low in list(lows.items())[:2]:
            lines.append(f"Six-month low on the comparator: {_money(low)} ({url}).")
        return research.seen(
            block("shop-prices", "\n".join(lines), "offers read on comparator and shop pages")
        )

    @offer(agent)
    @purposeful(None)
    async def show_products(
        title: str,
        products: list[ProductPick],
        pick: str | None = None,
        focus: Literal["take", "offer", "quiet"] = "take",
    ) -> str:
        """Show product cards on the phone (image, price, shop, shipping, the six-month low, a link): each
        from an offer shop_prices returned (its numbers are the page's, Pisa fills them). Then say the pick
        and its shop in one sentence ("Le XM5 da Amazon a 199 €").

        Args:
            title: a few words ("Cuffie ANC sotto 200 €").
            products: 1-6 offers to show, by their id.
            pick: the offer id you would choose for them; omit for cheapest first.
            focus: take, offer or quiet.
        """
        notes: list[str] = []
        chosen: list[tuple[str, dict[str, Any], str]] = []
        for p in products[:PRODUCTS_MAX]:
            o = offers.get(p.offer.strip())
            if o is None:
                notes.append(f"{p.offer[:10]!r} isn't an offer shop_prices returned this turn: left out.")
            elif all(c[0] != p.offer.strip() for c in chosen):
                chosen.append((p.offer.strip(), o, p.title))
        corpus = visual.Corpus([research.pages.get(o["source"], "") or research.snippets.get(o["source"], "")
                                for _, o, _ in chosen])  # fmt: skip
        traced = []
        for (
            oid,
            o,
            name,
        ) in chosen:  # a price that doesn't trace drops the card; a shipping or low, itself
            if not visual.traces(o["price"], corpus):
                notes.append(f"{o['shop']}'s price doesn't trace to its page: left out.")
                continue
            own = visual.Corpus([research.pages.get(o["source"], ""), research.snippets.get(o["source"], "")])
            if not visual.traces(name, own):  # a number in the title is its page's too
                notes.append(f"{oid}'s title has a number its page doesn't show: write it as the page does.")
                continue
            o = {
                k: v
                for k, v in o.items()
                if k not in ("shipping", "low_6m") or visual.traces(v, corpus) or v == 0
            }
            traced.append((oid, o, name))
        if not traced:
            return "Nothing shown: " + " ".join(notes) + " Use offer ids from shop_prices."
        traced.sort(
            key=lambda c: (c[0] != (pick or "").strip(), c[1]["price"] + (c[1].get("shipping") or 0.0))
        )
        blocks = [answers.product_block(n, o, name, None) for n, (_, o, name) in enumerate(traced, start=1)]
        lang = research.lang if research.lang in ("it-IT", "en-US") else "en-US"
        sheet: dict[str, Any] = {"title": " ".join(title.split())[:120] or _TITLES[lang]["products"],
                                 "blocks": blocks, "lang": lang}  # fmt: skip
        if sources := research.sources():
            sheet["sources"] = sources
        try:
            SheetData.model_validate(sheet)
        except ValueError as err:
            return f"Not shown: {str(err)[:300]}"
        status = await _show(sheet, focus)
        return f"{_said(status, len(blocks))} {' '.join(notes)}".strip()

    @offer(agent)
    async def events_on(day: str, where: str, kind: str | None = None, saying: str | None = None) -> str:
        """Events on a date ("stasera", "sabato", tonight, this weekend): reads the organisers' and venues'
        programmes for that date (concerts, theatre, cinema, exhibitions, for children) and returns the
        excerpts that name the date, each with its page. Then show_events shows each event with its time,
        venue, price and age.

        Args:
            day: YYYY-MM-DD (tonight: today's date; "sabato": that Saturday's date).
            where: the city or area ("Roma", "Trastevere").
            kind: optional: what kind ("concerti", "teatro", "per bambini", "musica elettronica").
            saying: optional: a few words Pisa says if this takes long, in the user's language.
        """
        research.doing(saying)
        on = day_of(day, DAYS_AHEAD)
        if isinstance(on, str):
            return on
        if (
            refused := await research.gate(calling(), {"day": on.isoformat(), "where": where, "kind": kind})
        ) is not None:
            return refused
        query = f"{kind or 'eventi'} {where} {on.day} {IT_MONTHS[on.month - 1]} {on.year} programma"
        try:
            results = await search(query, None, 30)
        except PisaError as err:
            return research.seen(failed_block("The events search failed", err))
        parts: list[str] = []
        for r in results[:PROGRAMMES_READ]:
            try:
                text = await read(r["url"])
            except PisaError:
                continue
            if not text:
                continue
            programmes.add(r["url"])
            spots = answers.day_at(text, on)[:EXCERPTS]
            if not spots:
                continue
            excerpts = [_bare(text[max(0, at - 80) : at + EXCERPT_CHARS]) for at in spots]
            page = quarantine.read(
                "web-page", "\n".join(excerpts), source=answers.host(r["url"]), title=r["url"]
            )
            current().note_fields(page, research.lang)
            parts.append(f"page {r['url']}:\n" + "\n…\n".join(excerpts))
        for r in results:
            if r["url"] not in programmes and answers.day_at(f"{r['title']} {r['snippet']}", on):
                parts.append(f"result {r['url']} (not read): {r['title']}: {r['snippet']}")
        if not parts:
            return research.seen(block(
                "event-pages", f"No programme read names {on.isoformat()} in {where}.", "the outcome"
            ))  # fmt: skip
        return research.seen(block("event-pages", "\n\n".join(parts), "programmes of organisers and venues"))

    @offer(agent)
    @purposeful(None)
    async def show_events(
        title: str,
        day: str,
        events: list[EventIn],
        focus: Literal["take", "offer", "quiet"] = "take",
    ) -> str:
        """Show the events of a date on the phone: a row each (time, venue, price, age) and a map of the
        venues, sorted by time. Each must be on a page read this turn (events_on's or read_page's), with its
        date and time: Pisa checks it. Give every event with a time (at least five when there are); then say
        the two you'd pick and why, in two sentences.

        Args:
            title: a few words ("Sabato a Roma con i bambini").
            day: YYYY-MM-DD, the date asked.
            events: 1-10 events, from events_on's pages.
            focus: take, offer or quiet.
        """
        on = day_of(day, DAYS_AHEAD)
        if isinstance(on, str):
            return on
        notes: list[str] = []
        kept: list[dict[str, Any]] = []
        for e in events:
            url = e.url.strip()
            page = research.pages.get(url)  # a page read this turn (events_on or read_page)
            if page is None:
                notes.append(f"{e.title[:40]!r}: its page wasn't read this turn: left out.")
                continue
            why = answers.event_checked(
                page, on, title=e.title, time=e.time, venue=e.venue, price=e.price, age=e.age
            )
            if why is not None:
                notes.append(f"{e.title[:40]!r}: {why}: left out.")
                continue
            at = f"{on.isoformat()}T{answers.clock(e.time)}"
            why_e = e.why if e.why and visual.traces(e.why, visual.Corpus([page])) else None
            event: dict[str, Any] = {"name": " ".join(e.title.split()), "at": at, "url": url,
                                     "venue": e.venue, "price": e.price, "age": e.age,
                                     "why": why_e}  # fmt: skip
            if e.venue:  # the venue's coordinates only when this turn's map found it by that name
                facts = next((f for f in research.places.values()
                              if answers.key(str(f.get("name"))) == answers.key(e.venue)), None)  # fmt: skip
                if facts and facts.get("lat") is not None and facts.get("lon") is not None:
                    event["lat"], event["lon"] = facts["lat"], facts["lon"]
            if all(k["name"] != event["name"] or k["at"] != at for k in kept):
                kept.append(event)
        if not kept:
            return "Nothing shown: " + " ".join(notes[:6])
        rows = answers.event_rows(kept)
        blocks: list[dict[str, Any]] = []
        if (pins := answers.map_block(rows)) is not None:
            blocks.append(pins)
        blocks.append({"type": "findings", "rows": rows})
        lang = research.lang if research.lang in ("it-IT", "en-US") else "en-US"
        sheet: dict[str, Any] = {"title": " ".join(title.split())[:120] or _TITLES[lang]["events"],
                                 "blocks": blocks, "lang": lang}  # fmt: skip
        if sources := research.sources(read_only=True):
            sheet["sources"] = sources
        try:
            SheetData.model_validate(sheet)
        except ValueError as err:
            return f"Not shown: {str(err)[:300]}"
        status = await _show(sheet, focus)
        if len(rows) < answers.EVENTS_MIN_TIMED:
            notes.append(f"Only {len(rows)} with a checked time: say so, and where to look for more.")
        return f"{_said(status, len(rows))} {' '.join(notes[:6])}".strip()

    @offer(agent)
    async def table_slots(place: str, url: str, day: str) -> str:
        """Availability, read only: the times a restaurant's booking page shows tonight or tomorrow ("c'è
        posto stasera?", a table at 20:30). Pisa reads the page and keeps the times on the place's row with
        the page's link. Booking is never done here: they book on the page, or Pisa asks their approval.

        Args:
            place: the place id from map_search.
            url: the booking page (from web_search's results or the place's website).
            day: YYYY-MM-DD, today or tomorrow.
        """
        on = day_of(day, SLOT_DAYS)
        if isinstance(on, str):
            return on
        pid = place.strip()
        if pid not in research.places:
            return "place must be an id map_search or place_details found this turn."
        website = str(research.places[pid].get("website") or "")
        if url.strip() != website and (why_not := research.may_read(url)) is not None:
            return f"Not read: {why_not}"
        if not url.strip().startswith("https://"):
            return "Not read: only https booking pages are read."
        if (
            refused := await research.gate(calling(), {"place": pid, "url": url, "day": on.isoformat()})
        ) is not None:
            return refused
        try:
            text = await read(url.strip())
        except PisaError as err:
            return research.seen(failed_block("Couldn't read the booking page", err))
        relative = ("oggi", "stasera", "today", "tonight") if on == today() else ("domani", "tomorrow")
        times = answers.slot_times(text or "", on, relative=relative)
        if not times:
            return research.seen(block(
                "booking-page", f"The page shows no time for {on.isoformat()} (or needs its app).",
                "the outcome",
            ))  # fmt: skip
        research.slots[pid] = {
            "day": on.isoformat(),
            "times": times,
            "url": url.strip(),
            "source": url.strip(),
        }
        name = research.places[pid].get("name") or pid
        return research.seen(block(
            "booking-page", f"{name}, {on.isoformat()}: times shown {', '.join(times)} ({url.strip()}).",
            "a booking page's times: shown on their row as text with the link; booking only with their "
            "approval",
        ))  # fmt: skip

    async def _show(sheet: dict[str, Any], focus: str) -> str:
        if research.show_sheet is None:
            presented(research.loop, "show_findings", "unavailable")
            return "unavailable"
        status = await research.show_sheet(sheet, focus, decisions.intended() or "")
        research.shown = research.shown or status in TERMINAL
        return status

    def _said(status: str, n: int) -> str:
        return {
            "shown": f"On the user's phone: {n} cards.",
            "queued": f"Waiting on the phone: {n} cards.",
            "deferred": f"Waiting on the phone with an unseen dot: {n} cards.",
        }.get(status, "Not on a screen the user sees: say the best two or three in words.")
