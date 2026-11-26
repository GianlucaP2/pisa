"""`pisa worker`: DBOS on its own system database (PISA_DBOS_DATABASE_URL, the `pisa_dbos` database owned by
pisa_app).

Schedules (cron evaluated in PISA_TIMEZONE):
- catalog + USD→EUR refresh at 03:00;
- ghost sweep of the cost ledger every 5 minutes;
- Gmail sync + intake every 2 minutes; Calendar sync every 5 minutes;
- nothing about them goes by age: prune-history (03:30) removes only the closed list (a public
  feed's copies, pairing codes, the curious sources' cache), the tasks they deleted and the messages their own
  setting names;
- the text read from Gmail is set to NULL 30 days after it was fetched, a thing's keys 30 days after its last
  task closed (purge-mail, 03:40);
- place provider content at most 24 h, Google coordinates 30 days, resolutions 30 days, the provider call
  ledger 90 days (prune-places, every 15 minutes, so nothing outlives its limit by more than that);
- every minute, three workflows side by side, so a slow
  step in one never delays another's:
  - minute-on-time: the briefing time trigger, reminders (and "leave now") and their floors, the approvals'
    lapse, the end of quiet mode (held messages released), the Sunday's two pushes;
  - minute-plan: done by being there, the re-plan triggers, check-ins, aging, attend, the Live Activity;
  - minute-initiative: Pisa's own initiative, mostly gated hourly or daily inside (money_back, people,
    rituals, the vault, skills…), the night last: what the user did becomes evidence, then claims, at 03:30
    in the user's zone.
  "Pisa, ferma tutto" pauses every initiative step (`worker_steps.STEPS` marks them), in whichever workflow.

Two long-lived `Runtime`s for the process (`host.WorkerHost`: the on-time group's, the rest's), not one a
minute; one DBOS step per job, each under its own `pg_try_advisory_lock` and time limit, on the ledger as
itself (`maintenance.run_job`).
"""

import asyncio
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dbos import DBOS, DBOSConfig

from pisa import __version__
from pisa.degrade import fails_closed
from pisa.domain import experimental
from pisa.domain.errors import PisaError
from pisa.logging import adopt, get_logger
from pisa.services import maintenance, review, worker_steps
from pisa.settings import Settings, get_settings
from pisa.workers.host import WorkerHost, starter

_log = get_logger("pisa.worker")


@DBOS.step()
def refresh_catalog_step() -> int:
    return asyncio.run(maintenance.refresh_catalog_and_fx(get_settings()))


@DBOS.step()
def sweep_ghosts_step() -> int:
    return asyncio.run(maintenance.sweep_ghosts(get_settings()))


@DBOS.step()
def prune_history_step() -> None:
    asyncio.run(maintenance.prune_history(get_settings()))


@DBOS.step()
def purge_mail_step() -> None:
    asyncio.run(maintenance.purge_mail(get_settings()))


@DBOS.step()
def prune_places_step() -> None:
    asyncio.run(maintenance.prune_places(get_settings()))


@DBOS.workflow()
def purge_mail_workflow(scheduled_at: datetime, context: Any) -> None:
    purge_mail_step()


@DBOS.workflow()
def prune_places_workflow(scheduled_at: datetime, context: Any) -> None:
    prune_places_step()


# --- the minute -----------------------------------------------------------------------

# Two hosts: the on-time group's own runtime and loop, and one for the rest (`worker_steps.GROUP_HOST`).
ON_TIME_HOST, MAIN_HOST = worker_steps.ON_TIME_HOST, worker_steps.MAIN_HOST
_POOL_SIZES = {ON_TIME_HOST: 8, MAIN_HOST: 20}
_hosts: dict[str, WorkerHost] = {}
_hosts_lock = threading.Lock()


def host(name: str = MAIN_HOST) -> WorkerHost:
    """One of the process's runtime hosts (`run_worker` builds them; a bare DBOS launch, in tests, on first
    use)."""
    with _hosts_lock:
        if name not in _hosts:
            _hosts[name] = WorkerHost(get_settings(), start=starter(_POOL_SIZES[name]), name=name)
        return _hosts[name]


@DBOS.step()
def minute_start_step(group: str) -> bool:
    return host(worker_steps.host_for(group)).stopped(group)


@DBOS.step()
def job_step(name: str) -> dict[str, Any]:
    return host(worker_steps.host_for(name)).job(name)


def run_minute(group: str) -> list[dict[str, Any]]:
    """The minute's one loop: one group's steps (`worker_steps.due`), one DBOS step each. A group whose
    previous run is still in flight is skipped this minute. If the stop can't be read, Pisa's initiative waits
    (their own requests still run)."""
    runs = host(worker_steps.host_for(group))
    if not runs.enter(group):
        _log.info("minute_group_busy", group=group)
        return []
    try:
        try:
            stopped = minute_start_step(group)
        except Exception as exc:  # the stop is a guard: unread, Pisa's initiative waits
            fails_closed(_log, "minute_stop", exc, group=group)
            stopped = True
        # an off feature's steps never run (the host's own settings, never a cached copy)
        spec = getattr(runs, "experimental", None)
        if not isinstance(spec, str):
            own = Settings()  # type: ignore[call-arg]
            spec = experimental.review_spec(own.experimental, review_mode=own.review_mode)
        enabled = experimental.enabled(spec)
        return [job_step(name) for name in worker_steps.due(group, stopped, enabled)]
    finally:
        runs.leave(group)


