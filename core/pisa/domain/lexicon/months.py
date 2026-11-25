"""Month names, Italian and English, full and short, and the tables built from them.

No I/O here."""

from collections.abc import Iterable, Mapping

IT_MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
             "ottobre", "novembre", "dicembre")  # fmt: skip
EN_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december")  # fmt: skip
IT_MONTHS_SHORT = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")
EN_MONTHS_SHORT = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# as an English line writes them ("October 12", "Oct 12")
EN_MONTHS_TITLE = tuple(m.title() for m in EN_MONTHS)
EN_MONTHS_SHORT_TITLE = tuple(m.title() for m in EN_MONTHS_SHORT)

# how a line in each language writes a month ("12 ottobre", "October 12")
MONTH_NAMES: dict[str, tuple[str, ...]] = {"it-IT": IT_MONTHS, "en-US": EN_MONTHS_TITLE}
# the short form a compact line writes ("12 ott", "Oct 12")
MONTHS_SHORT: dict[str, tuple[str, ...]] = {"it-IT": IT_MONTHS_SHORT, "en-US": EN_MONTHS_SHORT_TITLE}


def month_numbers(*tables: Iterable[str], extra: Mapping[str, int] | None = None) -> dict[str, int]:
    """Each word of the tables (twelve words each, January first) and of `extra`, to its month number."""
    found: dict[str, int] = {}
    for table in tables:
        for n, word in enumerate(table, start=1):
            found.setdefault(word, n)
    for word, n in (extra or {}).items():
        found.setdefault(word, n)
    return found


def month_forms(*tables: Iterable[str], extra: Mapping[str, int] | None = None) -> dict[int, tuple[str, ...]]:
    """Each month number to its words in the tables and `extra`, in that order, each word once."""
    forms: dict[int, list[str]] = {n: [] for n in range(1, 13)}
    for word, n in month_numbers(*tables, extra=extra).items():
        forms[n].append(word)
    return {n: tuple(words) for n, words in forms.items()}
