"""The night's frame (the five conditions). Each test fails on revert."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.domain import night as rules
from pisa.domain.errors import PisaError
from pisa.infra import hard_limit, ledger
from tests.conftest import pair
from tests.test_measure import _same

NIGHT_AT = datetime(2026, 10, 21, 1, 30, tzinfo=UTC)  # 03:30 in Rome (CEST): the night of 21 October
YESTERDAY = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # 10:00 in Rome on the 20th
CONTACT = "Ornella Ravaglia"


def _night(client: Any) -> Any:
    return client.app.state.runtime.night


def _run(client: Any, fn: Any, *args: Any) -> Any:
    return client.portal.call(fn, *args)


def _count(db: psycopg.Connection, table: str) -> int:
    return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # type: ignore[index]


def _conversation(db: psycopg.Connection) -> uuid.UUID:
    device_id, _ = pair(db)
    cid = uuid.uuid4()
    db.execute("INSERT INTO conversations (id, device_id) VALUES (%s, %s)", (cid, device_id))
    return cid


def _reply(tool: str | None) -> Any:
    """Pisa's reply's model messages as the conversation keeps them: its tool call, then the return."""
    parts = [{"part_kind": "tool-call", "tool_name": tool, "args": "{}", "tool_call_id": "x"}] if tool else []
    return Jsonb([{"kind": "response", "parts": parts}])


def _turns(db: psycopg.Connection, n: int, *, at: datetime = YESTERDAY, tool: str = "get_agenda") -> None:
    """n exchanges of his yesterday, a minute apart: his words, then Pisa's reply carrying its tool call."""
    cid = _conversation(db)
    for i in range(n):
        t = at + timedelta(minutes=i)
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, lang, created_at) "
            "VALUES (%s, 'user', %s, 'it-IT', %s)",
            (cid, f"cosa ho oggi {i}?", t),
        )
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, lang, created_at, model_messages) "
            "VALUES (%s, 'assistant', 'Hai la palestra.', 'it-IT', %s, %s)",
            (cid, t + timedelta(seconds=10), _reply(tool)),
        )


def _tavily(client: Any, http_handler: dict[str, Any]) -> list[str]:
    """Tavily set up, answered by a fake; the queries that left Pisa."""
    import httpx

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            sent.append(json.loads(request.content)["query"])
            found = [{"url": "https://e.invalid", "title": "t", "content": "c"}]
            return httpx.Response(200, json={"results": found, "usage": {"credits": 1}})
        return httpx.Response(404)

    http_handler["handler"] = handler
    _run(client, client.app.state.runtime.place_providers.set_key, "tavily", "tvly-test-key-0001")
    return sent


def _running(db: psycopg.Connection) -> None:
    """Tonight's night is running (searches are a night step's, never outside one)."""
    db.execute("INSERT INTO pisa_nights (night, started_at) VALUES ('2026-10-21', %s)", (NIGHT_AT,))


async def _tools() -> set[str]:
    return {"get_agenda", "get_tasks"}


# --- (a) shadow: the prompt is byte-identical with the night's tables empty and full ---------------------


def _fill_the_night(db: psycopg.Connection) -> None:
    """Every table the night frame adds, filled, and a night's call on the ledger."""
    db.execute(
        "INSERT INTO pisa_nights (night, started_at, finished_at, status, counts, tools, spent_eur, brier) "
        "VALUES ('2026-10-19', now(), now(), 'done', %s, '{get_agenda}', 0.1, 0.2)",
        (Jsonb({"n8_replay": 3}),),
    )
    db.execute(
        "INSERT INTO pisa_night_searches (night, at, kind, stripped, sent) "
        "VALUES ('2026-10-19', now(), 'c', 2, true)"
    )
    db.execute(
        "INSERT INTO pisa_night_replays (night, message_id, tools, missing, replied, passed) "
        "VALUES ('2026-10-19', 1, '{get_agenda}', '{}', true, true)"
    )
    db.execute(
        "INSERT INTO pisa_night_predictions (night, for_day, kind, ref, p, outcome) "
        "VALUES ('2026-10-19', '2026-10-19', 'event_kept', '[\"c\", \"e\", 0]', 0.8, true), "
        "('2026-10-19', '2026-10-20', 'talks', NULL, 0.7, NULL)"
    )
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at, budget) VALUES (gen_random_uuid(), 'utility', 'x', 'settled', 0.1, 0.1, true, 't', "
        "now(), 'night')"
    )


