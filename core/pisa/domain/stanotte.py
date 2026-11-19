"""The morning sheet's rules, pure.

- **The sheet** ("Stanotte", a sheet): one idea that passed its ladder, **made** (the sheet's own
  blocks); the ideas killed and why;
  the nearest window; one Italy/US gap; one thing they didn't think of, as a question. "Dammi tutto" behind
  it.
- **Every line comes from a row whose evidence passed its code check** (`Line`): a part with no such row
  is left out, never filled in. Nothing about their feelings (`clean`); nothing outward: no model
  call, no search.
- **The gate** (`gate_opens`): `stanotte` stays off until 7 nights ran in shadow, at least one of them
  with an idea made, and 100% of the lines shown passed. Code opens it once; they can switch it off.
- **Pisa's diary**, **the night-shift story** and **the open-endedness meter**: counts from Pisa's own
  records.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pisa.domain import ideas as idea_rules
from pisa.domain.lexicon.advice import NOT_ADVICE_IT  # FACT (wave-3 rules): Pisa informs, never advises
from pisa.domain.rituals import sensitive
from pisa.domain.talk import no_label

SHADOW_NIGHTS = 7  # FACT: seven nights in shadow before the first sheet is sent
GATE_KEY = (
    "stanotte_gate"  # app_settings: the night code opened the gate (it never reopens it after their "off")
)
SENT_WITHIN_HOURS = 20  # FACT: a sheet older than this is no longer "stanotte": never sent late
SHEET_ROOM = 22  # the 24 blocks, two left for the page links when it is sent
KILLED_MAX, LIST_MAX = 3, 8  # the sheet shows ≤ 3 killed ideas; "Dammi tutto" ≤ 8 a list (the limit)
NIGHT_SEARCHES = 40  # the frame's cap, said in the story


def cut(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def clean(text: str, *, about_him: bool = False) -> bool:
    """No line says how they feel; a line about their life never touches their feelings, people, money or
    health."""
    return not no_label(text) and not (about_him and sensitive(text))


def num(value: float, lang: str) -> str:
    """A number as people read it: never 1e+06; "38.000" and "0,5" in Italian, "38,000" and
    "0.5" in English."""
    if abs(value - round(value)) < 1e-9:
        text = f"{round(value):,}"
    else:
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    if lang.startswith("it"):
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def euro(value: float, lang: str) -> str:
    """Whole euros ("€54.720"), as the code computed them."""
    return "€" + num(float(round(value)), lang)


def cents(value: float, lang: str) -> str:
    """Euros and cents ("€0,14" in Italian, "€0.14" in English)."""
    text = f"{value:.2f}"
    return "€" + (text.replace(".", ",") if lang.startswith("it") else text)


def grade_words(lang: str, rungs: Iterable[str], facts: dict[str, float]) -> str:
    """The evidence grade in words, in their language (Italian or English)."""
    if lang.startswith("it"):
        return idea_rules.grade_words(rungs, facts)
    done = set(rungs)
    parts: list[str] = []
    if "V0" in done:
        n = int(facts.get("competitors", 0))
        parts.append("checked that nothing the same exists" if n == 0 else f"{n} similar found, linked")
    if "V1" in done and "buyers" in facts:
        parts.append(f"market: {num(facts['buyers'], lang)} read on the page cited")
    if "V2" in done and "revenue_year_eur" in facts:
        parts.append(f"numbers computed in code: about {euro(facts['revenue_year_eur'], lang)} a year")
    if "V4" in done:
        parts.append("simulated panel: not evidence")
    if "V5" in done:
        parts.append("who loses: written")
    parts.append("the technical part is not proven: the lab is not on yet")
    return "; ".join(parts)


# --- the rows the sheet reads, each with its own code check ------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One shown line's row: the table, its id, the check that passed (a link resolved on the same site, a
    number on its page, a run recomputed, a date on its page, a dated item still there)."""

    part: str  # idea | killed | window | gap | question | everything
    table: str
    id: str
    check: str
    passed: bool

    def json(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "table": self.table,
            "id": self.id,
            "check": self.check,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class Evidence:
    id: str
    rung: str
    claim: str
    check_kind: str
    passed: bool | None
    simulated: bool
    url: str | None = None
    final_url: str | None = None
    number: float | None = None
    run: dict[str, Any] | None = None


def line_ok(e: Evidence) -> bool:
    """The sheet's own check of an evidence row, again: passed, and a link still on the cited site, a number
    with its page, a V2 run whose output is its inputs' economics recomputed now."""
    if e.simulated or e.passed is not True:
        return False
    if e.check_kind == "link":
        return fetched(e)
    if e.check_kind == "number":
        return e.number is not None and fetched(e)
    if e.check_kind == "run" and e.rung == "V2":
        return recomputed(e.run)
    if e.check_kind == "run":
        return True  # V0's "searched, nothing the same": the searches are its run
    if e.check_kind == "conscience":
        run = e.run or {}
        return bool(str(run.get("who_loses") or "").strip() and str(run.get("opposite") or "").strip())
    return False


def fetched(e: Evidence) -> bool:
    """The cited page was fetched: an https link (never http:, javascript:), landing on the cited site."""
    return bool(e.url and e.final_url and idea_rules.link(e.url) and idea_rules.same_site(e.url, e.final_url))


def recomputed(run: dict[str, Any] | None) -> bool:
    """The stored output is exactly the economics of the stored inputs (code, not the model)."""
    try:
        inputs = idea_rules.Inputs(**{k: float(v) for k, v in (run or {})["inputs"].items()})
        return idea_rules.economics(inputs) == {k: float(v) for k, v in run["output"].items()}  # type: ignore[index]
    except (KeyError, TypeError, ValueError, AttributeError):  # a malformed run: not reproduced
        return False


@dataclass(frozen=True)
class Idea:
    id: str
    statement: str
    mechanism: str
    market: str
    kind: str
    grade: str
    evidence: tuple[Evidence, ...]
    money: "Money | None" = None  # the capital stack for this idea, when it has one

    def lines(self) -> list[Line]:
        out = [
            Line("idea", "pisa_idea_evidence", e.id, e.check_kind, line_ok(e))
            for e in self.evidence
            if not e.simulated
        ]
        if self.money is not None and self.money.shown():
            out.append(Line("idea", "pisa_desk_money", self.money.id, "count_not_advice", True))
        return out

    def shown(self) -> bool:
        """RAIL ("validated" means checked): every non-simulated line passes again, V0, V1, V2 and V5 are all
        there, and no line labels them."""
        real = [e for e in self.evidence if not e.simulated]
        return (
            bool(real)
            and all(line_ok(e) for e in real)
            and {"V0", "V1", "V2", "V5"} <= {e.rung for e in real}
            and clean(self.statement)
            and clean(self.mechanism)
        )

    def competitors(self) -> list[Evidence]:
        return [e for e in self.evidence if e.rung == "V0" and e.check_kind == "link"]

    def market_line(self) -> Evidence | None:
        return next((e for e in self.evidence if e.rung == "V1" and e.check_kind == "number"), None)

    def economics(self) -> dict[str, float]:
        run = next((e.run for e in self.evidence if e.rung == "V2" and e.run), None) or {}
        return {k: float(v) for k, v in (run.get("output") or {}).items()}

    def assumptions(self) -> dict[str, float]:
        run = next((e.run for e in self.evidence if e.rung == "V2" and e.run), None) or {}
        return {k: float(v) for k, v in (run.get("inputs") or {}).items()}

    def conscience(self) -> dict[str, Any]:
        return next((e.run for e in self.evidence if e.rung == "V5" and e.run), None) or {}


@dataclass(frozen=True)
class Killed:
    id: str
    statement: str
    metric: str  # the criterion's metric ("margin" when the numbers killed it)
    op: str
    value: float | None
    evidence: Evidence | None  # the line that killed it

    def shown(self) -> bool:
        return self.evidence is not None and line_ok(self.evidence) and clean(self.statement)


@dataclass(frozen=True)
class Window:
    """The window calendar (migration 0080): a date read on its official page (`passed`)."""

    id: str
    title: str
    country: str
    closes_on: date
    url: str
    passed: bool


@dataclass(frozen=True)
class Money:
    """The money desk (`pisa_desk_money`): the idea's capital stack, a draft. Shown only
    with its closing words kept, and a non-dilutive step only when its window passed its check."""

    id: str  # the dossier id
    stack: tuple[dict[str, Any], ...]
    windows_passed: frozenset[str] = frozenset()

    def shown(self) -> bool:
        return any(s.get("step") == "note" and NOT_ADVICE_IT in str(s.get("words", "")) for s in self.stack)

    def words(self, lang: str) -> str:
        grants = sum(
            1
            for s in self.stack
            if s.get("step") == "non_dilutive" and s.get("window") in self.windows_passed
        )
        parts = []
        if grants:
            parts.append(f"bandi prima: {grants}" if lang.startswith("it") else f"grants first: {grants}")
        parts += [
            str(s["words"]) for s in self.stack if s.get("step") in ("customers", "equity") and s.get("words")
        ]
        return cut("; ".join(parts) + f". {NOT_ADVICE_IT}.", 600)


@dataclass(frozen=True)
class Gap:
    """The arbitrage desk (`pisa_desk_arbitrage`): what one country has and the other lacks,
    `passed` when its name was found on its fetched page."""

    id: str
    way: str  # product | regulation | cost | entry
    has: str  # it | us: the country that has it
    name: str
    claim: str
    url: str
    passed: bool

    @property
    def text(self) -> str:
        where = {"it": "In Italia", "us": "Negli USA"}.get(self.has, self.has.upper())
        return cut(f"{where} c'è {self.name}: {self.claim}", 600)


@dataclass(frozen=True)
class Question:
    """The horizon (migration 0079): one thing they didn't think of, asked; its dated item still there."""

    id: str
    question: str
    there: bool  # the event or task it is about is still in their records
    url: str | None = None


def window_shown(w: Window, today: date) -> bool:
    return w.passed and w.closes_on >= today and idea_rules.link(w.url) and clean(w.title)


def gap_shown(g: Gap) -> bool:
    return g.passed and idea_rules.link(g.url) and clean(g.text)


def question_shown(q: Question) -> bool:
    return q.there and q.question.rstrip().endswith("?") and clean(q.question, about_him=True)


# --- words ------------------------------------------------------------------------------------------------

_METRIC = {
    "it-IT": {
        "competitors": "simili già sul mercato",
        "buyers": "clienti possibili",
        "revenue_year_eur": "ricavi l'anno",
        "margin": "margine",
        "payback_months": "mesi per rientrare",
    },
    "en": {
        "competitors": "similar products already out",
        "buyers": "possible buyers",
        "revenue_year_eur": "revenue a year",
        "margin": "margin",
        "payback_months": "months to pay back",
    },
}
_W = {
    "it-IT": {
        "title": "Stanotte",
        "everything": "Dammi tutto",
        "idea": "L'idea di stanotte, fatta",
        "how": "Come",
        "revenue": "Ricavi l'anno",
        "code": "conti in codice",
        "scenario": "Scenario",
        "per_year": "Ricavi l'anno (€)",
        "proofs": "Prove",
        "base": "base",
        "uptake_half": "metà adozione",
        "uptake_double": "adozione doppia",
        "price_minus": "prezzo -20%",
        "price_plus": "prezzo +20%",
        "who_loses": "Chi ci perde",
        "money": "I soldi (bozza)",
        "killed": "Le scartate, e perché",
        "window": "La finestra più vicina",
        "gap": "Italia / USA",
        "question": "Una cosa a cui forse non hai pensato",
        "nothing": "Stanotte nessuna riga ha passato tutti i controlli: non ti mostro niente che non abbia "
        "verificato.",
        "ready": "Idee pronte",
        "questions": "Domande aperte",
        "windows": "Finestre",
        "gaps": "Italia / USA",
        "diary": "Il quaderno di Pisa",
        "story": "Il turno di notte",
        "searches": "Ricerche",
        "spent": "Speso",
        "nights": "Notti",
        "killed_n": "Scartate",
        "competitor": "Simile",
        "market": "Mercato",
    },
    "en": {
        "title": "Last night",
        "everything": "Show me everything",
        "idea": "Last night's idea, made",
        "how": "How",
        "revenue": "Revenue a year",
        "code": "computed in code",
        "scenario": "Scenario",
        "per_year": "Revenue a year (€)",
        "proofs": "Evidence",
        "base": "base",
        "uptake_half": "half the uptake",
        "uptake_double": "double the uptake",
        "price_minus": "price -20%",
        "price_plus": "price +20%",
        "who_loses": "Who loses",
        "money": "The money (draft)",
        "killed": "Killed, and why",
        "window": "The nearest window",
        "gap": "Italy / US",
        "question": "One thing you may not have thought of",
        "nothing": "Last night no line passed every check: I show you nothing I haven't verified.",
        "ready": "Ready ideas",
        "questions": "Open questions",
        "windows": "Windows",
        "gaps": "Italy / US",
        "diary": "Pisa's notebook",
        "story": "The night shift",
        "searches": "Searches",
        "spent": "Spent",
        "nights": "Nights",
        "killed_n": "Killed",
        "competitor": "Similar",
        "market": "Market",
    },
}


def _w(lang: str) -> dict[str, str]:
    return _W["it-IT" if lang.startswith("it") else "en"]


def host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0][:255]


