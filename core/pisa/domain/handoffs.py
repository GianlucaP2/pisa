# ruff: noqa: E501
"""handoffs. Pisa prepares (the page, the number, the words, the list) and the user's tap in the
official app is the yes: Pisa never sends, books, calls or pays by itself.


RAIL: every URL Pisa puts on a button comes from a template here, its parameters percent-encoded (control and
bidi characters gone first), on a fixed host or a strict tel:/sms: form; a place's own website (from the places
provider) is the one URL kept as given, and only when it is plain ASCII https with no credentials, no port, no
redirector or shortener and no other URL inside it (no open redirect); the button then shows its host.
`safe_url` is the last check before a URL leaves Pisa, whatever built it. Pure: no I/O.
"""

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pisa.domain import locale
from pisa.domain.lexicon.months import EN_MONTHS_TITLE
from pisa.domain.lexicon.weekdays import DAY_NAMES
from pisa.domain.money_links import HOSTS as MONEY_HOSTS
from pisa.domain.text import fold

Kind = Literal["open_url", "call", "message", "map"]
Purpose = Literal["booking", "ride", "message", "call", "shopping", "money"]
Ride = Literal["uber", "lyft", "freenow", "taxi"]
Store = Literal["glovo", "deliveroo", "esselunga", "instacart", "justeat"]
Platform = Literal["thefork", "opentable"]

# What the phone may open (the block's schema allows the same): https, tel: and sms:, Pisa's templates only.
SCHEMES = ("https", "tel", "sms")
# The fixed hosts of Pisa's templates (a place's own website is checked by `place_site` instead).
TEMPLATE_HOSTS = frozenset({
    "wa.me", "m.uber.com", "lyft.com", "maps.apple.com", "www.google.com", "www.thefork.it",
    "www.thefork.com",
    "www.opentable.com", "www.opentable.it", "glovoapp.com", "deliveroo.it", "www.esselunga.it",
    "www.instacart.com", "www.free-now.com", "www.ittaxi.it", "www.justeat.it",
})  # fmt: skip

# FACT: a store's own landing page (the user logs in in its app; Pisa never does).
STORES: dict[str, str] = {
    "glovo": "https://glovoapp.com/it/it/",
    "deliveroo": "https://deliveroo.it/it/",
    "esselunga": "https://www.esselunga.it/it-it/spesa-online.html",
    "instacart": "https://www.instacart.com/store",
    "justeat": "https://www.justeat.it/",
}
STORE_NAMES = {"glovo": "Glovo", "deliveroo": "Deliveroo", "esselunga": "Esselunga",
               "instacart": "Instacart", "justeat": "Just Eat"}  # fmt: skip
# FACT: the big cities' radio-taxi dispatchers (public numbers, printed on every cab). Italy's: on only when
# the user's country is IT (PISA_COUNTRY), as is the itTaxi app.
TAXI_COUNTRY = "IT"
TAXI_DISPATCH: dict[str, tuple[str, str]] = {
    "roma": ("Radio Taxi 3570", "+39063570"),
    "rome": ("Radio Taxi 3570", "+39063570"),
    "milano": ("Taxi Blu 4040", "+39024040"),
    "milan": ("Taxi Blu 4040", "+39024040"),
}
FREENOW_URL = "https://www.free-now.com/it/"
ITTAXI_URL = "https://www.ittaxi.it/"

FOLLOW_UP_AFTER: dict[str, timedelta] = {  # how long after the first tap Pisa asks how it went (once)
    "booking": timedelta(minutes=20),
    "call": timedelta(minutes=15),
}
OPEN_FOR = timedelta(hours=24)  # a preparation the user can still answer about
LIST_MAX = 30  # a shopping list's lines
TEXT_MAX = 1000  # a handoff's prepared words (the block's limit)

