"""The day planner's solver: OR-Tools CP-SAT. No I/O: the service gives it
minutes.

**Model** (a circuit model; at most ~15 tasks and a handful of events a day):
- Time is in whole minutes from the start of the planning day (0) to its end (`horizon`); tasks go only in
  between. The plan may be made earlier (`start_at` < 0, before `day.start`): trips to the first events may
  then start before the day does.
- Nodes of one circuit (`AddCircuit`): the start point (node 0, free from `start_at`), every fixed event block
  with a place (mandatory) and every task with a place (optional: a self-loop means "not today"). The arc
  i → j means "j comes right after i"; then j may start only after i ends plus the transition (travel +
  `buffer`). A pair the service didn't ask a travel time for (`None`: nobody goes back to the start point,
  nor from a later event to an earlier one) has no arc.
- Each visited node gets a **transition interval** just before it (travel, then the buffer). Transitions,
  tasks and events share one `AddNoOverlap`, so a task without a place (done anywhere) fits in any free gap
  but never in the middle of a trip or a meeting.
- Events are fixed in time, but the arrival may be late (`arrival ≥ start`, even past the end of the day): an
  impossible connection makes a late plan, never an infeasible one. Overlapping events
  are merged into one block beforehand. A *window* event (a festival) takes the user from any arrival
  between its start and its `latest` (its end minus the minimum stay) until its end: late only after
  `latest`, so a stop can go before it and its arrival moves.
- Tasks: optional intervals; opening hours as alternative windows (exactly one if the task is placed);
  a deadline as a hard upper bound on the end.

**Two solves** (lexicographic):
1. The events alone: the least total lateness `L*` (0 whenever the trips fit).
2. Everything, with `Σ late ≤ L*` as a hard constraint: no task is ever worth being late for a meeting.
   Maximise the sum of weights (priority x urgency) minus travel minutes, minus how late in the day the urgent
   tasks start (weighted: the more important first), minus a tie-break on every start (earlier is better) so
   that equal plans don't jump around between re-plans. One worker and a fixed seed: the
   same input gives the same plan.
A time limit (2 s by default) covers both; the best plan found so far is used. When the events alone have no
solution, their trips are added one at a time and a trip that can't fit is left out (`dropped`).

Why a circuit and not one big sequence of NoOverlap with pairwise travel: with transitions that depend on the
order, the circuit states "what comes next" directly, so each leg's travel is exact and errand chaining (two
nearby errands back to back) falls out of minimising travel.
"""

from dataclasses import dataclass, field, replace

from ortools.sat.python import cp_model

# Objective weights. The tie-break stays below one point of anything else (15 tasks x 1,440 minutes = 21,600
# at most), and a task's value above what its own start can cost (30,000 x 1,440 per weight point).
VALUE_SCALE = 100_000_000  # per weight point of a placed task
TRAVEL_COST = 300_000  # per minute of travel on the chosen legs
URGENT_START_COST = 30_000  # per weight point, per minute an urgent task starts after the day's start
TIE_BREAK = 1  # per minute any task starts after the day's start
FIRST_SOLVE_SHARE = 0.25  # of the time limit, for the events alone
# Per minute a window event's arrival is after its start: below a task's value and a travel minute, so lunch
# before the festival wins, and with nothing to do first the user arrives at the start.
WINDOW_ARRIVAL_COST = 1_000
# Per minute a soft event moves: below a minute of travel, and a whole day's move (1,440 minutes) is worth
# less than one weight point of a placed task, so it moves only for a stop, and as little as it can.
SOFT_MOVE_COST = 50_000
LATE_OVER_MOVES = 100_000  # the events-only pass: a minute late outweighs any soft event's moves of a day


@dataclass(frozen=True)
class Block:
    """Fixed calendar time: one event, or overlapping events merged."""

    key: str
    start: int
    end: int
    location: int | None  # index into the travel matrix; None = no place (a call, or unknown)
    early: int = 0  # more slack before it after a trip: the user's buffer for this kind of event
    # A window event (a festival): arrive any time from `start` to `latest`; only after that is late.
    latest: int | None = None
    # A soft event (one the user made in Pisa): it may start anywhere in this range (its day), keeping
    # its length, when a stop needs the time; each minute moved costs SOFT_MOVE_COST.
    shift: tuple[int, int] | None = None


@dataclass(frozen=True)
class TaskIn:
    key: str
    duration: int
    weight: int  # priority weight x urgency
    location: int | None = None
    windows: tuple[tuple[int, int], ...] = ()  # when the place is open; () = any time
    deadline: int | None = None  # the end may not be later
    urgent: bool = False  # overdue or due today: earlier is better


