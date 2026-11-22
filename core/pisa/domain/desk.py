"""The CEO's desk, its rules, pure.

- **The window calendar**: every date comes from an **official page** (Italy, the EU, the
  US: `official`), is **linked**, and is **checked by code on the fetched page**: the date's own words are on
  the page (`quoted`), they say the same day (`parse_date`), and that day is among the page's dates
  (`dates_in`). A date that fails any check is never shown (`visible`).
- **The arbitrage desk, four ways**: product, regulation, cost, entry.
- **The customer's voice**: public reviews only (`public_review`), each quote found on its fetched page.
- **The money desk, as drafts**: the capital stack in code, non-dilutive money first, customers second, equity
  last (`capital_stack`); eligibility checked in code against their legal form (`eligible`). Nothing is sent,
  applied for or paid.
- **The decision journal**: Pisa's p and their p, Brier-scored per party and domain.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pisa.domain.lexicon.advice import NOT_ADVICE_IT
from pisa.domain.lexicon.dates import day_or_none
from pisa.domain.lexicon.months import EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS, IT_MONTHS_SHORT, month_numbers
from pisa.domain.night import brier
from pisa.domain.text import fold_spaces

# --- caps and sizes ---

DESK_CAP_EUR = Decimal("0.04")  # FACT: the M row is ≈ €0.005; x8 margin, under the €0.25
DESK_SEARCHES = 6  # the desk's share of the night's 40: ideas 26, curiosity 8, desk 6
SOURCES_PER_NIGHT = 4  # official pages fetched a night from the fixed list (a person's pace)
PAGES_MAX = 16  # every page the desk fetches in a night, all reads together
PAGE_CHARS = 8000  # what of a page a model reads (the check runs on the whole page)
WINDOWS_PER_PAGE = 6
CHECK_DAYS = 7  # FACT ("every night"): a window not re-checked for a week is not shown
HORIZON_DAYS = 180  # the calendar's reach: the next six months
DOSSIERS_MAX = 3  # FACT: the dossiers follow the ≤ 3 ideas ready for a morning
NEIGHBOURS = 3  # the nearest ideas a dossier lists (the cemetery's too)
QUOTE_MAX = 240
VOICES_MAX = 4
ACQUIRERS_MAX = 3

Country = Literal["it", "us", "eu"]
WindowKind = Literal["grant", "rule", "fair", "cfp", "tax", "budget"]
Way = Literal["product", "regulation", "cost", "entry"]
WAYS: tuple[Way, ...] = ("product", "regulation", "cost", "entry")  # FACT: four ways
Party = Literal["pisa", "owner"]
Domain = Literal["market", "timing_cost", "window"]
DOMAINS: tuple[Domain, ...] = ("market", "timing_cost", "window")
# Their legal forms, in a fixed vocabulary (their setting `desk.legal_forms`, never inferred)
FORMS = frozenset({"forfettario", "ditta", "srl", "srls", "spa", "llc", "c_corp", "s_corp", "sole_prop"})

# --- the fixed list of official pages (a starting list; a person would read these) ---

SOURCES: tuple[tuple[Country, WindowKind, str], ...] = (
    ("it", "grant", "https://www.invitalia.it/cosa-facciamo/rafforziamo-le-imprese"),
    ("it", "grant", "https://www.mimit.gov.it/it/incentivi"),
    ("it", "tax", "https://www.agenziaentrate.gov.it/portale/web/guest/schede/scadenzario"),
    ("eu", "grant", "https://eic.ec.europa.eu/eic-funding-opportunities_en"),
    ("us", "grant", "https://www.sbir.gov/topics"),
    ("us", "tax", "https://www.irs.gov/businesses/small-businesses-self-employed/online-tax-calendar"),
    ("us", "grant", "https://www.grants.gov/search-grants"),
    ("us", "rule", "https://www.federalregister.gov/public-inspection/current"),
)
# Discovery queries, fixed in code (nothing of theirs in them by construction; still through the frame's
# filter)
DISCOVERY: tuple[tuple[Country, str], ...] = (
    ("it", "bando contributi imprese scadenza presentazione domande"),
    ("us", "small business innovation research solicitation deadline"),
)

# --- official pages: the host decides, never the page ---

# the regions' and the autonomous provinces' own domains, named one by one (a pattern like
# `regione.<anything>.it` would pass `regione.evil-bandi.it`)
_REGIONS = (
    "abruzzo basilicata calabria campania emilia-romagna fvg lazio liguria lombardia marche molise piemonte "
    "puglia sardegna sicilia toscana umbria vda veneto taa"
).split()
_IT_HOSTS = re.compile(
    r"(?:^|\.)(?:gov\.it|invitalia\.it|inps\.it|inail\.it|simest\.it|camcom\.it|"
    r"provincia\.tn\.it|provincia\.bz\.it|"
    r"regione\.(?:" + "|".join(re.escape(r) for r in _REGIONS) + r")\.it)$"
)
_US_HOSTS = re.compile(r"(?:^|\.)(?:gov|mil)$")
_EU_HOSTS = re.compile(r"(?:^|\.)europa\.eu$")


def host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def link(url: str) -> bool:
    """A link Pisa may cite and fetch: https, a host, no credentials, no spaces (core-ideas' own rule)."""
    return bool(re.fullmatch(r"https://[^\s/@:]+(?::443)?(?:/\S*)?", url or "")) and len(url) <= 2000


def official(url: str, country: Country | None = None) -> Country | None:
    """RAIL ("each from an official page"): the country whose official host serves `url`
    (a public administration's own domain), or None. With `country`, only that one."""
    if not link(url):
        return None
    h = host(url)
    found: Country | None = (
        "it"
        if _IT_HOSTS.search(h)
        else "us"
        if _US_HOSTS.search(h)
        else "eu"
        if _EU_HOSTS.search(h)
        else None
    )
    return found if country is None or found == country else None


# Public reviews: app stores and review sites a person reads without an account
_REVIEW_HOSTS = re.compile(
    r"(?:^|\.)(?:trustpilot\.com|g2\.com|capterra\.com|capterra\.it|getapp\.com|apps\.apple\.com|"
    r"play\.google\.com|tripadvisor\.com|tripadvisor\.it|yelp\.com|producthunt\.com)$"
)


def public_review(url: str) -> bool:
    """RAIL (public reviews only): a review page of a public review site or app store; never a
    social profile, a forum thread of a person, mail or anything behind an account."""
    return link(url) and bool(_REVIEW_HOSTS.search(host(url)))


# --- dates on a page ---

_MONTHS = month_numbers(IT_MONTHS, IT_MONTHS_SHORT, EN_MONTHS, EN_MONTHS_SHORT, extra={"sett": 9, "sept": 9})
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:°|o|st|nd|rd|th)?\s+(?:di\s+)?({_MONTH})\.?,?\s+(\d{{4}})\b", re.I)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)


