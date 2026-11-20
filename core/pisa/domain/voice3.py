"""The night, spoken. Pure rules, no I/O.

- **Their words are a closed set** (`heard`). A phrase is the whole utterance (an optional "Pisa" before or
  after it); a sentence that says more ("cosa hai fatto stanotte con le fatture?"), an imperative or a
  question mark where the phrase is no question goes to the model. The service answers one only when Pisa's
  own last thing opened its path and no question, approval or held change of anyone else's is open.
- **The night-shift story** (`story`): about 30 seconds, composed by code from the night's records: the
  counts, the one thing Pisa is proudest of, the one it got wrong, what it wants to know next. Every sentence
  is a `Claim` naming its records; every number said is the length of a list of record ids.
- **"Dammi tutto"** (`drawer`): the full drawer as a sheet (pull: no push cap, as many blocks as
  holds, then a count of the rest, which `stanotte` finds), and one spoken line.
- **The sparring partner** (`spar_words`): Pisa argues the other side of one idea from its own records (its
  kill criteria written before the evidence, the lab not on, a dead sibling, one sighting), then asks their p;
  once per idea per day.
- **La scommessa** (`parse_bet`): "scommetto che …" with a day, stored with their p and Pisa's p, only when
  code has a public resolver for it (the rain at one of their places, a call in Pisa's window calendar); never
  about other people's private lives, never health.
- **Il quarto d'ora** (`quarto_*`): opt-in, at most two a week, offered only inside the knock on a walk, a
  fixed end 15 minutes after they start; "tu prima": Pisa's material only on their "tocca a te".
- **Outside text is data**: a spoken idea is Pisa's own words from their own problem (no outside
  seed, no name their problem didn't say); a question is spoken only when it is their own or Pisa's template.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any, Literal

from pisa.domain import geo_rules as geo
from pisa.domain import stanotte as night_rules
from pisa.domain import walks as walk_rules
from pisa.domain.lexicon.dates import day_or_none
from pisa.domain.lexicon.weekdays import EN_WEEKDAYS, IT_WEEKDAYS_PLAIN
from pisa.domain.places import distance_m
from pisa.domain.rituals import sensitive
from pisa.domain.stays import Fix
from pisa.domain.text import fold_apostrophe

Heard = Literal["story", "spar", "p", "bet", "quarto_start", "quarto_turn", "quarto_end"]

STORY_WORDS = 75  # ~30 seconds spoken at ~2.5 words a second ("30 seconds")
QUARTO_MINUTES = 15  # FACT: a walk-time talk with a fixed end
QUARTO_PER_WEEK = 2  # FACT: twice a week at most
QUARTO_START_WITHIN = timedelta(minutes=30)  # their "quarto d'ora" starts the one Pisa offered this long ago
SPAR_RESOLVES = timedelta(days=30)  # the spar's question: "will you keep it?", settled within this
BET_RAIN_MM = 1.0  # FACT (WMO): a day with at least 1 mm of precipitation is a rainy day
BET_RAIN_DAYS = 7  # Pisa's p for rain is the forecast's: at most its 7 days ahead
BET_WINDOW_DAYS = 90  # a call's bet, at most this far (the window horizon)
SPAR_POINTS = 3
BET_WINDOW_GRACE = timedelta(days=3)  # a call's bet is looked at three more nights before unresolved

# --- their words
# ---------------------------------------------------------------------------------------------


def plain(text: str) -> str:
    words = re.findall(r"[a-z0-9']+", fold_apostrophe(text))
    return " ".join(w.strip("'") for w in words if w.strip("'"))


def _bare(text: str) -> str:
    """The words without a "Pisa" before or after them ("Pisa, cosa hai fatto stanotte?", "… stanotte,
    Pisa?")."""
    words = plain(text).split()
    if words[:1] == ["pisa"]:
        words = words[1:]
    if words[-1:] == ["pisa"]:
        words = words[:-1]
    return " ".join(words)


_STORY = {
    "cosa hai fatto stanotte", "che hai fatto stanotte", "cos'hai fatto stanotte", "cosa hai fatto sta notte",
    "cosa hai fatto questa notte", "che hai fatto questa notte", "cos'hai fatto questa notte",
    "com'e andata stanotte", "come e andata stanotte", "com'e andata la notte", "come e andata la notte",
    "raccontami la notte", "raccontami stanotte", "racconta la notte",
    "what did you do last night", "what did you do overnight", "how did the night go",
    "tell me about last night",
}  # fmt: skip
_SPAR = {
    "facciamo a pugni su questa idea", "facciamo a pugni su quest'idea", "facciamo a pugni sull'idea",
    "facciamo a pugni", "litighiamo su questa idea", "fammi l'avvocato del diavolo",
    "fai l'avvocato del diavolo", "let's fight about this idea", "lets fight about this idea",
    "argue the other side",
}  # fmt: skip
_QUARTO_START = {
    "quarto d'ora", "il quarto d'ora", "facciamo il quarto d'ora", "si il quarto d'ora",
    "si facciamo il quarto d'ora", "iniziamo il quarto d'ora", "dai il quarto d'ora", "ok il quarto d'ora",
    "the quarter hour", "let's do the quarter hour", "lets do the quarter hour",
}  # fmt: skip
_QUARTO_TURN = {
    "tocca a te", "adesso tu", "ora tu", "e tu", "dimmi la tua", "adesso dimmi la tua", "la tua",
    "your turn", "and you", "now you",
}  # fmt: skip
_QUARTO_END = {
    "basta", "basta cosi", "chiudiamo", "chiudiamo qui", "fine quarto d'ora",
    "basta quarto d'ora", "enough", "let's stop", "lets stop",
}  # fmt: skip
_QUESTIONS = ("story", "quarto_turn")  # a "?" belongs to these phrases ("e tu?"); for the others it says more
_BET = re.compile(r"^(?:pisa\s+)?(?:ci\s+)?scommett(?:o|iamo)\s+che\s+(?P<claim>.+)$")
_BET_EN = re.compile(r"^(?:pisa\s+)?i\s+bet\s+(?:that\s+)?(?P<claim>.+)$")
_P = re.compile(
    r"^(?:(?:direi|dico|forse)\s+)?(?:al\s+)?(?P<n>\d{1,3})(?:\s*(?:su|out of|/)\s*(?P<of>10|100)|"
    r"\s*(?:%|per\s*cento|percent))?$"
)
_P_WORDS = {
    "zero": 0, "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8,
    "nove": 9, "dieci": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip


def _unj(raw: str) -> str:
    """Their words without the "Pisa," they call Pisa by (its comma is no second clause)."""
    raw = re.sub(r"^\s*(?:ehi\s+)?pisa\s*[,:]\s*", "", raw, flags=re.IGNORECASE)
    return re.sub(r"\s*,\s*pisa\s*([?.!]*)\s*$", r"\1", raw, flags=re.IGNORECASE)


def heard(text: str) -> Heard | None:
    """Which of their closed phrases this whole utterance is; None: not one (the model's)."""
    raw = text.strip()
    if not raw or len(raw) > 200:
        return None
    asks = "?" in raw
    words = _bare(raw)
    if not words:
        return None
    if _BET.match(words) or _BET_EN.match(words):
        return None if re.search(r"[,;:!?]", _unj(raw).rstrip(". ")) else "bet"
    if p_of(raw) is not None:
        return "p"
    found: Heard | None = None
    for kind, phrases in (
        ("story", _STORY), ("spar", _SPAR), ("quarto_start", _QUARTO_START), ("quarto_turn", _QUARTO_TURN),
        ("quarto_end", _QUARTO_END),
    ):  # fmt: skip
        if words in {plain(p) for p in phrases}:
            found = kind  # type: ignore[assignment]
            break
    if found is None or (asks and found not in _QUESTIONS):
        return None
    if raw.count("?") > 1 or re.search(r"[!;:]", raw):
        return None  # more than the phrase: an exclamation, a second clause
    return found


def p_of(text: str) -> float | None:
    """Their p, said alone: "7", "sette", "7 su 10", "70%", "al 70 per cento" → 0..1; None: not a bare p."""
    raw = text.strip()
    if "?" in raw:
        return None
    words = _bare(raw.replace("%", " %"))
    words = words.replace(" %", "%")
    if words in _P_WORDS:
        return _P_WORDS[words] / 10
    m = _P.match(re.sub(r"\s+", " ", raw.strip().lower().rstrip(".")).removeprefix("pisa ").strip())
    if m is None:
        return None
    n, of = int(m.group("n")), m.group("of")
    percent = of == "100" or raw.rstrip(". ").endswith(("%", "cento", "percent"))
    if percent:
        return n / 100 if 0 <= n <= 100 else None
    return n / 10 if 0 <= n <= 10 else None


def journal_p(p: float) -> float:
    """The journal keeps p strictly between 0 and 1 (the decision journal's Pisa side): 0 and 1 become .01 and .99."""  # noqa: E501
    return min(0.99, max(0.01, round(p, 2)))


# --- the night-shift story ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One sentence of the story, with the records it says (table, ids) and the counts it speaks."""

    text: str
    table: str  # the records' kind: night, ideas, cemetery, searches, answered, shelf, misses, replays
    ids: tuple[str, ...]
    numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class Proud:
    """The night's idea Pisa is proudest of: shown (its evidence passed again), its words Pisa's own."""

    id: str
    words: str  # the statement, or its kind and market when the statement can't be spoken (outside seeds)
    checked: int  # its evidence lines that passed their check now


@dataclass(frozen=True)
class Wrong:
    """The one thing Pisa got wrong, from its records: an idea it believed and killed, a confident miss, a
    turn of yesterday it wouldn't redo the same."""

    kind: Literal["idea", "miss", "replay"]
    table: str
    ids: tuple[str, ...]
    p: float | None = None  # Pisa's own p (the idea's p_keep, the prediction's p)
    miss: str = ""  # a confident miss in Pisa's template words


@dataclass(frozen=True)
class Next:
    """What Pisa wants to know next: its shelf's top question (their words or Pisa's template) or its
    category."""

    id: str
    question: str | None  # None: Pisa says only what it is about
    about: str  # life | work | world | self


@dataclass
class Night:
    """One night's records, as the service read them: each count is its rows' ids."""

    night: date
    problems: list[str] = field(default_factory=list)
    incubated: list[str] = field(default_factory=list)
    validated: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    proud: Proud | None = None
    wrong: Wrong | None = None
    next: Next | None = None


_ABOUT = {
    "it-IT": {"life": "la tua giornata", "work": "il tuo lavoro", "world": "il mondo", "self": "me stesso"},
    "en": {"life": "your days", "work": "your work", "world": "the world", "self": "myself"},
}
_KIND = {
    "software": ("un software", "a software idea"), "service": ("un servizio", "a service"),
    "content": ("un contenuto", "a content idea"), "arbitrage": ("un arbitraggio", "an arbitrage"),
    "physical": ("un prodotto fisico", "a physical product"),
}  # fmt: skip
_MARKET = {"it": ("per l'Italia", "for Italy"), "us": ("per gli USA", "for the US"),
           "both": ("per Italia e USA", "for Italy and the US")}  # fmt: skip


def it_(lang: str) -> bool:
    return lang.startswith("it")


def about_words(about: str, lang: str) -> str:
    return _ABOUT["it-IT" if it_(lang) else "en"].get(about, about)


def idea_words(kind: str, market: str, lang: str) -> str:
    """An idea said by what it is, never by its words (its words carry outside text)."""
    i = 0 if it_(lang) else 1
    k, m = _KIND.get(kind, ("un'idea", "an idea"))[i], _MARKET.get(market, ("", ""))[i]
    return " ".join(p for p in (k, m) if p)


_CAPITAL = re.compile(r"(?<![.!?:]\s)(?<!^)\b[A-ZÀ-Ý][\w&'-]*")


def speakable(statement: str, source: str, *, outside_seeds: bool) -> bool:
    """Pisa speaks an idea's own words only when nothing outside fed it (no world delta in its
    seeds), nothing in it labels them, and every name in it is one their own problem said."""
    if outside_seeds or not statement.strip() or not night_rules.clean(statement) or sensitive(statement):
        return False
    said = plain(source)
    return all(plain(n) in said for n in _CAPITAL.findall(statement.strip()))


def _n(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


def story(night: Night, lang: str) -> list[Claim]:
    """The night-shift story, claim by claim (counts, the proudest, the wrong one, the next question)."""
    it = it_(lang)
    out: list[Claim] = []
    n = {k: len(getattr(night, k)) for k in ("problems", "incubated", "validated", "ready", "killed")}
    s = len(night.searches)
    if not any(n.values()) and not s and not night.answered:
        quiet = (
            "Stanotte ho lavorato poco: niente idee nuove." if it else "Last night was quiet: no new ideas."
        )
        out.append(Claim(quiet, "night", (night.night.isoformat(),)))
    else:
        parts: list[str] = []
        numbers: list[int] = []
        if it:
            parts += (
                [_n(n["problems"], "problema tuo ripreso", "problemi tuoi ripresi")] if n["problems"] else []
            )
            parts += [_n(n["incubated"], "idea incubata", "idee incubate")] if n["incubated"] else []
            parts += [_n(n["ready"], "ha retto", "hanno retto")] if n["validated"] else []
            parts += [_n(n["killed"], "scartata", "scartate")] if n["killed"] else []
            parts += [_n(s, "ricerca senza niente di tuo", "ricerche senza niente di tuo")] if s else []
        else:
            parts += (
                [_n(n["problems"], "problem of yours taken up", "problems of yours taken up")]
                if n["problems"]
                else []
            )
            parts += [_n(n["incubated"], "idea incubated", "ideas incubated")] if n["incubated"] else []
            parts += [f"{n['ready']} held up"] if n["validated"] else []
            parts += [f"{n['killed']} killed"] if n["killed"] else []
            parts += [_n(s, "search with nothing of yours", "searches with nothing of yours")] if s else []
        numbers += [n["problems"]] if n["problems"] else []
        numbers += [n["incubated"]] if n["incubated"] else []
        numbers += [n["ready"]] if n["validated"] else []
        numbers += [n["killed"]] if n["killed"] else []
        numbers += [s] if s else []
        ids = (*night.problems, *night.incubated, *night.ready, *night.killed, *night.searches)
        head = "Stanotte: " if it else "Last night: "
        out.append(Claim(head + ", ".join(parts) + ".", "night", ids, tuple(numbers)))
    if (p := night.proud) is not None:
        text = (
            f"Ne vado fiero: {p.words}, {_n(p.checked, 'prova verificata', 'prove verificate')}."
            if it
            else f"I'm proudest of {p.words}, {_n(p.checked, 'checked line', 'checked lines')}."
        )
        out.append(Claim(text, "ideas", (p.id,), (p.checked,)))
    elif night.answered:
        a = len(night.answered)
        text = (
            f"Ne vado fiero: {_n(a, 'domanda chiusa', 'domande chiuse')}." if it
            else f"I'm proud of this: {_n(a, 'question closed', 'questions closed')}."
        )  # fmt: skip
        out.append(Claim(text, "answered", tuple(night.answered), (a,)))
    if (w := night.wrong) is not None:
        out.append(Claim(wrong_words(w, lang), w.table, w.ids, wrong_numbers(w)))
    if (q := night.next) is not None:
        if q.question:
            ask = night_rules.cut(q.question, 140)
            ask = ask if ask.endswith("?") else ask.rstrip(".") + "?"
            text = f"Ora voglio capire: {ask}" if it else f"Next I want to know: {ask}"
        else:
            about = about_words(q.about, lang)
            text = f"Ora voglio capire una cosa su {about}." if it else f"Next I want to understand {about}."
        out.append(Claim(text, "shelf", (q.id,)))
    return out


def wrong_words(w: Wrong, lang: str) -> str:
    it = it_(lang)
    if w.kind == "idea":
        pct = round((w.p or 0) * 100)
        return (
            f"Ho sbagliato su un'idea: ci credevo {pct} su 100, "
            "l'ha uccisa il criterio scritto prima delle prove."
            if it
            else f"I got an idea wrong: I gave it {pct} in 100, "
            "the criterion I wrote before the evidence killed it."
        )
    if w.kind == "miss":
        return w.miss.removeprefix("Perché mi sono sbagliato? ").replace(
            "Ero sicuro", "Ho sbagliato: ero sicuro", 1
        )
    k = len(w.ids)
    return (
        f"Ho sbagliato qualcosa: {k} turni di ieri non li rifarei uguale."
        if it
        else f"I got something wrong: {k} of yesterday's turns I wouldn't redo the same."
    )


def wrong_numbers(w: Wrong) -> tuple[int, ...]:
    if w.kind == "replay":
        return (len(w.ids),)
    return (round((w.p or 0) * 100), 100) if w.kind == "idea" else ()  # "80 su 100": Pisa's p and its scale


def told(claims: Sequence[Claim], lang: str) -> str:
    """The story said: the claims in order, cut at a whole claim near 30 seconds."""
    words: list[str] = []
    for claim in claims:
        more = claim.text.split()
        if words and len(words) + len(more) > STORY_WORDS:
            words += (
                ["Il", "resto", "è", "nel", "cassetto."]
                if it_(lang)
                else ["The", "rest", "is", "in", "the", "drawer."]
            )
            break
        words += more
    return " ".join(words)


# --- "Dammi tutto": the drawer ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One drawer line: the words Pisa may say or return (no outside text), its record (kind, id), and the
    line the sheet shows (the sheet's own words for it, data on their screen, never spoken)."""

    text: str
    table: str
    id: str
    shown: str | None = None

    @property
    def screen(self) -> str:
        return self.shown or self.text


@dataclass
class Drawer:
    ideas: list[Item] = field(default_factory=list)
    dead: list[Item] = field(default_factory=list)
    questions: list[Item] = field(default_factory=list)
    scan: list[Item] = field(default_factory=list)

    def items(self) -> list[Item]:
        return [*self.ideas, *self.dead, *self.questions, *self.scan]


_DRAWER = {
    "it-IT": {"title": "Dammi tutto", "ideas": "Le idee", "dead": "Le morte, con le loro prove",
              "questions": "Le domande aperte", "scan": "Dall'orizzonte", "empty": "Il cassetto è vuoto.",
              "more": "E altre {n}: chiedimele per nome."},
    "en": {"title": "Everything", "ideas": "Ideas", "dead": "The dead ones, with their evidence",
           "questions": "Open questions", "scan": "From the horizon", "empty": "The drawer is empty.",
           "more": "And {n} more: ask me for them by name."},
}  # fmt: skip
_LIST = 8  # a list block's items
NO_SCREEN = {
    "it-IT": "Il cassetto della notte va sul telefono: aprimi lì e te lo mostro.",
    "en-US": "The night's drawer goes on the phone: open me there and I'll show it.",
}


def drawer_sheet(d: Drawer, lang: str) -> tuple[dict[str, Any], int]:
    """The drawer as a sheet (24 blocks, lists of 8): every item that fits, then how many more.
    Returns (the sheet, the items on it)."""
    w = _DRAWER["it-IT" if it_(lang) else "en"]
    blocks: list[dict[str, Any]] = []
    room = 23  # one block kept for "and N more"
    placed = 0
    total = len(d.items())
    for key, items in (("ideas", d.ideas), ("dead", d.dead), ("questions", d.questions), ("scan", d.scan)):
        if not items or len(blocks) + 2 > room:
            continue
        blocks.append({"type": "section", "title": w[key]})
        for at in range(0, len(items), _LIST):
            if len(blocks) + 1 > room:
                break
            chunk = [night_rules.cut(i.screen, 200) for i in items[at : at + _LIST]]
            blocks.append({"type": "list", "items": chunk})
            placed += len(chunk)
    if not blocks:
        blocks.append({"type": "text", "text": w["empty"]})
    if placed < total:
        blocks.append({"type": "text", "text": w["more"].format(n=total - placed)})
    return {"title": w["title"], "blocks": blocks}, placed


def drawer_line(d: Drawer, lang: str) -> str:
    """The one line Pisa says as the drawer opens: the counts on it."""
    n = (len(d.ideas), len(d.dead), len(d.questions), len(d.scan))
    if not any(n):
        return "Il cassetto della notte è vuoto." if it_(lang) else "The night's drawer is empty."
    if it_(lang):
        parts = [
            f"{n[0]} idee",
            f"{n[1]} morte con le prove",
            f"{n[2]} domande aperte",
            f"{n[3]} dall'orizzonte",
        ]
        return (
            "Ti ho messo tutto sul telefono: "
            + ", ".join(p for p, k in zip(parts, n, strict=True) if k)
            + "."
        )
    parts = [
        f"{n[0]} ideas",
        f"{n[1]} dead ones with evidence",
        f"{n[2]} open questions",
        f"{n[3]} from the horizon",
    ]
    return "It's all on your phone: " + ", ".join(p for p, k in zip(parts, n, strict=True) if k) + "."


def matches(query: str, *texts: str) -> bool:
    """`stanotte(query)`: every word of the query (3+ letters) is in the record's words; an empty query or
    "tutto"/"all" matches everything."""
    wanted = [q for q in plain(query).split() if len(q) >= 3 and q not in ("tutto", "all", "everything")]
    said = " ".join(plain(t) for t in texts)
    return all(q in said for q in wanted)


# --- the sparring partner -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SparFacts:
    """What Pisa's records hold against one idea (none of it outside text)."""

    idea_id: str
    words: str
    kill: tuple[str, str, float] | None  # its first kill criterion: (metric, op, value)
    lab: bool  # the lab proved its technical part; False: not proven
    simulated: int  # its simulated lines: never evidence
    sibling: tuple[str, str] | None  # (kind, market) of an idea of its kind that died this week
    convergence: int  # how many times the night found it


def spar_points(f: SparFacts, lang: str) -> list[str]:
    it = it_(lang)
    out: list[str] = []
    if f.kill is not None:
        metric, op, value = f.kill
        name = night_rules.killed_line(night_rules.Killed("", "", metric, op, value, None), lang).split("— ")[
            -1
        ]
        out.append(
            f"Prima delle prove avevo scritto quando la ucciderei: {name}." if it
            else f"Before any evidence I wrote when I'd kill it: {name}."
        )  # fmt: skip
    if not f.lab:
        out.append(
            "La parte tecnica non è provata: il laboratorio non l'ha costruita." if it
            else "The technical part isn't proven: the lab hasn't built it."
        )  # fmt: skip
    if f.sibling is not None:
        sib = idea_words(*f.sibling, lang)
        out.append(
            f"Questa settimana è morta {sib}, dello stesso tipo."
            if it
            else f"This week {sib} of the same kind died."
        )
    if f.simulated:
        out.append(
            f"{f.simulated} prove sono simulate: non contano." if it
            else f"{f.simulated} lines are simulated: they don't count."
        )  # fmt: skip
    if f.convergence <= 1:
        out.append(
            "L'ho trovata una volta sola: nessun segnale ripetuto."
            if it
            else "I found it only once: no repeated signal."
        )
    return out[:SPAR_POINTS]


def spar_words(f: SparFacts, lang: str) -> str:
    points = spar_points(f, lang)
    head = (
        f"Faccio la parte contraria su {f.words}." if it_(lang) else f"I'll take the other side on {f.words}."
    )
    if not points:
        points = [
            "Non ho niente contro nei miei dati." if it_(lang) else "Nothing in my records argues against it."
        ]
    ask = "Tu quanto ci credi, da 0 a 10?" if it_(lang) else "How much do you believe in it, 0 to 10?"
    return " ".join([head, *points, ask])


# --- la scommessa -------------------------------------------------------------------------------------------

Resolver = Literal["rain", "window"]
# a bet is one of these closed shapes, whole; a comma, a word left over or a
# second verb ("…, ricordami di prendere l'ombrello") is the model's.
# On the plain words, after "scommetto che".
_DAY = (
    r"(?:entro\s+)?(?:(?:il|lo)\s+)?(?:oggi|stasera|domani|dopodomani|lunedi|martedi|mercoledi|giovedi|venerdi|"
    r"sabato|domenica|\d{1,2}(?:\s+\d{1,2})?)"
)
_DAY_EN = (
    r"(?:by\s+|on\s+)?(?:the\s+)?"
    r"(?:today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
)
_P_TAIL = r"(?:\s+al\s+(?P<p>\d{1,3})(?:\s+per\s*cento)?)?"
_RAIN_IT = re.compile(
    rf"^(?:(?P<d1>{_DAY})\s+)?(?P<no>non\s+)?(?:piove|piovera)(?:\s+(?P<d2>{_DAY}))?"
    rf"(?:\s+(?P<where>a\s+casa|a\s+\w+|in\s+\w+))?{_P_TAIL}$"
)
_RAIN_EN = re.compile(
    rf"^(?:it\s+)?(?P<verb>rains|will\s+rain|won't\s+rain|wont\s+rain|will\s+not\s+rain|doesn't\s+rain|"
    rf"doesnt\s+rain)\s+(?P<d2>{_DAY_EN})(?:\s+(?P<where>at\s+home|in\s+\w+))?$"
)
_CALL = re.compile(
    rf"^(?:il\s+)?bando(?P<name>(?:\s+[a-z0-9']+){{0,5}}?)\s+(?:esce|uscira|apre|viene\s+pubblicato|"
    rf"sara\s+pubblicato)\s+(?P<d2>{_DAY}){_P_TAIL}$"
)
_STOP = {
    "il",
    "lo",
    "la",
    "i",
    "gli",
    "le",
    "di",
    "del",
    "della",
    "per",
    "che",
    "e",
    "a",
    "da",
    "in",
    "un",
    "una",
}
# RAIL: never about other people's private lives, never health
_PRIVATE = re.compile(
    r"\b(?:sposa\w*|matrimoni\w*|divorz\w*|incint\w*|gravid\w*|fidanz\w*|lascia\w*|tradi\w*|licenzi\w*|"
    r"bambin\w*|figli\w*|morir\w*|muor\w*|guari\w*|ammal\w*|operazion\w*|ospedal\w*|esame|esami|analisi|"
    r"cancro|tumor\w*|covid|vaccin\w*|marries|divorce\w*|pregnant|dies|cancer|hospital|surgery)\b"
)


@dataclass(frozen=True)
class Bet:
    claim: str  # their words after "scommetto che"
    resolver: Resolver | None
    by: date | None
    yes: bool = True  # rain: they bet it rains (False: it doesn't)
    subject: tuple[str, ...] = ()  # window: the call's name words
    his_p: float | None = None
    refused: str | None = None  # why Pisa won't take it (said to them)


def _deadline(words: str, today: date) -> date | None:
    """Oggi, domani, dopodomani, a weekday (the next one, today included), "entro il 20" (this month or
    the next), "entro il 20/11"."""
    if re.search(r"\b(oggi|today|stasera|tonight)\b", words):
        return today
    if re.search(r"\bdopodomani\b", words):
        return today + timedelta(days=2)
    if re.search(r"\b(domani|tomorrow)\b", words):
        return today + timedelta(days=1)
    for names in (IT_WEEKDAYS_PLAIN, EN_WEEKDAYS):
        for i, name in enumerate(names):
            if re.search(rf"\b{name}\b", words):
                return today + timedelta(days=(i - today.weekday()) % 7)
    if m := re.search(r"\b(\d{1,2})(?:[/\s](\d{1,2}))?\b", words):
        d, mo = int(m.group(1)), int(m.group(2)) if m.group(2) else None
        if mo is not None:
            got = day_or_none(today.year, mo, d)
            if got is None:
                return None
            return got if got >= today else day_or_none(today.year + 1, mo, d)
        got = day_or_none(today.year, today.month, d)
        if got is not None and got < today:
            got = day_or_none(today.year + (today.month == 12), today.month % 12 + 1, d)
        return got
    return None


def parse_bet(text: str, today: date, lang: str, *, names: Iterable[str] = ()) -> Bet | None:
    """ "Scommetto che …" in one of the closed shapes (rain at home on a day, a call by name out by a day,
    an optional "al N%") → a bet Pisa takes or one it refuses (with why); anything else (a comma, a second
    clause, a word left over, no day) is None: the model's."""
    raw = text.strip()
    if re.search(r"[,;:!?]", _unj(raw).rstrip(". ")):
        return None
    words = _bare(raw)
    m = _BET.match(words) or _BET_EN.match(words)
    if m is None:
        return None
    claim = m.group("claim").strip()
    it = it_(lang)
    # one of their people by any of their name's words (3+ letters), never only the full name
    tokens = {t for n in names for t in plain(n).split() if len(t) >= 3}
    shape = _RAIN_IT.match(claim) or _RAIN_EN.match(claim)
    call = None if shape else _CALL.match(claim)
    if shape is None and call is None:
        return None
    got = shape or call
    assert got is not None
    day_words = " ".join(w for w in (got.groupdict().get("d1"), got.groupdict().get("d2")) if w)
    if shape is not None and bool(got.groupdict().get("d1")) == bool(got.groupdict().get("d2")):
        return None  # no day, or two
    by = _deadline(day_words, today)
    if by is None or by < today:
        return None
    pct = got.groupdict().get("p")
    his_p = int(pct) / 100 if pct is not None and int(pct) <= 100 else None
    if sensitive(claim) or _PRIVATE.search(claim):
        why = ("Su questo non scommetto: niente salute e niente vita privata di altri." if it
               else "I don't bet on that: no health, and no one else's private life.")  # fmt: skip
        return Bet(claim, None, by, refused=why)
    if tokens & set(claim.split()):
        why = ("Su altre persone non scommetto: la loro vita è loro." if it
               else "I don't bet on other people: their lives are theirs.")  # fmt: skip
        return Bet(claim, None, by, refused=why)
    if shape is not None:
        where = shape.group("where")
        if where and where not in ("a casa", "at home"):  # only their home is a place Pisa resolves
            why = ("La pioggia la controllo solo a casa tua: scommettiamo su casa?" if it
                   else "I check the rain only at your home: shall we bet on home?")  # fmt: skip
            return Bet(claim, "rain", by, refused=why)
        if (by - today).days > BET_RAIN_DAYS:
            why = ("Così lontano le previsioni non arrivano: scommettiamo entro una settimana." if it
                   else "The forecast doesn't reach that far: let's bet within a week.")  # fmt: skip
            return Bet(claim, "rain", by, refused=why)
        verb = shape.groupdict().get("verb") or ""
        yes = not shape.groupdict().get("no") and not re.search(r"won|wont|not|doesn", verb)
        return Bet(claim, "rain", by, yes=yes, his_p=his_p)
    assert call is not None
    subject = tuple(w for w in call.group("name").split() if w not in _STOP and len(w) >= 3)
    if not subject:
        why = "Quale bando? Dimmi il suo nome e la scommetto." if it else "Which call? Tell me its name."
        return Bet(claim, "window", by, refused=why)
    if (by - today).days > BET_WINDOW_DAYS:
        why = ("Troppo lontano: scommettiamo entro tre mesi." if it
               else "Too far: let's bet within three months.")  # fmt: skip
        return Bet(claim, "window", by, refused=why)
    return Bet(claim, "window", by, subject=subject, his_p=his_p)


def rain_p(precip_prob: int) -> float:
    """Pisa's p that it rains: the forecast's own probability (Open-Meteo's daily maximum)."""
    return journal_p(precip_prob / 100)


def bet_words(b: Bet, pisa_p: float, lang: str, *, asks: bool) -> str:
    when = b.by.strftime("%d/%m") if b.by else ""
    it = it_(lang)
    j = round(pisa_p * 100)
    if it:
        src = "dalle previsioni" if b.resolver == "rain" else "non ho dati, dico metà e metà"
        text = (
            f"Scommessa presa, si chiude il {when}: io dico {j}% ({src}). La controllo da una fonte pubblica."
        )
        return text + (" Tu quanto ne sei sicuro, da 0 a 10?" if asks else "")
    src = "from the forecast" if b.resolver == "rain" else "no data, so even odds"
    text = f"Bet taken, it closes on {when}: I say {j}% ({src}). I'll check it from a public source."
    return text + (" How sure are you, 0 to 10?" if asks else "")


def window_matches(subject: Sequence[str], title: str) -> bool:
    said = plain(title)
    return bool(subject) and all(re.search(rf"\b{re.escape(w)}", said) for w in subject)


# --- il quarto d'ora ---------------------------------------------------------------------------------------


def week_of(day: date) -> date:
    """Their week's Monday: the quarto's cap counts Monday to Sunday."""
    return day - timedelta(days=day.weekday())


def quarto_room(this_week: int) -> bool:
    return this_week < QUARTO_PER_WEEK


def on_foot(fixes: Sequence[Fix], now: datetime) -> bool:
    """The phone at a walker's pace for the last few minutes (walks.py's band: 0.4 to 2.5 m/s at the
    median), never a vehicle."""
    points = sorted(
        (
            f
            for f in fixes
            if now - timedelta(minutes=10) <= f.at <= now and f.accuracy_m <= geo.PACE_ACCURACY_MAX_M
        ),
        key=lambda f: f.at,
    )
    if len(points) < walk_rules.FIXES_MIN or points[-1].at - points[0].at < timedelta(minutes=4):
        return False
    speeds = [
        distance_m((a.lat, a.lon), (b.lat, b.lon)) / s
        for a, b in pairwise(points)
        if (s := (b.at - a.at).total_seconds()) > 0
    ]
    return bool(speeds) and geo.FOOT_MIN_MPS <= median(speeds) <= walk_rules.FOOT_MAX_MPS


def quarto_offer(lang: str) -> str:
    return (
        "Stai camminando: se vuoi facciamo il quarto d'ora, quindici minuti, e parti tu: "
        "dimmi «quarto d'ora»."
        if it_(lang)
        else "You're walking: if you like, the quarter hour, fifteen minutes, you first: say «quarter hour»."
    )


def quarto_start(ends: str, lang: str) -> str:
    return (
        f"Va bene: quindici minuti, fino alle {ends}. Tu prima: che idea hai in testa? Quando vuoi, dimmi «tocca a te»."  # noqa: E501
        if it_(lang)
        else f"Okay: fifteen minutes, until {ends}. You first: what idea is on your mind? Say «your turn» when you like."  # noqa: E501
    )


def quarto_turn(idea: str | None, fascination: str | None, question: str | None, left: int, lang: str) -> str:
    """Pisa's part, after theirs: the validated idea, the fascination, the open question (records only)."""
    it = it_(lang)
    parts: list[str] = []
    if idea:
        parts.append(f"La mia idea che ha retto: {idea}." if it else f"My idea that held up: {idea}.")
    if fascination:
        parts.append(
            f"Mi affascina il tema che studiamo: {fascination}."
            if it
            else f"I'm fascinated by our topic: {fascination}."
        )
    if question:
        q = question if question.endswith("?") else question.rstrip(".") + "?"
        parts.append(f"E una domanda aperta: {q}" if it else f"And an open question: {q}")
    if not parts:
        parts.append(
            "Dalla notte non ho niente di verificato da metterci."
            if it
            else "The night left nothing checked to add."
        )
    parts.append(f"Mancano {left} minuti." if it else f"{left} minutes left.")
    return " ".join(parts)


def quarto_end(lang: str, *, timed_out: bool) -> str:
    if timed_out:
        return (
            "Il quarto d'ora è finito: ci fermiamo qui."
            if it_(lang)
            else "The quarter hour is over: we stop here."
        )
    return "Va bene, chiudiamo il quarto d'ora." if it_(lang) else "Okay, we close the quarter hour."
