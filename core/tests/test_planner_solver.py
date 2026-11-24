"""The planner's solver on fixed scenarios (no I/O): the squeezed hour with two errands chained before a
closing, a task that doesn't fit, overlapping deadlines, a closed day, no location, impossible connections
between meetings, place-less tasks never in a trip, and the time limit."""

from itertools import pairwise
from typing import Any

import pytest
from ortools.sat.python import cp_model

from pisa.planner.solver import Block, Problem, Solution, TaskIn, solve

# Locations: 0 = where the user is, 1 = the office, 2 = the post office, 3 = a pharmacy next to it.
TRAVEL = (
    (0, 20, 15, 16),
    (20, 0, 5, 6),
    (15, 5, 0, 2),
    (16, 6, 2, 0),
)
# Minutes from 08:00.
H = 13 * 60  # 08:00-21:00


def spans(solution: Solution, problem: Problem) -> list[tuple[int, int, str]]:
    """Everything that takes time, to check nothing overlaps: tasks, events (from the arrival) and trips."""
    out = [(s, e, k) for k, (s, e) in solution.tasks.items()]
    out += [(solution.arrivals[b.key], b.end, b.key) for b in problem.blocks if b.location is not None]
    out += [(b.start, b.end, b.key) for b in problem.blocks if b.location is None]
    starts = {**solution.arrivals, **{k: v[0] for k, v in solution.tasks.items()}}
    out += [
        (starts[leg.to] - leg.buffer - leg.minutes, starts[leg.to] - leg.buffer, f"to {leg.to}")
        for leg in solution.legs
    ]
    return sorted(out)


def assert_no_overlap(solution: Solution, problem: Problem) -> None:
    items = spans(solution, problem)
    for (s1, e1, k1), (s2, e2, k2) in pairwise(items):
        assert e1 <= s2, f"{k1} [{s1},{e1}) overlaps {k2} [{s2},{e2})"


def test_the_squeezed_hour() -> None:
    """The only free hour is between two meetings at the office (after the second, the post office has
    closed); the pharmacy is 2 minutes from the post office: both errands go in the hour, back to back."""
    problem = Problem(
        horizon=H,
        travel=TRAVEL,
        origin=1,  # at the office
        buffer=5,
        blocks=(Block("meeting-1", 0, 120, 1), Block("meeting-2", 180, 320, 1)),  # 08-10, 11-13:20
        tasks=(
            TaskIn("post", 10, 6, location=2, windows=((20, 330),), urgent=True),  # 08:20-13:30, due today
            TaskIn("pharmacy", 10, 2, location=3),
        ),
    )
    solution = solve(problem)
    assert solution.status == "optimal"
    post, pharmacy = solution.tasks["post"], solution.tasks["pharmacy"]
    assert 120 <= min(post[0], pharmacy[0]) and max(post[1], pharmacy[1]) <= 180  # inside the free hour
    assert post[1] <= 330  # before the post office closes
    order = solution.order
    assert abs(order.index("post") - order.index("pharmacy")) == 1  # chained
    assert solution.arrivals == {"meeting-1": 0, "meeting-2": 180}  # never late
    assert_no_overlap(solution, problem)


def test_a_task_that_does_not_fit_is_left_out() -> None:
    problem = Problem(H, TRAVEL, 0, 5, (), (TaskIn("too-long", H + 1, 3), TaskIn("small", 30, 1)))
    solution = solve(problem)
    assert set(solution.tasks) == {"small"}


def test_overlapping_deadlines() -> None:
    # Three 60-minute tasks due by 10:00 from 08:00: two fit; the one left out is the least important.
    tasks = (
        TaskIn("a", 60, 6, deadline=120, urgent=True),
        TaskIn("b", 60, 4, deadline=120, urgent=True),
        TaskIn("c", 60, 2, deadline=120, urgent=True),
    )
    solution = solve(Problem(H, TRAVEL, None, 5, (), tasks))
    assert set(solution.tasks) == {"a", "b"}
    assert all(end <= 120 for _, end in solution.tasks.values())
    assert solution.tasks["a"][0] < solution.tasks["b"][0]  # the more important first


def test_a_closed_day() -> None:
    closed = TaskIn("post", 10, 6, location=2, windows=((0, 0),))  # no opening long enough
    open_ = TaskIn("pharmacy", 10, 1, location=3, windows=((60, 120),))
    solution = solve(Problem(H, TRAVEL, 0, 5, (), (closed, open_)))
    assert "post" not in solution.tasks and 60 <= solution.tasks["pharmacy"][0] <= 110


