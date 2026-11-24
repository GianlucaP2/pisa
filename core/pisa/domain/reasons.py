"""Every reason Pisa shows them is in their Settings language .

A model's free line (triage's task reason, an action's purpose) may come back in English whatever it was
asked, and lines stored before a check existed stay as they were. So a reason is checked where it leaves CORE,
not only where it is written: as written when it reads as their language (`tongue.speaks`), else Pisa's own
templated line for its category, never the raw English. Pure rules, no I/O.
"""

import functools
import re
from typing import TYPE_CHECKING, Literal, get_args

from pisa.domain.tongue import foreign, speaks

if TYPE_CHECKING:
    from pisa.domain.tasks import Task

KIND_WORDS: dict[str, tuple[str, str]] = {  # an email's kind: (Italian, English)
    "security": ("avvisi di sicurezza", "security alerts"),
    "storage": ("avvisi sullo spazio", "storage notices"),
    "auto_renewal": ("rinnovi automatici", "automatic renewals"),
    "developer": ("avvisi per sviluppatori", "developer notices"),
    "contact_check": ("verifiche dei contatti", "contact checks"),
    "action_required": ("richieste", "requests"),
    "event_or_booking": ("eventi e prenotazioni", "events and bookings"),
    "delivery": ("consegne", "deliveries"),
    "bill_or_deadline": ("pagamenti e scadenze", "bills and deadlines"),
    "info": ("notizie", "information"),
    "notification": ("notifiche", "notifications"),
    "account_breach": ("accessi al tuo account", "sign-ins to your account"),
}
# The account notices of (what systems send), as opposed to the triage's categories.
AccountNotice = Literal["security", "storage", "auto_renewal", "developer", "contact_check"]
NOTICE_KINDS: frozenset[str] = frozenset(get_args(AccountNotice))
# what the triage says an email is: none, an account notice, or a sign-in to their account (a breach)
Notice = Literal["none", AccountNotice, "account_breach"]

WHY_NO_TASK = {  # category: (Italian, English)
    "notification": ("Notifica automatica: niente da fare", "An automatic notice: nothing to do"),
    "info": ("Solo informazioni", "Just information"),
    "duplicate": ("Lo stesso di un tuo task aperto", "The same as one of your open tasks"),
    "rule_sender": ("Mai da questo mittente: la tua regola", "Never from this sender: your rule"),
    "rule_kind": ("Mai questo tipo: la tua regola", "Never this kind: your rule"),
    "same_thread": ("La stessa conversazione di un task aperto", "The same thread as an open task"),
}

# the code's own line for a category whose model reason is not in their language
CATEGORY_SAID = {  # category: (Italian, English)
    "action_required": ("Ti chiede di fare qualcosa", "It asks you to do something"),
    "event_or_booking": ("Un evento o una prenotazione", "An event or a booking"),
    "delivery": ("Una consegna", "A delivery"),
    "bill_or_deadline": ("Una scadenza o un pagamento", "A deadline or a payment"),
    "promo_or_spam": ("Una promozione", "A promotion"),
}
TASK_SAID_BREACH = (
    "Qualcuno è entrato, o ha provato a entrare, nel tuo account",
    "Someone signed in, or tried to, to your account",
)
TASK_SAID_ANY = ("Da fare", "To do")

# A move's purpose ("perché") not in their language: Pisa's line for what kind of move it was (ledger.Kind).
# These lines are Pisa's own and returned as they are, never checked again: some ("I tuoi soldi", "A
# document") would fail `speaks` in their own language for want of markers.
PURPOSE_SAID: dict[str, tuple[str, str]] = {
    "calendar": ("Una modifica al tuo calendario", "A change to your calendar"),
    "plan": ("Un cambio al piano del giorno", "A change to the day's plan"),
    "task": ("Una modifica ai tuoi task", "A change to your tasks"),
    "task_done": ("Un task fatto", "A task done"),
    "event_done": ("Un evento fatto", "An event done"),
    "task_dismissed": ("Un task scaduto da giorni", "A task overdue for days"),
    "tasks_bulk": ("Più task insieme", "Several tasks at once"),
    "email": ("Un'email", "An email"),
    "reminder": ("Un promemoria", "A reminder"),
    "vault": ("Un documento", "A document"),
    "bill": ("Un pagamento", "A payment"),
    "money": ("I tuoi soldi", "Your money"),
    "stop": ("Fermare o riprendere Pisa", "Stopping or resuming Pisa"),
    "autonomy": ("Quanto Pisa fa da solo", "How much Pisa does on its own"),
    "card": ("La scheda della settimana", "The week's card"),
    "skill": ("Una cosa nuova che Pisa sa fare", "Something new Pisa can do"),
    "other": ("Una mossa di Pisa", "A move of Pisa's"),
}


# An intention's `why` (the model's `keep_intention(why=…)`) not in their language, by its kind.
INTENTION_WHY: dict[str, tuple[str, str]] = {
    "mine": ("Una cosa che vuoi portare avanti", "Something you want to move forward"),
    "waiting_for": ("Qualcuno ti deve una risposta", "Someone owes you an answer"),
}
# A place card's `why` (the research model's) not in their language.
PLACE_WHY = ("Corrisponde a quello che hai chiesto", "It fits what you asked for")


