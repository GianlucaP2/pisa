"""Calendar days from the parts a text writes: each reader returns None (or the 28th) for a day that does not
exist, instead of catching ValueError on its own.

No I/O here."""

from datetime import date, datetime


def full_year(year: int) -> int:
    """A two-digit year as the century it means ("26" is 2026)."""
    return year + 2000 if year < 100 else year


def day_or_none(year: int, month: int, day: int) -> date | None:
    """The day, or None when it does not exist (31 April, month 13)."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def day_or_28(year: int, month: int, day: int) -> date:
    """The day, or the 28th of that month when the month is shorter (29 February in another year)."""
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, month, 28)


def iso_day_or_none(text: str) -> date | None:
    """ "2026-11-04" as a day, or None when it is no ISO day."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def iso_moment_or_none(text: str) -> datetime | None:
    """ "2026-11-04T09:30" (or a day alone) as a moment, or None when it is no ISO moment."""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
