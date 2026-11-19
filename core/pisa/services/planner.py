"""The day plan: Pisa places the open tasks into the free time of the day.

1. **Inputs**: the timed events from now to the end of the planning day (fixed), the open tasks due today or
   overdue (the most important first), then other open tasks by priority (at most 15 in all), each task's
   place, duration (`default_task_min` when unset), the latest location (at most 15 minutes old), opening
   hours from the place details (cached 24 h).
2. **Travel**: one matrix per plan through the TravelTimes port by `default_mode`, only for the pairs a plan
   can use (never back to the start point, nor from a later event to an earlier one). Cells between tasks and
   events are reused for the day; the row from where the user is (rounded to ~100 m) for an hour around its
   departure slot. So a periodic re-plan asks for the origin row at most. When no provider
   answers (none configured, the cap, errors, the time budget), the minutes are estimated from the
   straight-line distance (plus a wait for public transport), and the plan says so in `sources.skipped`.
3. **Solve**: pisa.planner.solver (CP-SAT, 2 s; events first, so no task makes the user late).
4. **Explain**: one deterministic sentence per task (pisa.planner.explain), no model.
5. **Store** in `plans`; GET /v1/plan serves the latest for the day.

The whole plan has a 10 s budget (opening hours get 5 s of it); one plan is made at a time (lock PLAN), and an
explicit ask within 15 s of a plan made from the same tasks, events, settings and place returns that plan.

**Re-plan** (`tick`, every minute from the worker): when there is no plan yet for the planning day, when a
task, an event or the settings changed (a fingerprint), when the user moved more than 1 km from the plan's
start, and every 30 minutes, between `planner.day.start` and `.end`; never more than once a minute. Before
`day.start` it wakes early enough for the first event's trip: its estimated leave time minus the lead, minus
one re-plan interval. `POST /v1/plan` re-plans at once.
"""

import asyncio
import hashlib
import math
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal, LiteralString
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import actuals, event_kinds
from pisa.domain import settings as rules
from pisa.domain.errors import PisaError
from pisa.domain.home import Home
from pisa.domain.learning import event_kind
from pisa.domain.lexicon import travel
from pisa.domain.owner import Owner
from pisa.domain.places import WALK_MAX_MIN, Mode, distance_m, open_windows
from pisa.domain.tasks import Task, task_place_id, todo_json
from pisa.domain.tasks import todo as pool_todo
from pisa.domain.weather import RAIN_LEG_MIN
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.planner.explain import TaskFacts, Urgency, unplanned_reason, why
from pisa.planner.options import Change
from pisa.planner.solver import Block, Problem, Solution, TaskIn, solve
from pisa.ports.places import Coord
from pisa.services import home as home_store
from pisa.services import plan_store
from pisa.services.actuals import load_bands
from pisa.services.learning import event_buffers
from pisa.services.places import PlacesService
from pisa.services.tasks import TasksService
from pisa.services.trips import TripsService
from pisa.services.weather import WeatherService

TIME_LIMIT_S = 2.0
PLAN_BUDGET_S = 10.0  # the whole plan: opening hours, travel times, the solve
DETAILS_BUDGET_S = 5.0  # opening hours for the plan's places; slower ones count as unknown (any time)
REPLAN_EVERY = timedelta(minutes=30)
REPLAN_GUARD = timedelta(minutes=1)
STALE_REASON = "tasks changed since it was made"  # `current`: a plan read after its tasks changed
COALESCE = timedelta(seconds=15)  # an explicit ask this soon after the same plan gets that plan
COALESCE_M = 100  # ... made from about the same place (the origin key's precision)
MOVED_M = 1_000
SAME_PLACE_M = 30  # positions closer than this are one, with no trip between them
ORIGIN_DECIMALS = 3  # the start point's cache key: ~100 m, so GPS jitter doesn't miss the cache
ORIGIN_REUSE = timedelta(hours=1)  # the origin row's cells serve departures this close to theirs
SLOT = timedelta(minutes=15)
FREE_MIN = 15  # free time shorter than this is not listed as an item
# The straight-line estimate when no travel provider answers: km/h, a detour factor for real streets, and a
# wait for public transport.
SPEED_KMH: dict[str, float] = {"walk": 4.5, "bike": 14.0, "transit": 18.0, "drive": 25.0}
DETOUR = 1.3
TRANSIT_WAIT_MIN = 5
ESTIMATE = "estimate"  # the provider of an estimated cell
BASE_PREFIX = "base:"  # the key of the day's end at the stay (a hotel): a trip back, no item
PRIORITY_WEIGHT = {1: 3, 2: 2, 3: 1}
URGENCY_WEIGHT: dict[str, int] = {"overdue": 3, "today": 2, "later": 1}
# An event Pisa created for the user (an approved calendar_create keeps Pisa's own event id): a soft event.
MADE_BY_PISA: LiteralString = (
    "EXISTS (SELECT 1 FROM approvals a WHERE a.kind = 'calendar_create' AND a.status = 'done' "
    "AND a.payload->'body'->>'id' = e.id)"
)
_EVENTS_SQL: LiteralString = (
    "SELECT DISTINCT ON (e.id) e.id, e.title, e.starts_at, e.ends_at, e.place_id::text, "  # noqa: S608
    "p.lat, p.lon, p.name, p.category, e.attendees, e.kind_owner, e.min_stay_owner, " + MADE_BY_PISA + ", "
    "e.attended_at FROM calendar_events e LEFT JOIN places p ON p.id = e.place_id "
    "WHERE NOT e.all_day AND e.starts_at < %s AND (e.ends_at > %s OR (e.id = ANY(%s) "
    "AND e.ends_at > %s)) ORDER BY e.id, e.calendar_id"
)
_EVENT_COLUMNS: LiteralString = (
    "SELECT DISTINCT ON (e.id) e.id, e.calendar_id, e.title, e.starts_at, e.ends_at, e.all_day, e.location, "
    "e.place_id::text, e.kind_owner, e.min_stay_owner, " + MADE_BY_PISA + ", e.attended_at, p.lat, p.lon "
    "FROM calendar_events e LEFT JOIN places p ON p.id = e.place_id WHERE e.status <> 'cancelled' AND "
)
_EVENT_LIST_SQL: LiteralString = (
    _EVENT_COLUMNS + "((NOT e.all_day AND e.starts_at < %s AND e.ends_at > %s) "
    "OR (e.all_day AND e.start_date < %s AND e.end_date > %s)) ORDER BY e.id, e.calendar_id"
)
_EVENT_ONE_SQL: LiteralString = _EVENT_COLUMNS + "e.id = %s ORDER BY e.id, e.calendar_id"


def _event_json(row: Sequence[Any], tz: ZoneInfo, home: Home | None) -> dict[str, Any]:
    eid, calendar, title, start, end, all_day, location, place_id, owner, stay, mine, attended, lat, lon = row
    here = event_kinds.at_home(place_id, (lat, lon) if lat is not None else None, home)
    kind = event_kinds.resolve(
        title, start, end, made_by_pisa=mine, owner_kind=owner, owner_min_stay=stay, home=here
    )
    return ({"attended_at": attended.astimezone(tz).isoformat()} if attended else {}) | {
        "id": eid, "calendar": calendar, "title": title, "start": start.astimezone(tz).isoformat(),
        "end": end.astimezone(tz).isoformat(), "all_day": all_day, "location": location, "place_id": place_id,
        "kind": kind.kind, "kind_source": kind.source, "min_stay_min": kind.min_stay_min,
    }  # fmt: skip


MODE_WORDS = travel.PLANNER_MODE_WORDS["en-US"]


# The user asked for this stop at this point of the day: worth more than any errand, so the solver
# places it whenever it fits (never by making a fixed event late: lateness is solved first).
_INSERTED_WEIGHT = 100
SOFT_FROM = dtime(7, 0)  # a soft event moved for a stop never starts before this


def next_mode(settings: dict[str, Any], *, rain: bool = False) -> tuple[str, Mode] | None:
    """The mode after walking among those the user allows, as (the plan item's mode, how it's
    routed): car is "drive", a taxi goes by car. In the rain, never the bike either (
    "Piove: 12 min in bici"). None: nothing else is allowed."""
    for name in settings["places"].get("modes") or ["transit"]:
        if name != "walk" and not (rain and name == "bike"):
            return ("drive" if name == "car" else name), ROUTED_MODES[name]
    return None


ROUTED_MODES: dict[str, Mode] = {
    "walk": "walk", "transit": "transit", "bike": "bike", "car": "drive", "taxi": "drive",
}  # fmt: skip
RAIN_MATRIX_S = 1.5  # the transit times for the rain's legs: estimates after this


# the mode after walking (`next_mode`): never "walk" itself
IT_MODE_WORDS = {m: w for m, w in travel.PLANNER_MODE_WORDS["it-IT"].items() if m != "walk"}


def rain_title(minutes: int, lang: str, mode: str = "transit") -> str:
    """A walk switched by the rain says so: "Piove: 18 min con i mezzi" (or the user's next
    allowed mode)."""
    if lang == "it-IT":
        return f"Piove: {minutes} min {IT_MODE_WORDS.get(mode, 'con i mezzi')}"
    return f"Rain: {minutes} min {MODE_WORDS.get(mode, 'by public transport')}"


_log = get_logger("pisa.planner")


