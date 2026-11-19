"""What Pisa can do on this install, said from what is configured, never from a list written once. Pure rules,
no I/O.

"Cosa puoi fare?" was answered with mail, WhatsApp, calls and bookings on an install with none of them
connected, and the list said twice. Now:
- `asks`: their question about what Pisa can do (a closed set of phrasings); code answers it (`answer`), once,
  from `Facts` (the connectors' states, what the phone's app handles, the place and web keys, push, the voice
  mode), with what is not connected yet and where they connect it. No model round: nothing to overclaim.
- RAIL `overclaim`: any other reply's sentence that says Pisa can do something this install can't (mail or
  the calendar with Google not connected, a message or a call the phone's app can't open, a booking: never) is
  held, and the turn ends with the honest line (`not_connected`).
"""

import re
from dataclasses import dataclass

from pisa.domain import locale


@dataclass(frozen=True)
class Facts:
    """This install now, for this device."""

    google: bool = False  # Google connected (Gmail and Calendar)
    tasks: bool = True  # Pisa's own tasks, reminders and plan
    maps: bool = False  # a place provider has its key (Apple Maps or Google Maps): places, travel times
    details: bool = False  # Google Maps: hours and reviews
    web: bool = False  # Tavily's key: web research
    directions: bool = False  # the phone's app opens maps (pisa.directions)
    messages: bool = False  # the phone's app opens WhatsApp or Messages with the text (pisa.compose)
    calls: bool = False  # the phone's app dials after the user's yes (pisa.call)
    health: bool = False  # Apple Health or Garmin on
    bank: bool = False  # a bank connected (read-only)
    push: bool = False  # this phone gets notifications
    voice: str = "natural"  # natural or economy
    talker: str | None = None  # natural mode's talker (gemini, xai)


_ASKS = re.compile(
    r"\b(?:(?:che\s+)?cosa|che)\s+(?:sai|puoi|riesci\s+a)\s+fare\b"
    r"|\bcosa\s+fai\s+(?:per\s+me|di\s+preciso)\b"
    r"|\bin\s+cosa\s+(?:mi\s+)?(?:puoi|sai)\s+aiutar\w*"
    r"|\bcome\s+(?:mi\s+)?(?:puoi|potresti)\s+aiutar\w*"
    r"|\bquali\s+sono\s+le\s+tue\s+(?:funzioni|capacit\w+)"
    r"|\bwhat\s+(?:can|could)\s+you\s+do\b"
    r"|\bwhat\s+are\s+you\s+able\s+to\s+do\b"
    r"|\bhow\s+can\s+you\s+help\b"
    r"|\bwhat\s+are\s+your\s+(?:features|abilities|capabilities)\b",
    re.IGNORECASE,
)


def asks(text: str) -> bool:
    """Their words ask what Pisa can do ("cosa puoi fare?", "what can you do?"): a short question, no task."""
    words = text.split()
    return 0 < len(words) <= 12 and _ASKS.search(text) is not None


_CAN: dict[str, dict[str, str]] = {
    "it": {
        "tasks": "le tue attività, i promemoria e il piano della giornata",
        "google": "la posta di Gmail (la cerco, la leggo, preparo le risposte: le mando solo dopo il tuo sì) "
        "e il calendario",
        "maps": "luoghi e tempi di viaggio",
        "details": "orari e recensioni dei locali",
        "web": "ricerche sul web",
        "weather": "il meteo dei prossimi 7 giorni",
        "directions": "le indicazioni sulle mappe del telefono",
        "messages": "messaggi WhatsApp o iMessage che preparo io e mandi tu",
        "calls": "chiamate dal telefono, dopo la tua conferma",
        "health": "i dati di salute (sonno e attività)",
        "bank": "i movimenti del conto, in sola lettura",
        "push": "gli avvisi con le notifiche",
        "natural": "la conversazione a voce",
        "economy": "la conversazione a voce (modalità economica)",
    },
    "en": {
        "tasks": "your tasks, reminders and the day's plan",
        "google": "your Gmail (I search it, read it and draft replies: sent only after your yes) and your "
        "calendar",
        "maps": "places and travel times",
        "details": "opening hours and reviews",
        "web": "web research",
        "weather": "the weather for the next 7 days",
        "directions": "directions in your phone's maps",
        "messages": "WhatsApp or iMessage messages I prepare and you send",
        "calls": "calls from your phone, after you confirm",
        "health": "your health data (sleep and activity)",
        "bank": "your bank transactions, read-only",
        "push": "alerts as notifications",
        "natural": "voice conversation",
        "economy": "voice conversation (economy mode)",
    },
}
_MISSING: dict[str, dict[str, str]] = {
    "it": {
        "google": "Google, per posta e calendario (lo colleghi nell'app: Impostazioni, Connettori)",
        "places": "mappe e ricerche web (servono le chiavi di Apple Maps, Google Maps o Tavily: "
        "Impostazioni, Luoghi e ricerca)",
        "phone": "messaggi e chiamate dal telefono (l'app di questo telefono non li apre ancora)",
        "push": "le notifiche (le attivi nell'app: Impostazioni, Notifiche)",
    },
    "en": {
        "google": "Google, for mail and calendar (connect it in the app: Settings, Connectors)",
        "places": "maps and web research (they need an Apple Maps, Google Maps or Tavily key: Settings, "
        "Places and search)",
        "phone": "messages and calls from the phone (this phone's app doesn't open them yet)",
        "push": "notifications (turn them on in the app: Settings, Notifications)",
    },
}
_HEAD = {
    "it": ("Qui mi occupo di: {can}.", "Non ancora collegati: {missing}.", "Non prenoto e non pago "
           "nulla al posto tuo."),
    "en": ("Here I can handle {can}.", "Not connected yet: {missing}.", "I don't book or pay for anything "
           "on your behalf."),
}  # fmt: skip


