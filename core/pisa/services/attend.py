"""Proactivity as decisions: the candidates the senses propose, decided and delivered.

- **Propose** (`propose`, in the sense's own transaction): a candidate row, once per `key`. A sense without
  this service (tests, a tool run alone) delivers by today's rules at once (`deliver_now`): one path either
  way.
- **Tick** (every minute, after the senses, lock ATTEND):
  - Pisa's promises whose condition holds (a time, arriving at or leaving a place, the next talk), the weather
    against the plan's walks and rides, and the plan's newly unplanned items become candidates;
  - the pending batch is decided once a floor is waiting or the oldest has waited `BATCH_WINDOW` (what came
    within it is decided together);
  - `attend.decide` within `DECIDE_TIMEOUT_S`; a failure, the timeout or the AI caps → today's rules;
  - code's validator (`pisa.domain.attend.validate`), then delivery: a notification (`now`, `breakpoint`, `at`
    as its `not_before`, the phone channels `silent`), the next conversation (`fold_next`), or nothing
    (`drop`);
  - the decision in `pisa_decisions` (move, purpose, provenance by code, the rails that changed it);
  - **feedback**: what became of each delivered item (answered, "dopo", dismissed, Undo, ignored) goes into
    `feedback_events`; the user's last outcomes for the same source are the next decision's examples.
- Logs carry counts and ids only, never the words.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain import attend as brain
from pisa.brain.conversation import profile_instruction
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.domain import attend as rules
from pisa.domain import signals, talk
from pisa.domain import voice1 as voice_rules
from pisa.domain.body import Presence
from pisa.domain.decisions import Decision as Move
from pisa.domain.decisions import provenance
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.domain.tasks import local_day_bounds
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services import foreign, guardian
from pisa.services.notifications import PENDING_MAX_AGE, Kind, enqueue, in_meeting, spoken_since
from pisa.services.plan_store import made_from_now, open_tasks

WEATHER_AHEAD = timedelta(hours=3)  # walks and rides leaving within this are checked against the rain
EXAMPLES = 5  # the user's last outcomes per source in the prompt
SAID_SINCE = timedelta(hours=2)
FLOOR_STALE = timedelta(minutes=5)  # reminders' own catch-up: an older floor is no longer news
PENDING_KEPT = timedelta(days=1)
_log = get_logger("pisa.attend")
# the ask rails: their "always" never overrides these
ASK_RAILS = frozenset({"ask_quiet", "ask_cap", "knock_past_morning"})


@dataclass
class AttendReport:
    ran: bool = True
    proposed: int = 0
    decided: int = 0
    by_rules: int = 0
    delivered: int = 0
    outcomes: int = 0
    rails: dict[str, int] = field(default_factory=dict)


def _row(c: rules.Candidate, key: str, delivery: dict[str, Any]) -> tuple[Any, ...]:
    return (
        uuid.UUID(c.id), key, c.source, c.kind, c.text, c.lang, c.urgency, c.priority, c.deadline, c.floor,
        c.outside, c.alone, c.must_show, Jsonb(delivery), c.created_at,
    )  # fmt: skip


async def propose(
    conn: AsyncConnection,
    candidate: rules.Candidate,
    key: str,
    delivery: dict[str, Any] | None = None,
    plan_change: float | None = None,
) -> bool:
    """Store a candidate (once per key). True when it is new. `plan_change` (shadow): a replan's
    measure of what moved; NULL for every other source."""
    row = await (
        await conn.execute(
            "INSERT INTO attend_candidates (id, key, source, kind, text, lang, urgency, priority, deadline, "
            "floor, outside, alone, must_show, delivery, created_at, plan_change) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING 1",
            (*_row(candidate, key, delivery or {}), plan_change),
        )
    ).fetchone()
    return row is not None


async def deliver_now(
    conn: AsyncConnection, candidate: rules.Candidate, delivery: dict[str, Any], *, timezone: str
) -> uuid.UUID | None:
    """Today's rules, at once (a sense with no attend service): the notification it always sent."""
    decision = rules.rules(candidate)
    if decision.choice == "drop":
        return None
    return await _enqueue(conn, [candidate], decision, delivery, candidate.created_at, timezone)


