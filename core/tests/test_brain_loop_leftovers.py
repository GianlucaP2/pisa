"""Part 2: the leftovers. Natural mode's backed claim marked by code, `_said_for` bounded, Grok's
early words filed under the right turn, the delivery's window by title_key, M5; each with a test that fails
without its fix (the handoff's table). The three CI timing tests now wait on conditions (their own files)."""

import asyncio
from datetime import date, datetime
from typing import Any

import pytest

from pisa.domain.actions import grounding
from pisa.domain.whisper import Kind, Priority
from pisa.ports.talker import OwnerWords, PisaWords
from pisa.services import talk
from pisa.services.talk import Thought
from tests.test_live_talk import _run, _until
from tests.virtual_time import run_talk


@pytest.fixture
def calendar(http_handler, db, secrets_key) -> Any:  # type: ignore[no-untyped-def]
    from tests.actions_fake import ActionsGoogle, connect
    from tests.assistant_env import set_settings

    fake = ActionsGoogle()
    http_handler["handler"] = fake
    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)
    return fake


# --- natural mode: a backed claim is code's mark, never the whisper's text ----------------------------------


def test_a_quoted_i_ve_moved_in_the_brain_s_words_doesn_t_let_a_false_fatto_through() -> None:
    quoted = Thought("Marco scrive: «ho spostato la riunione alle 15».", Kind.SAY)  # Pisa changed nothing
    run, session, _, _ = _run(quoted)

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("cosa dice Marco?", final=True))
        await _until(lambda: session.whispers)
        session.queue.put_nowait(PisaWords("Sì, fatto: l'ho spostata.", final=True))
        await _until(lambda: any(w.kind is Kind.CORRECT for w in session.whispers))
        run.end("owner")
        await task

    run_talk(go())
    assert run.incidents == 1  # corrected: the quote is Marco's words, not Pisa's change


def test_a_change_pisa_s_record_backs_is_marked_and_the_talker_may_confirm_it(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    import uuid

    from tests.assistant_env import rome
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    clock.now = rome(2026, 10, 30, 10)
    words = "metti il dentista oggi alle 17"
    put = [("create_event", {"title": "Dentista", "start": "oggi alle 17", "owner_said": words})]
    asked = "l'hai messo davvero?"
    model = Script(
        {words: [put], asked: ["Sì, l'ho messo alle 17."], "e il meteo?": ["Sole tutto il giorno."]}
    )
    fake_models.default = model
    device = pair(db)
    conversations = client.app.state.runtime.conversations
    play(client, db, secrets_key, model, words, device=device)
    play(client, db, secrets_key, model, asked, device=device)
    # no change this turn: the claim stands on Pisa's act a turn ago, and code marks it for the talker
    assert conversations.claimed(uuid.UUID(device[0]))
    play(client, db, secrets_key, model, "e il meteo?", device=device)
    assert not conversations.claimed(uuid.UUID(device[0]))  # nothing claimed now


# --- _said_for is bounded; Grok's words heard before the user's final belong to it -------------------------


def test_said_for_keeps_the_latest_finals_only() -> None:
    run, session, _, _ = _run(Thought("Ok.", Kind.SAY))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        for n in range(12):
            session.queue.put_nowait(OwnerWords(f"frase numero {n}", final=True))
            session.queue.put_nowait(PisaWords(f"Risposta {n}.", final=True))
        # Finals said while Pisa thinks fold into one turn, so fewer than 12 answers; the last one arrives
        await _until(lambda: run._finals == 12 and run._thought_for == 12, within=4)
        run.end("owner")
        await task

    run_talk(go())
    assert len(run._said_for) <= talk.SAID_KEPT and max(run._said_for) == 12


def test_grok_s_answer_before_the_final_transcript_is_filed_under_it_no_answering_twice() -> None:
    run, session, _, _ = _run(Thought("Domani alle 9 hai il dentista.", Kind.SAY))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("cosa ho", final=False))  # Grok: partial words, then it answers
        session.queue.put_nowait(PisaWords("Domani alle 9 hai il dentista.", final=True))
        await asyncio.sleep(0.02)
        session.queue.put_nowait(OwnerWords("cosa ho domani?", final=True))  # the final transcript, late
        await _until(lambda: session.whispers)
        run.end("owner")
        await task

    run_talk(go())
    (said,) = session.whispers
    assert said.priority is Priority.SILENT  # the talker already said it: never twice


# --- deliveries: the next email moves the window of the matching task ---------------------------------------


def test_the_next_email_moves_the_window_of_the_task_with_the_same_title_not_the_first_windowed_one(
    db, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.intake_fake import IntakeModel, task
    from tests.test_intake import add_email, intake

    order = {"category": "delivery", "automated": True, "asks_owner_to_act": False,
             "keys": [{"kind": "order", "value": "402-7711234"}]}  # fmt: skip
    model = IntakeModel()
    model.triage = {"Il tuo ordine arriva domani": order, "Aggiornamento sul tuo ordine": order}
    model.extract["Il tuo ordine arriva domani"] = {
        "tasks": [task("Ricevere il pacco Amazon", due_at="2026-10-21", due_kind="date", priority=2,
                       expected_from="2026-10-21T10:00", expected_until="2026-10-21T14:00")]
    }  # fmt: skip
    model.extract["Aggiornamento sul tuo ordine"] = {
        "tasks": [
            task("Consegnare il reso", due_at="2026-10-22", due_kind="date", priority=1,
                 expected_from="2026-10-22T08:00", expected_until="2026-10-22T09:00"),
            task("Ricevere il pacco Amazon", due_at="2026-10-21", due_kind="date", priority=2,
                 expected_from="2026-10-21T12:00", expected_until="2026-10-21T16:00"),
        ]
    }  # fmt: skip
    add_email(
        db, "m-1", "Il tuo ordine arriva domani", "Ordine 402-7711234: consegna domani tra le 10 e le 14."
    )
    intake(db, secrets_key, model)
    add_email(
        db, "m-2", "Aggiornamento sul tuo ordine", "Ordine 402-7711234: ora tra le 12 e le 16; il reso…"
    )
    intake(db, secrets_key, model)
    (row,) = db.execute(
        "SELECT expected_from, expected_until FROM tasks WHERE title = 'Ricevere il pacco Amazon'"
    ).fetchall()
    assert (row[0], row[1]) == (rome(2026, 10, 21, 12), rome(2026, 10, 21, 16))  # its own window


# --- M5: an end-only clause never borrows the start of a plan already there ---------------------------------


def test_an_end_only_clause_never_borrows_the_start_of_a_stated_plan() -> None:
    fri = date(2026, 10, 30)

    def mode(words: str, start: int, end: int) -> str:
        start_at, end_at = datetime(2026, 10, 30, start), datetime(2026, 10, 30, end)
        return grounding("calendar_create", words, read_outside=False, attendees=False, start=start_at,
                         end=end_at, default_day=fri, today=fri, title="Call con Marco")[0]  # fmt: skip

    assert (
        mode("alle 9 ho il treno, metti la call con Marco fino alle 11", 9, 11) == "held"
    )  # the 9 is the train's
    # an item of the command's own list still gives its start (the night: "alle 14 in ufficio, call … fino
    # alle 4")
    assert mode("metti alle 14 in ufficio, call con Marco fino alle quattro", 14, 16) == "words"
