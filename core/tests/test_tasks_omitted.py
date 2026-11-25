"""Quality (v0.1.3): a turn that read their tasks and answered "Oggi non hai eventi" / "oggi niente" while
the store holds tasks for the day asked gets code's own line naming them ("Hai 3 cose da fare: A, B e C."),
from the store. Never when the reply names the tasks (an empty calendar beside them is true), when there are
none, or when the turn read nothing. Scripted model only, synthetic titles."""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pisa.brain import conversation as brain
from pisa.domain import claims
from pisa.domain.tasks import Task
from tests.replay.scenarios import c
from tests.test_answer_first import TASKS, WORDS, N, _play  # pyright: ignore[reportPrivateUsage]

ROME = ZoneInfo("Europe/Rome")
TASKS_IT = TASKS["it-IT"]

NO_EVENTS = {"it-IT": "Oggi non hai eventi.", "en-US": "You have no events today."}
NOTHING = {"it-IT": "Oggi niente.", "en-US": "You have nothing to do today."}
LINE = {
    "it-IT": "Hai 3 cose da fare: Pagare l'affitto, Chiamare il dentista e Comprare il latte.",
    "en-US": "You have 3 things to do: Pay the rent, Call the dentist and Buy milk.",
}
NAMED = {
    "it-IT": "Oggi non hai eventi, ma ti restano tre cose: pagare l'affitto, chiamare il dentista e comprare "
    "il latte.",
    "en-US": "You have no events today, but three things are left: pay the rent, call the dentist and buy "
    "milk.",
}
LANGS = ["it-IT", "en-US"]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("said", ["no_events", "nothing"])
def test_a_reply_that_leaves_out_the_day_s_tasks_gets_code_s_line(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, said
) -> None:
    words = (NO_EVENTS if said == "no_events" else NOTHING)[lang]
    script = [[c("get_tasks")], words]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert reply == f"{words} {LINE[lang]}", reply