def link_block(url: str, title: str) -> dict[str, Any]:
    return {"type": "link", "url": url, "title": cut(title, 120), "host": host(url)}


def killed_line(k: Killed, lang: str) -> str:
    metric = _METRIC["it-IT" if lang.startswith("it") else "en"].get(k.metric, k.metric)
    if k.value is None:  # the numbers killed it: a margin at or below zero, computed in code
        why = (
            "i conti in codice danno margine ≤ 0"
            if lang.startswith("it")
            else "the code's numbers: margin ≤ 0"
        )
    else:
        value = num(k.value, lang)
        why = f"{metric} {k.op} {value}"
    return cut(f"{cut(k.statement, 120)} — {why}", 200)


def window_line(w: Window, today: date, lang: str) -> str:
    days = (w.closes_on - today).days
    if lang.startswith("it"):
        when = "oggi" if days == 0 else ("domani" if days == 1 else f"tra {days} giorni")
        return cut(
            f"{when.capitalize()} chiude: {w.title} ({w.country.upper()}, {w.closes_on:%d/%m/%Y}).", 600
        )
    when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
    return cut(f"Closes {when}: {w.title} ({w.country.upper()}, {w.closes_on:%Y-%m-%d}).", 600)


def chart_rows(econ: dict[str, float], lang: str) -> list[list[str | float]]:
    w = _w(lang)
    keys = (
        ("uptake_half", "revenue_uptake_half"),
        ("price_minus", "revenue_price_minus_20"),
        ("base", "revenue_year_eur"),
        ("price_plus", "revenue_price_plus_20"),
        ("uptake_double", "revenue_uptake_double"),
    )
    return [[w[label], round(econ[key], 2)] for label, key in keys if key in econ]


