# ruff: noqa: F811  (the `calendar` fixture is imported from test_night_commands)
"""The reviewer's database probes, kept as tests."""

from tests.assistant_env import rome
from tests.conftest import pair
from tests.test_acting import Model, socket_turns
from tests.test_night_commands import NIGHT, Night, calendar, created, said  # noqa: F401

DAY = rome(2026, 10, 30, 13)


def test_a_new_proposal_with_a_similar_title_does_not_supersede_an_unrelated_one(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = DAY
    model = Night(
        {
            "metti la call con Paola alle 5": (
                [("create_event", {"title": "Call con Paola", "start": "domani alle 5"})],
                None,
            ),
            "e metti la call con Marco alle 6": (
                [("create_event", {"title": "Call con Marco", "start": "domani alle 6"})],
                None,
            ),
        }
    )
    fake_models.default = model
    device = pair(db)
    socket_turns(client, db, secrets_key, model, "metti la call con Paola alle 5", device=device)  # type: ignore[arg-type]
    socket_turns(client, db, secrets_key, model, "e metti la call con Marco alle 6", device=device)  # type: ignore[arg-type]
    rows = db.execute("SELECT payload->>'title', status FROM approvals ORDER BY created_at").fetchall()
    assert ("Call con Paola", "superseded") not in rows, rows


def test_two_questions_asked_in_separate_rounds_are_not_both_approved_by_one_si(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = DAY
    calls = [
        ("create_event", {"title": "Call con Paola", "start": "domani alle 5"}),
        ("create_event", {"title": "Palestra", "start": "domani alle 6"}),
    ]
    model = Model("metti la call con Paola alle 5 e la palestra alle 6", calls, steps=True)
    fake_models.default = model
    device = pair(db)
    asked, yes = socket_turns(
        client,
        db,
        secrets_key,
        model,
        "metti la call con Paola alle 5 e la palestra alle 6",
        "sì",
        device=device,
    )
    print("ASKED:", said(asked))
    print("YES:", said(yes))
    statuses = [r[0] for r in db.execute("SELECT status FROM approvals ORDER BY created_at").fetchall()]
    # two separate questions ("?" twice): one sì should not run both
    assert said(asked).count("?") < 2 or statuses.count("done") < 2, (said(asked), statuses)


def test_a_done_claim_with_a_negation_word_is_still_caught(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = DAY
    model = Night({"spostali al 30": ([], "Fatto, non preoccuparti: sono sul 30.")})
    fake_models.default = model
    (reply,) = socket_turns(client, db, secrets_key, model, "spostali al 30")  # type: ignore[arg-type]
    assert not calendar.events
    assert "Fatto" not in said(reply), said(reply)


def test_ok_sposto_is_caught(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = DAY
    model = Night({"spostali al 30": ([], "Ok, sposto tutto al 30. Spostati!")})
    fake_models.default = model
    (reply,) = socket_turns(client, db, secrets_key, model, "spostali al 30")  # type: ignore[arg-type]
    assert "sposto" not in said(reply).lower() and "Spostati" not in said(reply), said(reply)


def test_venerdi_said_at_0230_on_friday_is_not_next_week(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = NIGHT  # Fri 30 Oct, 02:38; the user still lives Thursday night
    model = Night(
        {
            "metti il dentista venerdì alle 10": (
                [("create_event", {"title": "Dentista", "start": "venerdì alle 10"})],
                None,
            )
        }
    )
    fake_models.default = model
    (reply,) = socket_turns(client, db, secrets_key, model, "metti il dentista venerdì alle 10")  # type: ignore[arg-type]
    print("REPLY:", said(reply), created(calendar))
    # acted silently on 6 November?
    assert created(calendar).get("Dentista") != "2026-11-06T10:00", said(reply)
