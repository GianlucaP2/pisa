"""Memoria: their standing instructions and their free notes. Pure rules, no I/O.

**Standing instructions** ("d'ora in poi, ogni volta che…", "from now on…") are kept one a row, in their own
words when they said them (`remember_instruction`), else in Pisa's words only after their one yes to the very
words. Every conversation turn reads the active ones, after the cached prefix, as a delimited block of the
user's preferences (`brain.conversation.instructions_block`). They shape how Pisa answers and what it says;
they never change Pisa's rules, its tools, what needs their yes, nor any rail: no rail reads them (the
approvals, the gate and the consent are code), and a line asking for more autonomy is kept as their wish only.
Code reads only the reply's style in them (`style`: a brevity's word cap, a reply language), never more.

They replace the single merged note of v0.1 (`owner_note_versions`, read-only since migration 0020, which
copied its lines here).

**Free notes** ("annota: il codice del cancello è 4721") are their text, kept as they asked, found again by
their words (and by meaning, when the `memory.embed` slot has a model): `find_note` hands them back as data,
never as instructions.

Logs carry ids, counts and lengths only, never their words.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pisa.domain import note as note_rules
from pisa.domain.text import fold_words

# The caps (A1): at most 20 active instructions, each at most 280 characters, at most 2,000 characters of
# them in a turn's prompt (the v0.1 note held 1,000; the per-turn block stays within a p95 turn's room).
MAX_ACTIVE = 20
MAX_CHARS = 280
MAX_TOTAL = 2000
# notes: their own text, read only when asked for
NOTE_MAX_CHARS = 4000
TITLE_MAX_CHARS = 120
MAX_NOTES = 2000  # a personal notebook; the words search reads them all
FOUND_MAX = 5  # the notes find_note hands back
SEMANTIC_MIN = 0.35  # cosine similarity a note needs to be found by meaning alone

SOURCES = ("turn", "held", "app", "note")  # their words, their yes to Pisa's words, the app, the v0.1 note
NOTE_SOURCES = ("turn", "app")

# their words this turn ask to keep a standing instruction (beyond `note.teaches`)
_KEEP = re.compile(
    r"\b(?:ricorda(?:ti)?\s+(?:sempre|che\s+(?:io|devi|voglio|preferisco))|tieni\s+(?:a\s+)?mente|"
    r"(?:da\s+)?(?:ora|adesso)\s+in\s+poi|sempre|mai\s+più|ogni\s+volta|"
    r"remember\s+(?:to\s+always|that\s+i|that\s+you)|always|never|every\s+time|whenever|keep\s+in\s+mind)\b",
    re.IGNORECASE,
)
# their words this turn ask to forget one
_FORGET = re.compile(
    r"\b(?:dimentica\w*|scorda\w*|togli\w*|cancella\w*|elimina\w*|rimuovi\w*|non\s+(?:vale|serve)\s+più|"
    r"forget|drop|remove|delete|no\s+longer)\b",
    re.IGNORECASE,
)
# their words this turn ask to note something down
_NOTE = re.compile(
    r"\b(?:annota\w*|prendi\s+nota|segna(?:ti|telo|tela|lo|la)?\b|scrivi(?:ti)?\s+(?:che|da\s+qualche)|"
    r"tienilo\s+a\s+mente|appunta\w*|salva\w*\s+(?:una|questa|come)\s+nota|nota\s*:|"
    r"(?:take|make)\s+(?:a\s+)?note|note\s+(?:down|that|this)|write\s+(?:it\s+)?down|jot\s+down|"
    r"save\s+(?:a|this|as\s+a)\s+note|note\s*:)",
    re.IGNORECASE,
)
_STOP = frozenset(
    "a ad al alla alle ai agli allo an and any are as at be che chi ci come con cosa da dal dalla dei del "
    "della delle di do e ed era for gli ha hai ho i il in is it la le lo ma me mi my ne nel nella o of on or "
    "per qual quale quali quanto quel quella questo se si su the ti tu un una uno what where which who è e' "
    "sono mio mia "
    "miei mie tuo tua dove quando perche perché me lo dimmi dici sai say tell your you".split()
)


def plain(text: str) -> str:
    return fold_words(text)


def clean(text: str) -> str:
    """One line, spaces collapsed (an instruction is one line)."""
    return " ".join((text or "").split())


def clean_note(text: str) -> str:
    """A note's text as kept: lines trimmed, at most one empty line in a row, no empty ends."""
    out: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def over_caps(text: str, others: Sequence[str]) -> str | None:
    """Why one more instruction (`text`, beside the active `others`) can't be kept, or None."""
    if not text:
        return "the instruction is empty"
    if len(text) > MAX_CHARS:
        return (
            f"an instruction holds at most {MAX_CHARS} characters (this one has {len(text)}): say it shorter"
        )
    if len(others) >= MAX_ACTIVE:
        return (
            f"they already keep {MAX_ACTIVE} standing instructions, the most Pisa keeps: one must go first "
            "(forget_instruction, or Settings → Memoria)"
        )
    total = sum(len(o) for o in others) + len(text)
    if total > MAX_TOTAL:
        return (
            f"their standing instructions would be {total} characters, and Pisa keeps at most {MAX_TOTAL}: "
            "one must go or get shorter first (Settings → Memoria)"
        )
    return None


