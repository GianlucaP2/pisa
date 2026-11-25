"""Outside text is read
into typed fields, and the planner sees the fields, never the text as something to follow. Pure rules.

- **`Fields`**: what a reader made of one email, page, invite or document: who it is from, when, the dates
  and amounts as printed, the e-mail addresses, links and phone numbers in it, and a short neutral summary
  written by a model with no tools (the intake gist for an email, the page reader for a page). Every value is
  marked `outside`, with its source said as people say it ("la mail di Marco Rossi").
- **`values`**: the values an act could carry (an address, a link, a phone number, an amount, a date),
  normalised so the same value written two ways is one. `found_in` finds them in a tool's arguments;
  `in_words` says whether the user's own words carry it (then it is theirs, not the email's).
- **`source_line`**: the card's line naming the outside part ("L'indirizzo viene dalla mail di Marco Rossi"):
  a plain name or a host only (`plain_source`), never a sentence a sender wrote in their name.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pisa.domain import locale
from pisa.domain.email_keys import found_keys
from pisa.domain.intentions import speakable
from pisa.domain.lexicon.dates import day_or_none, full_year
from pisa.domain.lexicon.months import EN_MONTHS, EN_MONTHS_SHORT, IT_MONTHS, IT_MONTHS_SHORT, month_numbers
from pisa.domain.tasks import day_named
from pisa.domain.text import ascii_fold, fold_words
from pisa.domain.visible import plain

Kind = Literal["email", "web-page", "invite", "document"]
What = Literal["address", "link", "phone", "amount", "date"]
WHATS: tuple[What, ...] = ("address", "link", "phone", "amount", "date")
SUMMARY_MAX = 600  # characters of the reader's summary the planner sees
VALUES_MAX = 12  # of each kind, per source
SOURCE_MAX = 80
NAME_WORDS = 4  # a sender's name is said only when it is a plain name of at most 4 words

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")
# an address written to dodge a pattern: "x at evil dot example", "x [at] evil [dot] example"
_SEP_DOT = r"(?:\s*\[\s*dot\s*\]\s*|\s*\(\s*dot\s*\)\s*|\s+dot\s+|\s+punto\s+|\.)"
# the local part dodged too ("mario dot rossi at evil dot example")
_OBF_EMAIL = re.compile(
    rf"(?<![\w.+-])([\w+-]{{1,64}}(?:{_SEP_DOT}[\w+-]{{1,64}})*)"
    r"(?:\s*\[\s*at\s*\]\s*|\s*\(\s*at\s*\)\s*|\s+at\s+|\s+chiocciola\s+)"
    rf"([\w-]{{1,63}}(?:{_SEP_DOT}[\w-]{{1,63}})+)",
    re.IGNORECASE,
)
_LINK = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'«»()\[\]]{3,300}", re.IGNORECASE)
# a link with no scheme ("pay-evil.example/now", "pay-evil.example"): a host whose last label is
# letters, never part of an e-mail address
_BARE_LINK = re.compile(
    r"(?<![@\w.-])(?:[a-z0-9-]{1,63}\.)+[a-z]{2,12}(?![@\w-])(?:/[^\s<>\"'«»()\[\]]*)?", re.IGNORECASE
)
_PHONE = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d .\-/]{6,16}\d(?!\w)")
_AMOUNT_DIGITS = re.compile(r"\d+(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?")
# an amount is a number with its currency ("€ 49,90", "1250 EUR", "EUR1250") or in an
# amount's own form ("120,00"); a bare number ("2 biglietti") is never one
_CUR = r"(?:€|\$|£|eur(?:os?)?(?![a-z])|usd(?![a-z])|gbp(?![a-z])|dollar[si]?(?![a-z])|dollaro(?![a-z]))"
_NUM = r"\d+(?:[.,]\d{3})*(?:[.,]\d{1,2})?"
_AMOUNT = re.compile(rf"{_CUR}\s?{_NUM}|(?<![\w.,]){_NUM}\s?{_CUR}", re.IGNORECASE)
_CENTS = re.compile(r"(?<![\d.,/:-])\d{1,3}(?:\.\d{3})*,\d{2}(?![\d.,/:-])")
# "2 mila euro", "1,5 k EUR", "3 thousand dollars"
_TIMES_WORD = re.compile(
    rf"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(mila|mille|k|thousand|milioni|million)\s+{_CUR}", re.IGNORECASE
)
_WORDED = re.compile(r"((?:[a-zà-ù]+[\s-]){0,6}[a-zà-ù]+)\s+" + _CUR, re.IGNORECASE)
# the full and the short names ("Nov 12", "12 nov")
_MONTHS = month_numbers(IT_MONTHS, EN_MONTHS, IT_MONTHS_SHORT, EN_MONTHS_SHORT)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b")
_D_MONTH_Y = re.compile(r"\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b", re.IGNORECASE)
_D_MONTH = re.compile(r"\b(\d{1,2})\s+([a-z]+)\b(?!\s+\d{4})", re.IGNORECASE)
_DM = re.compile(r"(?<![\d/.,:])(\d{1,2})[/.](\d{1,2})(?![\d/.,:]|\.\d)")  # "14/11": "12.11"
# English order ("November 12", "Nov 12, 2026")
_MONTH_D = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.IGNORECASE)
_TIME = re.compile(r"(?<![\d.,/:])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d.,/:])")
_NAME = re.compile(r"\b[A-ZÀ-Ý][a-zà-ÿ]{1,20}(?: [A-ZÀ-Ý][a-zà-ÿ]{1,20}){1,2}\b")
_NOT_NAMES = frozenset(
    "il la lo le gli un una the per ciao grazie gentile caro cara dear hi hello buongiorno buonasera".split()
)
CODE_KINDS = ("order", "booking", "ticket", "tracking", "reference")


def _day(y: int, m: int, d: int) -> str | None:
    found = day_or_none(full_year(y), m, d)
    return found.isoformat() if found is not None else None


def _next(today: date, m: int, d: int) -> str | None:
    """A day named without its year: its next occurrence from today (today included)."""
    for y in (today.year, today.year + 1):
        if (found := _day(y, m, d)) is not None and found >= today.isoformat():
            return found
    return None


def dates_in(text: str, today: date | None = None) -> list[str]:
    """The days a text names, as ISO dates ("12/11/2026", "12 novembre 2026", "2026-11-12T10:00"); with
    `today`, also those named without a year ("14 novembre", "14/11"), at their next occurrence."""
    out = [d for y, m, dd in _ISO.findall(text) if (d := _day(int(y), int(m), int(dd)))]
    out += [d for dd, m, y in _DMY.findall(text) if (d := _day(int(y), int(m), int(dd)))]
    out += [
        d
        for dd, name, y in _D_MONTH_Y.findall(text)
        if name.lower() in _MONTHS and (d := _day(int(y), _MONTHS[name.lower()], int(dd)))
    ]
    out += [
        d
        for name, dd, y in _MONTH_D.findall(text)
        if y and name.lower() in _MONTHS and (d := _day(int(y), _MONTHS[name.lower()], int(dd)))
    ]
    if today is not None:
        out += [
            d
            for name, dd, y in _MONTH_D.findall(text)
            if not y and name.lower() in _MONTHS and (d := _next(today, _MONTHS[name.lower()], int(dd)))
        ]
        out += [
            d
            for dd, name in _D_MONTH.findall(text)
            if name.lower() in _MONTHS and (d := _next(today, _MONTHS[name.lower()], int(dd)))
        ]
        out += [d for dd, m in _DM.findall(text) if (d := _next(today, int(m), int(dd)))]
    return list(dict.fromkeys(out))


_IT_NUMBERS = {
    "zero": 0, "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6,
    "sette": 7, "otto": 8, "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13,
    "quattordici": 14, "quindici": 15, "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19,
    "venti": 20, "vent": 20, "trenta": 30, "trent": 30, "quaranta": 40, "quarant": 40, "cinquanta": 50,
    "cinquant": 50, "sessanta": 60, "sessant": 60, "settanta": 70, "settant": 70, "ottanta": 80,
    "ottant": 80, "novanta": 90, "novant": 90,
}  # fmt: skip
_EN_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip


def _it_number(word: str) -> int | None:
    """An Italian number written as a word ("duecentocinquanta", "milleduecento"), or None."""
    total, current, rest = 0, 0, word
    while rest:
        if rest.startswith(("mila", "mille")):
            total, current = total + max(current, 1) * 1000, 0
            rest = rest[4:] if rest.startswith("mila") else rest[5:]
        elif rest.startswith("cento"):
            current, rest = max(current, 1) * 100, rest[5:]
        elif head := max((w for w in _IT_NUMBERS if rest.startswith(w)), key=len, default=""):
            current, rest = current + _IT_NUMBERS[head], rest[len(head) :]
        else:
            return None
    return total + current


def _en_number(words: list[str]) -> int | None:
    total, current = 0, 0
    for w in words:
        if w == "hundred":
            current = max(current, 1) * 100
        elif w == "thousand":
            total, current = total + max(current, 1) * 1000, 0
        elif w in _EN_NUMBERS:
            current += _EN_NUMBERS[w]
        elif w != "and":
            return None
    return total + current if words else None


_MULTIPLIERS = frozenset({"mila", "mille", "k", "thousand", "milioni", "million"})


def worded_amounts(text: str) -> list[str]:
    """Amounts written in words ("duecento euro", "two hundred fifty dollars"), as "200 EUR"."""
    out = [
        f"{round(float(n.replace(',', '.')) * (1_000_000 if w.lower().startswith('mili') else 1000))} EUR"
        for n, w in _TIMES_WORD.findall(text)
    ]
    for m in _WORDED.finditer(text):
        if re.search(r"\d\s*$", text[: m.start()]) and m.group(1).split()[0].lower() in _MULTIPLIERS:
            continue  # "2 mila euro" is the digits' amount (above), never "mila" alone
        words = [ascii_fold(w).lower() for w in re.split(r"[\s-]+", m.group(1))]
        for i in range(len(words)):  # the longest run of number words just before the currency
            tail = words[i:]
            value = _it_number(tail[0]) if len(tail) == 1 else _en_number(tail)
            if value is None and len(tail) == 1:
                value = _en_number(tail)
            if value:
                out.append(f"{value} EUR")
                break
    return out


def norm(what: What, value: str, today: date | None = None) -> str:
    """One spelling for a value: an address lower-case, a link without its scheme, `www.` and end slash, a
    phone's digits (its last nine: +39 or not), an amount's digits and cents, a date as ISO."""
    v = unicodedata.normalize("NFKC", plain(value)[0]).strip().strip(".,;:")
    if what == "address":
        return v.casefold()
    if what == "link":
        v = re.sub(r"^(?:https?://)?(?:www\.)?", "", v, flags=re.IGNORECASE)
        return v.rstrip("/").casefold()
    if what == "phone":
        return re.sub(r"\D", "", v)[-9:]
    if what == "amount":
        m = _AMOUNT_DIGITS.search(v)
        if not m:
            return ""
        digits = re.sub(r"\D", "", m.group())
        cents = re.search(r"[.,](\d{1,2})$", m.group())
        whole = digits[: -len(cents.group(1))] if cents else digits
        return f"{int(whole or 0)}.{(cents.group(1) if cents else '0').ljust(2, '0')}"
    days = dates_in(v, today)
    return days[0] if days else ""


def plain_source(name: str | None, address: str | None, *, known: bool = False) -> str:
    """Who sent it, as Pisa may say it: the display name when it is a plain name
    (letters only, at most 4 words), else the address's domain; never a sentence a sender put in the name.
    a sender who isn't one of their people (`known`) is named with the domain beside the name
    ("Marco Rossi, evil.example"): a stranger can take a familiar name, never their domain."""
    shown = " ".join((name or "").split())
    said = speakable(shown)
    domain = (address or "").rpartition("@")[2].strip().casefold()
    domain = domain if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain) else ""
    if said and said == shown and len(said.split()) <= NAME_WORDS:
        return said if known or not domain else f"{said}, {domain}"
    return domain or "?"


def _safe_source(source: str) -> str:
    """The source a card or a question may say: a plain name or a host, else "?"."""
    s = " ".join(source.split())
    host = r"[a-z0-9-]+(?:\.[a-z0-9-]+)+"
    if re.fullmatch(rf"[^\W\d_]+(?: [^\W\d_]+){{0,3}}(?:, {host})?", s) or re.fullmatch(host, s):
        return s[:SOURCE_MAX]
    return "?"


@dataclass(frozen=True)
class Fields:
    kind: Kind
    source: str  # who wrote it, as Pisa may say it: "Marco Rossi", "bank.example" (`plain_source`)
    sender: str | None = None
    sent: str | None = None  # when it was sent or published, as Pisa writes it
    title: str | None = None  # the subject, the page's address, the invite's title
    summary: str | None = None  # the tool-less reader's neutral summary; None when there is none yet
    values: dict[What, tuple[str, ...]] = field(default_factory=dict)  # as printed
    hidden: int = 0  # hidden runs left out before the reader read it (a count)
    codes: tuple[str, ...] = ()  # order, booking, ticket, tracking numbers, as printed
    times: tuple[str, ...] = ()  # the clock times it names, as printed
    names: tuple[str, ...] = ()  # other people's or places' names it names (letters only)
    # what the reader found that answers their question: (name, quote, where), each in the text
    candidates: tuple[tuple[str, str, str], ...] = ()


def _addresses(text: str) -> list[str]:
    found = _EMAIL.findall(text)
    for local, domain in _OBF_EMAIL.findall(text):
        host = re.sub(_SEP_DOT, ".", domain, flags=re.IGNORECASE)
        name = re.sub(_SEP_DOT, ".", local, flags=re.IGNORECASE)
        if re.fullmatch(r"[\w-]+(?:\.[\w-]+)*\.[a-z]{2,10}", host, re.IGNORECASE):
            found.append(f"{name}@{host}")
    return found


_ABBREVIATIONS = frozenset(
    {"sig", "sigg", "dott", "prof", "avv", "ing", "gent", "egr", "spett", "geom", "rag"}
)


def _bare_links(text: str) -> list[str]:
    """Hosts and links written without a scheme, never an address's domain, a dotted number or a title
    ("Sig.ra", "Dott.ssa")."""
    return [
        m.rstrip(".,;:")
        for m in _BARE_LINK.findall(text)
        if m.split(".", 1)[0].casefold() not in _ABBREVIATIONS
    ]


def _amounts(text: str) -> list[str]:
    return [m.group() for m in _AMOUNT.finditer(text)] + _CENTS.findall(text) + worded_amounts(text)


def _phones(text: str) -> list[str]:
    """Phone numbers, never a date ("12/11/2026") nor an amount."""
    return [
        p.strip()
        for p in _PHONE.findall(text)
        if len(re.sub(r"\D", "", p)) >= 8 and not dates_in(p) and not _DMY.search(p) and "/" not in p.strip()
    ]


def extract(text: str, today: date | None = None) -> dict[What, tuple[str, ...]]:
    """The values in a text, as printed, each once: addresses, links, phone numbers, amounts, dates."""
    clean = unicodedata.normalize("NFKC", plain(text)[0])
    keys = found_keys(clean)
    found: dict[What, list[str]] = {
        "address": _addresses(clean),
        "link": [m.rstrip(".,;:") for m in _LINK.findall(clean) + _bare_links(clean)],
        "phone": _phones(clean),
        "amount": [k.value for k in keys if k.kind == "amount"] + _amounts(clean),
        "date": [k.value for k in keys if k.kind == "date"] + dates_in(clean, today),
    }
    out: dict[What, tuple[str, ...]] = {}
    for what, values in found.items():
        seen: dict[str, str] = {}
        for v in values:
            if (n := norm(what, v, today)) and n not in seen:
                seen[n] = v
        if seen:
            out[what] = tuple(list(seen.values())[:VALUES_MAX])
    return out


def shown(text: str, keys: Iterable[Mapping[str, Any]] = ()) -> tuple[tuple[str, ...], ...]:
    """What they ask about that is no act's value: the codes (the rules' and the intake's keys,
    verbatim), the clock times and the names, as printed."""
    clean = unicodedata.normalize("NFKC", plain(text)[0])
    codes = [k.value for k in found_keys(clean) if k.kind in CODE_KINDS]
    codes += [str(k.get("value")) for k in keys if k.get("kind") in CODE_KINDS and k.get("value")]
    times = [f"{int(h)}:{m}" for h, m in _TIME.findall(clean)]
    names = [n for n in _NAME.findall(clean) if n.split()[0].casefold() not in _NOT_NAMES]
    return tuple(tuple(dict.fromkeys(v for v in vs if v))[:VALUES_MAX] for vs in (codes, times, names))


def read(
    kind: Kind,
    text: str,
    *,
    source: str,
    sender: str | None = None,
    sent: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    hidden: int = 0,
    today: date | None = None,
    keys: Iterable[Mapping[str, Any]] = (),
    candidates: Iterable[tuple[str, str, str]] = (),
) -> Fields:
    """The fields of one outside text: the values by rule, from the text and from the reader's summary
    (a value the reader wrote is a value too), the summary as the reader wrote it, and the
    reader's candidates that the text itself carries (`grounded`)."""
    said = " ".join(plain(summary or "")[0].split())[:SUMMARY_MAX] or None
    codes, times, names = shown(text, keys)
    return Fields(
        kind=kind, source=" ".join(source.split())[:SOURCE_MAX], sender=sender, sent=sent, title=title,
        summary=said, values=extract(f"{text}\n{said or ''}", today), hidden=hidden, codes=codes, times=times,
        names=names, candidates=grounded(candidates, text),
    )  # fmt: skip


