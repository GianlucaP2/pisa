"""Release audit §9: today's plan said from nothing, or from a plan of tasks that are gone.

Three replies of the real model, replayed with the scripted one (synthetic words and tasks):
- f4-en-2 / f4-it-1: no tool ran, yet the reply said nothing was planned today and named tasks from earlier
  rows. The day is their lists: with no read this turn, the answer to "what should I do?" waits for one, then
  the honest line.
- f4-it-2: `what_now` ran and named tasks that had been deleted. Today's stored plan was made before they were
  deleted (the minute's re-plan had not run yet): a plan naming a task that is no longer open is made again
  before it is read, and is never put in a turn's context as their day.
- f1-en-1 / f1-en-2: an English request right after an Italian row added the task under the Italian title
  that same stale plan carried in the turn's context.
"""

from typing import Any

import pytest

from pisa.domain import claims
from pisa.services.conversation import READ_LINE
from tests.replay.scenarios import c

N = "2026-11-18T10:00"
READ_HINT = READ_LINE.split(":")[0].strip()
GONE_EN = [("Buy milk, bread and eggs", "10:30", "11:00"), ("Buy a gift", "11:00", "11:30")]
GONE_IT = [("Comprare latte, pane e uova", "10:30", "11:00"), ("Comprare un regalo", "11:00", "11:30")]
TODAY_EN = ["Pay the electricity bill", "Call the plumber"]
TODAY_IT = ["Pagare la bolletta della luce", "Chiamare l'idraulico"]

# (lang, their words, the reply of the run, the seeded tasks, the plan of gone tasks)
NO_READ = [
    ("en-US", "What should I do now?",
     "You have nothing planned for today. You could buy milk, bread and eggs, and pick up a gift.",
     TODAY_EN, GONE_EN),
    ("it-IT", "Cosa devo fare oggi?",
     "Oggi non hai niente in programma. Potresti comprare latte, pane e uova, e un regalo.",
     TODAY_IT, GONE_IT),
]  # fmt: skip


def _scenario(lang: str, words: str, world: dict[str, Any], script: list[Any], sid: str) -> dict[str, Any]:
    return {
        "id": sid, "now": N, "lang": lang, "max_questions": 0, "world": world,
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip


def _turn(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> Any:  # fmt: skip
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    return _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]


@pytest.mark.parametrize("case", NO_READ, ids=["f4-en-2", "f4-it-1"])
def test_the_day_said_with_no_read_gets_the_honest_line(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, false, today, gone = case
    world = {"tasks_today": today, "plan_gone": gone}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, world, [false], "audit-day-never"))  # fmt: skip
    assert "milk" not in turn.reply and "latte" not in turn.reply, turn.reply
    assert turn.reply.strip() == claims.unread_truth(lang, day=True), turn.reply


@pytest.mark.parametrize("case", NO_READ, ids=["f4-en-2", "f4-it-1"])
def test_the_day_said_with_no_read_is_held_for_a_read_round(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, false, today, gone = case
    right = (
        "Now: pay the electricity bill, then call the plumber."
        if lang == "en-US"
        else "Adesso: la bolletta della luce, poi l'idraulico."
    )
    step = {"seen": today[0], "then": right, "else": false}
    script = [{"hint": READ_HINT, "then": [c("what_now")], "else": false}, step]
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks_today": today, "plan_gone": gone}, script,
                           "audit-day-read"))  # fmt: skip
    assert turn.reply.strip().endswith(right), turn.reply
    assert [name for name, _ in turn.calls] == ["what_now"]


@pytest.mark.parametrize(
    ("lang", "words"), [("it-IT", "Cosa faccio adesso?"), ("en-US", "What should I do now?")]
)
def test_what_now_never_reads_a_plan_of_gone_tasks(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words
) -> None:
    """f4-it-2: what_now ran, and its plan was the one made before the earlier tasks were deleted."""
    today, gone = (TODAY_IT, GONE_IT) if lang == "it-IT" else (TODAY_EN, GONE_EN)
    right, wrong = f"Adesso: {today[0]}.", f"Adesso: {gone[0][0]}."
    step = {"seen": today[0], "then": right, "else": wrong}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks_today": today, "plan_gone": gone}, [[c("what_now")], step],
                           "audit-what-now"))  # fmt: skip
    assert turn.reply.strip().endswith(right), turn.reply


@pytest.mark.parametrize(
    ("words", "gone", "english"),
    [
        ("Add a task to renew my passport by next Friday", "Rinnovare il passaporto", "Renew my passport"),
        ("Add 'call the bank' for tomorrow, it's urgent", "Chiamare la banca", "Call the bank"),
    ],
    ids=["f1-en-1", "f1-en-2"],
)
def test_an_english_request_after_an_italian_row_gets_an_english_title(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, words, gone, english
) -> None:
    """The turn's language was English (the language instruction said so); the Italian title came from the
    turn's context: today's plan, made before the Italian row's task was deleted. A model that takes the
    title it sees in Pisa's own data (as the real one did) now sees none."""
    add = {"hint": gone, "then": [c("add_task", title=gone)], "else": [c("add_task", title=english)]}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario("en-US", words, {"plan_gone": [(gone, "10:30", "11:00")]}, [add, "Added."],
                           "audit-title-lang"))  # fmt: skip
    assert turn.calls and turn.calls[0][0] == "add_task", turn.calls
    titles = [r[0] for r in db.execute("SELECT title FROM tasks WHERE status = 'open'").fetchall()]
    assert titles == [english], titles


@pytest.mark.parametrize(
    "sentence",
    ["You have nothing planned for today.", "Oggi non hai niente in programma.",
     "Non hai nulla da fare oggi.", "Today you have two things: milk and a gift.", "Your day is free."],
)  # fmt: skip
def test_a_sentence_about_the_day_needs_a_read(sentence: str) -> None:
    assert claims.unread_state(sentence, read=False) is not None, sentence
    assert claims.unread_state(sentence, read=True) is None


@pytest.mark.parametrize(
    "sentence",
    ["Today is Friday.", "Oggi c'è il sole.", "Domani piove.", "It's 9 pm.", "Is anything planned today?"],
)
def test_other_sentences_about_today_need_no_read(sentence: str) -> None:
    assert claims.unread_state(sentence, read=False) is None, sentence


@pytest.mark.parametrize(
    ("words", "asked"),
    [("What should I do now?", True), ("Cosa devo fare oggi?", True), ("Cosa faccio adesso?", True),
     ("What's on my agenda today?", True), ("What should I do in Lisbon this weekend?", False),
     ("Cosa devo fare per rinnovare il passaporto?", False), ("Che tempo fa oggi?", False)],
)  # fmt: skip
def test_their_day_asked(words: str, asked: bool) -> None:
    assert claims.asks_day(words) is asked
