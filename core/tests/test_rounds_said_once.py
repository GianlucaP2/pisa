"""Release audit §32 D2: some replies were said twice or mixed languages. Two rounds of model text were
joined, and sometimes a line of the server's followed them:

- f11-en-2 (an en-US device, "Ciao, come va?" typed): "Hi! I'm good, thanks. Ciao, tutto bene. Oggi hai la
  giornata libera, ma due cose scadono entro stasera: … You have 2 things to do: Pay the electricity bill and
  Call the plumber." Two greetings, two languages, and the server's tasks line after a reply that had said
  them.
- f11-it-2 (it-IT, "Hi, how are you?"): "Bene, grazie. … vuoi che le metta in piano? Bene, grazie per aver
  chiesto. Oggi hai la giornata libera, …"
- w3-it-1: "Va bene, grazie. Come va da te? Ciao! E tu, come stai?"
- r2-it-1: "Oggi, mercoledì 25 novembre, non hai niente in agenda. Oggi non hai eventi. Ti restano due cose…"
- f11-en-2, "What's on my list?": "Book the flights to Lisbon, due by Monday 30 November. You have three open
  tasks: …, and book the flights to Lisbon, …"

The cause, replayed with the scripted model: a round's statements were said as they streamed, before the
model's tool call in the same response was known, and the round that read the result answered again. Now,
outside a talk, a round's words wait for its end: a round that went on to a tool is said only when no later
round says anything. A reply greets, says how Pisa is and asks back once each, a greeting only as its first
words. A typed turn is answered in the language it is written in (a language they ask for wins), and the
server's tasks line never follows their small talk. Scripted model only, synthetic titles.
"""

from typing import Any

import pytest

from pisa.domain import talk
from pisa.domain.research import asked_lang
from tests.replay.scenarios import c, done

N = "2026-11-25T08:20"
EN_TASKS = ["Pay the electricity bill", "Call the plumber"]
IT_TASKS = ["Pagare la bolletta della luce", "Chiamare l'idraulico"]
HOW_IT = "Ciao, come va?"
HOW_EN = "Hi, how are you?"
LIST_EN = "Sorry, can you answer in English? What's on my list?"
F11_SAY = (
    "Ciao, tutto bene. Oggi hai la giornata libera, ma due cose scadono entro stasera: pagare la bolletta "
    "della luce e chiamare l'idraulico."
)
F11_OFFER = "Vuoi che partiamo dalla bolletta?"


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          lang: str, turns: list[dict[str, Any]], scripts: list[Any], tasks: list[str]) -> Any:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario: dict[str, Any] = {
        "id": "rounds-said-once", "now": N, "lang": lang, "max_questions": 0,
        "world": {"tasks_today": tasks}, "turns": turns, "script": scripts, "expect": {},
    }  # fmt: skip
    return play(client, db, fake_models, secrets_key, clock, http_handler, scenario)


def _langs(db: Any) -> list[str]:
    """The language each reply was kept in, in order."""
    rows = db.execute("SELECT lang FROM messages WHERE role = 'assistant' ORDER BY id").fetchall()
    return [str(r[0]) for r in rows]


@pytest.mark.parametrize("first", ["Hi! I'm good, thanks.", "Ciao! Sto bene, grazie."])
def test_f11_en_2_one_answer_in_the_language_typed(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, first
) -> None:
    """Case 1: the words written before get_tasks (in either language) are not said once the read's round
    answers; the reply is that round's finish, in Italian, with no line of the server's; then "answer in
    English" is answered in English."""
    script = [[first, c("get_tasks", group="today")], [done(f"{F11_SAY} {F11_OFFER}")]]
    listed = "You have two things due today: pay the electricity bill and call the plumber."
    then = [[c("get_tasks")], [done(listed)]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "en-US",
                   [{"owner": HOW_IT, "typed": True}, {"owner": LIST_EN, "typed": True}], [script, then],
                   EN_TASKS)  # fmt: skip
    reply = str(result.turns[0].reply).strip()
    assert reply == F11_SAY, reply
    assert str(result.turns[1].reply).strip() == listed
    assert _langs(db) == ["it-IT", "en-US"]


def test_f11_en_2_rep_1_no_tasks_line_after_small_talk(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Case 2: the reply counted three open tasks where today has two: the server added "You have 2 things to
    do: …" to a greeting. Never on their small talk; the earlier round's question goes too."""
    say = (
        "Ciao, bene! Oggi non hai niente in piano, ma ci sono tre cose aperte: pagare la bolletta della luce "
        "e chiamare l'idraulico entro oggi, e prenotare i voli per Lisbona entro lunedì 30."
    )
    script = [
        ["Hi, I'm good, thanks. The electricity bill is due today, so want me to find a slot for it?",
         c("get_tasks")],
        [done(say)],
    ]  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "en-US",
                   [{"owner": HOW_IT, "typed": True}], [script], EN_TASKS)  # fmt: skip
    assert str(result.turns[0].reply).strip() == say