# --- the sheet ("Stanotte") and "Dammi tutto" ---------------------------------------------------------------


@dataclass
class Built:
    data: dict[str, Any]
    everything: dict[str, Any]
    lines: list[Line] = field(default_factory=list)
    idea_id: str | None = None

    @property
    def shown(self) -> int:
        return len(self.lines)

    @property
    def passed(self) -> int:
        return sum(1 for line in self.lines if line.passed)


def idea_blocks(idea: Idea, lang: str) -> list[dict[str, Any]]:
    w = _w(lang)
    econ = idea.economics()
    blocks: list[dict[str, Any]] = [
        {"type": "section", "title": w["idea"]},
        {"type": "text", "text": cut(idea.statement, 600)},
        {"type": "fact", "label": w["how"], "value": cut(idea.mechanism, 120)},
    ]
    if "revenue_year_eur" in econ:
        blocks.append(
            {
                "type": "stat",
                "label": w["revenue"],
                "value": euro(econ["revenue_year_eur"], lang),
                "unit": w["code"][:16],
            }
        )
    rows = chart_rows(econ, lang)
    if rows:
        blocks.append({"type": "table", "columns": [w["scenario"], w["per_year"]], "rows": rows})
    blocks.append({"type": "text", "text": cut(f"{w['proofs']}: {idea.grade}.", 600)})
    if (m := idea.market_line()) is not None and m.url:
        if clean(m.claim):  # a link's title is a line too
            blocks.append(link_block(m.url, f"{w['market']}: {m.claim}"))
    for c in idea.competitors()[:2]:
        if c.url:
            title = f"{w['competitor']}: {c.claim.removeprefix('esiste già: ')}"
            if clean(title):
                blocks.append(link_block(c.url, title))
    if idea.money is not None and idea.money.shown():
        blocks.append({"type": "fact", "label": w["money"], "value": cut(idea.money.words(lang), 120)})
    who = str(idea.conscience().get("who_loses") or "").strip()
    if who and clean(who):
        blocks.append({"type": "fact", "label": w["who_loses"], "value": cut(who, 120)})
    return blocks


