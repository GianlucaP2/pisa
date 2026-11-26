"""The playbooks, as pure data and rules: the eval's vocabulary, the transition table, the checks the
playbooks place, the task-versus-proposal rule and the task state machine. No database, no model."""

from datetime import UTC, datetime, timedelta

import pytest

from pisa.domain import playbooks as pb
from pisa.domain import task_machine
from pisa.domain.playbooks import Ev, plan, replay

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def e(
    id: str, kind: str, minutes: int, *, actor: str = "other", debtor: str = "none", hard: bool = True, **more
):  # type: ignore[no-untyped-def]
    return Ev(id, kind, T0 + timedelta(minutes=minutes), actor, debtor, hard, **more)


def run_plan(playbook: str, *events: Ev):  # type: ignore[no-untyped-def]
    rep = replay(playbook, list(events))
    return rep, plan(rep, list(events))


def actions(p: pb.Plan) -> set[tuple[str, str, str]]:
    return {(a.kind, a.key, a.status) for a in p.actions}


def test_the_vocabulary_is_the_evals() -> None:
    assert set(pb.PLAYBOOKS) == set(pb.STATES) == set(pb.TERMINAL)
    assert set(pb.EVENT_KINDS) >= {
        "order_placed",
        "owner_replied",
        "label_created",
        "acknowledged",
        "refund_refused",
    }
    assert {"paid", "returned", "attended"} <= set(pb.EVENT_KINDS)
    assert pb.NEXT_ACTIONS == ("do", "reply", "check", "decide", "prepare", "wait", "none")
    assert pb.WAITING_ON == ("courier", "merchant", "buyer", "venue", "person", "none")
    assert pb.EFFECT_KINDS == ("money", "time", "places", "people", "other_storylines", "documents", "risk")
    assert pb.ACTION_KEYS["check"] == (
        "refund_arrived",
        "parcel_delivered",
        "item_ok",
        "payout_received",
        "payment_cleared",
    )
    assert pb.EFFECT_KEYS["risk"] == ("injection_attempt", "suspicious_sender")
    # every state of every playbook has a supporting event, and every rule respects the table
    assert all(pb.SUPPORT[s] for states in pb.STATES.values() for s in states)
    for rule in pb.RULES:
        assert rule.event in pb.SUPPORT[rule.to] and rule.to in pb.STATES[rule.playbook]


def test_a_transition_needs_a_supporting_event() -> None:
    # a delivered mail cannot open a conversation or move a booking: no rule, no step
    rep, _ = run_plan("booking_appointment", e("1", "appointment", 0), e("2", "delivered", 5))
    assert rep.state == "booked" and [s.event for s in rep.steps] == ["appointment"]
    rep, _ = run_plan("refund_support", e("1", "refund_requested", 0), e("2", "shipped", 5))
    assert rep.state == "asked"


def test_a_closing_needs_an_exact_key_else_it_is_held() -> None:
    soft = e("2", "cancelled", 5, hard=False)
    rep, p = run_plan("marketplace_sale", e("1", "order_placed", 0), soft)
    assert (
        rep.state == "sold"
        and [h.to for h in rep.held] == ["cancelled"]
        and rep.held[0].why == "no_exact_key"
    )
    assert ("do", "ship", "task") in actions(p)  # nothing was closed: the owner still has to ship
    # an instruction-carrying message never closes, even with the key
    rep, _ = run_plan("marketplace_sale", e("1", "order_placed", 0), e("2", "cancelled", 5, injected=True))
    assert rep.state == "sold" and rep.held[0].why == "injected"
    # with the key it closes
    rep, p = run_plan("marketplace_sale", e("1", "order_placed", 0), e("2", "cancelled", 5))
    assert (
        rep.state == "cancelled"
        and p.next_action == "none"
        and actions(p) == {("do", "relist_item", "proposal")}
    )


def test_no_close_by_time() -> None:
    # the replay and the plan take no clock: a storyline a year old is exactly as open as a new one
    old = e("1", "order_placed", -60 * 24 * 400)
    rep, p = run_plan("marketplace_sale", old)
    assert rep.state == "sold" and not rep.held and p.next_action == "do"
    # the task machine knows no reason that is a time
    assert not {r for r in task_machine._TABLE} & {"expired", "timeout", "overdue", "stale", "aged"}
    assert task_machine.next_state("waiting", "overdue", hard=True, by_owner=True) is None
    assert task_machine.next_state("open", "expired", hard=True, by_owner=True) is None