@dataclass(frozen=True)
class Problem:
    horizon: int
    travel: tuple[tuple[int | None, ...], ...]  # minutes between locations (matrix index); None = not asked
    origin: int | None  # where the plan starts; None = unknown (the first leg costs nothing)
    buffer: int
    blocks: tuple[Block, ...] = ()
    tasks: tuple[TaskIn, ...] = ()
    start_at: int = 0  # when the start point is left: now, before the day's start when planning early


@dataclass(frozen=True)
class Leg:
    """The transition into `to` (a task or block key): `minutes` of travel from `from_location` (None = the
    start point), then `buffer` minutes of slack. A leg with no travel is slack alone."""

    to: str
    from_location: int | None
    to_location: int
    minutes: int  # travel only
    buffer: int


@dataclass
class Solution:
    status: str  # optimal | feasible | infeasible | unknown
    tasks: dict[str, tuple[int, int]] = field(default_factory=dict)  # key → (start, end), placed tasks
    arrivals: dict[str, int] = field(default_factory=dict)  # block key → arrival minute (late if > start)
    legs: list[Leg] = field(default_factory=list)  # in time order
    order: list[str] = field(default_factory=list)  # visited circuit nodes after the start, in time order
    late: int = 0  # total minutes late at events
    dropped: list[str] = field(default_factory=list)  # blocks whose trip couldn't fit: shown without it
    shifts: dict[str, int] = field(default_factory=dict)  # soft block key → its new start, when it moved
    tasks_solved: bool = True  # False: the second solve found nothing in time, so no task is placed


# FACT (row 31): the plan is the solver's; the model decides when to replan and how to explain it.
def solve(problem: Problem, *, time_limit_s: float = 2.0) -> Solution:
    first_limit = time_limit_s * FIRST_SOLVE_SHARE
    events_only = replace(problem, tasks=())
    first = _Model(events_only).run(first_limit, minimise_late=True)
    dropped: list[str] = []
    if first.status not in ("optimal", "feasible"):
        events_only, dropped, first = _without_impossible_trips(events_only, first_limit)
        problem = replace(problem, blocks=events_only.blocks)
        if first.status not in ("optimal", "feasible"):
            return first
    second = _Model(problem).run(time_limit_s - first_limit, late_cap=first.late, hint=first)
    if second.status not in ("optimal", "feasible"):
        second = first  # the events alone are still a plan
        second.tasks_solved = not problem.tasks
    second.dropped = dropped
    return second


def _without_impossible_trips(problem: Problem, limit_s: float) -> tuple[Problem, list[str], Solution]:
    """Add the blocks' trips one at a time, in time order; a trip that makes the day impossible is left
    out."""
    blocks = [replace(b, location=None) for b in problem.blocks]
    dropped: list[str] = []
    for i, block in enumerate(problem.blocks):
        if block.location is None:
            continue
        trial = blocks.copy()
        trial[i] = block
        if _Model(replace(problem, blocks=tuple(trial))).run(limit_s, minimise_late=True).status in (
            "optimal",
            "feasible",
        ):
            blocks = trial
        else:
            dropped.append(block.key)
    final = replace(problem, blocks=tuple(blocks))
    return final, dropped, _Model(final).run(limit_s, minimise_late=True)