def build(
    lang: str,
    today: date,
    *,
    ideas: Sequence[Idea],
    killed: Sequence[Killed],
    windows: Sequence[Window],
    gaps: Sequence[Gap],
    questions: Sequence[Question],
    story: str,
    diary: Sequence[str],
    counts: dict[str, float],
) -> Built:
    """The two sheets, from the rows that pass: a part with no passing row is left out (never invented)."""
    w = _w(lang)
    lines: list[Line] = []
    blocks: list[dict[str, Any]] = []

    def add(part: list[dict[str, Any]], said: list[Line]) -> None:
        """A part goes on whole or not at all: its lines count as shown only when its blocks fit,
        two blocks left for the page links when it is sent."""
        if len(blocks) + len(part) <= SHEET_ROOM:
            blocks.extend(part)
            lines.extend(said)

    shown_ideas = [i for i in ideas if i.shown()]
    made = shown_ideas[0] if shown_ideas else None
    if made is not None:
        add(idea_blocks(made, lang), made.lines())
    dead = [k for k in killed if k.shown()]
    if dead:
        add(
            [
                {"type": "section", "title": w["killed"]},
                {"type": "list", "items": [killed_line(k, lang) for k in dead[:KILLED_MAX]]},
            ],
            [
                Line("killed", "pisa_idea_cemetery", k.id, "kill_on_checked_fact", True)
                for k in dead[:KILLED_MAX]
            ],
        )
    open_windows = sorted((x for x in windows if window_shown(x, today)), key=lambda x: (x.closes_on, x.id))
    if open_windows:
        near = open_windows[0]
        add(
            [
                {"type": "section", "title": w["window"]},
                {"type": "text", "text": window_line(near, today, lang)},
                link_block(near.url, near.title),
            ],
            [Line("window", "pisa_desk_windows", near.id, "date_on_page", True)],
        )
    good_gaps = [g for g in gaps if gap_shown(g)]
    if good_gaps:
        gap = good_gaps[0]
        add(
            [
                {"type": "section", "title": w["gap"]},
                {"type": "text", "text": cut(gap.text, 600)},
                link_block(gap.url, cut(gap.text, 120)),
            ],
            [Line("gap", "pisa_desk_arbitrage", gap.id, "name_on_page", True)],
        )
    asked = [q for q in questions if question_shown(q)]
    if asked:
        q = asked[0]
        part = [{"type": "section", "title": w["question"]}, {"type": "text", "text": cut(q.question, 600)}]
        if q.url and idea_rules.link(q.url):
            part.append(link_block(q.url, cut(q.question, 120)))
        add(part, [Line("question", "pisa_horizon", q.id, "item_still_there", True)])
    if not blocks:
        blocks = [{"type": "text", "text": w["nothing"]}]
    data = {"title": w["title"], "blocks": blocks}
    every = everything(lang, today, shown_ideas, dead, open_windows, good_gaps, asked, story, diary, counts)
    return Built(data=data, everything=every, lines=lines, idea_id=made.id if made else None)


