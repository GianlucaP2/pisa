"""R18: what an unreachable database means for a turn's steps. A guard stays closed and says so; an optional
step keeps its stated default and says so. No database here: the pool itself is down."""

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import psycopg
from structlog.testing import capture_logs

from pisa.brain import untrusted
from pisa.brain.conversation import ToolFamilies
from pisa.domain import decisions
from pisa.domain.errors import PisaError
from pisa.services.conversation import ConversationService

_DEVICE = uuid.UUID("00000000-0000-4000-8000-000000000001")


class _DownPool:
    def connection(self) -> Any:
        raise psycopg.OperationalError("down")


class _BrokenPool:
    def connection(self) -> Any:
        raise KeyError("bug")


def _service(pool: object = None, data: object = None) -> ConversationService:
    service = ConversationService.__new__(ConversationService)
    service._pool = cast(Any, pool or _DownPool())  # pyright: ignore[reportPrivateUsage]
    service._clock = lambda: datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # pyright: ignore[reportPrivateUsage]
    service._owner = cast(Any, SimpleNamespace(zone=ZoneInfo("Europe/Rome"), tag="it-IT"))  # pyright: ignore[reportPrivateUsage]
    service._tools = ToolFamilies(data=cast(Any, data))  # pyright: ignore[reportPrivateUsage]
    return service


def _events(logs: Sequence[Mapping[str, Any]]) -> list[tuple[str, str | None]]:
    return [(e["event"], e.get("during") or e.get("guard")) for e in logs]


def test_unknown_foreign_titles_stay_closed_and_say_so() -> None:
    class Data:
        async def foreign_titles(self) -> list[str]:
            raise psycopg.OperationalError("down")

    service = _service(data=Data())

    async def turn() -> tuple[str, ...] | None:
        with decisions.collecting():
            untrusted.open_turn(["Riunione"])
            try:
                await service._respond(  # pyright: ignore[reportPrivateUsage]
                    _DEVICE, "ciao", "it-IT", _no_emit, watch=None,  # pyright: ignore[reportArgumentType]
                    trace_id="t",
                )  # fmt: skip
            except AttributeError:  # the bare service stops after the guard: only the guard matters here
                pass
            return untrusted.current().foreign

    with capture_logs() as logs:
        assert asyncio.run(turn()) is None  # every list this turn is outside text
    assert ("database_unreachable", "foreign_titles") in _events(logs)
    assert "ciao" not in repr(logs)


def test_acts_unread_back_nothing_and_say_so() -> None:
    with capture_logs() as logs:
        acted = asyncio.run(_service()._acted_lately(_DEVICE))  # pyright: ignore[reportPrivateUsage]
    assert acted == (frozenset(), frozenset())  # an "I did it" is not backed
    assert _events(logs) == [("database_unreachable", "acted_lately")]


def test_an_unknown_phone_counts_as_locked_and_says_so() -> None:
    with capture_logs() as logs:
        assert asyncio.run(_service().phone_locked(_DEVICE)) is True
    assert _events(logs) == [("database_unreachable", "phone_locked")]
    assert logs[0]["closed"] is True


def test_words_only_keeps_its_stated_default_and_says_so() -> None:
    with capture_logs() as logs:
        assert asyncio.run(_service()._words_only("solo testo", tapped=False)) is True  # pyright: ignore[reportPrivateUsage]
        assert asyncio.run(_service()._words_only("che tempo fa", tapped=False)) is False  # pyright: ignore[reportPrivateUsage]
    assert _events(logs) == [("database_unreachable", "words_only")] * 2
    assert "testo" not in repr(logs)


def test_a_bug_in_an_optional_step_keeps_the_default_and_is_logged_by_its_cause() -> None:
    service = _service(pool=_BrokenPool())
    with capture_logs() as logs:
        kept = asyncio.run(service._kept_findings(uuid.uuid4()))  # pyright: ignore[reportPrivateUsage]
        body = asyncio.run(service._body(_DEVICE))  # pyright: ignore[reportPrivateUsage]
    assert (kept, body) == (None, "")
    warned = [e for e in logs if e["log_level"] == "warning"]
    assert [e["event"] for e in warned] == ["kept_findings_failed", "body_line_failed"]
    assert all("exc_info" not in e and e["cause"] for e in warned)  # the trace goes only to debug
    assert [e["event"] for e in logs if e["log_level"] == "debug"] == [
        "kept_findings_failed_trace",
        "body_line_failed_trace",
    ]


def test_a_refused_agenda_leaves_no_next_event_and_logs_its_code() -> None:
    class Data:
        async def agenda_day(self, day: object) -> dict[str, Any]:
            raise PisaError("not_found", "No agenda.")

    with capture_logs() as logs:
        _, upcoming = asyncio.run(_service(data=Data())._situation())  # pyright: ignore[reportPrivateUsage]
    assert upcoming is None
    assert [(e["event"], e.get("code")) for e in logs] == [("pick_situation_refused", "not_found")]


def test_a_tap_on_an_approval_decided_elsewhere_says_nothing_and_logs_its_code() -> None:
    from pisa.services.talk_turns import tap_decision

    class Approvals:
        async def pending_for(self, device_id: uuid.UUID, ids: list[str]) -> list[dict[str, Any]]:
            return [{"lang": "it-IT"}]

        async def decide(self, *args: object, **kwargs: object) -> dict[str, Any]:
            raise PisaError("approval_decided", "Already decided.")

    runtime = cast(Any, SimpleNamespace(approvals=Approvals()))
    with capture_logs() as logs:
        assert asyncio.run(tap_decision(runtime, _DEVICE, "a1", True, "it-IT")) == ("", False)
    assert [(e["event"], e.get("code"), e.get("cause")) for e in logs] == [
        ("tap_decision_refused", "approval_decided", "PisaError")
    ]


async def _no_emit(delta: str, final: bool) -> None:
    return None
