"""The complete answer of a research turn, on the screen and kept.

`compose` builds the `sheet` Pisa shows (the table of candidates, their pros and cons, why Pisa picked each
for them now, the guides that cite each, the sources) **from this turn's tool results only**, the "only what
this turn saw" rule extended to every cell:
- the facts (walk time, rating and reviews, open now, price) are Pisa's own from the map's results, never the
  model's words;
- "cited by" is code's: the pages and results this turn saw whose own text names the place;
- a pro or a con stands on the source the model names (a page or result this turn saw, or the place's reviews
  read this turn) and shares its words with it, else it is left out;
- a cell with no source stays empty.
The top places also go up as the `places` card (each photo credited, the Maps link: the phone's own
view).

`kept` is what the turn found, for the conversation: the criteria, the candidates and their facts,
the guides' quotes, the sources. It re-enters a later turn only through `my_findings`, as a data block:
its names, quotes and pros come from outside.
"""

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from pisa.domain import recognition as recognition_rules
from pisa.domain import requirements as need_rules
from pisa.domain.answers import place_blocks
from pisa.domain.profile import stems
from pisa.domain.reasons import place_why
from pisa.domain.research import EVENT_AHEAD_H, why_now
from pisa.domain.sheet import SHEET_BLOCKS, TABLE_ROWS
from pisa.domain.text import fold_words

ROWS_MAX = 10  # the table's candidates ("up to ~10 rows")
TOP = 3  # the top places with a photo (the places card) and a section of their own
POINTS_MAX = 3  # pros, cons each
CITED_MAX = 3  # the hosts named in a "cited by" cell
QUOTE_MAX = 160
KEPT_REVIEWS = 1500  # characters of a place's reviews kept for a deeper question
KEPT_PAGES = 12  # pages and results kept, their candidates and quotes
_COLUMNS = {
    "it-IT": ("Posto", "A piedi", "Voto", "Aperto", "Prezzo", "Citato da"),
    "en-US": ("Place", "On foot", "Rating", "Open", "Price", "Cited by"),
}
# the why column, beside pros and cons
_POINTS = {
    "it-IT": ("Posto", "Perché per te ora", "Pro", "Contro"),
    "en-US": ("Place", "Why for you now", "Pros", "Cons"),
}
EXCLUDED_MAX = 6  # contract answers-1 §2
ROWS_MIN = 3  # a pick's table shows at least three when three survive
_WORDS = {
    "it-IT": {"why": "Perché per te ora", "criteria": "Cosa conta", "open": "aperto", "closed": "chiuso",
              "until": "fino alle", "opens": "apre alle", "pick": "La mia scelta", "says": "dice"},
    "en-US": {"why": "Why for you now", "criteria": "What matters", "open": "open", "closed": "closed",
              "until": "until", "opens": "opens", "pick": "My pick", "says": "says"},
}  # fmt: skip


def key(text: str) -> str:
    """Lowercase letters and digits, single spaces, no accents: how a name is found in a text."""
    return fold_words(text or "")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def cited_by(name: str, research: Any) -> list[dict[str, str]]:
    """The pages and results this turn saw that cite the place (a guide, an award, the press): code reads it,
    the model never writes it. A page with reader candidates cites the names among them; another page or a
    result, when its own text names the place. Read pages first; each with the reader's quote when given."""
    wanted = key(name)
    if len(wanted) < 4:
        return []
    out: list[dict[str, str]] = []
    ordered = [u for u in research.urls if u in research.read] + [
        u for u in research.urls if u not in research.read
    ]
    for url in ordered:
        if hasattr(research, "own_page") and research.own_page(url):
            continue  # a place's own page cites nobody (a guide is a guide)
        text = research.pages.get(url) or research.snippets.get(url) or ""
        names = [n for n, _ in research.candidates.get(url, [])]
        # a page the reader read for their question cites only the names it found answering it
        # (a page that pans a place, or names it in passing, doesn't); text matching is for the rest
        if names:
            if not any(key(n) == wanted for n in names):
                continue
        elif f" {wanted} " not in f" {key(text)} ":
            continue
        host = _host(url)
        if not host or any(o["host"] == host for o in out):
            continue
        quote = next((q for n, q in research.candidates.get(url, []) if key(n) == wanted and q), "")
        out.append({"host": host, "url": url, "title": (research.urls.get(url) or host)[:120],
                    "quote": quote[:QUOTE_MAX]})  # fmt: skip
    return out