def dates_in(text: str, country: Country) -> set[date]:
    """Every calendar day a page writes, in Italian or English: ISO, numeric (day first in Italy and the EU,
    month first in the US), "31 gennaio 2027", "January 31, 2027"."""
    t = fold_spaces(text)
    found: set[date | None] = set()
    found |= {day_or_none(int(y), int(m), int(d)) for y, m, d in _ISO.findall(t)}
    for a, b, y in _NUMERIC.findall(t):
        m, d = (int(a), int(b)) if country == "us" else (int(b), int(a))
        found.add(day_or_none(int(y), m, d))
    found |= {day_or_none(int(y), _MONTHS[m.lower()], int(d)) for d, m, y in _DAY_MONTH.findall(t)}
    found |= {day_or_none(int(y), _MONTHS[m.lower()], int(d)) for m, d, y in _MONTH_DAY.findall(t)}
    return {d for d in found if d is not None}


def parse_date(text: str, country: Country) -> date | None:
    """The one day a short quote names, or None when it names none or several."""
    days = dates_in(text, country)
    return next(iter(days)) if len(days) == 1 else None


def quoted(quote: str, page: str) -> bool:
    """The quote's own words are on the page, as written there (folded)."""
    q = fold_spaces(quote)
    return len(q) >= 4 and q in fold_spaces(page)


