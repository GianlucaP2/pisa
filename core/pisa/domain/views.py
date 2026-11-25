"""Pisa's own views. A view is a named recipe: read-only reads from an allowlist, then sheet blocks
whose strings may hold placeholders `{{n.path}}` (the n-th read's JSON, dotted) and one `each` repeater over a
list. Data, never code: Pisa writes no Python, Swift or SQL at run time; a value is text, never markup; a
missing value drops its block, never the sheet. Pure."""

import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pisa.domain import drawing as drawing_rules

VIEWS_MAX = 50
READS_MAX = 6
EACH_MAX = 20  # the repeater's items
NAME_MAX, DESCRIPTION_MAX = 40, 200
NAME_MIN = 3  # a shorter name ("a") is in every request
DAYS_MIN, DAYS_MAX = 1, 90  # search_my_email's days, as the tool itself takes them
ARG_MAX = 200
STRING_MAX = 600
# RAIL: Pisa's own read-only tools a view may run, and the arguments each takes. Never a write, a send,
# a profile write (remember), or anything that changes the plan.
READS: dict[str, frozenset[str]] = {
    "get_current_time": frozenset(),
    "get_agenda": frozenset({"day"}),
    "get_tasks": frozenset(),
    "get_plan": frozenset(),
    "get_weather": frozenset({"where", "when"}),
    "map_search": frozenset({"query", "near"}),
    "web_search": frozenset({"query"}),
    "read_page": frozenset({"url"}),
    "search_my_email": frozenset({"query", "days"}),
}
# the public-data reads (read-only, cached, rate-limited; their results are outside data):
# the next departures (to "home" or nowhere in particular, a line), a line's problems, the weather alerts
# where they are, Rome's ZTL now, the nearest pharmacies in Rome, the next flight's leave-by time by transit.
PUBLIC_READS: dict[str, frozenset[str]] = {
    "next_departures": frozenset({"to", "line"}),
    "transit_alerts": frozenset({"line"}),
    "weather_alerts": frozenset(),
    "ztl_status": frozenset({"zone"}),
    "nearest_pharmacy": frozenset(),
    "airport_departure": frozenset(),
}
READS.update(PUBLIC_READS)
BLOCK_KEYS: dict[str, frozenset[str]] = {
    "text": frozenset({"text"}),
    "image": frozenset({"url", "alt", "credit"}),
    "link": frozenset({"url", "title"}),
    "list": frozenset({"items"}),
    "fact": frozenset({"label", "value"}),
    "section": frozenset({"title"}),
    "table": frozenset({"columns", "rows"}),
    "stat": frozenset({"label", "value", "unit", "trend", "derive", "of"}),
    "chips": frozenset({"items"}),
    "divider": frozenset(),
    # saved literally; a diagram is laid out again by code on each run (never a stored
    # layout), and every number traces to that run's reads
    "diagram": frozenset({"style", "direction", "nodes", "edges", "groups"}),
    "chart": frozenset({"mark", "title", "x", "y", "series"}),
    "timeline": frozenset({"items"}),
}
_PLACEHOLDER = re.compile(r"\{\{\s*(\d+|item)((?:\.[A-Za-z0-9_\-]+)*)\s*\}\}")
# braces that aren't {{n.path}} are no placeholder at all
_BRACES = re.compile(r"\{\{[^{}]*\}\}")
_WHOLE = re.compile(r"^\s*\{\{\s*(\d+|item)((?:\.[A-Za-z0-9_\-]+)*)\s*\}\}\s*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Missing(Exception):
    """A placeholder with no value in this run's reads: its block is dropped."""


@dataclass(frozen=True)
class Recipe:
    reads: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    # a saved drawing: its code as Pisa wrote it (checked again each time it is sent),
    # its title, summary and kind, and `data`, PISA_DATA's shape with placeholders filled from the reads
    drawing: dict[str, Any] | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reads": self.reads, "blocks": self.blocks}
        if self.drawing is not None:
            out["drawing"] = self.drawing
        return out


def whole_placeholder(value: Any) -> bool:
    """A value that is one placeholder, whole ("{{0.places}}")."""
    return isinstance(value, str) and _WHOLE.match(value) is not None


def _placeholders(value: Any) -> list[str]:
    """Every string of a drawing's data template (its keys are Pisa's structure, never filled)."""
    if isinstance(value, dict):
        return [s for v in value.values() for s in _placeholders(v)]
    if isinstance(value, list):
        return [s for v in value for s in _placeholders(v)]
    return [value] if isinstance(value, str) else []


def name_key(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.casefold()))


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _page(url: str) -> tuple[str, str, int | None, str, str]:
    parts = urlsplit(url.strip().rstrip(".,;:!?)]}"))
    host = (parts.hostname or "").removeprefix("www.")
    try:
        port = parts.port
    except ValueError:
        port = -1  # a port that isn't one: never the page they said
    return parts.scheme.casefold(), host, port, parts.path.rstrip("/"), parts.query  # the port