CANDIDATES_MAX = 15
CANDIDATE_NAME_MAX = 80
CANDIDATE_QUOTE_MAX = 200
CANDIDATE_WHERE_MAX = 80


def _key(text: str) -> str:
    """Lowercase letters and digits, single spaces, no accents: how a name or quote is found in a page."""
    return fold_words(plain(text)[0])


def grounded(candidates: Iterable[tuple[str, str, str]], text: str) -> tuple[tuple[str, str, str], ...]:
    """A candidate only when the text itself names it and
    holds its quote (case, accents and punctuation aside); its heading only when the text holds it too. The
    reader can't add a place, a quote or an award the page doesn't carry. At most CANDIDATES_MAX, one a
    name."""
    page = f" {_key(text)} "
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for name, quote, where in candidates:
        n, q, w = (" ".join(plain(x or "")[0].split()) for x in (name, quote, where))
        nk, qk = _key(n), _key(q)
        if not nk or nk in seen or f" {nk} " not in page or (qk and f" {qk} " not in page):
            continue
        seen.add(nk)
        wk = _key(w)
        out.append((n[:CANDIDATE_NAME_MAX], q[:CANDIDATE_QUOTE_MAX] if qk else "",
                    w[:CANDIDATE_WHERE_MAX] if wk and f" {wk} " in page else ""))  # fmt: skip
        if len(out) == CANDIDATES_MAX:
            break
    return tuple(out)


