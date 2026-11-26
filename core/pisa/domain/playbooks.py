"""The playbooks: the storyline eval's vocabulary as canonical data, and the rules that move a storyline.
Pure rules, no I/O (LIFE-GRAPH sections 2, 2b and 7; the eval's README and `score.py`).

What lives here:
- the five playbooks, their states and which states are terminal;
- the event kinds, the next-action kinds, who a storyline waits on, the effect kinds, and the action and
  effect keys (the fixed lists the events call may use, and nothing else);
- `SUPPORT`: which event kinds may move a storyline into a state (the eval's table, word for word);
- `RULES`: the transition table. A transition needs an event that supports it; time never moves a storyline;
  a closing transition (into a terminal state) needs an exact-key link, else it stays a proposal;
- `PLACEMENTS`: the playbook rules that place the owner's next actions at events, checks included
  (a refund granted places "check the refund arrived"; a parcel delivered places "check the item is right");
- `action_status`: the task-versus-proposal rule.

The engine in code replays a storyline's events through these tables. The model only ever produces events.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

Playbook = Literal[
    "marketplace_sale", "purchase_delivery", "refund_support", "booking_appointment", "conversation_request"
]
PLAYBOOKS: tuple[Playbook, ...] = (
    "marketplace_sale",
    "purchase_delivery",
    "refund_support",
    "booking_appointment",
    "conversation_request",
)

STATES: dict[str, tuple[str, ...]] = {
    "marketplace_sale": ("sold", "label", "handed_to_courier", "delivered", "cancelled", "returned"),
    "purchase_delivery": ("ordered", "shipped", "delivered", "cancelled"),
    "refund_support": ("asked", "acknowledged", "info_requested", "granted", "refused"),
    "booking_appointment": ("booked", "rescheduled", "cancelled", "completed"),
    "conversation_request": ("requested", "promised", "answered", "resolved"),
}
TERMINAL: dict[str, frozenset[str]] = {
    "marketplace_sale": frozenset({"delivered", "cancelled", "returned"}),
    "purchase_delivery": frozenset({"delivered", "cancelled"}),
    "refund_support": frozenset({"granted", "refused"}),
    "booking_appointment": frozenset({"cancelled", "completed"}),
    "conversation_request": frozenset({"resolved"}),
}
ALL_TERMINAL: frozenset[str] = frozenset().union(*TERMINAL.values())

# EMAIL-UNDERSTANDING's kinds, and the ones the eval assumed besides (label_created, acknowledged,
# refund_refused, paid, returned, attended).
EVENT_KINDS: tuple[str, ...] = (
    "order_placed", "shipped", "picked_up", "delivered", "cancelled", "refund_requested", "refund_granted",
    "appointment", "request_to_owner", "owner_promise", "owner_replied",
    "label_created", "acknowledged", "refund_refused", "paid", "returned", "attended",
)  # fmt: skip
ACTORS = ("owner", "other")
DEBTORS = ("owner", "other", "none")

NEXT_ACTIONS = ("do", "reply", "check", "decide", "prepare", "wait", "none")
ACTION_KINDS = ("do", "reply", "check", "decide", "prepare")  # `wait` and `none` are not actions to place
WAITING_ON = ("courier", "merchant", "buyer", "venue", "person", "none")
EFFECT_KINDS = ("money", "time", "places", "people", "other_storylines", "documents", "risk")

ACTION_KEYS: dict[str, tuple[str, ...]] = {
    "do": ("ship", "send_info", "pay", "attend", "relist_item", "keep_receipt"),
    "reply": ("reply", "confirm"),
    "check": ("refund_arrived", "parcel_delivered", "item_ok", "payout_received", "payment_cleared"),
    "prepare": ("bring_documents",),
    "decide": (),
}
EFFECT_KEYS: dict[str, tuple[str, ...]] = {
    "money": (
        "payout_pending", "payout_due", "payment_void", "no_charge", "refund_incoming", "refund_denied",
        "payment_deadline",
    ),
    "time": (
        "ship_deadline", "return_window", "response_deadline", "appointment", "appointment_moved",
        "slot_freed", "payment_deadline",
    ),
    "places": ("venue",),
    "people": (
        "waiting_on_courier", "waiting_on_merchant", "waiting_on_person", "buyer_waiting_on_owner",
        "merchant_waiting_on_owner", "venue_waiting_on_owner", "person_waiting_on_owner", "promise_made",
    ),
    "other_storylines": ("item_free_to_relist", "dispute_closed"),
    "documents": ("sale_record", "keep_receipt"),
    "risk": ("injection_attempt", "suspicious_sender"),
}  # fmt: skip

# Which event kinds may cause a transition INTO a state (the eval's `SUPPORT`, word for word).
SUPPORT: dict[str, frozenset[str]] = {
    "sold": frozenset({"order_placed"}),
    "label": frozenset({"label_created"}),
    "handed_to_courier": frozenset({"picked_up", "shipped"}),
    "delivered": frozenset({"delivered"}),
    "cancelled": frozenset({"cancelled"}),
    "returned": frozenset({"returned"}),
    "ordered": frozenset({"order_placed"}),
    "shipped": frozenset({"shipped"}),
    "asked": frozenset({"refund_requested"}),
    "acknowledged": frozenset({"acknowledged"}),
    "info_requested": frozenset({"request_to_owner"}),
    "granted": frozenset({"refund_granted"}),
    "refused": frozenset({"refund_refused"}),
    "booked": frozenset({"appointment"}),
    "rescheduled": frozenset({"appointment"}),
    "completed": frozenset({"attended"}),
    "requested": frozenset({"request_to_owner"}),
    "promised": frozenset({"owner_promise"}),
    "answered": frozenset({"owner_replied"}),
    "resolved": frozenset({"acknowledged"}),
}

# The events that may open a storyline. Everything else (a label, a courier's scan, a cancellation, a shop's
# reply) only ever joins one that a hard or structural link found: a courier's mail alone is no storyline.
START_EVENTS: dict[str, frozenset[str]] = {
    "marketplace_sale": frozenset({"order_placed"}),
    "purchase_delivery": frozenset({"order_placed"}),
    "refund_support": frozenset({"refund_requested"}),
    "booking_appointment": frozenset({"appointment"}),
    "conversation_request": frozenset({"request_to_owner", "owner_promise"}),
}
# The events each playbook takes in (as a state change or as context for its next action).
ACCEPTS: dict[str, frozenset[str]] = {
    "marketplace_sale": frozenset(
        {"order_placed", "label_created", "picked_up", "shipped", "delivered", "cancelled", "returned"}
    ),
    "purchase_delivery": frozenset(
        {"order_placed", "shipped", "delivered", "cancelled", "request_to_owner", "paid"}
    ),
    "refund_support": frozenset(
        {"refund_requested", "acknowledged", "request_to_owner", "refund_granted", "refund_refused",
         "owner_replied"}
    ),
    "booking_appointment": frozenset(
        {"appointment", "cancelled", "attended", "request_to_owner", "owner_replied", "acknowledged"}
    ),
    "conversation_request": frozenset({"request_to_owner", "owner_promise", "owner_replied", "acknowledged"}),
}  # fmt: skip


# ---- the transition rules ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    playbook: str
    event: str
    to: str
    # the states it may come from; None in the set stands for "no state yet" (the storyline's first event)
    sources: frozenset[str | None]
    actor: Literal["any", "owner", "other"] = "any"


def _from(*states: str | None) -> frozenset[str | None]:
    return frozenset(states)


_NON_FINAL_SALE = ("sold", "label", "handed_to_courier")
RULES: tuple[Rule, ...] = (
    # marketplace_sale: sold, label, handed to the courier, delivered; cancelled and returned branches
    Rule("marketplace_sale", "order_placed", "sold", _from(None)),
    Rule("marketplace_sale", "label_created", "label", _from("sold")),
    Rule("marketplace_sale", "picked_up", "handed_to_courier", _from("sold", "label")),
    Rule("marketplace_sale", "shipped", "handed_to_courier", _from("sold", "label")),
    Rule("marketplace_sale", "delivered", "delivered", _from(*_NON_FINAL_SALE)),
    Rule("marketplace_sale", "cancelled", "cancelled", _from(*_NON_FINAL_SALE)),
    Rule("marketplace_sale", "returned", "returned", _from("handed_to_courier", "delivered")),
    # purchase_delivery
    Rule("purchase_delivery", "order_placed", "ordered", _from(None)),
    Rule("purchase_delivery", "shipped", "shipped", _from("ordered")),
    Rule("purchase_delivery", "delivered", "delivered", _from("ordered", "shipped")),
    Rule("purchase_delivery", "cancelled", "cancelled", _from("ordered", "shipped")),
    # refund_support: asked, acknowledged, info requested, granted or refused
    Rule("refund_support", "refund_requested", "asked", _from(None)),
    Rule("refund_support", "acknowledged", "acknowledged", _from("asked", "info_requested"), "other"),
    Rule("refund_support", "request_to_owner", "info_requested", _from("asked", "acknowledged"), "other"),
    Rule("refund_support", "refund_granted", "granted", _from("asked", "acknowledged", "info_requested")),
    Rule("refund_support", "refund_refused", "refused", _from("asked", "acknowledged", "info_requested")),
    # booking_appointment (a second `appointment` is a move only when it changes the time or the key)
    Rule("booking_appointment", "appointment", "booked", _from(None)),
    Rule("booking_appointment", "appointment", "rescheduled", _from("booked", "rescheduled")),
    Rule("booking_appointment", "cancelled", "cancelled", _from("booked", "rescheduled")),
    Rule("booking_appointment", "attended", "completed", _from("booked", "rescheduled")),
    # conversation_request. A counterpart's thanks resolves it only after an answer; his own closing word
    # ("done", "risolto") resolves it from any open state.
    Rule(
        "conversation_request", "request_to_owner", "requested",
        _from(None, "requested", "promised", "answered"), "other",
    ),
    Rule("conversation_request", "owner_promise", "promised", _from(None, "requested"), "owner"),
    Rule("conversation_request", "owner_replied", "answered", _from("requested", "promised"), "owner"),
    Rule("conversation_request", "acknowledged", "resolved", _from("answered"), "other"),
    Rule(
        "conversation_request", "acknowledged", "resolved",
        _from("requested", "promised", "answered"), "owner",
    ),
)  # fmt: skip
# Playbooks with no key of their own: their identity is the thread, which then counts as an exact link.
KEYLESS = frozenset({"conversation_request"})


def is_terminal(state: str | None) -> bool:
    return state in ALL_TERMINAL


@dataclass(frozen=True)
class Ev:
    """An event as the engine reads it. `at` is the message's own date: the replay order (never arrival)."""

    id: str
    kind: str
    at: datetime
    actor: str = "other"
    debtor: str = "none"
    hard: bool = False  # the message is linked to the storyline by an exact key (or the thread, KEYLESS)
    when: datetime | None = None  # the event's own time (an appointment's start)
    new_key: bool = False  # the message names a key the storyline did not hold
    injected: bool = False  # the message carries an instruction aimed at an assistant: never closes anything


