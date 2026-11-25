"""Quality audit §23: one reply is one model output, plus code's own lines.

m1 (it, after a reset, "rispondimi sempre in modo breve"): "Cosa devo fare oggi?" was answered "Te la metto
nel piano? Oggi non hai eventi.": a stray offer opened the reply, and the brevity cut kept it and dropped the
three tasks. f12-it-1: "Ciao! Sto bene, grazie. E tu, come stai? Io sto bene, grazie. Come va a te?", the
answer twice. f12-en-3: "It said it would laugh at it later. Why did the scarecrow win an award?…", a stray
fragment before the joke.

The cause, replayed with the scripted model: the model wrote words AND called a tool in one response. The
stream run ends at that response's text (pydantic-ai's `run_stream`), its tool runs without its events (so the
turn's record missed the read, and the answer it backs was taken for one from memory), and the next round
answers from the result. The response's last words (no space after them, so still in the splitter) were taken
at the start of the next round, and so opened the answer; a question was said as it streamed. Now a round's
questions wait for its next sentence, and what was written before a tool the model went on to is held: said
only when no later round says anything. Under a standing "rispondimi breve" the round waits whole and the cut
keeps the answer (`research.answer_first`). A sentence said near word for word already is never said again.
"""

from typing import Any

import pytest

from pisa.domain import research, talk
from pisa.domain.research import spoken_words
from tests.replay.scenarios import c, done

N = "2026-11-18T10:00"
TASKS = {
    "it-IT": ["Pagare l'affitto", "Chiamare il dentista", "Comprare il latte"],
    "en-US": ["Pay the rent", "Call the dentist", "Buy milk"],
}
WORDS = {"it-IT": "Cosa devo fare oggi?", "en-US": "What do I have to do today?"}
BRIEF = {
    "it-IT": "D'ora in poi rispondimi sempre in modo breve",
    "en-US": "From now on, always keep your answers short",
}
OK = {"it-IT": "Va bene, risposte brevi da ora in poi.", "en-US": "Okay, short answers from now on."}
OFFER = {"it-IT": "Te la metto nel piano?", "en-US": "Shall I put it in your plan?"}
NO_EVENTS = {"it-IT": "Oggi non hai eventi.", "en-US": "You have no events today."}
ANSWER = {
    "it-IT": "Ti restano tre cose entro stasera: pagare l'affitto, chiamare il dentista e comprare il latte.",
    "en-US": "You have three things left for tonight: pay the rent, call the dentist and buy milk.",
}
FILLER = {
    "it-IT": "La giornata è abbastanza tranquilla, senza appuntamenti fissati in agenda.",
    "en-US": "The day is fairly quiet, with no appointments fixed in your calendar.",
}
LANGS = ["it-IT", "en-US"]


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          lang: str, words: str, script: list[Any], *, brief: bool = False,
          tasks: list[str] | None = None) -> str:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario: dict[str, Any] = {
        "id": "one-output", "now": N, "lang": lang, "max_questions": 0,
        "world": {"tasks_today": TASKS[lang] if tasks is None else tasks}, "turns": [{"owner": words}],
        "script": [script], "expect": {},
    }  # fmt: skip
    if brief:  # the evening before, its own conversation: the scenario's turn comes after a reset
        scenario |= {
            "taught": [BRIEF[lang]],
            "taught_script": [[[c("remember_instruction", text=BRIEF[lang])], OK[lang]]],
        }
    result = play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    return str(result.turns[-1].reply).strip()


def _has_tasks(reply: str, lang: str) -> bool:
    key = {"it-IT": ["affitto", "dentista", "latte"], "en-US": ["rent", "dentist", "milk"]}[lang]
    return all(k in reply.casefold() for k in key)


