"""Countries: where a place is (its country and zone), and which tasks only one country allows (the
country-bound tasks: "rinnovare la CIE" only in Italy, "DMV" only in the US). A country is any ISO 3166-1
alpha-2 code; no pair of countries is assumed to be the user's.

A small table, not a gazetteer: the airports, stations and cities Pisa knows (Italy's and the US's first, then
the usual hubs). A place Pisa doesn't know has no country (never a guess). No I/O."""

import re
from dataclasses import dataclass

from pisa.domain.text import ascii_fold

Country = str  # ISO 3166-1 alpha-2, upper case ("IT", "DE")
_CODE = re.compile(r"[A-Z]{2}")


def is_code(code: object) -> bool:
    """An ISO 3166-1 alpha-2 code's shape: two upper-case letters."""
    return isinstance(code, str) and _CODE.fullmatch(code) is not None


@dataclass(frozen=True)
class Where:
    city: str
    country: str  # ISO 3166-1 alpha-2
    tz: str


# IATA airport codes → the city, its country and zone
AIRPORTS: dict[str, Where] = {
    "FCO": Where("Roma", "IT", "Europe/Rome"), "CIA": Where("Roma", "IT", "Europe/Rome"),
    "MXP": Where("Milano", "IT", "Europe/Rome"), "LIN": Where("Milano", "IT", "Europe/Rome"),
    "BGY": Where("Bergamo", "IT", "Europe/Rome"), "NAP": Where("Napoli", "IT", "Europe/Rome"),
    "VCE": Where("Venezia", "IT", "Europe/Rome"), "BLQ": Where("Bologna", "IT", "Europe/Rome"),
    "FLR": Where("Firenze", "IT", "Europe/Rome"), "PSA": Where("Pisa", "IT", "Europe/Rome"),
    "TRN": Where("Torino", "IT", "Europe/Rome"), "CTA": Where("Catania", "IT", "Europe/Rome"),
    "PMO": Where("Palermo", "IT", "Europe/Rome"), "BRI": Where("Bari", "IT", "Europe/Rome"),
    "CAG": Where("Cagliari", "IT", "Europe/Rome"),
    "JFK": Where("New York", "US", "America/New_York"), "EWR": Where("New York", "US", "America/New_York"),
    "LGA": Where("New York", "US", "America/New_York"), "BOS": Where("Boston", "US", "America/New_York"),
    "IAD": Where("Washington", "US", "America/New_York"),
    "DCA": Where("Washington", "US", "America/New_York"),
    "MIA": Where("Miami", "US", "America/New_York"), "ATL": Where("Atlanta", "US", "America/New_York"),
    "ORD": Where("Chicago", "US", "America/Chicago"), "DFW": Where("Dallas", "US", "America/Chicago"),
    "DEN": Where("Denver", "US", "America/Denver"), "LAX": Where("Los Angeles", "US", "America/Los_Angeles"),
    "SFO": Where("San Francisco", "US", "America/Los_Angeles"),
    "SEA": Where("Seattle", "US", "America/Los_Angeles"),
    "LHR": Where("Londra", "GB", "Europe/London"), "CDG": Where("Parigi", "FR", "Europe/Paris"),
    "FRA": Where("Francoforte", "DE", "Europe/Berlin"), "MUC": Where("Monaco", "DE", "Europe/Berlin"),
    "AMS": Where("Amsterdam", "NL", "Europe/Amsterdam"), "MAD": Where("Madrid", "ES", "Europe/Madrid"),
    "ZRH": Where("Zurigo", "CH", "Europe/Zurich"), "IST": Where("Istanbul", "TR", "Europe/Istanbul"),
}  # fmt: skip

# Cities (and their usual spellings) → where; a station's name starts with its city ("Roma Termini")
CITIES: dict[str, Where] = {
    "roma": Where("Roma", "IT", "Europe/Rome"), "rome": Where("Roma", "IT", "Europe/Rome"),
    "milano": Where("Milano", "IT", "Europe/Rome"), "milan": Where("Milano", "IT", "Europe/Rome"),
    "napoli": Where("Napoli", "IT", "Europe/Rome"), "naples": Where("Napoli", "IT", "Europe/Rome"),
    "firenze": Where("Firenze", "IT", "Europe/Rome"), "florence": Where("Firenze", "IT", "Europe/Rome"),
    "venezia": Where("Venezia", "IT", "Europe/Rome"), "venice": Where("Venezia", "IT", "Europe/Rome"),
    "torino": Where("Torino", "IT", "Europe/Rome"), "turin": Where("Torino", "IT", "Europe/Rome"),
    "bologna": Where("Bologna", "IT", "Europe/Rome"), "bari": Where("Bari", "IT", "Europe/Rome"),
    "pisa": Where("Pisa", "IT", "Europe/Rome"), "genova": Where("Genova", "IT", "Europe/Rome"),
    "verona": Where("Verona", "IT", "Europe/Rome"), "salerno": Where("Salerno", "IT", "Europe/Rome"),
    "palermo": Where("Palermo", "IT", "Europe/Rome"), "catania": Where("Catania", "IT", "Europe/Rome"),
    "new york": Where("New York", "US", "America/New_York"),
    "nyc": Where("New York", "US", "America/New_York"),
    "brooklyn": Where("New York", "US", "America/New_York"),
    "manhattan": Where("New York", "US", "America/New_York"),
    "boston": Where("Boston", "US", "America/New_York"), "miami": Where("Miami", "US", "America/New_York"),
    "washington": Where("Washington", "US", "America/New_York"),
    "chicago": Where("Chicago", "US", "America/Chicago"),
    "los angeles": Where("Los Angeles", "US", "America/Los_Angeles"),
    "san francisco": Where("San Francisco", "US", "America/Los_Angeles"),
    "seattle": Where("Seattle", "US", "America/Los_Angeles"),
    "londra": Where("Londra", "GB", "Europe/London"), "london": Where("Londra", "GB", "Europe/London"),
    "parigi": Where("Parigi", "FR", "Europe/Paris"), "paris": Where("Parigi", "FR", "Europe/Paris"),
}  # fmt: skip


