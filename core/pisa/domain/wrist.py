"""What the wrist may show,
and the rules of a turn that comes without the socket. Pure rules, no I/O.

- **Origin**: where the user's words or tap came from: `phone` (the default) or `watch` (the phone relaying
  the watch: the watch always reaches Pisa through the iPhone). A REST turn names it in its body; any
  REST call the phone relays sends `X-Pisa-Origin: watch`. The request's origin is a context variable, so
  every row the request writes (the conversation's line, Pisa's moves, a decision) carries it, never passed
  around.
- **A wrist turn is short** by code (as the `SHORT_ROUTES`), never by a prompt line: Pisa's first sentence,
  the rest on the phone; a question to the user is said whole (a yes answers only what Pisa said).
- **`notify.wrist`**: at most WRIST_MAX characters in their language, Pisa's own words templated by kind:
  never a model line, never outside text, never a person's name, an amount or a document. Empty at a sensitive
  place and for health: the watch then shows only the kind.
- **One interruption level rule**: the guardian's decision for every notice: `now` → time-sensitive,
  `breakpoint` → active, a digest and the quiet hours' summary → passive; their "never" shown silently →
  passive.
"""

from contextvars import ContextVar
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pisa.domain.health import health_place
from pisa.domain.learning import sensitive_place

Origin = Literal["phone", "watch"]
ORIGINS: frozenset[str] = frozenset({"phone", "watch"})
ORIGIN_HEADER = "X-Pisa-Origin"
# The request's or the run's origin: set by the REST dependency (`X-Pisa-Origin`, `POST /v2/runs` included);
# every row written under it carries it. A socket turn is the phone's (the watch holds no socket, TN3135).
origin: ContextVar[str] = ContextVar("pisa_origin", default="phone")

WRIST_MAX = 60

# the ledger line of something decided from the watch
FROM_WATCH = {"it-IT": "dall'orologio", "en-US": "from the watch"}

Level = Literal["time-sensitive", "active", "passive"]


def current() -> str:
    return origin.get()


def parse(value: str | None) -> str:
    """An `X-Pisa-Origin` value: `phone` when absent; anything else than phone or watch is refused by the
    caller (400 validation_failed)."""
    return (value or "phone").strip().lower() or "phone"


def from_line(where: str | None, lang: str) -> str:
    """The "from where": "dall'orologio" for a line decided on the watch, '' for the phone's."""
    return FROM_WATCH.get(lang, FROM_WATCH["en-US"]) if where == "watch" else ""


# --- notify.wrist -------------------------------------------------------------------------------------------

_WRIST: dict[str, dict[str, str]] = {
    "it-IT": {
        "reminder": "Promemoria",
        "reminder_at": "Promemoria · alle {at}",
        "leave_now": "È ora di uscire",
        "alert": "Un avviso da Pisa",
        "briefing": "Il punto di Pisa",
        "replan": "La giornata è cambiata",
        "info": "Una nota di Pisa",
        "answer": "Pisa ha risposto · sul telefono",
        "check_in": "Com'è andata? Rispondi qui",
        "done": "Segnato come fatto · Annulla sul telefono",
        "card": "La settimana di Pisa è pronta",
        "ritual": "Da vedere sul telefono",
        "weekly": "Tre cose da confermare",
        "digest": "Qualche nota di Pisa",
    },
    "en-US": {
        "reminder": "Reminder",
        "reminder_at": "Reminder · at {at}",
        "leave_now": "Time to leave",
        "alert": "An alert from Pisa",
        "briefing": "Pisa's briefing",
        "replan": "Your day changed",
        "info": "A note from Pisa",
        "answer": "Pisa answered · on your phone",
        "check_in": "How did it go? Answer here",
        "done": "Marked done · Undo on your phone",
        "card": "Your week with Pisa is ready",
        "ritual": "On your phone",
        "weekly": "Three things to confirm",
        "digest": "A few notes from Pisa",
    },
}
# The approvals' kinds as the wrist names them (with the count of people their yes would notify)
_APPROVAL: dict[str, dict[str, str]] = {
    "it-IT": {
        "calendar_create": "Creare un evento",
        "calendar_move": "Spostare un evento",
        "calendar_cancel": "Annullare un evento",
        "email_send": "Mandare un'email",
        "other": "Una richiesta di Pisa",
        "notify_one": "avvisa 1 persona",
        "notify_many": "avvisa {n} persone",
    },
    "en-US": {
        "calendar_create": "Create an event",
        "calendar_move": "Move an event",
        "calendar_cancel": "Cancel an event",
        "email_send": "Send an email",
        "other": "A request from Pisa",
        "notify_one": "notifies 1 person",
        "notify_many": "notifies {n} people",
    },
}
# the guardian's paths whose notice is about one of their own items (a reminder, leaving for it, Pisa
# closing it, a place note, a cue): its title names their health ("Dentista alle 9:00") → nothing on the
# wrist. No guardian path is about health by its name.
_HEALTH_PATH = frozenset({"attend.reminder", "attend.leave", "attend.stay", "attend.intention", "place.note"})


