"""B-1: their words resume Pisa after a stop by code, in both languages, as "ferma tutto" stops it (f8-it-1:
"Ok, puoi ripartire" left Pisa stopped, and the model then said nothing had been stopped). While stopped,
their plain request resumes by code and Pisa says it resumed; a question about resuming, or words about
something else, go to the model; not stopped, the model answers. Scripted models only."""

from typing import Any

import pytest

from pisa.domain import stop as stop_rules
from tests.replay.scenarios import c

N = "2026-11-18T10:00"
# what the small model said in the transcript instead of resuming
WRONG = {"it-IT": "Non avevo fermato niente.", "en-US": "Nothing was stopped."}


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          lang: str, turns: list[tuple[str, list[Any]]], sid: str) -> Any:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario = {
        "id": sid, "now": N, "lang": lang, "max_questions": 0, "world": {},
        "turns": [{"owner": w} for w, _ in turns], "script": [s for _, s in turns], "expect": {},
    }  # fmt: skip
    return play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns


def _open_stops(db: Any) -> int:
    row = db.execute("SELECT count(*) FROM pisa_stops WHERE resumed_at IS NULL").fetchone()
    return int(row[0]) if row else 0


RESUMES = [
    ("it-IT", "Pisa, ferma tutto", "Ok, puoi ripartire"),  # the auditor's exact phrase
    ("it-IT", "ferma tutto", "Pisa, riprendi tutto"),
    ("en-US", "Stop everything", "OK, you can resume now"),
    ("en-US", "Pisa, stop everything", "Start again, please"),
]


@pytest.mark.parametrize("case", RESUMES, ids=[f"{k[0]}-{k[2]}" for k in RESUMES])
def test_their_words_resume_pisa_after_a_stop_by_code(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, stop, resume = case
    turns = _play(client, db, fake_models, secrets_key, clock, http_handler, lang,
                  [(stop, ["Va bene."]), (resume, [[c("search_tools", queries=["riprendi"])], WRONG[lang]])],
                  f"b1-{lang}")  # fmt: skip
    assert turns[0].reply.strip() == stop_rules.stopped_line(lang)
    assert turns[-1].reply.strip() == stop_rules.resumed_line(lang), turns[-1].reply
    assert turns[-1].by_code and turns[-1].calls == []  # no model round that may say nothing was stopped
    assert _open_stops(db) == 0
    assert db.execute("SELECT resumed_by FROM pisa_stops").fetchall() == [("voice",)]
    said = db.execute("SELECT role, content FROM messages").fetchall()  # the clock stands: no order
    assert {("user", resume), ("assistant", stop_rules.resumed_line(lang))} <= set(
        said
    )  # the next turn knows


@pytest.mark.parametrize(
    ("lang", "stop", "words"),
    [("it-IT", "Pisa, ferma tutto", "come faccio a ripartire?"),
     ("it-IT", "Pisa, ferma tutto", "come faccio a ripartire"),
     ("it-IT", "Pisa, ferma tutto", "riprendi la palestra domani"),
     ("en-US", "Stop everything", "How do I resume?")],
)  # fmt: skip
def test_a_question_about_resuming_goes_to_the_model_and_pisa_stays_stopped(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, stop, words
) -> None:
    turns = _play(client, db, fake_models, secrets_key, clock, http_handler, lang,
                  [(stop, ["Va bene."]), (words, ["Ok."])], f"b1-q-{lang}")  # fmt: skip
    assert turns[-1].reply.strip() == "Ok." and not turns[-1].by_code
    assert _open_stops(db) == 1


@pytest.mark.parametrize(
    ("lang", "words"), [("it-IT", "Ok, puoi ripartire"), ("en-US", "OK, you can resume now")]
)
def test_not_stopped_the_model_answers(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words
) -> None:
    turns = _play(client, db, fake_models, secrets_key, clock, http_handler, lang,
                  [(words, [WRONG[lang]])], f"b1-not-{lang}")  # fmt: skip
    assert turns[-1].reply.strip() == WRONG[lang] and not turns[-1].by_code  # true: nothing was stopped
    assert db.execute("SELECT count(*) FROM pisa_stops").fetchone() == (0,)


@pytest.mark.parametrize(
    ("words", "plain"),
    [("Ok, puoi ripartire", True), ("ok riparti pure", True), ("Pisa, riprendi", True),
     ("riprendi pure a lavorare", True), ("You can start again now", True), ("Resume everything", True),
     ("Pisa, back to work", True),
     ("come faccio a ripartire?", False), ("come faccio a ripartire", False), ("How do I resume?", False),
     ("non ripartire", False), ("don't resume", False), ("riprendi la palestra domani", False),
     ("riprendi da dove eravamo", False), ("resume the podcast", False)],
)  # fmt: skip
def test_the_plain_request_to_resume(words: str, plain: bool) -> None:
    assert stop_rules.resumes_by_code(words) is plain
