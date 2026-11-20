"""Never drop an intention. Pure rules, no I/O.

An intention is `{what, cue: time | place | event, next_step}` ("ricordami di…", "devo…", "non farmi
dimenticare…"), or something another person owes them (a waiting-for item). Its cue rides what already fires:
a time is a pool task (the reminders), a place a place note (the phone's region monitoring), an event is read
by attend's sense. One missing piece (a vague *what*, no *next step*) is asked once; then Pisa keeps what it
has. A promise in their own sent mail or in a message they dictate is proposed, never kept on its own; another
person's promise in a mail they received is only a waiting-for item for them. Nothing is deleted, only
archived; "fatto" closes it.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, get_args

from pisa.domain.text import fold_apostrophe

Kind = Literal["mine", "waiting_for"]
State = Literal["proposed", "open", "done", "archived"]
Cue = Literal["time", "place", "event", "none"]
Event = Literal["meeting_end", "arrive_home", "call_end"]
Source = Literal["voice", "sent_mail", "dictation", "received_mail"]
Missing = Literal["what", "next_step"]

KINDS: tuple[Kind, ...] = get_args(Kind)
CUES: tuple[Cue, ...] = ("time", "place", "event", "none")
EVENTS: tuple[Event, ...] = ("meeting_end", "arrive_home", "call_end")
WHAT_MIN, WHAT_MAX = 2, 200
STEP_MAX = 200
WHY_MAX = 300
WHO_MAX = 120
LISTED_MAX = 50


plain = fold_apostrophe


def clean(text: str | None, limit: int) -> str | None:
    """One line, at most `limit` characters; empty is None."""
    if text is None:
        return None
    one = " ".join(str(text).split())[:limit].strip()
    return one or None


# --- their words --------------------------------------------------------------------------------------------

# "ricordami di…", "non farmi dimenticare…", "devo ricordarmi…": they ask Pisa to keep something. A
# bare "devo", "dovrei", "I have to", "I must" is no ask ("devo andare", "cosa devo fare oggi?")
_INTENDS = re.compile(
    r"\b(?:ricordami|ricordamelo|ricordatemi|non\s+farmi\s+dimenticare|non\s+lasciarmi\s+dimenticare|"
    r"non\s+devo\s+dimenticar\w*|devo\s+ricordarmi|mi\s+devo\s+ricordare|remind\s+me|"
    r"(?:puoi|potresti)\s+ricordarmi|mi\s+ricordi\s+di|"
    r"don'?t\s+let\s+me\s+forget|i\s+(?:need|have)\s+to\s+remember)\b"
)
# "aspetto il contratto da Marco", "Marco mi deve mandare…": someone owes them
_WAITS = re.compile(
    r"\b(?:aspetto\s+(?:che|da|il|lo|la|i|gli|le|l'|un|una)|sto\s+aspettando|(?:mi|ci)\s+dev(?:e|ono)\s+"
    r"(?:mandare|dare|inviare|restituire|rispondere|richiamare|far\s+sapere)|deve\s+(?:mandarmi|darmi|"
    r"inviarmi|restituirmi|rispondermi|richiamarmi)|i'?m\s+waiting\s+(?:for|on)|waiting\s+on|owes\s+me|"
    r"(?:is|are)\s+(?:going\s+to|supposed\s+to)\s+send\s+me)\b"
)


_QUESTION_START = re.compile(
    r"^\W*(?:cosa|che|quanto|quanti|quanta|quando|dove|come|chi|perche|quale|quali|what|when|how|"
    r"where|who|why|which|do|does|did|is|are|can|could|should|shall|will|would)\b"
)


def is_question(words: str) -> bool:
    """Their words ask something ("devo prendere l'ombrello domani?", "cosa devo fare oggi?"):
    no intention to keep."""
    text = plain(words).strip()
    # "can you remind me…?", "ricordami di…, ok?" ask Pisa to keep it
    if intends(words):
        return False
    if text.endswith("?"):
        return True
    # "quando arrivo a casa ricordami…" opens with "quando" and asks nothing: an ask of theirs isn't a
    # question
    return bool(_QUESTION_START.match(text)) and not (intends(words) or waits(words))


# their own words closing one, or saying yes to a proposed one, this turn
_CLOSES = {
    "done": re.compile(
        r"\b(?:fatt[oaie]|finit[oaie]|chius[oaie]|completat[oaie]|arrivat[oaie]|ricevut[oaie]|chiudil[oaie]|"
        r"segnal[oaie]\s+come\s+fatt[oaie]|done|finished|arrived|got\s+it|close\s+it)\b"
    ),
    "archive": re.compile(
        r"\b(?:archivi\w*|lascia(?:lo|la|li|le)?\s+(?:perdere|stare)|togli\w*|non\s+(?:serve|importa)\s+piu|"
        r"archive|drop\s+it|never\s+mind|forget\s+it)\b"
    ),
    "confirm": re.compile(
        r"\b(?:si|sì|ok|okay|va\s+bene|tienil[oaie]|tienimel[oaie]|confermo|conferma\w*|certo|yes|yeah|keep\s+it|"
        r"confirm\w*|sure)\b"
    ),
}
_NEGATED = re.compile(r"\b(?:non|no|not|don'?t|never|mai)\b")


# "non mi serve più", "never mind" say "drop it": no negation of the act (L-C)
_DROP_IT = re.compile(r"\bnon\s+(?:mi\s+)?(?:serve|importa)\s+piu\b|\bnever\s+mind\b")


def says(words: str, how: str) -> bool:
    """Their words this turn say this act ("fatto", "archivialo", "sì, tienilo"), never negated, never asked.
    a confirm is refused when negated anywhere ("non confermo", "sì,
    ma non tenerlo", M-A), and a question closes nothing ("il contratto è arrivato?", L-B)."""
    text = plain(words).strip()
    if text.endswith("?"):
        return False
    if _NEGATED.search(_DROP_IT.sub(" ", text) if how == "archive" else text):
        return False
    return bool(_CLOSES[how].search(text))


def intends(words: str) -> bool:
    """Their words say they mean to do something ("ricordami di…", "devo…", "non farmi dimenticare…")."""
    return bool(_INTENDS.search(plain(words)))


def waits(words: str) -> bool:
    """Their words say someone owes them something ("aspetto il contratto da Marco", "Marco mi deve
    mandare…")."""
    return bool(_WAITS.search(plain(words)))


# a *what* that names nothing: "quella cosa", "una roba", "it"
_VAGUE = frozenset(
    "cosa cose coso roba faccenda quella quello questa questo quelle quelli una uno un il lo la le gli i di "
    "del della da per fare farla farlo fatto quel thing things stuff something it that this those do the a "
    "an of to".split()
)


def vague(what: str | None) -> bool:
    """No word in it names anything (a specific *what* helps; a generic one doesn't: HN §4.4)."""
    if what is None:
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", plain(what)) if len(w) > 1]
    return not [w for w in words if w not in _VAGUE]


def missing(
    what: str | None, next_step: str | None, kind: Kind = "mine", *, timed: bool = False
) -> Missing | None:
    """The one piece Pisa asks for: a *what* first, then (their own intentions only) a *next step*. A plain
    timed reminder ("ricordami domani alle 19 di chiamare Anna": `timed`) asks no step."""
    if vague(what):
        return "what"
    if kind == "mine" and not timed and not clean(next_step, STEP_MAX):
        return "next_step"
    return None


QUESTIONS: dict[Missing, dict[str, str]] = {
    "what": {"it-IT": "Che cosa, esattamente?", "en-US": "What exactly?"},
    "next_step": {
        "it-IT": "Qual è il primo passo concreto?",
        "en-US": "What's the first concrete step?",
    },
}


def question(piece: Missing, lang: str) -> str:
    return QUESTIONS[piece]["it-IT" if lang.startswith("it") else "en-US"]


# Recurring defaults, only when they say so ("ogni settimana", "ogni 2 mesi", "every month")
_NUMBERS = {"due": 2, "tre": 3, "quattro": 4, "sei": 6, "two": 2, "three": 3, "four": 4, "six": 6}
_REPEAT = re.compile(
    r"\b(?:ogni|every)\s+(?:(?P<n>\d{1,2}|due|tre|quattro|sei|two|three|four|six)\s+)?"
    r"(?P<unit>giorn[oi]|settiman[ae]|mes[ei]|day|days|week|weeks|month|months)\b|"
    r"\b(?P<all>tutti\s+i\s+giorni|tutte\s+le\s+settimane|tutti\s+i\s+mesi|daily|weekly|monthly)\b|"
    r"\bogni\s+(?P<wd>lunedi|martedi|mercoledi|giovedi|venerdi|sabato|domenica)\b|"
    r"\bevery\s+(?P<wd_en>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_UNIT = {
    "gio": "day",
    "day": "day",
    "dai": "day",
    "set": "week",
    "wee": "week",
    "mes": "month",
    "mon": "month",
}


def repeat_said(words: str) -> dict[str, Any] | None:
    """The repeat their words say ({"every": day | week | month, "interval": n}), or None: Pisa never makes an
    intention recurring on its own."""
    m = _REPEAT.search(plain(words))
    if m is None:
        return None
    if m.group("wd") or m.group("wd_en"):
        return {"every": "week", "interval": 1}
    if m.group("all"):
        every = m.group("all")
        unit = "day" if "giorni" in every or every == "daily" else "week" if "settiman" in every or every == (
            "weekly") else "month"  # fmt: skip
        return {"every": unit, "interval": 1}
    raw = m.group("n")
    n = 1 if raw is None else int(raw) if raw.isdigit() else _NUMBERS[raw]
    return {"every": _UNIT[m.group("unit")[:3]], "interval": min(max(n, 1), 12)}


# --- event cues --------------------------------------------------------------------------------------------

_STOP = frozenset(
    "il lo la i gli le un una di del della dei delle al alla con per da in su the a an of with to my mio mia "
    "riunione meeting call chiamata telefonata".split()
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", plain(text)) if len(w) > 2 and w not in _STOP}


def _close(a: str, b: str) -> bool:
    """The same word, or the same stem (Italian forms: "apro" / "aprire", "dentista" / "dentistico")."""
    if a == b:
        return True
    n = 5 if min(len(a), len(b)) >= 5 else 3
    return min(len(a), len(b)) >= 4 and a[:n] == b[:n]


def names_it(cue_ref: str | None, title: str) -> bool:
    """A meeting's title or a call's person matches the cue's words (any named word; no words: any)."""
    wanted, seen = _words(cue_ref or ""), _words(title)
    return not wanted or any(_close(w, s) for w in wanted for s in seen)


def said_by_him(text: str, words: str) -> bool:
    """RAIL: most of these words are theirs, this turn (a stem counts: "apro" says "aprire")."""
    mine = {w for w in re.findall(r"[a-z0-9]+", plain(text)) if len(w) > 2 and w not in _STOP}
    said = {w for w in re.findall(r"[a-z0-9]+", plain(words)) if len(w) > 2}
    if not mine:
        return False
    hit = sum(1 for w in mine if any(_close(w, s) for s in said))
    return hit * 3 >= len(mine) * 2


_BARE_HOUR = re.compile(r"\b(alle|ore|at)\s+([01]?\d|2[0-3])\b(?![:.]\d)", re.IGNORECASE)


def with_minutes(when: str) -> str:
    """ "domani alle 10" → "domani alle 10:00" (the task parser reads a clock time with its minutes)."""
    return _BARE_HOUR.sub(lambda m: f"{m.group(1)} {m.group(2)}:00", when)


EVENT_LEAD: dict[Event, dict[str, str]] = {
    "meeting_end": {"it-IT": "Finita la riunione", "en-US": "The meeting's over"},
    "arrive_home": {"it-IT": "Sei a casa", "en-US": "You're home"},
    "call_end": {"it-IT": "Finita la chiamata", "en-US": "The call's over"},
}


def _it(lang: str) -> bool:
    return lang.startswith("it")


def fire_text(event: Event, what: str | None, next_step: str | None, lang: str) -> str:
    """The one line Pisa says when an event cue holds: "Finita la riunione: mandare il verbale. Primo passo:
    aprire la bozza." """
    lead = EVENT_LEAD[event]["it-IT" if _it(lang) else "en-US"]
    body = what or ("quello che volevi fare" if _it(lang) else "what you meant to do")
    step = f" {'Primo passo' if _it(lang) else 'First step'}: {next_step}." if next_step else ""
    return f"{lead}: {body}.{step}"


def still_waiting(who: str | None, what: str | None, lang: str) -> str:
    """The waiting-for's one line at its due date, a statement (never an unprompted question):
    "Il contratto da Marco non è ancora arrivato." """
    if _it(lang):
        thing = what or "Quello che aspettavi"
        return f"{thing[:1].upper()}{thing[1:]}{f' da {who}' if who else ''} non è ancora arrivato."
    thing = what or "What you were waiting for"
    return f"{thing[:1].upper()}{thing[1:]}{f' from {who}' if who else ''} hasn't arrived yet."


def proposed_text(what: str | None, due_on: date | None, lang: str) -> str:
    """A promise Pisa found in their own sent mail or dictation, said once: "Hai promesso: Mandare il
    preventivo a Marco Rossi, entro il 23/10. Se vuoi che lo tenga, dimmi «tienilo»." """
    thing = what or ("una cosa" if _it(lang) else "something")
    if _it(lang):
        by = f", entro il {due_on:%d/%m}" if due_on else ""
        return f"Hai promesso: {thing}{by}. Se vuoi che lo tenga, dimmi «tienilo»."
    by = f", by {due_on:%m/%d}" if due_on else ""
    return f"You promised: {thing}{by}. Say «keep it» and Pisa keeps it."


def speakable(name: str | None) -> str | None:
    """A name Pisa may say, from outside text (a mail's address): letters and spaces only, at most
    40 characters and 4 words ("marco.rossi" → "marco rossi"); never digits, links or a sentence."""
    if not name:
        return None
    words = re.findall(r"[^\W\d_]+", name)[:4]
    out = " ".join(words)[:40].strip()
    return out or None


def outside_thing(what: str) -> str:
    """A thing named in a received mail ("il modulo, chiama subito il 800…"), as
    Pisa may say it: up to the first comma, colon or semicolon, letters, spaces and apostrophes only, at most
    8 words."""
    head = re.split(r"[,:;]", what, maxsplit=1)[0]
    words = re.findall(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)?", head)[:8]
    return " ".join(words)[:60].strip()


def promise_what(what: str, to: str, lang: str) -> str:
    """Their promise as an intention: "Mandare il preventivo a Marco"."""
    what, to = " ".join(what.split())[:120], " ".join(to.split())[:60]
    return f"Mandare {what} a {to}" if _it(lang) else f"Send {what} to {to}"


# --- the resumption packet ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Packet:
    """What they need to restart it in one look."""

    id: str
    kind: Kind
    state: State
    what: str | None
    why: str | None
    last_step: str | None
    next_step: str | None
    who: str | None
    cue: str  # in words: "venerdì 10:00", "da Esselunga", "a fine riunione", "nessuno"
    due_on: date | None
    thread: dict[str, str] | None  # {"id", "subject"} the mail it came from
    task: dict[str, Any] | None  # {"id", "title", "due_at"} the pool task that carries it
    document: dict[str, Any] | None  # {"id", "kind", "expires_on"}
    source: Source
    created_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "what": self.what, "why": self.why,
            "last_step": self.last_step, "next_step": self.next_step, "who": self.who, "cue": self.cue,
            "due_on": self.due_on.isoformat() if self.due_on else None, "thread": self.thread,
            "task": self.task, "document": self.document, "source": self.source,
            "created_at": self.created_at.isoformat(),
        }  # fmt: skip


CUE_WORDS: dict[str, dict[str, str]] = {
    "meeting_end": {"it-IT": "a fine riunione", "en-US": "when the meeting ends"},
    "arrive_home": {"it-IT": "quando arrivi a casa", "en-US": "when you get home"},
    "call_end": {"it-IT": "a fine chiamata", "en-US": "when the call ends"},
    "none": {"it-IT": "nessuno", "en-US": "none"},
}


def cue_words(
    cue: str, event: str | None, ref: str | None, at: str | None, place: str | None, lang: str
) -> str:
    key = "it-IT" if _it(lang) else "en-US"
    if cue == "time" and at:
        return at
    if cue == "place":
        return (
            (f"da {place}" if _it(lang) else f"at {place}")
            if place
            else ("a un posto" if _it(lang) else "a place")
        )
    if cue == "event" and event:
        base = CUE_WORDS[event][key]
        return f"{base} ({ref})" if ref else base
    return CUE_WORDS["none"][key]


def packet_lines(p: Packet, lang: str) -> list[str]:
    """The packet in a few short lines, their language, for a tool's answer or a notice."""
    it = _it(lang)
    lines = [f"{'Cosa' if it else 'What'}: {p.what or '?'}"]
    if p.who:
        lines.append(f"{'Chi' if it else 'Who'}: {p.who}")
    if p.why:
        lines.append(f"{'Perché' if it else 'Why'}: {p.why}")
    if p.last_step:
        lines.append(f"{'Ultimo passo fatto' if it else 'Last step done'}: {p.last_step}")
    lines.append(f"{'Prossimo passo' if it else 'Next step'}: {p.next_step or '?'}")
    lines.append(f"{'Quando' if it else 'Cue'}: {p.cue}")
    if p.due_on:
        lines.append(f"{'Entro' if it else 'Due'}: {p.due_on.isoformat()}")
    if p.thread:
        lines.append(f"{'Email' if it else 'Mail'}: {p.thread['subject']}")
    if p.task:
        lines.append(f"Task: {p.task['title']}")
    if p.document:
        lines.append(
            f"{'Documento' if it else 'Document'}: {p.document['kind']} ({p.document['expires_on']})"
        )
    return lines
