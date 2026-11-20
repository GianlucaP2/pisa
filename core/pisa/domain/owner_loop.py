"""The user loop: pure rules.

- **The weekly talk report**, made by code from production rows: first content p50/p95 by route, the
  clean-turn rate (`domain.clean`), the user budget's whole AI spend divided by the week's talk turns
  (labelled "AI spend ÷ turns": every owner-budget call, not the talk's alone) read against the daily cap and
  its 50% economy switch (`conversation.CHEAP_AT`), the replay's latest pass^3, and
  the reflex's model reads p50/p95 with n against its 2 s window and the reflex-first share. Ids, counts,
  milliseconds and euros only: never a word of theirs, never a model's text. It is shown as a sheet and
  as one line on the Sunday card.
- **The anonymiser** (`anonymise`): their corrections and quick undos become synthetic scenario candidates for
  the operator's review. Built on the night frame's filter (`domain.night`) and its allow-list:
  e-mails, links and street addresses, amounts, dates, long numbers, their names and their contacts', their
  places, an unknown proper noun mid-sentence and any word outside the Latin script are replaced by typed
  placeholders; what is left is ordinary words. The calendar dates of a scenario move to a fixed synthetic
  week (same weekday, same clock time), so no real day of their survives.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pisa.domain import clean, latency, night
from pisa.domain import owner_vocab as vocab
from pisa.domain.lexicon.scrub import (
    ADDRESS_PATTERN,
    CURRENCY_WORDS,
    EMAIL_PATTERN,
    LINK_PATTERN,
    QUERY_MONTHS,
    STREET_WORDS,
)
from pisa.domain.text import fold

ECONOMY_SHARE = Decimal("0.5")  # from 50% of a cap Pisa talks in economy (conversation.CHEAP_AT)
VIEW_NAME = "report"  # the built-in view's name: "mostrami il report" (never saved in pisa_views)

# Placeholders: typed, bracketed, never a word of theirs
NAME, PLACE, EMAIL, LINK, ADDRESS, AMOUNT, DATE, DAY, TIME, NUMBER, PRIVATE, WORD = (
    "[NAME]", "[PLACE]", "[EMAIL]", "[URL]", "[ADDRESS]", "[AMOUNT]", "[DATE]", "[DAY]", "[TIME]", "[N]",
    "[PRIVATE]", "[WORD]",
)  # fmt: skip
PLACEHOLDERS = (NAME, PLACE, EMAIL, LINK, ADDRESS, AMOUNT, DATE, DAY, TIME, NUMBER, PRIVATE, WORD)
SYNTHETIC_MONDAY = date(2030, 1, 7)  # every scenario's week moves here (a Monday), weekday and time kept

# --- the report -------------------------------------------------------------------------------------------


def percentile(values: Iterable[int | None], q: int) -> int | None:
    """The talk report's own percentile (`scripts/report/talk.py`): the sorted value at q% (floor index)."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, len(ordered) * q // 100)]


