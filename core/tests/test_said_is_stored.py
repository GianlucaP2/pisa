"""What Pisa said is what the chat holds; a task's state is the store's.

A talk's reply is the talker's to say: a rail that finds a claim the record doesn't back, or a task's state
the store contradicts, never rewrites the reply (the chat once held a list of "Non l'ho segnato: non ho
cambiato niente." lines in place of what was said). The reply is stored as
said; one short correction line follows it, as its own message and its own whisper. The text path keeps its
hold: a false sentence is never said there.

Pisa said "Non ci sono riuscito" for a task it had just closed, and called a closed task open: a failure claim
and a task's state are checked against the turn's results and the store. Synthetic words and tasks only.
"""

import asyncio
from typing import Any

import pytest

from pisa.domain import claims
from pisa.domain.whisper import Kind, Priority
from pisa.services.talk import Thought
from tests.replay.scenarios import c

N = "2026-11-18T10:00"


def _scenario(
    lang: str, turns: list[dict[str, Any]], script: list[Any], world: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": "said-is-stored", "now": N, "lang": lang, "max_questions": 0, "world": world,
        "turns": turns, "script": script, "expect": {},
    }  # fmt: skip


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> Any:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    return play(client, db, fake_models, secrets_key, clock, http_handler, scenario)


def _stored(db: Any) -> list[str]:
    rows = db.execute(
        "SELECT content FROM messages WHERE role = 'assistant' ORDER BY created_at, id"
    ).fetchall()
    return [r[0] for r in rows]


# (lang, their words, the reply the model gives with no change made, its answer, the correction)
FALSE_CLAIM = [
    ("it-IT", "Sposta la cena di domani alle 21",
     "La cena di domani è con i tuoi cugini. L'ho spostata alle 21.",
     "La cena di domani è con i tuoi cugini.", "Correzione: non l'ho spostata."),
    ("en-US", "Move tomorrow's dinner to 9pm",
     "Tomorrow's dinner is with your cousins. I've moved it to 9pm.",
     "Tomorrow's dinner is with your cousins.", "Correction: I haven't moved it."),
]  # fmt: skip


@pytest.mark.parametrize("case", FALSE_CLAIM, ids=["it", "en"])
def test_a_talk_turn_stores_what_was_said_and_one_correction_apart(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, false, answer, fix = case
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario(lang, [{"owner": words, "voice": True}], [[false]], {}))  # fmt: skip
    turn = result.turns[-1]
    # what the talker is given to say is the answer, untouched by the rail; the correction is its own line
    assert turn.reply.strip() == answer, turn.reply
    assert turn.correction == fix
    assert claims.is_correction(turn.correction)
    # the chat holds exactly what was said, then the correction, as two messages
    assert _stored(db) == [turn.reply, fix]


def test_several_claims_give_one_correction_line_never_a_list(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    false = "La cena è con i tuoi cugini. L'ho spostata alle 21. Il dentista l'ho segnato. L'ho aggiunto."
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario("it-IT", [{"owner": "Sposta la cena alle 21 e segna il dentista",
                                        "voice": True}], [[false]], {}))  # fmt: skip
    turn = result.turns[-1]
    assert turn.correction == "Correzione: non ho fatto quelle modifiche.", turn.correction
    assert turn.reply.strip() == "La cena è con i tuoi cugini.", turn.reply
    assert _stored(db)[-1] == turn.correction and len(_stored(db)) == 2


