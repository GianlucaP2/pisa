"""A tool's schema is built once, not on every turn.

Pisa builds its agent per turn (the tools are closures over the turn's state), and Pydantic AI builds each
tool's validator and JSON schema from the function's signature and docstring: ~140 tools, ~100 ms of every
turn before the model is asked, the same bytes each time. The schema depends only on the function's code, its
signature (annotations, defaults) and docstring, and the generator's options, so it is cached under exactly
those (a wrapper's own `__signature__` by its repr, every level of a `functools.wraps` chain); each tool gets
its own function and its own copy of the schema dicts. A function whose key can't be hashed is built as
before.

Installed on Pydantic AI's `function_schema` (the call `Tool.__init__` makes); `tests/test_core_latency.py`
checks that every tool definition of Pisa's agent is identical with the cache on and off.
"""

import copy
import dataclasses
import inspect
from collections.abc import Callable
from typing import Any

from pydantic_ai import _function_schema  # pyright: ignore[reportPrivateUsage]

MAX_ENTRIES = 2048  # far above Pisa's ~140 tools; a set built from changing data never grows without bound

_build: Callable[..., Any] = _function_schema.function_schema
_cache: dict[tuple[Any, ...], Any] = {}
stats = {"hits": 0, "misses": 0}


def _key(function: Callable[..., Any], args: tuple[Any, ...]) -> tuple[Any, ...] | None:
    levels: list[Any] = []
    current: Any = function
    for _ in range(8):  # functools.wraps chains: every level's own code and signature
        code = getattr(current, "__code__", None)
        if code is None:
            return None
        levels.append(
            (
                code,
                getattr(current, "__qualname__", None),
                repr(getattr(current, "__annotations__", None)),
                repr(getattr(current, "__defaults__", None)),
                repr(getattr(current, "__kwdefaults__", None)),
                getattr(current, "__doc__", None),
                repr(getattr(current, "__signature__", None)),  # set by a wrapper (purposeful): its repr
            )
        )
        current = getattr(current, "__wrapped__", None)
        if current is None:
            break
    else:
        return None
    key = (tuple(levels), *args)
    try:
        hash(key)
    except TypeError:
        return None
    return key


def function_schema(
    function: Callable[..., Any],
    schema_generator: Any,
    *,
    tool_name: str | None = None,
    takes_ctx: bool | None = None,
    docstring_format: Any = "auto",
    require_parameter_descriptions: bool = False,
) -> Any:
    """Pydantic AI's `function_schema`, built once per function shape."""
    options = dict(
        tool_name=tool_name,
        takes_ctx=takes_ctx,
        docstring_format=docstring_format,
        require_parameter_descriptions=require_parameter_descriptions,
    )
    key = _key(
        function, (schema_generator, tool_name, takes_ctx, docstring_format, require_parameter_descriptions)
    )
    if key is None or not inspect.isfunction(inspect.unwrap(function)):
        return _build(function, schema_generator, **options)
    built = _cache.get(key)
    if built is None:
        stats["misses"] += 1
        if len(_cache) >= MAX_ENTRIES:
            _cache.clear()
        built = _cache[key] = _build(function, schema_generator, **options)
    else:
        stats["hits"] += 1
    return dataclasses.replace(
        built,
        function=function,
        json_schema=copy.deepcopy(built.json_schema),
        return_schema=copy.deepcopy(built.return_schema),
        positional_fields=list(built.positional_fields),
    )


def install() -> None:
    """Pydantic AI's tools build their schemas through the cache (idempotent)."""
    _function_schema.function_schema = function_schema  # type: ignore[assignment]