_PHONE = re.compile(r"^\+[1-9][0-9]{5,14}$")
_TEL = re.compile(r"^tel:\+[1-9][0-9]{5,14}$")
_SMS = re.compile(r"^sms:(\+[1-9][0-9]{5,14})?&body=[A-Za-z0-9%._~-]*$")
# Premium and paid numbers Pisa never puts on a button (IT 89x, 892, 899, 166, 144, 12xx; US 900, 976).
PREMIUM = ("+3989", "+39166", "+39144", "+3912", "+1900", "+1976",
           # elsewhere too (UK 09/087/070, FR 089/08 9, DE 0900/0137, ES 803-807/905,
           # CH 0900/0901/0906, AT 0900/0930) and the shared international codes (satellite, networks, UIFN)
           "+449", "+4487", "+4470", "+3389", "+49900", "+49137", "+34803", "+34806", "+34807",
           "+34905", "+41900", "+41901", "+41906", "+43900", "+43930",
           "+870", "+881", "+882", "+883", "+888", "+979", "+808")  # fmt: skip
# Who forwards a click elsewhere: a site that is one of these is no place's own page.
REDIRECTORS = frozenset({
    "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "lnkd.in", "rebrand.ly", "cutt.ly",
    "shorturl.at", "tiny.cc", "rb.gy", "l.facebook.com", "lm.facebook.com", "l.instagram.com", "out.reddit.com",
    "href.li", "urldefense.com", "safelinks.protection.outlook.com", "click.redditmail.com", "linktr.ee",
    # more shorteners
    "j.mp", "amzn.to", "amzn.eu", "youtu.be", "t.ly", "s.id", "v.gd", "cli.re", "bl.ink", "short.io",
    "trib.al", "dlvr.it", "fb.me", "wp.me", "g.co", "forms.gle", "maps.app.goo.gl", "page.link",
})  # fmt: skip
# a query parameter that names where to go next: a domain in it (even schemeless) is a redirect
_NEXT_PARAMS = frozenset({
    "q", "u", "url", "uri", "link", "redirect", "redirect_uri", "redirect_url", "next", "target", "dest",
    "destination", "continue", "out", "to", "goto", "r", "return", "returnurl", "return_to", "view", "site",
})  # fmt: skip
_DOMAINISH = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[/:?#]|$)")


def clean(text: object) -> str:
    """Text with no control or format characters (bidi overrides, zero-width), newlines kept."""
    return "".join(
        c for c in str(text) if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs")
    )


def premium(phone: str) -> bool:
    return phone.startswith(PREMIUM)


def _q(value: object) -> str:
    """A parameter, percent-encoded whole: no `&`, `#`, `?` or `/` survives to change the URL."""
    return quote(clean(value), safe="")


def _coord(value: float) -> str:
    return f"{float(value):.6f}"


def normalize_phone(text: str | None, region: str | None) -> str | None:
    """A phone number as E.164 ("+39063570"), or None when it isn't one. Spaces, dots, dashes and brackets
    go; "00" is "+"; without a country code, the region's (IT +39, US +1; None: the user's country,
    PISA_COUNTRY). Without either (or in a country Pisa has no rule for) a number needs its country code."""
    if not text:
        return None
    raw = re.sub(r"[\s().\-/]", "", text.strip())
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not re.fullmatch(r"\+?[0-9]{6,15}", raw):
        return None
    if not raw.startswith("+"):
        home = (region or "").upper()
        if home == "US" and len(raw) == 10:
            raw = "+1" + raw
        elif home == "US" and len(raw) == 11 and raw.startswith("1"):
            raw = "+" + raw
        elif home == "IT":
            raw = "+39" + raw
        else:
            return None
    return raw if _PHONE.match(raw) else None


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


_PHONE_SPAN = re.compile(r"(?<![\w+])(\+|00)?\d[\d .\-/()]{4,20}\d(?![\w])")


def phones_in(words: str, region: str | None) -> set[str]:
    """The phone numbers the user's words hold, each as E.164 (their country's when they gave none)."""
    found: set[str] = set()
    for span in _PHONE_SPAN.finditer(words):
        number = normalize_phone(span.group(0), region)
        if number is not None:
            found.add(number)
    return found


def phone_in(words: str, phone: str, region: str | None) -> bool:
    """RAIL: the number is in the user's own words, exactly (country code included): then it is theirs,
    not outside text's. A time or a date never adds up to one."""
    return phone in phones_in(words, region)


def tel(phone: str) -> str:
    if not _PHONE.match(phone) or premium(phone):
        raise ValueError("not an E.164 phone number")
    return f"tel:{phone}"


def whatsapp(phone: str | None, text: str) -> str:
    """WhatsApp's own click-to-chat: with a number the chat opens on it; without, WhatsApp asks whom to send
    it to (the user picks the contact). The words are prefilled, never sent."""
    body = _q(text[:TEXT_MAX])
    if phone is None:
        return f"https://wa.me/?text={body}"
    if not _PHONE.match(phone):
        raise ValueError("not an E.164 phone number")
    return f"https://wa.me/{phone[1:]}?text={body}"


def sms(phone: str | None, text: str) -> str:
    """iMessage/SMS: `sms:<number>&body=` (iOS's form); without a number the user picks the contact."""
    if phone is not None and not _PHONE.match(phone):
        raise ValueError("not an E.164 phone number")
    return f"sms:{phone or ''}&body={_q(text[:TEXT_MAX])}"


def _pairs(pairs: list[tuple[str, object]]) -> str:
    # brackets percent-encoded too (the phone opens the URL exactly as given, RFC 3986)
    return "&".join(f"{quote(k, safe='')}={_q(v)}" for k, v in pairs)


def uber(pickup: tuple[float, float] | None, dropoff: tuple[float, float], nickname: str) -> str:
    """Uber's universal link (the app, else the mobile site): pickup and dropoff prefilled; the user taps
    Request in Uber."""
    pairs: list[tuple[str, object]] = [("action", "setPickup")]
    if pickup is None:
        pairs.append(("pickup", "my_location"))
    else:
        pairs += [("pickup[latitude]", _coord(pickup[0])), ("pickup[longitude]", _coord(pickup[1]))]
    pairs += [("dropoff[latitude]", _coord(dropoff[0])), ("dropoff[longitude]", _coord(dropoff[1])),
              ("dropoff[nickname]", nickname[:80])]  # fmt: skip
    return "https://m.uber.com/ul/?" + _pairs(pairs)


def lyft(pickup: tuple[float, float] | None, dropoff: tuple[float, float]) -> str:
    """Lyft's universal link (developer.lyft.com/docs/universal-links)."""
    pairs: list[tuple[str, object]] = [("id", "lyft")]
    if pickup is not None:
        pairs += [("pickup[latitude]", _coord(pickup[0])), ("pickup[longitude]", _coord(pickup[1]))]
    pairs += [("destination[latitude]", _coord(dropoff[0])), ("destination[longitude]", _coord(dropoff[1]))]
    return "https://lyft.com/ride?" + _pairs(pairs)


def apple_maps(name: str, lat: float | None, lon: float | None) -> str:
    url = f"https://maps.apple.com/?q={_q(name[:120])}"
    return url + (f"&ll={_coord(lat)},{_coord(lon)}" if lat is not None and lon is not None else "")


def booking_search(platform: Platform, name: str, city: str | None) -> str:
    """The restaurant's page on a booking platform, found by its name (never a login, never a booking)."""
    words = " ".join(w for w in (name, city or "") if w).strip()[:120]
    if platform == "thefork":
        return f"https://www.thefork.it/search?queryText={_q(words)}"
    return f"https://www.opentable.com/s?term={_q(words)}"


def store_page(store: str) -> str:
    if store not in STORES:
        raise ValueError(f"unknown store {store!r}")
    return STORES[store]


def place_site(url: str | None) -> str | None:
    """A place's own website, as the places provider gave it: kept only when it is plain ASCII https on a real
    host (no punycode), with no credentials, no port, no control or bidi character, not a redirector or a
    shortener, and no other URL in its path or query, even encoded (a redirect Pisa can't vouch for)."""
    if not url:
        return None
    text = url.strip()
    if (
        len(text) > 2000
        or not text.isascii()
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text)
    ):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or "." not in host or parts.username or parts.password:
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", host) or "xn--" in host or re.fullmatch(r"[0-9.]+", host):
        return None
    # a trailing dot, or a last label a browser reads as an IP (decimal, hex: "0x7f.1")
    if host.endswith(".") or re.fullmatch(r"0x[0-9a-f]*|[0-9]+", host.rsplit(".", 1)[-1]):
        return None
    if host in REDIRECTORS or host.removeprefix("www.") in REDIRECTORS:
        return None
    if re.fullmatch(r"(www\.|maps\.)?google\.[a-z.]+", host) and parts.path.startswith(
        ("/url", "/aclk", "/amp")
    ):
        return None
    if re.fullmatch(r"((www|m|music)\.)?youtube\.com", host) and parts.path.startswith("/redirect"):
        return None
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _NEXT_PARAMS and _DOMAINISH.match(unquote(value).strip().lower()):
            return None
    try:
        if parts.port not in (None, 443):
            return None
    except ValueError:
        return None
    inside = parts.path + "?" + parts.query
    for _ in range(3):  # what an encoded (or twice encoded) path or query really says
        inside = unquote(inside)
    low = inside.lower()
    if re.search(r"(https?|javascript|data|ftp)\s*:", low) or "//" in low or "\\" in low:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in inside):
        return None
    return text