_LABELS: dict[What, str] = {
    "address": "addresses", "link": "links", "phone": "phones", "amount": "amounts", "date": "dates",
}  # fmt: skip
NO_SUMMARY = "(not summarised yet: only the fields above)"


def render(f: Fields) -> str:
    """The fields as the planner sees them: one per line, the outside mark on the block's own rule."""
    lines = [f"from: {f.sender}" if f.sender else f"source: {f.source}"]
    if f.sent:
        lines.append(f"sent: {f.sent}")
    if f.title:
        lines.append(f"{'subject' if f.kind == 'email' else 'title'}: {f.title}")
    for what, label in _LABELS.items():
        if f.values.get(what):
            lines.append(f"{label} (as printed): " + "; ".join(f.values[what]))
    for label, found in (("codes", f.codes), ("times", f.times), ("names", f.names)):
        if found:
            lines.append(f"{label} (as printed): " + "; ".join(found))
    lines.append(f"summary (Pisa's reader, no tools): {f.summary}" if f.summary else f"summary: {NO_SUMMARY}")
    if f.candidates:  # what the page names that answers their question, each in its own words
        lines.append(f"candidates the page names for the user's question ({len(f.candidates)}):")
        lines += [
            f"- {name}" + (f" [{where}]" if where else "") + (f': "{quote}"' if quote else "")
            for name, quote, where in f.candidates
        ]
    return "\n".join(lines)


