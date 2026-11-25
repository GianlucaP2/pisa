"""The weather: Open-Meteo's forecast as Pisa says it. Pure rules, no I/O.

- A **summary** is the WMO weather code in the user's language ("Pioggia debole" / "Light rain").
- Forecasts are kept 30 minutes per **cell** of 0.01° (about 1 km).
- **Rain** for the plan: an hour whose precipitation probability is 60% or more.
- **When** ("oggi", "domani", "venerdì", "2026-10-24") is a date from today up to 7 days ahead.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pisa.domain.errors import PisaError
from pisa.domain.lexicon.weekdays import WEEKDAY_NUMBERS
from pisa.domain.text import fold_words

CACHE_MIN = 30
CELL_DEG = 0.01
DAYS_AHEAD = 7
RAIN_PROB = 60  # % an hour must reach for the rain rule (walking legs) and the umbrella
RAIN_LEG_MIN = 15  # a walking leg this long or longer goes by transit in the rain


@dataclass(frozen=True)
class Hour:
    time: datetime  # aware, the start of the hour
    temp_c: float
    precip_prob: int
    code: int


@dataclass(frozen=True)
class Day:
    date: date
    min_c: float
    max_c: float
    precip_mm: float
    precip_prob: int
    wind_kmh: float
    code: int


@dataclass(frozen=True)
class Forecast:
    days: list[Day]
    hours: list[Hour]


# WMO weather interpretation codes, as Open-Meteo documents them.
_WMO: dict[int, tuple[str, str]] = {
    0: ("Sereno", "Clear sky"),
    1: ("Prevalentemente sereno", "Mainly clear"),
    2: ("Parzialmente nuvoloso", "Partly cloudy"),
    3: ("Coperto", "Overcast"),
    45: ("Nebbia", "Fog"),
    48: ("Nebbia con brina", "Rime fog"),
    51: ("Pioviggine debole", "Light drizzle"),
    53: ("Pioviggine", "Drizzle"),
    55: ("Pioviggine intensa", "Dense drizzle"),
    56: ("Pioviggine gelata debole", "Light freezing drizzle"),
    57: ("Pioviggine gelata", "Freezing drizzle"),
    61: ("Pioggia debole", "Light rain"),
    63: ("Pioggia", "Rain"),
    65: ("Pioggia forte", "Heavy rain"),
    66: ("Pioggia gelata debole", "Light freezing rain"),
    67: ("Pioggia gelata", "Freezing rain"),
    71: ("Neve debole", "Light snow"),
    73: ("Neve", "Snow"),
    75: ("Neve forte", "Heavy snow"),
    77: ("Nevischio", "Snow grains"),
    80: ("Rovesci deboli", "Light showers"),
    81: ("Rovesci", "Showers"),
    82: ("Rovesci forti", "Heavy showers"),
    85: ("Rovesci di neve deboli", "Light snow showers"),
    86: ("Rovesci di neve", "Snow showers"),
    95: ("Temporale", "Thunderstorm"),
    96: ("Temporale con grandine debole", "Thunderstorm with light hail"),
    99: ("Temporale con grandine", "Thunderstorm with hail"),
}


def summary(code: int, lang: str) -> str:
    it, en = _WMO.get(code, ("Tempo variabile", "Changeable"))
    return it if lang == "it-IT" else en


def cell(lat: float, lon: float) -> str:
    """The cache key: the position rounded to 0.01° (about 1 km)."""
    return f"{round(lat / CELL_DEG) * CELL_DEG:.2f},{round(lon / CELL_DEG) * CELL_DEG:.2f}"


def rainy_hours(forecast: Forecast) -> list[datetime]:
    return [h.time for h in forecast.hours if h.precip_prob >= RAIN_PROB]


def _deg(value: float) -> str:
    return f"{round(value)}"


def briefing_line(day: Day, lang: str) -> str:
    """ "Oggi: pioggia debole, 9\u201313 °C; prendi l'ombrello." One sentence of a spoken briefing. The range
    takes an en dash, which a voice reads as "to" (a hyphen can be read as "minus")."""
    words = summary(day.code, lang)
    words = words[0].lower() + words[1:]
    span = f"{_deg(day.min_c)}\u2013{_deg(day.max_c)} °C"
    rain = day.precip_prob >= RAIN_PROB
    if lang == "it-IT":
        return f"Oggi: {words}, {span}" + ("; prendi l'ombrello." if rain else ".")
    return f"Today: {words}, {span}" + ("; take an umbrella." if rain else ".")


def _plain(text: str) -> str:
    return fold_words(text, r"[a-z0-9-]+")


def parse_when(text: str | None, today: date) -> date:
    """today/oggi, tomorrow/domani, a weekday (the next one, today included), or YYYY-MM-DD; up to 7 days."""
    words = _plain(text or "today")
    if words in ("", "today", "oggi", "now", "adesso", "ora"):
        day = today
    elif words in ("tomorrow", "domani"):
        day = today + timedelta(days=1)
    elif words in ("dopodomani", "day after tomorrow"):
        day = today + timedelta(days=2)
    elif any(w in WEEKDAY_NUMBERS for w in words.split()):
        weekday = next(WEEKDAY_NUMBERS[w] for w in words.split() if w in WEEKDAY_NUMBERS)
        day = today + timedelta(days=(weekday - today.weekday()) % 7)
    else:
        try:
            day = date.fromisoformat(words[:10])
        except ValueError:
            raise PisaError(
                "validation_failed",
                f"{text!r} is not a day: say today, tomorrow, a weekday or YYYY-MM-DD.",
                "Say the day differently",
            ) from None
    if not today <= day <= today + timedelta(days=DAYS_AHEAD):
        raise PisaError(
            "validation_failed",
            f"The forecast covers today and the next {DAYS_AHEAD} days, not {day.isoformat()}.",
            "Ask about a day within a week",
        )
    return day
