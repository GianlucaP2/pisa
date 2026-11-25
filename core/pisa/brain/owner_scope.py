"""The owner's own items: a reminder, a place note, a task, written after outside text.

Scope: only writes that touch the user's own items (`remember_to`, `remember_at_place`, `cancel_reminder`,
task create and reschedule). Outward and spend tools keep the central gate (`receipts.TaintedTurn`) and
their approvals.

When the turn carries outside text (this turn's reads, or still in the history):

1. INTENT comes only from THIS turn's owner message: a reminder or cancel verb, the tool's own cue (a place
   "quando sono al…", a time "alle due"), or a yes to a reminder Pisa itself offered in its last reply.
   Never from the model.
2. ARGUMENTS pass by exclusion, not by literal naming: a content word (3+ letters, or a number) is
   OUTSIDE-ONLY when the outside text holds it and neither the user's own words (this turn, the last few
   turns) nor the titles of the user's own items (tasks not made from an email, reminders they set) do. Any
   outside-only word gates the write. A paraphrase found nowhere passes.
3. A time's numbers must be the user's or derivable from them ("alle due" is 2 and 14): numbers only the
   outside text holds gate the write.

Pure functions plus one loader of the user's own item titles."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pisa.brain.untrusted import current
from pisa.domain.errors import PisaError
from pisa.domain.rituals import _STOP  # pyright: ignore[reportPrivateUsage]
from pisa.domain.tasks import is_short_yes
from pisa.domain.text import fold

RECENT_OWNER_TURNS = 5  # the user's own words in this many earlier turns of the talk count as theirs

# the user's words asking for a reminder or an alert
REMIND = re.compile(
    r"\b(ricord\w*|promemoria|non farmi dimenticare|avvis\w*|allarm\w*|notific\w*|remind\w*|"
    r"don'?t let me forget|let me know|alert\w*|notify)\b",
    re.IGNORECASE,
)
# a place cue: "quando sono al supermercato", "when I'm at the shop"
PLACE_CUE = re.compile(
    r"\b(?:quando|appena|una volta che)\s+(?:sono|arrivo|passo|vado|esco|sar[oò]|sono\s+arrivat\w+)\s+"
    r"(?:a|al|allo|alla|all|da|dal|in|nel|vicino|presso)\b"
    r"|\b(?:when|once|as soon as)\s+(?:i'?m|i\s+am|i\s+get|i\s+arrive|i\s+reach)\s+(?:at|to|near|in)?\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = (
    "una?|due|tre|quattro|cinque|sei|sette|otto|nove|dieci|undici|dodici|mezzogiorno|mezzanotte|"
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midnight"
)
# a time cue: "alle due", "alle 14", "all'una", "at 2", "tra 10 minuti", "in 10 minutes"
TIME_CUE = re.compile(
    rf"\b(?:alle(?:\s+ore)?|all['\u2019]|at|by)\s*(?:\d|{_NUMBER_WORDS})\b"
    r"|\b(?:tra|fra|in)\s+(?:\d+|un|una|mezz'?ora)\s*(?:minut\w*|min|or[ae]|hours?|mins?|ore)?\b",
    re.IGNORECASE,
)
# the user's words asking for a task to be made or moved
TASK_ASK = re.compile(
    r"\b(aggiung\w*|mett\w*|segn\w*|cre\w*|inserisc\w*|sposta\w*|rimanda\w*|anticipa\w*|add|put|create|"
    r"note|schedule|move|reschedule|postpone|push|set)\b",
    re.IGNORECASE,
)
# a yes: "sì", "ok", "va bene", "do it"
_DO_IT = re.compile(r"^\W*(?:do it|go ahead|fallo|fai pure|facciamolo)\W*$", re.IGNORECASE)
# Pisa's own reply offering a reminder or an alert (a question)
_OFFER = re.compile(
    r"\b(ricord\w*|promemoria|avvis\w*|notific\w*|remind\w*|alert\w*|notify)\b", re.IGNORECASE
)

_MORE_STOP = frozenset(
    "nel nella nelle nei negli sul sulla sulle sui dal dalla dalle dai non per una uno gli the and for with "
    "from that this have has you your can will its was are".split()
)
_NUMBER_VALUES = {
    "un": 1, "una": 1, "uno": 1, "one": 1, "due": 2, "two": 2, "tre": 3, "three": 3, "quattro": 4, "four": 4,
    "cinque": 5, "five": 5, "sei": 6, "six": 6, "sette": 7, "seven": 7, "otto": 8, "eight": 8, "nove": 9,
    "nine": 9, "dieci": 10, "ten": 10, "undici": 11, "eleven": 11, "dodici": 12, "twelve": 12,
    "mezzogiorno": 12, "noon": 12, "mezzanotte": 0, "midnight": 0, "mezza": 30, "mezzo": 30, "half": 30,
    "quarto": 15, "quarter": 15,
}  # fmt: skip


@dataclass(frozen=True)
class Ask:
    """What the user said: this turn's words, their words in the last few earlier turns of the talk, and
    whether Pisa's own last reply offered a reminder."""

    words: str
    recent: tuple[str, ...] = ()
    offered: bool = False

    def said(self) -> str:
        return " ".join((self.words, *self.recent))


