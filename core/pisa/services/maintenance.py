"""Jobs run by the worker: catalog + exchange rate refresh, ghost sweep, pruning, and how one of the worker's
steps (`worker_steps.STEPS`) runs: under its own advisory lock and time limit, on the ledger as itself
(`run_job`), after the minute's start (`minute_start`)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain.models.catalog import refresh_catalog
from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.degrade import degrades
from pisa.domain import locale
from pisa.domain import retention as retention_rules
from pisa.domain.public_sources import SOURCES
from pisa.domain.schedule import Period
from pisa.domain.tasks import DELETED_KEPT
from pisa.infra import ledger
from pisa.infra.fx import refresh_usd_to_eur
from pisa.infra.keys import KeyStore
from pisa.logging import get_logger
from pisa.services import erase, retention, step_runs, worker_steps
from pisa.services import settings as owner_settings
from pisa.services.runtime import Runtime
from pisa.settings import Settings

_log = get_logger("pisa.maintenance")


async def refresh_catalog_and_fx(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    async with (
        await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn,
        httpx.AsyncClient(transport=transport) as http,
    ):
        await refresh_usd_to_eur(conn, http)
        report = await refresh_catalog(
            conn, http, KeyStore(settings.secrets_key), ollama_base_url=settings.ollama_base_url
        )
    return report.models


async def catalog_is_empty(settings: Settings) -> bool:
    async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
        row = await (await conn.execute("SELECT NOT EXISTS (SELECT 1 FROM model_catalog)")).fetchone()
    return bool(row and row[0])


async def sweep_ghosts(settings: Settings, now: datetime | None = None) -> int:
    async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
        return await ledger.sweep_ghosts(conn, now or datetime.now(UTC))


def _app_clock() -> datetime:
    """The app's own clock (the prunes' `now` is a test's moment when given)."""
    return datetime.now(UTC)


async def clock_skewed(conn: psycopg.AsyncConnection, job: str) -> bool:
    """The app's clock and the database's disagree by more than an hour: every prune skips the run
    (a wrong clock would delete by the wrong age; the next run, with the clocks agreeing, catches up)."""
    db_now = await retention.db_now(conn)
    skew = abs(_app_clock() - db_now)
    if skew > retention_rules.CLOCK_SKEW:
        _log.warning("prune_skipped_clock_skew", job=job, skew_s=int(skew.total_seconds()))
        return True
    return False


# A step cut at its limit (`worker_steps.timeout_for`) waits this long, doubling per cut in a row.
BACKOFF_FIRST = timedelta(minutes=15)
BACKOFF_MAX = timedelta(hours=6)
BACKOFF_KEY = "worker_backoff:"  # + the job: {"until": iso, "cuts": n} in app_settings


def backoff_after(cuts: int) -> timedelta:
    return min(BACKOFF_FIRST * 2 ** max(cuts - 1, 0), BACKOFF_MAX)


async def run_job(
    runtime: Runtime,
    name: str,
    job: Callable[[], Awaitable[object]],
    *,
    timeout_s: float | None = None,
    locks: AsyncConnectionPool | None = None,
    period: Period | None = None,
) -> dict[str, object]:
    """One job, under its lock and its time limit, on the ledger as itself. Never raises: the outcome is `ok`,
    `busy` (another run holds it), `backoff` (cut recently, waiting), `not_due` (a periodic step outside its
    period's window, or already run in it: no log line), `timeout` or `failed`. Logs one cost line: ids and
    counts only. `locks`: the pool its lock's connection comes from (the worker's own, reset with
    `pg_advisory_unlock_all`, so its locks never pin the runtime's pool); else the runtime's. `period`: the
    step's (`worker_steps.period_for`); a periodic job runs once a period, on the user's clock (their zone
    now), claimed before it runs (`step_runs`)."""
    limit = timeout_s if timeout_s is not None else worker_steps.timeout_for(name)
    lock = worker_steps.lock_for(name)
    period = period if period is not None else worker_steps.period_for(name)
    since = runtime.clock()
    started = time.monotonic()
    outcome = "ok"
    async with (locks or runtime.pool).connection() as conn:
        row = await (await conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))", lock)).fetchone()
        if not (row and row[0]):
            _log.info("worker_job", job=name, outcome="busy")
            return {"job": name, "outcome": "busy"}
        try:
            cut = await get_setting(conn, BACKOFF_KEY + name)
            if isinstance(cut, dict) and datetime.fromisoformat(str(cut["until"])) > since:
                _log.info("worker_job", job=name, outcome="backoff", cuts=int(cut.get("cuts", 1)))
                return {"job": name, "outcome": "backoff"}
            claimed: step_runs.Claim | None = None
            if not period.every_minute:
                claimed = await step_runs.claim(conn, name, period, runtime.owner.local(since), since)
                if claimed is None:
                    return {"job": name, "outcome": "not_due"}
            try:
                with ledger.job_scope(name):
                    await asyncio.wait_for(job(), limit)
            except TimeoutError:
                outcome = "timeout"
                cuts = int(cut.get("cuts", 0)) + 1 if isinstance(cut, dict) else 1
                wait = backoff_after(cuts)
                await set_setting(
                    conn,
                    BACKOFF_KEY + name,
                    Jsonb({"until": (runtime.clock() + wait).isoformat(), "cuts": cuts}),
                )
                backoff_s = int(wait.total_seconds())
                _log.warning("minute_step_timeout", step=name, limit_s=limit, cuts=cuts, backoff_s=backoff_s)
            except Exception:
                outcome = "failed"
                _log.exception("minute_step_failed", step=name)
            else:
                claimed = None  # ran: the period is spent
                if cut is not None:
                    await delete_setting(conn, BACKOFF_KEY + name)
            if claimed is not None and period.retry:  # cut or failed: tried again in its period
                try:
                    await step_runs.give_back(conn, claimed)
                except Exception:
                    _log.warning("step_period_kept", job=name)
        finally:
            try:
                await conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", lock)
            except Exception:  # a broken connection: its session's locks went with it. A cancel landing
                # here leaves the lock on the connection: the lock pool's reset unlocks it on return.
                _log.warning("job_unlock_failed", job=name)
        calls, spent = 0, Decimal(0)  # the report says nothing was spent rather than failing the job
        with degrades(_log, "job_spend", job=name):
            calls, spent = await ledger.job_spend(conn, name, since)
    ms = int((time.monotonic() - started) * 1000)
    _log.info("worker_job", job=name, outcome=outcome, ms=ms, calls=calls, eur=f"{spent:.6f}")
    return {"job": name, "outcome": outcome, "ms": ms, "calls": calls, "eur": f"{spent:.6f}"}


