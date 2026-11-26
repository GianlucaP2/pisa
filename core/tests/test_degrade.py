"""One policy for a step that may fail: an unreachable database, a bug and a guard each say so in the log."""

import asyncio

import psycopg
import pytest
from structlog.testing import capture_logs

from pisa.degrade import degrades, fails_closed
from pisa.logging import get_logger

_log = get_logger("test.degrade")


def test_unreachable_database_keeps_the_default_and_says_so() -> None:
    value = "default"
    with capture_logs() as logs, degrades(_log, "briefing", device_id="d1"):
        raise psycopg.OperationalError("down")
    assert value == "default"
    assert logs == [
        {"event": "database_unreachable", "during": "briefing", "device_id": "d1", "log_level": "error"}
    ]


def test_a_bug_is_logged_by_its_cause_not_hidden_its_trace_only_at_debug() -> None:
    with capture_logs() as logs, degrades(_log, "catch_up"):
        raise KeyError("x")
    warned, traced = logs
    assert (warned["event"], warned["log_level"], warned["cause"]) == (
        "catch_up_failed",
        "warning",
        "KeyError",
    )
    assert "exc_info" not in warned
    assert (traced["event"], traced["log_level"]) == ("catch_up_failed_trace", "debug") and traced["exc_info"]


def test_a_cancel_passes_through() -> None:
    with pytest.raises(asyncio.CancelledError), degrades(_log, "catch_up"):
        raise asyncio.CancelledError


def test_a_place_lookup_says_the_same_to_the_model_and_logs_by_kind() -> None:
    """The note tools tell the model "the place wasn't found (<class>)" whatever stopped it; the log tells a
    refusal (its code) from a failure (its class), and never names the place."""
    from pisa.brain.place_tools import resolved
    from pisa.domain.errors import PisaError

    class Places:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def resolve(self, text: str) -> tuple[tuple[float, float], str, str | None]:
            raise self.exc

    with capture_logs() as logs:
        refused = asyncio.run(resolved(Places(PisaError("not_found", "No such place.")), "il forno di Zeta"))  # type: ignore[arg-type]
        broken = asyncio.run(resolved(Places(KeyError("lat")), "il forno di Zeta"))  # type: ignore[arg-type]
    assert (refused, broken) == ("PisaError", "KeyError")
    assert [(e["event"], e.get("code"), e.get("cause")) for e in logs] == [
        ("place_not_resolved", "not_found", None),
        ("place_resolve_failed", None, "KeyError"),
    ]
    assert "forno di Zeta" not in repr(logs)


def test_a_failure_line_whose_cleaning_breaks_still_goes_out_without_the_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pisa import failures

    def broken(reason: object) -> str | None:
        raise RuntimeError("bug")

    monkeypatch.setattr(failures, "request_id", broken)
    with capture_logs() as logs:
        failures.external_failure("tavily", "search", 500, "key=sk-secretsecretsecret")
    assert logs[0]["status"] == 500 and logs[0]["request_id"] is None and logs[0]["kind"] == "response"
    assert "secret" not in repr(logs)


def test_a_guard_fails_closed_and_says_why() -> None:
    with capture_logs() as logs:
        fails_closed(_log, "device_auth", psycopg.OperationalError("down"))
        fails_closed(_log, "stop_requested", RuntimeError("bug"))
    assert logs[0] == {
        "event": "database_unreachable",
        "during": "device_auth",
        "closed": True,
        "log_level": "error",
    }
    assert logs[1]["event"] == "guard_failed_closed"
    assert logs[1]["guard"] == "stop_requested"
    assert logs[1]["cause"] == "RuntimeError"
