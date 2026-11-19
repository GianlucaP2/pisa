"""The competence ledger, the signals,
the harness that lives a week, the memory section, calibration, sampling agreement. Shadow only: nothing Pisa
does reads any of it. Each row's test fails when its fix is reverted (`tests/mutate_measure.py`)."""

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pisa.domain import attend as A
from pisa.domain import calibration as cal
from pisa.domain import signals
from pisa.domain.body import Presence, stamped
from tests.assistant_env import rome
from tests.conftest import pair
from tests.owner import owner_at
from tests.replay import harness
from tests.replay.scenarios import SCENARIOS
from tests.test_proactivity import _attend, _propose
from tests.test_skills import _on, rt, skills, wish

T0 = rome(2026, 10, 29, 9)
_NONCE = re.compile(r"-[0-9a-f]{16}>")  # a data block's random fence, new on every call


def _same(text: str) -> str:
    return _NONCE.sub("-…>", text)


def _ledger(db: psycopg.Connection, kind: str, key: str) -> dict[str, Any]:
    cur = db.execute(
        "SELECT runs, kept, undone, corrected, failed, trust_fast, trust_slow, n FROM pisa_competence "
        "WHERE kind = %s AND key = %s",
        (kind, key),
    )
    row = cur.fetchone()
    assert row is not None, (kind, key)
    return dict(zip([d.name for d in cur.description or []], row, strict=True))


# --- row 1: the competence ledger ---------------------------------------------------------------------------