def test_the_text_path_still_holds_before_saying(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Typed (or the phone's own voice): the false sentence is held and never said; the truth takes its
    place, one reply, no correction apart."""
    lang, words, false, answer, _ = FALSE_CLAIM[0]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario(lang, [{"owner": words}], [[false]], {}))  # fmt: skip
    turn = result.turns[-1]
    assert "L'ho spostata" not in turn.reply
    assert turn.reply.startswith(answer) and "Non l'ho spostata" in turn.reply, turn.reply
    assert turn.correction == ""
    assert _stored(db) == [turn.reply]


def test_a_stray_correction_is_left_out_of_a_talk_s_reply_with_nothing_spliced(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """correction_left_out: a correction about another topic is left out of what is said; nothing is
    appended in its place, and the chat holds what was said."""
    answer = "Domani a Lisbona ci sono 18 gradi e sole."
    stray = "Una correzione: nella risposta precedente sul treno per Porto non avevo verificato l'orario."
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario("it-IT", [{"owner": "Che tempo fa domani a Lisbona?", "voice": True}],
                             [[f"{answer} {stray}"]], {}))  # fmt: skip
    turn = result.turns[-1]
    assert turn.reply.strip() == answer, turn.reply
    assert turn.correction == ""
    assert _stored(db) == [turn.reply]


# --- task claims grounded in the tool result and the store --------------------------------------------------

CLOSED_THEN_FAILED = [
    ("it-IT", "Ho comprato il latte, toglilo dalla lista", "Comprare il latte", "latte",
     "Non ci sono riuscito.", "Invece ci sono riuscito: «Comprare il latte» è chiusa."),
    ("en-US", "I bought the milk, tick it off", "Buy milk", "milk",
     "I couldn't do it.", "Actually it worked: «Buy milk» is closed."),
]  # fmt: skip


@pytest.mark.parametrize("case", CLOSED_THEN_FAILED, ids=["it", "en"])
def test_i_couldn_t_after_the_task_was_closed_is_corrected(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, title, ref, false, truth = case
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario(lang, [{"owner": words}], [[[c("complete_task", task_ref=ref)], false]],
                             {"tasks": [title]}))  # fmt: skip
    turn = result.turns[-1]
    assert db.execute("SELECT status FROM tasks WHERE title = %s", (title,)).fetchone() == ("done",)
    assert false.rstrip(".") not in turn.reply, turn.reply
    assert truth in turn.reply, turn.reply


@pytest.mark.parametrize("case", CLOSED_THEN_FAILED, ids=["it", "en"])
def test_i_couldn_t_in_a_talk_stays_out_and_its_correction_follows(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, title, ref, false, _ = case
    answer = "Bene, uno in meno." if lang == "it-IT" else "Good, one less."
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario(lang, [{"owner": words, "voice": True}],
                             [[[c("complete_task", task_ref=ref)], f"{answer} {false}"]],
                             {"tasks": [title]}))  # fmt: skip
    turn = result.turns[-1]
    assert turn.reply.strip() == answer, turn.reply
    lead = "Correzione: ci sono riuscito" if lang == "it-IT" else "Correction: it worked"
    assert turn.correction.startswith(lead) and f"«{title}»" in turn.correction, turn.correction
    assert _stored(db) == [turn.reply, turn.correction]


DONE_SAID_OPEN = [
    ("it-IT", "Cosa mi resta da comprare?", "Comprare il latte", "Il latte è ancora da comprare.",
     "«Comprare il latte» è già chiusa."),
    ("en-US", "What do I still have to buy?", "Buy milk", "Buy milk is still open.",
     "«Buy milk» is already closed."),
]  # fmt: skip


@pytest.mark.parametrize("case", DONE_SAID_OPEN, ids=["it", "en"])
def test_a_closed_task_said_open_is_corrected_from_the_store(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, title, false, truth = case
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario(lang, [{"owner": words}], [[[c("get_tasks", status="all")], false]],
                             {"tasks_done": [title]}))  # fmt: skip
    turn = result.turns[-1]
    assert false not in turn.reply, turn.reply
    assert truth in turn.reply, turn.reply


def test_an_open_task_said_open_stands(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    said = "Il latte è ancora da comprare."
    result = _play(client, db, fake_models, secrets_key, clock, http_handler,
                   _scenario("it-IT", [{"owner": "Cosa mi resta da comprare?"}],
                             [[[c("get_tasks")], said]], {"tasks": ["Comprare il latte"]}))  # fmt: skip
    assert said in result.turns[-1].reply


def test_the_results_say_a_task_s_state_plainly() -> None:
    from datetime import UTC, datetime

    from pisa.domain.tasks import Task, state_line

    at = datetime(2026, 11, 18, 9, tzinfo=UTC)
    done = Task("t1", "Comprare il latte", None, "done", None, "none", None, None, 2, "manual", None, None,
                None, at, at)  # fmt: skip
    assert state_line(done, "Europe/Rome") == "«Comprare il latte», done (closed)"


# --- the rules ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "false"),
    [
        ("Non ci sono riuscito.", True),
        ("Non sono riuscito a chiuderla.", True),
        ("I couldn't mark it done.", True),
        ("Non sono riuscito a trovare il ristorante.", False),  # no change of theirs
        ("I couldn't find a table for four.", False),
        ("Non sono riuscito a prenotare.", False),  # a booking is none of their lists' changes
    ],
)
def test_a_failure_claim_against_a_closed_task(sentence: str, false: bool) -> None:
    closed = claims.Record(done=frozenset({"complete_task"}))
    assert claims.false_failure(sentence, closed) is false
    # nothing done this turn, or the change failed: "I couldn't" stands
    assert not claims.false_failure(sentence, claims.Record())
    assert not claims.false_failure(
        sentence, claims.Record(frozenset({"complete_task"}), frozenset({"complete_task"}))
    )


@pytest.mark.parametrize(
    ("sentence", "tasks", "wrong"),
    [
        ("Il latte è ancora da comprare.", [("Comprare il latte", "done")], ("Comprare il latte", "done")),
        ("Il latte è già chiuso.", [("Comprare il latte", "open")], ("Comprare il latte", "open")),
        ("Buy milk is still open.", [("Buy milk", "done")], ("Buy milk", "done")),
        ("Buy milk is done.", [("Buy milk", "done")], None),
        # a repeat: an open one and a done one of the same title agree with either
        (
            "Il latte è ancora da comprare.",
            [("Comprare il latte", "done"), ("Comprare il latte", "open")],
            None,
        ),
        ("Il pane è ancora da comprare.", [("Comprare il latte", "done")], None),  # names no task of theirs
        ("Il latte è ancora da comprare?", [("Comprare il latte", "done")], None),  # a question
    ],
)
def test_a_task_state_against_the_store(sentence: str, tasks: list[tuple[str, str]], wrong: Any) -> None:
    assert claims.wrong_state(sentence, tasks) == wrong


def test_the_correction_line_is_one_short_line() -> None:
    one = claims.unbacked_clause(
        ["L'ho segnato."], "it-IT", waiting=False, failed=[False], unverified=[False]
    )
    assert claims.correction("it-IT", [one]) == "Correzione: non l'ho segnato."
    many = claims.unbacked_clause(
        ["I've marked it.", "I added it."],
        "en-US",
        waiting=False,
        failed=[False, False],
        unverified=[False, False],
    )
    assert claims.correction("en-US", [many]) == "Correction: I haven't made those changes."
    assert claims.correction("it-IT", []) == ""
    assert claims.correction("it-IT", [one], lead=False) == "Non l'ho segnato."
    # never itself a claim the rail would hold
    for line in ("Correzione: non l'ho segnato.", "«Comprare il latte» è già chiusa.",
                 "Invece ci sono riuscito: «Comprare il latte» è chiusa."):  # fmt: skip
        assert claims.claim(line) is None


# --- the talk: the correction is its own whisper after the answer -------------------------------------------


def test_the_talk_says_the_correction_as_its_own_whisper() -> None:
    from pisa.ports.talker import OwnerWords
    from tests.test_live_talk import _run, _until  # pyright: ignore[reportPrivateUsage]
    from tests.virtual_time import run_talk

    answer, fix = "La cena di domani è con i tuoi cugini.", "Correzione: non l'ho spostata."
    run, session, _, _ = _run(Thought(answer, Kind.SAY, correction=fix))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("sposta la cena di domani alle 21", final=True))
        await _until(lambda: any(w.text == fix for w in session.whispers))
        run.end("owner")
        await task

    run_talk(go())
    said = [w for w in session.whispers if w.priority is not Priority.SILENT and w.kind is not Kind.HOLD]
    texts = [w.text for w in said]
    assert answer in texts and fix in texts and texts.index(fix) == texts.index(answer) + 1  # apart, after
    assert all(fix not in t or t == fix for t in texts)  # never inside the answer


def test_the_history_reads_the_correction_with_its_reply_and_the_question_stays_last() -> None:
    from pisa.services.conversation import _with_correction  # pyright: ignore[reportPrivateUsage]

    fix = "Correzione: non l'ho segnato."
    assert _with_correction("La cena è alle 20.", fix) == "La cena è alle 20. Correzione: non l'ho segnato."
    assert (
        _with_correction("La cena è alle 20. Cambio la nota?", fix)
        == "La cena è alle 20. Correzione: non l'ho segnato. Cambio la nota?"
    )


def test_the_next_turn_reads_one_reply_with_its_correction(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """The chat holds two messages; the model's history reads them as the one reply it corrects."""
    lang, words, false, answer, fix = FALSE_CLAIM[0]
    turns = [{"owner": words, "voice": True}, {"owner": "Ok grazie", "voice": True}]
    _play(client, db, fake_models, secrets_key, clock, http_handler,
          _scenario(lang, turns, [[false], ["Prego."]], {}))  # fmt: skip
    seen: list[Any] = []

    async def read() -> None:
        from pisa.services.conversation import _recent_turns  # pyright: ignore[reportPrivateUsage]

        async with client.app.state.runtime.pool.connection() as conn:
            row = await (await conn.execute("SELECT id FROM conversations LIMIT 1")).fetchone()
            seen.extend(await _recent_turns(conn, row[0]))

    client.portal.call(read)
    replies = [t.content for t in seen if t.role == "assistant"]
    assert replies[0] == f"{answer} {fix}", replies
