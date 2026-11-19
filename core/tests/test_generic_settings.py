"""Pisa for anyone: the time zone is required, the language and country are settings, nothing defaults to one
user's home. No database."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pisa.domain import locale
from pisa.services.owner import provide
from pisa.settings import Settings


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type, call-arg]


def test_the_time_zone_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PISA_TIMEZONE", raising=False)
    with pytest.raises(ValidationError) as caught:
        _settings()
    assert [e["loc"] for e in caught.value.errors()] == [("timezone",)]
    assert _settings(timezone="America/New_York").timezone == "America/New_York"


def test_a_time_zone_pisa_does_not_know_is_refused() -> None:
    with pytest.raises(ValidationError, match="PISA_TIMEZONE 'Mars/Olympus' is not a time zone"):
        _settings(timezone="Mars/Olympus")


@pytest.mark.parametrize(("given", "kept"), [("us", "US"), (" it ", "IT"), ("", None)])
def test_the_country_is_an_iso_code_or_unset(given: str, kept: str | None) -> None:
    assert _settings(timezone="UTC", country=given).country == kept


@pytest.mark.parametrize("given", ["USA", "I1", "Italia"])
def test_a_country_that_is_not_an_iso_code_is_refused(given: str) -> None:
    with pytest.raises(ValidationError, match="ISO 3166-1 alpha-2"):
        _settings(timezone="UTC", country=given)


def test_the_language_is_english_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PISA_LANGUAGE", raising=False)
    monkeypatch.delenv("PISA_COUNTRY", raising=False)
    plain = _settings(timezone="UTC")
    assert (plain.language, plain.country) == ("en", None)
    assert _settings(timezone="UTC", language="it").language == "it"
    with pytest.raises(ValidationError):
        _settings(timezone="UTC", language="fr")


def test_the_cli_names_a_missing_or_wrong_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pisa import cli
    from pisa.settings import get_settings

    monkeypatch.delenv("PISA_TIMEZONE", raising=False)
    monkeypatch.setenv("PISA_COUNTRY", "Italia")
    monkeypatch.chdir("/")  # no .env here
    get_settings.cache_clear()
    try:
        assert cli.main(["devices", "list"]) == 2
    finally:
        get_settings.cache_clear()
    err = capsys.readouterr().err
    assert "PISA_TIMEZONE is required" in err
    assert "PISA_COUNTRY: PISA_COUNTRY 'Italia' is not an ISO 3166-1 alpha-2 code" in err


def test_pisa_s_own_defaults_are_english_with_no_country(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PISA_LANGUAGE", raising=False)
    monkeypatch.delenv("PISA_COUNTRY", raising=False)
    owner = provide(_settings(timezone="UTC"), lambda: datetime.now(UTC))
    assert (owner.language, owner.tag, owner.country) == ("en", "en-US", None)
    assert not owner.in_country("IT") and owner.home == "UTC"
    assert owner.region() is None and owner.hour_cycle() == "h12"


def test_the_suite_s_user_is_italian() -> None:
    owner = provide(_settings(timezone="Europe/Rome"), lambda: datetime.now(UTC))  # the suite's environment
    assert (owner.language, owner.tag, owner.country) == ("it", "it-IT", "IT")
    assert owner.in_country("it") and owner.home == "Europe/Rome"
    assert owner.region() == "IT" and owner.hour_cycle() == "h23"


def test_full_text_search_follows_the_language() -> None:
    assert locale.ts_config("en-US") == "english" and locale.ts_config("it") == "italian"


def test_without_a_location_a_place_is_matched_anywhere_never_near_a_built_in_city() -> None:
    from pisa.ports.places import ProviderPlace
    from pisa.services.places import _distinct_near

    far = ProviderPlace(provider="apple", provider_id="1", name="Corner Cafe", lat=40.7, lon=-74.0)
    assert _distinct_near([far], None) == [far]
    assert _distinct_near([far], (51.5, -0.12)) == []  # 50 km is the radius once Pisa knows where the user is


def test_env_example_documents_every_setting_with_no_real_value() -> None:
    import re

    from tests.conftest import REPO_ROOT

    text = (REPO_ROOT / ".env.example").read_text()
    names = set(re.findall(r"^(PISA_[A-Z0-9_]+)=", text, re.MULTILINE))
    wanted = {f"PISA_{name.upper()}" for name in Settings.model_fields}
    wanted |= {"PISA_DRAWING_BOX"}  # read from the environment
    assert wanted <= names, sorted(wanted - names)
    values = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", text, re.MULTILINE))
    for secret in ("PISA_SECRETS_KEY", "POSTGRES_PASSWORD", "PISA_APP_PASSWORD", "PISA_MIGRATE_PASSWORD"):
        assert values[secret] == "", secret  # never a value of anyone's
    assert values["PISA_TIMEZONE"] == ""  # required, and nobody's by default
