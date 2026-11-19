# ruff: noqa: E501
"""Perceive, then ask; Pisa's own states; the character file and Pisa's opinions. Pure rules, no I/O.

**Perceive, then ask.** A cue is an **observable** on one of three channels, all theirs:
- `voice`: how they speak this turn, against **their own** 30-day baseline, measured **on the phone**; the
  server receives only the closed enums of `Heard` (protocol `utterance.voice`), never audio, never a baseline;
- `words`: their own words this turn (never a mail, an invite, a page, a search result or a tool's output);
- `day`: their own calendar today (their events only: an invite or a Gmail-made event is a stranger's).
Each says a **tempo**, `up` (faster, fuller) or `down` (slower, lighter): a direction of observables, never an
emotion. No emotion is inferred, labelled or stored anywhere (`talk.no_label` holds every question).

`decide` is the guard (no quota): a cue-question needs **Pisa's own reason** (two independent channels
agree, or a fact Pisa is missing for something they asked), never while others are present or inside a floor
message, never after outside text in the turn, never for a kind they left unanswered or waved away (quiet until they
raise it themselves), never twice a kind a day, and at most `CEILING_PER_DAY` a day (a ceiling against bugs, not a
budget).

**Their word, kept.** A state they name is kept only on their "sì, ricordalo", exactly in their words (`his_word`), and
used back in them ("Ti sento di corsa: cotto anche oggi?").

**Pisa's own states**: "e tu, come stai?" is answered from Pisa's records only (`state_answer`): what it got
wrong and fixed first, out loud; what it learned; what it would like to understand; then "ci sono, se serve".
Never needy, never a feeling beyond its records.

**The character file** and **opinions**: Pisa proposes a trait of its own and they approve it; Pisa may give
its view once on a decision of their (`disagree`) or on a habit (`habit`), with its reason, then lets it go. The
rails (`opinion_rails`): never health, never tax, never about other people.
"""

import re
from dataclasses import dataclass
from typing import Literal

from pisa.domain.talk import no_label
from pisa.domain.text import words_keep_apostrophe

Tempo = Literal["up", "down"]
Channel = Literal["voice", "words", "day"]
CHANNELS: tuple[Channel, ...] = ("voice", "words", "day")
Reason = Literal["two_channels", "missing_fact"]

# A ceiling in code against bugs ("no fixed quota"): never more cue-questions than this in their day.
CEILING_PER_DAY = 2
# `day`: their own timed events today at or above this many is a full day (design-2 §2.2's "4 riunioni su 6").
DAY_FULL = 5
# Their named word waits this long for their "sì, ricordalo" (in memory only; never stored before it).
REMEMBER_WITHIN_S = 600
WORD_MAX = 40

# --- the voice contract (protocol `utterance.voice`) ---------------------------------------------
PACE = ("slow", "usual", "fast")
LOUDNESS = ("soft", "usual", "loud")
GAPS = ("few", "usual", "many")
PLACE = ("whisper", "normal", "noisy_place")


@dataclass(frozen=True)
class Heard:
    """How they spoke this turn, each against their own baseline on the phone. None: the phone didn't say."""

    pace: str | None = None
    loudness: str | None = None
    gaps: str | None = None
    place: str | None = None

    @classmethod
    def of(cls, wire: object) -> "Heard | None":
        """From the utterance's `voice` (a dict or a model with those fields); anything else is None."""
        if wire is None:
            return None
        get = wire.get if isinstance(wire, dict) else lambda k: getattr(wire, k, None)  # type: ignore[union-attr]
        values = {k: get(k) for k in ("pace", "loudness", "gaps", "place")}
        allowed = {"pace": PACE, "loudness": LOUDNESS, "gaps": GAPS, "place": PLACE}
        clean = {k: v if isinstance(v, str) and v in allowed[k] else None for k, v in values.items()}
        return cls(**clean) if any(clean.values()) else None


