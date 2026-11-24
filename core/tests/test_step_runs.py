"""Once per period is the scheduler's (`maintenance.run_job` with `services/step_runs.py`): a periodic step
runs at most once a period, at its local time on the user's clock (their zone now), and the per-service
"already ran" keys were carried over once (migration 0014)."""

from datetime import time, timedelta
from typing import Any

import psycopg
import pytest

from pisa.db import migrate as runner
from pisa.db.migrate import load_migrations, migrate
from pisa.domain import schedule, zone
from pisa.domain.schedule import Period
from pisa.services import maintenance, worker_steps
from tests.assistant_env import rome
from tests.test_migrate import own_database_url  # noqa: F401  # pyright: ignore[reportUnusedImport]  (the fixture)


def rt(client: Any) -> Any:
    return client.app.state.runtime


def runner_for(client: Any, period: Period, *, fail: bool = False) -> tuple[list[Any], Any]:
    """A fake step named "test_<kind>" with `period`: run(at) sets the clock and runs it once through the
    scheduler; the list holds the instants it really ran at."""
    runtime = rt(client)
    ran: list[Any] = []
    name = f"test_{period.kind}"

    async def job() -> None:
        ran.append(runtime.clock())
        if fail:
            raise RuntimeError("boom")

    def run(clock: Any, at: Any) -> str:
        clock.now = at
        return str(
            client.portal.call(lambda: maintenance.run_job(runtime, name, job, period=period))["outcome"]
        )

    return ran, run


def recorded(db: psycopg.Connection, step: str) -> str | None:
    row = db.execute("SELECT period FROM step_runs WHERE step = %s", (step,)).fetchone()
    return row[0] if row else None


def test_a_minute_step_runs_every_minute_and_records_nothing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    ran, run = runner_for(client, schedule.MINUTE)
    assert [run(clock, rome(2026, 11, 2, 9, 0)) for _ in range(3)] == ["ok", "ok", "ok"]
    assert run(clock, rome(2026, 11, 2, 9, 1)) == "ok"
    assert len(ran) == 4 and recorded(db, "test_minute") is None