NEAR = 200  # the window's own words sit within 200 characters of its date (as ideas' `on_page`)
_STOP = frozenset(
    "bando bandi della delle degli dello sulla nella entro dalle alle anno anni the and for with from".split()
)


def _keywords(title: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{4,}", fold_spaces(title)) if w not in _STOP}


def near(title: str, quote: str, page: str) -> bool:
    """A keyword of the window's title is within `NEAR` characters of one occurrence of its date's words (a
    "Aggiornato il 31 gennaio 2027" elsewhere on the page names no window)."""
    words, q, text = _keywords(title), fold_spaces(quote), fold_spaces(page)
    if not words or len(q) < 4:
        return False
    at = text.find(q)
    while at >= 0:
        around = text[max(0, at - NEAR) : at + len(q) + NEAR]
        if any(re.search(rf"\b{re.escape(w)}", around) for w in words):
            return True
        at = text.find(q, at + 1)
    return False


def window_checked(day: date, quote: str, page: str | None, country: Country, title: str) -> bool:
    """RAIL (the tests table: "a window date not found on its fetched source page is not shown"): the page
    was fetched, the date's words are on it, they name exactly `day`, `day` is one of the page's dates, and
    the window's own words sit next to that date."""
    if page is None:
        return False
    return (
        quoted(quote, page)
        and parse_date(quote, country) == day
        and day in dates_in(page, country)
        and near(title, quote, page)
    )


def visible(passed: bool | None, url: str, country: Country, day: date, checked: date, today: date) -> bool:
    """What a sheet may show of a window: checked by code, on an official page, re-checked this week,
    ahead."""
    return (
        passed is True
        and official(url, country) == country
        and (today - checked).days <= CHECK_DAYS
        and 0 <= (day - today).days <= HORIZON_DAYS
    )


_KIND_WORDS = {
    "grant": "chiude il bando",
    "rule": "entra in vigore",
    "fair": "c'è la fiera",
    "cfp": "chiude la call",
    "tax": "scade",
    "budget": "si chiude il budget",
}


def window_words(kind: WindowKind, title: str, day: date, today: date) -> str:
    """The window in words, its days counted in code ("tra 19 giorni chiude il bando …")."""
    days = (day - today).days
    when = "oggi" if days == 0 else "domani" if days == 1 else f"tra {days} giorni"
    return f"{when} {_KIND_WORDS[kind]}: {title}"


# --- the money desk, as drafts ---

Eligible = Literal["eligible", "not_eligible", "unknown"]
# FACT (wave-3 rules): Pisa informs, never tax or legal advice


def eligible(his_form: str | None, forms: Sequence[str]) -> Eligible:
    """RAIL: eligibility checked in code against their legal form in that country. Unknown
    (and said so) when they never set their form or the page states none."""
    if his_form not in FORMS or not forms:
        return "unknown"
    return "eligible" if his_form in forms else "not_eligible"


def capital_stack(
    grants: Sequence[tuple[str, Eligible]], buyers: float | None, revenue_year_eur: float | None
) -> list[dict[str, str]]:
    """FACT: non-dilutive money first, customers second, equity last. Each step a draft
    line; nothing here applies, sends or pays."""
    stack: list[dict[str, str]] = []
    for window_id, ok in grants:
        if ok != "not_eligible":
            stack.append({"step": "non_dilutive", "window": window_id, "eligible": ok})
    customers = (
        f"clienti: {round(revenue_year_eur):,} € l'anno nei conti in codice".replace(",", ".")
        if revenue_year_eur
        else "clienti: i conti non sono ancora fatti"
    )
    stack.append({"step": "customers", "words": customers, "buyers": str(buyers or "")})
    stack.append({"step": "equity", "words": "capitale di terzi: solo dopo i primi due"})
    stack.append({"step": "note", "words": NOT_ADVICE_IT})  # Pisa informs, never advises
    return stack


# --- the decision journal ---


def scores(
    rows: Iterable[tuple[Party, Domain, float | None, bool | None]],
) -> dict[tuple[Party, Domain], float]:
    """Brier per party and per domain over the decisions resolved (a party that gave no p is not scored)."""
    pairs: dict[tuple[Party, Domain], list[tuple[float, bool]]] = {}
    for party, domain, p, outcome in rows:
        if p is not None and outcome is not None:
            pairs.setdefault((party, domain), []).append((p, outcome))
    return {k: s for k, v in pairs.items() if (s := brier(v)) is not None}


def journal_words(scored: dict[tuple[Party, Domain], float]) -> str:
    """ "Su questioni di mercato tu 0.18, io 0.22"."""
    names = {"market": "questioni di mercato", "timing_cost": "tempi e costi", "window": "finestre"}
    parts = []
    for domain in DOMAINS:
        him, j = scored.get(("owner", domain)), scored.get(("pisa", domain))
        if him is None and j is None:
            continue
        said = [f"tu {him:.2f}" if him is not None else None, f"io {j:.2f}" if j is not None else None]
        parts.append(f"su {names[domain]} " + ", ".join(s for s in said if s))
    return ("; ".join(parts) or "nessuna decisione chiusa").capitalize()


# The words a page uses for each legal form: a form counts as eligible only when the page writes it
# word-bounded, "srls" never read as "srl", on the folded page
FORM_WORDS: dict[str, re.Pattern[str]] = {
    "forfettario": re.compile(r"\bforfettari[oa]?\b|\bregime forfettario\b"),
    "ditta": re.compile(r"\bditt[ae] individual[ei]\b|\bimpres[ae] individual[ei]\b"),
    "srls": re.compile(r"\bs\.?r\.?l\.?s\b\.?|\bresponsabilita limitata semplificata\b"),
    "srl": re.compile(
        r"\bs\.?r\.?l\b(?!\.?\s?s\b)\.?|\bsocieta a responsabilita limitata\b(?! semplificata)"
    ),
    "spa": re.compile(r"\bs\.?p\.?a\b\.?|\bsocieta per azioni\b"),
    "llc": re.compile(r"\bllcs?\b|\blimited liability compan(?:y|ies)\b"),
    "c_corp": re.compile(r"\bc[ -]corp(?:oration)?s?\b"),
    "s_corp": re.compile(r"\bs[ -]corp(?:oration)?s?\b"),
    "sole_prop": re.compile(r"\bsole proprietor(?:s|ships?)?\b"),
}
# a form named after one of these (within the clause before it) is excluded, never eligible
_NEGATION = re.compile(
    r"\b(?:esclus[eiao]|non (?:sono )?ammess[eiao]|non possono|ad eccezione|tranne|eccetto|not eligible|"
    r"ineligible|excluded|except|other than)\b"
)
_CLAUSE = 80


def forms_on_page(forms: Iterable[str], page: str) -> list[str]:
    """The eligible forms a model read that the page itself writes as eligible: each mention word-bounded,
    and never one the page excludes ("escluse le S.r.l.", "LLCs are not eligible")."""
    text = fold_spaces(page)
    kept: set[str] = set()
    for f in forms:
        pattern = FORM_WORDS.get(f)
        if pattern is None:
            continue
        for m in pattern.finditer(text):
            # the clause before it (a negation in a nearby sentence errs towards "unknown": never "eligible")
            before = re.split(r"[;!?\n]", text[max(0, m.start() - _CLAUSE) : m.start()])[-1]
            after = text[m.end() : m.end() + 30]
            if not _NEGATION.search(before) and not re.match(
                r"\W*(?:are|is) (?:not eligible|excluded)", after
            ):
                kept.add(f)
                break
    return sorted(kept)


def his_form(forms: object, country: Country) -> str | None:
    """Their legal form in a country (their setting `desk.legal_forms`, e.g. {"it": "forfettario", "us":
    "llc"}); the EU's calls go by their Italian form. None when they never set it: never guessed."""
    if not isinstance(forms, dict):
        return None
    got = forms.get("it" if country == "eu" else country)
    return got if isinstance(got, str) and got in FORMS else None


def markets(market: str) -> tuple[Country, ...]:
    """The countries an idea's market reads windows from."""
    return {"it": ("it", "eu"), "us": ("us",)}.get(market, ("it", "eu", "us"))  # type: ignore[return-value]
