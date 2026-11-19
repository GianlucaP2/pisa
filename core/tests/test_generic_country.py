"""Country-specific code is on only for a user in that country (PISA_COUNTRY): Italy's taxi numbers, phone
numbers without a prefix, airports, public feeds, connectors (PEC, pagoPA, the tax calendar, the Agenzia
delle Entrate), AIRE, the bill watchdog, the residency line and the desk's Italy/US search. No database."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from pisa.domain import connectors as connector_rules
from pisa.domain import handoffs, locale, public_sources
from pisa.domain.owner import Owner
from tests.owner import owner_at

IT = ("it", "IT")
NONE = ("en", None)
US = ("en", "US")


def _owner(value: tuple[str, str | None]) -> Owner:
    """The user in that country, for a service the test builds."""
    return owner_at("UTC", language=cast(locale.Language, value[0]), country=value[1])


@pytest.mark.parametrize(
    ("where", "radio"), [(IT, ("Radio Taxi 3570", "+39063570")), (NONE, None), (US, None)]
)
def test_italy_s_radio_taxis_only_in_italy(where: tuple[str, str | None], radio: object) -> None:
    assert handoffs.dispatcher("Via Example 1, 00100 Roma", _owner(where).country) == radio


def test_a_number_without_its_prefix_takes_the_user_s_country() -> None:
    # the region the tools pass: the phone's, else the user's country (the Owner's `region()`)
    assert handoffs.normalize_phone("333 123 4567", _owner(IT).region()) == "+393331234567"
    assert handoffs.normalize_phone("(212) 555-0142", _owner(US).region()) == "+12125550142"
    nowhere = _owner(NONE).region()
    assert handoffs.normalize_phone("333 123 4567", nowhere) is None  # no country: a prefix is needed
    assert handoffs.normalize_phone("+44 7700 900123", nowhere) == "+447700900123"  # with one, anywhere
    assert handoffs.normalize_phone("333 123 4567", "IT") == "+393331234567"  # the phone's region wins


def test_italian_public_sources_only_in_italy() -> None:
    italian = [k for k, s in public_sources.SOURCES.items() if s.country == "IT"]
    assert set(italian) == {"rome_static", "rome_trips", "rome_alerts", "dpc_bulletin", "lazio_pharmacies",
                            "rome_ztl"}  # fmt: skip
    assert all(public_sources.on(k, "IT") for k in public_sources.SOURCES)
    assert not any(public_sources.on(k, None) for k in italian)
    assert public_sources.on("nyc_alerts", None) and public_sources.on("nws_alerts:40.71,-74.01", None)


def _public(where: tuple[str, str | None]) -> Any:
    from pisa.services.public_data import PublicDataService

    clock = lambda: datetime(2026, 10, 20, 9, 0, tzinfo=UTC)  # noqa: E731
    return PublicDataService(cast(Any, None), cast(Any, None), owner=_owner(where), clock=clock)


@pytest.mark.parametrize("tool", ["ztl_status", "nearest_pharmacy"])
def test_rome_s_reads_say_they_are_off_outside_italy(tool: str) -> None:
    from pisa.services.public_data import OFF_HERE

    found = asyncio.run(_public(NONE).read(tool, {}))
    assert found["ok"] is False and found["summary"] == OFF_HERE


def test_rome_s_ztl_reads_in_italy() -> None:
    found = asyncio.run(_public(IT).read("ztl_status", {}))
    assert found["ok"] is True and found["summary"].startswith("Rome,")


def test_rome_s_feeds_and_italy_s_alerts_are_off_outside_italy() -> None:
    rome = (41.9, 12.5)
    svc = _public(IT)
    assert svc._city(rome) == "rome" and svc._country(rome) == "IT"
    svc = _public(NONE)
    assert svc._city(rome) is None and svc._country(rome) is None
    assert svc._city((40.75, -73.99)) == "nyc" and svc._country((40.75, -73.99)) == "US"


def test_italian_connectors_are_off_outside_italy() -> None:
    italian = {s.id for s in connector_rules.REGISTRY if s.country == "IT"}
    assert italian == {"admin_mail", "admin_pec", "admin_calendar", "ade_cassetto"}
    facts = connector_rules.Facts(
        enabled=True, has_secret=True, secret_expires_on=None, consent_expires_at=None, failures=0,
        google_status="connected",
    )  # fmt: skip
    now, today = datetime(2026, 10, 20, tzinfo=UTC), date(2026, 10, 20)
    pec = connector_rules.BY_ID["admin_pec"]
    assert (
        connector_rules.in_reach(pec, "IT") and connector_rules.state_of(pec, facts, now, today, "IT") == "on"
    )
    assert not connector_rules.in_reach(pec, "US")
    assert connector_rules.state_of(pec, facts, now, today, "US") == "off"
    assert connector_rules.tools_off({s.id: "off" for s in connector_rules.REGISTRY if s.id in italian}) >= (
        connector_rules.ADMIN_TOOLS
    )
    garmin = connector_rules.BY_ID["garmin"]
    assert connector_rules.state_of(garmin, facts, now, today, "US") == "on"  # no country: anywhere


def test_aire_is_never_asked_outside_italy() -> None:
    from pisa.services.vault import VaultService

    vault = VaultService(cast(Any, None), owner=_owner(US), clock=lambda: datetime(2026, 10, 20, tzinfo=UTC))
    assert asyncio.run(vault.ask_aire(cast(Any, None))) is False


def test_the_residency_line_is_unasked_only_in_italy() -> None:
    from pisa.services.residency import ResidencyService

    residency = ResidencyService(cast(Any, None), cast(Any, None), owner=_owner(NONE))
    assert asyncio.run(residency.monthly_line("en-US")) is None


def test_the_desk_reads_italy_only_for_a_user_in_italy() -> None:
    from pisa.services.desk import _serves

    italy, us = _owner(IT), _owner(US)
    assert _serves("it", italy) and _serves("us", italy) and _serves("eu", italy)
    assert not _serves("it", us) and _serves("us", us) and _serves("eu", us)


def test_italian_airports_are_routed_only_in_italy() -> None:
    from pisa.services.public_data import AIRPORTS, ITALIAN_AIRPORTS

    assert ITALIAN_AIRPORTS <= set(AIRPORTS) and "JFK" not in ITALIAN_AIRPORTS