def voice_tempo(heard: Heard | None) -> Tempo | None:
    """The voice channel: faster than their usual is `up`; slower, without clipped gaps, is `down`."""
    if heard is None or heard.place in ("whisper", "noisy_place"):
        return None
    if heard.pace == "fast" or (heard.loudness == "loud" and heard.gaps == "few"):
        return "up"
    if heard.pace == "slow" and heard.gaps != "few":
        return "down"
    return None


def others_maybe(heard: Heard | None) -> bool:
    """A whisper or a noisy place: people may be near them. Pisa asks nothing about them then."""
    return heard is not None and heard.place in ("whisper", "noisy_place")


# --- their words ------------------------------------------------------------------------------------------

_WORDS_UP = re.compile(
    r"\b(di corsa|di fretta|non ho tempo|devo scappare|scappo|al volo|veloce veloce|sbrigati|sono in ritardo|"
    r"in a rush|in a hurry|no time|got to run|gotta run|quickly please|i'm late)\b",
    re.IGNORECASE,
)
_WORDS_DOWN = re.compile(
    r"\b(con calma|finalmente|che bella giornata|alla grande|è andata bene|tutto tranquillo|"
    r"take your time|finally|what a day off|went well|all calm)\b",
    re.IGNORECASE,
)
# Their words naming the state itself: they raised it (a quiet kind may be asked again later)
_RAISED_UP = re.compile(
    r"\b(giornata (pesante|lunga|infinita|no)|sono (cotto|a pezzi|distrutto|stanco|stressato)|"
    r"heavy day|long day|i'm (beat|exhausted|tired|stressed)|rough day)\b",
    re.IGNORECASE,
)
_RAISED_DOWN = re.compile(
    r"\b(giornata (bella|ottima|leggera|tranquilla)|sto (bene|benissimo)|good day|great day|i'm (good|great))\b",
    re.IGNORECASE,
)


def words_tempo(text: str) -> Tempo | None:
    up, down = bool(_WORDS_UP.search(text)), bool(_WORDS_DOWN.search(text))
    return "up" if up and not down else "down" if down and not up else None


def raised(text: str) -> set[Tempo]:
    """The kinds their own words name this turn (they raised it: Pisa may ask about it again, later)."""
    out: set[Tempo] = set()
    if _RAISED_UP.search(text):
        out.add("up")
    if _RAISED_DOWN.search(text):
        out.add("down")
    return out


_FIT = re.compile(
    r"\b(cosa|che) (faccio|mi consigli(?: di fare)?) stasera\b|\b(esco|vado) o (resto|sto) a casa\b|"
    r"\bwhat should i do tonight\b|\bshould i (go out|stay in)\b",
    re.IGNORECASE,
)


def asks_for_fit(text: str) -> bool:
    """Their own request needs a fact Pisa is missing: how their day went ("cosa faccio stasera?")."""
    return bool(_FIT.search(text))


def day_tempo(own_events_today: int) -> Tempo | None:
    """The day channel, from their own timed events today (never a stranger's invite): a full day is `up`. An
    empty calendar says nothing (a free day is not a light one), so good days come from their voice and words."""
    return "up" if own_events_today >= DAY_FULL else None


# --- the guard --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    cues: dict[Channel, Tempo]  # only their channels: voice, words, day
    asked_for: bool = False  # their request this turn needs the fact (asks_for_fit)
    others: bool = False  # others present (a whisper, a noisy place, a meeting with other people)
    floor: bool = False  # this reply is (or carries) a floor message
    outside: bool = False  # the turn read outside text (mail, invite, page, search, a tool's result)
    open_question: bool = False  # the reply already asks (an approval, a yes, Pisa's question)
    quiet: frozenset[str] = frozenset()  # kinds they left unanswered or waved away, not raised since
    asked_today: tuple[str, ...] = ()  # the kinds Pisa asked today (the ceiling counts them)


@dataclass(frozen=True)
class Ask:
    tempo: Tempo
    reason: Reason
    channels: tuple[Channel, ...]


