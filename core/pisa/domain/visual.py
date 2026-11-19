"""The rules Pisa's drawings stand on. Pure.

- Every number traces: each number a sheet shows (a chart's y and numeric x, a stat's value, a table's
  numeric cell) must be one this turn's tool results carry, read the Italian or the English way ("1.234,5" is
  1234.5), with `%` and units around it, at the precision shown (12,3 °C shows 12.34). Derived numbers only
  through a declared `derive` over traced ones, computed here, never written by the model.
- The basis: a diagram from Pisa's own knowledge says so ("Da quello che so").
- A tap asks: the turn's words are the box's "Sul disegno: …" around the label from the server's copy
  of the sheet, the turn marked `origin: drawing` by its caller (never read from the words); they
  ground nothing (no write, no save, no plan change).
- Nothing approval-like: a drawing's label that reads like Pisa's approval, Face ID or payment is left out.
- The fallback: a phone that doesn't draw a block type gets the same answer as a table or a list.
"""

import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from pisa.domain import drawing as drawing_rules
from pisa.domain.sheet import DRAWING_TYPES, TABLE_COLUMNS, TABLE_ROWS

Basis = Literal["tools", "knowledge", "mixed"]
DERIVES: tuple[str, ...] = ("sum", "mean", "delta", "share")
LIST_ITEMS = 8  # a list block's items
MAX_VALUE = Decimal("1e15")  # a shown number above this traces nothing
MAX_PLACES = 12

# A number in a tool's text: digits with Italian or English group and decimal marks; a minus only where a
# number starts (never a date's "-11").
_NUMBER = re.compile(r"(?<![\w.,])[-\u2212]?\d(?:[\d.,'\u00a0\u202f]*\d)?(?:\s?%)?")
_ONE_NUMBER = re.compile(
    r"^\s*[-\u2212+]?\d(?:[\d.,'\u00a0\u202f]*\d)?\s*(?:%|°C|°F|°|€|\$|£|[A-Za-z/°²³]{1,6})?\s*$"
)
_GROUP = re.compile(r"['\u00a0\u202f]")
# A clock time or a date in a tool's text is no value a chart may show: "09:00" never traces a 9, nor
# "2026-11-10" an 11 (a year written alone still counts)
_MOMENTS = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\b\d{1,2}:\d{2}(?::\d{2})?\b")


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def readings(token: str) -> list[Decimal]:
    """Every value a written number may mean: "1.234" is 1234 (Italian) or 1.234 (English), "1,5" is 1.5,
    "1.234,5" is 1234.5, "1,234.5" is 1234.5; "45%" is 45 (and 0.45). A run of dotted parts that isn't
    grouping ("10.11.2026") gives its parts."""
    raw = token.strip()
    percent = raw.endswith("%")
    raw = _GROUP.sub("", raw.rstrip("% ").replace("\u2212", "-"))
    sign = "-" if raw.startswith("-") else ""
    body = raw.lstrip("-+")
    out: list[Decimal] = []
    dots, commas = body.count("."), body.count(",")
    if dots and commas:
        decimal_mark = "." if body.rfind(".") > body.rfind(",") else ","
        group_mark = "," if decimal_mark == "." else "."
        whole, _, frac = body.rpartition(decimal_mark)
        out.append(_decimal(sign + whole.replace(group_mark, "") + "." + frac) or Decimal(0))
    elif dots + commas == 0:
        out.append(Decimal(sign + body))
    else:
        mark = "." if dots else ","
        parts = body.split(mark)
        if len(parts) == 2:
            out.append(Decimal(f"{sign}{parts[0]}.{parts[1]}"))  # a decimal mark
            if len(parts[1]) == 3:
                out.append(Decimal(sign + parts[0] + parts[1]))  # … or a thousands mark
        elif all(len(p) == 3 for p in parts[1:]) and 1 <= len(parts[0]) <= 3:
            out.append(Decimal(sign + "".join(parts)))  # 1.234.567
        else:
            out += [Decimal(p) for p in parts if p]  # 10.11.2026: its parts
    if percent:
        out += [v / 100 for v in out]
    return out