@pytest.mark.parametrize("lang", LANGS)
def test_no_events_beside_the_tasks_named_is_left_alone(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    script = [[c("get_tasks")], NAMED[lang]]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert reply == NAMED[lang], reply


@pytest.mark.parametrize("lang", LANGS)
def test_no_tasks_in_the_store_no_line(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    script = [[c("get_tasks")], NOTHING[lang]]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script,
                  tasks=[])  # fmt: skip
    assert reply == NOTHING[lang], reply


def test_a_turn_about_something_else_that_says_no_events_is_left_alone(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Their words don't ask what the day holds, and the reply is only about the calendar: true as said."""
    script = [[c("get_tasks")], "Domenica non hai eventi."]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                  "Ho qualcosa in calendario domenica?", script)  # fmt: skip
    assert reply == "Domenica non hai eventi.", reply


@pytest.mark.parametrize(
    ("reply", "asked_day", "omits"),
    [
        ("Oggi non hai eventi.", True, True),
        ("Oggi non hai eventi.", False, False),  # the calendar only: true
        ("Nessun evento oggi, ma devi pagare l'affitto.", True, True),  # 1 of 2, no total (§27 w2)
        ("Nessun evento oggi: l'affitto e il dentista.", True, False),
        ("Oggi niente.", False, True),
        ("Non hai niente da fare.", False, True),
        ("You're free today.", False, True),
        ("Hai qualcosa da fare stasera?", False, False),  # a question states nothing
        ("Adesso: l'affitto, poi il dentista.", True, False),  # named in its own words, without the verb
        ("Oggi non hai eventi. Ah, il dentista.", False, False),  # not their day asked: one names
        ("Oggi non hai eventi. Ricordati di pagare.", True, True),  # a verb alone names no task
    ],
)
def test_omits_tasks(reply: str, asked_day: bool, omits: bool) -> None:
    assert (
        claims.omits_tasks(reply, ["Pagare l'affitto", "Chiamare il dentista"], asked_day=asked_day) is omits
    )


def test_the_line_in_code_s_words() -> None:
    assert claims.tasks_line("it-IT", ["A", "B", "C"]) == "Hai 3 cose da fare: A, B e C."
    assert claims.tasks_line("it-IT", ["A"]) == "Hai una cosa da fare: A."
    assert claims.tasks_line("en-US", ["A"], tomorrow=True) == "Tomorrow you have one thing to do: A."
    assert (
        claims.tasks_line("it-IT", ["A", "B"], 1, tomorrow=True)
        == "Domani hai 3 cose da fare: A, B e un'altra."
    )
    assert (
        claims.tasks_line("en-US", list("ABCDEF")) == "You have 6 things to do: A, B, C, D, E and one more."
    )


# §27 w2: with their day asked, the answer carries the total: fewer named and no total, or a count that isn't
# theirs, gets code's line
THREE = ["Pagare la bolletta", "Chiamare il dentista", "Comprare il pane"]


@pytest.mark.parametrize(
    ("reply", "omits"),
    [
        ("Hai due cose con scadenza: la bolletta e il dentista, più il pane.", True),  # 2 said, 3 there
        ("Prima la bolletta, poi il dentista.", True),  # 2 of 3 named, no total
        ("Hai tre cose: la bolletta, il dentista e il pane.", False),
        ("La bolletta, il dentista e il pane.", False),  # every one named
        ("Hai 3 cose da fare, a partire dalla bolletta.", False),  # the total said
        ("You have a few things: the bill first.", True),  # "a few" is no count
    ],
)
def test_the_day_asked_carries_the_total(reply: str, omits: bool) -> None:
    assert claims.omits_tasks(reply, THREE, asked_day=True) is omits


def test_a_count_with_tasks_counted_but_not_named() -> None:
    # 2 named titles and 1 from an email (counted, not named): "tre cose" is the total
    assert claims.omits_tasks("Hai tre cose: la bolletta, il dentista e una dalla mail.", THREE[:2],
                              asked_day=True, total=3) is False  # fmt: skip
    assert claims.stated_counts("Hai due cose con scadenza") == [2]
    assert claims.stated_counts("tra due ore") == []


def test_what_now_is_answered_by_one() -> None:
    assert claims.asks_now("Cosa faccio adesso?") and claims.asks_now("What should I do next?")
    assert not claims.asks_now("Cosa devo fare oggi?")
    assert claims.omits_tasks("Adesso: la bolletta.", THREE, asked_day=True, whole_day=False) is False


def test_how_many_things_asks_the_day() -> None:
    """§30 w2 rep 2: "dimmi quante cose ho da fare oggi" asks the day: "due cose con scadenza" with three
    open gets code's total line."""
    words = "Aggiungi «comprare il pane» alla lista e dimmi quante cose ho da fare oggi"
    assert claims.asks_day(words) and not claims.asks_day("Quanti anni ha Marco?")
    said = "Fatto, il pane è in lista. Per oggi hai due cose con scadenza: la bolletta e la farmacia."
    assert claims.omits_tasks(said, THREE, asked_day=claims.asks_day(words)) is True


# v0.1.4 ruling (§30 w2 rep 3): a "today" answer covers what is due today, what is overdue and the open tasks
# with no due date, shown apart; the total said follows the split
def _world(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
           lang: str, words: str, script: list[Any], world: dict[str, Any]) -> str:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario: dict[str, Any] = {
        "id": "today-split", "now": N, "lang": lang, "max_questions": 0, "world": world,
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip
    result = play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    return str(result.turns[-1].reply).strip()


W2 = "Aggiungi «comprare il pane» alla lista e dimmi quante cose ho da fare oggi"
W2_REPLY = "Fatto, il pane è in lista. Per oggi hai due cose con scadenza: la bolletta del gas e la farmacia."


def test_w2_rep3_the_bread_with_no_date_is_in_the_total(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """§30 w2 rep 3: two due today, the bread added with no date; "due cose con scadenza" gets the total with
    its split."""
    script = [[c("add_task", title="Comprare il pane"), c("get_tasks", group="today")], W2_REPLY]
    world = {"tasks_today": ["Pagare la bolletta del gas", "Passare in farmacia"]}
    reply = _world(client, db, fake_models, secrets_key, clock, http_handler, "it-IT", W2, script,
                            world)  # fmt: skip
    assert reply == (
        f"{W2_REPLY} Hai 3 cose da fare: 2 per oggi e 1 senza scadenza: Pagare la bolletta del gas, "
        "Passare in farmacia e Comprare il pane."
    ), reply


@pytest.mark.parametrize("lang", LANGS)
def test_more_than_three_with_no_date_three_named_the_rest_counted(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    script = [[c("get_tasks", group="today")], NOTHING[lang]]
    world = {"tasks_today": ["Pagare l'affitto"], "tasks": ["U1", "U2", "U3", "U4", "U5"]}
    reply = _world(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang],
                            script, world)  # fmt: skip
    line = {
        "it-IT": "Hai 6 cose da fare: 1 per oggi e 5 senza scadenza: Pagare l'affitto, U1, U2, U3 e altre 2.",
        "en-US": "You have 6 things to do: 1 for today and 5 with no date: Pagare l'affitto, U1, U2, U3 and "
        "2 more.",
    }[lang]
    assert reply == f"{NOTHING[lang]} {line}", reply


def test_only_today_s_keeps_the_old_wording_and_no_no_date_words(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [[c("get_tasks", group="today")], NOTHING["it-IT"]]
    reply = _world(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                            WORDS["it-IT"], script, {"tasks_today": TASKS_IT})  # fmt: skip
    assert reply == f"{NOTHING['it-IT']} {LINE['it-IT']}", reply
    assert "senza scadenza" not in reply


def test_the_line_with_its_split() -> None:
    assert (
        claims.tasks_line("it-IT", ["A", "B", "C"], undated=1)
        == "Hai 3 cose da fare: 2 per oggi e 1 senza scadenza: A, B e C."
    )
    assert (
        claims.tasks_line("en-US", ["A", "B", "C"], undated=1)
        == "You have 3 things to do: 2 for today and 1 with no date: A, B and C."
    )
    assert claims.tasks_line("it-IT", ["A", "B"], undated=2) == "Hai 2 cose senza scadenza: A e B."
    assert claims.tasks_line("it-IT", ["A"], undated=1) == "Hai una cosa senza scadenza: A."
    assert claims.tasks_line("en-US", ["A", "B"], undated=2) == "You have 2 things with no date: A and B."
    assert claims.tasks_line("en-US", ["A"], undated=1) == "You have one thing with no date: A."
    assert (
        claims.tasks_line("it-IT", ["A", "B"], undated=1)
        == "Hai 2 cose da fare: 1 per oggi e 1 senza scadenza: A e B."
    )
    assert claims.tasks_line("it-IT", ["A", "B"]) == "Hai 2 cose da fare: A e B."  # 0 with no date: as before


def _task(title: str, due: datetime | None, *, start: datetime | None = None, kind: str = "date") -> Task:
    at = datetime(2026, 11, 18, 8, 0, tzinfo=ROME)
    return Task(
        id=title, title=title, notes=None, status="open", due_at=due, due_kind=kind,  # type: ignore[arg-type]
        duration_min=None, place_text=None, priority=2, source_kind="manual", source_ref=None,
        source_label=None, confidence=None, created_at=at, updated_at=at, expected_from=start,
    )  # fmt: skip


def _today(tasks: list[Task], now: datetime, timezone: str, skip: int) -> str:
    """get_tasks(today)'s text, inside its data block."""
    text = brain._today_tasks(tasks, now, timezone, skip)  # pyright: ignore[reportPrivateUsage]
    found = re.search(r"<tasks-\w+>\n(.*)\n</tasks-", text, re.DOTALL)
    return found.group(1) if found else text


def test_the_today_result_s_format() -> None:
    now = datetime(2026, 11, 18, 10, 0, tzinfo=ROME)
    day = datetime(2026, 11, 18, 23, 59, tzinfo=ROME)
    tasks = [
        _task("Vecchia", datetime(2026, 11, 16, 23, 59, tzinfo=ROME)),
        _task("Oggi", day),
        _task("Domani", datetime(2026, 11, 19, 23, 59, tzinfo=ROME)),
        _task("Senza", None),
        _task("Alle nove domani", None, start=datetime(2026, 11, 19, 9, 0, tzinfo=ROME)),  # not today's
    ]
    text = _today(tasks, now, "Europe/Rome", 0)
    assert text == (
        "3 open tasks (today): 1 for today, 1 overdue, 1 with no date. For today: Oggi (by Wed 18 Nov). "
        "Overdue: Vecchia (by Mon 16 Nov). With no date: Senza."
    ), text
    assert _today([tasks[1]], now, "Europe/Rome", 0) == "1 open tasks (today): Oggi (by Wed 18 Nov)."
    assert _today([tasks[2]], now, "Europe/Rome", 0) == (
        "No open tasks in today (none due today, overdue or with no date)."
    )


def test_w2_s_today_result_shows_the_bread_apart() -> None:
    now = datetime(2026, 11, 25, 10, 0, tzinfo=ROME)
    day = datetime(2026, 11, 25, 23, 59, tzinfo=ROME)
    tasks = [
        _task("Pagare la bolletta del gas", day),
        _task("Passare in farmacia", day),
        _task("Comprare il pane", None),
    ]
    old = (
        "2 open tasks (today): Pagare la bolletta del gas (by Wed 25 Nov); Passare in farmacia (by Wed 25 "
        "Nov)."
    )
    assert _today(tasks[:2], now, "Europe/Rome", 0) == old
    assert _today(tasks, now, "Europe/Rome", 0) == (
        "3 open tasks (today): 2 for today, 1 with no date. For today: Pagare la bolletta del gas (by Wed 25 "
        "Nov); Passare in farmacia (by Wed 25 Nov). With no date: Comprare il pane."
    )
    many = [*tasks[:1], *(_task(f"U{n}", None) for n in range(1, 8))]
    assert _today(many, now, "Europe/Rome", 0) == (
        "8 open tasks (today): 1 for today, 7 with no date. For today: Pagare la bolletta del gas (by Wed 25 "
        "Nov). With no date: U1; U2; U3; and 4 more with no date."
    )


def test_the_total_said_is_never_counted_twice() -> None:
    """§29 w1 rep 2: "non hai niente in agenda" (the calendar) and "Ti restano tre cose…" with three tasks:
    the total is said, so code adds no "Hai 3 cose da fare" after it (two answers in one message)."""
    said = "Non hai niente in agenda oggi. Ti restano tre cose da chiudere entro oggi, senza orario."
    assert claims.omits_tasks(said, THREE, asked_day=True) is False
    assert claims.omits_tasks("Non hai niente da fare oggi.", THREE, asked_day=True) is True
