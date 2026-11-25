"""Pisa's skills: wish to skill, the skill garden, every complaint becomes a test. Pure rules.

- **A wish** is the user's own sentence ("vorrei che tu potessi dirmi…", "I wish you could…"), never outside
  text. Pisa writes 2-5 acceptance checks from it (data: an input, the block the sheet must hold, the words it
  must show) and a view recipe (the rails); the trial runs it on their data and counts the checks
  passed.
- **The garden**: a skill unused for 30 days withers; a schedule they gave ("ogni mattina alle 8") runs it.
- **Complaints**: their corrections of Pisa are kept as private replay candidates, minimised (`minimise`): a
  capitalised word (a name) or a number stays only if they said it.
"""

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from pisa.domain import public_sources, views

Status = Literal["proposed", "trial", "on", "withered", "off"]
CHECKS_MIN, CHECKS_MAX = 2, 5
SKILLS_MAX = 30
NAME_MAX = 60
MADE_FROM_MAX = 300
WITHER_AFTER = timedelta(days=30)
CHECK_TEXT_MAX = 120

# a wish for a new ability ("vorrei che tu potessi dirmi…"), never any "vorrei che mi
# chiamassi", "impara a memoria", "learn to ski": an ability of Pisa's (potessi, sapessi…) or Pisa learning
# one.
_WISH = re.compile(
    r"\b(?:vorrei\s+che\s+(?:tu\s+)?(?:potessi|sapessi|riuscissi\s+a)|mi\s+piacerebbe\s+che\s+(?:tu\s+)?"
    r"(?:potessi|sapessi|riuscissi\s+a)|sarebbe\s+(?:bello|utile)\s+se\s+(?:tu\s+)?(?:potessi|sapessi)|"
    r"(?:puoi|potresti)\s+imparare\s+a\s+(?:dirmi|avvisarmi|mostrarmi|ricordarmi|controllare|tenere)|"
    r"i\s+wish\s+you\s+(?:could|knew\s+how\s+to)|i'?d\s+like\s+you\s+to\s+be\s+able\s+to|"
    r"(?:can|could)\s+you\s+learn\s+to\s+(?:tell|show|check|warn|remind|track))\b",
    re.IGNORECASE,
)
_NEGATED_WISH = re.compile(r"\b(?:non\s+vorrei|i\s+don'?t\s+wish|non\s+mi\s+piacerebbe)\b", re.IGNORECASE)
_RENAME = re.compile(r"\b(?:rinomin\w*|chiamal[ao]|cambia(?:le)?\s+(?:il\s+)?nome|rename|call\s+it)\b", re.I)
_WHEN = re.compile(
    r"\b(?:ogni|tutti\s+i|tutte\s+le|alle|every|each|at|daily|weekdays|weekends|mattina|sera|morning"
    r"|evening)\b",
    re.IGNORECASE,
)
# a correction says so: a marked "no" followed by the correction ("no, intendevo sabato", "ma
# no, alle 22", "no aspetta…"), or a correction phrase anywhere; never a "ma no, che bello", "sbagliato
# treno", a "wrong number", a question.
_MARKED = re.compile(
    r"^\W*(?:no|ma\s+no|nope|no\s+no)\b[\s,!.]+(?:aspetta\W+)?(?:intendevo|ti\s+ho\s+detto"
    r"|te\s+l'?ho\s+detto|"
    r"non\b|era\b|volevo|alle\b|all'|a\s+\w+|per\s+\w+|di\s+\w+|sabato|domenica|luned|marted|mercoled|gioved|"
    r"venerd|domani|oggi|stasera|i\s+(?:said|meant|told\s+you)|it'?s|it\s+was|at\b|on\b|tomorrow|today)",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"\b(?:hai\s+sbagliato|hai\s+capito\s+male|non\s+(?:è|e')\s+quello\s+che\s+(?:ti\s+)?ho\s+chiesto|"
    r"non\s+intendevo|non\s+ho\s+detto|non\s+ti\s+ho\s+chiesto|not\s+what\s+i\s+(?:asked|said|meant)|"
    r"you\s+got\s+it\s+wrong|you\s+misunderstood|that'?s\s+(?:wrong|not\s+right)|i\s+didn'?t\s+(?:ask"
    r"|say))\b",
    re.IGNORECASE,
)
_NAMEISH = re.compile(
    r"\b[A-ZÀ-Ý][\w'\u2019-]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://[^\s)]+|\b\d[\d\s./-]{3,}\d\b"
)
_DAYS = {"daily": (0, 1, 2, 3, 4, 5, 6), "weekdays": (0, 1, 2, 3, 4), "weekends": (5, 6)}


