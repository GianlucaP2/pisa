# ruff: noqa: E501
"""The rows the reviewers of the weekend fixes found (W10 on Gemini, and the
antonyms), each with one test that fails when its fix is undone."""

from typing import Any

from tests.virtual_time import run_talk

# --- N1: a negation counts only when it governs the change verb ---------------------------------------------


def test_his_go_ahead_with_a_negation_elsewhere_is_no_refusal() -> None:
    from pisa.domain.actions import asks_it, refuses

    for go in (
        "non so perché hai bisogno del sì, ti ho detto aggiungilo",
        "aggiungilo, non chiedermelo più",
        "spostalo a domani, non serve chiedere",
        "mettilo domani, non oggi",
    ):
        assert not refuses(go) and asks_it(go), go
    for no in ("no, non aggiungerlo", "non cancellare il bagnoschiuma", "lascia stare"):
        assert refuses(no) and not asks_it(no), no


# --- N2: the no-words timers stand still while Pisa thinks or speaks -------------------------------------------


def test_no_unheard_line_or_close_over_pisa_s_long_line_and_the_timers_count_from_its_end(
    monkeypatch: Any,
) -> None:
    import asyncio

    from pisa.services import talk
    from tests.test_live_talk import _run
    from tests.test_talk_timeline import VoiceLink

    monkeypatch.setattr(talk, "TICK_S", 0.01)
    monkeypatch.setattr(talk, "IDLE_AFTER_S", 0.05)
    monkeypatch.setattr(talk, "DEAF_AFTER_S", 0.05)
    monkeypatch.setattr(talk, "HAND_END_S", 0.1)
    monkeypatch.setattr(talk, "UNHEARD_SAY_S", 0.3)
    monkeypatch.setattr(talk, "UNHEARD_CLOSE_S", 0.6)
    run, session, _, _ = _run()
    run.link = VoiceLink()  # room noise all along: a voice, never a word

    def unheard() -> list[str]:
        return [w.id for w in session.whispers if w.id == "unheard_voice"]

    async def go() -> float:
        task = asyncio.create_task(run.run())
        run._speaking = True  # pyright: ignore[reportPrivateUsage]  # Pisa reads back a long email
        await asyncio.sleep(0.9)  # longer than UNHEARD_CLOSE_S
        assert not task.done() and unheard() == []  # never said over Pisa, never closed while Pisa speaks
        run._speaking = False  # pyright: ignore[reportPrivateUsage]
        ended = talk.time.monotonic()  # the talk's clock: the loop's
        await asyncio.sleep(0.15)
        assert not task.done() and unheard() == []  # counted from the line's end, not its start
        assert await asyncio.wait_for(task, 3) == "idle"
        return talk.time.monotonic() - ended

    after = run_talk(go())
    assert after >= 0.55 and unheard() == ["unheard_voice"] and "unheard" in run.close_detail


# --- W10 on Gemini: no partials, so their recent voice at the close ends their turn ------------------------------


def test_on_gemini_his_voice_at_the_close_ends_his_turn_and_his_last_words_are_kept(
    monkeypatch: Any,
) -> None:
    import asyncio

    from pisa.ports.talker import OwnerWords
    from pisa.services import talk
    from tests.test_live_talk import _run

    monkeypatch.setattr(talk, "LAST_FINAL_WAIT_S", 1.0)
    run, session, _, _ = _run()
    release = asyncio.Event()
    think = run.hooks.think

    async def slow(words: str, trace: str, yes: bool) -> Any:
        await release.wait()
        return await think(words, trace, yes)

    run.hooks.think = slow
    after: list[str] = []
    run.hooks.after = lambda words, thought: after.append(words)

    async def end_turn() -> None:  # Gemini: no partials; the final comes when the turn is ended
        session.ended_turns += 1
        session.queue.put_nowait(OwnerWords("e ricordami di chiamare Marco domani alle 9", final=True))

    session.end_turn = end_turn  # type: ignore[method-assign]

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await asyncio.sleep(0.05)
        run._voice_at = talk.time.monotonic()  # pyright: ignore[reportPrivateUsage]  # they speak; no word yet
        run.end("owner", "phone", "press")  # they press close mid-sentence
        await task
        release.set()

    run_talk(go())
    assert session.ended_turns == 1
    assert after == [
        "e ricordami di chiamare Marco domani alle 9"
    ]  # their last words, answered after the talk


