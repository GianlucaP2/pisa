"""The ledger: everything Pisa did in a week, why, from where, with its Undo. Pure rules:
the week's bounds, a line's kind and source, the three things that mattered, the Sunday card's sheet.

The ledger is a view over what Pisa already records (`pisa_decisions`, approvals, `done_by_presence`,
`task_bulk`); `pisa/services/ledger.py` reads them. Every line's `why` is Pisa's own (the brain's purpose) or
code's sentence, never an email's text; titles are the user's items (data, shown as Today shows them).
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pisa.domain import autonomy as autonomy_rules
from pisa.domain import unlocks as unlock_rules

Kind = Literal[
    "calendar",
    "plan",
    "task",
    "task_done",
    "event_done",
    "task_dismissed",
    "tasks_bulk",
    "email",
    "reminder",
    "vault",
    "bill",
    "money",
    "stop",
    "autonomy",
    "card",
    "skill",
    "other",
]
Source = Literal["owner_said", "owner_yes", "outside_text", "rule", "follow_up", "job"]
LINES_MAX = 200  # a week's lines
MATTERED = 3
UNDOABLE_MAX = 10
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")

# What a decision's tool did, as a line's kind (the calendar tools come with their approval).
_TOOL_KIND: dict[str, Kind] = {
    "create_event": "calendar",
    "move_event": "calendar",
    "cancel_event": "calendar",
    "undo_last_change": "calendar",
    "apply_option": "plan",
    "skip_item": "plan",
    "replan": "plan",
    "plan_stop": "plan",
    "undo_adjustment": "plan",
    "send_draft": "email",
    "draft_email": "email",
    "draft_reply": "email",
    "edit_draft": "email",
    "complete_task": "task_done",
    "aging": "task_dismissed",
    "remember_to": "reminder",
    "add_document": "vault",
    "vault_reminder": "vault",
    "bill_task": "bill",
    "money_task": "money",
    "stop_everything": "stop",
    "resume_everything": "stop",
    "autonomy_level": "autonomy",
    "sunday_card": "card",
    "skill_on": "skill",
}
# A move recorded by a tool whose line another record already gives (the voice close is in done_by_presence).
DUPLICATED = frozenset({"mark_done"})
# The decisions Pisa made by a rule of theirs or a job, outside any turn of the user.
_RULE_TOOLS = frozenset({"vault_reminder", "bill_task", "money_task", "autonomy_act"})
_JOB_TOOLS = frozenset({"sunday_card"})
_FOLLOW_TOOLS = frozenset({"aging"})
_TASK_PREFIX = (
    "add_task",
    "rename_task",
    "set_task_",
    "reschedule_task",
    "restore_task",
    "delete_task",
    "dismiss_task",
    "plan_task",
    "tasks_bulk",
)


def kind_of(tool: str) -> Kind:
    if tool in _TOOL_KIND:
        return _TOOL_KIND[tool]
    if tool.startswith(_TASK_PREFIX):
        return "task"
    return "other"


def source_of(tool: str, provenance: str, rail: str | None = None) -> Source:
    """RAIL: from code's provenance, never the model's word. A Pisa's initiative is a rule, a follow-up
    or a job by what made it; an act Pisa did at an autonomy level (rail `autonomy …`) is the user's standing
    rule."""
    if provenance == "outside_text":
        return "outside_text"
    if (rail or "").startswith("autonomy") or tool in _RULE_TOOLS:
        return "rule"
    if tool in _JOB_TOOLS:
        return "job"
    if tool in _FOLLOW_TOOLS:
        return "follow_up"
    if provenance == "owner_words":
        return "owner_said"
    if provenance == "pisa_proposal":
        return "owner_yes"
    return "rule"


def parse_week(text: str | None, today: date) -> date:
    """The Monday of an ISO week `YYYY-Www` (this week's when empty). ValueError when it isn't one."""
    if not text:
        return today - timedelta(days=today.weekday())
    m = _WEEK.match(text.strip())
    if m is None:
        raise ValueError("week must be YYYY-Www")
    return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)


def week_of(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


# The weight of a line for "the three things that mattered": their calendar first (other people saw it), then
# what Pisa did by a rule of theirs, then emails and tasks; a line undone never matters.
_WEIGHT: dict[str, int] = {
    "calendar": 6,
    "plan": 5,
    "bill": 5,
    "money": 5,
    "vault": 5,
    "email": 4,
    "event_done": 3,
    "task_done": 3,
    "autonomy": 3,
    "task_dismissed": 2,
    "tasks_bulk": 2,
    "task": 1,
}


def mattered(lines: list[dict[str, Any]], n: int = MATTERED) -> list[dict[str, Any]]:
    kept = [ln for ln in lines if not ln.get("undone") and ln["kind"] not in ("stop", "card")]
    ranked = sorted(kept, key=lambda ln: (_WEIGHT.get(ln["kind"], 0), ln["at"]), reverse=True)
    return ranked[:n]


_SHEET_WORDS = {
    "it-IT": {
        "title": "La tua settimana con Pisa",
        "done": "Cose fatte",
        "undone": "Annullate",
        "mattered": "Le tre che contano",
        "undo": "Puoi ancora annullare",
        "ask": "Posso smettere di chiedere?",
        "subs": "Abbonamenti visti",
        "empty": "Questa settimana non ho fatto niente da solo.",
        "learned": "Cosa ho imparato a fare per te",
        "keep": "Le tengo?",
    },
    "en-US": {
        "title": "Your week with Pisa",
        "done": "Things done",
        "undone": "Undone",
        "mattered": "The three that mattered",
        "undo": "You can still undo",
        "ask": "Can I stop asking?",
        "subs": "Subscriptions seen",
        "empty": "This week I did nothing on my own.",
        "learned": "What I learned to do for you",
        "keep": "Shall I keep them?",
    },
}
CARD_TEXT = {
    "it-IT": "La tua settimana con Pisa: {n} cose fatte{extra}.",
    "en-US": "Your week with Pisa: {n} things done{extra}.",
}
CARD_EXTRA = {"it-IT": ", {p} proposte", "en-US": ", {p} proposals"}


def words(lang: str) -> dict[str, str]:
    return _SHEET_WORDS["it-IT" if lang == "it-IT" else "en-US"]


def _item(text: str | None, limit: int = 200) -> str:
    return " ".join((text or "").split())[:limit] or "-"


def cue_line(counts: dict[str, int], lang: str) -> str | None:
    """ "the Sunday card counts them": Pisa's questions this week about how they are."""
    if not counts.get("asked"):
        return None
    if lang == "it-IT":
        return (
            f"Domande su come stai: {counts['asked']} (risposte {counts['answered']}, lasciate stare "
            f"{counts['waved']}, senza risposta {counts['unanswered']})."
        )
    return (
        f"Questions about how you are: {counts['asked']} (answered {counts['answered']}, waved away "
        f"{counts['waved']}, unanswered {counts['unanswered']})."
    )


# the cost of keeping. The families as they read them (the deletion's own)
FAMILY_SAID: dict[str, tuple[str, str]] = {
    "messages": ("conversazioni", "conversations"),
    "places": ("luoghi e GPS", "places and GPS"),
    "mail": ("mail", "mail"),
    "money": ("soldi", "money"),
    "records": ("registro di Pisa", "Pisa's records"),
    "memory": ("memoria di Pisa", "Pisa's memory"),
}
OFFSITE_STALE = timedelta(days=8)  # the off-site copy runs daily: none this long and Pisa asks again


def _size(n: int) -> str:
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{max(n, 0) // 1024} KB"


def store_line(size_bytes: int, kept: dict[str, int], lang: str) -> str:
    """The database's size and the rows kept per family (counts only)."""
    it = lang == "it-IT"
    rows = ", ".join(f"{FAMILY_SAID[f][0 if it else 1]} {n:,}".replace(",", "." if it else ",")
                     for f, n in kept.items() if f in FAMILY_SAID)  # fmt: skip
    head = f"Il tuo archivio: {_size(size_bytes)}" if it else f"Your store: {_size(size_bytes)}"
    return f"{head}; righe tenute: {rows}." if it else f"{head}; rows kept: {rows}."


def backup_ask(last: datetime | None, now: datetime, lang: str) -> str | None:
    """While no off-site copy reached its bucket in OFFSITE_STALE, the standing ask (None: on)."""
    if last is not None and now - last <= OFFSITE_STALE:
        return None
    if lang == "it-IT":
        return (
            "Tengo tutto finché non cancelli tu, ma il backup fuori sede non è attivo: se il server si "
            "perde, si perde tutto. Attivalo (15 minuti)."
        )
    return (
        "I keep everything until you delete it, but the off-site backup is not on: if the server is lost, "
        "everything is. Turn it on (15 minutes)."
    )


def sheet(
    lang: str,
    *,
    total: int,
    undone: int,
    top: list[dict[str, Any]],
    undoable: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    skills: dict[str, list[dict[str, str]]] | None = None,
    cost_line: str | None = None,
    unlocks: list[dict[str, Any]] | None = None,
    talk_line: str | None = None,
    cue_line: str | None = None,
    curious_line: str | None = None,
    store: str | None = None,
    backup: str | None = None,
    outcome_line: str | None = None,
    interruptions_line: str | None = None,
) -> dict[str, Any]:
    """The Sunday card as a sheet (`{title, blocks}`); the caller checks it with the protocol's
    model. The month's "Pisa cost you / found you" line right after the counts, then the
    abilities unlocked this week, each with its way back."""
    w = words(lang)
    blocks: list[dict[str, Any]] = [
        {"type": "stat", "label": w["done"], "value": str(total)},
        {"type": "stat", "label": w["undone"], "value": str(undone)},
    ]
    if backup:  # the standing ask, first, until the off-site copy is on
        blocks.append({"type": "text", "text": backup[:600]})
    if cost_line:
        blocks.append({"type": "text", "text": cost_line[:600]})
    if outcome_line:  # the week's AI spend over what they accepted (counts only)
        blocks.append({"type": "text", "text": outcome_line[:600]})
    if interruptions_line:  # "trattenute N / passate M", a line on this card
        blocks.append({"type": "text", "text": interruptions_line[:600]})
    if talk_line:  # the user loop: the week's talk report, one line of numbers
        blocks.append({"type": "text", "text": talk_line[:600]})
    if cue_line:  # the week's questions about how they are, counted
        blocks.append({"type": "text", "text": cue_line[:600]})
    if curious_line:  # il prurito's rows this week, both kinds counted
        blocks.append({"type": "text", "text": curious_line[:600]})
    if store:  # the cost of keeping, counts only
        blocks.append({"type": "text", "text": store[:600]})
    blocks += unlock_rules.blocks(unlocks or [], lang)
    if not total:
        blocks.append({"type": "text", "text": w["empty"]})
    if top:
        blocks += [
            {"type": "section", "title": w["mattered"]},
            {"type": "list", "items": [_item(ln.get("title") or ln.get("why")) for ln in top][:8]},
        ]
    if undoable:
        blocks += [
            {"type": "section", "title": w["undo"]},
            {"type": "list", "items": [_item(ln.get("title") or ln.get("why")) for ln in undoable][:8]},
        ]
    if proposals:  # one proposal per card, in the research's words
        blocks += autonomy_rules.card_blocks(proposals[0], lang)
    if subscriptions:
        blocks.append({"type": "section", "title": w["subs"]})
        blocks += [{"type": "text", "text": s["text"][:600]} for s in subscriptions[:4]]
    learned = (skills or {}).get("learned") or []
    withered = (skills or {}).get("withered") or []
    if learned:  # what Pisa learned to do for them this week
        blocks += [
            {"type": "section", "title": w["learned"]},
            {"type": "list", "items": [s["name"] for s in learned][:8]},
        ]
    if withered:  # the withered skills, "la tengo?"
        blocks.append({"type": "section", "title": w["keep"]})
        blocks += [{"type": "text", "text": s["text"][:600]} for s in withered[:4]]
    return {"title": w["title"], "blocks": blocks[:24]}


def money_text(amount: Decimal | None, currency: str | None, lang: str) -> str:
    """84,20 € / $61.07: as people read it."""
    if amount is None:
        return ""
    if lang == "it-IT":
        number = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{number} {'€' if currency == 'EUR' else currency or ''}".strip()
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "", "")
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency or ''}".strip()
