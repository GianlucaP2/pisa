# ruff: noqa: E501
"""The protocol-first ladder, step one.

- **The ladder as data**: per category (restaurants, travel, retail, utilities, Italian public services) and
  country, the routes Pisa knows, best first (the ladder's order, protocol → business agent → signed browser →
  call → deep link, except where the research puts a deep link first: utilities, public services). Each
  route says whether it is `live` today, only `watch`ed (the research found it announced or partner-only), or
  `none`. The preparers ask `routes(category, country)` and use the first live rung Pisa can carry today
  (`CARRIED`): the research's §5 table is the seed; `app_settings.world.ladder` replaces a category's routes.
- **UCP retail** (US): the merchants Pisa may talk to (an allowlist: `app_settings.world.merchants` replaces it),
  what their catalog and cart said, parsed strictly. RAIL: a price is shown only as the merchant stated it
  (an integer amount in minor units and a currency); an item without one says so; Pisa never computes, rounds or
  invents a price, and the total is the merchant's own total line (never Pisa's sum). The checkout link is the
  merchant's own `continue_url`, https on the merchant's own domain, plain (`place_site`), and the button shows
  its host. Nothing is bought: the user pays on the merchant's page.

Pure: no I/O.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pisa.domain import countries, handoffs, identity

Category = Literal["restaurants", "travel", "retail", "utilities", "public_it"]
Rung = Literal["protocol", "business_agent", "signed_browser", "call", "deep_link"]
State = Literal["live", "watch", "none"]
CATEGORIES: tuple[Category, ...] = ("restaurants", "travel", "retail", "utilities", "public_it")
RUNGS: tuple[Rung, ...] = ("protocol", "business_agent", "signed_browser", "call", "deep_link")
# What Pisa carries today (step one): a UCP cart for retail; a call they place; a deep link. No business agent, no
# signed browser yet: their routes are known but skipped.
CARRIED: frozenset[Rung] = frozenset({"protocol", "call", "deep_link"})
PRODUCTS_MAX = 5  # a search's results shown
QUANTITY_MAX = 20
ITEMS_MAX = 10  # a cart's lines
DISCOVERY_DAYS = 7  # a merchant's /.well-known/ucp, read again after
DISCOVERY_ERROR_FOR = timedelta(hours=1)  # a discovery that failed (a timeout, a 5xx), tried again after
CURRENCY = re.compile(r"^[A-Z]{3}$")
MINOR = {"JPY": 0, "KRW": 0}  # minor-unit digits (ISO 4217) other than 2


@dataclass(frozen=True)
class Route:
    rung: Rung
    state: State
    via: str  # the protocol, the platform, the app ("UCP", "Shopify Shop MCP", "TheFork")
    note: str = ""
    source: str = ""


def _r(rung: Rung, state: State, via: str, note: str = "", source: str = "") -> Route:
    return Route(rung, state, via, note, source)


_RESEARCH = "a survey of public ordering protocols (Nov 2026)"
# FACT: the first rung that works today, per category and country.
SEED: dict[str, dict[str, list[Route]]] = {
    "restaurants": {
        "US": [_r("protocol", "watch", "UCP food", "coming soon (DoorDash, Toast, Square, Uber Eats)", _RESEARCH),
               _r("business_agent", "none", "OpenTable/Resy", "no public agent API; AI Mode only inside Google", _RESEARCH),
               _r("call", "live", "the restaurant's phone", "they call; Pisa writes what to say", _RESEARCH),
               _r("deep_link", "live", "OpenTable, Resy", "", _RESEARCH)],
        "IT": [_r("protocol", "none", "—", "no protocol in Italy", _RESEARCH),
               _r("business_agent", "none", "TheFork", "no public agent API", _RESEARCH),
               _r("call", "live", "the restaurant's phone", "they call; Pisa writes what to say", _RESEARCH),
               _r("deep_link", "live", "TheFork", "", _RESEARCH)],
    },
    "travel": {
        "US": [_r("protocol", "watch", "UCP lodging", "draft (Booking, Expedia, Hilton, Marriott)", _RESEARCH),
               _r("business_agent", "watch", "Etraveli Flight MCP", "OAuth; open to individuals unknown (owner decision)", _RESEARCH),
               _r("signed_browser", "watch", "signed browser", "", _RESEARCH),
               _r("call", "live", "the hotel's phone", "", _RESEARCH),
               _r("deep_link", "live", "the airline's or the hotel's page", "Pisa searches, they book", _RESEARCH)],
        "IT": [_r("protocol", "none", "—", "lastminute.com on Visa's rail: approved agents only", _RESEARCH),
               _r("business_agent", "none", "Trenitalia, Italo, ITA", "none found", _RESEARCH),
               _r("call", "live", "the hotel's phone", "", _RESEARCH),
               _r("deep_link", "live", "the carrier's or the hotel's page", "Pisa searches, they book", _RESEARCH)],
    },
    "retail": {
        "US": [_r("protocol", "live", "UCP", "search, cart, the checkout link to them (allowlisted merchants)", _RESEARCH),
               _r("business_agent", "watch", "Shopify Shop MCP", "", _RESEARCH),
               _r("signed_browser", "watch", "WebMCP stores", "", _RESEARCH),
               _r("deep_link", "live", "the store's search page", "", _RESEARCH)],
        "IT": [_r("protocol", "watch", "UCP", "few merchants; Nexi pilots", _RESEARCH),
               _r("business_agent", "watch", "Shopify Shop MCP", "", _RESEARCH),
               _r("deep_link", "live", "the store's own page", "", _RESEARCH)],
    },
    "utilities": {
        "US": [_r("deep_link", "live", "the provider's portal", "they log in", _RESEARCH),
               _r("call", "live", "the provider's service line", "service issues", _RESEARCH)],
        "IT": [_r("deep_link", "live", "the provider's portal", "they log in; Pisa drafts, they send (PEC/email)", _RESEARCH),
               _r("call", "live", "the provider's service line", "service issues", _RESEARCH)],
    },
    "public_it": {
        "IT": [_r("protocol", "none", "pagoPA, app IO, SPID, CIE", "accredited parties only; SPID/CIE are personal", _RESEARCH),
               _r("deep_link", "live", "app IO, the agency's portal, the pagoPA notice", "they authenticate; Pisa prepares the data", _RESEARCH),
               _r("call", "live", "the office (CUP, anagrafe)", "appointments", _RESEARCH)],
    },
}  # fmt: skip


def _route(raw: Any) -> Route | None:
    if not isinstance(raw, dict):
        return None
    rung, state, via = raw.get("rung"), raw.get("state"), raw.get("via")
    if (
        rung not in RUNGS
        or state not in ("live", "watch", "none")
        or not isinstance(via, str)
        or not via.strip()
    ):
        return None
    return Route(rung, state, handoffs.clean(via)[:80], handoffs.clean(raw.get("note") or "")[:200],
                 handoffs.clean(raw.get("source") or "")[:200])  # fmt: skip


def ladder(setting: dict[str, Any] | None) -> dict[str, dict[str, list[Route]]]:
    """The seed, a category's countries replaced by `app_settings.world.ladder` where it gives valid routes
    (in the order given: the first is the preferred one)."""
    out = {c: {k: list(v) for k, v in by.items()} for c, by in SEED.items()}
    for category, by_country in ((setting or {}).get("ladder") or {}).items():
        if category not in CATEGORIES or not isinstance(by_country, dict):
            continue
        for country, raw in by_country.items():
            if not countries.is_code(country) or not isinstance(raw, list):
                continue
            routes = [r for r in (_route(x) for x in raw) if r is not None]
            if routes:
                out.setdefault(category, {})[country] = routes
    return out


def routes(table: dict[str, dict[str, list[Route]]], category: str, country: str | None) -> list[Route]:
    """A category's routes in a country (the user's own, PISA_COUNTRY, when the case has none), best first.
    A country the research has no routes for (it covered Italy and the US; `app_settings.world.ladder` adds
    any country) takes the US's, then Italy's."""
    by = table.get(category) or {}
    where = (country or "").upper()
    return list(by.get(where) or by.get("US") or by.get("IT") or [])


def first_rung(
    table: dict[str, dict[str, list[Route]]],
    category: str,
    country: str | None,
    available: frozenset[str] | set[str] = CARRIED,
) -> Rung | None:
    """The highest rung that is live today, that Pisa carries, and that this case has (`available`: e.g. no
    `protocol` when the merchant has no UCP endpoint, no `call` without a number)."""
    for r in routes(table, category, country):
        if r.state == "live" and r.rung in CARRIED and r.rung in available:
            return r.rung
    return None


def order(
    table: dict[str, dict[str, list[Route]]], category: str, country: str | None, rungs: list[str]
) -> list[str]:
    """The given rungs in the ladder's order for this category and country (live ones first; a rung the
    ladder doesn't list keeps its place after them)."""
    ranked = [r.rung for r in routes(table, category, country) if r.state == "live"]
    return sorted(rungs, key=lambda g: ranked.index(g) if g in ranked else len(RUNGS) + rungs.index(g))


