"""Heard in 1 s: Pisa's first words say back the unusual part of what the user asked ("Pizza al
taglio da guida, non la più vicina… cerco."), so they know at once Pisa heard what matters; never a generic
holding phrase when there is something to restate. Pure rules from their own words: no fact, no claim.

Only a search (find, look for, recommend) whose words carry something unusual (a "non", a "senza", a
guide, opening hours, a diet, a price, a number of people): an ordinary "cosa ho domani" has nothing to say
back, and the reflex or the brain answers it quickly anyway.
"""

import re

# only a search (find, look for, recommend, where can I…): "voglio/mi serve/I need/I want" open
# commands too ("I need to cancel the meeting"), never said back
_ASK = re.compile(
    r"^(?:(?:ok|allora|senti|pisa|ehi|hey|e|and|so)\W+)*"
    r"(?:mi\s+)?(?:trovami|cercami|trova|cerca|consigliami|consiglia|suggeriscimi|suggerisci|"
    r"mi\s+(?:trovi|cerchi|consigli)|dove\s+(?:posso\s+)?"
    r"(?P<where>trovo|trovare|mangio|mangiare|prendo|prendere|bevo|bere)|"
    r"find\s+me|find|look\s+for|search\s+for|recommend|suggest|"
    r"where\s+can\s+i\s+(?P<where_en>get|find|eat|have))\b\W*",
    re.IGNORECASE,
)
# What follows is a command, not a thing to find: a verb first ("spostare", "to cancel", "you to delete"),
# "che …", or a change verb anywhere ("trova il modo di cancellare la riunione")
_NOT_A_THING = re.compile(
    r"^(?:\w+(?:are|ere|ire)\b|to\b|you\b|che\b|di\b|if\b|se\b|how\b|come\b)", re.IGNORECASE
)
_CHANGE = re.compile(
    r"\b(?:spost\w*|cancell\w*|annull\w*|elimin\w*|togli\w*|mett\w*|aggiung\w*|mand\w*|invi\w*|"
    r"prenot\w*|move|cancel|delete|remove|send|book|add|reschedule|postpone)\b",
    re.IGNORECASE,
)
_ARTICLE = re.compile(r"^(?:un|una|uno|un'|dei|degli|delle|qualche|il|la|lo|i|gli|le|a|an|some|the)\s+", re.I)
_UNUSUAL = re.compile(
    r"\b(?:non|not|no|senza|without|da\s+guida|guida|guide|michelin|gambero\s+rosso|aperto|aperta|aperti|open|"
    r"vegan\w*|vegetarian\w*|gluten\w*|celiac\w*|economic\w*|cheap|budget|tranquill\w*|quiet|dehors|terrazza|"
    r"terrace|outdoor|all'aperto|vista|view|cani|dog\w*|autentic\w*|authentic|local\w*|tipic\w*|migliore|best|"
    r"per\s+\d+|for\s+\d+|entro|within|sotto\s+i|under|prima\s+delle|before|dopo\s+le|after)\b",
    re.IGNORECASE,
)
_END = re.compile(r"\s*(?:\?|\bper favore\b|\bplease\b|\bgrazie\b|\bthanks\b).*$", re.IGNORECASE)
# Their words, said back to them: "vicino a me" → "vicino a te"
_YOURS = {
    "it-IT": {"me": "te", "mio": "tuo", "mia": "tua", "miei": "tuoi", "mie": "tue", "noi": "voi",
              "nostro": "vostro", "nostra": "vostra", "mangio": "mangiare", "prendo": "prendere",
              "bevo": "bere", "trovo": "trovare"},
    "en-US": {"me": "you", "my": "your", "i": "you", "our": "your", "us": "you", "we": "you"},
}  # fmt: skip
# "dove posso mangiare senza glutine": the verb is part of what they asked
_SAY_VERB = frozenset({"mangiare", "mangio", "bere", "bevo", "prendere", "prendo", "eat", "have"})
MAX_WORDS = 12
TAIL = {"it-IT": "cerco.", "en-US": "on it."}


def _swap(word: str, yours: dict[str, str]) -> str:
    bare = word.rstrip(",.;:!")
    return yours.get(bare.casefold(), bare) + word[len(bare) :]


def heard(words: str, lang: str) -> str | None:
    """The line that says back the unusual part of a search ("Pizza al taglio da guida, non la più vicina…
    cerco."), or None when the words ask no search or have nothing unusual in it."""
    text = " ".join(words.replace("\u2019", "'").split())
    asked = _ASK.match(text)
    if asked is None:
        return None
    what = _END.sub("", text[asked.end() :]).strip(" ,.;:!")
    what = _ARTICLE.sub("", what)
    if not what or not _UNUSUAL.search(what) or _NOT_A_THING.match(what) or _CHANGE.search(what):
        return None
    verb = asked.group("where") or asked.group("where_en")
    if verb and verb.casefold() in _SAY_VERB:
        what = f"{verb} {what}"
    yours = _YOURS.get(lang, _YOURS["en-US"])
    out = [_swap(w, yours) for w in what.split()][:MAX_WORDS]
    said = " ".join(out).rstrip(" ,.;:")
    return f"{said[:1].upper()}{said[1:]}… {TAIL.get(lang, TAIL['en-US'])}"