@pytest.mark.parametrize("lang", LANGS)
def test_m1_an_offer_written_before_the_read_never_opens_the_answer(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    """The audit's shape: the offer, then get_tasks, in one response; the next round answers. One answer,
    starting with the answer, the tasks in it, within the cap."""
    script = [[OFFER[lang], c("get_tasks")], f"{NO_EVENTS[lang]} {ANSWER[lang]}"]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script,
                  brief=True)  # fmt: skip
    assert OFFER[lang] not in reply and "?" not in reply, reply
    assert _has_tasks(reply, lang), reply
    assert spoken_words(reply) <= 25, reply


@pytest.mark.parametrize("lang", LANGS)
def test_m1_an_offer_with_a_space_after_it_waits_too(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    """The offer streamed whole (a space after it, so the splitter gives it at once): it waits for its round's
    next sentence, and the round went on to the read."""
    script = [[f"{OFFER[lang]} ", c("get_tasks")], ANSWER[lang]]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert reply == ANSWER[lang], reply


@pytest.mark.parametrize("lang", LANGS)
def test_m1_the_brevity_cut_keeps_the_answer(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    """The cut to "rispondimi breve" (25 words, two sentences): filler and offers go, the tasks stay. Before
    `answer_first` the cut kept the first two sentences and the tasks went: the cap cut them, not the
    model."""
    long = f"{NO_EVENTS[lang]} {FILLER[lang]} {ANSWER[lang]} {OFFER[lang]}"
    script = [[c("get_tasks")], long]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script,
                  brief=True)  # fmt: skip
    assert _has_tasks(reply, lang), reply
    assert FILLER[lang] not in reply and not reply.startswith(OFFER[lang]), reply
    assert spoken_words(reply) <= 25, reply


@pytest.mark.parametrize("lang", LANGS)
def test_words_written_before_a_tool_are_said_when_nothing_follows(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    """Held, never lost: the next round wrote nothing, so the words written before the read are the reply."""
    script = [[ANSWER[lang], c("get_tasks")], ""]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert _has_tasks(reply, lang), reply


def test_f12_it_1_the_greeting_is_said_once(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """ "Ciao! Sto bene, grazie. E tu, come stai?", a tool, then "Io sto bene, grazie. Come va a te?": the
    first round's question waits and goes; its greeting, said as it streamed, is never said again."""
    script = [
        ["Ciao! Sto bene, grazie. E tu, come stai?", c("get_tasks")],
        "Io sto bene, grazie. Come va a te?",
    ]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT", "Ciao! Come stai oggi?",
                  script, tasks=[])  # fmt: skip
    assert reply == "Ciao! Sto bene, grazie. Come va a te?", reply


def test_f12_it_1_in_one_round_the_near_duplicate_goes(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Two answers in one response, no tool: the sentence said near word for word goes. (Its second question
    stays: "Come va a te?" after "E tu, come stai?" is not the same words, and code can't tell safely.)"""
    script = ["Ciao! Sto bene, grazie. E tu, come stai? Io sto bene, grazie. Come va a te?"]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT", "Ciao! Come stai oggi?",
                  script, tasks=[])  # fmt: skip
    assert reply.count("bene, grazie") == 1, reply


def test_f12_en_3_a_stray_fragment_never_opens_the_joke(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    joke = "Why did the scarecrow win an award? Because he was outstanding in his field."
    script = [["It said it would laugh at it later.", c("get_tasks")], joke]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, "en-US", "Tell me a short joke",
                  script, tasks=[])  # fmt: skip
    assert reply == joke, reply


def test_answer_first_drops_the_offer_and_the_filler_first() -> None:
    sentences = ["Te la metto nel piano? ", "Oggi non hai eventi. ", f"{ANSWER['it-IT']} "]
    results = "Pagare l'affitto; Chiamare il dentista; Comprare il latte"
    kept, rest = research.answer_first(sentences, results, [], word_cap=25, sentence_cap=2)
    assert kept == sentences[1:] and rest == sentences[:1]
    kept, _ = research.answer_first(sentences, results, [], word_cap=18, sentence_cap=2)
    assert kept == sentences[2:]
    # nothing from a tool: the first sentences, as the cut always kept
    kept, _ = research.answer_first(["Uno due. ", "Tre quattro. ", "Cinque sei. "], "", [], sentence_cap=2)
    assert kept == ["Uno due. ", "Tre quattro. "]


@pytest.mark.parametrize(
    ("sentence", "said", "same"),
    [
        ("Io sto bene, grazie.", ["Ciao! Sto bene, grazie. E tu, come stai?"], True),
        ("I'm fine, thanks.", ["Hi! I'm fine, thanks."], True),
        ("Hai tre cose: l'affitto, il dentista e il latte.", ["Hai tre cose."], False),
        ("Hai il dentista alle 18.", ["Hai il dentista alle 17."], False),
        ("Non sto bene.", ["Sto bene."], False),
        ("Chiama Marco.", ["Chiama Mario."], False),
    ],
)
def test_near_duplicate(sentence: str, said: list[str], same: bool) -> None:
    assert talk.near_duplicate(sentence, said) is same


# --- quality audit §24 m1: a read the turn made counts before any no-read line ----------------------------
#
# After the reset, with "rispondimi sempre in modo breve", "Cosa devo fare oggi?" called get_tasks and
# get_agenda (with finish in 2 reps) and the reply was "Non ho riletto la tua giornata adesso…"; turn
# 3, get_plan and get_tasks, "Non ho riletto la tua lista adesso…". The causes, replayed: (1) the
# model answered first from what it knew, and the claim rail held those sentences for a read round;
# under the cap the round's cut had already put its rest on the screen, and once something is on the
# screen every later sentence follows it there, so the read round's answer never reached them and
# nothing was said; (2) finish beside the reads, then nothing: finish's sentence was never said, and
# the sentence held for the read stayed "unread" though the read had happened. Now a sentence held for
# a read the turn then made is a sentence written before a read (said only when nothing later is),
# finish's sentence beside a read is held the same way, a cut round that a rail left unsaid puts
# nothing on the screen, and under the cap a round's statements before a tool wait for the round that
# reads its result.

READ_HINT = "you didn't read the lists this turn"  # READ_LINE: the read round's own instructions
NOT_READ = ["riletto", "haven't checked"]
LONG = {lang: f"{NO_EVENTS[lang]} {FILLER[lang]} {ANSWER[lang]}" for lang in LANGS}
DAY_WORDS = {"it-IT": "Com'è organizzata la mia giornata?", "en-US": "How is my day organised?"}
LISTED = {
    "it-IT": "Nella tua lista ci sono tre cose: l'affitto, il dentista e il latte.",
    "en-US": "Your list has three things: the rent, the dentist and milk.",
}


def _first_then_read(first: str, read: list[Any], after: str) -> list[Any]:
    """The model answers from what it knows; told it didn't read, it reads (`read`), then says `after`."""
    return [{"hint": READ_HINT, "then": read, "else": first}, after]


def _reads() -> list[Any]:
    return [c("get_tasks"), c("get_agenda")]


def _answers(reply: str, lang: str) -> None:
    assert _has_tasks(reply, lang), reply
    assert not any(x in reply for x in NOT_READ), reply
    assert reply == reply.strip(), reply


SHAPES_24 = {
    # rep 1-2: an answer from memory over the cap, the read round, its answer
    "read_round_answers": lambda lang: _first_then_read(LONG[lang], _reads(), ANSWER[lang]),
    # finish beside the reads in the read round, then nothing
    "read_round_finish_then_nothing": lambda lang: _first_then_read(
        ANSWER[lang], [*_reads(), done(ANSWER[lang])], ""
    ),
    "read_round_finish_then_answer": lambda lang: _first_then_read(
        LONG[lang], [*_reads(), done(ANSWER[lang])], ANSWER[lang]
    ),
    # the reads and finish in the first response, then nothing
    "reads_with_finish_then_nothing": lambda lang: [[*_reads(), done(ANSWER[lang])], ""],
    # the reads beside text that takes the cap (framing), the answer in the next round
    "reads_beside_framing": lambda lang: [[f"{NO_EVENTS[lang]} {FILLER[lang]}", *_reads()], ANSWER[lang]],
    # the answer beside the reads, nothing after
    "reads_beside_the_answer": lambda lang: [[LONG[lang], *_reads()], ""],
    # a read beside text, then a read alone (its own round), then the answer
    "read_beside_text_then_a_read_alone": lambda lang: [
        [f"{NO_EVENTS[lang]} {FILLER[lang]}", c("get_agenda")],
        [c("get_tasks")],
        ANSWER[lang],
    ],
    "reads_then_the_answer": lambda lang: [_reads(), f"{NO_EVENTS[lang]} {ANSWER[lang]}"],
}


@pytest.mark.parametrize("brief", [True, False], ids=["brief", "plain"])
@pytest.mark.parametrize("shape", list(SHAPES_24))
@pytest.mark.parametrize("lang", LANGS)
def test_m1_24_a_read_the_turn_made_is_never_denied(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, shape, brief
) -> None:
    script = SHAPES_24[shape](lang)
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script,
                  brief=brief)  # fmt: skip
    _answers(reply, lang)
    if brief:
        assert spoken_words(reply) <= 25, reply


@pytest.mark.parametrize("brief", [True, False], ids=["brief", "plain"])
@pytest.mark.parametrize("lang", LANGS)
def test_m1_24_turn_3_get_plan_and_get_tasks(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, brief
) -> None:
    """Rep 3, turn 3: get_plan and get_tasks after an answer from memory, finish beside them, then nothing:
    never "Non ho riletto la tua lista adesso…"."""
    script = _first_then_read(LISTED[lang], [c("get_plan"), c("get_tasks"), done(LISTED[lang])], "")
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, DAY_WORDS[lang], script,
                  brief=brief)  # fmt: skip
    _answers(reply, lang)


def test_m1_24_with_no_read_the_no_read_line_stays(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """The rail itself: an answer from memory, and a read round that reads nothing and says nothing."""
    script = _first_then_read(ANSWER["it-IT"], [c("get_current_time")], "")
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT", WORDS["it-IT"], script,
                  brief=True)  # fmt: skip
    assert "riletto" in reply and not _has_tasks(reply, "it-IT"), reply


# --- v0.1.3: an answer written as the last words beside a tool --------------------------------------------
AFTER = {
    "it-IT": "Il dentista è la più urgente.",
    "en-US": "The dentist is the most urgent.",
}


@pytest.mark.parametrize("lang", LANGS)
def test_the_answer_as_last_words_beside_a_tool_is_said_when_the_next_round_says_something_else(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    """Without a brevity cap: the answer was the last sentence written beside a tool (no space after it, so
    held as last words) and the next round said something else: the answer was dropped, the reply was only
    the next round's sentence. A statement on the turn's subject carries into the reply, in its place."""
    script = [[ANSWER[lang], c("get_tasks")], AFTER[lang]]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert reply == f"{ANSWER[lang]} {AFTER[lang]}", reply


@pytest.mark.parametrize("lang", LANGS)
def test_the_last_words_are_said_once_when_the_next_round_says_the_same(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    script = [[ANSWER[lang], c("get_tasks")], f"{ANSWER[lang]} {AFTER[lang]}"]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert reply == f"{ANSWER[lang]} {AFTER[lang]}", reply


@pytest.mark.parametrize("lang", LANGS)
def test_a_question_as_last_words_beside_a_tool_still_waits(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang
) -> None:
    script = [[OFFER[lang], c("get_tasks")], ANSWER[lang]]
    reply = _play(client, db, fake_models, secrets_key, clock, http_handler, lang, WORDS[lang], script)
    assert reply == ANSWER[lang], reply