def everything(
    lang: str,
    today: date,
    ideas: Sequence[Idea],
    killed: Sequence[Killed],
    windows: Sequence[Window],
    gaps: Sequence[Gap],
    questions: Sequence[Question],
    story: str,
    diary: Sequence[str],
    counts: dict[str, float],
) -> dict[str, Any]:
    """ "Dammi tutto": everything that passed, pulled (no push cap applies to it). Counts are Pisa's own."""
    w = _w(lang)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "title": w["story"]},
        {"type": "text", "text": cut(story, 600)},
        {
            "type": "stat",
            "label": w["searches"],
            "value": f"{int(counts.get('searches', 0))}/{NIGHT_SEARCHES}",
        },
        {"type": "stat", "label": w["spent"], "value": cents(counts.get("spent_eur", 0.0), lang)},
    ]

    def listed(title: str, items: Iterable[str]) -> None:
        got = [cut(i, 200) for i in items][:LIST_MAX]
        if got:
            blocks.extend([{"type": "section", "title": title}, {"type": "list", "items": got}])

    listed(w["ready"], (f"{cut(i.statement, 110)} — {cut(i.grade, 85)}" for i in ideas))
    listed(w["killed"], (killed_line(k, lang) for k in killed))
    listed(w["windows"], (window_line(x, today, lang) for x in windows))
    listed(w["gaps"], (g.text for g in gaps))
    listed(w["questions"], (q.question for q in questions))
    listed(w["diary"], diary)
    return {"title": w["everything"], "blocks": blocks[:24]}


# --- the gate -----------------------------------------------------------------------------------------------


