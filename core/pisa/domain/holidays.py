"""The public holidays of the user's countries (Italy and the US), by the phone's region.

Italy: the national holidays (Capodanno, Epifania, Pasquetta, Liberazione, Lavoro, Repubblica, Ferragosto,
Ognissanti, Immacolata, Natale, Santo Stefano). The US: the federal holidays (on their own day; the observed
Friday or Monday is not one). Easter by the Gregorian computus. Pure, no I/O."""

from datetime import date, timedelta

Region = str


def easter(year: int) -> date:
    """Easter Sunday (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th weekday (0 = Monday) of the month; n = -1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def italy(year: int) -> dict[date, str]:
    return {
        date(year, 1, 1): "Capodanno",
        date(year, 1, 6): "Epifania",
        easter(year): "Pasqua",
        easter(year) + timedelta(days=1): "Pasquetta",
        date(year, 4, 25): "Festa della Liberazione",
        date(year, 5, 1): "Festa del Lavoro",
        date(year, 6, 2): "Festa della Repubblica",
        date(year, 8, 15): "Ferragosto",
        date(year, 11, 1): "Ognissanti",
        date(year, 12, 8): "Immacolata",
        date(year, 12, 25): "Natale",
        date(year, 12, 26): "Santo Stefano",
    }


def united_states(year: int) -> dict[date, str]:
    return {
        date(year, 1, 1): "New Year's Day",
        _nth(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth(year, 2, 0, 3): "Presidents' Day",
        _nth(year, 5, 0, -1): "Memorial Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        _nth(year, 9, 0, 1): "Labor Day",
        _nth(year, 10, 0, 2): "Columbus Day",
        date(year, 11, 11): "Veterans Day",
        _nth(year, 11, 3, 4): "Thanksgiving",
        date(year, 12, 25): "Christmas Day",
    }


CALENDARS = {"IT": italy, "US": united_states}


def holiday(day: date, region: Region) -> str | None:
    """The holiday's name on that day in that country; None on a working day or a region Pisa has no calendar
    for."""
    calendar = CALENDARS.get(region.upper())
    return calendar(day.year).get(day) if calendar is not None else None