def gist_summary(gist: Mapping[str, Any] | None) -> str | None:
    """The intake gist's fixed fields (a model with no tools) as one neutral summary."""
    if not gist:
        return None
    parts = [str(gist.get("what") or "").strip()]
    for name, label in (("when", "when"), ("where", "where"), ("asked", "asks the user"),
                        ("deadline", "by")):  # fmt: skip
        if value := str(gist.get(name) or "").strip():
            parts.append(f"{label}: {value}")
    said = "; ".join(p for p in parts if p)
    return said or None


# --- taint follows the value -------------------------------------------------------------------------------


def values(f: Fields, today: date | None = None) -> list[tuple[What, str]]:
    """(what, normalised value) of every value an act could carry."""
    return [(what, n) for what, vs in f.values.items() for v in vs if (n := norm(what, v, today))]


_MONEY_KEY = re.compile(r"amount|price|total|cost|eur|cents|importo|prezzo|sum", re.IGNORECASE)


def _strings(args: Any, key: str = "") -> Iterable[str]:
    """Every string of a tool's arguments; a number under a money key as an amount."""
    if isinstance(args, bool):
        return
    if isinstance(args, str):
        yield args
    elif isinstance(args, int | float):
        yield f"{args} EUR" if _MONEY_KEY.search(key) else str(args)
    elif isinstance(args, Mapping):
        for k, v in args.items():
            if not str(k).startswith("_"):
                yield from _strings(v, str(k))
    elif isinstance(args, list | tuple):
        for v in args:
            yield from _strings(v, key)