def _minutes(a: datetime, b: datetime) -> int:
    """Whole minutes from a to b, in UTC: datetimes sharing a tzinfo subtract by wall clock (DST)."""
    return int((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds() // 60)


def _ceil_minute(moment: datetime) -> datetime:
    """The next whole minute (or this one): items never overlap by the seconds of `now`."""
    whole = moment.replace(second=0, microsecond=0)
    return whole if whole == moment else whole + timedelta(minutes=1)


@dataclass(frozen=True)
class Window:
    day: date  # the planning day (the day the window starts)
    start: datetime  # the plan's start: now (to the next minute), or day.start when it is earlier
    end: datetime
    day_start: datetime  # the whole window, for the fingerprint
    day_end: datetime

    @property
    def due_by(self) -> datetime:
        """ "Due today": by the end of the planning day's date, or of the window when it ends later."""
        midnight = datetime.combine(
            self.day + timedelta(days=1), datetime.min.time(), tzinfo=self.day_start.tzinfo
        )
        return max(midnight, self.day_end, key=lambda d: d.astimezone(UTC))

    @property
    def errands_end(self) -> datetime:
        """Tasks are placed only up to here: the window's end, or midnight after the planning day when the day
        runs past it (a late night out is for events, never an errand at 00:24)."""
        midnight = datetime.combine(
            self.day + timedelta(days=1), datetime.min.time(), tzinfo=self.day_start.tzinfo
        )
        return min(midnight, self.day_end, key=lambda d: d.astimezone(UTC))

    @property
    def minutes(self) -> int:
        return max(0, _minutes(self.start, self.end))


def window(planner: dict[str, Any], now: datetime, timezone: str) -> Window:
    """The planning window that contains `now`, or today's if it hasn't started (or is over)."""
    tz = ZoneInfo(timezone)
    local = now.astimezone(tz)
    start_t = rules.parse_hhmm(planner["day"]["start"], "planner.day.start")
    end_t = rules.parse_hhmm(planner["day"]["end"], "planner.day.end")
    day = local.date()
    if end_t < start_t and local.time() < end_t:
        day -= timedelta(days=1)  # after midnight, still inside yesterday's window
    day_start = datetime.combine(day, start_t, tzinfo=tz)
    day_end = datetime.combine(day + timedelta(days=1) if end_t < start_t else day, end_t, tzinfo=tz)
    start = _ceil_minute(now.astimezone(UTC))
    start = min(max(start, day_start.astimezone(UTC)), day_end.astimezone(UTC))
    return Window(day, start.astimezone(tz), day_end, day_start, day_end)


def _key(c: Coord, *, origin: bool = False) -> str:
    return f"{c[0]:.{ORIGIN_DECIMALS}f},{c[1]:.{ORIGIN_DECIMALS}f}" if origin else f"{c[0]:.5f},{c[1]:.5f}"


def _estimate(a: Coord, b: Coord, mode: str) -> int:
    minutes = math.ceil(distance_m(a, b) * DETOUR / 1000 / SPEED_KMH[mode] * 60)
    return minutes + (TRANSIT_WAIT_MIN if mode == "transit" else 0)


def _slot(moment: datetime) -> datetime:
    utc = moment.astimezone(UTC).replace(second=0, microsecond=0)
    return utc - timedelta(minutes=utc.minute % 15)


@dataclass
class Matrix:
    minutes: list[list[int | None]]  # None = a pair no plan uses (not asked)
    provider: list[list[str]]  # per cell: the provider id, or "estimate"


@dataclass
class Entry:
    """An event or a task as the plan sees it, after the day's adjustments (and an option's changes)."""

    kind: Literal["event", "task"]
    key: str  # the event id or the task id
    title: str
    start: datetime | None = None  # UTC; always for events, and for a task moved to a fixed time
    end: datetime | None = None
    place_id: str | None = None
    coord: Coord | None = None
    place_name: str | None = None
    category: str | None = None
    duration: int | None = None  # tasks: minutes (None: the default)
    task: Task | None = None
    adjusted: bool = False
    skipped: bool = False
    location: int | None = None  # index into the travel matrix
    attendees: int | None = None  # events: how many others (a count): the kind of event, for its buffer
    # tasks: the time it must fit in, for "Plan it" before / after / instead of an event
    within: tuple[datetime, datetime] | None = None
    # fixed | window | soft, who said so, and a window's minimum stay (events only)
    event_kind: str = "fixed"
    kind_source: str = "inferred"
    min_stay: int | None = None


@dataclass
class Day:
    """Everything a plan of the day is made from."""

    now: datetime
    win: Window
    settings: dict[str, Any]
    fingerprint: str
    here: Coord | None
    origin: Coord | None
    origin_label: str | None
    skipped: list[str]
    events: list[Entry]
    tasks: list[Entry]
    places: dict[str, dict[str, Any]]  # place id → coord, name, category, windows, location
    positions: list[Coord]
    matrix: Matrix
    adjustments: list[Change]
    rain: list[datetime] = field(default_factory=list)  # hours with rain likely, when walking
    buffers: dict[str, int] = field(default_factory=dict)  # kind of event → minutes earlier (said yes)
    switched: frozenset[tuple[int, int]] = frozenset()  # legs already on transit times (long walks, rain)
    switch_mode: tuple[str, Mode] | None = None  # the mode those legs go by (next_mode), once chosen
    todo: list[dict[str, Any]] = field(default_factory=list)  # the few open tasks that matter today
    # their spread per kind (task, event, travel), scored nightly against what really happened
    bands: dict[str, actuals.Band] = field(default_factory=dict)
    # the open tasks when it was gathered, each with its last change (`open_tasks`), stored with the plan
    open_tasks: dict[str, str] = field(default_factory=dict)


@dataclass
class Built:
    """A solved day: the solution and what it was made of."""

    solution: Solution
    blocks: list[dict[str, Any]]
    facts: dict[str, dict[str, Any]]
    events: list[Entry]
    tasks: list[Entry]
    notes: list[str]
    matrix: Matrix | None = None  # the day's, with the rain's transit legs in it (None: the day's own)
    transit_legs: set[tuple[int, int]] = field(default_factory=set)  # (from, to) switched to transit by rain
    rain_legs: set[tuple[int, int]] = field(default_factory=set)  # of those, the ones still in a rainy hour
    switch_mode: tuple[str, Mode] | None = None  # the mode the switched legs go by (next_mode)


class PlannerService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        tasks: TasksService,
        places: PlacesService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        time_limit_s: float = TIME_LIMIT_S,
        weather: WeatherService | None = None,
        trips: TripsService | None = None,
    ) -> None:
        self._pool = pool
        self._tasks = tasks
        self._places = places
        self._weather = weather
        self.trips = trips  # country-bound tasks
        self._owner = owner
        self._clock = clock
        self._time_limit_s = time_limit_s

    # --- the plan -------------------------------------------------------------------------------------------

    async def get(self, day: date | None = None, *, requested_by: uuid.UUID | None = None) -> dict[str, Any]:
        """The latest plan for the day as it stands now (`current`); today's is made now when there is none
        yet (recording the device that asked, so a throwaway device's plans go with it)."""
        today = await self.today()
        stored = await self.current(day or today, requested_by=requested_by)
        if stored is not None:
            return stored
        if day is None or day == today:
            return await self.plan(reason="ask", requested_by=requested_by)
        raise PisaError("not_found", f"There is no plan for {day.isoformat()}.", "Ask for today's plan")

    async def current(
        self, day: date | None = None, *, requested_by: uuid.UUID | None = None
    ) -> dict[str, Any] | None:
        """RAIL: the latest plan of `day` (default the planning day) as their lists are now. Every reader of
        the stored plan reads it through here (or `stored_current`): a plan of today made from other open
        tasks than the open tasks now (one added, closed, deleted or changed since, `made_from_now`) is made
        again first, at once, never read. The minute's re-plan catches up within a minute; a turn, Today or a
        notice in that minute would otherwise say tasks that are gone and miss the ones just added. None: no
        plan of that day yet."""
        today = await self.today()
        day = day or today
        async with self._pool.connection() as conn:
            row = await plan_store.latest_row(conn, day)
            if row is None:
                return None
            plan, built_from = row
            if day != today or plan_store.made_from_now(built_from, await plan_store.open_tasks(conn)):
                return plan
        return await self.plan(reason=STALE_REASON, requested_by=requested_by)

    @property
    def tasks(self) -> TasksService:
        return self._tasks

    async def today(self) -> date:
        """The planning day the user is in now (or about to start)."""
        async with self._pool.connection() as conn:
            planner = (await self._owner.settings(conn))["planner"]
        return window(planner, self._clock(), self._owner.tz).day

    async def fingerprint_for(self, day: date) -> str:
        """What a plan of `day` would be made from now (tasks, events, settings, adjustments)."""
        async with self._pool.connection() as conn:
            planner = (await self._owner.settings(conn))["planner"]
            win = window(planner, self._clock(), self._owner.tz)
            if win.day != day:
                win = window(planner, win.day_start + timedelta(days=(day - win.day).days), self._owner.tz)
            return await self._fingerprint(conn, win)

    # --- events and their kinds ----------------------------------------------------------------

    async def events(self, day: date | None = None, days: int = 1) -> dict[str, Any]:
        """GET /v1/events: the events of `days` days from `day` (default today), each with its kind (fixed,
        window, soft), who said so, and a window's minimum stay."""
        tz = self._owner.zone
        first = day or self._clock().astimezone(tz).date()
        start = datetime.combine(first, dtime.min, tzinfo=tz)
        end = datetime.combine(first + timedelta(days=days), dtime.min, tzinfo=tz)
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(_EVENT_LIST_SQL, (end, start, end.date(), first))).fetchall()
            home = await home_store.read(conn)
        return {"timezone": self._owner.tz, "events": [_event_json(r, tz, home) for r in rows]}

    async def event(self, event_id: str) -> dict[str, Any]:
        """One event as GET /v1/events shows it."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_EVENT_ONE_SQL, (event_id,))).fetchone()
            home = await home_store.read(conn)
        if row is None:
            raise PisaError(
                "not_found", f"There is no event with id {event_id[:60]!r}.", "Refresh the agenda"
            )
        return _event_json(row, self._owner.zone, home)

    async def set_kind(self, event_id: str, kind: str | None, min_stay: int | None) -> dict[str, Any]:
        """PATCH /v1/events/{id}: the user's kind for an event (null: Pisa infers it again), and a window's
        minimum stay. Pisa's own data: Google is never edited. The plan is made again."""
        if kind not in (None, *event_kinds.KINDS):
            raise PisaError(
                "validation_failed", "The kind must be fixed, window, soft or null.", "Fix the request"
            )
        if min_stay is not None and (kind != "window" or not 0 <= min_stay <= 1440):
            raise PisaError(
                "validation_failed",
                "The minimum stay (min_stay_min) is for a window event only, from 0 to 1440 minutes.",
                "Fix the request",
            )
        async with self._pool.connection() as conn:
            changed = await conn.execute(
                "UPDATE calendar_events SET kind_owner = %s, min_stay_owner = %s WHERE id = %s",
                (kind, min_stay, event_id),
            )
            if not changed.rowcount:
                raise PisaError(
                    "not_found", f"There is no event with id {event_id[:60]!r}.", "Refresh the agenda"
                )
            row = await (await conn.execute(_EVENT_ONE_SQL, (event_id,))).fetchone()
            home = await home_store.read(conn)
        _log.info("event_kind_set", kind=kind or "inferred")
        assert row is not None
        return _event_json(row, self._owner.zone, home)

    async def plan(self, *, reason: str = "ask", requested_by: uuid.UUID | None = None) -> dict[str, Any]:
        """A plan now (POST /v1/plan, the replan tool, the first GET of the day). One at a time; an ask within
        15 s of a plan made from the same inputs and place gets that plan."""
        async with self._lock(wait=True):
            now = self._clock()
            here = await self._here()
            async with self._pool.connection() as conn:
                win = window((await self._owner.settings(conn))["planner"], now, self._owner.tz)
                fingerprint = await self._fingerprint(conn, win)
                row = await (
                    await conn.execute(
                        "SELECT generated_at, fingerprint, from_lat, from_lon, plan FROM plans "
                        "WHERE day = %s ORDER BY generated_at DESC LIMIT 1",
                        (win.day,),
                    )
                ).fetchone()
            if row is not None and now - row[0] < COALESCE and row[1] == fingerprint:
                was = (row[2], row[3]) if row[2] is not None else None
                if (was is None) == (here is None) and (
                    was is None or here is None or distance_m(was, here) < COALESCE_M
                ):
                    return row[4]
            return await self._make(reason, now, requested_by)

    async def tick(self) -> dict[str, Any] | None:
        """The re-plan triggers (see the module docstring). Returns the new plan, or None."""
        async with self._lock(wait=False) as got:
            if not got:
                return None  # a plan is being made right now
            now = self._clock()
            async with self._pool.connection() as conn:
                settings = await self._owner.settings(conn)
                win = window(settings["planner"], now, self._owner.tz)
                if not now < win.day_end or not await self._awake(conn, settings, win, now):
                    return None
                row = await (
                    await conn.execute(
                        "SELECT generated_at, fingerprint, from_lat, from_lon FROM plans WHERE day = %s "
                        "ORDER BY generated_at DESC LIMIT 1",
                        (win.day,),
                    )
                ).fetchone()
                if row is not None and now - row[0] < REPLAN_GUARD:
                    return None
                fingerprint = await self._fingerprint(conn, win)
            reason = None
            if row is None:
                reason = "first plan of the day"
            elif fingerprint != row[1]:
                reason = "tasks, events or settings changed"
            else:
                here = await self._here()
                if here is not None and (row[2] is None or distance_m(here, (row[2], row[3])) > MOVED_M):
                    reason = "moved more than 1 km"
                elif now - row[0] >= REPLAN_EVERY:
                    reason = "every 30 min"
            return await self._make(reason, now) if reason else None

    def locked(self) -> AbstractAsyncContextManager[bool]:
        """The plan lock, waited for: one plan, or one applied option, at a time."""
        return self._lock(wait=True)

    def _lock(self, *, wait: bool) -> AbstractAsyncContextManager[bool]:
        # PLAN keeps the minute's re-plan (`tick`, tried) apart from a plan asked for and an applied option
        # (waited for); the `plan` step's own lock covers only the minute's runs.
        return locks.held(self._pool, locks.PLAN, wait=wait)

    async def _awake(
        self, conn: AsyncConnection, settings: dict[str, Any], win: Window, now: datetime
    ) -> bool:
        """Inside the day; before it, once the first event's trip is near: its estimated leave time minus the
        "leave now" lead, minus one re-plan interval."""
        if now >= win.day_start:
            return True
        latest = await self._places.latest_location()
        if latest is None:
            return False  # no trip to plan from: the plan starts at the first event (or the day)
        row = await (
            await conn.execute(
                "SELECT e.starts_at, p.lat, p.lon FROM calendar_events e JOIN places p ON p.id = e.place_id "
                "WHERE NOT e.all_day AND p.lat IS NOT NULL AND e.ends_at > %s AND e.starts_at < %s "
                "ORDER BY e.starts_at LIMIT 1",
                (now, win.day_end),
            )
        ).fetchone()
        if row is None:
            return False
        planner = settings["planner"]
        trip = _estimate(latest[0], (row[1], row[2]), settings["places"]["default_mode"])
        lead = timedelta(minutes=trip + planner["buffer_min"] + planner["leave_lead_min"]) + REPLAN_EVERY
        return now >= row[0] - lead

    async def _elsewhere(self) -> set[str]:
        """Open tasks only another country allows (not in today's todo here)."""
        if self.trips is None:
            return set()
        async with self._pool.connection() as conn:
            return await self.trips.elsewhere_ids(conn)

    async def _here(self) -> Coord | None:
        try:
            return await self._places.here()
        except PisaError:
            return None

    async def _fingerprint(self, conn: AsyncConnection, win: Window) -> str:
        row = await (
            await conn.execute(
                "SELECT coalesce((SELECT string_agg(id::text || status || updated_at::text "
                "|| coalesce(place_id::text, '') || coalesce(duration_min::text, ''), ',' ORDER BY id) "
                "FROM tasks WHERE status = 'open'), '') || '|' || "
                "coalesce((SELECT string_agg(DISTINCT id || starts_at::text || ends_at::text "
                "|| coalesce(place_id::text, '') || coalesce(kind_owner, '') "
                "|| coalesce(min_stay_owner::text, ''), ',') FROM calendar_events "
                "WHERE ends_at > %s AND starts_at < %s AND NOT all_day), '') || '|' || "
                "coalesce((SELECT value::text FROM app_settings WHERE key = 'settings'), '') || '|' || "
                "coalesce((SELECT string_agg(id::text, ',' ORDER BY id) FROM plan_adjustments WHERE "
                "day = %s), '') || '|' || "
                # The buffers in use (as event_buffers): a new one, or one off, moves "leave now".
                "coalesce((SELECT string_agg(subject_key || confirmed_value::text, ',' ORDER BY subject_key) "
                "FROM profile_claims WHERE subject_key LIKE 'lateness:%%' AND confirmed_value ? 'buffer_min' "
                "AND state <> 'retired'), "
                "'') || '|' || "
                # their band (the night's planned against actual): a new one re-plans the day
                "coalesce((SELECT string_agg(kind || mid::text || lo::text || hi::text, ',' ORDER BY kind) "
                "FROM plan_bands), '')",
                (win.day_start, win.day_end, win.day),
            )
        ).fetchone()
        return hashlib.sha256((row[0] if row else "").encode()).hexdigest()

    # --- making a plan ------------------------------------------------------------------------------------

    async def _make(
        self, reason: str, now: datetime, requested_by: uuid.UUID | None = None
    ) -> dict[str, Any]:
        day = await self.gather(now)
        built = await self.solve_day(day, day.events, day.tasks, self._time_limit_s)
        result = self.assemble(day, built)
        await self.store(day, reason, result, requested_by)
        _log.info(
            "plan_made",
            reason=reason,
            status=built.solution.status,
            placed=len(built.solution.tasks),
            unplanned=len(result["unplanned"]),
            late_min=built.solution.late,
        )
        return result

    async def store(
        self, day: Day, reason: str, result: dict[str, Any], requested_by: uuid.UUID | None = None
    ) -> None:
        here = day.here
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO plans (id, day, generated_at, reason, fingerprint, from_lat, from_lon, plan, "
                "requested_by, open_tasks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(), day.win.day, day.now, reason, day.fingerprint,
                    here[0] if here else None, here[1] if here else None, Jsonb(result), requested_by,
                    Jsonb(day.open_tasks),
                ),
            )  # fmt: skip

    async def gather(
        self,
        now: datetime,
        *,
        extra_places: Sequence[str] = (),
        include_events: Sequence[str] = (),
        all_pairs: bool = False,
        budget_s: float | None = None,
    ) -> Day:
        """Everything a plan of the day is made from, with the day's adjustments applied. For options,
        `extra_places` (similar places) join the travel matrix, and `all_pairs` asks every pair, since a
        variant may change the order of the day."""
        deadline = time.monotonic() + (PLAN_BUDGET_S if budget_s is None else budget_s)
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            win = window(settings["planner"], now, self._owner.tz)
            fingerprint = await self._fingerprint(conn, win)
            built_from = await plan_store.open_tasks(conn)  # what `current` compares the stored plan with
            adjustments = await load_adjustments(conn, win.day)
        mode = settings["places"]["default_mode"]
        skipped: list[str] = []
        named = [c.event_id for c in adjustments if c.event_id] + list(include_events)
        events = await self._events(win, now, named)
        tasks = await self._candidates(win, now, adjustments)
        aged = await self._tasks.aged_ids()  # an aged task is asked about, not in today's todo
        open_tasks = await self._tasks.list("open", low=False)
        zone = self._owner.zone
        pinned = {t.id for t in open_tasks if owner_deadline(t, win.day, zone)}
        # a task only another country allows is out of today's todo, never a pinned one
        timed = {e.key for e in tasks} | aged | (await self._elsewhere() - pinned)
        todo = [  # its reason in their language
            todo_json(t, self._owner.tz, str(settings["language"]))
            for t in pool_todo(open_tasks, now, timed, pinned)
        ]

        # Where the plan starts: here; else the stay (a hotel booked as a multi-day all-day event, which is
        # also where the day ends); else the first event's place.
        here = await self._here()
        origin: Coord | None = here
        origin_label = "here" if here else None
        base = await self._base(win) if here is None else None
        if base is not None:
            origin, origin_label = base.coord, base.place_name or base.title
            skipped.append(
                f"routes: no location from the last 15 minutes: the plan starts and ends at {origin_label}"
            )
            events.append(base)
        elif here is None:
            first = next((e for e in events if e.coord is not None), None)
            if first is not None:
                origin, origin_label = first.coord, first.place_name or first.title
                skipped.append(
                    f"routes: no location from the last 15 minutes: the plan starts at {first.title}"
                )
            else:
                skipped.append("routes: no location from the last 15 minutes: the first leg is left out")

        # Places, their coordinates and opening hours (tasks', the adjustments' and the options' places).
        place_ids = [t.place_id for t in tasks if t.place_id]  # online tasks have none (task_place_id)
        place_ids += [c.place_id for c in adjustments if c.place_id] + list(extra_places)
        place_ids = list(dict.fromkeys(place_ids))
        budget = min(DETAILS_BUDGET_S, deadline - time.monotonic())
        places = await self._place_facts(place_ids, win, skipped, budget)
        for t in tasks:
            info = places.get(t.place_id or "", {})
            t.coord, t.place_name, t.category = info.get("coord"), info.get("name"), info.get("category")
        events, tasks = apply_changes(events, tasks, adjustments, places)

        # Positions: index 0 = the start point (when known), then each distinct place.
        positions: list[Coord] = [origin] if origin is not None else []
        first_place = len(positions)

        def index_of(coord: Coord | None) -> int | None:
            if coord is None:
                return None
            for i in range(first_place, len(positions)):
                if distance_m(positions[i], coord) < SAME_PLACE_M:
                    return i
            positions.append(coord)
            return len(positions) - 1

        for e in events + tasks:
            e.location = index_of(e.coord)
        for info in places.values():
            info["location"] = index_of(info.get("coord"))
        if all_pairs:  # every pair between places, and from the start point (never back to it)
            needed = {
                (i, j) for i in range(len(positions)) for j in range(first_place, len(positions)) if i != j
            }
        else:
            nowu = now.astimezone(UTC)
            blocks = _blocks(
                [
                    e
                    for e in events + tasks
                    if not e.skipped and e.start is not None and e.end and e.end > nowu
                ]
            )
            needed = _needed_pairs(
                origin is not None,
                blocks,
                [t.location for t in tasks if not t.skipped and t.start is None],
            )
        matrix = await self._matrix(
            positions, origin is not None, needed, mode, now, skipped, deadline - time.monotonic()
        )
        rain = await self._rain(mode, origin or (positions[0] if positions else None), win, deadline)
        async with self._pool.connection() as conn:
            buffers = await event_buffers(conn)
            bands = await load_bands(conn)  # their real spread, from the night's planned vs actual
        return Day(
            now, win, settings, fingerprint, here, origin, origin_label, skipped, events, tasks, places,
            positions, matrix, adjustments, rain, buffers, todo=todo, bands=bands, open_tasks=built_from,
        )  # fmt: skip

    async def _rain(self, mode: str, where: Coord | None, win: Window, deadline: float) -> list[datetime]:
        """The rainy hours of the planning day when the user walks; none on any failure."""
        if mode != "walk" or self._weather is None or where is None:
            return []
        try:
            async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
                return await self._weather.rainy_hours(where, win.day)
        except (PisaError, TimeoutError) as err:
            _log.info("plan_weather_skipped", cause=getattr(err, "code", "timeout"))
            return []

    def _soft_range(
        self, block: dict[str, Any], win: Window, minute: Callable[[datetime], int]
    ) -> tuple[int, int] | None:
        """Where a soft event may start: within its own day, from 07:00 to what still ends by midnight, and
        only within the plan. Fixed and window events never move. RAIL: only an event the user
        marked soft themselves (`kind_source` owner: "Pisa may move it") moves for a stop; one Pisa made at
        the user's time is the user's event.
        A conflict is said in the plan, never solved by moving
        it."""
        members: list[Entry] = block["members"]
        if any(
            m.kind != "event" or m.event_kind != "soft" or m.kind_source != "owner" or m.adjusted
            for m in members
        ):
            return None
        zone = self._owner.zone
        local = block["start"].astimezone(zone)
        morning = datetime.combine(local.date(), SOFT_FROM, tzinfo=zone)
        midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)
        length = block["end"] - block["start"]
        lo = max(minute(morning), 0)
        hi = min(minute(midnight - length), win.minutes - (minute(block["end"]) - minute(block["start"])))
        return (lo, hi) if lo <= hi else None

    async def solve_day(
        self,
        day: Day,
        events: list[Entry],
        tasks: list[Entry],
        time_limit_s: float,
        rain_budget_s: float | None = None,
    ) -> Built:
        """The day as given (events and tasks after any changes), solved. In the rain, long walks go by
        transit and the day is solved again; `rain_budget_s` bounds that pass (the transit times' wait), and
        none left skips it (the options' budget)."""
        win, planner, now = day.win, day.settings["planner"], day.now

        def minute(moment: datetime) -> int:  # minutes from the plan's start (in UTC)
            return _minutes(win.start, moment)

        nowu = now.astimezone(UTC)
        fixed = [e for e in events + tasks if not e.skipped and e.start is not None and e.end is not None]
        blocks = _blocks([e for e in fixed if e.end and e.end > nowu])  # what is over isn't planned again
        task_in: list[TaskIn] = []
        facts: dict[str, dict[str, Any]] = {}
        for entry in tasks:
            if entry.skipped or entry.start is not None or entry.task is None:
                continue
            t = entry.task
            info = day.places.get(entry.place_id or "", {})
            windows = info.get("windows")
            urgency = self._urgency(t, win, now)
            # their own estimate (or the default) at their real median for tasks (the band, once the
            # night has scored enough of them)
            duration = actuals.estimate(entry.duration or planner["default_task_min"], day.bands.get("task"))
            deadline_min = None
            if t.due_kind == "datetime" and t.due_at is not None and win.start < t.due_at < win.end:
                deadline_min = minute(t.due_at)
            task_in.append(
                TaskIn(
                    key=t.id,
                    duration=duration,
                    weight=_weight(t, urgency),
                    location=entry.location,
                    windows=tuple((minute(a), minute(b)) for a, b in windows) if windows is not None else (),
                    deadline=deadline_min,
                    urgent=urgency != "later",
                )
            )
            if entry.within is not None:  # "Plan it" before / after / instead of an event
                lo, hi = (max(0, minute(entry.within[0])), minute(entry.within[1]))
                inside = [(max(a, lo), min(b, hi)) for a, b in (task_in[-1].windows or ((lo, hi),))]
                inside = [(a, b) for a, b in inside if b - a >= duration]
                task_in[-1] = replace(
                    task_in[-1], windows=tuple(inside) or ((0, 0),), weight=_INSERTED_WEIGHT
                )
            if (windows is not None and not windows) or t.place_elsewhere:  # closed, or in another city
                task_in[-1] = TaskIn(t.id, duration, 0, entry.location, ((0, 0),))
            facts[t.id] = {"task": t, "entry": entry, "urgency": urgency, "duration": duration,
                           "windows": windows, "info": info}  # fmt: skip
        start_at = -_minutes(_ceil_minute(now), win.start) if now < win.start else 0
        limit = max(0, minute(win.errands_end))
        if limit < win.minutes:  # no errand after midnight, nor (from the window) before the day or now
            task_in = [replace(t, windows=_clip(t.windows, limit)) for t in task_in]
        trip = day.bands.get("travel")  # trips at their real median (the band, when scored)
        problem = Problem(
            horizon=win.minutes,
            travel=tuple(
                tuple(m if m is None or m == 0 or trip is None else actuals.estimate(m, trip) for m in row)
                for row in day.matrix.minutes
            ),
            origin=0 if day.origin is not None else None,
            buffer=planner["buffer_min"],
            blocks=tuple(
                Block(
                    b["key"],
                    minute(b["start"]),
                    minute(_ceil_minute(b["end"])),  # an end with seconds rounds up: nothing is placed in it
                    b["location"],
                    latest=minute(b["latest"]) if b.get("latest") is not None else None,
                    shift=self._soft_range(b, win, minute),
                    # The user said yes to leaving earlier for this kind of event (the weekly check):
                    # given to the solver, so nothing is planned in those minutes.
                    early=day.buffers.get(event_kind(b["members"][0].title, b["members"][0].attendees), 0),
                )
                for b in blocks
            ),
            tasks=tuple(task_in),
            start_at=min(0, start_at),
        )
        solution = await asyncio.to_thread(solve, problem, time_limit_s=time_limit_s)
        notes: list[str] = []
        for b in blocks:  # a soft event Pisa moved for a stop: the plan shows it at its new time
            if b["key"] in solution.shifts:
                length = b["end"] - b["start"]
                b["moved_from"] = b["start"]
                b["start"] = win.start + timedelta(minutes=solution.shifts[b["key"]])
                b["end"] = b["start"] + length
        if solution.status not in ("optimal", "feasible"):
            _log.warning("plan_unsolved", status=solution.status)
            notes.append("plan: Pisa couldn't work out the day in time; the events are shown as they are")
        titles = {b["key"]: b["title"] for b in blocks}
        for key in solution.dropped:
            notes.append(f"routes: the trip to {titles[key]} doesn't fit the day; it is shown without one")
        built = Built(solution, blocks, facts, events, tasks, notes)
        # A walk over the user's limit (45 minutes by default), or a long one in the rain, goes
        # by the next mode they allow (transit, ferries included; else bike, car or taxi): those legs
        # take its times, solved again. With no budget left (the options' 2 s), the rain's walks stay as they
        # are but a long walk still never does: it takes an estimated time.
        # In the rain the switch skips the bike too; a day with both goes by the dry mode.
        spent = rain_budget_s is not None and rain_budget_s <= 0
        dry = next_mode(day.settings, rain=True)
        wet = set() if spent or dry is None else self._wet_legs(day, built)
        fallback = day.switch_mode or (dry if wet else next_mode(day.settings))
        built.switch_mode = fallback
        if fallback is None:  # walking is all the user allows (or walking and the bike, in the rain)
            return built
        switch = self._long_walks(day, built) | wet
        if not switch:
            return built
        routed = fallback[1]
        matrix = Matrix([row[:] for row in day.matrix.minutes], [row[:] for row in day.matrix.provider])
        left: float | None = 0.0
        if spent:
            for i, j in switch:
                matrix.minutes[i][j] = _estimate(day.positions[i], day.positions[j], routed)
                matrix.provider[i][j] = ESTIMATE
        else:
            began = time.monotonic()
            wait = RAIN_MATRIX_S if rain_budget_s is None else min(RAIN_MATRIX_S, rain_budget_s)
            transit = await self._matrix(
                day.positions, day.origin is not None, switch, routed, now, notes, wait
            )
            for i, j in switch:
                matrix.minutes[i][j], matrix.provider[i][j] = transit.minutes[i][j], transit.provider[i][j]
            left = None if rain_budget_s is None else rain_budget_s - (time.monotonic() - began)
        # Solved once more with those times, within what is left of the budget; a leg already switched isn't
        # switched again, and the legs switched by that solve stay switched (switched | its own).
        again = await self.solve_day(
            replace(day, matrix=matrix, rain=[], switched=day.switched | switch, switch_mode=fallback),
            events,
            tasks,
            time_limit_s,
            rain_budget_s=left,
        )
        self.with_rain_modes(day, again, again.matrix or matrix, again.transit_legs | switch)
        return again

    def with_rain_modes(
        self, day: Day, built: Built, matrix: Matrix | None, transit: set[tuple[int, int]]
    ) -> None:
        """`built` uses `matrix`, where the `transit` legs take transit times. Each such leg the solution
        still uses goes by transit; "Piove" (and `rain: true`) only on those still in a rainy hour: the new
        timing may have moved one out of the rain."""
        built.matrix, built.transit_legs = matrix, set(transit)
        built.rain_legs = self._wet_legs(day, built, among=transit)

    def _long_walks(self, day: Day, built: Built) -> set[tuple[int, int]]:
        """Walking legs over the user's walking limit (places.walk_max_min): never planned on foot."""
        limit = int(day.settings["places"].get("walk_max_min") or WALK_MAX_MIN)
        if day.settings["places"]["default_mode"] != "walk":
            return set()
        if built.solution.status not in ("optimal", "feasible"):
            return set()
        return {
            (leg.from_location, leg.to_location)
            for leg in built.solution.legs
            if leg.from_location is not None
            and leg.minutes > limit
            and (leg.from_location, leg.to_location) not in day.switched
        }

    def _wet_legs(
        self, day: Day, built: Built, among: set[tuple[int, int]] | None = None
    ) -> set[tuple[int, int]]:
        """Walking legs of 15 minutes or more that fall in an hour with rain likely; with `among`, those of
        these legs (already switched to transit, whatever their length) that fall in such an hour."""
        if not day.rain or built.solution.status not in ("optimal", "feasible"):
            return set()
        start = day.win.start.astimezone(UTC)
        arrivals = {**built.solution.arrivals, **{k: v[0] for k, v in built.solution.tasks.items()}}
        hours = [h.astimezone(UTC) for h in day.rain]
        wet: set[tuple[int, int]] = set()
        for leg in built.solution.legs:
            if leg.from_location is None or leg.to not in arrivals:
                continue
            if among is not None and (leg.from_location, leg.to_location) not in among:
                continue
            if among is None and leg.minutes < RAIN_LEG_MIN:
                continue
            leave = start + timedelta(minutes=arrivals[leg.to] - leg.buffer - leg.minutes)
            end = leave + timedelta(minutes=leg.minutes)
            if any(h < end and leave < h + timedelta(hours=1) for h in hours):
                wet.add((leg.from_location, leg.to_location))
        return wet

    def _urgency(self, task: Task, win: Window, now: datetime) -> Urgency:
        if task.due_at is None:
            return "later"
        if task.due_at < now:
            return "overdue"
        return "today" if task.due_at < win.due_by else "later"

    async def _events(self, win: Window, now: datetime, named: Sequence[str] = ()) -> list[Entry]:
        """The timed events of the planning day (one per event id), with their place's position: the ones
        already over too, so the plan shows what happened to them. Before the day starts, from now:
        their trips start from here. Events the day's adjustments name are included as well."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    _EVENTS_SQL,
                    (win.end, min(now, win.day_start), list(named), win.day_start),
                )
            ).fetchall()
            home = await home_store.read(conn)
        events = []
        for (
            eid,
            title,
            start,
            end,
            place_id,
            lat,
            lon,
            name,
            category,
            attendees,
            owner,
            stay,
            mine,
            done,
        ) in rows:
            here = event_kinds.at_home(place_id, (lat, lon) if lat is not None else None, home)
            kind = event_kinds.resolve(
                title, start, end, made_by_pisa=mine, owner_kind=owner, owner_min_stay=stay, home=here
            )
            wait = kind.kind == "window" and event_kinds.waits(title, at_home=here)
            if done is not None and wait and start < done < end:
                end = done  # the wait is over when the delivery was confirmed
            events.append(Entry(
                "event", eid, title, start.astimezone(UTC), end.astimezone(UTC), place_id,
                (lat, lon) if lat is not None else None, name, category, attendees=attendees,
                event_kind=kind.kind, kind_source=kind.source, min_stay=kind.min_stay_min,
            ))  # fmt: skip
        return sorted(events, key=lambda e: (e.start, e.key))

    async def _base(self, win: Window) -> Entry | None:
        """The stay on the planning day: a multi-day all-day event with a place (a hotel from Gmail). As an
        entry: the end of the day there (a trip back, no item of its own)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT e.id, e.title, p.id::text, p.lat, p.lon, p.name, p.category FROM "
                    "calendar_events e "
                    "JOIN places p ON p.id = e.place_id WHERE e.all_day AND p.lat IS NOT NULL "
                    "AND e.start_date <= %s AND e.end_date > %s AND e.end_date - e.start_date >= 2 "
                    "ORDER BY e.start_date DESC, e.id LIMIT 1",
                    (win.day, win.day),
                )
            ).fetchone()
        if row is None:
            return None
        eid, title, place_id, lat, lon, name, category = row
        end = win.end.astimezone(UTC)
        return Entry(
            "event", f"{BASE_PREFIX}{eid}", title, end, end + timedelta(minutes=1), place_id, (lat, lon),
            name, category,
        )  # fmt: skip

    async def _candidates(
        self, win: Window, now: datetime, adjustments: Sequence[Change] = ()
    ) -> list[Entry]:
        """The tasks with a time today: a window the user (or a delivery's email) gave, and any open
        task an adjustment names ("Plan it"). Never one Pisa would place by itself (they are the `todo`)."""
        open_tasks = await self._tasks.list("open", low=False)  # low is never planned
        aged, _ = await self._aged_and_yesterday(win.day, now)
        # a task overdue for days leaves the plan (aging asks about it once); the list keeps it.
        named = {c.task_id for c in adjustments if c.task_id}
        open_tasks = [t for t in open_tasks if t.id not in aged or t.id in named]
        # a delivery with its window is no errand to schedule: on its own day it stands at its
        # window, on any other day it isn't in the plan.
        delivery = [t for t in open_tasks if t.expected_from is not None and t.expected_until is not None]
        zone = self._owner.zone
        due = [t for t in delivery if t.expected_from and t.expected_from.astimezone(zone).date() == win.day]
        # Pisa never places a task at a time by itself. A task is in
        # the plan at a time only when the user gave one (its window above, or "Plan it": an adjustment
        # naming it); every other open task is in the day's `todo`, by deadline then priority.
        # a task's `due` is a deadline ("entro le 19"), never the time to do it: a task the
        # user timed ("chiamare il notaio alle 15") has their start (`expected_from`) and stands at it above;
        # a deadline today is pinned in the `todo` (`owner_deadline`), never a block after its deadline.
        chosen = [t for t in open_tasks if t.id in named and t not in delivery]
        return [
            Entry("task", t.id, t.title, place_id=task_place_id(t), duration=t.duration_min, task=t)
            for t in chosen
        ] + [
            Entry("task", t.id, t.title, start=_utc(t.expected_from), end=_utc(t.expected_until), task=t)
            for t in due
            if t.expected_from is not None and t.expected_until is not None
        ]

    async def _aged_and_yesterday(self, day: date, now: datetime) -> tuple[set[str], set[str]]:
        """The aged tasks (aging), and the tasks yesterday's plan had in it."""
        aged = await self._tasks.aged_ids()
        async with self._pool.connection() as conn:
            plan = await (
                await conn.execute(
                    "SELECT plan FROM plans WHERE day = %s ORDER BY generated_at DESC LIMIT 1",
                    (day - timedelta(days=1),),
                )
            ).fetchone()
        items = (plan[0].get("items") or []) if plan else []
        return aged, {i["task_id"] for i in items if i.get("task_id")}

    async def _place_facts(
        self, place_ids: list[str], win: Window, skipped: list[str], budget_s: float
    ) -> dict[str, dict[str, Any]]:
        """For each place: its position, name, category, and opening windows within the plan (None =
        unknown)."""
        out: dict[str, dict[str, Any]] = {}

        async def one(pid: str) -> None:
            place_id = uuid.UUID(pid)
            name, category = await self._places.label(place_id)
            entry: dict[str, Any] = {"coord": None, "name": name, "category": category, "windows": None}
            out[pid] = entry
            try:
                entry["coord"] = await self._places.coords(place_id)
            except PisaError as err:  # no coordinates: planned without the travel to it
                _log.info("plan_place_coords_missing", place_id=pid, code=err.code)
            info, _ = await self._places.place_brief(place_id)  # details without reviews, cached 24 h
            if info is not None:
                entry["windows"] = open_windows(
                    info.current + info.weekly,
                    win.start,
                    win.end,
                    self._owner.tz,
                    frozenset(info.closed_dates),
                )

        try:
            async with asyncio.timeout(max(0.0, budget_s)):
                await asyncio.gather(*(one(pid) for pid in place_ids))
        except TimeoutError:
            skipped.append("details: opening hours took too long; some places count as open all day")
        for pid in place_ids:  # a task whose place has no position is done "anywhere": say so
            if pid in out and out[pid]["coord"] is None:
                skipped.append(f"places: no position for {out[pid]['name']}: planned as if done anywhere")
        return out

    async def _matrix(
        self,
        positions: list[Coord],
        has_origin: bool,
        needed: set[tuple[int, int]],
        mode: Mode,
        now: datetime,
        skipped: list[str],
        budget_s: float,
    ) -> Matrix:
        """Minutes between the pairs a plan can use: the cache first, then the TravelTimes port, one call per
        start position. What doesn't arrive in time is estimated."""
        n = len(positions)
        matrix = Matrix(
            [[0 if i == j else None for j in range(n)] for i in range(n)], [[ESTIMATE] * n for _ in range(n)]
        )
        slot = _slot(now)

        def key(i: int) -> str:
            return _key(positions[i], origin=has_origin and i == 0)

        ask: dict[int, list[int]] = {}
        wanted = set()
        for i, j in sorted(needed):
            if distance_m(positions[i], positions[j]) < SAME_PLACE_M:
                matrix.minutes[i][j] = 0
                continue
            matrix.minutes[i][j] = _estimate(positions[i], positions[j], mode)  # until a provider answers
            wanted.add((key(i), key(j)))
            ask.setdefault(i, []).append(j)
        if not ask:
            return matrix
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT from_key, to_key, slot, minutes, provider FROM travel_cache WHERE mode = %s "
                    "AND created_at > %s",
                    (mode, now - timedelta(days=1)),
                )
            ).fetchall()
        cached: dict[tuple[str, str], tuple[timedelta, int | None, str]] = {}
        for f, t, s, m, p in rows:
            if (f, t) not in wanted:
                continue
            distance = abs(s - slot)
            if (f, t) not in cached or distance < cached[(f, t)][0]:
                cached[(f, t)] = (distance, m, p)
        for i in list(ask):
            for j in list(ask[i]):
                hit = cached.get((key(i), key(j)))
                origin_row = has_origin and i == 0
                if hit is None or (origin_row and hit[0] > ORIGIN_REUSE):
                    continue  # the origin row is reused only around its departure; the rest for the day
                if hit[1] is not None:
                    matrix.minutes[i][j], matrix.provider[i][j] = hit[1], hit[2]
                ask[i].remove(j)
            if not ask[i]:
                del ask[i]
        estimated: list[str] = []
        try:
            async with asyncio.timeout(max(0.0, budget_s)):
                for i, targets in ask.items():
                    try:
                        estimates, used, more = await self._places.travel_between(
                            positions[i], [positions[j] for j in targets], mode, depart_at=max(now, slot)
                        )
                    except PisaError as err:
                        if not estimated:
                            estimated.append(
                                f"routes: {err.message} Travel times are estimated from the distance."
                            )
                        continue
                    skipped.extend(s for s in more if s not in skipped)
                    async with self._pool.connection() as conn:
                        for j, e in zip(targets, estimates, strict=True):
                            if e is not None:
                                matrix.minutes[i][j], matrix.provider[i][j] = e.best_minutes, used
                            await conn.execute(
                                "INSERT INTO travel_cache (from_key, to_key, mode, slot, minutes, "
                                "traffic_minutes, provider, created_at) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                (
                                    key(i), key(j), mode, slot, e.best_minutes if e else None,
                                    e.traffic_minutes if e else None, used, self._clock(),
                                ),
                            )  # fmt: skip
        except TimeoutError:
            estimated.append("routes: travel times took too long; the rest are estimated from the distance")
        skipped.extend(estimated)
        return matrix

    # --- from the solution ---------------------------------------------------------------------

    def assemble(self, day: Day, built: Built) -> dict[str, Any]:
        win, now, solution, blocks, facts = day.win, day.now, built.solution, built.blocks, built.facts
        mode, matrix = day.settings["places"]["default_mode"], built.matrix or day.matrix
        skipped = [*day.skipped, *built.notes]
        tz = self._owner.zone
        lang = str(day.settings["language"])  # every line Pisa writes into the plan is in their language
        it = lang == "it-IT"
        base = win.start.astimezone(UTC)
        earliest = min(base, _ceil_minute(now.astimezone(UTC)))  # before the day starts: now

        def at(minute: int) -> datetime:
            return base + timedelta(minutes=minute)

        items: list[dict[str, Any]] = []
        titles: dict[str, str] = {}
        place_of: dict[str, str | None] = {}
        block_of = {b["key"]: b for b in blocks}
        fixed: list[tuple[datetime, datetime, str]] = []  # events and the trips to them: before any task
        for b in blocks:
            titles[b["key"]], place_of[b["key"]] = b["title"], b["place_id"]
            for member in b["members"]:
                if member.kind == "task":  # a task moved to a fixed time, or a delivery's window
                    task = member.task
                    expected = task is not None and task.expected_from is not None and task.expected_until
                    timed = task is not None and task.source_kind != "email"  # the user's time
                    if expected and timed:
                        sentence = "All'ora che hai detto." if it else "At the time you said."
                    elif expected and member.start is not None and member.end is not None:
                        a, z = f"{member.start.astimezone(tz):%H:%M}", f"{member.end.astimezone(tz):%H:%M}"
                        sentence = (
                            f"Consegna prevista tra le {a} e le {z}."
                            if it
                            else f"Delivery expected between {a} and {z}."
                        )
                    else:
                        sentence = "Spostato qui come hai chiesto." if it else "Moved here at your request."
                    items.append(_task_item(member, member.start, member.end, sentence))
            events = [m for m in b["members"] if m.kind == "event" and not m.key.startswith(BASE_PREFIX)]
            if not events:
                fixed.append((b["start"], b["end"], b["title"]))
                continue
            # one item per calendar event, with its own title and times; the events it
            # overlaps are named (`overlaps`). The route still treats the block as one stop (one trip in).
            for n, ev in enumerate(events):
                assert ev.start is not None and ev.end is not None
                single = len(events) == 1
                start = b["start"] if single else ev.start  # a soft event Pisa moved: the block's new time
                end = b["end"] if single else ev.end
                # an event keeps its own place (none stays none: the wait at home in a
                # block at the park); the trip in, arrival and lateness are for events at the block's place
                here = single or (ev.place_id is not None and ev.place_id == b["place_id"])
                item = {"kind": "event", "start": start, "end": end, "title": ev.title, "event_id": ev.key,
                        "event_ids": [ev.key], "place_id": ev.place_id, "status": "planned"}  # fmt: skip
                others = [o for o in events if o is not ev and o.start < ev.end and ev.start < o.end]  # type: ignore[operator]
                if others:
                    item["overlaps"] = [{"event_id": o.key, "title": o.title} for o in others]
                if ev.adjusted:
                    item["adjusted"] = True
                item |= _kind_fields(ev)
                if single and b.get("moved_from") is not None:  # a soft event Pisa moved for a stop
                    item["moved_from"] = b["moved_from"]
                    was = f"{b['moved_from'].astimezone(tz):%H:%M}"
                    item["why"] = (
                        f"Spostato dalle {was} perché ci stia la tappa: solo nel piano di Pisa, il "
                        "calendario tiene il suo orario."
                        if it
                        else f"Moved from {was} so the stop fits: in Pisa's plan only, the calendar keeps "
                        "its time."
                    )
                latest = b.get("latest") if single else _window_latest([ev])
                arrival = solution.arrivals.get(b["key"])
                on_time = max(start, earliest)
                # A window event is reached any time up to its latest arrival: only later is late.
                due = max(latest, on_time) if latest is not None else on_time
                if here and arrival is not None and at(arrival) > on_time and (n == 0 or at(arrival) > start):
                    # late: the event keeps its own times; a later event of the block is late only when
                    # the trip in arrives after its own start
                    leg_in = next((leg for leg in solution.legs if leg.to == b["key"]), None)
                    there = at(arrival) - timedelta(minutes=leg_in.buffer if leg_in else 0)
                    there = max(there, on_time)
                    late = _minutes(due, there) if there > due else 0
                    if latest is not None and there > on_time:
                        # The arrival moved inside the window (a stop before it): the item starts when the
                        # trip (with its buffer) ends, so it doesn't overlap what comes first.
                        item["arrive_at"] = item["start"] = min(at(arrival), end)
                    if there >= end:
                        earliest_there = f"{there.astimezone(tz):%H:%M}"
                        item["why"] = (
                            f"Non ci arrivi prima che finisca: al più presto alle {earliest_there}."
                            if it
                            else f"You can't get there before it ends: the earliest is {earliest_there}."
                        )
                    elif late > 0:
                        by = f"{there.astimezone(tz):%H:%M}"
                        item["why"] = (
                            f"Ci arrivi alle {by}, con {late} min di ritardo."
                            if it
                            else f"You can get there by {by}, {late} min late."
                        )
                items.append(item)
                fixed.append((item["start"], end, ev.title))
        for key in solution.tasks:
            task = facts[key]["task"]
            titles[key], place_of[key] = task.title, facts[key]["entry"].place_id
        legs_into = {leg.to: leg for leg in solution.legs}
        previous: dict[str, str | None] = {}
        for i, key in enumerate(solution.order):
            previous[key] = solution.order[i - 1] if i > 0 else None
        for key, (s, e) in solution.tasks.items():
            fact = facts[key]
            task: Task = fact["task"]
            entry: Entry = fact["entry"]
            closes = None
            for a, b in fact["windows"] or []:
                if _utc(a) <= at(s) and at(e) <= _utc(b) and _utc(b) < _utc(win.end):
                    closes = b
            leg = legs_into.get(key)
            prev = previous.get(key)
            trip = leg.minutes if leg is not None else 0
            chained = prev if prev in titles and 0 < trip <= 10 else None
            if prev in titles and trip == 0 and entry.place_id and place_of.get(prev) == entry.place_id:
                chained = prev
            sentence = why(
                TaskFacts(
                    task.title, task.priority, fact["urgency"], task.due_at, task.due_kind, entry.place_name
                ),
                self._owner.tz,
                lang,
                closes_at=closes,
                next_to=titles.get(chained) if chained else None,
                next_to_minutes=(leg.minutes if leg else 0) if chained else None,
            )
            items.append(_task_item(entry, at(s), at(e), sentence))
        arrivals = {**solution.arrivals, **{k: v[0] for k, v in solution.tasks.items()}}
        reserved: list[tuple[datetime, datetime]] = []  # slack without a trip: never free time
        sources: set[str] = set()
        for leg in solution.legs:
            arrive = at(arrivals[leg.to])
            leave = arrive - timedelta(minutes=leg.minutes + leg.buffer)  # with the user's buffer, if any
            if leg.minutes == 0:
                reserved.append((leave, arrive))
                continue
            provider = (
                matrix.provider[leg.from_location][leg.to_location]
                if leg.from_location is not None
                else ESTIMATE
            )
            sources.add(provider)
            end = arrive  # the buffer belongs to the trip
            block = block_of.get(leg.to)
            if block is not None:
                fixed.append((leave, arrive, block["title"]))
                due = block["latest"] if block.get("latest") is not None else block["start"]
                if arrive > max(due, earliest):
                    # Late: the trip keeps its real time, overlapping the event's start (the one overlap
                    # allows), and keeps its "leave now".
                    end = leave + timedelta(minutes=leg.minutes)
                    if end >= block["end"]:  # unreachable before it ends: shown up to its start
                        end = max(block["start"], earliest)
                        if leave >= end:
                            continue
            pair = (leg.from_location, leg.to_location)
            switched_to = (built.switch_mode or next_mode(day.settings) or ("transit", "transit"))[0]
            wet, leg_mode = pair in built.rain_legs, switched_to if pair in built.transit_legs else mode
            title = (
                rain_title(leg.minutes, day.settings["language"], leg_mode)
                if wet
                else f"{leg.minutes} min {MODE_WORDS[leg_mode]}"
            )
            items.append(
                {"kind": "travel", "start": leave, "end": end, "title": title,
                 "mode": leg_mode, "minutes": leg.minutes,
                 "provider": provider, "to_place_id": place_of.get(leg.to), "leave_at": leave}
                | ({"rain": True} if wet else {})  # the rain rule switched it (IOS shows the note only then)
            )  # fmt: skip
        end = win.end.astimezone(UTC)
        busy = [(it["start"], it["end"], "") for it in items] + [(s, e, "") for s, e in reserved]
        free = [
            {"kind": "free", "start": s, "end": e}
            for s, e in _gaps(base, end, busy)
            if _minutes(s, e) >= FREE_MIN
        ]
        # What is already over stays on the day as it was (its status says what happened).
        for entry in built.events:
            if entry.key.startswith(BASE_PREFIX):
                continue
            if (
                not entry.skipped
                and entry.start is not None
                and entry.end is not None
                and entry.end <= _utc(now)
            ):
                past: dict[str, Any] = {
                    "kind": "event", "start": entry.start, "end": entry.end, "title": entry.title,
                    "event_id": entry.key, "event_ids": [entry.key], "place_id": entry.place_id,
                    "status": "planned",
                } | _kind_fields(entry)  # fmt: skip
                if entry.adjusted:
                    past["adjusted"] = True
                items.append(past)
        # Skipped items stay on the day with their own times, no trip, and don't take time.
        for entry in built.events + built.tasks:
            if entry.skipped and entry.start is not None and entry.end is not None:
                gone = _task_item(entry, entry.start, entry.end, None) if entry.kind == "task" else {
                    "kind": "event", "start": entry.start, "end": entry.end, "title": entry.title,
                    "event_id": entry.key, "event_ids": [entry.key], "place_id": entry.place_id,
                } | _kind_fields(entry)  # fmt: skip
                gone["status"] = "skipped"
                gone.pop("why", None)
                items.append(gone)
        for it in items:  # their band around what Pisa planned (low, high minutes), when scored
            if it["kind"] == "task" and it.get("task_id") in facts and it.get("start") and it.get("end"):
                it["scaled"] = True  # from their band: the night divides it
                spread = actuals.range_of(_minutes(it["start"], it["end"]), day.bands.get("task"))
            elif it["kind"] == "travel":
                spread = actuals.range_of(int(it["minutes"]), day.bands.get("travel"))
            else:
                spread = None
            if spread is not None:
                it["band"] = {"lo_min": spread[0], "hi_min": spread[1]}
        timeline = sorted(items + free, key=lambda it: (_utc(it["start"]), _utc(it["end"])))
        longest = max((_minutes(s, e) for s, e in _gaps(base, end, fixed)), default=0)
        solved = solution.status in ("optimal", "feasible") and solution.tasks_solved
        unplanned = []
        for key, fact in facts.items():
            if key in solution.tasks:
                continue
            task = fact["task"]
            deadline = (
                task.due_at if task.due_kind == "datetime" and task.due_at and task.due_at > now else None
            )
            if task.place_elsewhere:
                word = "Altrove" if day.settings["language"] == "it-IT" else "Elsewhere"
                unplanned.append(
                    {"task_id": key, "title": task.title, "reason": f"{word}: {task.place_elsewhere}"}
                )
                continue
            unplanned.append(
                {
                    "task_id": key,
                    "title": task.title,
                    "reason": unplanned_reason(
                        duration=fact["duration"],
                        windows=fact["windows"],
                        longest_free=longest,
                        deadline=deadline,
                        plan_start=win.start,
                        timezone=self._owner.tz,
                        lang=lang,
                        busy=fixed,
                        solved=solved,
                        # Only when midnight cuts the day short: at 20:45 on an
                        # 08:00-21:00 day the reason is the day's end, not midnight.
                        errands_end=(
                            win.errands_end
                            if win.errands_end.astimezone(UTC) < win.day_end.astimezone(UTC)
                            else None
                        ),
                    ),
                }
            )
        routes = None
        if sources:
            real = sorted(sources - {ESTIMATE})
            routes = real[0] if real else ESTIMATE
        return _jsonable(
            {
                "date": win.day.isoformat(),
                "generated_at": now.astimezone(tz),
                "from": (
                    {"lat": day.origin[0], "lon": day.origin[1], "label": day.origin_label}
                    if day.origin
                    else None
                ),
                "items": timeline,
                "unplanned": unplanned,
                "todo": day.todo,  # the pool's head for today (no time: the user's, when they have it)
                "sources": {"routes": routes, "skipped": skipped},
                # the factor each kind was planned at, so the night scores their
                # actual minutes against the unscaled plan (never against its own band)
                "factors": {k: actuals.factor(day.bands.get(k)) for k in ("task", "travel")},
            },
            tz,
        )