async def _enqueue(
    conn: AsyncConnection,
    members: Sequence[rules.Candidate],
    decision: rules.Decision,
    delivery: dict[str, Any],
    now: datetime,
    timezone: str,
) -> uuid.UUID:
    first = members[0]
    voice_floor = any(talk.is_floor(c.kind, c.urgency) for c in members)
    same = all(c.kind == first.kind for c in members)
    kind = cast(Kind, "reminder" if voice_floor else first.kind if same else "info")
    # RAIL: a now-floor's notice is `now` whatever the decision (never held for a
    # pause).
    now_floor = any(c.floor and c.urgency == "now" for c in members)
    urgency: talk.Urgency = "now" if decision.choice == "now" or now_floor else "breakpoint"
    summary = delivery.get("summary") if len(members) == 1 else None
    event_at = delivery.get("event_at")
    notification_id, _ = await enqueue(
        conn, kind=kind, text=decision.text, lang=first.lang, now=now, timezone=timezone, summary=summary,
        urgency=urgency, event_at=datetime.fromisoformat(event_at) if event_at else None,
        check_in_id=uuid.UUID(delivery["check_in_id"]) if delivery.get("check_in_id") else None,
        choices=delivery.get("choices"), done=delivery.get("done"), priority=first.priority
        if first.priority in ("low", "normal", "urgent") else "normal",
        not_before=decision.at if decision.choice == "at" else None,
        silent=not rules.spoken(decision.channel),
        speak=next((c.speak for c in members if c.speak), None),
        path=f"attend.{first.source}" + (":outside" if any(c.outside for c in members) else "")
        + (":shown" if any(c.must_show for c in members) else ""),
        person_id=uuid.UUID(delivery["person_id"]) if delivery.get("person_id") else None,
    )  # fmt: skip
    if delivery.get("ask_id"):  # Pisa's question in it (the knock's, the tragitto's)
        await conn.execute(
            "UPDATE notifications SET ask_id = %s WHERE id = %s",
            (uuid.UUID(delivery["ask_id"]), notification_id),
        )
    if delivery.get("check_in_id"):  # the check-in and its notice, together
        await conn.execute(
            "UPDATE check_ins SET notification_id = %s WHERE id = %s",
            (notification_id, uuid.UUID(delivery["check_in_id"])),
        )
    return notification_id


async def asks_today(conn: AsyncConnection, now: datetime, timezone: str, *, pending: bool = True) -> int:
    """Pisa's unprompted asks today, their day: the notices carrying one of Pisa's
    one-yes questions (the morning thought's, a ritual's, the knock's, the tragitto's), and a knock or a
    tragitto offer still being decided (`pending`: the senses' count; the decision's own counts only what
    went out)."""
    start, end = local_day_bounds(now.astimezone(ZoneInfo(timezone)).date(), timezone)
    row = await (
        await conn.execute(
            "SELECT (SELECT count(*) FROM notifications WHERE ask_id IS NOT NULL AND created_at >= %s "
            "AND created_at < %s) + (SELECT count(*) FROM attend_candidates WHERE source IN ('knock', "
            "'tragitto') AND decided_at IS NULL AND created_at >= %s AND %s)",
            (start, end, start, pending),
        )
    ).fetchone()
    return int(row[0]) if row else 0


