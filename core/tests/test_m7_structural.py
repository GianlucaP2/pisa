"""Audit M7, hardened (arm A run 1 re-read): a phrase list kept missing false claims about their lists, so the
trigger is structural. In a turn that touched no list (no read of one, no write to one), any sentence that
refers to their lists or an item's state is held for one read round, then the honest line. And the list tools
de-duplicate from the store, so calling them is always the model's safe move. English and Italian; scripted
models only."""

from typing import Any

import pytest

from pisa.domain import claims
from pisa.domain import tasks as task_rules
from pisa.services.conversation import READ_LINE
from tests.replay.scenarios import c

N = "2026-11-18T10:00"
READ_HINT = READ_LINE.split(":")[0].strip()


def _scenario(lang: str, words: str, world: dict[str, Any], script: list[Any], sid: str) -> dict[str, Any]:
    return {
        "id": sid, "now": N, "lang": lang, "max_questions": 0, "world": world,
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip


def _turn(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> Any:  # fmt: skip
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    return _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]


# (lang, their words, the open task in the store, the false sentence from arm A run 1)
SLIPPED = [
    ("en-US", "I bought milk, bread and eggs", "Buy milk",
     "I don't have milk, bread or eggs on your list, so there's nothing to tick off."),
    ("it-IT", "Ho pagato la bolletta della luce", "Pagare la bolletta della luce",
     "La bolletta della luce risulta già chiusa in lista."),
    ("it-IT", "Ho pagato la bolletta della luce", "Pagare la bolletta della luce",
     "Già fatto: la bolletta della luce è segnata come pagata."),
    ("it-IT", "Ho rinnovato il passaporto", "Rinnovare il passaporto",
     "Già fatto: il rinnovo del passaporto è segnato come fatto."),
    ("it-IT", "Ho pagato la bolletta della luce", "Pagare la bolletta della luce",
     "Non ho una bolletta della luce aperta in lista."),
    ("it-IT", "Aggiungi ritirare la ricetta in farmacia", "Comprare il pane",
     "«Ritirare la ricetta in farmacia» è in lista per domani, non l'ho aggiunta di nuovo."),
    ("en-US", "I picked up the pharmacy prescription", "Pick up the pharmacy prescription",
     "The pharmacy prescription isn't on this list."),
    ("it-IT", "Ricordami di ordinare la pizza alle 20", "Comprare il pane",
     "Il promemoria per la pizza è già impostato."),
]  # fmt: skip
IDS = [f"{k[0]}-{n}" for n, k in enumerate(SLIPPED)]


@pytest.mark.parametrize("case", SLIPPED, ids=IDS)
def test_every_slipped_sentence_refers_to_the_lists(case: tuple[str, str, str, str]) -> None:
    sentence = case[3]
    assert claims.unread_state(sentence, read=False) is not None, sentence
    assert claims.unread_state(sentence, read=True) is None  # a list touched this turn: said as it is


@pytest.mark.parametrize(
    "sentence",
    ["Domani piove.", "It's 9 pm.", "La riunione è già iniziata.", "Is milk on your list?",
     "Ci sono 18 gradi a Roma.", "Your train leaves at 10:12."],
)  # fmt: skip
def test_sentences_about_other_things_need_no_read(sentence: str) -> None:
    assert claims.unread_state(sentence, read=False) is None


def test_the_writes_count_as_touching_the_lists() -> None:
    assert {"add_task", "complete_task", "keep_intention", "remember_to", "get_tasks"} <= claims.LIST_TOUCH
    assert "web_search" not in claims.LIST_TOUCH and "get_weather" not in claims.LIST_TOUCH


@pytest.mark.parametrize("case", SLIPPED, ids=IDS)
def test_with_no_read_the_sentence_is_held_for_a_read_round(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, task, false = case
    after = "Ho letto la lista." if lang == "it-IT" else "I read your list."
    script = [{"hint": READ_HINT, "then": [c("get_tasks")], "else": false}, after]
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": [task]}, script, "m7s-read"))  # fmt: skip
    assert false not in turn.reply and turn.reply.strip() == after, turn.reply
    assert [name for name, _ in turn.calls] == ["get_tasks"]


@pytest.mark.parametrize("case", SLIPPED, ids=IDS)
def test_a_model_that_never_reads_gets_the_honest_line(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, task, false = case
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": [task]}, [false], "m7s-never"))  # fmt: skip
    assert false not in turn.reply, turn.reply
    assert turn.reply.strip() == claims.unread_truth(lang), turn.reply


# "Già fatto" is also a done claim: after a read the claim rail (not this one) decides it (the test below)
PLAIN = [n for n, k in enumerate(SLIPPED) if not k[3].startswith("Già fatto")]


@pytest.mark.parametrize("case", [SLIPPED[n] for n in PLAIN], ids=[IDS[n] for n in PLAIN])
def test_after_a_read_the_same_sentence_is_said(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    """The rail holds only an unread sentence: after get_tasks the same words pass (right or wrong, the
    model's answer then stands on the store it read)."""
    lang, words, task, false = case
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": [task]}, [[c("get_tasks")], false],
                           "m7s-after"))  # fmt: skip
    assert turn.reply.strip() == false, turn.reply


@pytest.mark.parametrize("lang", ["en-US", "it-IT"])
def test_a_gia_fatto_after_a_read_is_not_held_by_the_list_rail(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    """ "Già fatto" / "Already done" after complete_task found it done in the store: the list rail lets it
    through (complete_task touched the list), and the model says it from the tool's own "Already done"."""
    title = "Pagare la bolletta della luce" if lang == "it-IT" else "Pay the electricity bill"
    ref = "bolletta della luce" if lang == "it-IT" else "electricity bill"
    words = "Ho pagato la bolletta della luce" if lang == "it-IT" else "I paid the electricity bill"
    said = "Era già segnata come pagata." if lang == "it-IT" else "It was already marked as paid."
    step = {"seen": "Already done", "then": said, "else": "?"}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks_done": [title]}, [[c("complete_task", task_ref=ref)], step],
                           "m7s-done"))  # fmt: skip
    assert turn.reply.strip() == said, turn.reply


# --- de-duplication in the tools ----------------------------------------------------------------------------


def test_the_same_item_is_conservative() -> None:
    assert task_rules.same_item("Comprare il latte", "comprare latte")
    assert task_rules.same_item("Buy milk", "buy  Milk!")
    assert not task_rules.same_item("Buy milk", "Buy oat milk")
    assert not task_rules.same_item("Chiamare Anna", "Chiamare Marco")


@pytest.mark.parametrize(("lang", "words", "open_title", "asked"), [
    ("en-US", "Add buy milk", "Buy milk", "buy milk"),
    ("it-IT", "Aggiungi comprare il latte", "Comprare il latte", "comprare latte"),
])  # fmt: skip
def test_add_task_on_an_open_item_says_already_and_adds_nothing(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words, open_title, asked
) -> None:
    said = "It's already on your list." if lang == "en-US" else "È già nella tua lista."
    step = {"seen": "already on the list", "then": said, "else": "?"}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": [open_title]}, [[c("add_task", title=asked)], step],
                           "m7s-dedupe"))  # fmt: skip
    assert turn.reply.strip() == said, turn.reply
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (1,)


