"""Fixes at the merge, from the reviewers'
probes. A ready answer says only what Pisa's data shows and is used once; a not-done result never gets a done
receipt; the talker is corrected only on digits it said, once per exchange; Pisa's own day context never holds
a profile write; an unanswered turn's words never outlive a withdrawal."""

import asyncio
import json
from datetime import timedelta
from typing import Any

from pisa.brain.receipts import receipt_for
from pisa.brain.task_tools import _titles
from pisa.domain import results, speculation
from pisa.domain.reflex import RulesRead
from pisa.domain.state import grounded
from pisa.domain.whisper import Kind, Priority
from pisa.ports.talker import OwnerWords, PisaWords
from pisa.services.conversation import _withdraws
from pisa.services.talk import Thought
from tests.owner import owner_at
from tests.test_live_talk import _run, _until
from tests.test_one_brain import AGENDA, DEVICE, Scripted, passes
from tests.test_profile import claims, said
from tests.test_research_eval import NOW
from tests.virtual_time import run_talk


def test_a_ready_answer_says_only_what_pisa_s_data_shows() -> None:
    assert not grounded("Sì, domani hai il dentista", ["Sunday. Events: no events. Tasks due: no tasks due."])
    assert not grounded("Non hai niente domani alle 9", ["Open tasks: Paga bolletta. 9"])
    assert not grounded("Alle 17:00 hai la cena con Marco", [AGENDA + " 17:00"])  # a name not in the data
    assert not grounded("Fatto, spostato alle 17:00.", ["17:00"])  # a change claimed
    data = ["Saturday 2026-10-31. Events: 17:00-18:00 Dentista Rossi."]
    assert grounded("Domani alle 17:00 hai il Dentista Rossi.", data)


def test_a_ready_answer_is_used_once_and_never_after_a_correction(monkeypatch: Any) -> None:
    monkeypatch.setattr(speculation, "STABLE_S", 0.05)
    s = Scripted(RulesRead("question", "dentista", ["agenda"], "tomorrow"), "Domani alle 17:00.")
    run_talk(passes(s, "a che ora ho il dentista", "a che ora ho il dentista domani", gap=0.1))
    assert s.state(DEVICE).ready is not None
    # a later pass on other words (the rules, no draft) clears it: never the pre-move time again
    s.by = "rules"
    run_talk(passes(s, "spostalo alle 18", "a che ora ho il dentista domani", gap=0.1))
    assert s.state(DEVICE).ready is None


def test_a_not_done_result_inside_a_data_block_gets_no_done_receipt() -> None:
    several = _titles("Several tasks match: Paga A; Paga B. Ask the owner which one; don't guess.")
    assert results.status_of(receipt_for("rename_task", several)) == results.ERROR
    none = _titles("No open task matches 'x'. Ask the owner which task they mean.")
    assert results.status_of(receipt_for("set_task_notes", none)) == results.ERROR


def test_the_talker_is_corrected_only_on_digits_and_once() -> None:
    run, session, _, _ = _run(Thought("La call con Marco è alle 17.", Kind.SAY))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho la call?", final=True))
        session.queue.put_nowait(PisaWords("Un attimo, controllo una cosa.", final=True))  # a filler: no fact
        await _until(lambda: session.whispers or session.answers)
        for line in [
            "Hai una call con Marco alle 17.",
            "Sei a posto, alle 18 hai la call.",
            "Alle 19 allora.",
        ]:
            session.queue.put_nowait(PisaWords(line, final=True))
        await asyncio.sleep(0.2)
        run.end("owner")
        await task

    run_talk(go())
    corrections = [w for w in session.whispers if w.kind is Kind.CORRECT and w.priority is Priority.NOW]
    assert len(corrections) <= 1  # "18" and "19" are wrong, but Pisa corrects once per exchange, never a loop
    assert not any("again" in w.id for w in session.whispers[:1])  # the filler with "una" corrected nothing


def _plan(db: Any) -> None:
    plan = {"items": [{"kind": "event", "title": "Palestra", "start": (NOW + timedelta(hours=3)).isoformat(),
                       "end": (NOW + timedelta(hours=4)).isoformat()}]}  # fmt: skip
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
        "'2026-10-20', %s, 'test', 'f', %s)",
        (NOW, json.dumps(plan)),
    )


def test_pisa_s_own_day_context_never_holds_a_profile_write(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    _plan(db)
    jazz = {"kind": "like", "statement": "Ama il jazz", "quote": "Amo il jazz"}
    said(db, secrets_key, "Amo il jazz", [("remember_about_me", jazz)], with_data=True)
    assert claims(db)  # remembered at once, with today's plan in the turn


def test_an_unanswered_turn_s_words_never_outlive_a_withdrawal() -> None:
    for words in ("no lascia stare", "anzi no", "No.", "forget it", "lascia perdere, dopo vediamo"):
        assert _withdraws(words)
    assert not _withdraws("e mettila alle 5")


def test_a_filler_with_a_number_word_corrects_nothing() -> None:
    run, session, _, _ = _run(Thought("Hai la call con Marco alle 17.", Kind.SAY))

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("a che ora ho la call?", final=True))
        session.queue.put_nowait(PisaWords("Un attimo, controllo una cosa.", final=True))  # "una" is no 1
        await _until(lambda: session.whispers or session.answers)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    said = [w for w in session.whispers] + [w for _, w in session.answers]
    assert said and all(w.kind is not Kind.CORRECT for w in said)


# --- the live check: a retired reflex model (gpt-4.1-nano, 404) was called on every pass ----------------


class _Refusing:
    def __init__(self, error: Exception) -> None:
        self.error, self.calls = error, 0

    async def run(self, slot: str, call: Any, **_: Any) -> Any:
        self.calls += 1
        raise self.error


def test_a_refused_reflex_model_rests_and_the_rules_read_the_words() -> None:
    from pisa.domain.errors import PisaError
    from pisa.services.reflex import SLOW_IN_A_ROW, ReflexService
    from tests.test_one_brain import Reads

    gone = _Refusing(PisaError("provider_error", "OpenAI doesn't know the model (HTTP 404)."))
    service = ReflexService(gone, Reads(), owner=owner_at(), clock=lambda: NOW)  # type: ignore[arg-type]
    for words in ("a che ora ho il dentista", "a che ora ho il dentista domani", "cosa ho oggi pomeriggio"):
        _, by = run_talk(service._read(words, "it-IT"))  # pyright: ignore[reportPrivateUsage]
        assert by == "rules"
    assert gone.calls == 1  # one refusal, then the rules alone: no call (and no failure row) per pass

    class _Slow(_Refusing):
        async def run(self, slot: str, call: Any, **_: Any) -> Any:
            self.calls += 1
            await asyncio.sleep(0.2)

    slow = _Slow(TimeoutError())
    service = ReflexService(slow, Reads(), owner=owner_at(), clock=lambda: NOW, budget_s=0.05)  # type: ignore[arg-type]
    said = [
        run_talk(service._read("cosa ho oggi pomeriggio", "it-IT"))[1]  # pyright: ignore[reportPrivateUsage]
        for _ in range(SLOW_IN_A_ROW + 2)
    ]
    # Each read past the window is the model's, cut ("slow"); resting, the rules alone
    assert said == ["slow"] * SLOW_IN_A_ROW + ["rules"] * 2
    assert slow.calls == SLOW_IN_A_ROW  # three slow reads in a row: the model rests
