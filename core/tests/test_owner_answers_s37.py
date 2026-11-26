"""Release audit §37 (v0.1.6, storylines on a real model): the owner's own replies were never his answer.

Of 28 messages the owner wrote in reply, the model called none `owner_replied`: 13 got no event, 8
`acknowledged` (two closed a request he had only answered: false_close), 6 `appointment` (a booking with an
"attend" task out of his "that day works"), 1 `refund_requested`. The request stayed open with its reply task
(invented_work 27). Code knows who wrote the message and that it answers someone: the gate says so, whatever
the model read. Also: a shop's "thanks for writing" in the message that asks for a photo is no
acknowledgement, and a sale's "ship it in 3 days" opens no conversation beside the sale.

Scripted model only, synthetic data.
"""

from typing import Any

from pisa.domain import mail_events
from tests.mail_world import OWNER, EventsModel, add_mail, ev, google_account, rows, run_intake, task_rows
from tests.test_storylines import MARKET, SOLD, SOLD_EVENT, SOLD_TEXT, story

ANSWER = "Friday at 10 is fine for me."


def _kinds(found: mail_events.Understood) -> list[str]:
    return [e.kind for e in found.events]


def _validate(
    events: list[dict[str, Any]], text: str, *, owner: bool, replying: bool
) -> mail_events.Understood:
    return mail_events.validate(
        {"events": events, "effects": []},
        text=text,
        subject="Re: visit",
        owner_authored=owner,
        replying=replying,
    )


def test_an_owner_s_acknowledgement_is_his_reply() -> None:
    found = _validate([ev("acknowledged", ANSWER, actor="owner")], ANSWER, owner=True, replying=True)
    assert _kinds(found) == ["owner_replied"] and found.events[0].debtor == "other"
    # in a message he starts too: acknowledged is the other side's word, never his
    found = _validate([ev("acknowledged", ANSWER, actor="owner")], ANSWER, owner=True, replying=False)
    assert _kinds(found) == ["owner_replied"]


def test_a_time_the_owner_accepts_in_a_reply_is_his_answer_not_a_booking() -> None:
    raw = [ev("appointment", ANSWER, actor="owner", playbook="booking_appointment", at="2026-12-04T10:00")]
    found = _validate(raw, ANSWER, owner=True, replying=True)
    assert _kinds(found) == ["owner_replied"]
    assert found.events[0].playbook is None and found.events[0].occurred_at is None
    # a message he starts may book (his own "I booked the hall for Friday at 10")
    assert _kinds(_validate(raw, ANSWER, owner=True, replying=False)) == ["appointment"]


def test_an_owner_s_reply_with_no_answer_read_is_still_his_answer() -> None:
    text = "The signed contract is attached.\nBest, Ada"
    found = _validate([], text, owner=True, replying=True)
    assert _kinds(found) == ["owner_replied"] and found.events[0].quote == "The signed contract is attached."
    # a reply that promises, nothing read: his promise, so what he owes stays his (never "answered")
    for promised in ("I will send you the figures by Friday.", "Ti mando i conti entro venerdì.",
                     "Ti richiamerò lunedì."):  # fmt: skip
        found = _validate([], promised + "\nAda", owner=True, replying=True)
        assert _kinds(found) == ["owner_promise"] and found.events[0].quote == promised, promised
    # a promise he made stays a promise, and two answers are one
    promise = ev("owner_promise", "I'll call you back on Monday.", actor="owner")
    text = "I'll call you back on Monday."
    assert _kinds(_validate([promise], text, owner=True, replying=True)) == ["owner_promise"]
    twice = [ev("owner_replied", ANSWER, actor="owner"), ev("acknowledged", ANSWER, actor="owner")]
    assert _kinds(_validate(twice, ANSWER, owner=True, replying=True)) == ["owner_replied"]
    # not a reply (nobody else wrote in the thread), nothing read: nothing made up
    assert _kinds(_validate([], text, owner=True, replying=False)) == []
    # a message someone else wrote: no answer of the owner is ever added
    assert _kinds(_validate([], text, owner=False, replying=True)) == []


def test_a_thanks_for_writing_in_a_message_that_asks_is_no_acknowledgement() -> None:
    text = "Thanks for getting in touch. To proceed we need a photo of the damaged item."
    raw = [
        ev("acknowledged", "Thanks for getting in touch."),
        ev("request_to_owner", "To proceed we need a photo of the damaged item.", debtor="owner"),
    ]
    assert _kinds(_validate(raw, text, owner=False, replying=False)) == ["request_to_owner"]
    # alone, the other side's acknowledgement stays
    raw = [ev("acknowledged", "Received, all sorted.")]
    assert _kinds(_validate(raw, "Received, all sorted.", owner=False, replying=False)) == ["acknowledged"]


def test_an_answered_request_is_no_task_and_no_booking(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    google_account(db)
    ask = "Which day works for the visit: Thursday or Friday?"
    add_mail(
        db, "c1", sender="anna.berti@example.com", subject="Visit", minutes=0, name="Anna Berti", text=ask
    )
    add_mail(
        db, "c2", sender=OWNER, subject="Re: Visit", thread="t-c1", minutes=60, owner=True, name="Ada Demo",
        text=ANSWER, quoted=f"> {ask}",
    )  # fmt: skip
    model = EventsModel()
    model.say("Visit", [ev("request_to_owner", ask, debtor="owner", playbook="conversation_request")])
    # the model reads his answer as a booking
    model.say("Re: Visit", [ev("appointment", ANSWER, actor="owner", playbook="booking_appointment")])
    run_intake(db, secrets_key, model)
    assert rows(db, "SELECT kind FROM mail_event ORDER BY kind") == [
        ("owner_replied",),
        ("request_to_owner",),
    ]
    assert rows(db, "SELECT kind FROM storyline") == [("conversation_request",)]
    assert story(db)[:2] == ("conversation_request", "waiting")
    assert [t for t in task_rows(db) if t[1] == "open"] == []


def test_a_sale_s_ship_it_opens_no_conversation(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    google_account(db)
    add_mail(db, "s1", sender=MARKET, subject=SOLD, minutes=0, name="Mercatino Nova", text=SOLD_TEXT)
    model = EventsModel()
    model.say(SOLD, [SOLD_EVENT, ev("request_to_owner", "Please ship it within 3 days.", debtor="owner")])
    run_intake(db, secrets_key, model)
    assert rows(db, "SELECT kind FROM storyline") == [("marketplace_sale",)]
