"""`pisa.domain.voice1`: the knock, the morning on "dimmi", il tragitto.

- **The senses** (in attend's minute, so "Pisa, ferma tutto" stops them with the rest of Pisa's initiative):
  - `knock`: once a morning (key `knock:<day>`), outside quiet hours and meetings, under the 2-a-day ask cap,
    when their records hold something not said to them today. A one-yes question (`ritual_asks`, kind `knock`,
    the parts' record ids only) and an attend candidate, decided by `attend.decide` and its rails.
  - `tragitto`: the phone on the move on foot or by transit (the fixes) while the plan has them travelling
    to a known place: "Vuoi il tragitto?" once a trip (key `tragitto:<day>:<leave_at>`), the same rails.
- **The words** are made from the records when they say yes (never kept, never invented): the floor item
  (the plan's next leave time, else today's next fixed event), the morning thought of today, the morning
  sheet's lead (only with its gate open and every line still passing); the trip: the plan's next items, the
  next leave time, the forecast at arrival.
- **Their words** (`heard`, the conversation's reflex, no model call): "dimmi"/"cosa c'è?" opens the open
  offer, or the morning in the morning; "dopo" and "basta" close it, or stop the reading; "dammi tutto" opens
  the night's drawer on the phone (`NightVoice.drawer`). Nothing is done, sent or booked:
  Pisa only speaks.
- Logs carry ids and counts only.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import attend as attend_rules
from pisa.domain import dossier as dossier_rules
from pisa.domain import settings as setting_rules
from pisa.domain import surprise as surprise_rules
from pisa.domain import voice1 as rules
from pisa.domain import weather as weather_rules
from pisa.domain.owner import Owner
from pisa.domain.tasks import local_day_bounds
from pisa.logging import get_logger
from pisa.services import zone
from pisa.services.attend import asks_today, propose
from pisa.services.foreign import EVENTS, FOREIGN_EVENT, FOREIGN_TASK
from pisa.services.notifications import in_meeting
from pisa.services.plan_store import made_from_now, open_tasks
from pisa.services.rituals import ritual_on
from pisa.services.stop import stopped_now
from pisa.services.zone import located_phone

if TYPE_CHECKING:
    from pisa.services.dossier import DossierService, Parts

_log = get_logger("pisa.voice1")
# outside text never makes Pisa knock: never an event a stranger titled (an invite, one Gmail
# made, a shared calendar's), never a task made from an email. Constant SQL fragments, no input.
NEXT_OWN_EVENT = (
    "SELECT o.id, o.title, o.starts_at FROM calendar_events o WHERE NOT o.all_day "  # noqa: S608
    "AND o.status <> 'cancelled' AND o.starts_at > %s AND o.starts_at < %s "
    # judged on every row with its id (their calendar and one shared with them: foreign if either is)
    f"AND NOT coalesce((SELECT bool_or(coalesce({FOREIGN_EVENT}, false)) FROM {EVENTS} WHERE e.id = o.id), "
    "true) ORDER BY o.starts_at LIMIT 1"
)
# fails closed: no row (NULL) is foreign; the same id in two calendars is foreign if either is
EVENT_FOREIGN = f"SELECT bool_or(coalesce({FOREIGN_EVENT}, false)) FROM {EVENTS} WHERE e.id = %s"  # noqa: S608
TASK_FOREIGN = f"SELECT bool_or({FOREIGN_TASK}) FROM tasks t WHERE t.id::text = %s"  # noqa: S608
QUIET_KEY = "voice1_quiet"  # app_settings: {kind: since}: Pisa stopped offering it ("basta", 3 waved away)
WEATHER_S = 3.0  # the forecast at arrival: left out when the provider is slower
SHEET_WITHIN = timedelta(hours=20)  # the sheet's own rule: last night's, built within this


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


async def _foreign(conn: AsyncConnection, item: dict[str, Any]) -> bool:
    """A plan item whose title a stranger controls (an invite's event, an email's task).
    Fails closed: an item with no record of theirs, or one Pisa can't find, is never spoken."""
    if item.get("event_id"):
        row = await (await conn.execute(EVENT_FOREIGN, (str(item["event_id"]),))).fetchone()
    elif item.get("task_id"):
        row = await (await conn.execute(TASK_FOREIGN, (str(item["task_id"]),))).fetchone()
    else:
        return True
    return row is None or row[0] is None or bool(row[0])


