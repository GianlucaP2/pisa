"""Earned autonomy: kept and undone per action type, the ladder's levels, Pisa's proposal
and the gate the approvals read. Pure rules.

The ladder: `ask` (today's behaviour; every type starts there) → `tell` (Pisa does it and says it) → `silent`
(Pisa does it; the ledger has it) → `ledger` (Pisa does it; only the Sunday card shows it). A level changes
only by the user: the app after Face ID (the phone's signature) or their spoken yes to Pisa's Sunday proposal
(one yes). **Money, sends to other people and anything after outside text stay at `ask` whatever the level**,
and so does everything while Pisa is stopped.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, get_args

from pisa.domain.lexicon.months import MONTH_NAMES

Level = Literal["ask", "tell", "silent", "ledger"]
Verdict = Literal["ask", "tell", "silent"]
LADDER: tuple[Level, ...] = get_args(Level)
ActionType = Literal[
    "calendar_create",
    "calendar_move",
    "calendar_cancel",
    "task_done",
    "task_close",
    "email_draft",
    "email_send",
]
TYPES: tuple[ActionType, ...] = get_args(ActionType)
# RAIL: a send to other people never climbs; nor would any type that moves money (none exists in Pisa
# yet: a payment type, when there is one, goes here; removed the gate's unused `money` flag).
LOCKED = frozenset({"email_send"})


@dataclass(frozen=True)
class Thresholds:
    """The windows (what each rung takes is the per-type table `RUNGS` below)."""

    window: timedelta = timedelta(days=30)  # the kept and undone the app shows
    proposal_valid: timedelta = timedelta(days=8)  # their yes accepts a proposal this recent


THRESHOLDS = Thresholds()


def next_level(level: str) -> Level | None:
    i = LADDER.index(level) if level in LADDER else 0
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


def gate(
    level: str,
    *,
    type_: str,
    outside: bool,
    others: bool,
    stopped: bool = False,
    early: bool = False,
) -> Verdict:
    """RAIL: what Pisa may do with a change it would otherwise propose: `ask` (one yes, as today), `tell` (do
    it and say it), `silent` (do it; the ledger has it). Outside text, other people, a locked type (sends;
    money), a stop and a start before 07:00 keep it at `ask` whatever the level."""
    if type_ in LOCKED or outside or others or stopped or early:
        return "ask"
    if level == "tell":
        return "tell"
    if level in ("silent", "ledger"):
        return "silent"
    return "ask"


_YES = re.compile(
    r"^\W*(?:s[iì]|ok(?:ay)?|va\s+bene|certo|dai|perfetto|d'accordo|volentieri|yes|yeah|yep|sure|go\s+ahead|"
    r"fine|of\s+course|smetti\s+(?:pure\s+)?di\s+chiedere|non\s+chiedermelo\s+pi[uù]|stop\s+asking)\b",
    re.IGNORECASE,
)
_NO = re.compile(r"^\W*(?:no|nope|non\s+ancora|not\s+yet|aspetta|wait)\b", re.IGNORECASE)
# a no anywhere ("certo che no", "dai, no", "fine, non farlo") and a "later" are no yes; "non
# chiedermelo più" / "senza chiedere" are go-aheads, taken out before the check.
_GO_AHEAD = re.compile(
    r"non\s+(?:me\s+lo\s+)?chied\w*(?:\s+\w+)?\s+pi[uù]|non\s+serve\s+chieder\w*|senza\s+chieder\w*|"
    r"non\s+chiedermelo|don'?t\s+ask(?:\s+me)?(?:\s+again|\s+anymore|\s+any\s+more)?|no\s+need\s+to\s+ask",
    re.IGNORECASE,
)
_NO_ANYWHERE = re.compile(
    r"\b(?:no|non|not|don'?t|never|mai|nope)\b|\b(?:vediamo|più\s+avanti|piu\s+avanti|dopo|ci\s+penso|"
    r"later|next\s+week|settimana\s+prossima|domenica\s+prossima|let\s+me\s+think)\b",
    re.IGNORECASE,
)
# a yes to the proposal itself says so: stop asking, on your own, from now on.
_PERMISSION = re.compile(
    r"senza\s+chied|non\s+(?:me\s+lo\s+)?chied\w*|smetti\s+di\s+chied|non\s+serve\s+chied|da\s+sol[oa]|"
    r"d'ora\s+in\s+poi|from\s+now\s+on|without\s+asking|stop\s+asking|don'?t\s+ask|no\s+need\s+to\s+ask|"
    r"on\s+your\s+own|\bpure\b",
    re.IGNORECASE,
)


def accepts(words: str) -> bool:
    """Their spoken yes: "sì", "ok, vai", "va bene", "non chiedermelo più" (a go-ahead); never a "no"
    anywhere, a "later", a question."""
    text = words.strip()
    if not text or text.endswith("?") or _NO.search(text):
        return False
    if _NO_ANYWHERE.search(_GO_AHEAD.sub(" ", text)):
        return False
    return _YES.search(text) is not None or _GO_AHEAD.search(text) is not None


def binds(words: str) -> bool:
    """Their yes is to Pisa's proposal to stop asking ("sì, falli pure senza chiedermelo"), never a
    yes to something else said in the same breath ("sì, sposta la riunione")."""
    return accepts(words) and _PERMISSION.search(words) is not None


_AGAIN = re.compile(
    r"chied\w*(?:\s+\w+){0,2}\s+(?:di\s+nuovo|prima|sempre|ancora)|torna\s+a\s+chied|ask\s+me\s+"
    r"(?:first|again|before)|go\s+back\s+to\s+asking",
    re.IGNORECASE,
)


_PROPOSED = re.compile(
    r"senza\s+chiederti|senza\s+dirtelo|solo\s+la\s+domenica|without\s+asking|without\s+telling"
    r"|only\s+on\s+sunday",
    re.IGNORECASE,
)


def proposed_in(reply: str) -> bool:
    """Pisa's own reply put a proposal to them (its words, `proposal_text`): a plain "sì" right after answers
    it."""
    return _PROPOSED.search(reply) is not None


def asks_again(words: str) -> bool:
    """Their words take a level back to ask ("chiedimelo di nuovo", "ask me first again")."""
    return _AGAIN.search(words) is not None


_VERBS = {
    "it-IT": {
        "calendar_create": ("creato {n} eventi", "li creo"),
        "calendar_move": ("spostato {n} eventi", "li sposto"),
        "calendar_cancel": ("annullato {n} eventi", "li annullo"),
        "task_done": ("chiuso {n} attività", "le chiudo"),
        "task_close": ("tolto {n} attività ferme", "le tolgo"),
        "email_draft": ("preparato {n} bozze", "le preparo"),
    },
    "en-US": {
        "calendar_create": ("created {n} events", "I'll create them"),
        "calendar_move": ("moved {n} events", "I'll move them"),
        "calendar_cancel": ("cancelled {n} events", "I'll cancel them"),
        "task_done": ("closed {n} tasks", "I'll close them"),
        "task_close": ("cleared {n} stale tasks", "I'll clear them"),
        "email_draft": ("drafted {n} emails", "I'll draft them"),
    },
}
_HOW = {
    "it-IT": {
        "tell": "e te lo dico, senza chiederti prima",
        "silent": "senza dirtelo ogni volta (li trovi nel registro)",
        "ledger": "e te li faccio vedere solo la domenica",
    },
    "en-US": {
        "tell": "and tell you, without asking first",
        "silent": "without telling you each time (they're in the ledger)",
        "ledger": "and show you only on Sunday",
    },
}


def proposal_text(type_: str, level: str, kept: int, days: int, lang: str) -> str:
    """Pisa's question with its evidence: "Ho spostato 23 eventi in 30 giorni e non ne hai annullato nessuno:
    d'ora in poi li sposto e te lo dico, senza chiederti prima?"."""
    key = "it-IT" if lang == "it-IT" else "en-US"
    did, will = _VERBS[key].get(type_, ("", ""))
    how = _HOW[key].get(level, "")
    if key == "it-IT":
        done = did.format(n=kept)
        return f"Ho {done} in {days} giorni e non ne hai annullato nessuno: d'ora in poi {will} {how}?"
    return f"I {did.format(n=kept)} in {days} days and you undid none: from now on {will} {how}?"


