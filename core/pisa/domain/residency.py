"""The residency counter. **Pisa counts, Pisa never advises**: every line carries
"è un conteggio, non una consulenza".

A day counts for the country where the phone was at the end of that day (the region, the day's last report);
else where their trips put them; else their calendar; else it is unknown. Their corrections win. Days are
counted for any country (ISO 3166-1 alpha-2). The rules are data (`app_settings.residency_rules`): a rule
counts days of presence, or weights this year's and the years before's (the US substantial presence test).
Without that setting only a user in Italy (PISA_COUNTRY=IT) has a rule, Italy's 183 days; the US tests
(`KNOWN_RULES`) apply only when the setting names them (`{"rule": "us_substantial_presence"}`); anywhere else
Pisa counts the days and checks no rule until they set their own (a tax rule is never assumed). No I/O."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pisa.domain import countries
from pisa.domain.lexicon.advice import NOT_ADVICE

Source = Literal["phone_region", "trip", "calendar", "his_words"]
# The order a day's evidence is believed in (their words first)
ORDER: tuple[Source, ...] = ("his_words", "phone_region", "trip", "calendar")

WARN_AT = (150, 170)

# The rules: any part of a day counts as a whole day in both countries (a day they fly Rome → New York counts
# for Italy and for the US); Italy's "most of the tax year" is 183 days, 184 in a leap year; the US
# substantial presence test weighs three years (≥ 31 this year); 183 US days in the current year end the
# closer-connection exception.
KNOWN_RULES: tuple[dict[str, Any], ...] = (
    {"rule": "it_presence_183", "country": "IT", "kind": "days", "threshold": 183, "leap_threshold": 184,
     "warn_at": list(WARN_AT), "label": {"it-IT": "Italia", "en-US": "Italy"}},
    {"rule": "us_substantial_presence", "country": "US", "kind": "weighted", "weights": [1, 1 / 3, 1 / 6],
     "threshold": 183, "min_current": 31, "warn_at": list(WARN_AT), "label": {"it-IT": "USA", "en-US": "US"}},
    {"rule": "us_closer_connection", "country": "US", "kind": "days", "threshold": 183,
     "warn_at": list(WARN_AT),
     "label": {"it-IT": "USA, giorni dell'anno (eccezione closer connection)",
               "en-US": "US, days this year (closer connection exception)"}},
)  # fmt: skip


_KNOWN = {r["rule"]: r for r in KNOWN_RULES}
# Without a setting: Italy's rule, for a user in Italy only
DEFAULT_RULES: tuple[dict[str, Any], ...] = (_KNOWN["it_presence_183"],)


def default_rules(country: str | None) -> list[dict[str, Any]]:
    """The rules without a setting: Italy's 183 days for a user in Italy (PISA_COUNTRY=IT), else none."""
    return [dict(r) for r in DEFAULT_RULES] if (country or "").upper() == "IT" else []


def rules_of(setting: Any, country: str | None) -> list[dict[str, Any]]:
    """The rules Pisa counts by: the user's setting when every rule in it is well formed, else the
    defaults. A rule of the setting is written out, or named (`{"rule": "us_closer_connection"}`: one of
    `KNOWN_RULES`)."""
    if not isinstance(setting, list) or not setting:
        return default_rules(country)
    out = []
    for raw in setting:
        if isinstance(raw, dict) and set(raw) == {"rule"} and raw["rule"] in _KNOWN:
            raw = _KNOWN[raw["rule"]]
        if not isinstance(raw, dict) or not _valid(raw):
            return default_rules(country)
        out.append(dict(raw))
    return out


def _valid(rule: Mapping[str, Any]) -> bool:
    if not isinstance(rule.get("rule"), str) or not isinstance(rule.get("country"), str):
        return False
    if not isinstance(rule.get("threshold"), int | float) or rule["threshold"] <= 0:
        return False
    if rule.get("kind") == "days":
        return True
    weights = rule.get("weights")
    return (
        rule.get("kind") == "weighted"
        and isinstance(weights, list)
        and 1 <= len(weights) <= 5
        and all(isinstance(w, int | float) and 0 <= w <= 1 for w in weights)
    )


@dataclass(frozen=True)
class Evidence:
    day: date
    country: str
    source: Source


def pick(day: date, found: Mapping[Source, str | None]) -> Evidence | None:
    """The day's country by the order of belief; None when nothing says."""
    for source in ORDER:
        if (country := found.get(source)) is not None:
            return Evidence(day, normal(country), source)
    return None


def pick_all(day: date, found: Mapping[Source, list[str]]) -> list[Evidence]:
    """Every country of the first source that says something (a day may be in two)."""
    for source in ORDER:
        if said := found.get(source):
            return [Evidence(day, c, source) for c in dict.fromkeys(normal(x) for x in said)]
    return []


def closes(text: str, line: str) -> bool:
    """The line is in the text with its closing words (a count is never said without them)."""
    return line in text and any(words in line for words in NOT_ADVICE.values())


def normal(country: str) -> str:
    """An ISO 3166-1 alpha-2 code, upper case; "other" for anything else."""
    code = country.strip().upper()
    return code if countries.is_code(code) else "other"