def carried(text: str, today: date | None = None) -> set[tuple[What, str]]:
    """(what, normalised value) of every value a text carries: what `found_in` matches against."""
    clean = unicodedata.normalize("NFKC", plain(text)[0])
    seen: list[tuple[What, list[str]]] = [
        ("address", _addresses(clean)),
        ("link", _LINK.findall(clean) + _bare_links(clean)),
        ("phone", _phones(clean)),
        ("amount", _amounts(clean)),
    ]
    out: set[tuple[What, str]] = {(what, n) for what, vs in seen for v in vs if (n := norm(what, v, today))}
    dates: set[tuple[What, str]] = {("date", d) for d in dates_in(clean, today)}
    return out | dates


def found_in(
    args: Any, known: Mapping[tuple[What, str], str], today: date | None = None
) -> list[tuple[What, str, str]]:
    """(what, normalised value, source) of each outside value `known` holds that a tool's arguments carry."""
    text = "\n".join(_strings(args))
    if not text.strip() or not known:
        return []
    return [(w, v, known[(w, v)]) for w, v in sorted(carried(text, today)) if (w, v) in known]


def in_words(what: What, value: str, words: Iterable[str], today: date | None = None) -> bool:
    """The user's own words carry this value: then the act's value is theirs, whatever an email also says. A
    date is their only when their words name that same day ("domani" is their only when tomorrow is
    the email's day)."""
    for said in words:
        if not said:
            continue
        if what == "date":
            named = day_named(said, today) if today is not None else None
            if (named is not None and named.isoformat() == value) or value in dates_in(said, today):
                return True
            continue
        if (what, value) in carried(said, today):
            return True
        if what == "amount" and value in {
            norm("amount", n) for n in re.findall(r"\d+(?:[.,]\d{1,2})?", said)
        }:
            return True  # their own "49,90" with no currency is their amount
    return False


