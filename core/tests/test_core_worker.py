"""The worker loop rebuilt. One
long-lived runtime, the minute in three workflows, one DBOS step per job under its lock and limit, a per-job
cost line. The four risks, each tested: state leaking between minutes, clock injection, DST, a step that
raises or hangs."""

import asyncio
import itertools
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter
from psycopg_pool import AsyncConnectionPool
from structlog.testing import capture_logs

from pisa.domain.schedule import MINUTE
from pisa.infra import hard_limit, ledger
from pisa.infra import locks as lock_ids
from pisa.services import maintenance, worker_steps
from pisa.workers import main as worker
from pisa.workers.host import WorkerHost
from tests.support.minute import jobs, run_minute, step
from tests.test_worker import dbos_url  # noqa: F401  # pyright: ignore[reportUnusedImport]  (the fixture)


def rt(client: Any) -> Any:
    return client.app.state.runtime


class _Fake:
    """A runtime with a real pool and a clock, and nothing else: `run_job` needs only those."""

    def __init__(self, pool: AsyncConnectionPool, clock: Any = None) -> None:
        self.pool = pool
        self.clock = clock or (lambda: datetime.now(UTC))

    async def close(self) -> None:
        await self.pool.close(timeout=0)  # never waits for a job still running: the host must


async def _pool(url: str) -> Any:
    pool: Any = AsyncConnectionPool(url, min_size=1, max_size=6, open=False, kwargs={"autocommit": True})
    await pool.open(wait=True)
    return pool


class _Host:
    """A host that records the jobs it is asked to run (the DBOS side of the worker, without runtimes)."""

    def __init__(self, ran: list[str], stopped: bool = False) -> None:
        self.ran, self._stopped, self.busy = ran, stopped, set[str]()

    def stopped(self, group: str = "") -> bool:
        return self._stopped

    def job(self, name: str) -> dict[str, Any]:
        self.ran.append(name)
        return {"job": name, "outcome": "ok"}

    def enter(self, group: str) -> bool:
        if group in self.busy:
            return False
        self.busy.add(group)
        return True

    def leave(self, group: str) -> None:
        self.busy.discard(group)


# --- the groups ------------------------------------------------------------------------------------------


def test_the_minute_s_steps_split_into_three_groups_each_once(client) -> None:  # type: ignore[no-untyped-def]
    names = [s.name for s in worker_steps.STEPS if s.group in worker_steps.MINUTE_GROUPS]
    grouped = [n for g in worker_steps.MINUTE_GROUPS.values() for n in g]
    assert sorted(grouped) == sorted(names)  # every step in exactly one group, none invented
    assert {s.name for s in worker_steps.STEPS} - set(names) == {"gmail", "calendar"}  # their own schedules
    assert all(worker_steps.job_named(rt(client), s.name) is not None for s in worker_steps.STEPS)
    assert set(worker_steps.MINUTE_GROUPS) == {"on_time", "plan", "initiative"}
    on_time = worker_steps.MINUTE_GROUPS["on_time"]
    assert {"briefing_time", "reminders", "approvals_expire", "quiet_release"} <= set(on_time)
    assert {"done", "plan", "check_ins", "attend"} <= set(worker_steps.MINUTE_GROUPS["plan"])
    initiative = worker_steps.MINUTE_GROUPS["initiative"]
    assert {"money_back", "people", "rituals", "vault_reminders", "learn"} <= set(initiative)
    assert initiative[-1] == "learn"  # the night last in its group
    # Each group keeps the table's own order (reminders before their floors, done before the plan, …).
    for group in worker_steps.MINUTE_GROUPS:
        assert list(worker_steps.MINUTE_GROUPS[group]) == [
            n for n in names if worker_steps.BY_NAME[n].group == group
        ]


def test_three_minute_schedules_replace_every_minute() -> None:
    minute = {s.name: s for s in worker.SCHEDULES if s.cron == "0 * * * * *"}
    assert set(minute) == {"minute-on-time", "minute-plan", "minute-initiative"}
    assert "every-minute" in worker.OBSOLETE_SCHEDULES  # the deployed schedule is removed at start
    assert minute["minute-initiative"].workflow is worker.minute_initiative_workflow


