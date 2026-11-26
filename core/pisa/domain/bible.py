"""Pisa's voice bible (`voice_bible.md`, beside this module). Its rules go, word for word, into
the talker's whisper rules and the brain's prompt; `third_person` is the replay's check (Pisa speaks in the
first person, never of itself as "Pisa", and never "come assistente")."""

import re
from functools import cache
from pathlib import Path

from pisa.domain import locale, prompt_examples

BIBLE = Path(__file__).resolve().parent / "voice_bible.md"


def rules(lang: str) -> str:
    """The bible's rules, as the prompts cite them: their examples in `lang`."""
    return _rules(locale.short(lang))


@cache
def _rules(language: str) -> str:
    text = BIBLE.read_text(encoding="utf-8")
    body = text.split("## The rules", 1)[1].split("\n## ", 1)[0].strip()
    body = prompt_examples.fill(body, language)
    return f"Pisa's voice (the voice bible, core/pisa/domain/voice_bible.md):\n{body}"


# Pisa as the subject of a verb ("Pisa ha spostato", "Pisa ci sta ancora lavorando", "Pisa will move
# it"), or an assistant's phrase about itself
_THIRD = re.compile(
    r"\bPisa\s+(?:ha|hanno|è|e'|sta|stava|sarà|può|non|ci|ti|lo|la|li|le|l'|deve|vuole|farà|sposta|"
    r"mette|manda|has|is|was|will|can|did|does|moved|added|sent|thinks|says|can't|won't|didn't)\b"
    r"|\b(?:come|in quanto)\s+(?:tuo\s+)?assistente\b|\bas an? (?:ai|assistant|language model)\b"
    r"|\bsono (?:un'intelligenza|un assistente|un'ia)\b",
    re.IGNORECASE,
)


def third_person(reply: str) -> list[str]:
    """What in Pisa's reply speaks of Pisa in the third person or as an assistant ([] when none)."""
    return [m.group(0) for m in _THIRD.finditer(reply)]
