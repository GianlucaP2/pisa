"""The reviewer's probes of part 2, kept as tests (made realistic: Pisa's line streams
before a barge-in; noise ends with the user's speech stopped)."""

import asyncio
import uuid
from typing import Any

import pytest

from pisa.domain.whisper import Kind, Priority, Whisper, grounding
from pisa.ports.talker import OwnerSpeaking, OwnerWords, PisaWords
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


def _seq(thoughts):
    it = iter(thoughts)

    async def think(words, trace, y):
        t = next(it)
        if isinstance(t, tuple):
            await t[1].wait()
            t = t[0]
        return t

    return think


# 1a real path: Pisa acted 1 turn ago (within 2 h); now the reply quotes an email "ho spostato": marked
# backed?
def test_1a_quote_after_recent_act_is_not_backed(
    client, db, calendar, fake_models, secrets_key, clock
) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    clock.now = rome(2026, 10, 30, 10)
    words = "metti il dentista oggi alle 17"
    put = [("create_event", {"title": "Dentista", "start": "oggi alle 17", "owner_said": words})]
    q = "cosa dice Marco?"
    model = Script({words: [put], q: ["Marco ha scritto: ho spostato la riunione alle 15."]})
    fake_models.default = model
    device = pair(db)
    conv = client.app.state.runtime.conversations
    play(client, db, secrets_key, model, words, device=device)
    frames, _, _ = play(client, db, secrets_key, model, q, device=device)
    print("FRAMES", [f for f in frames if isinstance(f, dict) and f.get("type", "").startswith("reply")][-3:])
    assert not conv.claimed(uuid.UUID(device[0])), "quoted 'ho spostato' marked backed (Pisa acted <2h ago)"