def said_page(url: str, said: str) -> bool:
    """`url` is an address the user said: the whole address (scheme, host, path and,
    the query), never a piece of a longer one ("https://a.example/x" in "https://a.example/x/evil") or of
    their words, nor their page with a query they didn't say ("https://a.example/x?to=evil")."""
    return any(_page(found.group(0)) == _page(url) for found in _URL.finditer(said))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def validate(raw: Any, hosts: set[str], said: str = "") -> Recipe | str:
    """The recipe as Pisa may keep it, or why not. `hosts`: the pages this turn read (an image or link URL, or
    a read_page address, is a placeholder or https on one of them, or an address the user said)."""
    if not isinstance(raw, dict):
        return "the recipe is {reads: [...], blocks: [...]}"
    reads, blocks = raw.get("reads") or [], raw.get("blocks") or []
    drawing = raw.get("drawing")
    if not isinstance(reads, list) or not isinstance(blocks, list) or (not blocks and drawing is None):
        return "the recipe needs reads (a list) and at least one block"
    if len(reads) > READS_MAX:
        return f"at most {READS_MAX} reads"
    if len(blocks) > 24:
        return "at most 24 blocks"
    kept_reads: list[dict[str, Any]] = []
    for read in reads:
        if not isinstance(read, dict) or read.get("tool") not in READS:
            tool = read.get("tool") if isinstance(read, dict) else read
            return f"{str(tool)[:40]!r} isn't a read a view may run (only {', '.join(sorted(READS))})"
        args = read.get("args") or {}
        if not isinstance(args, dict) or set(args) - READS[read["tool"]]:
            return f"{read['tool']} takes only {sorted(READS[read['tool']]) or 'no arguments'}"
        for key, value in args.items():
            if not isinstance(value, str | int) or isinstance(value, bool) or len(str(value)) > ARG_MAX:
                return f"{read['tool']}.{key}: a short text or a number"
            if "{{" in str(value):
                return "a read's arguments are fixed values, never placeholders"
        if read["tool"] == "search_my_email" and "days" in args:
            try:  # 1 to 90 days, as the tool takes them
                args = {**args, "days": max(DAYS_MIN, min(DAYS_MAX, int(args["days"])))}
            except ValueError:
                return "search_my_email.days: a number of days"
        if read["tool"] == "read_page":
            url = str(args.get("url") or "")
            if not url.startswith("https://") or (host(url) not in hosts and not said_page(url, said)):
                return "read_page reads only an https page read this turn or an address the user said"
        kept_reads.append({"tool": read["tool"], "args": dict(args)})
    each = 0
    for block in blocks:
        if not isinstance(block, dict):
            return "each block is an object"
        if block.get("type") == "each":
            each += 1
            if each > 1:
                return "one each repeater per view"
            if set(block) - {"type", "from", "block"} or not _WHOLE.match(str(block.get("from") or "")):
                return 'each is {"type": "each", "from": "{{n.path}}", "block": {...}}'
            inner = block.get("block")
            if not isinstance(inner, dict) or inner.get("type") == "each":
                return "each repeats one block (never another each)"
            if (why := _block(inner, len(kept_reads), hosts, item=True)) is not None:
                return why
            if (why := _refs(str(block["from"]), len(kept_reads), item=False)) is not None:
                return why
        elif (why := _block(block, len(kept_reads), hosts, item=False)) is not None:
            return why
    if drawing is not None:
        if (why := _drawing(drawing, len(kept_reads))) is not None:
            return why
        drawing = {k: drawing[k] for k in ("html", "title", "summary", "kind", "data")}
    return Recipe(kept_reads, [dict(b) for b in blocks], drawing)


