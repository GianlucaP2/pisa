"""Fixes at the merge of the overlap and economy work with the brain's replay fixes."""

from tests.assistant_env import rome
from tests.test_brain_replay_fixes import _play
from tests.test_overlaps_and_economy import AMAZON, DAY

D = "2026-10-"


def test_an_answer_with_a_command_of_its_own_never_completes_the_question_s(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from tests.replay.scenarios import c, done

    # Pisa asked "A che ora?" about the dentist; the user gives the gym its own time: "alle 17" is the gym's,
    # never the dentist's (joined with a space, the two turns were one clause)
    scenario = {
        "id": "x-own-command", "now": D + "30T10:00", "lang": "it-IT", "world": {}, "max_questions": 1,
        "turns": [{"owner": "metti il dentista giovedì"},
                  {"owner": "intanto metti la palestra alle 17", "advance_s": 5}],
        "script": [["A che ora?"],
                   [[c("create_event", title="Dentista", start="2026-11-05T17:00", owner_said="alle 17"),
                     done("Fatto: dentista giovedì alle 5.")]]],
        "expect": {"events": []},
    }  # fmt: skip
    _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert db.execute("SELECT count(*) FROM approvals WHERE status = 'pending'").fetchone()[0] == 1  # asks


def test_earlier_turns_ground_a_done_only_and_never_one_taken_back(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock
) -> None:
    from tests.assistant_env import add_task, set_settings
    from tests.test_brain_loop import Script, play

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    clock.now = rome(*DAY, 12, 38)
    task = add_task(db, "Pacco Amazon", rome(*DAY).date())
    db.execute("INSERT INTO connector_accounts (connector, status) VALUES ('google', 'connected')")  # M8
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet) VALUES ('amz', 'amz', 'Amazon', %s, 'Consegnato: il tuo pacco', %s, ARRAY['INBOX'], "
        "'Il tuo pacco è stato consegnato')",
        (AMAZON, rome(*DAY, 11)),
    )
    first = "il pacco di Amazon è arrivato, lo puoi segnare come fatto"
    second = "no, aspetta"
    third = "controlla la mail di Amazon"
    model = Script({
        first: ["Lo controllo tra un attimo."],
        second: ["Va bene, aspetto."],
        third: [[("search_my_email", {"query": "Amazon"})], [("mark_done", {"what": "Pacco Amazon"})]],
    })  # fmt: skip
    fake_models.default = model
    play(client, db, secrets_key, model, first, second, third)
    # "no, aspetta" took the first turn back: after the email, the user's earlier words ground no done
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() != ("done",)
