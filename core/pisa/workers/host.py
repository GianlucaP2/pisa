"""Long-lived runtimes in the worker process, not a fresh runtime, pool and HTTP clients every minute.

A host holds one `Runtime` on its own event loop, in its own thread: its pool and clients belong to that
loop. DBOS runs each scheduled workflow in a thread of its own; a step hands its coroutine to a host's loop
and waits for it. The worker has two hosts: the on-time group's own, so sync CPU work
elsewhere (Gmail's intake, the night) never stalls a reminder, and one for the rest. Each minute's start logs
its loop's lag.

Each host keeps its job locks on a small pool of its own, reset with `pg_advisory_unlock_all`,
and runs a group at most once at a time: a minute whose previous run of that group is still in flight
skips it.

Each call has a backstop past the job's own limit (`maintenance.run_job` cancels at the limit; the backstop
only catches a coroutine that ignores its cancellation): the waiting step returns `hung`, and the job's lock
stays held until the coroutine ends, so no second run starts beside it.
"""

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg_pool import AsyncConnectionPool

from pisa.db.errors import UNREACHABLE
from pisa.db.pool import open_lock_pool
from pisa.degrade import unreachable
from pisa.domain import experimental
from pisa.logging import get_logger
from pisa.services import maintenance, worker_steps
from pisa.services.runtime import Runtime
from pisa.settings import Settings

BACKSTOP_S = 15.0  # past a job's own limit, before its step stops waiting
CLOSE_WAIT_S = 10.0  # on shutdown, for the jobs in flight
LOCK_POOL_SIZE = 4  # a host runs at most its groups' one job each at a time, plus the Google syncs
_log = get_logger("pisa.worker.host")

Starter = Callable[[Settings], Awaitable[Runtime]]


def starter(pool_size: int) -> Starter:
    async def start(settings: Settings) -> Runtime:
        runtime = await Runtime.start(settings, pool_size=pool_size)
        await runtime.pool.wait(timeout=30)
        return runtime

    return start


class WorkerHost:
    def __init__(
        self, settings: Settings | None, *, start: Starter | None = None, name: str = "main"
    ) -> None:
        self._settings = settings
        self._start = start or starter(20)
        self.name = name
        self._runtime: Runtime | None = None
        self._locks: AsyncConnectionPool | None = None
        self._starting: asyncio.Lock | None = None
        self._groups: set[str] = set()
        self._groups_lock = threading.Lock()
        self._inflight: set[concurrent.futures.Future[Any]] = set()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name=f"pisa-worker-{name}", daemon=True
        )
        self._thread.start()
        self.starts = 0  # how many runtimes this host built (one, unless a start failed)

    @property
    def experimental(self) -> str:
        """PISA_EXPERIMENTAL for this host: its own settings' (never a cached copy another test or process
        built), else read now. Under review mode (PISA_REVIEW_MODE) review mode's mark: no experimental step
        runs, nor a supported one review mode keeps off (`experimental.REVIEW_OFF`)."""
        settings = self._settings or Settings()  # type: ignore[call-arg]
        return experimental.review_spec(settings.experimental, review_mode=settings.review_mode)

    async def runtime(self) -> Runtime:
        """The host's runtime, built on first use; a start that fails (the database down) is tried again by
        the next call."""
        if self._runtime is not None:
            return self._runtime
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            if self._runtime is None:
                runtime = await self._start(self._settings)  # type: ignore[arg-type]
                if self._settings is not None and self._locks is None:
                    self._locks = await open_lock_pool(self._settings.database_url, max_size=LOCK_POOL_SIZE)
                self._runtime = runtime
                self.starts += 1
                _log.info("worker_runtime_started", host=self.name)
        return self._runtime

    def call[T](self, fn: Callable[[Runtime], Awaitable[T]], *, timeout_s: float) -> T:
        """`fn(runtime)` on the host's loop, from any thread; raises TimeoutError past `timeout_s`."""

        async def go() -> T:
            return await fn(await self.runtime())

        future = asyncio.run_coroutine_threadsafe(go(), self._loop)
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"no answer in {timeout_s:.0f} s") from None

    def job(self, name: str) -> dict[str, Any]:
        """One of the minute's steps (or a Google sync) by name, under its lock and limit. Never raises."""

        async def go(runtime: Runtime) -> dict[str, Any]:
            return await maintenance.run_step(runtime, name, locks=self._locks)

        try:
            return self.call(go, timeout_s=worker_steps.timeout_for(name) + BACKSTOP_S)
        except TimeoutError:
            _log.error("worker_job_hung", job=name)
            return {"job": name, "outcome": "hung"}
        except UNREACHABLE:  # the runtime couldn't reach the database: the next minute tries again
            unreachable(_log, "worker_job", job=name)
            return {"job": name, "outcome": "failed"}
        except Exception:  # the runtime couldn't start otherwise (a bug, a setting): its trace, then the same
            _log.exception("worker_job_not_run", job=name)
            return {"job": name, "outcome": "failed"}

    def enter(self, group: str) -> bool:
        """A group's run begins; False while its previous run is still in flight (overlapping minutes
        would pin connections and wait on each other's locks)."""
        with self._groups_lock:
            if group in self._groups:
                return False
            self._groups.add(group)
            return True

    def leave(self, group: str) -> None:
        with self._groups_lock:
            self._groups.discard(group)

    def stopped(self, group: str = "") -> bool:
        """The minute's start (`maintenance.minute_start`): their zone read again, then the stop. Logs how
        long the loop took to start it (its lag: ids and counts only)."""
        asked = time.monotonic()

        async def go(runtime: Runtime) -> bool:
            _log.info(
                "worker_loop_lag", host=self.name, group=group, ms=int((time.monotonic() - asked) * 1000)
            )
            return await maintenance.minute_start(runtime)

        return self.call(go, timeout_s=30.0)

    def close(self) -> None:
        """Waits up to CLOSE_WAIT_S for the jobs in flight, then closes the runtime and the lock pool."""
        concurrent.futures.wait(list(self._inflight), timeout=CLOSE_WAIT_S)

        async def shut() -> None:
            if self._runtime is not None:
                await self._runtime.close()
                self._runtime = None
            if self._locks is not None:
                await self._locks.close(timeout=5)
                self._locks = None

        try:
            asyncio.run_coroutine_threadsafe(shut(), self._loop).result(timeout=60)
        except Exception:
            _log.exception("worker_runtime_close_failed", host=self.name)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
