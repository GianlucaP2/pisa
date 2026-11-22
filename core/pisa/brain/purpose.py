"""`purpose` on Pisa's action tools: one line, in the user's language, of
what the user gets from the call. It is optional, so a call without it still runs (never block the user);
the turn's log counts it as missing.

`purposeful(move)` adds the argument to a tool, its description included, and records the move when the call
itself recorded none and did something (its result isn't a "Not changed" kind): `act` by default, with the
provenance code gives it (the user's words, or outside text when the turn read some). The calendar, email
and option tools record their own move where the effect happens (act or one yes), so theirs is exact.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pisa.brain.untrusted import current
from pisa.domain import decisions
from pisa.domain.actions import grounded_change
from pisa.domain.tools import named

# on every action tool, so short ("così arrivi in tempo": the user's gain, not Pisa's act)
PURPOSE_DOC = "purpose: what the user gets from it, one short line in their language (not what Pisa does)."

type Tool = Callable[..., Awaitable[str]]


def purposeful(move: decisions.Move | None = "act") -> Callable[[Tool], Tool]:
    def wrap(fn: Tool) -> Tool:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def run(*args: Any, purpose: str | None = None, **kwargs: Any) -> str:
            with decisions.intending(purpose):
                before = decisions.noted()
                result = await fn(*args, **kwargs)
                if move is not None and decisions.noted() == before and not decisions.no_effect(result):
                    called = signature.bind_partial(*args, **kwargs).arguments
                    decisions.note(move, fn.__name__, _provenance(fn.__name__, called))
                return result

        param = inspect.Parameter(
            "purpose", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None, annotation=str | None
        )
        params = [*signature.parameters.values(), param]
        annotations = {**fn.__annotations__, "purpose": str | None}
        doc = _with_arg(fn.__doc__ or "", PURPOSE_DOC)
        run.__signature__ = signature.replace(parameters=params)  # type: ignore[attr-defined]
        run.__annotations__ = annotations
        run.__doc__ = doc
        return run

    return wrap


# The changes Pisa offers from its own lists (an option, an item of the plan): vague words after Pisa's offer
# are Pisa's initiative, not the user's choice, as `ActionsService.write_through` records them.
_GROUNDED = named(lambda t: t.offered)


def _provenance(tool: str, args: dict[str, Any]) -> decisions.Provenance:
    """RAIL: provenance by code: outside text first; for Pisa's offered changes, the user's words naming the
    change (`grounded_change`) or else Pisa's initiative; otherwise the user's words."""
    outside = bool(current().outside())
    if tool not in _GROUNDED:
        return decisions.provenance(said=True, outside=outside)
    words, today = decisions.owner_words()
    named = today is not None and grounded_change(tool, args, words, today)
    own = tool == "apply_option"  # an option is Pisa's proposal: naming it chooses it (pisa_proposal)
    return decisions.provenance(said=named and not own, outside=outside, unprompted=not named)


def _with_arg(doc: str, entry: str) -> str:
    """The docstring with one more entry in its Args (the model reads each argument's description from it)."""
    lines = doc.rstrip().splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "Args:":
            indent = line[: len(line) - len(line.lstrip())] + "    "
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or lines[end].startswith(indent)):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return "\n".join([*lines[:end], indent + entry, *lines[end:]])
    indent = next((ln[: len(ln) - len(ln.lstrip())] for ln in lines[1:] if ln.strip()), "        ")
    return "\n".join([*lines, "", indent + "Args:", indent + "    " + entry])