def _drawing(drawing: Any, reads: int) -> str | None:
    """A saved drawing's code passes the drawing check as it is; its data's
    placeholders name reads of the recipe; never an item placeholder (no each)."""
    if not isinstance(drawing, dict) or set(drawing) != {"html", "title", "summary", "kind", "data"}:
        return "a drawing view is {html, title, summary, kind, data}"
    if not isinstance(drawing["data"], dict):
        return "a drawing view's data is a JSON object"
    if drawing["kind"] not in drawing_rules.KINDS:
        return "a drawing's kind is one of " + ", ".join(drawing_rules.KINDS)
    if found := drawing_rules.problems(str(drawing["html"])):
        return "the drawing's code: " + "; ".join(found[:3])
    for text in _placeholders(drawing["data"]):
        if len(text) > STRING_MAX:
            return f"data: a string over {STRING_MAX} characters"
        if (why := _refs(text, reads, item=False)) is not None:
            return why
    return None


def fill_data(template: Any, results: list[Any]) -> tuple[Any, int]:
    """PISA_DATA from a drawing view's template and this run's reads: a whole placeholder takes
    its value as it is (a list or an object too); placeholders inside a text fill as text; a value missing
    today is null. (data, how many were missing)."""
    missing = [0]

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if not isinstance(value, str):
            return value
        whole = _WHOLE.match(value)
        if whole:
            found = _value(whole.group(1), whole.group(2), results)
            if found is None:
                missing[0] += 1
            return found
        try:
            return _PLACEHOLDER.sub(lambda m: _text(_lookup(m.group(1), m.group(2), results, None)), value)
        except Missing:
            missing[0] += 1
            return None

    return walk(template), missing[0]


def _value(source: str, path: str, results: list[Any]) -> Any:
    if not source.isdigit() or int(source) >= len(results):
        return None
    value = results[int(source)]
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _refs(text: str, reads: int, *, item: bool) -> str | None:
    for found in _PLACEHOLDER.finditer(text):
        source = found.group(1)
        if source == "item" and not item:
            return "{{item…}} only inside an each block"
        if source != "item" and int(source) >= reads:
            return f"{{{{{source}…}}}}: there is no read {source}"
    return None


def _block(block: dict[str, Any], reads: int, hosts: set[str], *, item: bool) -> str | None:
    kind = block.get("type")
    if kind not in BLOCK_KEYS:
        return f"{str(kind)[:20]!r} isn't a block template"
    if set(block) - BLOCK_KEYS[kind] - {"type"}:
        return f"{kind} takes only {sorted(BLOCK_KEYS[kind])}"
    points = _chart_points(block) if kind == "chart" else []
    if kind == "chart" and (
        not points or any(not (isinstance(p.get("y"), str) and _WHOLE.match(p["y"])) for p in points)
    ):  # a chart's numbers come from the view's reads, never stored literals
        return (
            "a chart keeps each point's y as a placeholder of its read ({{0.days.0.max_c}}), never a number"
        )
    filled = [p[k] for p in points for k in ("x", "y") if isinstance(p.get(k), str) and _WHOLE.match(p[k])]
    rest = {k: v for k, v in block.items() if k not in ("type", "series")}
    if kind == "chart":  # the series without the points' whole placeholders
        rest["series"] = [{k: v for k, v in s.items() if k != "points"} if isinstance(s, dict) else s
                          for s in block["series"]]  # fmt: skip
        rest["points"] = [
            {k: v for k, v in p.items() if k not in ("x", "y") or v not in filled} for p in points
        ]
    elif "series" in block:
        rest["series"] = block["series"]
    for text in _strings(rest):
        if len(text) > STRING_MAX:
            return f"{kind}: a string over {STRING_MAX} characters"
        if kind in ("diagram", "chart", "timeline") and "{{" in text:
            return f"a {kind} is saved as it is: placeholders only as a chart point's whole x or y"
        if (why := _refs(text, reads, item=item)) is not None:
            return why
    for text in filled:
        if (why := _refs(text, reads, item=item)) is not None:
            return why
    if kind in ("image", "link"):
        url = str(block.get("url") or "")
        if not _WHOLE.match(url) and (not url.startswith("https://") or host(url) not in hosts):
            return f"{kind}.url: a placeholder or an https page read this turn"
    return None