def tally(
    evidence: list[Evidence], days_in_range: int, rules: list[dict[str, Any]] | None = None
) -> dict[str, int]:
    """Days per country, every rule's country listed (0 without a day); a travel day counts for each country
    they were in (any part of a day is a day). "unknown": the days nothing says."""
    out = dict.fromkeys((str(r["country"]) for r in rules or ()), 0)
    for _, country in {(e.day, normal(e.country)) for e in evidence}:
        out[country] = out.get(country, 0) + 1
    out["unknown"] = max(0, days_in_range - len({e.day for e in evidence}))
    return out


def leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def threshold(rule: Mapping[str, Any], year: int) -> float:
    return float(rule.get("leap_threshold") or rule["threshold"]) if leap(year) else float(rule["threshold"])


def count(rule: Mapping[str, Any], years: list[dict[str, int]]) -> float:
    """A rule's count: `years[0]` is this year's tally, then the years before."""
    country = rule["country"]
    if rule["kind"] == "days":
        return float(years[0].get(country, 0))
    total = sum(
        w * (years[n].get(country, 0) if n < len(years) else 0) for n, w in enumerate(rule["weights"])
    )
    return round(total, 1)


def met(rule: Mapping[str, Any], value: float, current: int, year: int = 2001) -> bool:
    return value >= threshold(rule, year) and current >= int(rule.get("min_current", 0))


def _num(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def note(
    rule: Mapping[str, Any], value: float, current: int, lang: str, year: int = 2001, unknown_before: int = 0
) -> str:
    """One rule's line, always closed by "è un conteggio, non una consulenza"; a warning from 150 and 170. A
    weighted rule says how many days of the years before it has no data for."""
    it = lang == "it-IT"
    label = (rule.get("label") or {}).get(lang) or rule["country"]
    limit = threshold(rule, year)  # 184 in a leap year
    head = f"{label}: {_num(value)} su {_num(limit)}" if it else f"{label}: {_num(value)} of {_num(limit)}"
    if rule["kind"] == "weighted":
        head += (f" ({current} quest'anno + la quota degli anni prima)" if it
                 else f" ({current} this year + the share of the years before)")  # fmt: skip
        if unknown_before:
            head += (f"; {unknown_before} giorni degli anni prima senza dati" if it
                     else f"; {unknown_before} days of the years before unknown")  # fmt: skip
    if met(rule, value, current, year):
        head += ": soglia raggiunta" if it else ": threshold reached"
    else:
        crossed = [w for w in rule.get("warn_at") or [] if value >= w]
        if crossed:
            head += f": attenzione, oltre {max(crossed)}" if it else f": careful, past {max(crossed)}"
    return f"{head}. {NOT_ADVICE.get(lang, NOT_ADVICE['en-US'])}"


def by_rule(
    rules: list[dict[str, Any]], years: list[dict[str, int]], lang: str, year: int = 2001
) -> list[dict[str, Any]]:
    out = []
    for rule in rules:
        value = count(rule, years)
        current = years[0].get(rule["country"], 0)
        weights = rule.get("weights") or []
        unknown_before = sum(
            years[n].get("unknown", 0) for n in range(1, min(len(years), len(weights))) if weights[n]
        )
        out.append({"rule": rule["rule"], "country": rule["country"], "count": value,
                    "threshold": threshold(rule, year),
                    "note": note(rule, value, current, lang, year, unknown_before)})  # fmt: skip
    return out


_IN = {"US": "negli USA", "GB": "nel Regno Unito", "NL": "nei Paesi Bassi"}  # Italian, where "in" won't do


def days_line(year: int, days: Mapping[str, int], named: list[str], lang: str) -> str:
    """ "Nel 2026: 160 giorni in Italia, 40 negli USA, 3 altrove, 2 senza dati": the rules' countries
    (`named`), then every other country they were in, most days first; a place with no country as
    elsewhere."""
    it = lang == "it-IT"
    visited = sorted(
        (c for c, n in days.items() if n and countries.is_code(c) and c not in named),
        key=lambda c: (-days[c], c),
    )
    named = [*named, *visited]
    parts = [
        f"{days.get(c, 0)} " + ((_IN.get(c) if it else None) or f"in {countries.name(c, lang)}")
        for c in named
    ]
    if elsewhere := sum(n for c, n in days.items() if c not in named and c != "unknown"):
        parts.append(f"{elsewhere} " + ("altrove" if it else "elsewhere"))
    if days.get("unknown"):
        parts.append(f"{days['unknown']} " + ("senza dati" if it else "unknown"))
    if not parts:
        parts = ["0 altrove" if it else "0 elsewhere"]
    unit = "giorni" if it else "days"
    first, sep, rest = parts[0].partition(" ")
    return (f"Nel {year}: " if it else f"In {year}: ") + ", ".join([f"{first} {unit}{sep}{rest}", *parts[1:]])


def answer(year: int, days: dict[str, int], computed: list[dict[str, Any]], lang: str) -> str:
    """What Pisa says when asked ("quanti giorni sono stato in Italia quest'anno?") and once a month."""
    head = days_line(year, days, list(dict.fromkeys(r["country"] for r in computed)), lang)
    lines = [head + "."]
    lines += [r["note"].removesuffix(" " + NOT_ADVICE.get(lang, NOT_ADVICE["en-US"])) for r in computed]
    return " ".join(lines) + " " + NOT_ADVICE.get(lang, NOT_ADVICE["en-US"])
