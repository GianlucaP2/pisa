"""Quality audit §23 Gate 7: a fired promise's reminder was written through attend (`attend_candidates`
names its notification), yet the candidate's outcome read `expired`.

The cause: the dispatcher marks a notice it sent to no device (no live socket, no push token: the audit's
driver keeps none) `delivered_via = 'none'`, the same mark as a notice that lapsed unsent, and the outcomes
step read every 'none' as `expired`, and taught it to the next decisions as their answer. A notice that went
out at its time is never `expired`: it has no outcome of theirs (they never saw it) and no label. The expiry
of undecided candidates never touches one whose notification was written.
"""

import asyncio
from datetime import timedelta
from typing import Any

from tests.test_proactivity import T0, _attend, _propose  # pyright: ignore[reportPrivateUsage]


def _reminder(env: Any) -> Any:
    return _propose(env, "p", "Bere acqua.", kind="reminder", urgency="now", floor=True,
                    deadline=env.clock() + timedelta(minutes=5))  # fmt: skip


def test_a_reminder_sent_to_no_device_is_never_expired(db) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with _attend(db, T0) as env:
            await _reminder(env)
            env.clock.now += timedelta(seconds=3)
            assert (await env.attend.tick()).delivered == 1
            # the dispatcher, a second later: no socket, no token
            db.execute("UPDATE notifications SET delivered_via = 'none', delivered_at = %s",
                       (env.clock() + timedelta(seconds=1),))  # fmt: skip
            for minutes in (1, 40):  # past the dispatcher's age and the "ignored" wait
                env.clock.now = T0 + timedelta(minutes=minutes)
                await env.attend.tick()

    asyncio.run(go())
    outcome, notification = db.execute("SELECT outcome, notification_id FROM attend_candidates").fetchone()
    kind = db.execute("SELECT kind FROM notifications WHERE id = %s", (notification,)).fetchone()
    assert (outcome, kind) == (None, ("reminder",))
    assert db.execute("SELECT count(*) FROM feedback_events WHERE kind = 'attend'").fetchone() == (0,)


def test_a_notice_that_lapsed_unsent_is_still_expired(db) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with _attend(db, T0) as env:
            await _propose(env, "a", "Il pacco da ritirare.")
            env.clock.now += timedelta(minutes=2)
            await env.attend.tick()
            # held past the dispatcher's age, then marked as never out
            db.execute("UPDATE notifications SET delivered_via = 'none', delivered_at = %s",
                       (env.clock() + timedelta(minutes=20),))  # fmt: skip
            env.clock.now += timedelta(minutes=25)
            await env.attend.tick()

    asyncio.run(go())
    assert db.execute("SELECT outcome FROM attend_candidates").fetchone() == ("expired",)


def test_the_expiry_never_touches_a_candidate_whose_notice_was_written(db) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        async with _attend(db, T0) as env:
            await _reminder(env)
            env.clock.now += timedelta(seconds=3)
            await env.attend.tick()
            # a decision whose bookkeeping is caught half-way: its notice written, the row still open
            db.execute("UPDATE attend_candidates SET decided_at = NULL")
            env.clock.now += timedelta(days=2)  # past every deadline and the pending day
            await env.attend.tick()

    asyncio.run(go())
    outcome, notification = db.execute("SELECT outcome, notification_id FROM attend_candidates").fetchone()
    assert outcome != "expired" and notification is not None
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (1,)  # never decided again
