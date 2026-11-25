"""The morning briefing: one service, four triggers.

- glasses_on, app_open (presence): inside the window, repeat rule, quiet ignored; `notify` to that device.
- phrase (a matching utterance): no window, no repeat rule, always freshly composed, quiet ignored; the `say`
  reply (spoken by the gateway).
- time (the worker, every minute): repeat rule; quiet holds it, then it is delivered as itself; the delivery
  rule to every device.

Explicit requests (`ask`: `POST /v1/briefings`, the `get_briefing` tool) and the phrase never count against
the repeat rule: it looks only at automatic briefings. The advisory lock BRIEFING wraps "is one due? →
compose → store → deliver", so simultaneous triggers (glasses and app together, two devices, the worker)
give one briefing; the decision reads the `briefings` table, so a restart never sends one twice.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import briefing as brain
from pisa.brain.intake import EXTRACT_CATEGORIES
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.db.settings import get_setting, set_setting
from pisa.domain import riepilogo
from pisa.domain import rituals as ritual_rules
from pisa.domain import settings as rules
from pisa.domain.errors import PisaError
from pisa.domain.mail import NEVER_TO_MODEL_LABELS
from pisa.domain.owner import Owner
from pisa.domain.places import WALK_MAX_MIN, closes_between
from pisa.domain.surprise import Thought
from pisa.domain.tasks import Task, local_day_bounds, task_place_id
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services import foreign
from pisa.services.notifications import NOTIFY_MAX_CHARS, enqueue
from pisa.services.people import PeopleService
from pisa.services.places import PlacesService
from pisa.services.planner import PlannerService
from pisa.services.stop import stopped_now
from pisa.services.surprise import SurpriseService
from pisa.services.tasks import TasksService
from pisa.services.weather import WeatherService

Trigger = Literal["glasses_on", "app_open", "phrase", "time", "ask"]
AUTOMATIC: tuple[Trigger, ...] = ("glasses_on", "app_open", "time")
# the briefing's triggers are the user's settings (glasses on, app open, a time).
TIME_CATCH_UP = timedelta(minutes=30)  # a missed briefing.time is still delivered this long after it
COMPOSE_DEADLINE_S = 6.0  # the text is ready ≤ 6 s after the trigger, or the fixed template is used
EMAIL_LOOKBACK = timedelta(hours=24)  # "since the previous briefing" when there is none yet
MAX_MAIL = 10
OTHER_TASKS = 3
MISSED_KEY = "briefing_time_missed"  # app_settings: the last briefing day whose missed time was logged
PLACE_NOTES_S = 3.0  # travel time and closing warnings: left out when a provider is slower
_log = get_logger("pisa.briefing")


@dataclass(frozen=True)
class Briefing:
    id: uuid.UUID
    day: date
    trigger: Trigger
    text: str
    lang: str
    created_at: datetime

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": str(self.id),
            "day": self.day.isoformat(),
            "trigger": self.trigger,
            "text": self.text,
            "lang": self.lang,
            "created_at": self.created_at.isoformat(),
        }
        return out


_SELECT = "SELECT id, day, trigger, text, lang, created_at FROM briefings"


class _TooLong(Exception):
    """The thought would make the notice longer than the outbox keeps (its savepoint rolls
    back)."""


class TwoCountryLines(Protocol):
    """Pisa's own lines for the briefing (the residency count once a month, a quiet-hours landing)."""

    async def lines(self, lang: str) -> list[str]: ...

    async def said_in(self, text: str) -> None: ...