def test_a_daily_step_runs_once_a_day_from_its_local_time_while_open(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    ran, run = runner_for(client, schedule.daily(time(7, 0), open_for=timedelta(hours=2)))
    assert run(clock, rome(2026, 11, 2, 6, 59)) == "not_due"
    assert run(clock, rome(2026, 11, 2, 7, 0)) == "ok"
    assert run(clock, rome(2026, 11, 2, 7, 1)) == "not_due"  # once a day
    assert recorded(db, "test_daily") == "2026-11-02"
    assert run(clock, rome(2026, 11, 3, 9, 0)) == "not_due"  # its window closed: the day waits for the next
    assert run(clock, rome(2026, 11, 4, 8, 30)) == "ok"  # a worker down at 07:00 still runs it while open
    # the phone moves to New York: 07:00 is theirs there now (13:00 in Rome), never Rome's again
    rt(client).owner.place = zone.PhoneContext("America/New_York", "US")
    assert run(clock, rome(2026, 11, 5, 7, 0)) == "not_due"  # 01:00 in New York
    assert run(clock, rome(2026, 11, 5, 13, 0)) == "ok"
    assert len(ran) == 3


def test_a_weekly_step_runs_once_a_week_on_its_weekday_from_its_local_time(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    ran, run = runner_for(client, schedule.SUNDAY_CARD)  # Sunday 18:00, still on Monday, never from Tuesday
    assert run(clock, rome(2026, 10, 24, 18, 0)) == "not_due"  # Saturday
    assert run(clock, rome(2026, 10, 25, 17, 59)) == "not_due"
    assert run(clock, rome(2026, 10, 25, 18, 0)) == "ok"
    assert run(clock, rome(2026, 10, 25, 19, 0)) == "not_due"  # once a week
    assert run(clock, rome(2026, 10, 26, 9, 0)) == "not_due"
    assert recorded(db, "test_weekly") == "2026-10-25"
    assert run(clock, rome(2026, 11, 2, 9, 0)) == "ok"  # Monday: last Sunday's never went (a stop)
    assert run(clock, rome(2026, 11, 10, 9, 0)) == "not_due"  # Tuesday: past its window
    assert len(ran) == 2


def test_a_monthly_step_runs_once_a_month_from_the_first_at_its_time(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    ran, run = runner_for(client, schedule.monthly(time(9, 0)))
    assert run(clock, rome(2026, 11, 1, 9, 0)) == "ok"
    assert run(clock, rome(2026, 11, 15, 12, 0)) == "not_due"  # once a month
    assert run(clock, rome(2026, 12, 1, 8, 59)) == "not_due"  # still November's
    assert run(clock, rome(2026, 12, 3, 8, 0)) == "ok"  # December's, late (no window: the whole month)
    assert recorded(db, "test_monthly") == "2026-12-01"
    assert len(ran) == 2


def test_a_failed_run_is_tried_again_in_its_period_unless_spent_once_it_starts(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    daily = schedule.daily(time(7, 0))
    ran, run = runner_for(client, daily, fail=True)
    assert run(clock, rome(2026, 11, 2, 7, 0)) == "failed"
    assert recorded(db, "test_daily") is None  # given back
    assert run(clock, rome(2026, 11, 2, 7, 1)) == "failed"
    assert len(ran) == 2
    spent, run_spent = runner_for(client, Period("weekly", time(7, 0), 0, retry=False), fail=True)
    assert run_spent(clock, rome(2026, 11, 2, 7, 0)) == "failed"
    assert run_spent(clock, rome(2026, 11, 2, 7, 1)) == "not_due"  # logged once, never every minute
    assert recorded(db, "test_weekly") == "2026-11-02" and len(spent) == 1


def test_the_periodic_steps_keep_their_local_times() -> None:
    period = worker_steps.period_for
    assert period("sunday_card") == schedule.SUNDAY_CARD
    assert period("weekly_check") == schedule.WEEKLY_CHECK
    assert period("learn") == schedule.NIGHT_LEARNING
    assert period("reminders") == schedule.MINUTE and period("nonexistent") == schedule.MINUTE
    card, check, night = schedule.SUNDAY_CARD, schedule.WEEKLY_CHECK, schedule.NIGHT_LEARNING
    assert card.due(rome(2026, 10, 26, 23, 59)) == "2026-10-25" and card.due(rome(2026, 10, 27, 0, 0)) is None
    assert check.due(rome(2026, 10, 26, 17, 59)) == "2026-10-25" and check.due(rome(2026, 10, 26, 18)) is None
    assert night.due(rome(2026, 10, 26, 3, 30)) == "2026-10-26" and night.due(rome(2026, 10, 26, 9)) is None


@pytest.mark.parametrize(
    ("key", "value", "step", "period"),
    [
        ("profile_learn_ran", "2026-11-19", "learn", "2026-11-19"),
        ("profile_weekly_sent", "2026-11-15", "weekly_check", "2026-11-15"),
        ("ledger.sunday_sent", "2026-W46", "sunday_card", "2026-11-15"),  # the ISO week's Sunday
    ],
)
def test_the_upgrade_carries_each_already_ran_key_over_once(
    own_database_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
    step: str,
    period: str,
) -> None:
    every = load_migrations()
    before = [m for m in every if m.version < 14]
    monkeypatch.setattr(runner, "load_migrations", lambda: before)
    migrate(own_database_url)
    with psycopg.connect(own_database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, to_jsonb(%s::text))", (key, value))
    monkeypatch.setattr(runner, "load_migrations", lambda: every)
    assert "0014_step_runs.sql" in migrate(own_database_url)
    with psycopg.connect(own_database_url) as conn:
        assert conn.execute("SELECT period FROM step_runs WHERE step = %s", (step,)).fetchone() == (period,)
        assert conn.execute("SELECT 1 FROM app_settings WHERE key = %s", (key,)).fetchone() is None
    # the upgrade day: the step's period already ran, so it never runs twice
    upgrade_day = {
        "learn": rome(2026, 11, 19, 4, 0),
        "weekly_check": rome(2026, 11, 16, 9, 0),
        "sunday_card": rome(2026, 11, 16, 9, 0),
    }[step]
    p = worker_steps.period_for(step)
    due = p.due(upgrade_day)
    assert due is not None and due == period
    assert p.ran(due, period)