def asks_wish(words: str) -> bool:
    """Their own words wish for something Pisa can't do yet ("vorrei che tu potessi dirmi se il treno è in
    ritardo")."""
    return bool(_WISH.search(words)) and not _NEGATED_WISH.search(words)


def asks_rename(words: str) -> bool:
    return bool(_RENAME.search(words))


def asks_schedule(words: str) -> bool:
    """Their words give a when ("ogni mattina alle 8", "every weekday at 7")."""
    return bool(_WHEN.search(words))


def is_complaint(words: str) -> bool:
    """A correction of Pisa: "no, ti ho detto…", "hai sbagliato", "non è quello che ho chiesto", "that's
    wrong"."""
    text = words.strip()
    if not text or text.endswith("?"):
        return False
    return bool(_MARKED.search(text) or _CORRECTION.search(text))


def checks_of(raw: Any) -> list[dict[str, str]] | str:
    """RAIL: 2-5 checks, each data: `input` (what it is about), and `shows` (a block template) and/or `has`
    (words the sheet must show). Never code: plain short strings. The reason it is refused otherwise."""
    if not isinstance(raw, list) or not CHECKS_MIN <= len(raw) <= CHECKS_MAX:
        return f"write {CHECKS_MIN} to {CHECKS_MAX} checks from the user's sentence"
    out: list[dict[str, str]] = []
    for check in raw:
        if not isinstance(check, dict):
            return "each check is {input, shows?, has?}"
        kept = {
            k: " ".join(str(v).split())[:CHECK_TEXT_MAX]
            for k, v in check.items()
            if k in ("input", "shows", "has") and isinstance(v, str) and v.strip()
        }
        if "input" not in kept or not ({"shows", "has"} & kept.keys()):
            return "each check needs an input and what the sheet shows (a block) or has (words)"
        out.append(kept)
    return out


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _texts(v)]
    if isinstance(value, list):
        return [s for v in value for s in _texts(v)]
    return []


def passes(check: dict[str, str], sheet: dict[str, Any] | None) -> bool:
    """A check passes when the trial's sheet holds its block template and shows its words (case-folded)."""
    if sheet is None:
        return False
    blocks = sheet.get("blocks") or []
    if (shows := check.get("shows")) and not any(b.get("type") == shows.strip().lower() for b in blocks):
        return False
    if has := check.get("has"):
        text = " ".join(_texts([sheet.get("title"), blocks])).casefold()
        return has.casefold() in text
    return True


def schedule_of(at: str, days: str) -> dict[str, str] | str:
    """`{at: "HH:MM", days}` or the reason it isn't one."""
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at.strip()):
        return "at must be HH:MM"
    if days not in _DAYS:
        return "days must be daily, weekdays or weekends"
    hour, minute = at.strip().split(":")
    return {"at": f"{int(hour):02d}:{minute}", "days": days}


def due(schedule: dict[str, str] | None, local: datetime, last_run_on: date | None) -> bool:
    """Its time today has come and it hasn't run today."""
    if not schedule or last_run_on == local.date():
        return False
    hour, minute = (int(x) for x in schedule["at"].split(":"))
    return local.weekday() in _DAYS.get(schedule["days"], ()) and local.time() >= time(hour, minute)


def withers(status: str, last_used: datetime | None, enabled: datetime | None, now: datetime) -> bool:
    """An `on` skill unused for 30 days (since it was turned on, if never used)."""
    since = last_used or enabled
    return status == "on" and since is not None and now - since >= WITHER_AFTER


def lines_of(sheet: dict[str, Any], limit: int = 300) -> str:
    """A sheet as one short line for a notify: its texts, facts and stats, in order."""
    parts: list[str] = []
    for b in sheet.get("blocks") or []:
        kind = b.get("type")
        if kind == "text":
            parts.append(str(b.get("text")))
        elif kind in ("fact", "stat"):
            parts.append(
                f"{b.get('label')}: {b.get('value')}{(' ' + str(b['unit'])) if b.get('unit') else ''}"
            )
        elif kind == "list":
            parts.append(", ".join(str(i) for i in b.get("items") or []))
        elif kind == "table":
            parts += [" ".join(str(c) for c in row) for row in (b.get("rows") or [])[:3]]
    return " · ".join(p for p in parts if p)[:limit]


