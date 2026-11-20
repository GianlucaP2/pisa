"""Weekday names, Italian and English, Monday first (as `date.weekday()`), and the tables built from them.

No I/O here."""

from collections.abc import Iterable

IT_WEEKDAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
# as a folded text writes them (accents gone)
IT_WEEKDAYS_PLAIN = ("lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica")
EN_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
EN_WEEKDAYS_TITLE = tuple(d.title() for d in EN_WEEKDAYS)
IT_WEEKDAYS_SHORT = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")
EN_WEEKDAYS_SHORT_TITLE = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# how a line in each language names a day ("martedì", "Tuesday")
DAY_NAMES: dict[str, tuple[str, ...]] = {"it-IT": IT_WEEKDAYS, "en-US": EN_WEEKDAYS_TITLE}
# the short form a compact line writes ("mar 12", "Tue 12")
WEEKDAYS_SHORT: dict[str, tuple[str, ...]] = {"it-IT": IT_WEEKDAYS_SHORT, "en-US": EN_WEEKDAYS_SHORT_TITLE}


def weekday_numbers(*tables: Iterable[str], start: int = 0) -> dict[str, int]:
    """Each word of the tables (seven words each, Monday first) to its day number, Monday being `start`."""
    found: dict[str, int] = {}
    for table in tables:
        for n, word in enumerate(table, start=start):
            found.setdefault(word, n)
    return found


# the folded names a turn's words are matched with, Monday 0
WEEKDAY_NUMBERS = weekday_numbers(IT_WEEKDAYS_PLAIN, EN_WEEKDAYS)
