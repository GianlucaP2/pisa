"""Re-planning on request: options, applying one, adjustments and undo.

For a target item (or the rest of today) Pisa solves a few variants of the day with the planner, each a whole
day plan with its effects, and offers at most three, best first:
- `now`: go there first (from where the user is); `after_next`: after the next fixed stop;
- `alternative`: up to two similar places (the same category as the item's place, or restaurants and cafés for
  a meal) within 800 m of the straight line from here to the next fixed stop, or within 10 minutes of travel
  from either end; open for the whole slot (when their hours are known); rated 4.0 or more (when known);
- `shorten` (meals only, 30 minutes at least); `skip`;
- with no target (`reorganize`): the rest of the day as it is from here, skipping one of the next events, or a
  late meal after the next stop.
- `fit` with no way to keep the item at its place on time adds the `alternative` options.
Meals move only inside their window; anything else inside the planner day. Each variant has 0.5 s of solver
time; the whole call (gathers, searches, solves) 2 s, with `skip` solved first so it is always offered.
`dropped` lists what a variant skips, the events it can't reach before they end, and the tasks the day with
the target skipped places and the variant doesn't. Options are kept 15 minutes; applying one stores its
changes as adjustments sharing an `option_ref` (Pisa's own plan; the Google calendar is never edited) and
re-plans; the options are consumed then. If the day changed since (an event, a task, an adjustment), the
options are stale: 409 plan_changed.
"""

import asyncio
import dataclasses
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.logging import get_logger
from pisa.planner.options import (
    MEAL_CATEGORIES,
    MEAL_MIN,
    MEAL_WINDOWS,
    Action,
    Candidate,
    Change,
    Effects,
    Goal,
    Variant,
    meal_of,
    rank,
    summary,
    why,
)
from pisa.ports.places import Coord
from pisa.services.places import PlacesService
from pisa.services.planner import (
    BASE_PREFIX,
    Built,
    Day,
    Entry,
    PlannerService,
    _ceil_minute,
    _estimate,
    _minutes,
    apply_changes,
)

OPTIONS_TTL = timedelta(minutes=15)
VARIANT_LIMIT_S = 0.5
OPTIONS_SOLVE_S = 2.0
CORRIDOR_M = 800  # a similar place this close to the straight line from here to the next stop
NEAR_END_MIN = 10  # ... or this many minutes of travel from either end
MIN_RATING = 4.0
MAX_ALTERNATIVES = 2
ALTERNATIVES_CHECKED = (
    4  # candidates whose opening hours are looked up (details cost money past the free tier)
)
Source = Literal["voice", "app", "check_in"]
InsertWhen = Literal["before", "after", "instead", "fits"]
_log = get_logger("pisa.plan_options")


class PlanOptionsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        planner: PlannerService,
        places: PlacesService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        write_through: Callable[..., Awaitable[tuple[str | None, str | None]]] | None = None,
        undo_through: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._pool = pool
        self._planner = planner
        self._places = places
        self._owner = owner
        self._clock = clock
        # an option the user chose moves the real Google event too
        # (event id, start, end, how the user chose: "tap" or "voice") → the approval's id or None; and its
        # undo with the plan's → what Pisa says, or None.
        # (event id, start, end, "tap" | "voice", invited=the count the option named, option_id=…) → (the
        # approval's id or None, what to tell the user about the calendar or None).
        self.write_through = write_through
        self.undo_through = undo_through

    # --- options ------------------------------------------------------------------------------------------

    async def options(self, target: dict[str, str] | None, goal: Goal = "fit") -> dict[str, Any]:
        now = self._clock()
        deadline = time.monotonic() + OPTIONS_SOLVE_S  # the whole call: gathers, searches, solves
        named = [target["event_id"]] if target and target.get("event_id") else []
        day = await self._planner.gather(now, include_events=named, all_pairs=True, budget_s=_left(deadline))
        entry = _find(day, target) if target else None
        if target and entry is None:
            raise PisaError("not_found", "That item isn't in today's plan.", "Ask for today's plan")
        if entry is None:
            goal = "reorganize"
        lang = day.settings["language"]
        # A task the plan placed has no time of its own: a skip shows it where the plan had it.
        times = await self._planned_times(day.win.day, entry) if entry is not None else (None, None)
        if goal == "alternative" and entry is not None:
            day, entry, similar = await self._with_similar(day, target, entry, now, named, deadline)
        else:
            similar = []
        nxt = _next_stop(day, entry)
        base = await self._planner.solve_day(
            day, day.events, day.tasks, VARIANT_LIMIT_S, rain_budget_s=_left(deadline)
        )
        solved = await self._solve(
            day, entry, nxt, _variants(day, entry, nxt, goal, similar, lang, times), base, None, deadline
        )
        if goal == "fit" and entry is not None and not any(_fits(c) for c, _ in solved) and _left(deadline):
            # No way to keep it at its place on time: similar places on the way, as for `alternative`.
            there, moved, similar = await self._with_similar(day, target, entry, now, named, deadline)
            if similar:
                after = _next_stop(there, moved)
                found = _variants(there, moved, after, "alternative", similar, lang, times)
                alternatives = [v for v in found if v[0] == "alternative"]
                reference = next((b for c, b in solved if c.variant == "skip"), base)
                solved += await self._solve(
                    there, moved, after, alternatives, base, reference, deadline, guaranteed=False
                )
        title = (
            entry.title if entry else ("Il resto della giornata" if lang == "it-IT" else "The rest of today")
        )
        body = await self._offer(day, title, [c for c, _ in solved], now)
        _log.info("plan_options", goal=goal, offered=len(body["options"]), solved=len(solved))
        return body

    async def _offer(
        self, day: Day, title: str, candidates: list[Candidate], now: datetime
    ) -> dict[str, Any]:
        """The best three, stored for 15 minutes under the day's fingerprint (apply checks it)."""
        best = rank(candidates)
        options_id = uuid.uuid4()
        tz = self._owner.zone
        invited = await self._invited([c for o in best for c in o.changes])
        lang = day.settings["language"]
        body = {
            "options_id": str(options_id),
            "expires_at": (now + OPTIONS_TTL).astimezone(tz).isoformat(),
            "target_title": title,
            "options": [
                {
                    "id": f"o{i + 1}",
                    "summary": _with_invited(c.summary, _moved_invited(c.changes, invited), lang),
                    "why": c.why,
                    "changes": [
                        _json(ch.json(), tz)
                        | ({"invited": invited.get(ch.event_id or "", 0)} if _moves(ch, invited) else {})
                        for ch in c.changes
                    ],
                    "effects": {
                        "late_min": c.effects.late_min,
                        "extra_travel_min": c.effects.extra_travel_min,
                        "dropped": c.effects.dropped,
                    },
                    "plan": c.plan,
                }
                for i, c in enumerate(best)
            ],
        }
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM plan_options WHERE expires_at <= %s", (now,))
            await conn.execute(
                "INSERT INTO plan_options (id, day, fingerprint, body, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (options_id, day.win.day, day.fingerprint, Jsonb(body), now, now + OPTIONS_TTL),
            )
        return body

    async def _invited(self, changes: list[Change]) -> dict[str, int]:
        """How many people each moved event invites (the synced count): an option that moves one says so,
        "e avviso 2 invitati", so choosing it is the one yes (choice 1)."""
        ids = sorted({c.event_id for c in changes if c.action == "move" and c.event_id})
        if not ids:
            return {}
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, coalesce(attendees, 0) FROM calendar_events WHERE id = ANY(%s)", (ids,)
                )
            ).fetchall()
        return {event_id: int(n) for event_id, n in rows if n}

    # --- "Plan it" with an intent -------------------------------------------------------------------

    async def insert(
        self,
        *,
        task_id: str | None,
        place_id: str | None,
        when: InsertWhen,
        relative_to: str | None,
        duration_min: int | None,
        source: Source = "app",
    ) -> dict[str, Any]:
        """POST /v1/plan/insert: a task (or a place, made a task) before / after / instead of an event, or
        when it fits. The planner places it on the way (the circuit: here → the stop → the event, never a
        round trip), moving what can move: a window event's arrival (a festival), never a fixed event. When
        it fits, it is pinned there (an adjustment, like any move) and the new plan comes back; when something
        fixed is in the way, options (the shape): go now (late for the event), after it, or instead of
        it."""
        if (task_id is None) == (place_id is None):
            raise PisaError(
                "validation_failed", "Give exactly one of task_id and place_id.", "Fix the request"
            )
        if duration_min is not None and not 5 <= duration_min <= 480:
            raise PisaError(
                "validation_failed",
                "The duration (duration_min) must be 5 to 480 minutes.",
                "Fix the request",
            )
        tasks = self._planner.tasks
        created = False
        if place_id is not None:
            title = await self._places.name(_uuid(place_id))
            task = await tasks.create(
                title=title, source_kind="manual", place_id=place_id, duration_min=duration_min
            )
            task_id, created = task.id, True
        elif duration_min is not None:
            await tasks.update(task_id or "", {"duration_min": duration_min})
        assert task_id is not None
        try:
            return await self._insert_stop(task_id, when, relative_to, source)
        except PisaError:
            if created:  # a refused stop leaves no task behind (each retry would add one)
                await tasks.delete(task_id)
            raise

    async def _insert_stop(
        self, task_id: str, when: InsertWhen, relative_to: str | None, source: Source
    ) -> dict[str, Any]:
        async with self._planner.locked():
            now = self._clock()
            deadline = time.monotonic() + OPTIONS_SOLVE_S
            named = [relative_to] if relative_to else []
            day = await self._planner.gather(
                now, include_events=named, all_pairs=True, budget_s=_left(deadline)
            )
            entry = _find(day, {"task_id": task_id})
            if entry is None:
                raise PisaError("not_found", "That task isn't open today.", "Check the task")
            target = _find(day, {"event_id": relative_to}) if relative_to else _next_stop(day, entry)
            if relative_to and target is None:
                raise PisaError("not_found", "That event isn't in today's plan.", "Ask for today's plan")
            if when != "fits" and target is None:
                raise PisaError(
                    "validation_failed", "There is no event later today to plan it around.", "Use when: fits"
                )
            first: list[Change] = []
            within: tuple[datetime, datetime] | None = None
            if target is not None and target.start is not None and target.end is not None:
                if when == "before":
                    within = (now.astimezone(UTC), _latest_arrival(target))
                elif when == "after":
                    within = (target.end, day.win.end.astimezone(UTC))
                elif when == "instead":
                    first = [_skip(target)]
                    within = (max(target.start, now.astimezone(UTC)), target.end)
            events, task_entries = apply_changes(day.events, day.tasks, first, day.places)
            task_entries = [
                dataclasses.replace(t, within=within) if t.key == task_id else t for t in task_entries
            ]
            built = await self._planner.solve_day(
                day, events, task_entries, OPTIONS_SOLVE_S, rain_budget_s=_left(deadline)
            )
            placed = built.solution.tasks.get(task_id)
            if placed is not None:
                base = day.win.start.astimezone(UTC)
                start, end = (base + timedelta(minutes=m) for m in placed)
                await self.add(day.win.day, [*first, _move(entry, start, end)], entry.title, source)
                _log.info("plan_inserted", when=when, fits=True)
            else:
                body = await self._insert_options(day, entry, target, now, deadline)
                _log.info("plan_inserted", when=when, fits=False, offered=len(body["options"]))
                return body
        return await self._planner.plan(reason="stop inserted")

    async def _insert_options(
        self, day: Day, entry: Entry, target: Entry | None, now: datetime, deadline: float
    ) -> dict[str, Any]:
        """Something fixed is in the way: go there now (and be late for the event), after the event, or
        instead of it; each solved and costed like the options."""
        lang = day.settings["language"]
        origin = 0 if day.origin is not None else None
        variants: list[VariantSpec] = []
        for timing in ("now", "after_next"):
            slot = _slot(day, entry, target, timing, entry.location, origin)  # type: ignore[arg-type]
            if slot is not None:
                label = summary(timing, lang, place=entry.place_name, after=target.title if target else None)  # type: ignore[arg-type]
                variants.append((timing, [_move(entry, *slot)], label, None, False))  # type: ignore[arg-type]
        if target is not None:
            slot = _slot(day, entry, None, "now", entry.location, origin)
            changes = [_skip(target)] + ([_move(entry, *slot)] if slot is not None else [])
            variants.append(("skip", changes, summary("skip", lang, title=target.title), None, False))
        base = await self._planner.solve_day(
            day, day.events, day.tasks, VARIANT_LIMIT_S, rain_budget_s=_left(deadline)
        )
        solved = await self._solve(day, entry, target, variants, base, None, deadline)
        return await self._offer(day, entry.title, [c for c, _ in solved], now)

    async def _with_similar(
        self,
        day: Day,
        target: dict[str, str] | None,
        entry: Entry,
        now: datetime,
        named: list[str],
        deadline: float,
    ) -> tuple[Day, Entry, list[str]]:
        """The day again with similar places in its travel matrix (unchanged when there are none)."""
        try:
            similar = await asyncio.wait_for(
                self._similar(day, entry, _next_stop(day, entry)), _left(deadline)
            )
        except TimeoutError:
            _log.info("plan_options_search_failed", cause="time_limit")
            return day, entry, []
        if not similar:
            return day, entry, []
        again = await self._planner.gather(
            now, extra_places=similar, include_events=named, all_pairs=True, budget_s=_left(deadline)
        )
        found = _find(again, target)
        return (again, found, similar) if found is not None else (day, entry, [])

    async def _solve(
        self,
        day: Day,
        entry: Entry | None,
        nxt: Entry | None,
        variants: list[tuple[Variant, list[Change], str, datetime | None, bool]],
        base: Built,
        reference: Built | None,
        deadline: float,
        *,
        guaranteed: bool = True,
    ) -> list[tuple[Candidate, Built]]:
        """Each variant solved and costed against `base` (the day as it is). Tasks dropped are those the
        `reference` places and the variant doesn't: the day with the target skipped, so a task that fitted
        only through the base's lateness isn't reported. `skip` is solved first, always (`guaranteed`:
        the one variant solved past the deadline, feasible or not); the others while the time lasts."""
        lang = day.settings["language"]
        out: list[tuple[Candidate, Built]] = []
        for n, (variant, changes, label, meal_start, hours_unknown) in enumerate(
            sorted(variants, key=lambda v: v[0] != "skip")
        ):
            first = guaranteed and n == 0
            if not first and time.monotonic() >= deadline:
                break  # 2 s in all: the variants solved so far are the options
            events, tasks = apply_changes(day.events, day.tasks, changes, day.places)
            limit = VARIANT_LIMIT_S if first else min(VARIANT_LIMIT_S, _left(deadline))
            # No rain pass per variant: the base plan's modes, its transit legs included;
            # a new walk over 45 minutes takes an estimated transit time, no wait.
            plain = dataclasses.replace(
                day, rain=[], matrix=base.matrix or day.matrix, switch_mode=base.switch_mode
            )
            built = await self._planner.solve_day(plain, events, tasks, limit, rain_budget_s=0)
            self._planner.with_rain_modes(
                day, built, built.matrix or base.matrix, base.transit_legs | built.transit_legs
            )
            if built.solution.status not in ("optimal", "feasible"):
                continue
            if reference is None:
                reference = built if variant == "skip" and entry is not None else base
            effects = _effects(day, base, reference, built)
            arrival, late = _next_arrival(day, built, nxt)
            sentence = why(
                lang,
                self._owner.tz,
                next_title=nxt.title if nxt else None,
                next_arrival=arrival,
                next_late_min=late,
                effects=effects,
                mode=day.settings["places"]["default_mode"],
                hours_unknown=hours_unknown,
            )
            plan = self._planner.assemble(day, built)
            out.append((Candidate(variant, changes, effects, plan, label, sentence, meal_start), built))
        return out

    async def apply(
        self, options_id: str, option_id: str, source: Source = "app", approved: str | None = None
    ) -> dict[str, Any]:
        """Apply one option: under the plan lock, the options are consumed in the transaction that stores the
        option's changes, so a second apply of the same options (a double tap, voice and the app) finds them
        gone. Then a new plan.

        when the option's calendar part needs the user's yes, nothing is applied: the
        whole option waits for that one yes (`approved`: its approval, whose calendar move is done)."""
        try:
            key = uuid.UUID(options_id)
        except ValueError:
            key = None
        if approved is None and (held := await self._held(key, options_id, option_id, source)) is not None:
            plan = await self._planner.plan(reason="option waits for a yes")
            return plan | {"calendar": [held]}
        async with self._planner.locked():
            now = self._clock()
            row = None
            if key is not None:
                async with self._pool.connection() as conn:
                    row = await (
                        await conn.execute(
                            "SELECT day, fingerprint, body, expires_at FROM plan_options WHERE id = %s",
                            (key,),
                        )
                    ).fetchone()
            if row is None or row[3] <= now:
                raise _expired()
            day, fingerprint, body, _ = row
            option = next((o for o in body["options"] if o["id"] == option_id), None)
            if option is None:
                raise PisaError(
                    "validation_failed",
                    f"There is no option {option_id!r} in these options.",
                    "Fix the request",
                )
            if await self._planner.fingerprint_for(day) != fingerprint:
                raise PisaError(
                    "plan_changed",
                    "The day changed since these options were made (an event, a task or an adjustment).",
                    "Ask for options again",
                )
            changes = [_with_times(_change(c), option["plan"]) for c in option["changes"]]
            invited = {c["event_id"]: int(c["invited"]) for c in option["changes"] if c.get("invited")}
            async with self._pool.connection() as conn, conn.transaction():
                consumed = await (
                    await conn.execute("DELETE FROM plan_options WHERE id = %s RETURNING id", (key,))
                ).fetchone()
                if consumed is None:
                    raise _expired()
                ref = await self._insert(conn, day, changes, body["target_title"], source)
        # Outside the plan lock: the sync after it re-plans.
        notes = await self._through(ref, changes, source, invited, option_id, approved)
        plan = await self._planner.plan(reason="option applied")
        return plan | {"calendar": notes} if notes else plan

    async def _held(
        self, key: uuid.UUID | None, options_id: str, option_id: str, source: Source
    ) -> str | None:
        """Whether the option's calendar part needs the user's yes first: then its one approval is made (the
        whole option rides on it) and what to tell them comes back; None: apply it now."""
        if self.write_through is None or key is None:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT body FROM plan_options WHERE id = %s AND expires_at > %s",
                                   (key, self._clock()))
            ).fetchone()  # fmt: skip
        option = next((o for o in (row[0]["options"] if row else []) if o["id"] == option_id), None)
        if option is None:
            return None  # apply() says what is wrong with it
        for c in option["changes"]:
            if c.get("action") != "move" or not c.get("event_id") or not c.get("start") or not c.get("end"):
                continue
            approval_id, note = await self.write_through(
                c["event_id"], datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"]),
                "tap" if source == "app" else "voice", invited=int(c.get("invited") or 0),
                option_id=option_id, check={"options_id": options_id, "option_id": option_id},
            )  # fmt: skip
            if approval_id is not None:
                return note
        return None

    async def undo_for_approval(self, approval_id: str) -> None:
        """The calendar change of an option was undone (from Changes): Pisa's plan goes back with it, the
        whole option (one Undo)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "DELETE FROM plan_adjustments WHERE option_ref IN "
                    "(SELECT option_ref FROM plan_adjustments WHERE approval_id = %s) RETURNING day",
                    (uuid.UUID(approval_id),),
                )
            ).fetchall()
        if rows and rows[0][0] == await self._planner.today():
            await self._planner.plan(reason="adjustment undone")

    async def _through(
        self,
        ref: uuid.UUID,
        changes: list[Change],
        source: Source,
        invited: dict[str, int],
        option_id: str,
        approved: str | None = None,
    ) -> list[str]:
        """Each event an option moved, moved in Google too (when it is the user's own), linked to its
        adjustment so undoing the option undoes both. A failure leaves the plan's change and is said: what the
        calendar did, for the user, one line per event that didn't simply move."""
        notes: list[str] = []
        if self.write_through is None:
            return notes
        for c in changes:
            if c.action != "move" or not c.event_id or c.start is None or c.end is None:
                continue
            if approved is not None and await self._approved_event(approved) == c.event_id:
                await self._link(ref, c.event_id, approved)  # already moved by the yes: its Undo is this one
                continue
            try:
                approval_id, note = await self.write_through(
                    c.event_id, c.start, c.end,
                    # Another event of an option the user said yes to: the yes named only the first one's
                    # people.
                    "approved_option" if approved is not None else "tap" if source == "app" else "voice",
                    invited=invited.get(c.event_id, 0), option_id=option_id,
                )  # fmt: skip
            except PisaError as err:
                _log.warning("write_through_failed", code=err.code)
                notes.append(f"The calendar event was not moved ({err.message}); only Pisa's plan changed.")
                continue
            if note:
                notes.append(note)
            if approval_id is not None:
                await self._link(ref, c.event_id, approval_id)
                continue
        return notes

    async def _link(self, ref: uuid.UUID, event_id: str, approval_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE plan_adjustments SET approval_id = %s WHERE option_ref = %s AND event_id = %s",
                (uuid.UUID(approval_id), ref, event_id),
            )

    async def _approved_event(self, approval_id: str) -> str | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT payload->>'event_id' FROM approvals WHERE id = %s", (uuid.UUID(approval_id),)
                )
            ).fetchone()
        return row[0] if row else None

    # --- adjustments --------------------------------------------------------------------------------------

    async def add(self, day: date, changes: list[Change], title: str, source: Source) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await self._insert(conn, day, changes, title, source)

    async def _insert(
        self, conn: AsyncConnection, day: date, changes: list[Change], title: str, source: Source
    ) -> uuid.UUID:
        """The changes of one option (or one skip) as adjustments sharing an `option_ref`: that ref."""
        now, ref = self._clock(), uuid.uuid4()
        for c in changes:
            await conn.execute(
                "INSERT INTO plan_adjustments (id, day, action, event_id, task_id, title, starts_at, "
                "ends_at, place_id, duration_min, source, created_at, option_ref) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(), day, c.action, c.event_id, c.task_id, title, c.start, c.end, c.place_id,
                    c.duration_min, source, now, ref,
                ),
            )  # fmt: skip
        return ref

    async def skip(self, target: dict[str, str], source: Source) -> dict[str, Any]:
        """Skip an item today (voice "saltalo", a check-in answer): an adjustment, then a new plan. Under the
        plan lock, like an option's apply: options made before the skip are then stale (409 plan_changed),
        never applied on top of it (queue)."""
        named = [target["event_id"]] if target.get("event_id") else []
        async with self._planner.locked():
            now = self._clock()
            day = await self._planner.gather(now, include_events=named)
            entry = _find(day, target)
            if entry is None:
                raise PisaError("not_found", "That item isn't in today's plan.", "Ask for today's plan")
            start, end = await self._planned_times(day.win.day, entry)
            change = _change_of(entry, "skip", start=start, end=end)
            await self.add(day.win.day, [change], entry.title, source)
        return await self._planner.plan(reason="item skipped")

    async def manual(self, target: dict[str, str], action: str, start: str | None) -> dict[str, Any]:
        """A move or a skip from the app (source app), then a new plan. A moved task keeps its
        duration, an event its length. It must start inside today's planner window, not in the past
        (03:00 would drop it from the plan, tomorrow would show it today), and end by the
        window's end."""
        if action == "skip":
            return await self.skip(target, "app")
        if not start:
            raise PisaError("validation_failed", "A move needs start (RFC 3339).", "Pick a time")
        try:
            moment = datetime.fromisoformat(start)
        except ValueError:
            raise PisaError(
                "validation_failed", "The start must be an RFC 3339 date-time.", "Fix the request"
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self._owner.zone)
        named = [target["event_id"]] if target.get("event_id") else []
        async with self._planner.locked():
            day = await self._planner.gather(self._clock(), include_events=named)
            entry = _find(day, target)
            if entry is None:
                raise PisaError("not_found", "That item isn't in today's plan.", "Ask for today's plan")
            begin = moment.astimezone(UTC)
            end = begin + timedelta(minutes=_length(day, entry))
            _check_start(begin, day.win.day_start, day.win.day_end, self._clock(), self._owner.tz)
            _check_end(end, day.win.day_end, self._owner.tz)
            change = _move(entry, begin, end)
            await self.add(day.win.day, [change], entry.title, "app")
        return await self._planner.plan(reason="item moved")

    async def stored(self, options_id: str) -> dict[str, Any]:
        """Pisa's options by id, as they were made, while they last: what
        a `plan` object's `options_id` names on the phone."""
        try:
            key = uuid.UUID(options_id)
        except ValueError:
            raise _expired() from None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT body FROM plan_options WHERE id = %s AND expires_at > %s", (key, self._clock())
                )
            ).fetchone()
        if row is None:
            raise _expired()
        return row[0]

    async def adjustments(self, day: date | None = None) -> list[dict[str, Any]]:
        """The day's adjustments in the order they were made; the changes of one option share `option_ref`."""
        day = day or await self._planner.today()
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, day, action, event_id, task_id::text, title, starts_at, ends_at, "
                    "place_id::text, duration_min, created_at, source, option_ref FROM plan_adjustments "
                    "WHERE day = %s ORDER BY seq",
                    (day,),
                )
            ).fetchall()
        out = []
        for (
            aid,
            d,
            action,
            event_id,
            task_id,
            title,
            start,
            end,
            place_id,
            minutes,
            created,
            source,
            ref,
        ) in rows:
            item: dict[str, Any] = {"id": str(aid), "date": d.isoformat(), "action": action}
            item |= {"event_id": event_id} if event_id else {"task_id": task_id}
            item |= {
                "title": title,
                "start": start.astimezone(tz).isoformat() if start else None,
                "end": end.astimezone(tz).isoformat() if end else None,
                "place_id": place_id,
                "duration_min": minutes,
                "created_at": created.astimezone(tz).isoformat(),
                "source": source,
                "option_ref": str(ref),
            }
            out.append(item)
        return out

    async def undo(self, adjustment_id: str) -> list[str]:
        """Undo an adjustment with the other changes of its option; the actions removed, in order."""
        try:
            key = uuid.UUID(adjustment_id)
        except ValueError:
            key = None
        rows: list[tuple[Any, ...]] = []
        if key is not None:
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "DELETE FROM plan_adjustments WHERE option_ref = "
                        "(SELECT option_ref FROM plan_adjustments WHERE id = %s) "
                        "RETURNING day, action, seq, approval_id",
                        (key,),
                    )
                ).fetchall()
        if not rows:
            raise PisaError(
                "not_found", f"There is no adjustment {adjustment_id[:40]!r}.", "Refresh the adjustments"
            )
        undone = [action for _, action, _, _ in sorted(rows, key=lambda r: r[2])]
        for approval in {r[3] for r in rows if r[3] is not None}:  # the Google event goes back too
            if self.undo_through is not None and (said := await self.undo_through(str(approval))):
                undone.append(f"({said})")
        if rows[0][0] == await self._planner.today():
            await self._planner.plan(reason="adjustment undone")
        return undone

    async def _planned_times(self, day: date, entry: Entry) -> tuple[datetime | None, datetime | None]:
        """Where an item is in the latest plan (to show it skipped there)."""
        if entry.start is not None:
            return entry.start, entry.end
        plan = await self._planner.current(day)
        for item in (plan or {}).get("items", []):
            if item.get("task_id") == entry.key:
                return datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
        return None, None

    # --- similar places -----------------------------------------------------------------------------------

    async def _similar(self, day: Day, entry: Entry, nxt: Entry | None) -> list[str]:
        """Up to four candidate places like the item's (their hours are then looked up), nearest to the way
        first. Searches go through the places service, its providers and the cap."""
        meal = meal_of(entry.title, entry.category)
        query = ("café" if meal == "breakfast" else "restaurant") if meal else entry.category
        if not query:
            return []
        here = day.origin or entry.coord
        end = nxt.coord if nxt and nxt.coord else None
        if here is None:
            return []
        center = ((here[0] + end[0]) / 2, (here[1] + end[1]) / 2) if end else here
        try:
            found = await self._places.search(query, near=center, limit=10)
        except PisaError as err:
            _log.info("plan_options_search_failed", cause=err.code)
            return []
        mode = day.settings["places"]["default_mode"]
        scored: list[tuple[float, str]] = []
        for p in found:
            if p["id"] == entry.place_id or p["lat"] is None or p["lon"] is None:
                continue
            category = p.get("category") or ""
            if meal and not MEAL_CATEGORIES.search(category):
                continue
            if not meal and category.casefold() != (entry.category or "").casefold():
                continue
            if p.get("rating") is not None and p["rating"] < MIN_RATING:
                continue
            spot = (p["lat"], p["lon"])
            off = _off_route(spot, here, end) if end else distance_m(spot, here)
            close = _estimate(here, spot, mode) <= NEAR_END_MIN or (
                end and _estimate(spot, end, mode) <= NEAR_END_MIN
            )
            if off <= CORRIDOR_M or close:
                scored.append((off, p["id"]))
        return [pid for _, pid in sorted(scored)[:ALTERNATIVES_CHECKED]]


