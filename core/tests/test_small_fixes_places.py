"""The LOW line (batch 11b): one test per item, each built from its scenario."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from pisa.brain.research_tools import _different_allowed
from pisa.brain.research_turn import ResearchTurn
from pisa.db.pool import open_pool
from pisa.domain import learning as rules
from pisa.domain import profile as profile_rules
from pisa.planner.solver import Block, Problem, TaskIn, solve
from pisa.services.learning import event_buffers
from pisa.services.profile import PAUSE_LOG_KEY, ProfileService
from tests.assistant_env import add_event, rome, set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.places_env import at_location
from tests.test_learning import (
    AMSTERDAM,
    HOME,
    NIGHT1,
    NIGHT2,
    SUNDAY,
    TZ,
    at,
    claims,
    learning,
    owner,  # noqa: F401 (the fixture: three weeks of a life)
    stay,
)
from tests.test_learning_fixes import MONDAY, place


def profile_call(db: psycopg.Connection, clock: Clock, action: Any) -> Any:
    async def go() -> Any:
        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            return await action(ProfileService(pool, owner=owner_at(TZ), clock=clock))
        finally:
            await pool.close()

    return asyncio.run(go())


def lateness_claim(db: psycopg.Connection, state: str = "active") -> str:
    claim_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, support, "
        "against, confidence, state, sensitive, hard, created_at, updated_at, subject_key, value, "
        "confirmed_value) "
        "VALUES (%s, 'routine', 'Per gli incontri con altri arriva di solito circa 10 minuti dopo', '{}', 1, "
        "'{}', 'confirmed', 5, 0, 0.8, %s, false, false, now(), now(), 'lateness:social', %s, %s)",
        (claim_id, state, Jsonb({"minutes": 10, "late": 5, "samples": 5}), Jsonb({"buffer_min": 10})),
    )
    return claim_id


# --- dismissals count toward "changed" only by weight -------------------------------------------------------


def test_three_dismissed_cards_dont_make_a_liked_place_changed() -> None:
    now = datetime(2026, 10, 25, 3, 30, tzinfo=UTC)
    liked = [rules.Ev("S3", "like", "place:x", 1.0, 1, {}, now - timedelta(days=20 - d)) for d in range(5)]
    dismissed = [
        rules.Ev("S11", "like", "place:x", 0.1, -1, {}, now - timedelta(days=3 - d)) for d in range(3)
    ]
    t = rules.tally(liked + dismissed, now)
    state, changed = rules.claim_state(t, previous="active", used_before=True, needs_confirmation=False,
                                       confirmed=False)  # fmt: skip
    assert (state, changed) == ("active", False) and t.contrary_run < rules.CHANGED_RUN  # 3 x 0.1 / 0.3 = 1
    rejected = [rules.Ev("S8", "like", "place:x", 0.3, -1, {}, now - timedelta(days=3 - d)) for d in range(3)]
    t = rules.tally(liked + rejected, now)
    assert rules.claim_state(t, previous="active", used_before=True, needs_confirmation=False,
                             confirmed=False) == ("asking", True)  # fmt: skip


# --- the buffer is given to the solver ----------------------------------------------------------------------


def test_the_owners_buffer_is_kept_free_by_the_solver() -> None:
    """Dinner at minute 120, 10 minutes away, 5 of slack, 10 more for the user's buffer: the trip starts at
    95, so a 100-minute call no longer fits before it (it did when the buffer was applied after the solve,
    overlapping the trip)."""
    problem = Problem(
        horizon=600,
        travel=((0, 10), (10, 0)),
        origin=0,
        buffer=5,
        blocks=(Block("dinner", 120, 240, 1, early=10),),
        tasks=(TaskIn("call", 100, 1),),
    )
    solution = solve(problem, time_limit_s=2)
    (leg,) = [leg for leg in solution.legs if leg.to == "dinner"]
    assert leg.minutes == 10 and leg.buffer == 15
    assert solution.arrivals["dinner"] == 120 and solution.tasks["call"][0] >= 240


# --- the plan fingerprint without retired claims; a buffer turned off ---------------------------------------


def test_a_retired_or_switched_off_buffer_changes_the_plan_and_the_lateness_claim_stays(
    db, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import run

    now = rome(2026, 10, 27, 9)
    claim_id = lateness_claim(db)

    def fingerprint() -> str:
        return run(db, secrets_key, now, lambda p, env: p.fingerprint_for(date(2026, 10, 27)))

    with_buffer = fingerprint()
    db.execute("UPDATE profile_claims SET state = 'retired' WHERE id = %s", (claim_id,))
    assert fingerprint() != with_buffer  # no longer used by the planner: the plan is made again
    db.execute("UPDATE profile_claims SET state = 'active' WHERE id = %s", (claim_id,))
    assert fingerprint() == with_buffer

    profile_call(db, Clock(now), lambda p: p.edit(claim_id, statement=None, scope=None, hard=None,
                                                  buffer_off=True))  # fmt: skip
    assert fingerprint() != with_buffer

    async def buffers() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(dsn(db)) as conn:
            return await event_buffers(conn)

    assert asyncio.run(buffers()) == {}
    row = db.execute("SELECT state, value, confirmed_value FROM profile_claims WHERE id = %s", (claim_id,))
    assert row.fetchone() == ("active", {"minutes": 10, "late": 5, "samples": 5}, {"buffer_off": True})
    week = learning(db, rome(2026, 11, 1, 18, 5), lambda s: s.weekly())
    assert not [i for i in week["items"] if i["kind"] == "buffer"]  # not offered again


# --- the long-walk re-solve: its budget, switched | wet, no long walk with the budget spent -----------------


def test_with_no_budget_left_a_long_walk_still_goes_by_transit(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.places_env import HERE_MILAN
    from tests.test_live_bugs import VEENMOND
    from tests.test_planner import run, with_place

    set_settings(db, {"quiet": {"hours": None}, "places": {"default_mode": "walk"}})
    at_location(db, HERE_MILAN, rome(2026, 10, 20, 8, 29))
    event = add_event(db, "Colazione a Veenmond", rome(2026, 10, 20, 10), minutes=60)
    with_place(db, "calendar_events", event, place(db, "Veenmond", "cafe", VEENMOND))

    async def solve_spent(p: Any, env: Any) -> dict[str, Any]:
        day = await p.gather(env.clock())
        built = await p.solve_day(day, day.events, day.tasks, 2.0, rain_budget_s=0)  # the options' 2 s spent
        return p.assemble(day, built)

    plan = run(db, secrets_key, rome(2026, 10, 20, 8, 30), solve_spent)
    (trip,) = [i for i in plan["items"] if i["kind"] == "travel"]
    assert trip["mode"] == "transit"  # an estimate, no wait: never a 3-hour walk


# --- skip / later: whole words or phrases -------------------------------------------------------------------


def test_skip_and_later_are_whole_words_or_phrases_never_a_stem_in_an_answer() -> None:
    skip = (profile_rules.SKIP_ALONE, profile_rules.SKIP_PHRASES)
    later = (profile_rules.LATER_ALONE, profile_rules.LATER_PHRASES)
    for answer in ("passo spesso dal bar sotto casa", "salto il pranzo quasi sempre", "il prossimo anno"):
        assert not profile_rules.says(answer, *skip), answer
    for answer in ("in pausa pranzo vado in palestra", "dopo il lavoro vado a correre", "stop motion"):
        assert not profile_rules.says(answer, *later), answer
    for words in ("salta", "dai, salta questa", "prossima domanda", "skip this one", "next"):
        assert profile_rules.says(words, *skip), words
    for words in ("basta per oggi", "dopo", "facciamo dopo", "let's stop here", "Later."):
        assert profile_rules.says(words, *later), words


# --- a waiting write expires after 10 minutes; one question per turn ----------------------------------------


def test_a_waiting_write_expires_after_10_minutes_and_one_question_waits_at_a_time(db) -> None:  # type: ignore[no-untyped-def]
    device = uuid.UUID(pair(db)[0])
    clock = Clock(rome(2026, 10, 24, 12))

    def remember(statement: str) -> str:
        return profile_call(db, clock, lambda p: p.remember(
            kind="like", statement=statement, quote=statement.lower(), owner_words=statement.lower(),
            tainted=True, device=device,
        ))  # fmt: skip

    asked = remember("Mi piace il sushi")
    assert asked.startswith("Not done yet") and "Mi piace il sushi" in asked
    assert remember("Mi piace la pizza").startswith("Not asked: Pisa is already waiting")  # one at a time
    clock.now += timedelta(minutes=11)  # the user answers "sì" 11 minutes later: it answers something else
    profile_call(db, clock, lambda p: p.turn_started("sì", device))
    done = profile_call(db, clock, lambda p: p.confirm_pending("sì", device))
    assert done.startswith("Nothing done") and claims(db) == {}
    assert not db.execute("SELECT 1 FROM profile_claims").fetchone()


# --- a new off-limits topic forgets candidates too, and matches categories ----------------------------------


def test_an_off_limits_topic_forgets_candidates_and_places_by_category(db, owner) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    learning(db, NIGHT1, lambda s: s.nightly())
    db.execute("UPDATE places SET category = 'gym' WHERE id = %s", (owner["cafe"],))  # "Caffè Lento": a gym
    found = claims(db)
    cafe = [k for k in found if owner["cafe"] in k]
    assert any(found[k]["state"] == "candidate" for k in cafe)  # its routine: a candidate
    profile_call(db, Clock(NIGHT1), lambda p: p.off_limits("gym"))
    assert not [k for k in claims(db) if owner["cafe"] in k]
    learning(db, NIGHT2, lambda s: s.nightly())
    assert not [k for k in claims(db) if owner["cafe"] in k]  # nor learned again


# --- the pause log compares datetimes -----------------------------------------------------------------------


def test_resuming_doesnt_stretch_a_pause_that_already_ended_in_another_offset(db) -> None:  # type: ignore[no-untyped-def]
    ended = "2026-10-24T13:30:00+02:00"  # the app's offset; 11:30 UTC
    window = {"from": "2026-10-24T12:00:00+02:00", "until": ended}
    db.execute("INSERT INTO app_settings (key, value) VALUES (%s, %s)", (PAUSE_LOG_KEY, Jsonb([window])))
    profile_call(db, Clock(datetime(2026, 10, 24, 11, 45, tzinfo=UTC)), lambda p: p.pause(None))
    log = db.execute("SELECT value FROM app_settings WHERE key = %s", (PAUSE_LOG_KEY,)).fetchone()
    assert log == ([window],)  # as a string "…13:30:00+02:00" > "…11:45:00+00:00": it was moved to 11:45


# --- travelling: home isn't re-learned during a trip --------------------------------------------------------


def test_a_long_trip_never_makes_the_hotel_home(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    start = date(2026, 9, 8)
    for d in range(22):  # 22 nights at home: enough to learn it (21)
        day = start + timedelta(days=d)
        stay(db, device, at(day, 20), at(day + timedelta(days=1), 7), HOME)
    for d in range(22, 48):  # 26 nights in Amsterdam: more than at home in the last 28 days
        day = start + timedelta(days=d)
        stay(db, device, at(day, 21), at(day + timedelta(days=1), 8), AMSTERDAM)
    for d in range(22, 49):
        learning(db, at(start + timedelta(days=d), 7), lambda s: s.update_travelling())
    travelling = learning(db, at(start + timedelta(days=48), 9), lambda s: s.update_travelling())
    assert travelling is not None and travelling["since"] == "2026-09-30"


# --- the weekly "why" counts the last 4 weeks only ----------------------------------------------------------


def test_the_weekly_why_counts_only_the_last_4_weeks(db, owner) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    learning(db, NIGHT1, lambda s: s.nightly())
    learning(db, NIGHT2, lambda s: s.nightly())
    bar = db.execute(
        "SELECT id FROM profile_claims WHERE subject_key = %s", (f"place:{owner['bar']}",)
    ).fetchone()
    assert bar is not None
    for i in range(6):  # six picks in August: not "in the last 4 weeks"
        db.execute(
            "INSERT INTO profile_evidence (id, claim_id, signal, ref, weight, direction, scope, observed_at, "
            "summary, kind, subject_key) VALUES (%s, %s, 'S3', %s, 1, 1, '{}', %s, 'x', 'like', %s)",
            (uuid.uuid4(), bar[0], f"old-{i}", rome(2026, 8, 3 + i, 12), f"place:{owner['bar']}"),
        )
    recent = db.execute(
        "SELECT count(*) FROM profile_evidence WHERE claim_id = %s AND observed_at > %s",
        (bar[0], SUNDAY - timedelta(days=28)),
    ).fetchone()
    assert recent is not None
    changed = next(i for i in learning(db, SUNDAY, lambda s: s.weekly())["items"] if i["kind"] == "changed")
    assert f"Visto {recent[0]} volte nelle ultime 4 settimane" in changed["why"]


# --- forgetting a place's like stops its routine ------------------------------------------------------------


def test_forgetting_a_places_like_stops_its_routine(db, owner) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    learning(db, NIGHT1, lambda s: s.nightly())
    found = claims(db)
    assert [k for k in found if k.startswith(f"routine:{owner['cafe']}")]
    like = db.execute(
        "SELECT id::text FROM profile_claims WHERE subject_key = %s", (f"place:{owner['cafe']}",)
    ).fetchone()
    assert like is not None
    profile_call(db, Clock(NIGHT1), lambda p: p.forget(like[0]))
    assert not [k for k in claims(db) if owner["cafe"] in k]
    learning(db, NIGHT2, lambda s: s.nightly())  # two more Tuesdays and Thursdays there
    assert not [k for k in claims(db) if owner["cafe"] in k]


# --- "went back" is compared with the previous visit --------------------------------------------------------


def test_a_visit_split_by_a_short_gap_is_one_return(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    coord = (45.469, 9.102)
    pid = place(db, "Trattoria Milanese", "restaurant", coord)
    stay(db, device, at(MONDAY, 12), at(MONDAY, 13), coord, pid)  # the first visit
    later = MONDAY + timedelta(days=14)
    stay(db, device, at(later, 12), at(later, 12, 40), coord, pid)  # back two weeks later…
    stay(db, device, at(later, 13), at(later, 13, 40), coord, pid)  # …a call outside, and in again
    learning(db, at(date(2026, 10, 25), 3, 30), lambda s: s.nightly())
    returns = db.execute("SELECT count(*) FROM profile_evidence WHERE signal = 'S1'").fetchone()
    assert returns == (1,)


# --- "something different" against category and cuisine -----------------------------------------------------


def test_something_different_never_has_a_cuisine_the_owner_avoids() -> None:
    research = ResearchTurn(lang="it-IT", prompt_chars=0, avoid=["Non mangia sushi"], different_allowed=True)
    notes: list[str] = []
    assert not _different_allowed(
        research, "Hana", "Un posto diverso dal solito", [], notes, "sushi_restaurant"
    )
    assert notes and "never 'something different'" in notes[0]
    assert _different_allowed(research, "Hana", "Un posto diverso dal solito", [], [], "ramen_restaurant")


# --- extraction's token limit, and a low reasoning effort ---------------------------------------------------
