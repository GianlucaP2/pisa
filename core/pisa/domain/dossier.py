"""One finite morning. Pure rules, no
I/O.

- **One knock, finite.** The morning's knock carries one dossier: the knock's own parts (the floor item,
  the morning thought, the `stanotte` sheet's lead, il prurito, il consiglio, la prova, il quarto d'ora), the
  admin radar (obligations due soon), their open loops (intentions due, promises waiting for their yes, what
  they wait for), the night's drafts (replies Pisa drafted that wait for their yes) and "domani, provato"
  (only when the day holds a real conflict). **At most three items are spoken** (`SPOKEN_MAX`), the most
  pressing first (`RANK`); the rest go on the phone, and the spoken part ends with "È tutto" (`ALL`).
- **Nothing reads as done unless it was done** (`reads_done`): an item whose words say done, sent, paid or
  booked while its record isn't done is never spoken. Every item's words are code's own templates, from
  records; a draft "aspetta il tuo sì", never "inviata".
- **Outside text is never spoken**: an item whose words rest on a stranger's text (a received mail's
  waiting-for, an invite's title) goes only to the phone, as data.
- **Each item names the records read to make it** (`sources`): the dossier logs them (ids only) and the phone
  shows them; each item gets their thumb on the phone.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Any, Literal, get_args

from pisa.domain import rituals
from pisa.domain import voice1 as knock
from pisa.domain.text import fold

Kind = Literal[
    "provato", "floor", "radar", "loop", "draft", "thought", "sheet", "prurito", "perche", "consiglio",
    "prova", "quarto",
]  # fmt: skip
KINDS: tuple[Kind, ...] = get_args(Kind)

SPOKEN_MAX = 3  # RAIL (the brief): at most three items are spoken; the rest go on the phone
RADAR_DAYS = 7  # the radar's obligations due within this many days (or overdue) are in the dossier
RADAR_SOON_DAYS = 1  # due today, tomorrow or overdue: pressing (ranked with the day's floor)
DRAFT_HOURS = 24  # the night's drafts: Pisa's unsent replies made or changed in the last day
ITEMS_MAX = 20  # the phone's list: at most this many items a morning

# The order an item is spoken in: a real conflict in the day first, then the day's next fixed thing, what is
# due
# now (the radar's pressing items, their loops), the night's answers, the rest. Lower is sooner.
RANK: dict[str, int] = {
    "provato": 0,
    "floor": 1,
    "radar_soon": 2,
    "loop": 3,
    "prurito": 4,
    "perche": 5,
    "radar": 6,
    "draft": 7,
    "thought": 8,
    "sheet": 9,
    "consiglio": 10,
    "prova": 11,
    "quarto": 12,
}

ALL = {"it-IT": "È tutto.", "en-US": "That's all."}
REST = {
    "it-IT": ("L'altra è sul telefono.", "Le altre {n} sono sul telefono."),
    "en-US": ("The other one is on your phone.", "The other {n} are on your phone."),
}

# RAIL: the words that say a thing is done (Italian and English). A line with one of them is spoken only when
# its
# record is done.
_DONE = re.compile(
    r"\b(?:fatt[oaie]|complet\w*|inviat[oaie]|mandat[oaie]|spedit[oaie]|pagat[oaie]|saldat[oaie]|"
    r"prenotat[oaie]|chius[oaie]|risolt[oaie]|consegnat[oaie]|confermat[oaie]|archiviat[oaie]|"
    r"sistemat[oaie]|evas[oaie]|done|sent|paid|booked|completed|finished|resolved|delivered|settled|"
    r"confirmed|archived|handled)\b"
)


def reads_done(text: str) -> bool:
    """The line says something is done (fatto, inviato, pagato, prenotato, done, sent, paid, booked…)."""
    return bool(_DONE.search(fold(text)))


def word(table: dict[str, str], lang: str) -> str:
    return table.get(lang, table["en-US"])


@dataclass(frozen=True)
class Source:
    table: str
    id: str

    def json(self) -> dict[str, str]:
        return {"table": self.table, "id": self.id}


@dataclass(frozen=True)
class Item:
    """One thing the morning holds, from its record (`table`, `ref`), with the records read to make it."""

    kind: Kind
    text: str
    table: str
    ref: str
    sources: tuple[Source, ...] = ()
    rank: int = 99
    due: date | None = None  # its own day, when it has one (the radar, a loop): sooner first within a rank
    outside: bool = False  # its words rest on outside text: phone only
    done: bool = False  # its record is done (and read back): only then may its words say so
    spoken: bool = field(default=False, compare=False)

    def json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "table": self.table,
            "ref": self.ref,
            "sources": [s.json() for s in self.sources],
            "rank": self.rank,
            "outside": self.outside,
            "spoken": self.spoken,
        }


def speakable(item: Item) -> bool:
    """RAIL: an item Pisa may say aloud: its words are code's from their records (never a stranger's), and
    they never
    read as done unless it was done."""
    return bool(item.text.strip()) and not item.outside and (item.done or not reads_done(item.text))


def from_part(part: knock.Part) -> Item:
    """A knock part as a dossier item: its own record is the one source read."""
    return Item(
        part.kind,
        part.text,
        part.table,
        part.ref,
        (Source(part.table, part.ref),),
        RANK.get(part.kind, 99),
    )


def assemble(
    items: Iterable[Item], *, said: frozenset[tuple[str, str]] = frozenset(), speak: bool = True
) -> list[Item]:
    """The morning's dossier, in order: one item a record, the most pressing first (rank, then its own day),
    at most ITEMS_MAX. What was already said this morning (`said`, by record) stays spoken and first; with
    `speak`, the next speakable ones are marked spoken until SPOKEN_MAX were said this morning in all; the
    rest
    go on the phone."""
    seen: set[tuple[str, str]] = set()
    unique: list[Item] = []
    for item in items:
        if not item.text.strip() or (item.table, item.ref) in seen:
            continue
        seen.add((item.table, item.ref))
        unique.append(item)
    ordered = sorted(unique, key=lambda i: ((i.table, i.ref) not in said, i.rank, i.due or date.max))[
        :ITEMS_MAX
    ]
    room = max(0, SPOKEN_MAX - len(said)) if speak else 0  # RAIL: three a morning, however often they ask
    out: list[Item] = []
    for item in ordered:
        before = (item.table, item.ref) in said
        now = not before and room > 0 and speakable(item)
        room -= now
        out.append(replace(item, spoken=before or now))
    return out


def spoken(items: Sequence[Item], lang: str) -> str:
    """What Pisa says: each spoken item's sentence (at most SPOKEN_MAX), how many wait on the phone, then "È
    tutto".
    Nothing to say: the knock's own "nothing new" line."""
    said = [i for i in items if i.spoken][:SPOKEN_MAX]
    if not said:
        return knock.word(knock.NOTHING, lang)
    lines = [knock.one_line(i.text) for i in said]
    rest = len(items) - len(said)
    if rest:
        one, many = REST.get(lang, REST["en-US"])
        lines.append(one if rest == 1 else many.format(n=rest))
    lines.append(word(ALL, lang))
    return " ".join(lines)