def canonical(type_: str, level: str, nonce: str, expires_at: str) -> str:
    """What the phone shows and signs after Face ID to change a level (the form)."""
    return f"pisa-autonomy\ntype={type_}\nlevel={level}\nnonce={nonce}\nexpires_at={expires_at}"


# --- the research's thresholds


@dataclass(frozen=True)
class Rung:
    """To climb to this rung: at least `last` actions at the level below, at most `undone_max` of the last
    `last` undone, and at least `days` at that level. The rule of three: 0 errors in n bounds the error rate

    3/n with 95% confidence."""

    last: int
    undone_max: int
    days: int


# One table: per type, the rung it may climb to and what it takes. A rung missing: never (a send to other
# people never climbs; a cancellation or a draft never goes ledger-only: drafts stay visible).
RUNGS: dict[str, dict[str, Rung]] = {
    "calendar_move": {"tell": Rung(20, 0, 14), "silent": Rung(60, 1, 30), "ledger": Rung(150, 2, 56)},
    "calendar_create": {"tell": Rung(20, 0, 14), "silent": Rung(60, 1, 30), "ledger": Rung(150, 2, 56)},
    "calendar_cancel": {"tell": Rung(20, 0, 14), "silent": Rung(60, 1, 30)},
    "task_done": {"tell": Rung(15, 0, 14), "silent": Rung(50, 1, 14), "ledger": Rung(120, 2, 30)},
    "task_close": {"tell": Rung(15, 0, 14), "silent": Rung(50, 1, 14), "ledger": Rung(120, 2, 30)},
    "email_draft": {"tell": Rung(10, 0, 7), "silent": Rung(30, 0, 14)},
    "email_send": {},
}
EVIDENCE = timedelta(days=90)  # Pisa keeps its records 90 days: the evidence is what they hold
RECENT = timedelta(days=30)
RECENT_MIN = 5  # fewer actions than this in the last 30 days: the evidence is stale, no proposal
STOP_FREEZE = timedelta(days=14)  # "ferma tutto": every type back to ask, no proposal for 14 days
# 95% upper limits of a Poisson count for 0..4 events (0 → the rule of three)
_POISSON95 = (3.0, 4.74, 6.30, 7.75, 9.15)


