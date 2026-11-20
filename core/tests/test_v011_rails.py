"""v0.1.1 audit (real model, synthetic rows).

Item 3 (f6-en-1, f6-it-1): with Google not connected the model said itself that it couldn't add the lunch,
and the claim rail appended "I haven't put it: it didn't work." / "Non l'ho aggiunto: non ci sono riuscito."
after its promise ("Connect Google and I'll put it on your calendar"). A reply that already says the change
wasn't made gets no rail line repeating it; one that doesn't still gets it.

Item 4 (f8-it-2): "Annulla" right after add_task undoes the task Pisa just added."""

import uuid

import pytest

from pisa.domain import claims
from pisa.services.conversation import TurnState, _released  # pyright: ignore[reportPrivateUsage]

ROWS = [
    ("en-US", "Put lunch with Sara on my calendar tomorrow at 1 pm",
     "I couldn't add lunch with Sara at 13:00: Google isn't connected. ",
     "Connect Google in Settings and I'll put it on your calendar.", "I haven't put it: it didn't work."),
    ("it-IT", "Metti in calendario pranzo con Sara domani alle 13",
     "Non sono riuscito ad aggiungere il pranzo con Sara: Google non è collegato. ",
     "Collega Google nelle Impostazioni e lo aggiungo.", "Non l'ho aggiunto: non ci sono riuscito."),
]  # fmt: skip


def _failed_turn(words: str) -> TurnState:
    turn = TurnState(conversation_id=uuid.uuid4(), text=words)
    turn.failures[("create_event", "{}")] = "create_event: not done: Google is not connected"
    return turn


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_a_failure_the_reply_already_says_gets_no_rail_line(row: tuple[str, ...]) -> None:
    lang, words, said, promise, line = row
    kept, false = _released([promise], _failed_turn(words), lang, said=said)
    assert line not in kept
    assert kept == promise  # what comes next, said as written; nothing repeated
    assert false == 1  # still counted as a claim the record doesn't back


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_a_failure_the_reply_does_not_say_still_gets_the_line(row: tuple[str, ...]) -> None:
    lang, words, _, promise, line = row
    kept, _ = _released([promise], _failed_turn(words), lang, said="")
    assert kept == line
    done = "I've put it on your calendar." if lang == "en-US" else "L'ho aggiunto in calendario."
    kept, _ = _released([done], _failed_turn(words), lang, said="Lunch with Sara, tomorrow at 13:00.")
    assert kept == line


@pytest.mark.parametrize(
    ("sentence", "reply", "lang", "said"),
    [
        ("I'll put it on your calendar.", "I couldn't add lunch with Sara.", "en-US", True),
        ("I'll put it on your calendar.", "It wasn't added: Google isn't connected.", "en-US", True),
        ("Lo aggiungo.", "Non posso metterlo in calendario.", "it-IT", True),
        ("Lo aggiungo.", "Non riesco ad aggiungerlo: Google non è collegato.", "it-IT", True),
        ("I'll put it on your calendar.", "Lunch with Sara, tomorrow at 13:00.", "en-US", False),
        ("Lo aggiungo.", "Il pranzo con Sara è domani alle 13.", "it-IT", False),
        ("I'll put it on your calendar.", "I couldn't send the email.", "en-US", False),  # another change
        ("Lo aggiungo.", "Non sono riuscito a spostare la cena.", "it-IT", False),
    ],
)
def test_says_not_done(sentence: str, reply: str, lang: str, said: bool) -> None:
    assert claims.says_not_done(sentence, reply, lang) is said


# Item 4 (f8-it-2): "Aggiungi un'attività: innaffiare le piante", then "Annulla": the model called
# undo_last_change, which answered there was no change of Pisa's to undo, and the task stayed. Pisa's last
# change was the task it had just added: undo removes it (restorable for 24 h, as delete_task's).
UNDO = [
    ("it-IT", "Aggiungi un'attività: innaffiare le piante", "innaffiare le piante", "Annulla",
     "Aggiunta: innaffiare le piante.", "Tolta: innaffiare le piante."),
    ("en-US", "Add a task: water the plants", "water the plants", "Undo",
     "Added: water the plants.", "Removed: water the plants."),
]  # fmt: skip


@pytest.mark.parametrize("case", UNDO, ids=[k[0] for k in UNDO])
def test_undo_right_after_add_task_removes_the_task(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    from tests.replay.scenarios import c
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    lang, words, title, undo, added, removed = case
    scenario = {
        "id": f"v011-4-{lang}", "now": "2026-11-18T10:00", "lang": lang, "max_questions": 0, "world": {},
        "turns": [{"owner": words}, {"owner": undo, "advance_s": 10}],
        "script": [[[c("add_task", title=title)], added], [[c("undo_last_change")], removed]],
        "expect": {},
    }  # fmt: skip
    turns = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns
    assert [name for name, _ in turns[-1].calls] == ["undo_last_change"]
    assert db.execute("SELECT status, deleted_at IS NOT NULL FROM tasks").fetchall() == [("deleted", True)]
    assert turns[-1].reply.strip() == removed, turns[-1].reply  # said as done: the record backs it


@pytest.mark.parametrize("advance_s", [20 * 60], ids=["20 minutes later"])
def test_undo_never_takes_an_older_task_for_the_last_change(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, advance_s
) -> None:
    """Only a task added minutes ago and untouched since is "the last change": an older one stays (a bare
    "annulla" about something else never deletes it; delete_task names a task)."""
    from tests.replay.scenarios import c
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    scenario = {
        "id": "v011-4-older", "now": "2026-11-18T10:00", "lang": "it-IT", "max_questions": 0, "world": {},
        "turns": [{"owner": "Aggiungi un'attività: innaffiare le piante"},
                  {"owner": "Annulla", "advance_s": advance_s}],
        "script": [[[c("add_task", title="innaffiare le piante")], "Aggiunta: innaffiare le piante."],
                   [[c("undo_last_change")], "Non c'è niente da annullare."]],
        "expect": {},
    }  # fmt: skip
    _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert db.execute("SELECT status FROM tasks").fetchall() == [("open",)]