def pages_seen(research: Any) -> list[dict[str, Any]]:
    """The pages and results this turn saw, read pages first, for the recognition rules: each
    with its title, its text (a read page's, else the result's snippet), the reader's quotes and whether a
    place publishes it itself."""
    ordered = [u for u in research.urls if u in research.read] + [
        u for u in research.urls if u not in research.read
    ]
    own = getattr(research, "own_page", None)
    return [
        {"url": u, "title": research.urls.get(u) or "",
         "text": research.pages.get(u) or research.snippets.get(u) or "",
         "quotes": research.candidates.get(u, []), "own": bool(own(u)) if own is not None else False}
        for u in ordered
    ]  # fmt: skip


def recognition(name: str, research: Any) -> list[dict[str, Any]]:
    """The awards this turn's guide pages and results give the place,
    code's from their text, never the model's."""
    return recognition_rules.recognitions(name, pages_seen(research))


def _belongs(url: str, websites: list[str], name: str) -> bool:
    """The page is this place's own: its website's host, or a host named after it (never another's menu)."""
    host = _host(url)
    sites = {_host(w if "//" in w else f"//{w}") for w in websites if w}
    if host and host in sites:
        return True
    label = re.sub(r"[^a-z0-9]", "", host.rsplit(".", 1)[0].split(".")[-1])
    words = key(name).split()
    # its name whole, or without its little words ("Osteria del Tralcio" → osteriatralcio.example)
    forms = {"".join(words), "".join(w for w in words if len(w) > 3)}
    return len(label) >= 5 and any(len(f) >= 5 and (f in label or label in f) for f in forms)


def evidence_of(research: Any, place_id: str) -> list[tuple[str, str, str]]:
    """What this turn read about one place, for its hard requirements: (text, the name to find
    in it or "" when the whole text is about the place, source). Its reviews and attributes (the map), its own
    pages read (its menu), the sentences of other pages and results that name it, the reader's quotes."""
    facts = research.places.get(place_id) or {}
    name = str(facts.get("name") or "")
    out: list[tuple[str, str, str]] = []
    if text := research.reviews.get(place_id):
        out.append((text, "", "map"))
    site = [str(facts["website"])] if facts.get("website") else []
    for url in [u for u in research.urls if u in research.read] + [
        u for u in research.urls if u not in research.read
    ]:
        text = research.pages.get(url) or research.snippets.get(url) or ""
        if url in research.read and _belongs(url, site, name):
            out.append((text, "", url))
        elif name:
            out.append((text, name, url))
            out += [(q, "", url) for n, q in research.candidates.get(url, []) if q and key(n) == key(name)]
    return out


def _content(text: str) -> set[str]:
    return stems(text)


def traced(point: str, source: str, place_id: str, research: Any) -> bool:
    """A pro or con stands on its source: a page or result this turn saw, or this place's reviews and
    attributes read this turn (its id, or "reviews"); and it shares its words with that source (two content
    words when it has more than two, else one)."""
    src = source.strip()
    if src in research.urls:
        text = research.pages.get(src) or research.snippets.get(src) or ""
        text += " " + " ".join(f"{n} {q}" for n, q in research.candidates.get(src, []))
    elif src in (place_id, "reviews", "map") or key(src) == key(
        str(research.places.get(place_id, {}).get("name"))
    ):
        text = research.reviews.get(place_id, "")
    else:
        return False
    said = _content(point)
    if not said or not text:
        return False
    need = 2 if len(said) > 2 else 1
    return len(said & _content(text)) >= need