# --- variants ---------------------------------------------------------------------------------------------


def _expired() -> PisaError:
    return PisaError(
        "options_expired",
        "These options have expired or were already applied (they are kept 15 minutes).",
        "Ask for options again",
    )


def _find(day: Day, target: dict[str, str] | None) -> Entry | None:
    if not target:
        return None
    if target.get("event_id"):
        return next((e for e in day.events if e.key == target["event_id"]), None)
    return next((t for t in day.tasks if t.key == target.get("task_id")), None)


def _next_stop(day: Day, entry: Entry | None) -> Entry | None:
    """The next fixed event after now: not the item, not skipped, not the day's end at the hotel."""
    now = day.now.astimezone(UTC)
    upcoming = [
        e
        for e in day.events
        if e.start
        and e.start > now
        and not e.skipped
        and e is not entry
        and not e.key.startswith(BASE_PREFIX)
    ]
    if entry is not None and entry.start is not None and entry.start > now:
        upcoming = [e for e in upcoming if e.key != entry.key]
    return min(upcoming, key=lambda e: e.start or now, default=None)


def _travel(day: Day, a: int | None, b: int | None) -> int:
    if a is None or b is None or a == b:
        return 0
    minutes = day.matrix.minutes[a][b]
    return minutes if minutes is not None else 0


