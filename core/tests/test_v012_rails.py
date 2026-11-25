"""v0.1.2 rails from the release audit's §13 (synthetic rows)."""

import pytest

from pisa.domain import tongue


@pytest.mark.parametrize(
    ("title", "lang", "foreign"),
    [
        ("Ritirare la ricetta in farmacia", "en-US", True),
        ("Rinnovare il passaporto", "en-US", True),
        ("Pranzo con Sara", "en-US", True),
        ("Call the bank", "it-IT", True),
        ("Renew passport", "en-US", False),
        ("Dinner at La Pergola", "en-US", False),  # a name's article is the name's
        ("Lunch with Sara at Da Gino", "en-US", False),
        ("Chiamare la banca", "it-IT", False),
        ("Call con il team", "it-IT", False),  # theirs, mixed as they say it
        ("Buy milk", "it-IT", False),  # no marker of either: never refused
    ],
)
def test_rules_a_title_in_another_language(title: str, lang: str, foreign: bool) -> None:
    assert tongue.foreign_title(title, lang) is foreign


# f10-en-2 (§13): "Add a task to pick up my prescription at the pharmacy tomorrow" stored "Ritirare la ricetta
# in farmacia": the chat model wrote the title in Italian. add_task gives it back once, to say in English.
CASES = [
    ("en-US", "Add a task to pick up my prescription at the pharmacy tomorrow",
     "Ritirare la ricetta in farmacia", "Pick up prescription", "Pick up prescription"),
    ("en-US", "Add 'pranzo con Sara' to my list", "pranzo con Sara", None, "pranzo con Sara"),  # said as is
    ("it-IT", "Aggiungi un'attività: chiamare la banca", "Call the bank", "Chiamare la banca",
     "Chiamare la banca"),
]  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=["en-italian-title", "en-quoted", "it-english-title"])
def test_a_title_in_another_language_goes_back_once(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    from tests.replay.scenarios import c
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    lang, words, first, second, stored = case
    rounds: list = [[c("add_task", title=first)]]
    if second is not None:
        rounds.append([c("add_task", title=second)])
    rounds.append(f"Added: {stored}.")
    scenario = {
        "id": f"v012-1-{lang}", "now": "2026-11-18T10:00", "lang": lang, "max_questions": 0, "world": {},
        "turns": [{"owner": words}], "script": [rounds], "expect": {},
    }  # fmt: skip
    turn = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]
    assert db.execute("SELECT title FROM tasks").fetchall() == [(stored,)]
    assert [n for n, _ in turn.calls] == ["add_task"] * (2 if second else 1)


# f6-it-2 (§13), over HTTP with no extension: "Preferisci che te lo prepari su WhatsApp?" was said in all 3
# reps; an offer to prepare a message is a claim this client can't open.
@pytest.mark.parametrize(
    ("sentence", "claim"),
    [
        ("Preferisci che te lo prepari su WhatsApp?", "messages"),
        ("Lo preparo su WhatsApp per Tom? Lo mandi tu.", "messages"),
        ("Shall I draft a WhatsApp for Tom?", "messages"),
        ("Want me to call the restaurant?", "calls"),
        ("Ti preparo la lista della spesa.", None),
        ("I'll prepare the agenda for tomorrow.", None),
        ("Non posso prepararti un WhatsApp da qui: scrivilo tu.", None),  # says it can't
    ],
)
def test_rules_an_offer_to_prepare_a_message_is_a_claim(sentence: str, claim: str | None) -> None:
    from pisa.domain import abilities

    assert abilities.overclaim(sentence, abilities.Facts()) == claim
    assert abilities.overclaim(sentence, abilities.Facts(messages=True, calls=True)) is None


def test_rules_the_not_connected_line_names_only_what_was_claimed_and_not_yet_said() -> None:
    from pisa.domain import abilities

    bare = abilities.Facts()
    whole = abilities.not_connected(bare, "en-US")
    assert "Google" in whole and "messages and calls" in whole
    mail = abilities.not_connected(bare, "en-US", claimed=["mail"])
    assert "Google" in mail and "messages and calls" not in mail and "maps" not in mail
    said = "Google isn't connected to Pisa, so I can't reach your mail."
    assert abilities.not_connected(bare, "en-US", claimed=["mail"], said=said) == ""
    phone = abilities.not_connected(bare, "it-IT", claimed=["messages"], said="Google non è collegato.")
    assert "messaggi e chiamate" in phone and "Google" not in phone
    assert abilities.not_connected(bare, "en-US", claimed=["booking"]) == ""