def _clock(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else ""


def facts_cells(facts: dict[str, Any], lang: str) -> dict[str, str]:
    """The table's facts for one place, from this turn's map results only; "" when the turn didn't see it."""
    w = _WORDS.get(lang, _WORDS["en-US"])
    minutes, meters = facts.get("travel_min"), facts.get("distance_m")
    walk = f"{minutes} min" if minutes is not None else (f"{meters} m" if meters is not None else "")
    rating = ""
    if facts.get("rating") is not None:
        count = facts.get("rating_count")
        rating = f"{facts['rating']}" + (f" ({count})" if count else "")
    opened = ""
    if facts.get("open_now") is not None:
        at = _clock(facts.get("next_change"))
        if facts["open_now"]:
            opened = w["open"] + (f", {w['until']} {at}" if at else "")
        else:
            opened = w["closed"] + (f", {w['opens']} {at}" if at else "")
    level = facts.get("price_level")
    price = "€" * max(1, int(level)) if isinstance(level, int | float) and not math.isnan(level) else ""
    return {"walk": walk, "rating": rating, "open": opened, "price": price}


def reasoning(why: str) -> str:
    """ "why for them now" is Pisa's reasoning, not a fact: the facts (minutes, hours, rating, price)
    are the table's columns, from the map. A sentence of it that carries a number (a fact the cell can't
    trace: "è a 5 minuti, aperto fino alle 19") is left out."""
    kept = [x for x in re.split(r"(?<=[.!?])\s+", " ".join(why.split())) if x and not re.search(r"\d", x)]
    return _cell(" ".join(kept), 200)


def _cell(text: str, limit: int = 80) -> str:
    return " ".join(str(text).split())[:limit]


def for_him(
    research: Any, pid: str, facts: dict[str, Any], told: str, lang: str, open_now: bool | None
) -> tuple[list[str], str]:
    """Code's reasons from their situation (their next event and the minutes it leaves them, who is with them,
    the minutes on foot, what they like that the place has, open in this part of their day: `why_now`), then
    the model's own line (`told`): (the reasons, the whole why).
    The places card's why too, never a walk time alone when their own data says more."""
    now = why_now(
        facts.get("travel_min"), open_now, getattr(research, "now", None), getattr(research, "event", None),
        lang, company=list(getattr(research, "company", None) or []),
        likes=research.likes(pid) if hasattr(research, "likes") else [],
    )  # fmt: skip
    said = ", ".join(now)
    if said and told and not told[1:2].isupper():  # "…, aperto stamattina; è a due passi dalla guida"
        told = told[:1].lower() + told[1:]
    return now, f"{said[:1].upper()}{said[1:]}" + (f"; {told}" if said and told else told)


def _row(
    research: Any, pid: str, facts: dict[str, Any], why: str, points: dict[str, list[str]], lang: str
) -> dict[str, Any]:
    """One candidate's row. Its why begins with code's reasons from their situation (their next
    event, the minutes on foot, open in this part of their day: `research.why_now`), then the model's
    reasoning (the same `place_why` check as the places card, so one place reads the same on both)."""
    name = str(facts.get("name") or "")[:120]
    asked = getattr(research, "asked", None)
    verdict = (getattr(research, "verdicts", None) or {}).get(pid)
    # a time they named is the one that counts: "open now" is no reason for tomorrow's
    # lunch
    open_now = None if asked is not None and asked.named else facts.get("open_now")
    told = place_why(reasoning(why), lang) if why.strip() else ""
    now, whole = for_him(research, pid, facts, told, lang, open_now)
    cells = facts_cells(facts, lang)
    opened = verdict.of("open") if verdict is not None else None
    if opened is not None:  # open at the time they asked, checked (contract §1a)
        cells["open"] = _cell(opened.why, 80)
    unchecked = need_rules.unchecked(verdict, lang) if verdict is not None else ""
    if unchecked:  # what no source confirmed, marked, never left as if checked
        whole = f"{whole}; {unchecked}" if whole else unchecked[:1].upper() + unchecked[1:]
    return {
        "place_id": pid, "name": name, "why": _cell(whole, 200), "now": now, "told": told,
        **cells, "cited_by": cited_by(name, research), **points,
        "recognition": recognition(name, research) if hasattr(research, "pages") else [],
        "lat": facts.get("lat"), "lon": facts.get("lon"), "unchecked": unchecked,
        "open_at_asked": _open_at_asked(opened),
        "checks": [{"need": o.need, "ok": o.ok, "why": o.why, "source": o.source}
                   for o in (verdict.outcomes if verdict is not None else [])],
    }  # fmt: skip


def _open_at_asked(opened: Any) -> dict[str, Any] | None:
    """Contract answers-1 §1a: {ok, hours, source}; None when the turn didn't check the time."""
    if opened is None:
        return None
    out: dict[str, Any] = {"ok": opened.ok}
    if opened.ok is not None and opened.why:
        out["hours"] = _cell(opened.why, 80)
    # contract answers-1 §1a requires a `source`: unknown hours were the map's to give
    out["source"] = opened.source or "map"
    return out


def _short_why(row: dict[str, Any], limit: int = 80) -> str:
    """The why cell within the sheet's cell (80 characters): whole phrases only, their situation first; the
    model's reasoning when it fits (the section below the table says it whole)."""
    out = ""
    parts = [*row.get("now", []), *([row["unchecked"]] if row.get("unchecked") else [])]
    for part in [*parts, *([row["told"]] if row.get("told") else [])]:
        joined = f"{out}, {part}" if out else part
        if len(joined) > limit:
            break
        out = joined
    if not out:  # one phrase longer than the cell: its whole words that fit
        why = _cell(row.get("why", ""), 200)
        out = why if len(why) <= limit else why[: limit + 1].rsplit(" ", 1)[0]
    return f"{out[:1].upper()}{out[1:]}"


def _shut(facts: dict[str, Any], now: Any) -> bool:
    """Closed now and not opening within EVENT_AHEAD_H hours (a dinner asked at 18:30 may open at 19:00)."""
    if facts.get("open_now") is not False:
        return False
    try:
        opens = datetime.fromisoformat(str(facts.get("next_change")))
    except ValueError:
        return True
    if now is None or opens.tzinfo is None:
        return True
    return not timedelta(0) < opens - now <= timedelta(hours=EVENT_AHEAD_H)


def _filled(
    research: Any, rows: list[dict[str, Any]], notes: list[str], lang: str, out_of: set[str] | None = None
) -> list[dict[str, Any]]:
    """A pick with fewer than ROWS_MIN rows while the turn found more: the places it
    checked (place_details) first, then the map's in its order, never one closed now that doesn't open soon.
    Their facts are this turn's; their why is code's alone (no pro or con: no source was named)."""
    if len(rows) >= ROWS_MIN:
        return []
    have = {r["place_id"] for r in rows}
    checked = [f[1] for f in sorted(research.facts) if f[0] == "details"]
    order = [*dict.fromkeys([*checked, *research.places])]
    out: list[dict[str, Any]] = []
    for pid in order:
        facts = research.places.get(pid)
        if len(rows) + len(out) >= ROWS_MIN or facts is None or pid in have or _shut(facts, research.now):
            continue
        verdict = (getattr(research, "verdicts", None) or {}).get(pid)
        if pid in (out_of or set()) or (verdict is not None and verdict.failed is not None):
            continue  # never one a hard requirement excluded
        out.append(_row(research, pid, facts, "", {"pros": [], "cons": []}, lang))
        have.add(pid)
    if out:
        notes.append(
            "Also in the table, from this turn's results: " + ", ".join(r["name"] for r in out) + "."
        )
    return out


def compose(
    research: Any,
    title: str,
    criteria: list[str],
    pick: str,
    chosen: list[Any],
    lang: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str]]:
    """(the sheet's data or None, the rows kept for the record, notes for the model). `chosen`: the model's
    places (`place_id`, `why`, `pros`, `cons`), best first."""
    notes: list[str] = []
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    verdicts = getattr(research, "verdicts", None) or {}
    for item in chosen:
        pid = item.place_id.strip()
        facts = research.places.get(pid)
        if facts is None:
            notes.append(f"{pid[:40]!r} wasn't found by map_search or place_details this turn: left out.")
            continue
        if any(r["place_id"] == pid for r in rows) or any(e["place_id"] == pid for e in excluded):
            continue
        if (verdict := verdicts.get(pid)) is not None and (failed := verdict.failed) is not None:
            # a finalist that fails a hard requirement checked against a source
            # is never shown as a candidate: it is named among the excluded, with the reason and its source
            excluded.append({"place_id": pid, "name": str(facts.get("name") or "")[:120],
                             "why": _cell(failed.why, 120), "source": failed.source or "map"})  # fmt: skip
            notes.append(f"{facts.get('name')} left out: {failed.why} (checked: {failed.source or 'map'}).")
            continue
        if len(rows) == ROWS_MAX:
            notes.append(f"Only {ROWS_MAX} rows: {facts.get('name')} left out.")
            continue
        name = str(facts.get("name") or "")[:120]
        points: dict[str, list[str]] = {"pros": [], "cons": []}
        for side in ("pros", "cons"):
            for p in getattr(item, side)[:POINTS_MAX]:
                if traced(p.text, p.source, pid, research):
                    points[side].append(_cell(p.text, 120))
                else:
                    notes.append(
                        f"{side[:-1]} {p.text[:40]!r} of {name}: its source doesn't say it this turn: "
                        "left out."
                    )
        rows.append(_row(research, pid, facts, item.why, points, lang))
    rows += _filled(research, rows, notes, lang, {e["place_id"] for e in excluded})
    research_excluded = getattr(research, "excluded", None)
    if isinstance(research_excluded, list):
        research_excluded[:] = excluded[:EXCLUDED_MAX]
    if not rows:
        return None, [], notes
    picked = next((r for r in rows if r["place_id"] == pick.strip()), rows[0])
    rows.sort(key=lambda r: r is not picked)  # the pick first, the rest in the model's order
    w = _WORDS.get(lang, _WORDS["en-US"])
    blocks: list[dict[str, Any]] = []
    named = [_cell(c, 40) for c in criteria if c.strip()][:12]
    if named:
        blocks += [{"type": "section", "title": w["criteria"]}, {"type": "chips", "items": named}]
    # the map and the rows with their cards; a phone without them gets the tables
    blocks += place_blocks(rows, research.places, getattr(research, "slots", None))
    blocks.append({
        "type": "table", "columns": list(_COLUMNS.get(lang, _COLUMNS["en-US"])),
        "rows": [[_cell(r["name"]), r["walk"], r["rating"], r["open"], r["price"], _cited_cell(r)]
                 for r in rows[:TABLE_ROWS]],
    })  # fmt: skip
    blocks.append({  # why for them now, per row, beside its pros and cons
        "type": "table", "columns": list(_POINTS.get(lang, _POINTS["en-US"])),
        "rows": [[_cell(r["name"]), _short_why(r), _cell("; ".join(r["pros"])),
                  _cell("; ".join(r["cons"]))] for r in rows],
    })  # fmt: skip
    for r in rows[:TOP]:
        heading = f"{w['pick']}: {r['name']}" if r is picked else r["name"]
        blocks += [{"type": "section", "title": _cell(heading, 120)},
                   {"type": "text", "text": _cell(f"{w['why']}: {r['why']}", 600)}]  # fmt: skip
        quotes = [f"{c['host']} {w['says']}: «{c['quote']}»" for c in r["cited_by"] if c["quote"]][:3]
        if quotes:
            blocks.append({"type": "list", "items": [_cell(q, 200) for q in quotes]})
    data: dict[str, Any] = {"title": _cell(title, 120) or "Pisa", "blocks": blocks[:SHEET_BLOCKS]}
    if excluded:  # the sheet's field beside the findings block;
        # a phone without `findings` gets its "Esclusi" section from `visual.answers_for_phone`
        data["excluded"] = excluded_items(excluded)
    return data, rows, notes


