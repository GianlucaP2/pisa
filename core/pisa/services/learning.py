"""What Pisa learns from what the user does.

**Signals** (`collect`, nightly, from what was observed since the last run), each recorded once as
`profile_evidence` with its scope, weight and a readable summary:
- S1 went back to a place (a stay of 20 minutes or more; near home or work it counts half; at most 3 a month);
- S2 lateness per kind of event (the stay at the event's place vs its start; precise stays only);
- S3 a "go" (directions) or a visit after a places card (the first slot weighs half: position bias); a card
  shown or tapped is never a like, a tap is kept as `interest`; S11 a card dismissed (0.1
  against);
- S4 routines (the same place, weekday and time, 3 times in 4 weeks);
- S5 walks between stays (their minutes: the 80th percentile becomes the walking limit);
- S8 a check-in answered "another place" (0.3 against that place).

**Never evidence**: anything observed while learning was paused, places of health, worship or politics, home
and work themselves (except for routines), sensitive subjects, off-limits topics, forgotten claims
(tombstones), and any text from outside: an event gives only its time, its place and how many others attend;
reviews, pages and emails are never read here.

**Consolidation** (`consolidate`, nightly at 03:30 in the user's zone, after the signals): evidence →
`observed` claims with the confidence rules of pisa.domain.learning (no model). **Travelling**
(`update_travelling`): a night more than 50 km from home starts it, a night back home ends it.

**The weekly check** (`weekly`, `answer`, `weekly_tick`): at most 3 items, offered Sunday 18:00 (local) as a
notify (kind info, weekly) at most once a week. **Buffers**: a lateness item answered "yes" sets that kind of
event's buffer, which the planner adds to its "leave now".
"""

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import home as home_rules
from pisa.domain import learning as rules
from pisa.domain import profile as profile_rules
from pisa.domain import schedule
from pisa.domain.errors import PisaError
from pisa.domain.lexicon.weekdays import WEEKDAYS_SHORT
from pisa.domain.owner import Owner
from pisa.domain.places import WALK_MAX_MIN, distance_m
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services.notifications import enqueue
from pisa.services.places import city_of
from pisa.services.profile import ProfileService, pause_windows

_log = get_logger("pisa.learning")
Coord = tuple[float, float]

WATERMARK_KEY = "profile_signals_until"  # evidence is collected up to here
# RAIL: the weekly check's window and rate are rails.
WEEKLY_OPEN = timedelta(days=1)  # Sunday 18:00 to Monday 18:00 (`schedule.WEEKLY_CHECK`'s window)
BUFFERS_DECLINED_KEY = "profile_buffers_declined"  # event kinds the user said no to: never asked again
FIRST_RUN = timedelta(days=28)  # the first run learns from the last 4 weeks
SETTLE = timedelta(hours=2)  # stays are still being worked out for this long
HISTORY = timedelta(days=90)  # stays read for "went back"
NIGHT_AT = time(3, 0)  # where the phone is at 03:00 is where the user slept
AT_HOME_M = 100.0  # a stay this close to home or work is there (no taste evidence)
ASKED_AGAIN_AFTER = timedelta(days=21)  # a claim in the weekly check isn't asked about again before this
OBSERVED = ("S1", "S2", "S3", "S4", "S5", "S8", "S11")  # never `interest` (a tap) nor `extracted`
ANSWERS = {"buffer": ("yes", "no")}
CLAIM_ANSWERS = ("right", "wrong", "forget")


@dataclass
class Life:
    """Where the user sleeps and works, worked out from stays (never told to Pisa by anyone else)."""

    home: Coord | None = None
    work: Coord | None = None
    nights: dict[date, Coord] = field(default_factory=dict)

    def where(self, local_day: date) -> str | None:
        """home / travel on that day: travel when the night before it or after it was > 50 km from home."""
        nights = [n for d in (local_day - timedelta(days=1), local_day) if (n := self.nights.get(d))]
        if self.home is None or not nights:
            return None
        far = any(distance_m(self.home, n) > rules.TRAVEL_KM * 1000 for n in nights)
        return "travel" if far else "home"

    def near(self, coord: Coord, metres: float) -> bool:
        return any(p is not None and distance_m(p, coord) <= metres for p in (self.home, self.work))


@dataclass(frozen=True)
class Row:
    """One piece of evidence to record."""

    signal: str
    ref: str
    kind: str
    subject: str
    weight: float
    direction: int
    scope: dict[str, str]
    at: datetime
    summary: str
    value: dict[str, Any] | None = None


async def event_buffers(conn: AsyncConnection) -> dict[str, int]:
    """Minutes to leave earlier, per kind of event (pisa.domain.learning.event_kind): only where the user said
    yes in the weekly check. The planner adds them to the trip, so "leave now" comes that much earlier."""
    rows = await (
        await conn.execute(
            "SELECT subject_key, confirmed_value FROM profile_claims WHERE subject_key LIKE 'lateness:%%' "
            "AND state <> 'retired' AND confirmed_value ? 'buffer_min'"
        )
    ).fetchall()
    return {key.split(":", 1)[1]: int(value["buffer_min"]) for key, value in rows}


_cell = home_rules.cell
# ~300 m: places of life are found by cells this size


def _moved(nights: dict[date, Coord], today: date, known: list[float]) -> bool:
    """The user lives somewhere else now: MOVED_NIGHTS of the last MOVED_DAYS nights in one place far from
    the known home (a trip that long is a move, so home is learned again and the trip can end)."""
    recent = [c for d, c in nights.items() if d >= today - timedelta(days=rules.MOVED_DAYS)]
    if len(recent) < rules.MOVED_NIGHTS:
        return False
    cell, count = Counter(_cell(c) for c in recent).most_common(1)[0]
    if count < rules.MOVED_NIGHTS:
        return False
    members = [c for c in recent if _cell(c) == cell]
    centre = (sum(c[0] for c in members) / len(members), sum(c[1] for c in members) / len(members))
    return distance_m((float(known[0]), float(known[1])), centre) > rules.TRAVEL_KM * 1000