@dataclass(frozen=True)
class Step:
    event_id: str
    event: str
    before: str | None
    after: str


@dataclass(frozen=True)
class Held:
    """A transition the rules refused to make by themselves: it stays a proposal for the owner."""

    event_id: str
    event: str
    to: str
    why: Literal["no_exact_key", "injected"]


@dataclass
class Replay:
    playbook: str
    state: str | None = None
    steps: list[Step] = field(default_factory=list)
    held: list[Held] = field(default_factory=list)
    # the owner's open request (the id of the request_to_owner event, with debtor owner), None when settled
    request_open: str | None = None
    paid: bool = False  # the owner said he paid (a `paid` event), so the payment is no longer his to make
    last_at: datetime | None = None
    last_when: datetime | None = None
    seen: list[str] = field(default_factory=list)  # every event id replayed, in order


def _rule_for(playbook: str, ev: Ev, state: str | None, replay: Replay) -> Rule | None:
    for rule in RULES:
        if rule.playbook != playbook or rule.event != ev.kind or state not in rule.sources:
            continue
        if rule.actor != "any" and ev.actor != rule.actor:
            continue
        if rule.to == "rescheduled" and not (
            ev.new_key
            or (ev.when is not None and replay.last_when is not None and ev.when != replay.last_when)
        ):
            continue  # a reminder repeats the appointment; only a changed time or a new key moves it
        return rule
    return None


