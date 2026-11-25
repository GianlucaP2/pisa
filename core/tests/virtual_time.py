"""An event loop on a virtual clock, for a test of a timer's order or a time budget.

The loop's `time()` moves only when the loop would wait for its next timer, and then jumps straight to it: its
timers run in deadline order and a budget is spent exactly, whatever the machine's load. Work done on another
thread (`run_in_executor`) takes no virtual time: a test that awaits one gives it no deadline, or gives it a
thread that never answers (the budget is what is under test).

    asyncio.run(main(), loop_factory=VirtualTimeLoop)

The virtual clock starts at the wall's monotonic time, so a time taken before the loop ran (a dataclass's
default) is a moment ago on it. A module that reads `time.monotonic()` itself reads the loop's clock with
`monkeypatch.setattr(module, "time", LoopClock)`; `run_virtual(main, module)` does both for one run. A later
loop's clock starts where an earlier one's stopped when that is past the wall, so the clock never goes back.
"""

import asyncio
import selectors
import time
from collections.abc import Coroutine
from types import ModuleType
from typing import Any


class _JumpingSelector(selectors.SelectSelector):
    """A selector whose wait for the loop's next timer takes no wall time: the virtual clock jumps to it."""

    latest = 0.0  # where the last virtual clock stopped

    def __init__(self) -> None:
        super().__init__()
        self.now = max(time.monotonic(), _JumpingSelector.latest)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout:
            self.now += timeout
            _JumpingSelector.latest = max(_JumpingSelector.latest, self.now)
        return super().select(0)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self._jumping = _JumpingSelector()
        super().__init__(self._jumping)

    def time(self) -> float:
        return self._jumping.now


class LoopClock:
    """A module's `time` on the running loop's clock (the virtual one on a VirtualTimeLoop); the wall's
    outside a loop."""

    @staticmethod
    def monotonic() -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return time.monotonic()


def run_virtual[T](main: Coroutine[Any, Any, T], *clocked: ModuleType) -> T:
    """`asyncio.run(main)` on a VirtualTimeLoop, each of `clocked` reading its `time.monotonic()` from the
    loop's clock while it runs."""
    kept = [(module, module.time) for module in clocked]
    try:
        for module in clocked:
            setattr(module, "time", LoopClock)  # noqa: B010
        return asyncio.run(main, loop_factory=VirtualTimeLoop)
    finally:
        for module, was in kept:
            setattr(module, "time", was)  # noqa: B010


def run_talk[T](main: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(main)` on a VirtualTimeLoop with a live talk's clocks (`talk`'s, `talk_media`'s and the
    reflex's) on the loop's: its timers and a test's waits keep their order however loaded the machine is."""
    from pisa.services import reflex, talk, talk_media

    return run_virtual(main, talk, talk_media, reflex)