def test_no_location_leaves_out_the_first_leg() -> None:
    solution = solve(Problem(H, TRAVEL, None, 0, (), (TaskIn("post", 10, 6, location=2, urgent=True),)))
    assert solution.tasks["post"] == (0, 10) and solution.legs == []  # no trip from an unknown start


def test_an_impossible_connection_makes_a_late_plan_not_none() -> None:
    # Two meetings back to back at places 20 min apart: the plan arrives late at the second.
    problem = Problem(H, TRAVEL, 0, 0, (Block("a", 60, 120, 1), Block("b", 120, 180, 0)), ())
    solution = solve(problem)
    assert solution.status in ("optimal", "feasible") and solution.arrivals["b"] == 140
    assert_no_overlap(solution, problem)


def test_overlapping_meetings_are_one_block_upstream() -> None:
    # The service merges overlapping events; a block without a place is fixed time, never travelled to.
    problem = Problem(H, TRAVEL, 0, 5, (Block("call", 0, 60, None),), (TaskIn("t", 30, 1),))
    solution = solve(problem)
    assert solution.tasks["t"][0] >= 60


def test_place_less_tasks_never_go_in_a_trip() -> None:
    problem = Problem(
        H,
        TRAVEL,
        0,
        0,
        (Block("meeting", 30, 90, 1),),  # 20 minutes away: leave at 08:10
        (TaskIn("call", 25, 3, urgent=True),),  # done anywhere, urgent: as early as possible
    )
    solution = solve(problem)
    call = solution.tasks["call"]
    assert call[1] <= 10 or call[0] >= 90  # before leaving, or after the meeting: never during the trip
    assert_no_overlap(solution, problem)


def test_the_time_limit_holds_on_a_full_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit is CP-SAT's, in wall seconds, shared by the two solves. Checked by what the code hands
    CP-SAT and why each solve stopped, not by the wall: on a loaded machine the wall ran past any margin, and
    the second solve found no task in its share. Here CP-SAT keeps each share on its deterministic clock (as
    many seconds of its own work), so what it finds in the limit is the same on any machine."""
    shares: list[float] = []
    stopped: list[str] = []
    real = cp_model.CpSolver.solve

    def on_work_clock(self: cp_model.CpSolver, model: cp_model.CpModel, *args: Any, **kwargs: Any) -> Any:
        shares.append(self.parameters.max_time_in_seconds)
        self.parameters.max_deterministic_time = self.parameters.max_time_in_seconds
        self.parameters.max_time_in_seconds = 600.0  # bounds a hang only
        code = real(self, model, *args, **kwargs)
        stopped.append(self.status_name(code))
        return code

    monkeypatch.setattr(cp_model.CpSolver, "solve", on_work_clock)
    travel = tuple(tuple(0 if i == j else 5 + (i * 7 + j * 3) % 25 for j in range(16)) for i in range(16))
    tasks = tuple(
        TaskIn(f"t{i}", 20 + (i * 13) % 50, 1 + i % 9, location=1 + i, windows=((0, 300), (360, 700)))
        for i in range(15)
    )
    blocks = (Block("m1", 120, 180, 0), Block("m2", 400, 460, 3))
    solution = solve(Problem(H, travel, 0, 10, blocks, tasks), time_limit_s=1.0)
    assert shares == [0.25, 0.75] and sum(shares) == 1.0  # the events first, then the tasks: the limit in all
    assert stopped == ["OPTIMAL", "FEASIBLE"]  # the full day's tasks were cut by their share of the limit
    assert solution.status in ("optimal", "feasible") and solution.tasks  # and the best plan found is kept


# --- review fixes ---------------------------------------------------------------------------------------


def test_no_task_is_worth_being_late_for_a_meeting() -> None:
    """H1: an errand worth the most (weight 9) fits only between two meetings if the user arrives 20 minutes
    late at the second one. The meetings come first: the errand stays out."""
    problem = Problem(
        horizon=H,
        travel=TRAVEL,
        origin=1,
        buffer=5,
        blocks=(Block("a", 60, 120, 1), Block("b", 150, 200, 0)),  # office, then 20 min away
        tasks=(TaskIn("post", 20, 9, location=2, windows=((120, 150),), urgent=True),),
    )
    solution = solve(problem)
    assert solution.late == 0 and solution.arrivals["b"] == 150
    assert "post" not in solution.tasks


def test_a_dense_day_is_as_late_as_the_meetings_alone() -> None:
    """H1: back-to-back meetings 20 minutes apart can't all be on time; tasks never add lateness."""
    blocks = (Block("a", 0, 60, 1), Block("b", 60, 120, 0), Block("c", 120, 180, 1))
    alone = solve(Problem(H, TRAVEL, 1, 5, blocks, ()))
    tasks = tuple(TaskIn(f"t{i}", 15, 9, location=2 + i % 2, urgent=True) for i in range(6))
    busy = solve(Problem(H, TRAVEL, 1, 5, blocks, tasks))
    assert alone.late > 0 and busy.late == alone.late