def offered_reminder(reply: str | None) -> bool:
    """Pisa's reply is a question that offers a reminder or an alert ("Vuoi che te lo ricordi?")."""
    return bool(reply) and "?" in (reply or "") and bool(_OFFER.search(reply or ""))


def said_yes(words: str) -> bool:
    return is_short_yes(words) or bool(_DO_IT.match(words))


def asked(ask: Ask, *cues: re.Pattern[str]) -> bool:
    """The user's message this turn asks for it: a reminder verb, one of the tool's own cues, or a yes to a
    reminder Pisa offered."""
    if REMIND.search(ask.words) or any(cue.search(ask.words) for cue in cues):
        return True
    return ask.offered and said_yes(ask.words)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text.replace("\u2019", "'")))


def content(text: str) -> set[str]:
    """Content words of 3+ letters plus every number (leading zeros dropped), case and accent folded."""
    out: set[str] = set()
    for w in _tokens(text):
        if w.isdigit():
            out.add(str(int(w)))
        elif len(w) >= 3 and w not in _STOP and w not in _MORE_STOP:
            out.add(w)
    return out


def numbers(text: str) -> set[str]:
    return {w for w in content(text) if w.isdigit()}


def _ints(text: str) -> set[str]:
    """Every number in the text, as an integer string ("00" and "0" alike)."""
    return {str(int(n)) for n in re.findall(r"\d+", text)}


def clock_numbers(said: str) -> set[str]:
    """The clock numbers the user's words state or imply: digits, number words ("due" is 2 and, in the
    afternoon, 14), the half and quarter hours, and the minute 0 only when they named an hour (E1 OS-1:
    never a date number)."""
    out = _ints(said)
    for w in _tokens(said):
        if w in _NUMBER_VALUES:
            out.add(str(_NUMBER_VALUES[w]))
    for n in list(out):
        if 1 <= int(n) < 12:
            out.add(str(int(n) + 12))  # "alle due" may be 14:00
    if {"mezza", "mezzo", "half"} & set(_tokens(said)):
        out.add("30")
    if {"quarto", "quarter"} & set(_tokens(said)):
        out |= {"15", "45"}
    if out:
        out.add("0")  # "alle due" is 14:00
    return out


def owner_numbers(said: str, today: date) -> set[str]:
    """The numbers a date in the user's words may hold: their clock numbers, and today's date and the
    week's days (day, month, year)."""
    out = clock_numbers(said)
    for ahead in range(8):
        day = today + timedelta(days=ahead)
        out |= {str(day.year), str(day.month), str(day.day)}
    return out


# a clock time in a `when`: "14:00", "14.30", "T20:00", "alle 9", "ore 21", "at 9"
_CLOCK = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)")
_CLOCK_WORD = re.compile(r"\b(?:alle|all'|ore|at)\s*(\d{1,2})\b", re.IGNORECASE)


def split_time(when: str) -> tuple[set[str], set[str]]:
    """(clock numbers, date numbers) of a time as written: the hour and minute, and every other number."""
    clock: set[str] = set()
    for m in _CLOCK.finditer(when):
        clock |= {str(int(m.group(1))), str(int(m.group(2)))}
    rest = _CLOCK.sub(" ", when)
    for m in _CLOCK_WORD.finditer(rest):
        clock.add(str(int(m.group(1))))
    rest = _CLOCK_WORD.sub(" ", rest)
    return clock, _ints(rest)


async def owner_items(data: Any) -> list[str]:
    """The user's own items' titles: tasks not made from an email (and no stranger's title), the reminders
    they set. Never an email-made or invite title."""
    titles: list[str] = []
    foreign = current().foreign
    tasks = getattr(data, "tasks", None)
    if tasks is not None and foreign is not None:  # unknown foreign titles: none counts as theirs
        try:
            found = await tasks("all")
        except PisaError:
            found = []
        titles += [t.title for t in found if t.source_kind != "email" and t.title not in foreign]
    live = getattr(data, "live_promises", None)
    if live is not None:
        try:
            promises = await live()
        except PisaError:
            promises = []
        titles += [str(p["what"]) for p in promises]
    return titles


def outside_only(texts: Sequence[str], ask: Ask, items: Iterable[str]) -> list[str]:
    """The words of `texts` the outside text holds and neither the user's words nor their own items do."""
    mine = content(ask.said())
    for item in items:
        mine |= content(item)
    outside: set[str] = set()
    for text in current().texts():
        outside |= content(text)
    wanted: set[str] = set()
    for text in texts:
        wanted |= content(text)
    return sorted(w for w in wanted if w in outside and w not in mine)