def _words(lang: str) -> dict[str, str]:
    return _WRIST.get(lang, _WRIST["en-US"])


def _fit(text: str) -> str:
    return text if len(text) <= WRIST_MAX else text[: WRIST_MAX - 1].rstrip() + "…"


def withheld(
    *,
    here: tuple[str | None, str | None] | None,
    place: str | None,
    path: str | None,
    about: str | None = None,
) -> bool:
    """RAIL: nothing on the wrist while they are at a sensitive place (`here`: their open stay's
    name and category), about one (a `done` at the dentist's), or about their health (`about`: the item's own
    title on one of _HEALTH_PATH's paths, or with no path)."""
    if here is not None and sensitive_place(*here):
        return True
    if place and (sensitive_place(place, None) or health_place(place, None)):
        return True
    on_item = path is None or path in _HEALTH_PATH
    return bool(about) and on_item and (health_place(about, None) or sensitive_place(about, None))


def touches_sensitive(*texts: str | None) -> bool:
    """RAIL: a turn's words or Pisa's answer name their health or a sensitive place (code's lists, as
    `withheld`): the wrist shows the answer, never says it aloud."""
    return any(t and (health_place(t, None) or sensitive_place(t, None)) for t in texts)


def wrist(
    kind: str,
    lang: str,
    *,
    speak: str | None = None,
    event_at: datetime | None = None,
    timezone: str | None = None,
    turn: bool = False,
    ledger_week: str | None = None,
    ritual: str | None = None,
    weekly: bool | None = None,
    path: str | None = None,
    withhold: bool = False,
) -> str:
    """A notice's wrist line: Pisa's template for its kind, with at most a time; '' when withheld. Never the
    notice's text, never its title, place, person or amount."""
    if withhold:
        return ""
    w = _words(lang)
    if turn:
        line = w["answer"]
    elif ledger_week:
        line = w["card"]
    elif ritual:
        line = w["ritual"]
    elif weekly:
        line = w["weekly"]
    elif speak == "leave_now":
        line = w["leave_now"]
    elif kind == "reminder" and event_at is not None:
        local = event_at.astimezone(ZoneInfo(timezone)) if timezone else event_at  # on the user's clock
        line = w["reminder_at"].format(at=f"{local:%H:%M}")
    elif path in ("quiet.summary", "guardian.digest"):
        line = w["digest"]
    else:
        line = w.get(kind, w["info"])
    return _fit(line)


def approval_wrist(kind: str, notified: int, lang: str) -> str:
    """An approval on the wrist: its kind and how many people their yes would notify; never who, what or
    how much ("Spostare un evento · avvisa 3 persone")."""
    a = _APPROVAL.get(lang, _APPROVAL["en-US"])
    line = a.get(kind, a["other"])
    if notified == 1:
        line += " · " + a["notify_one"]
    elif notified > 1:
        line += " · " + a["notify_many"].format(n=notified)
    return _fit(line)


def notified(kind: str, payload: dict[str, object]) -> int:
    """How many people an approval's act would notify: an email's recipients; a calendar change's guests
    when the others are told (`send_updates` not "none"). A count only."""
    if kind == "email_send":  # the draft's recipients (to and cc), as the card names them
        return len(_list(payload.get("names")))
    if kind.startswith("calendar_"):
        if str(payload.get("send_updates") or "all") == "none":
            return 0
        named = _list(payload.get("notified"))
        if named:
            return len(named)
        guests = [g for g in _list(payload.get("attendees")) if not (isinstance(g, dict) and g.get("self"))]
        return len(guests)
    return 0


def _list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [x for x in value.split(",") if x.strip()]
    return []


# --- the interruption level --------------------------------------------------------------------


def level(decision: str | None, *, digest: bool = False, silent: bool = False) -> Level:
    """The one rule for every notice's `aps.interruption-level`, from the guardian's decision: `now` →
    time-sensitive, `breakpoint` (released at one) → active, a digest (the guardian's, the quiet hours'
    summary) → passive; their "never" shown silently → passive."""
    if silent or digest or decision in ("digest", "quiet"):
        return "passive"
    if decision == "now":
        return "time-sensitive"
    return "active"


# an approval has 10 minutes to be answered (seen on the wrist, decided on the phone)
APPROVAL_LEVEL: Level = "time-sensitive"
