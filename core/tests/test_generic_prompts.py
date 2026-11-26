"""The fixed prompts are written once, in English, for any user: their example phrases come from the table of
Pisa's language, the night's prompts write in that language, and the user is "they". No database."""

import re

import pytest

from pisa.brain.attend import instructions as attend_instructions
from pisa.brain.conversation import self_block, system_prompt
from pisa.domain import bible, locale, prompt_examples
from pisa.domain.whisper import Fresh, instructions, talker_rules
from tests.owner import owner_at

ITALIAN_ONLY = ("li sposto", "così arrivi in tempo", "Certamente!", "C'è altro?", "Un attimo", "Anzi,")


def test_both_tables_have_the_same_examples() -> None:
    assert set(prompt_examples.EXAMPLES["en"]) == set(prompt_examples.EXAMPLES["it"])
    assert all(v.strip() for table in prompt_examples.EXAMPLES.values() for v in table.values())


@pytest.mark.parametrize("lang", ["en", "it"])
def test_every_template_is_filled(lang: str) -> None:
    for text in (system_prompt(lang), talker_rules(lang), bible.rules(lang), attend_instructions(lang)):
        assert not re.search(r"\{[a-z_]+\}", text)  # no example left unnamed


def test_an_english_pisa_s_prompts_quote_english_examples() -> None:
    prompt, talker = system_prompt("en"), talker_rules("en")
    assert '"I\'ll move them", "I\'ll remind you"' in prompt and '"at half past 3", "Friday at 10"' in prompt
    assert '"One moment, checking."' in talker and '"Actually, it\'s closed today."' in talker
    for phrase in ITALIAN_ONLY:
        assert phrase not in prompt and phrase not in talker, phrase
    assert '"Three things: …"' in attend_instructions("en")


def test_an_italian_pisa_s_prompts_keep_their_italian_examples() -> None:
    prompt, talker = system_prompt("it"), talker_rules("it")
    assert '"li sposto", "te lo ricordo"' in prompt and '"alle 3 e mezza", "venerdì alle 10"' in prompt
    assert '"Un attimo, controllo."' in talker and '"Anzi, è chiuso oggi."' in talker
    assert '"Tre cose: …"' in attend_instructions("it")


def test_the_rules_are_the_same_in_both_languages() -> None:
    """Only the examples differ: every rule line is there in both, in the same order."""

    def skeleton(text: str) -> list[str]:
        return [
            re.sub(r'"[^"]*"|\([^)]*\)', "", line)[:12] for line in text.splitlines() if line.startswith("-")
        ]

    assert skeleton(system_prompt("en")) == skeleton(system_prompt("it"))
    assert skeleton(talker_rules("en")) == skeleton(talker_rules("it"))


def test_the_prompts_follow_pisa_s_language() -> None:
    english, italian = owner_at(language="en", country=None).tag, owner_at().tag  # Pisa's start language
    assert system_prompt(english) == system_prompt("en") and talker_rules(english) == talker_rules("en")
    assert system_prompt(italian) == system_prompt("it") and self_block(italian) == self_block("it")


def test_the_talker_speaks_the_session_s_language_with_pisa_s_examples() -> None:
    text = instructions(Fresh(now="martedì 20 ottobre, 13:00"), "", "en-US", pisa_lang="en-US")
    assert text.startswith(talker_rules("en")) and "Speak English." in text


@pytest.mark.parametrize(("language", "name"), [("en", "English"), ("it", "Italian")])
def test_the_night_writes_in_pisa_s_language(language: locale.Language, name: str) -> None:
    from pisa.brain import curious, desk, ideas

    tag = locale.TAGS[language]  # Pisa's start language, the night services' Owner's
    for rule in (desk.WINDOWS, ideas.PREPARE, curious.SCORE):
        assert "Write in {language}." in rule
        assert f"Write in {name}." in prompt_examples.in_language(rule, tag)
    filled = desk.ARBITRAGE.format(way="b2c", language="{language}")
    assert f"Write in {name}." in prompt_examples.in_language(filled, tag)


def test_the_user_is_they_in_the_prompts() -> None:
    for lang in ("en", "it"):
        for text in (system_prompt(lang), talker_rules(lang), bible.rules(lang)):
            assert not re.search(r"\b(he|his|him|himself)\b", text, re.IGNORECASE), lang