def bound(actions: int, undone: int) -> float | None:
    """The 95% upper bound of the error rate after `undone` errors in `actions` (None when it can't say)."""
    if actions <= 0 or undone >= len(_POISSON95):
        return None
    return min(1.0, _POISSON95[undone] / actions)


def earned(
    type_: str,
    level: str,
    outcomes: list[bool],
    since: datetime,
    now: datetime,
    *,
    recent: int,
    stopped_at: datetime | None = None,
) -> Level | None:
    """The next rung Pisa may propose for this type, from its actions at this level (`outcomes`: undone or
    not, newest first), or None: never for a locked type, never within 14 days of a stop, never on stale
    evidence."""
    up = next_level(level)
    rung = RUNGS.get(type_, {}).get(up) if up is not None else None
    if rung is None or type_ in LOCKED or recent < RECENT_MIN:
        return None
    if stopped_at is not None and now - stopped_at < STOP_FREEZE:
        return None
    last = outcomes[: rung.last]
    if len(last) < rung.last or sum(last) > rung.undone_max or now - since < timedelta(days=rung.days):
        return None
    return up


# --- the Sunday card's wording: one proposal, its numbers, the last error, three answers

_SCOPE = {
    "it-IT": {
        "calendar_move": "Spostamenti di calendario (solo eventi tuoi, senza altri invitati)",
        "calendar_create": "Nuovi eventi (solo tuoi, senza altri invitati)",
        "calendar_cancel": "Annullare eventi (solo tuoi, senza altri invitati)",
        "task_done": "Chiudere le attività fatte",
        "task_close": "Togliere le attività ferme",
        "email_draft": "Bozze di email (nulla parte senza di te)",
    },
    "en-US": {
        "calendar_move": "Calendar moves (your own events, no other guests)",
        "calendar_create": "New events (your own, no other guests)",
        "calendar_cancel": "Cancelling events (your own, no other guests)",
        "task_done": "Closing tasks you did",
        "task_close": "Clearing stale tasks",
        "email_draft": "Email drafts (nothing leaves without you)",
    },
}
_NOUN = {
    "it-IT": {
        "calendar_move": "spostamenti", "calendar_create": "eventi creati",
        "calendar_cancel": "eventi annullati",
        "task_done": "attività chiuse", "task_close": "attività tolte", "email_draft": "bozze",
    },
    "en-US": {
        "calendar_move": "moves", "calendar_create": "events created", "calendar_cancel": "events cancelled",
        "task_done": "tasks closed", "task_close": "tasks cleared", "email_draft": "drafts",
    },
}  # fmt: skip
_FROM_TODAY = {
    "it-IT": {
        "tell": "Da oggi: li faccio e te lo dico subito dopo, invece di chiederti prima.",
        "silent": "Da oggi: li faccio senza dirtelo ogni volta; li trovi nel registro.",
        "ledger": "Da oggi: li faccio e li vedi solo la domenica, nel registro.",
    },
    "en-US": {
        "tell": "From today: I do them and tell you right after, instead of asking first.",
        "silent": "From today: I do them without telling you each time; they're in the ledger.",
        "ledger": "From today: I do them and you see them only on Sunday, in the ledger.",
    },
}
_RUNG_NAME = {
    "it-IT": {"tell": "faccio e ti dico", "silent": "faccio senza dirtelo", "ledger": "solo nel registro"},
    "en-US": {"tell": "do and tell", "silent": "do silently", "ledger": "ledger only"},
}
_CARD = {
    "it-IT": {
        "title": "Una proposta da Pisa",
        "why": "Perché lo propongo",
        "changes": "Cosa cambia",
        "evidence": "Ultimi {n} {noun}: {kept} tenuti, {undone} annullati (dal {since}).",
        "bound": "In pratica: errore stimato al massimo {pct}%: non è zero, per questo resto al passo "
        "«{rung}».",
        "no_error": "Nessun errore da quando te lo chiedo.",
        "last_error": "Ultimo errore: {on}, {what}.",
        "last_on": "Ultimo errore: {on}.",
        "undo": "Ti scrivo subito cosa ho fatto. Un tocco su Annulla e torna com'era.",
        "others": "Se c'è un altro invitato, ti chiedo sempre prima.",
        "first_undo": "Al primo annullamento torno a chiederti.",
        "stop": "«Pisa, ferma tutto» blocca ogni azione automatica, subito.",
        "answers": ["Sì", "Sì, ma solo per …", "No, continua a chiedere"],
        "quiet": "Non serve rispondere: se non scegli nulla, non cambia niente.",
    },
    "en-US": {
        "title": "A proposal from Pisa",
        "why": "Why I'm proposing it",
        "changes": "What changes",
        "evidence": "Last {n} {noun}: {kept} kept, {undone} undone (since {since}).",
        "bound": "In practice: estimated error at most {pct}%: not zero, so I stay at «{rung}».",
        "no_error": "No error since I started asking.",
        "last_error": "Last error: {on}, {what}.",
        "last_on": "Last error: {on}.",
        "undo": "I tell you right away what I did. One tap on Undo puts it back.",
        "others": "If someone else is invited, I always ask first.",
        "first_undo": "At the first undo I go back to asking.",
        "stop": "“Pisa, stop everything” stops every automatic action, at once.",
        "answers": ["Yes", "Yes, but only for …", "No, keep asking"],
        "quiet": "No need to answer: if you choose nothing, nothing changes.",
    },
}