def decide(ctx: Context) -> Ask | None:
    """Pisa's own reason to ask, or None. RAIL: only their three channels count; nothing from outside can open
    or fill a question, so a turn that read outside text asks nothing."""
    if ctx.outside or ctx.others or ctx.floor or ctx.open_question:
        return None
    if len(ctx.asked_today) >= CEILING_PER_DAY:
        return None
    cues: dict[Channel, Tempo] = {
        ch: t for ch, t in ctx.cues.items() if ch in CHANNELS and t in ("up", "down")
    }
    tempos: set[Tempo] = set(cues.values())
    if len(tempos) != 1:
        return None  # no cue, or the channels disagree
    tempo: Tempo = next(iter(tempos))
    if tempo in ctx.quiet or tempo in ctx.asked_today:
        return None
    channels: tuple[Channel, ...] = tuple(ch for ch in CHANNELS if ch in cues)
    if len(channels) >= 2:
        return Ask(tempo, "two_channels", channels)
    if ctx.asked_for:
        return Ask(tempo, "missing_fact", channels)
    return None  # one channel alone never asks


# --- the question ---------------------------------------------------------------------------------------

_OPEN = {
    ("up", "voice"): {"it-IT": "Ti sento di corsa", "en-US": "You sound rushed"},
    ("up", "day"): {"it-IT": "Oggi sei a {n} impegni", "en-US": "You're at {n} things today"},
    ("up", "words"): {"it-IT": "Mi scrivi di corsa", "en-US": "You're writing in a rush"},
    ("down", "voice"): {"it-IT": "Parli più disteso del solito", "en-US": "You're talking slower than usual"},
    ("down", "words"): {"it-IT": "Mi scrivi con calma", "en-US": "You're writing calmly"},
}
_GUESS = {
    "up": {"it-IT": "giornata pesante?", "en-US": "a heavy day?"},
    "down": {"it-IT": "è andata bene?", "en-US": "did it go well?"},
}
_HIS = {"it-IT": "{w} anche oggi?", "en-US": "{w} again today?"}
_WAVE_OFFER = {"it-IT": "O lascio stare.", "en-US": "Or I'll leave it."}


def lang_key(lang: str) -> str:
    return "it-IT" if lang == "it-IT" else "en-US"


def question(ask: Ask, lang: str, *, his: str | None = None, events: int = 0) -> str:
    """The observable, then the question they can wave away; in their word when they asked Pisa to keep one."""
    key = lang_key(lang)
    first = next(ch for ch in ("voice", "day", "words") if ch in ask.channels)
    opening = _OPEN[(ask.tempo, first)][key].format(n=events)
    guess = _HIS[key].format(w=his) if his else _GUESS[ask.tempo][key]
    return f"{opening}: {guess} {_WAVE_OFFER[key]}"


# --- their answer -----------------------------------------------------------------------------------------

Answer = Literal["wave", "named", "yes"]