def test_playbook_checks() -> None:
    # "refund granted" places check refund_arrived (a task, not before the money can arrive)
    rep, p = run_plan("refund_support", e("1", "refund_requested", 0), e("2", "refund_granted", 60))
    assert rep.state == "granted" and p.next_action == "check" and p.waiting_on == "none"
    check = next(a for a in p.actions if a.key == "refund_arrived")
    assert (check.kind, check.status) == ("check", "task") and check.after == T0 + timedelta(
        minutes=60, days=7
    )
    # "delivered" places check item_ok before the return window closes, and the receipt as a proposal
    rep, p = run_plan(
        "purchase_delivery", e("1", "order_placed", 0), e("2", "shipped", 60), e("3", "delivered", 120)
    )
    assert rep.state == "delivered" and p.next_action == "check"
    item = next(a for a in p.actions if a.key == "item_ok")
    assert item.status == "task" and item.before == T0 + timedelta(minutes=120, days=14)
    assert ("do", "keep_receipt", "proposal") in actions(p)
    # a sale delivered: check the payout
    _, p = run_plan(
        "marketplace_sale",
        e("1", "order_placed", 0),
        e("2", "label_created", 5),
        e("3", "shipped", 10),
        e("4", "delivered", 20),
    )
    assert ("check", "payout_received", "task") in actions(p)
    # a check that belongs to a later moment (the parcel is still on its way) is a proposal
    _, p = run_plan("purchase_delivery", e("1", "order_placed", 0), e("2", "shipped", 60))
    assert p.next_action == "wait" and p.waiting_on == "courier"
    assert actions(p) == {("check", "parcel_delivered", "proposal")}


def test_the_task_versus_proposal_rule() -> None:
    assert pb.action_status("do", "ship", "asked") == "task"
    assert pb.action_status("check", "item_ok", "playbook") == "task"
    # anything only an effect or the model suggested is a proposal, whatever its key
    assert pb.action_status("do", "ship", "effect") == "proposal"
    assert pb.action_status("do", "relist_item", "playbook") == "proposal"
    assert pb.action_status("do", "keep_receipt", "asked") == "proposal"
    assert pb.action_status("prepare", "bring_documents", "playbook") == "proposal"
    assert pb.action_status("check", "parcel_delivered", "later") == "proposal"


@pytest.mark.parametrize(
    ("playbook", "events", "state", "next_action", "waiting_on", "tasks", "proposals"),
    [
        ("marketplace_sale", ["order_placed"], "sold", "do", "none", {"ship"}, set()),
        (
            "marketplace_sale",
            ["order_placed", "label_created", "shipped"],
            "handed_to_courier",
            "wait",
            "courier",
            set(),
            {"parcel_delivered"},
        ),
        ("purchase_delivery", ["order_placed"], "ordered", "wait", "merchant", set(), set()),
        ("purchase_delivery", ["order_placed", "cancelled"], "cancelled", "none", "none", set(), set()),
        (
            "refund_support",
            ["refund_requested", "acknowledged"],
            "acknowledged",
            "wait",
            "merchant",
            set(),
            set(),
        ),
        ("refund_support", ["refund_requested", "refund_refused"], "refused", "none", "none", set(), set()),
        ("booking_appointment", ["appointment"], "booked", "do", "none", {"attend"}, {"bring_documents"}),
        ("booking_appointment", ["appointment", "attended"], "completed", "none", "none", set(), set()),
        ("conversation_request", ["request_to_owner"], "requested", "reply", "none", {"reply"}, set()),
    ],
)
def test_the_playbooks_place_what_the_eval_expects(  # type: ignore[no-untyped-def]
    playbook, events, state, next_action, waiting_on, tasks, proposals
) -> None:
    evs = [
        e(str(i), kind, i * 5, debtor="owner" if kind == "request_to_owner" else "none")
        for i, kind in enumerate(events)
    ]
    rep, p = run_plan(playbook, *evs)
    assert (rep.state, p.next_action, p.waiting_on) == (state, next_action, waiting_on)
    assert {a.key for a in p.actions if a.status == "task"} == tasks
    assert {a.key for a in p.actions if a.status == "proposal"} == proposals


def test_a_request_to_the_owner_is_his_until_he_answers() -> None:
    ask = e("2", "request_to_owner", 5, debtor="owner")
    rep, p = run_plan("refund_support", e("1", "refund_requested", 0), ask)
    assert (
        rep.state == "info_requested" and p.next_action == "do" and ("do", "send_info", "task") in actions(p)
    )
    assert ("people", "merchant_waiting_on_owner") in p.effects
    reply = e("3", "owner_replied", 10, actor="owner")
    rep, p = run_plan("refund_support", e("1", "refund_requested", 0), ask, reply)
    assert rep.state == "info_requested" and (p.next_action, p.waiting_on) == ("wait", "merchant")
    assert not any(a.status == "task" for a in p.actions)
    # the shop acknowledges the photo: acknowledged, still waiting on the shop
    ack = e("4", "acknowledged", 15)
    rep, p = run_plan("refund_support", e("1", "refund_requested", 0), ask, reply, ack)
    assert rep.state == "acknowledged" and p.waiting_on == "merchant"
    # a payment request and the owner's "already paid"
    order = e("1", "order_placed", 0)
    pay = e("2", "request_to_owner", 5, debtor="owner")
    _, p = run_plan("purchase_delivery", order, pay)
    assert ("do", "pay", "task") in actions(p) and p.waiting_on == "none"
    _, p = run_plan("purchase_delivery", order, pay, e("3", "paid", 10, actor="owner"))
    assert (p.next_action, p.waiting_on) == ("wait", "merchant")
    assert ("check", "payment_cleared", "proposal") in actions(p)