def _cited_cell(row: dict[str, Any]) -> str:
    """The "Citato da" cell: the recognition first ("Tavola Chiara Tre Lune 2026"), then the hosts citing
    it."""
    chips = [recognition_rules.chip(r) for r in row.get("recognition") or []]
    hosts = [c["host"] for c in row["cited_by"][:CITED_MAX]]
    return _cell(", ".join([*chips, *hosts][: CITED_MAX + len(chips)]))


def excluded_items(excluded: list[dict[str, str]]) -> list[dict[str, str]]:
    """The sheet's `excluded` as the contract carries it (answers-1 §2), at most
    EXCLUDED_MAX."""
    return [
        {"name": e["name"][:120], "why": e["why"][:120], "source": e["source"]}
        for e in excluded[:EXCLUDED_MAX]
    ]


def kept(
    research: Any, rows: list[dict[str, Any]], criteria: list[str], pick: str, request: str
) -> dict[str, Any]:
    """What this research turn found, for the conversation's deeper questions: the criteria, the
    candidates (shown or only seen), the guides' candidates and quotes, the reviews read, the sources."""
    rows = [{**r, "cited_by": r["cited_by"][:CITED_MAX]} for r in rows]
    shown = {r["place_id"] for r in rows}
    seen = [
        {"place_id": pid, **{k: f.get(k) for k in ("name", "rating", "rating_count", "distance_m",
                                                   "travel_min",
                                                   "open_now", "price_level", "category")}}
        for pid, f in research.places.items() if pid not in shown
    ][:20]  # fmt: skip
    pages = []
    for url in [u for u in research.urls if u in research.read][:KEPT_PAGES]:
        pages.append({"url": url, "title": (research.urls.get(url) or "")[:120],
                      "candidates": [[n, q] for n, q in research.candidates.get(url, [])][:15]})  # fmt: skip
    results = [{"url": u, "text": (research.snippets.get(u) or "")[:500]}
               for u in research.urls if u not in research.read][:KEPT_PAGES]  # fmt: skip
    reviews = {pid: text[:KEPT_REVIEWS] for pid, text in research.reviews.items()}
    out = {"request": _cell(request, 300), "criteria": [_cell(c, 80) for c in criteria][:12], "pick": pick,
           "shown": rows, "seen": seen, "pages": pages, "results": results, "reviews": reviews}  # fmt: skip
    if excluded := list(getattr(research, "excluded", None) or []):  # why each was left out
        out["excluded"] = excluded_items(excluded)
    return out


