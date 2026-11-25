"""The real model's own answers, replayed offline.

The recorded turn files hold what Pisa's real model called and what Pisa said, scenario by
scenario and turn by turn, in the paid depth runs (the recorded world, synthetic). A scripted model here says,
turn by turn, exactly what the real model said, through the current code, and the depth checks score what Pisa
would now speak and show. Free; it must meet the targets before anyone pays for a depth run.

What a recording holds, and so what the replay can and cannot reproduce:
- Pisa's words, not the model's: Pisa's interim line ("Cerco, un attimo.") and its "I couldn't show it on your
  phone" are code's and are taken out (the current code adds its own); the rest is replayed as the model's
  words, a little conservatively (a truth line code said for a claim is replayed as words of the answer).
- The call names, not their arguments: each call is mapped onto the scenario's deep script's arguments (its
  queries, pages, places), as many of each tool as the recording made. The sheet the real model made
  (`show_findings`) is the deep script's rows with the real words as its `lead` (as many rows as the
  recording showed, when its log says so); `show_places` and `show_place` show those places as a card, and a
  turn that showed nothing shows nothing. A tool whose arguments the recording cannot give (an agenda, a
  departure board, a note's lines) is left out.
- Only the runs that missed a check: a run at 100% was not printed. The checks of the research path the
  replay cannot reproduce (candidates, sources, table, quick) are carried from the recording when it failed
  them; what the research changes touch (what is said, how much, and what is shown of it) is the replay's own.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.replay import depth
from tests.replay.depth_world import PLACES

# the recorded turn files are not shipped with the code: PISA_DEPTH_TURNS names the folder that holds them
TURNS = Path(os.environ.get("PISA_DEPTH_TURNS", "turns"))
# the model's research path: carried from the recording. "quick" is the replay's own,
# judged from what the code shows of the recorded call (the code can now cut a quick one's card to one place)
CARRIED = ("candidates", "sources", "table")
_CODE_LINES = (
    "Cerco, un attimo.", "Let me look, one moment.",
    "Non sono riuscito a mostrartelo sul telefono.", "I couldn't show it on your phone.",
)  # fmt: skip
_CARRIED_HARD = ("untraced",)  # the research path's hard fails: carried from the recording
_NO_ARGS = frozenset({"where_am_i", "nearest_pharmacy"})
_SHOWS = ("show_findings", "show_places", "show_place")
# a review finding: a lead recorded as the claim rail's truth line alone was a change claim the rail held
# (d01 and d02 in 4 runs): replayed as such a claim naming the pick, never as the truth line (which names
# nothing, so any code says its own lead instead and the recorded failure vanishes without the change too)
_TRUTH = re.compile(r"(?:Non l'ho [^:.]*: non ho cambiato niente|I haven't [^:.]*: nothing has changed)\.")


@dataclass
class Turn:
    owner: str
    calls: list[str]
    said: str


@dataclass
class Recorded:
    """One run of one scenario, as the paid run's log printed it."""

    sid: str
    turns: list[Turn] = field(default_factory=list)
    failed: list[tuple[int, str, str]] = field(default_factory=list)  # (turn, check, why)
    hard: list[tuple[int, str]] = field(default_factory=list)  # (turn, kind)


_HEAD = re.compile(r"^## depth (\S+):")
_HARD = re.compile(r"^\s+HARD turn (\d+) (\w+):")
_FAIL = re.compile(r"^\s+- turn (\d+) ([\w-]+): (.*)$")
_OWNER = re.compile(r"^\s+\d+\. owner: (.*)$")
_CALLS = re.compile(r"^\s+calls:\s?(.*)$")
_SAID = re.compile(r"^\s+Pisa: (.*)$")


_NOT_SAID = ("## ", "Post job", "git version", "Temporarily", "Adding repository", "http", "Cleaning", "##[")