def test_a_skill_used_then_undone_shows_undone_and_a_lower_fast_trust(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    service = skills(client)
    wish(client)
    sid = _on(db)
    fresh = _ledger(db, "skill", sid)
    assert (fresh["runs"], fresh["n"], fresh["trust_fast"], fresh["trust_slow"]) == (
        0,
        0,
        0.5,
        0.5,
    )  # a new one
    for _ in range(3):
        assert client.portal.call(service.run, "massima Roma")[1] is not None
        clock.now += timedelta(hours=1)
    kept = _ledger(db, "skill", sid)
    assert (kept["kept"], kept["undone"], kept["n"]) == (3, 0, 3)
    assert kept["trust_fast"] > kept["trust_slow"] > 0.5  # three kept: the fast one moved more
    clock.now += timedelta(minutes=3)
    client.portal.call(service.run, "massima Roma")
    clock.now += timedelta(minutes=1)
    client.portal.call(service.run, "massima Roma", "schedule")  # its schedule ran it, not they
    clock.now += timedelta(minutes=1)
    client.portal.call(service.disable, sid)  # off two minutes after they used it: that use was undone
    row = _ledger(db, "skill", sid)
    assert (row["runs"], row["kept"], row["undone"], row["n"]) == (5, 4, 1, 5)  # the scheduled one stays kept
    assert db.execute("SELECT outcome FROM pisa_skill_uses WHERE how = 'schedule'").fetchone() == ("kept",)
    db.execute("DELETE FROM pisa_skill_uses WHERE how = 'schedule'")
    row = _ledger(db, "skill", sid)
    assert row["trust_fast"] < kept["trust_fast"] and row["trust_slow"] < kept["trust_slow"]
    # one undone, and the fast one fell below the slow one
    db.execute("DELETE FROM pisa_skill_uses WHERE outcome = 'kept'")
    alone = _ledger(db, "skill", sid)
    assert alone["undone"] == 1 and alone["trust_fast"] < alone["trust_slow"]


def test_a_use_he_corrects_is_corrected_and_one_that_shows_nothing_failed(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.personal_evals import PersonalEvals
    from tests.test_skills import _exchange

    service = skills(client)
    wish(client)
    sid = _on(db)
    device = pair(db)[0]
    client.portal.call(service.run, "massima Roma")
    _exchange(db, device, "dimmi la massima a Roma", "Max: 18", clock.now)
    clock.now += timedelta(seconds=30)
    evals = PersonalEvals(rt(client).pool, owner=owner_at(), clock=clock)
    assert client.portal.call(evals.heard, uuid.UUID(str(device)), "no, hai sbagliato", "it-IT")
    assert _ledger(db, "skill", sid)["corrected"] == 1
    db.execute("UPDATE pisa_skills SET recipe = %s WHERE id = %s", (Jsonb({"reads": [], "blocks": []}), sid))
    _, sheet, _ = client.portal.call(service.run, "massima Roma")
    outcomes = [r[0] for r in db.execute("SELECT outcome FROM pisa_skill_uses ORDER BY used_at").fetchall()]
    assert outcomes[0] == "corrected" and (outcomes[-1] == "failed") == (sheet is None)


def test_the_ledger_counts_the_k19_types_as_autonomy_does(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa_tools.competence import ledger

    for n, undone in enumerate((False, False, True)):
        at = clock.now - timedelta(days=1 + n)
        db.execute(
            "INSERT INTO approvals (id, kind, summary, details, payload, status, lang, created_at, "
            "expires_at, executed_at, undone_at, origin) VALUES (gen_random_uuid(), 'calendar_move', 'x', "
            "'[]', '{}', 'done', 'it-IT', %s, %s, %s, %s, 'words')",
            (at, at, at, at + timedelta(minutes=1) if undone else None),
        )
    autonomy = rt(client).autopilot.autonomy

    async def both() -> tuple[Any, Any]:
        async with rt(client).pool.connection() as conn:
            return await autonomy.outcomes(conn), await ledger(conn)

    outcomes, book = client.portal.call(both)
    moves = outcomes["calendar_move"]
    row = book[("type", "calendar_move")]
    assert (row.runs, row.kept, row.undone) == (
        len(moves),
        sum(not u for _, u, _ in moves),
        sum(u for _, u, _ in moves),
    )
    assert (
        book[("type", "email_send")].n == 0 and book[("type", "email_send")].trust_slow == 0.5
    )  # no outcome: prior


def test_outcomes_at_the_same_moment_rank_by_their_row(db) -> None:  # type: ignore[no-untyped-def]
    at = T0
    ids = [str(uuid.UUID(int=n)) for n in (9, 5, 1)]  # inserted from the largest: the scan order is reversed
    for n, rid in enumerate(ids):
        task = db.execute(
            "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) VALUES "
            "(gen_random_uuid(), %s, %s, 'dismissed', 'manual', %s, %s) RETURNING id::text",
            (f"t{n}", f"t{n}", at, at),
        ).fetchone()[0]  # type: ignore[index]
        db.execute(
            "INSERT INTO pisa_decisions (id, at, move, tool, ref, provenance) "
            "VALUES (%s, %s, 'act', 'aging', %s, 'pisa_initiative')",
            (rid, at, task),
        )
    ranked = db.execute(
        "SELECT ref FROM pisa_outcomes_ranked WHERE key = 'task_close' ORDER BY r"
    ).fetchall()  # an aging run closes several at one `at`: a stable order, by the row
    assert [r[0] for r in ranked] == sorted(ids)


# --- row 2: the signals view --------------------------------------------------------------------------------


def test_a_stay_candidate_has_no_plan_change_and_a_replan_one_has_a_value_in_0_1(db) -> None:  # type: ignore[no-untyped-def]
    task, other = str(uuid.uuid4()), str(uuid.uuid4())
    start = (T0 + timedelta(hours=3)).isoformat()
    before = {"items": [
        {"kind": "task", "task_id": task, "title": "Palestra", "start": start},
        {"kind": "task", "task_id": other, "title": "Spesa", "start": (T0 + timedelta(hours=5)).isoformat()},
    ], "unplanned": []}  # fmt: skip
    after = {
        "items": [before["items"][1]],
        "unplanned": [{"task_id": task, "title": "Palestra", "reason": "no time"}],
    }
    for n, plan in enumerate((before, after)):
        db.execute(
            "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
            "'2026-10-29', %s, 'test', 'f', %s)",
            (T0 + timedelta(minutes=n), json.dumps(plan)),
        )

    async def go() -> None:
        async with _attend(db, T0 + timedelta(minutes=2)) as env:
            await _propose(env, "stay", "Sei arrivato in palestra.", source="stay")
            assert (await env.attend.tick()).proposed == 1

    asyncio.run(go())
    rows = dict(db.execute("SELECT source, plan_change FROM attend_candidates").fetchall())
    assert rows["stay"] is None
    assert rows["replan"] == pytest.approx(0.5)  # one of the two items dropped
    signal = db.execute("SELECT source, value FROM pisa_signals WHERE signal = 'plan_change'").fetchall()
    assert signal == [("attend:replan", pytest.approx(0.5))]


def test_plan_change_counts_a_move_a_new_leave_at_and_a_broken_buffer() -> None:
    def item(key: str, start: int, end: int, **more: Any) -> dict[str, Any]:
        return {"kind": "task", "task_id": key, "start": (T0 + timedelta(minutes=start)).isoformat(),
                "end": (T0 + timedelta(minutes=end)).isoformat(), **more}  # fmt: skip

    plan = {"items": [item("a", 0, 60), item("b", 120, 180)]}
    assert signals.plan_change(plan, plan) == 0.0
    assert signals.plan_change(plan, {"items": [item("a", 5, 65), item("b", 120, 180)]}) == 0.0  # noise
    assert signals.plan_change(plan, {"items": [item("a", 30, 90), item("b", 120, 180)]}) == 0.5  # moved
    broken = {"items": [item("a", 0, 130), item("b", 120, 180)]}  # a now ends after b starts
    assert signals.plan_change(plan, broken) == 0.5
    assert signals.plan_change(broken, broken) == 0.0  # broken before too: no news
    trip = {"kind": "travel", "to_place_id": "p", "start": T0.isoformat(), "leave_at": T0.isoformat()}
    assert signals.plan_change(plan, {"items": [*plan["items"], trip]}) == pytest.approx(1 / 3)
    assert signals.plan_change({"items": []}, {"items": []}) == 0.0


def test_a_delivery_while_locked_with_earbuds_out_is_not_seen(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    now = T0 + timedelta(minutes=5)
    locked = stamped(
        None, {"foreground": False, "locked": True, "route": "speaker", "glasses_worn": False}, now
    )
    db.execute(
        "INSERT INTO device_presence (device_id, app, glasses, earbuds, state, updated_at) "
        "VALUES (%s, 'background', 'off', 'disconnected', %s, %s)",
        (device, Jsonb(locked), now),
    )

    async def go() -> None:
        async with _attend(db, now) as env:
            await _propose(env, "a", "Promemoria uno.")
            env.clock.now += timedelta(minutes=3)
            await env.attend.tick()
            db.execute("UPDATE device_presence SET earbuds = 'connected', state = %s",
                       (Jsonb(stamped(locked, {"route": "earbuds"}, env.clock.now)),))  # fmt: skip
            await _propose(env, "b", "Promemoria due.")
            env.clock.now += timedelta(minutes=3)
            await env.attend.tick()

    asyncio.run(go())
    rows = db.execute("SELECT text, seen FROM attend_candidates WHERE notification_id IS NOT NULL").fetchall()
    assert dict(rows) == {"Promemoria uno.": False, "Promemoria due.": True}


def test_seen_reads_the_phone_the_ears_and_the_glasses() -> None:
    now = T0
    assert signals.seen(None, now) is None
    front = Presence("foreground", state=stamped(None, {"locked": False}, now) or {})
    assert signals.seen(front, now, now) is True
    assert signals.seen(Presence("background", earbuds="connected"), now, now) is True
    assert signals.seen(Presence("background", glasses="on"), now, now) is True
    assert signals.seen(Presence("background", glasses="off", earbuds="disconnected"), now, now) is False
    old = stamped(None, {"foreground": True, "locked": False}, now - timedelta(hours=1))
    assert (
        signals.seen(Presence("background", state=old or {}), now, now) is False
    )  # an old "in front": unknown
    # the presence row's own fields age too: earbuds reported an hour ago are unknown, not in their ears
    assert signals.seen(Presence("background", earbuds="connected"), now, now - timedelta(hours=1)) is None
    assert signals.seen(Presence("foreground"), now, now - timedelta(minutes=11)) is None


def test_outcome_error_is_signed_against_the_slow_trust_before_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    for n, undone in enumerate((False, True)):  # kept, then undone (newer)
        at = clock.now - timedelta(days=2 - n)
        db.execute(
            "INSERT INTO approvals (id, kind, summary, details, payload, status, lang, created_at, "
            "expires_at, executed_at, undone_at, origin) VALUES (gen_random_uuid(), 'calendar_create', 'x', "
            "'[]', '{}', 'done', 'it-IT', %s, %s, %s, %s, 'words')",
            (at, at, at, at if undone else None),
        )
    errors = [r[0] for r in db.execute(
        "SELECT value FROM pisa_signals WHERE signal = 'outcome_error' AND source = 'type:calendar_create' "
        "ORDER BY at"
    ).fetchall()]  # fmt: skip
    a = 1 - 2 ** (-1 / 30)
    assert errors == [
        pytest.approx(0.5),
        pytest.approx(0 - (0.5 * (1 - a) + a)),
    ]  # kept vs 0.5; undone vs 0.511


# --- shadow means shadow ------------------------------------------------------------------------------------


def _fill_the_new_tables(db: psycopg.Connection) -> None:
    """Every column and view this lane adds, filled: skill outcomes, a replan's plan_change, a seen bit (the
    skill itself is off: its name in the prompt would be the, not this lane's)."""
    db.execute(
        "INSERT INTO pisa_skills (id, name, name_key, kind, status, made_from, checks, recipe, digest, "
        "enabled_at, created_at, updated_at) VALUES (gen_random_uuid(), 'ombra', 'ombra', 'view', 'off', "
        "'x', '[]', '{\"reads\": [], \"blocks\": []}', 'd', now(), now(), now())"
    )
    for outcome in ("kept", "undone", "corrected", "failed"):
        db.execute(
            "INSERT INTO pisa_skill_uses (skill_id, used_at, how, outcome) "
            "SELECT id, now(), 'app', %s FROM pisa_skills",
            (outcome,),
        )
    db.execute(
        "INSERT INTO attend_candidates (id, key, source, kind, text, lang, urgency, created_at, decided_at, "
        "outcome, plan_change, seen) VALUES (gen_random_uuid(), 'shadow:1', 'replan', 'replan', 'x', "
        "'it-IT', 'breakpoint', now() - interval '3 days', now() - interval '3 days', 'ignored', 0.7, false)"
    )
    assert db.execute("SELECT count(*) FROM pisa_signals").fetchone()[0] >= 5  # type: ignore[index]


def test_shadow_the_decider_s_prompt_is_byte_identical_with_the_new_tables_filled(db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain import attend as brain

    async def prompt() -> str:
        async with _attend(db, T0) as env:
            candidates = [
                A.Candidate(str(uuid.UUID(int=1)), "replan", "replan", "Il piano è cambiato.", "it-IT", T0),
                A.Candidate(str(uuid.UUID(int=2)), "promise", "info", "Ricordati il pane.", "it-IT", T0),
            ]
            moment = await env.attend._moment(candidates, T0)  # pyright: ignore[reportPrivateUsage]
            return _same(brain.prompt(candidates, moment)[0])

    empty = asyncio.run(prompt())
    _fill_the_new_tables(db)
    assert asyncio.run(prompt()) == empty


def test_shadow_the_conversation_s_prompt_is_byte_identical_with_the_new_tables_filled(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    words = "che cosa ho oggi?"
    seen: list[tuple[str, str]] = []
    for filled in (False, True):
        if filled:
            _fill_the_new_tables(db)
        model = Heard()
        fake_models.default = model
        socket_turns(client, db, secrets_key, model, words)  # type: ignore[arg-type]
        history = json.dumps(
            [[str(getattr(p, "content", "")) for p in m.parts] for m in fake_models.seen_messages[-1]]
        )
        seen.append((_same(model.instructions[0]), _same(history)))
        db.execute("TRUNCATE messages, conversations, pisa_decisions, notifications, devices CASCADE")
    assert seen[0] == seen[1]


# --- row 3: the harness lives a week ------------------------------------------------------------------------


def _scenario(name: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == name)


def test_a_scenario_with_two_days_sees_the_night_run_once_between_them(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from tests.test_replay import _play

    runtime = client.app.state.runtime
    nights: list[datetime] = []
    nightly = runtime.learning.nightly

    async def counted() -> dict[str, int]:
        nights.append(clock.now)
        return await nightly()

    runtime.learning.nightly = counted
    try:
        two = _play(client, db, fake_models, secrets_key, clock, http_handler,
                    _scenario("m-05-ieri-ti-ho-detto-dove-ho-lasciato-le-chiavi"))  # fmt: skip
        assert two.nights == 1 and len(two.turns) == 2
        assert [f"{n.astimezone(harness.TZ):%m-%d %H:%M}" for n in nights] == ["11-04 03:30"]
        assert f"{clock.now.astimezone(harness.TZ):%m-%d %H:%M}" == "11-04 08:30"  # the second day's turn
        one = _play(client, db, fake_models, secrets_key, clock, http_handler,
                    _scenario("m-01-cosa-ho-mangiato-martedi"))  # fmt: skip
        assert one.nights == 0 and len(nights) == 1
    finally:
        runtime.learning.nightly = nightly


def test_a_step_runs_the_attend_tick_and_the_night(client) -> None:  # type: ignore[no-untyped-def]
    runtime = client.app.state.runtime
    ran: list[str] = []
    tick, nightly = runtime.attend.tick, runtime.learning.nightly

    async def t() -> None:
        ran.append("attend")

    async def n() -> None:
        ran.append("night")

    runtime.attend.tick, runtime.learning.nightly = t, n
    try:
        assert client.portal.call(harness.live_step, runtime, {"attend": True}) == 0
        assert client.portal.call(harness.live_step, runtime, {"night": True}) == 1
        assert client.portal.call(harness.live_step, None, {"night": True}) == 0  # no runtime: nothing
    finally:
        runtime.attend.tick, runtime.learning.nightly = tick, nightly
    assert ran == ["attend", "night"]


# --- row 4: the memory section ------------------------------------------------------------------------------


def test_the_lure_check_fails_a_reply_that_names_the_lure() -> None:
    scenario = _scenario("m-02-la-cena-con-federico-a-lecce")

    def problems(reply: str) -> list[str]:
        return harness.memory_problems(
            scenario, [harness.TurnLog(scenario["turns"][0]["owner"], reply, False)]
        )

    assert problems("La cena con Federico a Lecce è andata benissimo, avete riso tanto.") == [
        "lure 'Federico' stated as a fact",
        "lure 'Lecce' stated as a fact",
        "no abstention: the records are empty",
    ]
    assert problems("Non ho traccia di una cena con Federico a Lecce.") == []  # named, not as a fact
    assert problems("Non ne ho traccia. Com'è andata a Lecce?") == []  # a question asks, it doesn't say
    assert problems("È andata bene, mi pare.") == ["no abstention: the records are empty"]
    # the review: a clause after ':' or ';' is its own
    assert problems("Non ne ho traccia: la cena con Federico a Lecce è andata benissimo.") == [
        "lure 'Federico' stated as a fact",
        "lure 'Lecce' stated as a fact",
    ]
    assert problems("Non ne ho traccia; Federico era a Lecce.") == [
        "lure 'Federico' stated as a fact",
        "lure 'Lecce' stated as a fact",
    ]
    # "se" and a bare "no" don't excuse it
    assert "lure 'Federico' stated as a fact" in problems("Se ricordo bene, Federico era a Lecce. Non lo so.")
    assert "lure 'Lecce' stated as a fact" in problems("No, Federico era a Lecce. Non ne ho traccia.")
    # a confident tag question states it
    assert "lure 'Lecce' stated as a fact" in problems("Non ne ho traccia. A Lecce è stata ottima, vero?")
    # an echo of their question doesn't
    assert problems("Mi chiedi della cena con Federico a Lecce sabato. Non ne ho traccia.") == []
    # typographic apostrophes
    assert problems("No, non me l\u2019hai detto.") == []
    en = _scenario("m-06-what-did-tomasz-say-about-krakow")
    log = [harness.TurnLog("x", "Tomasz said the Krakow trip was great.", False)]
    assert "lure 'Kraków' stated as a fact" in harness.memory_problems(en, log)
    typographic = [harness.TurnLog("x", "I don\u2019t know: nothing on that trip.", False)]
    assert harness.memory_problems(en, typographic) == []


def test_six_memory_scenarios_count_in_pass_k_since_w3_lane_5() -> None:
    memory = [s for s in SCENARIOS if s.get("group") == "memory" and s["id"].startswith("m-")]
    assert len(memory) == 6 and all(s["memory"]["abstain"] for s in memory)
    assert sum(1 for s in memory if s["memory"]["lures"]) >= 3
    ok = harness.Result("a", [], [], 0, [])
    lured = harness.Result("m-02", ["lure 'Lecce' stated as a fact"], [], 0, [], group="memory")
    text = harness.table([[ok, ok], [lured, lured]])
    assert "pass^2: 1/2" in text  # The memory run is in the total (the gate)
    assert "memory (in pass^k):" in text
    assert "- m-02: NO (lure 'Lecce' stated as a fact)" in text and "memory pass^2: 0/1" in text
    assert "- m-02 run 1: lure 'Lecce' stated as a fact" in text  # a failing memory run is a failing run


# --- row 5: calibration in the replay report ----------------------------------------------------------------


def test_the_report_has_the_ece_and_auroc_fields() -> None:
    def run(
        passed: bool, confidence: float | None, questions: int = 0, why: str = "event x"
    ) -> harness.Result:
        return harness.Result("s", [] if passed else [why], [], questions, [], confidence=confidence)

    empty = harness.calibration([[run(True, None), run(False, None)]])  # an empty ledger: no crash
    assert (
        empty == "calibration: ECE n/a (n=0) · type-2 AUROC n/a (n=0) · over-asking 0 · confident failures 1"
    )
    asked = run(False, None, questions=2, why="questions: 2, at most 0")
    assert "over-asking 1 · confident failures 0" in harness.calibration([[asked]])
    crowded = run(False, None, questions=1, why="questions in one reply: [3], at most 1")
    assert "over-asking 1 · confident failures 0" in harness.calibration([[crowded]])
    rated = [[run(n % 2 == 0, 0.9 if n % 2 == 0 else 0.2)] for n in range(20)]
    line = harness.calibration(rated)
    assert line.startswith("calibration: ECE 0.150 · type-2 AUROC 1.000 · ")
    memory = harness.Result("m", ["lure"], [], 0, [], group="memory", confidence=0.9)
    assert harness.calibration([*rated, [memory]]) == line  # the memory group stays out


def test_ece_and_auroc_are_what_they_say() -> None:
    assert cal.ece([(1.0, True), (0.0, False)]) == 0.0
    assert cal.ece([(0.8, False)] * 4) == pytest.approx(0.8)
    assert cal.auroc([(0.9, True), (0.1, False)]) == 1.0
    assert cal.auroc([(0.1, True), (0.9, False)]) == 0.0
    assert cal.auroc([(0.5, True), (0.5, False)]) == 0.5
    assert cal.auroc([(0.5, True)]) is None
    assert cal.line([(0.5, True)] * 19, 0, 0).startswith(
        "calibration: ECE n/a (n=19) · type-2 AUROC n/a (n=19)"
    )
    assert "AUROC n/a (n=20, one class)" in cal.line([(0.5, True)] * 20, 0, 0)


# --- row 6: sampling agreement, offline ---------------------------------------------------------------------


class _Stop(Exception):
    pass
