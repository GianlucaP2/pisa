"""Cost per good answer with the failures kept, an
honest p95 (each with its n, the cold first turn dropped), and the reflex against its 2 s window. Each
figure has a test that fails when its change is reverted (`tests/mutate_cost.py`), on fixture ledgers with
failures in them: a failure's cost counts."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg

from pisa.domain import cost, latency
from pisa.domain.sheet import SheetData
from tests.assistant_env import rome, set_settings
from tests.replay import harness
from tests.replay.scenarios import SCENARIOS
from tests.test_life_admin import WEEK, hdr
from tests.test_replay import _play

# --- 1. the replay: € per passed scenario and per scenario attempted, failures kept in the spend ----------


def _call(
    db: psycopg.Connection, status: str, eur: str, *, slot: str = "chat.conversation", budget: str = "owner",
    at: datetime | None = None, ms: int | None = None, code: str | None = None,
) -> None:  # fmt: skip
    """One ledger row: a settled call costs `eur`; a reserved or swept one counts its reservation."""
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at, budget, latency_ms, error_code) VALUES (gen_random_uuid(), %s, 'm', %s, %s, %s, true, "
        "'t', coalesce(%s, now()), %s, %s, %s)",
        (
            slot, status, Decimal(eur) if status in ("reserved", "swept") else Decimal(0),
            Decimal(eur) if status in ("settled", "failed") else None, at, budget, ms, code,
        ),
    )  # fmt: skip


def _results() -> list[list[harness.Result]]:
    """Three scenarios played pass^3: one passes every run, one fails a run, one fails every run."""
    ok = harness.Result("a", [], [harness.TurnLog("x", "Fatto.", False, rounds=1, seconds=2.0)], 0, [])
    bad = harness.Result(
        "b", ["false claim"], [harness.TurnLog("y", "Fatto.", False, rounds=1, seconds=3.0)], 0, []
    )
    return [[ok, ok, ok], [ok, bad, ok], [bad, bad, bad]]


# --- 1. first content apart from the whole turn, each p50/p95 with its n; the cold first turn dropped ------


def test_1_first_content_and_total_time_each_with_n_the_cold_turn_dropped() -> None:
    turns = [
        harness.TurnLog("a", "ok", False, rounds=1, seconds=30.0, first_s=25.0, cold=True),  # cold cache
        harness.TurnLog("b", "ok", False, rounds=1, seconds=4.0, first_s=1.0),
        harness.TurnLog("c", "ok", False, rounds=1, seconds=9.0, first_s=2.0),
        harness.TurnLog("d", "ok", False, rounds=1, seconds=5.0, first_s=None),  # Pisa said nothing
        harness.TurnLog("sì", "Fatto.", True, first_s=0.1),  # code answered: no model turn
    ]
    results = [[harness.Result("a", [], turns, 0, [])]]
    text = harness.summary(results)
    assert "model turns 4:" in text  # the cold turn is still a model turn (rounds, tokens)
    assert "total time p50 5.0 s, p95 9.0 s (n=3), max 9.0 s" in text
    assert "first content p50 2.0 s, p95 2.0 s (n=2)" in text
    assert "(the first model turn after a model change dropped: 1 cold)" in text
    assert harness.p95_seconds(results) == 9.0  # the latency guard's p95 drops it too
    assert latency.figure([]) == "p50 -, p95 - (n=0)"


def test_1_mark_cold_takes_the_first_model_turn_and_skips_a_code_only_result() -> None:
    code_only = harness.Result("y", [], [harness.TurnLog("sì", "Fatto.", True)], 0, [])
    assert not harness.mark_cold(code_only) and not code_only.turns[0].cold
    turns = [
        harness.TurnLog("sì", "Fatto.", True),
        harness.TurnLog("a", "ok", False),
        harness.TurnLog("b", "ok", False),
    ]
    assert harness.mark_cold(harness.Result("z", [], turns, 0, []))
    assert [t.cold for t in turns] == [False, True, False]


def test_1_a_played_turn_records_its_first_content_apart_from_its_whole_time(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    scenario = next(s for s in SCENARIOS if s.get("script") and not s.get("world"))
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    model = [t for t in result.turns if not t.by_code and t.reply.strip()]
    assert model, scenario["id"]
    assert all(t.first_s is not None and 0 <= t.first_s <= t.seconds for t in model)


# --- 2. the Sunday card: € per accepted outcome over the week ---------------------------------------------


def _outcomes(db: psycopg.Connection) -> None:
    """2026-W43 (Monday 19 October): what Pisa records. Accepted: one yes to Pisa's proposal, two tasks Pisa
    made that got done (one from an email, one a bill Pisa added), one draft of Pisa's they sent. Not
    accepted: their own words' change, a refused proposal, an undone yes, the send's own approval, their own
    task, an open task of Pisa's, an unsent draft, and last week's yes."""
    inside, before = rome(2026, 10, 21, 10), rome(2026, 10, 12, 10)

    def approval(
        kind: str, status: str, origin: str, auth: str | None, at: datetime, undone: bool = False
    ) -> uuid.UUID:
        approval_id = uuid.uuid4()
        db.execute(
            "INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at, decided_at, "
            "decided_auth, executed_at, origin, undone_at) "
            "VALUES (%s, %s, 's', '{}', %s, %s, %s, %s, %s, %s, %s, %s)",
            (approval_id, kind, status, at, at, at, auth, at if status == "done" else None, origin,
             at if undone else None),
        )  # fmt: skip
        return approval_id

    approval("calendar_move", "done", "proposal", "tap", inside)  # their yes: accepted
    approval("calendar_move", "done", "words", "words", inside)  # their own words: not a yes to Pisa
    approval("calendar_create", "rejected", "proposal", "tap", inside)  # refused
    approval("calendar_cancel", "done", "held", "voice", inside, undone=True)  # undone
    approval("calendar_move", "done", "proposal", "tap", before)  # last week
    send = approval("email_send", "done", "proposal", "biometric", inside)  # counted as the draft
    # the merge (review MEDIUM 1): Pisa acting on its own is no yes of theirs; an undone send no draft
    approval("calendar_move", "done", "proposal", "autonomy:silent", inside)
    approval("calendar_create", "done", "held", "autonomy:tell", inside)
    undone_send = approval("email_send", "done", "proposal", "biometric", inside, undone=True)

    def task(source: str, status: str) -> uuid.UUID:
        task_id = uuid.uuid4()
        db.execute(
            "INSERT INTO tasks (id, title, status, source_kind, title_key, created_at, updated_at) "
            "VALUES (%s, 't', %s, %s, %s, %s, %s)",
            (task_id, status, source, uuid.uuid4().hex, before, inside),
        )
        return task_id

    task("email", "done")  # Pisa made it from an email: accepted
    task("email", "open")  # not done
    task("voice", "done")  # their own task
    bill = task("manual", "done")  # a bill task Pisa added on its own: accepted
    db.execute(
        "INSERT INTO pisa_decisions (at, move, tool, ref, provenance, outcome) "
        "VALUES (%s, 'act', 'bill_task', %s, 'pisa_initiative', 'done')",
        (before, str(bill)),
    )
    for sent_at, approval_id in ((inside, send), (None, None), (inside, undone_send)):
        db.execute(
            "INSERT INTO email_drafts (id, gmail_draft_id, thread_id, reply_to_id, in_reply_to, "
            "to_addresses, subject, body, created_at, updated_at, sent_at, approval_id) "
            "VALUES (gen_random_uuid(), 'g', 't', 'r', 'i', ARRAY['a@example.com'], 's', 'b', "
            "%s, %s, %s, %s)",
            (before, before, sent_at, approval_id),
        )
    # The week's owner spend, failures in it: €0.40 settled, €0.10 a call cut short, €0.05 a swept ghost,
    # a refused call (no cost); the eval budget and last week's never count.
    for status, eur, budget, at in (
        ("settled", "0.40", "owner", inside), ("settled", "0.10", "owner", inside),
        ("swept", "0.05", "owner", inside), ("failed", "0", "owner", inside),
        ("settled", "1.00", "eval", inside), ("settled", "2.00", "owner", before),
    ):  # fmt: skip
        _call(db, status, eur, budget=budget, at=at)


def test_2_the_sunday_card_shows_the_week_s_cost_per_accepted_outcome(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    clock.now = rome(2026, 10, 25, 18)  # Sunday of 2026-W43
    _outcomes(db)
    summary: dict[str, Any] = client.get(f"/v1/ledger/summary?week={WEEK}", headers=hdr(db)).json()
    assert summary["cost_per_outcome"] == {
        "spent_eur": "0.5500",
        "yes": 1,
        "tasks_done": 2,
        "drafts_sent": 1,
        "accepted": 4,
        "per_accepted_eur": "0.1375",
    }
    texts = [b.get("text", "") for b in summary["sheet"]["blocks"]]
    line = (
        "Costo per risultato: spesa AI €0.55 su 4 accettati (1 tuoi sì, 2 attività fatte, 1 bozze inviate) "
        "= €0.1375 l'uno."
    )
    assert line in texts
    SheetData.model_validate(summary["sheet"])


def test_2_a_week_with_nothing_accepted_says_so_and_divides_by_nothing() -> None:
    nothing = cost.Accepted(0, 0, 0)
    assert cost.week(Decimal("0.30"), nothing)["per_accepted_eur"] is None
    assert cost.card_line(Decimal("0.30"), nothing, "en-US").endswith("= nothing accepted this week.")


# --- 3. the reflex: its model reads against the 2 s window, with n, and the reflex-first share ------------


def test_3_the_reflex_figures_count_only_the_model_s_reads_and_a_cut_one_at_the_window() -> None:
    reads = [("model", 900), ("model", 1500), ("slow", 2003), ("rules", 5), ("final", 3000), (None, None)]
    firsts = ["reflex", "brain", "brain", "reflex", None]
    r = latency.reflex(reads, firsts)
    assert r.ms == (900, 1500, 2003) and r.over == 1 and (r.first, r.turns) == (2, 4)
    line = harness.reflex_figures(r)
    assert line == (
        "reflex: model reads p50 1500 ms, p95 ≥ 2003 ms (n=3), past the 2.0 s window 1/3; "
        "reflex-first 2/4 turns (50%)"
    )
    assert harness.reflex_figures(latency.reflex([], [])) == (
        "reflex: no model read (n=0) against the 2.0 s window; reflex-first 0/0 turns"
    )


def test_3_the_weekly_talk_report_carries_the_reflex_against_its_window(client, db) -> None:  # type: ignore[no-untyped-def]
    session = uuid.uuid4()
    db.execute(
        "INSERT INTO talk_sessions (id, talker, voice, trigger, lang, started_at) "
        "VALUES (%s, 'gemini', 'v', 'press', 'it-IT', %s)",
        (session, rome(2026, 10, 19, 9)),
    )
    for n, (by, ms, first) in enumerate(
        [("model", 1200, "reflex"), ("model", 1700, "brain"), ("slow", 2001, "brain"), ("rules", 3, "reflex"),
         (None, None, None)], 1
    ):  # fmt: skip
        db.execute(
            "INSERT INTO talk_turns (session_id, n, owner_end_at, route, failed, unbacked, redone, "
            "reflex_by, reflex_ms, first_content_by) VALUES (%s, %s, %s, 'speaker', 0, 0, false, %s, %s, %s)",
            (session, n, rome(2026, 10, 19, 9, n), by, ms, first),
        )
    report = client.portal.call(client.app.state.runtime.autopilot.owner_loop.weekly, WEEK)
    assert report["reflex"] == {
        "reads": 3,
        "p50_ms": 1700,
        "p95_ms": 2001,
        "over_window": 1,
        "window_ms": 2000,
        "reflex_first": 2,
        "turns": 4,
    }
    from pisa.domain import owner_loop as rules

    sheet = rules.sheet(report, "en-US")
    facts = {b["label"]: b["value"] for b in sheet["blocks"] if b["type"] == "fact"}
    assert facts["Model read"] == "p50 1.7 s, p95 2.0 s (n=3)"
    assert facts["Past the 2.0 s window"] == "1/3" and facts["Reflex first"] == "2/4 (50%)"
    SheetData.model_validate(sheet)


def test_a_turn_keeps_its_slowest_model_read() -> None:
    """Review MEDIUM 2: each pass overwrote the turn's read, so a slow model read followed by the rules (the
    reflex resting after slow reads) or by a faster read left the turn out of "past the window"."""
    from pisa.services.talk import Exchange, _worse_read  # pyright: ignore[reportPrivateUsage]

    at = datetime(2026, 10, 21, 10, tzinfo=UTC)
    e = Exchange(n=1, owner_end_at=at, owner_end=0.0)
    for by, ms in (("rules", 4), ("model", 900), ("slow", 2003), ("model", 600), ("rules", 3), ("final", 2)):
        if _worse_read(e, by, ms):
            e.reflex_by, e.reflex_ms = by, ms
    assert (e.reflex_by, e.reflex_ms) == ("slow", 2003)
    quick = Exchange(n=2, owner_end_at=at, owner_end=0.0)
    for by, ms in (("model", 700), ("model", 1200), ("model", 400)):
        if _worse_read(quick, by, ms):
            quick.reflex_by, quick.reflex_ms = by, ms
    assert (quick.reflex_by, quick.reflex_ms) == ("model", 1200)
    rules = Exchange(n=3, owner_end_at=at, owner_end=0.0)
    for by, ms in (("rules", 4), ("final", 2)):
        if _worse_read(rules, by, ms):
            rules.reflex_by, rules.reflex_ms = by, ms
    assert (rules.reflex_by, rules.reflex_ms) == ("final", 2)  # no model read: the latest pass, as before