def apply_changes(
    events: list[Entry], tasks: list[Entry], changes: Sequence[Change], places: dict[str, dict[str, Any]]
) -> tuple[list[Entry], list[Entry]]:
    """The day's items with changes applied in order (the latest change of an item wins). Unknown items are
    left alone: an adjustment for an event that left the calendar does nothing."""
    events = [replace(e) for e in events]
    tasks = [replace(t) for t in tasks]
    by_key = {("event", e.key): e for e in events} | {("task", t.key): t for t in tasks}
    for change in changes:
        entry = by_key.get(("event", change.event_id) if change.event_id else ("task", change.task_id or ""))
        if entry is None:
            continue
        if change.action == "plan":
            continue  # the user asked Pisa to fit it in today: it is planned, at Pisa's time
        if change.action == "skip":
            entry.skipped = True
            if change.start is not None and change.end is not None and entry.start is None:
                entry.start, entry.end = change.start, change.end  # where it was, to show it skipped
            continue
        entry.adjusted, entry.skipped = True, False
        if change.action == "move" and change.start is not None and change.end is not None:
            entry.start, entry.end = change.start.astimezone(UTC), change.end.astimezone(UTC)
        elif change.action == "replace_place" and change.place_id:
            info = places.get(change.place_id, {})
            entry.place_id, entry.coord = change.place_id, info.get("coord")
            entry.location = info.get("location")  # its matrix index: the new place is where Pisa goes
            entry.place_name, entry.category = info.get("name"), info.get("category")
        elif change.action == "shorten" and change.duration_min:
            if entry.kind == "event" and entry.start is not None:
                entry.end = entry.start + timedelta(minutes=change.duration_min)
            else:
                entry.duration = change.duration_min
                if entry.start is not None:
                    entry.end = entry.start + timedelta(minutes=change.duration_min)
    return events, tasks


