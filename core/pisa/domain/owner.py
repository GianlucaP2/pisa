"""The user's context, passed in: their language, country and zone, and the time where they are.

`Owner` is the one holder of it in a process (each Runtime builds its own from PISA_LANGUAGE, PISA_COUNTRY and
PISA_TIMEZONE; `services/zone.py` moves it when the primary phone reports where it is). Every service that
needs the user's day, zone or language asks the Owner it was given, so a zone change is seen by all of them at
once, and two runtimes in one process (the worker's hosts, the replay's passes) never share one.

`OwnerContext` is what one request or one step reads, once: the Settings language, the zone, the settings and
the instant it was read at.

No I/O here: the settings are read through the reader the runtime gives (`services/settings.py load`)."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from pisa.domain import locale
from pisa.domain.locale import Language, Tag
from pisa.domain.zone import HourCycle, PhoneContext

# (a connection or a pool, Pisa's start language) -> the user's settings over the defaults
SettingsReader = Callable[[Any, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class OwnerContext:
    """The user's context as one request or step read it."""

    language: Tag  # their Settings language (Pisa's start language until they pick one)
    country: str | None  # PISA_COUNTRY (ISO 3166-1 alpha-2, upper case), None when not set
    zone: ZoneInfo  # their zone now: the phone's, else PISA_TIMEZONE
    home: str  # PISA_TIMEZONE: where they live, and their people with them
    region: str | None  # the phone's region, else their country
    hour_cycle: HourCycle  # how they read times
    settings: Mapping[str, Any]  # their settings over the defaults
    at: datetime  # the instant it was read at

    @property
    def tz(self) -> str:
        return self.zone.key

    @property
    def away(self) -> bool:
        """Whether the phone says they are in another zone than home."""
        return self.tz != self.home

    def now(self) -> datetime:
        """The instant it was read, on their clock."""
        return self.at.astimezone(self.zone)

    def today(self) -> date:
        return self.now().date()

    def in_country(self, code: str) -> bool:
        return self.country == code.upper()


class Owner:
    """The user's context in this process. `place` is what the primary phone said last (None before any
    report: PISA_TIMEZONE)."""

    def __init__(
        self,
        home: str,
        *,
        language: Language,
        country: str | None,
        read: SettingsReader,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.home = home
        self.language: Language = language
        self.country = country.upper() if country else None
        self.place: PhoneContext | None = None
        self._read = read
        self._clock = clock

    # --- the zone ----------------------------------------------------------------------------------------

    @property
    def tz(self) -> str:
        """Their zone now: the phone's, else PISA_TIMEZONE."""
        return self.place.tz if self.place is not None else self.home

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def away(self) -> bool:
        return self.place is not None and self.place.tz != self.home

    def local(self, at: datetime) -> datetime:
        """`at` on their clock, in their zone now."""
        return at.astimezone(self.zone)

    def day(self, at: datetime) -> date:
        """Their day at `at`, in their zone now."""
        return self.local(at).date()

    def now(self) -> datetime:
        return self.local(self._clock())

    def today(self) -> date:
        return self.now().date()

    def hour_cycle(self, lang: str | None = None) -> HourCycle:
        """The phone's hour cycle, else the language's habit (12-hour clock in English, 24 in Italian)."""
        if self.place is not None and self.place.hour_cycle is not None:
            return self.place.hour_cycle
        return "h12" if (lang or self.tag) == "en-US" else "h23"

    def region(self, default: str | None = None) -> str | None:
        """The phone's region (ISO 3166-1), else `default`, else their country; None when nothing says it."""
        if self.place is not None and self.place.region:
            return self.place.region
        return default if default is not None else self.country

    # --- the language and the country --------------------------------------------------------------------

    @property
    def tag(self) -> Tag:
        """Pisa's start language (PISA_LANGUAGE) as a BCP-47 tag: what a reply defaults to with no other."""
        return cast(Tag, locale.TAGS[self.language])

    def in_country(self, code: str) -> bool:
        """Whether their country is `code`: the gate for country-specific features. Off when none is set."""
        return self.country == code.upper()

    # --- one read ----------------------------------------------------------------------------------------

    async def settings(self, conn: Any) -> dict[str, Any]:
        """Their settings over the defaults (Settings' own read), on `conn` (a connection, or a pool)."""
        return await self._read(conn, self.tag)

    async def context(self, conn: Any, at: datetime | None = None) -> OwnerContext:
        """Their context now (or at `at`): one settings read, the zone as the phone last said."""
        settings = await self.settings(conn)
        return OwnerContext(
            language=cast(Tag, locale.tag_of(settings.get("language") or self.tag)),
            country=self.country,
            zone=self.zone,
            home=self.home,
            region=self.region(),
            hour_cycle=self.hour_cycle(),
            settings=settings,
            at=at if at is not None else self._clock(),
        )

    async def lang(self, conn: Any) -> Tag:
        """Their Settings language now."""
        return (await self.context(conn)).language