class BriefingService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        gateway: ModelGateway,
        tasks: TasksService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        deadline_s: float = COMPOSE_DEADLINE_S,
        places: PlacesService | None = None,
        planner: PlannerService | None = None,
        weather: WeatherService | None = None,
        people: PeopleService | None = None,
        two_countries: TwoCountryLines | None = None,
        rituals: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        public: Callable[[], Awaitable[list[str]]] | None = None,
        surprise: SurpriseService | None = None,
        stanotte: Callable[[AsyncConnection, str], Awaitable[tuple[uuid.UUID, str] | None]] | None = None,
    ) -> None:
        self._people = people  # the day's lines about people (dates, an orbit, a thank-you)
        self._places = places
        self._planner = planner
        self._weather = weather
        self._pool = pool
        self._gateway = gateway
        self._tasks = tasks
        self._owner = owner
        self._clock = clock
        self._deadline_s = deadline_s
        # Pisa's two-country lines for the briefing (the month's residency count, a landing said in
        # quiet hours); each is marked said only once the briefing holding it is out
        self.two_countries = two_countries
        # the rituals' settings; with `one_thing` on, an automatic
        # briefing is the single thing that matters today, in one sentence (the rest only when they ask).
        self.rituals = rituals
        # the public data for the city they are in; never waits long or fails it
        self.public = public
        self.surprise = surprise  # Pisa's morning thought
        # last night's sheet, carried by the first automatic briefing's notice once the
        # gate (`stanotte`) is open; (sheet id, the line the briefing ends with).
        self.stanotte = stanotte

    # --- triggers ------------------------------------------------------------------------------------------

    async def on_presence(
        self, device_id: uuid.UUID, trigger: Literal["glasses_on", "app_open"]
    ) -> Briefing | None:
        """glasses_on / app_open: inside the window, per the repeat rule; ignores quiet; to that device."""
        return await self._automatic(trigger, device_id)

    async def on_time(self) -> Briefing | None:
        """The worker's minute check: the first check at or after briefing.time on a briefing day without an
        automatic briefing yet delivers one; later than 30 minutes it is skipped and logged."""
        return await self._automatic("time", None)

    async def for_phrase(self, utterance: str) -> Briefing | None:
        """A briefing when the utterance is one of the phrases and the phrase trigger is on, else None (the
        utterance goes to the conversation agent). Always freshly composed; the gateway speaks it."""
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
        briefing = settings["briefing"]
        if not briefing["triggers"]["phrase"] or not rules.matches_phrase(utterance, briefing["phrases"]):
            return None
        return await self._fresh("phrase", settings)

    async def ask(self) -> Briefing:
        """`POST /v1/briefings` and the `get_briefing` tool: composed now, stored, returned, not delivered."""
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
        return await self._fresh("ask", settings)

    async def ask_text(self) -> str:
        return (await self.ask()).text

    async def latest_view(self) -> dict[str, Any]:
        """`GET /v1/briefings/latest`: the latest briefing, and when it is today's, the riepilogo as a view
        of the current plan (`domain.riepilogo`): its `text` is the morning's summary line and what is left
        today, derived now by code (no model call), so a task closed or added since shows at once; `morning`
        keeps the text as composed. Another day's briefing is as it was composed (`view` null)."""
        briefing = await self.latest()
        out = briefing.json() | {"morning": briefing.text, "view": None}
        if self._planner is None:
            return out
        try:
            today = await self._planner.today()
            if briefing.day != today:
                return out
            # RAIL (the planner's): a plan made from other open tasks than now is made again first
            plan = await self._planner.current(today)
        except PisaError as err:
            _log.info("briefing_view_skipped", cause=err.code)
            return out
        now = self._clock()
        if plan is not None:
            left, also = riepilogo.plan_items(plan, now)
        else:  # no plan of today yet: their open tasks due by today's end, in due order
            end = local_day_bounds(today, self._owner.tz)[1]
            due = sorted(
                (
                    t
                    for t in await self._tasks.list("open", low=False)
                    if t.due_at is not None and t.due_at < end
                ),
                key=lambda t: t.due_at or end,
            )
            left = [riepilogo.Item("task", t.title, t.due_at if t.due_kind == "datetime" else None, t.id)
                    for t in due][: riepilogo.ITEMS_MAX]  # fmt: skip
            also = []
        async with self._pool.connection() as conn:
            gone = await (
                await conn.execute(
                    "SELECT title FROM tasks WHERE status <> 'open' AND updated_at >= %s",
                    (briefing.created_at,),
                )
            ).fetchall()
        titles = [i.title for i in (*left, *also)] + [str(r[0]) for r in gone]
        summary = riepilogo.summary_line(briefing.text, titles)
        out["text"] = riepilogo.text(
            briefing.lang, summary, left, also, self._owner.zone, self._owner.hour_cycle()
        )
        out["view"] = {
            "summary": summary,
            "items": [riepilogo.item_json(i) for i in left],
            "also": [riepilogo.item_json(i) for i in also],
            "updated_at": now.isoformat(),
        }
        return out

    async def latest(self) -> Briefing:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_SELECT + " ORDER BY created_at DESC LIMIT 1")).fetchone()
        if row is None:
            raise PisaError("not_found", "There is no briefing yet.", "Ask Pisa for one")
        return Briefing(*row)

    # --- the decision --------------------------------------------------------------------------------------

    async def _automatic(self, trigger: Trigger, device_id: uuid.UUID | None) -> Briefing | None:
        async with self._pool.connection() as conn:
            if await stopped_now(conn):  # no automatic briefing while Pisa is stopped
                return None
        # Try, never wait: a trigger that loses the race to another one is a duplicate.
        async with locks.held(self._pool, locks.BRIEFING) as got:
            if not got:
                _log.info("briefing_busy", trigger=trigger)
                return None
            async with self._pool.connection() as conn:
                settings = await self._owner.settings(conn)
                now = self._clock()
                if not await self._due(conn, trigger, settings, now):
                    return None
            result = await self._compose(settings, now, trigger)
            async with self._pool.connection() as conn, conn.transaction():
                if trigger != "time":
                    # The user showed up: a time briefing held by quiet mode is superseded.
                    await conn.execute(
                        "UPDATE notifications SET delivered_via = 'none', delivered_at = %s "
                        "WHERE delivered_via = 'held' AND delivered_at IS NULL AND id IN "
                        "(SELECT notification_id FROM briefings WHERE trigger = 'time')",
                        (now,),
                    )
                # Pisa's morning thought, at most one a day, after the text (its question last)
                thought = await self._thought(conn, trigger, settings, now, result)
                if thought is not None:
                    result = f"{result} {thought[0].text}"
                sheet = await self._sheet(conn, trigger, settings, now)
                if sheet is not None:
                    result = f"{result} {sheet[1]}"
                notification_id, _ = await enqueue(
                    conn,
                    kind="briefing",
                    text=result,
                    lang=settings["language"],
                    now=now,
                    timezone=self._owner.tz,
                    device_id=device_id,
                    respect_quiet=trigger == "time",  # presence: the user just showed up
                    path="briefing",
                )
                if thought is not None and thought[1] is not None:
                    # its question is one of the rituals': their bare yes or no answers it (decided by
                    # code)
                    await conn.execute(
                        "UPDATE notifications SET ask_id = %s WHERE id = %s",
                        (thought[1], notification_id),
                    )
                if sheet is not None:  # the notice opens the sheet (GET /v1/sheets/{sheet_id})
                    await conn.execute(
                        "UPDATE notifications SET ritual = 'stanotte', sheet_id = %s WHERE id = %s",
                        (sheet[0], notification_id),
                    )
                stored = await self._store(conn, trigger, result, settings, now, notification_id)
            await self._said(result)
            _log.info("briefing_delivered", briefing_id=str(stored.id), trigger=trigger)
            return stored

    async def _sheet(
        self, conn: AsyncConnection, trigger: Trigger, settings: dict[str, Any], now: datetime
    ) -> tuple[uuid.UUID, str] | None:
        """Last night's sheet, in the briefing's transaction behind a savepoint (
        it failing never fails the briefing). Not spent on a time briefing held by quiet hours.
        """
        if self.stanotte is None or trigger not in AUTOMATIC:
            return None
        if trigger == "time" and rules.is_quiet(now, settings, self._owner.tz):
            return None
        try:
            async with conn.transaction():
                return await self.stanotte(conn, settings["language"])
        except Exception as exc:
            _log.warning("briefing_sheet_skipped", error=exc.__class__.__name__)
            return None

    async def _thought(
        self, conn: AsyncConnection, trigger: Trigger, settings: dict[str, Any], now: datetime, said: str
    ) -> tuple[Thought, uuid.UUID | None] | None:
        """The day's thought, kept in the briefing's transaction (a savepoint: it failing never fails
        the briefing). Never in a time briefing that quiet hours hold: they may never see that one."""
        if self.surprise is None or (trigger == "time" and rules.is_quiet(now, settings, self._owner.tz)):
            return None
        day = rules.briefing_day(now, settings, self._owner.tz)  # the briefing's day, not the clock's
        try:
            async with conn.transaction():
                # only the first automatic briefing they got today (one held, superseded or sent
                # to no device never reached them)
                earlier = await (
                    await conn.execute(
                        "SELECT 1 FROM briefings b JOIN notifications n ON n.id = b.notification_id "
                        "WHERE b.day = %s AND b.trigger = ANY(%s) AND n.delivered_via IS NOT NULL "
                        "AND n.delivered_via NOT IN ('none', 'held') LIMIT 1",
                        (day, list(AUTOMATIC)),
                    )
                ).fetchone()
                if earlier is not None:
                    return None
                got = await self.surprise.offer(conn, settings["language"], said, day)
                if got is not None and len(f"{said} {got[0].text}") > NOTIFY_MAX_CHARS:
                    raise _TooLong  # never a yes bound to a question the outbox would cut
                return got
        except _TooLong:
            _log.info("morning_thought_too_long")
            return None
        except Exception as exc:
            _log.warning("morning_thought_skipped", cause=exc.__class__.__name__)
            return None

    async def _due(
        self, conn: AsyncConnection, trigger: Trigger, settings: dict[str, Any], now: datetime
    ) -> bool:
        briefing = settings["briefing"]
        if not briefing["triggers"]["time" if trigger == "time" else trigger]:
            return False
        local = now.astimezone(self._owner.zone)
        if trigger != "time" and not rules.in_span(local.time(), briefing["window"]):
            return False
        day = rules.briefing_day(now, settings, self._owner.tz)
        last = await (
            await conn.execute(
                # Only briefings that reached the user or are on their way (not held by quiet, not 'none').
                "SELECT b.day, b.created_at FROM briefings b "
                "LEFT JOIN notifications n ON n.id = b.notification_id "
                "WHERE b.trigger = ANY(%s) AND n.delivered_via IS DISTINCT FROM 'none' "
                "AND NOT COALESCE(%s AND n.delivered_via = 'held' AND n.delivered_at IS NULL, false) "
                "ORDER BY b.created_at DESC LIMIT 1",
                (list(AUTOMATIC), trigger != "time"),
            )
        ).fetchone()
        repeat = briefing["repeat_after_min"]
        if repeat is None:
            allowed = last is None or last[0] != day
        else:
            allowed = last is None or now - last[1] >= timedelta(minutes=repeat)
        if trigger != "time":
            return allowed
        at = rules.briefing_time_on(day, settings, self._owner.tz)
        if now < at:
            return False
        done = await (
            await conn.execute("SELECT 1 FROM briefings WHERE trigger = 'time' AND day = %s", (day,))
        ).fetchone()
        if done is not None or not allowed:
            return False
        if now - at < TIME_CATCH_UP:
            return True
        if await get_setting(conn, MISSED_KEY) != day.isoformat():
            await set_setting(conn, MISSED_KEY, Jsonb(day.isoformat()))
            _log.warning(
                "briefing_time_missed", day=day.isoformat(), late_min=int((now - at).total_seconds() // 60)
            )
        return False

    async def _fresh(self, trigger: Trigger, settings: dict[str, Any]) -> Briefing:
        now = self._clock()
        result = await self._compose(settings, now, trigger)
        async with self._pool.connection() as conn:
            stored = await self._store(conn, trigger, result, settings, now)
        await self._said(result)
        _log.info("briefing_composed", briefing_id=str(stored.id), trigger=trigger)
        return stored

    async def _said(self, text: str) -> None:
        """The two-country lines this briefing holds, marked said (never before)."""
        if self.two_countries is not None:
            try:
                await self.two_countries.said_in(text)
            except Exception as exc:
                _log.warning("briefing_two_countries_unmarked", cause=exc.__class__.__name__)

    async def _store(
        self,
        conn: AsyncConnection,
        trigger: Trigger,
        text: str,
        settings: dict[str, Any],
        now: datetime,
        notification_id: uuid.UUID | None = None,
    ) -> Briefing:
        briefing = Briefing(
            uuid.uuid4(),
            rules.briefing_day(now, settings, self._owner.tz),
            trigger,
            text,
            settings["language"],
            now,
        )
        await conn.execute(
            "INSERT INTO briefings (id, day, trigger, text, lang, created_at, notification_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                briefing.id,
                briefing.day,
                briefing.trigger,
                briefing.text,
                briefing.lang,
                briefing.created_at,
                notification_id,
            ),
        )
        return briefing

    # --- composition ---------------------------------------------------------------------------------------

    async def _compose(self, settings: dict[str, Any], now: datetime, trigger: Trigger) -> str:
        """Never silence: an empty day is a fixed sentence (no model call); a model failure, the template.
        Pisa's two-country lines go after the composed text as written, outside the word
        budget and never through the model (a count keeps its "non una consulenza")."""
        if trigger in AUTOMATIC and self.rituals is not None and (await self.rituals())["one_thing"]["on"]:
            return await self.one_thing(settings, now)
        data = await self.gather(settings, now)
        lines, data.two_countries = data.two_countries, []
        text = await self._body(data, trigger)
        return " ".join([text, *lines]) if lines else text

    async def _body(self, data: brain.BriefingData, trigger: Trigger) -> str:
        if data.empty:
            return brain.EMPTY_DAY[data.lang]
        instructions, facts = brain.prompt(data)

        draft = brain.Draft()

        async def call(ctx: CallContext) -> str:
            return await brain.compose(ctx.model, data, draft, ctx.usage.update)

        try:
            async with asyncio.timeout(self._deadline_s):
                text = await self._gateway.run(
                    brain.SLOT,
                    call,
                    prompt_chars=len(instructions) + len(facts),
                    max_output_tokens=2 * brain.max_tokens(data.max_words),  # at most two answers
                )
        except PisaError as err:
            _log.warning("briefing_fallback", trigger=trigger, cause=err.code, detail=err.detail or None)
            return brain.fallback(data)
        except TimeoutError:
            # Only the shorter-answer retry ran out of time: the first answer, cut at a sentence, still beats
            # the template.
            first = brain.trim(draft.first, data.max_words) if draft.first else None
            if first is not None:
                _log.warning("briefing_retry_timeout", trigger=trigger, deadline_s=self._deadline_s)
                return first
            _log.warning("briefing_fallback", trigger=trigger, cause="timeout", deadline_s=self._deadline_s)
            return brain.fallback(data)
        except Exception as exc:
            _log.warning("briefing_fallback", trigger=trigger, cause=exc.__class__.__name__)
            return brain.fallback(data)
        trimmed = brain.trim(text, data.max_words) if text else None
        if trimmed is None:
            _log.warning("briefing_fallback", trigger=trigger, cause="too_long" if text else "empty")
            return brain.fallback(data)
        return trimmed

    async def one_thing(self, settings: dict[str, Any], now: datetime) -> str:
        """The one thing that matters today, from the deadlines, the calendar and the pool, by
        code (no model): an overdue task, else one due today, else the next timed event, else the pool's
        first."""
        data = await self.gather(
            {**settings, "briefing": {**settings["briefing"], "sections": ["agenda", "tasks"]}}, now
        )
        tz = self._owner.zone

        def things(tasks: list[brain.TaskItem]) -> list[ritual_rules.Thing]:
            return [ritual_rules.Thing(t.title, _at(t.due, t.due_kind, tz))
                    for t in tasks]  # fmt: skip

        events = [ritual_rules.Thing(e.title, e.start.astimezone(tz)) for e in data.events if not e.all_day
                  and e.start > now]  # fmt: skip
        todo = [ritual_rules.Thing(data.first_task.title)] if data.first_task else things(data.other_tasks)
        said = ritual_rules.one_thing(
            data.lang,
            overdue=things(data.overdue),
            due_today=things(data.due_today),
            events=events,
            todo=todo,
        )
        return said or brain.EMPTY_DAY[data.lang]  # an empty day: the briefing's own sentence

    async def gather(self, settings: dict[str, Any], now: datetime) -> brain.BriefingData:
        """Only the chosen sections."""
        sections = settings["briefing"]["sections"]
        data = brain.BriefingData(
            lang=settings["language"],
            max_words=settings["briefing"]["max_words"],
            sections=list(sections),
            now=now,
            timezone=self._owner.tz,
            hour_cycle=self._owner.hour_cycle(),
            region=self._owner.region(),
        )
        today = now.astimezone(self._owner.zone).date()
        if "agenda" in sections:
            day = (await self._tasks.agenda(today, 1))["days"][0]
            data.events = [
                brain.Event(
                    datetime.fromisoformat(e["start"]),
                    e["title"],
                    e["location"],
                    e["all_day"],
                    outside=bool(e.get("foreign") or e.get("invited")),  # a stranger's title
                )
                for e in day["events"]
                if e["all_day"] or datetime.fromisoformat(e["end"]) > now  # what is still ahead today
            ]
        if "agenda" in sections and self._places is not None:
            first = next(
                (e for e in day["events"] if not e["all_day"] and e.get("place_id")
                 and datetime.fromisoformat(e["start"]) > now),
                None,
            )  # fmt: skip
            if first is not None:
                try:  # the briefing never waits long on a map provider
                    async with asyncio.timeout(PLACE_NOTES_S):
                        data.travel = await self._travel_note(first, settings, now)
                except TimeoutError:
                    _log.warning("briefing_place_note_skipped", note="travel", cause="timeout")
        if "tasks" in sections:
            open_tasks = await self._tasks.list("open", low=False)  # no briefing line for low
            aged = await self._tasks.aged_ids()  # overdue for days: asked about once, not briefed
            open_tasks = [t for t in open_tasks if t.id not in aged]
            self._split_tasks(data, open_tasks, today, now)
            if self._places is not None:
                try:
                    async with asyncio.timeout(PLACE_NOTES_S):
                        data.closing = await self._closing_notes(open_tasks, today, now)
                except TimeoutError:
                    _log.warning("briefing_place_note_skipped", note="closing", cause="timeout")
        if "tasks" in sections and self._planner is not None:
            try:  # the plan is made in about 2 s; the briefing never waits longer than this for it
                making = asyncio.ensure_future(self._planner.get())
                async with asyncio.timeout(PLACE_NOTES_S):
                    # Shielded: when the briefing stops waiting, the plan is still made and stored.
                    data.first_task = first_task(await asyncio.shield(making), now)
                if data.first_task is not None and data.first_task.task_id:
                    async with self._pool.connection() as conn:
                        mail = await foreign.task_foreign(conn, [data.first_task.task_id])
                    data.first_task = dataclasses.replace(data.first_task, outside=mail)
            except TimeoutError:
                _log.warning("briefing_place_note_skipped", note="plan", cause="timeout")
            except PisaError as err:
                _log.info("briefing_plan_skipped", cause=err.code)
        if "email" in sections:
            data.mail = await self._important_mail(now)
        if "weather" in sections and self._weather is not None:
            try:  # Open-Meteo answers in well under a second; the briefing never waits longer than this
                async with asyncio.timeout(PLACE_NOTES_S):
                    data.weather = await self._weather.briefing_line(data.lang)
            except TimeoutError:
                _log.warning("briefing_place_note_skipped", note="weather", cause="timeout")
            except PisaError as err:
                _log.info("briefing_weather_skipped", cause=err.code)
        if self.two_countries is not None:
            try:
                data.two_countries += await self.two_countries.lines(data.lang)
            except Exception as exc:  # never fails the briefing
                _log.warning("briefing_two_countries_skipped", cause=exc.__class__.__name__)
        if self.public is not None:  # whichever city they are in
            try:
                async with asyncio.timeout(PLACE_NOTES_S):
                    data.public = await self.public()
            except TimeoutError:
                _log.warning("briefing_public_skipped", cause="timeout")
            except Exception as exc:
                _log.warning("briefing_public_skipped", cause=exc.__class__.__name__)
        if self._people is not None:  # never waits long, never fails the briefing
            try:
                async with asyncio.timeout(PLACE_NOTES_S):
                    data.people = await self._people.morning_lines(data.lang)
            except TimeoutError:
                _log.warning("briefing_place_note_skipped", note="people", cause="timeout")
            except Exception as exc:
                _log.warning("briefing_people_skipped", cause=exc.__class__.__name__)
        return data

    async def _travel_note(
        self, event: dict[str, Any], settings: dict[str, Any], now: datetime
    ) -> brain.TravelNote | None:
        """From where the user is (a location at most 15 minutes old) to the first timed event with a place,
        by the default mode. Any failure leaves it out: the briefing never waits on it."""
        assert self._places is not None
        mode = settings["places"]["default_mode"]
        try:
            here = await self._places.here()
            place_id = uuid.UUID(event["place_id"])
            target = await self._places.coords(place_id)
            estimates, _, _ = await self._places.travel_between(here, [target], mode)
            name = await self._places.name(place_id)
        except PisaError as err:
            _log.info("briefing_travel_skipped", cause=err.code)
            return None
        if estimates[0] is None:
            return None
        minutes = estimates[0].best_minutes
        if mode == "walk" and minutes > WALK_MAX_MIN:  # never a walk of hours: transit instead
            try:
                estimates, _, _ = await self._places.travel_between(here, [target], "transit")
            except PisaError as err:
                _log.info("briefing_travel_skipped", cause=err.code)
                return None
            if estimates[0] is None:
                return None
            mode, minutes = "transit", estimates[0].best_minutes
        start = datetime.fromisoformat(event["start"])
        return brain.TravelNote(
            event["title"], name, minutes, mode, start - timedelta(minutes=minutes),
            outside=bool(event.get("foreign") or event.get("invited")),
        )  # fmt: skip

    async def _closing_notes(self, tasks: list[Task], today: date, now: datetime) -> list[brain.ClosingNote]:
        """Tasks due today at a place that is open now but closes before the task's time is up."""
        assert self._places is not None
        end_today = local_day_bounds(today, self._owner.tz)[1]
        notes: list[brain.ClosingNote] = []
        for task in tasks:
            if task_place_id(task) is None or task.due_at is None or not now <= task.due_at < end_today:
                continue
            try:
                info, _ = await self._places.place_brief(uuid.UUID(task.place_id))
                name = await self._places.name(uuid.UUID(task.place_id))
            except PisaError as err:
                _log.info("briefing_hours_skipped", cause=err.code)
                continue
            if info is None:
                continue
            until = task.due_at if task.due_kind == "datetime" else end_today
            closes = closes_between(
                info.current + info.weekly, now, until, self._owner.tz, frozenset(info.closed_dates)
            )
            if closes is not None:
                notes.append(brain.ClosingNote(task.title, name, closes, outside=task.source_kind == "email"))
        return notes

    def _split_tasks(self, data: brain.BriefingData, tasks: list[Task], today: date, now: datetime) -> None:
        end_today = local_day_bounds(today, self._owner.tz)[1]
        others: list[Task] = []
        for task in tasks:
            if task.due_at is not None and task.due_at < now:
                data.overdue.append(_item(task))
            elif task.due_at is not None and task.due_at < end_today:  # a `date` task is due at 23:59
                data.due_today.append(_item(task))
            else:
                others.append(task)
        far = datetime.max.replace(tzinfo=UTC)
        others.sort(key=lambda t: (t.priority, t.due_at or far, t.created_at))  # the most important first
        data.other_tasks = [_item(t) for t in others[:OTHER_TASKS]]

    async def _important_mail(self, now: datetime) -> list[brain.Mail]:
        """Mail triaged as important (an action, an event or booking, a delivery, a bill or deadline) since
        the previous briefing: sender and subject only."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute("SELECT max(created_at) FROM briefings")).fetchone()
            since = row[0] if row and row[0] else now - EMAIL_LOOKBACK
            rows = await (
                await conn.execute(
                    "SELECT coalesce(from_name, from_address, ''), subject FROM email_messages "
                    "WHERE intake_category = ANY(%s) AND intake_at > %s AND NOT (labels && %s) "
                    "ORDER BY sent_at DESC LIMIT %s",
                    (sorted(EXTRACT_CATEGORIES), since, list(NEVER_TO_MODEL_LABELS), MAX_MAIL),
                )
            ).fetchall()
        return [brain.Mail(sender, subject) for sender, subject in rows]


def first_task(plan: dict[str, Any], now: datetime) -> brain.FirstTask | None:
    """The first task placed from now on, and the leave time of the trip right before it."""
    items = plan.get("items") or []
    for i, item in enumerate(items):
        if item.get("kind") != "task" or datetime.fromisoformat(item["end"]) <= now:
            continue
        before = items[i - 1] if i > 0 else None
        leave = before.get("leave_at") if before and before.get("kind") == "travel" else None
        return brain.FirstTask(
            item["title"],
            datetime.fromisoformat(item["start"]),
            datetime.fromisoformat(leave) if leave else None,
            task_id=item.get("task_id"),
        )
    return None


def _item(task: Task) -> brain.TaskItem:
    return brain.TaskItem(task.title, task.due_at, task.due_kind, outside=task.source_kind == "email")


def _at(due: datetime | None, kind: str, tz: ZoneInfo) -> datetime | None:
    """A task's time today, only when it has one (a `date` task is due by the day's end: no time said)."""
    return due.astimezone(tz) if due is not None and kind == "datetime" else None