async def load_adjustments(conn: AsyncConnection, day: date) -> list[Change]:
    rows = await (
        await conn.execute(
            "SELECT action, event_id, task_id::text, starts_at, ends_at, place_id::text, duration_min "
            "FROM plan_adjustments WHERE day = %s ORDER BY seq",
            (day,),
        )
    ).fetchall()
    return [Change(a, e, t, s, en, p, d) for a, e, t, s, en, p, d in rows]


def _task_item(
    entry: Entry, start: datetime | None, end: datetime | None, sentence: str | None
) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": "task", "start": start, "end": end, "title": entry.title,
                            "task_id": entry.key, "place_id": entry.place_id, "why": sentence,
                            "status": "planned", "scheduled_by": scheduled_by(entry)}  # fmt: skip
    if entry.adjusted:
        item["adjusted"] = True
    return item


def owner_deadline(task: Task, day: date, zone: ZoneInfo) -> bool:
    """The user gave this task a deadline at a time on `day`
    ("entro le 19"): a `datetime` due on that day whose source is their (`owner`, or none recorded before
    0056). A deadline, never a block: it is pinned in the day's `todo`, so Today never loses it."""
    return (
        task.due_kind == "datetime"
        and task.due_at is not None
        and task.due_at.astimezone(zone).date() == day
        and task.deadline_source in (None, "owner")
    )