def snapshot(research: Any, request: str) -> dict[str, Any] | None:
    """A research turn that showed no findings still keeps what it found (None when it found nothing).
    a narrow follow-up search never replaces the deep findings: when the conversation keeps
    earlier ones, this turn's evidence joins them (its places seen, pages, results, reviews).
    `request` is their own words, never the model's nor a label."""
    if research.findings is not None:
        return {**research.findings, "request": request}
    if not research.places and not research.urls:
        return None
    now = kept(research, [], research.criteria, "", request)
    if research.earlier is None:
        return now
    out = dict(research.earlier)
    shown = {r["place_id"] for r in out.get("shown", [])}
    seen = {s["place_id"] for s in out.get("seen", [])}
    out["seen"] = [*out.get("seen", []), *(s for s in now["seen"] if s["place_id"] not in shown | seen)]
    pages = {p["url"] for p in out.get("pages", [])}
    out["pages"] = [*out.get("pages", []), *(p for p in now["pages"] if p["url"] not in pages)]
    results = {r["url"] for r in out.get("results", [])}
    out["results"] = [*out.get("results", []), *(r for r in now["results"] if r["url"] not in results)]
    out["reviews"] = {**out.get("reviews", {}), **now["reviews"]}
    return out


def sourced(found: dict[str, Any]) -> str:
    """The kept findings' text that came from a source, for the next turn's corpus: the
    rows' cells code filled from the map, the recognition and checks code read, the pages' and results' own
    words and the reviews. Never Pisa's words (its why, pros and cons, criteria): a figure the model made up
    there would become "sourced" on the next turn, and a correction of it would be held back."""
    lines: list[str] = []
    for r in found.get("shown", []):
        lines.append(" ".join(str(r.get(k) or "") for k in ("name", "walk", "rating", "open", "price")))
        lines += [recognition_rules.chip(x) for x in r.get("recognition") or []]
        lines += [str(c.get("why") or "") for c in r.get("checks") or []]
        lines += [str(c.get("quote") or "") for c in r.get("cited_by") or []]
    for p in found.get("pages", []):
        lines += [str(q) for _, q in p.get("candidates") or [] if q]
    lines += [str(x.get("text") or "") for x in found.get("results", [])]
    lines += [str(t) for t in (found.get("reviews") or {}).values()]
    lines += [str(e.get("why") or "") for e in found.get("excluded") or []]
    return "\n".join(x for x in lines if x.strip())


