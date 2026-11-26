"""The data behind the conversation agent's tools (AssistantData): agenda, tasks, and email search."""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from rapidfuzz import fuzz

from pisa.domain import cards, quarantine, visible
from pisa.domain import tasks_bulk as bulk_rules
from pisa.domain.cards import Cards
from pisa.domain.errors import PisaError
from pisa.domain.mail import EMAIL_REF_MIN, NEVER_TO_MODEL_LABELS, EmailHit
from pisa.domain.settings import is_quiet
from pisa.domain.tasks import AlreadyListed, Task, title_key, twin
from pisa.logging import get_logger
from pisa.planner.options import meal_of
from pisa.services import foreign, storyline_chat, storyline_views, storylines, things
from pisa.services.conversation import current_turn, turn_device
from pisa.services.done import DoneService
from pisa.services.follow import FollowService
from pisa.services.notifications import release_held
from pisa.services.plan_options import PlanOptionsService
from pisa.services.planner import PlannerService
from pisa.services.settings import SettingsService
from pisa.services.tasks import TasksService
from pisa.services.tasks_bulk import TasksBulkService

_MAX_QUERY_WORDS = 6
ITEM_MATCH = 80  # how close the user's words must be to an item's title (partial ratio, normalised)

LOW_EMAILS_SAID = 10  # the low list by voice: the newest emails only
PLAN_SEEN_MAX = 10  # the plan items the day context names (what is left of today)
SUBJECT_MAX = 120  # a subject as the planner sees it
_log = get_logger("pisa.assistant")


# the titles strangers control (pisa/services/foreign.py, one predicate): events in the
# window any of Pisa's lists can show (an agenda or a find
# reaches weeks out), and every email-made task (open, done or dismissed)
# and their places (a stranger's `location`) of 10 characters or more, long enough to be their words only
# by chance (a city's name alone would mark every list naming it)
FOREIGN_TITLES = (  # constant SQL fragments, no input
    f"SELECT x.t FROM {foreign.EVENTS} CROSS JOIN LATERAL (VALUES (e.title), "  # noqa: S608
    "(CASE WHEN char_length(e.location) >= 10 THEN e.location END)) x(t) "
    f"WHERE {foreign.FOREIGN_EVENT} AND x.t IS NOT NULL AND e.ends_at > %s "
    f"AND e.starts_at < %s UNION ALL SELECT t.title FROM tasks t WHERE {foreign.FOREIGN_TASK}"
)


