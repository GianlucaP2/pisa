"""The turn rules, one algorithm for the app and the server: how finished the user's
words sound, and so how long to wait before closing the turn. Pure rules, no I/O.

The table is `protocol/turn-rules.v1.json` (mirrored here; a test keeps the two equal) and the algorithm is
the README's "The turn rules", with its (text, lang) → kind cases in
`protocol/fixtures/turn-rules/cases.json`:

1. The typographic apostrophe (U+2019) is read as ' and the text trimmed. Nothing said yet → **unfinished**.
2. It ends in one of `unfinished_endings` (a comma, a colon, a dash, "…", an elision "l'") → **unfinished**.
3. The words are the lowercase runs of letters, digits and apostrophes. The last word is one a sentence can't
   end on (a conjunction, a preposition, an article, a filler: `unfinished_last`) → **unfinished**.
4. It ends with "?" → **complete**.
5. Openers ("ok", "allora", "senti", "hey"…) are skipped; the first word left opens a question
   (`question_first`, with at least `question_min_words` words) or a command (`command_first`, at least
   `command_min_words`) → **complete**.
6. Otherwise **other**.

The waits: 3 s / 1.2 s / 1.8 s. The server's `turn_hint` gives the same; a model can take the rules'
place later behind `TurnModel` (pisa.services.turn_hint).
"""

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["unfinished", "other", "complete"]

WAITS_MS: dict[Kind, int] = {"unfinished": 3000, "other": 1800, "complete": 1200}
P_END: dict[Kind, float] = {"unfinished": 0.0, "other": 0.5, "complete": 1.0}
UNFINISHED_ENDINGS: tuple[str, ...] = (",", ":", ";", "-", "\u2013", "\u2014", "\u2026", "...", "'")
QUESTION_MIN_WORDS = 3
COMMAND_MIN_WORDS = 2

HINT_MIN_INTERVAL_S = 0.5  # the contract: at most 2 partials a second are answered


@dataclass(frozen=True)
class LangRules:
    unfinished_last: frozenset[str]
    question_first: frozenset[str]
    command_first: frozenset[str]
    openers: frozenset[str]


RULES: dict[str, LangRules] = {
    "it": LangRules(
        unfinished_last=frozenset(
            """
            a agli ai al all' alla alle allo allora anche che cioe cioè col coll' come con da dai dal dall'
            dalla degli dei del dell' della delle dello dev' di dove e ed eh ehm fra gli i il in l l' la le lo
            ma mentre mia mio mmm negli nei nel nell' nella nelle nello o oppure per perché però poi quando
            quell' quindi se su sua sui sul sull' sulla tipo tra tuo uhm un un' una uno
            """.split()
        ),
        question_first=frozenset(
            """
            che chi com'è come cos'è cosa dov'è dove perché posso puoi quale quali quando quant'è quanta
            quante quanti quanto
            """.split()
        ),
        command_first=frozenset(
            """
            accendi aggiungi annulla apri avvia cancella cerca cercami chiama compra controlla dimmi fammi
            ferma imposta invita leggi leggimi manda mandami metti prenota prenotami ricordami rispondi scrivi
            scrivigli segna spegni sposta trova trovami
            """.split()
        ),
        openers=frozenset(
            """
            allora dai ehi ok okay pisa scusa senti
            """.split()
        ),
    ),
    "en": LangRules(
        unfinished_last=frozenset(
            """
            a about an and as at because but by er erm for from her his hmm if in into like my of on or our so
            than that the their then these this those to uh um when which while who with your
            """.split()
        ),
        question_first=frozenset(
            """
            are can could do does how is should what what's when where which who why will would
            """.split()
        ),
        command_first=frozenset(
            """
            add book buy call cancel check email find give invite log move open play put read remind reply
            search send set stop tell text turn write
            """.split()
        ),
        openers=frozenset(
            """
            hey ok okay pisa please so well
            """.split()
        ),
    ),
}
_WORD = re.compile(r"[\w']+")


def normal(text: str) -> str:
    """The typographic apostrophe read as ', trimmed: the same words on the phone and here."""
    return text.replace("\u2019", "'").strip()


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(normal(text))]


def language(lang: str) -> str:
    """The table's language for a BCP-47 tag ("it-IT" → "it"); English for anything else."""
    short = lang.split("-")[0].lower()
    return short if short in RULES else "en"


def kind(text: str, lang: str) -> Kind:
    """How finished the words sound (the README's "The turn rules", steps 1-6)."""
    rules = RULES[language(lang)]
    said = normal(text)
    found = words(said)
    if not found or said.endswith(UNFINISHED_ENDINGS) or found[-1] in rules.unfinished_last:
        return "unfinished"
    if said.endswith("?"):
        return "complete"
    body = found
    while body and body[0] in rules.openers:
        body = body[1:]
    if body and body[0] in rules.question_first and len(body) >= QUESTION_MIN_WORDS:
        return "complete"
    if body and body[0] in rules.command_first and len(body) >= COMMAND_MIN_WORDS:
        return "complete"
    return "other"