def scheduled_by(entry: Entry) -> str:
    """Who gave a task in the plan its time: an email's window (a delivery), else the user (their time, or
    "Plan it")."""
    task = entry.task
    return "email" if task is not None and task.source_kind == "email" and task.expected_from else "owner"


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def _weight(task: Task, urgency: Urgency) -> int:
    return PRIORITY_WEIGHT.get(task.priority, 1) * URGENCY_WEIGHT[urgency]


def _gaps(
    start: datetime, end: datetime, busy: list[tuple[datetime, datetime, str]]
) -> list[tuple[datetime, datetime]]:
    """The stretches of [start, end) no busy span covers, in UTC."""
    out: list[tuple[datetime, datetime]] = []
    cursor, end = _utc(start), _utc(end)
    for s, e, _ in sorted(busy, key=lambda b: _utc(b[0])):
        s, e = _utc(s), _utc(e)
        if e <= cursor:
            continue
        if s > cursor:
            out.append((cursor, min(s, end)))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        out.append((cursor, end))
    return [(a, b) for a, b in out if a < b]


def _needed_pairs(
    has_origin: bool, blocks: list[dict[str, Any]], task_locations: list[int | None]
) -> set[tuple[int, int]]:
    """The position pairs a plan can use: from the start point to anything, between tasks and events both
    ways, from an event to a later one; never back to the start point, nor from an event to an earlier one."""
    nodes: list[tuple[int, datetime | None, datetime | None]] = [
        (b["location"], b["start"], b["end"]) for b in blocks if b["location"] is not None
    ]
    nodes += [(loc, None, None) for loc in task_locations if loc is not None]
    pairs: set[tuple[int, int]] = set()
    for a, (la, sa, _) in enumerate(nodes):
        if has_origin and la != 0:
            pairs.add((0, la))
        for b, (lb, _, eb) in enumerate(nodes):
            if a == b or la == lb:
                continue
            if sa is not None and eb is not None and sa >= eb:
                continue  # from a later event back to an earlier one
            pairs.add((la, lb))
    return pairs