def replay(playbook: str, events: Sequence[Ev]) -> Replay:
    """The storyline's state from its events in date order. A step needs a rule whose event supports the
    state (`SUPPORT`); a closing step also needs an exact-key link, else it is held as a proposal; nothing
    here reads the clock."""
    out = Replay(playbook)
    for ev in sorted(events, key=lambda e: (e.at, e.id)):
        out.seen.append(ev.id)
        out.last_at = ev.at
        if ev.kind == "request_to_owner" and ev.debtor == "owner":
            out.request_open = ev.id
        elif ev.kind == "paid":
            out.paid, out.request_open = True, None
        elif ev.kind == "owner_replied" and ev.actor == "owner":
            out.request_open = None  # he answered what was asked
        rule = _rule_for(playbook, ev, out.state, out)
        if ev.kind == "appointment" and ev.when is not None:
            out.last_when = ev.when
        if rule is None or ev.kind not in SUPPORT[rule.to]:
            continue  # no rule moves this event here (the table's own guard: never beyond `SUPPORT`)
        if is_terminal(out.state) and not (playbook == "marketplace_sale" and rule.to == "returned"):
            continue  # a closed storyline is not moved again (a new request is a new storyline)
        if rule.to == out.state and playbook != "booking_appointment":
            continue  # the same step again: evidence, no transition
        if rule.to in TERMINAL[playbook] and (not ev.hard or ev.injected):
            out.held.append(Held(ev.id, ev.kind, rule.to, "injected" if ev.injected else "no_exact_key"))
            continue
        out.steps.append(Step(ev.id, ev.kind, out.state, rule.to))
        out.state = rule.to
    return out