# their answer is read from a **closed set** only. Anything else (a "?", a
# command, a request, more words, a reminder) is not an answer: the model hears it as any other words.
_WAVES = frozenset({
    "no", "no grazie", "nah", "nope", "niente", "lascia stare", "lascia perdere", "lasciamo stare",
    "lasciamo perdere", "no lascia stare", "no lascia perdere", "non ora", "non adesso", "leave it", "not now",
    "no thanks", "no thank you", "drop it", "no leave it",
})  # fmt: skip
_FINES = frozenset({"tutto ok", "tutto bene", "sto bene", "no sto bene", "i'm fine", "im fine", "all good"})
_YESES = frozenset({
    "sì", "si", "yes", "yeah", "yep", "ok", "abbastanza", "parecchio", "un po'", "un po", "molto", "eh sì",
    "eh si", "direi di sì", "direi di si", "sì abbastanza", "si abbastanza", "sì un po'", "si un po'",
    "a bit", "kind of", "pretty much", "very", "yes a bit", "yeah a bit", "è andata bene", "bene", "sì bene",
    "si bene", "it did", "yes it did",
})  # fmt: skip
_YES_LEAD = ("sì", "si", "yes", "yeah", "abbastanza", "parecchio", "molto", "un po'", "un po", "eh", "direi")
_ME = ("sono", "mi sento", "sto", "i'm", "im", "i am", "i feel", "feeling")
# The states they may name and Pisa may keep in their word: a closed list, never a health word, never a name.
STATES = frozenset({
    "cotto", "cotta", "stanco", "stanca", "stanco morto", "stanca morta", "a pezzi", "distrutto", "distrutta",
    "sfinito", "sfinita", "esausto", "esausta", "pieno", "piena", "incasinato", "incasinata", "di corsa",
    "pesante", "lunga", "una giornata pesante", "giornata pesante", "una giornata lunga", "giornata lunga",
    "tranquillo", "tranquilla", "sereno", "serena", "contento", "contenta", "carico", "carica",
    "rilassato", "rilassata", "alla grande", "benissimo", "beat", "exhausted", "tired", "wiped", "wiped out",
    "swamped", "slammed", "busy", "fried", "relaxed", "calm", "great", "good",
})  # fmt: skip
_REMEMBERS = ("ricordalo", "ricordatelo", "tienilo a mente", "remember it", "remember that")
_TODAY = ("anche oggi", "oggi", "today")
REMEMBER_YES = frozenset({
    "sì ricordalo", "si ricordalo", "ricordalo", "ricordatelo", "sì ricordatelo", "si ricordatelo", "sì", "si",
    "yes", "ok", "sì grazie", "si grazie", "yes remember it", "remember it", "yes please",
})  # fmt: skip


def _norm(text: str) -> str:
    """Casefolded, NFC, punctuation to spaces (the apostrophe kept), spaces collapsed."""
    return words_keep_apostrophe(text)


def _strip(words: str, ends: tuple[str, ...], *, lead: bool) -> tuple[str, bool]:
    for end in ends:
        if lead and (words == end or words.startswith(end + " ")):
            return words[len(end) :].strip(), True
        if not lead and (words == end or words.endswith(" " + end)):
            return words[: len(words) - len(end)].strip(), True
    return words, False


def his_word(text: str) -> str | None:
    """The state they named, from the closed list (`STATES`), as they said it: "sì, cotto" → "cotto"; "sono
    stanco morto" → "stanco morto". None for anything else: a question, a command, a name, a health word."""
    if "?" in text:
        return None
    words = _norm(text)
    words, _ = _strip(words, _REMEMBERS, lead=False)
    words, _ = _strip(words, _TODAY, lead=False)
    for _ in range(2):
        words, _ = _strip(words, _YES_LEAD, lead=True)
    words, _ = _strip(words, _ME, lead=True)
    words, _ = _strip(words, _TODAY, lead=False)
    return words if words in STATES and not _HEALTH.search(words) else None


def private(text: str, people: list[str] | tuple[str, ...] = ()) -> bool:
    """A health word or a name of their people (the other-people rail) is in `text`."""
    folded = text.casefold()
    named = any(n and re.search(rf"\b{re.escape(n.casefold())}\b", folded) for n in people)
    return bool(_HEALTH.search(text)) or named


def keepable(word: str, people: list[str] | tuple[str, ...] = ()) -> bool:
    """A word Pisa may keep in their voice: from `STATES` only, never a health word, never a name
    of their people."""
    return word in STATES and not private(word, people)


def answer(text: str, tempo: Tempo) -> Answer | None:
    """Their reply to Pisa's cue-question, from a closed set: wave it away ("no", "lascia stare" and nothing after;
    "tutto ok" to a heavy-day question), name a state from `STATES`, or a bare yes. None: anything else (a
    question, a command, more words): unanswered, the model answers them."""
    if "?" in text:
        return None
    words = _norm(text)
    if words in _WAVES or (tempo == "up" and words in _FINES):
        return "wave"
    if his_word(text):
        return "named"
    if words in _YESES:
        return "yes"
    return None


def says_remember(text: str) -> bool:
    """Their "ricordalo" at the very end of a named state ("sì, cotto, ricordalo")."""
    return "?" not in text and _strip(_norm(text), _REMEMBERS, lead=False)[1]