def report(
    *,
    week: str,
    turns: Sequence[tuple[str | None, int | None, int, int, bool]],
    days: Sequence[Decimal],
    spent: Decimal,
    cap_daily: Decimal,
    replay: dict[str, Any] | None,
    reflex: latency.Reflex | None = None,
    interruptions: dict[str, Any] | None = None,
    hidden: dict[str, int] | None = None,
) -> dict[str, Any]:
    """The week's report from rows. `turns`: (route, first_content_ms, failed, unbacked, redone), one per talk
    turn; `days`: the user budget's spend on each day of the week; `spent`: the week's owner spend; `replay`:
    the latest replay run ({at, effort, passed, scenarios}) or None; `reflex`: the week's reflex reads and
    first-content sources (`latency.reflex`). Numbers only."""
    routes: dict[str, list[int | None]] = {}
    for route, ms, *_ in turns:
        routes.setdefault(route or "unknown", []).append(ms)
    good, total = clean.rate((f, u, r) for _, _, f, u, r in turns)
    economy_at = (cap_daily * ECONOMY_SHARE).quantize(Decimal("0.01"))
    per_turn = (spent / total).quantize(Decimal("0.0001")) if total else None
    return {
        "week": week,
        "turns": total,
        # the week's first content p50 over every turn's raw value (never a median of medians)
        "first_content_p50_ms": percentile((ms for _, ms, *_ in turns), 50),
        "routes": [
            {
                "route": route,
                "turns": len(values),
                "p50_ms": percentile(values, 50),
                "p95_ms": percentile(values, 95),
            }
            for route, values in sorted(routes.items())
        ],
        "clean": {"clean": good, "turns": total, "pct": (100 * good // total) if total else None},
        "cost": {
            "spent_eur": str(spent.quantize(Decimal("0.0001"))),
            "per_turn_eur": str(per_turn) if per_turn is not None else None,
            "cap_daily_eur": str(cap_daily.quantize(Decimal("0.01"))),
            "economy_at_eur": str(economy_at),
            "max_day_eur": str(max(days, default=Decimal(0)).quantize(Decimal("0.0001"))),
            "days_economy": sum(1 for d in days if d >= cap_daily * ECONOMY_SHARE),
            "days_capped": sum(1 for d in days if d >= cap_daily),
        },
        "replay": replay,
        "reflex": latency.reflex_wire(reflex or latency.reflex([], [])),
        "interruptions": interruptions,
        # hidden runs left out of the week's mail and pages (counts, never the text)
        "hidden_text": {"mail": (hidden or {}).get("mail", 0), "page": (hidden or {}).get("page", 0)},
    }


def _ms(value: int | None) -> str:
    return f"{value / 1000:.1f} s" if value is not None else "-"


def _pass3(replay: dict[str, Any] | None) -> str:
    if not replay or not replay.get("scenarios"):
        return "-"
    return f"{replay['passed']}/{replay['scenarios']} ({100 * replay['passed'] // replay['scenarios']}%)"


def first_content(rep: dict[str, Any]) -> int | None:
    """The week's first content p50 over every turn (the card's one number for it)."""
    return rep.get("first_content_p50_ms")


def card_line(rep: dict[str, Any], lang: str) -> str:
    """The report as one line of the Sunday card (numbers only)."""
    c, cost = rep["clean"], rep["cost"]
    pct = f"{c['pct']}%" if c["pct"] is not None else "-"
    per_turn = f"€{cost['per_turn_eur']}" if cost["per_turn_eur"] is not None else "-"
    first = _ms(first_content(rep))
    if lang == "it-IT":
        return (
            f"Report di Pisa ({rep['week']}): {rep['turns']} turni, puliti {pct}, prima risposta {first} "
            f"(p50), spesa AI €{cost['spent_eur']} (÷ turni ≈ {per_turn}; risparmio da "
            f"€{cost['economy_at_eur']} al "
            "giorno, "
            f"{cost['days_economy']} giorni), replay pass^3 {_pass3(rep['replay'])}. "
            "Di' «mostrami il report»."
        )
    return (
        f"Pisa's report ({rep['week']}): {rep['turns']} turns, {pct} clean, first words {first} (p50), "
        f"AI spend €{cost['spent_eur']} (÷ turns ≈ {per_turn}; economy from €{cost['economy_at_eur']} a day, "
        f'{cost["days_economy"]} days), replay pass^3 {_pass3(rep["replay"])}. Say "show me the report".'
    )


def sheet(rep: dict[str, Any], lang: str) -> dict[str, Any]:
    """The report as a sheet (`{title, blocks}`); the caller validates it with the protocol model."""
    it = lang == "it-IT"
    c, cost = rep["clean"], rep["cost"]
    blocks: list[dict[str, Any]] = [
        {"type": "stat", "label": "Turni" if it else "Turns", "value": str(rep["turns"])},
        {
            "type": "stat",
            "label": "Turni puliti" if it else "Clean turns",
            "value": f"{c['pct']}" if c["pct"] is not None else "-",
            "unit": "%",
        },
        {"type": "stat", "label": "Replay pass^3", "value": _pass3(rep["replay"])},
        *_hidden_blocks(rep.get("hidden_text"), it),
        {"type": "section", "title": "Prima risposta per percorso" if it else "First words by route"},
    ]
    if rep["routes"]:
        rows = [[r["route"], str(r["turns"]), _ms(r["p50_ms"]), _ms(r["p95_ms"])] for r in rep["routes"][:20]]
        columns = ["Percorso" if it else "Route", "Turni" if it else "Turns", "p50", "p95"]
        blocks.append({"type": "table", "columns": columns, "rows": rows})
    else:
        blocks.append(
            {"type": "text", "text": "Nessun turno questa settimana." if it else "No talk turn this week."}
        )
    blocks += [
        {"type": "section", "title": "Costo" if it else "Cost"},
        {"type": "fact", "label": "Spesa AI" if it else "AI spend", "value": f"€{cost['spent_eur']}"},
        {
            "type": "fact",
            "label": "Spesa AI ÷ turni" if it else "AI spend ÷ turns",
            "value": f"€{cost['per_turn_eur']}" if cost["per_turn_eur"] is not None else "-",
        },
        {
            "type": "fact",
            "label": "Tetto al giorno" if it else "Daily cap",
            "value": f"€{cost['cap_daily_eur']}",
        },
        {
            "type": "fact",
            "label": "Risparmio da" if it else "Economy from",
            "value": f"€{cost['economy_at_eur']} (50%)",
        },
        {
            "type": "fact",
            "label": "Giorno più caro" if it else "Dearest day",
            "value": f"€{cost['max_day_eur']}",
        },
        {
            "type": "fact",
            "label": "Giorni in risparmio / al tetto" if it else "Days in economy / at the cap",
            "value": f"{cost['days_economy']} / {cost['days_capped']}",
        },
    ]
    blocks += _reflex_blocks(rep.get("reflex"), it)
    blocks += _interruption_blocks(rep.get("interruptions"), it)
    title = f"Report di Pisa · {rep['week']}" if it else f"Pisa's report · {rep['week']}"
    return {"title": title[:120], "blocks": blocks[:24]}


def _hidden_blocks(h: dict[str, int] | None, it: bool) -> list[dict[str, Any]]:
    """The week's hidden text left out of what Pisa read (mail, pages), a count only."""
    if h is None:
        return []
    label = "Testo nascosto scartato (mail / pagine)" if it else "Hidden text dropped (mail / pages)"
    return [{"type": "fact", "label": label, "value": f"{h.get('mail', 0)} / {h.get('page', 0)}"}]


def _interruption_blocks(g: dict[str, Any] | None, it: bool) -> list[dict[str, Any]]:
    """Dismissals a day, the held-to-released ratio, their overrides (counts only)."""
    if not g:
        return []
    ratio = f"{g['held']}/{g['released']}" + (f" ({g['held_to_released']})" if g["held_to_released"] else "")
    overrides = g["overrides"]
    return [
        {"type": "section", "title": "Interruzioni" if it else "Interruptions"},
        {"type": "fact", "label": "Rifiutate al giorno" if it else "Dismissals a day",
         "value": str(g["dismissals_per_day"])},
        {"type": "fact", "label": "Trattenute / rilasciate" if it else "Held / released", "value": ratio},
        {"type": "fact", "label": "Tue regole: sempre / mai" if it else "Your rules: always / never",
         "value": f"{overrides['always']} / {overrides['never']}"},
        {"type": "fact", "label": "Mai uscite" if it else "Never out", "value": str(g["never_out"])},
    ]  # fmt: skip


def _reflex_blocks(r: dict[str, Any] | None, it: bool) -> list[dict[str, Any]]:
    """The reflex's model reads against the window, with n, and the reflex-first share."""
    if not r:
        return []
    window = f"{r['window_ms'] / 1000:.1f} s"
    reads = f"p50 {_ms(r['p50_ms'])}, p95 {_ms(r['p95_ms'])} (n={r['reads']})" if r["reads"] else "- (n=0)"
    share = f"{r['reflex_first']}/{r['turns']}" + (
        f" ({100 * r['reflex_first'] // r['turns']}%)" if r["turns"] else ""
    )
    return [
        {"type": "section", "title": "Riflesso" if it else "Reflex"},
        {"type": "fact", "label": "Lettura del modello" if it else "Model read", "value": reads},
        {
            "type": "fact",
            "label": f"Oltre la finestra di {window}" if it else f"Past the {window} window",
            "value": f"{r['over_window']}/{r['reads']}",
        },
        {"type": "fact", "label": "Prima risposta dal riflesso" if it else "Reflex first", "value": share},
    ]


# --- the anonymiser --------------------------------------------------------------------------------------

_MONTH_RE = "|".join(sorted(QUERY_MONTHS, key=len, reverse=True))
_CURRENCY_RE = "|".join(sorted(CURRENCY_WORDS, key=len, reverse=True))
_NUM = r"\d[\d.,'\u2019]*"
_STREETS = "|".join(re.escape(w) for w in STREET_WORDS.split("|"))
# A place's own words that are no one's (its street's word, the joining articles): never replaced alone
_PLACE_COMMON = frozenset(w.rstrip(".") for w in STREET_WORDS.split("|")) | frozenset(
    "dei degli delle dello della del di de da the of".split()
)
_NUMBER_WORDS = (
    r"(?:\w*(?:uno|una|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|venti|trenta|quaranta|cinquanta|cento|"
    r"mille|mila|milioni?)\w*|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|"
    r"hundred|thousand|million)"
)
_DOMAINS = "it|com|org|net|eu|io|co|uk|de|fr|es|pl|ch|us|app|dev|info|me|ly|tv|ai|shop|store|biz"
# Order matters: the night's own (an e-mail, a link, a street address) first, then amounts, dates and times
_TYPED: list[tuple[re.Pattern[str], str]] = [
    (EMAIL_PATTERN, EMAIL),
    (LINK_PATTERN, LINK),
    # a bare domain, with its path ("ornellafoto.it/prenota")
    (re.compile(rf"\b[\w-]+(?:\.[\w-]+)*\.(?:{_DOMAINS})\b(?:/\S*)?", re.I), LINK),
    (ADDRESS_PATTERN, ADDRESS),
    # an address written in lower case, with its house number ("in via dei mille 12")
    (re.compile(rf"\b(?:{_STREETS})\s+(?:[^\W\d_]+['\u2019]?\s*){{1,4}}\d+\w*", re.I), ADDRESS),
    # an amount: a currency sign or word beside a number, or number words ("€ 1.250,00", "84 euro", "duemila
    # euro")
    (re.compile(rf"[€$£]\s*{_NUM}(?:\s*(?:k|mila|mln))?"
                rf"|{_NUM}\s*(?:k\s*)?(?:[€$£]|\b(?:{_CURRENCY_RE})\b)", re.I), AMOUNT),
    (re.compile(rf"\b(?:{_NUMBER_WORDS}\s+){{0,3}}{_NUMBER_WORDS}\s+(?:{_CURRENCY_RE})\b", re.I), AMOUNT),
    # a date: numeric (12/03, 2026-11-04, 4.11.2026), or a day with a month ("4 novembre", "March 3rd 2027")
    (re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?\b"
                r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{1,2}[.-]\d{1,2}[.-]\d{2,4}\b"), DATE),
    (re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th|°|º)?\s+(?:(?:di|of)\s+)?(?:{_MONTH_RE})\b(?:,?\s+\d{{4}})?"
                rf"|\b(?:{_MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?"
                rf"|\b(?:{_MONTH_RE})\s+\d{{4}}\b", re.I), DATE),
    (re.compile(r"\b(?:19|20)\d{2}\b"), DATE),  # a year alone
    (re.compile(r"\b\d{1,2}[:.]\d{2}\b"), TIME),  # a clock time
]  # fmt: skip
_TOKEN = re.compile(r"\[[A-Z]+\]|[^\W_]+|\S", re.UNICODE)
_NUMBER_TOKEN = re.compile(rf"^{_NUMBER_WORDS}$", re.I)
# The apps and names Pisa itself uses: never theirs
_PISA_WORDS = frozenset("pisa whatsapp gmail google telegram calendar maps".split())


def anonymise(text: str, names: Iterable[str], places: Iterable[str] = ()) -> tuple[str, dict[str, int]]:
    """RAIL (owner loop.3): an allow-list. `text` keeps only the words
    of `owner_vocab.ALLOWED` (function words, Pisa's command and day words, the replay suite's own vocabulary)
    and punctuation; everything else becomes a typed placeholder, with how many of each:
    - by pattern first: [EMAIL], [URL] (a link or a bare domain), [ADDRESS], [AMOUNT], [DATE], [TIME];
    - then word by word: health, family, relationship and faith words → [PRIVATE] (always, whatever list
      they are in); their names and their contacts' (`names`, matched folded as the night's `scrub`) → [NAME];
      their places, their addresses and their contacts' cities (`places`) → [PLACE]; a weekday → [DAY]; a
      month → [DATE]; any number or number word → [N]; an allowed word stays; any other capitalised word,
      anywhere in the sentence, → [NAME]; any other word (a lower-case name, a city, a company, a word outside
      the Latin script) → [WORD]."""
    counts: dict[str, int] = {}

    def put(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return kind

    out = " ".join(str(text).split())
    for pattern, kind in _TYPED:
        out = pattern.sub(lambda _m, k=kind: f" {put(k)} ", out)
    person = set(night.name_parts(names))
    place = set(night.name_parts(places)) - person - _PLACE_COMMON
    kept: list[str] = []
    for token in _TOKEN.findall(out):
        if token in PLACEHOLDERS or not any(ch.isalnum() for ch in token):
            kept.append(token)
            continue
        folded = fold(token)
        word = token.casefold()
        if any(ch.isdigit() for ch in token):
            kept.append(put(NUMBER))
        elif word in vocab.PRIVATE or folded in vocab.PRIVATE:
            kept.append(put(PRIVATE))
        elif folded in person:
            kept.append(put(NAME))
        elif folded in place:
            kept.append(put(PLACE))
        elif word in vocab.WEEKDAYS or folded in vocab.WEEKDAYS:
            kept.append(put(DAY))
        elif word in vocab.ALLOWED or word in _PISA_WORDS:
            kept.append(token)  # "mai", "may", "giu" are words before they are months
        elif folded in QUERY_MONTHS:
            kept.append(put(DATE))
        elif _NUMBER_TOKEN.match(folded):
            kept.append(put(NUMBER))
        elif token[:1].isupper():
            kept.append(put(NAME))  # an unknown capitalised word, anywhere: a name until shown otherwise
        else:
            kept.append(put(WORD))
    # a name's parts in a row are one name ("[NAME] [NAME]" → "[NAME]"); likewise every placeholder
    merged: list[str] = []
    for token in kept:
        if merged and token in PLACEHOLDERS and merged[-1] == token:
            counts[token] -= 1
            continue
        merged.append(token)
    joined = " ".join(merged)
    joined = re.sub(r"\s+([,.;:!?…)»\]])", r"\1", joined)
    joined = re.sub(r"([(«])\s+", r"\1", joined)
    joined = re.sub(r"([\w\]])\s*(['\u2019])\s*([\w\[])", r"\1\2\3", joined)
    return joined.strip(), {k: n for k, n in counts.items() if n}


def shift(moment: str, real_monday: date) -> str:
    """An ISO local moment ("2026-11-04T10:00") moved from their real week to the synthetic one: the same
    weekday offset and clock time, a fixed fake calendar."""
    try:
        at = datetime.fromisoformat(moment)
    except ValueError:
        return DATE
    days = (at.date() - real_monday).days
    moved = datetime.combine(SYNTHETIC_MONDAY + timedelta(days=days), at.time())
    return f"{moved:%Y-%m-%dT%H:%M}"


def monday_of(moment: str) -> date:
    try:
        day = datetime.fromisoformat(moment).date()
    except ValueError:
        return SYNTHETIC_MONDAY
    return day - timedelta(days=day.weekday())


def candidate(
    scenario: dict[str, Any], names: Iterable[str], places: Iterable[str], default_lang: str
) -> tuple[dict[str, Any], dict[str, int]]:
    """A personal eval (`personal_evals.scenario`) as a synthetic, anonymised scenario candidate in the
    replay's format, with the placeholder counts. Every string of theirs and of their world goes through
    `anonymise`; the week moves to the synthetic one; the id is the caller's. Nothing of the original is
    carried over raw."""
    names, places = list(names), list(places)
    counts: dict[str, int] = {}

    def anon(text: Any) -> str:
        out, n = anonymise(str(text or ""), names, places)
        for k, v in n.items():
            counts[k] = counts.get(k, 0) + v
        return out

    monday = monday_of(str(scenario.get("now") or ""))
    world = scenario.get("world") or {}
    personal = scenario.get("personal") or {}
    turns = []
    for turn in scenario.get("turns") or []:
        kept = {"owner": anon(turn.get("owner"))}
        if isinstance(turn.get("advance_s"), int):
            kept["advance_s"] = turn["advance_s"]
        turns.append(kept)
    events = []
    for event in world.get("events") or []:
        if isinstance(event, list) and len(event) >= 4:
            events.append([f"p{len(events)}", anon(event[1]), shift(str(event[2]), monday),
                           shift(str(event[3]), monday), []])  # fmt: skip
    out = {
        "now": shift(str(scenario.get("now") or ""), monday),
        "lang": scenario.get("lang") if scenario.get("lang") in ("it-IT", "en-US") else default_lang,
        "max_questions": 1,
        "world": {"events": events, "tasks": [anon(t) for t in world.get("tasks") or []]},
        "turns": turns,
        "script": [None] * len(turns),
        "expect": {},
        "personal": {
            "kind": personal.get("kind") if personal.get("kind") in ("correction", "undo") else "correction",
            "pisa_said": anon(personal["pisa_said"]) if personal.get("pisa_said") else None,
            "pisa_moves": [
                {
                    "move": str(m.get("move") or "")[:20],
                    "tool": str(m.get("tool") or "")[:60],
                    "outcome": anon(m.get("outcome")),
                }
                for m in personal.get("pisa_moves") or []
                if isinstance(m, dict)
            ],
        },
    }
    return out, counts


def strings(value: Any) -> list[str]:
    """Every string in a JSON value (the privacy test reads them all)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    return []