# ---- the next action, the checks and the effects -----------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """A playbook rule: at this state (and condition), the owner's action `kind`/`key` is placed."""

    kind: str
    key: str
    source: Literal["asked", "playbook", "effect", "later"]
    when: Literal["always", "request_open", "no_request"] = "always"
    after_days: int = 0  # a check that waits (the money takes days to arrive): not before this many days
    within_days: int = 0  # a check that has a window (the return window): done before it closes


PLACEMENTS: dict[tuple[str, str], tuple[Placement, ...]] = {
    ("marketplace_sale", "sold"): (Placement("do", "ship", "asked"),),
    ("marketplace_sale", "label"): (Placement("do", "ship", "asked"),),
    ("marketplace_sale", "handed_to_courier"): (Placement("check", "parcel_delivered", "later"),),
    ("marketplace_sale", "delivered"): (Placement("check", "payout_received", "playbook", after_days=5),),
    ("marketplace_sale", "cancelled"): (Placement("do", "relist_item", "effect"),),
    ("purchase_delivery", "ordered"): (Placement("do", "pay", "asked", "request_open"),),
    ("purchase_delivery", "shipped"): (Placement("check", "parcel_delivered", "later"),),
    ("purchase_delivery", "delivered"): (
        Placement("check", "item_ok", "playbook", within_days=14),
        Placement("do", "keep_receipt", "effect"),
    ),
    ("refund_support", "info_requested"): (Placement("do", "send_info", "asked", "request_open"),),
    ("refund_support", "granted"): (Placement("check", "refund_arrived", "playbook", after_days=7),),
    ("booking_appointment", "booked"): (
        Placement("reply", "confirm", "asked", "request_open"),
        Placement("do", "attend", "playbook", "no_request"),
        Placement("prepare", "bring_documents", "effect", "no_request"),
    ),
    ("booking_appointment", "rescheduled"): (
        Placement("reply", "confirm", "asked", "request_open"),
        Placement("do", "attend", "playbook", "no_request"),
    ),
    ("conversation_request", "requested"): (Placement("reply", "reply", "asked"),),
    ("conversation_request", "promised"): (Placement("reply", "reply", "playbook"),),
}
# A person who is asked something answers it, so the owner's own request is read from `request_open`.

_WAITING: dict[tuple[str, str], str] = {
    ("marketplace_sale", "handed_to_courier"): "courier",
    ("purchase_delivery", "ordered"): "merchant",
    ("purchase_delivery", "shipped"): "courier",
    ("refund_support", "asked"): "merchant",
    ("refund_support", "acknowledged"): "merchant",
    ("refund_support", "info_requested"): "merchant",
    ("conversation_request", "answered"): "person",
}

