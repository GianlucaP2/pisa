"""The rules behind the richer answers. Pure.

- The `findings` and `map` blocks of a research answer, from the rows the research table already holds and the
  map's facts (coordinates, open now): code's values only, a value the turn didn't see left out.
- Prices from shops (a13, "dove al prezzo migliore"): the offers a comparator's or a shop's page shows (shop,
  price, shipping, the six-month low when shown), read from the page's own text; every number then traces
  to it.
- Events by date (a10, a19, "sabato", "stasera"): an event stands only on a programme page read this turn
  whose text carries its date and its time.
- Availability, read only (a03): the times a booking page shows for that day. Shown as text with the page's
  link; booking stays an approval with Face ID. Nothing here acts.
All outside text stays data: these functions read it, they never follow it.
"""

import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from pisa.domain.lexicon.months import EN_MONTHS, IT_MONTHS
from pisa.domain.lexicon.numbers import grouped_amount
from pisa.domain.sheet import (
    CITED_MAX,
    FINDING_ROWS,
    PINS_MAX,
    PRODUCTS_MAX,
    RECOGNITION_MAX,
    SLOT_TIMES,
    OpenAtAsked,
    Recognition,
    Slots,
)
from pisa.domain.text import fold_words

OFFERS_MAX = 12  # offers kept from one page
EVENTS_MAX = 10  # a findings block's rows
EVENTS_MIN_TIMED = 5  # comparison §3.8: at least five with times when they exist


def key(text: str) -> str:
    return fold_words(text or "")


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _https(url: Any) -> str | None:
    text = str(url or "").strip()
    return text if text.startswith("https://") and len(text) <= 2000 and " " not in text else None


# --- the research answer's rows and map -------------------------------------------------------------------


def badge(recognition: list[dict[str, Any]] | None) -> str | None:
    """A pin's chip: the first award ("Michelin ★", "Tre Spicchi"), within 40 characters."""
    if not recognition:
        return None
    first = recognition[0]
    text = " ".join(str(x) for x in (first.get("guide"), first.get("award")) if x)
    return text[:40] or None


def _valid(model: Any, value: Any) -> dict[str, Any] | None:
    """A value other code sets on the row (`open_at_asked`, `recognition`) only
    when it is the contract's shape; never a sheet that fails after the answer was spoken."""
    try:
        return model.model_validate(value).model_dump(mode="json")  # type: ignore[no-any-return]
    except (ValidationError, TypeError, ValueError):
        return None


def place_row(n: int, row: dict[str, Any], facts: dict[str, Any], pick: bool) -> dict[str, Any]:
    """One research row (`pisa.brain.findings._row`) as a findings row: its cells, the map's coordinates, open
    at the asked time (the row's own when the research set it, else the map's now), its guides."""
    out: dict[str, Any] = {"id": f"r{n}", "kind": "place", "name": str(row["name"])[:120] or "?"}
    if row.get("place_id"):
        out["place_id"] = str(row["place_id"])[:300]
    if pick:
        out["pick"] = True
    for k, limit in (("why", 200), ("walk", 40), ("rating", 40), ("price", 40)):
        if value := " ".join(str(row.get(k) or "").split())[:limit]:
            out[k] = value
    if row.get("open_at_asked") and (asked_row := _valid(OpenAtAsked, row["open_at_asked"])) is not None:
        out["open_at_asked"] = asked_row
    elif facts.get("open_now") is not None or row.get("open"):
        asked: dict[str, Any] = {"ok": facts.get("open_now"), "source": "map"}
        if hours := str(row.get("open") or "")[:80]:
            asked["hours"] = hours
        out["open_at_asked"] = asked
    found = [_valid(Recognition, r) for r in row.get("recognition") or [] if isinstance(r, dict)]
    if recognition := [r for r in found if r is not None][:RECOGNITION_MAX]:
        out["recognition"] = recognition
    hosts = [str(c["host"])[:255] for c in row.get("cited_by") or [] if c.get("host")][:CITED_MAX]
    if hosts:
        out["cited_by"] = hosts
    lat, lon = facts.get("lat"), facts.get("lon")
    if (
        isinstance(lat, int | float)
        and isinstance(lon, int | float)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    ):
        out["lat"], out["lon"] = float(lat), float(lon)
    for side in ("pros", "cons"):
        if points := [str(p)[:120] for p in row.get(side) or [] if str(p).strip()][:3]:
            out[side] = points
    if row.get("slots") and (slots := _valid(Slots, row["slots"])) is not None:
        out["slots"] = slots
    return out