def day_words(on: date | datetime, lang: str) -> str:
    """ "12 ottobre" / "October 12"."""
    key = "it-IT" if lang == "it-IT" else "en-US"
    month = MONTH_NAMES[key][on.month - 1]
    return f"{on.day} {month}" if key == "it-IT" else f"{month} {on.day}"


def card_blocks(proposal: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """The Sunday card's proposal as blocks: the type and its scope, what changes from today, why
    (the numbers, the bound, the last error), what changes for them, the three answers, and that silence
    changes
    nothing."""
    key = "it-IT" if lang == "it-IT" else "en-US"
    w, type_, level = _CARD[key], proposal["type"], proposal["level"]
    why = [
        w["evidence"].format(
            n=proposal["kept"] + proposal["undone"], noun=_NOUN[key].get(type_, type_), kept=proposal["kept"],
            undone=proposal["undone"], since=day_words(date.fromisoformat(proposal["since_on"]), lang),
        ),
    ]  # fmt: skip
    if proposal.get("bound_pct") is not None:
        why.append(w["bound"].format(pct=proposal["bound_pct"], rung=_RUNG_NAME[key][level]))
    last = proposal.get("last_error")
    if last:
        on = day_words(date.fromisoformat(last["on"]), lang)
        why.append(
            w["last_error"].format(on=on, what=last["what"])
            if last.get("what")
            else w["last_on"].format(on=on)
        )
    else:
        why.append(w["no_error"])
    changes = [w["undo"], w["first_undo"], w["stop"]]
    if type_.startswith("calendar_"):
        changes.insert(1, w["others"])
    return [
        {"type": "section", "title": w["title"]},
        {"type": "text", "text": _SCOPE[key].get(type_, type_)},
        {"type": "text", "text": _FROM_TODAY[key][level]},
        {"type": "section", "title": w["why"]},
        {"type": "list", "items": [i[:200] for i in why]},
        {"type": "section", "title": w["changes"]},
        {"type": "list", "items": [i[:200] for i in changes]},
        {"type": "chips", "items": w["answers"]},
        {"type": "text", "text": w["quiet"]},
    ]


RUBBER_STAMP = timedelta(seconds=3)  # a yes faster than this carries little information