# f6-en-2 (§13): "Google isn't connected to Pisa, so I can't reach your mail…" and then the whole "Not
# connected yet: Google…; maps…; messages and calls…; notifications…" block appended.
def test_a_reply_that_already_says_google_is_off_gets_no_block(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from tests.conftest import pair_v2, set_key
    from tests.test_abilities import _ask, _model

    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    fake_models.default = _model(
        [],
        "Google isn't connected to Pisa, so I can't reach your mail. "
        "I can read your emails and reply for you.",
    )
    answer = _ask(client, token, device_id, "Did Marco reply to my email?", "en-US")
    assert "I can read your emails" not in answer  # the claim is held, as before
    assert "Not connected yet" not in answer and "notifications" not in answer
    assert "Google isn't connected to Pisa" in answer


# f2-en-2 (§14): "I bought milk, bread and eggs" with Buy milk, Buy bread and Buy eggs open: complete_task got
# the whole list as one reference, matched all three as candidates and asked which; nothing was ticked off.
CASES_DONE = [
    ("en-US", "I bought milk, bread and eggs", ["Buy milk", "Buy bread", "Buy eggs", "Pick up shirts"],
     "milk, bread and eggs", "Done: milk, bread and eggs."),
    ("it-IT", "Ho preso latte, pane e uova", ["Comprare il latte", "Comprare il pane", "Comprare le uova",
     "Ritirare le camicie"], "latte, pane e uova", "Fatto: latte, pane e uova."),
]  # fmt: skip


@pytest.mark.parametrize("case", CASES_DONE, ids=["en", "it"])
def test_one_reference_naming_several_tasks_marks_each_done(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    from tests.replay.scenarios import c
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    lang, words, tasks, ref, said = case
    scenario = {
        "id": f"v012-done-{lang}", "now": "2026-11-18T10:00", "lang": lang, "max_questions": 0,
        "world": {"tasks": tasks}, "turns": [{"owner": words}],
        "script": [[[c("complete_task", task_ref=ref)], said]], "expect": {},
    }  # fmt: skip
    _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    rows = dict(db.execute("SELECT title, status FROM tasks").fetchall())
    assert [rows[t] for t in tasks] == ["done", "done", "done", "open"]


def test_rules_a_list_reference_resolves_each_part_or_none() -> None:
    from types import SimpleNamespace
    from typing import Any

    from pisa.domain.tasks import resolve_each

    def task(n: int, title: str) -> Any:  # resolve_each reads a task's id and title only
        return SimpleNamespace(id=str(n), title=title)

    open_ = [task(1, "Buy milk"), task(2, "Buy bread"), task(3, "Buy eggs"), task(4, "Pay rent and bills")]
    assert [t.title for t in resolve_each("milk, bread and eggs", open_) or []] == [
        "Buy milk",
        "Buy bread",
        "Buy eggs",
    ]
    assert resolve_each("milk and the dentist", open_) is None  # a part that names nothing: no guess
    assert resolve_each("milk", open_) is None  # one part: resolve_task's


# f6-en-2 (§14): "I've prepared a WhatsApp to Tom instead: …" over HTTP; then "Want that?" / "Ti va?" / "Lo
# preparo?" left asking about an offer no longer said.
@pytest.mark.parametrize(
    ("sentence", "claim"),
    [
        ("I've prepared a WhatsApp to Tom instead: Hi Tom, I'll be late.", "messages"),
        ("Ti ho preparato un WhatsApp per Tom.", "messages"),
        ("I've prepared your agenda for tomorrow.", None),
    ],
)
def test_rules_the_done_form_of_an_offer_is_a_claim(sentence: str, claim: str | None) -> None:
    from pisa.domain import abilities

    assert abilities.overclaim(sentence, abilities.Facts()) == claim


@pytest.mark.parametrize(
    ("sentence", "asks"),
    [("Want that?", True), ("Ti va?", True), ("Lo preparo?", True), ("Ok.", False),
     ("Do you want me to move the dentist to Friday at 10?", False)],
)  # fmt: skip
def test_rules_a_short_question_about_it(sentence: str, asks: bool) -> None:
    from pisa.domain import abilities

    assert abilities.asks_about_it(sentence) is asks


def test_a_short_question_after_a_held_offer_goes_with_it(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from tests.conftest import pair_v2, set_key
    from tests.test_abilities import _ask, _model

    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    fake_models.default = _model([], "Sono in ritardo, capisco. Lo preparo su WhatsApp per Tom? Ti va?")
    answer = _ask(client, token, device_id, "Avvisa Tom che arrivo tardi")
    assert "WhatsApp per Tom" not in answer and "Ti va?" not in answer
    assert "Sono in ritardo, capisco." in answer