VariantSpec = tuple[Variant, list[Change], str, datetime | None, bool]


def _variants(
    day: Day,
    entry: Entry | None,
    nxt: Entry | None,
    goal: Goal,
    similar: list[str],
    lang: str,
    times: tuple[datetime | None, datetime | None] = (None, None),
) -> list[VariantSpec]:
    """(variant, changes, summary, meal start, hours unknown) for each variant worth solving. `times`: where
    the target is in the latest plan (a task placed by the planner), so that skipping it keeps it there."""
    out: list[VariantSpec] = []
    if entry is None:  # the rest of the day around a delay
        out.append(("as_is", [], summary("as_is", lang), None, False))
        now = day.now.astimezone(UTC)
        upcoming = sorted((e for e in day.events if e.start and e.end and e.end > now and not e.skipped),
                          key=lambda e: e.start or now)  # fmt: skip
        for e in upcoming[:2]:
            out.append(("skip", [_skip(e)], summary("skip", lang, title=e.title), None, False))
        for e in upcoming:
            if meal_of(e.title, e.category):
                later = _slot(day, e, _next_stop(day, e), "after_next", e.location, 0 if day.origin else None)
                if later:
                    after = _next_stop(day, e)
                    out.append(
                        (
                            "after_next",
                            [_move(e, *later)],
                            summary("after_next", lang, after=after.title if after else ""),
                            later[0],
                            False,
                        )
                    )
                break
        return out
    origin = 0 if day.origin is not None else None
    if goal == "alternative":
        for pid in similar:
            info = day.places.get(pid, {})
            if info.get("location") is None:
                continue
            best = None
            for timing in ("now", "after_next"):
                slot = _slot(day, entry, nxt, timing, info["location"], origin)
                if slot and _open(info.get("windows"), *slot):
                    best = (timing, slot)
                    break
            if best is None:
                continue
            timing, (start, end) = best
            label = summary(timing, lang, place=info["name"], after=nxt.title if nxt else None)  # type: ignore[arg-type]
            changes = [_move(entry, start, end), _replace(entry, pid)]
            meal_start = start if meal_of(entry.title, entry.category) else None
            out.append(("alternative", changes, label, meal_start, info.get("windows") is None))
            if sum(1 for v in out if v[0] == "alternative") == MAX_ALTERNATIVES:
                break
        out.append(("skip", [_skip(entry, *times)], summary("skip", lang, title=entry.title), None, False))
        return out
    meal = meal_of(entry.title, entry.category)
    windows = day.places.get(entry.place_id or "", {}).get("windows")
    for timing in ("now", "after_next"):
        slot = _slot(day, entry, nxt, timing, entry.location, origin)
        if slot and _open(windows, *slot):
            label = summary(timing, lang, place=entry.place_name, after=nxt.title if nxt else None)  # type: ignore[arg-type]
            out.append((timing, [_move(entry, *slot)], label, slot[0] if meal else None, False))  # type: ignore[arg-type]
    if meal and nxt is not None and nxt.start is not None:
        slot = _slot(day, entry, nxt, "now", entry.location, origin)
        if slot:
            start, _ = slot
            leave = nxt.start - timedelta(
                minutes=_travel(day, entry.location, nxt.location) + day.settings["planner"]["buffer_min"]
            )
            minutes = _minutes(start, leave)
            full = _minutes(*slot)
            end = start + timedelta(minutes=minutes)
            if MEAL_MIN <= minutes < full and _open(windows, start, end):  # open for it, too
                changes = [_move(entry, start, end), _shorten(entry, minutes)]
                out.append(("shorten", changes, summary("shorten", lang, minutes=minutes), start, False))
    out.append(("skip", [_skip(entry, *times)], summary("skip", lang, title=entry.title), None, False))
    return out