def site_host(url: str) -> str:
    """The host a button shows under a place's own site ("gabrielli.example")."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def safe_url(url: str, *, site: str | None = None, phone: str | None = None, money: bool = False) -> bool:
    """RAIL: the last check before a URL goes on a button: an https URL on a template host (or exactly the
    place's own website, `site`); a tel: of exactly the button's number (never a premium one); an sms: in Pisa's
    strict form; nothing else, no spaces or control characters."""
    if not url or len(url) > 2000 or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    scheme = url.split(":", 1)[0].lower()
    if scheme not in SCHEMES:
        return False
    if scheme == "tel":
        return bool(_TEL.match(url)) and url == f"tel:{phone}" and not premium(url[4:])
    if scheme == "sms":
        number = url[4:].split("&", 1)[0]
        return bool(_SMS.match(url)) and not (number and premium(number))
    if site is not None and url == site:
        return place_site(url) is not None
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    # an airline's or a service's own page only on a money button (purpose `money`)
    hosts = TEMPLATE_HOSTS | MONEY_HOSTS if money else TEMPLATE_HOSTS
    return (parts.hostname or "") in hosts and not parts.username and not parts.password


_WORKED = re.compile(
    r"\b(si|sì|fatto|fatta|prenotato|prenotata|confermato|confermata|spostato|spostata|ok|okay|yes|yep|done|"
    r"booked|worked|tutto bene|e andata bene|andata bene|perfetto|preso|presa)\b"
)
_FAILED = re.compile(
    r"\b(no|non|niente|nope|pieno|pieni|chiuso|chiusi|occupato|didn't|didnt|not|full|failed|nessuno)\b"
)


def outcome(words: str) -> Literal["worked", "failed"] | None:
    """Their word on a handoff ("sì, fatto", "prenotato", "no, era pieno"): worked, failed, or None when it says
    neither. A negation wins (RAIL: no write on a "non ancora")."""
    plain = fold(words)
    if _FAILED.search(plain):
        return "failed"
    if _WORKED.search(plain):
        return "worked"
    return None


def day_time(at: datetime, lang: str) -> str:
    """ "venerdì 7 alle 21", "Friday the 7th at 9 PM" (as a person says it on the phone)."""
    day = DAY_NAMES.get(lang, DAY_NAMES[locale.tag_of(lang)])[at.weekday()]
    if lang.startswith("en"):
        hour = at.strftime("%-I:%M %p").replace(":00", "")
        return f"{day}, {EN_MONTHS_TITLE[at.month - 1]} {at.day}, at {hour}"
    clock = f"{at.hour}" if at.minute == 0 else f"{at.hour}:{at.minute:02d}"
    return f"{day} {at.day} alle {clock}"


def booking_script(people: int, at: datetime, name: str | None, lang: str) -> str:
    """What the user says on the phone to book: one line."""
    who = f" a nome {name.strip()}" if name and name.strip() else ""
    if lang.startswith("en"):
        who = f" under the name {name.strip()}" if name and name.strip() else ""
        return f"Hi, I'd like to book a table for {people} on {day_time(at, lang)}{who}."
    persons = "persona" if people == 1 else "persone"
    return f"Buonasera, vorrei prenotare un tavolo per {people} {persons} {day_time(at, lang)}{who}."


def taxi_script(pickup: str | None, destination: str, name: str | None, lang: str) -> str:
    where = pickup or ("dove sono ora" if not lang.startswith("en") else "where I am now")
    if lang.startswith("en"):
        who = f", name {name.strip()}" if name and name.strip() else ""
        return f"Hi, a taxi at {where}, going to {destination}{who}, please."
    who = f", a nome {name.strip()}" if name and name.strip() else ""
    return f"Buongiorno, un taxi in {where} per {destination}{who}, grazie."


def dispatcher(address: str | None, country: str | None) -> tuple[str, str] | None:
    """The city's radio taxi (name, E.164) from the pickup's address, when Pisa knows it (in the user's
    country only)."""
    if (country or "").upper() != TAXI_COUNTRY.upper():
        return None
    plain = fold(address or "")
    return next((v for k, v in TAXI_DISPATCH.items() if re.search(rf"\b{k}\b", plain)), None)


def follow_up_text(purpose: str, target: str, lang: str) -> str:
    """The one question after the tap: how did it go."""
    if lang.startswith("en"):
        return {
            "booking": f"How did the booking at {target} go? If it's done, I'll put it in the calendar.",
            "call": f"How did the call to {target} go?",
        }.get(purpose, f"How did it go with {target}?")
    return {
        "booking": f"Com'è andata la prenotazione da {target}? Se è fatta, la metto in calendario.",
        "call": f"Com'è andata la chiamata a {target}?",
    }.get(purpose, f"Com'è andata con {target}?")


def shopping_list(items: list[str]) -> str:
    """One line each, as the user pastes it in the store's app; duplicates (any case) once."""
    seen: set[str] = set()
    lines: list[str] = []
    for item in items:
        line = " ".join(str(item).split())[:120]
        key = fold(line)
        if line and key not in seen:
            seen.add(key)
            lines.append(f"- {line}")
    return "\n".join(lines[:LIST_MAX])


_BUY = re.compile(r"^(compra|comprare|compro|acquista|acquistare|buy)\s+", re.IGNORECASE)
_ARTICLE = re.compile(r"^((il|lo|la|i|gli|le|l'|un|uno|una|del|dello|della|dei|degli|delle|the|some|a|an)\s*)",
                      re.IGNORECASE)  # fmt: skip


def shopping_task(title: str) -> str | None:
    """A task of the pool that is something to buy ("Comprare il latte" → "latte"), else None: only a task that
    starts with a buying verb (never "prendi i bambini", "get back to Marco", "fare la spesa")."""
    text = clean(title).strip()
    if not _BUY.match(text):
        return None
    rest = _ARTICLE.sub("", _BUY.sub("", text, count=1), count=1).strip()
    return rest or None


def in_words(item: str, words: str) -> bool:
    """A list item they said (each of its words, accents and case aside, among their words)."""
    said = set(re.findall(r"\w+", fold(words)))
    mine = re.findall(r"\w+", fold(item))
    return bool(mine) and all(w in said for w in mine)


# after outside text in the turn, Pisa prepares only what their own words ask for.
_ASKS: dict[str, re.Pattern[str]] = {
    "booking": re.compile(r"\b(prenot\w*|tavolo|book\w*|table|reserv\w*)\b"),
    "ride": re.compile(r"\b(uber|lyft|taxi|freenow|free now|ride|corsa|passaggio|portami)\b"),
    "message": re.compile(r"\b(scriv\w*|messaggi\w*|whatsapp|imessage|sms|rispondi\w*|avvis\w*|text|message|tell|write)\b"),
    "call": re.compile(r"\b(chiam\w*|telefon\w*|call|phone|ring)\b"),
    "shopping": re.compile(r"\b(spesa|ordin\w*|compra\w*|acquist\w*|carrello|glovo|deliveroo|esselunga|instacart|just ?eat|order|groceries|shop\w*|buy|cart)\b"),
}  # fmt: skip
_FOREIGN = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|net|org|io|it|ly|co|me|app|info|biz|xyz)\b\S*|\d[\d .\-]{2,}\d)",
                      re.IGNORECASE)  # fmt: skip