class Corpus:
    """The numbers this turn's tool results carry (sorted, for a match at a shown precision)."""

    def __init__(self, texts: Iterable[str]) -> None:
        found: set[Decimal] = set()
        for text in texts:
            for token in _NUMBER.findall(_MOMENTS.sub(" ", text or "")):
                found.update(readings(token))
        self.values = sorted(found)
        self.read = False  # set by the caller: whether the turn read anything at all

    def has(self, value: Decimal, places: int) -> bool:
        """A value shown with `places` decimals matches a carried number that rounds to it."""
        if not value.is_finite() or places > MAX_PLACES or abs(value) >= MAX_VALUE:
            return False  # no chart shows such a number (and quantize would raise)
        half = Decimal(5) * Decimal(10) ** (-(places + 1))
        lo, hi = bisect_left(self.values, value - half), bisect_right(self.values, value + half)
        step = Decimal(10) ** -places
        with localcontext() as ctx:
            ctx.prec = 80
            shown = value.quantize(step, ROUND_HALF_UP)
        with localcontext() as ctx:
            ctx.prec = 80
            return any(v.quantize(step, ROUND_HALF_UP) == shown for v in self.values[lo:hi])


def _places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def traces(value: Any, corpus: Corpus) -> bool:
    """A number (int, float, or a number written with its unit) that this turn's results carry."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return False
        shown = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return corpus.has(shown, _places(shown))
    tokens = _NUMBER.findall(str(value))
    if not tokens:
        return True  # no number in it: nothing to trace
    for token in tokens:
        options = readings(token)
        if not any(corpus.has(v, _places(v)) for v in options):
            return False
    return True


_QUANTITY = re.compile(
    r"(?<![\w.,])[-\u2212]?\d(?:[\d.,'\u00a0\u202f]*\d)?\s?(?:%|€|\$|£|°|euro\b|eur\b|km\b|kg\b|ms\b|GB\b|MB\b|TB\b)"
    r"|(?:€|\$|£)\s?\d(?:[\d.,]*\d)?|(?<![\w.,])\d{1,3}(?:[.,'\u00a0]\d{3})+(?![\d])|(?<![\w.,])\d{5,}",
    re.IGNORECASE,
)


def quantities_trace(text: str, corpus: Corpus) -> bool:
    """A drawing from what Pisa knows carries no numbers: a quantity in a label (with %, a
    currency or a unit, grouped, or five digits and more) is shown only when this turn's results carry it; a
    name's digit (HTTP/2, Layer 7) or a year is not a quantity."""
    return all(traces(q, corpus) for q in _QUANTITY.findall(_MOMENTS.sub(" ", text or "")))


def numeric_text(value: Any) -> bool:
    """A table cell that is one number (with its unit): checked like a number."""
    return isinstance(value, str) and bool(_ONE_NUMBER.match(value)) and any(c.isdigit() for c in value)


def derive(kind: str, values: list[float]) -> float | None:
    """CORE computes it (the model never writes the result): sum, mean, delta (last minus first), share (the
    first as a percentage of the total of all)."""
    if not values:
        return None
    if kind == "sum":
        return sum(values)
    if kind == "mean":
        return sum(values) / len(values)
    if kind == "delta":
        return values[-1] - values[0] if len(values) >= 2 else None
    if kind == "share":
        total = sum(values)
        return values[0] / total * 100 if total else None
    return None


def derive_series(kind: str, ys: list[float]) -> list[float | None]:
    """A series' own derive, point by point: sum is the running total, mean the running mean, delta the
    change from the previous point (none for the first), share each point's percentage of the series'
    total."""
    out: list[float | None] = []
    total = sum(ys)
    for i, y in enumerate(ys):
        if kind == "sum":
            out.append(sum(ys[: i + 1]))
        elif kind == "mean":
            out.append(sum(ys[: i + 1]) / (i + 1))
        elif kind == "delta":
            out.append(y - ys[i - 1] if i else None)
        elif kind == "share":
            out.append(y / total * 100 if total else None)
    return out


def rounded(value: float) -> float | int:
    """A derived number as the phone shows it: two decimals at most, an integer when it is one."""
    r = round(value, 2)
    return int(r) if r == int(r) else r


def written(value: float, lang: str) -> str:
    r = rounded(value)
    text = f"{r}"
    return text.replace(".", ",") if lang == "it-IT" else text