def _clip(windows: tuple[tuple[int, int], ...], limit: int) -> tuple[tuple[int, int], ...]:
    """A task's opening windows (empty = any time) cut at `limit`; nothing left = closed ((0, 0))."""
    cut = tuple((a, min(b, limit)) for a, b in (windows or ((0, limit),)) if min(b, limit) > a)
    return cut or ((0, 0),)


def _blocks(entries: list[Entry]) -> list[dict[str, Any]]:
    """Fixed time (events, and tasks moved to a time): overlapping events become one block, since Pisa can be
    at one of them only; the block is at the place of its first member that has one. An item moved to a time
    (by an adjustment, or a task) stays a block of its own, with or without a place: when it
    overlaps the next event, that event is late, and the plan says so."""
    blocks: list[dict[str, Any]] = []
    for e in sorted(entries, key=lambda x: (x.start, x.key)):
        assert e.start is not None and e.end is not None
        alone = e.adjusted or e.kind == "task"
        last_alone = blocks and any(m.adjusted or m.kind == "task" for m in blocks[-1]["members"])
        if blocks and e.start < blocks[-1]["end"] and not alone and not last_alone:
            b = blocks[-1]
            b["end"] = max(b["end"], e.end)
            b["title"] += f" / {e.title}"
            b["members"].append(e)
            if b["location"] is None and e.location is not None:
                b["location"], b["place_id"] = e.location, e.place_id
            continue
        blocks.append(
            {"key": e.key, "start": e.start, "end": e.end, "title": e.title, "location": e.location,
             "place_id": e.place_id, "members": [e]}
        )  # fmt: skip
    for b in blocks:
        b["latest"] = _window_latest(b["members"])
    return blocks


def _kind_fields(entry: Entry) -> dict[str, Any]:
    """On a plan's event item: its kind, who said so, and a window's minimum stay."""
    fields: dict[str, Any] = {"event_kind": entry.event_kind, "kind_source": entry.kind_source}
    if entry.event_kind == "window":
        fields["min_stay_min"] = entry.min_stay
    return fields


def _window_latest(members: list[Entry]) -> datetime | None:
    """A block of window events only (a festival; two markets overlapping) may be reached any time up
    to the latest moment that still leaves each its minimum stay; anything fixed in it makes it fixed."""
    if not members or any(m.kind != "event" or m.event_kind != "window" or m.end is None for m in members):
        return None
    return min(m.end - timedelta(minutes=m.min_stay or 0) for m in members if m.end is not None)


def _jsonable(value: Any, tz: ZoneInfo) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v, tz) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v, tz) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(tz).isoformat()
    return value
