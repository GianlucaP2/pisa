"""Speculation with the rails. Pisa's brain starts on the user's partial words; what
it may do then is code's, never the model's:

- **Pisa's own reads** (the calendar and tasks in Pisa's database, today's plan, what waits for a yes): on any
  partial words. They cost nothing and change nothing.
- **Outside reads** (a provider's paid call: a travel time): only on the final words, even on a stable
  prefix (the place may still be being said). A stable prefix (words that stayed the same across two partials
  and STABLE_S) only lets the reflex draft its ready answer.
- **Writes**: only on the final words, by construction: the reflex has no write at all, and the main thinker
  runs on finals only.
"""

import re
from typing import Literal

Read = Literal["agenda", "tasks", "plan", "waiting", "travel", "weather"]
INTERNAL: frozenset[str] = frozenset({"agenda", "tasks", "plan", "waiting"})
OUTSIDE: frozenset[str] = frozenset({"travel", "weather"})  # the forecast, on final words
STABLE_S = 0.6  # the words so far held this long, across two partials: an outside read may start
STABLE_KEEP = 3  # … and at least this many words (a prefix of one or two words says nothing yet)

_WORD = re.compile(r"\w+", re.UNICODE)


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def may_read(read: str, *, final: bool = False) -> bool:
    """RAIL: Pisa's own reads on any words; an outside read only on the final words."""
    if read in INTERNAL:
        return True
    if read in OUTSIDE:
        # Only on the final words, never on a stable prefix: a paid read ran while the place was still
        # being said ("Milano" before "centrale"), one paid call per new place.
        return final
    return False  # an unknown read is never run on speculation


def same_prefix(before: str, after: str) -> bool:
    """The later partial keeps the earlier one's words (it only grew or stayed): the prefix held."""
    a, b = words(before), words(after)
    return len(a) >= STABLE_KEEP and b[: len(a)] == a


def follows(heard: str, final: str) -> bool:
    """The final words are the partial words the reflex read, grown or equal (a recogniser may still fix the
    last word): the reflex's reads and its ready answer are about these words."""
    a, b = words(heard), words(final)
    if not a or not b:
        return False
    keep = a[:-1] if len(a) > STABLE_KEEP else a
    return b[: len(keep)] == keep and len(b) - len(a) <= 3
