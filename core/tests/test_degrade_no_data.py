"""A failure is logged by its class and code, never by its message: a database error's message can quote a
value the user stored. Its traceback goes only to a debug line, and the release config logs at info."""

from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
import pytest
import structlog
from structlog.testing import capture_logs

from pisa.degrade import degrades, fails_closed
from pisa.services.session import failure
from pisa.settings import Settings

STORED_VALUE = "via-del-test-42-zeta"
_log = structlog.get_logger("test")


def _data_error(db: psycopg.Connection) -> psycopg.Error:
    with pytest.raises(psycopg.Error) as caught:
        with db.transaction():
            db.execute("SELECT %s::integer", (STORED_VALUE,))  # 22P02: its message quotes the value
    assert STORED_VALUE in str(caught.value)
    return caught.value


def _above_debug(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    kept = [e for e in logs if e["log_level"] != "debug"]
    assert kept and all(STORED_VALUE not in repr(e) for e in kept)
    return kept


def test_the_release_config_logs_at_info() -> None:
    assert Settings.model_fields["log_level"].default == "info"


def test_an_optional_step_logs_the_class_and_sqlstate_but_not_the_value(db: psycopg.Connection) -> None:
    exc = _data_error(db)
    with capture_logs() as logs, degrades(_log, "kept_findings"):
        raise exc
    (warned,) = _above_debug(logs)
    assert warned["event"] == "kept_findings_failed"
    assert warned["cause"] == exc.__class__.__name__ and warned["code"] == "22P02"


def test_a_closed_guard_and_a_session_failure_log_no_value(db: psycopg.Connection) -> None:
    exc = _data_error(db)
    with capture_logs() as logs:
        fails_closed(_log, "phone_locked", exc)
        failure(_log, exc, "frame_failed", "Something broke ({trace})")
    warned = _above_debug(logs)
    assert [e["event"] for e in warned] == ["guard_failed_closed", "frame_failed"]
    assert all(e["code"] == "22P02" for e in warned)
