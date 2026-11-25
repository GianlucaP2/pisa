"""The residency counter. Pisa counts days by country, never advises ("è un conteggio, non una
consulenza"). The phone's region at its last report of each day is noted here (reports it); trips and the
calendar fill the days without one; their corrections win. Logs carry counts only, never where they were."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import countries
from pisa.domain import residency as rules
from pisa.domain import zone as zone_rules
from pisa.domain.errors import PisaError
from pisa.domain.lexicon.advice import NOT_ADVICE
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.trips import TripsService
from pisa.services.zone import Report, primary_phone

_log = get_logger("pisa.residency")
TRIP_REACH = timedelta(days=180)  # a trip's arrival says where they are for at most this long
MONTH_KEY = "residency_line_month"


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Fix the request and try again")


class ResidencyService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        trips: TripsService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._trips = trips
        self._owner = owner
        self._clock = clock

    async def note(
        self,
        before: zone_rules.PhoneContext | None,
        new: zone_rules.PhoneContext,
        now: datetime,
        report: Report,
    ) -> None:
        """A report of the phone: every country it said that day is kept (any part of a day is a
        day), and the day's last. Only the primary phone, only a report that carried a region (a hello
        without one says nothing about the country; the last known is no fresh evidence)."""
        del before
        region = countries.country_of_region(report.region)
        if region is None:
            return
        day = now.astimezone(ZoneInfo(new.tz)).date()
        async with self._pool.connection() as conn:
            if report.device_id is None or report.device_id != await primary_phone(conn):
                return
            await conn.execute(
                "INSERT INTO residency_days (day, phone_regions, phone_region, phone_at) VALUES (%s, "
                "%s, %s, %s) "
                "ON CONFLICT (day) DO UPDATE SET phone_regions = CASE WHEN EXCLUDED.phone_region = "
                "ANY(residency_days.phone_regions) THEN residency_days.phone_regions ELSE "
                "residency_days.phone_regions || EXCLUDED.phone_region END, "
                "phone_region = CASE WHEN residency_days.phone_at IS NULL OR residency_days.phone_at <= "
                "EXCLUDED.phone_at THEN EXCLUDED.phone_region ELSE residency_days.phone_region END, "
                "phone_at = greatest(residency_days.phone_at, EXCLUDED.phone_at)",
                (day, [region], region, now),
            )

    async def correct(self, day: date, countries_said: list[str]) -> dict[str, Any]:
        """Their correction for a day (it wins over everything Pisa found); a travel day may name two
        countries."""
        now = self._clock()
        if day > now.astimezone(self._owner.zone).date():
            raise _invalid("A day in the future can't be corrected yet.")
        said = list(dict.fromkeys(countries_said))
        if not said or any(c != "other" and not countries.is_code(c) for c in said):
            raise _invalid("countries must be ISO 3166-1 alpha-2 codes (IT, DE, ...) or other.")
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO residency_days (day, corrected, corrected_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (day) DO UPDATE SET corrected = EXCLUDED.corrected, corrected_at = "
                "EXCLUDED.corrected_at",
                (day, said, now),
            )
        _log.info("residency_corrected", countries=len(said))
        return {"day": day.isoformat(), "country": said[0], "countries": said, "source": "his_words"}

    async def _evidence(self, conn: AsyncConnection, year: int, today: date) -> list[rules.Evidence]:
        first = date(year, 1, 1)
        last = min(date(year, 12, 31), today)
        if last < first:
            return []
        tz = self._owner.zone
        rows = await (
            await conn.execute(
                "SELECT day, phone_regions, corrected FROM residency_days WHERE day BETWEEN %s AND %s",
                (first, last),
            )
        ).fetchall()
        stored = {d: (phone, corrected) for d, phone, corrected in rows}
        end_of_last = datetime.combine(last + timedelta(days=1), time.min, tz)
        positions = await self._trips.positions(conn, end_of_last)
        # any part of a day is a day: a crossing's departure day counts where they left,
        # its arrival day for where they landed, besides the day's own country
        touched: dict[date, set[str]] = {}
        for day_of, country in await self._trips.crossings(conn, end_of_last):
            touched.setdefault(day_of, set()).add(rules.normal(country))
        events = await (
            await conn.execute(
                "SELECT starts_at, ends_at, coalesce(location, '') || ' ' || title FROM calendar_events "
                # an event Pisa made is no evidence of where they were
                "WHERE status <> 'cancelled' AND NOT made_by_pisa AND ends_at >= %s AND starts_at < %s",
                (datetime.combine(first, time.min, tz), end_of_last),
            )
        ).fetchall()
        placed = [(s, e, w.country) for s, e, text in events if (w := countries.city(text)) is not None]
        out: list[rules.Evidence] = []
        day = first
        index = 0
        where: tuple[datetime, str] | None = None
        while day <= last:
            end = datetime.combine(day + timedelta(days=1), time.min, tz)
            while index < len(positions) and positions[index][0] < end:
                where = positions[index]
                index += 1
            phone, corrected = stored.get(day, (None, None))
            trip = where[1] if where is not None and end - where[0] <= TRIP_REACH else None
            calendar = next(
                (c for s, e, c in reversed(placed) if s < end and e >= end - timedelta(hours=12)), None
            )
            found: dict[rules.Source, list[str]] = {
                "his_words": list(corrected or ()), "phone_region": list(phone or ()),
                "trip": [trip] if trip else [], "calendar": [calendar] if calendar else [],
            }  # fmt: skip
            day_evidence = rules.pick_all(day, found)
            out += day_evidence
            if not corrected:  # their word for the day stands alone
                seen = {e.country for e in day_evidence}
                out += [rules.Evidence(day, c, "trip") for c in sorted(touched.get(day, set()) - seen)]
            day += timedelta(days=1)
        return out

    async def report(self, year: int | None = None) -> dict[str, Any]:
        """The year's days by country, each rule's count and note, and the evidence."""
        now = self._clock()
        today = now.astimezone(self._owner.zone).date()
        year = year or today.year
        if not 2000 <= year <= today.year:
            raise _invalid("year must be between 2000 and this year.")
        async with self._pool.connection() as conn:
            lang = (await self._owner.settings(conn))["language"]
            ruleset = rules.rules_of(await get_setting(conn, "residency_rules"), self._owner.country)
            years = []
            evidence: list[rules.Evidence] = []
            depth = max((len(r.get("weights") or [1]) for r in ruleset), default=1)
            for back in range(depth):
                found = await self._evidence(conn, year - back, today)
                span = (min(date(year - back, 12, 31), today) - date(year - back, 1, 1)).days + 1
                years.append(rules.tally(found, max(span, 0), ruleset))
                if back == 0:
                    evidence = found
        lang = lang if lang in NOT_ADVICE else "en-US"
        return {
            "year": year,
            "days": years[0],
            "by_rule": rules.by_rule(ruleset, years, lang, year),
            "evidence": [
                {"day": e.day.isoformat(), "country": e.country, "source": e.source} for e in evidence
            ],
            "note": NOT_ADVICE[lang],
        }

    async def residency_text(self, year: int | None, lang: str) -> str:
        data = await self.report(year)
        lang = lang if lang in NOT_ADVICE else "en-US"
        return rules.answer(data["year"], data["days"], data["by_rule"], lang)

    def month(self) -> str:
        return self._owner.local(self._clock()).strftime("%Y-%m")

    async def monthly_line(self, lang: str) -> str | None:
        """The briefing's line, once a month (never daily): the first briefing of a month that holds it marks
        it said (`said_month`: only once it is on its way). Unasked only for a user in Italy (PISA_COUNTRY):
        the default rule is Italy's; asked, the count is always there."""
        if not self._owner.in_country("IT"):
            return None
        async with self._pool.connection() as conn:
            if await get_setting(conn, MONTH_KEY) == self.month():
                return None
        data = await self.report()
        if not any(e["source"] != "calendar" for e in data["evidence"]):
            return None  # nothing but their calendar: no line (it would only guess)
        lang = lang if lang in NOT_ADVICE else "en-US"
        return rules.answer(data["year"], data["days"], data["by_rule"], lang)

    async def said_month(self) -> None:
        async with self._pool.connection() as conn:
            await set_setting(conn, MONTH_KEY, Jsonb(self.month()))


