"""The worker's steps, each declared once: its name, its group (the workflow and the host it runs in), its
period, its time limit, whether it is Pisa's own initiative ("Pisa, ferma tutto" pauses it), its lock and what
it runs.

A step runs every minute unless its period says otherwise (`domain/schedule.py`): then the scheduler
(`maintenance.run_job`, with `step_runs`) runs it once a period, on the user's clock, and records it; the
service keeps only the work.

Everything else reads this table: the minute's three groups (`MINUTE_GROUPS`), the steps a stop pauses
(`INITIATIVE_STEPS`), each step's limit (`timeout_for`) and host (`host_for`), and the minute's one loop
(`due`, run by `workers.main.run_minute`).

The minute in three workflows, each its own DBOS schedule, so a slow step in one (the night, a ritual's model
call) never delays another's. Within a group, the steps run in this table's order.
- on_time: what has a time: the briefing, their reminders and their floors, the approvals' lapse, quiet's
  release; then the Sunday's two pushes (18:00);
- plan: the day's plan and what reads it, in the order they read each other;
- initiative: Pisa's initiative, mostly gated hourly or daily inside; the night last.

RAIL: the initiative steps wait while stopped. A step not marked keeps running while stopped: reminders they
asked for, the floors, approvals lapsing and expiring (their own requests), quiet mode's release.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pisa.domain import schedule
from pisa.domain.schedule import Period
from pisa.infra import locks as lock_ids
from pisa.services.notifications import release_held
from pisa.services.runtime import Runtime
from pisa.services.vault import redact_turns

Job = Callable[[], Awaitable[object]]

# The groups. The first three are the minute's; the Google syncs each have their own schedule.
ON_TIME, PLAN, INITIATIVE, SYNC = "on_time", "plan", "initiative", "sync"
# The worker's hosts: the on-time group's own runtime and loop, so sync CPU work elsewhere (Gmail's intake,
# the night) never stalls a reminder; one host for the rest.
ON_TIME_HOST, MAIN_HOST = "on_time", "main"
GROUP_HOST = {ON_TIME: ON_TIME_HOST, PLAN: MAIN_HOST, INITIATIVE: MAIN_HOST, SYNC: MAIN_HOST}

# Each step's own limit. A step that runs past it is cancelled and logged, then waits out a backoff
# (`maintenance.BACKOFF_FIRST`, doubling per cut in a row, at most `BACKOFF_MAX`) so a step that can't finish
# in its limit is never cut every minute. Steps whose own work can pass 50 s have their own limit: the night
# (its 02:00-06:00 window from 03:30), a static dataset's download (public_feeds' BIG_TIMEOUT is 120 s, then
# its parse), the weekly check and the rituals (model calls), the Google syncs, the connectors.
STEP_TIMEOUT_S = 50.0


@dataclass(frozen=True)
class Step:
    name: str
    group: str
    job: Callable[[Runtime], Job]  # what it runs, from the worker's runtime
    initiative: bool = False  # Pisa's own initiative: paused by "Pisa, ferma tutto"
    timeout_s: float = STEP_TIMEOUT_S
    period: Period = schedule.MINUTE  # when it is due; a periodic one runs once a period

    @property
    def lock(self) -> tuple[int, str]:
        """Its advisory lock, `(class, hashtext(name))`: one run of a step at a time, across minutes and
        processes."""
        return (lock_ids.JOB, self.name)

    @property
    def host(self) -> str:
        return GROUP_HOST[self.group]


async def night_tick(runtime: Runtime) -> dict[str, int]:
    """Once a day from 03:30 in their zone (`schedule.NIGHT_LEARNING`), under the stop: the night's
    learning, then the night."""
    result = await runtime.learning.nightly()
    await runtime.night.run()
    return result


async def _nothing() -> None:
    return None


def _release_held(r: Runtime) -> Job:
    # their quiet hours where they are now, not at home: the runtime's Owner
    return lambda: release_held(r.pool, now=r.clock(), owner=r.owner)


def _connectors(r: Runtime) -> Job:
    connectors = getattr(r, "connectors", None)
    return connectors.tick if connectors else _nothing


STEPS: tuple[Step, ...] = (
    Step("briefing_time", ON_TIME, lambda r: r.briefings.on_time, initiative=True),  # a scheduled push
    # Reminders first: a slow plan never delays them; "leave now" reads the plan made a minute earlier.
    Step("reminders", ON_TIME, lambda r: r.reminders.run),
    # the floors reminders proposed go out now, before the plan's slower steps.
    Step("attend_floors", ON_TIME, lambda r: r.attend.floors),
    # Pisa closing items by itself; before the plan and the check-ins, which read it
    Step("done", PLAN, lambda r: r.done.tick, initiative=True),
    Step("plan", PLAN, lambda r: r.planner.tick, initiative=True),  # the day's re-plans
    # follow-ups; after the plan: it reads the latest one
    Step("check_ins", PLAN, lambda r: r.follow.tick, initiative=True),
    # follow-ups: tasks overdue for days, one question, then gone
    Step("aging", PLAN, lambda r: r.follow.aging, initiative=True),
    # Pisa speaking first; after the senses: what they proposed this minute is decided (a floor at once).
    Step("attend", PLAN, lambda r: r.attend.tick, initiative=True),
    # the lock screen's next stop (its pushes)
    Step("live_activity", PLAN, lambda r: r.live_activity.tick, initiative=True),
    Step("approvals_lapsing", ON_TIME, lambda r: r.approvals.lapsing),  # said once before they lapse
    Step("approvals_expire", ON_TIME, lambda r: r.approvals.expire),  # unanswered for 10 minutes
    # their quiet hours where they are now; on time, before the Sunday's pushes
    Step("quiet_release", ON_TIME, _release_held),
    # what the gate held goes at a breakpoint or in the digest (the holds' releases and digests: Pisa
    # speaking first)
    Step("guardian", ON_TIME, lambda r: r.guardian.tick, initiative=True),
    # the one "com'è andata?" after a tap (Pisa speaking first)
    Step("handoffs", INITIATIVE, lambda r: r.handoffs.tick, initiative=True),
    # Pisa putting claims owed, late refunds and supplies in the pool (hourly inside)
    Step("money_back", INITIATIVE, lambda r: r.money_back.tick, initiative=True),
    # Sunday from 18:00, once a week (a scheduled push; a model call)
    Step(
        "weekly_check",
        ON_TIME,
        lambda r: r.learning.weekly_tick,
        initiative=True,
        timeout_s=120.0,
        period=schedule.WEEKLY_CHECK,
    ),
    # the user loop: the week's talk report (numbers, no push), their corrections and quick undos as
    # anonymised scenario candidates; before the card, which reads the report
    Step("owner_loop", ON_TIME, lambda r: r.autopilot.owner_loop.tick),
    # Sunday 18:00, the week with Pisa (a scheduled push)
    Step(
        "sunday_card",
        ON_TIME,
        lambda r: r.autopilot.ledger.sunday_tick,
        initiative=True,
        period=schedule.SUNDAY_CARD,
    ),
    # Pisa putting renewals in the pool
    Step("vault_reminders", INITIATIVE, lambda r: r.autopilot.vault.tick, initiative=True),
    # the research's first ten reminders put in the pool
    Step("vault_starters", INITIATIVE, lambda r: r.autopilot.vault.starters, initiative=True),
    Step("vault_redact", INITIATIVE, lambda r: lambda: redact_turns(r.pool, r.clock())),
    # the garden withers, scheduled skills run (Pisa speaking first)
    Step("skills", INITIATIVE, lambda r: r.autopilot.skills.tick, initiative=True),
    Step(
        "personal_evals", INITIATIVE, lambda r: r.autopilot.evals.scan_undos
    ),  # a quick undo: a private test
    # the Ghost Run, Chiudiamo, the week's poster, something different (Pisa speaking first; model calls)
    Step("rituals", INITIATIVE, lambda r: r.rituals.tick, initiative=True, timeout_s=120.0),
    # Pisa putting birthdays in the pool, the orbits' and thank-yous' groundwork (hourly)
    Step("people", INITIATIVE, lambda r: r.people.tick, initiative=True),
    # one errand combo a day at most (Pisa speaking first)
    Step("errands", INITIATIVE, lambda r: r.errands.tick, initiative=True),
    # a wanted city's stops, weekly (no push); a static dataset's download and parse
    Step("public_data", INITIATIVE, lambda r: r.public.tick, timeout_s=300.0),
    # the connectors that are on, when their sync is due; PSD2 renewals; the radar's reminders into the pool,
    # the syncs Pisa starts by itself. A bank's pages, a PEC inbox, Garmin's day: network calls in a row
    Step("connectors", INITIATIVE, _connectors, initiative=True, timeout_s=240.0),
    # 3 days before a trip abroad, once (Pisa speaking first)
    Step("trip_leaving", INITIATIVE, lambda r: r.trips.leaving, initiative=True),
    # what they did becomes evidence, then claims; 03:30 in their zone, once a day, paused by a stop (Pisa's
    # initiative). Last of the initiative group, with its own long limit: a slow night holds no other group.
    Step(
        "learn",
        INITIATIVE,
        lambda r: lambda: night_tick(r),
        initiative=True,
        timeout_s=2 * 3600.0,
        period=schedule.NIGHT_LEARNING,
    ),
    # the Google syncs, each on its own schedule (not the minute's), on the process's runtime
    Step("gmail", SYNC, lambda r: r.sync_gmail_and_intake, timeout_s=110.0),
    Step("calendar", SYNC, lambda r: r.sync_calendar, timeout_s=280.0),
)
BY_NAME = {s.name: s for s in STEPS}

MINUTE_GROUPS: dict[str, tuple[str, ...]] = {
    group: tuple(s.name for s in STEPS if s.group == group) for group in (ON_TIME, PLAN, INITIATIVE)
}
INITIATIVE_STEPS = frozenset(s.name for s in STEPS if s.initiative)


def timeout_for(name: str) -> float:
    step = BY_NAME.get(name)
    return step.timeout_s if step else STEP_TIMEOUT_S


def period_for(name: str) -> Period:
    step = BY_NAME.get(name)
    return step.period if step else schedule.MINUTE


def lock_for(name: str) -> tuple[int, str]:
    step = BY_NAME.get(name)
    return step.lock if step else (lock_ids.JOB, name)


def host_for(step_or_group: str) -> str:
    """The host a step, or a group, runs on."""
    if step_or_group in GROUP_HOST:
        return GROUP_HOST[step_or_group]
    step = BY_NAME.get(step_or_group)
    return step.host if step else MAIN_HOST


def job_named(runtime: Runtime, name: str) -> Job | None:
    """A step's job by name, from the worker's runtime; None for a name the table doesn't have."""
    step = BY_NAME.get(name)
    return step.job(runtime) if step else None


def due(group: str, stopped: bool) -> list[str]:
    """The minute's steps of one group, in order; while stopped, Pisa's own initiative waits (their own
    requests never do)."""
    return [name for name in MINUTE_GROUPS[group] if not (stopped and name in INITIATIVE_STEPS)]
