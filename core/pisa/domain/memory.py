"""Pisa learns them from what they say, and uses it
when it matters. Pure rules, no I/O.

- **What the night's pass may keep** (`why_not`): a taste or a habit (`like`, `dislike`, `routine`) with a
  **subject and a value** ("musica" → "house, in particolare tech house"), quoted in their own words of that
  conversation (at least two of them, word for word), the value saying what the quote says (a content stem in
  common, the same yes or no). Never a sensitive kind or word (the list, in the subject, the value or the
  quote), never other people (a relation word or a name of their people in the quote or the value). Each is a
  `candidate` with provenance `inferred`: shown in About you, confirmed by their yes.
- **The same claim again** (`same`): their claim with the same subject whose value shares most of its content,
  or a claim they said whose sentence does: one more quote reinforces it, never a second row.
- **The profile in a turn by relevance** (`turn_lines`): hard limits and their style always; the rest only
  when it touches their words or the moment (the topics below: music for an evening, food for a meal,
  breakfast in the morning); at most `CORE_MAX` lines with the past picks, usually far fewer. No words (a
  job's prompt): every line that fits the moment, as before.
- **Past picks in a pick turn** (`pick_kind`, `pick_line`): for the kind of place their words ask for, what
  happened to Pisa's cards (went, liked, didn't go), newest first, at most `PICKS_MAX`.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pisa.domain.learning import clean_name
from pisa.domain.profile import (
    CORE_MAX,
    QUOTE_MIN_WORDS,
    STATEMENT_MAX,
    Claim,
    in_words,
    is_sensitive,
    plain,
    says_no,
    scope_fits,
    stems,
    time_of_day,
)

# --- the night's extraction pass
# --------------------------------------------

Kind = Literal["like", "dislike", "routine"]
KINDS: tuple[str, ...] = ("like", "dislike", "routine")  # taste and habit claims, never anything else
CONVERSATION_EUR = Decimal("0.005")  # FACT: at most €0.005 a conversation
NIGHT_EUR = Decimal("0.05")  # FACT: a hard €0.05 a night, through the ledger
EST_CALL_EUR = Decimal("0.002")  # one call's cost assumed before it runs, until the night's dearest is known
WORDS_MAX = 6000  # characters of their words one call reads (≈ 1,500 tokens: far under €0.005 on the slot)
MAX_TOKENS = 600  # the call's answer: at most PROPOSALS_MAX short claims
PROPOSALS_MAX = 8  # claims kept from one conversation
CONVERSATIONS_MAX = 40  # conversations read a night (the rest wait)
SUBJECT_MAX = 40
VALUE_MAX = 140
QUOTE_MAX = 200
# one quote of theirs: support 1 (confidence 0.33); twice: 0.5, the weekly "Penso che…"
EXTRACTED_WEIGHT = 1.0
SAME_VALUE = 0.5  # this share of the value's content stems in common: the same claim
SIGNAL = "extracted"  # profile_evidence.signal of a quote the night kept
SUBJECT_PREFIX = (
    "words:"  # profile_claims.subject_key of a claim from their words: words:<kind>:<subject>:<hash>
)

# Other people: a quote or a value naming a relation is about someone else (nothing about other
# people).
OTHER_PEOPLE = (  # prefixes of words
    "moglie", "marito", "fidanzat", "mamma", "madre", "padre", "babbo", "fratell", "sorell", "figli", "nonn",
    "cugin", "nipot", "suocer", "cognat", "amic", "colleghi", "coinquilin", "girlfriend", "boyfriend",
    "husband", "mother", "father", "brother", "sister", "daughter", "friend", "colleague", "neighbo",
    "roommate",
)  # fmt: skip
OTHER_PEOPLE_WORDS = frozenset({  # whole words (short ones would match words of their own)
    "zio", "zia", "zii", "zie", "papa", "collega", "capo", "ragazza", "ragazzo", "compagno", "compagna",
    "partner", "wife", "son", "mum", "mom", "dad", "boss",
})  # fmt: skip
_THEY = frozenset({"lui", "lei", "loro", "suo", "sua", "suoi", "sue", "he", "she", "him", "her", "his",
                   "hers", "they", "them", "their"})  # fmt: skip
# someone else's taste said by them ("A Marco piace il sushi", "gli piace", "Giulia
# ama la techno") or someone else's words they passed on (a pasted email, a page, "mi ha scritto…"): the
# sentence around the quote tells, never the quote alone (the model may quote a fragment without the name).
_DATIVE = re.compile(
    r"\b(?:(?:a|ad|al|alla|allo|ai|alle)\s+(?!me\b|noi\b)[a-z0-9]+|gli|le)\s+(?:non\s+)?"
    r"(?:piace|piacciono|piaceva|piacevano|piacerebbe)\b"
)
_THIRD_PERSON = frozenset({  # a taste verb in the third person: someone else's (their own is "amo", "adoro"…)
    "ama", "amano", "adora", "adorano", "preferisce", "preferiscono", "odia", "odiano", "detesta", "likes",
    "loves", "hates", "prefers", "enjoys", "adores",
})  # fmt: skip
# Someone else's words passed on: phrases (their own "ti ho detto", "vado a letto", "I read every night" are
# theirs), and the stems of outside text.
_REPORTED_PHRASES = (
    "mi ha detto", "ha detto che", "hanno detto", "mi hanno detto", "dice che", "dicono che", "mi ha scritto",
    "ha scritto", "hanno scritto", "mi scrive", "scrive che", "ho letto", "letto su", "letto che", "sul sito",
    "su questo sito", "he said", "she said", "they said", "says", "told me", "wrote", "writes", "i read that",
)  # fmt: skip
_REPORTED_STEMS = (
    "articol",
    "recension",
    "review",
    "newsletter",
    "email",
    "mail",
    "inoltr",
    "forward",
    "website",
)
_QUOTED = re.compile(r"«[^»]*»|“[^”]*”|\"[^\"\n]*\"|^\s*>.*$", re.MULTILINE)


def around(quote: str, his_words: str) -> str:
    """The sentence (or sentences) of their words the quote is in: what decides whose taste it is."""
    q = f" {plain(quote)} "
    found: list[str] = []
    for message in his_words.splitlines():
        if q not in f" {plain(message)} ":
            continue
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", message) if q in f" {plain(s)} "]
        found += sentences or [message]
    return " ".join(found) or his_words  # across two messages: all of it (more context only blocks more)


def not_his(quote: str, his_words: str) -> str | None:
    """Why the quote is not their own taste: inside quotation marks (text they passed on), reported words, or
    someone else's taste in its sentence; else None."""
    if any(in_words(quote, m.group(0)) for m in _QUOTED.finditer(his_words)):
        return "quoted text"
    sentence = plain(around(quote, his_words))
    words = set(sentence.split())
    if any(f" {r} " in f" {sentence} " for r in _REPORTED_PHRASES) or any(
        f" {s}" in f" {sentence} " for s in _REPORTED_STEMS
    ):
        return "quoted text"
    if words & _THIRD_PERSON or _DATIVE.search(sentence):
        return "other people"
    return None