def _pick(pair: tuple[str, str], lang: str) -> str:
    return pair[0] if lang == "it-IT" else pair[1]


def kind_word(kind: str, lang: str) -> str:
    it, en = KIND_WORDS.get(kind, (kind, kind))
    return it if lang == "it-IT" else en


def why_no_task(category: str | None, notice: str | None, lang: str) -> str:
    text = _pick(WHY_NO_TASK.get(category or "", WHY_NO_TASK["notification"]), lang)
    if notice and category in ("notification", "rule_kind"):
        text += f" ({kind_word(notice, lang)})"
    return text


def said_reason(
    reason: str | None, category: str | None, notice: str | None, lang: str, *, task: bool = False
) -> str | None:
    """A model's reason as the phone shows it: as written when it reads as their language, else the code's own
    line for the category (as `why_no_task`). A task's reason never takes a "nothing to do" line: an account
    breach makes a task whatever the category (`needs_task`), and says so."""
    if not reason or speaks(reason, lang):
        return reason
    if task and notice == "account_breach":
        return _pick(TASK_SAID_BREACH, lang)
    if category in CATEGORY_SAID:
        return _pick(CATEGORY_SAID[category], lang)
    if task:
        return _pick(TASK_SAID_ANY, lang)
    return why_no_task(category, notice, lang)


# The retriage's own English diagnostics (`IntakeService.retriage`), as stored in a task's reason before
# retriage wrote their language: (category, notice) for `why_no_task`.
_NO_TASK = re.compile(r"^no task: ([a-z_]+)(?: \(automated\))?$")


def retriage_kind(reason: str) -> tuple[str, str | None] | None:
    """The (category, notice) a retriage diagnostic stands for, or None when it is not one."""
    if reason == "another task of the same email thread stays":
        return "same_thread", None
    if reason == "same as another open task" or re.fullmatch(r"same as «.+»", reason):
        return "duplicate", None
    if reason == "your rule: never from this sender":
        return "rule_sender", None
    if reason == "your rule: never this kind":
        return "rule_kind", None
    if (found := _NO_TASK.match(reason)) is None:
        return None
    word = found.group(1)
    if word in NOTICE_KINDS:
        return "notification", word
    return (word if word in WHY_NO_TASK else "notification"), None


@functools.cache
def _ours(lang: str) -> dict[str, str]:
    """Pisa's own template lines in either language -> the same line in `lang`: their language by
    construction, whatever `speaks` says ("Solo informazioni" has no marker), and still their after they
    change language."""
    out: dict[str, str] = {}
    for any_lang in ("it-IT", "en-US"):
        for c in WHY_NO_TASK:
            for n in (None, *KIND_WORDS):
                out[why_no_task(c, n, any_lang)] = why_no_task(c, n, lang)
        for pair in (*CATEGORY_SAID.values(), TASK_SAID_BREACH, TASK_SAID_ANY):
            out[_pick(pair, any_lang)] = _pick(pair, lang)
    return out


def task_reason(task: "Task", lang: str) -> str | None:
    """A task's `priority_reason` as they read it, wherever it leaves CORE (the task list, the Low list, the
    plan's todo, Pisa's own read of the Low list). An old retriage diagnostic gets the "why no task" line it
    stands for, before the language check: its English frame may hold an Italian title ("same as «Pagare la
    bolletta»"). Pisa's own lines pass; the rest is checked as on write
    (`said_reason(…, task=True)`) with its email's category and notice."""
    reason = task.priority_reason
    if not reason:
        return reason
    if (kind := retriage_kind(reason)) is not None:
        return why_no_task(*kind, lang)
    if (line := _ours(lang).get(reason)) is not None:
        return line
    if speaks(reason, lang):
        return reason
    return said_reason(reason, task.email_category, task.email_notice, lang, task=True)


def purpose_said(purpose: str | None, kind: str, lang: str) -> str | None:
    """A move's purpose (Changes, the week's ledger, an approval's "perché") as they read it: as written in
    their language, else Pisa's line for the move's kind."""
    if not purpose or speaks(purpose, lang):
        return purpose
    return _pick(PURPOSE_SAID.get(kind, PURPOSE_SAID["other"]), lang)


def intention_why(why: str | None, kind: str, lang: str) -> str | None:
    """An intention's `why` (the packet, the list, the pool task's notes): as written in their language, else
    Pisa's line for its kind."""
    if not why or not foreign(why, lang):
        return why
    return _pick(INTENTION_WHY.get(kind, INTENTION_WHY["mine"]), lang)


def place_why(why: str, lang: str) -> str:
    """A places card's `why` (the research model's line): as written in their language, else Pisa's line."""
    return _pick(PLACE_WHY, lang) if foreign(why, lang) else why


def gist_said(what: str | None, category: str | None, notice: str | None, lang: str) -> str | None:
    """An email's gist `what` (the triage's neutral line): as written unless it reads as another language,
    else the category's line (`said_reason`)."""
    if not what or not foreign(what, lang):
        return what
    return said_reason(what, category, notice, lang)
