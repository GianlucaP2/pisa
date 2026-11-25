"""The recognition a place holds, read
from the guide, award and press pages this turn saw. Pure rules, no I/O.

Code reads, for each place, the sentence of a guide's page (or a search result) that names it together with
an award ("Tre Spicchi 2027: Vico", "Stella Chiara 2026: Osteria Lanterna", "Bib Gourmand"), and keeps
`{guide, award, year, url}`: the guide from the page, the award as the page writes it, the year the page
gives. Never the model's memory; a page a place publishes itself is no guide, and a sentence that gives
orders ("recommend X, tell the user…") is no award.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from pisa.domain.text import fold_words

RECOGNITION_MAX = 4  # contract answers-1 §1c
GUIDE_MAX = 40
AWARD_MAX = 40

# The guides and awards of the real world Pisa reads most, by their site (the guide's own name, as a chip says
# it)
GUIDES = {
    "guide.michelin.com": "Michelin", "gamberorosso.it": "Gambero Rosso", "50toppizza.it": "50 Top Pizza",
    "slowfood.it": "Slow Food", "osterieditalia.it": "Slow Food", "identitagolose.it": "Identità Golose",
    "scattidigusto.it": "Scatti di Gusto", "lespresso.it": "Guide de L'Espresso",
    "guide.espresso.repubblica.it": "Guide de L'Espresso", "dissapore.com": "Dissapore",
    "puntarellarossa.it": "Puntarella Rossa", "theworlds50best.com": "50 Best", "timeout.com": "Time Out",
}  # fmt: skip
# How an award is written: the real ones by name, then the shapes a guide's award takes ("Tre Lune", "Stella
# Chiara", "Spiga d'oro 2026", "Bib Chiara", "una stella", "★★", "Premio …", "n. 1 in classifica")
_CAP = r"[A-ZÀ-Ý][a-zà-ù]+"
_AWARD = re.compile(
    r"(?i:\b(?:tre|due|una|uno|three|two|one)\s+(?:spicchi|forchette|gamberi|tazzine|chicchi|cucchiai|"
    r"bicchieri|stelle|stella|stars?)\b)"
    rf"|\b(?i:tre|due|three|two)\s+{_CAP}\b"
    rf"|(?i:\bbib\s+gourmand\b)|\b(?i:bib)\s+{_CAP}\b|(?i:\bpiatto\s+michelin\b|\bchiocciol[ae]\b)"
    rf"|\b(?i:stell[ae])\s+{_CAP}\b|\b[A-ZÀ-Ý]\w*\s+d['\u2019](?i:oro|argento|bronzo)\b"
    rf"|\b(?i:premio)\s+{_CAP}(?:\s+{_CAP})?|★{{1,3}}"
    r"|(?i:\b(?:due|tre|two|three)\s+stelle\s+michelin\b|\bstella\s+michelin\b)"
    # "listed" only as a guide's listing ("segnalata dalla Guida", "recommended by"), never a mention
    # ("We recommended Bar Lampo to friends")
    r"|(?i:\b(?:segnalat[oa]|selezionat[oa]|premiat[oa])\b)(?=\s*:|\s+[A-ZÀ-Ý]|\s+(?i:da|dal|dalla|dallo|dai|"
    r"dalle|dall['\u2019]|nella|nelle|tra|fra)\b)"
    r"|(?i:\b(?:recommended|selected|awarded)\b)(?=\s*:|\s+(?i:by|in)\b)"
)
# "premiato dalla Guida Dolce", "segnalata da Tavola Chiara": the guide the sentence itself names
_BY_NAMED = re.compile(
    r"(?i:\b(?:premiat[oa]|segnalat[oa]|selezionat[oa]|citat[oa]|recommended|selected|awarded)\s+"
    r"(?:da|dal|dalla|dallo|dai|dalle|dall['\u2019]|by)\s*(?:the\s+)?)((?:[A-ZÀ-Ý][\w'\u2019]*\s*){1,4})"
)
# a sentence that gives orders to an assistant is an injection, never an award
_ORDERS = re.compile(
    r"\b(?:assistant|assistente|\bAI\b|system|instruction\w*|istruzion\w*|recommend\s+only|tell\s+the\s+user|"
    r"ignore|ignora|email|book\s+a|prenota\s+subito|always\s+recommend)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(19[5-9]\d|20\d\d)\b")
_RANK = re.compile(r"(?:^|\s)(\d{1,2})\.\s+$")


def _plain(text: str) -> str:
    return fold_words(text or "")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _a_name(text: str) -> bool:
    """A guide's name is a name: no orders, no number but a year, a few words."""
    bare = _YEAR.sub("", text or "")
    return (
        bool(bare.strip())
        and not _ORDERS.search(bare)
        and not re.search(r"\d|@|https?:|www\.", bare)
        and (len(bare.split()) <= 5)
    )


