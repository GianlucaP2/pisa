"""A task is written for the person who owes the action. Pure rules, no I/O.

Two refusals, both for a task text that would send the owner to do what someone else owes, or to write to
himself:
- the debtor is someone else (the shop owes the answer) and the text is phrased as the owner's to-do
  ("Reply to the shop", "Rispondere al negozio"): that is a "waiting" task, and it reads as one
  ("In attesa del negozio");
- the text names the owner as the other side of an exchange ("Reply to <the owner> about the refund"), which
  is what happens when a reply quotes the owner's own mail.

The debtor comes from code's reading of the mail, never from the text of a mail.
"""

import re
from dataclasses import dataclass
from typing import Literal

Debtor = Literal["owner", "other", "none"]

OTHER_AS_OWNER_TODO = "debtor_other_as_owner_todo"
OWNER_AS_COUNTERPART = "owner_as_counterpart"

# the words that make a task a wait: "Waiting for the refund", "In attesa del corriere", "Aspettare ..."
_WAITING = re.compile(
    r"^\W*(?:waiting|awaiting|await|expecting|expect|pending|in attesa|attesa|aspett\w*|attend\w*)\b"
    r"|\b(?:waiting (?:for|on)|in attesa (?:di|del|della|dei|delle|dello|degli)|awaiting)\b",
    re.IGNORECASE,
)
# the verbs a to-do starts with, in English (the bare form) and Italian (infinitive or imperative)
_TODO_VERBS = frozenset(
    """
    reply respond answer send write email call phone pay ship post book buy order pick collect
    return confirm check sign submit fill print bring take renew cancel schedule prepare review
    approve file upload download forward follow remind contact ask tell provide complete update
    rispondi rispondere scrivi scrivere invia inviare manda mandare chiama chiamare paga pagare
    spedisci spedire prenota prenotare compra comprare ordina ordinare ritira ritirare restituisci
    restituire conferma confermare controlla controllare firma firmare compila compilare stampa
    stampare porta portare rinnova rinnovare annulla annullare prepara preparare approva approvare
    carica caricare scarica scaricare inoltra inoltrare ricorda ricordare contatta contattare chiedi
    chiedere dici dire fornisci fornire completa completare aggiorna aggiornare verifica verificare
    """.split()
)
_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)?", re.UNICODE)
# a verb that talks to someone, and the name after it
_TALK = (
    r"(?:reply|respond|write|answer|email|e-mail|tell|call|phone|thank|remind|ask|"
    r"rispond\w*|scriv\w*|invi\w*|mand\w*|chiam\w*|avvis\w*|ringrazi\w*|ricord\w*|chied\w*)"
)


@dataclass(frozen=True)
class Finding:
    code: str
    reason: str  # one plain sentence, for a log or a test, never shown to the owner as is


def owner_todo(text: str) -> bool:
    """The text is phrased as the owner's to-do: not a wait, and its first word is an action verb."""
    if _WAITING.search(text):
        return False
    first = _WORD.search(text)
    return bool(first) and first.group(0).lower() in _TODO_VERBS  # type: ignore[union-attr]


def names_owner(text: str, owner_names: tuple[str, ...]) -> bool:
    """A verb that talks to someone, then the owner's name or address, in the same phrase."""
    for name in owner_names:
        name = name.strip()
        if len(name) < 2:
            continue
        near = rf"\b{_TALK}\b[^.;:!?\n]{{0,40}}?(?<!\w){re.escape(name)}(?!\w)"
        if re.search(near, text, re.IGNORECASE):
            return True
    return False


def lint(title: str, *, debtor: Debtor, owner_names: tuple[str, ...] = ()) -> tuple[Finding, ...]:
    """What is wrong with the task text, nothing when it is fine. `owner_names`: his display-name variants
    and the local parts of his addresses."""
    found: list[Finding] = []
    if debtor == "other" and owner_todo(title):
        found.append(
            Finding(OTHER_AS_OWNER_TODO, "someone else owes this, but it reads as the owner's to-do")
        )
    if names_owner(title, owner_names):
        found.append(Finding(OWNER_AS_COUNTERPART, "the text names the owner as the one to write to"))
    return tuple(found)


def refused(title: str, *, debtor: Debtor, owner_names: tuple[str, ...] = ()) -> bool:
    """The task is refused: it fails either rule."""
    return bool(lint(title, debtor=debtor, owner_names=owner_names))