# 1a without a recent act: what does the rail do to the quote itself?
def test_1a_quote_no_act_reply_text(client, db, calendar, fake_models, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    clock.now = rome(2026, 10, 30, 10)
    q = "cosa dice Marco?"
    model = Script({q: ["Marco ha scritto: ho spostato la riunione alle 15."]})
    fake_models.default = model
    device = pair(db)
    conv = client.app.state.runtime.conversations
    frames, logs, _ = play(client, db, secrets_key, model, q, device=device)
    print("LOGS", [e for e in logs if e["event"] == "claim_without_change"])
    print("FRAMES", frames[-4:])
    assert not conv.claimed(uuid.UUID(device[0]))


# 1c: hallucinated past claim in a later turn, Pisa acted 1 turn ago: marked backed for the talker
def test_1c_unrelated_claim_within_2h_marked(client, db, calendar, fake_models, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.conftest import pair
    from tests.test_brain_loop import Script, play

    clock.now = rome(2026, 10, 30, 10)
    words = "metti il dentista oggi alle 17"
    put = [("create_event", {"title": "Dentista", "start": "oggi alle 17", "owner_said": words})]
    q = "sposta la cena con Anna a domani"
    model = Script({words: [put], q: ["Fatto, ho spostato la cena a domani."]})
    fake_models.default = model
    device = pair(db)
    conv = client.app.state.runtime.conversations
    play(client, db, secrets_key, model, words, device=device)
    play(client, db, secrets_key, model, q, device=device)
    assert not conv.claimed(uuid.UUID(device[0])), (
        "no change this turn, claim about another object marked backed"
    )


# 1b: brain whisper "Li sposto al 30." unbacked, talker "Li sposto." -> corrected
def test_1b_doing_claims() -> None:
    w = Whisper(Kind.SAY, Priority.PAUSE, "Li sposto al 30.", id="w1")
    assert grounding("Li sposto al 30.", [w], current=[w]).done_without_done


# 2a: owner barges in while Pisa says its answer to n; Pisa's words final arrive after the user's partials
def test_2a_barge_in_answer_n_not_said_twice() -> None:
    gate = asyncio.Event()
    run, s, _, _ = _run()
    run.hooks.think = _seq(
        [
            (Thought("Domani piove tutto il giorno.", Kind.SAY), gate),
            Thought("Nel pomeriggio piove forte.", Kind.SAY),
        ]
    )

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(OwnerWords("che tempo fa domani?", final=True))
        await asyncio.sleep(0.02)
        # Pisa is saying it (the talker streams it)
        s.queue.put_nowait(PisaWords("Domani piove", final=False))
        await asyncio.sleep(0.02)
        s.queue.put_nowait(OwnerSpeaking(True))
        s.queue.put_nowait(OwnerWords("e nel pomer", final=False))
        # Pisa's cut answer, its final
        s.queue.put_nowait(PisaWords("Domani piove tutto il giorno.", final=True))
        await asyncio.sleep(0.02)
        gate.set()
        await _until(lambda: len(s.whispers) >= 1)
        s.queue.put_nowait(OwnerWords("e nel pomeriggio?", final=True))
        await _until(lambda: len(s.whispers) >= 2)
        run.end("owner")
        await t

    run_talk(go())
    print("WHISPERS", [(w.text, w.priority) for w in s.whispers])
    first, second = s.whispers[:2]
    assert second.priority is not Priority.SILENT, "n+1's answer silenced by Pisa's old words for n"
    assert first.priority is Priority.SILENT, "answer to n said twice"


# 2b: owner noise: partial words and no final; Pisa's false claim is never corrected
@pytest.mark.parametrize("empty_final", [False, True])
def test_2b_noise_keeps_owner_open_and_false_claim_unchecked(empty_final: bool, monkeypatch: Any) -> None:
    monkeypatch.setattr(talk, "OWNER_FINAL_WAIT_S", 0.1)
    monkeypatch.setattr(talk, "TICK_S", 0.05)
    run, s, _, _ = _run(Thought("Sposto la cena alle 21?", Kind.ASK))

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(OwnerWords("sposta la cena alle 21", final=True))
        await _until(lambda: s.whispers)
        s.queue.put_nowait(OwnerSpeaking(True))
        s.queue.put_nowait(OwnerWords("eh", final=False))  # noise
        s.queue.put_nowait(OwnerSpeaking(False))
        if empty_final:
            s.queue.put_nowait(OwnerWords("", final=True))
        s.queue.put_nowait(PisaWords("Fatto, l'ho spostata alle 21.", final=True))
        try:
            await _until(lambda: any(w.kind is Kind.CORRECT for w in s.whispers), within=1.0)
        finally:
            print("OPEN", run._owner_open, "UNCHECKED", run._unchecked)
            run.end("owner")
            await t

    run_talk(go())


def test_2a2_barge_in_silences_n_plus_1() -> None:
    gate = asyncio.Event()
    run, s, _, _ = _run()
    run.hooks.think = _seq(
        [
            (Thought("Domani piove tutto il giorno.", Kind.SAY), gate),
            Thought("Nel pomeriggio piove.", Kind.SAY),
        ]
    )

    async def go():
        t = asyncio.create_task(run.run())
        s.queue.put_nowait(OwnerWords("che tempo fa domani?", final=True))
        await asyncio.sleep(0.02)
        # Pisa is saying it (the talker streams it)
        s.queue.put_nowait(PisaWords("Domani piove", final=False))
        await asyncio.sleep(0.02)
        s.queue.put_nowait(OwnerSpeaking(True))
        s.queue.put_nowait(OwnerWords("e nel pomer", final=False))
        s.queue.put_nowait(PisaWords("Domani piove tutto il giorno.", final=True))
        await asyncio.sleep(0.02)
        gate.set()
        await _until(lambda: len(s.whispers) >= 1)
        s.queue.put_nowait(OwnerWords("e nel pomeriggio?", final=True))
        await _until(lambda: len(s.whispers) >= 2)
        run.end("owner")
        await t

    run_talk(go())
    print("WHISPERS2", [(w.text, w.priority) for w in s.whispers])
    assert s.whispers[1].priority is not Priority.SILENT, "n+1's answer silenced by Pisa's words for n"


def _deliveries(db, secrets_key, second_tasks):  # type: ignore[no-untyped-def]
    from tests.intake_fake import IntakeModel, task
    from tests.test_intake import add_email, intake

    order = {
        "category": "delivery",
        "automated": True,
        "asks_owner_to_act": False,
        "keys": [{"kind": "order", "value": "402-7711234"}],
    }
    model = IntakeModel()
    model.triage = {"Il tuo ordine arriva domani": order, "Aggiornamento sul tuo ordine": order}
    model.extract["Il tuo ordine arriva domani"] = {
        "tasks": [
            task(
                "Ricevere il pacco Amazon",
                due_at="2026-10-21",
                due_kind="date",
                priority=2,
                expected_from="2026-10-21T10:00",
                expected_until="2026-10-21T14:00",
            )
        ]
    }
    model.extract["Aggiornamento sul tuo ordine"] = {"tasks": second_tasks(task)}
    add_email(
        db, "m-1", "Il tuo ordine arriva domani", "Ordine 402-7711234: consegna domani tra le 10 e le 14."
    )
    intake(db, secrets_key, model)
    add_email(
        db, "m-2", "Aggiornamento sul tuo ordine", "Ordine 402-7711234: ora tra le 12 e le 16; il reso…"
    )
    intake(db, secrets_key, model)
    return db.execute("SELECT title, due_at, expected_from, expected_until FROM tasks").fetchall()


# 5a: the reso next to the delivery: the window is right now, but does the reso's due date move the delivery?
def test_5a_reso_does_not_move_the_delivery_s_due(db, secrets_key) -> None:  # type: ignore[no-untyped-def]

    rows = _deliveries(
        db,
        secrets_key,
        lambda task: [
            task(
                "Consegnare il reso",
                due_at="2026-10-22",
                due_kind="date",
                priority=1,
                expected_from="2026-10-22T08:00",
                expected_until="2026-10-22T09:00",
            ),
            task(
                "Ricevere il pacco Amazon",
                due_at="2026-10-21",
                due_kind="date",
                priority=2,
                expected_from="2026-10-21T12:00",
                expected_until="2026-10-21T16:00",
            ),
        ],
    )
    print("ROWS5a", rows)
    (row,) = rows
    assert row[1].date().isoformat() == "2026-10-21", "delivery's due moved to the reso's day"


# 5b: the courier's update re-extracted with a slightly different title: does the window still move?
def test_5b_one_windowed_task_other_title_moves_window(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome

    rows = _deliveries(
        db,
        secrets_key,
        lambda task: [
            task(
                "Ricevere il pacco",
                due_at="2026-10-21",
                due_kind="date",
                priority=2,
                expected_from="2026-10-21T12:00",
                expected_until="2026-10-21T16:00",
            )
        ],
    )
    print("ROWS5b", rows)
    (row,) = rows
    assert (row[2], row[3]) == (rome(2026, 10, 21, 12), rome(2026, 10, 21, 16)), "window stale"