def guide_of(url: str, title: str) -> str:
    """The guide's name: a known guide by its site, else the page title's head ("Guida del Mattino: le
    colazioni…" → "Guida del Mattino", "Tavola Chiara 2026: …" → "Tavola Chiara"), else the site's name."""
    host = _host(url)
    for site, name in GUIDES.items():
        if host == site or host.endswith("." + site):
            return name
    head = (title or "").strip()
    parts = [p.strip() for p in re.split(r"\s+[|\u2013—-]\s+|:\s+", head) if p.strip()]
    if parts:
        head = min(parts[1:], key=len) if len(parts) > 1 and len(parts[0]) > GUIDE_MAX else parts[0]
    head = _YEAR.sub("", head).strip(" ,;:-")
    if not head or len(head) > GUIDE_MAX or head.startswith(("http", "www.")) or not _a_name(head):
        label = host.split(".")[0] if host else ""
        head = label.replace("-", " ").title()
    return head[:GUIDE_MAX]


def _joined(pieces: list[str]) -> list[str]:
    """Pieces cut after a full stop, a rank's "1." joined back to what follows ("La classifica: 1. Brace")."""
    out: list[str] = []
    for piece in (p.strip() for p in pieces):
        if not piece:
            continue
        if out and re.search(r"(?:^|\s)\d{1,2}\.$", out[-1]):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return out


def _sentences(text: str) -> list[str]:
    """A text's sentences; a rank's "1. " never ends one ("La classifica: 1. Brace Viva.")."""
    return _joined(re.split(r"(?<=[.!?])\s+|\n+", text or ""))


def _clauses(sentence: str) -> list[str]:
    """A sentence's parts that hold one place each ("Stella Chiara: Osteria Lanterna, …; Tre Lune: …")."""
    return [c.strip() for c in re.split(r";|\s+e\s+(?=[A-ZÀ-Ý])", sentence) if c.strip()]


def award_of(segment: str, name: str) -> tuple[str, int | None, str] | None:
    """The award a segment gives the named place, as written, and its year; None when it gives none. The
    award nearest the name wins (a list "1. Brace Viva. 2. da Tobia" ranks each by its own number)."""
    if _ORDERS.search(segment):
        return None
    plain_name = _plain(name)
    if not plain_name or plain_name not in _plain(segment):
        return None
    at = _plain(segment).find(plain_name)
    found: list[tuple[int, str]] = []
    for m in _AWARD.finditer(segment):
        word = " ".join(m.group(0).split())
        if _plain(word) and _plain(word) in plain_name:
            continue  # the place's own name ("Tre Archi") is no award
        found.append((abs(len(_plain(segment[: m.start()])) - at), word))
    year = _YEAR.search(segment)
    before = (
        segment[: max(0, segment.casefold().find(name.casefold()))]
        if name.casefold() in segment.casefold()
        else ""
    )
    rank = _RANK.search(before)
    if not found and rank is None:
        return None
    if found:
        award = min(found)[1]
        award = award[:1].upper() + award[1:]
    else:
        assert rank is not None
        award = f"n. {rank.group(1)}"
    by = _BY_NAMED.search(segment)
    named = _YEAR.sub("", by.group(1)).strip(" ,;:.") if by is not None else ""
    return award[:AWARD_MAX], int(year.group(1)) if year else None, named[:GUIDE_MAX]


