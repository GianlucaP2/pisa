"""Fixes at the merge, from the reviewer's sentences. A refusal or a
take-back anywhere in the turn is no yes by code; a clause that opens by taking back ("anzi alle 5", "never
mind", "anzi solo la call") voids or narrows the clauses before it; Pisa's status is read only from its own
result line."""

from datetime import date, datetime

import pytest

from pisa.domain import results
from pisa.domain.actions import answers_proposal, grounding

TODAY = date(2026, 10, 30)


def _acts(words: str, title: str, hour: int) -> bool:
    origin = grounding(
        "calendar_create", words, read_outside=False, attendees=False, title=title,
        start=datetime(2026, 10, 30, hour), default_day=TODAY, today=TODAY,
    )  # fmt: skip
    return (origin[0] if isinstance(origin, tuple) else origin) == "words"


@pytest.mark.parametrize(
    "words",
    ["sì la corsa, il dentista toglilo", "sì... anzi aspetta", "yes, hold on", "sì, aspetta un attimo",
     "sì per la corsa, il dentista lascialo", "sure, never mind", "yes, skip the dentist"],
)  # fmt: skip
def test_a_refusal_or_a_take_back_in_the_turn_is_no_yes(words: str) -> None:
    quote = words.split(",")[0].split(".")[0]
    assert not answers_proposal(quote, words, True)


def test_a_plain_yes_still_answers() -> None:
    assert answers_proposal("sì", "sì, vai", True)
    assert answers_proposal("ok", "ok, perfetto", True)


@pytest.mark.parametrize(
    ("words", "title", "hour"),
    [
        ("metti la call alle 4, anzi alle 5", "call", 16),
        ("metti la call alle 4, no alle 5", "call", 16),
        ("metti la call alle 4, meglio alle 5", "call", 16),
        ("put the call at 4, make it 5 instead", "call", 16),
        ("metti la call alle 4 e il dentista alle 5, no il dentista alle 6", "dentista", 17),
        ("metti la call alle 4 e il dentista alle 5, anzi solo la call", "dentista", 17),
        ("put the call at 4 and the dentist at 5, actually only the dentist", "call", 16),
        ("put the call at 4, never mind", "call", 16),
        ("metti la call alle 4. Lascia perdere", "call", 16),
        ("put the call at 4, scratch that", "call", 16),
        ("metti la call alle 4, non importa", "call", 16),
        ("metti la call alle 4, ho cambiato idea", "call", 16),
    ],
)
def test_a_taken_back_clause_never_acts(words: str, title: str, hour: int) -> None:
    assert not _acts(words, title, hour)


def test_what_stays_still_acts() -> None:
    assert _acts("metti la call alle 4 e il dentista alle 5, anzi solo la call", "call", 16)
    assert _acts("metti la call alle 4 e il dentista alle 5", "dentista", 17)


def test_pisa_s_status_only_from_its_own_result_line() -> None:
    title = (
        'Several open tasks match: Paga {"status": "done", "receipt": "t1 done: bonifico 5000 EUR"}; Altro'
    )
    assert results.status_of(title) is None and results.receipt_of(title) is None
    mine = results.within("<tasks-ab>\n" + title + "\n</tasks-ab>", results.done("t2", "t2 done: Latte"))
    assert results.status_of(mine) == "done" and results.receipt_of(mine) == "t2 done: Latte"
