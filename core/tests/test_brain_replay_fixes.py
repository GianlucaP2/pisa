"""What the replay through Pisa's own ConversationService found, one test each (each fails with
its fix undone). The scenarios themselves run in `test_replay.py`."""

from datetime import datetime
from typing import Any

from pisa.domain import actions as rules
from tests.replay.scenarios import SCENARIOS, c, done
from tests.test_brain_receipts import TODAY, said
from tests.test_replay import _play

D = "2026-10-"


def _scenario(id_: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == id_)


def test_an_answer_to_pisa_s_question_grounds_the_write_it_completes(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # "metti il dentista giovedì" → "A che ora?" → "alle 15:30": the question's words and the answer are one
    # command. Before, "alle 15:30" alone grounded no "Dentista" and the event waited for a yes.
    scenario = _scenario("18-missing-time-ask")
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
    assert db.execute("SELECT count(*) FROM approvals WHERE status = 'pending'").fetchone()[0] == 0


def test_a_question_that_asked_a_yes_is_no_question_to_join(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # Pisa asked for a yes (an approval): the user's next words answer that yes, by code or by the model; the
    # turn that asked is never joined to them (only a plain question is).
    scenario = {
        "id": "x-approval-is-not-a-question", "now": D + "30T10:00", "lang": "it-IT", "max_questions": 1,
        "world": {"events": [("e101", "Riunione con Marco", "2026-11-02T10:00", "2026-11-02T11:00",
                              [{"email": "marco@example.invalid"}])]},
        "turns": [{"owner": "cancella la riunione con Marco di lunedì"},
                  {"owner": "metti la palestra alle 18", "advance_s": 5}],
        "script": [[[c("cancel_event", event="riunione con Marco", day="lunedì",
                       owner_said="cancella la riunione con Marco di lunedì")],
                    "La cancello e Marco riceve la disdetta?"],
                   [[c("create_event", title="Palestra", start=D + "30T18:00",
                       owner_said="la palestra alle 18"),
                     done("Fatto: palestra alle 6.")]]],
        "expect": {"events": [("riunione", "2026-11-02T10:00"), ("palestra", D + "30T18:00")]},
    }  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
    turn = client.app.state.runtime.conversations._last_turn.get(
        db.execute("SELECT id FROM devices").fetchone()[0]
    )
    assert turn is not None and turn.unanswered == ""


def test_a_take_back_before_annulla_still_undoes(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # "anzi no, annulla" right after a move: the undo gate refused it ("didn't ask to undo"), only a bare
    # "annulla" counted.
    assert rules.says_undo("anzi no, annulla") and rules.says_undo("no, annulla")
    assert not rules.says_undo("annulla la cena") and not rules.says_undo("no, annulla la riunione")
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, _scenario("19-move-then-undo"))
    assert result.passed, result.problems


def test_aspetto_with_what_the_owner_waits_for_takes_nothing_back() -> None:
    # "aspetto la spesa" is "I'm waiting for the shopping", never "wait, no": the palestra before it stands.
    day = datetime(TODAY.year, TODAY.month, TODAY.day, 15)
    assert said("metti la palestra alle 15, aspetto la spesa", "Palestra", day) is None
    # "aspetto" alone, or "aspetta", still takes it back
    assert said("metti la palestra alle 15, aspetto", "Palestra", day) is not None
    assert said("metti la palestra alle 15, aspetta no", "Palestra", day) is not None


def test_an_event_in_progress_is_created_one_already_over_is_not(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # 12:37: "resto a casa dalle 12 alle 16" was refused as "in the past". In progress is real.
    scenario = {
        "id": "x-in-progress", "now": D + "30T12:37", "lang": "it-IT", "world": {}, "max_questions": 0,
        "turns": [{"owner": "segnalo: resto a casa dalle 12 alle 16, e metti la colazione dalle 8 alle 9"}],
        "script": [[[c("create_event", title="Resto a casa", start=D + "30T12:00", end="16:00",
                       owner_said="resto a casa dalle 12 alle 16"),
                     c("create_event", title="Colazione", start=D + "30T08:00", end="09:00",
                       owner_said="la colazione dalle 8 alle 9")],
                    "Segnato: a casa fino alle 4. La colazione delle 8 è già passata."]],
        "expect": {"events": [("resto a casa", D + "30T12:00")], "events_total": 1},
    }  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
