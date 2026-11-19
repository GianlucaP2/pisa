"""Pisa's own voice is never the user's (corollary discharge).

their "turn" repeated Pisa's own last line word for word ("Ho sbagliato,
non so cosa intendi"): Pisa's voice reached the microphone and came back as their words. The window counted
from when Pisa's line was written, not from when the phone finished saying it, and the plain voice path (an
utterance, not a talk) had no guard at all. One rule for both: a line is Pisa's to hear back until
ECHO_AFTER_S past the end of its speech (its words at SPEECH_WPS), and heard words that are mostly that line,
in its order, are its echo.
Never a bare yes or no (their "sì" to Pisa's "sì o no?"), never a question of their unless Pisa asked it.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pisa.domain import talk as form

ECHO_AFTER_S = 10.0  # past the end of Pisa's speech, its words heard back are still Pisa's
SPEECH_WPS = 2.0  # words a second Pisa's voice says (a slow Italian voice: the window errs long)
ECHO_COVER = 0.85  # this share of the heard words, in Pisa's order, makes them Pisa's echo
ECHO_MIN_WORDS = 5  # fewer words than this are theirs (a "va bene", "domani alle 17")
ECHO_LINE_SHARE = 0.6  # the heard words cover at least this share of one Pisa line
KEPT_LINES = 6  # Pisa's last lines kept per device


@dataclass(frozen=True)
class Line:
    """One line Pisa said: when it was sent (monotonic seconds) and its text."""

    at: float
    text: str

    def until(self) -> float:
        """The last moment its echo can still come back: its speech's end, plus ECHO_AFTER_S."""
        return until(self.at, len(form.echo_words(self.text)))


def until(at: float, words: int) -> float:
    """A line of `words` words sent at `at` can come back until its speech's end plus ECHO_AFTER_S."""
    return at + words / SPEECH_WPS + ECHO_AFTER_S


def same_word(a: str, b: str) -> bool:
    """The same word, or one cut short by the transcript ("intend" for "intendi")."""
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def in_order(words: list[str], heard: list[str], same: Callable[[str, str], bool] = same_word) -> int:
    """How many of `words` appear in `heard` in the same order (a greedy subsequence; a word cut short by the
    transcript counts)."""
    got, i = 0, 0
    for w in words:
        j = i
        while j < len(heard) and not same(w, heard[j]):
            j += 1
        if j < len(heard):
            got, i = got + 1, j + 1
    return got


def asked_words(text: str) -> list[str]:
    """The words of a line's questions (its sentences ending in "?"), numbers as digits."""
    return [w for question in re.findall(r"[^.!?…]+\?", text) for w in form.echo_words(question)]


def recent(lines: Iterable[Line], now: float) -> list[Line]:
    """Pisa's lines whose echo can still come back at `now`."""
    return [line for line in lines if line.until() >= now]


def own_echo(heard: str, lines: Iterable[Line], now: float, *, bare: Callable[[str], bool]) -> bool:
    """The heard words are Pisa's own recent line come back: at least ECHO_MIN_WORDS words, ECHO_COVER of them
    in the order of ONE live line of Pisa's, covering at least ECHO_LINE_SHARE of that line. A question
    is Pisa's echo only against Pisa's own questions. Never their bare yes or no (`bare`), and never their
    pick of one option of Pisa's last question ("Vuoi la palestra domani alle 17 o venerdì alle 18?" →
    "palestra venerdì alle 18")."""
    words = form.echo_words(heard)
    if len(words) < ECHO_MIN_WORDS or bare(heard):
        return False
    live = recent(lines, now)
    if live and picks_option(words, live[-1].text):
        return False
    asked = heard.rstrip().endswith("?")
    said = [asked_words(line.text) if asked else form.echo_words(line.text) for line in live]
    return any(
        (got := in_order(words, s)) >= ECHO_COVER * len(words) and got >= ECHO_LINE_SHARE * len(s)
        for s in said
        if s
    )


_OR = re.compile(r"\s+(?:o|oppure|or)\s+|,\s*", re.IGNORECASE)


def picks_option(words: list[str], line: str) -> bool:
    """The heard words are (mostly) one option of the line's last question, never all of it."""
    questions = re.findall(r"[^.!?…]+\?", line)
    if not questions:
        return False
    options = [form.echo_words(o) for o in _OR.split(questions[-1]) if o.strip()]
    if len(options) < 2:
        return False
    whole = form.echo_words(questions[-1])
    if in_order(words, whole) >= ECHO_LINE_SHARE * len(whole):
        return False  # the whole question come back: an echo, not a pick
    return any(in_order(words, option) >= 0.5 * len(words) for option in options)


def kept(lines: list[Line], line: Line) -> list[Line]:
    """Pisa's lines with this one, the oldest dropped past KEPT_LINES."""
    return [*lines, line][-KEPT_LINES:]