# --- the items' own words (code's templates, records only) --------------------------------------------------


def _when(due: date, today: date, lang: str) -> str:
    it = lang == "it-IT"
    days = (due - today).days
    if days < 0:
        return f"scadeva il {due:%d/%m}" if it else f"was due {due:%d/%m}"
    if days == 0:
        return "scade oggi" if it else "is due today"
    if days == 1:
        return "scade domani" if it else "is due tomorrow"
    return f"scade il {due:%d/%m}" if it else f"is due {due:%d/%m}"


def radar_line(what: str, due: date | None, today: date, lang: str) -> str:
    """An obligation of the admin radar: its own words (code's, from the radar's record) and its day."""
    text = knock.one_line(what, 120).rstrip(".")
    if due is None:
        return f"{text}: da fissare." if lang == "it-IT" else f"{text}: date to set."
    return f"{text}: {_when(due, today, lang)}."


def radar_rank(due: date | None, today: date) -> int:
    soon = due is not None and (due - today).days <= RADAR_SOON_DAYS
    return RANK["radar_soon"] if soon else RANK["radar"]


def loop_line(
    kind: str, state: str, what: str, who: str | None, due: date | None, today: date, lang: str
) -> str:
    """An open loop of theirs: a promise waiting for their yes, what they wait for, an intention due."""
    it = lang == "it-IT"
    what = knock.one_line(what, 100).rstrip(".")
    if state == "proposed":
        return f"Hai promesso: {what}. Lo tengo?" if it else f"You promised: {what}. Shall I keep it?"
    if kind == "waiting_for":
        by = f" da {who}" if who and it else f" from {who}" if who else ""
        return f"Aspetti ancora {what}{by}." if it else f"You're still waiting for {what}{by}."
    if due is not None and due < today:
        return f"Ancora da fare: {what}." if it else f"Still to do: {what}."
    return f"Da fare oggi: {what}." if it else f"To do today: {what}."


