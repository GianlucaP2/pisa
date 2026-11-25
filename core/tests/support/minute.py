"""The worker's minute, run on a test's own runtime: `workers.main.run_minute` (the one loop) with its host
calling `maintenance` through `call` (a portal's `call`, or a loop's `run_until_complete` wrapped the same
way), so a test runs the minute exactly as the worker does, without DBOS or a second runtime."""

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from pisa.services import maintenance, worker_steps
from pisa.workers import main as worker


class PortalHost:
    def __init__(self, call: Callable[..., Any], runtime: Any) -> None:
        self._call, self._runtime = call, runtime

    def enter(self, group: str) -> bool:
        return True

    def leave(self, group: str) -> None:
        return None

    def stopped(self, group: str = "") -> bool:
        return bool(self._call(maintenance.minute_start, self._runtime))

    def job(self, name: str) -> dict[str, Any]:
        return self._call(maintenance.run_step, self._runtime, name)


def run_minute(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[..., Any],
    runtime: Any,
    groups: Iterable[str] = tuple(worker_steps.MINUTE_GROUPS),
) -> list[dict[str, Any]]:
    """The minute's groups (all three, in order, unless named), each through the worker's loop."""
    monkeypatch.setattr(worker, "host", lambda *a: PortalHost(call, runtime))
    return [result for group in groups for result in worker.run_minute(group)]


def jobs(fakes: dict[str, Any]) -> Callable[[Any, str], Any]:
    """A `worker_steps.job_named` stand-in: these steps' fake jobs, every other step absent."""
    return lambda runtime, name: fakes.get(name)


def step(call: Callable[..., Any], runtime: Any, name: str) -> bool:
    """One worker step as the scheduler runs it (its period on the user's clock, under its lock): True when
    it ran, False when it was not due (or already ran in its period)."""
    outcome = call(maintenance.run_step, runtime, name)["outcome"]
    assert outcome in ("ok", "not_due"), outcome
    return outcome == "ok"
