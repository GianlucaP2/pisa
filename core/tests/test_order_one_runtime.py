"""core-order round 4: replay runs 11-12. The server's replay pass plays every scenario in ONE runtime; an
approved calendar change's follow-up (Pisa's calendar synced, the day re-planned: `ApprovalsService.later`)
that was still running when the next scenario's world was reset stored a plan of the next day made from an
empty calendar, and the next scenario's `get_plan` said "Nothing planned today" beside their events. The
harness now finishes a scenario's follow-ups in its own world (`harness.settle`)."""

import asyncio
import threading
from typing import Any

from tests.replay.scenarios import SCENARIOS
from tests.test_replay import _play

# ends on their "sì" to two moves; the next one plays the same day with its own two events
_YES = next(s for s in SCENARIOS if s["id"] == "b5-16-haiku-s-va-bene-si-o-no")
_NEXT = next(s for s in SCENARIOS if s["id"] == "b5-q2-16-haiku-s-ti-sposto-cosi")


def _slow_follow_up(runtime: Any, seconds: float) -> list[str]:
    """The follow-up of an approved calendar change, slower (the plan takes ~2 s on the server): returns the
    log of when it ran."""
    original = runtime.after_calendar_change
    ran: list[str] = []

    async def slow() -> None:
        await asyncio.sleep(seconds)
        await original()
        ran.append("done")

    runtime.actions.after_calendar_change = slow
    return ran


def _plan_results(fake_models: Any) -> list[str]:
    out: list[str] = []
    for messages in fake_models.seen_messages:
        for message in messages:
            for part in getattr(message, "parts", []):
                tool = getattr(part, "tool_name", "")
                if tool in ("get_plan", "what_now") and part.part_kind == "tool-return":
                    out.append(str(part.content))
    return out


def test_a_scenario_ends_with_its_calendar_follow_up_done(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """His "sì" applied two moves: the scenario returns only once Pisa's calendar copy and plan followed
    them."""
    runtime = client.app.state.runtime
    ran = _slow_follow_up(runtime, 0.3)
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, _YES)
    assert result.passed, result.problems
    assert ran == ["done", "done"] or ran == ["done"], ran  # one follow-up per applied change, all finished
    assert all(t.done() for t in runtime.approvals._background)
    reasons = [r[0] for r in db.execute("SELECT reason FROM plans ORDER BY generated_at").fetchall()]
    assert reasons and reasons[-1] == "calendar changed", reasons


def test_the_next_scenario_reads_its_own_plan_never_the_last_ones_follow_up(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Two scenarios in one app, as the server's pass plays them: the second's plan holds its own events."""
    runtime = client.app.state.runtime
    _slow_follow_up(runtime, 0.3)
    assert _play(client, db, fake_models, secrets_key, clock, http_handler, _YES).passed
    fake_models.seen_messages.clear()
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, _NEXT)
    plans = _plan_results(fake_models)
    assert plans, "the script reads the plan"
    assert all("Call con Anna" in p and "Nothing planned today" not in p for p in plans), plans
    assert result.passed, result.problems


def test_settled_returns_with_a_follow_up_ended_on_another_loop() -> None:
    """A follow-up started on another loop (the test client's socket) that ended there, its discard never run:
    `settled` returns (it spun forever before, hanging the suite)."""
    from pisa.services.approvals import ApprovalsService

    other = asyncio.new_event_loop()
    try:
        ended = other.create_task(asyncio.sleep(0))
        other.run_until_complete(ended)
    finally:
        other.close()
    service = ApprovalsService.__new__(ApprovalsService)
    service._background = {ended}  # its discard never ran

    # in a thread of its own: a spin holds its loop (no timeout fires there), so the test fails, never hangs
    waiting = threading.Thread(target=asyncio.run, args=(service.settled(),), daemon=True)
    waiting.start()
    waiting.join(3)
    assert not waiting.is_alive(), "settled() never returned"