def _slot(
    day: Day,
    entry: Entry,
    nxt: Entry | None,
    timing: Literal["now", "after_next"],
    location: int | None,
    origin: int | None = None,
) -> tuple[datetime, datetime] | None:
    """When the item would be, at `location`: going there now, or after the next stop. Meals stay in their
    window (at least 30 minutes); anything else inside the planner day, at its full length."""
    buffer = day.settings["planner"]["buffer_min"]
    if timing == "now":
        trip = _travel(day, origin, location)
        start = _ceil_minute(day.now.astimezone(UTC)) + timedelta(minutes=trip + (buffer if trip else 0))
    else:
        if nxt is None or nxt.end is None:
            return None
        trip = _travel(day, nxt.location, location)
        start = nxt.end + timedelta(minutes=trip + (buffer if trip else 0))
    length = _length(day, entry)
    meal = meal_of(entry.title, entry.category)
    if meal:
        tz = day.win.day_start.tzinfo
        local = start.astimezone(tz)
        opens, closes = MEAL_WINDOWS[meal]
        first = datetime.combine(local.date(), opens, tzinfo=tz).astimezone(UTC)
        last = datetime.combine(local.date(), closes, tzinfo=tz).astimezone(UTC)
        start = max(start, first)
        end = min(start + timedelta(minutes=length), last)
        if _minutes(start, end) < MEAL_MIN:
            return None
        return start, end
    end = start + timedelta(minutes=length)
    if end > day.win.end.astimezone(UTC):
        return None
    return start, end


