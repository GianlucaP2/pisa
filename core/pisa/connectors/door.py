"""The doors: the one check every outgoing text passes before an HTTP call leaves Pisa. A
refusal is logged with the door, ids and counts only, and the call doesn't go out (`pisa.domain.taint`)."""

from pisa.domain import taint
from pisa.logging import get_logger

_log = get_logger("pisa.door")


def guard(door: str, *values: object, strict: bool = False, **ids: str) -> None:
    """RAIL: raise the door's refusal (`validation_failed`, detail `personal_refused`) when
    any of `values` is their (`Personal`) and not stripped; `strict`: anything not `Stripped` is refused."""
    if count := taint.refused(*values, strict=strict):
        _log.info("door_refused", door=door, refused=count, **ids)
        raise taint.refusal(door, count)
