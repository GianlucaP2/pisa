"""Fixes at the merge: each fails without its fix.
When in doubt Pisa asks once: a wrong act on the user's calendar or a sent email costs more than one yes."""

import uuid
from datetime import timedelta
from typing import Any

from pisa.domain import actions as rules
from tests.test_talking_like_a_person import _queue

# --- F1: "no, …" corrects by code only when the rest is the time itself ------------------------------------


def test_a_period_word_or_another_item_goes_to_the_model() -> None:
    assert rules.corrected_time("no, alle 22") == (22, 0)
    assert rules.corrected_time("ma no, alle 21:30") == (21, 30)
    assert rules.corrected_time("no, alle 9 di sera") is None
    assert rules.corrected_time("no la palestra alle 22") is None


# --- F3: several times, or a negation, ground no move -------------------------------------------------------


def test_several_times_or_a_negation_ground_nothing() -> None:
    from datetime import date, datetime

    # `owner_said` replaced verb_times; the same probes, through the move's grounding
    today = date(2026, 10, 20)

    def mode(words: str) -> str:
        return rules.decide_mode("calendar_move", words, read_outside=False, attendees=False,
                                 start=datetime(2026, 10, 20, 21), default_day=today, today=today,
                                 title="Cena")  # fmt: skip

    assert mode("sposta la cena alle 21") == "words"
    assert mode("sposta la riunione di Marco alle 15 e la cena alle 21") == "held"
    assert mode("sposta la cena alle 21 non alle 22") == "held"
    assert mode("non spostare la cena alle 21") == "held"


# --- F4: cancel, option and send: no negation, one choice, nothing after "mandala" ------------------------


def test_cancel_option_and_send_are_unambiguous() -> None:
    assert rules.says_cancel("cancella la cena")
    assert not rules.says_cancel("non cancellare la cena, cancella il pranzo")
    assert rules.says_option(2, "la seconda")
    assert not rules.says_option(2, "non la seconda")
    assert not rules.says_option(2, "la seconda no, la prima")
    assert rules.says_send("sì, mandala pure") and rules.says_send("send it")
    for not_plain in (
        "mandala dopo",
        "mandala a Marco invece",
        "send it to my boss instead",
        "manda un messaggio",
    ):
        assert not rules.says_send(not_plain), not_plain


# --- F2: a held item released at a breakpoint is aged from now, not from when it was queued ----------------


def test_a_released_item_is_not_stale(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from structlog.testing import capture_logs

    from pisa.services.notifications import Dispatcher

    nid = _queue(client, "info", "Il pacco è arrivato.")
    db.execute(
        "UPDATE notifications SET queued_at = %s, not_before = %s WHERE id = %s",
        (clock.now - timedelta(minutes=90), clock.now + timedelta(minutes=2), uuid.UUID(nid)),
    )
    runtime: Any = client.app.state.runtime
    from tests.conftest import pause_outbox

    pause_outbox(client)  # this test's dispatcher is the only one delivering
    dispatcher = Dispatcher(
        runtime.pool, runtime.live, runtime.push, clock=runtime.clock, unasked=runtime.conversations.unasked,
        breakpoints=runtime.live, owner=runtime.owner
    )  # fmt: skip
    with capture_logs() as logs:
        client.portal.call(dispatcher.release, [nid])
    assert not [e for e in logs if e["event"] == "notification_expired"]  # held 90 min, still said