def digest(found: dict[str, Any], place: str | None = None) -> str:
    """The kept findings as text for `my_findings` (inside a data block): the criteria, each candidate with
    its facts, cited-by, pros, cons and Pisa's why; the guides' candidates; the reviews of the one asked."""
    lines = [f"request: {found.get('request', '')}"]
    if found.get("criteria"):
        lines.append("criteria: " + "; ".join(found["criteria"]))
    asked = (place or "").strip()
    wanted = key(asked)

    def named(name: Any, place_id: Any) -> bool:  # by its id as given, or by its name
        return bool(asked) and (asked == str(place_id) or (bool(wanted) and wanted in key(str(name or ""))))

    for r in found.get("shown", []):
        if asked and not named(r["name"], r["place_id"]):
            continue
        bits = [f"{r['name']} (id {r['place_id']})"] + [
            f"{k} {r[k]}" for k in ("walk", "rating", "open", "price") if r.get(k)
        ]
        if r["place_id"] == found.get("pick"):
            bits.append("Pisa's pick")
        lines.append("- " + ", ".join(bits))
        for x in r.get("recognition") or []:  # the award, from the guide's own page
            lines.append(f"  recognition: {recognition_rules.chip(x)} ({x['url']})")
        for c in r.get("checks") or []:
            state = {True: "met", False: "failed", None: "not checked"}[c.get("ok")]
            lines.append(
                f"  {c['need']}: {state}, {c['why']}" + (f" ({c['source']})" if c.get("source") else "")
            )
        if r.get("why"):
            lines.append(f"  why Pisa picked it: {r['why']}")
        for c in r.get("cited_by", []):
            lines.append(
                f"  cited by {c['host']} ({c['url']})" + (f": «{c['quote']}»" if c.get("quote") else "")
            )
        lines += [f"  pro: {p}" for p in r.get("pros", [])] + [f"  con: {c}" for c in r.get("cons", [])]
        if asked and (text := found.get("reviews", {}).get(r["place_id"])):
            lines.append(f"  reviews and hours read: {text}")
    if asked:  # the one asked about among those only seen, and what the pages say of it
        for s in found.get("seen", []):
            if named(s.get("name"), s.get("place_id")):
                lines.append(
                    f"- seen, not shown: {s.get('name')} (id {s['place_id']}), rating {s.get('rating')}"
                )
                if text := found.get("reviews", {}).get(s["place_id"]):
                    lines.append(f"  reviews and hours read: {text}")
        for p in found.get("pages", []):
            for n, q in p.get("candidates", []):
                if wanted and wanted in key(n):
                    lines.append(
                        f"  page {p['title'] or p['url']} ({p['url']}): {n}" + (f": «{q}»" if q else "")
                    )
    for e in found.get("excluded") or []:  # left out by a hard requirement, with its source
        if not asked or (wanted and wanted in key(e.get("name", ""))):
            lines.append(f"- excluded: {e['name']}: {e['why']} (source: {e['source']})")
    if not asked:
        for s in found.get("seen", []):
            lines.append(f"- seen, not shown: {s.get('name')} (id {s['place_id']}), rating {s.get('rating')}")
        for p in found.get("pages", []):
            names = "; ".join(f"{n}: «{q}»" if q else n for n, q in p.get("candidates", []))
            lines.append(f"page {p['title'] or p['url']} ({p['url']})" + (f": {names}" if names else ""))
        for r in found.get("results", []):
            lines.append(f"result {r['url']}: {r['text']}")
    return "\n".join(lines)


KEPT_CHARS = 60_000  # a kept record's JSON, at most: the bulkiest parts go first
KEPT_ROWS = 5  # a conversation keeps its last five research records


def bounded(found: dict[str, Any]) -> dict[str, Any]:
    """The record within KEPT_CHARS: the search results go first, then the reviews, then the pages' quotes;
    the shown candidates and the criteria always stay."""
    out = dict(found)
    out["shown"] = [{**r, "cited_by": (r.get("cited_by") or [])[:CITED_MAX]} for r in out.get("shown", [])]
    for part, empty in (("results", []), ("reviews", {}), ("pages", []), ("seen", [])):
        if len(json.dumps(out, default=str)) <= KEPT_CHARS:
            break
        out[part] = empty
    while len(json.dumps(out, default=str)) > KEPT_CHARS and out["shown"]:  # never over its cap
        out["shown"] = out["shown"][:-1]
    return out
