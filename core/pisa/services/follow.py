"""Following the day: item statuses from where the user was, check-ins, and the day so
far.

- **Statuses** are worked out when a plan is read (pisa.domain.follow): the stored plan says what was planned,
  the stays say what happened. The trip from here to an item's place is the straight-line estimate (no
  provider call: it is asked every minute).
- **Check-ins** (every minute, after the plan): at most one per item per day (again only after "on my way",
  15 minutes later and still missed), never two open at once, none during quiet hours (deferred: when they
  end, only what is still relevant is asked), and only when `planner.check_ins` is on. An open check-in that
  is no longer relevant (the user got there after all, the task was done, the day is over) is dropped.
- **Answers** (voice, a notification button, the app): `missed` → reschedule or alternative (the
  options), skip (an adjustment), on my way, did it; `confirm` → done (the task is completed) or not done.
- **Offers** ride a check-in too (the "Conosciamoci" chip): Pisa's notice carries a
  check-in id and its one choice, answered like any check-in (`POST /v1/check-ins/{id}/answer`). An offer is
  about no task or event, so it is no `check_ins` row (never in the day's list, never blocking a follow-up):
  its notice is the record and one `app_settings` value the open offer (`OFFER_KEY`), answered once, from the
  app only. Its answer starts what the offer's `path` names (`FollowService(offers=...)`).
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import patch_setting, set_setting
from pisa.domain import aging as aging_rules
from pisa.domain import follow, wrist
from pisa.domain import geo_rules as geo
from pisa.domain.attend import Candidate
from pisa.domain.decisions import Decision as Move
from pisa.domain.errors import PisaError
from pisa.domain.follow import CheckInKind, Item, Seen, Status
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.domain.stays import NEWEST
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services import foreign, guardian, stays
from pisa.services.attend import deliver_now
from pisa.services.places import LOCATION_FRESH, PlacesService
from pisa.services.plan_options import PlanOptionsService, Source
from pisa.services.planner import PlannerService, _estimate
from pisa.services.tasks import TasksService

if TYPE_CHECKING:
    from pisa.services.attend import AttendService

# RAIL: check-ins are rate-limited (one open at a time, none in quiet hours); whether to ask is
# code's for now.
REASK = timedelta(minutes=15)  # "on my way": asked again this much later if still missed
CHECK_IN_OPEN_MAX = timedelta(hours=1)  # unanswered this long: dropped, so it can't block the day's next ones
OFFER_KEY = "check_in_offer"  # the open offer riding a check-in: {id, path, answered_at, answer, …}
_log = get_logger("pisa.follow")

# What an offer's answer starts, by the offer's path (the device that answered, or None).
OfferStart = Callable[[uuid.UUID | None], Awaitable[object]]


async def open_offer(conn: AsyncConnection, path: str) -> uuid.UUID:
    """An offer about to ride a check-in: it is the open one now (a newer one closes it). The caller enqueues
    its notice, in the same transaction, with this check-in id, its choices and this `path`."""
    check_in_id = uuid.uuid4()
    await set_setting(conn, OFFER_KEY, Jsonb({
        "id": str(check_in_id), "path": path, "answered_at": None, "answer": None, "answer_origin": None,
    }))  # fmt: skip
    return check_in_id


class FollowService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        planner: PlannerService,
        options: PlanOptionsService,
        tasks: TasksService,
        places: PlacesService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attend: "AttendService | None" = None,
        offers: Mapping[str, OfferStart] | None = None,
    ) -> None:
        self._pool = pool
        self._planner = planner
        self._options = options
        self._tasks = tasks
        self._places = places
        self._owner = owner
        self._clock = clock
        self._attend = attend  # the check-in's notice is a candidate (or today's rules at once)
        self._offers = dict(offers or {})  # an offer's path → what its answer starts

    # --- statuses -----------------------------------------------------------------------------------------

    async def overlay(self, plan: dict[str, Any] | None) -> dict[str, Any] | None:
        """The plan with each event's and task's status as of now."""
        if plan is None:
            return None
        found = await self.statuses(plan)
        items = []
        for item in plan.get("items", []):
            key = item.get("event_id") or item.get("task_id")
            if item.get("kind") in ("event", "task") and item.get("status") != "skipped" and key in found:
                item = {**item, "status": found[key]}
            items.append(item)
        return {**plan, "items": items}

    async def statuses(self, plan: dict[str, Any]) -> dict[str, Status]:
        now = self._clock()
        tz = self._owner.zone
        day = date.fromisoformat(plan["date"])
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        past = day < await self._planner.today()
        if past:  # a past day is judged as it ended, not from where the user is now
            now = min(now, datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz))
        items = [
            i
            for i in plan.get("items", [])
            if i.get("kind") in ("event", "task") and i.get("status") != "skipped"
        ]
        place_ids = list({i["place_id"] for i in items if i.get("place_id")})
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            rows = await (
                await conn.execute(
                    "SELECT id::text, lat, lon FROM places WHERE id = ANY(%s::uuid[]) AND lat IS NOT NULL",
                    (place_ids,),
                )
            ).fetchall()
            seen = [
                Seen(s["arrived_at"], s["departed_at"], s["lat"], s["lon"], s["accuracy_m"], s["place_id"])  # type: ignore[arg-type]
                for s in await stays.between(conn, midnight, now)
            ]
            done = {
                key
                for (key,) in await (
                    await conn.execute(
                        "SELECT coalesce(event_id, task_id::text) FROM check_ins WHERE day = %s "
                        "AND answer IN ('did_it', 'done') UNION SELECT id::text FROM tasks "
                        "WHERE status = 'done' AND id::text = ANY(%s) UNION SELECT id FROM calendar_events "
                        "WHERE attended_at IS NOT NULL AND id = ANY(%s)",
                        (
                            day,
                            [i["task_id"] for i in items if i.get("task_id")],
                            [i["event_id"] for i in items if i.get("kind") == "event" and i.get("event_id")],
                        ),
                    )
                ).fetchall()
            }  # a check-in's answer, a completed task, an attended event
        coords = {pid: (lat, lon) for pid, lat, lon in rows}
        located = await self._here(now, at_end=past)
        here, accuracy = located if located else (None, 0.0)
        mode = settings["places"]["default_mode"]
        out: dict[str, Status] = {}
        for i in items:
            key = i.get("event_id") or i.get("task_id")
            if key in done:
                out[key] = "done"
                continue
            place = coords.get(i.get("place_id") or "")
            item = Item(
                i["kind"],
                datetime.fromisoformat(i["start"]),
                datetime.fromisoformat(i["end"]),
                i.get("place_id"),
                place,
            )
            trip = _estimate(here, place, mode) if here and place else None
            out[key] = follow.status(
                item, seen, now=now, here=here, trip_min=trip, day_start=midnight, here_accuracy_m=accuracy
            )
        return out

    async def _here(self, now: datetime, *, at_end: bool = False) -> tuple[tuple[float, float], float] | None:
        """Where the user is, precise enough to say "away": the latest fix
        when it is at most 15 minutes old with an accuracy of 150 m or better, else the ongoing stay; with its
        accuracy. None otherwise: an imprecise location is no location. For a past day (`at_end`, `now` its
        end) it is where the user was as the day ended: the last such fix before then, or a stay over it."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT lat, lon, accuracy_m, measured_at FROM device_locations "
                    "WHERE measured_at <= %s AND accuracy_m <= %s ORDER BY measured_at DESC LIMIT 1",
                    (now + NEWEST, geo.PLACE_ACCURACY_MAX_M),
                )
            ).fetchone()
            if row is not None and now - row[3] <= LOCATION_FRESH:
                return (row[0], row[1]), row[2]
            if at_end:
                row = await (
                    await conn.execute(
                        "SELECT lat, lon, accuracy_m FROM stays WHERE arrived_at <= %s "
                        "AND (departed_at IS NULL OR departed_at >= %s) ORDER BY arrived_at DESC LIMIT 1",
                        (now, now),
                    )
                ).fetchone()
                return ((row[0], row[1]), row[2]) if row else None
            staying = await stays.ongoing_stay(conn, now)
            if staying is None:
                return None
            newer = await (
                await conn.execute(
                    "SELECT 1 FROM device_locations WHERE measured_at > %s AND measured_at <= %s LIMIT 1",
                    (staying[3], now + NEWEST),
                )
            ).fetchone()
        # A newer fix, however imprecise, may mean the user left: the stay is no longer "here".
        return None if newer else ((staying[0], staying[1]), staying[2])

    # --- check-ins ----------------------------------------------------------------------------------------

    async def tick(self) -> int:
        """Ask about one missed or visited item, when the rules allow. Returns how many check-ins were
        made. One tick at a time: the worker's `check_ins` step, under its own lock (a minute's job can start
        while the last one runs); a second open check-in is also refused by the database."""
        now = self._clock()
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
        if not settings["planner"]["check_ins"]:
            return 0
        if is_quiet(now, settings, self._owner.tz):
            return 0  # deferred: what is still relevant when quiet ends is asked then
        today = await self._planner.today()
        plan = await self._planner.current(today)  # never a check-in about a task gone since the plan
        found = await self.statuses(plan) if plan else {}
        titles = {
            (i.get("event_id") or i.get("task_id")): i for i in (plan or {}).get("items", [])
            if i.get("kind") in ("event", "task")
        }  # fmt: skip
        async with self._pool.connection() as conn:
            open_rows = await (
                await conn.execute(
                    "SELECT id, day, kind, coalesce(event_id, task_id::text), created_at, purpose "
                    "FROM check_ins "
                    "WHERE answered_at IS NULL AND dropped_at IS NULL"
                )
            ).fetchall()
            still_open = 0
            for cid, day, open_kind, key, created, purpose in open_rows:
                # An aging question is about a task the plan left out: only its hour ends it.
                if now - created > CHECK_IN_OPEN_MAX or (purpose != "aging" and _settled(
                    open_kind, titles.get(key), found.get(key), day=day, today=today, now=now
                )):  # fmt: skip
                    await conn.execute("UPDATE check_ins SET dropped_at = %s WHERE id = %s", (now, cid))
                    # a notice deferred to a later time (attend's `at`) closes with it.
                    await conn.execute(
                        "UPDATE notifications SET delivered_via = 'none', delivered_at = %s "
                        "WHERE check_in_id = %s AND delivered_via IS NULL",
                        (now, cid),
                    )
                    _log.info("check_in_dropped", check_in_id=str(cid))
                else:
                    still_open += 1
            if still_open or plan is None:
                return 0
            asked = {
                key: (answer, reask)
                for key, answer, reask in await (
                    await conn.execute(
                        "SELECT DISTINCT ON (coalesce(event_id, task_id::text)) coalesce(event_id, "
                        "task_id::text), "
                        "answer, reask_at FROM check_ins WHERE day = %s "
                        "ORDER BY coalesce(event_id, task_id::text), created_at DESC",
                        (today,),
                    )
                ).fetchall()
            }
            candidates: list[tuple[str, str, CheckInKind]] = []
            for key, status in found.items():
                kind: CheckInKind | None = (
                    "missed"
                    if status == "missed"
                    else "confirm"
                    if status == "visited" and titles[key]["kind"] == "task"
                    else None
                )
                if kind is None:
                    continue
                if key in asked:
                    answer, reask = asked[key]
                    if not (
                        kind == "missed" and answer == "on_my_way" and reask is not None and reask <= now
                    ):
                        continue  # once per item per day
                candidates.append((titles[key]["start"], key, kind))
            if not candidates:
                return 0
            _, key, chosen = min(candidates)
            item = titles[key]
            lang = settings["language"]
            place = None
            if item.get("place_id"):
                row = await (
                    await conn.execute("SELECT name FROM places WHERE id = %s", (item["place_id"],))
                ).fetchone()
                place = row[0] if row else None
            text = follow.check_in_text(chosen, item["title"], place, lang)
            choices = follow.choices(chosen, lang)
            check_in_id = uuid.uuid4()
            try:
                await self._ask(conn, now, today, chosen, item, text, choices, lang, check_in_id)
            except UniqueViolation:  # another check-in opened meanwhile: never two at once
                return 0
        _log.info("check_in_asked", check_in_id=str(check_in_id), kind=chosen)
        return 1

    async def _ask(
        self,
        conn: AsyncConnection,
        now: datetime,
        today: date,
        chosen: CheckInKind,
        item: dict[str, Any],
        text: str,
        choices: list[dict[str, str]],
        lang: str,
        check_in_id: uuid.UUID,
        purpose: str | None = None,
    ) -> None:
        """The check-in and its notice (or its candidate), together or not at all: the notice's id is set on
        the check-in when it goes out; a check-in attend drops is dropped."""
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO check_ins (id, day, kind, event_id, task_id, title, text, choices, created_at, "
                "purpose) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    check_in_id, today, chosen, item.get("event_id"), item.get("task_id"), item["title"],
                    text, Jsonb(choices), now, purpose,
                ),
            )  # fmt: skip
            # a check-in on an item whose title is a stranger's is outside text (the model)
            candidate = Candidate(
                str(uuid.uuid4()), "check_in", "check_in", text, lang, now, alone=True, speak="check_in",
                outside=await foreign.item_foreign(conn, item),
            )  # fmt: skip
            delivery: dict[str, Any] = {"check_in_id": str(check_in_id), "choices": choices}
            # the check-in after a meeting with one of their
            # people carries that person, so their "always for Marta" reaches it
            if item.get("event_id") and (person := await guardian.event_person(conn, str(item["event_id"]))):
                delivery["person_id"] = str(person)
            if self._attend is not None:
                await self._attend.propose(conn, candidate, f"check_in:{check_in_id}", delivery)
            else:
                await deliver_now(conn, candidate, delivery, timezone=self._owner.tz)

    async def check_ins(
        self, status: Literal["open", "all"] = "open", day: date | None = None
    ) -> list[dict[str, Any]]:
        day = day or await self._planner.today()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    _SELECT
                    + " WHERE day = %s"
                    + (" AND answered_at IS NULL AND dropped_at IS NULL" if status == "open" else "")
                    + " ORDER BY created_at",
                    (day,),
                )
            ).fetchall()
        return [self._json(r) for r in rows]

    async def answer(
        self, check_in_id: str, choice: str, source: Source = "app", device_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        now = self._clock()
        try:
            key = uuid.UUID(check_in_id)
        except ValueError:
            key = None
        async with self._pool.connection() as conn:
            row = (await (await conn.execute(_SELECT + " WHERE id = %s", (key,))).fetchone()) if key else None
        if row is None and key is not None and source == "app":  # an offer: only the user's own tap takes it
            if (offered := await self._answer_offer(key, choice, now, device_id)) is not None:
                return offered
        if row is None:
            raise PisaError(
                "not_found", f"There is no check-in {check_in_id[:40]!r}.", "Refresh the check-ins"
            )
        record = self._json(row)
        if choice not in {c["id"] for c in record["choices"]}:
            names = ", ".join(c["id"] for c in record["choices"])
            raise PisaError("validation_failed", f"The choice must be one of: {names}.", "Fix the request")
        target = (
            {"event_id": record["event_id"]} if record.get("event_id") else {"task_id": record["task_id"]}
        )
        async with self._pool.connection() as conn:  # one answer wins, even two at once
            taken = await (
                await conn.execute(
                    "UPDATE check_ins SET answered_at = %s, answer = %s, reask_at = %s, answer_origin = %s "
                    "WHERE id = %s AND answered_at IS NULL AND dropped_at IS NULL RETURNING id",
                    (now, choice, now + REASK if choice == "on_my_way" else None, wrist.current(), key),
                )
            ).fetchone()
        if taken is None:
            raise PisaError(
                "check_in_answered",
                "This check-in was already answered or is no longer open.",
                "Refresh the plan",
            )
        plan: dict[str, Any] | None = None
        options: dict[str, Any] | None = None
        if choice in ("reschedule", "alternative"):
            options = await self._options.options(target, "fit" if choice == "reschedule" else "alternative")
        elif choice == "skip":
            plan = await self._options.skip(target, "check_in")
        elif choice == "not_done" and await self._kept(key, record.get("task_id"), now):
            pass  # "non ancora" to "è ancora da fare?": it stays, back in the plan
        elif choice in ("did_it", "done", "not_done"):
            if choice != "not_done" and record.get("task_id"):
                await self._tasks.update(record["task_id"], {"status": "done"})
            plan = await self._planner.plan(reason="check-in answered")
        _log.info("check_in_answered", check_in_id=check_in_id, choice=choice, source=source)
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_SELECT + " WHERE id = %s", (key,))).fetchone()
        assert row is not None
        return {"check_in": self._json(row), "plan": await self.overlay(plan), "options": options}

    async def _answer_offer(
        self, key: uuid.UUID, choice: str, now: datetime, device_id: uuid.UUID | None
    ) -> dict[str, Any] | None:
        """An offer's check-in answered (None: no offer has this id). Its notice says what was asked; the open
        offer is taken once (an earlier offer, closed by a newer one, is no longer open)."""
        async with self._pool.connection() as conn:
            sent = await (
                await conn.execute(
                    "SELECT path, text, choices, created_at FROM notifications WHERE check_in_id = %s "
                    "AND path = ANY(%s) ORDER BY created_at LIMIT 1",
                    (key, list(self._offers)),
                )
            ).fetchone()
            if sent is None:
                return None
            path, text, choices, created = sent
            choices = list(choices or [])
            if choice not in {c["id"] for c in choices}:
                names = ", ".join(c["id"] for c in choices)
                raise PisaError(
                    "validation_failed", f"The choice must be one of: {names}.", "Fix the request"
                )
            taken = await patch_setting(
                conn,
                OFFER_KEY,
                {"answered_at": now.isoformat(), "answer": choice, "answer_origin": wrist.current()},
                when={"id": str(key), "answered_at": None},
            )
        if taken is None:
            raise PisaError(
                "check_in_answered",
                "This check-in was already answered or is no longer open.",
                "Refresh the check-ins",
            )
        start = self._offers.get(str(path))
        if start is not None:
            await start(device_id)
        _log.info("check_in_answered", check_in_id=str(key), choice=choice, source="app", offer=path)
        tz = self._owner.zone
        created, answered = created.astimezone(tz), now.astimezone(tz)
        # The v1 check-in's kind is missed or confirm (the app decodes no other): an offer is Pisa's question
        # with a yes, a `confirm` about no task or event.
        return {
            "check_in": {
                "id": str(key), "date": created.date().isoformat(), "kind": "confirm",
                "title": choices[0]["label"], "text": text, "choices": choices,
                "created_at": created.isoformat(), "answered_at": answered.isoformat(), "answer": choice,
            },
            "plan": None,
            "options": None,
        }  # fmt: skip

    async def _kept(self, key: uuid.UUID | None, task_id: str | None, now: datetime) -> bool:
        """An aging question answered "not yet": the task is kept (back in the plan and the briefing)."""
        if task_id is None:
            return False
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE tasks SET kept_at = %s WHERE id = %s AND EXISTS (SELECT 1 FROM check_ins "
                    "WHERE id = %s AND purpose = 'aging') RETURNING id",
                    (now, uuid.UUID(task_id), key),
                )
            ).fetchone()
        return row is not None

    # --- tasks that move on ---------------------------------------------------------------------

    async def aging(self) -> int:
        """Every minute: an aged task (overdue for days, pisa.domain.aging) gets one "è ancora da fare?" (a
        check-in, done / not yet), one at a time; asked a week ago and unanswered, it is dismissed
        (restorable), its reason in Pisa's decisions. Returns the tasks asked or dismissed."""
        now = self._clock()
        changed = 0
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            lang = settings["language"]
            # a question that never reached the user (attend dropped it, a meeting, quiet)
            # counts for nothing: asked again later, never a week that started unseen.
            await conn.execute(
                "UPDATE tasks t SET aging_asked_at = NULL FROM check_ins c "
                "LEFT JOIN notifications n ON n.id = c.notification_id "
                "WHERE c.task_id = t.id AND c.purpose = 'aging' AND c.created_at >= t.aging_asked_at "
                "AND t.status = 'open' AND c.answered_at IS NULL AND c.dropped_at IS NOT NULL "
                "AND (n.id IS NULL OR n.delivered_at IS NULL OR n.delivered_via = 'none')"
            )
            # … and the week counts from when the question reached the user, not from when it was queued
            stale = await (
                await conn.execute(
                    "SELECT DISTINCT t.id::text, t.due_at, t.kept_at, t.updated_at > t.aging_asked_at "
                    "FROM tasks t JOIN check_ins c ON c.task_id = t.id AND c.purpose = 'aging' "
                    "AND c.created_at >= t.aging_asked_at JOIN notifications n ON n.id = c.notification_id "
                    "WHERE t.status = 'open' AND c.answered_at IS NULL AND n.delivered_via <> 'none' "
                    "AND n.delivered_at < %s AND (t.kept_at IS NULL OR t.kept_at < t.aging_asked_at)",
                    (now - aging_rules.DISMISS_AFTER,),
                )
            ).fetchall()
        for task_id, due_at, kept_at, touched in stale:
            if touched or not aging_rules.aged(due_at, kept_at, now):
                # the user restored, moved or changed it since the question: it is kept.
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE tasks SET kept_at = %s WHERE id = %s", (now, uuid.UUID(task_id))
                    )
                continue
            # a week without an answer to "ancora da fare?" means no; the user can restore
            # it.
            await self._tasks.update(task_id, {"status": "dismissed"}, label=False)
            async with self._pool.connection() as conn:
                await decision_log.store(
                    conn,
                    [Move("act", "aging", "pisa_initiative", ref=task_id,
                          purpose=aging_rules.gone_reason(lang))],
                    at=now, trace_id=None, device_id=None,
                )  # fmt: skip
            _log.info("task_aged_out", task_id=task_id)
            changed += 1
        if not settings["planner"]["check_ins"] or is_quiet(now, settings, self._owner.tz):
            return changed  # asked when check-ins are on and outside quiet hours, like the rest
        aged = await self._tasks.aged_ids()
        if not aged:
            return changed
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id::text, title, due_at FROM tasks WHERE id = ANY(%s) "
                    "AND (aging_asked_at IS NULL "
                    "OR aging_asked_at < kept_at) ORDER BY due_at LIMIT 1",
                    ([uuid.UUID(t) for t in aged],),
                )
            ).fetchone()
            if row is None:
                return changed
            task_id, title, due_at = row
            days = max(1, (now - due_at).days)
            check_in_id = uuid.uuid4()
            try:
                await self._ask(
                    conn, now, await self._planner.today(), "confirm", {"task_id": task_id, "title": title},
                    aging_rules.question(title, days, lang), follow.choices("confirm", lang), lang,
                    check_in_id,
                    purpose="aging",
                )  # fmt: skip
            except UniqueViolation:  # another check-in is open: the next minute
                return changed
            await conn.execute(
                "UPDATE tasks SET aging_asked_at = %s WHERE id = %s", (now, uuid.UUID(task_id))
            )
        _log.info("task_aging_asked", task_id=task_id, check_in_id=str(check_in_id))
        return changed + 1

    def _json(self, row: tuple[Any, ...]) -> dict[str, Any]:
        cid, day, kind, event_id, task_id, title, text, choices, created, answered, answer, _ = row
        tz = self._owner.zone
        out: dict[str, Any] = {"id": str(cid), "date": day.isoformat(), "kind": kind}
        out |= {"event_id": event_id} if event_id else {"task_id": task_id}
        return out | {
            "title": title,
            "text": text,
            "choices": choices,
            "created_at": created.astimezone(tz).isoformat(),
            "answered_at": answered.astimezone(tz).isoformat() if answered else None,
            "answer": answer,
        }

    # --- the day so far -----------------------------------------------------------------------------------

    async def day(self, day: date | None = None, *, spoken: bool = False) -> dict[str, Any]:
        """Their day: the stays and the plan's items. `spoken` ("cosa ho fatto oggi?"):
        a health, worship or politics stay is left out; only their direct question names one (`recall`)."""
        day = day or await self._planner.today()
        tz = self._owner.zone
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=tz)
        async with self._pool.connection() as conn:
            found = await stays.between(conn, midnight, midnight + timedelta(days=1))
        latest = await self._planner.current(day)
        if latest is None and day == await self._planner.today():
            latest = await self._planner.get(day)  # today's is made when there is none yet, as GET /v1/plan
        plan = await self.overlay(latest)
        items = [
            {"kind": i["kind"], "title": i["title"], "status": i.get("status", "planned")}
            | ({"event_id": i["event_id"]} if i["kind"] == "event" else {"task_id": i["task_id"]})
            for i in (plan or {}).get("items", [])
            if i.get("kind") in ("event", "task")
        ]  # fmt: skip
        return {
            "date": day.isoformat(),
            "stays": [
                {
                    "arrived_at": s["arrived_at"].astimezone(tz).isoformat(),  # type: ignore[attr-defined]
                    "departed_at": s["departed_at"].astimezone(tz).isoformat() if s["departed_at"] else None,  # type: ignore[attr-defined]
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "place_id": s["place_id"],
                    "name": s["name"],
                }
                for s in found
                if not (spoken and sensitive_place(s["name"], s["category"]))  # type: ignore[arg-type]
            ],
            "items": items,
        }


def _settled(
    kind: str, item: dict[str, Any] | None, status: Status | None, *, day: date, today: date, now: datetime
) -> bool:
    """An open check-in is dropped only on positive evidence that the question is moot: the day is over,
    the item left the plan, the user was there (or it is done or skipped), or it ended longer ago than it
    lasts. A stale location (status back to `planned`) is no evidence: the push stays answerable."""
    if day != today or item is None:
        return True
    if status in ("done", "skipped") or (kind == "missed" and status in ("ongoing", "visited")):
        return True
    start, end = item.get("start"), item.get("end")
    if not start or not end:
        return False
    begins, ends = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return now > ends + (ends - begins)


_SELECT = (
    "SELECT id, day, kind, event_id, task_id::text, title, text, choices, created_at, answered_at, answer, "
    "dropped_at FROM check_ins"
)
