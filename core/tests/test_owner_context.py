"""The user's context is a value passed in: one Owner, its zone the phone's, its language theirs."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from pisa.domain.owner import Owner
from pisa.domain.zone import PhoneContext

AT = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)  # a Sunday: 00:30 Monday in Rome, 18:30 in New York


async def _stored(_conn: Any, language: str) -> dict[str, Any]:
    return {"language": language, "briefing": {}}


async def _italian(_conn: Any, _language: str) -> dict[str, Any]:
    return {"language": "it-IT"}


def test_the_zone_is_home_until_the_phone_says_otherwise() -> None:
    owner = Owner("Europe/Rome", language="en", country="it", read=_stored, clock=lambda: AT)
    assert owner.tz == "Europe/Rome" and not owner.away and owner.country == "IT"
    assert owner.today() == date(2026, 11, 9) and owner.day(AT) == date(2026, 11, 9)
    owner.place = PhoneContext("America/New_York", "US", "h12")
    assert owner.tz == "America/New_York" and owner.away
    assert owner.local(AT).hour == 18 and owner.today() == date(2026, 11, 8)
    assert owner.hour_cycle() == "h12" and owner.region() == "US"


def test_two_owners_never_share_a_zone() -> None:
    rome = Owner("Europe/Rome", language="it", country="IT", read=_stored)
    other = Owner("Europe/Rome", language="it", country="IT", read=_stored)
    rome.place = PhoneContext("Asia/Tokyo")
    assert rome.tz == "Asia/Tokyo" and other.tz == "Europe/Rome"


def test_one_read_gives_language_settings_and_day() -> None:
    owner = Owner("Europe/Rome", language="en", country=None, read=_stored, clock=lambda: AT)
    context = asyncio.run(owner.context(None))
    assert context.language == "en-US" and context.settings["language"] == "en-US"
    assert context.today() == date(2026, 11, 9) and context.tz == "Europe/Rome"
    assert context.hour_cycle == "h12" and context.region is None and not context.in_country("IT")
    chosen = Owner("Europe/Rome", language="en", country="IT", read=_italian, clock=lambda: AT)
    assert asyncio.run(chosen.lang(None)) == "it-IT"  # their Settings choice wins over the start language