class _Model:
    def __init__(self, problem: Problem) -> None:
        self.p = problem
        m = self.m = cp_model.CpModel()
        p = problem
        start_at = min(0, p.start_at)
        horizon = p.horizon
        known = [t for row in p.travel for t in row if t is not None]
        n_place_blocks = sum(1 for b in p.blocks if b.location is not None)
        # Late arrivals may run past the day's end: enough room for every trip in a row.
        latest = max([horizon, *(b.end for b in p.blocks)]) + (n_place_blocks + 1) * (
            max(known, default=0) + p.buffer + max((b.early for b in p.blocks), default=0)
        )
        intervals: list[cp_model.IntervalVar] = []
        self.late_terms: list[cp_model.LinearExprT] = []
        self.value: list[cp_model.LinearExprT] = []

        # Circuit nodes: 0 = the start point, then blocks with a place, then tasks with a place.
        self.nodes: list[tuple[str, int | None]] = [("", p.origin)]
        self.starts: dict[int, cp_model.IntVar] = {}
        ends: dict[int, cp_model.LinearExprT] = {0: start_at}
        present: dict[int, cp_model.IntVar | bool] = {0: True}
        self.is_block: dict[int, bool] = {0: False}
        self.early: dict[int, int] = {}
        self.arrivals: dict[str, cp_model.IntVar] = {}

        self.shifts: dict[str, cp_model.IntVar] = {}
        self.moves: list[cp_model.IntVar] = []  # minutes each soft event moved
        for block in p.blocks:
            start: cp_model.LinearExprT = max(
                start_at, block.start
            )  # in progress: not late for the part gone
            if block.end <= max(start_at, block.start):
                continue
            end: cp_model.LinearExprT = block.end
            lowest = max(start_at, block.start)
            if block.shift is not None and block.start >= start_at:  # a soft event may move
                length = block.end - block.start
                lo, hi = max(start_at, block.shift[0]), max(start_at, block.shift[1])
                moved = m.new_int_var(min(lo, block.start), max(hi, block.start), f"soft_{block.key}")
                gap = m.new_int_var(0, latest, f"moved_{block.key}")
                m.add_abs_equality(gap, moved - block.start)
                self.value.append(-SOFT_MOVE_COST * gap)
                self.moves.append(gap)
                self.shifts[block.key] = moved
                start, end, lowest = moved, moved + length, min(lo, block.start)
            if block.location is None:  # no place: no travel to be late from
                size = block.end - max(start_at, block.start)
                intervals.append(m.new_interval_var(start, size, end, f"event_{block.key}"))
                continue
            arrival = m.new_int_var(lowest, latest, f"arrive_{block.key}")
            leave = m.new_int_var(lowest, latest, f"leave_{block.key}")
            m.add_max_equality(leave, [arrival, end])
            m.add(arrival >= start)
            if block.latest is None:
                self.late_terms.append(arrival - start)
            else:  # a window: late only after its latest arrival; earlier is better when nothing else gains
                late = m.new_int_var(0, latest, f"late_{block.key}")
                m.add(late >= arrival - max(lowest, block.latest))
                self.late_terms.append(late)
                self.value.append(-WINDOW_ARRIVAL_COST * (arrival - start))
            stay = m.new_int_var(0, block.end - lowest, f"stay_{block.key}")
            m.add(stay == leave - arrival)
            intervals.append(m.new_interval_var(arrival, stay, leave, f"event_{block.key}"))
            n = len(self.nodes)
            self.nodes.append((block.key, block.location))
            self.starts[n], ends[n], present[n], self.is_block[n] = arrival, leave, True, True
            self.early[n] = block.early
            self.arrivals[block.key] = arrival

        self.task_vars: dict[str, tuple[cp_model.IntVar, cp_model.IntVar, cp_model.IntVar]] = {}
        for task in p.tasks:
            if task.duration > horizon:
                continue
            here = m.new_bool_var(f"do_{task.key}")
            start = m.new_int_var(0, horizon - task.duration, f"start_{task.key}")
            end = m.new_int_var(task.duration, horizon, f"end_{task.key}")
            intervals.append(m.new_optional_interval_var(start, task.duration, end, here, f"task_{task.key}"))
            if task.windows:
                choices = []
                for k, (open_, close) in enumerate(task.windows):
                    if close - open_ < task.duration:
                        continue
                    pick = m.new_bool_var(f"win_{task.key}_{k}")
                    m.add(start >= open_).only_enforce_if(pick)
                    m.add(end <= close).only_enforce_if(pick)
                    choices.append(pick)
                if choices:
                    m.add(sum(choices) == here)
                else:
                    m.add(here == 0)  # no opening long enough
            if task.deadline is not None:
                m.add(end <= task.deadline).only_enforce_if(here)
            self.value.append(VALUE_SCALE * task.weight * here)
            placed_start = m.new_int_var(0, horizon, f"placed_start_{task.key}")
            m.add(placed_start == start).only_enforce_if(here)
            m.add(placed_start == 0).only_enforce_if(here.Not())
            self.value.append(-TIE_BREAK * placed_start)
            if task.urgent:
                self.value.append(-URGENT_START_COST * task.weight * placed_start)
            self.task_vars[task.key] = (start, end, here)
            if task.location is not None:
                n = len(self.nodes)
                self.nodes.append((task.key, task.location))
                self.starts[n], ends[n], present[n], self.is_block[n] = start, end, here, False

        # The circuit and the transition into each visited node.
        arcs: list[tuple[int, int, cp_model.IntVar | bool]] = []
        self.arc_vars: dict[tuple[int, int], cp_model.IntVar] = {}
        transitions: dict[int, cp_model.IntVar] = {}
        for j in range(1, len(self.nodes)):
            transitions[j] = m.new_int_var(0, latest - start_at, f"trans_{self.nodes[j][0]}")
        # The start point stays out of the circuit (a self-loop) only when nothing with a place is visited.
        alone = m.new_bool_var("start_alone")
        arcs.append((0, 0, alone))
        for j in range(1, len(self.nodes)):
            if self.is_block[j]:
                m.add(alone == 0)
            else:
                m.add_implication(present[j], alone.Not())  # type: ignore[arg-type]
        for i in range(len(self.nodes)):
            for j in range(len(self.nodes)):
                if i == j:
                    if i != 0 and not self.is_block[i]:
                        arcs.append((i, i, present[i].Not()))  # type: ignore[union-attr]
                    continue
                if j == 0:  # back to the start: the end of the day, nothing to respect
                    lit = m.new_bool_var(f"arc_{i}_0")
                    arcs.append((i, 0, lit))
                    self.arc_vars[(i, 0)] = lit
                    continue
                travel = self.travel(i, j)
                if travel is None:
                    continue  # a pair nobody uses: no arc
                lit = m.new_bool_var(f"arc_{i}_{j}")
                arcs.append((i, j, lit))
                self.arc_vars[(i, j)] = lit
                buffer = self.buffer(travel, i, j)
                m.add(self.starts[j] >= ends[i] + travel + buffer).only_enforce_if(lit)
                m.add(transitions[j] == travel + buffer).only_enforce_if(lit)
                self.value.append(-TRAVEL_COST * travel * lit)
        if len(self.nodes) > 1:  # nothing with a place: no route to choose
            m.add_circuit(arcs)
        for j in range(1, len(self.nodes)):
            go = m.new_int_var(start_at, latest, f"go_{self.nodes[j][0]}")
            m.add(go == self.starts[j] - transitions[j])
            if self.is_block[j]:  # a block's trip happens before its (possibly late) arrival
                intervals.append(
                    m.new_interval_var(go, transitions[j], self.starts[j], f"travel_{self.nodes[j][0]}")
                )
                continue
            m.add(transitions[j] == 0).only_enforce_if(present[j].Not())  # type: ignore[union-attr]
            intervals.append(
                m.new_optional_interval_var(go, transitions[j], self.starts[j], present[j], f"travel_{j}")  # type: ignore[arg-type]
            )
        m.add_no_overlap(intervals)

    def travel(self, i: int, j: int) -> int | None:
        """Minutes between two nodes; an unknown start point costs nothing (the first leg is left out)."""
        a, b = self.nodes[i][1], self.nodes[j][1]
        if a is None or b is None or a == b:
            return 0
        return self.p.travel[a][b]

    def buffer(self, travel: int, i: int, j: int) -> int:
        """Slack after a trip and around fixed events; none from the start point when already there. A trip to
        an event the user leaves earlier for (their buffer, `early`) has that much more: the solver keeps it
        free, so nothing is planned in it."""
        if travel > 0:
            return self.p.buffer + self.early.get(j, 0)
        if i != 0 and (self.is_block[i] or self.is_block[j]):
            return self.p.buffer
        return 0

    def run(
        self,
        time_limit_s: float,
        *,
        minimise_late: bool = False,
        late_cap: int | None = None,
        hint: Solution | None = None,
    ) -> Solution:
        m = self.m
        total_late = sum(self.late_terms)
        if minimise_late:
            # Lateness first; a soft event moves only to avoid it, and as little as it can.
            m.minimize(
                cp_model.LinearExpr.weighted_sum(
                    [total_late, *self.moves], [LATE_OVER_MOVES] + [1] * len(self.moves)
                )
            )
        else:
            if late_cap is not None and self.late_terms:
                m.add(total_late <= late_cap)
            m.maximize(sum(self.value))
        if hint is not None:
            for key, minute in hint.arrivals.items():
                if key in self.arrivals:
                    m.add_hint(self.arrivals[key], minute)
            for _, _, here in self.task_vars.values():
                m.add_hint(here, False)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.05, time_limit_s)
        solver.parameters.num_workers = 1  # deterministic
        solver.parameters.random_seed = 7
        code = solver.solve(m)
        if code == cp_model.OPTIMAL:
            status = "optimal"
        elif code == cp_model.FEASIBLE:
            status = "feasible"
        elif code == cp_model.INFEASIBLE:
            status = "infeasible"
        else:
            status = "unknown"
        out = Solution(status)
        if status not in ("optimal", "feasible"):
            return out
        out.late = int(solver.value(total_late)) if self.late_terms else 0
        for key, (start, end, here) in self.task_vars.items():
            if solver.value(here):
                out.tasks[key] = (solver.value(start), solver.value(end))
        for key, arrival in self.arrivals.items():
            out.arrivals[key] = solver.value(arrival)
        for key, moved in self.shifts.items():
            block = next(b for b in self.p.blocks if b.key == key)
            if solver.value(moved) != block.start:
                out.shifts[key] = solver.value(moved)
        # Walk the circuit from the start point to get the order and the legs.
        successor = {i: j for (i, j), lit in self.arc_vars.items() if solver.value(lit)}
        current, seen = 0, {0}
        while current in successor and successor[current] != 0 and successor[current] not in seen:
            nxt = successor[current]
            seen.add(nxt)
            key, location = self.nodes[nxt]
            out.order.append(key)
            travel = self.travel(current, nxt) or 0
            buffer = self.buffer(travel, current, nxt)
            if (travel > 0 or buffer > 0) and location is not None:
                out.legs.append(Leg(key, self.nodes[current][1], location, travel, buffer))
            current = nxt
        return out
