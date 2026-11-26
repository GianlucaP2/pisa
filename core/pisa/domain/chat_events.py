"""What the owner's own chat words can say about a storyline (LIFE-GRAPH section 7: chat episodes).
Pure rules, no I/O.

"Ho già spedito", "l'ho pagato", "risolto": each is an event from the owner, like a courier's mail is one from
the courier. Code, not the model, decides whether the owner's words say it:
- the quote must be the owner's own words of THIS turn, word for word (never an email's, never Pisa's);
- the words must say the event's kind (`SAYS`), as a statement: a question, a negation ("non ancora"), a
  plan ("devo spedire") or a wish says nothing happened;
- a storyline is closed by them only when they name it: its exact key, the other side's name, or the thing
  itself ("l'appuntamento") when it is the only one of its kind that is open (`names`). Else the closing is a
  proposal for the owner's yes, like any closing without an exact key.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pisa.domain.text import fold

# The events the owner can say in chat, and the words that say each (folded: lower case, no accents).
CHAT_KINDS: tuple[str, ...] = ("shipped", "paid", "acknowledged", "cancelled", "attended", "owner_replied")
UNDO = "undo"

SAYS: dict[str, re.Pattern[str]] = {
    "shipped": re.compile(
        r"\b(?:sped(?:ito|ita|ii)|mandat[oa]|spedizione fatta|consegnat[oa] al corriere|shipped|sent it|"
        r"sent (?:the|your) (?:parcel|package|item)|posted it|mailed it|"
        r"dropped (?:it|the parcel|the package) off|"
        r"handed (?:it|the parcel|the package) (?:in|to the courier))\b"
    ),
    "paid": re.compile(
        r"\b(?:pagat[oa]|ho pagato|bonifico fatto|fatto il bonifico|paid|"
        r"settled (?:it|the (?:bill|payment|invoice)))\b"
    ),
    "acknowledged": re.compile(
        r"\b(?:risolt[oa]|fatto|sistemat[oa]|chiuso|a posto|done|resolved|sorted|all good|"
        r"taken care of|handled|fixed)\b"
    ),
    "cancelled": re.compile(
        r"\b(?:disdett[oa]|annullat[oa]|disdire|cancell?ed|cancell?ato|called (?:it )?off)\b"
    ),
    "attended": re.compile(
        r"\b(?:ci sono stat[oa]|sono stat[oa]|ci sono andat[oa]|sono andat[oa]|went (?:there|to)|"
        r"been there|attended|was there|visita fatta)\b"
    ),
    "owner_replied": re.compile(
        r"\b(?:ho risposto|risposto|ho richiamato|richiamat[oa]|replied|answered|wrote back|"
        r"called (?:them )?back|"
        r"got back to (?:them|him|her))\b"
    ),
}
# a statement that nothing happened (yet), that it is a question, a plan or a wish
_NOT_A_STATEMENT = re.compile(
    r"\b(?:non|not|never|mai|no|ancora da|devo|dovrei|vorrei|sto per|devi|must|should|need to|needs to|"
    r"have to|"
    r"will|going to|gonna|forse|maybe|perhaps|se|if|when|quando|appena|dopo che|before|prima di)\b|n't"
)
_UNDO = re.compile(
    r"\b(?:annulla|togli|ritira|undo|take (?:it|that) back|scratch that|cancel that|"
    r"non (?:l'|lo |la )?(?:ho|ho ancora)? ?(?:fatto|spedito|pagato|risolto)|ho sbagliato|my mistake|"
    r"i was wrong)\b"
)

# the thing a playbook is about, as the owner says it: naming it names the storyline when it is the only one
NOUNS: dict[str, tuple[str, ...]] = {
    "marketplace_sale": ("vendita", "venduto", "sale", "sold"),
    "purchase_delivery": ("ordine", "order", "pacco", "parcel", "package", "acquisto", "purchase"),
    "refund_support": ("rimborso", "refund", "reso", "return", "reclamo", "claim"),
    "booking_appointment": (
        "appuntamento",
        "prenotazione",
        "visita",
        "appointment",
        "booking",
        "reservation",
    ),
    "conversation_request": (),
}
NAME_MIN = 4  # a name's shortest word that counts as naming someone


def squash(text: str) -> str:
    """Folded, the typographic apostrophe plain, every run of spaces one."""
    return " ".join(fold(text.replace("\u2019", "'")).split())


def in_words(quote: str, words: str) -> bool:
    """The quote is the owner's own words, word for word (case, accents and spaces aside)."""
    q = squash(quote).strip(" .,;:!\"'«»")
    return len(q) >= 3 and q in squash(words)


def says(kind: str, quote: str, words: str) -> bool:
    """The owner's words say that this happened: the quote has the kind's words, and nothing in what they said
    this turn makes it a question, a negation or a plan."""
    pattern = SAYS.get(kind)
    if pattern is None or not pattern.search(squash(quote)):
        return False
    folded = squash(words)
    if "?" in words or _NOT_A_STATEMENT.search(folded):
        return False
    return True


def points(words: str) -> bool:
    """The owner's words may say one of these events (so the tool is offered at once): a statement with one
    of the kinds' words."""
    folded = squash(words)
    if "?" in words or _NOT_A_STATEMENT.search(folded):
        return False
    return any(p.search(folded) for p in SAYS.values())


def undoes(words: str) -> bool:
    """The owner's words take back what they said before."""
    return bool(_UNDO.search(squash(words)))


@dataclass(frozen=True)
class Named:
    """How the owner's words named a storyline: by its exact `key`, by `name` (the other side), by `noun` (the
    thing itself, the only one of its kind open), or not at all (`None`)."""

    how: str | None

    @property
    def exact(self) -> bool:
        return self.how is not None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", squash(text)))


def names(words: str, *, keys: Iterable[str], who: Iterable[str], playbook: str, only_of_kind: bool) -> Named:
    """Whether the owner's words name this storyline. `keys`: its exact keys as the mail wrote them; `who`:
    the other side's names (a display name, a brand); `only_of_kind`: no other open one of its playbook."""
    said = squash(words)
    spoken = _words(words)
    for key in keys:
        k = squash(key)
        if len(k) >= 4 and k in said:
            return Named("key")
    for name in who:
        if any(len(part) >= NAME_MIN and part in spoken for part in _words(name)):
            return Named("name")
    if only_of_kind and any(noun in spoken for noun in NOUNS.get(playbook, ())):
        return Named("noun")
    return Named(None)