@dataclass(frozen=True)
class Proposal:
    """One claim the model proposed from their words."""

    kind: str
    subject: str
    value: str
    quote: str

    def clean(self) -> "Proposal":
        def one(text: str, cap: int) -> str:
            return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:cap]

        return Proposal(
            self.kind.strip().lower(),
            one(self.subject, SUBJECT_MAX).strip(" :.,;").lower(),
            one(self.value, VALUE_MAX).strip(" .,;"),
            one(self.quote, QUOTE_MAX).strip(' «»"“”'),
        )

    @property
    def statement(self) -> str:
        """The claim's sentence: its subject and its value ("musica: house, in particolare tech house")."""
        return f"{self.subject}: {self.value}"[:STATEMENT_MAX]


def _about_others(text: str, people: Iterable[str]) -> bool:
    words = plain(text).split()
    if (OTHER_PEOPLE_WORDS | _THEY) & set(words):
        return True
    if any(w.startswith(stem) for w in words for stem in OTHER_PEOPLE):
        return True
    names = {w for p in people for w in plain(p).split() if len(w) >= 3}  # their people, any of their names
    return bool(names & set(words))


def why_not(p: Proposal, his_words: str, people: Iterable[str] = ()) -> str | None:
    """Why the night must not keep this proposal, or None (RAIL: code's check, never the model's word)."""
    if p.kind not in KINDS:
        return "kind"
    if not p.subject or len(plain(p.value)) < 3:
        return "no subject or value"
    if len(plain(p.quote).split()) < QUOTE_MIN_WORDS:
        return "quote too short"
    if not in_words(p.quote, his_words):
        return "quote not their words"
    if not (stems(p.value) & stems(p.quote)) and not in_words(p.value, p.quote):
        return "value not in the quote"
    if says_no(p.value) != says_no(p.quote) and p.kind != "dislike":
        return "yes or no not theirs"
    people = list(people)
    sentence = around(p.quote, his_words)
    if any(_about_others(t, people) for t in (p.quote, p.value, p.subject, sentence)):
        return "other people"
    if any(is_sensitive(p.kind, t) for t in (p.subject, p.value, p.quote, sentence)):
        return "sensitive"  # the sentence too: "sono vegano, quindi niente carne" is a diet
    return not_his(p.quote, his_words)