def _chart_points(block: dict[str, Any]) -> list[dict[str, Any]]:
    series = block.get("series")
    if not isinstance(series, list):
        return []
    return [
        p for s in series if isinstance(s, dict) and isinstance(s.get("points"), list)
        for p in s["points"] if isinstance(p, dict)
    ]  # fmt: skip


def _lookup(source: str, path: str, results: list[Any], item: Any) -> Any:
    value = item if source == "item" else results[int(source)] if int(source) < len(results) else None
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise Missing(path)
    if value is None or value == "" or isinstance(value, dict | list):
        raise Missing(path)
    if isinstance(value, float) and not math.isfinite(value):
        raise Missing(path)  # NaN or infinity is no value to show
    return value


def _text(value: Any) -> str:
    """A value as plain text: one line, no control characters, never read as a placeholder again."""
    text = _CONTROL.sub(" ", str(value))
    return " ".join(text.split()).replace("{{", "{ {")


def _fill(value: Any, results: list[Any], item: Any) -> Any:
    if isinstance(value, str):
        whole = _WHOLE.match(value)
        if whole:  # a lone placeholder keeps a number a number (a table's cell, a stat)
            found = _lookup(whole.group(1), whole.group(2), results, item)
            return found if isinstance(found, int | float) and not isinstance(found, bool) else _text(found)
        return _PLACEHOLDER.sub(lambda m: _text(_lookup(m.group(1), m.group(2), results, item)), value)
    if isinstance(value, list):
        return [_fill(v, results, item) for v in value]
    if isinstance(value, dict):  # a chart's series and points
        return {k: _fill(v, results, item) for k, v in value.items()}
    return value


def fill(recipe: Recipe, results: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """The view's blocks with this run's values. (blocks, how many were dropped for a missing value)."""
    out: list[dict[str, Any]] = []
    dropped = 0
    for block in recipe.blocks:
        if block.get("type") == "each":
            whole = _WHOLE.match(str(block["from"]))
            items: Any = None
            if whole:
                source, path = whole.group(1), whole.group(2)
                items = results[int(source)] if source.isdigit() and int(source) < len(results) else None
                for part in [p for p in path.split(".") if p]:
                    items = (
                        items.get(part)
                        if isinstance(items, dict)
                        else (
                            items[int(part)]
                            if isinstance(items, list) and part.isdigit() and int(part) < len(items)
                            else None
                        )
                    )
            if not isinstance(items, list):
                dropped += 1
                continue
            for element in items[:EACH_MAX]:
                try:
                    out.append(_fill_block(block["block"], results, element))
                except Missing:
                    dropped += 1
            continue
        try:
            out.append(_fill_block(block, results, None))
        except Missing:
            dropped += 1
    return out, dropped


FIELDS_SHOWN = 24  # the field paths a refused save lists, per read


def fields(value: Any, prefix: str = "", out: list[str] | None = None) -> list[str]:
    """A read's leaf paths as a recipe names them ("0.day.min_c"); a list by its first item; a
    null value is a field too."""
    out = [] if out is None else out
    if len(out) >= FIELDS_SHOWN:
        return out
    if isinstance(value, dict) and value:
        for key, inner in value.items():
            fields(inner, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list) and value:
        fields(value[0], f"{prefix}.0", out)
    elif prefix:
        out.append(prefix)
    return out[:FIELDS_SHOWN]


def _absent(source: str, path: str, results: list[Any], item: Any, *, leaf: bool = True) -> bool:
    """A path whose key the read's shape doesn't have ("days" in get_weather's). A null, an empty value or an
    index past the end today is unknown, never wrong ("{{0.events.0.title}}", an empty
    day). A path that ends on a whole
    object or list names no value a cell can show (`_lookup` finds none), so it is wrong too, save in an
    each block's `from` (`leaf=False`)."""
    if leaf and _container(source, path, results, item):
        return True
    value = item if source == "item" else results[int(source)] if int(source) < len(results) else None
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict):
            if part not in value:
                return True
            value = value[part]
        elif isinstance(value, list):
            if not part.isdigit():
                return True
            if int(part) >= len(value):
                return False
            value = value[int(part)]
        else:
            return value not in (None, "")  # a key under a number or a text is wrong; under a null, unknown
    return False