class AssistantDataService:
    @property
    def timezone(self) -> str:
        """The user's zone now (the phone's, through the tasks service)."""
        return self._tasks.tz

    def __init__(
        self,
        pool: AsyncConnectionPool,
        tasks: TasksService,
        settings: SettingsService,
        brief: Callable[[], Awaitable[str]],
        planner: PlannerService,
        options: PlanOptionsService,
        follow: FollowService,
        done: DoneService,
        bulk: TasksBulkService,
    ) -> None:
        self._done = done
        self._planner = planner
        self._options = options
        self._follow = follow
        self._pool = pool
        self._tasks = tasks
        self._settings = settings
        self._brief = brief
        self._bulk = bulk  # the runtime's one bulk service (the ledger undoes through it too)
        # the plan_task calls of one round share one replan
        self._replan_lock = asyncio.Lock()
        self._asked_seq = self._planned_seq = 0
        self._last_plan: dict[str, Any] = {}
        # whether a timed reminder reaches the user (`phone_notify.Reach`: a push path, or the phone's own
        # notifications working), set by the runtime; None: not known here, taken as reaching (as before)
        self.reach: Callable[[str], Awaitable[bool]] | None = None

    def today(self) -> date:
        return self._tasks.owner.day(self._tasks.now())

    def now(self) -> datetime:
        return self._tasks.now()

    async def remember_to(
        self,
        what: str,
        condition: str,
        due_at: datetime | None,
        place_id: str | None,
        purpose: str | None,
        provenance: str,
    ) -> str:
        promise_id = uuid.uuid4()
        async with self._pool.connection() as conn:
            if place_id is not None:  # a place Pisa doesn't know is the tool's answer, not a failure
                try:
                    known = await (
                        await conn.execute("SELECT 1 FROM places WHERE id = %s", (uuid.UUID(place_id),))
                    ).fetchone()
                except ValueError:
                    known = None
                if known is None:
                    raise PisaError("not_found", "No place has that id.", "Use a place id from Pisa's tools")
            lang = await self._tasks.owner.lang(conn)
            await conn.execute(
                "INSERT INTO pisa_promises (id, what, condition, due_at, place_id, purpose, lang, "
                "provenance, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (promise_id, what[:200], condition, due_at, uuid.UUID(place_id) if place_id else None,
                 (purpose or "")[:120] or None, lang, provenance, self.now()),
            )  # fmt: skip
        return str(promise_id)

    async def promise(self, promise_id: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT p.what, p.condition, p.due_at, pl.name FROM pisa_promises p LEFT JOIN places pl "
                    "ON pl.id = p.place_id WHERE p.id = %s AND p.cancelled_at IS NULL",
                    (uuid.UUID(promise_id),),
                )
            ).fetchone()
        if row is None:
            return None
        return {"what": row[0], "condition": row[1], "due_at": row[2], "place": row[3]}

    async def reaches(self, promise_id: str) -> bool:
        """The timed reminder `promise_id` will reach the user at its time: a push path to a device of theirs,
        or their phone's own notifications working (it acked recently, notifications allowed) with the
        reminder in its schedule. False: they'll see it when they open the app."""
        if self.reach is None:
            return True
        try:
            return await self.reach(promise_id)
        except Exception as exc:  # can't tell: the honest side
            _log.warning("reminder_reach_unknown", cause=exc.__class__.__name__)
            return False

    async def live_promises(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT p.id, p.what, p.condition, p.due_at, pl.name FROM pisa_promises p "
                    "LEFT JOIN places pl ON pl.id = p.place_id "
                    "WHERE p.fired_at IS NULL AND p.cancelled_at IS NULL "
                    "ORDER BY p.created_at DESC LIMIT 20"
                )
            ).fetchall()
        return [
            {"id": str(r[0]), "what": r[1], "condition": r[2], "due_at": r[3], "place": r[4]} for r in rows
        ]

    async def cancel_promise(self, promise_id: str) -> bool:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE pisa_promises SET cancelled_at = %s WHERE id = %s AND fired_at IS NULL "
                    "AND cancelled_at IS NULL RETURNING id",
                    (self.now(), uuid.UUID(promise_id)),
                )
            ).fetchone()
        return row is not None

    async def tasks_bulk_match(
        self,
        which: str,
        *,
        days: int | None = None,
        source: str | None = None,
        ids: list[str] | None = None,
        sender: str | None = None,
    ) -> list[Task]:
        if which not in bulk_rules.FILTERS:
            return []
        return await self._bulk.match(cast(bulk_rules.Filter, which), days=days, source=source, ids=ids or (),
                                      sender=sender)  # fmt: skip

    async def tasks_bulk_apply(self, ids: list[str], action: str) -> dict[str, Any]:
        return await self._bulk.apply(ids, cast(bulk_rules.Action, action), origin="voice")

    async def thing(self, ref: str) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            found = await things.find(conn, ref)
            return await things.read(conn, found, self.timezone) if found is not None else None

    async def tasks_bulk_undo(self) -> dict[str, Any]:
        return await self._bulk.undo()

    async def set_quiet_until(self, until: datetime | None) -> str:
        await self._settings.patch({"quiet": {"until": until.isoformat() if until else None}})
        stored = (await self._settings.get())["quiet"].get("until")  # read back
        if stored:
            local = datetime.fromisoformat(str(stored)).astimezone(ZoneInfo(self.timezone))
            return f"Quiet mode is on until {local:%H:%M} ({local:%Y-%m-%d})."
        if until is not None:
            return "Not set: quiet mode's end isn't stored. Say so."
        released = await release_held(self._pool, now=self.now(), owner=self._tasks.owner)
        settings = await self._settings.get()
        if is_quiet(self.now(), settings, self.timezone):
            hours = settings["quiet"]["hours"]
            span = f"{hours['start']}-{hours['end']}"
            return f"Quiet until is cleared, but the quiet hours ({span}) still apply now."
        return "Quiet mode is off." + (" The held messages went out as one summary." if released else "")

    async def plan_task(self, task_id: str, title: str) -> dict[str, Any]:
        """ "mettilo oggi quando posso": a `plan` adjustment for today; the planner gives it a
        time. The only way Pisa times a task the user gave no time."""
        day = self.now().astimezone(ZoneInfo(self.timezone)).date()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO plan_adjustments (id, day, action, task_id, title, source, created_at, "
                "option_ref) VALUES (%s, %s, 'plan', %s, %s, 'voice', %s, %s)",
                (uuid.uuid4(), day, uuid.UUID(task_id), title[:200], self.now(), uuid.uuid4()),
            )
        self._asked_seq += 1
        mine = self._asked_seq
        async with self._replan_lock:
            if self._planned_seq >= mine:  # a replan that began after this one was asked holds it
                return self._last_plan
            upto = self._asked_seq
            self._last_plan = await self._planner.plan(reason="plan_task")
            self._planned_seq = upto
            return self._last_plan

    async def plan(self, *, fresh: bool = False) -> dict[str, Any]:
        plan = await (self._planner.plan(reason="asked by voice") if fresh else self._planner.get())
        return await self._follow.overlay(plan)  # type: ignore[return-value]

    # --- following the day --------------------------------------------------------------

    async def _target(self, item: str) -> dict[str, str]:
        """An item of today's plan from the user's words: its id, a meal ("pranzo"), or a title."""
        plan = await self._planner.get()
        items = await self._own_items(plan)
        text = item.strip()

        def target(i: dict[str, Any]) -> dict[str, str]:
            return {"event_id": i["event_id"]} if i["kind"] == "event" else {"task_id": i["task_id"]}

        for i in items:
            if text in (i.get("event_id"), i.get("task_id")):
                return target(i)
        meal = meal_of(text)
        if meal:
            meals = [i for i in items if meal_of(i["title"]) == meal]
            if meals:  # the next one that isn't over, else the last
                ahead = [i for i in meals if datetime.fromisoformat(i["end"]) > self.now()]
                return target((ahead or meals[-1:])[0])
        key = title_key(text)
        scored = sorted(
            ((fuzz.partial_ratio(key, title_key(i["title"])), i) for i in items), key=lambda s: -s[0]
        )
        good = [i for score, i in scored if score >= ITEM_MATCH and score == scored[0][0]]
        if not good:
            raise PisaError(
                "not_found", f"Nothing in today's plan matches {text[:60]!r}.", "Say it differently"
            )
        if len({i["title"] for i in good}) > 1:
            names = "; ".join(i["title"] for i in good)
            raise PisaError("validation_failed", f"Several items match: {names}.", "Say which one")
        return target(good[0])

    async def _own_items(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """The plan's events and tasks, one per event: a block of overlapping events ("Golfslag Playground /
        Pranzo") becomes each of its events with its own title and times, so "salta il pranzo" names the
        lunch, never the first event of the block."""
        items = [i for i in plan.get("items", []) if i.get("kind") in ("event", "task")]
        ids = [eid for i in items if i["kind"] == "event" for eid in i.get("event_ids") or [i["event_id"]]]
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, title, starts_at, ends_at FROM calendar_events WHERE id = ANY(%s)", (ids,)
                )
            ).fetchall()
        own = {eid: (title, start, end) for eid, title, start, end in rows}
        out: list[dict[str, Any]] = []
        for i in items:
            if i["kind"] != "event" or not i.get("event_ids") or len(i["event_ids"]) == 1:
                out.append(i)
                continue
            for eid in i["event_ids"]:
                title, start, end = own.get(eid, (i["title"], None, None))
                out.append(i | {"event_id": eid, "event_ids": [eid], "title": title or i["title"],
                                "start": start.isoformat() if start else i["start"],
                                "end": end.isoformat() if end else i["end"]})  # fmt: skip
        return out

    async def plan_options(self, item: str | None, goal: str) -> dict[str, Any]:
        target = await self._target(item) if item else None
        return _offered(await self._options.options(target, goal))  # type: ignore[arg-type]

    async def plan_stop(
        self, what: str | None, when: str, relative_to: str | None, place_id: str | None, minutes: int | None
    ) -> dict[str, Any]:
        """By voice ("pranzo prima del festival"): an open task by its title, or a place Pisa found, before
        / after / instead of an event of today (by its title), or when it fits. The new plan, or options."""
        task_id = None
        if place_id is None:
            if not what:
                raise PisaError("validation_failed", "Say what to plan: a task or a place.", "Say what")
            task_id = await self._open_task(what)
        event_id = None
        if relative_to and when != "fits":
            target = await self._target(relative_to)
            event_id = target.get("event_id")
            if event_id is None:
                raise PisaError("validation_failed", f"{relative_to[:60]!r} isn't an event.", "Name an event")
        out = await self._options.insert(
            task_id=task_id, place_id=place_id, when=when, relative_to=event_id,  # type: ignore[arg-type]
            duration_min=minutes, source="voice",
        )  # fmt: skip
        return _offered(out) if "options_id" in out else await self.plan_overlay(out)

    async def _open_task(self, what: str) -> str:
        """An open task from the user's words: its id, or the closest title."""
        open_tasks = await self._tasks.list("open")
        for t in open_tasks:
            if what.strip() == t.id:
                return t.id
        key = title_key(what)
        scored = sorted(
            ((fuzz.partial_ratio(key, title_key(t.title)), t) for t in open_tasks), key=lambda s: -s[0]
        )
        if not scored or scored[0][0] < ITEM_MATCH:
            raise PisaError("not_found", f"No open task matches {what[:60]!r}.", "Say it differently")
        return scored[0][1].id

    async def apply_option(self, options_id: str, option_id: str) -> dict[str, Any]:
        return await self.plan_overlay(await self._options.apply(options_id, option_id, "voice"))

    async def mark_done(self, what: str, pick: int | None = None) -> dict[str, Any]:
        return await self._done.mark(what, pick)

    async def skip_item(self, item: str) -> dict[str, Any]:
        return await self.plan_overlay(await self._options.skip(await self._target(item), "voice"))

    async def undo_adjustment(self, adjustment_id: str) -> str:
        options = self._options
        made = await options.adjustments()
        if adjustment_id.strip().casefold() == "last":
            if not made:
                raise PisaError("not_found", "Pisa hasn't changed today's plan.", "Nothing to undo")
            chosen = made[-1]
        else:
            chosen = next((a for a in made if a["id"] == adjustment_id.strip()), None)
            if chosen is None:
                raise PisaError(
                    "not_found", f"There is no adjustment {adjustment_id[:40]!r} today.", "Say which one"
                )
        actions = await options.undo(chosen["id"])  # with the other changes of its option
        return f"{' + '.join(actions)} {chosen['title']}"

    async def answer_check_in(self, check_in_id: str, choice: str) -> dict[str, Any]:
        out = await self._follow.answer(check_in_id, choice, "voice")
        if isinstance(out.get("options"), dict):
            _offered(out["options"])
        return out

    async def day(self, day: date) -> dict[str, Any]:
        return await self._follow.day(day, spoken=True)  # no sensitive stay unasked

    async def default_mode(self) -> str:
        async with self._pool.connection() as conn:
            return (await self._tasks.owner.settings(conn))["places"]["default_mode"]

    async def context(self) -> str | None:
        """The open check-in and the latest options still valid: what "la seconda" or "saltalo" means.
        And today as the user sees it in Pisa (Today's times, Pisa's moves), so a time question is answered
        from what they see."""
        lines: list[str] = []
        if today := await self.today_seen():
            lines.append(today)
        open_ = await self._follow.check_ins("open")
        for c in open_[-1:]:
            choices = ", ".join(f"{x['id']} ({x['label']})" for x in c["choices"])
            lines.append(f'Open check-in {c["id"]} about {c["title"]}: "{c["text"]}" Choices: {choices}.')
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT body FROM plan_options WHERE expires_at > %s ORDER BY created_at DESC LIMIT 1",
                    (self.now(),),
                )
            ).fetchone()
        if row is not None:
            body = row[0]
            listed = "; ".join(f"{o['id']} {o['summary']}" for o in body["options"])
            lines.append(f"Latest options {body['options_id']} for {body['target_title']}: {listed}.")
        if shown := (await self.storyline_cards()).text:  # what Pisa follows for them, as cards
            lines.append(shown)
        return "\n".join(lines) or None

    async def storyline_cards(self) -> Cards:
        """The storyline cards the model reads in place of raw mail: the open storylines that need the user or
        moved lately, at most five, each cut short. Never fails a turn."""
        try:
            async with self._pool.connection() as conn:
                return await storyline_views.build_cards(conn, self.now(), ZoneInfo(self.timezone))
        except Exception as exc:  # the cards are a help: a turn goes on without them
            _log.warning("storyline_cards_skipped", cause=exc.__class__.__name__)
            return Cards()

    async def storyline_progress(self, ref: str, kind: str, quote: str, words: str, turn: str) -> str:
        """The user's own word on a storyline (`services/storyline_chat`): what the tool says."""
        async with self._pool.connection() as conn, conn.transaction():
            owner = await storylines.owner_context(conn, self._tasks.owner)
            got = await storyline_chat.progress(
                conn, ref=ref, kind=kind, quote=quote, words=words, turn=turn, now=self.now(), owner=owner
            )
        return got.text

    async def foreign_titles(self) -> list[str]:
        """The titles others wrote that Pisa's lists may hold now (see `foreign_in`)."""
        now = self.now()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    FOREIGN_TITLES,
                    (
                        now - timedelta(days=foreign.TITLES_BEHIND_DAYS),
                        now + timedelta(days=foreign.TITLES_AHEAD_DAYS),
                    ),
                )
            ).fetchall()
        return [*(str(title) for (title,) in rows if title), *(await self.storyline_cards()).foreign]

    async def foreign_in(self, text: str) -> bool:
        """The text names a title someone else wrote: an event's
        a stranger controls (`foreign.FOREIGN_EVENT`, in the lists' window) or a task made from an email."""
        if not text:
            return False
        now = self.now()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    FOREIGN_TITLES,
                    (
                        now - timedelta(days=foreign.TITLES_BEHIND_DAYS),
                        now + timedelta(days=foreign.TITLES_AHEAD_DAYS),
                    ),
                )
            ).fetchall()
        if any(title and str(title) in text for (title,) in rows):
            return True
        # a card holds the other side's name, key and quote: outside text, as an email-made title is
        return cards.HEADER in text and any(s in text for s in (await self.storyline_cards()).foreign)

    async def today_seen(self) -> str | None:
        """The latest plan of today, the times the user sees in Today.
        What is over is left out."""
        tz = ZoneInfo(self.timezone)
        now = self.now()
        # as their lists are now: a plan made before a task was added, closed or deleted is made again
        plan = await self._planner.current(now.astimezone(tz).date())
        if plan is None:
            return None
        seen: list[str] = []
        for item in plan.get("items", []):
            if item.get("kind") not in ("event", "task") or not item.get("start") or not item.get("end"):
                continue
            start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
            if end <= now:
                continue
            line = f"{start.astimezone(tz):%H:%M}-{end.astimezone(tz):%H:%M} {item.get('title', '')}"
            if overlaps := item.get("overlaps"):
                # the user sees each event on its own, the overlap said ("la corsa alle 14,
                # durante l'attesa della spesa"). "during" only when it lies inside
                # the other; the wait that holds the run "overlaps" it.
                inside = [o for o in overlaps if _within(start, end, _span(plan.get("items", []), o))]
                around = [o for o in overlaps if o not in inside]
                for word, these in (("during", inside), ("overlaps", around)):
                    if these:
                        line += f" ({word} " + ", ".join(f"«{o['title']}»" for o in these) + ")"
            if moved := item.get("moved_from"):
                line += (
                    f" (moved by Pisa from {datetime.fromisoformat(moved).astimezone(tz):%H:%M}: the calendar"
                )
                line += " keeps that time)"
            seen.append(line)
        pool = _pool_line(plan, now, tz)
        if not seen and not pool:
            return None
        return (
            "Today in Pisa's plan, as the user sees it in Today: "
            + ("; ".join(seen[:PLAN_SEEN_MAX]) or "nothing")
            + "."
            + pool
        )

    async def open_check_in(self) -> str | None:
        open_ = await self._follow.check_ins("open")
        return str(open_[-1]["text"]) if open_ else None

    async def latest_check_in(self) -> dict[str, Any] | None:
        open_ = await self._follow.check_ins("open")
        return open_[-1] if open_ else None

    async def plan_overlay(self, plan: dict[str, Any]) -> dict[str, Any]:
        return await self._follow.overlay(plan)  # type: ignore[return-value]

    async def briefing(self) -> str:
        return await self._brief()

    async def agenda_day(self, day: date) -> dict[str, Any]:
        return (await self._tasks.agenda(day, 1))["days"][0]

    async def tasks(self, status: str) -> list[Task]:
        return await self._tasks.list(status)

    async def low_emails(self) -> list[dict[str, Any]]:
        from pisa.services.intake import quiet_inbox

        async with self._pool.connection() as conn:
            lang = await self._tasks.owner.lang(conn)
            return await quiet_inbox(conn, lang, LOW_EMAILS_SAID)

    async def add_task(
        self,
        title: str,
        due: str | None,
        place: str | None,
        priority: int | None,
        duration_min: int | None = None,
        start: str | None = None,
        *,
        from_outside: bool = False,
    ) -> Task:
        """`from_outside`: Pisa held it after outside text and they said yes: its title
        came from what Pisa read, so it is an email task (pisa/services/foreign.py), outside text where
        listed."""
        kind = None if not due else ("date" if len(due.strip()) == 10 else "datetime")
        device = turn_device()
        turn = current_turn.get()
        key = title_key(title)
        if turn is not None and key in turn.added:  # a continued turn already added it
            return await self._tasks.get(turn.added[key])
        # the same item open on their list is never added twice; the store says it is there
        if (same := twin(title, await self.tasks("open"))) is not None:
            raise AlreadyListed(same)
        task = await self._tasks.create(
            title=title,
            source_kind="email" if from_outside else "voice",
            source_ref=None if from_outside else (f"device:{device}" if device else None),
            due_at=due,
            due_kind=kind,
            place_text=place,
            priority=priority,
            duration_min=duration_min,
            start_at=start,
        )
        if turn is not None:
            turn.added[key] = task.id
            turn.done.append(f"added the task «{task.title}»")
        return task

    async def set_duration(self, task_id: str, minutes: int | None) -> Task:
        return await self._tasks.update(task_id, {"duration_min": minutes})

    async def complete_task(self, task_id: str) -> Task:
        return await self._tasks.update(task_id, {"status": "done"})

    async def update_task(self, task_id: str, changes: dict[str, Any], *, label: bool = True) -> Task:
        return await self._tasks.update(task_id, changes, label=label)

    async def delete_task(self, task_id: str) -> Task:
        """The user said to delete it: done at once, restorable for 24 h (no "Sicuro?")."""
        return await self._tasks.soft_delete(task_id)

    async def deleted_tasks(self) -> list[Task]:
        return await self._tasks.deleted_recently()

    async def undelete_task(self, task_id: str) -> Task:
        return await self._tasks.undelete(task_id)

    async def search_email(self, query: str, days: int, limit: int) -> list[EmailHit]:
        """Every word must appear in the sender, subject or preview. Promotions, social and spam are never
        returned: they are never shown to a model."""
        words = [w for w in re.findall(r"[^\W_]+", query.casefold()) if len(w) > 1][:_MAX_QUERY_WORDS]
        since = self._tasks.now() - timedelta(days=days)
        match = sql.SQL(
            "(subject ILIKE %s OR coalesce(from_name, '') ILIKE %s OR coalesce(from_address, '') ILIKE %s "
            "OR snippet ILIKE %s)"
        )
        statement = sql.SQL(
            "SELECT id, coalesce(from_name, from_address, ''), subject, sent_at, snippet FROM email_messages "
            "WHERE sent_at >= %s AND NOT (labels && %s) {} ORDER BY sent_at DESC LIMIT %s"
        ).format(sql.SQL(" ").join(sql.SQL("AND {}").format(match) for _ in words))
        params: list[Any] = [since, list(NEVER_TO_MODEL_LABELS)]
        for word in words:
            params += [f"%{word}%"] * 4  # letters and digits only: no LIKE wildcards
        params.append(limit)
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(statement, params)).fetchall()
        return [EmailHit(*row) for row in rows]

    async def email_fields(self, ref: str, lang: str) -> quarantine.Fields | None:
        found = await self.email_text(ref)
        if found is None:
            return None
        hit, body = found
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT m.from_address, coalesce(m.intake_gist, "
                    "(SELECT jsonb_build_object('what', e.quote) "
                    "FROM mail_event e WHERE e.message_id = m.id AND e.quote IS NOT NULL "
                    "ORDER BY e.created_at LIMIT 1)), coalesce(m.intake_keys, (SELECT "
                    "coalesce(jsonb_agg(jsonb_build_object('kind', CASE k.key_kind "
                    "WHEN 'order_id' THEN 'order' WHEN 'booking_ref' THEN 'booking' WHEN 'tracking' "
                    "THEN 'tracking' ELSE 'reference' END, 'value', k.verbatim_source)), '[]'::jsonb) "
                    "FROM entity_key k WHERE k.message_id = m.id)) FROM email_messages m WHERE m.id = %s",
                    (hit.id,),
                )
            ).fetchone()
        address, gist, keys = (row[0], row[1], row[2] or []) if row else (None, None, [])
        his = False  # one of their people is named by name alone; anyone else with the domain beside it
        if address:
            async with self._pool.connection() as conn:
                his = bool(
                    await (
                        await conn.execute(
                            "SELECT 1 FROM people p, unnest(p.emails) e WHERE lower(e) = lower(%s) LIMIT 1",
                            (address,),
                        )
                    ).fetchone()
                )
        text, _ = visible.plain(body)
        # the intake's one line: the first event's own sentence (`mail_event.quote`), or the gist an older
        # intake wrote; a voice
        # turn never waits on a reader: no event yet is the fields alone
        said = quarantine.gist_summary(gist)
        name = " ".join(visible.plain(hit.sender)[0].split())[: quarantine.SOURCE_MAX]
        sender = f"{name} <{address}>" if address and address != name else name
        return quarantine.read(
            "email", f"{hit.subject}\n{text}", source=quarantine.plain_source(hit.sender, address, known=his),
            sender=sender, sent=f"{hit.sent_at.astimezone(ZoneInfo(self.timezone)):%d %b %H:%M}",
            title=" ".join(visible.plain(hit.subject)[0].split())[:SUBJECT_MAX], summary=said,
            today=self.today(), keys=keys,
        )  # fmt: skip

    async def email_gists(self, ids: list[str]) -> dict[str, tuple[str, str | None]]:
        if not ids:
            return {}
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT m.id, coalesce(m.intake_gist->>'what', e.quote), m.from_address "
                    "FROM email_messages m LEFT JOIN LATERAL (SELECT quote FROM mail_event "
                    "WHERE message_id = m.id AND quote IS NOT NULL ORDER BY created_at LIMIT 1) e ON true "
                    "WHERE m.id = ANY(%s) "
                    "AND coalesce(m.intake_gist->>'what', e.quote) IS NOT NULL",
                    (ids,),
                )
            ).fetchall()
        return {str(i): (str(w), a) for i, w, a in rows}

    async def email_text(self, ref: str) -> tuple[EmailHit, str] | None:
        # the id as shown,
        # its brackets or quotes aside; its whole id at any length, a start of it only from EMAIL_REF_MIN
        ref = ref.strip().strip("[]«»\"'` ")
        if not re.fullmatch(r"[0-9A-Za-z_-]+", ref):
            return None
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, coalesce(from_name, from_address, ''), subject, sent_at, snippet, body "
                    "FROM email_messages WHERE (id = %s OR (%s AND id LIKE %s AND NOT EXISTS (SELECT 1 "
                    "FROM email_messages x WHERE x.id = %s))) AND NOT (labels && %s) "
                    "ORDER BY id = %s DESC LIMIT 2",
                    (
                        ref,
                        len(ref) >= EMAIL_REF_MIN,
                        ref.replace("_", "\\_") + "%",
                        ref,  # an exact id, even a hidden one, is never read as a start
                        list(NEVER_TO_MODEL_LABELS),
                        ref,
                    ),
                )
            ).fetchall()
        if rows and rows[0][0] == ref:  # its whole id: that email, even when another's id starts with it
            rows = rows[:1]
        if len(rows) != 1:  # none, or two: never one email's words for another's
            return None
        *hit, body = rows[0]
        return EmailHit(*hit), (body or hit[4] or "")


