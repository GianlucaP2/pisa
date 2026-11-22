"""B-2: a write that failed once and then succeeded on a retry is said as done (f3-en-1, f5-en-1: a retried
`keep_intention` saved the reminder, and the reply said "It didn't work"). The turn's record keeps the final
outcome of the same call: a success clears the earlier failure of the same tool when the call names no object
of its own (its arguments were all that changed); a failure of one named object still stands beside another's
success ("move A" failed, "move B" done). Scripted models only."""

import pytest

from tests.replay.scenarios import c

N = "2026-11-18T10:00"

CASES = [
    ("en-US", "remind me to call Anna tomorrow at 7 pm", "phone Anna", "call Anna", "tomorrow at 19:00",
     "Done: I'll remind you tomorrow at 7 pm to call Anna."),
    ("it-IT", "ricordami domani alle 19 di chiamare Anna", "telefonare ad Anna", "chiamare Anna",
     "domani alle 19", "Fatto: domani alle 19 ti ricordo di chiamare Anna."),
]  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=[k[0] for k in CASES])
def test_a_retried_keep_intention_that_saved_is_said_as_saved(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    lang, words, wrong, what, when, reply = case
    script = [
        [c("keep_intention", what=wrong, cue="time", when=when)],  # not their words: refused
        [c("keep_intention", what=what, cue="time", when=when)],  # the retry: kept
        reply,
    ]
    scenario = {
        "id": f"b2-{lang}", "now": N, "lang": lang, "max_questions": 0, "world": {},
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip
    turn = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]
    assert [name for name, _ in turn.calls] == ["keep_intention", "keep_intention"]
    assert db.execute("SELECT count(*) FROM intentions").fetchone() == (1,)  # saved
    assert turn.reply.strip() == reply, turn.reply  # never "It didn't work"
    assert "didn't work" not in turn.reply and "non ci sono riuscito" not in turn.reply


def test_a_failure_of_a_named_object_still_stands_beside_another_s_success() -> None:
    from pisa.services.conversation import _settle, _target  # pyright: ignore[reportPrivateUsage]

    failures: dict[tuple[str, str], str] = {}
    _settle(failures, "move_event", _target({"event": "cena"}), "move_event: Not changed: busy")
    _settle(failures, "move_event", _target({"event": "palestra"}), None)
    assert ("move_event", "cena") in failures  # move A failed, move B done
    _settle(failures, "move_event", _target({"event": "Cena", "start": "21"}), None)
    assert failures == {}  # A, done on a retry
    wrong, right = _target({"what": "phone Anna"}), _target({"what": "call Anna"})
    _settle(failures, "keep_intention", wrong, "keep_intention: Not kept")
    _settle(failures, "add_task", _target({"what": "x"}), None)  # another tool clears nothing
    assert ("keep_intention", wrong) in failures
    _settle(failures, "keep_intention", right, None)
    assert failures == {}  # the same call, retried with other arguments