class AttendService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        gateway: ModelGateway | None,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        profile: Any = None,  # ProfileService: the core profile instruction (optional)
        weather: Any = None,  # WeatherService: rainy_hours (optional)
        decide: Callable[..., Any] | None = None,  # tests and the eval: (candidates, moment) → decisions
        senses: Sequence[Callable[[datetime], Any]] = (),  # more senses (the knock, il tragitto, the loops)
    ) -> None:
        self._pool = pool
        self._gateway = gateway
        self._owner = owner
        self._clock = clock
        self._profile = profile
        self._weather = weather
        self._decide = decide
        self.senses: list[Callable[[datetime], Any]] = list(senses)

    # --- the senses' entry --------------------------------------------------------------------------------

    async def propose(
        self,
        conn: AsyncConnection,
        candidate: rules.Candidate,
        key: str,
        delivery: dict[str, Any] | None = None,
    ) -> bool:
        return await propose(conn, candidate, key, delivery)

    # --- the minute's work ----------------------------------------------------------------------------------

    async def tick(self) -> AttendReport:
        return await self._locked(floors_only=False)

    async def floors(self) -> AttendReport:
        """Right after reminders: the floors they proposed go out this minute, by today's
        rules, before the plan's slower steps."""
        return await self._locked(floors_only=True)

    async def _locked(self, *, floors_only: bool) -> AttendReport:
        # ATTEND spans two worker steps (`attend`, `attend_floors`) in two groups that run side by side; each
        # step's own job lock never excludes the other.
        async with locks.held(self._pool, locks.ATTEND) as got:
            if not got:
                return AttendReport(ran=False)
            return await self._tick(self._clock(), floors_only=floors_only)

    async def _tick(self, now: datetime, *, floors_only: bool = False) -> AttendReport:
        report = AttendReport()
        if not floors_only:
            for sense in (self._promises, self._replans, self._rain, *self.senses):
                try:
                    report.proposed += await sense(now)
                except Exception as exc:  # one sense failing never stops the decisions
                    _log.warning("attend_sense_failed", sense=sense.__name__, cause=exc.__class__.__name__)
            report.outcomes = await self._outcomes(now)
        async with self._pool.connection() as conn:
            pending = await _pending(conn, now)
        # RAIL: a floor goes by today's rules at once, in the template's words: the rails
        # force its choice anyway, so waiting up to 2 s for the model only delays a leave-now.
        batch = [(c, d) for c, d in pending if c.floor]
        decided: list[rules.Decision] = [rules.rules(c) for c, _ in batch]
        others = [(c, d) for c, d in pending if not c.floor]
        due = rules.batch_due([c.created_at for c, _ in others], [], now, [c.alone for c, _ in others])
        if not floors_only and others and due:
            others = others[: rules.BATCH_MAX]
            batch += others
            folded = [c for c, d in others if d.get("next_talk")]
            rest = [c for c, _ in others if c not in folded]
            decided += [
                rules.Decision((c.id,), "fold_next", "voice", c.text, by="rules", rails=("next_talk",))
                for c in folded
            ]
            # an email's items are decided in a call of their own, so its words can't
            # steer what Pisa does with a check-in, a done or a promise; both calls at once.
            groups = [g for g in ([c for c in rest if not c.outside], [c for c in rest if c.outside]) if g]
            for made in await asyncio.gather(*(self._decisions(g, now) for g in groups)):
                decided += made
        if not batch:
            return report
        candidates = [c for c, _ in batch]
        deliveries = {c.id: d for c, d in batch}
        by_id = {c.id: c for c in candidates}
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            ctx = rules.Context(
                now=now,
                quiet=is_quiet(now, settings, self._owner.tz),
                meeting=await in_meeting(conn, now),
                voice_ok=talk.voice_allowed(False, *await spoken_since(conn, now, self._owner.tz)),
                quiet_at=lambda moment: is_quiet(moment, settings, self._owner.tz),
                asks_left=voice_rules.asks_left(await asks_today(conn, now, self._owner.tz, pending=False)),
                morning=now.astimezone(self._owner.zone).time() < voice_rules.MORNING_END,
            )
            his = await guardian.his_rules(conn, settings)
            for decision in decided:
                members = [by_id[i] for i in decision.ids]
                final = rules.validate(decision, members, ctx)
                # never over the ask rails (a question Pisa would
                # knock with in quiet hours, past the morning, or over the day's asks)
                if (
                    final.choice in ("at", "drop", "fold_next", "show_silently")
                    and not ASK_RAILS & set(final.rails)
                    and any(
                        guardian.always(his, f"attend.{c.source}", deliveries[c.id].get("person_id"))
                        for c in members
                    )
                ):  # their "always" wins over attend's later, fold and drop
                    final = dataclasses.replace(
                        final, choice="now", at=None, channel="voice_and_phone",
                        rails=(*final.rails, "his_rule_always"),
                    )  # fmt: skip
                try:  # one failing decision never rolls back the batch
                    async with conn.transaction():
                        report.delivered += await self._carry_out(
                            conn, final, members, _delivery(members, deliveries), now
                        )
                except Exception as exc:
                    _log.warning("attend_delivery_failed", cause=exc.__class__.__name__, ids=len(members))
                    continue
                if any(c.source in rules.ASKS for c in members):  # the cap counts each ask as it goes out
                    left = voice_rules.asks_left(await asks_today(conn, now, self._owner.tz, pending=False))
                    ctx = dataclasses.replace(ctx, asks_left=left)
                report.decided += 1
                report.by_rules += final.by == "rules"
                for rail in final.rails:
                    report.rails[rail] = report.rails.get(rail, 0) + 1
        _log.info("attend_decided", decided=report.decided, by_rules=report.by_rules,
                  delivered=report.delivered, rails=report.rails)  # fmt: skip
        return report

    async def _decisions(self, candidates: list[rules.Candidate], now: datetime) -> list[rules.Decision]:
        """The model's, covering every candidate; on a failure, the timeout or the caps: today's rules."""
        budget = (
            rules.DECIDE_TIMEOUT_S if any(c.floor for c in candidates) else rules.DECIDE_TIMEOUT_NO_FLOOR_S
        )
        try:
            moment = await self._moment(candidates, now)
            if self._decide is not None:
                made = await asyncio.wait_for(self._decide(candidates, moment), budget)
            elif self._gateway is not None:
                made = await asyncio.wait_for(self._model(candidates, moment), budget)
            else:
                made = []
        except TimeoutError:
            _log.warning("attend_timeout", candidates=len(candidates))
            made = []
        except Exception as exc:  # RAIL: no model, a provider error, the caps: today's rules decide
            _log.warning("attend_model_failed", cause=getattr(exc, "code", exc.__class__.__name__))
            made = []
        return rules.cover(made, candidates)

    async def _model(self, candidates: list[rules.Candidate], moment: brain.Moment) -> list[rules.Decision]:
        assert self._gateway is not None

        async def call(ctx: CallContext) -> list[rules.Decision]:
            out, usage = await brain.decide(ctx.model, candidates, moment, self._owner.tag)
            ctx.usage.update(usage)
            return out

        return await self._gateway.run(
            brain.SLOT,
            call,
            prompt_chars=brain.prompt_chars(candidates, self._owner.tag),
            max_output_tokens=brain.MAX_OUTPUT_TOKENS,
            bill_reserve_on_cancel=True,  # a call cut at the budget is paid for: its reservation stands
        )

    async def _moment(self, candidates: Sequence[rules.Candidate], now: datetime) -> brain.Moment:
        """What the decision sees: Pisa's body, the plan item now, what Pisa said unasked lately, the user's
        outcomes."""
        sources = sorted({c.source for c in candidates})
        async with self._pool.connection() as conn:
            presence = await (
                await conn.execute(
                    "SELECT app, glasses, earbuds, companion FROM device_presence "
                    "ORDER BY updated_at DESC LIMIT 1"
                )
            ).fetchone()
            said = await (
                await conn.execute(
                    "SELECT kind, created_at FROM notifications WHERE created_at > %s AND kind <> 'briefing' "
                    "ORDER BY created_at DESC LIMIT 6",
                    (now - SAID_SINCE,),
                )
            ).fetchall()
            examples = await (
                await conn.execute(
                    "SELECT subject, from_value, to_value FROM (SELECT *, row_number() OVER "
                    "(PARTITION BY subject ORDER BY created_at DESC) AS n FROM feedback_events "
                    "WHERE kind = 'attend' AND subject = ANY(%s)) x WHERE n <= %s ORDER BY created_at DESC",
                    ([f"attend:{s}" for s in sources], EXAMPLES),
                )
            ).fetchall()
            doing, doing_outside = await _doing(conn, now)
        tz = self._owner.zone
        body = (
            "app {}, glasses {}, earbuds {}, companion {}".format(*(v or "unknown" for v in presence))
            if presence
            else ""
        )
        profile = ""
        if self._profile is not None:
            try:
                profile = profile_instruction(await self._profile.core_profile())
            except Exception as exc:
                _log.warning("attend_profile_failed", cause=exc.__class__.__name__)
        return brain.Moment(
            now=now,
            timezone=self._owner.tz,
            lang=candidates[0].lang,
            body=body,
            doing=doing,
            doing_outside=doing_outside,
            said=[f"{kind} at {at.astimezone(tz):%H:%M}" for kind, at in said],
            profile=profile,
            examples=[f"{s.removeprefix('attend:')}: {f} → {t}" for s, f, t in examples],
        )

    async def _carry_out(
        self,
        conn: AsyncConnection,
        decision: rules.Decision,
        members: Sequence[rules.Candidate],
        delivery: dict[str, Any],
        now: datetime,
    ) -> int:
        notification_id: uuid.UUID | None = None
        outcome: str | None = None
        if decision.choice == "drop":
            outcome = "dropped"
            if delivery.get("check_in_id"):  # a check-in Pisa chose not to ask: never left open
                await conn.execute("UPDATE check_ins SET dropped_at = %s WHERE id = %s",
                                   (now, uuid.UUID(delivery["check_in_id"])))  # fmt: skip
        elif decision.choice != "fold_next":
            notification_id = await _enqueue(conn, members, decision, delivery, now, self._owner.tz)
        record = {
            "choice": decision.choice, "channel": decision.channel, "by": decision.by,
            "rails": list(decision.rails),
            "at": decision.at.isoformat() if decision.at else None, "together": list(decision.ids),
            "text": decision.text,
        }  # fmt: skip
        await conn.execute(
            "UPDATE attend_candidates SET decided_at = %s, decision = %s, notification_id = %s, "
            "outcome = %s, outcome_at = CASE WHEN %s::text IS NULL THEN NULL ELSE %s::timestamptz END, "
            "seen = %s WHERE id = ANY(%s)",
            (now, Jsonb(record), notification_id, outcome, outcome, now,
             await _seen(conn, now) if notification_id is not None else None,
             [uuid.UUID(c.id) for c in members]),
        )  # fmt: skip
        # RAIL: provenance by code: an email's words make it outside text; the rest is Pisa's initiative.
        move = Move(
            rules.move(decision.choice),
            "attend.decide" if decision.by == "model" else "attend.rules",
            provenance(said=False, outside=any(c.outside for c in members), unprompted=True),
            ref=members[0].id,
            purpose=decision.purpose,
            rail=",".join(decision.rails) or None,
            outcome=outcome,
        )
        await decision_log.store(conn, [move], at=now, trace_id=None, device_id=None)
        return notification_id is not None

    # --- what Pisa brings up in the next conversation (fold_next) -------------------------------------------

    async def folded(self, conn: AsyncConnection, now: datetime) -> list[tuple[str, bool, list[str]]]:
        """The decisions folded into the next talk: (words, from outside text, the candidates' ids), a merge
        once, not once per member; outside when any member is; older than FOLD_KEPT they expire. Nothing is
        marked here: `said` marks them once the turn has spoken."""
        await conn.execute(
            "UPDATE attend_candidates SET outcome = 'expired', outcome_at = %s WHERE decision->>'choice' = "
            "'fold_next' AND outcome IS NULL AND decided_at < %s",
            (now, now - rules.FOLD_KEPT),
        )
        rows = await (
            await conn.execute(
                "SELECT id, decision->>'text', decision->'together', outside FROM attend_candidates "
                "WHERE decision->>'choice' = 'fold_next' AND outcome IS NULL ORDER BY decided_at, id"
            )
        ).fetchall()
        decisions: dict[str, tuple[str, bool, list[str]]] = {}
        for row_id, text, together, outside in rows:
            key = ",".join(together or [str(row_id)])
            words, out, members = decisions.get(key, (text, False, []))
            decisions[key] = (words, out or bool(outside), [*members, str(row_id)])
        return list(decisions.values())[: rules.FOLD_MAX]

    async def said(self, conn: AsyncConnection, ids: Sequence[str], now: datetime) -> int:
        """The folded items a turn brought up, once it has spoken: said, and a label each."""
        rows = await (
            await conn.execute(
                "UPDATE attend_candidates SET outcome = 'said_in_talk', outcome_at = %s "
                "WHERE id = ANY(%s) AND outcome IS NULL RETURNING source",
                (now, [uuid.UUID(i) for i in ids]),
            )
        ).fetchall()
        for (source,) in rows:
            await _feedback(conn, source, "fold_next", "said_in_talk", now)
        return len(rows)

    # --- feedback -----------------------------------------------------------------------------------------

    async def _outcomes(self, now: datetime) -> int:
        """What became of what Pisa said unasked: answered, "dopo", dismissed, undone or ignored."""
        found = 0
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT c.id, c.source, c.decision->>'choice' AS choice, c.delivery, n.delivered_at, "
                "n.delivered_via, n.device_id, n.put_off_at, n.queued_at, n.not_before, n.ask_id, "
                "n.guardian, ci.answered_at, ci.dropped_at "
                "FROM attend_candidates c "
                "JOIN notifications n ON n.id = c.notification_id "
                "LEFT JOIN check_ins ci ON ci.id = n.check_in_id "
                "WHERE c.outcome IS NULL AND c.notification_id IS NOT NULL AND c.decided_at > %s",
                (now - timedelta(days=2),),
            )
            for row in await cur.fetchall():
                what = await _outcome_of(conn, row, now)
                if what is None:
                    continue
                await conn.execute(
                    "UPDATE attend_candidates SET outcome = %s, outcome_at = %s WHERE id = %s",
                    (what, now, row["id"]),
                )
                await conn.execute(
                    "UPDATE pisa_decisions SET outcome = %s, outcome_at = %s "
                    "WHERE ref = %s AND tool LIKE 'attend.%%'",
                    (what, now, str(row["id"])),
                )
                await _feedback(conn, row["source"], row["choice"], what, now)
                found += 1
        return found

    # --- senses that live here ------------------------------------------------------------------------------

    async def _promises(self, now: datetime) -> int:
        """Pisa's own follow-ups (`remember_to`) whose condition holds now: a place's, or the next talk. A
        timed one is the reminders step's (`RemindersService`): at its exact minute, a floor, never this
        sense's decision."""
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    "SELECT p.id, p.what, p.condition, p.lang, p.provenance FROM pisa_promises p "
                    "WHERE p.fired_at IS NULL AND p.cancelled_at IS NULL AND ("
                    "p.condition = 'next_talk' "
                    "OR (p.condition = 'arrive' AND EXISTS (SELECT 1 FROM stays s "
                    "WHERE s.place_id = p.place_id AND s.arrived_at > p.created_at AND s.arrived_at <= %s)) "
                    "OR (p.condition = 'leave' AND EXISTS (SELECT 1 FROM stays s "
                    "WHERE s.place_id = p.place_id AND s.departed_at > p.created_at "
                    "AND s.departed_at <= %s))) FOR UPDATE",
                    (now, now),
                )
            ).fetchall()
            for promise_id, what, condition, lang, origin in rows:
                candidate = rules.Candidate(
                    str(uuid.uuid4()), "promise", "info", rules.promise_text(what, lang), lang, now,
                    outside=origin == "outside_text",
                )  # fmt: skip
                made += await propose(
                    conn, candidate, f"promise:{promise_id}", {"next_talk": condition == "next_talk"}
                )
                await conn.execute("UPDATE pisa_promises SET fired_at = %s WHERE id = %s", (now, promise_id))
        return made

    async def _replans(self, now: datetime) -> int:
        """Items the latest plan of today no longer fits that the previous one did (a meeting moved, a
        re-plan)."""
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            plans = await (
                await conn.execute(
                    "SELECT day, plan FROM plans WHERE generated_at > %s AND day = (SELECT day FROM plans "
                    "ORDER BY generated_at DESC LIMIT 1) ORDER BY generated_at DESC LIMIT 2",
                    (now - timedelta(hours=18),),
                )
            ).fetchall()
            if len(plans) < 2:
                return 0
            (day, latest), (_, previous) = plans
            change = signals.plan_change(previous, latest)  # stored, read by nothing (shadow)
            before = {i.get("task_id"): i for i in previous.get("items") or [] if i.get("task_id")}
            made = 0
            for gone in latest.get("unplanned") or []:
                item = before.get(gone.get("task_id"))
                if item is None or item.get("status") == "skipped":
                    continue
                start = datetime.fromisoformat(item["start"]) if item.get("start") else None
                if start is not None and start <= now:
                    continue
                # a task from an email has the email's words in its title: outside text
                source = await (
                    await conn.execute(
                        "SELECT source_kind FROM tasks WHERE id::text = %s", (gone["task_id"],)
                    )
                ).fetchone()
                candidate = rules.Candidate(
                    str(uuid.uuid4()), "replan", "replan", rules.replan_text(gone["title"], lang), lang, now,
                    deadline=start, outside=bool(source and source[0] == "email"),
                )  # fmt: skip
                made += await propose(conn, candidate, f"replan:{day}:{gone['task_id']}", plan_change=change)
            return made

    async def _rain(self, now: datetime) -> int:
        """The plan's walks and rides leaving within WEATHER_AHEAD, against the forecast's rainy hours."""
        if self._weather is None:
            return 0
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            row = await (
                await conn.execute(
                    "SELECT day, plan, from_lat, from_lon, open_tasks FROM plans ORDER BY generated_at DESC "
                    "LIMIT 1"
                )
            ).fetchone()
            current = row is not None and made_from_now(row[4], await open_tasks(conn))
        if row is None or row[2] is None or not current:  # a plan of tasks gone since: none, until made again
            return 0
        day, plan, lat, lon, _ = row
        trips = [
            i for i in plan.get("items") or []
            if i.get("kind") == "travel" and i.get("mode") in ("walk", "bike") and i.get("leave_at")
            and now < datetime.fromisoformat(i["leave_at"]) <= now + WEATHER_AHEAD
        ]  # fmt: skip
        if not trips:
            return 0
        rainy = await self._weather.rainy_hours((lat, lon), day)
        made = 0
        async with self._pool.connection() as conn:
            for trip in trips:
                leave = datetime.fromisoformat(trip["leave_at"])
                hit = next((h for h in rainy if h <= leave < h + timedelta(hours=1)), None)
                if hit is None:
                    continue
                candidate = rules.Candidate(
                    str(uuid.uuid4()), "weather", "info",
                    rules.weather_text(hit.astimezone(self._owner.zone), trip["mode"], lang), lang, now,
                    deadline=leave,
                )  # fmt: skip
                made += await propose(conn, candidate, f"weather:{day}:{trip.get('to_place_id')}:{leave:%H}")
        return made