def test_a_stop_skips_initiative_steps_in_every_group(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ran: list[str] = []
    fake = _Host(ran, stopped=True)
    monkeypatch.setattr(worker, "host", lambda *a: fake)
    monkeypatch.setattr(worker, "minute_start_step", lambda group: worker.host().stopped(group))
    monkeypatch.setattr(worker, "job_step", lambda name: worker.host().job(name))
    for group in worker_steps.MINUTE_GROUPS:
        worker.run_minute(group)
    every = {n for g in worker_steps.MINUTE_GROUPS.values() for n in g}
    assert set(ran) == every - worker_steps.INITIATIVE_STEPS  # their reminders, approvals, quiet's release
    assert "learn" not in ran and "reminders" in ran


def test_a_stop_that_can_t_be_read_lets_only_his_own_steps_run(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ran: list[str] = []

    def broken(group: str) -> bool:
        raise RuntimeError("database down")

    monkeypatch.setattr(worker, "host", lambda *a: _Host([]))
    monkeypatch.setattr(worker, "minute_start_step", broken)
    monkeypatch.setattr(worker, "job_step", lambda name: ran.append(name))
    with capture_logs() as logs:
        worker.run_minute("on_time")
    assert ran == [n for n in worker_steps.MINUTE_GROUPS["on_time"] if n not in worker_steps.INITIATIVE_STEPS]
    closed = [e for e in logs if e["event"] == "guard_failed_closed"]
    assert closed and closed[0]["guard"] == "minute_stop" and closed[0]["cause"] == "RuntimeError"


def test_a_group_still_in_flight_is_skipped_by_the_next_minute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ran: list[str] = []
    fake = _Host(ran)
    monkeypatch.setattr(worker, "host", lambda *a: fake)
    monkeypatch.setattr(worker, "minute_start_step", lambda group: False)
    monkeypatch.setattr(worker, "job_step", lambda name: ran.append(name))
    fake.enter("initiative")  # the previous minute's initiative run, still going
    assert worker.run_minute("initiative") == [] and ran == []
    worker.run_minute("plan")  # another group runs
    assert ran == list(worker_steps.MINUTE_GROUPS["plan"])
    fake.leave("initiative")
    worker.run_minute("initiative")
    assert ran[-1] == "learn" and "initiative" not in fake.busy  # left after its run


def test_the_host_lets_one_run_of_a_group_at_a_time() -> None:
    host = WorkerHost(None, start=_never)
    try:
        assert host.enter("plan") and not host.enter("plan") and host.enter("on_time")
        host.leave("plan")
        assert host.enter("plan")
    finally:
        host.close()


async def _never(settings: Any) -> Any:
    raise AssertionError("no runtime needed")


def test_the_on_time_group_has_its_own_host() -> None:
    for name in worker_steps.MINUTE_GROUPS["on_time"]:
        assert worker_steps.host_for(name) == worker.ON_TIME_HOST
    assert worker_steps.host_for("on_time") == worker.ON_TIME_HOST
    for name in (*worker_steps.MINUTE_GROUPS["plan"], *worker_steps.MINUTE_GROUPS["initiative"], "gmail"):
        assert worker_steps.host_for(name) == worker.MAIN_HOST
    assert worker_steps.host_for("calendar") == worker.MAIN_HOST


def test_a_stalled_main_loop_never_delays_a_reminder(test_database_url: str, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Sync CPU work on the main host's loop (Gmail's intake, the night) holds that loop only."""

    async def start(settings: Any) -> Any:
        return _Fake(await _pool(test_database_url))

    async def reminders() -> None:
        return None

    async def stall(runtime: Any) -> None:
        time.sleep(2)  # noqa: ASYNC251  (sync work: the loop can't run anything else meanwhile)

    monkeypatch.setattr(worker_steps, "job_named", jobs({"reminders": reminders}))
    main, on_time = WorkerHost(None, start=start, name="main"), WorkerHost(None, start=start, name="on_time")
    try:
        on_time.call(lambda r: _noop(), timeout_s=10)  # both up
        main.call(lambda r: _noop(), timeout_s=10)
        t = threading.Thread(target=lambda: main.call(stall, timeout_s=10))
        t.start()
        time.sleep(0.2)
        started = time.monotonic()
        assert on_time.job("reminders")["outcome"] == "ok"
        assert time.monotonic() - started < 1.0
        t.join(5)
    finally:
        main.close()
        on_time.close()


# --- risk: a step that raises or hangs ------------------------------------------------------------------


def test_risk_a_raising_step_never_stops_the_next(test_database_url: str, db) -> None:  # type: ignore[no-untyped-def]
    ran: list[str] = []

    async def boom() -> None:
        raise RuntimeError("boom")

    async def fine() -> None:
        ran.append("fine")

    async def go() -> list[str]:
        pool = await _pool(test_database_url)
        try:
            fake = _Fake(pool)
            a = await maintenance.run_job(fake, "plan", boom)  # type: ignore[arg-type]
            b = await maintenance.run_job(fake, "reminders", fine)  # type: ignore[arg-type]
            return [str(a["outcome"]), str(b["outcome"])]
        finally:
            await pool.close()

    assert asyncio.run(go()) == ["failed", "ok"] and ran == ["fine"]


def test_risk_a_hanging_step_is_cancelled_at_its_limit_and_its_lock_freed(test_database_url: str, db) -> None:  # type: ignore[no-untyped-def]
    cancelled: list[bool] = []

    async def hang() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def quick() -> None:
        return None

    async def go() -> list[str]:
        pool = await _pool(test_database_url)
        try:
            fake = _Fake(pool)
            started = time.monotonic()
            first = await maintenance.run_job(fake, "hang_probe", hang, timeout_s=0.3)  # type: ignore[arg-type]
            assert time.monotonic() - started < 3
            async with pool.connection() as conn:  # no advisory lock left on the pool's connections
                held = await (
                    await conn.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s "
                        "AND objid = (hashtext('hang_probe')::bigint & 4294967295)::oid "
                        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                        (lock_ids.JOB,),
                    )
                ).fetchone()
            assert held == (0,)
            again = await maintenance.run_job(fake, "hang_probe", quick)  # type: ignore[arg-type]
            return [str(first["outcome"]), str(again["outcome"])]
        finally:
            await pool.close()

    assert asyncio.run(go()) == ["timeout", "backoff"]  # the lock went with it; the job waits out its backoff
    assert cancelled == [True]
    assert (
        worker_steps.timeout_for("approvals_expire") == worker_steps.STEP_TIMEOUT_S < 60
    )  # within its minute
    assert worker_steps.timeout_for("learn") > worker_steps.STEP_TIMEOUT_S  # the night has its own


def test_risk_overlap_a_job_still_running_is_skipped_by_the_next_minute(test_database_url: str, db) -> None:  # type: ignore[no-untyped-def]
    async def slow() -> None:
        await asyncio.sleep(0.5)

    async def go() -> list[str]:
        pool = await _pool(test_database_url)
        try:
            fake = _Fake(pool)
            # every minute here: the lock is under test, not the night's period
            first = asyncio.create_task(maintenance.run_job(fake, "learn", slow, period=MINUTE))  # type: ignore[arg-type]
            await asyncio.sleep(0.1)
            second = await maintenance.run_job(fake, "learn", slow, period=MINUTE)  # type: ignore[arg-type]
            other = await maintenance.run_job(fake, "people", slow)  # type: ignore[arg-type]
            return [str((await first)["outcome"]), str(second["outcome"]), str(other["outcome"])]
        finally:
            await pool.close()

    assert asyncio.run(go()) == ["ok", "busy", "ok"]  # one night at a time; other jobs never wait for it


def test_risk_a_slow_night_never_delays_the_on_time_steps(test_database_url: str, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Two DBOS threads, one runtime: the night holds its own thread and lock; reminders run beside it."""
    night_running = threading.Event()

    async def night() -> None:
        night_running.set()
        await asyncio.sleep(2)

    async def reminders() -> None:
        return None

    async def start(settings: Any) -> Any:
        return _Fake(await _pool(test_database_url))

    monkeypatch.setattr(worker_steps, "job_named", jobs({"reminders": reminders, "learn": night}))
    monkeypatch.setattr(
        worker_steps, "period_for", lambda name: MINUTE
    )  # the hosts under test, not the 03:30
    host = WorkerHost(None, start=start)  # type: ignore[arg-type]
    try:
        results: dict[str, Any] = {}
        t = threading.Thread(target=lambda: results.setdefault("learn", host.job("learn")))
        t.start()
        assert night_running.wait(5)
        started = time.monotonic()
        assert host.job("reminders")["outcome"] == "ok"
        assert time.monotonic() - started < 1.0  # not 2 s behind the night
        t.join(5)
        assert results["learn"]["outcome"] == "ok"
        assert host.starts == 1
    finally:
        host.close()


def test_risk_a_step_ignoring_its_cancellation_is_left_holding_its_lock(
    test_database_url: str, db, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The host's backstop: the waiting DBOS step returns `hung`, and no second run starts beside it."""
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # swallows every cancellation

    async def start(settings: Any) -> Any:
        return _Fake(await _pool(test_database_url))

    monkeypatch.setattr(worker_steps, "job_named", jobs({"rituals": stubborn}))
    monkeypatch.setattr(worker_steps, "timeout_for", lambda name: 0.2)
    monkeypatch.setattr("pisa.workers.host.BACKSTOP_S", 0.5)
    host = WorkerHost(None, start=start)  # type: ignore[arg-type]
    try:
        assert host.job("rituals")["outcome"] == "hung"
        assert host.job("rituals")["outcome"] in {"busy", "hung"}  # never a second run beside the first
        host.call(lambda r: _set(release), timeout_s=5)
    finally:
        host.close()


async def _set(event: asyncio.Event) -> None:
    event.set()
    await asyncio.sleep(0.1)


# --- risk: state leaking between minutes ----------------------------------------------------------------


def test_risk_state_one_runtime_for_the_process_not_one_a_minute() -> None:
    built: list[int] = []

    class R:
        async def close(self) -> None:
            return None

    async def start(settings: Any) -> Any:
        built.append(1)
        return R()

    async def read(runtime: Any) -> Any:
        return runtime

    host = WorkerHost(None, start=start)  # type: ignore[arg-type]
    try:
        first = host.call(read, timeout_s=5)
        for _ in range(5):  # five minutes
            assert host.call(read, timeout_s=5) is first
        assert built == [1] and host.starts == 1
    finally:
        host.close()


def test_risk_state_a_runtime_that_fails_to_start_is_tried_again_next_minute() -> None:
    tries: list[int] = []

    class R:
        async def close(self) -> None:
            return None

    async def start(settings: Any) -> Any:
        tries.append(1)
        if len(tries) == 1:
            raise OSError("database down")
        return R()

    async def read(runtime: Any) -> Any:
        return runtime

    host = WorkerHost(None, start=start)  # type: ignore[arg-type]
    try:
        assert host.job("reminders")["outcome"] == "failed"  # minute 1: no runtime, nothing run
        first = host.call(read, timeout_s=5)  # minute 2: up
        assert host.call(read, timeout_s=5) is first  # minute 3: the same one
    finally:
        host.close()
    assert len(tries) == 2 and host.starts == 1


async def _noop() -> None:
    return None


def test_risk_state_the_minute_reads_his_zone_again(client, db) -> None:  # type: ignore[no-untyped-def]
    """The api stores the phone's zone; a worker started in Rome follows him to New York next minute."""
    runtime = rt(client)
    client.app.state.runtime.owner.place = None
    db.execute(
        "INSERT INTO owner_context (id, tz, updated_at) VALUES (true, 'America/New_York', now()) "
        "ON CONFLICT (id) DO UPDATE SET tz = EXCLUDED.tz"
    )
    client.portal.call(maintenance.minute_start, runtime)
    assert runtime.owner.tz == "America/New_York"
    db.execute("UPDATE owner_context SET tz = 'Asia/Tokyo'")
    client.portal.call(maintenance.minute_start, runtime)
    assert runtime.owner.tz == "Asia/Tokyo"
    client.app.state.runtime.owner.place = None


def test_risk_state_money_back_is_hourly_in_one_runtime(client, clock) -> None:  # type: ignore[no-untyped-def]
    """A gate held in memory (the hourly `tick`) works only in a runtime that lives past the minute: with a
    fresh one each minute it ran every minute."""
    money_back = rt(client).money_back
    assert client.portal.call(money_back.tick) == 0
    seen: list[datetime] = [money_back._ticked]
    clock.now += timedelta(minutes=1)
    client.portal.call(money_back.tick)
    assert money_back._ticked == seen[0]
    clock.now += timedelta(hours=1)
    client.portal.call(money_back.tick)
    assert money_back._ticked == clock.now


# --- risk: clock injection, and the per-job cost line ---------------------------------------------------


def test_risk_clock_a_job_s_calls_are_on_the_ledger_as_that_job(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = rt(client)
    clock.now = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)

    async def calls_the_model() -> None:
        async with runtime.pool.connection() as conn:
            for _ in range(2):
                await ledger.reserve(
                    conn, slot="s", model_ref="openai:x", estimate_eur=Decimal("0.01"), price_known=True,
                    trace_id="t", now=runtime.clock(), tz="Europe/Rome", hard=hard_limit.DEFAULT,
                )  # fmt: skip

    result = client.portal.call(maintenance.run_job, runtime, "rituals", calls_the_model)
    assert result["outcome"] == "ok" and result["calls"] == 2 and result["eur"] == "0.020000"
    rows = db.execute("SELECT job, created_at FROM llm_calls").fetchall()
    assert rows == [("rituals", clock.now), ("rituals", clock.now)]  # the injected clock, not the wall's

    async def a_turn() -> None:  # outside any job: no job on its row
        async with runtime.pool.connection() as conn:
            await ledger.reserve(
                conn, slot="s", model_ref="openai:x", estimate_eur=Decimal("0.01"), price_known=True,
                trace_id="u", now=runtime.clock(), tz="Europe/Rome", hard=hard_limit.DEFAULT,
            )  # fmt: skip

    client.portal.call(a_turn)
    assert db.execute("SELECT job FROM llm_calls WHERE trace_id = 'u'").fetchone() == (None,)


def test_risk_clock_the_steps_read_the_runtime_s_clock(client, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The night's 03:30 is the injected clock's, through the group runner, in his zone."""
    runtime = rt(client)
    ran: list[int] = []

    async def nightly() -> dict[str, int]:
        ran.append(1)
        return {"evidence": 0, "claims": 0}

    async def night_run() -> None:
        return None

    monkeypatch.setattr(runtime.learning, "nightly", nightly)
    monkeypatch.setattr(runtime.night, "run", night_run)
    monkeypatch.setattr(
        worker_steps, "job_named", lambda r, n: (lambda: worker_steps.night_tick(r)) if n == "learn" else None
    )
    rome = ZoneInfo("Europe/Rome")
    clock.now = datetime(2026, 11, 5, 3, 29, tzinfo=rome).astimezone(UTC)
    run_minute(monkeypatch, client.portal.call, runtime, ["initiative"])
    assert ran == []
    clock.now = datetime(2026, 11, 5, 3, 30, tzinfo=rome).astimezone(UTC)
    run_minute(monkeypatch, client.portal.call, runtime, ["initiative"])
    assert ran == [1]


# --- risk: DST --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("day", [datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 3, 29, tzinfo=UTC)])
def test_risk_dst_the_minute_fires_every_60_s_and_03_30_once(day: datetime) -> None:
    """The schedules' crons in Rome, as DBOS's scheduler reads them (croniter), across both DST nights: the
    minute never repeats or skips (each run's id is its own), the 03:30 prunes fire once."""
    rome = ZoneInfo("Europe/Rome")
    for s in worker.SCHEDULES:
        it = croniter(s.cron, (day - timedelta(seconds=1)).astimezone(rome), second_at_beginning=True)
        fired = []
        while (at := it.get_next(datetime)).astimezone(UTC) < day + timedelta(days=1):
            fired.append(at)
        ids = {f"sched-{s.name}-{at.isoformat()}" for at in fired}
        assert len(ids) == len(fired)
        utc = [at.astimezone(UTC) for at in fired]  # (aware times in one zone subtract by the wall clock)
        gaps = {(b - a).total_seconds() for a, b in itertools.pairwise(utc)}
        if s.cron == "0 * * * * *":
            assert gaps == {60.0} and len(fired) == 24 * 60
        if s.cron == "0 30 3 * * *":
            assert len(fired) == 1 and fired[0].astimezone(rome).hour == 3


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (datetime(2026, 10, 24, 22, tzinfo=UTC), datetime(2026, 10, 25, 2, 30, tzinfo=UTC)),  # 03:30 CET
        (datetime(2026, 3, 28, 23, tzinfo=UTC), datetime(2026, 3, 29, 1, 30, tzinfo=UTC)),  # 03:30 CEST
    ],
)
def test_risk_dst_the_night_runs_once_at_03_30_his_time(client, clock, monkeypatch, start, expected) -> None:  # type: ignore[no-untyped-def]
    runtime = rt(client)
    ran: list[datetime] = []

    async def nightly() -> dict[str, int]:
        ran.append(clock.now)
        return {"evidence": 0, "claims": 0}

    async def night_run() -> None:
        return None

    monkeypatch.setattr(runtime.learning, "nightly", nightly)
    monkeypatch.setattr(runtime.night, "run", night_run)
    client.app.state.runtime.owner.place = None
    clock.now = start
    for _ in range(6 * 60):  # every minute of the night, through the hour that repeats or vanishes
        step(client.portal.call, runtime, "learn")
        clock.now += timedelta(minutes=1)
    assert ran == [expected]


# --- DBOS: one step per job --------------------------------------------------------------------------------


def test_each_job_is_its_own_dbos_step(db, dbos_url, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from dbos import DBOS

    ran: list[str] = []

    fake = _Host(ran)
    monkeypatch.setattr(worker, "host", lambda *a: fake)
    DBOS(config={"name": "pisa-worker-test", "system_database_url": dbos_url})
    try:
        DBOS.launch()
        handle = DBOS.start_workflow(worker.minute_on_time_workflow, datetime.now(UTC), None)
        handle.get_result()
        steps: Any = DBOS.list_workflow_steps(handle.workflow_id)
    finally:
        DBOS.destroy(destroy_registry=False)
    on_time = list(worker_steps.MINUTE_GROUPS["on_time"])
    assert ran == on_time
    assert [s["function_name"].rsplit(".", 1)[-1] for s in steps] == ["minute_start_step"] + [
        "job_step"
    ] * len(on_time)
    assert [s["output"]["job"] for s in steps[1:]] == on_time  # each job's outcome checkpointed


def test_risk_a_night_cut_at_its_limit_is_marked_failed_not_done(client, db, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runtime = rt(client)

    async def slow(night: Any) -> int:
        await asyncio.sleep(5)
        return 1

    monkeypatch.setattr(runtime.night, "score", slow)
    client.app.state.runtime.owner.place = None
    clock.now = datetime(2026, 11, 5, 3, 30, tzinfo=ZoneInfo("Europe/Rome")).astimezone(UTC)
    result = client.portal.call(
        lambda: maintenance.run_job(runtime, "learn", runtime.night.run, timeout_s=0.3)
    )
    assert result["outcome"] == "timeout"
    assert db.execute("SELECT status FROM pisa_nights").fetchall() == [("failed",)]  # never `done`


# --- the review's fixes -----------------------------------------------------------------------


def test_steps_whose_own_work_passes_50_s_have_their_own_limit() -> None:
    from pisa.connectors.public_feeds import BIG_TIMEOUT

    assert (
        worker_steps.timeout_for("public_data") > (BIG_TIMEOUT.read or 0) + 60
    )  # the download, then the parse
    assert worker_steps.timeout_for("weekly_check") >= 120 and worker_steps.timeout_for("rituals") >= 120
    assert worker_steps.timeout_for("reminders") == worker_steps.STEP_TIMEOUT_S


def test_a_cut_step_backs_off_instead_of_running_every_minute(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    runtime = rt(client)
    runs: list[int] = []

    async def slow() -> None:
        runs.append(1)
        await asyncio.sleep(5)

    async def quick() -> None:
        runs.append(2)

    def run(job: Any) -> str:
        result = client.portal.call(lambda: maintenance.run_job(runtime, "public_data", job, timeout_s=0.2))
        return str(result["outcome"])

    assert run(slow) == "timeout"
    clock.now += timedelta(minutes=1)
    assert run(slow) == "backoff"  # not again the next minute
    clock.now += maintenance.BACKOFF_FIRST
    assert run(slow) == "timeout"  # tried again after 15 min: cut twice, now 30 min
    clock.now += maintenance.BACKOFF_FIRST
    assert run(quick) == "backoff"
    clock.now += maintenance.BACKOFF_FIRST
    assert run(quick) == "ok"
    assert runs == [1, 1, 2]
    assert db.execute("SELECT count(*) FROM app_settings WHERE key LIKE 'worker_backoff:%%'").fetchone() == (
        0,
    )
    assert maintenance.backoff_after(20) == maintenance.BACKOFF_MAX


def test_a_download_cut_at_the_limit_is_recorded_as_failed(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """public_data's own backoff (a day for a static dataset) holds after a cut too: no re-download a minute
    later (it skipped `_cached`'s `except Exception`, so `failed_at` was never written)."""
    public = rt(client).public

    class Hang:
        async def get(self, *a: Any, **k: Any) -> bytes:
            await asyncio.sleep(5)
            return b""

    public._fetch = Hang()

    async def go() -> None:
        await asyncio.wait_for(public._cached("rome_static"), 0.2)

    with pytest.raises(TimeoutError):
        client.portal.call(go)
    assert db.execute("SELECT failed_at FROM public_cache WHERE key = 'rome_static'").fetchone() == (
        clock.now,
    )


def test_job_locks_live_on_their_own_pool_reset_on_return(test_database_url: str, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.db.pool import open_lock_pool

    async def go() -> tuple[str, Any]:
        runtime_pool: Any = AsyncConnectionPool(
            test_database_url, min_size=1, max_size=1, open=False, kwargs={"autocommit": True}, timeout=1
        )
        await runtime_pool.open(wait=True)
        locks = await open_lock_pool(test_database_url, max_size=2)
        await locks.wait()
        try:

            async def uses_the_pool() -> None:  # the job's own connection: the runtime's only one
                async with runtime_pool.connection() as conn:
                    await conn.execute("SELECT 1")

            result = await maintenance.run_job(_Fake(runtime_pool), "med3_probe", uses_the_pool, locks=locks)  # type: ignore[arg-type]
            async with locks.connection() as conn:  # a lock left on a connection (a cancel in the unlock)
                await conn.execute("SELECT pg_advisory_lock(%s, hashtext('med3_probe'))", (lock_ids.JOB,))
            # the pool's reset runs in its background worker after the return: wait for it (≤ 2 s), so the
            # check never races it under a loaded test run (flaky under -n 6)
            held: Any = None
            for _ in range(40):
                async with runtime_pool.connection() as conn:
                    held = await (
                        await conn.execute(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s "
                            "AND objid = (hashtext('med3_probe')::bigint & 4294967295)::oid "
                            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                            (lock_ids.JOB,),
                        )
                    ).fetchone()
                if held == (0,):
                    break
                await asyncio.sleep(0.05)
            return str(result["outcome"]), held
        finally:
            await locks.close()
            await runtime_pool.close()

    # The lock never pinned the runtime's one connection; the connection returned holding none.
    assert asyncio.run(go()) == ("ok", (0,))


def test_close_waits_for_a_job_in_flight(test_database_url: str, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    finished: list[bool] = []

    async def job() -> None:
        await asyncio.sleep(0.5)
        finished.append(True)

    async def start(settings: Any) -> Any:
        return _Fake(await _pool(test_database_url))

    monkeypatch.setattr(worker_steps, "job_named", jobs({"people": job}))
    host = WorkerHost(None, start=start)
    host.call(lambda r: _noop(), timeout_s=10)
    results: list[Any] = []
    t = threading.Thread(target=lambda: results.append(host.job("people")))
    t.start()
    time.sleep(0.1)
    host.close()
    t.join(5)
    assert finished == [True] and results[0]["outcome"] == "ok"


def test_money_back_s_hour_counts_from_a_run_that_finished(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    money_back = rt(client).money_back

    async def broken(conn: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(money_back, "_reassess", broken)
    with pytest.raises(RuntimeError):
        client.portal.call(money_back.tick)
    assert money_back._ticked is None
