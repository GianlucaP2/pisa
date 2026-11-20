"""Fixes at the merge: each fails without
its fix."""

import dataclasses
from datetime import timedelta

import psycopg

from pisa.brain.research_turn import ResearchTurn
from pisa.domain import understand as rules
from tests.owner import owner_at
from tests.test_understand import u
from tests.test_understanding_service import week  # noqa: F401 (the user's week)
from tests.understand_lexicon import ENTRIES, TODAY

# --- HIGH 1: a name someone else wrote never rewrites the user's words by the rules alone -----------------


def _with_outside(name: str) -> rules.Lexicon:
    return rules.lexicon([dataclasses.replace(e, outside=e.name == name) for e in ENTRIES], TODAY)


def test_an_outside_name_is_only_fixed_by_the_model() -> None:
    lex = _with_outside("Dario Ferri")
    assert not u("chiama dario ferry", lex=lex).corrections  # no model: the user's words stay
    fixed = u("chiama dario ferry", lex=lex, oracle=lambda span, e: True)
    assert [c.via for c in fixed.corrections] == ["model"]


def test_spam_senders_never_reach_the_lexicon(db: psycopg.Connection, week) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    import asyncio

    from pisa.db.pool import open_pool
    from pisa.services.lexicon import LexiconService
    from tests.conftest import Clock, dsn
    from tests.test_research_eval import NOW

    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels) "
        "VALUES ('spam', 'ts', 'Giulia Bianchy', 'x@spam.example', 's', %s, '{SPAM}')",
        (NOW - timedelta(days=1),),
    )

    async def go() -> dict[str, rules.Entry]:
        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            _, entries, _ = await LexiconService(pool, owner=owner_at(), clock=Clock(NOW)).current()
            return {e.name: e for e in entries}
        finally:
            await pool.close()

    names = asyncio.run(go())
    assert "Giulia Bianchy" not in names and "Nino Ferri" in names
    assert names["Nino Ferri"].outside


# --- MEDIUM 2: two days for two plans are not a contradiction; the question names the right day -----------


def test_two_days_for_two_plans_ask_nothing() -> None:
    assert u("oggi pranzo con nino e venerdi cena").ask is None


def test_the_day_question_names_the_day_said() -> None:
    r = u(
        "dopodomani sabato andiamo al vondelpark"
    )  # 24 Oct 2026 is a Saturday: the day after tomorrow is Monday
    assert r.ask == "day" and r.question == "Dopodomani è lunedì: intendi dopodomani o sabato?"


# --- LOW 3: an upper-case scheme never reaches the sources card ---------------------------------------------


def test_an_upper_case_scheme_is_not_a_source() -> None:
    turn = ResearchTurn.__new__(ResearchTurn)
    turn.urls = {"HTTPS://example.com/a": "A", "https://example.com/b": "B"}
    turn.read = set()
    assert [s["url"] for s in turn.sources()] == ["https://example.com/b"]