def _span(items: list[dict[str, Any]], other: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """The times of the plan item an `overlaps` entry names (by its event id, else its title)."""
    for key, value in (("event_id", other.get("event_id")), ("title", other.get("title"))):
        for i in items:
            if value and i.get(key) == value and i.get("start") and i.get("end"):
                return datetime.fromisoformat(i["start"]), datetime.fromisoformat(i["end"])
    return None


def _within(start: datetime, end: datetime, span: tuple[datetime, datetime] | None) -> bool:
    return span is not None and span[0] <= start and end <= span[1]


def _offered(out: dict[str, Any]) -> dict[str, Any]:
    """FACT: these options were offered by Pisa in this turn."""
    turn = current_turn.get()
    if turn is not None and "options_id" in out:
        turn.offered.append(str(out["options_id"]))
    return out


FREE_MIN = 30  # free this long now, Pisa may suggest one task of the pool


def _pool_line(plan: dict[str, Any], now: datetime, tz: ZoneInfo) -> str:
    """today's tasks with no time (the plan's `todo`, by deadline then priority, with the
    deadline's source), and when the user is free now: Pisa may suggest one ("hai un'ora libera: passi a
    prendere il bagnoschiuma?"), never put it in the plan by itself."""
    todo = plan.get("todo") or []
    if not todo:
        return ""
    listed = []
    for t in todo:
        due = ""
        if t.get("due_at"):
            day = datetime.fromisoformat(t["due_at"]).astimezone(tz)
            due = f" (by {day:%a %d %b}, from {t.get('deadline_source') or 'owner'})"
        listed.append(f"«{t['title']}»{due}")
    line = " Today's tasks with no time (the pool's head): " + "; ".join(listed) + "."
    timed = [
        (datetime.fromisoformat(i["start"]), datetime.fromisoformat(i["end"]))
        for i in plan.get("items", [])
        if i.get("kind") in ("event", "task") and i.get("start") and i.get("end")
    ]
    starts = [start for start, _ in timed if start > now]
    busy = any(start <= now < end for start, end in timed)
    until = min(starts) if starts else None
    free = int((until - now).total_seconds() // 60) if until else None
    if not busy and (free is None or free >= FREE_MIN):
        when = f"until {until.astimezone(tz):%H:%M} ({free} min)" if until and free is not None else "for now"
        line += (
            f" The user is free {when}: you may suggest one of them, in one line; never put one in the plan "
            "by yourself (only when they say so: plan_task)."
        )
    return line
