""" "the day as a story": the rituals and Pisa's questions that take one yes.

- **Settings** (`get`, `put`, `switch`): app_settings `rituals`; every ritual can be turned off in Settings
  (`PUT /v1/rituals`) and by voice (`set_ritual`).
- **The minute tick** (`tick`, the worker): the Ghost Run at its hour, Chiudiamo at their closing hour, the
  week's poster on Sunday at 19:00, something different on a free slot. Each at most once a day (the sheet's
  unique `(ritual, day)`, or the asks' log), never in quiet hours (their "basta notifiche" included: then
  nothing at all, never a held night message), as a notice (kind info) through the usual delivery (its
  rails: the voice rate, the push).
- **Sheets** (`sheet`, `act`): a ritual's notice names its sheet; the phone reads it (`GET /v1/sheets/{id}`)
  and posts a button (`POST /v1/sheets/{id}/actions/{action_id}`), the user's own act.
- **Pisa's questions** (`ask`, `answer`): the Ghost Run's fix, an idea, a hunch. A bare yes or no to the
  newest of them is decided by code (the conversation's yes chain, `answer_newest`), never by text Pisa read.
  Nothing changes without their yes. The user's words and locations are never logged.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import explore as explore_rules
from pisa.domain import profile as profile_rules
from pisa.domain import rituals as rules
from pisa.domain import streaks as streak_rules
from pisa.domain import weather as weather_rules
from pisa.domain.errors import PisaError
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.schedule import on_sunday
from pisa.domain.settings import in_span, is_quiet
from pisa.domain.sheet import SHEET_BLOCKS, RitualSheetData
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services.explore import where_now
from pisa.services.notifications import enqueue
from pisa.services.streaks import sessions as streak_sessions
from pisa.services.surprise import answered as surprise_answered
from pisa.services.unasked import Unasked

if TYPE_CHECKING:
    from pisa.services.actions import ActionsService
    from pisa.services.approvals import ApprovalsService
    from pisa.services.done import DoneService
    from pisa.services.explore import ExploreService
    from pisa.services.ledger import LedgerService
    from pisa.services.places import PlacesService
    from pisa.services.profile import ProfileService
    from pisa.services.tasks import TasksService
    from pisa.services.walks import WalksService
    from pisa.services.weather import WeatherService

_log = get_logger("pisa.rituals")
# The rituals' app_settings keys, owned here: others ask `rituals_of`, `ritual_on` or `stanotte_said`.
RITUALS_KEY = "rituals"  # their switches and times over `rules.DEFAULTS`
STANOTTE_OWNER_KEY = "stanotte_owner"  # their own last switch of the sheet (code never overrides it)
DECLINED_KINDS_KEY = "rituals_declined_idea_kinds"  # their "no" to an idea, remembered for that kind
GHOST_RAN_KEY = "rituals_ghost_run_ran"  # the day the last Ghost Run rehearsed (once, whatever it found)
GHOST_DEFERRED_KEY = "rituals_ghost_run_deferred"  # the day a Ghost Run due in quiet hours rehearses
LATE_MARGIN = timedelta(minutes=30)  # a deferred fix still worth asking: it starts at least this much later
IDEA_TRIED_KEY = "rituals_idea_tried"  # the day Pisa last looked for an idea: one search a day at most (cost)
IDEA_KEPT = timedelta(hours=6)
FIX_KEPT_MAX = timedelta(hours=20)
# the model's receipt for the knock or the tragitto opened in a turn; never its content
OPENED_RECEIPT = (
    "Opened: Pisa's code says it right after your reply. Say only a short ok, never what it holds."
)
Idea = dict[str, str]  # {kind, key, text}
IdeaSource = Callable[[set[str], set[str]], Awaitable[list[Idea]]]


async def rituals_of(conn: AsyncConnection) -> dict[str, dict[str, Any]]:
    """Their rituals over the defaults: the switches and times other services read."""
    return rules.with_defaults(await get_setting(conn, RITUALS_KEY))


async def ritual_on(conn: AsyncConnection, name: str) -> bool:
    """Whether their ritual (or Pisa's offer) `name` is on."""
    return bool((await rituals_of(conn))[name]["on"])


async def stanotte_said(conn: AsyncConnection) -> object | None:
    """Their own last switch of the sheet (None: they never said); code never overrides it."""
    return await get_setting(conn, STANOTTE_OWNER_KEY)


async def put_rituals(
    pool: AsyncConnectionPool, owner: Owner, patch: dict[str, Any], *, code: bool = False
) -> dict[str, dict[str, Any]]:
    """Their change to the rituals (Settings, their words) or, with `code`, Pisa's own (the Stanotte gate
    opening once): the rituals key's one writer, so the sheet's gate needs no RitualsService."""
    async with pool.connection() as conn, conn.transaction():
        current = await rituals_of(conn)
        try:
            updated = rules.apply_patch(current, patch)
        except ValueError as exc:
            raise PisaError("validation_failed", f"Rituals: {exc}.", "Fix the field") from None
        hours = (await owner.settings(conn))["quiet"]["hours"]
        for name, value in patch.items():  # a time it could never run at
            if (
                isinstance(value, dict)
                and "at" in value
                and hours is not None
                and in_span(rules.at_time(updated[name]["at"]), hours)
            ):
                raise PisaError(
                    "validation_failed",
                    f"Rituals: {name}.at is inside quiet hours ({hours['start']}-{hours['end']}): it "
                    f"would "
                    "never run.",
                    "Pick a time outside quiet hours",
                )
        await set_setting(conn, RITUALS_KEY, Jsonb(updated))
        # their own word on the sheet; code never overrides it
        if not code and "stanotte" in patch:
            await set_setting(conn, STANOTTE_OWNER_KEY, Jsonb(updated["stanotte"]))
    _log.info("rituals_set", rituals=",".join(sorted(patch)))
    return updated


class DayRehearsal:
    """The Ghost Run's readers of a day: its blocks (events and timed tasks) and the travel minutes between
    them. The rituals rehearse with them, and so do the CEO's prova generale and the dossier's "domani,
    provato", which need no more of the rituals."""

    def __init__(
        self, pool: AsyncConnectionPool, *, owner: Owner, places: "PlacesService | None" = None
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._places = places

    async def rehearse_blocks(self, day: date) -> tuple[list[rules.Block], dict[tuple[str, str], int]]:
        """The day's blocks and the minutes between consecutive ones with a
        place, the Ghost Run's own readers."""
        blocks = await self.day_blocks(day)
        travel, _ = await self.travel(blocks)
        return blocks, travel

    async def day_blocks(self, day: date) -> list[rules.Block]:
        """The day's blocks alone (no travel call), to re-check a rehearsal at reading time."""
        blocks, _ = await self.tomorrow(day)
        return blocks

    async def tomorrow(self, day: date) -> tuple[list[rules.Block], list[rules.DueTask]]:
        lo, hi = local_day_bounds(day, self._owner.tz)
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            events = await (
                await conn.execute(
                    "SELECT e.id, e.calendar_id, e.title, e.starts_at, e.ends_at, e.place_id::text, "
                    "coalesce(e.attendees, 0), c.access_role, p.name FROM calendar_events e "
                    "JOIN calendars c ON c.id = e.calendar_id LEFT JOIN places p ON p.id = e.place_id "
                    # an event Pisa made on their yes stays in tomorrow's plan (they must see it)
                    "WHERE NOT e.all_day AND e.status <> 'cancelled' AND coalesce(e.accepted, true) "
                    # a focus block, a working location or an out-of-office is no
                    # appointment: never a block that clashes
                    "AND coalesce(e.event_type, 'default') NOT IN ('focusTime', 'workingLocation', "
                    "'outOfOffice') "
                    "AND e.starts_at < %s AND e.ends_at > %s "
                    "ORDER BY e.starts_at",
                    (hi, lo),
                )
            ).fetchall()
            tasks = await (
                await conn.execute(
                    "SELECT t.id::text, t.title, t.due_at, t.due_kind, t.duration_min, t.expected_from, "
                    "t.expected_until, t.place_id::text, p.name FROM tasks t LEFT JOIN places p ON p.id = "
                    "t.place_id WHERE t.status = 'open' AND t.priority < 3 AND ((t.expected_from >= %s AND "
                    "t.expected_from < %s) OR (t.due_at >= %s AND t.due_at < %s)) ORDER BY t.due_at",
                    (lo, hi, lo, hi),
                )
            ).fetchall()
        blocks: list[rules.Block] = []
        seen: set[str] = set()
        for eid, cal, title, starts, ends, place_id, others, role, place in events:
            if eid in seen:
                continue
            seen.add(eid)
            blocks.append(
                rules.Block(
                    "event",
                    eid,
                    title,
                    starts.astimezone(tz),
                    ends.astimezone(tz),
                    place_id,
                    place,
                    movable=role == "owner" and others == 0,
                    calendar_id=cal,
                )
            )
        due: list[rules.DueTask] = []
        for tid, title, due_at, due_kind, minutes, start, until, place_id, place in tasks:
            length = timedelta(minutes=minutes or rules.TASK_BLOCK_MIN)
            if start is not None and lo <= start < hi:
                end = until if until is not None and until > start else start + length
                blocks.append(
                    rules.Block(
                        "task",
                        tid,
                        title,
                        start.astimezone(tz),
                        end.astimezone(tz),
                        place_id,
                        place,
                        movable=True,
                    )
                )
            elif due_at is not None:
                deadline = hi if due_kind == "date" else due_at
                due.append(
                    rules.DueTask(tid, title, deadline.astimezone(tz), minutes or rules.TASK_BLOCK_MIN)
                )
        return blocks, due

    async def travel(
        self, blocks: list[rules.Block]
    ) -> tuple[dict[tuple[str, str], int], dict[str, datetime]]:
        """Minutes between consecutive blocks with a place (the default mode); the leave-at times. A provider
        failure leaves that pair out (the Ghost Run never waits on one)."""
        travel: dict[tuple[str, str], int] = {}
        leave: dict[str, datetime] = {}
        if self._places is None:
            return travel, leave
        async with self._pool.connection() as conn:
            mode = str((await self._owner.settings(conn))["places"]["default_mode"])
        timed = sorted(blocks, key=lambda b: b.start)
        for a, b in pairwise(timed):
            if not (a.place_id and b.place_id) or a.place_id == b.place_id:
                continue
            try:
                origin = await self._places.coords(uuid.UUID(a.place_id))
                target = await self._places.coords(uuid.UUID(b.place_id))
                estimates, _, _ = await self._places.travel_between(origin, [target], mode)  # type: ignore[arg-type]
            except PisaError as err:
                _log.info("ghost_run_travel_skipped", cause=err.code)
                continue
            if estimates and estimates[0] is not None:
                minutes = int(estimates[0].best_minutes)
                travel[(a.ref, b.ref)] = minutes
                leave[b.ref] = b.start - timedelta(minutes=minutes)
        return travel, leave


class RitualsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        unasked: Unasked,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        tasks: "TasksService | None" = None,
        actions: "ActionsService | None" = None,
        approvals: "ApprovalsService | None" = None,
        done: "DoneService | None" = None,
        places: "PlacesService | None" = None,
        weather: "WeatherService | None" = None,
        ideas: IdeaSource | None = None,
        profile: "ProfileService | None" = None,
        rehearsal: DayRehearsal | None = None,
        ledger: "LedgerService | None" = None,
        walks: "WalksService | None" = None,
        explore: "ExploreService | None" = None,
        stanotte: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
        voice1: Any = None,
    ) -> None:
        self._profile = profile
        self.ledger = ledger  # Chiudiamo's action log
        self.walks = walks  # the end-of-walk recap
        self.explore = explore  # the glint, the Map of the Life
        # the Sunday card's "Stanotte" part (empty while the gate is shut)
        self.stanotte = stanotte
        self.voice1 = voice1  # `KnockService`: what the knock and the tragitto carry
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self.unasked = unasked
        self._tasks = tasks
        self._actions = actions
        self._approvals = approvals
        self._done = done
        self._places = places
        self.rehearsal = rehearsal or DayRehearsal(pool, owner=owner, places=places)
        self._weather = weather
        self._ideas = ideas
        # A question Pisa asked inside a turn (a hunch): {device: (ask id, that turn, the unasked sequence
        # then, the question's words)}
        self._in_turn: dict[uuid.UUID, tuple[uuid.UUID, object, int, str]] = {}
        self._code_says: dict[uuid.UUID, tuple[str, object]] = {}  # said after the reply

    def _local(self, moment: datetime | None = None) -> datetime:
        return (moment or self._clock()).astimezone(self._owner.zone)

    # --- settings --------------------------------------------------------------------------------------

    async def get(self, conn: AsyncConnection | None = None) -> dict[str, dict[str, Any]]:
        if conn is None:
            async with self._pool.connection() as own:
                return await rituals_of(own)
        return await rituals_of(conn)

    async def put(self, patch: dict[str, Any], *, code: bool = False) -> dict[str, dict[str, Any]]:
        """Their change (Settings, their words) or, with `code`, Pisa's own (the Stanotte gate opening
        once)."""
        return await put_rituals(self._pool, self._owner, patch, code=code)

    async def switch(self, ritual: str, on: bool, words: str) -> str:
        """By voice ("spegni il Ghost Run"): only on the user's own words this turn naming it (RAIL)."""
        if ritual not in rules.DEFAULTS:
            return f"Unknown ritual {ritual!r}: one of {', '.join(rules.NAMES)}."
        if not rules.said_switch(words, ritual, on):
            return "Not changed: a ritual is switched only when the user's own words this turn name it."
        await self.put({ritual: {"on": on}})
        now = (await self.get())[ritual]  # the ritual as stored, read back after the write
        at = f" at {now['at']}" if now.get("on") and now.get("at") else ""
        return f"{ritual} is now {'on' if now.get('on') else 'off'}{at}. Say it in a few words."

    # --- the minute tick -----------------------------------------------------------------------------------

    async def tick(self) -> list[str]:
        """Every minute (the worker): whatever ritual is due now. One failing never stops the others."""
        sent: list[str] = []
        async with self._pool.connection() as conn:
            settings = await self.get(conn)
            quiet = is_quiet(self._clock(), await self._owner.settings(conn), self._owner.tz)
        local = self._local()
        if quiet:
            # RAIL: no ritual in quiet hours, none held for later; the Ghost Run alone waits for their
            # first minute out of quiet, and goes then only if still relevant
            if settings["ghost_run"]["on"] and rules.due_at(
                local, rules.at_time(settings["ghost_run"]["at"])
            ):
                async with self._pool.connection() as conn:
                    day = (local.date() + timedelta(days=1)).isoformat()
                    await set_setting(conn, GHOST_DEFERRED_KEY, Jsonb(day))
            return sent
        async with self._pool.connection() as conn:
            deferred = await get_setting(conn, GHOST_DEFERRED_KEY)
            if deferred:
                await set_setting(conn, GHOST_DEFERRED_KEY, Jsonb(None))
        if deferred and date.fromisoformat(deferred) >= local.date():
            try:
                if await self.ghost_run(day=date.fromisoformat(deferred), late=True):
                    sent.append("ghost_run")
            except Exception:
                _log.exception("ritual_failed", ritual="ghost_run")
        jobs: list[tuple[str, Callable[[], Awaitable[object]]]] = []
        if settings["ghost_run"]["on"] and rules.due_at(local, rules.at_time(settings["ghost_run"]["at"])):
            jobs.append(("ghost_run", self.ghost_run))
        if settings["closing"]["on"] and rules.due_at(local, rules.at_time(settings["closing"]["at"])):
            jobs.append(("closing", self.closing))
        if (
            settings["week_poster"]["on"]
            and on_sunday(local.date())
            and rules.due_at(local, rules.WEEK_POSTER_AT)
        ):
            jobs.append(("week_poster", self.week_poster))
        if settings["something_different"]["on"] and settings["something_different"]["per_week"] > 0:
            jobs.append(("something_different", self.something_different))
        if settings["walk_recap"]["on"] and self.walks is not None:
            jobs.append(("walk_recap", self.walks.tick))
        if settings["life_map"]["on"] and self.explore is not None:
            explore = self.explore
            jobs.append(("life_map", lambda: explore.season_tick(self._store_sheet, self._notify)))
        for name, job in jobs:
            try:
                if await job():
                    sent.append(name)
            except Exception:
                _log.exception("ritual_failed", ritual=name)
        return sent

    # --- sheets --------------------------------------------------------------------------------------------

    async def _store_sheet(
        self,
        conn: AsyncConnection,
        ritual: str,
        day: date,
        data: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> uuid.UUID | None:
        """Kept once per ritual and day (None: already made today). The phone's actions, without effects."""
        shown = {
            **data,
            **(
                {"actions": [{k: a[k] for k in ("id", "label", "style")} for a in actions]} if actions else {}
            ),
        }
        RitualSheetData.model_validate(shown)  # the contract's limits, before anything is stored
        sheet_id = uuid.uuid4()
        row = await (
            await conn.execute(
                "INSERT INTO ritual_sheets (id, ritual, day, data, actions, created_at) VALUES "
                "(%s, %s, %s, %s, %s, %s) ON CONFLICT (ritual, day) DO NOTHING RETURNING id",
                (sheet_id, ritual, day, Jsonb(shown), Jsonb(actions), self._clock()),
            )
        ).fetchone()
        return sheet_id if row else None

    async def _refresh_sheet(
        self,
        conn: AsyncConnection,
        ritual: str,
        day: date,
        data: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> uuid.UUID:
        shown = {**data, **({"actions": [{k: a[k] for k in ("id", "label", "style")} for a in actions]}
                            if actions else {})}  # fmt: skip
        RitualSheetData.model_validate(shown)
        row = await (
            await conn.execute(
                "UPDATE ritual_sheets SET data = %s, actions = %s, updated_at = %s WHERE ritual = %s AND "
                "day = %s "
                "RETURNING id",
                (Jsonb(shown), Jsonb(actions), self._clock(), ritual, day),
            )
        ).fetchone()
        assert row is not None
        return row[0]

    async def sheet(self, sheet_id: str) -> dict[str, Any]:
        """The `present` object of a ritual's sheet (sheet; actions)."""
        key = _uuid(sheet_id)
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT data, created_at, actions, updated_at FROM ritual_sheets WHERE id = %s", (key,)
                )
            ).fetchone()
        if row is None:
            raise PisaError("not_found", "No such sheet (a ritual's sheet is kept 90 days).", "")
        data, created, actions, updated = row
        live = {a["id"] for a in actions if await self._live(a)}
        dead = sum(1 for a in actions if a["id"] not in live)
        shown = [a for a in data.get("actions") or [] if a["id"] in live]
        data = {k: v for k, v in data.items() if k != "actions"} | ({"actions": shown} if shown else {})
        # a button no longer live leaves the sheet, and the revision says so
        revision = int((updated or created).timestamp() * 1000) + dead
        return {"object_id": f"sheet:{key}", "kind": "sheet", "revision": revision, "data": data}

    async def _live(self, action: dict[str, Any]) -> bool:
        """Whether a button still does something now: unused, before its `until`, and what it acts on still
        open (the question unanswered and unexpired; the change still within its own undo)."""
        now = self._clock()
        if action.get("used") or (action.get("until") and datetime.fromisoformat(action["until"]) <= now):
            return False
        do, ref = action["do"], action["ref"]
        if do == "undo_ledger":  # the ledger's line: its own Undo's window
            if self.ledger is None:
                return False
            try:
                return bool((await self.ledger.find(ref))["undo"]["available"])
            except PisaError:
                return False
        if do.startswith("ask_"):
            query = "SELECT 1 FROM ritual_asks WHERE id = %s AND status = 'open' AND expires_at > %s"
        elif do == "undo_approval":
            query = (
                "SELECT 1 FROM approvals WHERE id = %s AND status = 'done' AND undone_at IS NULL AND "
                "undo IS NOT NULL AND undo_until > %s"
            )
        elif do == "undo_done":
            query = (
                "SELECT 1 FROM done_by_presence WHERE task_id = %s AND undone_at IS NULL AND undo_until > %s"
            )
        else:  # undo_task: still done, closed within the last 24 hours
            query = "SELECT 1 FROM tasks WHERE id = %s AND status = 'done' AND done_at > %s"
            now = now - UNDO_BUTTON_FOR
        async with self._pool.connection() as conn:
            return (await (await conn.execute(query, (uuid.UUID(ref), now))).fetchone()) is not None

    async def act(self, sheet_id: str, action_id: str, auth: str = "tap") -> str:
        """A button on a ritual's sheet: the user's own act, bound on the server (the phone sends
        only its id; what it does, until when, and that it is used once are Pisa's). The sentence Pisa
        says."""
        key = _uuid(sheet_id)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute("SELECT actions FROM ritual_sheets WHERE id = %s FOR UPDATE", (key,))
            ).fetchone()
            actions: list[dict[str, Any]] = list(row[0]) if row else []
            action = next((a for a in actions if a.get("id") == action_id), None)
            if action is None:
                raise PisaError("not_found", "No such button on that sheet.", "")
            if action.get("used"):  # one tap, once (yes and no share their question: both are spent)
                raise PisaError("approval_decided", "That button was already used.", "")
            if action.get("until") and datetime.fromisoformat(action["until"]) <= now:
                raise PisaError("approval_expired", "That button is over.", "Ask Pisa again")
            if not action["do"].startswith("ask_") and not await self._live(action):
                raise PisaError(
                    "undo_expired", "That can no longer be undone (24 hours, or already undone).", ""
                )
            for a in actions:
                if a is action or (action["do"].startswith("ask_") and a.get("ref") == action["ref"]):
                    a["used"] = True
            await conn.execute(
                "UPDATE ritual_sheets SET actions = %s, updated_at = %s WHERE id = %s",
                (Jsonb(actions), now, key),
            )
        do, ref = action["do"], action["ref"]
        if do in ("ask_yes", "ask_no"):
            said = await self.answer(_uuid(ref), do == "ask_yes", auth)
            if said is None:
                raise PisaError("approval_decided", "That question was already answered or is over.", "")
            return said
        return await self._undo(do, ref)

    async def _undo(self, do: str, ref: str) -> str:
        lang = await self._owner.lang(self._pool)
        try:
            if do == "undo_ledger" and self.ledger is not None:
                await self.ledger.undo(ref)
            elif do == "undo_approval" and self._approvals is not None:
                await self._approvals.undo(ref)
            elif do == "undo_done" and self._done is not None:
                await self._done.undo_task(ref)
            elif do == "undo_task" and self._tasks is not None:
                await self._tasks.update(ref, {"status": "open"})
            else:
                raise PisaError("not_found", "That can't be undone here.", "")
        except PisaError:
            raise
        return "Annullato." if lang == "it-IT" else "Undone."

    async def _notify(
        self,
        conn: AsyncConnection,
        ritual: str,
        text: str,
        lang: str,
        sheet_id: uuid.UUID | None,
        ask_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        notification_id, _ = await enqueue(
            conn,
            kind="info",
            text=text,
            lang=lang,
            now=self._clock(),
            timezone=self._owner.tz,
            respect_quiet=False,
            path="ritual",
        )  # quiet was checked first: a ritual is never held for the morning
        await conn.execute(
            "UPDATE notifications SET ritual = %s, sheet_id = %s, ask_id = %s WHERE id = %s",
            (ritual, sheet_id, ask_id, notification_id),
        )
        _log.info("ritual_sent", ritual=ritual, notification_id=str(notification_id), asks=ask_id is not None)
        return notification_id

    # --- Pisa's questions (one yes)
    # ---------------------------------------------------------------------------

    async def _ask(
        self,
        conn: AsyncConnection,
        kind: str,
        question: str,
        payload: dict[str, Any],
        expires: datetime,
        *,
        device_id: uuid.UUID | None = None,
        sheet_id: uuid.UUID | None = None,
        idea: Idea | None = None,
    ) -> uuid.UUID:
        ask_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO ritual_asks (id, kind, question, payload, idea_key, idea_kind, device_id, sheet_id, "
            "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ask_id,
                kind,
                question,
                Jsonb(payload),
                idea["key"] if idea else None,
                idea["kind"] if idea else None,
                device_id,
                sheet_id,
                self._clock(),
                expires,
            ),
        )
        return ask_id

    def asked_in_turn(self, device_id: uuid.UUID, ask_id: uuid.UUID, question: str) -> None:
        """A question Pisa asked in this turn: the user's next bare yes or no answers it (newest wins), once
        Pisa's reply really asked it (`answer_newest` checks)."""
        from pisa.services.conversation import current_turn  # the turn asking it (no import cycle at load)

        self._in_turn[device_id] = (ask_id, current_turn.get(), self.unasked.mark(), question)

    def bind(self, device_id: uuid.UUID, ask_id: uuid.UUID, turn: Any, question: str) -> None:
        """A question code said in its own reply (no model turn): the user's
        next bare yes or no answers it, exactly as `asked_in_turn`'s."""
        self._in_turn[device_id] = (ask_id, turn, self.unasked.mark(), question)

    def code_says(self, device_id: uuid.UUID, turn: Any) -> str | None:
        """What the knock or the tragitto carries, opened in this turn by the model's
        `answer_my_question`: code says it after the reply, in its own words (once)."""
        held = self._code_says.get(device_id)
        if held is None or held[1] is not turn:
            return None
        del self._code_says[device_id]
        return held[0]

    def question_in_turn(self, device_id: uuid.UUID, turn: Any) -> str | None:
        """The question one of the rituals asked in this turn (a hunch
        noted), for code to say in its own words when the reply didn't ("il venerdì sera è ormai il tuo rito
        col sushi?" over the hunch's "Il venerdì sera il sushi è diventato la tua abitudine?": their yes
        lapsed)."""
        held = self._in_turn.get(device_id)
        return held[3] if held is not None and held[1] is turn else None

    @staticmethod
    def said(question: str, reply: str) -> bool:
        """The reply says the question (its words, in order), as `answer_newest` checks it."""
        wanted = " ".join(rules.plain(question).rstrip("?").split())
        return bool(wanted) and wanted in " ".join(rules.plain(reply).split())

    def newest_ask(self, device_id: uuid.UUID, latest: Any = None) -> uuid.UUID | None:
        """Pisa's newest question to this device, when it is one of the rituals' and nothing was said after it
        (newest wins): a ritual's notice (its id is the newest in the unasked sequence, and no reply of
        Pisa's came after it), or a question Pisa asked in `latest`, the device's latest turn, with nothing
        said unasked since."""
        mark = getattr(latest, "asked", None)
        what = self.unasked.newest_check_in(device_id)
        if (
            what is not None
            and what.startswith("ask:")
            and (mark is None or self.unasked.since(device_id, mark))
        ):
            return _uuid(what.removeprefix("ask:"))
        held = self._in_turn.get(device_id)
        if held is not None and latest is not None and held[1] is latest:
            if not self.unasked.since(device_id, held[2]):
                return held[0]
        return None

    async def answer_newest(
        self, device_id: uuid.UUID, yes: bool, latest: Any = None, *, hold: bool = False
    ) -> str | None:
        """The conversation's yes chain: a bare yes or no to Pisa's newest question of these, decided
        by code. None when there is none open (the model goes on). `latest`: the device's latest turn."""
        ask_id = self.newest_ask(device_id, latest)
        if ask_id is None:
            return None
        held = self._in_turn.get(device_id)
        if held is not None and held[0] == ask_id and not await self._reply_asked(device_id, held[3]):
            # the turn's reply never asked it: it lapses, unanswered
            self._in_turn.pop(device_id, None)
            async with self._pool.connection() as conn:
                await conn.execute("UPDATE ritual_asks SET status = 'lapsed' WHERE id = %s", (ask_id,))
            _log.info("ritual_ask_never_said", ask_id=str(ask_id))
            return None
        said = await self.answer(ask_id, yes, "voice", hold_for=device_id if hold else None, device=device_id)
        if said is not None:
            self._in_turn.pop(device_id, None)
            self.unasked.said(device_id, None)  # answered: a later yes isn't this one's
        return said

    async def answer_in_turn(self, device_id: uuid.UUID, yes: bool) -> str | None:
        """`answer_my_question` (a talk, where code doesn't hear the yes): the same newest-wins check against
        the device's previous turn (never None, which would skip it)."""
        from pisa.services.conversation import current_turn

        turn = current_turn.get()
        if turn is None:
            return None
        return await self.answer_newest(device_id, yes, turn.before, hold=True)

    async def _reply_asked(self, device_id: uuid.UUID, question: str) -> bool:
        """Whether Pisa's latest reply to this device said the question (its words, in order)."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT m.content FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.device_id = %s AND m.role = 'assistant' ORDER BY m.id DESC LIMIT 1",
                    (device_id,),
                )
            ).fetchone()
        return self.said(question, str(row[0]) if row else "")

    async def answer(
        self,
        ask_id: uuid.UUID,
        yes: bool,
        auth: str,
        *,
        hold_for: uuid.UUID | None = None,
        device: uuid.UUID | None = None,
    ) -> str | None:
        """The user's yes or no to one of Pisa's questions: the fix runs on yes, nothing on no; a "no" to an
        idea is remembered for its kind; a hunch is confirmed or dropped. None when it isn't open. `device`:
        the device they answered from (a tap on a sheet names none)."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT id, kind, payload, idea_kind, expires_at FROM ritual_asks WHERE id = %s "
                    "AND status = 'open' FOR UPDATE",
                    (ask_id,),
                )
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                await conn.execute("UPDATE ritual_asks SET status = 'lapsed' WHERE id = %s", (ask_id,))
                return None
            await conn.execute(
                "UPDATE ritual_asks SET status = %s, answered_at = %s, answered_by = %s WHERE id = %s",
                ("yes" if yes else "no", now, auth if auth in ("voice", "tap") else "words", ask_id),
            )
            lang = await self._owner.lang(conn)
            if row["kind"] == "idea" and not yes and row["idea_kind"]:
                declined = set(await get_setting(conn, DECLINED_KINDS_KEY) or [])
                declined.add(row["idea_kind"])
                await set_setting(conn, DECLINED_KINDS_KEY, Jsonb(sorted(declined)))
            if row["kind"] == "hunch":
                await conn.execute(
                    "UPDATE hunches SET status = %s, answered_at = %s WHERE ask_id = %s",
                    ("confirmed" if yes else "wrong", now, ask_id),
                )
            thought_title = None
            if row["kind"] == "thought":  # kept on the day's thought (their "no" silences the kind)
                thought_title = await surprise_answered(conn, ask_id, yes, now)
        result = None
        if yes and row["kind"] == "ghost_fix":
            result = await self._apply_fix(row["payload"], auth, device)
            if result is None:
                async with self._pool.connection() as conn:
                    await conn.execute("UPDATE ritual_asks SET status = 'failed' WHERE id = %s", (ask_id,))
        elif yes and row["kind"] == "idea":
            result = row["payload"].get("said")
        elif row["kind"] == "hunch":
            await self._hunch_answered(ask_id, yes)
        elif row["kind"] == "thought":  # the yes names the task
            result = thought_title
        elif yes and row["kind"] in ("knock", "tragitto") and self.voice1 is not None:
            # their yes opens the knock (the morning) or the trip, made now from the records
            result = await self.voice1.opened(row["kind"], lang, row["payload"])
            if hold_for is not None:
                # in a turn (`answer_my_question`) the model gets a receipt, never the
                # content (the sheet's lead): code says it after the reply (`code_says`)
                from pisa.services.conversation import current_turn

                self._code_says[hold_for] = (
                    result or rules.answered(row["kind"], True, lang),
                    current_turn.get(),
                )
                _log.info("ritual_answered", ask_id=str(ask_id), kind=row["kind"], yes=yes, auth=auth)
                return OPENED_RECEIPT
        _log.info("ritual_answered", ask_id=str(ask_id), kind=row["kind"], yes=yes, auth=auth)
        if yes and row["kind"] == "ghost_fix" and result is None:
            return rules.COULDNT.get(lang, rules.COULDNT["en-US"])
        return rules.answered(row["kind"], yes, lang, result)

    async def _apply_fix(self, fix: dict[str, Any], auth: str, device: uuid.UUID | None) -> str | None:
        """The Ghost Run's one fix, on their yes: their own event moved (24 h Undo), or a task's block set.
        Only when things stand as Pisa saw them: the item still at its start, the target
        slot still free."""
        start = datetime.fromisoformat(fix["start"])
        end = datetime.fromisoformat(fix["end"])
        was = datetime.fromisoformat(fix["was_start"]) if fix.get("was_start") else None
        blocks, _ = await self._tomorrow(self._local(start).date())
        mover = next((b for b in blocks if b.ref == fix["ref"]), None)
        if was is not None and (mover is None or mover.start != was):
            _log.info("ritual_fix_stale", reason="moved")
            return None
        if any(b.ref != fix["ref"] and start < b.end and b.start < end for b in blocks):
            _log.info("ritual_fix_stale", reason="slot_taken")
            return None
        try:
            if fix["kind"] == "move_event" and self._actions is not None:
                done, said = await self._actions.move_on_yes(
                    fix["calendar_id"], fix["ref"], start, auth, was, device=device
                )
                return said if done else None
            if fix["kind"] == "task_block" and self._tasks is not None:
                task = await self._tasks.update(fix["ref"], {"start_at": start.isoformat()})
                if task.expected_from is None:  # said from the block as stored
                    return None
                lang = await self._owner.lang(self._pool)
                hm = f"{self._local(task.expected_from):%H:%M}"
                it = lang == "it-IT"
                return f"Fatto: {task.title} alle {hm}." if it else f"Done: {task.title} at {hm}."
        except PisaError as err:
            _log.warning("ritual_fix_failed", cause=err.code)
        return None

    # --- Ghost Run -----------------------------------------------------------------------------------------

    async def ghost_run(self, force: bool = False, *, day: date | None = None, late: bool = False) -> bool:
        """Tomorrow rehearsed: at most one problem and one fix, pushed as "Domani, in 20 secondi" with its
        sheet. Nothing at all when tomorrow is empty or nothing is worth saying. `late`: deferred out of quiet
        hours (`day` is then today): only when the fix still starts 30 minutes from now or later (or, with no
        fix, before the day's first block), else nothing."""
        tomorrow = day or self._local().date() + timedelta(days=1)
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            if not force and await self._made(conn, "ghost_run", tomorrow):
                return False
            # one rehearsal a day whatever it finds (never every minute of its window)
            if not force and await get_setting(conn, GHOST_RAN_KEY) == tomorrow.isoformat():
                return False
            await set_setting(conn, GHOST_RAN_KEY, Jsonb(tomorrow.isoformat()))
        blocks, due = await self._tomorrow(tomorrow)
        if not blocks and not due:
            return False
        travel, leave = await self.travel(blocks)
        rain_at, weather, bike = await self._weather_for(tomorrow, lang)
        problem = rules.find_problem(blocks, travel=travel, due=due, rain_at=rain_at, bike=bike, lang=lang)
        if problem is None:
            _log.info("ghost_run_quiet", blocks=len(blocks))
            return False
        now = self._clock()
        first = min((b.start for b in blocks), default=None)
        if late and not (
            (problem.fix is not None and problem.fix.start >= now + LATE_MARGIN)
            or (problem.fix is None and first is not None and first > now)
        ):
            _log.info("ghost_run_dropped_late", problem=problem.kind)
            return False
        data = rules.ghost_blocks(lang, blocks, problem, leave, weather)
        actions: list[dict[str, Any]] = []
        async with self._pool.connection() as conn, conn.transaction():
            ask_id = None
            expires = self._clock() + FIX_KEPT_MAX
            if problem.fix is not None and problem.question:
                ask_id = uuid.uuid4()
                was = problem.fix.was_start or problem.fix.start
                expires = min(problem.fix.start, was, expires)  # before either moment comes
                yes, no = ("Sì", "No") if lang == "it-IT" else ("Yes", "No")
                until = expires.isoformat()  # the buttons end with their question
                actions = [
                    {
                        "id": "a1",
                        "label": yes,
                        "style": "primary",
                        "do": "ask_yes",
                        "ref": str(ask_id),
                        "until": until,
                    },
                    {
                        "id": "a2",
                        "label": no,
                        "style": "plain",
                        "do": "ask_no",
                        "ref": str(ask_id),
                        "until": until,
                    },
                ]
            sheet_id = await self._store_sheet(conn, "ghost_run", tomorrow, data, actions)
            if sheet_id is None:
                return False
            if ask_id is not None and problem.fix is not None and problem.question:
                await conn.execute(
                    "INSERT INTO ritual_asks (id, kind, question, payload, sheet_id, created_at, expires_at) "
                    "VALUES (%s, 'ghost_fix', %s, %s, %s, %s, %s)",
                    (ask_id, problem.question, Jsonb(problem.fix.json()), sheet_id, self._clock(), expires),
                )
            said = f"{problem.line} {problem.question}" if problem.question else problem.line
            await self._notify(
                conn, "ghost_run", rules.line(lang, "push_fix", line=said), lang, sheet_id, ask_id
            )
        _log.info("ghost_run_sent", problem=problem.kind, fix=problem.fix.kind if problem.fix else None)
        return True

    async def _made(self, conn: AsyncConnection, ritual: str, day: date) -> bool:
        row = await (
            await conn.execute("SELECT 1 FROM ritual_sheets WHERE ritual = %s AND day = %s", (ritual, day))
        ).fetchone()
        return row is not None

    async def rehearse_blocks(self, day: date) -> tuple[list[rules.Block], dict[tuple[str, str], int]]:
        return await self.rehearsal.rehearse_blocks(day)

    async def day_blocks(self, day: date) -> list[rules.Block]:
        return await self.rehearsal.day_blocks(day)

    async def _tomorrow(self, day: date) -> tuple[list[rules.Block], list[rules.DueTask]]:
        return await self.rehearsal.tomorrow(day)

    async def travel(
        self, blocks: list[rules.Block]
    ) -> tuple[dict[tuple[str, str], int], dict[str, datetime]]:
        return await self.rehearsal.travel(blocks)

    async def _weather_for(self, day: date, lang: str) -> tuple[datetime | None, str | None, bool]:
        """(the first rainy hour tomorrow between 7 and 21, a one-line forecast, whether they go by bike)."""
        if self._weather is None or self._places is None:
            return None, None, False
        async with self._pool.connection() as conn:
            bike = (await self._owner.settings(conn))["places"]["default_mode"] == "bike"
        try:
            latest = await self._places.latest_location()
            if latest is None:
                return None, None, bike
            forecast = await self._weather.forecast(latest[0])
        except PisaError as err:
            _log.info("ghost_run_weather_skipped", cause=err.code)
            return None, None, bike
        tz = self._owner.zone
        rainy = [
            h
            for h in weather_rules.rainy_hours(forecast)
            if h.astimezone(tz).date() == day and time(7) <= h.astimezone(tz).time() <= time(21)
        ]
        line = next((weather_rules.briefing_line(d, lang) for d in forecast.days if d.date == day), None)
        return (rainy[0].astimezone(tz) if rainy else None), line, bike

    # --- Chiudiamo -----------------------------------------------------------------------------------------

    async def closing_sheet(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Today's recap: what Pisa did (the action log), what moved, what's open for tomorrow; Undo per line
        where allows (a calendar change within its 24 h, a task closed by being there)."""
        local = self._local()
        lo, hi = local_day_bounds(local.date(), self._owner.tz)
        _, next_hi = local_day_bounds(local.date() + timedelta(days=1), self._owner.tz)
        now = self._clock()
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            approvals = await (
                await conn.execute(
                    "SELECT id::text, kind, coalesce(result, summary), undo IS NOT NULL AND undone_at IS "
                    "NULL "
                    "AND undo_until > %s FROM approvals WHERE status = 'done' AND executed_at >= %s AND "
                    "executed_at < %s ORDER BY executed_at",
                    (now, lo, hi),
                )
            ).fetchall()
            presence = await (
                await conn.execute(
                    "SELECT task_id::text, title, undo_until > %s AND undone_at IS NULL FROM "
                    "done_by_presence "
                    "WHERE done_at >= %s AND done_at < %s AND task_id IS NOT NULL ORDER BY done_at",
                    (now, lo, hi),
                )
            ).fetchall()
            closed = await (
                await conn.execute(
                    "SELECT id::text, title FROM tasks WHERE status = 'done' AND done_at >= %s AND "
                    "done_at < %s ORDER BY done_at",
                    (lo, hi),
                )
            ).fetchall()
            still = await (
                await conn.execute(
                    "SELECT title FROM tasks WHERE status = 'open' AND priority < 3 AND due_at < %s "
                    "ORDER BY due_at LIMIT 8",
                    (next_hi,),
                )
            ).fetchall()
            ledger = await self.ledger.lines(conn, lo, hi, lang) if self.ledger is not None else None
        did: list[rules.DoneLine] = []
        moved: list[rules.DoneLine] = []
        if ledger is not None:  # the ledger is the action log: its lines, oldest first, its own Undo
            for line in reversed(ledger):
                undo = {"do": "undo_ledger", "ref": line["id"]} if line["undo"]["available"] else None
                text = str(line["title"] or line["why"] or "")
                if not text:
                    continue
                if line["kind"] in ("task_done", "event_done"):
                    text = f"✓ {text}"
                (moved if line.get("type") == "calendar_move" else did).append(rules.DoneLine(text, undo))
            approvals, presence = [], []  # the ledger holds them
            listed = {d.text.removeprefix("✓ ") for d in did}
            closed = [(tid, title) for tid, title in closed if title not in listed]
        for approval_id, kind, text, undoable in approvals:
            undo = {"do": "undo_approval", "ref": approval_id} if undoable else None
            (moved if kind == "calendar_move" else did).append(rules.DoneLine(str(text), undo))
        by_presence = {tid for tid, _, _ in presence}
        for tid, title, undoable in presence:
            did.append(rules.DoneLine(f"✓ {title}", {"do": "undo_done", "ref": tid} if undoable else None))
        for tid, title in closed:
            if tid not in by_presence:
                did.append(rules.DoneLine(f"✓ {title}", {"do": "undo_task", "ref": tid}))
        return rules.closing_sheet(lang, did, moved, [t for (t,) in still])

    async def closing(self, force: bool = False) -> bool:
        """At their closing hour: the recap as a sheet, pushed as "Chiudiamo?"; nothing on an empty day."""
        made = await self.closing_sheet()
        if made is None:
            return False
        data, actions = made
        _until(actions, self._clock() + UNDO_BUTTON_FOR)
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            day = self._local().date()
            if force:
                await conn.execute("DELETE FROM ritual_sheets WHERE ritual = 'closing' AND day = %s", (day,))
            sheet_id = await self._store_sheet(conn, "closing", day, data, actions)
            if sheet_id is None:
                return False
            text = (
                "Chiudiamo? Ecco la giornata in un minuto." if lang == "it-IT" else "Shall we close the day?"
            )
            await self._notify(conn, "closing", text, lang, sheet_id)
        return True

    async def closing_now(self) -> tuple[str, dict[str, Any]] | None:
        """ "Chiudiamo": the recap now (by voice); (sheet id, the sheet's data) or None on an empty day."""
        made = await self.closing_sheet()
        if made is None:
            return None
        data, actions = made
        _until(actions, self._clock() + UNDO_BUTTON_FOR)
        async with self._pool.connection() as conn, conn.transaction():
            day = self._local().date()
            sheet_id = await self._store_sheet(conn, "closing", day, data, actions)
            if sheet_id is None:  # the 21:30 push's sheet, brought up to now (its id stays valid)
                sheet_id = await self._refresh_sheet(conn, "closing", day, data, actions)
        shown = (await self.sheet(str(sheet_id)))["data"]
        return str(sheet_id), shown

    # --- the week's shape ----------------------------------------------------------------------------------

    async def week_poster(self, force: bool = False) -> bool:
        """Sunday: the week as a poster (hours by kind, the gym streak, one highlight), from the calendar and
        the tasks done; numbers by code only."""
        local = self._local()
        day = local.date()
        start = day - timedelta(days=day.weekday())  # Monday
        weeks = 4
        lo = local_day_bounds(start - timedelta(weeks=weeks - 1), self._owner.tz)[0]
        hi = local_day_bounds(day, self._owner.tz)[1]
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            if not force and await self._made(conn, "week_poster", day):
                return False
            rows = await (
                await conn.execute(
                    # Pisa's events made on their yes are part of their week (display, not learning)
                    "SELECT DISTINCT ON (id) title, starts_at, ends_at, coalesce(attendees, 0) FROM "
                    "calendar_events WHERE NOT all_day AND status <> 'cancelled' AND coalesce(accepted, "
                    "true) "
                    "AND starts_at >= %s AND starts_at < %s",
                    (lo, hi),
                )
            ).fetchall()
            done = await (
                await conn.execute(
                    "SELECT count(*) FROM tasks WHERE status = 'done' AND done_at >= %s AND done_at < %s",
                    (local_day_bounds(start, self._owner.tz)[0], hi),
                )
            ).fetchone()
        events = [rules.WeekEvent(t, s.astimezone(tz), e.astimezone(tz), a) for t, s, e, a in rows]
        this_week = [e for e in events if e.start.date() >= start]
        if not this_week:
            return False
        gym = [0] * weeks
        for e in events:
            if rules.kind_of(e.title, e.attendees) == "gym":
                index = weeks - 1 - (start - (e.start.date() - timedelta(days=e.start.weekday()))).days // 7
                if 0 <= index < weeks:
                    gym[index] += 1
        shape = rules.week_shape(this_week, gym, int(done[0]) if done else 0)
        data = rules.poster(lang, shape, weeks)
        try:  # the streaks that matter (two weeks or more); never fails the poster
            async with self._pool.connection() as conn:
                found = streak_rules.streaks(
                    await streak_sessions(conn, day, self._owner.tz, self._clock()), day
                )
            data["blocks"] = streak_rules.into_poster(data["blocks"], found, lang)
        except Exception:
            _log.exception("poster_streaks_skipped")
        if self.stanotte is not None:  # never fails the poster either
            try:
                extra = await self.stanotte(lang)
                if extra and len(data["blocks"]) + len(extra) <= SHEET_BLOCKS:
                    data["blocks"] = [*data["blocks"], *extra]
            except Exception:
                _log.exception("poster_stanotte_skipped")
        async with self._pool.connection() as conn, conn.transaction():
            if force:
                await conn.execute(
                    "DELETE FROM ritual_sheets WHERE ritual = 'week_poster' AND day = %s", (day,)
                )
            sheet_id = await self._store_sheet(conn, "week_poster", day, data, [])
            if sheet_id is None:
                return False
            text = (
                "La forma della tua settimana è pronta."
                if lang == "it-IT"
                else "The shape of your week is ready."
            )
            await self._notify(conn, "week_poster", text, lang, sheet_id)
        return True

    # --- something different -----------------------------------------------------------------------

    async def something_different(self) -> bool:
        """At most `per_week` times a week (and once a day), on a free evening or weekend slot, never during a
        booked block: one concrete idea, as a question, never the same idea twice, never a kind they said no
        to."""
        if self._ideas is None and self.explore is None:
            return False
        local = self._local()
        lo, hi = local - rules.IDEA_FREE_FOR, local + rules.IDEA_FREE_FOR
        week_start = local_day_bounds(local.date() - timedelta(days=local.weekday()), self._owner.tz)[0]
        today = local_day_bounds(local.date(), self._owner.tz)[0]
        async with self._pool.connection() as conn:
            settings = await self.get(conn)
            busy = await (
                await conn.execute(
                    "SELECT starts_at, ends_at FROM calendar_events WHERE NOT all_day AND status <> "
                    "'cancelled' "
                    "AND coalesce(accepted, true) AND starts_at < %s AND ends_at > %s",
                    (hi, lo),
                )
            ).fetchall()
            counts = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE created_at >= %s), count(*) FILTER (WHERE created_at >= "
                    "%s) "
                    "FROM ritual_asks WHERE kind = 'idea'",
                    (week_start, today),
                )
            ).fetchone()
            declined = set(await get_setting(conn, DECLINED_KINDS_KEY) or [])
            used = {
                k
                for (k,) in await (
                    # kept apart from the asks (90 days): never the same idea, ever
                    await conn.execute(
                        "SELECT idea_key FROM ritual_asks WHERE idea_key IS NOT NULL UNION "
                        "SELECT idea_key FROM ritual_ideas_used"
                    )
                ).fetchall()
            }
            general = await self._owner.settings(conn)
            lang = str(general["language"])
        if not general["profile"]["something_different"]:
            return False  # the own switch (About you) stops it too
        week, day = (int(counts[0]), int(counts[1])) if counts else (0, 0)
        if week >= settings["something_different"]["per_week"] or day:
            return False
        if not rules.idea_slot(
            local, [(s.astimezone(local.tzinfo), e.astimezone(local.tzinfo)) for s, e in busy]
        ):
            return False
        async with self._pool.connection() as conn, conn.transaction():
            if await get_setting(conn, IDEA_TRIED_KEY) == local.date().isoformat():
                return False  # one look a day: a slot with nothing new never searches every minute
            await set_setting(conn, IDEA_TRIED_KEY, Jsonb(local.date().isoformat()))
        # once a week, the first idea is a glint, a new place where they have never been
        found = await self.explore.glint(declined, used) if self.explore is not None else []
        if not found and self._ideas is not None:
            found = await self._ideas(declined, used)
        ideas = [i for i in found if i["kind"] not in declined and i["key"] not in used]
        if not ideas:
            return False
        idea = ideas[0]
        question = (
            explore_rules.glint_question(lang, idea["text"]) if idea.get("glint")
            else rules.idea_question(lang, idea["text"])
        )  # fmt: skip
        said = f"Bene: {idea['text']}." if lang == "it-IT" else f"Great: {idea['text']}."
        async with self._pool.connection() as conn, conn.transaction():
            ask_id = await self._ask(
                conn,
                "idea",
                question,
                {"said": said, **({"glint": True} if idea.get("glint") else {})},
                self._clock() + IDEA_KEPT,
                idea=idea,
            )
            await conn.execute(
                "INSERT INTO ritual_ideas_used (idea_key, used_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (idea["key"], self._clock()),
            )
            await self._notify(conn, "something_different", question, lang, None, ask_id)
        return True

    # --- the ledger of hunches -----------------------------------------------------------------------------

    async def note_hunch(
        self, guess: str, evidence: list[str], device_id: uuid.UUID | None, kind: str = "like"
    ) -> str:
        """A guess about the user, stored with its evidence as a candidate claim (in
        About you, never in a prompt) and asked once, in this turn; never acted on until they confirm.
        Sensitive guesses are refused here, by code."""
        text = " ".join(guess.split())[:200]
        kind = kind if kind in rules.HUNCH_KINDS else "like"
        if not text or not evidence:
            return "Not stored: a hunch needs the guess and at least one piece of evidence Pisa saw."
        if rules.hunch_states(text):  # a guess is asked, never stated
            return (  # how to ask it again
                "Not stored: a hunch is one question, with no statement about them before it. Call "
                "note_hunch "
                'again with only ONE short yes-or-no question to them ("Il sushi del venerdì è ormai una tua '
                'abitudine?") and say nothing about them as a fact.'
            )
        if profile_rules.is_sensitive(kind, text) or any(sensitive_place(e, None) for e in [text, *evidence]):
            # never a guess from a sensitive stay (the one helper: health, worship, politics)
            return (
                "Not stored: Pisa never guesses about sensitive things (health, money, "
                "beliefs). Don't ask it."
            )
        key = rules.hunch_key(text)
        now = self._clock()
        question = rules.hunch_question(text)
        hunch_id = uuid.uuid4()
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT status FROM hunches WHERE key = %s", (key,))).fetchone()
        if row is not None:
            return f"Already {row[0]}: never asked twice. Don't ask it."
        claim = None
        if self._profile is not None:
            claim = await self._profile.hunch_claim(
                kind=kind, statement=text, hunch_id=str(hunch_id), evidence=list(evidence)
            )
            if claim is None:
                return "Not stored: Pisa doesn't learn this (paused, off limits or sensitive). Don't ask it."
        async with self._pool.connection() as conn, conn.transaction():
            ask_id = await self._ask(
                conn, "hunch", question, {"hunch": text}, now + rules.HUNCH_ASK_KEPT, device_id=device_id
            )
            await conn.execute(
                "INSERT INTO hunches (id, text, kind, key, evidence, status, ask_id, created_at, asked_at, "
                "claim_id) VALUES (%s, %s, %s, %s, %s, 'asked', %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                (hunch_id, text, kind, key, Jsonb([e[:200] for e in evidence[:5]]), ask_id, now, now, claim),
            )
        if device_id is not None:
            self.asked_in_turn(device_id, ask_id, question)
        _log.info("hunch_noted", ask_id=str(ask_id))
        return f'Stored as a hunch. Ask it once, in these words: "{question}" Act on it only if they say yes.'

    async def _hunch_answered(self, ask_id: uuid.UUID, yes: bool) -> None:
        """Their answer reaches the hunch's claim: confirmed on yes, retired by them on no."""
        if self._profile is None:
            return
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT claim_id FROM hunches WHERE ask_id = %s", (ask_id,))
            ).fetchone()
        if row is not None and row[0] is not None:
            await self._profile.hunch_answered(str(row[0]), yes)

    async def hunches(self) -> list[str]:
        """The hunches they confirmed (the ledger of asks; the claims are the profile's)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT text FROM hunches WHERE status = 'confirmed' ORDER BY answered_at DESC LIMIT 10"
                )
            ).fetchall()
        return [r[0] for r in rows]


UNDO_BUTTON_FOR = timedelta(hours=24)  # a recap's Undo button (the change's own undo may end sooner)


def _until(actions: list[dict[str, Any]], until: datetime) -> None:
    for action in actions:
        action.setdefault("until", until.isoformat())


def _uuid(text: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(text).removeprefix("sheet:"))
    except ValueError:
        raise PisaError("not_found", "No such id.", "") from None


def place_ideas(
    pool: AsyncConnectionPool,
    places: "PlacesService",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IdeaSource:
    """The ideas from their places memory: a new place of the kind they go to most, near the place they go
    to most, that they have never visited. (The provider's names are data: only a name and an address are
    said.)"""

    async def ideas(declined: set[str], used: set[str]) -> list[Idea]:
        async with pool.connection() as conn:
            home = await home_store.read(conn)
            # where they are now, else their home (never the place they stayed most: a trip's hotel can be
            # that)
            here = await where_now(conn, clock()) or (home.coord if home is not None else None)
            rows = await (
                await conn.execute(
                    "SELECT p.id, p.category, p.lat, p.lon, count(*) AS n FROM stays s JOIN places p ON "
                    "p.id = s.place_id WHERE p.category IS NOT NULL AND p.lat IS NOT NULL AND s.arrived_at > "
                    "now() - interval '90 days' GROUP BY p.id ORDER BY n DESC LIMIT 20"
                )
            ).fetchall()
        if here is None:
            return []  # Pisa knows neither where they are nor their home: no idea (never a far trip's)
        # never a place to sleep, and only the kinds they go to around where they are now
        rows = [
            r
            for r in rows
            if not explore_rules.lodging(str(r[1]))
            and explore_rules.near_here((r[2], r[3]), here)
            and not sensitive_place(None, str(r[1]))  # never "another pharmacy near you"
        ]
        if not rows:
            return []
        async with pool.connection() as conn:  # every place they ever stayed at, not only the top 20
            visited = {str(r[0]) for r in await (
                await conn.execute("SELECT DISTINCT place_id FROM stays WHERE place_id IS NOT NULL")
            ).fetchall()}  # fmt: skip
        kinds: dict[str, int] = {}
        for _, category, _, _, n in rows:
            kinds[str(category)] = kinds.get(str(category), 0) + int(n)
        out: list[Idea] = []
        for category, _ in sorted(kinds.items(), key=lambda kv: -kv[1]):
            if category in declined:
                continue
            try:  # near where they are now, never near a place they went to on a trip
                found = await places.search(category, near=here, limit=5, radius_m=explore_rules.IDEA_NEAR_M)  # type: ignore[arg-type]
            except PisaError as err:
                _log.info("idea_search_skipped", cause=err.code)
                return out
            for place in found:
                key = rules.idea_key(category, str(place.get("id") or place.get("name")))
                if (
                    str(place.get("id")) in visited
                    or key in used
                    or explore_rules.lodging(place.get("category"))
                ):
                    continue
                lat, lon = place.get("lat"), place.get("lon")
                if lat is None or lon is None or not explore_rules.near_here((lat, lon), here):
                    continue  # nothing far, nothing in another city or country than where they are
                name = str(place.get("name") or "")[:80]
                where = str(place.get("address") or "")[:80]
                out.append({"kind": category, "key": key, "text": f"{name}, {where}" if where else name})
                break
            if out:
                return out
        return out

    return ideas
