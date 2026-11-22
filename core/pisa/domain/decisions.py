"""Pisa's decision log: what Pisa did, proposed, showed or said unasked, with the
model's one-line `purpose` and a provenance that **code** sets, never the model.

A tool records its move where the effect happens (`note`); the purpose the model gave with the call is picked
up from the call's context (`intending`). The turn collects its moves (`collecting`) and the service stores
them in `pisa_decisions` at the turn's end. Pure: no I/O.
"""

import contextlib
import re
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from typing import Literal

Move = Literal["act", "propose", "show", "ask", "look", "speak", "defer", "drop", "promise"]
Provenance = Literal["owner_words", "pisa_proposal", "outside_text", "pisa_initiative"]
PURPOSE_MAX = 120
# FACT: Pisa's tools say "nothing happened" with these openings ("Not changed: …", "No open task matches …",
# "Several tasks match …"): such a call made no move to record. Only the result's first line says so:
# a later "Not changed: the calendar" doesn't undo what the first line did.
_NO_EFFECT = re.compile(r"(Not |No |Several |Nothing |Already )")
# A result in Pisa's own data block (`untrusted.data_block`): its rule line and opening marker come first.
_WRAPPED = re.compile(
    r"\s*(The text between <[a-z-]+-[0-9a-f]{16}> and [^\n]*\n)?\s*(<[a-z-]+-[0-9a-f]{16}>\n)?"
)


@dataclass(frozen=True)
class Decision:
    move: Move
    tool: str
    provenance: Provenance
    ref: str | None = None
    purpose: str | None = None
    rail: str | None = None
    outcome: str | None = None
    key: str | None = None  # a show's present_id: the device's events find its row by it


_purpose: ContextVar[str | None] = ContextVar("pisa_purpose", default=None)
_moves: ContextVar[list[Decision] | None] = ContextVar("pisa_moves", default=None)
_words: ContextVar[tuple[str, date | None]] = ContextVar("pisa_words", default=("", None))


class NotCollecting(RuntimeError):
    """A move was noted where no turn collects its moves."""


def provenance(*, said: bool, outside: bool, unprompted: bool = False) -> Provenance:
    """RAIL: provenance is code's verdict, never the model's word. Outside text read in the turn taints first
    (what Pisa read can't have been the user's idea); then Pisa acting unasked; then the user's own words;
    the rest is Pisa's proposal."""
    if outside:
        return "outside_text"
    if unprompted:
        return "pisa_initiative"
    return "owner_words" if said else "pisa_proposal"


def learnable(provenance: str, outside_kinds: "list[str] | tuple[str, ...] | None") -> bool:
    """RAIL for the night's compile: Pisa never
    learns from a tainted turn. A decision is learnable only when code knows its turn read no outside text:
    its `outside_kinds` recorded and empty (NULL, a row from before they were, is unknown, so never), and its
    provenance not outside text."""
    return outside_kinds is not None and not outside_kinds and provenance != "outside_text"


def clean(purpose: str | None) -> str | None:
    text = " ".join((purpose or "").split())
    return text[:PURPOSE_MAX] or None


@contextlib.contextmanager
def intending(purpose: str | None) -> Iterator[None]:
    """The model's purpose for the tool call being run, for the moves it records; reset after the call."""
    token = _purpose.set(clean(purpose))
    try:
        yield
    finally:
        _purpose.reset(token)


def note(
    move: Move,
    tool: str,
    provenance: Provenance,
    *,
    ref: object = None,
    rail: str | None = None,
    outcome: str | None = None,
    key: str | None = None,
) -> None:
    """Record a move in the current turn. Outside a turn (`collecting`) it raises: a move noted where no turn
    collects would be lost silently. A move made both in a turn and outside one (a REST action, which has its
    own record) says so with `note_if_collecting`."""
    moves = _moves.get()
    if moves is None:
        raise NotCollecting(f"the move {move} {tool} was noted outside a turn")
    ref_text = str(ref) if ref is not None else None
    moves.append(Decision(move, tool, provenance, ref_text, _purpose.get(), rail, outcome, key))


def note_if_collecting(
    move: Move,
    tool: str,
    provenance: Provenance,
    *,
    ref: object = None,
    rail: str | None = None,
    outcome: str | None = None,
    key: str | None = None,
) -> None:
    """`note` for a move made in a turn or outside one (a REST action, a tap on a sheet): outside a turn it
    records nothing, as that path keeps its own record (the approval's row)."""
    if _moves.get() is not None:
        note(move, tool, provenance, ref=ref, rail=rail, outcome=outcome, key=key)


def no_effect(result: str) -> bool:
    wrapper = _WRAPPED.match(result)
    return bool(_NO_EFFECT.match(result, wrapper.end() if wrapper else 0))


def noted() -> int:
    """How many moves the current turn has recorded so far."""
    moves = _moves.get()
    return len(moves) if moves is not None else 0


def acted_tools() -> frozenset[str]:
    """The tools of the current turn's acts that weren't refused or left pending (which
    change was made, so a claim is backed only by a change of its kind)."""
    return frozenset(m.tool for m in _moves.get() or [] if m.move == "act" and m.outcome in (None, "done"))


def acted() -> bool:
    """Whether the current turn made a change so far: an `act` that wasn't refused or left pending (
    a reply may say it was done only then)."""
    moves = _moves.get()
    return bool(moves) and any(m.move == "act" and m.outcome in (None, "done") for m in moves or [])


def take() -> list[Decision]:
    """The moves recorded so far in this turn, handed over once (stored before the turn's last word)."""
    moves = _moves.get()
    if not moves:
        return []
    taken = list(moves)
    moves.clear()
    return taken


def turn() -> object | None:
    """This turn's identity (its moves list): equal only within one turn."""
    return _moves.get()


def intended() -> str | None:
    """The purpose the model gave with the tool call being run."""
    return _purpose.get()


def owner_words() -> tuple[str, date | None]:
    """The user's words this turn and the user's day, for code to judge whose choice a move was."""
    return _words.get()


@contextlib.contextmanager
def collecting(words: str = "", today: date | None = None) -> Iterator[list[Decision]]:
    """The moves of one turn, in order; `words` the user's for this turn."""
    moves: list[Decision] = []
    token, said = _moves.set(moves), _words.set((words, today))
    try:
        yield moves
    finally:
        _moves.reset(token)
        _words.reset(said)