def remember_answer(text: str) -> bool | None:
    """Their reply to "Te lo ricordo con la tua parola, «cotto»?": True on their "sì, ricordalo" (or a bare yes),
    False on a no; None otherwise ("ricordalo: compra il latte", "ricordamelo domani" go to the model)."""
    if "?" in text:
        return None
    words = _norm(text)
    if words in REMEMBER_YES:
        return True
    if words in _WAVES or words in {"no non serve", "non serve", "no don't"}:
        return False
    return None


SAID = {
    "wave": {"it-IT": "Va bene, lascio stare.", "en-US": "Ok, I'll leave it."},
    "yes": {"it-IT": "Capito. Ci sono, se serve.", "en-US": "Got it. I'm here if you need me."},
    "named": {
        "it-IT": "Capito. Te lo ricordo con la tua parola, «{w}»?",
        "en-US": "Got it. Shall I remember it in your word, “{w}”?",
    },
    "kept": {"it-IT": "Ok, «{w}»: me lo ricordo.", "en-US": "Ok, “{w}”: I'll remember it."},
    "not_kept": {"it-IT": "Ok, non lo tengo.", "en-US": "Ok, I won't keep it."},
}


def said(kind: str, lang: str, word: str = "") -> str:
    return SAID[kind][lang_key(lang)].format(w=word)


def holds(question_text: str, his: str | None = None) -> bool:
    """RAIL: every question Pisa asks passes `no_label` against their own word (if they gave one)."""
    return not no_label(question_text, f"sono {his}" if his else "")


# --- Pisa's own states ---------------------------------------------------------------------------

_HOW_ARE_YOU = re.compile(
    r"^\W*(?:e\s+)?(?:tu\s*,?\s*)?come\s+stai(?:\s+tu)?(?:\s*,?\s*pisa)?\W*$|"
    r"^\W*(?:pisa\s*,?\s*)?(?:e\s+)?tu\s*,?\s*come\s+stai\W*$|"
    r"^\W*(?:and\s+you\s*,?\s*)?how\s+are\s+you(?:\s+doing)?(?:\s*,?\s*pisa)?\W*$|^\W*(?:pisa\s*,?\s*)?how\s+are\s+you\W*$",
    re.IGNORECASE,
)
_CHARACTER = re.compile(
    r"^\W*(?:pisa\s*,?\s*)?(?:com'è|com'e|come è|qual è) il tuo carattere\W*$|^\W*(?:pisa\s*,?\s*)?"
    r"(?:what's|what is) your character(?: like)?\W*$|^\W*(?:pisa\s*,?\s*)?what are you like\W*$",
    re.IGNORECASE,
)


def is_how_are_you(text: str) -> bool:
    return bool(_HOW_ARE_YOU.match(text.strip()))


def is_character(text: str) -> bool:
    return bool(_CHARACTER.match(text.strip()))


@dataclass(frozen=True)
class StateRecords:
    """Pisa's own records, the only source of its answer about itself."""

    fixed: tuple[str, str] | None = (
        None  # (what Pisa heard, what they meant): Pisa's mistake they corrected, newest
    )
    learned: int = 0  # what Pisa learned about them this week (claims from their own words, kept)
    wondering: int = 0  # what Pisa would like to understand (its hypotheses, still candidates)


_STATE = {
    "fixed": {
        "it-IT": "Ho sbagliato una cosa: avevo capito «{h}», era «{m}». L'ho corretto.",
        "en-US": "I got something wrong: I heard “{h}”, it was “{m}”. I've fixed it.",
    },
    "learned": {
        "it-IT": "Questa settimana ho imparato {n} cose su di te.",
        "en-US": "This week I learned {n} things about you.",
    },
    "learned_1": {"it-IT": "Questa settimana ho imparato una cosa su di te.", "en-US": "This week I learned one thing about you."},
    "wondering": {
        "it-IT": "Ho {n} cose su di te che vorrei capire meglio.",
        "en-US": "There are {n} things about you I'd like to understand better.",
    },
    "wondering_1": {
        "it-IT": "Ho una cosa su di te che vorrei capire meglio.",
        "en-US": "There's one thing about you I'd like to understand better.",
    },
    "nothing": {"it-IT": "Nei miei registri, niente di nuovo.", "en-US": "Nothing new in my records."},
    "here": {"it-IT": "Ci sono, se serve.", "en-US": "I'm here if you need me."},
}  # fmt: skip