# The tasks a playbook or a request may make: the rest is a proposal, whoever suggested it.
TASK_KEYS = frozenset(
    {
        ("do", "ship"), ("do", "send_info"), ("do", "pay"), ("do", "attend"),
        ("reply", "reply"), ("reply", "confirm"),
        ("check", "refund_arrived"), ("check", "item_ok"), ("check", "payout_received"),
    }
)  # fmt: skip


def action_status(kind: str, key: str, source: str) -> Literal["task", "proposal"]:
    """The task-versus-proposal rule. An action is a `task` when a message in the storyline asks for it, or
    a playbook rule places it at an event, and it is one of the actions a task may be. Everything that comes
    only from an effect (relist the item, keep the receipt, bring documents), anything the model suggested as
    "what a person would normally do", and a check that belongs to a later moment ("parcel delivered?" while
    it is in transit) is a proposal."""
    if source in ("asked", "playbook") and (kind, key) in TASK_KEYS:
        return "task"
    return "proposal"


@dataclass(frozen=True)
class Action:
    kind: str
    key: str
    status: Literal["task", "proposal"]
    evidence: str | None  # the id of the event it rests on
    after: datetime | None = None  # a check that waits: not before this day
    before: datetime | None = None  # a check with a window: before it closes


@dataclass(frozen=True)
class Plan:
    state: str | None
    next_action: str
    waiting_on: str
    actions: tuple[Action, ...]
    effects: tuple[tuple[str, str], ...]


_SELLING = [("money", "payout_pending"), ("time", "ship_deadline"), ("people", "buyer_waiting_on_owner")]


def _effects(
    playbook: str, state: str | None, *, confirm: bool, send_info: bool, pay: bool
) -> list[tuple[str, str]]:
    table: dict[str, dict[str, list[tuple[str, str]]]] = {
        "marketplace_sale": {
            "sold": [
                ("money", "payout_pending"),
                ("time", "ship_deadline"),
                ("people", "buyer_waiting_on_owner"),
            ],
            "label": [
                ("money", "payout_pending"),
                ("time", "ship_deadline"),
                ("people", "buyer_waiting_on_owner"),
            ],
            "handed_to_courier": [("money", "payout_pending"), ("people", "waiting_on_courier")],
            "delivered": [("money", "payout_due"), ("documents", "sale_record")],
            "cancelled": [("other_storylines", "item_free_to_relist"), ("money", "payment_void")],
        },
        "purchase_delivery": {
            "ordered": [("people", "waiting_on_merchant")],
            "shipped": [("people", "waiting_on_courier")],
            "delivered": [("time", "return_window"), ("documents", "keep_receipt")],
            "cancelled": [("money", "no_charge")],
        },
        "refund_support": {
            "asked": [("people", "waiting_on_merchant")],
            "acknowledged": [("people", "waiting_on_merchant"), ("time", "response_deadline")],
            "info_requested": [("people", "merchant_waiting_on_owner")]
            if send_info
            else [("people", "waiting_on_merchant"), ("time", "response_deadline")],
            "granted": [("money", "refund_incoming"), ("other_storylines", "dispute_closed")],
            "refused": [("money", "refund_denied"), ("other_storylines", "dispute_closed")],
        },
        "booking_appointment": {
            "booked": [("time", "appointment"), ("places", "venue")]
            + ([("people", "venue_waiting_on_owner")] if confirm else []),
            "rescheduled": [("time", "appointment_moved"), ("places", "venue")],
            "cancelled": [("time", "slot_freed")],
        },
        "conversation_request": {
            "requested": [("people", "person_waiting_on_owner")],
            "promised": [("people", "promise_made")],
            "answered": [("people", "waiting_on_person")],
            "resolved": [],
        },
    }
    out = list(table.get(playbook, {}).get(state or "", []))
    if playbook == "purchase_delivery" and state == "ordered" and pay:
        out += [("money", "payment_deadline"), ("time", "payment_deadline")]
    return out