def map_block(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Every row with coordinates pinned (≤ 12); None when none has them."""
    pins = []
    for r in rows:
        if r.get("lat") is None:
            continue
        pin: dict[str, Any] = {"id": r["id"], "name": r["name"], "lat": r["lat"], "lon": r["lon"]}
        mark = badge(r.get("recognition")) or (str(r["at"])[11:16] if r.get("at") else None)
        if mark:
            pin["badge"] = mark
        if r.get("pick"):
            pin["pick"] = True
        pins.append(pin)
    return {"type": "map", "pins": pins[:PINS_MAX]} if pins else None


def place_blocks(
    rows: list[dict[str, Any]],
    places: dict[str, dict[str, Any]],
    slots: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The research answer's map and findings blocks (pick first, as the rows are)."""
    made = []
    for n, row in enumerate(rows[:FINDING_ROWS], start=1):
        pid = str(row.get("place_id") or "")
        with_slots = {**row, "slots": (slots or {}).get(pid)} if (slots or {}).get(pid) else row
        made.append(place_row(n, with_slots, places.get(pid) or {}, pick=n == 1))
    if not made:
        return []
    found = map_block(made)
    return [*([found] if found else []), {"type": "findings", "rows": made}]


# --- prices from shops --------------------------------------------------------------------------------------

COMPARATORS = ("trovaprezzi.it", "idealo.it", "kelkoo.it", "pricerunner.com", "pagomeno.it")
_EURO = r"(?:€|eur(?:o)?\b)"
_AMOUNT = r"\d{1,3}(?:[.\u00a0 ]\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
# an amount never starts inside a word or a number ("WH-1000XM5 199,00 €" is 199, never 5199)
_PRICE = re.compile(rf"(?:{_EURO}\s?(?P<a>{_AMOUNT})|(?<![\w.,\-])(?P<b>{_AMOUNT})\s?{_EURO})", re.IGNORECASE)
_FREE = re.compile(r"spedizione\s+gratuit\w*|sped(?:\.|izione)?\s+gratis|free\s+(?:delivery|shipping)", re.I)
_SHIP = re.compile(
    rf"(?:\+\s*)?(?:sped(?:\.|izione)?|shipping|consegna|delivery)\s*:?\s*(?:\+\s*)?(?:{_EURO}\s?(?P<a>{_AMOUNT})|"
    rf"(?P<b>{_AMOUNT})\s?{_EURO})|(?:\+\s*)(?P<c>{_AMOUNT})\s?{_EURO}\s*(?:di\s+)?(?:sped\w*|spedizione|shipping)",
    re.IGNORECASE,
)
_LOW = re.compile(
    rf"(?:minimo|prezzo\s+pi[uù]\s+basso|lowest(?:\s+price)?|low)[^.€\d]{{0,40}}(?:6|sei|six)\s*(?:mesi|months?)"
    rf"[^€\d]{{0,20}}(?:{_EURO}\s?(?P<a>{_AMOUNT})|(?P<b>{_AMOUNT})\s?{_EURO})",
    re.IGNORECASE,
)
_SELLER = re.compile(  # the words before a seller in any case; the seller's name capitalised
    r"(?i:venduto\s+da|negozio|offerta\s+di|sold\s+by)\s*:?\s*(?P<s>[A-Z][\w&.'\-]*(?:\s[A-Z][\w&.'\-]*){0,3})"
)
_SHOP_HOST = re.compile(r"\b(?P<s>[a-z0-9][a-z0-9-]{1,40}\.(?:it|com|eu|de|fr|es|net|shop|store))\b", re.I)
KNOWN_SHOPS = (
    "Amazon", "eBay", "MediaWorld", "Unieuro", "Euronics", "ePRICE", "Bpm Power", "Monclick", "Yeppon",
    "Trony", "Expert", "Comet", "Juice", "Decathlon",
    "Leroy Merlin", "Feltrinelli", "IBS", "Zalando", "Esselunga", "Carrefour", "Coop", "Conad", "Back Market",
    "Fnac", "AliExpress", "Teknozone", "Esse Shop", "Onlinestore", "Thomann", "Strumentimusicali",
)  # fmt: skip
# a brand (Sony, Bose, Sennheiser) is a product's name before it is a shop: its store counts by its host or
# as a seller written ("Venduto da: Sennheiser Store"), never by the brand alone


def amount(text: str) -> float | None:
    """'1.234,56' → 1234.56, '199,00' → 199.0, '189.85' → 189.85, '1 299' → 1299."""
    value = grouped_amount(text)
    return value if value is not None and 0 <= value < 1_000_000 else None
    return value if 0 <= value < 1_000_000 else None


def _group(m: re.Match[str]) -> float | None:
    for name in ("a", "b", "c"):
        if name in m.re.groupindex and m.group(name):
            return amount(m.group(name))
    return None


def _shop_in(text: str, *, first: bool = False) -> str | None:
    """The seller named in this stretch: the last one written ("Venduto da: X"), else the last known shop
    (`first`: the first of each, for the stretch after a price)."""
    if sellers := list(_SELLER.finditer(text)):
        return " ".join(sellers[0 if first else -1].group("s").split())[:80]
    low = f" {key(text)} "
    named = [
        (low.find(f" {key(shop)} ") if first else low.rfind(f" {key(shop)} "), shop) for shop in KNOWN_SHOPS
    ]
    if first and (hits := [n for n in named if n[0] >= 0]):
        return min(hits)[1]
    if not first and (last := max(named))[0] >= 0:
        return last[1]
    if m := _SHOP_HOST.search(text):
        found = m.group("s").lower()
        return None if found.removeprefix("www.") in COMPARATORS else found[:80]
    return None


def low_6m(text: str) -> float | None:
    """The six-month low a comparator shows ("Prezzo minimo degli ultimi 6 mesi: 164,97 €")."""
    m = _LOW.search(text or "")
    return _group(m) if m else None


def offers(text: str, source: str) -> list[dict[str, Any]]:
    """The offers a comparator's or a shop's page text shows: each price with the shop named before it (up to
    the previous price) or just after it, and its shipping when written beside it. A price with no shop is
    the page's own shop's when the page is a shop's (its host), else left out. Cheapest delivered first."""
    text = " ".join((text or "").split())
    page_shop = None if host(source) in COMPARATORS else host(source) or None
    lows = {m.span() for m in _LOW.finditer(text)}
    ships = [(m.span(), _group(m)) for m in _SHIP.finditer(text)]
    free = [m.span() for m in _FREE.finditer(text)]
    out: list[dict[str, Any]] = []
    last_end = 0
    prices = list(_PRICE.finditer(text))
    for i, m in enumerate(prices):
        span = m.span()
        if any(a <= span[0] < b for a, b in lows) or any(a <= span[0] < b for (a, b), _ in ships):
            last_end = span[1]
            continue
        price = _group(m)
        if price is None or price <= 0:
            continue
        # the stretch after a price stops at the next price (never the next offer's shop)
        stop = prices[i + 1].start() if i + 1 < len(prices) else len(text)
        before, after = text[last_end : span[0]], text[span[1] : min(stop, span[1] + 80)]
        seller = _SELLER.search(before[-160:])  # a marketplace's seller, written
        if page_shop is not None:  # a shop's own page: its prices are its own
            shop = " ".join(seller.group("s").split())[:80] if seller else page_shop
        else:
            shop = _shop_in(before[-160:]) or _shop_in(after[:60], first=True)
        last_end = span[1]
        if shop is None:
            continue
        if seller is None and page_shop is None and not _shop_in(before[-160:]) and shop in after:
            last_end = span[1] + after.index(shop) + len(shop)  # that shop is this price's
        shipping = next((v for (a, _), v in ships if span[1] <= a <= span[1] + 40), None)
        if shipping is None and any(span[1] <= a <= span[1] + 40 for a, _ in free):
            shipping = 0.0
        offer = {"shop": shop, "price": price, "currency": "EUR", "source": source}
        if shipping is not None:
            offer["shipping"] = shipping
        if not any(o["shop"] == shop and o["price"] == price for o in out):
            out.append(offer)
    out.sort(key=lambda o: o["price"] + (o.get("shipping") or 0.0))
    return out[:OFFERS_MAX]


def product_block(n: int, offer: dict[str, Any], title: str, image_url: str | None) -> dict[str, Any]:
    """A product card from an offer Pisa read (its numbers are the page's)."""
    block: dict[str, Any] = {
        "type": "product", "id": f"p{n}", "title": " ".join(title.split())[:120] or offer["shop"],
        "price": offer["price"], "currency": offer.get("currency", "EUR"), "shop": str(offer["shop"])[:80],
        "url": offer.get("url") or offer["source"], "source": offer["source"],
    }  # fmt: skip
    if (image := _https(image_url)) is not None:
        block["image_url"] = image
    for k in ("shipping", "low_6m"):
        if offer.get(k) is not None:
            block[k] = offer[k]
    return block


PRODUCTS = PRODUCTS_MAX


# --- events by date -----------------------------------------------------------------------------------------

_MONTHS = {"it-IT": IT_MONTHS, "en-US": EN_MONTHS}
_TIME = re.compile(r"\b(?:ore\s*|h\s*|alle\s*|at\s*)?([01]?\d|2[0-3])[:.]([0-5]\d)\b", re.IGNORECASE)


def day_forms(day: date) -> list[str]:
    """The ways a programme writes a day: "15 novembre", "15 nov", "november 15", "15/11", "15.11", "15-11",
    "2026-11-15" (keyed: lowercase, no accents, words only)."""
    forms = [day.isoformat(), f"{day.day}/{day.month}", f"{day.day:02d}/{day.month:02d}",
             f"{day.day}.{day.month}", f"{day.day:02d}.{day.month:02d}",
             f"{day.day:02d}-{day.month:02d}"]  # fmt: skip
    for months in _MONTHS.values():
        name = months[day.month - 1]
        forms += [f"{day.day} {name}", f"{day.day} {name[:3]}", f"{name} {day.day}", f"{name[:3]} {day.day}"]
    return forms


def day_at(text: str, day: date) -> list[int]:
    """Where the text writes the day (character offsets, in the keyed text for words, raw for digits)."""
    low = text.casefold()
    keyed = f" {key(text)} "
    at: list[int] = []
    for form in day_forms(day):
        if form[0].isdigit() and not form.split()[0].isdigit():  # a numeric form: raw text, whole
            # "15/11/2026", "15.11.26" name the day too (with their year)
            dated = rf"(?<![\d/.\-]){re.escape(form)}(?:[/.\-](?:\d{{4}}|\d{{2}}))?(?![\d/.\-]?\d)"
            at += [m.start() for m in re.finditer(dated, low)]
        elif (f" {key(form)} ") in keyed:
            at += [m.start() for m in re.finditer(re.escape(form), low)]
    return sorted(set(at))


def clock(value: str) -> str | None:
    """'21:00', '21.00', 'ore 21' → 'HH:MM'; None otherwise."""
    m = re.fullmatch(r"\s*(?:ore\s*)?([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\s*", value or "", re.IGNORECASE)
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}" if m else None