def _length(day: Day, entry: Entry) -> int:
    if entry.kind == "event" and entry.start and entry.end:
        return max(1, _minutes(entry.start, entry.end))
    return entry.duration or day.settings["planner"]["default_task_min"]


def _open(windows: list[tuple[datetime, datetime]] | None, start: datetime, end: datetime) -> bool:
    """Open for the whole slot; unknown hours count as open (Apple gives none)."""
    if windows is None:
        return True
    return any(a.astimezone(UTC) <= start and end <= b.astimezone(UTC) for a, b in windows)


def _change_of(entry: Entry, action: Action, **fields: Any) -> Change:
    event_id = entry.key if entry.kind == "event" else None
    task_id = entry.key if entry.kind == "task" else None
    return Change(action, event_id, task_id, **fields)


def _move(entry: Entry, start: datetime, end: datetime) -> Change:
    return _change_of(entry, "move", start=start, end=end)


def _skip(entry: Entry, start: datetime | None = None, end: datetime | None = None) -> Change:
    """Skip it where it is: its own times, else where the latest plan had it (a task the planner placed)."""
    if entry.start is not None:
        start, end = entry.start, entry.end
    return _change_of(entry, "skip", start=start, end=end)


def _replace(entry: Entry, place_id: str) -> Change:
    return _change_of(entry, "replace_place", place_id=place_id)