# --- UCP merchants ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Merchant:
    key: str  # "target"
    name: str  # "Target"
    host: str  # "target.com": its /.well-known/ucp, and the domain its endpoint and checkout links must be on
    country: str
    search: str  # its own search page, `{q}` the query (the deep-link rung)
    aliases: tuple[str, ...] = field(default=())


# FACT: UCP's US retail co-developers and Google's named merchants. Each merchant's own
# search page (a page the user opens; never a login, never a checkout).
MERCHANTS: tuple[Merchant, ...] = (
    Merchant("target", "Target", "target.com", "US", "https://www.target.com/s?searchTerm={q}"),
    Merchant("walmart", "Walmart", "walmart.com", "US", "https://www.walmart.com/search?q={q}"),
    Merchant("wayfair", "Wayfair", "wayfair.com", "US", "https://www.wayfair.com/keyword.php?keyword={q}"),
    Merchant("etsy", "Etsy", "etsy.com", "US", "https://www.etsy.com/search?q={q}"),
    Merchant("nike", "Nike", "nike.com", "US", "https://www.nike.com/w?q={q}"),
    Merchant("sephora", "Sephora", "sephora.com", "US", "https://www.sephora.com/search?keyword={q}"),
    Merchant("ulta", "Ulta Beauty", "ulta.com", "US", "https://www.ulta.com/search?search={q}", ("ulta beauty",)),
)  # fmt: skip
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def merchants(setting: dict[str, Any] | None) -> tuple[Merchant, ...]:
    """The allowlist: `app_settings.world.merchants` (each {key, name, host, country, search}) replaces the
    seed when it gives any valid one; a search page must be https on the merchant's own domain."""
    out: list[Merchant] = []
    for raw in (setting or {}).get("merchants") or ():
        if not isinstance(raw, dict):
            continue
        key, name, host = (
            str(raw.get("key") or ""),
            str(raw.get("name") or ""),
            str(raw.get("host") or "").lower(),
        )
        search, country = str(raw.get("search") or ""), str(raw.get("country") or "US").upper()
        if not (re.fullmatch(r"[a-z0-9_]{2,30}", key) and name.strip() and _HOST.match(host)
                and countries.is_code(country) and "{q}" in search):  # fmt: skip
            continue
        if not on_domain(search.replace("{q}", "x"), host):
            continue
        out.append(Merchant(key, handoffs.clean(name)[:40], host, country, search))
    return tuple(out) or MERCHANTS


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", handoffs.clean(text).casefold()).strip()