async def run_step(
    runtime: Runtime, name: str, *, locks: AsyncConnectionPool | None = None
) -> dict[str, object]:
    """One of the worker's steps by name (`worker_steps.STEPS`), under its lock and limit. Never raises;
    `absent` for a name the table doesn't have."""
    job = worker_steps.job_named(runtime, name)
    if job is None:
        return {"job": name, "outcome": "absent"}
    return await run_job(runtime, name, job, locks=locks)


async def minute_start(runtime: Runtime) -> bool:
    """Before a minute's group: the user's zone read again (the api stores it; a long-lived worker
    would keep the one it started with), then the stop. True: stopped."""
    try:
        await runtime.zone.load()
    except Exception:
        _log.exception("zone_reload_failed")
    return bool(await runtime.autopilot.stopped())


TRAVEL_RETENTION = timedelta(days=1)  # travel cells: provider content, for the day
PLACE_CALLS_KEPT = timedelta(days=90)  # the places providers' call ledger (provider, SKU, units: no place)


async def prune_history(settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Nothing about them goes by age. The 03:30 removes only the closed list (a public feed's
    copies past use, pairing tokens, the curious sources' cache), the tasks they deleted (restorable 24 h) and
    the conversation messages their own setting names."""
    moment = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
        if await clock_skewed(conn, "prune_history"):
            return counts
        # the NWS's per-point copies, gone once past any use (their `stale` age)
        cur = await conn.execute(
            "DELETE FROM public_cache WHERE key LIKE 'nws\\_alerts:%%' "
            "AND coalesce(fetched_at, failed_at) < %s",
            (moment - SOURCES["nws_alerts"].stale,),
        )
        counts["public_cache"] = cur.rowcount
        # a task they deleted is restorable 24 h, then gone (their delete, not Pisa's)
        cur = await conn.execute(
            "DELETE FROM tasks WHERE status = 'deleted' AND deleted_at < %s", (moment - DELETED_KEPT,)
        )
        counts["deleted_tasks"] = cur.rowcount
        counts |= await prune_routine(conn, moment)
        # their conversation messages, as long as their setting says (cited ones stay; a day's grace)
        value = (await owner_settings.load(conn, locale.TAGS[settings.language]))["messages_retention"]
        counts |= await retention.prune_messages(conn, moment, value, settings.timezone)
    _log.info("history_pruned", **counts)
    return counts


async def prune_routine(conn: psycopg.AsyncConnection, moment: datetime) -> dict[str, int]:
    """Security material and a public cache (the closed list), batched."""
    counts: dict[str, int] = {}
    batched = retention.delete_batched  # LIMIT-ed deletes in a loop, never one long lock
    codes = moment - retention_rules.PAIR_TOKENS_KEPT  # expired a week ago
    counts["pair_tokens"] = await batched(  # QR pairing tokens (their pairings go with them)
        conn,
        "DELETE FROM pair_tokens WHERE ctid IN (SELECT ctid FROM pair_tokens WHERE expires_at < %s LIMIT %s)",
        (codes,),
    )
    counts["pisa_curious_sources"] = await batched(  # a public page's reliability, checked again after this
        conn,
        "DELETE FROM pisa_curious_sources WHERE ctid IN "
        "(SELECT ctid FROM pisa_curious_sources WHERE checked_on < %s LIMIT %s)",
        ((moment - retention_rules.CURIOUS_SOURCES_KEPT).date(),),
    )
    return counts


async def prune_places(settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Provider terms: cached provider content (travel cells included) after 24 h, Google
    coordinates after 30 days; Pisa's research after 30 days; place usage records after 90 days."""
    from pisa.services.places import prune

    moment = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    try:
        async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
            if not await clock_skewed(conn, "prune_places"):
                counts = await prune(conn, moment)
                cur = await conn.execute(
                    "DELETE FROM place_calls WHERE at < %s", (moment - PLACE_CALLS_KEPT,)
                )
                counts["calls"] = cur.rowcount
                cur = await conn.execute(
                    "DELETE FROM travel_cache WHERE created_at < %s", (moment - TRAVEL_RETENTION,)
                )
                counts["travel"] = cur.rowcount
                _log.info("places_pruned", **counts)
    finally:
        # a deletion they signed that a restart or a crash interrupted runs again from where it
        # stopped (the job's lock: never two at once, and none while the API runs one). Its own step: a skewed
        # clock or a failing places prune never holds their deletion back
        counts["erase_jobs"] = await erase.run_pending(settings.database_url)
    return counts