def plain(text: str) -> str:
    """Lower case, no accents, single spaces."""
    return re.sub(r"\s+", " ", ascii_fold(text).lower()).strip()


def airport(code: str) -> Where | None:
    return AIRPORTS.get(code.strip().upper())


def city(text: str | None) -> Where | None:
    """The first known city named in `text` (a station "Roma Termini", an address "…, New York, NY")."""
    if not text:
        return None
    words = plain(text)
    best: tuple[int, Where] | None = None
    for name, where in CITIES.items():
        if (m := re.search(rf"\b{re.escape(name)}\b", words)) is not None and (
            best is None or m.start() < best[0]
        ):
            best = (m.start(), where)
    return best[1] if best else None


def country_of_region(region: str | None) -> str | None:
    return region.upper() if region and re.fullmatch(r"[A-Za-z]{2}", region) else None


# --- country-bound tasks ------------------------------------------------------------------------------------

# Things only one country allows IN PERSON: an office, a counter, a document issued at one (never a
# place's bare name, "in Italia", nor a word that is also something else, ASL, nor a number they only send,
# codice fiscale, SSN). Words, as patterns in plain text.
BOUND: dict[Country, tuple[str, ...]] = {
    "IT": (
        r"\bcie\b", r"carta d.?identita", r"anagrafe", r"questura", r"motorizzazione", r"patente italiana",
        r"medico di base", r"tessera sanitaria", r"permesso di soggiorno",
    ),
    "US": (
        r"\bdmv\b", r"real id", r"social security office",
        r"\buscis\b.*\b(?:biometric|interview|appointment)",
        r"\bstate id\b",
    ),
}  # fmt: skip
# A task done from anywhere: by phone, by mail, online, a message, a payment, a booking, learning something
# ("Chiamare la mamma in Italia", "Pagare F24 online", "Prenotare il volo", "Imparare ASL")
ANYWHERE = re.compile(
    r"\bonline\b|\bon line\b|\bsul sito\b|\bvia (?:app|email|mail|pec)\b|"
    r"^(?:chiam|telefon|mand|invi|sped|scriv|rispond|pag|prenot|compr|ordin|impar|studi|cerc|controll|verific|"
    r"call|phone|send|mail|email|text|write|reply|pay|book|buy|order|learn|study|check|look|search)",
)


def bound_to(*texts: str | None, his_words: bool = True) -> Country | None:
    """The one country a task can only be done in, in person, from its own words; None when it names neither
    or both, or when it is done from anywhere (a call, a message, a payment, online). Their own words binding
    it ("solo in Italia") win, only when they are their (`his_words`: never an email's or a
    calendar's text, 1)."""
    if his_words and (said := said_country(*texts)) is not None:
        return said
    words = plain(" ".join(t for t in texts if t))
    title = plain(texts[0] or "") if texts else ""
    if ANYWHERE.search(title):
        return None
    hits: list[Country] = [c for c, patterns in BOUND.items() if any(re.search(p, words) for p in patterns)]
    return hits[0] if len(hits) == 1 else None