_MONTH_WORDS = "|".join(sorted({m[:3] for ms in _MONTHS.values() for m in ms}))
_ANY_DAY = re.compile(
    rf"\b\d{{1,2}}\s+(?:{_MONTH_WORDS})[a-z]*\b|\b(?:{_MONTH_WORDS})[a-z]*\s+\d{{1,2}}\b|"
    r"(?<![\d/.\-])\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?(?![\d/.\-])|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
STRETCH_MAX = 1500


def _a_date(found: str) -> bool:
    """ "21.00", "20.30" or "12.50" is a time or an amount, never another day: a numeric day/month
    counts only with a day 1-31 and a month 1-12."""
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.]\d{2,4})?", found)
    return m is None or (1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12)


def stretches(text: str, day: date) -> list[str]:
    """The parts of the text about that day: from each place it names the day to the next day it names
    (another date), at most STRETCH_MAX characters. A programme's Sunday never answers for its Saturday."""
    own = {key(f) for f in day_forms(day)}
    out = []
    for at in day_at(text, day):
        end = min(len(text), at + STRETCH_MAX)
        for m in _ANY_DAY.finditer(text, at + 1, end):
            if key(m.group(0)) not in own and m.start() > at + 3 and _a_date(m.group(0)):
                end = m.start()
                break
        out.append(text[at:end])
    return out