def minimise(text: str, said: str) -> str:
    """RAIL: a name, an address, a link or a number stays only if the user said it; the rest is `<x>`."""
    heard = said.casefold()

    def keep(m: re.Match[str]) -> str:
        return m.group(0) if m.group(0).casefold() in heard else "<x>"

    return _NAMEISH.sub(keep, text)


# what a skill's reads bring in. Email, the web and the map are outside text: the
# sheet's lines reach the model in a block of that kind, so every later write in the turn holds; Pisa's own
# lists stay own.
OUTSIDE_READS = {
    "search_my_email": "email-results",
    "web_search": "web-results",
    "read_page": "web-results",
    "map_search": "map-results",
    # the public feeds' words (an agency's alert, a stop's name) are outside text too
    **dict.fromkeys(views.PUBLIC_READS, "public-data"),
}
_ORDER = ("email-results", "web-results", "map-results", "public-data")
_NO_TIMER = ("email-results", "web-results", "map-results")


def read_kind(reads: list[dict[str, Any]] | None) -> str:
    """The data block kind for what these reads brought: the most outside one; `tasks` for the agenda, the
    plan and the tasks (their titles may come from invites); `action` only when every read is Pisa's own
    (time, weather)."""
    tools = {str(r.get("tool")) for r in reads or [] if isinstance(r, dict)}
    kinds = {OUTSIDE_READS[t] for t in tools if t in OUTSIDE_READS}
    for kind in _ORDER:
        if kind in kinds:
            return kind
    # the plan lists the day's events too (invites' titles): a list, checked as the agenda
    return "tasks" if tools & {"get_agenda", "get_tasks", "get_plan"} else "action"


def reads_outside(reads: list[dict[str, Any]] | None) -> bool:
    """Never on a timer: email, the web, the map. A
    skill that reads public data runs on their schedule only when EVERY read of its recipe is a public
    read."""
    tools = [str(r.get("tool")) for r in reads or [] if isinstance(r, dict)]
    if any(t in views.PUBLIC_READS for t in tools):
        return not all(t in views.PUBLIC_READS for t in tools)
    return read_kind(reads) in _NO_TIMER


# a scheduled public skill's push is code's words only: the sheet's lines verbatim and the
# source's credit (CC BY and BY-SA ask for it), never a model's
_CREDIT = re.compile(r"^(?:Dati|Data|Orari):")


def scheduled_text(name: str, sheet: dict[str, Any], reads: list[dict[str, Any]] | None) -> str:
    """The push of a scheduled skill: its lines; a public skill's credit kept whole at the end (taken out of
    the lines so a cut never drops it)."""
    tools = [str(r.get("tool")) for r in reads or [] if isinstance(r, dict)]
    if not tools or not all(t in views.PUBLIC_READS for t in tools):
        return f"{name}: {lines_of(sheet)}"[:1000]
    credits = [str(b.get("text")) for b in sheet.get("blocks") or []
               if b.get("type") == "text" and _CREDIT.match(str(b.get("text") or ""))]  # fmt: skip
    body = {
        **sheet,
        "blocks": [b for b in sheet.get("blocks") or [] if str(b.get("text") or "") not in credits],
    }
    credit = "; ".join(dict.fromkeys(credits)) or public_sources.TOOL_CREDIT
    return f"{name}: {lines_of(body, 300)} ({credit})"[:1000]


def reads_text(reads: list[dict[str, Any]] | None) -> str:
    """What a skill reads, as the phone shows it before Face ID: "search_my_email(banca),
    get_weather(Roma)"."""
    parts = []
    for r in reads or []:
        args = ", ".join(f"{k}={v}" for k, v in sorted((r.get("args") or {}).items()))
        parts.append(f"{r.get('tool')}({args})")
    return ", ".join(parts)[:300] or "-"


def digest(kind: str, recipe: Any, checks: Any) -> str:
    """What the phone's signature turns on: the skill's kind, recipe and checks, canonical."""
    canonical = json.dumps(
        {"kind": kind, "recipe": recipe, "checks": checks}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
