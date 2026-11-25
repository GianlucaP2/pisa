"""Work started and not waited for: each task is kept referenced until it finishes (the event loop
keeps only a weak reference, and an unreferenced task can be collected mid-run)."""

import asyncio
from collections.abc import Coroutine, Iterator
from typing import Any


class Background:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Future[Any]] = set()

    def keep[F: asyncio.Future[Any]](self, task: F) -> F:
        """Keep a task that is already running until it finishes."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start(self, work: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Run `work` as a task, kept until it finishes."""
        return self.keep(asyncio.create_task(work))

    def __iter__(self) -> Iterator[asyncio.Future[Any]]:
        return iter(list(self._tasks))

    def __len__(self) -> int:
        return len(self._tasks)