def subject_key(p: Proposal) -> str:
    """Stable for the same kind, subject and value's content (a forgotten one's tombstone blocks it back)."""
    digest = hashlib.sha256(" ".join(sorted(stems(p.value) or {plain(p.value)})).encode()).hexdigest()[:12]
    return f"{SUBJECT_PREFIX}{p.kind}:{plain(p.subject)[:SUBJECT_MAX]}:{digest}"[:200]


def same(p: Proposal, kind: str, statement: str, key: str | None) -> bool:
    """`p` says again what this claim says: the same kind and subject with most of the value's content, or (a
    claim they said, no subject) most of the sentence's content."""
    if kind != p.kind or says_no(statement) != says_no(p.statement):
        return False
    if key and key.startswith(SUBJECT_PREFIX):
        _, _, rest = key.partition(SUBJECT_PREFIX)
        their_subject = rest.split(":")[1] if rest.count(":") >= 2 else ""
        if their_subject != plain(p.subject)[:SUBJECT_MAX]:
            return False
        theirs = stems(statement.partition(":")[2])
        mine = stems(p.value)
    else:
        theirs, mine = stems(statement), stems(f"{p.subject} {p.value}")
    return bool(mine and theirs) and len(mine & theirs) / len(mine | theirs) >= SAME_VALUE


# --- the profile in a turn, by relevance ---------------------------------------------------------