def test_f11_it_2_the_language_typed_and_one_answer(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Case 3 and the language: "Hi, how are you?" typed on an it-IT device is answered in English; the
    round before the read is not said, its offer neither."""
    say = (
        "I'm good, thanks for asking. You're free today, but two things are due tonight: the bill and the "
        "plumber."
    )
    script = [
        ["I'm good, thanks. The bill and the plumber are due tonight: want me to plan them?",
         c("get_tasks", group="today")],
        [done(say)],
    ]  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": HOW_EN, "typed": True}], [script], IT_TASKS)  # fmt: skip
    assert str(result.turns[0].reply).strip() == say
    assert _langs(db) == ["en-US"]


def test_a_voice_turn_keeps_the_device_s_language(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """A transcript can mishear: a spoken turn keeps the turn's language, as before."""
    script = [[done("Sto bene, grazie.")]]
    _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT", [{"owner": HOW_EN}], [script],
          [])  # fmt: skip
    assert _langs(db) == ["it-IT"]


def test_w3_it_1_one_question_back(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Case 4: a reply with finish beside its text, then a later round's own small talk: "Ciao!" in the middle
    and a second "E tu, come stai?" are never said."""
    first = "Va bene, grazie. Come va da te?"
    script = [[first, done(first)], [done("Ciao! Va tutto bene, grazie. E tu, come stai?")]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": HOW_IT}], [script], [])  # fmt: skip
    assert str(result.turns[0].reply).strip() == first


def test_r2_it_1_the_day_said_once(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Case 5: the day said from memory before get_tasks and get_agenda, then the read's answer: only the
    answer is said."""
    say = "Oggi non hai eventi. Ti restano due cose entro stasera: pagare l'affitto e ritirare il pacco."
    script = [
        ["Oggi, mercoledì 25 novembre, non hai niente in agenda. ", c("get_tasks", group="today"),
         c("get_agenda")],
        [say, done(say)],
    ]  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": "Fammi il riepilogo di oggi", "typed": True}], [script],
                   ["Pagare l'affitto", "Ritirare il pacco"])  # fmt: skip
    assert str(result.turns[0].reply).strip() == say


def test_r2_it_1_spoken_streams_and_never_says_it_again(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """A1: a spoken turn (economy, a talk) keeps streaming each round as it comes (the first words fast); the
    later round's sentence that says again what an earlier round said aloud is dropped ("Oggi non hai eventi."
    after "… non hai niente in agenda."), its new words kept."""
    first = "Oggi, mercoledì 25 novembre, non hai niente in agenda."
    say = "Oggi non hai eventi. Ti restano due cose entro stasera: pagare l'affitto e ritirare il pacco."
    script = [[first + " ", c("get_tasks", group="today"), c("get_agenda")], [say, done(say)]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": "Fammi il riepilogo di oggi"}], [script],
                   ["Pagare l'affitto", "Ritirare il pacco"])  # fmt: skip
    reply = str(result.turns[0].reply).strip()
    assert reply == f"{first} Ti restano due cose entro stasera: pagare l'affitto e ritirare il pacco.", reply


def test_f11_en_2_a_task_written_before_the_read_is_not_said_again(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Case 6: one task named before the read (a space after it: the splitter gave it at once), then the
    whole list: the list alone."""
    say = (
        "You have three open tasks: pay the electricity bill and call the plumber, both due today, and book "
        "the flights to Lisbon, due Monday 30 November."
    )
    script = [["Book the flights to Lisbon, due by Monday 30 November. ", c("get_tasks")], [done(say)]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "en-US",
                   [{"owner": LIST_EN, "typed": True}], [script], EN_TASKS)  # fmt: skip
    assert str(result.turns[0].reply).strip() == say


@pytest.mark.parametrize(
    ("sentence", "said", "again"),
    [
        ("Ciao, tutto bene.", ["Hi! ", "I'm good, thanks. "], True),
        ("Bene, grazie per aver chiesto.", ["Bene, grazie. "], True),
        ("E tu, come stai?", ["Va bene, grazie. ", "Come va da te? "], True),
        ("Ciao!", ["Va bene, grazie. "], True),  # a greeting only opens a reply
        ("Ciao!", [], False),
        ("Come va a te?", ["Ciao! ", "Sto bene, grazie. "], False),  # the first question back
        ("Bene, l'ho aggiunto.", ["Sto bene. "], False),  # says something else
        ("Oggi hai tre cose.", ["Ciao! "], False),
    ],
)
def test_small_talk_is_said_once(sentence: str, said: list[str], again: bool) -> None:
    assert talk.small_talk_again(sentence, said) is again


def test_a_greeting_never_opens_the_middle_of_a_reply() -> None:
    assert talk.without_greeting("Ciao, oggi hai tre cose.") == "Oggi hai tre cose."
    assert talk.without_greeting("Oggi hai tre cose.") == "Oggi hai tre cose."


@pytest.mark.parametrize(
    ("words", "lang", "answered_in"),
    [
        (HOW_IT, "en-US", "it-IT"),
        (HOW_EN, "it-IT", "en-US"),
        (LIST_EN, "en-US", "en-US"),
        ("E domani cosa devo fare?", "en-US", "it-IT"),
        ("Grazie", "en-US", "en-US"),  # one word: no language
    ],
)
def test_typed_words_are_answered_in_their_language(words: str, lang: str, answered_in: str) -> None:
    assert asked_lang(words, lang) == answered_in
