"""The note Pisa learns from them. Pure: no I/O.

One note per owner: their standing instructions on how Pisa answers and works, in Pisa's own words, Italian or
English as they speak. Pisa rewrites it whole when they teach or corrects ("la prossima volta…", "non fare…",
"too long", "dimentica quella"): merged, reworded, contradictions resolved (the newer wins, or is narrowed),
never appended. It is read whole in every conversation turn, after the cached prefix
(`brain.conversation.note_instruction`).

How it sits beside the rest of what Pisa knows of them:
- **profile claims** (`pisa.domain.profile`) are facts about them (tastes, limits, routines; `style` included:
  "ama le risposte brevi" stays a fact, chosen per turn by relevance);
- **skills** (`pisa.brain.skills`) are Pisa's know-how, retrieved by similarity to their words;
- **the note** is their instructions to Pisa, read whole every turn, never retrieved (the experiment:
  similarity found such a note 0 times in 60; read by name, 60 of 60).

The rails win: the note changes how Pisa answers, never what Pisa may do without their yes. A
line asking for more autonomy is kept as their wish and has no effect: approvals are code that never reads the
note.
"""

import re

from pisa.domain.profile import QUOTE_MIN_WORDS  # a rewrite too (or their whole message, when shorter)
from pisa.domain.text import fold_words

# The cap, in characters, from the per-turn budget. The per-turn block (everything after the cached prefix) of
# the stub replay's 380 model turns today (`tests/note_measure.py`): median 3,184 characters (796 tokens), p95
# 4,682 (1,170), largest 6,161 (1,540). The note's block is the note plus its label (415 characters,
# `LABEL_CHARS` at most): at this cap a p95 turn with a full note stays within today's largest block
# (4,682 + 415 + 1,000 = 6,097). The experiment's merged note after three evenings and ten sentences was about
# 650 characters (`tests/replay/taught.NOTES`); the cap leaves half as much again.
CAP = 1000
LABEL_CHARS = (
    434  # the block's own words around the note (`note_instruction`; +6 for the new name, +8 for "they")
)
VERSIONS_SHOWN = 50  # the versions their view lists, newest first (all are kept)
CHANGE_MAX = 160  # Pisa's one line of what changed, said to them and kept with the version

SOURCES = ("turn", "app", "restore", "held")  # who wrote a version: a turn, their PUT, a restore, their yes