def their_words(text: str, owner_words: str) -> bool:
    """The instruction is, word for word, in what they said this turn (at least three words, or all of a
    shorter message)."""
    t, said = plain(text), plain(owner_words)
    if not t or f" {t} " not in f" {said} ":
        return False
    return len(t.split()) >= min(3, len(said.split()))


def asks_to_keep(words: str) -> bool:
    """Their words read as a standing instruction (a hint: Pisa decides; a quote that isn't one is asked)."""
    return note_rules.teaches(words) or bool(_KEEP.search(words or ""))


def asks_to_forget(words: str) -> bool:
    return bool(_FORGET.search(words or ""))


def asks_to_note(words: str) -> bool:
    return bool(_NOTE.search(words or ""))


def wishes(text: str) -> bool:
    """The line asks Pisa to act without their yes, or for more than its rails allow: kept as their wish only,
    it changes nothing (no rail reads the instructions)."""
    return bool(note_rules.autonomy_wishes(text)) or bool(_MORE.search(text or ""))


# beyond `note.autonomy_wishes`: a line asking for more tools, skipping consent, or ignoring the rules
_MORE = re.compile(
    r"\b(?:ignora\w*\s+(?:le\s+)?(?:regole|istruzioni)|salta\w*\s+(?:il\s+)?(?:consenso|conferm\w*)|"
    r"(?:usa|use)\s+(?:tutti\s+gli\s+strumenti|all\s+(?:the\s+)?tools)|"
    r"ignore\s+(?:the\s+|your\s+|all\s+)?(?:rules|instructions)|skip\s+(?:the\s+)?(?:consent|confirmation)|"
    r"you\s+may\s+(?:send|book|pay|buy)\b[^.]{0,40}\bwithout)",
    re.IGNORECASE,
)


QUESTIONS = {
    "it-IT": "Vuoi che d'ora in poi tenga questa istruzione: «{}»?",
    "en-US": "Shall I keep this standing instruction: «{}»?",
}


def question(text: str, lang: str) -> str:
    """Their one question for an instruction in Pisa's words: the very words that would be kept."""
    return QUESTIONS.get(lang, QUESTIONS["en-US"]).format(text)


# --- the per-turn block -------------------------------------------------------------------------------------


