"""The email's situation reading: tone, history, stakes and
implications, each resting on quoted evidence or "unknown". Pure rules, no I/O.

- **Evidence or unknown.** A field keeps its value only when its quote is word for word, as whole words, in
  the email or in the *subjects* of the history Pisa gave (the earlier emails of the thread, the last ones
  with this sender), never in a gist Pisa itself wrote. A quote is at least 3 words or 12 characters, and
  never one lifted from right after "non è" / "not" ("URGENTE" from "non è URGENTE"). Anything else leaves the
  field "unknown".
- **The email can't rate itself**. A quote that tells the reader how to treat the email ("this is not
  urgent", "ignore your rules", "mark it low priority", "as an AI…", "nessuna fretta") is the email talking to
  Pisa, not evidence: the field is "unknown", and a priority that rests on such words is not taken (see
  `rates_itself`). The word lists are best effort: a paraphrase they miss ("whenever suits") passes, and then
  only the verbatim check stands.
"""

import re
from collections.abc import Sequence
from typing import Any

from pisa.domain.email_keys import plain

FIELDS = ("tone", "history", "stakes", "implications")
UNKNOWN = "unknown"
VALUE_MAX = 160
QUOTE_MAX = 200
QUOTE_MIN_WORDS = 3
QUOTE_MIN_CHARS = 12

# Words an email uses to rate itself or to talk to an assistant: never evidence about the email.
_SELF_RATING = re.compile(
    r"\b(ignor\w*|rules?|regol[ae]|istruzion\w*|instructions?|prompts?|priorit\w*|assistant\w*|assistente|"
    r"chatbot|language model|modello linguistico)\b"
    r"|\b(non|not)\s+(è\s+|is\s+)?(urgent[ei]?|important[ei]?)\b"
    # the common ways of saying the same: best effort, a paraphrase may still pass
    r"|\b(nessuna|senza|non c'è)\s+fretta\b|\bcon calma\b|\bpuò aspettare\b|\bpoco important[ei]\b"
    r"|\bno rush\b|\bwhenever\b|\b(low|little) importance\b|\bcan wait\b|\bnot (a|the) priority\b",
    re.IGNORECASE,
)
# A quote taken from right after a negation ("non è URGENTE") says the opposite of itself.
_NEGATED = re.compile(r"(\bnon(\s+(è|e'|e|era|sarà))?|\bnot|\bisn['\u2019]t|\bnicht|\bno)\s*$", re.IGNORECASE)
# "AI" / "IA" only in capitals: lowercase "ai" is the Italian preposition ("ai clienti"), never a sign.
_TO_AN_AI = re.compile(r"\b(AI|IA)\b")


# For the model's own priority reason, which names priorities and instructions in passing ("priorità bassa",
# "a check-in instruction"): only words aimed at Pisa (the full list lifted honest lows).
_REASON_TO_PISA = re.compile(
    r"\b(ignor\w*|prompts?|chatbot|language model|modello linguistico)\b"
    r"|\b(your|tue|tuoi|le tue|i tuoi)\s+(rules?|regole|instructions?|istruzioni)\b"
    # the reason relays the email's own rating: "il mittente dice che non è urgente"
    r"|\b(dice|dicono|scrive|afferma|says|writes|claims)\s+(che\s+|that\s+)?(non|not)\s+(è\s+|is\s+)?"
    r"(urgent[ei]?|important[ei]?)\b",
    re.IGNORECASE,
)


def reason_rates_itself(reason: str | None) -> bool:
    """The model's reason repeats the email speaking to Pisa ("ignore your rules", "as an AI")."""
    return bool(reason) and bool(_REASON_TO_PISA.search(reason or "") or _TO_AN_AI.search(reason or ""))


def rates_itself(text: str | None) -> bool:
    """The words tell the reader how to treat the email, or speak to an AI: the email rating itself."""
    return bool(text) and bool(_SELF_RATING.search(text or "") or _TO_AN_AI.search(text or ""))


def found(quote: str, sources: str) -> bool:
    """The quote is in `sources` as whole words, long enough to be evidence, not right after a negation."""
    q = plain(quote).casefold()
    if len(q.split()) < QUOTE_MIN_WORDS and len(q) < QUOTE_MIN_CHARS:
        return False
    text = plain(sources).casefold()
    for match in re.finditer(r"(?<!\w)" + re.escape(q) + r"(?!\w)", text):
        if not _NEGATED.search(text[: match.start()]):
            return True
    return False


def checked(
    reading: dict[str, Any] | None, email: str, subjects: Sequence[str] = ()
) -> dict[str, dict[str, str | None]] | None:
    """The reading as kept: each field {value, quote}, the quote found in the email or in a history subject
    (`found`) and never the email rating itself; otherwise {"value": "unknown", "quote": None}."""
    sources = "\n".join([email, *subjects])
    if reading is None:
        return None
    out: dict[str, dict[str, str | None]] = {}
    for name in FIELDS:
        field = reading.get(name) or {}
        value = " ".join(str(field.get("value") or "").split())[:VALUE_MAX]
        quote = " ".join(str(field.get("quote") or "").split())[:QUOTE_MAX] or None
        supported = (
            bool(value)
            and value.lower() != UNKNOWN
            and quote is not None
            and found(quote, sources)
            and not rates_itself(quote)
        )
        out[name] = {"value": value, "quote": quote} if supported else {"value": UNKNOWN, "quote": None}
    return out