def merchant(allow: tuple[Merchant, ...], named: str) -> Merchant | None:
    """The allowlisted merchant their words name ("Target", "target.com", "su Walmart"), else None."""
    said = (
        _plain(named).removeprefix("su ").removeprefix("da ").removeprefix("on ").removeprefix("at ").strip()
    )
    for m in allow:
        names = {m.key, _plain(m.name), _plain(m.host), _plain(m.host.split(".")[0]), *map(_plain, m.aliases)}
        if said in names:
            return m
    return None


def on_domain(url: str, host: str) -> bool:
    """The URL is plain https (`place_site`) on the merchant's domain or one of its subdomains."""
    if handoffs.place_site(url) is None:
        return False
    name = (urlsplit(url).hostname or "").lower()
    return name == host or name.endswith("." + host)


def search_page(m: Merchant, query: str) -> str:
    """The deep-link rung: the merchant's own search page for their words, the query percent-encoded whole."""
    return m.search.replace("{q}", quote(handoffs.clean(query)[:120], safe=""))


@dataclass(frozen=True)
class Price:
    amount: int  # minor units, as the merchant stated it
    currency: str


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    price: Price | None


@dataclass(frozen=True)
class Line:
    product_id: str
    title: str
    quantity: int
    price: Price | None  # one item's, as stated
    total: Price | None  # the line's, as stated


@dataclass(frozen=True)
class Cart:
    id: str
    lines: tuple[Line, ...]
    total: Price | None  # the merchant's own total line, never Pisa's sum
    continue_url: str | None