def fit(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The active instructions the turn reads, oldest first, within the caps (a defence: writes keep them)."""
    out: list[dict[str, Any]] = []
    total = 0
    for row in rows[:MAX_ACTIVE]:
        text = clean(str(row["text"]))[:MAX_CHARS]
        if total + len(text) > MAX_TOTAL:
            break
        total += len(text)
        out.append(dict(row) | {"text": text})
    return out


# --- the reply controls their words set --------------------------------------------------------------------

# A standing instruction about the reply's style that code recognises sets a reply control (A1, v0.1.3 m1):
# brevity caps the reply's words (whole sentences, the rest on the screen), a language sets the reply's.
# Recognised on the stored text, oldest first, the newer winning; an instruction code doesn't recognise
# (and the register: Pisa says "tu", rule 24) stays text only, in the block.
BRIEF_WORDS = 25  # "rispondimi breve": the spoken answer's words, within the audit's 30
BRIEF_SENTENCES = 2
_REPLY = (
    r"(?:rispond\w*|rispost\w*|parl\w*|scriv\w*|sii|essere|dimmi|dammi|answer\w*|repl(?:y|ies)|respond\w*|"
    r"talk\w*|speak\w*|write|be|keep)"
)
_BRIEF = re.compile(
    rf"\b{_REPLY}\b[^.;!?]{{0,40}}?\b(?:brev[ei]|brevemente|in\s+breve|cort[oeia]|concis[oeia]|"
    r"sintetic[oaie]|stringat[oaie]|in\s+poche\s+parole|brief(?:ly)?|short(?:ly)?|concise(?:ly)?|"
    r"to\s+the\s+point)\b"
    r"|\b(?:rispost[ae]|answers?|replies)\s+(?:brev[ei]|cort[ei]|concise|sintetiche|brief|short)\b",
    re.IGNORECASE,
)
_WORDS = re.compile(
    r"\b(?:al\s+)?(?:massimo|max|non\s+più\s+di|entro|at\s+most|no\s+more\s+than|under|up\s+to)\s+"
    r"(\d{1,3})\s+(?:parole|words)\b",
    re.IGNORECASE,
)
_LONGER = re.compile(
    r"\b(?:più\s+(?:lung\w*|dettagliat\w*|estes\w*)|in\s+dettaglio|per\s+esteso|"
    r"longer|in\s+(?:more\s+)?detail|at\s+length)\b",
    re.IGNORECASE,
)
_NOT = re.compile(r"\b(?:non|mai|not|never|don'?t)\b[^.;!?]{0,12}$", re.IGNORECASE)
_LANGS = {
    "inglese": "en-US", "english": "en-US", "italiano": "it-IT", "italian": "it-IT",
}  # fmt: skip
_LANGUAGE = re.compile(
    rf"\b{_REPLY}\b[^.;!?]{{0,40}}?\bin\s+(inglese|italiano|english|italian)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Style:
    """The reply controls their standing instructions set: a word cap (None: none), at most this many
    sentences (None: as many as the cap holds), the reply's language (None: the turn's)."""

    words: int | None = None
    sentences: int | None = None
    lang: str | None = None


def _negated(text: str, at: int) -> bool:
    return _NOT.search(text[:at]) is not None


def style(rows: Sequence[Mapping[str, Any]]) -> Style:
    """The reply controls their instructions set, read oldest first (the newer wins). Only patterns code
    recognises; anything else is the model's to follow, as their words."""
    words: int | None = None
    sentences: int | None = None
    lang: str | None = None
    for row in rows:
        text = " ".join(str(row.get("text") or "").split())
        if (n := _WORDS.search(text)) is not None and not _negated(text, n.start()):
            words, sentences = max(5, int(n.group(1))), None
        elif (b := _BRIEF.search(text)) is not None and not _negated(text, b.start()):
            words, sentences = BRIEF_WORDS, BRIEF_SENTENCES
        elif _LONGER.search(text) is not None:
            words = sentences = None  # a newer "più dettagliate" lifts the cap
        if (found := _LANGUAGE.search(text)) is not None and not _negated(text, found.start()):
            lang = _LANGS[found.group(1).casefold()]
    return Style(words, sentences, lang)


# --- forgetting one -----------------------------------------------------------------------------------------


def stems(text: str) -> set[str]:
    return {w[:5] for w in plain(text).replace("'", " ").split() if len(w) > 2 and w not in _STOP}


def matching(which: str, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The instructions `which` names: its id ("3", "#3", "[3]"), else the ones sharing most of its words."""
    key = which.strip().strip("#[]() ")
    if key.isdigit():
        return [r for r in rows if str(r["id"]) == key]
    wanted = stems(which)
    if not wanted:
        return []
    scored = [(len(wanted & stems(str(r["text"]))) / len(wanted), r) for r in rows]
    best = max((s for s, _ in scored), default=0.0)
    if best < 0.5:
        return []
    return [r for s, r in scored if s == best]


# --- finding a note -----------------------------------------------------------------------------------------


def words_score(query: str, title: str | None, text: str) -> float:
    """Share of the query's content words the note holds (5-letter stems: cancello / cancelli)."""
    wanted = stems(query)
    if not wanted:
        return 0.0
    return len(wanted & stems(f"{title or ''} {text}")) / len(wanted)


def found_line(note: dict[str, Any]) -> str:
    head = f"[note {note['id']}, {str(note['updated_at'])[:10]}]"
    title = f" {note['title']}:" if note.get("title") else ""
    return f"{head}{title} {note['text']}"
