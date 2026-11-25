"""Text folded for matching: one place for the ways the domain compares words, each a named variant.

- `strip_marks`: accents gone (NFKD, combining marks dropped), case kept;
- `fold`: case folded and accents gone ("Città" → "citta");
- `fold_apostrophe`: `fold`, the typographic apostrophe written as a plain one (U+2019 → U+0027);
- `fold_spaces`: `fold`, spaces collapsed;
- `fold_words`: `fold`, then only its words, one space apart ("Via Roma, 1" → "via roma 1");
- `ascii_fold`: accents gone and any other non-ASCII letter dropped, case kept ("Zoë ø" → "Zoe ");
- `words_keep_apostrophe`: case folded, NFC, punctuation to spaces with the apostrophe kept, accents kept;
- `people_name_key`: the people table's `name_key`, written and read with this one rule.

No I/O here."""

import re
import unicodedata

WORD = r"[a-z0-9]+"


def strip_marks(text: str) -> str:
    """Accents gone (NFKD, combining marks dropped); case kept."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def fold(text: str) -> str:
    """Case folded, accents gone: "Città" → "citta"."""
    return strip_marks(text.casefold())


def fold_apostrophe(text: str) -> str:
    """`fold`, with the typographic apostrophe (U+2019) written as a plain one."""
    return fold(text.replace("\u2019", "'"))


def fold_spaces(text: str) -> str:
    """`fold`, every run of spaces one space, none at the ends ("1°  Marzo " → "1° marzo")."""
    return " ".join(fold(text).split())


def fold_words(text: str, word: str = WORD) -> str:
    """`fold`, then only its words (by default lower-case letters and digits), one space apart."""
    return " ".join(re.findall(word, fold(text)))


def ascii_fold(text: str) -> str:
    """Accents gone and every other non-ASCII character dropped (NFKD, then ASCII only); case kept."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def words_keep_apostrophe(text: str) -> str:
    """Case folded, NFC, the typographic apostrophe a plain one, punctuation to spaces (the apostrophe kept),
    spaces collapsed; accents kept."""
    plain = unicodedata.normalize("NFC", text.casefold()).replace("\u2019", "'")
    return " ".join(re.sub(r"[^\w' ]+", " ", plain).split())


def people_name_key(name: str) -> str:
    """The people table's `name_key`: its writer (services/people) and every reader (domain/guardian) use
    this rule: `fold_words` ("Lucia D'Amico" → "lucia d amico")."""
    return fold_words(name)