async def _seen(conn: AsyncConnection, now: datetime) -> bool | None:
    """(shadow): could they see or hear a delivery now (the latest presence)? Logged, never read."""
    row = await (
        await conn.execute(
            "SELECT app, glasses, earbuds, companion, state, updated_at FROM device_presence "
            "ORDER BY updated_at DESC LIMIT 1"
        )
    ).fetchone()
    presence = Presence(row[0], row[1], row[2], row[3], row[4] or {}) if row else None
    return signals.seen(presence, now, row[5] if row else None)


def _delivery(members: Sequence[rules.Candidate], deliveries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A merge's notice carries its floor's delivery (the earliest event's), never a lesser member's."""
    ranked = sorted(
        members,
        key=lambda c: (not c.floor, c.urgency != "now", deliveries[c.id].get("event_at") or "9999"),
    )
    return deliveries[ranked[0].id]


async def _pending(conn: AsyncConnection, now: datetime) -> list[tuple[rules.Candidate, dict[str, Any]]]:
    # a candidate whose notice was written is decided, whatever its row says: never expired, never decided
    # again (quality audit §23 Gate 7). What waited too long (a worker down) goes nowhere: a floor past its
    # deadline or older than reminders' own catch-up, anything else after a day.
    await conn.execute(
        "UPDATE attend_candidates SET decided_at = %s, outcome = 'expired', outcome_at = %s "
        "WHERE decided_at IS NULL AND notification_id IS NULL "
        "AND ((floor AND (deadline < %s OR created_at < %s)) OR created_at < %s)",
        (now, now, now, now - FLOOR_STALE, now - PENDING_KEPT),
    )
    # a check-in closed while its candidate waited (answered, or dropped as stale) goes
    # nowhere.
    await conn.execute(
        "UPDATE attend_candidates a SET decided_at = %s, outcome = 'expired', outcome_at = %s "
        "FROM check_ins ci WHERE a.decided_at IS NULL AND a.notification_id IS NULL "
        "AND ci.id::text = a.delivery->>'check_in_id' "
        "AND (ci.answered_at IS NOT NULL OR ci.dropped_at IS NOT NULL)",
        (now, now),
    )
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT id, source, kind, text, lang, urgency, priority, deadline, floor, outside, alone, must_show, "
        "delivery, created_at FROM attend_candidates WHERE decided_at IS NULL AND notification_id IS NULL "
        "ORDER BY floor DESC, created_at"
    )
    return [
        (
            rules.Candidate(
                str(r["id"]), r["source"], r["kind"], r["text"], r["lang"], r["created_at"], r["urgency"],
                r["deadline"], r["floor"], r["outside"], r["alone"], r["must_show"], r["priority"],
                same_day=r["source"] == "knock" and bool((r["delivery"] or {}).get("same_day")),
            ),
            r["delivery"] or {},
        )
        for r in await cur.fetchall()
    ]  # fmt: skip


async def _doing(conn: AsyncConnection, now: datetime) -> tuple[str | None, bool]:
    """The latest plan's item at this moment (Pisa's own list), and whether its title is a stranger's
    (an invite's, Gmail's, a shared one's, Pisa's after outside text, an email's task)."""
    row = await (
        await conn.execute("SELECT plan, open_tasks FROM plans ORDER BY generated_at DESC LIMIT 1")
    ).fetchone()
    if row is None or not made_from_now(row[1], await open_tasks(conn)):
        return None, False  # no plan, or one of tasks gone since: nothing known to be doing
    for item in row[0].get("items") or []:
        if item.get("start") and item.get("end"):
            if datetime.fromisoformat(item["start"]) <= now < datetime.fromisoformat(item["end"]):
                text = f"{item.get('kind')}: {item.get('title', '')}"[:120]
                return text, await foreign.item_foreign(conn, item)
    return None, False


async def _outcome_of(conn: AsyncConnection, row: dict[str, Any], now: datetime) -> str | None:
    if row["put_off_at"] is not None:
        return "later"
    if row["answered_at"] is not None:
        return "answered"
    if row["dropped_at"] is not None:
        return "dismissed"
    delivered = row["delivered_at"]
    if delivered is None:
        return None  # not out yet (held, waiting for its time or a breakpoint)
    if row["delivered_via"] == "none":
        # never out: it lapsed (past the dispatcher's age, a lapsed offer, a digest item whose event was
        # over), or it went out at its time to no device (none connected, no push token: quality audit §23
        # Gate 7, where the driver keeps no socket). That notice never expired: no outcome of theirs, and
        # no label for the next decisions (they never saw it)
        due = row["not_before"] or row["queued_at"]
        lapsed = (
            row["ask_id"] is not None
            or row["guardian"] == "digest"
            or due is None
            or delivered - due > PENDING_MAX_AGE
        )
        return "expired" if lapsed else None
    done = (row["delivery"] or {}).get("done") or {}
    if done.get("task_id"):
        undone = await (
            await conn.execute(
                "SELECT 1 FROM feedback_events WHERE kind = 'done_undone' AND subject = %s "
                "AND created_at >= %s",
                (f"task:{done['task_id']}", delivered),
            )
        ).fetchone()
        if undone:
            return "undone"
    talked = await (
        await conn.execute(  # from the device the notice went to (any, when it went to all)
            "SELECT 1 FROM messages m JOIN conversations c ON c.id = m.conversation_id WHERE m.role = 'user' "
            "AND m.created_at >= %s AND m.created_at <= %s AND (%s::uuid IS NULL OR c.device_id = %s::uuid) "
            "LIMIT 1",
            (delivered, delivered + rules.ANSWERED_WITHIN, row["device_id"], row["device_id"]),
        )
    ).fetchone()
    if talked:
        return "answered"
    return "ignored" if now - delivered >= rules.IGNORED_AFTER else None


async def dismissed(conn: AsyncConnection, notification_id: uuid.UUID, at: datetime) -> bool:
    """The user dismissed Pisa's notice on the phone (IOS 20's app event): `dismissed`, unless an outcome is
    already known. True when it labelled one."""
    rows = await (
        await conn.execute(
            "UPDATE attend_candidates SET outcome = 'dismissed', outcome_at = %s WHERE notification_id = %s "
            "AND outcome IS NULL RETURNING id, source, decision->>'choice'",
            (at, notification_id),
        )
    ).fetchall()
    for candidate_id, source, choice in rows:
        await conn.execute(
            "UPDATE pisa_decisions SET outcome = 'dismissed', outcome_at = %s "
            "WHERE ref = %s AND tool LIKE 'attend.%%'",
            (at, str(candidate_id)),
        )
        await _feedback(conn, source, choice, "dismissed", at)
    return bool(rows)


async def _feedback(
    conn: AsyncConnection, source: str, choice: str | None, outcome: str, now: datetime
) -> None:
    """The pattern: the user's outcome, as a label for the next decisions' examples."""
    await conn.execute(
        "INSERT INTO feedback_events (id, kind, subject, from_value, to_value, created_at) "
        "VALUES (%s, 'attend', %s, %s, %s, %s)",
        (uuid.uuid4(), f"attend:{source}", choice, outcome, now),
    )