def state_answer(records: StateRecords, lang: str) -> str:
    """Pisa's state, from its records only: wrong first, out loud; then what it learned and wonders; then "ci
    sono, se serve". Never a feeling, never needy (`needy` holds it)."""
    key = lang_key(lang)
    out: list[str] = []
    if records.fixed is not None:
        heard, meant = records.fixed
        out.append(_STATE["fixed"][key].format(h=heard[:60], m=meant[:60]))
    if records.learned:
        out.append(_STATE["learned_1" if records.learned == 1 else "learned"][key].format(n=records.learned))
    if records.wondering:
        out.append(
            _STATE["wondering_1" if records.wondering == 1 else "wondering"][key].format(n=records.wondering)
        )
    if not out:
        out.append(_STATE["nothing"][key])
    out.append(_STATE["here"][key])
    return " ".join(out)


_NEEDY = re.compile(
    r"\b(mi manchi|mi sei mancat\w|mi sento|sono (felice|triste|content\w|sol[oa])|mi annoio|ho bisogno di te|"
    r"non lasciarmi|i miss you|i missed you|i feel|i'm (happy|sad|lonely|bored)|i need you|don't leave me)\b",
    re.IGNORECASE,
)


def needy(text: str) -> bool:
    """RAIL: Pisa never claims a feeling beyond its records, never asks for them."""
    return bool(_NEEDY.search(text))


# --- the character file --------------------------------------------------------------------------------

# Pisa's own traits, proposed one at a time; each is theirs to approve (and never proposed again once decided).
TRAITS: dict[str, dict[str, str]] = {
    "wrong_first": {"it-IT": "dire prima quando sbaglio", "en-US": "saying first when I get something wrong"},
    "short": {"it-IT": "parlare corto, la cosa prima", "en-US": "speaking short, the thing first"},
    "days_add_up": {
        "it-IT": "tenere d'occhio che le tue giornate tornino",
        "en-US": "keeping an eye on your days adding up",
    },
}
_TRAIT_OFFER = {
    "it-IT": "Una cosa su di me: vorrei tenere un tratto mio, {t}. Ti va?",
    "en-US": "One thing about me: I'd like to keep a trait of my own, {t}. Is that ok?",
}
_TRAITS_SAID = {
    "it-IT": (
        "Il mio carattere, finora: {t}.",
        "Non ho ancora tratti miei: me li approvi tu, uno alla volta.",
    ),
    "en-US": (
        "My character so far: {t}.",
        "I have no traits of my own yet: you approve them, one at a time.",
    ),
}
TRAIT_DECIDED = {
    True: {"it-IT": "Grazie: lo tengo.", "en-US": "Thanks: I'll keep it."},
    False: {"it-IT": "Va bene, lo lascio.", "en-US": "Ok, I'll drop it."},
}


def next_trait(decided: set[str]) -> str | None:
    return next((k for k in TRAITS if k not in decided), None)


def trait_offer(key: str, lang: str) -> str:
    lk = lang_key(lang)
    return _TRAIT_OFFER[lk].format(t=f"«{TRAITS[key][lk]}»" if lk == "it-IT" else f"“{TRAITS[key][lk]}”")


def traits_said(approved: list[str], lang: str) -> str:
    lk = lang_key(lang)
    some, none = _TRAITS_SAID[lk]
    names = [TRAITS[k][lk] for k in approved if k in TRAITS]
    return some.format(t="; ".join(names)) if names else none


# --- opinions ---------------------------------------------------------------------------------

OpinionKind = Literal["disagree", "habit"]
# The same item of theirs moved this many times in a week: Pisa says once that it disagrees, with the reason.
MOVES_FOR_DISAGREE = 3
# Their turns after LATE_FROM_HOUR on this many of the last 7 nights: a habit Pisa may give a view on, once.
LATE_NIGHTS = 3
LATE_FROM_HOUR = 1  # 01:00-04:59 in their zone
LATE_UNTIL_HOUR = 5

