"""The language defaults follow PISA_LANGUAGE: settings, spoken fallbacks, number formatting. Italian stays
a supported language with its own strings. No database."""

import pytest

from pisa.domain import locale
from pisa.domain import settings as setting_rules
from tests.owner import owner_at


def test_an_english_pisa_s_settings_start_in_english() -> None:
    tag = owner_at(language="en", country=None).tag  # Pisa's start language, the Owner's
    assert setting_rules.defaults(tag)["language"] == "en-US"
    assert setting_rules.with_defaults(None, tag)["language"] == "en-US"
    assert (
        setting_rules.with_defaults({"language": "it-IT"}, tag)["language"] == "it-IT"
    )  # the user's choice wins


def test_an_italian_pisa_s_settings_start_in_italian() -> None:
    assert setting_rules.defaults(owner_at().tag)["language"] == "it-IT"


@pytest.mark.parametrize(
    ("language", "tag", "unlock"), [("en", "en-US", "Unlock"), ("it", "it-IT", "Sblocca")]
)
def test_the_fallbacks_speak_pisa_s_language(language: locale.Language, tag: str, unlock: str) -> None:
    from pisa.domain.locked import unlock_line
    from pisa.domain.quarantine import source_label
    from pisa.domain.stanotte import num

    pisa = owner_at(language=language, country=None).tag  # Pisa's start language, passed in
    assert pisa == tag
    assert unlock_line(pisa).startswith(unlock)
    assert num(38000.5, pisa) == ("38,000.5" if language == "en" else "38.000,5")
    label = source_label("email", "Studio Example", "xx-XX")  # a language with no table: English
    assert label.startswith("from the email of")
    # an explicit language always wins over the default
    assert unlock_line("it-IT").startswith("Sblocca") and unlock_line("en-US").startswith("Unlock")
    assert num(0.5, "it-IT") == "0,5" and num(0.5, "en-US") == "0.5"


def test_tags_and_tables() -> None:
    assert locale.short("it-IT") == "it" and locale.short("en-GB") == "en" and locale.short("fr") == "en"
    assert locale.tag_of("it") == "it-IT" and locale.tag_of("en-GB") == "en-US"
    assert locale.name("it-IT") == "Italian" and locale.name("en-US") == "English"
    table = {"en": "Good morning", "it": "Buongiorno"}
    assert locale.pick(table, "it-IT") == "Buongiorno" and locale.pick(table, "en-US") == "Good morning"