def _join(items: list[str], lang: str) -> str:
    if len(items) < 2:
        return "".join(items)
    last = " e " if lang == "it" else " and "
    return "; ".join(items[:-1]) + last + items[-1]


def can(f: Facts, lang: str) -> list[str]:
    """What Pisa does on this install, in `lang`, best first."""
    words = locale.pick(_CAN, lang)
    keys = [
        k
        for k, on in (
            ("tasks", f.tasks),
            ("google", f.google),
            ("maps", f.maps),
            ("details", f.details),
            ("web", f.web),
            ("weather", True),
            ("directions", f.directions),
            ("messages", f.messages),
            ("calls", f.calls),
            ("health", f.health),
            ("bank", f.bank),
            ("push", f.push),
            ("economy" if f.voice == "economy" else "natural", True),
        )
        if on
    ]
    return [words[k] for k in keys]


def missing(f: Facts, lang: str) -> list[str]:
    """What is not connected yet, each with where they connect it, in `lang`."""
    words = locale.pick(_MISSING, lang)
    keys = [
        k
        for k, off in (
            ("google", not f.google),
            ("places", not (f.maps or f.web)),
            ("phone", not (f.messages or f.calls)),
            ("push", not f.push),
        )
        if off
    ]
    return [words[k] for k in keys]


def not_connected(f: Facts, lang: str) -> str:
    """The one line naming what is not connected yet and where (empty: everything is)."""
    gone = missing(f, lang)
    return locale.pick(_HEAD, lang)[1].format(missing=_join(gone, locale.short(lang))) if gone else ""


def answer(f: Facts, lang: str) -> str:
    """Pisa's answer to "cosa puoi fare?": what it does here, what is not connected yet and where, and that it
    never books or pays. Each said once."""
    short = locale.short(lang)
    head, _, never = locale.pick(_HEAD, lang)
    lines = [head.format(can=_join(can(f, lang), short))]
    if line := not_connected(f, lang):
        lines.append(line)
    lines.append(never)
    return " ".join(lines)


# --- RAIL: a claim of what this install can't do ----------------------------------------------------------

ABLE_REACH = 60  # characters after "posso"/"I can" its object is looked for in
_ABLE = re.compile(
    r"\b(?:posso|possiamo|so|sono\s+in\s+grado\s+di|riesco\s+a|ti\s+aiuto\s+a|i\s+can|i'm\s+able\s+to|"
    r"i\s+am\s+able\s+to|i\s+could|we\s+can|gestisco|mi\s+occupo(?:\s+anche)?\s+di|leggo|rispondo|scrivo|"
    r"chiamo|prenoto|i\s+(?:handle|manage|read|reply|write|call|book)|i\s+take\s+care\s+of)\b",
    re.IGNORECASE,
)
_NOT = re.compile(
    r"\b(?:non|not|can't|cannot|no|ancora|yet|colleg\w*|connect\w*|senza|without|se\s+(?:colleghi|attivi)|"
    r"once\s+you|if\s+you)\b",
    re.IGNORECASE,
)
_MAIL = re.compile(r"\b(?:e-?mail\w*|mail|posta|gmail|inbox)\b", re.IGNORECASE)
_CALENDAR = re.compile(r"\b(?:calendari\w*|calendar\w*)\b", re.IGNORECASE)
_MESSAGES = re.compile(r"\b(?:whats\s?app|imessage|sms|messaggi\w*|messages?|texts?)\b", re.IGNORECASE)
_CALLS = re.compile(r"\b(?:chiam\w+|telefon\w+|calls?|calling|dial\w*|phone\s+(?:a|the)\b)", re.IGNORECASE)
_BOOK = re.compile(r"\b(?:prenot\w+|book(?:s|ing)?|reserv\w+|pag\w+\s+(?:per\s+te|al\s+posto)|pay\s+for)\b",
                   re.IGNORECASE)  # fmt: skip


def overclaim(sentence: str, f: Facts, *, asked: bool = False) -> str | None:
    """What a sentence claims Pisa can do that this install can't (None: nothing). A sentence that says it
    isn't connected, or how to connect it, claims nothing. `asked`: the turn asked what Pisa can do, so a
    listed ability counts without "posso"/"I can" ("Leggere le email, scrivere su WhatsApp…")."""
    able = _ABLE.search(sentence)
    if _NOT.search(sentence) or not (asked or able):
        return None
    # unasked, the ability's object follows its "posso" closely ("Posso rispondere alle tue email")
    scope = sentence if asked or able is None else sentence[able.end() : able.end() + ABLE_REACH]
    for name, pattern, have in (
        ("mail", _MAIL, f.google),
        ("calendar", _CALENDAR, f.google),
        ("messages", _MESSAGES, f.messages),
        ("calls", _CALLS, f.calls),
        ("booking", _BOOK, False),
    ):
        if not have and pattern.search(scope):
            return name
    return None
