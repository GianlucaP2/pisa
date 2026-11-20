"""The understanding step's names fix never takes the user's verb (release audit §10, f2-en-1).

A task's title starts with what to do ("Pay the electricity bill", "Comprare il latte"). The names fix
compared spans of up to four words with the whole title, so "I paid the electricity bill" matched "Pay the
electricity bill" by sound: the small model said "same", and Pisa's model read "I Pay the electricity bill", a
thing done turned into a thing to do. The same in Italian: "ho comprato il latte" → "ho Comprare il latte".
Now the user's own verb stays as they said it; only the words after it may be fixed, to the title's own
object words, and when those are said right nothing changes at all. Synthetic words only."""

import asyncio
from datetime import date
from typing import Any

import pytest

from pisa.brain.understand import understand
from pisa.domain import understand as rules
from tests.replay import harness
from tests.replay.scenarios import c

LEX = rules.lexicon(
    [
        rules.Entry("Pay the electricity bill", "task", why="today"),
        rules.Entry("Pay the rent", "task", why="week"),
        rules.Entry("Pagare la bolletta", "task", why="today"),
        rules.Entry("Pagare la bolletta della luce", "task", why="today"),
        rules.Entry("Comprare il latte", "task", why="week"),
        rules.Entry("BOBBY'S Bitterballen", "place", why="visited"),
    ],
    date(2026, 11, 19),
)


def meant(words: str, lang: str) -> tuple[str, list[tuple[str, str]], int]:
    """What the step makes of `words` when the small model says "same" to every doubt: (meant, fixes,
    the doubts it was asked)."""
    asked: list[object] = []

    async def same(utterance: str, doubt: object) -> bool:
        asked.append(doubt)
        return True

    done = asyncio.run(understand(words, LEX, lang, same))
    return done.result.meant, [(f.frm, f.to) for f in done.result.corrections], len(asked)


@pytest.mark.parametrize(
    ("lang", "words"),
    [
        ("en-US", "I paid the electricity bill"),  # f2-en-1, word for word
        ("it-IT", "Ho pagato la bolletta"),  # the Italian equivalent
        ("it-IT", "ho pagato la bolletta della luce"),
        ("it-IT", "ho comprato il latte"),
        ("en-US", "I've paid the rent"),
    ],
)
def test_a_thing_done_keeps_the_user_s_verb_and_needs_no_question(lang: str, words: str) -> None:
    assert meant(words, lang) == (words, [], 0)


@pytest.mark.parametrize(
    ("lang", "words", "fixed", "fix"),
    [
        ("en-US", "I paid the electrisity bill", "I paid the electricity bill",
         ("the electrisity bill", "the electricity bill")),
        ("it-IT", "ho pagato la boletta", "ho pagato la bolletta", ("la boletta", "la bolletta")),
    ],
)  # fmt: skip
def test_a_misheard_object_is_fixed_and_the_verb_kept(
    lang: str, words: str, fixed: str, fix: tuple[str, str]
) -> None:
    got, fixes, _ = meant(words, lang)
    assert (got, fixes) == (fixed, [fix])


@pytest.mark.parametrize(
    ("lang", "words", "fixed"),
    [
        ("en-US", "pay the electrisity bill", "Pay the electricity bill"),  # the title's own verb: whole
        ("it-IT", "comprare il late", "Comprare il latte"),
        ("it-IT", "pagare la boletta", "Pagare la bolletta"),
        ("it-IT", "devo tornare da bobi biterbalen", "devo tornare da BOBBY'S Bitterballen"),  # a place
    ],
)
def test_the_name_fixes_that_worked_before_still_work(lang: str, words: str, fixed: str) -> None:
    assert meant(words, lang)[0] == fixed


def test_mark_done_closes_the_task_named_in_the_words_after_understanding(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, monkeypatch
) -> None:
    """f2-en-1 end to end, the small model saying "same" to any doubt (as the run's did): the model reads
    their words as they typed them, marks the bill done from them, and the bill is closed."""
    from pydantic_ai.messages import UserPromptPart

    from pisa.brain import understand as understanding
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    asked: list[str] = []

    async def same(model: Any, utterance: str, doubt: Any, report: Any) -> bool:
        asked.append(doubt.span)
        return True

    monkeypatch.setattr(understanding, "confirm", same)
    words = "I paid the electricity bill"
    scenario: dict[str, Any] = {
        "id": "f2-en-1-understood", "now": "2026-11-19T15:30", "lang": "en-US", "max_questions": 0,
        "world": {"tasks_today": ["Pay the electricity bill", "Pay the rent"]},
        "turns": [{"owner": words}],
        "script": [[[c("mark_done", what="electricity bill")], "{last}"]], "expect": {},
    }  # fmt: skip
    inner = harness.script(scenario)
    read: list[str] = []

    async def model(messages: Any, info: Any):  # type: ignore[no-untyped-def]
        read.extend(str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart))
        async for step in inner(messages, info):
            yield step

    real = harness.script
    harness.script = lambda s: model  # type: ignore[assignment]
    try:
        turn = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]
    finally:
        harness.script = real
    assert read and read[-1] == words and asked == [], (read, asked)  # their words, no name asked about
    assert [name for name, _ in turn.calls] == ["mark_done"]
    rows = dict(db.execute("SELECT title, status FROM tasks").fetchall())
    assert rows == {"Pay the electricity bill": "done", "Pay the rent": "open"}