@DBOS.workflow()
def minute_on_time_workflow(scheduled_at: datetime, context: Any) -> None:
    run_minute(worker_steps.ON_TIME)


@DBOS.workflow()
def minute_plan_workflow(scheduled_at: datetime, context: Any) -> None:
    run_minute(worker_steps.PLAN)


@DBOS.workflow()
def minute_initiative_workflow(scheduled_at: datetime, context: Any) -> None:
    run_minute(worker_steps.INITIATIVE)


@DBOS.workflow()
def prune_history_workflow(scheduled_at: datetime, context: Any) -> None:
    prune_history_step()


@DBOS.workflow()
def refresh_catalog_workflow(scheduled_at: datetime, context: Any) -> None:
    refresh_catalog_step()


@DBOS.workflow()
def sweep_ghosts_workflow(scheduled_at: datetime, context: Any) -> None:
    sweep_ghosts_step()


@DBOS.workflow()
def gmail_workflow(scheduled_at: datetime, context: Any) -> None:
    job_step("gmail")


@DBOS.workflow()
def calendar_workflow(scheduled_at: datetime, context: Any) -> None:
    job_step("calendar")


@dataclass(frozen=True)
class Schedule:
    name: str
    workflow: Any
    cron: str  # 6 fields: seconds first
    feature: str | None = None  # its experimental feature (`domain.experimental`): off, not scheduled


SCHEDULES = (
    Schedule("catalog-refresh", refresh_catalog_workflow, "0 0 3 * * *"),
    Schedule("ledger-ghost-sweep", sweep_ghosts_workflow, "0 */5 * * * *"),
    Schedule("gmail-sync", gmail_workflow, "0 */2 * * * *", feature="google"),
    Schedule("calendar-sync", calendar_workflow, "30 */5 * * * *", feature="google"),
    Schedule("prune-history", prune_history_workflow, "0 30 3 * * *"),
    Schedule("purge-mail", purge_mail_workflow, "0 40 3 * * *"),
    Schedule("prune-places", prune_places_workflow, "0 */15 * * * *"),
    Schedule("minute-on-time", minute_on_time_workflow, "0 * * * * *"),
    Schedule("minute-plan", minute_plan_workflow, "0 * * * * *"),
    Schedule("minute-initiative", minute_initiative_workflow, "0 * * * * *"),
)
# schedules earlier versions registered: DBOS keeps a schedule until it is deleted, so launch deletes these
OBSOLETE_SCHEDULES = (
    "locations-prune",
    "daily-prune",
    "prune-locations",
    "prune-email",
    "briefing-time",
    "reminders",
    "quiet-release",
    "profile-learn",
    "every-minute",
)


def schedules(enabled: frozenset[str]) -> tuple[Schedule, ...]:
    """The schedules this install runs: none of an experimental feature that is off."""
    return tuple(s for s in SCHEDULES if s.feature is None or experimental.on(s.feature, enabled))


def run_worker(settings: Settings) -> int:
    if not settings.dbos_database_url:
        print("PISA_DBOS_DATABASE_URL is not set (see .env.example)", file=sys.stderr)
        return 2
    try:  # App Store review mode on a fresh database only: a clean exit, before DBOS starts
        asyncio.run(review.preflight(settings.database_url, review_mode=settings.review_mode))
    except PisaError as err:
        print(f"error: {err.message} {err.action}.", file=sys.stderr)
        return 2
    with _hosts_lock:  # before the launch: a recovered workflow finds them
        for name, size in _POOL_SIZES.items():
            _hosts[name] = WorkerHost(settings, start=starter(size), name=name)
    config: DBOSConfig = {
        "name": "pisa-worker",
        "system_database_url": settings.dbos_database_url,
        "application_version": __version__,
    }
    DBOS(config=config)
    DBOS.launch()
    adopt("dbos")  # DBOS gave its logger its own handler: its lines go through Pisa's, without the words
    enabled = experimental.enabled(settings.experimental, review_mode=settings.review_mode)
    if unknown := experimental.unknown(settings.experimental):
        _log.warning("experimental_unknown", ids=list(unknown))
    running = schedules(enabled)
    # an obsolete schedule, or one whose experimental feature is off now: DBOS keeps it until it is deleted
    for name in (*OBSOLETE_SCHEDULES, *(s.name for s in SCHEDULES if s not in running)):
        if DBOS.get_schedule(name) is not None:
            DBOS.delete_schedule(name)
    DBOS.apply_schedules(
        [
            {
                "schedule_name": s.name,
                "workflow_fn": s.workflow,
                "schedule": s.cron,
                "cron_timezone": settings.timezone,
            }
            for s in running
        ]
    )
    _log.info("worker_started", schedules=[s.name for s in running], timezone=settings.timezone)
    if asyncio.run(maintenance.catalog_is_empty(settings)):
        _log.info("catalog_empty_refreshing_now")
        DBOS.start_workflow(refresh_catalog_workflow, datetime.now(), None)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    _log.info("worker_stopping")
    DBOS.destroy()
    for runs in _hosts.values():
        runs.close()
    return 0