def _container(source: str, path: str, results: list[Any], item: Any) -> bool:
    value = item if source == "item" else results[int(source)] if int(source) < len(results) else None
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False
    return isinstance(value, dict | list) and bool(value)


def unresolved(recipe: Recipe, results: list[Any]) -> list[str]:
    """The placeholders whose path the shape of a read that answered now
    doesn't have ("{{0.days.0.min}}" where get_weather's result has `day.min_c`): a view saved with them shows
    nothing later. A read that failed now (None) is never held against the recipe, nor a value null today."""
    wrong: list[str] = []

    def check(text: str, item: Any, items_known: bool, *, leaf: bool = True) -> None:
        # "{{get_weather.max}}" filled nothing and showed itself on the phone, with no refusal
        for braces in _BRACES.findall(text):
            if not _PLACEHOLDER.fullmatch(braces) and braces not in wrong:
                wrong.append(braces)
        for found in _PLACEHOLDER.finditer(text):
            source, path = found.group(1), found.group(2)
            if source == "item":
                if not items_known:
                    continue
            elif int(source) >= len(results) or results[int(source)] is None:
                continue
            if _absent(source, path, results, item, leaf=leaf) and found.group(0) not in wrong:
                wrong.append(found.group(0))

    if recipe.drawing is not None:  # a whole list or object is a value PISA_DATA can carry
        for text in _placeholders(recipe.drawing.get("data")):
            check(text, None, False, leaf=False)
    for block in recipe.blocks:
        if block.get("type") == "each":  # its `from` names a list; an item is checked by the first one
            whole = _WHOLE.match(str(block["from"]))
            items: Any = None
            if whole and whole.group(1).isdigit():
                check(str(block["from"]), None, False, leaf=False)
                n = int(whole.group(1))
                items = results[n] if n < len(results) else None
                for part in [p for p in whole.group(2).split(".") if p]:
                    items = items.get(part) if isinstance(items, dict) else None
            first = items[0] if isinstance(items, list) and items else None
            for text in _strings(block["block"]):
                check(text, first, first is not None)
            continue
        for text in _strings({k: v for k, v in block.items() if k != "type"}):
            check(text, None, False)
    return wrong


def _fill_block(block: dict[str, Any], results: list[Any], item: Any) -> dict[str, Any]:
    if block.get("type") == "table" and isinstance(block.get("rows"), list):
        rows = []
        for row in block["rows"]:
            try:  # a table keeps the rows it can fill
                rows.append(_fill(row, results, item))
            except Missing:
                continue
        if not rows:
            raise Missing("rows")
        return {**{k: _fill(v, results, item) for k, v in block.items() if k != "rows"}, "rows": rows}
    return {k: _fill(v, results, item) for k, v in block.items()}


# RAIL: the user's own words this turn ask to keep what Pisa showed ("salvala come meteo", "tienila",
# "save it as weather"); never negated ("non salvarla")
_SAVE = re.compile(
    r"\b(?:salva\w*|tienil[aoei]|tienimel[aoei]|tienitel[aoei]|memorizza\w*|conserva\w*|save|keep)\b",
    re.IGNORECASE,
)
_NOT = re.compile(r"\b(?:non|no|mai|don'?t|never|not)\b", re.IGNORECASE)


def asks_to_save(owner_words: str) -> bool:
    return bool(_SAVE.search(owner_words)) and not _NOT.search(owner_words) and "?" not in owner_words


# the user's words say to replace a view of the same name ("sostituiscila", "replace it")
_REPLACE = re.compile(
    r"\b(?:sostituisc\w*|rimpiazz\w*|sovrascriv\w*|replace\w*|overwrite\w*)\b", re.IGNORECASE
)


# a negation anywhere in the replacing's own clause before it governs it ("non
# voglio che la sostituisci", "I don't think you should replace it"); one in another clause doesn't ("sì
# sostituiscila, non mi serve più"). A question never asks it ("sostituiscila? no"). When in doubt Pisa asks.
_CLAUSE_END = re.compile(r"[,.;:!]")


def asks_to_replace(owner_words: str) -> bool:
    found = _REPLACE.search(owner_words)
    if found is None or "?" in owner_words:
        return False
    clause = _CLAUSE_END.split(owner_words[: found.start()])[-1]
    return not _NOT.search(clause)