def drafts_line(n: int, lang: str) -> str:
    """The night's drafts: replies Pisa drafted that wait for their yes (never "sent": nothing left)."""
    if lang == "it-IT":
        return (
            "Una bozza di risposta aspetta il tuo sì."
            if n == 1
            else f"{n} bozze di risposta aspettano il tuo sì."
        )
    return (
        "A draft reply is waiting for your yes." if n == 1 else f"{n} draft replies are waiting for your yes."
    )


def provato_line(line: str, lang: str) -> str:
    """ "Domani, provato": the night rehearsed the day and found one real conflict (the Ghost Run's own
    words)."""
    head = "Ho provato la giornata" if lang == "it-IT" else "I rehearsed the day"
    return f"{head}: {knock.one_line(line, knock.PART_MAX - len(head) - 2)}"


# --- "domani, provato": a real conflict only (the Ghost Run's own checks, never its deadline or rain lines)
# ----


@dataclass(frozen=True)
class Conflict:
    kind: Literal["clash", "transfer"]
    line: str
    blocks: tuple[rituals.Block, rituals.Block]


def _hm(moment: datetime) -> str:
    """The Ghost Run's own clock words ("9:30", "10")."""
    return f"{moment:%H:%M}".removeprefix("0") if moment.minute else f"{moment.hour}"


def _low(title: str) -> str:
    return title[:1].lower() + title[1:] if title[:2] != title[:2].upper() else title


def _utc(moment: datetime) -> datetime:
    """Blocks compare in UTC (two moments in one zone compare by wall clock, ignoring the fall-back
    hour's fold)."""
    return moment.astimezone(UTC)


def conflict(
    blocks: Sequence[rituals.Block], travel: dict[tuple[str, str], int], lang: str
) -> Conflict | None:
    """The day's first real conflict, in the Ghost Run's own words: two blocks overlapping, else a trip
    between two that doesn't fit the time between them. Nothing else (a deadline with no block, rain on a bike
    day) is a conflict: no line."""
    timed = sorted(blocks, key=lambda b: _utc(b.start))
    for a, b in pairwise(timed):
        if _utc(b.start) < _utc(a.end):
            line = rituals.line(lang, "clash", a=b.title, at=_hm(b.start), b=_low(a.title))
            return Conflict("clash", line, (a, b))
    for a, b in pairwise(timed):
        need = travel.get((a.ref, b.ref))
        have = int((_utc(b.start) - _utc(a.end)).total_seconds() // 60)
        if need is not None and need > have:
            words = {"a": _low(a.title), "b": _low(b.title), "need": need, "have": max(have, 0)}
            line = rituals.line(lang, "transfer", **words)
            return Conflict("transfer", line, (a, b))
    return None


def still(kind: str, refs: Sequence[str], need: int | None, blocks: Sequence[rituals.Block]) -> bool:
    """A rehearsal's conflict still holds at reading time: both blocks still in the day (not
    cancelled, declined or gone), and still overlapping, or the trip between them still not fitting."""
    found = {b.ref: b for b in blocks}
    if len(refs) != 2 or any(r not in found for r in refs):
        return False
    a, b = sorted((found[r] for r in refs), key=lambda x: _utc(x.start))
    if kind == "clash":
        return _utc(b.start) < _utc(a.end)
    have = int((_utc(b.start) - _utc(a.end)).total_seconds() // 60)
    return need is not None and need > have
