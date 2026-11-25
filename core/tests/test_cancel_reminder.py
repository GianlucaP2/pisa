"""§27 w3-it-1: "Annulla il promemoria per Anna" after `remember_to` cancels that reminder
(`cancel_reminder`): it never fires, and Pisa says so only after the result says "Cancelled". Several
matching: nothing cancelled, the user picks. Outside text never cancels one."""

from typing import Any

import psycopg

from pisa.domain import follow
from tests.replay.scenarios import c

N = "2026-11-18T10:00"


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          turns: list[str], script: list[Any]) -> Any:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario = {
        "id": "cancel-reminder", "now": N, "lang": "it-IT", "max_questions": 0, "world": {},
        "turns": [{"owner": t} for t in turns], "script": script, "expect": {},
    }  # fmt: skip
    return play(client, db, fake_models, secrets_key, clock, http_handler, scenario)


def _live(db: psycopg.Connection) -> list[str]:
    rows = db.execute(
        "SELECT what FROM pisa_promises WHERE fired_at IS NULL AND cancelled_at IS NULL ORDER BY what"
    ).fetchall()
    return [r[0] for r in rows]


def test_promises_named() -> None:
    live = [{"id": "1", "what": "chiamare Anna"}, {"id": "2", "what": "pagare la bolletta"}]
    assert [p["id"] for p in follow.promises_named("Anna", live)] == ["1"]
    assert [p["id"] for p in follow.promises_named("il promemoria per Anna", live)] == ["1"]
    assert follow.promises_named("il promemoria", live) == []  # names none: the tool asks which one
    two = [*live, {"id": "3", "what": "comprare un regalo per Anna"}]
    assert [p["id"] for p in follow.promises_named("Anna", two)] == ["1", "3"]


def test_cancel_the_reminder_named(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [
        [[c("remember_to", what="chiamare Anna", condition="time", when="domani alle 9")],
         "Te lo ricordo domani alle 9."],
        [[c("remember_to", what="pagare la bolletta", condition="time", when="domani alle 18")],
         "Te lo ricordo domani alle 18."],
        [[c("cancel_reminder", what="Anna")], "Fatto, l'ho annullato."],
    ]  # fmt: skip
    run = _play(client, db, fake_models, secrets_key, clock, http_handler,
                ["Ricordami domani alle 9 di chiamare Anna", "Ricordami domani alle 18 di pagare la bolletta",
                 "Annulla il promemoria per Anna"], script)  # fmt: skip
    assert _live(db) == ["pagare la bolletta"]
    assert [name for name, _ in run.turns[-1].calls] == ["cancel_reminder"]


def test_several_matching_cancel_nothing_and_list_them(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [
        [[c("remember_to", what="chiamare Anna", condition="time", when="domani alle 9")], "Va bene."],
        [[c("remember_to", what="regalo per Anna", condition="time", when="domani alle 18")], "Va bene."],
        [[c("cancel_reminder", what="Anna")], "Quale dei due?"],
    ]  # fmt: skip
    _play(client, db, fake_models, secrets_key, clock, http_handler,
          ["Ricordami domani alle 9 di chiamare Anna", "Ricordami domani alle 18 del regalo per Anna",
           "Annulla il promemoria per Anna"], script)  # fmt: skip
    assert _live(db) == ["chiamare Anna", "regalo per Anna"]


def test_words_not_asking_to_cancel_cancel_nothing(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [
        [[c("remember_to", what="chiamare Anna", condition="time", when="domani alle 9")], "Va bene."],
        [[c("cancel_reminder", what="Anna")], "Ok."],
    ]  # fmt: skip
    _play(client, db, fake_models, secrets_key, clock, http_handler,
          ["Ricordami domani alle 9 di chiamare Anna", "A che ora era quello per Anna?"], script)  # fmt: skip
    assert _live(db) == ["chiamare Anna"]


def test_the_one_reminder_set_is_cancelled_only_when_named(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """E1 CR-1: "Cancella la spesa dalla lista" and a model that wrongly calls cancel_reminder("la spesa"):
    the one reminder set is not cancelled; the result lists it, to ask."""
    script = [
        [[c("remember_to", what="chiamare Anna", condition="time", when="domani alle 9")], "Va bene."],
        [[c("cancel_reminder", what="la spesa")], "Quale promemoria?"],
    ]  # fmt: skip
    _play(client, db, fake_models, secrets_key, clock, http_handler,
          ["Ricordami domani alle 9 di chiamare Anna", "Cancella la spesa dalla lista"], script)  # fmt: skip
    assert _live(db) == ["chiamare Anna"]