@pytest.mark.parametrize(("lang", "title", "ref"), [
    ("en-US", "Pay the electricity bill", "electricity bill"),
    ("it-IT", "Pagare la bolletta della luce", "bolletta della luce"),
])  # fmt: skip
def test_complete_task_on_a_done_item_says_already_done(client, db, lang, title, ref) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai import Agent

    from pisa.brain.conversation import add_task_tools
    from tests.assistant_env import add_task
    from tests.test_intentions import call

    add_task(db, title, status="done")
    agent: Agent[None, str] = Agent(None)
    data = client.app.state.runtime.conversations.deps.tools.data  # pyright: ignore[reportPrivateUsage]
    add_task_tools(agent, data, lang=lambda: lang, gate=None)
    found = {n: t.function for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items()}
    out = call(client, found["complete_task"], task_ref=ref)
    assert "Already done" in out and title in out and "No open task" not in out, out


@pytest.mark.parametrize(("lang", "words", "what", "when"), [
    ("it-IT", "ricordami domani alle 19 di chiamare Anna", "chiamare Anna", "domani alle 19"),
    ("en-US", "remind me to call Anna tomorrow at 7 pm", "call Anna", "tomorrow at 19:00"),
])  # fmt: skip
def test_keep_intention_twice_says_already_kept(client, db, lang, words, what, when) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai import Agent

    from pisa.brain.loop_tools import add_loop_tools
    from pisa.brain.profile_tools import ProfileTurn
    from tests.test_intentions import call, rt
    from tests.test_rituals import _settings  # pyright: ignore[reportPrivateUsage]

    _settings(db)
    loops = rt(client).loops
    outs = []
    for _ in range(2):
        turn = ProfileTurn(words=words, lang=lang)
        agent: Agent[None, str] = Agent(None)
        add_loop_tools(agent, loops, turn, places=None, notes=None, today=lambda: rt(client).owner.today())
        found = {n: t.function for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items()}
        outs.append(call(client, found["keep_intention"], what=what, cue="time", when=when))
    assert outs[0].startswith("Kept") and outs[1].startswith("Not kept: Already kept"), outs
    assert db.execute("SELECT count(*) FROM intentions").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (1,)