# --- P2-N1: a task's `due` is a deadline, never the time to do it --------------------------------------------


def test_p2n1_his_deadline_today_is_no_block_and_stays_pinned_in_todo(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome, set_settings
    from tests.test_planner import run

    set_settings(db, {"language": "it-IT"})
    earlier = [
        add_task(db, f"Arretrato {n}", rome(2026, 11, 1).date(), priority=1) for n in range(5)
    ]  # TODO_MAX
    bread = add_task(db, "Comprare il pane", rome(2026, 11, 2, 19, 0))  # "entro le 19"
    past = add_task(db, "Pagare il bollo", rome(2026, 11, 2, 10, 0))  # "entro le 10": already past at 11
    db.execute("UPDATE tasks SET deadline_source = 'owner' WHERE due_at IS NOT NULL")
    plan = run(db, secrets_key, rome(2026, 11, 2, 11, 0), lambda p, env: p.plan())
    assert not [i for i in plan["items"] if i.get("task_id") in (bread, past)]  # never a block after it
    todo = {t["task_id"]: t for t in plan["todo"]}
    assert set(todo) == {*earlier, bread, past}  # both pinned past TODO_MAX: never lost
    assert todo[bread]["due_at"].startswith("2026-11-02T19:00") and todo[bread]["deadline_source"] == "owner"


def test_p2n1_his_alle_15_is_a_start_and_stands_at_15_on_today(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome, set_settings
    from tests.test_planner import run

    set_settings(db, {"language": "it-IT"})
    for n in range(5):
        add_task(db, f"Arretrato {n}", rome(2026, 10, 26 + n).date(), priority=1)

    async def go(planner: Any, env: Any) -> Any:  # add_task's `start` ("chiamare il notaio alle 15")
        task = await planner._tasks.create(
            title="Chiamare il notaio", source_kind="voice", start_at="2026-11-02T15:00"
        )
        return task.id, await planner.plan()

    notary, plan = run(db, secrets_key, rome(2026, 11, 2, 11, 0), go)
    (item,) = [i for i in plan["items"] if i.get("task_id") == notary]
    assert item["start"].startswith("2026-11-02T15:00") and item["scheduled_by"] == "owner"
    assert item["why"] == "All'ora che hai detto."
    assert notary not in {t["task_id"] for t in plan["todo"]}


# --- P2-H4: one turn's repeats merge, never another turn's, never an antonym --------------------------------


def test_p2h4_claims_merge_only_in_their_own_turn_and_never_an_antonym(client, db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    profile = client.app.state.runtime.profile

    def say(statement: str, words: str, turn: str) -> str:
        return asyncio.run(
            profile.remember(kind="like", statement=statement, quote=words, owner_words=words, turn=turn)
        )

    assert say("Ama correre la mattina", "amo correre la mattina", "t1").startswith("Remembered")
    # the same turn, the opposite verb: their correction, never "already remembered"
    assert say("Odia correre la mattina", "odio correre la mattina", "t1").startswith("Remembered")
    # another turn a moment later: never merged into the first turn's claim
    assert say("Ama correre la mattina presto", "amo correre la mattina presto", "t2").startswith(
        "Remembered"
    )
    # the recorded case: the same claim again in the same turn is one claim
    assert say("Ama correre la mattina presto", "amo correre la mattina presto", "t2").startswith("Already")
    assert say("Likes running in the morning", "I like running in the morning", "t3").startswith("Remembered")
    assert say("Hates running in the morning", "I hate running in the morning", "t3").startswith("Remembered")