def time_outside_only(when: str, ask: Ask, today: date) -> list[str]:
    """The numbers of a time the outside text holds and the user's words neither state nor imply: its hour
    and minute only against their clock numbers, its date parts against the near dates too (E1 OS-1)."""
    outside: set[str] = set()
    for text in current().texts():
        outside |= _ints(text)
    said = ask.said()
    clock, dated = split_time(when)
    found = ((clock & outside) - clock_numbers(said)) | ((dated & outside) - owner_numbers(said, today))
    return sorted(found, key=int)


def refusal(prefix: str, found: Sequence[str]) -> str:
    """What the tool says when a word comes from what Pisa read: it names the word."""
    named = ", ".join(f"«{w}»" for w in found)
    verb = "comes" if len(found) == 1 else "come"
    return f"{prefix}: {named} {verb} from what Pisa read (an email or a page), not from the user. Ask them."


async def gate(
    data: Any,
    ask: Ask,
    *,
    prefix: str,
    unasked: str,
    cues: Sequence[re.Pattern[str]] = (),
    texts: Sequence[str] = (),
    when: str | None = None,
    today: date,
) -> str | None:
    """None: the write may go on. After outside text: the user's message this turn must ask for it (`unasked`
    is what the tool says otherwise), and no argument word or time number may be outside-only."""
    if not current().outside():
        return None
    if not asked(ask, *cues):
        return unasked
    found = outside_only(texts, ask, await owner_items(data))
    if when:
        found = sorted({*found, *time_outside_only(when, ask, today)})
    return refusal(prefix, found) if found else None


# the task writes this rule covers: the arguments that are words, and those that are a time
_TASK_TEXT = {"add_task": ("title", "place"), "reschedule_task": (), "set_task_start": ()}
_TASK_TIME = {
    "add_task": ("due", "start"), "reschedule_task": ("due_at",), "set_task_start": ("start_at",),
}  # fmt: skip


def _time_text(value: Any) -> str:
    return value.strftime("%Y-%m-%dT%H:%M") if hasattr(value, "strftime") else str(value)


async def task_asked(data: Any, ask: Ask, tool: str, args: dict[str, Any], today: date) -> bool:
    """A task made or moved after outside text, on the user's own ask this turn and with no outside-only word
    or time in it: it needs no second yes."""
    if tool not in _TASK_TEXT or not asked(ask, TASK_ASK, TIME_CUE):
        return False
    texts = [str(args[k]) for k in _TASK_TEXT[tool] if args.get(k)]
    dated = args.get("due_kind") == "date"  # a day's deadline: its end-of-day clock is no one's words
    when = " ".join(
        _time_text(args[k])[:10] if dated and k == "due_at" else _time_text(args[k])
        for k in _TASK_TIME[tool]
        if args.get(k)
    )
    found = outside_only(texts, ask, await owner_items(data))
    return not found and not (when and time_not_said(when, ask, today))


_DAY_WORDS = {
    "oggi": 0, "today": 0, "stasera": 0, "stamattina": 0, "stanotte": 0, "tonight": 0,
    "domani": 1, "tomorrow": 1, "dopodomani": 2,
}  # fmt: skip
_WEEKDAYS = (
    ("lunedi", "monday"), ("martedi", "tuesday"), ("mercoledi", "wednesday"), ("giovedi", "thursday"),
    ("venerdi", "friday"), ("sabato", "saturday"), ("domenica", "sunday"),
)  # fmt: skip
_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def owner_days(said: str, today: date) -> set[date]:
    """The days the user's words name: today, tomorrow, the day after, a weekday's next one."""
    words = set(_tokens(said))
    days = {today + timedelta(days=ahead) for w, ahead in _DAY_WORDS.items() if w in words}
    for weekday, names in enumerate(_WEEKDAYS):
        if words & set(names):
            days.add(today + timedelta(days=(weekday - today.weekday()) % 7 or 7))
            if weekday == today.weekday():
                days.add(today)
    return days


def time_not_said(when: str, ask: Ask, today: date) -> list[str]:
    """A task's time the user's words neither state nor imply, wherever it came from: a task made or moved
    on their ask keeps their own day and hour ("spostalo a domani" is never 2027-03-01, "sposta il
    bagnoschiuma" names no day), else it waits for a yes."""
    said = ask.said()
    ints, days = _ints(said), owner_days(said, today)
    found: set[str] = set()
    for m in _DATE.finditer(when):
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if day not in days and str(day.day) not in ints:
            found.add(str(day.day))
    clock, _ = split_time(_DATE.sub(" ", when))
    found |= clock - clock_numbers(said)
    return sorted(found, key=int)
