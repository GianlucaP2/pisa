"""Who wrote a message and which thing it is about: the small pure rules behind the linker. No I/O.

- A sender's **brand** is the name in its domain ("brivio-sport" from orders@brivio-sport.example.com), so a
  merchant's order mail and its notification address ("brivio-sport-notifiche") are one issuer, and a shared
  mail provider or the reserved `example` domain is no brand at all.
- Two brands are **compatible** when they are equal, or one is the other plus a `-suffix`.
- A brand that is one letter away from another it is not compatible with is a **look-alike**: a risk, never a
  link.
"""

import re

from rapidfuzz.distance import Levenshtein

from pisa.domain.mail import FREEMAIL_DOMAINS

_GENERIC = frozenset({"co", "com", "net", "org", "gov", "ac", "edu", "example"})
_REPLY = re.compile(r"^\s*((re|fwd?|r|i|aw|wg|sv|rv)\s*:\s*)+", re.IGNORECASE)
LOOKALIKE_MIN = 6  # a shorter name is too easy to be one letter from another


def brand_of(address: str | None) -> str:
    """The brand in the address's domain; empty for a shared mail provider or a domain with no name."""
    domain = (address or "").strip().lower().rsplit("@", 1)[-1]
    if not domain or domain in FREEMAIL_DOMAINS:
        return ""
    labels = domain.split(".")[:-1]  # the top-level domain goes
    while len(labels) > 1 and labels[-1] in _GENERIC:
        labels.pop()
    brand = labels[-1] if labels else ""
    return "" if brand in _GENERIC else brand


def compatible(a: str, b: str) -> bool:
    """The same issuer: equal brands, or one the other plus a `-suffix` ("shop" and "shop-notifiche")."""
    if not a or not b:
        return False
    return a == b or a.startswith(b + "-") or b.startswith(a + "-")


def lookalike(a: str, b: str) -> bool:
    """One brand imitates the other: a letter away, and not the same issuer."""
    return min(len(a), len(b)) >= LOOKALIKE_MIN and not compatible(a, b) and Levenshtein.distance(a, b) <= 1


def norm_subject(subject: str | None) -> str:
    """The subject without its reply and forward prefixes, lower case, spaces collapsed."""
    return " ".join(_REPLY.sub("", subject or "").casefold().split())