def plan(rep: Replay, events: Sequence[Ev]) -> Plan:
    """The owner's next move, from the replayed state: the actions the playbook places (checks included),
    who the storyline waits on, and what the state does to his life. Pure: no clock (a check that waits says
    "not before"; nobody closes anything by time)."""
    state = rep.state
    by_id = {e.id: e for e in events}
    last = by_id.get(rep.steps[-1].event_id) if rep.steps else None
    open_request = rep.request_open is not None
    actions: list[Action] = []
    for p in PLACEMENTS.get((rep.playbook, state or ""), ()):
        if p.when == "request_open" and not open_request:
            continue
        if p.when == "no_request" and open_request:
            continue
        evidence = rep.request_open if p.when == "request_open" else (last.id if last else None)
        ref = by_id.get(evidence or "")
        after = ref.at + timedelta(days=p.after_days) if ref and p.after_days else None
        before = ref.at + timedelta(days=p.within_days) if ref and p.within_days else None
        actions.append(Action(p.kind, p.key, action_status(p.kind, p.key, p.source), evidence, after, before))
    if rep.paid and rep.playbook == "purchase_delivery" and state == "ordered":
        paid = next((e for e in sorted(events, key=lambda e: e.at) if e.kind == "paid"), None)
        actions.append(Action("check", "payment_cleared", "proposal", paid.id if paid else None))
    owner_tasks = [a for a in actions if a.status == "task"]
    waiting = "none" if owner_tasks else _WAITING.get((rep.playbook, state or ""), "none")
    if state is None:
        next_action = "none"
    elif owner_tasks:
        order = {"do": 0, "reply": 1, "decide": 2, "check": 3}
        next_action = min(owner_tasks, key=lambda a: order.get(a.kind, 9)).kind
    else:
        next_action = "wait" if waiting != "none" else "none"
    effects = _effects(
        rep.playbook,
        state,
        confirm=any(a.key == "confirm" and a.status == "task" for a in actions),
        send_info=any(a.key == "send_info" and a.status == "task" for a in actions),
        pay=any(a.key == "pay" and a.status == "task" for a in actions),
    )
    return Plan(state, next_action, waiting, tuple(actions), tuple(effects))


# ---- the coarse label for mail a storyline does not (yet) explain ------------------------------------------

_DELIVERY = frozenset({"shipped", "picked_up", "delivered", "label_created", "returned"})


def mail_category(kinds: Sequence[str]) -> str:
    """One coarse label for a message from the event kinds it carries: what the quiet inbox, the briefing and
    the kind rules group mail by. A message with no event is `info`."""
    found = set(kinds)
    if "request_to_owner" in found:
        return "action_required"
    if "appointment" in found:
        return "event_or_booking"
    if found & _DELIVERY:
        return "delivery"
    if found - {"owner_replied", "owner_promise", "acknowledged", "paid"}:
        return "notification"
    return "info"


# ---- the storyline's own words about itself, for tasks ----------------------------------------------------

TITLES: dict[str, tuple[str, str]] = {  # action key: (English, Italian); {who} is the counterpart
    "ship": ("Ship the parcel", "Spedire il pacco"),
    "send_info": ("Send the information they asked for", "Inviare le informazioni richieste"),
    "pay": ("Pay the order", "Pagare l'ordine"),
    "attend": ("Go to the appointment", "Andare all'appuntamento"),
    "reply": ("Reply to {who}", "Rispondere a {who}"),
    "confirm": ("Confirm the appointment", "Confermare l'appuntamento"),
    "refund_arrived": ("Check the refund has arrived", "Verificare che il rimborso sia arrivato"),
    "item_ok": ("Check the item is as expected", "Verificare che l'articolo sia a posto"),
    "payout_received": ("Check the payment has arrived", "Verificare che il pagamento sia arrivato"),
}
TITLE_REF = ("{title} ({ref})", "{title} ({ref})")


def task_title(key: str, *, who: str, ref: str | None, italian: bool) -> str:
    """The title of a task a playbook placed. `who`: the counterpart's name; `ref`: the storyline's own key
    as the mail wrote it (an order number), when it has one."""
    en, it = TITLES[key]
    title = (it if italian else en).format(who=who or ("la persona" if italian else "them"))
    return TITLE_REF[0].format(title=title, ref=ref) if ref else title