def test_a_booking_moves_only_when_the_time_or_the_key_changes() -> None:
    at = T0 + timedelta(days=30)
    book = e("1", "appointment", 0, when=at)
    rep, _ = run_plan("booking_appointment", book, e("2", "appointment", 60, when=at))  # a reminder
    assert rep.state == "booked"
    rep, p = run_plan("booking_appointment", book, e("2", "appointment", 60, when=at + timedelta(days=2)))
    assert rep.state == "rescheduled" and ("do", "attend", "task") in actions(p)
    rep, _ = run_plan("booking_appointment", book, e("2", "appointment", 60, new_key=True))
    assert rep.state == "rescheduled"
    # asked to confirm: the reply is the task, the visit follows once he answered
    asked = e("2", "request_to_owner", 5, debtor="owner")
    _, p = run_plan("booking_appointment", book, asked)
    assert actions(p) == {("reply", "confirm", "task")} and ("people", "venue_waiting_on_owner") in p.effects
    _, p = run_plan("booking_appointment", book, asked, e("3", "owner_replied", 9, actor="owner"))
    assert ("do", "attend", "task") in actions(p) and ("reply", "confirm", "task") not in actions(p)


def test_a_conversation_is_resolved_only_by_a_closing_word() -> None:
    ask = e("1", "request_to_owner", 0, debtor="owner")
    answer = e("2", "owner_replied", 10, actor="owner")
    rep, p = run_plan("conversation_request", ask, answer)
    assert rep.state == "answered" and (p.next_action, p.waiting_on) == ("wait", "person")
    thanks = e("3", "acknowledged", 20)
    assert run_plan("conversation_request", ask, answer, thanks)[0].state == "resolved"
    # the counterpart's "great, I will wait" after the owner promised does not resolve it
    promise = e("2", "owner_promise", 10, actor="owner")
    rep, p = run_plan("conversation_request", ask, promise, thanks)
    assert rep.state == "promised" and ("reply", "reply", "task") in actions(p)
    # the owner's own closing word resolves from any open state
    done = e("3", "acknowledged", 20, actor="owner")
    assert run_plan("conversation_request", ask, done)[0].state == "resolved"
    # a new request after an answer is the same conversation again
    again = e("3", "request_to_owner", 20, debtor="owner")
    rep, p = run_plan("conversation_request", ask, answer, again)
    assert rep.state == "requested" and p.next_action == "reply"


def test_the_order_is_the_order_things_happened_not_the_order_they_arrived() -> None:
    chain = [
        e("a", "order_placed", 0),
        e("b", "label_created", 10),
        e("c", "shipped", 20),
        e("d", "delivered", 30),
    ]
    expected = run_plan("marketplace_sale", *chain)[0].state
    assert expected == "delivered"
    for perm in ([3, 2, 1, 0], [2, 3, 0, 1], [1, 3, 0, 2]):
        assert run_plan("marketplace_sale", *[chain[i] for i in perm])[0].state == expected


def test_the_task_state_machine() -> None:
    assert task_machine.STATES == ("open", "waiting", "done", "cancelled", "superseded", "snoozed")
    ok = task_machine.next_state
    assert ok("open", "fulfilled", hard=True) == "done"
    assert ok("waiting", "fulfilled", hard=True) == "done"
    assert ok("open", "void", hard=True) == "cancelled"
    assert ok("open", "replaced", hard=True) == "superseded"
    assert ok("open", "waits", hard=False) == "waiting"  # waiting is no closing: no key needed
    assert ok("waiting", "asked_again", hard=False) == "open"
    # closing without an exact key or the owner's word is a proposal, not a change
    assert ok("open", "fulfilled", hard=False) is None
    assert ok("open", "void", hard=False) is None
    assert ok("open", "fulfilled", hard=False, by_owner=True) == "done"
    # a closed task is not closed again; only the owner reopens one
    assert ok("done", "fulfilled", hard=True) is None
    assert (
        ok("done", "reopened", hard=False) is None
        and ok("done", "reopened", hard=False, by_owner=True) == "open"
    )
    assert task_machine.status_of("done") == "done" and task_machine.status_of("cancelled") == "dismissed"
    assert task_machine.status_of("waiting") == task_machine.status_of("open") == "open"
    assert (
        task_machine.dedupe_key("e1", "ship") == ("e1", "ship")
        and task_machine.dedupe_key(None, "ship") is None
    )


def test_a_quote_the_model_wrote_with_accents_or_curly_quotes_is_the_email_s() -> None:
    """§36 B: the email says "e pronta" (no accent), the model copies "è pronta" with a curly apostrophe: the
    same sentence, kept; a sentence the email doesn't hold still isn't."""
    from pisa.domain.mail_events import quoted_in

    text = "L'etichetta per l'ordine ORD-82689 e pronta. Il pacco TRK1472337700 e partito."
    curly = chr(0x2019)  # the model's apostrophe
    assert quoted_in(f"L{curly}etichetta per l{curly}ordine ORD-82689 \u00e8 pronta.", text)
    assert quoted_in("Il pacco TRK1472337700 è partito", text)
    assert quoted_in("Il pacco è in consegna", text) is None
