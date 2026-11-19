"""The user's context for a service a test builds by hand: the suite's Italian user at home in `tz` (a test
about another language or country passes its own)."""

from collections.abc import Callable
from datetime import UTC, datetime

from pisa.domain.locale import Language
from pisa.domain.owner import Owner
from pisa.services.settings import read


def owner_at(
    tz: str = "Europe/Rome",
    *,
    language: Language = "it",
    country: str | None = "IT",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Owner:
    return Owner(str(tz), language=language, country=country, read=read, clock=clock)