# The countries Pisa names, by code: (Italian, English). Any other code is said as its code; a country is
# never guessed from a name Pisa doesn't know.
NAMES: dict[Country, tuple[str, str]] = {
    "IT": ("Italia", "Italy"), "US": ("USA", "the US"), "GB": ("Regno Unito", "the UK"),
    "FR": ("Francia", "France"), "DE": ("Germania", "Germany"), "ES": ("Spagna", "Spain"),
    "PT": ("Portogallo", "Portugal"), "NL": ("Paesi Bassi", "the Netherlands"), "BE": ("Belgio", "Belgium"),
    "CH": ("Svizzera", "Switzerland"), "AT": ("Austria", "Austria"), "IE": ("Irlanda", "Ireland"),
    "GR": ("Grecia", "Greece"), "PL": ("Polonia", "Poland"), "SE": ("Svezia", "Sweden"),
    "NO": ("Norvegia", "Norway"), "DK": ("Danimarca", "Denmark"), "FI": ("Finlandia", "Finland"),
    "CA": ("Canada", "Canada"), "MX": ("Messico", "Mexico"), "BR": ("Brasile", "Brazil"),
    "AR": ("Argentina", "Argentina"), "AU": ("Australia", "Australia"), "JP": ("Giappone", "Japan"),
    "CN": ("Cina", "China"), "IN": ("India", "India"),
}  # fmt: skip
# Their own words binding a task to a country: "solo in Italia", "quando torno negli USA", "when I'm
# back in the US", "da fare a New York". Never a bare place name ("Chiamare la mamma in Italia" stays free).
_COUNTRY_WORDS: dict[str, Country] = {
    **{plain(n): c for c, names in NAMES.items() for n in names},
    "stati uniti": "US", "america": "US", "the states": "US", "the united states": "US", "u.s.": "US",
    "uk": "GB", "inghilterra": "GB", "england": "GB", "united kingdom": "GB", "gran bretagna": "GB",
    "olanda": "NL", "holland": "NL", "netherlands": "NL",
}  # fmt: skip
_WHERE = r"(?:in|negli|nel|a|ad|at|to)\s+([a-z.]+(?:\s+[a-z.]+){0,2})"
_SAID = re.compile(
    r"\b(?:solo|soltanto|solamente|only|just)\s+(?:quando\s+(?:sono|torno|saro|arrivo)\s+|when\s+i(?:'m|\s+am)\s+"
    r"(?:back\s+)?)?"
    + _WHERE
    + r"|\b(?:quando|appena|una\s+volta)\s+(?:sono|torno|tornero|saro|arrivo|arrivero|rientro)\s+"
    + r"(?:di\s+nuovo\s+)?"
    + _WHERE
    + r"|\b(?:when|once)\s+i(?:'m|\s+am|\s+get|\s+land)\s+(?:back\s+)?"
    + _WHERE
    + r"|\bda\s+fare\s+"
    + _WHERE
)


def _named_country(where: str) -> Country | None:
    words = where.strip(" .")
    for n in range(len(words.split()), 0, -1):
        head = " ".join(words.split()[:n])
        if head in _COUNTRY_WORDS:
            return _COUNTRY_WORDS[head]
    found = city(words)
    return found.country if found is not None else None


def said_country(*texts: str | None) -> Country | None:
    """The one country their own words bind a task to ("solo in Italia", "quando torno a New York"); None when
    they name none, or both."""
    words = plain(" ".join(t for t in texts if t))
    hits: set[Country] = {
        c
        for m in _SAID.finditer(words)
        if (c := _named_country(next((g for g in m.groups() if g), ""))) is not None
    }
    return next(iter(hits)) if len(hits) == 1 else None


_ANYWHERE_WORDS = re.compile(
    r"\b(?:ovunque|dappertutto|da qualsiasi parte|da qualunque parte|in qualsiasi paese|anywhere|"
    r"from anywhere|wherever)\b"
)


def named_in_words(words: str, country: str | None) -> bool:
    """RAIL: their own words this turn name the country a task is bound to ("solo in Italia", "a New
    York"), or say it can be done anywhere (None). The pronoun "us" is no country word."""
    text = plain(words)
    if country is None:
        return bool(_ANYWHERE_WORDS.search(text))
    names = [n for n, c in _COUNTRY_WORDS.items() if c == country]
    if any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", text) for n in names):
        return True
    found = city(text)
    return found is not None and found.country == country


def only_here_line(titles: list[str], lang: str) -> str | None:
    """Before a departure: what only this country allows ("questi 3 li puoi fare solo qui: …")."""
    if not titles:
        return None
    named = ", ".join(titles[:5])
    n = len(titles)
    if lang == "it-IT":
        return (f"Questo lo puoi fare solo qui: {named}." if n == 1
                else f"Questi {n} li puoi fare solo qui: {named}.")  # fmt: skip
    return (
        f"This one you can only do here: {named}." if n == 1 else f"These {n} you can only do here: {named}."
    )


def name(country: str, lang: str) -> str:
    """The country as a sentence says it ("Italia", "the US"); its code when Pisa has no name for it."""
    it, en = NAMES.get(country, (country, country))
    return it if lang == "it-IT" else en


def plug(country: str | None, lang: str) -> str | None:
    """The adapter for the sockets where they land."""
    if country == "US":
        return (
            "Adattatore per le prese americane (tipo A/B)"
            if lang == "it-IT"
            else "US plug adapter (type A/B)"
        )
    if country == "IT":
        return (
            "Adattatore per le prese italiane (tipo C/F/L)"
            if lang == "it-IT"
            else "Italian plug adapter (C/F/L)"
        )
    return None