_PART = {
    "it-IT": {"address": "L'indirizzo", "link": "Il link", "phone": "Il numero", "amount": "L'importo",
              "date": "La data"},
    "en-US": {"address": "The address", "link": "The link", "phone": "The number", "amount": "The amount",
              "date": "The date"},
}  # fmt: skip
_OF = {
    "it-IT": {"email": "dalla mail di", "web-page": "dalla pagina", "invite": "dall'invito di",
              "document": "dal documento"},
    "en-US": {"email": "from the email of", "web-page": "from the page", "invite": "from the invite of",
              "document": "from the document"},
}  # fmt: skip


def source_label(kind: Kind, source: str, lang: str) -> str:
    """Where a value came from, as the card says it: "dalla mail di Marco Rossi" (a plain name or a host,
    never a sentence)."""
    lang = lang if lang in _OF else locale.tag_of(lang)
    return f"{_OF[lang][kind]} {_safe_source(source)}"


def cut(text: str, limit: int) -> str:
    """A line as the planner sees it: hidden characters out, one line, at most `limit` characters."""
    line = " ".join(plain(text)[0].split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


_MAIN_ARGS = (
    "notes",
    "text",
    "what",
    "note",
    "title",
    "statement",
    "to",
    "phone",
    "person",
    "task_ref",
    "place",
)
MAIN_MAX = 80


def value_question(tool: str, args: Mapping[str, Any], found: list[tuple[What, str, str]], lang: str) -> str:
    """What their yes approves, said in the question: the act, its main words (≤80
    characters), each outside value and where it comes from ("fare «set task notes»: «pratiche@studio…»
    (l'indirizzo pratiche@studio.example viene dalla mail di Studio)")."""
    doing = tool.replace("_", " ")
    main = next((str(args[k]) for k in _MAIN_ARGS if isinstance(args.get(k), str) and args[k].strip()), "")
    main = cut(main, MAIN_MAX)
    part = _PART.get(lang, _PART[locale.tag_of(lang)])
    verb = "viene" if lang == "it-IT" else "comes"
    lines = "; ".join(f"{part[what].lower()} {value} {verb} {source}" for what, value, source in found)
    head = f"fare «{doing}»" if lang == "it-IT" else f"do «{doing}»"
    return f"{head}{f': «{main}»' if main else ''} ({lines})"


def source_line(what: What, label: str, lang: str) -> str:
    """The card's line: "L'indirizzo viene dalla mail di Marco Rossi"."""
    lang = lang if lang in _PART else locale.tag_of(lang)
    verb = "viene" if lang == "it-IT" else "comes"
    return f"{_PART[lang][what]} {verb} {label}"
