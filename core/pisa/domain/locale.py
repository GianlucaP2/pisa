"""Pisa's languages: their tags, their tables, their names.

Which language and country the user has is not here: it is their context (`domain/owner.py`), passed in. A
turn in another supported language is answered in that language, and the user's own Settings choice
(`settings.language`) wins over the language Pisa was started with (PISA_LANGUAGE).

No I/O here."""

from typing import Literal, get_args

from pisa.domain.lexicon import languages

Language = Literal["en", "it"]
LANGUAGES: tuple[Language, ...] = ("en", "it")
# the BCP-47 tag the device protocol and the string tables use for each language
TAGS: dict[str, str] = {"en": "en-US", "it": "it-IT"}
# the same tags as the wire lists them (the device protocol, Settings, the city names), in that order
Tag = Literal["it-IT", "en-US"]
WIRE_TAGS: tuple[Tag, ...] = get_args(Tag)
# the Postgres text search configuration for each language
TS_CONFIGS: dict[str, str] = {"en": "english", "it": "italian"}
# the language's name, as a prompt says it ("Write in English")
NAMES = languages.NAMES


def short(lang: str) -> Language:
    """A language tag's table key: "it-IT" or "it" → "it"; English for anything else."""
    return "it" if lang.lower().startswith("it") else "en"


def tag_of(lang: str) -> str:
    """A supported BCP-47 tag for `lang`: "it" → "it-IT", "en-GB" → "en-US"."""
    return TAGS[short(lang)]


def ts_config(lang: str) -> str:
    """The Postgres text search configuration for `lang`."""
    return TS_CONFIGS[short(lang)]


def name(lang: str) -> str:
    """The language's English name, as a prompt says it: "English", "Italian"."""
    return NAMES[short(lang)]


def pick[T](table: dict[str, T], lang: str) -> T:
    """The entry of a per-language table (keyed "en"/"it") for `lang`."""
    return table[short(lang)]