def parse(name: str) -> list[Recorded]:
    out: list[Recorded] = []
    saying = False
    for line in (TURNS / name).read_text(encoding="utf-8").splitlines():
        if m := _HEAD.match(line):
            out.append(Recorded(m.group(1)))
        elif not out:
            continue
        elif m := _HARD.match(line):
            out[-1].hard.append((int(m.group(1)), m.group(2)))
        elif m := _FAIL.match(line):
            out[-1].failed.append((int(m.group(1)), m.group(2), m.group(3)))
        elif m := _OWNER.match(line):
            out[-1].turns.append(Turn(m.group(1), [], ""))
        elif (m := _CALLS.match(line)) and out[-1].turns:
            out[-1].turns[-1].calls = [c.strip() for c in m.group(1).split(",") if c.strip()]
        elif (m := _SAID.match(line)) and out[-1].turns:
            out[-1].turns[-1].said = m.group(1)
            saying = True
            continue
        elif saying and not line.startswith(_NOT_SAID):  # a reply over lines (a list): the log kept them
            out[-1].turns[-1].said += "\n" + line
            continue
        saying = False
    return out


def words_of(said: str) -> str:
    """The model's words in what Pisa said: Pisa's own interim and not-shown lines out."""
    text = said
    for line in _CODE_LINES:
        text = text.replace(line, " ")
    return "\n".join(x for x in (" ".join(line.split()) for line in text.split("\n")) if x)