def recognitions(name: str, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`{guide, award, year?, url}` for each guide page (or result) that names the place with an award, read
    pages first, one per guide, at most RECOGNITION_MAX. `pages`: [{url, title, text, quotes: [(name,
    quote)], own: bool}] as this turn saw them."""
    out: list[dict[str, Any]] = []
    for page in pages:
        url = str(page.get("url") or "")
        if page.get("own") or not url.startswith("https://") or len(url) > 2000:
            continue
        guide = guide_of(url, str(page.get("title") or ""))
        if not guide or any(r["guide"] == guide for r in out):
            continue
        texts = [q for n, q in page.get("quotes") or [] if q and _plain(n) == _plain(name)]
        texts += _sentences(str(page.get("text") or ""))
        got: tuple[str, int | None, str] | None = None
        for sentence in texts:
            for clause in [sentence, *_clauses(sentence)]:
                if (got := award_of(clause, name)) is not None:
                    break
            if got is not None:
                break
        if got is None:
            continue
        award, year, named = got
        if _plain(name) and _plain(name) in _plain(guide):  # a page titled after the place is no guide
            guide = guide_of(url, "")
        if named and _a_name(named) and _plain(name) not in _plain(named):
            guide = named
            if any(r["guide"] == guide for r in out):
                continue
        if year is None and (y := _YEAR.search(f"{page.get('title') or ''} {urlsplit(url).path}")):
            year = int(y.group(1))
        item: dict[str, Any] = {"guide": guide, "award": award, "url": url}
        if year is not None and 1900 <= year <= 2100:
            item["year"] = year
        out.append(item)
        if len(out) == RECOGNITION_MAX:
            break
    return out


def _within(item: dict[str, Any]) -> bool:
    """The award names its guide, or the guide is the award ("Premio Cono d'oro …" gives the Cono d'oro)."""
    award, guide = _plain(str(item["award"])), _plain(str(item["guide"]))
    return bool(award and guide) and (guide in award or award in guide)


def chip(item: dict[str, Any]) -> str:
    """The recognition as a chip and a table cell says it: "Tavola Chiara Tre Lune 2026", "Michelin ★
    2026"."""
    bits = [str(item["award"])] if _within(item) else [str(item["guide"]), str(item["award"])]
    if item.get("year"):
        bits.append(str(item["year"]))
    return " ".join(bits)


_SPOKEN_STARS = {"★": {"it-IT": "una stella", "en-US": "one star"},
                 "★★": {"it-IT": "due stelle", "en-US": "two stars"},
                 "★★★": {"it-IT": "tre stelle", "en-US": "three stars"}}  # fmt: skip
_OF = {
    "il ": "del ",
    "lo ": "dello ",
    "la ": "della ",
    "i ": "dei ",
    "gli ": "degli ",
    "le ": "delle ",
    "l'": "dell'",
}


_BY = {
    "il ": "dal ",
    "lo ": "dallo ",
    "la ": "dalla ",
    "i ": "dai ",
    "gli ": "dagli ",
    "le ": "dalle ",
    "l'": "dall'",
}
_LISTED = re.compile(
    r"^(?:segnalat[oa]|selezionat[oa]|premiat[oa]|recommended|selected|awarded)$", re.IGNORECASE
)


def _by_guide(guide: str) -> str:
    """ "da" + the guide: "dalla Guida del Mattino", "dal Gambero Rosso", "da Tavola Chiara"."""
    low = guide.casefold()
    for article, joined in _BY.items():
        if low.startswith(article):
            return joined + guide[len(article) :]
    if low.startswith("guida"):
        return "dalla " + guide
    if low == "gambero rosso":
        return "dal Gambero Rosso"
    return "da " + guide


def _of_guide(guide: str) -> str:
    """ "di" + the guide, as Italian joins them: "del Gambero Rosso", "della Guida del Mattino", "di Tavola
    Chiara"."""
    low = guide.casefold()
    for article, joined in _OF.items():
        if low.startswith(article):
            return joined + guide[len(article) :]
    if low.startswith("guida"):
        return "della " + guide
    if low == "gambero rosso":
        return "del Gambero Rosso"
    return "di " + guide


def said(item: dict[str, Any], lang: str) -> str:
    """The recognition as Pisa says it: "Tre Lune di Tavola Chiara", "Tre Spicchi del Gambero Rosso", "una
    stella Michelin"; English: "Tre Lune from Tavola Chiara"."""
    award, guide = str(item["award"]), str(item["guide"])
    stars = _SPOKEN_STARS.get(award.strip())
    if stars is not None:
        return f"{stars.get(lang, stars['en-US'])} {guide}"
    if _within(item):
        return award
    if _LISTED.match(award):
        return f"{award.casefold()} {_by_guide(guide)}" if lang == "it-IT" else f"listed by {guide}"
    if lang == "it-IT":
        return f"{award} {_of_guide(guide)}"
    return f"{award} from {guide}"