def _shorten(entry: Entry, minutes: int) -> Change:
    return _change_of(entry, "shorten", duration_min=minutes)


def _effects(day: Day, base: Built, reference: Built, built: Built) -> Effects:
    """Late minutes at the fixed stops reached, travel against the day as it is, and what drops out:
    the items this variant skips; events it can't reach (a trip that doesn't fit) or reaches only at or
    after their end; tasks the reference places and this variant doesn't."""

    def travel(b: Built) -> int:
        return sum(leg.minutes for leg in b.solution.legs)

    def skipped(b: Built) -> set[str]:
        return {e.key for e in b.events + b.tasks if e.skipped}

    was_skipped = skipped(base)
    dropped = [e.title for e in built.events + built.tasks if e.skipped and e.key not in was_skipped]
    late = 0
    for block in built.blocks:
        there, minutes = _block_late(day, built, block)
        if block["key"] in built.solution.dropped or (there is not None and there >= block["end"]):
            dropped += [m.title for m in block["members"] if not m.key.startswith(BASE_PREFIX)]
        else:
            late += minutes
    placed = set(built.solution.tasks)
    dropped += [reference.facts[key]["task"].title for key in reference.solution.tasks if key not in placed]
    return Effects(late, travel(built) - travel(base), list(dict.fromkeys(dropped)))