def _figures(text: str) -> set[str]:
    """The numbers a stretch writes, its dates and clock times left out."""
    text = _ANY_DAY.sub(lambda m: " " if _a_date(m.group(0)) else m.group(0), text)
    return set(re.sub(r"\D", " ", _TIME.sub(" ", text)).split())


def event_checked(
    page: str, day: date, *, title: str, time: str, venue: str | None, price: str | None, age: str | None
) -> str | None:
    """None when the page read this turn carries the event on that day at that time (the title's words
    within 200 characters of the time, in the part of the page about that day; the venue's words, the price's
    figures and the age as written); else why not, for the model."""
    text = " ".join((page or "").split())
    keyed = f" {key(text)} "
    words = [w for w in key(title).split() if len(w) > 2] or key(title).split()
    if not words or sum(f" {w} " in keyed for w in words) < max(1, (len(words) + 1) // 2):
        return "its title isn't on that page"
    when = clock(time)
    if when is None:
        return "its time isn't a clock time (HH:MM)"
    parts = stretches(text, day)
    if not parts:
        return f"that page doesn't name {day.isoformat()}"
    hour, minute = when.split(":")
    near = re.compile(rf"(?<!\d)0?{int(hour)}[:.]{minute}(?!\d)")
    need = max(1, (len(words) + 1) // 2)
    spots = [
        part[max(0, m.start() - 200) : m.end() + 200]
        for part in parts
        for m in near.finditer(part)
        if sum(f" {w} " in f" {key(part[max(0, m.start() - 200) : m.end() + 200])} " for w in words) >= need
    ]
    if not spots:
        return f"that page doesn't give {title[:40]!r} at {when} on {day.isoformat()}"
    if venue and not all(f" {w} " in keyed for w in key(venue).split() if len(w) > 2):
        return "its venue isn't on that page"
    # the price's and the age's figures beside the event (its title and time), never anywhere on
    # the page (a "15 €" stood on the date "15 novembre")
    figures = [_figures(spot) for spot in spots]
    for part, what in ((price, "price"), (age, "age")):
        if part and not any(all(d in f for d in re.findall(r"\d+", part)) for f in figures):
            return f"its {what} isn't on that page"
    return None


# --- availability, read only --------------------------------------------------------------------------------

# "orari" (opening hours) is no availability: never a slot's word
_SLOT_WORDS = re.compile(
    r"disponibil\w*|posti|prenota\w*|available|availability|slots?|tavol\w*|book\w*",
    re.IGNORECASE,
)
_HOURS_WORDS = re.compile(r"\b(?:orari\w*|aperto|apertura|chiuso|opening\s+hours|open\s+from|hours)\b", re.I)


def slot_times(
    text: str, day: date, *, evening_from: str = "11:00", relative: tuple[str, ...] = ()
) -> list[str]:
    """The times a booking page shows for that day: clock times in the stretch after the day (or after their
    day's own word, `relative`: "stasera", "oggi", "domani"), beside a booking word, sorted, at most
    SLOT_TIMES; none when it shows none. A page that names no day gives none (its opening hours
    are no table)."""
    text = " ".join((text or "").split())
    windows = stretches(text, day)
    if not windows and relative:
        said = re.compile(r"\b(?:" + "|".join(map(re.escape, relative)) + r")\b", re.IGNORECASE)
        windows = [text[m.start() : m.start() + 600] for m in said.finditer(text)]
    found: set[str] = set()
    for window in windows[:4]:
        window = window[:600]
        if not _SLOT_WORDS.search(window) or (
            _HOURS_WORDS.search(window) and not re.search(r"disponibil|available|posti liberi", window, re.I)
        ):
            continue
        for h, mm in _TIME.findall(window):
            t = f"{int(h):02d}:{mm}"
            if t >= evening_from:
                found.add(t)
    return sorted(found)[:SLOT_TIMES]


def event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Checked events as findings rows, by time (the list of a date has no pick)."""
    ordered = sorted(events, key=lambda e: e["at"])[:EVENTS_MAX]
    rows = []
    for n, e in enumerate(ordered, start=1):
        row: dict[str, Any] = {"id": f"r{n}", "kind": "event", "name": e["name"][:120], "at": e["at"]}
        for k, limit in (("venue", 120), ("price", 40), ("age", 40), ("why", 200)):
            if e.get(k):
                row[k] = str(e[k])[:limit]
        if _https(e.get("url")):
            row["url"] = e["url"]
        if e.get("lat") is not None and e.get("lon") is not None:
            row["lat"], row["lon"] = float(e["lat"]), float(e["lon"])
        rows.append(row)
    return rows


# --- the words that offer these tools at once (only offered; the model still decides) ---------

_ASKS_PRICE = re.compile(
    r"\b(?:prezzo\s+migliore|miglior\s+prezzo|al\s+prezzo\s+pi[uù]\s+basso|dove\s+(?:lo|la|le|li|l')\s*"
    r"(?:compro|trovo|prendo)|dove\s+conviene\s+comprar\w*|quanto\s+costa(?:no)?\s+(?:su|da|online)|"
    r"best\s+price|cheapest\s+(?:shop|place|price)|where\s+(?:can\s+i|to|should\s+i)\s+buy)\b",
    re.IGNORECASE,
)
_ASKS_EVENTS = re.compile(
    r"\b(?:eventi|concerti?|spettacol[io]|a\s+teatro|in\s+programma|cosa\s+(?:c'[eè]|si\s+fa|fare|danno)\s+"
    r"(?:stasera|stanotte|sabato|domenica|nel\s+(?:fine\s+settimana|weekend)|domani)|stasera\s+in\s+citt[aà]|"
    r"what'?s\s+on|events?\s+(?:tonight|this\s+weekend|on\s+saturday)|concerts?|gigs?)\b",
    re.IGNORECASE,
)
_ASKS_SLOTS = re.compile(
    r"\b(?:c'[eè]\s+posto|posti\s+liberi|un\s+tavolo\s+(?:stasera|domani)|"
    r"disponibilit[aà]\s+(?:stasera|domani)|a\s+table\s+(?:for|at|tonight|tomorrow)|any\s+tables?|"
    r"availability\s+(?:tonight|tomorrow))\b",
    re.IGNORECASE,
)


def tools_for(words: str) -> frozenset[str]:
    """The answer tools their words point to."""
    found: set[str] = set()
    if _ASKS_PRICE.search(words or ""):
        found |= {"shop_prices", "show_products"}
    if _ASKS_EVENTS.search(words or ""):
        found |= {"events_on", "show_events"}
    if _ASKS_SLOTS.search(words or ""):
        found.add("table_slots")
    return frozenset(found)
