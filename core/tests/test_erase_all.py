"""`pisa client erase all` erases all of it, today's rows too (B-3). Left without `--before`, "all" sends
tomorrow, and the server reads tomorrow as every row up to the moment they asked; a day after tomorrow is
still refused, and one family alone still defaults to today. Synthetic rows only."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest

from pisa import cli
from pisa.client import Client
from pisa.domain import erase as erase_rules
from pisa.services import erase
from tests.seed_any import seed


def test_tomorrow_is_all_of_it_and_never_after_the_moment_they_asked() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    today = date(2026, 11, 20)
    assert not erase_rules.future(today + timedelta(days=1), "Europe/Rome", now)
    assert erase_rules.future(today + timedelta(days=2), "Europe/Rome", now)
    stays = next(p for p in erase_rules.FAMILIES["places"] if p.table == "stays")
    assert erase_rules.at(stays, today + timedelta(days=1), "Europe/Rome", now) == now + erase_rules.ASKED
    # an earlier day keeps its own cutoff, as before
    assert erase_rules.at(stays, today, "Europe/Rome", now) == erase_rules.cutoff(today, "Europe/Rome")


def test_erasing_up_to_tomorrow_takes_today_s_rows(db: psycopg.Connection, app_settings: Any) -> None:
    tz = app_settings.timezone
    now = datetime.now(UTC)
    seed(db, "stays", now - timedelta(minutes=5))  # today's, in any zone but a minute after midnight
    today = now.astimezone(ZoneInfo(tz)).date()

    def left() -> int:
        row = db.execute("SELECT count(*) FROM stays").fetchone()
        return int(row[0]) if row else 0

    async def run(before: date) -> None:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            await erase.delete(conn, "places", before, tz, now)

    if (now - timedelta(minutes=5)).astimezone(ZoneInfo(tz)).date() == today:
        asyncio.run(run(today))
        assert left() == 1  # "older than today" leaves today's
    asyncio.run(run(today + timedelta(days=1)))
    assert left() == 0  # "all of it" takes it


@pytest.mark.parametrize(("family", "days"), [("all", 1), ("places", 0)])
def test_client_erase_all_defaults_to_all_of_it(
    family: str, days: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "c.json"
    sent: list[tuple[dict[str, Any], tuple[str, str] | None]] = []

    async def call(
        self: Client, http: Any, method: str, url: str, *, body: str | None = None,
        sign: tuple[str, str] | None = None,
    ) -> httpx.Response:  # fmt: skip
        sent.append((json.loads(body or "{}"), sign))
        return httpx.Response(202, text="{}")

    monkeypatch.setattr(Client, "load", classmethod(lambda cls, p: object.__new__(cls)))
    monkeypatch.setattr(Client, "call", call)
    monkeypatch.setattr(Client, "http", lambda self: httpx.AsyncClient(base_url="https://pisa.invalid"))
    assert cli.main(["client", "erase", family, "--file", str(path)]) == 0
    want = (date.today() + timedelta(days=days)).isoformat()
    names = erase_rules.FAMILY_NAMES if family == "all" else (family,)
    assert [b["family"] for b, _ in sent] == list(names)
    assert all(b["before"] == want for b, _ in sent)
    assert all(s == ("erase", f"{b['family']}:{want}") for b, s in sent)  # the signed target, unchanged