def _fits(c: Candidate) -> bool:
    """The item at its own place, on time, nothing dropped."""
    return c.variant in ("now", "after_next", "shorten") and c.effects.late_min == 0 and not c.effects.dropped


def _latest_arrival(event: Entry) -> datetime:
    """Before an event: by its start, or for a window event (a festival) by its end minus the minimum
    stay, so the stop goes first and the arrival moves."""
    assert event.start is not None and event.end is not None
    if event.event_kind == "window":
        return max(event.start, event.end - timedelta(minutes=event.min_stay or 0))
    return event.start


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PisaError("not_found", f"There is no place with id {value[:40]!r}.", "Search again") from None


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _block_late(day: Day, built: Built, block: dict[str, Any]) -> tuple[datetime | None, int]:
    """When the user gets to a fixed block (without the buffer), and how many minutes after it starts (or
    after now, for one already under way)."""
    base = day.win.start.astimezone(UTC)
    on_time = max(block["start"], min(base, _ceil_minute(day.now.astimezone(UTC))))
    arrival = built.solution.arrivals.get(block["key"])
    if arrival is None:
        return on_time, 0
    leg = next((leg for leg in built.solution.legs if leg.to == block["key"]), None)
    there = base + timedelta(minutes=arrival) - timedelta(minutes=leg.buffer if leg else 0)
    return there, max(0, _minutes(on_time, there))


def _next_arrival(day: Day, built: Built, nxt: Entry | None) -> tuple[datetime | None, int]:
    """When the user reaches the next fixed stop in this variant, and how late."""
    if nxt is None or nxt.start is None:
        return None, 0
    block = next((b for b in built.blocks if any(m.key == nxt.key for m in b["members"])), None)
    if block is None:
        return None, 0
    there, late = _block_late(day, built, block)
    return (there if late else nxt.start), late


def _off_route(p: Coord, a: Coord, b: Coord) -> float:
    """Metres from p to the segment a-b (flat approximation: city distances)."""
    k = math.cos(math.radians(a[0]))
    ax, ay, bx, by, px, py = a[1] * k, a[0], b[1] * k, b[0], p[1] * k, p[0]
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return distance_m(p, (ay + t * dy, (ax + t * dx) / k))


def _change(c: dict[str, Any]) -> Change:
    parse = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
    return Change(
        c["action"], c.get("event_id"), c.get("task_id"), parse(c.get("start")), parse(c.get("end")),
        c.get("place_id"), c.get("duration_min"),
    )  # fmt: skip


def _with_times(change: Change, plan: dict[str, Any]) -> Change:
    """A skip keeps the item where the option's plan shows it (a task the planner placed has no time of its
    own, and the skip change carries none)."""
    if change.action != "skip" or change.start is not None:
        return change
    key = ("event_id", change.event_id) if change.event_id else ("task_id", change.task_id)
    item = next((i for i in plan.get("items", []) if i.get(key[0]) == key[1] and i.get("start")), None)
    if item is None:
        return change
    start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
    return dataclasses.replace(change, start=start, end=end)


INVITED = {"it-IT": ("e avviso 1 invitato", "e avviso {n} invitati"),
           "en-US": ("and notify 1 invited person", "and notify {n} invited people")}  # fmt: skip


def _moves(change: Change, invited: dict[str, int]) -> bool:
    return change.action == "move" and bool(change.event_id) and change.event_id in invited


def _moved_invited(changes: list[Change], invited: dict[str, int]) -> int:
    return sum(invited[c.event_id] for c in changes if _moves(c, invited) and c.event_id)


def _with_invited(summary: str, n: int, lang: str) -> str:
    """ "Sposta la cena alle 21" → "… e avviso 2 invitati": choosing it is the one yes (as amended)."""
    if not n:
        return summary
    one, many = INVITED.get(lang, INVITED["en-US"])
    return f"{summary.rstrip('.').rstrip()} {one if n == 1 else many.format(n=n)}"


def _json(value: dict[str, object], tz: ZoneInfo) -> dict[str, object]:
    return {k: v.astimezone(tz).isoformat() if isinstance(v, datetime) else v for k, v in value.items()}


def _check_start(begin: datetime, day_start: datetime, day_end: datetime, now: datetime, tz: str) -> None:
    """A move's start: inside the planner window, from this minute on (the app sends whole minutes)."""
    local = ZoneInfo(tz)
    this_minute = now.astimezone(UTC).replace(second=0, microsecond=0)
    if not day_start.astimezone(UTC) <= begin < day_end.astimezone(UTC):
        raise PisaError(
            "validation_failed",
            f"The start must be inside today's plan, from {day_start.astimezone(local):%H:%M} to "
            f"{day_end.astimezone(local):%H:%M} ({day_start.astimezone(local):%Y-%m-%d}).",
            "Pick a time in today's plan",
        )
    if begin < this_minute:
        raise PisaError(
            "validation_failed",
            f"The start {begin.astimezone(local):%H:%M} is in the past.",
            "Pick a time from now on",
        )


def _check_end(end: datetime, day_end: datetime, tz: str) -> None:
    """A move's end: by the planner window's end (a task at 20:45 for an hour would run past 21:00)."""
    if end.astimezone(UTC) > day_end.astimezone(UTC):
        local = ZoneInfo(tz)
        raise PisaError(
            "validation_failed",
            f"It would end at {end.astimezone(local):%H:%M}, after today's plan ends "
            f"({day_end.astimezone(local):%H:%M}).",
            "Pick an earlier start",
        )