# A topic's words, as prefixes of accent-free lowercase words. A claim and their words share a topic:
# relevant.
TOPICS: dict[str, tuple[str, ...]] = {
    "music": ("music", "musica", "canzon", "concert", "dj", "djset", "club", "disco", "techno", "house",
              "jazz", "rock", "rap", "hiphop", "pop", "indie", "elettronic", "electronic", "vinil", "vinyl",
              "festival", "suonar", "ascolt", "artist", "band", "cantant", "ballar", "dance", "line up",
              "lineup", "rave", "live", "sentire"),
    "breakfast": ("colazion", "cornett", "brioche", "cappuccin", "caffe", "coffee", "breakfast", "pasticc",
                  "forno", "bakery", "bar"),
    "food": ("mangi", "cena", "cenar", "pranz", "ristorant", "trattori", "osteri", "pizz", "sushi", "burger",
             "hamburger", "cucina", "piatt", "food", "dinner", "lunch", "eat", "restaurant", "vino", "wine",
             "aperitiv", "gelat", "carbonara", "pasta", "veg", "street food", "bistrot", "smash"),
    "evening": ("sera", "stasera", "serata", "notte", "uscir", "esco", "locale", "locali", "drink",
                "cocktail", "aperitiv", "evening", "tonight", "night", "going out", "club", "disco"),
    "travel": ("viaggi", "volo", "voli", "hotel", "vacanz", "trip", "travel", "flight", "aeroport", "airport",
               "weekend fuori", "valigi"),
    "walking": ("piedi", "cammin", "passegg", "walk", "lontan", "vicin", "distanz", "minuti", "taxi", "metro",
                "bus", "bici", "bike", "macchina", "auto"),
    "shopping": ("compr", "negozi", "regal", "vestit", "scarp", "shop", "buy", "store", "mercat"),
    "sport": ("palestr", "corr", "corsa", "sport", "allen", "gym", "run", "nuot", "calcio", "padel", "tennis",
              "yoga"),
    "work": ("lavor", "uffici", "riunion", "meeting", "call", "progett", "work", "office", "client"),
    "place": ("posto", "posti", "dove", "consigli", "suggerisc", "trova", "cerca", "andiamo", "vado",
              "solito", "place", "where", "recommend", "suggest", "somewhere"),
    "again": ("solito", "altra volta", "come sempre", "stesso", "stessa", "ancora", "di nuovo", "again",
              "usual", "same", "last time", "l ultima volta"),
}  # fmt: skip
# A place's kind for a topic of their words (the provider's category, as prefixes of its words).
PLACE_KINDS: dict[str, tuple[str, ...]] = {
    "breakfast": ("cafe", "coffee", "bakery", "pastry"),
    "food": ("restaurant", "pizza", "food", "meal", "trattoria", "osteria", "bistro", "sushi", "burger",
             "ice cream"),
    "evening": ("bar", "pub", "night club", "wine bar", "cocktail", "club"),
    "music": ("night club", "bar", "music", "concert", "club", "live"),
}  # fmt: skip
PICK_TOPICS = ("breakfast", "food", "evening", "music")
MOMENT_TOPICS: dict[str, set[str]] = {  # a place asked with no kind: the kind the hour suggests
    "morning": {"breakfast"},
    "afternoon": {"food"},
    "evening": {"food", "evening"},
    "night": {"evening"},
}
PICKS_MAX = 5  # past picks in a pick turn
# a hunch is never acted on before their yes: none in a turn
CANDIDATES_MAX = 0
PLACE_LIKES_MAX = 5  # places they went back to, in a pick turn
ALWAYS_KINDS = ("style",)  # how they want Pisa to talk: every turn


def _has(text: str, stem: str) -> bool:
    if " " in stem:
        return f" {stem}" in f" {text}"
    return any(w.startswith(stem) for w in text.split())


def topics(text: str) -> set[str]:
    folded = plain(text)
    return {t for t, words in TOPICS.items() if any(_has(folded, w) for w in words)}


def turn_topics(words: str, local: datetime) -> set[str]:
    """Their words' topics, with the moment's: a place asked for in the morning is breakfast (
    "music for an evening, food for a meal")."""
    found = topics(words)
    if found & {"place", "again"} and not found & {
        "breakfast",
        "food",
        "evening",
        "music",
        "shopping",
        "sport",
    }:
        found |= MOMENT_TOPICS[time_of_day(local)]  # "il solito posto?", "come l'altra volta": by the hour
        found.add("place")
    return found


def claim_topics(c: Claim) -> set[str]:
    found = topics(c.statement.replace("«", " ").replace("»", " "))
    if c.kind == "mobility":
        found.add("walking")
    if (c.subject_key or "").startswith("place:"):
        found.add("place")
    if (c.subject_key or "").startswith("routine:") or c.kind == "routine":
        found.add("again")
    return found


