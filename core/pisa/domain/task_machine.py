"""The task state machine, in code. Pure rules, no I/O (EMAIL-UNDERSTANDING decision 5).

A task made from a storyline has a state: open, waiting (with a date it waits for), done, cancelled,
superseded or snoozed. The model never moves one: events do, through the table below.
- a change needs a reason, and the reasons are all things that *happened* (an event, the owner's word, a newer
  task); the clock is not one of them, so nothing is ever closed by time (a waiting task gets a nudge);
- closing (done, cancelled, superseded) needs an exact-key link or the owner's own word. Anything else is a
  proposal for him to accept;
- the dedupe key is (entity, intent): one task for one thing the owner has to do about one entity.
"""

from typing import Literal

TaskState = Literal["open", "waiting", "done", "cancelled", "superseded", "snoozed"]
STATES: tuple[TaskState, ...] = ("open", "waiting", "done", "cancelled", "superseded", "snoozed")
CLOSED: frozenset[str] = frozenset({"done", "cancelled", "superseded"})

# why a task changes: what happened
Reason = Literal[
    "fulfilled",  # the thing was done (he replied, the parcel left, the payment was made)
    "void",  # the thing no longer applies (the order was cancelled, the appointment cancelled)
    "not_a_task",  # under the storylines' rules nothing in the mail was the owner's to do (the re-triage)
    "waits",  # the next move is someone else's, or it is a check for a later day
    "asked_again",  # it is the owner's move again
    "replaced",  # a newer task for the same (entity, intent) took its place
    "snoozed",  # the owner put it off
    "reopened",  # an undo
]

# (reason, from-states) -> to-state
_TABLE: dict[str, tuple[frozenset[str], TaskState]] = {
    "fulfilled": (frozenset({"open", "waiting", "snoozed"}), "done"),
    "void": (frozenset({"open", "waiting", "snoozed"}), "cancelled"),
    "not_a_task": (frozenset({"open", "waiting", "snoozed"}), "cancelled"),
    "waits": (frozenset({"open", "snoozed"}), "waiting"),
    "asked_again": (frozenset({"waiting", "snoozed"}), "open"),
    "replaced": (frozenset({"open", "waiting", "snoozed"}), "superseded"),
    "snoozed": (frozenset({"open", "waiting"}), "snoozed"),
    "reopened": (frozenset({"done", "cancelled", "superseded", "waiting", "snoozed"}), "open"),
}


def next_state(current: str | None, reason: str, *, hard: bool, by_owner: bool = False) -> TaskState | None:
    """The task's next state, or None when the reason does not move it from `current`. A closing reason
    needs `hard` (the event is linked by an exact key) or `by_owner` (his own word, his own sent mail); an
    undo (`reopened`) is his."""
    entry = _TABLE.get(reason)
    if entry is None or (current or "open") not in entry[0]:
        return None
    to = entry[1]
    if to in CLOSED and not (hard or by_owner):
        return None  # a proposal, not a change
    if reason == "reopened" and not by_owner:
        return None
    return to


def status_of(state: str) -> str:
    """The task's old `status` that goes with a state (what every reader of `tasks.status` still sees)."""
    return {"done": "done", "cancelled": "dismissed", "superseded": "dismissed"}.get(state, "open")


def dedupe_key(entity_id: str | None, intent: str | None) -> tuple[str, str] | None:
    """One task per (entity, intent); none without both."""
    return (entity_id, intent) if entity_id and intent else None