def asks_for(purpose: str, words: str) -> bool:
    """Their words ask for this kind of preparation ("scrivi a…", "prenota…", "chiama…")."""
    pattern = _ASKS.get(purpose)
    return pattern is not None and bool(pattern.search(fold(words)))


def foreign(text: str, words: str) -> list[str]:
    """The links, sites and codes or numbers (4+ digits) in Pisa's prepared words that aren't in their own:
    after outside text, where they would come from."""
    said = fold(words)
    said_digits = digits(words)
    out: list[str] = []
    for m in _FOREIGN.finditer(clean(text)):
        bit = m.group(0).strip(" .,;:")
        if bit[:1].isdigit():
            if len(digits(bit)) >= 4 and digits(bit) not in said_digits:
                out.append(bit)
        elif fold(bit) not in said:
            out.append(bit)
    return out


# the words they dictate for a message ("scrivi a Marco su WhatsApp che arrivo alle 9", "digli
# che faccio tardi", "scrivi a Laura: ci vediamo lì"): the message is exactly them, never Pisa's rewording
_DICTATED = re.compile(
    r"\b(?:scrivi|scrivere|scrivigli|scrivile|manda(?:gli|le)?\s+un\s+messaggio|messaggia|di(?:gli|lle)|"
    r"d[iì]\s+a\s+\w+|avvisa\s+\w+|rispondi|text|tell|message)\b[^:]*?(?:\s+che\s+|:\s*|\s+that\s+)(?P<text>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)


def dictated(words: str) -> str | None:
    """The message they dictated, as they said it (its first letter capital), or None when they didn't say the text."""
    found = _DICTATED.search(words.strip())
    if found is None:
        return None
    text = found.group("text").strip()
    if not text or len(text) > TEXT_MAX:
        return None
    return text[0].upper() + text[1:]


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def quoting_his(reply: str, reworded: list[tuple[str, str]]) -> str:
    """The reply with each reworded message's text, where Pisa quotes it, as they
    dictated it ("Pronto per Marco: «Ciao Marco, arrivo alle 9.»" says «Arrivo alle 9»)."""
    for mine, his in reworded:
        for text in (mine, mine.rstrip(".!")):
            if len(text) > 3 and text in reply:
                reply = reply.replace(text, his)
                break
    return reply


def rewords(text: str, his: str) -> bool:
    """The model's text holds their dictated words with words of its own around them ("Ciao Marco, arrivo alle 9."
    for "arrivo alle 9"): then the message is their words. The same words (Pisa's punctuation only) stay; a text
    without their words is the model's own, for the rails to judge (a code, a link)."""
    mine, said = _words(text), _words(his)
    if not said or mine == said:
        return False
    return any(mine[i : i + len(said)] == said for i in range(len(mine) - len(said) + 1))
