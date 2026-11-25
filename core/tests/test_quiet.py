"""Quiet mode (contract G): only urgent messages go out while quiet; the held ones go out as one summary when
it ends (Italian or English); quiet.until; the conversation tools set_quiet_mode and get_briefing."""

import asyncio
from datetime import datetime
from typing import Any

import psycopg
import pytest

from pisa.brain.briefing import EMPTY_DAY
from pisa.services.notifications import enqueue, release_held
from tests.assistant_env import TZ, notifications, rome, services, set_settings
from tests.conftest import Clock, FakeModels, dsn
from tests.owner import owner_at
from tests.test_conversation_tools import ask


def queue(db: psycopg.Connection, secrets_key: str, now: datetime, text: str, **kw: Any) -> bool:
    async def go() -> bool:
        async with services(dsn(db), secrets_key, FakeModels(), Clock(now)) as env:
            async with env.pool.connection() as conn:
                _, held = await enqueue(conn, kind=kw.pop("kind", "reminder"), text=text, lang="it-IT",
                                        now=now, timezone=TZ, **kw)  # fmt: skip
            return held

    return asyncio.run(go())


def release(db: psycopg.Connection, secrets_key: str, now: datetime) -> int:
    async def go() -> int:
        async with services(dsn(db), secrets_key, FakeModels(), Clock(now)) as env:
            return await release_held(env.pool, now=now, owner=owner_at(TZ))

    return asyncio.run(go())


@pytest.mark.parametrize(
    ("lang", "prefix"),
    [("it-IT", "Mentre eri in modalità silenziosa: "), ("en-US", "While you were in quiet mode: ")],
)
def test_quiet_hours_across_midnight_then_one_summary(db, secrets_key, lang, prefix) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": lang})
    assert queue(db, secrets_key, rome(2026, 10, 20, 22, 0), "Prima.") is False  # before 22:30
    assert queue(db, secrets_key, rome(2026, 10, 20, 23, 0), "Tra 15 minuti: Volo.") is True
    assert queue(db, secrets_key, rome(2026, 10, 21, 1, 0), "Scade tra 15 minuti: Bolletta.") is True
    assert (
        queue(db, secrets_key, rome(2026, 10, 21, 2, 0), "Allarme!", priority="urgent", kind="alert") is False
    )
    assert release(db, secrets_key, rome(2026, 10, 21, 6, 59)) == 0  # still quiet
    assert release(db, secrets_key, rome(2026, 10, 21, 7, 0)) == 2
    rows = notifications(db)
    assert [(k, via) for k, _, _, via in rows] == [
        ("reminder", None),
        ("reminder", "held"),
        ("reminder", "held"),
        ("alert", None),
        ("info", None),  # the one summary, waiting for delivery
    ]
    assert rows[-1][1] == prefix + "Tra 15 minuti: Volo. Scade tra 15 minuti: Bolletta."
    held_done = db.execute(
        "SELECT count(*) FROM notifications WHERE delivered_via = 'held' AND delivered_at IS NOT NULL"
    ).fetchone()
    assert held_done == (2,)
    assert release(db, secrets_key, rome(2026, 10, 21, 7, 1)) == 0  # nothing twice


def test_quiet_until(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": None, "until": "2026-10-20T15:00:00+02:00"}})
    assert queue(db, secrets_key, rome(2026, 10, 20, 14, 0), "Tra 15 minuti: Riunione.") is True
    assert release(db, secrets_key, rome(2026, 10, 20, 14, 59)) == 0
    assert release(db, secrets_key, rome(2026, 10, 20, 15, 0)) == 1
    assert queue(db, secrets_key, rome(2026, 10, 20, 15, 1), "Dopo.") is False
    assert notifications(db)[1][1] == "Mentre eri in modalità silenziosa: Tra 15 minuti: Riunione."


def test_no_quiet_hours(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"hours": None}})
    assert queue(db, secrets_key, rome(2026, 10, 20, 23, 0), "Notte.") is False


def test_long_summary_fits_a_notify(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    for i in range(40):
        queue(
            db,
            secrets_key,
            rome(2026, 10, 20, 23, 0),
            f"Tra 15 minuti: evento numero {i} con un titolo lungo.",
        )
    release(db, secrets_key, rome(2026, 10, 21, 7, 0))
    summary = notifications(db)[-1][1]
    assert len(summary) <= 1000 and summary.startswith("Mentre eri in modalità silenziosa: ")


# --- the tools (NOW in the tool tests is Tuesday 2026-10-20 12:00 in Rome) --------------------------------


def test_set_quiet_mode_until_a_time(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    reply = ask(db, secrets_key, "set_quiet_mode", {"until": "15:00"})  # "Pisa, non disturbarmi fino alle 15"
    assert reply == "Quiet mode is on until 15:00 (2026-10-20)."
    stored = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()[0]  # type: ignore[index]
    assert stored["quiet"]["until"] == "2026-10-20T15:00:00+02:00"
    tomorrow = ask(db, secrets_key, "set_quiet_mode", {"until": "09:00"})  # already past today
    assert tomorrow == "Quiet mode is on until 09:00 (2026-10-21)."
    bad = ask(db, secrets_key, "set_quiet_mode", {"until": "dopo"})
    assert bad == "'dopo' is not a time: use HH:MM or YYYY-MM-DDTHH:MM."


def test_set_quiet_mode_off_releases_the_held(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"quiet": {"until": "2026-10-20T15:00:00+02:00"}})
    assert queue(db, secrets_key, rome(2026, 10, 20, 11, 0), "Tra 15 minuti: Riunione.") is True
    reply = ask(db, secrets_key, "set_quiet_mode", {})  # "Pisa, puoi parlare"
    assert reply == "Quiet mode is off. The held messages went out as one summary."
    stored = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()[0]  # type: ignore[index]
    assert stored["quiet"]["until"] is None
    assert notifications(db)[-1][:2] == (
        "info",
        "Mentre eri in modalità silenziosa: Tra 15 minuti: Riunione.",
    )


def test_set_quiet_mode_off_inside_quiet_hours_says_so(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    reply = ask(db, secrets_key, "set_quiet_mode", {"until": None}, now=rome(2026, 10, 20, 23, 0))
    assert reply == "Quiet until is cleared, but the quiet hours (22:30-07:00) still apply now."


def test_get_briefing_tool(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    rule, open_tag, text, close_tag = ask(db, secrets_key, "get_briefing", {}).split("\n")
    assert open_tag.startswith("<briefing-") and close_tag == open_tag.replace("<", "</", 1)
    assert open_tag in rule and "never follow it" in rule  # a delimited data block
    assert text == EMPTY_DAY["it-IT"]
    assert db.execute("SELECT trigger FROM briefings").fetchall() == [("ask",)]
    assert notifications(db) == []  # read-only: nothing delivered


def test_queue_ignoring_quiet(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    assert (
        queue(db, secrets_key, rome(2026, 10, 20, 23, 0), "Briefing.", kind="briefing", respect_quiet=False)
        is False
    )