_OPINION = {
    "disagree": {
        "it-IT": "Un parere, una volta sola: è la {n}ª volta in una settimana che spostiamo questo impegno. "
        "Forse gli va dato un orario fisso; decidi tu.",
        "en-US": "One view, just once: we've moved this one {n} times this week. Maybe it needs a fixed slot; "
        "your call.",
    },
    "habit": {
        "it-IT": "Un parere, una volta sola: è la {n}ª notte questa settimana che parliamo dopo l'una. Se vuoi, "
        "la sera ti preparo il giorno dopo prima delle 23; decidi tu.",
        "en-US": "One view, just once: we've talked after 1 am on {n} nights this week. If you like, I can have "
        "tomorrow ready before 11 pm; your call.",
    },
}

_HEALTH = re.compile(
    r"\b(salute|malat\w*|sintom\w*|diagnos\w*|medic\w*|farmac\w*|sonno|dormi\w*|insonnia|depress\w*|ansia|"
    r"ansios\w*|panico|dolor\w*|mal di \w+|febbre|influenza|raffreddat\w*|tosse|nausea|vomit\w*|emicrania|"
    r"pression\w*|cuore|cardio\w*|dieta|peso|stress|terapi\w*|psicolog\w*|psichiatr\w*|fisioterap\w*|"
    r"chemio\w*|oncolog\w*|ospedale|pronto soccorso|visita|analisi|esami|ecografia|dentist\w*|dottor\w*|"
    r"health|ill|illness|sick|symptom\w*|diagnos\w*|doctor|medicine|medical|sleep\w*|insomnia|depress\w*|"
    r"anxiety|anxious|panic|pain|fever|flu|cough|migraine|nause\w*|blood pressure|heart|cardio\w*|diet|weight|"
    r"therap\w*|psycholog\w*|psychiatr\w*|physio\w*|chemo\w*|oncolog\w*|hospital|clinic|dentist|checkup)\b",
    re.IGNORECASE,
)
_TAX = re.compile(
    r"\b(tass[ae]\w*|fisc\w+|irpef|iva\b|detra\w+|dedu\w+|f24|730|dichiarazione dei redditi|partita iva|"
    r"tax\w*|deduct\w*|irs\b|vat\b|write[- ]?off)\b",
    re.IGNORECASE,
)


def opinion(kind: OpinionKind, lang: str, *, n: int) -> str:
    """Never the event's title (it may name a doctor, a therapy, a person): "questo impegno"."""
    return _OPINION[kind][lang_key(lang)].format(n=n)


_WITH_SOMEONE = re.compile(
    r"\b(?:con|with|da|dal|dalla|from)\s+(?:la |il |lo |l'|mia |mio |my )?(?:[A-ZÀ-Ý][\w'-]+|mamma|papà|papa|"
    r"nonn[oa]|zi[oa]|mom|mum|dad|grandma|grandpa)\b"
)


def private_title(title: str, people: list[str] | tuple[str, ...] = ()) -> bool:
    """An event Pisa never gives a view on: a medical word in its title, or someone in it ("Cena con
    Nino", "Pranzo con mamma", a name of their people)."""
    return bool(_HEALTH.search(title) or _WITH_SOMEONE.search(title)) or bool(opinion_rails(title, people))


def opinion_rails(text: str, people: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The rails an opinion breaks: "health" (never a health view or diagnosis), "tax" (never tax
    advice: the "È un conteggio, non una consulenza"), "other people" (never about someone else), and
    `no_label`. An opinion that breaks any is never said."""
    out = []
    if _HEALTH.search(text):
        out.append("health")
    if _TAX.search(text):
        out.append("tax")
    folded = text.casefold()
    if any(name and re.search(rf"\b{re.escape(name.casefold())}\b", folded) for name in people):
        out.append("other people")
    if no_label(text):
        out.append("label")
    return out
