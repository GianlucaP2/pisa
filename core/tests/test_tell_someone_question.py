# ruff: noqa: E501
"""A Haiku replay (b5-16 family): Haiku adds "Avviso Anna anche io?" after its yes question,
and their "sì vai" bound nothing (the last "?" sentence names no change). A trailing tell-someone question that
names no change of its own falls back to the question before it; every rail of M6 still holds."""

from pisa.domain import actions, claims

MOVE_16 = {"when": "2026-10-30 16:00"}
MOVE_17 = {"when": "2026-10-30 17:00"}


def test_a_trailing_tell_question_leaves_his_yes_on_the_real_question() -> None:
    for said in (
        "Sposto la call con Anna alle 16 e la palestra alle 17, oggi venerdì 30: va bene? Avviso Anna anche io?",
        "Sposto la call con Anna alle 16 e la palestra alle 17, oggi venerdì 30: va bene? Ti avviso Anna?",
        "Sposto la call con Anna alle 16 e la palestra alle 17: ti va bene? Lo dico ad Anna?",
        "I'll move the call with Anna to 16:00 and the gym to 17:00: ok? Shall I let Anna know?",
    ):
        assert actions.says_change("calendar_move", MOVE_16, said), said
        assert actions.says_change("calendar_move", MOVE_17, said), said


def test_no_rail_is_loosened_by_the_tell_question() -> None:
    # a tell question alone binds nothing
    assert not actions.says_change("calendar_move", MOVE_16, "Avviso Anna anche io?")
    # a statement (no question) before it: nothing asked their yes for the move
    assert not actions.says_change("calendar_move", MOVE_16, "Ho visto la call alle 16. Avviso Anna?")
    # a tell question that also names a change is its own question (not skipped)
    assert not actions.says_change(
        "calendar_move", MOVE_16, "Sposto la palestra alle 17: va bene? Avviso Anna e sposto la call?"
    )
    # a question that asks something else stays what it asks
    assert not actions.says_change(
        "calendar_move", MOVE_17, "Sposto la palestra alle 17? No: ti serve altro?"
    )
    # M6: without its time nothing binds, tell question or not
    assert not actions.says_change("calendar_move", MOVE_17, "Sposto la palestra: va bene? Avviso Anna?")
    # B5's rails
    empty = claims.Record(frozenset(), frozenset(), frozenset())
    assert claims.unbacked("Sposto la palestra alle 17. Avviso Anna? Va bene?", "it-IT", empty)
