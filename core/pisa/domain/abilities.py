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
- RAIL `overclaim`, a feature that is off (`domain.experimental`): a sentence that sends them to Settings to
  connect it, or offers to walk them through it ("…sezione 'Collegamenti' nelle impostazioni, dove puoi
  scegliere la tua banca", "Tell me which bank you use and I'll walk you through the steps", the audit) is
  held, and the turn ends with "<label> isn't available on PISA." (`experimental.unavailable`), once.
"""

import dataclasses
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pisa.domain import experimental, locale, reminders
from pisa.domain.reminders import SaidMoment


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
    # the items of an experimental feature that is off (`hidden`): they don't exist on this install, so they
    # are neither said as something Pisa does nor named as "not connected yet"
    off: frozenset[str] = frozenset()
    # the features off on this install (`domain.experimental` ids), and those of them their words name
    # ("collega il mio conto in banca": bank): never sent to Settings for, never offered to set up
    unavailable: frozenset[str] = frozenset()
    asked_off: tuple[str, ...] = ()


# each item `can` and `missing` name, by the experimental features that make it (`domain.experimental`): an
# item none of whose features is on is hidden. An item not here (tasks, weather, push, the voice) is
# always there.
FEATURES: dict[str, tuple[str, ...]] = {
    "google": ("google",),
    "maps": ("places",),
    "details": ("places",),
    "web": ("places",),
    "directions": ("places",),
    "places": ("places",),
    "messages": ("phone_actions",),
    "calls": ("phone_actions",),
    "phone": ("phone_actions",),
    "health": ("apple_health", "garmin", "polar", "withings"),
    "bank": ("bank",),
    "reminders": ("reminders",),
}


def hidden(on: Callable[[str], bool]) -> frozenset[str]:
    """The items whose experimental features are all off (`on`: whether one feature is on)."""
    return frozenset(item for item, features in FEATURES.items() if not any(on(f) for f in features))


def without(f: Facts) -> Facts:
    """The facts with each hidden item (`Facts.off`) counted as absent."""
    absent = {name: False for name in ("google", "maps", "details", "web", "directions", "messages", "calls",
                                       "health", "bank") if name in f.off}  # fmt: skip
    return dataclasses.replace(f, **absent)


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
        "tasks_only": "le tue attività e il piano della giornata",
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
        "tasks_only": "your tasks and the day's plan",
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
            ("tasks_only" if "reminders" in f.off else "tasks", f.tasks),
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
        if on and k not in f.off
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
        if off and k not in f.off
    ]
    return [words[k] for k in keys]


OFF_CLAIM = "off:"  # a held claim about a feature off on this install: "off:<its id>"


# a sentence that says, in its own words, that Pisa can't do it or it isn't there ("so I can't connect your
# account", "Garmin isn't supported", "non posso collegare la banca", "qui non c'è")
_SAID_UNAVAILABLE = re.compile(
    r"\b(?:(?:can't|cannot|can\s+not|won't\s+be\s+able\s+to|unable\s+to|not\s+able\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:connect|link|add|set\s+up|access|read)\w*|"
    r"(?:isn't|is\s+not|aren't|are\s+not|not)\s+(?:(?:yet|currently)\s+)?(?:available|supported|enabled|"
    r"offered|on\s+(?:this\s+server|pisa))|switched\s+off|turned\s+off|disabled|doesn't\s+(?:exist|support)|"
    r"non\s+(?:posso|riesco\s+a|si\s+può|è\s+possibile)\s+(?:\w+\s+){0,2}?(?:collega|connett|aggiung|"
    r"attiva|legge|accede)\w*|"
    r"non\s+(?:è|sono)\s+(?:ancora\s+)?(?:disponibil\w*|support\w*|attiv\w*|previst\w*|present\w*)|"
    r"non\s+esist\w*|non\s+c'è|non\s+ci\s+sono|spent[aoei]|disattivat\w*)\b",
    re.IGNORECASE,
)


def _says_unavailable(feature: str, said: str) -> bool:
    """The reply already says the feature is off, in any words: a sentence that says it can't or isn't there
    and names the feature (its words, `experimental.named`, or its label), or names none while another of
    the reply's sentences names it ("Your bank is with Fineco. So I can't connect your account.")."""
    sentences = re.split(r"(?<=[.!?])\s+", said.replace("\u2019", "'"))
    names = [set(experimental.named(x)) | ({feature} if _labelled(feature, x) else set()) for x in sentences]
    if not any(feature in n for n in names):
        return False
    return any(
        _SAID_UNAVAILABLE.search(x) and (feature in n or not n) for x, n in zip(sentences, names, strict=True)
    )


def _labelled(feature: str, sentence: str) -> bool:
    plain = sentence.casefold()
    return any(experimental.label(feature, lang).casefold() in plain for lang in ("en", "it"))


def unavailable_lines(claimed: Iterable[str], lang: str, *, said: str = "") -> str:
    """The line for each off feature a held sentence was about, once each ("Bank linking isn't available on
    PISA."), in `lang`; one the reply already says goes, in these words or its own (`_says_unavailable`)."""
    features = dict.fromkeys(c.removeprefix(OFF_CLAIM) for c in claimed if c.startswith(OFF_CLAIM))
    lines = [(x, experimental.unavailable(x, lang)) for x in features]
    return " ".join(line for x, line in lines if line not in said and not _says_unavailable(x, said))


# what a held claim is about, as `missing` names it ("booking": never a line, Pisa never books)
_CLAIM_ITEM = {"mail": "google", "calendar": "google", "messages": "phone", "calls": "phone"}
# an item the reply already says isn't connected ("Google isn't connected to Pisa", audit §13)
_SAID_OFF = {
    "google": re.compile(r"\b(?:google|gmail)\b", re.IGNORECASE),
    "phone": re.compile(r"\b(?:whats\s?app|imessage|messag\w*|chiamat\w*|calls?)\b", re.IGNORECASE),
}
_OFF = re.compile(
    r"\b(?:not\s+connected|isn't\s+connected|aren't\s+connected|not\s+linked|can't|cannot|"
    r"non\s+(?:è|sono)\s+(?:ancora\s+)?collegat\w*|non\s+(?:posso|riesco))\b",
    re.IGNORECASE,
)


def not_connected(f: Facts, lang: str, *, claimed: Iterable[str] | None = None, said: str = "") -> str:
    """The one line naming what is not connected yet and where (empty: everything is). `claimed`: only the
    items a held claim was about; `said`: the reply's own words, whose items already said to be off go."""
    keys = [
        k
        for k, off in (
            ("google", not f.google),
            ("places", not (f.maps or f.web)),
            ("phone", not (f.messages or f.calls)),
            ("push", not f.push),
        )
        if off and k not in f.off
    ]
    if claimed is not None:
        wanted = {_CLAIM_ITEM[c] for c in claimed if c in _CLAIM_ITEM}
        keys = [k for k in keys if k in wanted]
    sentences = re.split(r"(?<=[.!?])\s+", said)
    keys = [
        k for k in keys
        if not any(_OFF.search(x) and k in _SAID_OFF and _SAID_OFF[k].search(x) for x in sentences)
    ]  # fmt: skip
    words = locale.pick(_MISSING, lang)
    gone = [words[k] for k in keys]
    return locale.pick(_HEAD, lang)[1].format(missing=_join(gone, locale.short(lang))) if gone else ""


def never(lang: str) -> str:
    """That Pisa never books or pays, in `lang` (a held booking claim's line)."""
    return locale.pick(_HEAD, lang)[2]


def answer(f: Facts, lang: str) -> str:
    """Pisa's answer to "cosa puoi fare?": what it does here, what is not connected yet and where, and that it
    never books or pays. Each said once. A feature their question names that is off (`Facts.asked_off`, "cosa
    puoi fare con la banca?") is said not to be on PISA, never "not connected yet"."""
    short = locale.short(lang)
    head, _, never = locale.pick(_HEAD, lang)
    lines = [head.format(can=_join(can(f, lang), short))]
    if line := not_connected(f, lang):
        lines.append(line)
    lines += [experimental.unavailable(feature, lang) for feature in dict.fromkeys(f.asked_off)]
    lines.append(never)
    return " ".join(lines)


# --- RAIL: a claim of what this install can't do ----------------------------------------------------------

ABLE_REACH = 60  # characters after "posso"/"I can" its object is looked for in
_ABLE = re.compile(
    r"\b(?:posso|possiamo|so|sono\s+in\s+grado\s+di|riesco\s+a|ti\s+aiuto\s+a|i\s+can|i'm\s+able\s+to|"
    r"i\s+am\s+able\s+to|i\s+could|we\s+can|gestisco|mi\s+occupo(?:\s+anche)?\s+di|leggo|rispondo|scrivo|"
    r"chiamo|prenoto|i\s+(?:handle|manage|read|reply|write|call|book)|i\s+take\s+care\s+of|"
    # an offer to prepare it ("te lo prepari su WhatsApp?", "Lo preparo su WhatsApp per Tom?", audit §13)
    r"prepar(?:o|i|iamo|arti|argli|arle|arlo|arla|are)|i'll\s+(?:prepare|draft|write|send|text|call|book)|"
    r"shall\s+i|want\s+me\s+to|should\s+i)\b",
    re.IGNORECASE,
)
_NOT = re.compile(
    r"\b(?:non|not|can't|cannot|no|ancora|yet|colleg\w*|connect\w*|senza|without|se\s+(?:colleghi|attivi)|"
    r"once\s+you|if\s+you)\b",
    re.IGNORECASE,
)
# the done form of a message ("I've prepared a WhatsApp to Tom", "Ti ho preparato un WhatsApp", audit §14);
# only a message: "Ho preparato lo spostamento: la Call con Anna alle 16" is a plan, never a phone call
_DONE_MESSAGE = re.compile(
    r"\b(?:i've|i\s+have)\s+(?:prepared|drafted|written)\s+(?:you\s+)?(?:a|an|the|your)?\s*"
    r"(?:whats\s?app|imessage|message|text|sms)\b"
    r"|\b(?:ti\s+ho|te\s+l'ho)\s+preparat\w*\b[^.!?]{0,40}\b(?:whats\s?app|imessage|messaggi\w*|sms)\b",
    re.IGNORECASE,
)
_MAIL = re.compile(r"\b(?:e-?mail\w*|mail|posta|gmail|inbox)\b", re.IGNORECASE)
_CALENDAR = re.compile(r"\b(?:calendari\w*|calendar\w*)\b", re.IGNORECASE)
_MESSAGES = re.compile(r"\b(?:whats\s?app|imessage|sms|messaggi\w*|messages?|texts?)\b", re.IGNORECASE)
_CALLS = re.compile(r"\b(?:chiam\w+|telefon\w+|calls?|calling|dial\w*|phone\s+(?:a|the)\b)", re.IGNORECASE)
_BOOK = re.compile(r"\b(?:prenot\w+|book(?:s|ing)?|reserv\w+|pag\w+\s+(?:per\s+te|al\s+posto)|pay\s+for)\b",
                   re.IGNORECASE)  # fmt: skip


ASKS_WORDS = 5  # "Want that?", "Ti va?", "Lo preparo?", "Shall I do it?": a short question


def asks_about_it(sentence: str) -> bool:
    """A short question that only points at what was just offered ("Want that?", "Ti va?", "Lo preparo?"):
    after a held offer it asks about something no longer said (audit §14)."""
    text = sentence.strip()
    return text.endswith("?") and len(re.findall(r"[\w']+", text)) <= ASKS_WORDS


# a reminder said as set ("Ti ricorderò domani alle 9", "Ho impostato un promemoria", "I'll remind you"): with
# reminders off (experimental), nothing will fire, so it is never said
_REMIND = re.compile(
    r"\b(?:ti\s+ricorder\w+|te\s+lo\s+ricordo|ti\s+avviser\w+|ti\s+avviso|ho\s+(?:impostato|messo|creato)\s+"
    r"(?:un\s+|il\s+)?promemoria|promemoria\s+(?:impostato|creato|fissato)|i'll\s+remind\s+you|i\s+will\s+"
    r"remind\s+you|reminder\s+(?:is\s+)?set|i've\s+set\s+(?:a|the|your)\s+reminder|i\s+set\s+(?:a|the)\s+reminder)\b",
    re.IGNORECASE,
)
# the same promise in other words: "domani alle 9 ti ricordo di…", "ti ricordo io", "riceverai un promemoria",
# "I'll ping you at 9", "you'll get a reminder" (a bare "ti ricordo che…" reminds now, and stays)
_LATER = (
    r"(?:domani|dopodomani|stasera|stanotte|più\s+tardi|alle\s+\d{1,2}|tra\s+\d+\s+\w+"
    r"|tra\s+(?:un|una|mezz)\w*\s+\w+)"
)
_REMIND_LATER = re.compile(
    rf"\b{_LATER}\b[^.!?]{{0,40}}\b(?:ti|te\s+lo)\s+ricordo\b|\bti\s+ricordo\s+io\b"
    r"|\b(?:ti\s+mand(?:o|erò)|riceverai|ti\s+arriver(?:à|anno))\s+(?:un\s+|una\s+|il\s+)?(?:promemoria|notific\w+)"
    r"|\b(?:i'll|i\s+will)\s+(?:ping|nudge|notify|alert|text)\s+you\b|\b(?:i'll|i\s+will)\s+send\s+you\s+a\s+"
    r"(?:reminder|notification)|\byou'll\s+(?:get|receive)\s+a\s+(?:reminder|notification)\b"
    r"|\b(?:i'll|i\s+will)\s+let\s+you\s+know\s+(?:at|tomorrow|tonight|in\s+\d)",
    re.IGNORECASE,
)
# a reminder offered ("I can set a reminder", "want me to remind you", "posso ricordartelo", "ti metto un
# promemoria?"): with reminders off, no reminder can be set, so it is never offered (the sentence goes,
# nothing is said in its place). "Ti ricordo che domani hai il dentista" reminds now, and stays.
_REMIND_OFFER = re.compile(
    r"\b(?:i\s+can|i\s+could)\s+(?:also\s+)?(?:set|add|create|schedule|make)\s+(?:you\s+)?(?:a|an|the)\s+reminder"
    r"|\b(?:i\s+can|i\s+could)\s+(?:also\s+)?remind\s+you\b"
    r"|\b(?:want|would\s+you\s+like|do\s+you\s+want)\s+me\s+to\s+(?:remind\s+you|set\s+(?:a|the)\s+reminder)"
    r"|\b(?:shall|should)\s+i\s+(?:remind\s+you|set\s+(?:a|the)\s+reminder)"
    r"|\bposso\s+(?:anche\s+)?ricordart\w*|\bvuoi\s+che\s+te\s+lo\s+ricordi|\bvuoi\s+che\s+ti\s+ricordi"
    r"|\bse\s+vuoi\s+(?:te\s+lo|ti)\s+ricordo\b"
    r"|\bposso\s+(?:anche\s+)?(?:impostart\w*|mettert\w*|mandart\w*|crear\w*|impostar\w*|mettere|mandare)"
    r"\s+(?:un\s+|il\s+)?promemoria|\bti\s+(?:metto|imposto|mando)\s+(?:un\s+)?promemoria\b[^.!]*\?",
    re.IGNORECASE,
)
_UNAVAILABLE = {
    "en": {"reminders": "Reminders aren't available on this server yet."},
    "it": {"reminders": "I promemoria non sono ancora disponibili su questo server."},
}


def unavailable(item: str, lang: str) -> str:
    """That an experimental feature that is off isn't there yet, in `lang`."""
    return locale.pick(_UNAVAILABLE, lang)[item]


_KEPT_NO_ALERT = {
    "en": "I've added «{title}» for {when}, but you won't get a reminder: reminders aren't available on this "
    "server yet.",
    "it": "Ho aggiunto «{title}» per {when}, ma non riceverai un promemoria: i promemoria non sono ancora "
    "disponibili su questo server.",
}


def kept_no_alert(title: str, when: str, lang: str) -> str:
    """What was kept for a time and that no reminder comes (reminders off), in `lang`: the held promise's
    line. `title`: the stored title (their words); `when`: the time as people say it."""
    return locale.pick(_KEPT_NO_ALERT, lang).format(title=title, when=when)


# a sentence sending them to Settings to connect something, or to a page where they would add it
_SETUP = re.compile(
    r"\b(?:impostazion\w*|settings|collegament\w*|connettor\w*|connectors?|colleg(?:a|are|arla|arlo|arli|"
    r"arle|ata|ato|ate|ati|hi|hiamo)|connect\w*|link(?:s|ed|ing)?|add\s+it|aggiunger\w*|scegli\w*|"
    r"choose|select\s+your|configur\w*|set\s+(?:it\s+)?up|attivar\w*)\b",
    re.IGNORECASE,
)
# an offer to take them through setting it up
_WALK = re.compile(
    r"\b(?:walk\s+you\s+through|guide\s+you|step\s+by\s+step|the\s+steps|ti\s+guid\w*|ti\s+spieg\w*\s+come|"
    r"passo\s+passo|i\s+passaggi|ti\s+accompagn\w*|tell\s+me\s+which|dimmi\s+(?:quale|che|qual\s+è))\b",
    re.IGNORECASE,
)
# a sentence that says it is off: honest, never held
_SAYS_OFF = re.compile(
    r"\b(?:(?:isn't|is\s+not|aren't|are\s+not|not)\s+(?:available|on\s+(?:this\s+server|pisa))|switched\s+off|"
    r"turned\s+off|doesn't\s+exist|non\s+(?:è|sono)\s+(?:disponibil\w*|attiv\w*|previst\w*|present\w*)|"
    r"non\s+esist\w*|non\s+c'è|spent[aoei]|disattivat\w*)\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:non|not|can't|cannot|no)\b", re.IGNORECASE)


def sends_to_off(sentence: str, f: Facts) -> str | None:
    """The feature off on this install a sentence sends them to Settings for, or offers to set up or walk
    them through (None: none): one the sentence names, or, when it names no feature, one their words named.
    A sentence that says it is off, or names only features that are on, is never held."""
    if not f.unavailable or _SAYS_OFF.search(sentence):
        return None
    named = experimental.named(sentence)
    off = [x for x in (named or f.asked_off) if x in f.unavailable]
    if not off:
        return None
    if _SETUP.search(sentence) or _WALK.search(sentence):
        return off[0]
    if _ABLE.search(sentence) and not _NEGATED.search(sentence) and any(x in f.unavailable for x in named):
        return off[0]  # "Posso leggere i movimenti della tua banca": only when it names the feature itself
    return None


def overclaim(sentence: str, f: Facts, *, asked: bool = False) -> str | None:
    """What a sentence claims Pisa can do that this install can't (None: nothing). A sentence that says it
    isn't connected, or how to connect it, claims nothing, unless the feature is off on this install
    (`sends_to_off`: "off:<id>"). `asked`: the turn asked what Pisa can do, so a listed ability counts without
    "posso"/"I can" ("Leggere le email, scrivere su WhatsApp…")."""
    if (feature := sends_to_off(sentence, f)) is not None:
        return f"off:{feature}"
    if not f.messages and _DONE_MESSAGE.search(sentence) and not _NOT.search(sentence):
        return "messages"
    if (
        "reminders" in f.off
        and (_REMIND.search(sentence) or _REMIND_LATER.search(sentence))
        and not re.search(r"\b(?:non|not|can't|cannot)\b", sentence, re.I)
    ):
        return "reminders"
    if "reminders" in f.off and _REMIND_OFFER.search(sentence):
        return "reminder_offer"  # held, and nothing said in its place
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


# --- RAIL: a promise for later with no write behind it ----------------------------------------------------

# "I'll take care of it" for a time ("al mattino ci penso io", "domattina me ne occupo io"): a promise of
# later as much as "ti ricorderò"; only with a time it names (a bare "ci penso io" is about now)
_TAKE_CARE = re.compile(
    r"\b(?:ci\s+penso\s+io|me\s+ne\s+occupo\s+io|i'll\s+take\s+care\s+of\s+(?:it|that|this)|"
    r"i\s+will\s+take\s+care\s+of\s+(?:it|that|this)|i'll\s+handle\s+(?:it|that)|leave\s+it\s+(?:to|with)\s+me)\b",
    re.IGNORECASE,
)
_WHEN = re.compile(
    r"\b(?:domattina|domani|dopodomani|stasera|stanotte|al\s+mattino|in\s+mattinata|più\s+tardi|alle\s+\d{1,2}|"
    r"tomorrow|tonight|in\s+the\s+morning|later|this\s+evening|first\s+thing|when\s+the\s+day\s+starts|"
    r"at\s+\d{1,2})\b",
    re.IGNORECASE,
)
# a promise said as one it can't keep ("non posso ricordartelo", "I can't remind you") promises nothing
_NEGATION = re.compile(
    r"\b(?:non\s+(?:ti|te|ci|posso|riesco|potrò)|can't|cannot|won't|not\s+able)\b", re.IGNORECASE
)
# the writes that make a reminder fire at its time or place (a task, timed or not, is on the list: no alert
# comes); a document's expiry is reminded by the vault itself
PROMISE_WRITES = frozenset({"remember_to", "keep_intention", "remember_at_place", "add_document"})


def promises_later(sentence: str) -> bool:
    """The sentence promises Pisa will remind them, or take care of it, at a later time ("I'll remind you
    when the day starts", "al mattino ci penso io", "ti avviso domani"). A question or a negation promises
    nothing."""
    text = sentence.replace("\u2019", "'").strip()
    if not text or text.endswith("?") or _NEGATION.search(text):
        return False
    if _REMIND.search(text) or _REMIND_LATER.search(text):
        return True
    return bool(_TAKE_CARE.search(text) and _WHEN.search(text))


# a reminder's result as `remember_to` gives it when it was stored ("Reminder set: «…» at 12:28 (id …)", or
# "… at 09:00 on Mon 23 Nov (id …)" for another day: `follow_tools._fire_words`)
_SET = re.compile(r"Reminder set: «[^»]*» at (\d{1,2}):(\d{2})(?: on [A-Za-z]{3} (\d{1,2}) ([A-Za-z]{3}))?")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DELAY_SLACK = timedelta(minutes=1)  # "tra 2 minuti": the stored minute was truncated, its turn's clock moved


def _set_for(results: Iterable[str], today: date | None) -> list[tuple[int, int, date | None]]:
    """(hour, minute, day) of each reminder this turn set; the day None when it can't be known."""
    found: list[tuple[int, int, date | None]] = []
    for r in results:
        for hour, minute, day, month in _SET.findall(r):
            on: date | None = today
            if day and today is not None and month.lower() in _MONTHS:
                try:
                    on = date(today.year, _MONTHS.index(month.lower()) + 1, int(day))
                except ValueError:
                    on = None
                if on is not None and on < today:
                    on = on.replace(year=today.year + 1)  # "on Fri 1 Jan" said in December
            elif day:
                on = None
            found.append((int(hour), int(minute), on))
    return found


def _matches(said: SaidMoment, stored: tuple[int, int, date | None], tz: ZoneInfo | None) -> bool:
    hour, minute, day = stored
    if said.at is not None:  # a delay: its minute, against the stored moment
        if day is None or tz is None:
            return True  # no moment to hold it against
        moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        return abs(moment - said.at) <= DELAY_SLACK
    if hour not in said.hours or minute != said.minute:
        return False
    return said.day is None or day is None or said.day == day


def promised_time_set(
    sentence: str, results: Iterable[str], now: datetime | None = None, tz: ZoneInfo | None = None
) -> bool:
    """A promise that names a moment ("te lo ricordo alle 12:28", "alle 9 e mezza", "at 9am", "tra 2
    minuti", "domani alle 9") stands only on a reminder set for that moment this turn (`remember_to`'s
    "Reminder set: … at 12:28 [on …]"); one naming no moment stands on any. The reply's words are read with
    `remember_to`'s grammar (`reminders.said_moments`); a delay is resolved from the turn's clock `now` to its
    minute (±1), a day it names must be the stored one; "alle 9" is 9 or 21 unless it says which."""
    said = reminders.said_moments(sentence, now, tz)
    if not said:
        return True
    today = now.astimezone(tz).date() if now is not None and tz is not None else None
    stored = _set_for(results, today)
    return all(any(_matches(s, one, tz) for one in stored) for s in said)


_LISTED_NO_ALERT = {
    "en": "It's on your list; you won't get an alert at a set time.",
    "it": "L'ho messo in lista; non ti arriverà un avviso a un'ora precisa.",
}


def listed_no_alert(lang: str) -> str:
    """A held promise's line after an untimed write (a task on their list): no alert comes at a time."""
    return locale.pick(_LISTED_NO_ALERT, lang)


# --- RAIL: "tap Send" with no button on their screen ------------------------------------------------------

_TAP = re.compile(
    r"\b(?:tap|press|hit|click)\s+[\"'«“]?(?:send|call|book|request|order|open|invia|chiama)\b"
    r"|\b(?:tocca|premi|clicca|schiaccia)\s+(?:su\s+)?[\"'«“]?(?:invia|chiama|prenota|ordina|apri|richiedi|send)\b"
    r"|\b(?:tap|press)\b[^.!?]{0,40}\bwhen\s+you(?:'re|\s+are)\s+ready\b"
    r"|\b(?:tocca|premi)\b[^.!?]{0,40}\bquando\s+(?:sei|sarai)\s+pront[oa]\b",
    re.IGNORECASE,
)


def says_tap(sentence: str) -> bool:
    """The sentence tells them to tap a button ("Tap Send when you're ready", "tocca Invia", "premi
    Invia")."""
    return bool(_TAP.search(sentence.replace("\u2019", "'")))


_NO_BUTTON = {
    "en": "This app can't show the button: send it yourself from your phone.",
    "it": "Quest'app non può mostrare il pulsante: mandalo tu dal telefono.",
}


def no_button(lang: str) -> str:
    """A held "tap Send"'s line: no button is on this app's screen; they do it themselves."""
    return locale.pick(_NO_BUTTON, lang)
