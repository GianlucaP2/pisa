"""Their data never leaves by a door unless it passed the strip.
Pure rules: the two types, the strip that makes the second from the first, and the doors' check.

- **`Personal`**: a `str` read from their rows that could name them or their world (people and their notes,
  the places they went, money rows, their dates, their messages and mail, their profile claims). A string
  built from one with `+`, `%`, `.format` on it, a slice or a str method stays `Personal`, and so does
  `str(p)`. **An f-string or a `" ".join(...)` over one does not**: Python builds a plain `str` there. The
  door modules are checked for f-strings over a `Personal` (`tests/taint_check.py`; in the tests `watch` makes
  one raise).
- **`Stripped`**: what of a text may leave Pisa: only `strip` (or `untouched`) makes one, from the night's
  filter (`night.scrub`).
- **The doors** (`refused`, `refusal`; `pisa.connectors.door.guard`): the web search and every web fetch, and
  any HTTP call Pisa makes with text in its query or body. A door refuses a `Personal`
  unless it is `Stripped`; the night's search door takes only a `Stripped` (`strict`). The refusal is a
  `PisaError("validation_failed")` with `detail` `personal_refused`: its message names the door and how many
  values were refused, never a value.
"""

import sys
from collections.abc import Iterable
from typing import Any, SupportsIndex, override

from pisa.domain import night
from pisa.domain.errors import PisaError

REFUSED = "personal_refused"  # the refusal's `detail` (its code: `validation_failed`, no new protocol code)

_watched: frozenset[str] = frozenset()


class PersonalInFString(AssertionError):
    """Raised (in the tests only) when a door module formats a `Personal` into a plain string."""


def watch(modules: Iterable[str]) -> None:
    """The tests' checker (`tests/taint_check.py`): an f-string or `str.format` in one of `modules` over a
    `Personal` raises `PersonalInFString`. Off in production (the empty set)."""
    global _watched
    _watched = frozenset(modules)


class Personal(str):
    """A value of their (see the module's doc). Strings built from it keep the type, except by an f-string or
    `join` (a plain `str`): what `tests/taint_check.py` flags in the door modules."""

    __slots__ = ()

    @override
    def __str__(self) -> "Personal":
        return self

    @override
    def __format__(self, spec: str) -> str:
        if _watched:
            caller = sys._getframe(1).f_globals.get("__name__", "")  # pyright: ignore[reportPrivateUsage]
            if caller in _watched:
                raise PersonalInFString(f"{caller} formats a Personal value into a plain str (core-taint)")
        return Personal(str.__format__(self, spec))

    @override
    def __add__(self, other: str) -> "Personal":
        return Personal(str.__add__(self, other))

    def __radd__(self, other: str) -> "Personal":
        return Personal(str.__add__(other, self))

    @override
    def __mod__(self, value: Any) -> "Personal":
        return Personal(str.__mod__(self, value))

    def __rmod__(self, template: Any) -> "Personal":
        return Personal(str.__mod__(template, self))

    @override
    def __mul__(self, n: SupportsIndex) -> "Personal":
        return Personal(str.__mul__(self, n))

    @override
    def __getitem__(self, key: SupportsIndex | slice) -> "Personal":
        return Personal(str.__getitem__(self, key))

    @override
    def format(self, *args: object, **kwargs: object) -> "Personal":
        return Personal(str.format(self, *args, **kwargs))

    @override
    def strip(self, chars: str | None = None) -> "Personal":
        return Personal(str.strip(self, chars))

    @override
    def lower(self) -> "Personal":
        return Personal(str.lower(self))

    @override
    def upper(self) -> "Personal":
        return Personal(str.upper(self))

    @override
    def casefold(self) -> "Personal":
        return Personal(str.casefold(self))

    @override
    def title(self) -> "Personal":
        return Personal(str.title(self))

    @override
    def replace(self, old: str, new: str, count: SupportsIndex = -1) -> "Personal":
        return Personal(str.replace(self, old, new, count))

    @override
    def split(self, sep: str | None = None, maxsplit: SupportsIndex = -1) -> list["Personal"]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return [Personal(p) for p in str.split(self, sep, maxsplit)]


class Stripped(str):
    """What of a text may leave Pisa: made only by `strip` or `untouched` (the filter). `removed` is how
    many things the filter took out."""

    removed: int

    def __new__(cls, text: str, removed: int = 0) -> "Stripped":
        made = super().__new__(cls, str.__str__(text))
        made.removed = removed
        return made


def personal(value: object) -> Personal | None:
    """A row's value as `Personal`, None for a NULL."""
    return None if value is None else Personal(value)


def strip(text: str, names: Iterable[str]) -> Stripped:
    """RAIL: what of `text` may leave Pisa: `night.scrub`'s allow-list over their names (`names`)."""
    clean, removed = night.scrub(text, names)
    return Stripped(clean, removed)


def untouched(text: str, names: Iterable[str]) -> Stripped | None:
    """`text` itself, punctuation and all, when the filter would take nothing out of it; else None (a
    text is refused whole, never sent half-stripped)."""
    return None if night.scrub(text, names)[1] else Stripped(text)


def tainted(value: object) -> int:
    """How many `Personal` values (not `Stripped`) are in `value`: a string, or a request's dict or list."""
    if isinstance(value, Stripped):
        return 0
    if isinstance(value, Personal):
        return 1
    if isinstance(value, dict):
        return sum(tainted(k) + tainted(v) for k, v in value.items())  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if isinstance(value, list | tuple | set | frozenset):
        return sum(tainted(v) for v in value)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return 0


def refused(*values: object, strict: bool = False) -> int:
    """RAIL: how many of `values` may not go out by a door: each `Personal` that is not
    `Stripped`; with `strict` (the night's search) every text that is not `Stripped`, a plain `str` too."""
    count = sum(tainted(v) for v in values)
    if strict:
        count += sum(1 for v in values if isinstance(v, str) and not isinstance(v, Stripped | Personal))
    return count


def refusal(door: str, count: int) -> PisaError:
    """The door's refusal: the door and the count, never a value."""
    return PisaError(
        "validation_failed",
        f"Pisa kept {count} personal value{'s' if count != 1 else ''} from going out by {door}.",
        "Nothing was sent. Only text that passed the personal filter may leave Pisa",
        detail=REFUSED,
    )