def _ref(item: dict[str, Any]) -> tuple[str, str]:
    """A plan item's record: (table, id), the same as the calendar's own floor item."""
    if item.get("event_id"):
        return "calendar_events", str(item["event_id"])
    return "tasks", str(item.get("task_id") or "")


async def _asked_lately(conn: AsyncConnection, now: datetime) -> bool:
    """One offer at a time: another of Pisa's questions went out in the last ONE_AT_A_TIME, or a knock or a
    tragitto offer is still being decided."""
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM notifications WHERE ask_id IS NOT NULL AND created_at > %s) OR "
            "EXISTS (SELECT 1 FROM attend_candidates WHERE source IN ('knock', 'tragitto') "
            "AND decided_at IS NULL)",
            (now - rules.ONE_AT_A_TIME,),
        )
    ).fetchone()
    return bool(row and row[0])


class KnockService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        weather: Any = None,  # WeatherService: the forecast at arrival (optional)
        stanotte: Any = None,  # StanotteService: the sheet's gate and its lines' checks (optional)
        quarto: Any = None,
        ceo: Sequence[Any] = (),
        later: Sequence[Any] = (),
        dossier: "Callable[[Parts], DossierService] | None" = None,
    ) -> None:
        # `NightVoice.quarto_part`: il quarto d'ora, offered only as the knock's last part
        self.quarto = quarto
        # il consiglio and la prova generale, on Sunday,
        # offered only as the knock's parts
        self.ceo: list[Any] = list(ceo)
        # `Prurito.part`: il prurito's answer and a miss's reason, the morning's parts too, and the
        # only ones the same day's knock carries (`again`)
        self.later: list[Any] = list(later)
        # the last part of Pisa's last reading of the knock (a bare "ok" answers it)
        self.read_last: str | None = None
        # the morning's one dossier (the radar, their loops, the night's drafts, "domani, provato"
        # beside these parts); their yes reads at most three of it, then "È tutto". Built here from its
        # maker, with these parts: the dossier reads the knock's parts and the knock says the dossier
        self.dossier: DossierService | None = dossier(self.parts) if dossier is not None else None
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._weather = weather
        self._stanotte = stanotte

    # --- the senses (attend's minute) ---------------------------------------------------------------------

    async def knock(self, now: datetime) -> int:
        """ "Posso dirti una cosa?", once a morning, when there is something new to say."""
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            lang = str(settings["language"])
            window = settings["briefing"]["window"]
            if await stopped_now(conn):  # RAIL: "Pisa, ferma tutto" (attend's step waits too)
                return 0
            if await self._silenced(conn, "knock", now):  # their "off", their "basta", 3 waved away
                return 0
            if setting_rules.is_quiet(now, settings, self._owner.tz) or not rules.in_morning(
                now, self._owner.tz, window["start"] if window else None
            ):
                return 0
            day = now.astimezone(self._owner.zone).date()
            key = f"knock:{day.isoformat()}"
            if await (
                await conn.execute("SELECT 1 FROM attend_candidates WHERE key = %s", (key,))
            ).fetchone():
                return 0  # once a morning, whatever became of it
            if await in_meeting(conn, now) or not rules.asks_left(
                await asks_today(conn, now, self._owner.tz)
            ):
                return 0
            if await _asked_lately(conn, now):
                return 0  # one offer at a time
            said = await self._said_today(conn, now)
            parts = rules.new_parts(await self.parts(conn, now, lang), said)
            # the radar, their loops, the night's drafts and "domani, provato" knock too (one
            # knock)
            extra = [] if self.dossier is None else await self.dossier.own_items(conn, now, lang)
            parts += [  # only what Pisa may say aloud knocks (never a stranger's words, never a false "done")
                rules.Part(i.kind, i.text, i.table, i.ref)
                for i in extra
                if (i.table, i.ref) not in said and dossier_rules.speakable(i)
            ]
            if not parts:
                return 0
            ask_id = uuid.uuid4()  # the ask is kept only when the candidate is
            proposed = await propose(conn, self._candidate("knock", lang, now), key, {"ask_id": str(ask_id)})
            if proposed:
                await self._ask(conn, ask_id, "knock", rules.word(rules.KNOCK, lang), now, parts=parts)
        _log.info("knock_proposed", parts=len(parts), proposed=proposed)
        return int(proposed)

    async def again(self, now: datetime) -> int:
        """ "Posso dirti una cosa?" later the same day, only for il prurito's answer that changes
        something (`rules.SAME_DAY`), once each; every other rail of the morning's knock holds (their "off"
        and "basta", quiet hours, meetings, the 2-a-day cap, one offer at a time).

        only after the morning (the morning has its own knock, once); never on a day they
        declined a knock ("no", "dopo": `close`'s "never asked again today"); only the answers whose rows
        opened after today's last knock; never a miss's reason (the morning knock's alone)."""
        if not self.later:
            return 0
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            lang = str(settings["language"])
            local = now.astimezone(self._owner.zone).time()
            if local < rules.MORNING_END:
                return 0  # the morning's knock is the morning's, once; never before it either
            if await stopped_now(conn) or await self._silenced(conn, "knock", now):
                return 0
            if setting_rules.is_quiet(now, settings, self._owner.tz):
                return 0
            start, _ = local_day_bounds(now.astimezone(self._owner.zone).date(), self._owner.tz)
            declined, last = await (
                await conn.execute(
                    "SELECT bool_or(status = 'no' AND result IS DISTINCT FROM 'tutto'), max(created_at) "
                    "FROM ritual_asks WHERE kind = 'knock' "
                    "AND created_at >= %s",
                    (start,),
                )
            ).fetchone() or (None, None)
            if declined:
                return 0  # their "no" or "dopo" to a knock today: never asked again today
            if await in_meeting(conn, now) or not rules.asks_left(
                await asks_today(conn, now, self._owner.tz)
            ):
                return 0
            if await _asked_lately(conn, now):
                return 0
            later = await self._later(conn, now, lang, since=last or start)
            parts = rules.new_parts(later, await self._said_today(conn, now))
            parts = [p for p in parts if p.kind in rules.SAME_DAY][:1]
            if not parts:
                return 0
            key = f"knock:later:{parts[0].table}:{parts[0].ref}"
            if await (
                await conn.execute("SELECT 1 FROM attend_candidates WHERE key = %s", (key,))
            ).fetchone():
                return 0  # once each, whatever became of it
            ask_id = uuid.uuid4()
            candidate = dataclasses.replace(self._candidate("knock", lang, now), same_day=True)
            proposed = await propose(conn, candidate, key, {"ask_id": str(ask_id), "same_day": True})
            if proposed:
                await self._ask(
                    conn, ask_id, "knock", rules.word(rules.KNOCK, lang), now, parts=parts, same_day=True
                )
        _log.info("knock_again_proposed", part=parts[0].kind, proposed=proposed)
        return int(proposed)

    async def _later(
        self,
        conn: AsyncConnection,
        now: datetime,
        lang: str,
        *,
        speaking: bool = False,
        since: datetime | None = None,
    ) -> list[rules.Part]:
        """`since`: the same day's knock: what opened after today's last knock, its own
        parts only."""
        out = []
        for part_of in self.later:
            try:
                async with conn.transaction():  # a part's failure never fails the knock
                    if since is None:
                        part = await part_of(conn, now, lang, speaking=speaking)
                    else:
                        part = await part_of(conn, now, lang, speaking=speaking, since=since)
            except Exception as exc:
                _log.warning("knock_part_skipped", cause=exc.__class__.__name__)
                part = None
            if part is not None:
                out.append(part)
        return out

    async def tragitto(self, now: datetime) -> int:
        """ "Vuoi il tragitto?", once a trip, when the phone is on the way to the plan's next place."""
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            lang = str(settings["language"])
            if await stopped_now(conn) or setting_rules.is_quiet(now, settings, self._owner.tz):
                return 0
            if await in_meeting(conn, now) or await self._silenced(conn, "tragitto", now):
                return 0
            trip = await self._trip(conn, now)
            if trip is None or not await self._moving(conn, now):
                return 0
            # once a day per destination (a re-plan moves the leave time, never the place)
            key = f"tragitto:{now.astimezone(self._owner.zone).date().isoformat()}:{trip['to']}"
            if await (
                await conn.execute("SELECT 1 FROM attend_candidates WHERE key = %s", (key,))
            ).fetchone():
                return 0  # once a trip
            if not rules.asks_left(await asks_today(conn, now, self._owner.tz)) or await _asked_lately(
                conn, now
            ):
                return 0
            if not await self._trip_text(conn, now, lang, with_weather=False):
                return 0  # nothing from their records to say on the way
            ask_id = uuid.uuid4()
            proposed = await propose(
                conn, self._candidate("tragitto", lang, now), key, {"ask_id": str(ask_id)}
            )
            if proposed:
                await self._ask(conn, ask_id, "tragitto", rules.word(rules.TRAGITTO, lang), now, trip=trip)
        _log.info("tragitto_proposed", proposed=proposed)
        return int(proposed)

    def _candidate(self, kind: rules.Offer, lang: str, now: datetime) -> attend_rules.Candidate:
        text = rules.word(rules.KNOCK if kind == "knock" else rules.TRAGITTO, lang)
        # an offer, never a floor; alone: its words are the question their yes answers (never merged)
        return attend_rules.Candidate(
            str(uuid.uuid4()), kind, "info", text, lang, now, urgency="breakpoint", alone=True
        )

    async def _ask(
        self,
        conn: AsyncConnection,
        ask_id: uuid.UUID,
        kind: rules.Offer,
        question: str,
        now: datetime,
        *,
        parts: list[rules.Part] | None = None,
        trip: dict[str, Any] | None = None,
        same_day: bool = False,
    ) -> None:
        local = now.astimezone(self._owner.zone)
        _, day_end = local_day_bounds(local.date(), self._owner.tz)
        # a knock is a morning's: its question lapses at noon (a notice deferred past it won't go)
        noon = datetime.combine(local.date(), rules.MORNING_END, self._owner.zone)
        expires = min(day_end, noon) if kind == "knock" and not same_day else day_end
        payload: dict[str, Any] = {
            "parts": [{"kind": p.kind, "table": p.table, "ref": p.ref} for p in parts or []]
        }
        if trip is not None:
            payload["trip"] = {"leave_at": trip["leave_at"], "to": trip["to"]}
        await conn.execute(
            "INSERT INTO ritual_asks (id, kind, question, payload, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (ask_id, kind, question, Jsonb(payload), now, expires),
        )

    # --- the morning's parts (records only) -----------------------------------------------------------------

    async def parts(
        self, conn: AsyncConnection, now: datetime, lang: str, *, speaking: bool = False
    ) -> list[rules.Part]:
        """The floor item, the morning thought, the sheet's lead, il quarto d'ora's offer: each
        from its record, the brief's order. `speaking`: they hear them now (the quarto's offer is kept)."""
        out = [p for p in (await self._floor(conn, now, lang), await self._thought(conn, now, lang)) if p]
        try:
            async with conn.transaction():  # the sheet's checks failing never fails the morning
                sheet = await self._sheet_lead(conn, now, lang)
        except Exception as exc:
            _log.warning("knock_sheet_skipped", cause=exc.__class__.__name__)
            sheet = None
        if sheet is not None:
            out.append(sheet)
        out += await self._later(conn, now, lang, speaking=speaking)
        for part_of in self.ceo:
            try:
                async with conn.transaction():  # a part's failure never fails the morning
                    part = await part_of(conn, now, lang, speaking=speaking)
            except Exception as exc:
                _log.warning("knock_part_skipped", cause=exc.__class__.__name__)
                part = None
            if part is not None:
                out.append(part)
        if self.quarto is not None and (quarto := await self.quarto(conn, now, lang, speaking=speaking)):
            out.append(quarto)
        return rules.ordered(out)

    async def _plan(self, conn: AsyncConnection, now: datetime) -> tuple[str, list[dict[str, Any]]] | None:
        day = now.astimezone(self._owner.zone).date()
        row = await (
            await conn.execute(
                "SELECT id::text, plan, open_tasks FROM plans WHERE day = %s ORDER BY generated_at DESC "
                "LIMIT 1",
                (day,),
            )
        ).fetchone()
        if row is None or not made_from_now(row[2], await open_tasks(conn)):
            return None  # a plan made before a task was added, closed or deleted is none to say from
        return row[0], list((row[1] or {}).get("items") or [])

    async def _floor(self, conn: AsyncConnection, now: datetime, lang: str) -> rules.Part | None:
        """The day's next leave time (the plan's), else its next fixed event (the calendar's)."""
        plan = await self._plan(conn, now)
        if plan is not None:
            _, items = plan
            for i, item in enumerate(items):
                leave = _at(item.get("leave_at")) if item.get("kind") == "travel" else None
                if leave is None or not now < leave <= now + rules.FLOOR_AHEAD:
                    continue
                target = next((t for t in items[i + 1 :] if t.get("kind") in ("task", "event")), None)
                if target and target.get("title") and not await _foreign(conn, target):
                    text = rules.floor_leave(str(target["title"]), leave, self._owner.tz, lang)
                    return rules.Part("floor", text, *_ref(target))
        _, day_end = local_day_bounds(now.astimezone(self._owner.zone).date(), self._owner.tz)
        row = await (
            await conn.execute(
                NEXT_OWN_EVENT,
                (now, min(day_end, now + rules.FLOOR_AHEAD)),
            )
        ).fetchone()
        if row is None or not str(row[1]).strip():
            return None
        return rules.Part(
            "floor", rules.floor_event(str(row[1]), row[2], self._owner.tz, lang), "calendar_events", row[0]
        )

    async def _thought(self, conn: AsyncConnection, now: datetime, lang: str) -> rules.Part | None:
        """The morning thought of today, its words made again from its record (no question in it: the
        briefing's yes was its own)."""
        day = now.astimezone(self._owner.zone).date()
        row = await (
            await conn.execute("SELECT id::text, kind, ref FROM morning_thoughts WHERE day = %s", (day,))
        ).fetchone()
        if row is None:
            return None
        thought_id, kind, ref = row
        if kind == "stuck_task":
            task = await (
                await conn.execute(
                    "SELECT title, created_at FROM tasks WHERE id = %s AND status = 'open' "
                    "AND deleted_at IS NULL",
                    (ref,),
                )
            ).fetchone()
            if task is None:
                return None
            days = (day - task[1].astimezone(self._owner.zone).date()).days
            text, question = surprise_rules.stuck_task(str(task[0]), days, lang)
            text = text.replace(question, "").strip()
        else:
            trip = await (await conn.execute("SELECT to_city FROM trips WHERE id = %s", (ref,))).fetchone()
            if trip is None or not str(trip[0] or "").strip():
                return None
            text, _ = surprise_rules.welcome_back(str(trip[0]), lang)
            text = text.rsplit(". ", 1)[0] + "."  # the welcome, not its question
        return rules.Part("thought", text, "morning_thoughts", thought_id)

    async def _sheet(self, conn: AsyncConnection, now: datetime) -> dict[str, Any] | None:
        """Last night's sheet, only with its gate open and every shown line passing its check now."""
        if self._stanotte is None or not await self._stanotte.open(conn):
            return None
        row = await (
            await conn.cursor(row_factory=dict_row).execute(
                "SELECT night, data, lines FROM pisa_sheets WHERE built_at >= %s "
                "AND shown > 0 ORDER BY night DESC LIMIT 1",
                (now - SHEET_WITHIN,),
            )
        ).fetchone()
        if row is None:
            return None
        shown, passed = await self._stanotte.recheck(conn, row["lines"])
        if (
            shown == 0 or passed != shown
        ):  # RAIL: a line that no longer passes withholds it, as the sheet's own
            _log.info("knock_sheet_withheld", shown=shown, passed=passed)
            return None
        return row

    async def _sheet_lead(self, conn: AsyncConnection, now: datetime, lang: str) -> rules.Part | None:
        sheet = await self._sheet(conn, now)
        if sheet is None:
            return None
        lead = next(
            (
                b["text"]
                for b in (sheet["data"] or {}).get("blocks", [])
                if b.get("type") == "text" and b.get("text")
            ),
            None,
        )
        if not lead:
            return None
        return rules.Part(
            "sheet", rules.sheet_lead(str(lead), lang), "pisa_sheets", sheet["night"].isoformat()
        )

    async def _said_today(self, conn: AsyncConnection, now: datetime) -> set[tuple[str, str]]:
        """The records already said to them today (by id, never by words): what a knock they opened
        carried; with a briefing they got today, the morning thought (only a briefing carries it) and the
        events of the day that were in their calendar by then (the briefing's agenda)."""
        start, end = local_day_bounds(now.astimezone(self._owner.zone).date(), self._owner.tz)
        said: set[tuple[str, str]] = set()
        for (payload,) in await (
            await conn.execute(
                "SELECT payload FROM ritual_asks WHERE kind = 'knock' AND status = 'yes' "
                "AND created_at >= %s",
                (start,),
            )
        ).fetchall():
            said |= {(str(p["table"]), str(p["ref"])) for p in (payload or {}).get("parts", [])}
        briefed = await (
            await conn.execute(
                "SELECT min(b.created_at) FROM briefings b JOIN notifications n ON n.id = b.notification_id "
                "WHERE b.created_at >= %s AND n.delivered_via IS NOT NULL "
                "AND n.delivered_via NOT IN ('none', 'held')",
                (start,),
            )
        ).fetchone()
        if briefed is None or briefed[0] is None:
            return said
        said |= {
            ("morning_thoughts", str(r[0]))
            for r in await (
                await conn.execute("SELECT id FROM morning_thoughts WHERE created_at >= %s", (start,))
            ).fetchall()
        }
        said |= {
            ("calendar_events", str(r[0]))
            for r in await (
                await conn.execute(
                    "SELECT id FROM calendar_events WHERE starts_at >= %s AND starts_at < %s "
                    "AND coalesce(updated, starts_at - interval '1 day') <= %s",
                    (start, end, briefed[0]),
                )
            ).fetchall()
        }
        return said

    # --- when Pisa stops offering --------------------------------------------------------

    async def _silenced(self, conn: AsyncConnection, kind: rules.Offer, now: datetime) -> bool:
        """Their "off" (`knock`/`tragitto`), their "basta" to it, or the last 3 offers that reached them all
        declined or let lapse since they last said "dimmi"."""
        if not await ritual_on(conn, kind):
            return True
        quiet = dict(await get_setting(conn, QUIET_KEY) or {})
        state = quiet.get(kind) or {}
        if state.get("quiet"):
            return True
        since = _at(state.get("since")) or datetime(2000, 1, 1, tzinfo=UTC)
        rows = await (
            await conn.execute(
                "SELECT CASE WHEN a.status = 'open' AND a.expires_at <= %s THEN 'lapsed' ELSE a.status END "
                "FROM ritual_asks a JOIN notifications n ON n.ask_id = a.id WHERE a.kind = %s "
                "AND a.created_at > %s AND n.delivered_via IS NOT NULL AND n.delivered_via <> 'none' "
                "ORDER BY a.created_at DESC LIMIT %s",
                (now, kind, since, rules.WAVED_AWAY),
            )
        ).fetchall()
        return rules.waved_away([str(r[0]) for r in rows])

    async def hush(self, kind: str) -> None:
        """Their "basta" to an offer: Pisa stops offering that kind until their "dimmi"."""
        await self._quiet(kind, True)

    async def welcome(self) -> None:
        """Their "dimmi": the offers come back, and older waved-away ones no longer count."""
        for kind in rules.OFFERS:
            await self._quiet(kind, False)

    async def _quiet(self, kind: str, on: bool) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (QUIET_KEY,))
            quiet = dict(await get_setting(conn, QUIET_KEY) or {})
            if not on and not (quiet.get(kind) or {}).get("quiet") and kind in quiet:
                return  # already welcome: keep its `since`
            quiet[kind] = {"quiet": on, "since": self._clock().isoformat()}
            await set_setting(conn, QUIET_KEY, Jsonb(quiet))
        _log.info("voice1_quiet", kind=kind, quiet=on)

    # --- il tragitto ---------------------------------------------------------------------------------

    async def _moving(self, conn: AsyncConnection, now: datetime) -> bool:
        device = await located_phone(conn)
        if device is None:
            return False
        fixes = await zone.fixes(conn, device, now - rules.MOVING_WITHIN, now)
        state = await (
            await conn.execute("SELECT state->>'route' FROM device_presence WHERE device_id = %s", (device,))
        ).fetchone()
        return rules.moving(fixes, now, state[0] if state else None)

    async def _trip(self, conn: AsyncConnection, now: datetime) -> dict[str, Any] | None:
        """The plan's travel item they are on now, to a known place (one of their places)."""
        plan = await self._plan(conn, now)
        if plan is None:
            return None
        for item in plan[1]:
            leave, end = _at(item.get("leave_at")), _at(item.get("end"))
            if item.get("kind") != "travel" or leave is None or end is None or not item.get("to_place_id"):
                continue
            if leave - rules.TRIP_SLACK <= now <= end + rules.TRIP_SLACK:
                place = await (
                    await conn.execute(
                        "SELECT lat, lon FROM places WHERE id::text = %s AND lat IS NOT NULL",
                        (str(item["to_place_id"]),),
                    )
                ).fetchone()
                if place is not None:
                    return {
                        "leave_at": leave.isoformat(),
                        "end": end,
                        "coord": (place[0], place[1]),
                        "to": str(item["to_place_id"]),
                    }
        return None

    async def _trip_text(self, conn: AsyncConnection, now: datetime, lang: str, *, with_weather: bool) -> str:
        plan = await self._plan(conn, now)
        trip = await self._trip(conn, now)
        if plan is None or trip is None:
            return ""
        items = plan[1]
        upcoming = [
            (str(t["title"]), at)
            for t in items
            if t.get("kind") in ("task", "event")
            and t.get("title")
            and (at := _at(t.get("start")))
            and at > now
            and not await _foreign(conn, t)  # never a stranger's title
        ]
        leave = None
        for i, item in enumerate(items):
            at = _at(item.get("leave_at")) if item.get("kind") == "travel" else None
            if at is None or at <= trip["end"]:
                continue
            target = next((t for t in items[i + 1 :] if t.get("kind") in ("task", "event")), None)
            if target and target.get("title") and not await _foreign(conn, target):
                leave = (str(target["title"]), at)
                break
        arrival = await self._arrival(trip, lang) if with_weather else None
        return rules.tragitto(upcoming, leave, arrival, self._owner.tz, lang)

    async def _arrival(self, trip: dict[str, Any], lang: str) -> tuple[str, float, int] | None:
        if self._weather is None:
            return None
        try:
            forecast = await asyncio.wait_for(self._weather.forecast(trip["coord"]), WEATHER_S)
        except Exception as exc:  # the forecast is Pisa's own cache or its provider: never fails the trip
            _log.info("tragitto_weather_skipped", cause=exc.__class__.__name__)
            return None
        hour = trip["end"].replace(minute=0, second=0, microsecond=0)
        found = next((h for h in forecast.hours if h.time == hour), None)
        if found is None:
            return None
        return weather_rules.summary(found.code, lang), found.temp_c, found.precip_prob

    # --- what they hear
    # --------------------------------------------------------------------------------------

    async def morning(self, lang: str) -> str:
        """The morning, spoken. One finite dossier, at most three items, the rest on the phone,
        then "È tutto" (without a dossier: the parts in the brief's order, short)."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            if self.dossier is None:
                return self._read(await self.parts(conn, now, lang, speaking=True))
            text, items = await self.dossier.say(conn, now, lang, await self.parts(conn, now, lang))
            spoken = [i for i in items if i.spoken]
            await self._keep_said(conn, now, lang, {i.kind for i in spoken})
            self.read_last = spoken[-1].kind if spoken else None
            return str(text)

    async def _keep_said(self, conn: AsyncConnection, now: datetime, lang: str, kinds: set[str]) -> None:
        """The parts whose offer or answer is kept once said (il quarto d'ora's offer, il
        prurito's
        answer told, il consiglio's and la prova's week) are kept only when the dossier spoke them."""
        producers = [*self.later, *self.ceo, *([self.quarto] if self.quarto is not None else [])]
        for part_of in producers:
            try:
                async with conn.transaction():  # a part's failure never fails the morning
                    part = await part_of(conn, now, lang)
                    if part is not None and part.kind in kinds:
                        await part_of(conn, now, lang, speaking=True)
            except Exception as exc:
                _log.warning("knock_part_skipped", cause=exc.__class__.__name__)

    async def same_day(self, lang: str) -> str:
        """The same day's knock opened: only its own parts (il prurito's answer;
        never a miss's reason, the morning knock's alone)."""
        now = self._clock()
        start, _ = local_day_bounds(now.astimezone(self._owner.zone).date(), self._owner.tz)
        async with self._pool.connection() as conn, conn.transaction():
            later = await self._later(conn, now, lang, speaking=True, since=start)
            return self._read([p for p in later if p.kind in rules.SAME_DAY])

    def _read(self, parts: list[rules.Part]) -> str:
        """Pisa's reading of the knock, in the brief's order; its last part kept."""
        said = rules.ordered(parts)
        self.read_last = said[-1].kind if said else None
        return rules.spoken(said)

    async def trip(self, lang: str) -> str:
        async with self._pool.connection() as conn:
            return await self._trip_text(conn, self._clock(), lang, with_weather=True)

    async def opened(self, kind: str, lang: str, payload: dict[str, Any] | None = None) -> str:
        """Their yes to the knock or the tragitto offer: what it carries, made now from the records. The same
        day's knock carries only its own parts."""
        if kind != "knock":
            return await self.trip(lang)
        kinds = {str(p.get("kind")) for p in (payload or {}).get("parts") or []}
        if kinds and kinds <= rules.SAME_DAY and not await self.in_morning():
            return await self.same_day(lang)
        return await self.morning(lang)

    async def close(self, ask_id: uuid.UUID, how: str) -> None:
        """Their "dopo" or "basta" to an open offer: closed, never asked again today (a declined knock never
        counts against Pisa's restraint: it is theirs to decline)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE ritual_asks SET status = 'no', answered_at = %s, answered_by = 'voice', result = %s "
                "WHERE id = %s AND status = 'open'",
                (self._clock(), how, ask_id),
            )
        _log.info("voice1_offer_closed", ask_id=str(ask_id), how=how)

    async def open_kind(self, ask_id: uuid.UUID) -> str | None:
        """The offer's kind when this ask is an open knock or tragitto, else None."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT kind FROM ritual_asks WHERE id = %s AND status = 'open' AND kind = ANY(%s) "
                    "AND expires_at > %s",
                    (ask_id, list(rules.OFFERS), self._clock()),
                )
            ).fetchone()
        return str(row[0]) if row else None

    async def briefed_last(self, device_id: uuid.UUID) -> bool:
        """Pisa's last thing to them was today's briefing (any not held by quiet hours, newer than
        Pisa's last reply to this device): their bare "dimmi" then asks for the morning."""
        now = self._clock()
        start, _ = local_day_bounds(now.astimezone(self._owner.zone).date(), self._owner.tz)
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT (SELECT max(b.created_at) FROM briefings b JOIN notifications n "
                    "ON n.id = b.notification_id WHERE b.created_at >= %s "
                    "AND coalesce(n.delivered_via, '') <> 'held'), "
                    "(SELECT max(m.created_at) FROM messages m "
                    "JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.device_id = %s AND m.role = 'assistant')",
                    (start, device_id),
                )
            ).fetchone()
        return row is not None and row[0] is not None and (row[1] is None or row[0] > row[1])

    async def carries(self, now: datetime) -> bool:
        """From midnight until noon, with the knock on (not their "off",
        not waved away, Pisa not stopped), the dossier will carry their loops: their own nudges wait for it.
        From midnight, not the window's start: a loop due today would be nudged at 00:00 and spoken again
        (r2)."""
        if now.astimezone(self._owner.zone).time() >= rules.MORNING_END:
            return False
        async with self._pool.connection() as conn:
            return not (await stopped_now(conn) or await self._silenced(conn, "knock", now))

    async def in_morning(self) -> bool:
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
        window = settings["briefing"]["window"]
        return rules.in_morning(self._clock(), self._owner.tz, window["start"] if window else None)