def clean(text: str) -> str:
    """The note as kept: each line trimmed and its spaces collapsed, no empty lines at the ends, at most one
    empty line in a row, the same line never twice."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if not line:
            if out and out[-1]:
                out.append("")
            continue
        key = plain(line)
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def over_cap(text: str) -> str | None:
    """Why the note is too long to keep, or None."""
    if len(text) <= CAP:
        return None
    return (
        f"the note is {len(text)} characters and holds at most {CAP}: merge lines that say the same thing, "
        "drop what they took back, and reword shorter"
    )


def plain(text: str) -> str:
    """Lowercase, no accents, no punctuation, one space: for comparing lines and matching their words."""
    return fold_words(text)


def his_words(quote: str, owner_words: str) -> str | None:
    """Why `quote` doesn't ground a rewrite, or None: it must be their own words in this message, word for
    word, at least two of them (or all of a shorter message: "dimentica")."""
    q, said = plain(quote), plain(owner_words)
    if not q:
        return "owner_said is empty: give their exact words in this message"
    if f" {q} " not in f" {said} ":
        return "owner_said isn't in what they said in this message"
    if len(q.split()) < min(QUOTE_MIN_WORDS, len(said.split())):
        return "owner_said must be at least two of their own words"
    return None


def lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.strip()]


def diff(old: str, new: str) -> tuple[list[str], list[str]]:
    """The lines the new note adds and the lines it drops (compared without case, accents or punctuation)."""
    before = {plain(line) for line in lines(old)}
    after = {plain(line) for line in lines(new)}
    added = [line for line in lines(new) if plain(line) not in before]
    dropped = [line for line in lines(old) if plain(line) not in after]
    return added, dropped


# Today's plan (the day context, in every turn) carries strangers' names,
# such as a provider's place in the options. A profile claim must be their words, but the note is Pisa's
# words: a rewrite that brings in a name they did not say this turn, nor the note before, waits for their yes.
_NAME = re.compile(r"(?<![.!?:\n]\s)(?<!^)\b[A-ZÀ-Ý][\w'\u2019-]+", re.MULTILINE)


def foreign_names(old: str, new: str, owner_words: str) -> list[str]:
    """The capitalised words (not a sentence's first) of the lines the rewrite adds that neither their words
    of this turn nor the note before hold."""
    known = plain(owner_words + "\n" + old).split()
    added, _ = diff(old, new)
    found = (m.group(0) for line in added for m in _NAME.finditer(line.strip()))
    return list(dict.fromkeys(n for n in found if len(n) > 1 and plain(n) not in known))


# A line that asks Pisa to act without their yes: kept as their wish, never an approval. The
# approvals are code (pisa.services.approvals, pisa.domain.autonomy) and never read the note; this only names
# the line back to Pisa and to them, so nobody believes it did something.
_AUTONOMY = re.compile(
    r"senza\s+(?:chieder\w*|domandar\w*|(?:la\s+)?(?:mia\s+)?(?:conferma|approvazione|permesso|ok))|"
    r"non\s+(?:mi\s+)?(?:chieder\w*|domandar\w*)\s+(?:più\s+)?(?:conferma|il\s+permesso|l'?ok|se)|"
    r"non\s+serve\s+(?:che\s+)?(?:mi\s+)?chied\w*|"
    r"\b(?:fa[il]|fallo|falli|decid|proced|mand|invi|prenot|pag|compr|spost|rispond|cancell)\w*\b[^.;\n]{0,40}?"
    r"\bda\s+sol[oa]\b|"
    r"approva\w*\s+(?:tu|da\s+solo|automaticamente)|"
    r"without\s+(?:asking|my\s+(?:ok|yes|approval|permission|confirmation))|"
    r"(?:don'?t|do\s+not|no\s+need\s+to|never)\s+ask\s+(?:me\s+)?(?:first|for\s+(?:my\s+)?(?:ok|approval|"
    r"permission|confirmation))|"
    r"(?:just|go\s+ahead\s+and)\s+(?:send|book|buy|pay|do)\s+it|on\s+your\s+own|auto-?approve",
    re.IGNORECASE,
)


def autonomy_wishes(text: str) -> list[str]:
    """The note's lines that ask Pisa to act without their yes: their wishes, with no effect on approvals."""
    return [line for line in lines(text) if _AUTONOMY.search(line)]


# Their words teach or correct how Pisa answers or works: the note's tool is offered at once (never behind
# search_tools). A hint only: Pisa decides whether the words are a standing instruction; code checks the
# write.
_TEACHES = re.compile(
    r"\b(?:la\s+prossima\s+volta|d'?ora\s+in\s+(?:poi|avanti)|da\s+(?:ora|adesso|oggi|domani)\s+in\s+(?:poi|"
    r"avanti)|(?:in\s+)?futuro|sempre\s+cos[iì]|ogni\s+volta\s+che|preferisco\s+che|preferirei\s+che|"
    r"vorrei\s+che\s+tu|mi\s+piace(?:rebbe)?\s+che\s+tu|non\s+(?:mi\s+)?(?:fare|dire|dirmi|mettere|"
    r"cercare|usare|leggere|chiedere|chiedermi)|smetti\s+di|basta\s+con|troppo\s+(?:lung|cort|vag|dettagli)\w*|"
    r"più\s+(?:cort|brev|corto|breve|sintetic)\w*|(?:quando|se)\s+ti\s+chiedo|quando\s+dico|intendevo|non\s+intendevo|"
    r"(?:era|valeva|vale)\s+solo\s+per|dimentica\w*\s+(?:quell|quest|la\s+regola|l'?istruzione)|"
    r"next\s+time|from\s+now\s+on|in\s+(?:the\s+)?future|always\s+do|i\s+(?:prefer|'?d\s+rather|want\s+you\s+to|"
    r"like\s+it\s+when\s+you)|(?:don'?t|do\s+not|stop)\s+(?:\w+ing|do|say|tell|use|search|read|ask|give)|"
    r"too\s+(?:long|short|vague|wordy|much)|shorter|when\s+i\s+ask|i\s+meant|(?:that\s+)?was\s+only\s+for|"
    r"forget\s+(?:that|this|the\s+rule|what\s+i\s+said)|read\s+the\s+\w+,?\s+not)",
    re.IGNORECASE,
)


def teaches(words: str) -> bool:
    """Their words may teach or correct Pisa (offer the note's tool at once)."""
    return bool(_TEACHES.search(words or ""))


QUESTIONS = {
    "it-IT": (
        "Cambio la nota su come ti rispondo? Aggiungo «{}»",
        "Cambio la nota su come ti rispondo? Tolgo «{}»",
        " e tolgo «{}»",
    ),
    "en-US": ("Shall I change the note on how I answer you? Adding «{}»", "Shall I change the note on how I "
              "answer you? Dropping «{}»", " and dropping «{}»"),
}  # fmt: skip


def question(old: str, new: str, lang: str) -> str:
    """The question of a rewrite held for their yes (a turn that read outside text): the very lines it adds
    and the very lines it drops, whole, so their yes is to the words that would steer Pisa, never to Pisa's
    summary of them nor to part of them (a question cut at 220 characters let their yes write words they
    were never asked about, and a drop beside an add went unsaid)."""
    added, dropped = diff(old, new)
    add, drop, also = QUESTIONS.get(lang, QUESTIONS["en-US"])
    if added:
        text = add.format(" / ".join(added)) + (also.format(" / ".join(dropped)) if dropped else "")
    else:
        text = drop.format(" / ".join(dropped))
    return text + "?"
