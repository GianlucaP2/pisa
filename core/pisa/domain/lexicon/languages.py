"""A language's English name, as a prompt says it ("Write in Italian"). Each caller keeps how it reads the
language it is given: by its code, by its exact tag, or by its first letters.

No I/O here."""

# by language code ("it", "en")
NAMES: dict[str, str] = {"en": "English", "it": "Italian"}
# by the BCP-47 tag a turn carries ("it-IT", "en-US")
TAG_NAMES: dict[str, str] = {"it-IT": "Italian", "en-US": "English"}
# by language code, with Dutch (the user may read Dutch: the reasons an email is shown are in their language)
NAMES_WITH_DUTCH: dict[str, str] = {"it": "Italian", "en": "English", "nl": "Dutch"}


def name_by_tag(lang: str) -> str:
    """ "Italian" for the it-IT tag, else "English"."""
    return TAG_NAMES.get(lang, "English")


def name_by_prefix(lang: str) -> str:
    """ "Italian" for any tag starting "it", else "English"."""
    return "Italian" if lang.startswith("it") else "English"