def test_an_event_out_of_reach_before_the_day_ends_still_gets_its_trip() -> None:
    """H2: planning at 20:00 (the day ends at 21:00), dinner at 20:50 is 100 minutes away: the plan arrives
    late (past the end of the day) instead of having no solution."""
    travel = ((0, 100), (100, 0))
    problem = Problem(60, travel, 0, 10, (Block("dinner", 50, 120, 1),), (TaskIn("call", 30, 2),))
    solution = solve(problem)
    assert solution.status in ("optimal", "feasible") and solution.arrivals["dinner"] == 110
    assert [(leg.to, leg.minutes, leg.buffer) for leg in solution.legs] == [("dinner", 100, 10)]
    assert solution.tasks == {}  # the call can't be done during the trip
    assert solution.dropped == []


def test_the_first_trip_may_start_before_the_day() -> None:
    """H3: planned at 07:30 (the day starts at 08:00), a meeting at 08:15 is 27 minutes away with a 10-minute
    buffer: leave at 07:38 and be on time."""
    travel = ((0, 27), (27, 0))
    problem = Problem(H, travel, 0, 10, (Block("meeting", 15, 75, 1),), (), start_at=-30)
    solution = solve(problem)
    assert solution.arrivals == {"meeting": 15} and solution.late == 0
    (leg,) = solution.legs
    assert 15 - leg.buffer - leg.minutes == -22  # 07:38


def test_the_same_input_gives_the_same_plan() -> None:
    """M7: place-less tasks of equal value don't move between two solves."""
    tasks = (*(TaskIn(f"call-{i}", 20, 2) for i in range(5)), TaskIn("post", 10, 6, location=2))
    problem = Problem(H, TRAVEL, 0, 5, (Block("m", 120, 180, 1),), tasks)
    first = solve(problem)
    for _ in range(3):
        again = solve(problem)
        assert again.tasks == first.tasks and again.order == first.order
    starts = sorted(start for start, _ in first.tasks.values())
    assert starts[0] == 0  # the tie-break packs the day from its start


def test_pairs_nobody_uses_have_no_travel_time() -> None:
    """M6: the service doesn't ask for trips back to the start point, nor from a later event to an earlier
    one."""
    travel = ((0, 20, 15), (None, 0, 5), (None, None, 0))
    problem = Problem(H, travel, 0, 5, (Block("a", 60, 120, 1), Block("b", 180, 240, 2)), ())
    solution = solve(problem)
    assert solution.late == 0 and solution.order == ["a", "b"]


def test_a_task_right_after_a_meeting_at_the_same_place_keeps_the_buffer() -> None:
    """L2: no trip, but the buffer after the meeting is still taken: a leg of slack only."""
    problem = Problem(
        H, TRAVEL, 1, 10, (Block("m", 0, 60, 1),), (TaskIn("t", 30, 6, location=1, urgent=True),)
    )
    solution = solve(problem)
    assert solution.tasks["t"] == (70, 100)
    assert [(leg.to, leg.minutes, leg.buffer) for leg in solution.legs] == [("t", 0, 10)]  # none from here


def test_one_errand_that_cannot_fit_leaves_a_valid_plan() -> None:
    """The start point stays out of the circuit when nothing with a place is visited: a lone errand that
    can't be done (its opening is during a call) is simply left out, not an impossible day."""
    problem = Problem(
        720, ((0,),), None, 10, (Block("call", -30, 300, None),), (TaskIn("post", 10, 4, 0, ((0, 275),)),)
    )
    solution = solve(problem)
    assert solution.status == "optimal" and solution.tasks_solved and solution.tasks == {}