def line(c: Claim) -> str:
    """One claim in a prompt: provenance, confidence, scope first (pisa.domain.profile.core_lines's form)."""
    tags = [f"{c.provenance} {c.confidence:.2f}"]
    if c.state == "candidate":
        tags.append("unconfirmed: their words once, say «mi sembra che…» and never act on it alone")
    if c.hard:
        tags.append("HARD LIMIT")
    if c.sensitive:
        tags.append("sensitive: use silently, never say it unless the user asks")
    if c.scope:
        tags.append("only " + ", ".join(f"{k} {v}" for k, v in sorted(c.scope.items())))
    return f"[{'; '.join(tags)}] {c.kind}: {c.statement}"


def relevant(
    claims: list[Claim], words: str, local: datetime, travelling: bool = False, room: int = CORE_MAX
) -> list[Claim]:
    """The claims for this turn: hard limits and style always; the rest only on their words' topics or content
    (a pick turn also takes their walking limit and the places they went back to); their unconfirmed claims
    only on their words' topic. At most `room`, hard limits first, then by how much they touch, then
    confidence."""
    fitting = [c for c in claims if scope_fits(c.scope, local, travelling)]
    asked = turn_topics(words, local)
    mine = stems(words)
    picking = bool(asked & {"place", *PICK_TOPICS})
    scored: list[tuple[int, Claim]] = []
    for c in fitting:
        on = claim_topics(c)
        touch = 2 * len(on & (asked - {"place", "again"})) + len(stems(c.statement) & mine)
        if c.state == "candidate":
            if touch < 2:  # unconfirmed: only on their words' own topic
                continue
        elif c.hard or c.kind in ALWAYS_KINDS:
            touch = max(touch, 100)
        elif picking and (c.kind == "mobility" or "walking" in on):
            touch = max(touch, 3)
        elif picking and "place" in on:  # a place they went back to, "posti tranquilli": a pick's taste
            touch = max(touch, 1)
        if touch <= 0:
            continue
        scored.append((touch, c))
    scored.sort(key=lambda tc: (not tc[1].hard, -tc[0], -tc[1].confidence, -tc[1].updated_at.timestamp()))
    out: list[Claim] = []
    unconfirmed = places = 0
    for _, c in scored:
        if c.state == "candidate":
            if unconfirmed >= CANDIDATES_MAX:
                continue
            unconfirmed += 1
        elif (c.subject_key or "").startswith("place:"):
            if places >= PLACE_LIKES_MAX:
                continue
            places += 1
        out.append(c)
        if len(out) >= room:
            break
    return out


# --- past picks --------------------------------------------------------------------------------------------


def pick_kind(words: str, local: datetime) -> tuple[str, ...]:
    """The provider categories of the kind of place their words ask for, or () when they ask for none."""
    asked = turn_topics(words, local)
    if not asked & {"place", *PICK_TOPICS}:
        return ()
    kinds: list[str] = []
    for topic in PICK_TOPICS:
        if topic in asked:
            kinds += PLACE_KINDS[topic]
    return tuple(dict.fromkeys(kinds))


def of_kind(category: str | None, kinds: tuple[str, ...]) -> bool:
    folded = plain((category or "").replace("_", " "))
    return bool(folded) and any(_has(folded, k) for k in kinds)


@dataclass(frozen=True)
class Pick:
    """A place one of Pisa's cards showed them, and what happened."""

    name: str
    category: str
    shown_at: datetime
    went: bool
    go: bool
    liked: bool  # a like of theirs for the place, in use
    dismissed: bool


def pick_line(p: Pick, local_shown: datetime) -> str:
    if p.went and p.liked:
        what = "they went, and like it"
    elif p.went:
        what = "they went"
    elif p.liked:
        what = "no visit from this card, but they like the place"  # never "didn't go"
    elif p.go:
        what = "they asked the way, no visit seen"
    elif p.dismissed:
        what = "they closed the card: didn't go"
    else:
        what = "didn't go"
    kind = clean_name(p.category.replace("_", " "))[1:-1][:30]  # a provider's text, cleaned
    return f"[past pick] {clean_name(p.name)} ({kind}), Pisa's card of {local_shown:%a %d %b}: {what}"