class BriefingLines:
    """Pisa's own lines for the briefing (services/briefing.py's `TwoCountryLines`): the month's
    residency
    count and the landings they made in their quiet hours. Each is marked said only when a briefing holding it
    went out, word for word (never paraphrased, never spent before)."""

    def __init__(self, trips: TripsService, residency: ResidencyService) -> None:
        self._trips, self._residency = trips, residency

    async def lines(self, lang: str) -> list[str]:
        out = [line for _, line in await self._trips.welcome_lines(lang)]
        if (count := await self._residency.monthly_line(lang)) is not None:
            out.append(count)
        return out

    async def said_in(self, text: str) -> None:
        lang = "it-IT" if "Benvenuto" in text or "conteggio" in text else "en-US"
        for trip_id, line in await self._trips.welcome_lines(lang):
            if line in text:
                await self._trips.welcomed(trip_id)
        count = await self._residency.monthly_line(lang)
        if count is not None and count in text and rules.closes(text, count):
            await self._residency.said_month()


class TwoCountriesData:
    """What the brain's two-country tools read (`TripsData`): the trips and the residency counter."""

    def __init__(self, trips: TripsService, residency: ResidencyService) -> None:
        self._trips, self._residency = trips, residency

    async def trip_text(self, lang: str) -> str:
        return await self._trips.trip_text(lang)

    async def residency_text(self, year: int | None, lang: str) -> str:
        return await self._residency.residency_text(year, lang)

    async def while_away(self, city: str) -> tuple[str, list[str]]:
        return await self._trips.while_away(city)

    async def checklist_sheet(self, lang: str) -> dict[str, Any] | None:
        return await self._trips.checklist_sheet(lang)