# Pisa draws by itself when the answer is a structure, a flow or a series of numbers; these words
# ask for one (the drawing skill joins, show_sheet is offered at once). "solo testo" / "disegna meno" turn it
# down.
_ASKS = re.compile(
    r"\b(?:disegn\w*|schema|schemi|diagramm\w*|grafic[oi]|andamento|linea del tempo|cronologia|timeline|"
    r"come funziona|come fa (?:un|una|il|la|lo|l')|fammelo vedere|dal lato|dall'altro lato|"
    r"draw|diagram|chart|graph|how does .{1,40} work|from the .{1,30} side)\b",
    re.IGNORECASE,
)
# only their explicit wish for words from now on is a lasting setting
# ("solo testo", "disegna meno", "niente disegni", "non disegnare più", "basta disegni", "text only", "stop
# drawing"); a one-off "non disegnarlo" / "senza disegni" holds this turn only; their past or someone else's
# drawing ("non ho mai disegnato", "mia figlia non disegna più") and a complaint ("no drawing showed up") set
# nothing
_PLAIN = re.compile(
    r"\b(?:solo (?:a )?(?:testo|parole)|disegna meno|niente (?:più |piu )?disegni|basta (?:con i )?disegni|"
    r"text only|words only|no more drawings?|stop drawing)\b",
    re.IGNORECASE,
)
_NOT_DRAW = re.compile(
    r"\b(?:non\s+(?:mi\s+|ci\s+)?disegnare\s+(?:più|piu)|smetti(?:la)?\s+di\s+disegnare|"
    r"(?:don'?t|never)\s+draw\s+(?:anymore|again))\b",
    re.IGNORECASE,
)
_ONCE = re.compile(
    r"\b(?:non\s+(?:me\s+lo\s+|me\s+la\s+|mi\s+|ci\s+|lo\s+|la\s+)?disegn(?:are|arlo|arla|armelo|armela)\b|"
    r"senza\s+disegn\w*|(?:don'?t|no need to)\s+draw\b)",
    re.IGNORECASE,
)


# "disegna pure" ends their "disegna meno" (never negated: "non disegnare più" is plain)
_DRAW_AGAIN = re.compile(
    r"\b(?:disegna pure|puoi disegnare|torna a disegnare|disegni pure|riprendi a disegnare|draw again|"
    r"you can draw|drawings? (?:are )?(?:back|on))\b",
    re.IGNORECASE,
)


def asks(words: str) -> bool:
    """Their words ask to see a structure, a flow or numbers drawn (never when they ask for text only)."""
    return bool(_ASKS.search(words or "")) and not plain(words) and not plain_once(words)


def plain(words: str) -> bool:
    """Their words want text only from now on ("solo testo", "disegna meno", "non disegnare più"): a
    lasting setting, until they say otherwise (`draw_again`)."""
    text = words or ""
    return bool(_PLAIN.search(text) or _NOT_DRAW.search(text)) and not _DRAW_AGAIN.search(text)


def plain_once(words: str) -> bool:
    """ "non disegnarlo", "senza disegni": words only for this turn, no setting."""
    text = words or ""
    return bool(_ONCE.search(text)) and not _DRAW_AGAIN.search(text)


def draw_again(words: str) -> bool:
    """Their words end "disegna meno" ("disegna pure", "puoi disegnare di nuovo")."""
    text = words or ""
    return bool(_DRAW_AGAIN.search(text)) and not _NOT_DRAW.search(text) and not _ONCE.search(text)


# RAIL: a drawing never imitates Pisa's approval, Face ID or a payment: such a label is left out
# ("Autorizza", "Touch ID", "Invia", a transfer, "Send"; matched on the
# normalised text: NFKC, no format characters, look-alike letters as Latin)
_APPROVAL = re.compile(
    r"\b(?:approv[ai]\w*|approve\w*|conferm\w*|confirm\w*|paga|pagare|pagamento|pagamenti|pagalo|pagala|pay|"
    r"pays|payment\w*|(?:face|touch)[\s\-_.]*id|autorizz\w*|authori[sz]\w*|bonific\w*)\b",
    re.IGNORECASE,
)
# … and a short label that is a send or buy button ("Invia", "Send now", "Acquista"): a flow's "App invia la
# domanda al modello" stays
_BUTTON = re.compile(
    r"^\W*(?:invia\w*|send|transfer|trasferisci|acquist[ai]|compra|buy|purchase)\b", re.IGNORECASE
)
# Cyrillic and Greek letters that look like Latin ones ("Approva" written with a Cyrillic A)
_LOOKALIKE = str.maketrans(
    "АВЕЅІЈКМНОРСТХУаеіјорсхуѕԁһӏΑΒΕΖΗΙΚΜΝΟΡΤΥΧαοιυνκρτ",
    "ABESIJKMHOPCTXYaeijopcxysdhlABEZHIKMNOPTYXaoiuvkpt",
)