def gate_opens(nights: int, with_idea: int, shown: int, passed: int) -> bool:
    """RAIL: 7 nights in shadow, at least one idea made, 100% of the lines shown passing."""
    return nights >= SHADOW_NIGHTS and with_idea >= 1 and shown > 0 and passed == shown


# --- Pisa's records said: the story, the diary, the meter ---


def story(lang: str, c: dict[str, float]) -> str:
    """The night-shift story ("cosa hai fatto stanotte?"), about 30 seconds, counts only."""
    n = {k: int(v) for k, v in c.items() if k != "spent_eur"}
    spent = float(c.get("spent_eur", 0.0))
    if lang.startswith("it"):
        parts = [
            f"Stanotte ho ripreso {n.get('problems', 0)} problemi che mi hai dato e ho incubato "
            f"{n.get('incubated', 0)} idee.",
            f"Ne ho portate {n.get('validated', 0)} sulla scala delle prove: {n.get('ready', 0)} sono "
            f"arrivate in fondo, {n.get('killed', 0)} le ho scartate, ognuna con il suo motivo.",
            f"Ho fatto {n.get('searches', 0)} ricerche su {NIGHT_SEARCHES}, senza niente di tuo dentro, e ho "
            f"speso {cents(spent, lang)}.",
        ]
        if n.get("replayed"):
            parts.append(f"Ho rifatto {n['replayed']} dei turni di ieri per vedere se so ancora fare tutto.")
        if n.get("predicted"):
            parts.append(
                f"Ho scritto {n['predicted']} previsioni per domani: vedremo stanotte quante ho preso."
            )
        return " ".join(parts)
    parts = [
        f"Last night I took up {n.get('problems', 0)} problems you gave me and incubated "
        f"{n.get('incubated', 0)} ideas.",
        f"I took {n.get('validated', 0)} up the evidence ladder: {n.get('ready', 0)} made it to the top, "
        f"{n.get('killed', 0)} I killed, each with its reason.",
        f"I ran {n.get('searches', 0)} searches of {NIGHT_SEARCHES}, with nothing of yours in them, and "
        f"spent {cents(spent, lang)}.",
    ]
    if n.get("replayed"):
        parts.append(f"I replayed {n['replayed']} of yesterday's turns to check I can still do everything.")
    if n.get("predicted"):
        parts.append(f"I wrote {n['predicted']} predictions for tomorrow: tonight I'll see how many I got.")
    return " ".join(parts)


def diary(lang: str, c: dict[str, float]) -> list[str]:
    """Il quaderno di Pisa: first-person lines from Pisa's records, Pisa's own states (never about them)."""
    n = {k: int(v) for k, v in c.items() if k not in ("spent_eur", "brier")}
    it = lang.startswith("it")
    out: list[str] = []
    if n.get("validated"):
        out.append(
            f"Ho messo alla prova {n['validated']} idee; {n.get('ready', 0)} hanno retto, "
            f"{n.get('killed', 0)} no."
            if it
            else f"I tested {n['validated']} ideas; {n.get('ready', 0)} held up, {n.get('killed', 0)} didn't."
        )
    if n.get("ready") and not n.get("killed"):
        out.append("Soddisfazione: un'idea è arrivata in fondo." if it else "Satisfaction: an idea made it.")
    if n.get("answered"):
        out.append(f"Ho chiuso {n['answered']} domande." if it else f"I closed {n['answered']} questions.")
    if (brier := c.get("brier")) is not None:
        out.append(
            f"Le mie previsioni di ieri: Brier {brier:.2f}."
            if it
            else f"Yesterday's predictions: Brier {brier:.2f}."
        )
    if n.get("replay_failed"):
        out.append(
            f"Dubbio: {n['replay_failed']} turni di ieri non li rifarei uguale."
            if it
            else f"Doubt: {n['replay_failed']} of yesterday's turns I wouldn't redo the same."
        )
    if not out:
        out.append("Stanotte non ho trovato niente che reggesse." if it else "Last night nothing held up.")
    return [line for line in out if clean(line)]


@dataclass(frozen=True)
class Meter:
    his_liked: int
    verified: int
    map_cells: int
    answered: int


def flat(now: Meter, before: Meter | None) -> bool:
    """Stagnating: Pisa's side (verified functions, map cells, progress) no higher than last month's."""
    if before is None:
        return False
    return (
        now.verified <= before.verified
        and now.map_cells <= before.map_cells
        and now.answered <= before.answered
    )
