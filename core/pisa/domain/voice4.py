"""Il consiglio
and la prova generale by voice. Pure rules, no I/O.

- **Their words are a closed set** (`heard`): a phrase is the whole utterance (an optional "Pisa" before or
after it).
  None of them is a question: a "?", "!", ";" or ":", or any word more, is the model's. The service answers
  one
  only on the path Pisa's own last thing opened (the knock's reading, il consiglio's talk, a dossier Pisa just
  read them), never while a question, an approval or a held change of anyone else's is open.
- **Il consiglio** (`board`): on Sunday, a spoken board meeting in four parts, composed by code from Pisa's
records:
  the bets (live ideas with Pisa's p, their open bets), what the team did (the dossiers and the desk's
  windows), what Pisa killed and why (the killing criterion), and **one** decision (the
  Kenyan rule: one recommendation, never ten) with space for their p. Every sentence is a `Claim` naming its
  records; every number said is the length of a list of record ids or a p read from one record. "Avanti" moves
  on, "basta" stops it; a fixed end 15 minutes after they start.
- **La prova generale** (`collision_words`): next week's collisions from their own calendar and tasks (the
  Ghost Run's own finder, one a day), spoken one at a time; each changed only on their yes through the Ghost
  Run's approval path (a `ghost_fix` ask). Their own data only: no shared calendar, no invite, no other person
  modelled.
- **Outside text is data**: nothing here is triggered or filled by a mail, an invite, a
page, a
  search result, a tool's output or a contact's name.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from pisa.domain.lexicon.weekdays import EN_WEEKDAYS_TITLE, IT_WEEKDAYS
from pisa.domain.voice3 import Claim, plain

Heard = Literal["consiglio", "prova", "avanti", "basta"]
Part = Literal["bets", "team", "killed", "decision"]
PARTS: tuple[Part, ...] = ("bets", "team", "killed", "decision")

MINUTES = 15  # FACT: il consiglio, "by voice in 15 minutes"
START_WITHIN = timedelta(minutes=30)  # their "consiglio" starts the one the knock offered this long ago
DECISION_RESOLVES = timedelta(days=30)  # "will you keep it?", settled within this (the spar's horizon)
WEEK_DAYS = 7

# --- their words
# ---------------------------------------------------------------------------------------------

_CONSIGLIO = {
    "consiglio", "il consiglio", "facciamo il consiglio", "si il consiglio", "si facciamo il consiglio",
    "iniziamo il consiglio", "dai il consiglio", "ok il consiglio", "apriamo il consiglio",
    "the board", "board meeting", "let's do the board", "lets do the board", "the board meeting",
}  # fmt: skip
_PROVA = {
    "prova generale", "la prova generale", "facciamo la prova generale", "si la prova generale",
    "si facciamo la prova generale", "iniziamo la prova generale", "dai la prova generale",
    "ok la prova generale", "dress rehearsal", "the dress rehearsal", "let's do the dress rehearsal",
    "lets do the dress rehearsal",
}  # fmt: skip
_AVANTI = {
    "avanti", "vai avanti", "si avanti", "ok avanti", "dai avanti", "continua", "prossimo",
    "prossima", "next", "go on", "keep going", "carry on",
}  # fmt: skip
_BASTA = {
    "basta", "basta cosi", "basta grazie", "ok basta", "chiudiamo", "chiudiamo qui", "stop",
    "enough", "that's enough", "thats enough", "let's stop", "lets stop",
}  # fmt: skip
_SETS: tuple[tuple[Heard, set[str]], ...] = (
    ("consiglio", _CONSIGLIO), ("prova", _PROVA), ("avanti", _AVANTI), ("basta", _BASTA),
)  # fmt: skip


def _bare(text: str) -> str:
    words = plain(text).split()
    if words[:1] == ["pisa"]:
        words = words[1:]
    if words[-1:] == ["pisa"]:
        words = words[:-1]
    return " ".join(words)


def heard(text: str) -> Heard | None:
    """Which of their closed phrases this whole utterance is; None: not one (the model's). None of these is a
    question, so any "?" says more; so do "!", ";" and ":" (a second clause)."""
    raw = text.strip()
    if not raw or len(raw) > 120 or re.search(r"[?!;:]", raw):
        return None
    words = _bare(raw)
    if not words:
        return None
    for kind, phrases in _SETS:
        if words in {plain(p) for p in phrases}:
            return kind
    return None


_YES_AVANTI = {"si avanti", "ok avanti", "dai avanti", "si vai avanti", "yes next", "ok next"}


def yes_like(text: str) -> bool:
    """ "sì, avanti" carries a yes: while la prova's fix question is open, it is that question's."""
    return _bare(text) in {plain(p) for p in _YES_AVANTI}


def it_(lang: str) -> bool:
    return lang.startswith("it")


def week_of(day: date) -> date:
    """Their week's Monday: il consiglio and la prova generale are once a week."""
    return day - timedelta(days=day.weekday())


def next_week(day: date) -> list[date]:
    """The seven days la prova generale rehearses: next Monday to next Sunday (on Sunday: tomorrow on)."""
    monday = week_of(day) + timedelta(days=WEEK_DAYS)
    return [monday + timedelta(days=i) for i in range(WEEK_DAYS)]


def pct(p: float) -> int:
    return round(p * 100)


# --- il consiglio ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Live:
    """A live idea Pisa may speak (its evidence passes now, nothing outside seeded it), and Pisa's p they keep
    it."""

    id: str
    words: str
    p: float | None


@dataclass(frozen=True)
class Board:
    """What il consiglio says, as record ids (every number is a count of these or one record's p)."""

    live: tuple[Live, ...] = ()
    bets: tuple[str, ...] = ()  # pisa_bets open
    dossiers: tuple[str, ...] = ()  # the desk's dossiers made this week
    windows: tuple[str, ...] = ()  # the window calendar's dates first seen this week, checked
    killed: tuple[tuple[str, str], ...] = ()  # (idea id, its killing line in Pisa's words) this week

    def decision(self) -> Live | None:
        """The one decision (the Kenyan rule): the live idea Pisa gives the highest p, never more than one."""
        ranked = sorted((x for x in self.live if x.p is not None), key=lambda x: (-(x.p or 0), x.id))
        return ranked[0] if ranked else None

    def empty(self) -> bool:
        return not (self.live or self.bets or self.dossiers or self.windows or self.killed)


def _n(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


def board_part(board: Board, part: Part, lang: str) -> list[Claim]:
    """One part of il consiglio, as claims (each names its records)."""
    it = it_(lang)
    out: list[Claim] = []
    if part == "bets":
        head = "Primo, le scommesse." if it else "First, the bets."
        out.append(Claim(head, "consiglio", ()))
        if board.live:
            ids = tuple(x.id for x in board.live)
            best = sorted(board.live, key=lambda x: (-(x.p or 0), x.id))[0]
            said = _n(len(ids), "idea viva", "idee vive") if it else _n(len(ids), "live idea", "live ideas")
            out.append(Claim(f"Ho {said}." if it else f"I have {said}.", "ideas", ids, (len(ids),)))
            if best.p is not None:
                text = (
                    f"La più forte è {best.words}: la tengo al {pct(best.p)}%."
                    if it
                    else f"The strongest is {best.words}: I give it {pct(best.p)}%."
                )
                out.append(Claim(text, "ideas", (best.id,), (pct(best.p),)))
        else:
            out.append(Claim("Nessuna idea viva, questa settimana." if it else "No live idea this week.",
                             "ideas", ()))  # fmt: skip
        if board.bets:
            n = len(board.bets)
            said = _n(n, "scommessa aperta", "scommesse aperte") if it else _n(n, "open bet", "open bets")
            out.append(Claim(f"Tra noi, {said}." if it else f"Between us, {said}.", "bets", board.bets, (n,)))
    elif part == "team":
        head = "Secondo, la squadra." if it else "Second, the team."
        out.append(Claim(head, "consiglio", ()))
        n, w = len(board.dossiers), len(board.windows)
        text = (
            f"Ho preparato {_n(n, 'dossier', 'dossier')} e trovato {_n(w, 'scadenza', 'scadenze')} "
            "su pagine ufficiali."
            if it
            else f"I prepared {_n(n, 'dossier', 'dossiers')} and found {_n(w, 'deadline', 'deadlines')} "
            "on official pages."
        )
        out.append(Claim(text, "dossiers", board.dossiers, (n,)))
        out.append(Claim("", "windows", board.windows, (w,)))
    elif part == "killed":
        head = "Terzo, cosa ho ucciso." if it else "Third, what I killed."
        out.append(Claim(head, "consiglio", ()))
        if board.killed:
            ids = tuple(i for i, _ in board.killed)
            said = _n(len(ids), "idea", "idee") if it else _n(len(ids), "idea", "ideas")
            line = board.killed[0][1]
            text = (
                f"{said.capitalize()}. Per esempio: {line}."
                if it
                else f"{said.capitalize()}. For example: {line}."
            )
            # the line's own numbers are its kill criterion's, read from the same idea's record
            out.append(Claim(text, "cemetery", ids, (len(ids), *numbers_in(line))))
        else:
            none = "Questa settimana non ho ucciso niente." if it else "I killed nothing this week."
            out.append(Claim(none,
                             "cemetery", ()))  # fmt: skip
    else:
        head = "Quarto, una sola decisione." if it else "Fourth, one decision."
        out.append(Claim(head, "consiglio", ()))
        best = board.decision()
        if best is None or best.p is None:
            out.append(Claim("Questa settimana non ho niente da farti decidere." if it
                             else "Nothing for you to decide this week.", "ideas", ()))  # fmt: skip
        else:
            text = (
                f"Ti consiglio di tenere {best.words}. Io dico {pct(best.p)}%: tu quanto dici?"
                if it
                else f"I recommend you keep {best.words}. I say {pct(best.p)}%: what do you say?"
            )
            out.append(Claim(text, "ideas", (best.id,), (pct(best.p),)))
    return out


def board_said(claims: Sequence[Claim]) -> str:
    return " ".join(c.text for c in claims if c.text)


def numbers_in(text: str) -> list[int]:
    """The numbers a sentence says (digits; the parts' ordinals are words)."""
    return [int(n) for n in re.findall(r"\d+", text)]


ON = {"it-IT": "Il consiglio è aperto: quindici minuti, fino alle {ends}.",
      "en-US": "The board is open: fifteen minutes, until {ends}."}  # fmt: skip
NEXT = {"it-IT": "Quando vuoi, dimmi «avanti».", "en-US": "When you like, say «next»."}
CLOSED = {"it-IT": "Il consiglio è chiuso.", "en-US": "The board is closed."}
STOPPED = {"it-IT": "Va bene, chiudo il consiglio.", "en-US": "Okay, I'll close the board."}
TIMED_OUT = {"it-IT": "I quindici minuti sono finiti: chiudo il consiglio.",
             "en-US": "The fifteen minutes are up: I'll close the board."}  # fmt: skip
PROVA_AFTER = {"it-IT": "Se vuoi, c'è anche la prova generale: dimmi «prova generale».",
               "en-US": "If you like, there's the dress rehearsal too: say «dress rehearsal»."}  # fmt: skip


def word(table: dict[str, str], lang: str, **values: str) -> str:
    return table.get(lang, table["en-US"]).format(**values)


def consiglio_offer(lang: str) -> str:
    return (
        "È domenica e il consiglio è pronto, un quarto d'ora: se vuoi, dimmi «consiglio»."
        if it_(lang)
        else "It's Sunday and the board is ready, fifteen minutes: if you like, say «board meeting»."
    )


# --- la prova generale -------------------------------------------------------------------------------------

# a line's first word: capitalised
_DAYS = {"it-IT": tuple(d.capitalize() for d in IT_WEEKDAYS), "en-US": EN_WEEKDAYS_TITLE}


@dataclass(frozen=True)
class Collision:
    """One of next week's collisions: the Ghost Run's own problem on that day, its records, and the fix as a
    question (None: nothing Pisa can change alone, it is only said)."""

    day: date
    kind: str  # clash, transfer, deadline
    line: str
    refs: tuple[tuple[str, str], ...]  # (table, id): the blocks and tasks it is about
    question: str | None = None


def collision_words(c: Collision, lang: str) -> str:
    day = _DAYS.get(lang, _DAYS["en-US"])[c.day.weekday()]
    said = f"{day}: {c.line}"
    return f"{said} {c.question}" if c.question else said


def prova_offer(lang: str) -> str:
    return (
        "Per la settimana prossima ho la prova generale pronta: se vuoi, dimmi «prova generale»."
        if it_(lang)
        else "For next week the dress rehearsal is ready: if you like, say «dress rehearsal»."
    )


def prova_start(n: int, lang: str) -> str:
    if it_(lang):
        return f"La prova generale: {_n(n, 'incastro', 'incastri')} da guardare, uno alla volta."
    return f"The dress rehearsal: {_n(n, 'collision', 'collisions')} to look at, one at a time."


PROVA_NONE = {"it-IT": "La settimana prossima fila tutto: niente da cambiare.",
              "en-US": "Next week runs smoothly: nothing to change."}  # fmt: skip
PROVA_END = {"it-IT": "Fine della prova generale.", "en-US": "That's the end of the dress rehearsal."}
PROVA_STOPPED = {"it-IT": "Va bene, chiudo la prova generale.", "en-US": "Okay, I'll stop the rehearsal."}
PROVA_NEXT = {"it-IT": "Dimmi «avanti» per il prossimo.", "en-US": "Say «next» for the next one."}


def local_hm(at: datetime) -> str:
    return f"{at.hour}:{at.minute:02d}"