def _normal(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Cf").translate(_LOOKALIKE)


def approval_like(text: str | None) -> bool:
    plain = _normal(text or "")
    return bool(_APPROVAL.search(plain)) or (len(plain.split()) <= 3 and bool(_BUTTON.search(plain)))


def knowledge_diagram(block: dict[str, Any], texts: list[str]) -> bool:
    """A diagram (or a timeline) whose labels this turn's results don't carry: Pisa's own knowledge. At least
    half its labels found in the results make it the tools'."""
    labels = [str(n.get("label") or "") for n in block.get("nodes") or block.get("items") or []]
    labels = [label.casefold() for label in labels if label]
    if not labels:
        return True
    corpus = "\n".join(texts).casefold()
    found = sum(label in corpus for label in labels)
    return found * 2 < len(labels)


def has_number(block: dict[str, Any]) -> bool:
    kind = block.get("type")
    if kind == "chart":
        return True
    if kind == "stat":
        return any(c.isdigit() for c in str(block.get("value") or ""))
    if kind == "table":
        return any(
            (isinstance(c, int | float) and not isinstance(c, bool)) or numeric_text(c)
            for row in block.get("rows") or []
            for c in row
        )
    return False


def basis(blocks: list[dict[str, Any]], texts: list[str], read: bool) -> Basis | None:
    """The sheet's basis, by code: knowledge when the turn read nothing and the sheet holds no number; mixed
    when a knowledge diagram stands beside traced data; else tools (None: the field is left out).
    a number the sheet shows traced, so a sheet with one is never
    "knowledge", whatever the turn read."""
    numbers = any(has_number(b) for b in blocks)
    drawn = [b for b in blocks if b.get("type") in ("diagram", "timeline")]
    knowing = any(knowledge_diagram(b, texts) for b in drawn)
    if numbers:
        return "mixed" if knowing else None
    return "knowledge" if not read or knowing else None


def shown_numbers(data: dict[str, Any] | None) -> list[str]:
    """The numbers a sheet Pisa showed
    carries, each traced to its own turn's results when it was shown: a chart's points, a table's
    numeric cells and the quantities in its text cells. A stat is left out (its value may be a derive CORE
    computed), and so is a sheet of what Pisa knows (it holds none). A later drawing may show them again."""
    if not data or data.get("basis") == "knowledge":
        return []
    out: list[str] = []
    for b in data.get("blocks") or []:
        kind = b.get("type")
        if kind == "chart":
            for s in b.get("series") or []:
                for p in s.get("points") or []:
                    out += [str(v) for v in (p.get("x"), p.get("y")) if isinstance(v, int | float)]
        elif kind == "table":
            for row in b.get("rows") or []:
                for c in row:
                    if isinstance(c, bool):
                        continue
                    if isinstance(c, int | float) or numeric_text(c):
                        out.append(str(c))
                    elif isinstance(c, str):
                        out += _QUANTITY.findall(_MOMENTS.sub(" ", c))
    return out


def _x(value: Any) -> str:
    return str(value)[:80]


def fallback(block: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The same answer for a phone that doesn't draw this block type (made by code): a chart as a table (x,
    then one column per series), a timeline as a list, a diagram as a list of "A → B: label". (block, what
    was left out or None)."""
    kind = block["type"]
    if kind == "chart":
        series = block["series"][: TABLE_COLUMNS - 1]
        xs: list[Any] = []
        for s in series:
            xs += [p["x"] for p in s["points"] if p["x"] not in xs]
        values = {(s["id"], p["x"]): p["y"] for s in series for p in s["points"]}
        rows: list[list[str | int | float]] = [
            [_x(x), *[values.get((s["id"], x), "") for s in series]] for x in xs[:TABLE_ROWS]
        ]
        left = (
            f"{len(xs) - TABLE_ROWS} chart row(s) past the {TABLE_ROWS}th" if len(xs) > TABLE_ROWS else None
        )
        columns = [str(block["x"]["label"])[:40], *[str(s["name"])[:40] for s in series]]
        return {"type": "table", "columns": columns, "rows": rows}, left
    if kind == "timeline":

        def when(value: str) -> str:
            return value[:16].replace("T", " ")

        items = [
            f"{when(i['at'])}{' → ' + when(i['end']) if i.get('end') else ''}: {i['label']}"[:200]
            for i in block["items"]
        ]
        left = f"{len(items) - LIST_ITEMS} timeline item(s)" if len(items) > LIST_ITEMS else None
        return {"type": "list", "items": items[:LIST_ITEMS]}, left
    labels = {n["id"]: n["label"] for n in block["nodes"]}
    lines = [
        f"{labels[e['from']]} → {labels[e['to']]}" + (f": {e['label']}" if e.get("label") else "")
        for e in block["edges"]
    ] or [str(n["label"]) for n in block["nodes"]]
    left = f"{len(lines) - LIST_ITEMS} diagram line(s)" if len(lines) > LIST_ITEMS else None
    return {"type": "list", "items": [line[:200] for line in lines[:LIST_ITEMS]]}, left


# a tap on an element is the user's act of asking about it.
# Its words are the box's, "Sul disegno: <label>"; that it is a tap is
# the turn's flag (`origin: drawing`), never read from these words
GONE = {"it-IT": "Non è più aperto.", "en-US": "It's no longer open."}
CONTEXT_LINES = 40


def tap_words(label: str, lang: str) -> str:
    return drawing_rules.ask_words(label, lang)


def element(data: dict[str, Any], element_id: str) -> tuple[str, list[str]] | None:
    """The element a tap names, in the server's copy of the sheet: (its label, the drawing's other labels and
    edges as context lines), or None when this revision has no such element. The first drawing that has it."""
    for block in data.get("blocks") or []:
        kind = block.get("type")
        if kind not in DRAWING_TYPES:
            continue
        found = _in(block, element_id)
        if found is not None:
            return found, _context(block)[:CONTEXT_LINES]
    return None


def _in(block: dict[str, Any], element_id: str) -> str | None:
    kind = block["type"]
    if kind == "diagram":
        labels = {n["id"]: n["label"] for n in block["nodes"]}
        if element_id in labels:
            return str(labels[element_id])
        if (m := re.fullmatch(r"e([1-9][0-9]?)", element_id)) and int(m.group(1)) <= len(block["edges"]):
            e = block["edges"][int(m.group(1)) - 1]
            return f"{labels[e['from']]} → {labels[e['to']]}" + (f" ({e['label']})" if e.get("label") else "")
        return None
    if kind == "chart":
        unit = block["y"].get("unit") or ""
        for s in block["series"]:
            for p in s["points"]:
                if f"{s['id']}/{p['id']}" == element_id:
                    return f"{s['name']}, {p['x']}: {p['y']}{(' ' + unit) if unit else ''}"
        return None
    for i in block["items"]:
        if i["id"] == element_id:
            return str(i["label"])
    return None


def _context(block: dict[str, Any]) -> list[str]:
    kind = block["type"]
    if kind == "diagram":
        labels = {n["id"]: n["label"] for n in block["nodes"]}
        lines = [f"{n['label']}" + (f": {n['detail']}" if n.get("detail") else "") for n in block["nodes"]]
        lines += [
            f"{labels[e['from']]} → {labels[e['to']]}" + (f" ({e['label']})" if e.get("label") else "")
            for e in block["edges"]
        ]
        return lines
    if kind == "chart":
        return [
            f"{s['name']}: " + ", ".join(f"{p['x']} {p['y']}" for p in s["points"]) for s in block["series"]
        ]
    return [f"{i['at']} {i['label']}" for i in block["items"]]


@dataclass(frozen=True)
class Tap:
    """A tap's element, from the server's copy of the sheet: its label, the drawing around it, and whether
    the turn that drew it read outside text (then the label is outside text too)."""

    label: str
    context: tuple[str, ...]
    outside: bool


def tap_head(words: str, tap: Tap, lang: str) -> str:
    """The tap turn's first line for the model: the template with the label inline only when the drawing is
    Pisa's own; after outside text, the template's head alone (the label goes in the quoted block)."""
    head = drawing_rules.ASK_WORDS.get(lang, drawing_rules.ASK_WORDS["en-US"]).split("{")[0].rstrip()
    return f"{head if tap.outside else words} (their tap on Pisa's drawing; what they tapped is below)"


def tap_data(tap: Tap) -> str:
    """What they tapped and the drawing around it, for a quoted data block."""
    return "\n".join([f"tapped: {tap.label}", "the drawing around it:", *tap.context])


# the answer blocks for the phone in hand. A phone that lists a type in
# its hello's `present.blocks` gets it (and not the tables it replaces: never the same table twice); any other
# gets the same answer as tables, lists and links, made here. The columns are the research table's
# (`pisa.brain.findings`), so an older phone sees today's sheet.
FINDINGS_COLUMNS = {
    "it-IT": ("Posto", "A piedi", "Voto", "Aperto", "Prezzo", "Citato da"),
    "en-US": ("Place", "On foot", "Rating", "Open", "Price", "Cited by"),
}
POINTS_COLUMNS = {
    "it-IT": ("Posto", "Perché per te ora", "Pro", "Contro"),
    "en-US": ("Place", "Why for you now", "Pros", "Cons"),
}
EVENT_COLUMNS = {
    "it-IT": ("Quando", "Evento", "Dove", "Prezzo", "Età"),
    "en-US": ("When", "Event", "Where", "Price", "Age"),
}
PRODUCT_COLUMNS = {
    "it-IT": ("Prodotto", "Negozio", "Prezzo", "Spedizione", "Minimo 6 mesi"),
    "en-US": ("Product", "Shop", "Price", "Shipping", "6-month low"),
}
_ANSWER_WORDS = {
    "it-IT": {"excluded": "Esclusi", "slots": "Posti alle", "page": "Apri la pagina", "open": "aperto",
              "closed": "chiuso", "free": "gratis"},
    "en-US": {"excluded": "Excluded", "slots": "Tables at", "page": "Open the page", "open": "open",
              "closed": "closed", "free": "free"},
}  # fmt: skip
CELL = 80


def _words(lang: str) -> dict[str, str]:
    return _ANSWER_WORDS.get(lang, _ANSWER_WORDS["en-US"])


def _cut(text: Any, limit: int = CELL) -> str:
    """Whole words within the cell."""
    out = " ".join(str(text or "").split())
    return out if len(out) <= limit else out[: limit + 1].rsplit(" ", 1)[0][:limit]


def recognition_label(r: dict[str, Any]) -> str:
    """ "Michelin ★ 2026", "Gambero Rosso Tre Spicchi 2027": the chip's text."""
    return " ".join(str(x) for x in (r.get("guide"), r.get("award"), r.get("year")) if x)