def test_a_a_turn_s_prompt_is_byte_identical_with_the_night_s_tables_empty_and_full(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    seen: list[tuple[str, str, int]] = []
    for filled in (False, True):
        if filled:
            _fill_the_night(db)
        model = Heard()
        fake_models.default = model
        socket_turns(client, db, secrets_key, model, "che cosa ho oggi?")  # type: ignore[arg-type]
        history = json.dumps(
            [[str(getattr(p, "content", "")) for p in m.parts] for m in fake_models.seen_messages[-1]]
        )
        seen.append((_same(model.instructions[0]), _same(history), len(model.instructions[0])))
        db.execute("TRUNCATE messages, conversations, pisa_decisions, notifications, devices CASCADE")
    assert seen[0] == seen[1]  # byte-identical: the night adds 0 chars to the first request


def test_a_the_decider_s_prompt_is_byte_identical_with_the_night_s_tables_full(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain import attend as brain
    from pisa.domain import attend as A
    from tests.test_measure import T0
    from tests.test_proactivity import _attend

    async def prompt() -> str:
        async with _attend(db, T0) as env:
            candidates = [A.Candidate(str(uuid.UUID(int=1)), "replan", "replan", "Il piano.", "it-IT", T0)]
            moment = await env.attend._moment(candidates, T0)  # pyright: ignore[reportPrivateUsage]
            return _same(brain.prompt(candidates, moment)[0])

    empty = asyncio.run(prompt())
    _fill_the_night(db)
    assert asyncio.run(prompt()) == empty


# --- (b) the night's own budget and the search cap ----------------------------------------------------------


def test_b_a_night_estimated_at_30_cents_stops_at_25_and_his_next_turn_is_served(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT

    async def spend() -> tuple[int, str | None, Decimal]:
        made, refused = 0, None
        async with client.app.state.runtime.pool.connection() as conn:
            with ledger.night_budget():
                for _ in range(3):  # three calls of €0.10: €0.30 estimated
                    try:
                        await ledger.reserve(
                            conn, slot="utility", model_ref="x", estimate_eur=Decimal("0.10"),
                            price_known=True, trace_id="n", now=clock.now, tz="Europe/Rome",
                            hard=hard_limit.DEFAULT,
                        )  # fmt: skip
                        made += 1
                    except PisaError as err:
                        refused = err.code
            # their morning turn: the user's budget, untouched by the night
            await ledger.reserve(conn, slot="conversation", model_ref="x", estimate_eur=Decimal("0.50"),
                                 price_known=True, trace_id="o", now=clock.now, tz="Europe/Rome",
                                 hard=hard_limit.DEFAULT)  # fmt: skip
            owner = await ledger.spent_since(conn, clock.now - timedelta(hours=1))
        return made, refused, owner

    made, refused, owner = _run(client, spend)
    assert (made, refused) == (2, "budget_exceeded")  # €0.20 spent, the third would pass €0.25
    assert owner == Decimal("0.50")  # the night's €0.20 is not theirs
    budgets = db.execute("SELECT budget, count(*) FROM llm_calls GROUP BY budget ORDER BY budget").fetchall()
    assert budgets == [("night", 2), ("owner", 1)]


def test_b_a_night_stops_once_his_ai_caps_are_reached_and_his_turns_are_still_his(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """The night runs by default (step_learn is supported): its calls are refused once the user's AI caps are
    reached, their spend and the night's together, while their own turns count only their own spend."""
    clock.now = NIGHT_AT

    async def reserve(conn: Any, eur: str) -> str | None:
        try:
            await ledger.reserve(
                conn, slot="utility", model_ref="x", estimate_eur=Decimal(eur), price_known=True,
                trace_id="n", now=clock.now, tz="Europe/Rome", hard=hard_limit.DEFAULT,
            )  # fmt: skip
        except PisaError as err:
            return err.code
        return None

    async def spend() -> tuple[str | None, str | None, str | None, str | None]:
        async with client.app.state.runtime.pool.connection() as conn:
            await ledger.set_caps(conn, ledger.Caps(Decimal("0.30"), Decimal("10")))
            theirs = await reserve(conn, "0.20")  # their own turn, on their budget
            with ledger.night_budget():
                first = await reserve(conn, "0.05")  # €0.25 together: under their €0.30
                second = await reserve(conn, "0.10")  # €0.35 together: past their cap, the night stops
            after = await reserve(conn, "0.04")  # their next turn: €0.24 of theirs, served
        return theirs, first, second, after

    assert _run(client, spend) == (None, None, "budget_exceeded", None)
    budgets = db.execute("SELECT budget, count(*) FROM llm_calls GROUP BY budget ORDER BY budget").fetchall()
    assert budgets == [("night", 1), ("owner", 2)]


def test_b_a_step_refused_by_the_cap_ends_the_night_as_capped(client, db, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    night = _night(client)
    ran: list[str] = []

    async def over(_: Any) -> int:
        assert ledger.budget() == "night"  # the step's calls go on the night's budget
        raise PisaError("budget_exceeded", "cap", "tomorrow")

    async def after(_: Any) -> int:
        ran.append("predict")
        return 0

    monkeypatch.setattr(night, "replay", over)
    monkeypatch.setattr(night, "predict", after)
    out = _run(client, night.run)
    assert out.get("capped") == 1 and ran == []
    assert db.execute("SELECT status FROM pisa_nights").fetchone() == ("capped",)


def test_b_the_41st_search_is_refused(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    night = _night(client)
    sent: list[str] = []

    async def web(query: str) -> list[Any]:
        sent.append(query)
        return []

    night._web = web  # pyright: ignore[reportPrivateUsage]
    _running(db)
    results = [
        _run(client, lambda: night.search("bando freelance digitale Italia", kind="c")) for _ in range(41)
    ]
    assert len(sent) == rules.NIGHT_SEARCHES == 40
    assert results[-1] is None and all(r == [] for r in results[:40])
    assert db.execute(
        "SELECT count(*) FILTER (WHERE sent), count(*) FILTER (WHERE refused = 'cap') "
        "FROM pisa_night_searches"
    ).fetchone() == (40, 1)


def test_b_a_search_outside_a_running_night_is_refused_and_a_crashed_night_is_failed(  # type: ignore[no-untyped-def]
    client, db, clock, monkeypatch
) -> None:
    clock.now = NIGHT_AT
    night = _night(client)
    sent: list[str] = []

    async def web(query: str) -> list[Any]:
        sent.append(query)
        return []

    night._web = web  # pyright: ignore[reportPrivateUsage]
    assert _run(client, lambda: night.search("bando freelance digitale Italia", kind="c")) is None
    assert sent == [] and _count(db, "pisa_nights") == 0  # no night row made by a search: tonight still runs

    async def broken(_: Any) -> int:
        raise RuntimeError("a bug")

    monkeypatch.setattr(night, "replay", broken)
    with pytest.raises(RuntimeError):
        _run(client, night.run)
    assert db.execute("SELECT status FROM pisa_nights").fetchone() == ("failed",)


# --- (c) the stop -------------------------------------------------------------------------------------------


def test_c_with_ferma_tutto_on_the_night_makes_no_call_and_writes_no_row(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _turns(db, 3)
    db.execute("INSERT INTO pisa_stops (stopped_at, stopped_by) VALUES (%s, 'voice')", (NIGHT_AT,))
    night = _night(client)
    sent: list[str] = []

    async def web(query: str) -> list[Any]:
        sent.append(query)
        return []

    night._web = web  # pyright: ignore[reportPrivateUsage]
    calls = _count(db, "llm_calls")
    assert _run(client, night.run) == {"paused": 1}
    assert _count(db, "llm_calls") == calls
    for table in ("pisa_nights", "pisa_night_replays", "pisa_night_predictions"):
        assert _count(db, table) == 0, table
    _running(db)  # even inside a running night, a stop refuses the search and writes no row
    assert _run(client, lambda: night.search("bando freelance digitale Italia", kind="c")) is None
    assert sent == [] and _count(db, "pisa_night_searches") == 0


def test_c_a_stop_said_mid_night_ends_it_before_the_next_step(client, db, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    night = _night(client)
    ran: list[str] = []

    async def replay(_: Any) -> int:
        db.execute("INSERT INTO pisa_stops (stopped_at, stopped_by) VALUES (%s, 'app')", (NIGHT_AT,))
        ran.append("replay")
        return 0

    async def predict(_: Any) -> int:
        ran.append("predict")
        return 0

    monkeypatch.setattr(night, "replay", replay)
    monkeypatch.setattr(night, "predict", predict)
    assert _run(client, night.run).get("stopped") == 1
    assert ran == ["replay"]


# --- (d) yesterday as a regression test, from night 1 -------------------------------------------------------


def test_d_night_1_replays_yesterday_s_real_turns_capped_at_30(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _turns(db, 35)
    _turns(db, 2, at=YESTERDAY - timedelta(days=2))  # not yesterday: not replayed
    out = _run(client, _night(client).run)  # tonight's real tools (the conversation agent's)
    assert out["n8_replay"] == rules.REPLAY_MAX == 30
    assert db.execute("SELECT count(*), bool_and(passed) FROM pisa_night_replays").fetchone() == (30, True)
    counts, tools = db.execute("SELECT counts, tools FROM pisa_nights").fetchone()  # type: ignore[misc]
    assert counts["n8_passed"] == 30 and "get_agenda" in tools


def test_d_a_tool_pisa_had_last_night_and_lost_tonight_fails_yesterday_s_turn(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _turns(db, 1, tool="old_tool")
    _turns(db, 1, at=YESTERDAY + timedelta(hours=1))
    db.execute(
        "INSERT INTO pisa_nights (night, started_at, status, tools) VALUES ('2026-10-20', %s, 'done', "
        "'{get_agenda,old_tool}')",
        (NIGHT_AT - timedelta(days=1),),
    )
    night = _night(client)
    night._offered = _tools  # pyright: ignore[reportPrivateUsage]
    _run(client, night.run)
    rows = db.execute("SELECT tools, missing, passed FROM pisa_night_replays ORDER BY message_id").fetchall()
    assert rows == [(["old_tool"], ["old_tool"], False), (["get_agenda"], [], True)]
    counts = db.execute("SELECT counts FROM pisa_nights WHERE night = '2026-10-21'").fetchone()[0]  # type: ignore[index]
    assert (counts["n8_passed"], counts["n8_failed"]) == (1, 1)


# --- N9: tomorrow's predictions, scored by Brier ------------------------------------------------------------


def test_five_to_ten_predictions_then_scored_by_brier(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    night = _night(client)
    night._offered = _tools  # pyright: ignore[reportPrivateUsage]
    db.execute("INSERT INTO calendars (id, name) VALUES ('c', 'Personale')")
    for i in range(7):  # tomorrow holds seven events: five kinds + five events at most
        start = datetime(2026, 10, 21, 7 + i, 0, tzinfo=UTC)
        db.execute(
            "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
            "VALUES ('c', %s, 'Riunione', %s, %s, false, 'confirmed')",
            (f"e{i}", start, start + timedelta(hours=1)),
        )
    _run(client, night.run)
    made = db.execute("SELECT kind, p FROM pisa_night_predictions WHERE for_day = '2026-10-21'").fetchall()
    assert rules.PREDICT_MIN <= len(made) <= rules.PREDICT_MAX == len(made) == 10
    assert all(0 < p < 1 for _, p in made)
    # the day happens: they talk at 10:00 (not early, not late), e0 is cancelled, the rest kept
    _turns(db, 1, at=datetime(2026, 10, 21, 8, 0, tzinfo=UTC))
    db.execute("UPDATE calendar_events SET status = 'cancelled' WHERE id = 'e0'")
    clock.now = NIGHT_AT + timedelta(days=1)
    out = _run(client, night.run)
    assert out["n9_score"] == 10
    rows = db.execute(
        "SELECT kind, ref, p, outcome FROM pisa_night_predictions WHERE for_day = '2026-10-21' ORDER BY id"
    ).fetchall()
    happened = {"talks": True, "early": False, "evening": False, "busy": False, "task_done": False}
    want = [happened[k] if k != "event_kept" else json.loads(ref)[1] != "e0" for k, ref, _, _ in rows]
    assert [o for *_, o in rows] == want
    expected = rules.brier([(float(p), o) for _, _, p, o in rows])
    assert db.execute("SELECT brier FROM pisa_nights WHERE night = '2026-10-22'").fetchone()[
        0
    ] == pytest.approx(  # type: ignore[index]
        expected, abs=1e-4
    )


def test_brier_is_the_mean_squared_error() -> None:
    assert rules.brier([(0.8, True), (0.3, False)]) == pytest.approx((0.04 + 0.09) / 2)
    assert rules.brier([]) is None
    assert rules.rate(0, 0) == 0.5 and rules.rate(13, 14) == pytest.approx(14 / 16, abs=1e-3)


# --- (e) the night reaches them only on a sheet, a view or a private page
# ------------------------------------


def test_e_a_full_night_writes_nothing_he_sees(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _turns(db, 5)
    seen = (
        "notifications",
        "messages",
        "attend_candidates",
        "tasks",
        "approvals",
        "pisa_decisions",
        "llm_calls",
    )
    before = {t: _count(db, t) for t in seen}
    night = _night(client)
    night._offered = _tools  # pyright: ignore[reportPrivateUsage]
    out = _run(client, night.run)
    assert out["done"] == 1 and out["n8_replay"] == 5 and out["n9_predict"] >= rules.PREDICT_MIN
    assert {t: _count(db, t) for t in seen} == before
    assert _run(client, night.run) == {"already": 1}  # once a night


# --- privacy: searches leave Pisa with nothing personal
# --------------------------------------------------------


def test_privacy_a_contact_s_name_seeded_in_a_query_never_leaves(client, db, clock, http_handler) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, 'ornella ravaglia', 'email', '{ornella.f@example.invalid}', "
        "now(), now())",
        (CONTACT,),
    )
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('trips', %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (Jsonb({"names": ["Livia Ferrante"]}),),
    )
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, sent_at) "
        "VALUES ('m1', 't1', 'Marco Bellini', 'mb85@example.invalid', now())"
    )  # their own name, only on their sent mail (their account's local part is "mb85")
    db.execute(
        "INSERT INTO profile_claims (id, kind, statement, provenance, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'routine', 'Lavora da Accenture a Bergamo con la sorella Chiara', "
        "'said', "
        "now(), now())"
    )
    sent = _tavily(client, http_handler)
    night = _night(client)  # the real night_web: Tavily, on the night's own place budget
    _running(db)
    query = (
        "rimborso IVA freelance per Ornella Ravaglia (ornella.f@example.invalid) e Livia, "
        "Via Immaginata 12 Lecce, €1.200 entro il 12/03/2027 o 15 marzo, Marco Bellini, "
        "Accenture Bergamo Chiara"
    )
    assert _run(client, lambda: night.search(query, kind="c")) is not None
    out = sent[0].casefold()
    for secret in ("ornella", "ravaglia", "example.invalid", "mb85", "garibaldi", "1.200", "12", "03",
                   "marzo", "marco", "bellini", "accenture", "bergamo", "chiara", "€"):  # fmt: skip
        assert secret not in out.split() and secret not in out.replace(" ", ""), secret
    assert "rimborso iva freelance" in out
    row = db.execute("SELECT stripped, sent FROM pisa_night_searches").fetchone()
    assert row is not None and row[0] >= 6 and row[1] is True
    # a query that is only their contact's name says nothing: it is not sent
    assert _run(client, lambda: night.search("Ornella Ravaglia", kind="c")) is None and len(sent) == 1


LEAKS = [  # Each must lose the personal part
    ("regalo per Ornella's compleanno", "ornella"),
    ("regalo per l'Ornella", "ornella"),
    ("regalo dall\u2019Ornella", "ornella"),
    ("studio Ravaglia-Rossi commercialista", "ravaglia"),
    ("codice fiscale BNCMRA85T10F205X", "bncmra85t10f205x"),
    ("IBAN IT60X0542811101000000123456 bonifico", "it60x"),
    ("partita IVA 12345678901 forfettario", "12345678901"),
    ("bollo auto targa AB123CD", "ab123cd"),
    ("rimborso volo AZ1234 ritardo", "az1234"),
    ("rimborso di duemila euro", "duemila"),
    ("refund of two thousand euros", "thousand"),
    ("refund of two thousand euros", "euros"),
    ("grant € two thousand startup", "two"),
    ("deadline 4th November grant", "november"),
    ("grant November 4th deadline", "4th"),
    ("convocatoria 4 noviembre ayudas", "noviembre"),
    ("nato nel 1985 agevolazioni", "1985"),
    ("Иван Петров grant startup", "иван"),
    ("王伟 grant startup", "王伟"),
    ("mb85 freelance tax", "mb85"),
    ("lavoro Accenture Bergamo consulenza", "accenture"),
]


@pytest.mark.parametrize(("query", "secret"), LEAKS, ids=[q for q, _ in LEAKS])
def test_privacy_the_filter_strips_each_leak(query: str, secret: str) -> None:
    names = ["Ornella Ravaglia", "mb85", "Accenture", "Bergamo", "Ivan"]
    clean, n = rules.scrub(query, names)
    assert secret not in clean.casefold() and n >= 1, clean
    assert rules.sendable(clean) or len(clean.split()) < 2, clean


def test_privacy_the_filter_strips_names_addresses_amounts_and_dates() -> None:
    clean, n = rules.scrub(
        "bando Ravàglia a Lecce, $40k, 2026-03-12, martedì, +39 333 1234567", ["Ornella Ravaglia"]
    )
    assert "Rav" not in clean and "40" not in clean and "03" not in clean and "333" not in clean
    assert "martedì" not in clean and n >= 5
    assert rules.scrub("bandi startup 2026 Italia", [])[0] == "bandi startup Italia"  # a year is a date
    assert (
        rules.scrub("via email freelance VAT refund Italy", [])[0] == "via email freelance VAT refund Italy"
    )
    assert not rules.sendable(rules.scrub("Ornella, 12/03", [CONTACT])[0])


# --- shadow, by construction: nothing but the night reads its tables ----------------------------------------


def test_a_no_turn_intake_profile_grounding_or_style_path_reads_the_night_s_tables() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "pisa"
    readers = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py") if "pisa_night" in p.read_text(encoding="utf-8")
    )
    # the morning sheet counts the night's rows for its story and diary (a night step, never a turn's path)
    # The retention prune (deletes by age, keeps a cited message; never a turn path)
    # The night-shift story counts them on purpose (by code, tested in test_night_story)
    # A confident miss's day, for its cause and the knock's line (by code, tested in test_itch_and_traits)
    assert readers == [
        "domain/erase.py",  # Their deletion by family (signed, the API only)
        "services/night.py",
        "services/stanotte.py",
        "services/voice3.py",
        "services/voice6.py",
    ]
    from pisa.services import night

    # the memory night's steps come first, the learning steps after them
    assert rules.STEPS[7:10] == ("n9_score", "n8_replay", "n9_predict")
    assert not hasattr(night.NightService, "notify")  # the night has no way to their phone


# --- the review of 99d240e -----------------------------------------------------------------


def test_a_real_socket_turn_is_replayed_with_the_tool_pisa_called(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock
) -> None:
    from tests.test_acting import Model, socket_turns

    clock.now = YESTERDAY  # yesterday, 10:00 in Rome: two real turns over the socket
    model = Model("cosa ho oggi?", [("get_agenda", {"day": "today"})])
    fake_models.default = model
    # "grazie" first: an exchange ends at their next words, so the later tool call is never the first's
    socket_turns(client, db, secrets_key, model, "grazie", "cosa ho oggi?")  # type: ignore[arg-type]
    clock.now = NIGHT_AT
    night = _night(client)
    _run(client, night.run)  # tonight's real tools
    rows = db.execute("SELECT tools, replied, passed FROM pisa_night_replays ORDER BY message_id").fetchall()
    assert rows == [([], True, True), (["get_agenda"], True, True)]
    # the next night, Pisa has lost get_agenda: yesterday's same kind of turn fails
    clock.now = YESTERDAY + timedelta(days=1)
    socket_turns(client, db, secrets_key, model, "cosa ho oggi?")  # type: ignore[arg-type]
    clock.now = NIGHT_AT + timedelta(days=1)
    night._offered = _tools_without_agenda  # pyright: ignore[reportPrivateUsage]
    _run(client, night.run)
    lost = db.execute("SELECT missing, passed FROM pisa_night_replays WHERE night = '2026-10-22'").fetchall()
    assert lost == [(["get_agenda"], False)]


async def _tools_without_agenda() -> set[str]:
    return {"get_tasks"}


def test_the_night_s_searches_never_spend_his_places_cap_or_notify_him(  # type: ignore[no-untyped-def]
    client, db, clock, http_handler
) -> None:
    from tests.assistant_env import set_settings

    clock.now = NIGHT_AT
    sent = _tavily(client, http_handler)
    set_settings(db, {"places": {"monthly_cap_eur": 0.01}})  # their cap: one cent of paid calls
    db.execute(  # the night already used the whole free tier this month
        "INSERT INTO place_calls (provider, sku, units, at, ok, budget) "
        "VALUES ('tavily', 'tavily.credit', 1000, %s, true, 'night')",
        (NIGHT_AT - timedelta(days=1),),
    )
    night = _night(client)
    _running(db)
    assert _run(client, lambda: night.search("bando freelance digitale Italia", kind="c")) is not None
    assert db.execute(
        "SELECT budget, units, reserved FROM place_calls WHERE at = %s", (NIGHT_AT,)
    ).fetchall() == [("night", 1, False)]
    providers = client.app.state.runtime.place_providers

    async def his_search() -> Any:
        return await providers.choose("web", lambda a: a.search("orari farmacia", max_results=1),
                                      usage=lambda a: a.usage_for_search())  # fmt: skip

    assert _run(client, his_search).provider == "tavily"  # their day's web is still free for them
    assert _count(db, "notifications") == 0
    # the night's own month: once it is spent, the night stops searching, silently
    db.execute(
        "INSERT INTO place_calls (provider, sku, units, at, ok, budget) "
        "VALUES ('tavily', 'tavily.credit', 2000, %s, true, 'night')",
        (NIGHT_AT - timedelta(days=1),),
    )
    before = len(sent)
    assert _run(client, lambda: night.search("bandi digitali Italia", kind="c")) is None
    assert len(sent) == before and _count(db, "notifications") == 0
    assert db.execute("SELECT refused FROM pisa_night_searches ORDER BY id DESC LIMIT 1").fetchone() == (
        "budget",
    )


def test_early_and_evening_rates_count_every_day(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    for back in (3, 5):  # they talked on two of the 14 days, both before 09:00 Rome
        _turns(db, 1, at=datetime(2026, 10, 21 - back, 5, 0, tzinfo=UTC))
    night = _night(client)
    night._offered = _tools  # pyright: ignore[reportPrivateUsage]
    _run(client, night.run)
    p = dict(
        db.execute("SELECT kind, p FROM pisa_night_predictions WHERE kind IN ('early', 'evening')").fetchall()
    )
    assert p["early"] == pytest.approx(rules.rate(2, 14), abs=1e-3)  # not rate(2, 2)
    assert p["evening"] == pytest.approx(rules.rate(0, 14), abs=1e-3)


def test_the_night_runs_only_in_his_night_hours(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import zone

    clock.now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)  # 11:00 in Rome: not a night
    assert _run(client, _night(client).run) == {"not_night": 1}
    assert _count(db, "pisa_nights") == 0
    # 03:30 in Rome is 22:30 in New York: while they are there, Rome's job does nothing
    client.app.state.runtime.owner.place = zone.PhoneContext("America/New_York", "US")
    clock.now = NIGHT_AT
    assert _run(client, _night(client).run) == {"not_night": 1}


def test_an_event_id_with_a_bar_is_scored_and_a_bad_ref_skipped(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    night = _night(client)
    night._offered = _tools  # pyright: ignore[reportPrivateUsage]
    db.execute("INSERT INTO calendars (id, name) VALUES ('c|x', 'Personale')")
    start = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES ('c|x', 'e|1', 'Riunione', %s, %s, false, 'confirmed')",
        (start, start + timedelta(hours=1)),
    )
    _run(client, night.run)  # tonight predicts the event, with its ids as they are
    db.execute(
        "INSERT INTO pisa_night_predictions (night, for_day, kind, ref, p) "
        "VALUES ('2026-10-21', '2026-10-21', 'event_kept', 'broken', 0.5)"
    )
    clock.now = NIGHT_AT + timedelta(days=1)
    out = _run(client, night.run)
    assert out["n9_score"] == 6  # five kinds and the event scored; the broken one skipped (never the night)
    rows = db.execute(
        "SELECT ref, outcome FROM pisa_night_predictions WHERE kind = 'event_kept' ORDER BY id"
    ).fetchall()
    assert [o for _, o in rows] == [True, None]


def test_a_search_s_kind_is_a_step_s_name(client) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        _run(client, lambda: _night(client).search("bando freelance digitale Italia", kind=CONTACT))


def test_a_possessive_and_an_elision_go_with_the_name() -> None:
    names = ["Ornella Ravaglia"]
    assert rules.scrub("regalo per Ornella's compleanno", names)[0] == "regalo per compleanno"
    assert rules.scrub("regalo dall'Ornella per la festa", names)[0] == "regalo per la festa"
