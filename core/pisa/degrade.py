"""What a failed store or provider means for a turn or a step: one policy, always logged.

Three answers, and every handler that degrades says which one it gives:

- `degrades(log, during)`: the step is optional. The caller set its stated default before the `with` and keeps
  it. An unreachable database is logged `database_unreachable` with `during`; anything else is a bug or a
  provider's failure and is logged `<during>_failed` with its cause (`cause_of`), never hidden.
- `unreachable(log, during)`: the one line for a database that could not be reached, where the caller has its
  own answer for it (an error to the user, a closed guard).
- `fails_closed(log, guard, exc)`: a guard could not decide. It answers "no" (refused, stopped, not allowed),
  and says so at warning with the cause's class.

Fields are ids, counts and names only: never the user's words, a secret, a URL with its query or a place's
coordinates (`failures.reason_of` cleans a provider's own words when they are worth keeping). A failure is
logged by its class and its code (a database error's SQLSTATE, a `PisaError`'s code), never its message: a
database error's message can quote a value the user stored. The traceback goes only to a separate debug line,
and the server logs at `info` unless the operator sets `PISA_LOG_LEVEL=debug`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from structlog.stdlib import BoundLogger

from pisa.db.errors import UNREACHABLE
from pisa.domain.errors import PisaError


def cause_of(exc: BaseException) -> dict[str, str]:
    """What may be logged about a failure: its class, and its SQLSTATE or `PisaError` code when it has one."""
    fields = {"cause": exc.__class__.__name__}
    code = exc.code if isinstance(exc, PisaError) else getattr(exc, "sqlstate", None)
    if isinstance(code, str) and code:
        fields["code"] = code
    return fields


def failed(log: BoundLogger, event: str, exc: BaseException, **fields: Any) -> None:
    """Log a failure at warning by its cause only; its traceback, which may quote data, only at debug."""
    log.warning(event, **cause_of(exc), **fields)
    log.debug(f"{event}_trace", exc_info=exc, **fields)


def unreachable(log: BoundLogger, during: str, **fields: Any) -> None:
    """Log that the database could not be reached for `during`."""
    log.error("database_unreachable", during=during, **fields)


@contextmanager
def degrades(log: BoundLogger, during: str, **fields: Any) -> Iterator[None]:
    """Run the block; if it fails, log why and go on with what the caller had (its stated default).

    A cancel is not a failure and passes through."""
    try:
        yield
    except UNREACHABLE:
        unreachable(log, during, **fields)
    except Exception as exc:
        failed(log, f"{during}_failed", exc, **fields)


def fails_closed(log: BoundLogger, guard: str, exc: BaseException, **fields: Any) -> None:
    """Log a guard that could not decide and therefore refuses (call it in the `except`, then refuse)."""
    if isinstance(exc, UNREACHABLE):
        unreachable(log, guard, closed=True, **fields)
        return
    failed(log, "guard_failed_closed", exc, guard=guard, **fields)