def money(value: float, currency: str, lang: str) -> str:
    """'199,00 €' in Italian, '€199.00' in English; another currency by its code."""
    if lang == "it-IT":
        figure = f"{value:,.2f}".replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
        return f"{figure} €" if currency == "EUR" else f"{figure} {currency}"
    return f"€{value:,.2f}" if currency == "EUR" else f"{value:,.2f} {currency}"


def _open_cell(row: dict[str, Any], lang: str) -> str:
    asked = row.get("open_at_asked") or {}
    if asked.get("hours"):
        return _cut(asked["hours"])
    if asked.get("ok") is None:
        return ""
    return _words(lang)["open" if asked["ok"] else "closed"]


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").removeprefix("www.")


def findings_fallback(block: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """A findings block as today's tables: places (facts, then why, pros and cons), or events (when, what,
    where, price, age); then each row's slots as a line and a link (never a button)."""
    rows = block["rows"][:TABLE_ROWS]
    places = [r for r in rows if r["kind"] == "place"]
    events = [r for r in rows if r["kind"] == "event"]
    out: list[dict[str, Any]] = []
    if places:
        out.append({
            "type": "table", "columns": list(FINDINGS_COLUMNS.get(lang, FINDINGS_COLUMNS["en-US"])),
            "rows": [[_cut(r["name"]), r.get("walk", ""), r.get("rating", ""), _open_cell(r, lang),
                      r.get("price", ""),
                      _cut(", ".join([*(recognition_label(x) for x in r.get("recognition") or []),
                                      *(r.get("cited_by") or [])]))] for r in places],
        })  # fmt: skip
        out.append({
            "type": "table", "columns": list(POINTS_COLUMNS.get(lang, POINTS_COLUMNS["en-US"])),
            "rows": [[_cut(r["name"]), _cut(r.get("why", "")), _cut("; ".join(r.get("pros") or [])),
                      _cut("; ".join(r.get("cons") or []))] for r in places],
        })  # fmt: skip
    if events:
        out.append({
            "type": "table", "columns": list(EVENT_COLUMNS.get(lang, EVENT_COLUMNS["en-US"])),
            "rows": [[str(r.get("at", ""))[:16].replace("T", " "), _cut(r["name"]), _cut(r.get("venue", "")),
                      r.get("price", ""), r.get("age", "")] for r in events],
        })  # fmt: skip
    w = _words(lang)
    for r in rows:
        if slots := r.get("slots"):
            out.append(
                {"type": "text", "text": f"{r['name'][:120]}: {w['slots']} {', '.join(slots['times'])}."}
            )
            out.append({"type": "link", "url": slots["url"], "title": w["page"], "host": _host(slots["url"])})
    return out


def products_fallback(products: list[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    """Product cards as one table and a link per offer."""
    cols = PRODUCT_COLUMNS.get(lang, PRODUCT_COLUMNS["en-US"])
    rows = [
        [_cut(p["title"]), _cut(p["shop"]), money(p["price"], p["currency"], lang),
         "" if p.get("shipping") is None else
         (_words(lang)["free"] if p["shipping"] == 0 else money(p["shipping"], p["currency"], lang)),
         "" if p.get("low_6m") is None else money(p["low_6m"], p["currency"], lang)]
        for p in products
    ]  # fmt: skip
    links = [
        {
            "type": "link",
            "url": p["url"],
            "title": _cut(f"{p['shop']}: {p['title']}", 120),
            "host": _host(p["url"]),
        }
        for p in products
        if _host(p["url"])
    ]
    return [{"type": "table", "columns": list(cols), "rows": rows}, *links]


def answers_for_phone(data: dict[str, Any], draws: frozenset[str] | None, lang: str) -> dict[str, Any]:
    """The sheet as this phone gets it (contract answers-1). `draws`: the hello's `present.blocks` (None:
    none).
    Code's alone: nothing here is the model's."""
    draws = draws or frozenset()
    blocks: list[dict[str, Any]] = list(data.get("blocks") or [])
    if not any(b.get("type") in ("findings", "map", "product") for b in blocks) and not data.get("excluded"):
        return data
    out: list[dict[str, Any]] = []
    findings = next((b for b in blocks if b.get("type") == "findings"), None)
    planned = findings_fallback(findings, lang) if findings is not None else []
    plain_columns = [tuple(t["columns"]) for t in planned if t["type"] == "table"]
    present_columns = {tuple(b.get("columns") or ()) for b in blocks if b.get("type") == "table"}
    products = [b for b in blocks if b.get("type") == "product"]
    for b in blocks:
        kind = b.get("type")
        if kind == "findings":
            if "findings" in draws:
                out.append(b)
            else:  # the tables the sheet doesn't already carry
                out += [
                    t for t in planned if t["type"] != "table" or tuple(t["columns"]) not in present_columns
                ]
        elif kind == "map":
            if "map" in draws:
                out.append(b)
        elif kind == "product":
            if "product" in draws:
                out.append(b)
            elif b is products[0]:
                out += products_fallback(products, lang)
        elif (
            kind == "table"
            and "findings" in draws
            and findings is not None
            and tuple(b["columns"]) in plain_columns
        ):
            continue  # the findings block shows it
        else:
            out.append(b)
    sent = {**data}
    if data.get("excluded") and not ("findings" in draws and findings is not None):
        sent.pop("excluded")
        under = max((i + 1 for i, b in enumerate(out) if b.get("type") == "table"), default=len(out))
        out[under:under] = [  # under the table, as the phone's "Esclusi" line
            {"type": "section", "title": _words(lang)["excluded"]},
            {
                "type": "list",
                "items": [_cut(f"{e['name']}: {e['why']}", 200) for e in data["excluded"][:LIST_ITEMS]],
            },
        ]
    # a phone that draws the map but not the rows (or the products) gets only the pins that name
    # what it was sent: a pin never opens a card the sheet doesn't carry (and the sheet stays valid)
    named = {r.get("id") for b in out if b.get("type") == "findings" for r in b.get("rows") or []}
    named |= {b.get("id") for b in out if b.get("type") == "product"}
    kept: list[dict[str, Any]] = []
    for b in out:
        if b.get("type") == "map":
            pins = [p for p in b.get("pins") or [] if p.get("id") in named]
            if not pins:
                continue
            b = {**b, "pins": pins}
        kept.append(b)
    sent["blocks"] = kept[:24]
    return sent
