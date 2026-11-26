"""The brain's side of the tool registry: how a tool's callable is offered to the conversation agent.

Each tool is declared once in `pisa.domain.tools` (what it does, how the rails treat it). A tool body is
offered with `@offer(agent)` in place of Pydantic AI's own decorator: a name missing from the registry raises
there, so a tool without a declaration can never be offered. While its body runs, `calling()` is its name:
the gates (the research turn's loop guard, a held change) take it from there, so no tool body repeats its
own name as a string. The definitions the model sees are the function's own (name, docstring, parameters):
the wrapper keeps them byte for byte (`tests/test_tool_definitions_snapshot.py`).
"""

import functools
import inspect
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, cast

from pydantic_ai import Agent

from pisa.domain import tools

# the tool whose body is running in this task (each tool call of a response runs in a task of its own)
_calling: ContextVar[str | None] = ContextVar("pisa_calling_tool", default=None)


def calling() -> str:
    """The name of the tool whose body is running (set by `offer`)."""
    name = _calling.get()
    if name is None:
        raise RuntimeError("no offered tool is running")
    return name


def declared(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The tool body, run with its own name as `calling()`; a body whose name the registry doesn't declare
    raises (fail closed: never offered)."""
    name = fn.__name__
    if tools.entry(name) is None:
        raise ValueError(f"{name}: not in the tool registry (pisa.domain.tools)")
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            token = _calling.set(name)
            try:
                return await fn(*args, **kwargs)
            finally:
                _calling.reset(token)

        return run_async

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        token = _calling.set(name)
        try:
            return fn(*args, **kwargs)
        finally:
            _calling.reset(token)

    return run


def offer[F: Callable[..., Any]](
    agent: Agent[Any, Any], *, ctx: bool = False, description: str | None = None
) -> Callable[[F], F]:
    """`@agent.tool_plain` (or `@agent.tool` with `ctx`) for a declared tool."""

    def register(fn: F) -> F:
        run = declared(fn)
        if ctx:
            agent.tool(run)
        elif description is not None:
            agent.tool_plain(description=description)(run)
        else:
            agent.tool_plain(run)
        return cast(F, run)

    return register