def price(raw: Any, currency: Any = None) -> Price | None:
    """A stated price: {amount: int (minor units), currency: "USD"}, or an int beside the response's currency.
    A float, a string or a negative amount is no stated price (Pisa never converts or guesses)."""
    if isinstance(raw, dict):
        currency, raw = raw.get("currency", currency), raw.get("amount")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 10**9:
        return None
    if not isinstance(currency, str) or not CURRENCY.match(currency):
        return None
    return Price(raw, currency)


def money(p: Price | None, lang: str) -> str:
    """A stated price as they read it: "$12.99", "12,99 $"; "prezzo non indicato" when the merchant gave none."""
    if p is None:
        return "price not stated" if lang.startswith("en") else "prezzo non indicato"
    digits = MINOR.get(p.currency, 2)
    whole, part = divmod(p.amount, 10**digits) if digits else (p.amount, 0)
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(p.currency)
    number = f"{whole:,}" + (f".{part:0{digits}d}" if digits else "")
    if lang.startswith("en"):
        return f"{symbol}{number}" if symbol else f"{number} {p.currency}"
    number = number.replace(",", "\u2009").replace(".", ",")
    return f"{number} {symbol or p.currency}"


def _text(raw: Any, limit: int) -> str:
    return " ".join(handoffs.clean(raw).split())[:limit] if isinstance(raw, str) else ""


def products(body: Any) -> list[Product]:
    """A catalog search's products ({products|items|results: [{id, title, price}]}), strictly: an id and a
    title each; the price only when stated."""
    if not isinstance(body, dict):
        return []
    currency = body.get("currency")
    raw = next((body[k] for k in ("products", "items", "results") if isinstance(body.get(k), list)), [])
    out: list[Product] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        pid, title = _text(p.get("id"), 120), _text(p.get("title") or p.get("name"), 120)
        if pid and title:
            out.append(Product(pid, title, price(p.get("price"), p.get("currency", currency))))
    return out[:PRODUCTS_MAX]


def cart(body: Any, host: str) -> Cart | None:
    """A checkout session (UCP's cart) as the merchant returned it: its lines, its own total, its
    `continue_url` (kept only on the merchant's domain, plain https)."""
    if not isinstance(body, dict) or not _text(body.get("id"), 200):
        return None
    currency = body.get("currency")
    lines: list[Line] = []
    for raw in body.get("line_items") or ():
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = raw["item"] if isinstance(raw.get("item"), dict) else {}
        qty = raw.get("quantity")
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= QUANTITY_MAX:
            continue
        pid, title = _text(item.get("id"), 120), _text(item.get("title"), 120)
        if not pid or not title:
            continue
        line_total = next((t.get("amount") for t in raw.get("totals") or () if isinstance(t, dict)
                           and t.get("type") in ("total", "subtotal")), None)  # fmt: skip
        lines.append(Line(pid, title, qty, price(item.get("price"), currency), price(line_total, currency)))
    total = next((t.get("amount") for t in body.get("totals") or () if isinstance(t, dict)
                  and t.get("type") == "total"), None)  # fmt: skip
    link = body.get("continue_url")
    link = link if isinstance(link, str) and on_domain(link, host) else None
    return Cart(_text(body["id"], 200), tuple(lines[:ITEMS_MAX]), price(total, currency), link)


def profile(body: Any, host: str) -> tuple[str, str, list[str]] | None:
    """A merchant's `/.well-known/ucp`: (version, the shopping service's REST endpoint, the capability names);
    None when it has none Pisa can use (the endpoint must be https on the merchant's own domain)."""
    if not isinstance(body, dict) or not isinstance(body.get("ucp"), dict):
        return None
    ucp = body["ucp"]
    version = _text(ucp.get("version"), 20)
    service = (
        (ucp.get("services") or {}).get("dev.ucp.shopping") if isinstance(ucp.get("services"), dict) else None
    )
    rest = service.get("rest") if isinstance(service, dict) else None
    endpoint = rest.get("endpoint") if isinstance(rest, dict) else None
    if not isinstance(endpoint, str) or not on_domain(endpoint.rstrip("/") + "/x", host):
        return None
    if identity.FORBIDDEN.search(urlsplit(endpoint).path):  # /api/orders/ucp is no catalog
        return None
    caps = [_text(c.get("name"), 80) for c in ucp.get("capabilities") or () if isinstance(c, dict)]
    return version, endpoint.rstrip("/"), [c for c in caps if c]