class LearningService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        profile: ProfileService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._profile = profile
        self._owner = owner
        self._clock = clock

    def _local(self, at: datetime) -> datetime:
        return at.astimezone(self._owner.zone)

    # --- the nightly run ------------------------------------------------------------------------------------

    async def nightly(self) -> dict[str, int]:
        """The night's learning: the signals since the last run, then travelling, then consolidation. The
        worker's `learn` step runs it once a day from 03:30 in the user's zone now (`schedule.NIGHT_LEARNING`,
        spent once it starts: a failing night is logged once, never retried every minute); Pisa's own
        initiative, so "Pisa, ferma tutto" pauses it."""
        recorded = await self.collect()
        async with self._pool.connection() as conn:
            paused = await self._profile.paused_until(conn) is not None
        if paused:  # nothing learned at all while paused: no travel, no claims
            _log.info("profile_nightly_paused")
            return {"evidence": recorded, "claims": 0}
        await self.update_travelling()
        claims = await self.consolidate()
        _log.info("profile_nightly", evidence=recorded, claims=claims)
        return {"evidence": recorded, "claims": claims}

    # --- where the user lives ------------------------------------------------------------------------------

    async def _life(self, conn: AsyncConnection, now: datetime) -> Life:
        rows = await (
            await conn.execute(
                "SELECT arrived_at, coalesce(departed_at, last_seen_at), lat, lon FROM stays "
                "WHERE coalesce(departed_at, last_seen_at) > %s ORDER BY arrived_at",
                (now - timedelta(days=40),),
            )
        ).fetchall()
        life = Life()
        today = self._local(now).date()
        for back in range(1, 36):
            day = today - timedelta(days=back)
            instant = datetime.combine(day + timedelta(days=1), NIGHT_AT, tzinfo=self._owner.zone)
            for arrived, left, lat, lon in rows:
                if arrived <= instant <= left:
                    life.nights[day] = (lat, lon)
                    break
        await self._home(conn, life, today)
        hours: Counter[tuple[int, int]] = Counter()
        days: dict[tuple[int, int], set[date]] = defaultdict(set)
        spots: dict[tuple[int, int], Coord] = {}
        for arrived, left, lat, lon in rows:
            local = self._local(arrived)
            if local.isoweekday() >= 6 or not 9 <= local.hour < 17:
                continue
            if life.home is not None and distance_m(life.home, (lat, lon)) <= AT_HOME_M * 3:
                continue
            cell = _cell((lat, lon))
            hours[cell] += int((min(left, arrived + timedelta(hours=9)) - arrived).total_seconds() / 3600)
            days[cell].add(local.date())
            spots.setdefault(cell, (lat, lon))
        for cell, total in hours.most_common(1):
            if total >= 8 and len(days[cell]) >= 3:
                life.work = spots[cell]
        return life

    async def _home(self, conn: AsyncConnection, life: Life, today: date) -> None:
        """The user's home (pisa.domain.home): theirs or a confirmed one as it is; a learned guess kept
        as it was during a trip unless the user moved; else learned again, only from
        LEARN_NIGHTS nights after the user last forgot it. None: Pisa doesn't know where the user lives."""
        known = await home_store.read(conn)
        if known is not None and known.fixed:
            life.home = known.coord
            return
        travelling = await home_store.travelling(conn)
        if known is not None and travelling and not _moved(life.nights, today, list(known.coord)):
            life.home = known.coord
            return
        forgot = await home_store.forgotten_at(conn)
        nights = life.nights
        if forgot is not None:
            nights = {d: c for d, c in nights.items() if d >= self._local(forgot).date()}
        learned = home_rules.learn(nights, today)
        if learned is None:
            life.home = known.coord if known is not None else None
            return
        life.home = learned
        if known is None or known.coord != learned:
            near = known is not None and distance_m(known.coord, learned) <= AT_HOME_M
            name = known.name if near and known is not None else await self._city(conn, learned)
            await home_store.write(
                conn, home_rules.Home(learned[0], learned[1], name, "learned", False), forgotten=forgot
            )

    async def _pause_windows(self, conn: AsyncConnection) -> list[tuple[datetime, datetime]]:
        return await pause_windows(conn)

    async def _company(self, conn: AsyncConnection, at: datetime) -> str | None:
        """ "others" when an event with guests was going on, "alone" when one without was; else unknown."""
        row = await (
            await conn.execute(
                "SELECT max(attendees) FROM calendar_events WHERE NOT all_day AND status <> 'cancelled' "
                "AND starts_at <= %s AND ends_at > %s AND attendees IS NOT NULL",
                (at, at),
            )
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return "others" if row[0] > 0 else "alone"

    # --- the signals ----------------------------------------------------------------------------------------

    async def collect(self) -> int:
        """Record what was observed since the last run (up to 2 hours ago). Nothing while paused, nothing
        from a pause window, nothing blocked (sensitive, off-limits, forgotten). Each item once (its source
        row)."""
        now = self._clock()
        until = now - SETTLE
        async with self._pool.connection() as conn:
            settings = await self._owner.settings(conn)
            lang = str(settings["language"])
            if await self._profile.paused_until(conn) is not None:
                await set_setting(conn, WATERMARK_KEY, Jsonb(until.isoformat()))
                _log.info("profile_signals_paused")
                return 0
            mark = await get_setting(conn, WATERMARK_KEY)
            since = datetime.fromisoformat(mark) if mark else now - FIRST_RUN
            life = await self._life(conn, now)
            windows = await self._pause_windows(conn)
            rows: list[Row] = []
            rows += await self._stays(conn, life, since, until, lang)
            rows += await self._lateness(conn, life, since, until, lang)
            rows += await self._outcomes(conn, life, since, until, lang)
            rows += await self._check_ins(conn, life, since, until, lang)
            names = await self._names(conn, {r.subject for r in rows})
            recorded = 0
            async with conn.transaction():
                for r in rows:
                    if any(start <= r.at < end for start, end in windows):
                        continue  # learning was paused then
                    sample = rules.statement(r.subject, r.value or {"minutes": 10}, names, lang)
                    if await self._profile.blocked(conn, r.kind, r.subject, sample):
                        continue
                    cur = await conn.execute(
                        "INSERT INTO profile_evidence (id, claim_id, signal, ref, weight, direction, scope, "
                        "observed_at, summary, kind, subject_key, value) VALUES (%s, NULL, %s, %s, %s, %s, "
                        "%s, "
                        "%s, %s, %s, %s, %s) ON CONFLICT (signal, ref, subject_key) WHERE subject_key IS NOT "
                        "NULL DO NOTHING",
                        (
                            uuid.uuid4(),
                            r.signal,
                            r.ref,
                            r.weight,
                            r.direction,
                            Jsonb(r.scope),
                            r.at,
                            r.summary[:200],
                            r.kind,
                            r.subject,
                            Jsonb(r.value) if r.value is not None else None,
                        ),
                    )
                    recorded += cur.rowcount
                await set_setting(conn, WATERMARK_KEY, Jsonb(until.isoformat()))
        _log.info("profile_signals", recorded=recorded)
        return recorded

    def _scope(self, at: datetime, life: Life, company: str | None) -> dict[str, str]:
        local = self._local(at)
        return rules.scope_at(local, company=company, where=life.where(local.date()))

    def _when(self, at: datetime, lang: str) -> str:
        local = self._local(at)
        return f"{WEEKDAYS_SHORT[lang if lang in WEEKDAYS_SHORT else 'en-US'][local.weekday()]} {local:%H:%M}"

    async def _stays(
        self, conn: AsyncConnection, life: Life, since: datetime, until: datetime, lang: str
    ) -> list[Row]:
        """S1 went back, S4 routines, S5 walks between stays."""
        it = lang == "it-IT"
        cur = conn.cursor(row_factory=dict_row)
        stays = await (
            await cur.execute(
                "SELECT s.device_id, s.arrived_at, s.departed_at, s.lat, s.lon, s.accuracy_m, "
                "s.place_id::text AS place_id, p.name, p.category FROM stays s "
                "LEFT JOIN places p ON p.id = s.place_id WHERE s.arrived_at >= %s AND s.departed_at IS NOT "
                "NULL "
                "ORDER BY s.arrived_at",
                (until - HISTORY,),
            )
        ).fetchall()
        rows: list[Row] = []
        seen_before: dict[str, datetime] = {}
        returns: Counter[tuple[str, str]] = Counter()
        routine_groups: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        for s in stays:
            long_enough = s["departed_at"] - s["arrived_at"] >= rules.STAY_MIN
            pid, coord = s["place_id"], (s["lat"], s["lon"])
            if not long_enough or pid is None or not rules.taste_place(s["name"], s["category"]):
                continue
            at_home = life.near(coord, AT_HOME_M)
            local = self._local(s["arrived_at"])
            if not at_home and s["arrived_at"] >= until - rules.ROUTINE_WINDOW:
                hour = round(local.hour + local.minute / 60) % 24  # 17:57 is "around 18"
                routine_groups[(pid, local.weekday(), hour)].append(s)
            # Back after the previous visit, not the first: a stay split by a short gap
            # (a phone call outside, a walk round the block) is the same visit.
            previous = seen_before.get(pid)
            seen_before[pid] = s["departed_at"]
            if previous is None or at_home or s["arrived_at"] - previous < timedelta(hours=4):
                continue
            month = (pid, f"{local:%Y-%m}")
            returns[month] += 1
            if returns[month] > rules.RETURNS_PER_MONTH or not since < s["departed_at"] <= until:
                continue
            weight = rules.RETURN_WEIGHT / 2 if life.near(coord, rules.NEAR_HOME_M) else rules.RETURN_WEIGHT
            company = await self._company(conn, s["arrived_at"])
            rows.append(
                Row(
                    "S1",
                    f"{pid}@{s['arrived_at']:%Y-%m-%dT%H:%M}",
                    "like",
                    f"place:{pid}",
                    weight,
                    1,
                    self._scope(s["arrived_at"], life, company),
                    s["arrived_at"],
                    (
                        f"Tornato a {rules.clean_name(s['name'])}, "
                        if it
                        else f"Went back to {rules.clean_name(s['name'])}, "
                    )
                    + self._when(s["arrived_at"], lang),
                )
            )
        for (pid, dow, hour), group in routine_groups.items():
            if len({self._local(g["arrived_at"]).date() for g in group}) < rules.ROUTINE_MIN:
                continue
            for g in group:
                if not since < g["departed_at"] <= until:
                    continue  # recorded by an earlier run (or before a pause moved the watermark on)
                name = rules.clean_name(g["name"])
                when = f"{WEEKDAYS_SHORT['it-IT' if it else 'en-US'][dow]} {hour:02d}"
                rows.append(
                    Row(
                        "S4",
                        f"{pid}@{g['arrived_at']:%Y-%m-%dT%H:%M}",
                        "routine",
                        f"routine:{pid}:{dow}:{hour}",
                        1.0,
                        1,
                        self._scope(g["arrived_at"], life, None),
                        g["arrived_at"],
                        f"Da {name}, {when}" if it else f"At {name}, {when}:00",
                    )
                )
        by_device: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for s in stays:
            by_device[s["device_id"]].append(s)
        for items in by_device.values():
            for a, b in pairwise(items):
                if not since < b["arrived_at"] <= until:
                    continue
                minutes = (b["arrived_at"] - a["departed_at"]).total_seconds() / 60
                metres = distance_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                if rules.trip_mode(metres, minutes) != "walk":
                    continue
                m = round(minutes)
                rows.append(
                    Row(
                        "S5",
                        f"trip@{b['arrived_at']:%Y-%m-%dT%H:%M}",
                        "mobility",
                        "mobility:walk",
                        1.0,
                        1,
                        self._scope(b["arrived_at"], life, None),
                        b["arrived_at"],
                        (f"{m} minuti a piedi, " if it else f"{m} minutes on foot, ")
                        + self._when(b["arrived_at"], lang),
                        {"minutes": m},
                    )
                )
        return rows

    async def _lateness(
        self, conn: AsyncConnection, life: Life, since: datetime, until: datetime, lang: str
    ) -> list[Row]:
        """S2: the user's arrival at a timed event with a known place (precise stays only), only for events
        the user accepted or organised (answer 1; unknown: skipped). The event gives its
        kind (from its title's words), its time and its place: never its text."""
        it = lang == "it-IT"
        events = await (
            await conn.execute(
                "SELECT e.calendar_id, e.id, e.title, e.starts_at, e.ends_at, e.attendees, p.lat, p.lon, "
                "p.name, p.category "
                "FROM calendar_events e JOIN places p ON p.id = e.place_id WHERE NOT e.all_day "
                # never an event Pisa made (arriving late to Pisa's plan is no habit of theirs)
                "AND e.accepted AND NOT e.made_by_pisa AND e.status <> 'cancelled' AND p.lat IS NOT NULL "
                "AND e.starts_at > %s "
                "AND e.starts_at <= %s",
                (since, until),
            )
        ).fetchall()
        rows: list[Row] = []
        for calendar_id, event_id, title, start, end, attendees, lat, lon, name, category in events:
            if rules.sensitive_place(name, category):
                continue  # a visit at a hospital, a church, a party office: never evidence
            stay = await (
                await conn.execute(
                    "SELECT arrived_at, lat, lon FROM stays WHERE accuracy_m <= %s AND arrived_at < %s "
                    "AND coalesce(departed_at, last_seen_at) > %s ORDER BY arrived_at",
                    (rules.LATE_ACCURACY_M, end, start - timedelta(hours=1)),
                )
            ).fetchall()
            near = [s for s in stay if distance_m((s[1], s[2]), (lat, lon)) <= rules.LATE_REACH_M]
            if not near or near[0][0] < start - timedelta(hours=1):
                continue  # not there, or there long before (an event at the office or at home)
            late = round((near[0][0] - start).total_seconds() / 60)
            kind = rules.event_kind(title, attendees)
            it_name, en_name = rules.EVENT_KIND_NAMES[kind]
            company = "others" if attendees else ("alone" if attendees == 0 else None)
            if late >= rules.LATE_MIN:
                summary = (
                    f"Arrivato {late} min dopo l'inizio ({it_name}), "
                    if it
                    else (f"Arrived {late} min after the start ({en_name}), ")
                )
            else:
                summary = f"Arrivato in orario ({it_name}), " if it else f"Arrived on time ({en_name}), "
            rows.append(
                Row(
                    "S2",
                    f"event:{calendar_id}:{event_id}:{start:%Y-%m-%dT%H:%M}",
                    "routine",
                    f"lateness:{kind}",
                    1.0,
                    1 if late >= rules.LATE_MIN else -1,
                    self._scope(start, life, company),
                    start,
                    summary + self._when(start, lang),
                    {"late_min": max(late, -60)},
                )
            )
        return rows

    async def _outcomes(
        self, conn: AsyncConnection, life: Life, since: datetime, until: datetime, lang: str
    ) -> list[Row]:
        """S3 a "go" (directions) or a visit after a card (position bias: the first slot weighs half), S11
        dismissed. A card shown or tapped ("plan") is never a like: a tap is
        kept as interest (signal `interest`, which no claim reads). A critique is their words: the night's
        pass reads them in the conversation (`pisa.services.memory`), never as a raw sentence here."""
        it = lang == "it-IT"
        cur = conn.cursor(row_factory=dict_row)
        rows_db = await (
            await cur.execute(
                "SELECT o.id, o.card_id, o.place_id::text AS place_id, o.action, o.created_at, "
                "p.name, p.category, (SELECT array_position(s.shown, o.place_id) FROM "
                "recommendation_outcomes s "
                "WHERE s.card_id = o.card_id AND s.action = 'shown' LIMIT 1) AS slot "
                "FROM recommendation_outcomes o LEFT JOIN places p ON p.id = o.place_id "
                "WHERE o.action IN ('go', 'plan', 'went', 'dismissed') "
                "AND o.created_at > %s AND o.created_at <= %s ORDER BY o.created_at",
                (since, until),
            )
        ).fetchall()
        rows: list[Row] = []
        for o in rows_db:
            if o["place_id"] is None or not rules.taste_place(o["name"], o["category"]):
                continue
            company = await self._company(conn, o["created_at"])
            scope = self._scope(o["created_at"], life, company)
            day = self._when(o["created_at"], lang)
            subject = f"place:{o['place_id']}"
            name = rules.clean_name(o["name"])
            if o["action"] == "dismissed":
                rows.append(
                    Row(
                        "S11",
                        f"dismissed:{o['card_id']}",
                        "like",
                        subject,
                        rules.IGNORED,
                        -1,
                        scope,
                        o["created_at"],
                        f"Scheda chiusa senza scegliere {name}"
                        if it
                        else f"Card closed without picking {name}",
                    )
                )
                continue
            slot = int(o["slot"] or 1)
            weight = rules.PICK_FIRST if slot == 1 else rules.PICK_OTHER
            if o["action"] == "plan":  # a tap: interest, never a taste
                rows.append(
                    Row(
                        rules.INTEREST,
                        f"card:{o['card_id']}",
                        "like",
                        subject,
                        weight,
                        1,
                        scope,
                        o["created_at"],
                        (
                            f"Toccato {name} su una scheda di Pisa, "
                            if it
                            else f"Tapped {name} on Pisa's card, "
                        )
                        + day,
                    )
                )
                continue
            what = {"go": ("Chiesto la strada per", "Asked the way to"), "went": ("Andato a", "Went to")}
            verb = what[o["action"]][0 if it else 1]
            rows.append(
                Row(
                    "S3",
                    f"card:{o['card_id']}",  # once a card and place: a "go" then a visit is one pick
                    "like",
                    subject,
                    weight,
                    1,
                    scope,
                    o["created_at"],
                    (
                        f"{verb} {name} da una scheda di Pisa (posto {slot}), "
                        if it
                        else f"{verb} {name} from Pisa's card (slot {slot}), "
                    )
                    + day,
                )
            )
        return rows

    async def _check_ins(
        self, conn: AsyncConnection, life: Life, since: datetime, until: datetime, lang: str
    ) -> list[Row]:
        """S8: a check-in answered "another place", without a reason: a little against that place only.
        "Skip" says nothing about taste (no reason given)."""
        it = lang == "it-IT"
        found = await (
            await conn.execute(
                "SELECT c.id, c.answered_at, p.id::text, p.name, p.category FROM check_ins c "
                "LEFT JOIN calendar_events e ON e.id = c.event_id LEFT JOIN tasks t ON t.id = c.task_id "
                "JOIN places p ON p.id = coalesce(e.place_id, t.place_id) WHERE c.answer = 'alternative' "
                "AND c.answered_at > %s AND c.answered_at <= %s",
                (since, until),
            )
        ).fetchall()
        rows: list[Row] = []
        for check_in_id, at, pid, name, category in found:
            if not rules.taste_place(name, category):
                continue
            rows.append(
                Row(
                    "S8",
                    f"check_in:{check_in_id}",
                    "like",
                    f"place:{pid}",
                    rules.REJECTED,
                    -1,
                    self._scope(at, life, None),
                    at,
                    f"Chiesto un altro posto invece di {rules.clean_name(name)}"
                    if it
                    else f"Asked for another place instead of {rules.clean_name(name)}",
                )
            )
        return rows

    async def _places(self, conn: AsyncConnection, subjects: set[str]) -> dict[str, tuple[str, str | None]]:
        """The places the subjects are about: id → (name, category)."""
        ids = {pid for s in subjects if (pid := rules.subject_place(s))}
        if not ids:
            return {}
        rows = await (
            await conn.execute(
                "SELECT id::text, name, category FROM places WHERE id = ANY(%s::uuid[])", (list(ids),)
            )
        ).fetchall()
        return {pid: (name, category) for pid, name, category in rows}

    async def _names(self, conn: AsyncConnection, subjects: set[str]) -> dict[str, str]:
        return {pid: name for pid, (name, _) in (await self._places(conn, subjects)).items()}

    # --- travelling -----------------------------------------------------------------------------------------

    async def update_travelling(self) -> dict[str, Any] | None:
        """A night more than 50 km from home starts `travelling` (since that night, in that city); two nights
        back home end it (one night home between two trips doesn't). Travel claims never
        change home claims (pisa.domain.learning)."""
        now = self._clock()
        async with self._pool.connection() as conn:
            life = await self._life(conn, now)
            last = self._local(now).date() - timedelta(days=1)
            night = life.nights.get(last)
            if life.home is None:
                await home_store.end_trip(conn)
                return None
            far = night is not None
            if not far or distance_m(life.home, night) <= rules.TRAVEL_KM * 1000:  # type: ignore[arg-type]
                current = await home_store.travelling(conn)
                before = life.nights.get(last - timedelta(days=1))
                home_twice = (
                    night is not None
                    and before is not None
                    and distance_m(life.home, before) <= rules.TRAVEL_KM * 1000
                )
                if current and not home_twice:
                    return current  # still travelling until a second night at home
                await home_store.end_trip(conn)
                return None
            since = last
            while (n := life.nights.get(since - timedelta(days=1))) is not None and (
                distance_m(life.home, n) > rules.TRAVEL_KM * 1000
            ):
                since -= timedelta(days=1)
            travelling = {"since": since.isoformat(), "city": await self._city(conn, night)}  # type: ignore[arg-type]
            await home_store.set_travelling(conn, travelling)
        _log.info("profile_travelling", since=travelling["since"])
        return travelling

    async def _city(self, conn: AsyncConnection, coord: Coord) -> str | None:
        """The city of the nearest known place (no paid call: places Pisa already has)."""
        row = await (
            await conn.execute(
                "SELECT address FROM places WHERE address IS NOT NULL AND lat BETWEEN %s AND %s "
                "AND lon BETWEEN %s AND %s ORDER BY (lat - %s) ^ 2 + (lon - %s) ^ 2 LIMIT 1",
                (coord[0] - 0.05, coord[0] + 0.05, coord[1] - 0.05, coord[1] + 0.05, coord[0], coord[1]),
            )
        ).fetchone()
        return (city_of(row[0]) or None) if row else None

    # --- consolidation --------------------------------------------------------------------------------------

    async def consolidate(self) -> int:
        """Evidence → observed claims (pisa.domain.learning): per subject and home / travel, the narrowest
        scope with enough support; confidence; used from 0.6 with support 3 (routines only once confirmed); a
        used claim the evidence turns against goes to the weekly check, never flipped silently; expired claims
        retire. Returns how many claims were written."""
        now = self._clock()
        written = 0
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            cur = conn.cursor(row_factory=dict_row)
            evidence = await (
                await cur.execute(
                    "SELECT signal, kind, subject_key, weight, direction, scope, observed_at, value "
                    "FROM profile_evidence WHERE subject_key IS NOT NULL AND signal = ANY(%s) "
                    "ORDER BY observed_at",
                    (list(OBSERVED),),
                )
            ).fetchall()
            groups: dict[tuple[str, str, str], list[rules.Ev]] = defaultdict(list)
            unscoped: set[tuple[str, str, str]] = set()  # evidence from when Pisa didn't know home
            for e in evidence:
                where = "travel" if e["scope"].get("where") == "travel" else "home"
                if e["scope"].get("where") not in ("home", "travel"):
                    unscoped.add((e["kind"], e["subject_key"], where))
                scope = {k: v for k, v in e["scope"].items() if k != "where"}
                groups[(e["kind"], e["subject_key"], where)].append(
                    rules.Ev(
                        e["signal"],
                        e["kind"],
                        e["subject_key"],
                        float(e["weight"]),
                        int(e["direction"]),
                        scope,
                        e["observed_at"],
                        e["value"],
                    )
                )
            places = await self._places(conn, {k[1] for k in groups})
            names = {pid: name for pid, (name, _) in places.items()}
            for (kind, subject, where), evs in groups.items():
                pid = rules.subject_place(subject)
                name, category = places.get(pid, (None, None)) if pid is not None else (None, None)
                if pid is not None and (
                    # Health, worship, politics, or no category: checked again here, since a place's
                    # name or category may have changed since its evidence was recorded.
                    not rules.taste_place(name, category)
                    # The user forgot their like of the place: no routine there either.
                    or (subject.startswith("routine:") and await self._profile.place_forgotten(conn, pid))
                ):
                    await self._drop(conn, subject)
                    continue
                written += await self._claim(
                    conn, kind, subject, where, evs, names, lang, now, category=category or "",
                    anywhere=(kind, subject, where) in unscoped,
                )  # fmt: skip
            retired = await conn.execute(
                "UPDATE profile_claims SET state = 'retired', retired_by = 'expired', updated_at = %s "
                "WHERE provenance = 'observed' "
                "AND state <> 'retired' AND expires_at IS NOT NULL AND expires_at < %s",
                (now, now),
            )
            if retired.rowcount:
                _log.info("profile_claims_expired", count=retired.rowcount)
        return written

    async def _drop(self, conn: AsyncConnection, subject: str) -> None:
        """What must not be learned: its evidence and its observed claims go."""
        await conn.execute("DELETE FROM profile_evidence WHERE subject_key = %s", (subject,))
        await conn.execute(
            "DELETE FROM profile_claims WHERE subject_key = %s AND provenance <> 'said'", (subject,)
        )

    @staticmethod
    def _newer(existing: dict[str, Any], evs: list[rules.Ev], now: datetime) -> bool:
        """Expired: back only with evidence newer than it had, in its own scope (its
        `last_evidence_at` is its scope's; newer visits in another scope aren't news about it). The user's
        "wrong" is for good."""
        scope = {k: v for k, v in (existing["scope"] or {}).items() if k != "where"}
        newest = rules.tally(evs, now, scope).last_at
        last = existing["last_evidence_at"]
        return newest is not None and (last is None or newest > last)

    async def _claim(
        self,
        conn: AsyncConnection,
        kind: str,
        subject: str,
        where: str,
        evs: list[rules.Ev],
        names: dict[str, str],
        lang: str,
        now: datetime,
        *,
        category: str = "",
        anywhere: bool = False,
    ) -> int:
        stype = rules.subject_type(subject)
        value: dict[str, Any] | None = None
        ordered = sorted(evs, key=lambda e: e.at)
        if stype == "lateness":
            lateness = rules.lateness_minutes([e.value for e in ordered if e.value])
            if lateness is None:
                return 0  # reported only with 5 samples
            minutes, late, samples = lateness
            value = {"minutes": minutes, "late": late, "samples": samples}
        elif stype == "mobility":
            walks = [float(e.value["minutes"]) for e in ordered if e.value][-rules.TRIP_LATEST :]
            if len(walks) < rules.LATE_SAMPLES:
                return 0
            value = {"minutes": min(WALK_MAX_MIN, round(rules.percentile(walks, 0.8)))}
        scope = rules.choose_scope(evs, now)
        t = rules.tally(evs, now, scope)
        statement = rules.statement(subject, value, names, lang)
        if await self._profile.blocked(
            conn, kind, subject, statement, category
        ):  # off limits by category too
            await self._drop(conn, subject)
            return 0
        cur = conn.cursor(row_factory=dict_row)
        existing = await (
            await cur.execute(
                # The user's edited claim is the subject's, whatever `where` they gave it (a
                # home like moved to "travel" mustn't come back as a new home claim); else this group's.
                "SELECT id, state, provenance, value, last_confirmed_at, retired_by, owner_edited, "
                "last_evidence_at, scope, coalesce(scope->>'where', 'home') AS claim_where "
                "FROM profile_claims "
                "WHERE subject_key = %s AND (owner_edited OR coalesce(scope->>'where', 'home') = %s) "
                "ORDER BY owner_edited DESC, updated_at DESC LIMIT 1",
                (subject, where),
            )
        ).fetchone()
        if existing is not None and existing["claim_where"] != where:
            return 0  # the user set this claim's place to the other group: this group learns nothing on it
        confirmed = existing is not None and existing["provenance"] == "confirmed"
        since = existing["last_confirmed_at"] if existing else None
        state, changed = rules.claim_state(
            t,
            previous=existing["state"] if existing else None,
            used_before=existing is not None and existing["state"] == "active",
            needs_confirmation=stype == "routine",
            confirmed=confirmed,
            # A change is judged on what came after the user's last "right".
            everything=rules.tally([e for e in evs if since is None or e.at > since], now),
            # what Pisa took back itself (`pisa`, 0108's noise) comes back on new evidence,
            # as an expired claim does (a visit after a tap is a like); only their own "wrong" is for good.
            retired_for_good=existing is not None
            and not (existing["retired_by"] in ("expired", "pisa") and self._newer(existing, evs, now)),
        )
        conf = profile_rules.confidence(t.support, t.against, confirmed)
        lifetime = profile_rules.LIFETIME.get(kind)
        expires = (t.last_at or now) + lifetime if lifetime else None
        # Scoped home only when all of it was observed knowing where home is: what Pisa saw before
        # holds anywhere.
        full_scope = scope | ({} if anywhere else {"where": where})
        digest = self._profile.subject_tombstone(kind, subject)  # stable: the sentence's numbers change
        if existing is None:
            claim_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO profile_claims (id, kind, statement, subject, polarity, scope, provenance, "
                "support, "
                "against, confidence, state, sensitive, hard, created_at, updated_at, last_evidence_at, "
                "expires_at, subject_key, value) VALUES (%s, %s, %s, %s, 1, %s, 'observed', %s, %s, %s, %s, "
                "false, false, %s, %s, %s, %s, %s, %s)",
                (
                    claim_id,
                    kind,
                    statement,
                    Jsonb({"hash": digest}),
                    Jsonb(full_scope),
                    t.support,
                    t.against,
                    conf,
                    state,
                    now,
                    now,
                    t.last_at,
                    expires,
                    subject,
                    Jsonb(value) if value else None,
                ),
            )
        else:
            claim_id = existing["id"]
            if existing["owner_edited"]:  # the user's sentence and scope win
                statement, full_scope = None, None
            await conn.execute(
                "UPDATE profile_claims SET statement = coalesce(%s, statement), scope = coalesce(%s, scope), "
                "support = %s, against = %s, confidence = %s, state = %s, updated_at = %s, "
                "last_evidence_at = %s, expires_at = %s, value = %s, "
                "retired_by = CASE WHEN %s = 'retired' THEN retired_by END, "
                "retired_reason = CASE WHEN %s = 'retired' THEN retired_reason END, "
                "retired_from = CASE WHEN %s = 'retired' THEN retired_from END WHERE id = %s",
                (
                    statement,
                    Jsonb(full_scope) if full_scope is not None else None,
                    t.support,
                    t.against,
                    conf,
                    state,
                    now,
                    t.last_at,
                    expires,
                    Jsonb(value) if value else None,
                    state,
                    state,
                    state,
                    claim_id,
                ),
            )
        if changed:
            _log.info("profile_claim_changed", claim_id=str(claim_id), kind=kind)
        await conn.execute(
            "UPDATE profile_evidence SET claim_id = %s WHERE subject_key = %s "
            "AND (CASE WHEN scope->>'where' = 'travel' THEN 'travel' ELSE 'home' END) = %s",
            (claim_id, subject, where),
        )
        return 1

    # --- buffers --------------------------------------------------------------------------------------------

    async def buffers(self) -> dict[str, int]:
        """Minutes to leave earlier, per kind of event: only the ones the user said yes to."""
        async with self._pool.connection() as conn:
            return await event_buffers(conn)

    # --- the weekly check -----------------------------------------------------------------------------------

    def _offer(self, now: datetime) -> datetime:
        """This week's offer time: the latest Sunday 18:00 (the user's time) at or before now, the start of
        the weekly check's period (`schedule.WEEKLY_CHECK`)."""
        local = self._local(now)
        return schedule.WEEKLY_CHECK.start(local).replace(tzinfo=local.tzinfo)

    def _open(self, now: datetime, offer: datetime) -> bool:
        """The weekly check is open from Sunday 18:00 to Monday 18:00."""
        return now - offer.astimezone(UTC) <= WEEKLY_OPEN

    async def weekly(self) -> dict[str, Any]:
        """GET /v1/profile/weekly: this week's items not yet answered, only while the check is open (Sunday
        18:00 to Monday 18:00; built then, never on other days' polls)."""
        now = self._clock()
        offer = self._offer(now)
        if not self._open(now, offer):
            return {"week": offer.date().isoformat(), "items": []}
        await self._build_week(offer)
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            items = await (
                await cur.execute(
                    "SELECT id::text, kind, statement, why, evidence FROM profile_weekly WHERE week = %s "
                    "AND answered_at IS NULL ORDER BY created_at",
                    (offer.date(),),
                )
            ).fetchall()
        return {"week": offer.date().isoformat(), "items": items}

    async def _build_week(self, offer: datetime) -> list[dict[str, Any]]:
        """The week's items, once: at most 3 (pisa.domain.learning.pick_weekly). Nothing while paused."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.profile.weekly'))")
            if await (
                await conn.execute("SELECT 1 FROM profile_weekly WHERE week = %s LIMIT 1", (offer.date(),))
            ).fetchone():
                return []
            if await get_setting(conn, f"profile_weekly_built:{offer.date()}") or (
                await self._profile.paused_until(conn) is not None
            ):
                return []
            lang = await self._owner.lang(conn)
            candidates = await self._candidates(conn, now, lang)
            chosen = rules.pick_weekly(candidates)
            for i, c in enumerate(chosen):  # one microsecond apart: the list reads in the pick's order
                await conn.execute(
                    "INSERT INTO profile_weekly (id, week, kind, claim_id, statement, why, evidence, value, "
                    "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4(),
                        offer.date(),
                        c.kind,
                        c.claim_id,
                        c.statement,
                        c.why,
                        Jsonb(c.evidence),
                        Jsonb(c.value) if c.value else None,
                        now + timedelta(microseconds=i),
                    ),
                )
            await set_setting(conn, f"profile_weekly_built:{offer.date()}", Jsonb(len(chosen)))
        _log.info("profile_weekly_built", week=offer.date().isoformat(), items=len(chosen))
        return [{"kind": c.kind} for c in chosen]

    async def _candidates(
        self, conn: AsyncConnection, now: datetime, lang: str
    ) -> list[rules.WeeklyCandidate]:
        it = lang == "it-IT"
        cur = conn.cursor(row_factory=dict_row)
        claims = await (
            await cur.execute(
                "SELECT c.id::text AS id, c.kind, c.statement, c.subject_key, c.state, c.provenance, "
                "c.confidence, "
                "c.support, c.against, c.value, c.confirmed_value, c.expires_at, c.created_at, "
                # "seen N times in the last 4 weeks": those 4 weeks only.
                "(SELECT count(*) FROM profile_evidence e WHERE e.claim_id = c.id AND e.observed_at > %s) "
                "AS seen, "
                "EXISTS (SELECT 1 FROM profile_weekly w WHERE w.claim_id = c.id AND w.created_at > %s) AS "
                "asked "
                "FROM profile_claims c WHERE c.state <> 'retired' AND NOT c.sensitive",
                (now - timedelta(days=28), now - ASKED_AGAIN_AFTER),
            )
        ).fetchall()
        declined = set(await get_setting(conn, BUFFERS_DECLINED_KEY) or [])
        out: list[rules.WeeklyCandidate] = []
        for c in claims:
            if c["asked"] or c["provenance"] == "said" or not c["subject_key"]:
                continue
            evidence = [
                {"summary": s, "at": self._local(at).isoformat()}
                for s, at in await (
                    await conn.execute(
                        "SELECT summary, observed_at FROM profile_evidence WHERE claim_id = %s "
                        "ORDER BY observed_at DESC LIMIT 5",
                        (uuid.UUID(c["id"]),),
                    )
                ).fetchall()
            ]
            why = rules.evidence_line(int(c["seen"]), 4, lang)
            conf = float(c["confidence"])
            stype = rules.subject_type(c["subject_key"])
            if c["state"] == "asking":
                why = ("Ultimamente sembra cambiato: " if it else "Lately this seems to have changed: ") + why
                out.append(
                    rules.WeeklyCandidate("changed", c["id"], c["statement"], why, evidence, c["value"], conf)
                )
            elif stype == "lateness" and c["state"] == "active" and not c["confirmed_value"]:
                value = c["value"] or {}
                minutes = int(value.get("minutes", 0))
                event_kind = c["subject_key"].split(":", 1)[1]
                if minutes < rules.BUFFER_MIN or event_kind in declined:
                    continue
                it_name, en_name = rules.EVENT_KIND_NAMES.get(event_kind, rules.EVENT_KIND_NAMES["other"])
                question = (
                    f"Per {it_name} arrivi di solito circa {minutes} minuti dopo l'inizio "
                    f"({value.get('late')} delle ultime {value.get('samples')}). Vuoi che ti dica di uscire "
                    f"{minutes} minuti prima per questi?"
                    if it
                    else f"For {en_name} you usually arrive about {minutes} minutes after the start "
                    f"({value.get('late')} of the last {value.get('samples')}). Want me to tell you to leave "
                    f"{minutes} minutes earlier for those?"
                )
                out.append(
                    rules.WeeklyCandidate(
                        "buffer", c["id"], question, why, evidence, {"buffer_min": minutes}, float(minutes)
                    )
                )
            elif (
                c["state"] == "candidate" and stype == "routine"  # 3 times in 4 weeks: confirmed before use
            ):
                out.append(
                    rules.WeeklyCandidate("new", c["id"], c["statement"], why, evidence, c["value"], conf)
                )
            elif c["state"] == "candidate" and rules.SHOW_LOW <= conf < rules.USE_CONFIDENCE:
                why = ("Penso che… " if it else "I think… ") + why
                out.append(
                    rules.WeeklyCandidate("confirm", c["id"], c["statement"], why, evidence, c["value"], conf)
                )
            elif (
                c["state"] == "active"
                and c["provenance"] == "observed"
                and c["created_at"] > now - timedelta(days=7)
            ):
                out.append(
                    rules.WeeklyCandidate("new", c["id"], c["statement"], why, evidence, c["value"], conf)
                )
            elif c["state"] == "active" and c["expires_at"] and c["expires_at"] < now + rules.EXPIRING_SOON:
                out.append(
                    rules.WeeklyCandidate(
                        "expiring", c["id"], c["statement"], why, evidence, c["value"], conf
                    )
                )
        return out

    async def answer(self, item_id: str, answer: str) -> dict[str, Any]:
        """POST /v1/profile/weekly/{item_id}: right (confirmed, at least 0.8), wrong (retired: strong evidence
        against), forget (claim and evidence go, a tombstone stays); a buffer: yes (leave that many minutes
        earlier for those events) or no (never asked again)."""
        now = self._clock()
        try:
            parsed = uuid.UUID(item_id)
        except ValueError:
            parsed = None
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            item = (
                await (
                    await cur.execute(
                        "SELECT w.kind, w.claim_id, w.answered_at, w.value, w.week, c.subject_key FROM "
                        "profile_weekly w "
                        "LEFT JOIN profile_claims c ON c.id = w.claim_id WHERE w.id = %s FOR UPDATE OF w",
                        (parsed,),
                    )
                ).fetchone()
                if parsed
                else None
            )
            if item is None or item["claim_id"] is None:
                raise PisaError("not_found", f"There is no weekly item with id {item_id[:40]!r}.", "Refresh")
            offer = self._offer(now)
            if item["week"] != offer.date() or not self._open(now, offer):  # only this week's, while open
                raise PisaError(
                    "validation_failed",
                    "This weekly check is over: only the current week's items can be answered.",
                    "Refresh the weekly check",
                )
            allowed = ANSWERS.get(item["kind"], CLAIM_ANSWERS)
            if answer not in allowed:
                raise PisaError(
                    "validation_failed",
                    f"A {item['kind']} item takes {' or '.join(allowed)}.",
                    "Answer with one of them",
                )
            if item["answered_at"] is not None:
                raise PisaError(
                    "validation_failed", "This item was already answered.", "Refresh the weekly check"
                )
            await conn.execute(
                "UPDATE profile_weekly SET answer = %s, answered_at = %s WHERE id = %s", (answer, now, parsed)
            )
            claim_id = item["claim_id"]
            if answer == "yes":
                await conn.execute(
                    "UPDATE profile_claims SET confirmed_value = %s, provenance = 'confirmed', "
                    "last_confirmed_at = %s, confidence = greatest(confidence, %s), updated_at = %s WHERE "
                    "id = %s",
                    (Jsonb(item["value"]), now, profile_rules.CONFIRMED_FLOOR, now, claim_id),
                )
            elif answer == "no":
                declined = await get_setting(conn, BUFFERS_DECLINED_KEY) or []
                kind = str(item["subject_key"]).split(":", 1)[1]
                await set_setting(conn, BUFFERS_DECLINED_KEY, Jsonb(sorted({*declined, kind})))
            elif answer == "right":
                await conn.execute(
                    "UPDATE profile_claims SET provenance = 'confirmed', state = 'active', "
                    "last_confirmed_at = %s, "
                    "confidence = greatest(confidence, %s), updated_at = %s WHERE id = %s",
                    (now, profile_rules.CONFIRMED_FLOOR, now, claim_id),
                )
            elif answer == "wrong":
                await conn.execute(
                    "UPDATE profile_claims SET state = 'retired', retired_by = 'owner', "
                    "against = against + 3, updated_at = %s "
                    "WHERE id = %s",
                    (now, claim_id),
                )
                await conn.execute(
                    "INSERT INTO profile_evidence (id, claim_id, signal, ref, weight, direction, scope, "
                    "observed_at, summary) VALUES (%s, %s, 'weekly_answer', %s, 3, -1, '{}', %s, %s)",
                    (uuid.uuid4(), claim_id, str(parsed), now, "Not true (weekly check)"),
                )
        if answer == "forget":
            await self._profile.forget(str(claim_id))
        _log.info("profile_weekly_answered", kind=item["kind"], answer=answer)
        return {"answer": answer}

    async def weekly_tick(self) -> bool:
        """Once a week from Sunday 18:00 (local; the worker's step, period `schedule.WEEKLY_CHECK`, open until
        Monday 18:00): the week's items are built and, when there are any, offered as a notify (kind info,
        weekly). Quiet mode holds it like any other message."""
        now = self._clock()
        offer = self._offer(now)
        await self._build_week(offer)
        async with self._pool.connection() as conn, conn.transaction():
            count = (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM profile_weekly WHERE week = %s AND answered_at IS NULL",
                        (offer.date(),),
                    )
                ).fetchone()
            )[0]  # type: ignore[index]
            if not count:
                return False
            lang = await self._owner.lang(conn)
            await enqueue(
                conn,
                kind="info",
                text=rules.WEEKLY_TEXT[lang if lang in rules.WEEKLY_TEXT else "en-US"].format(n=count),
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                weekly=True,
                path="learning.weekly",
            )
        _log.info("profile_weekly_offered", items=count)
        return True