def _rows_of(steps: list[Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """The deep script's sheet (its call's arguments) and its rows' place keys."""
    by_name = {p.name: p.key for p in PLACES}
    for rnd in steps:
        if isinstance(rnd, list):
            for c in rnd:
                if isinstance(c, tuple) and c[0] == "show_sheet":
                    table = next(b for b in c[1]["blocks"] if b["type"] == "table")
                    return c[1], [by_name[r[0]] for r in table["rows"] if r[0] in by_name]
    return None, []


def _named(said: str, keys: list[str]) -> list[str]:
    """The places (keys) the words name, in the order said, then the rest."""
    plain = depth._plain(said)  # pyright: ignore[reportPrivateUsage]
    hits = sorted((plain.find(depth._plain(p.name)), p.key) for p in PLACES  # pyright: ignore[reportPrivateUsage]
                  if depth._plain(p.name) in plain)  # fmt: skip  # pyright: ignore[reportPrivateUsage]
    named = [k for _, k in hits]
    return [*named, *[k for k in keys if k not in named]]


def _shown_rows(rec: Recorded, n: int) -> int | None:
    """How many rows the real model showed on turn n, when its log says so (a table check, or a quick one's
    "N research calls, table of M")."""
    for turn, check, why in rec.failed:
        if turn == n and check in ("table", "quick") and (m := re.search(r"table of (\d+)", why)):
            return int(m.group(1))
    return None


def calls_quick(rec: Recorded) -> set[int]:
    """The turns whose recorded quick fail was the research path's call count ("4 research calls"):
    the replay keeps only the calls its deep script has (d17 in one recorded run read 3 pages, the script 1),
    so it cannot reproduce that count; carried from the recording, as the research path's checks are. The
    table part of quick ("table of 3") stays the replay's own: the code decides what is shown."""
    out: set[int] = set()
    for t, check, why in rec.failed:
        m = re.search(r"(\d+) research calls", why)
        if check == "quick" and m is not None and int(m.group(1)) > depth.QUICK_CALLS:
            out.add(t)
    return out


def turn_steps(
    rec: Recorded, n: int, deep: list[Any], owner: str, sheet_title: str, area: str = "", now: str = ""
) -> list[Any]:
    """Turn n (1-based) as the real model played it, on the deep script's arguments."""
    t = rec.turns[n - 1]
    said = words_of(t.said)
    left = {c: t.calls.count(c) for c in set(t.calls)}
    rounds: list[list[Any]] = []
    if first := [(c, {}) for c in sorted(_NO_ARGS) if left.get(c)]:
        rounds.append(first)
    if left.get("my_findings"):
        rounds.append([("my_findings", {})])
    sheet, keys = _rows_of(deep)
    for rnd in deep:
        if not isinstance(rnd, list):
            continue
        kept = []
        for c in rnd:
            if not isinstance(c, tuple) or c[0] in ("show_sheet", "show_places"):
                continue
            if left.get(c[0], 0) > 0:
                left[c[0]] -= 1
                kept.append(c)
        if kept:
            rounds.append(kept)
    found = {k for rnd in rounds for name, a in rnd if name == "place_details" for k in [a["place"]]}
    shows = [c for c in t.calls if c in _SHOWS]
    if not shows:
        return [*rounds, said]
    if not keys:  # no sheet in the deep script (a quick one): the places the words name
        keys = _named(said, [])
    rows = _named(said, keys)
    # The places this turn's map searches returned (what the real model could show without details)
    mapped = [p.key for rnd in rounds for name, a in rnd if name == "map_search" and area
              for p in depth._top(str(a.get("query", "")), area, depth._local(now))]  # fmt: skip  # pyright: ignore[reportPrivateUsage]
    if (count := _shown_rows(rec, n)) is not None:
        # The recording showed more than the words name: the map's next ones fill the call
        rows = [*rows, *(k for k in dict.fromkeys(mapped) if k not in rows)][:count]
    rows = rows or keys
    if left.get("walk_times") and rows:
        rounds.append([("walk_times", {"places": [_id(k) for k in rows]})])
    for k in rows[:3]:  # a shown place is one this turn looked up (the map's own, when it called no details)
        if k not in found and _id(k) not in found and ("place_details" in t.calls or k not in mapped):
            rounds.append([depth.pd(k)])
    title = (sheet or {}).get("title") or sheet_title
    kind = shows[-1]
    if kind == "show_findings":
        pick = rows[0]
        if _TRUTH.fullmatch(said.strip()):
            name = depth.place(pick).name
            said = f"Fatto: per te ora {name}." if said.startswith("Non") else f"Done: for you now, {name}."
        places = [{"place_id": _id(k), "why": _why(sheet, k) or "qui vicino"} for k in rows]
        call = depth.call(
            "show_findings", title=title, request=owner, criteria=["qui vicino"], pick=_id(pick),
            places=places, lead=said,
        )  # fmt: skip
        return [*rounds, [call], ""]
    if kind == "show_place" or len(rows) == 1:
        return [*rounds, [depth.call("show_place", place=_id(rows[0]))], said]
    card = depth.card(title, owner, [(k, _why(sheet, k) or "qui vicino", []) for k in rows[:3]])
    return [*rounds, [card], said]


def _id(key: str) -> str:
    return depth.place(key).id


def _why(sheet: dict[str, Any] | None, key: str) -> str:
    if sheet is None:
        return ""
    table = next(b for b in sheet["blocks"] if b["type"] == "table")
    name = depth.place(key).name
    return next((str(r[-1]) for r in table["rows"] if r[0] == name), "")


def played(rec: Recorded) -> dict[str, Any]:
    """The scenario with the recording's turns as its script."""
    [s] = [x for x in depth.SCENARIOS if x["id"] == rec.sid]
    script = []
    for n, (deep, turn) in enumerate(zip(s["script"], s["turns"], strict=True), start=1):
        if n > len(rec.turns):
            script.append(deep)
            continue
        script.append(turn_steps(rec, n, deep, turn["owner"], depth.first(s), s["area"], s["now"]))
    return s | {"script": script}


@dataclass
class Replayed:
    rec: Recorded
    result: depth.DepthResult

    def checks(self) -> list[tuple[int, str, bool, str]]:
        """The replay's checks, with the research path's failures carried from the recording."""
        carried = {(t, c) for t, c, _ in self.rec.failed if c in CARRIED}
        carried |= {(t, "quick") for t in calls_quick(self.rec)}
        out = []
        for t, c, ok, why in self.result.checks:
            if (t, c) in carried:
                out.append((t, c, False, "carried from the recording: " + next(
                    w for tt, cc, w in self.rec.failed if (tt, cc) == (t, c))))  # fmt: skip
            else:
                out.append((t, c, ok, why))
        return out

    def hard(self) -> list[str]:
        """The replay's own long fails; quick and untraced carried from the recording (a review finding: the
        replay's evidence is the deep script's calls, never the pages the real model read, so a name it read
        elsewhere is untraced here only: d17's "Rivendita Flash")."""
        by_calls = calls_quick(self.rec)
        carried = [
            f"turn {t} {k}: carried from the recording" for t, k in self.rec.hard
            if k in _CARRIED_HARD or (k == "quick" and t in by_calls)
        ]  # fmt: skip
        own = [
            h for h in self.result.hard if h.split(" ", 2)[-1].split(":")[0] not in _CARRIED_HARD
            and not (h.split(" ", 2)[-1].startswith("quick") and int(h.split(" ", 2)[1]) in by_calls)
        ]  # fmt: skip
        return own + carried

    @property
    def deep(self) -> bool:
        return not self.hard() and all(ok for _, _, ok, _ in self.checks())


@dataclass
class Figures:
    short: int
    turns: int
    hard_per_60: int
    deep: list[str]
    scenarios: int


def figures(name: str, replayed: list[Replayed]) -> Figures:
    """Short over the turns there, the runs with a hard fail per 60 (the replay's, plus the runs the log cut
    off as recorded), the scenarios present deep in every run."""
    header = (TURNS / name).read_text(encoding="utf-8").splitlines()[0]
    m = re.search(r"runs with a hard fail (\d+)", header)
    absent_hard = max(0, (int(m.group(1)) if m else 0) - sum(1 for r in replayed if r.rec.hard))
    short = [ok for r in replayed for _, c, ok, _ in r.checks() if c == "short"]
    ids = sorted({r.rec.sid for r in replayed})
    deep = [sid for sid in ids if all(r.deep for r in replayed if r.rec.sid == sid)]
    return Figures(sum(short), len(short), sum(1 for r in replayed if r.hard()) + absent_hard, deep, len(ids))


def summary(name: str, replayed: list[Replayed]) -> str:
    """The figures the ruling asks for: short over the turns there, hard fails per 60, deep on the scenarios
    present, beside the recording's own."""
    header = (TURNS / name).read_text(encoding="utf-8").splitlines()[0]
    m = re.search(r"runs with a hard fail (\d+)", header)
    recorded_hard = int(m.group(1)) if m else 0
    present_hard = sum(1 for r in replayed if r.rec.hard)
    absent_hard = max(0, recorded_hard - present_hard)  # runs the log cut off
    short = [ok for r in replayed for _, c, ok, _ in r.checks() if c == "short"]
    was = [ok for r in replayed for _, c, ok, _ in r.result.checks if c == "short"]
    rec_short_misses = sum(1 for r in replayed for _, c, _ in r.rec.failed if c == "short")
    hard = sum(1 for r in replayed if r.hard())
    ids = sorted({r.rec.sid for r in replayed})
    deep = [sid for sid in ids if all(r.deep for r in replayed if r.rec.sid == sid)]
    lines = [
        f"{name}: {len(replayed)} recorded runs of {len(ids)} scenarios (the runs that missed a check)",
        f"  short: {sum(short)}/{len(short)} = {sum(short) / max(1, len(short)):.0%} of the turns there "
        f"(recorded: {len(short) - rec_short_misses}/{len(short)}; the replay's own {sum(was)}/{len(was)})",
        f"  hard fails: {hard} of the {len(replayed)} runs there, + {absent_hard} in runs the log cut off "
        f"= {hard + absent_hard} per 60 (recorded: {recorded_hard})",
        f"  deep on the scenarios present: {len(deep)}/{len(ids)} "
        f"({', '.join(d[:3] for d in deep) or 'none'})",
    ]
    return "\n".join(lines)


def table(replayed: list[Replayed]) -> str:
    """Each run's turns: words, sentences, what was said and shown, its failing checks."""
    out = []
    for r in replayed:
        res = r.result
        bad = [f"t{t} {c}" for t, c, ok, _ in r.checks() if not ok] + r.hard()
        out.append(f"## {r.rec.sid}: {'deep' if r.deep else '; '.join(bad)}")
        for n, t in enumerate(res.turns, 1):
            said = t.reply.strip()
            sentences = len([x for x in re.split(r"(?<=[.!?])\s+", said) if x.strip()])
            out.append(f"  {n}. {depth.spoken_words(said)} words, {sentences} sentences: {said[:300]}")
            if n <= len(res.shown) and res.shown[n - 1].strip():
                out.append(f"     shown: {' / '.join(res.shown[n - 1].split(chr(10)))[:300]}")
    return "\n".join(out)
