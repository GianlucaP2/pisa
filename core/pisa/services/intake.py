"""Intake: new email → triage → (for the four "something to do" categories) extraction → tasks.

- Promotions, social and spam (by Gmail label) are never sent to a model: skipped.
- Every model call goes through the gateway, so intake counts against the same caps. budget_exceeded (or a
  provider that is down, a missing key or model) **pauses** intake: the messages stay pending and the next
  sync tries again, so nothing is silently skipped. The pause is logged and kept in
  app_settings['intake_paused'].
- An answer (triage or extraction) still invalid after one retry marks that message `failed` (counted,
  logged) and intake moves on.
- Re-processing a message never duplicates its tasks: (message, normalised title) is unique.
- No task from automated notifications that only inform (triage decides: `needs_task`), and no repeats
  (from the first real day): one task per email (the most important), none while the email's
  thread already has an open task (such a message isn't sent to a model at all), and none when an open task
  created in the last 14 days has a near-identical title (normalised, token-set similarity ≥ 0.8). Existing
  tasks are never touched.
"""

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from rapidfuzz import fuzz

from pisa.brain.intake import (
    EXTRACT_CATEGORIES,
    EXTRACT_MAX_OUTPUT_TOKENS,
    EXTRACT_PROMPT,
    EXTRACT_SLOT,
    TRIAGE_MAX_OUTPUT_TOKENS,
    TRIAGE_PROMPT,
    TRIAGE_SLOT,
    IntakeFailed,
    Triage,
    ValidTask,
    extract,
    gist_json,
    marked_chars,
    needs_task,
    priority_of,
    settle,
    triage,
    unmark,
)
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.brain.reader import SLOT as READER_SLOT
from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.domain import done as done_rules
from pisa.domain import event_kinds, reading
from pisa.domain.attend import Candidate, intake_text
from pisa.domain.email_keys import merge_keys, strip_hidden
from pisa.domain.errors import PisaError
from pisa.domain.mail import (
    NEVER_TO_MODEL_LABELS,
    Address,
    MailMessage,
    brands,
    cancels,
    may_go_to_model,
    person_sender,
)
from pisa.domain.owner import Owner
from pisa.domain.places import physical_place
from pisa.domain.profile import plain
from pisa.domain.reasons import gist_said, said_reason, why_no_task
from pisa.domain.tasks import online_task, title_key
from pisa.domain.tongue import foreign
from pisa.infra import ledger, locks
from pisa.logging import get_logger
from pisa.services import email_rules, guardian, things
from pisa.services import home as home_store
from pisa.services.reader import ReaderService
from pisa.services.tasks import TasksService, insert_task

if TYPE_CHECKING:
    from pisa.services.attend import AttendService

_LOCK_INTAKE = locks.INTAKE
_PAUSING = frozenset(
    {"budget_exceeded", "model_not_configured", "provider_key_missing", "provider_error", "rate_limited"}
)
SIMILAR_WITHIN = timedelta(days=14)  # an open task this recent with a near-identical title: no new one
CORE6_LIVE = datetime(2026, 10, 21, 22, 15, tzinfo=UTC)  # tasks made before this escaped the rules
RETRIAGE_KEY = "intake_retriage_dry_run"  # the last dry run's list: what --apply dismisses
RULE_REASON = "your rule: never"  # a reason from the user's rules: always dismissed, never moved to low
SIMILAR_RATIO = 80  # token-set and plain similarity of the normalised titles, in percent
INBOX_MAX = 50  # the quiet "From your email" list
_log = get_logger("pisa.intake")


@dataclass
class IntakeReport:
    processed: int = 0
    skipped: int = 0
    tasks_created: int = 0
    duplicates: int = 0  # tasks not created: the thread has one, or an open task is near-identical
    failed: int = 0
    paused: str | None = None
    failures: list[str] = field(default_factory=list)


def source_label(message: MailMessage) -> str:
    sender = (message.sender.name or message.sender.address) if message.sender else "unknown sender"
    subject = message.subject.strip() or "(no subject)"
    if len(subject) > 60:
        subject = subject[:59].rstrip() + "…"
    return f"Email from {sender}: {subject}"


# a delivery confirmed this long before a wait at home starts, or after it ended, still closes it
WAIT_EARLY = timedelta(hours=1)
WAIT_LATE = timedelta(minutes=30)
FOLLOW_UP_CHECK_DAYS = 30
FOLLOW_UP_CHECK_MAX = 300  # replies read at most (each a triage call: about €0.0005 on the cheap slot)


def _message(row: tuple) -> MailMessage:  # type: ignore[type-arg]
    mid, thread, from_name, from_address, subject, sent_at, labels, snippet, body = row
    sender = Address(from_name, from_address) if from_address else None
    return MailMessage(mid, thread, sender, (), subject, sent_at, tuple(labels), snippet, body)


@dataclass(frozen=True)
class Retriaged:
    task_id: str
    title: str
    reason: str | None  # why it goes (English, for the logs and the dry run's list); None: it stays
    # what it is, in `why_no_task`'s terms (English, for the logs): the reason they see on
    # a task moved to low is `why_no_task(category, notice)` in their Settings language, never `reason`
    category: str | None = None
    notice: str | None = None


class IntakeService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        gateway: ModelGateway,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attend: "AttendService | None" = None,
    ) -> None:
        self._pool = pool
        self._gateway = gateway
        self._owner = owner
        self._clock = clock
        self._attend = attend  # `tell_owner` emails become candidates

    async def run(self, limit: int = 100) -> IntakeReport:
        report = IntakeReport()
        async with self._pool.connection() as lock:
            got = await (await lock.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_INTAKE,))).fetchone()
            if not (got and got[0]):
                return report
            try:
                await self._run(report, limit)
            finally:
                await lock.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_INTAKE,))
        if report.processed or report.paused or report.failed:
            _log.info(
                "intake_round",
                processed=report.processed,
                skipped=report.skipped,
                tasks_created=report.tasks_created,
                failed=report.failed,
                paused=report.paused is not None,
            )
        return report

    async def _run(self, report: IntakeReport, limit: int) -> None:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, thread_id, from_name, from_address, subject, sent_at, labels, snippet, body "
                    "FROM email_messages WHERE intake_status = 'pending' ORDER BY sent_at, id LIMIT %s",
                    (limit,),
                )
            ).fetchall()
            was_paused = await get_setting(conn, "intake_paused")
        for row in rows:
            message = _message(row)
            try:
                await self._one(message, report)
            except PisaError as err:
                if err.code not in _PAUSING:
                    raise
                report.paused = err.message
                break
        async with self._pool.connection() as conn:
            if report.paused:
                if not was_paused or was_paused.get("message") != report.paused:
                    _log.warning("intake_paused", cause=report.paused)
                await set_setting(
                    conn,
                    "intake_paused",
                    Jsonb({"since": self._clock().isoformat(), "message": report.paused}),
                )
            elif was_paused and report.processed:
                await delete_setting(conn, "intake_paused")
                _log.info("intake_resumed")

    async def _one(self, message: MailMessage, report: IntakeReport) -> None:
        if not may_go_to_model(message.labels):
            await self._finish(message.id, "skipped", "promo_or_spam", None)
            report.skipped += 1
            return
        now = self._clock()
        async with self._pool.connection() as conn:
            if await pisa_sent(conn, message.id):
                # a mail Pisa sent (on their yes) is Pisa's own words: no triage, no task,
                # never a follow-up that could close a task on Pisa's own claim
                await self._finish(message.id, "skipped", "pisa_sent", None)
                report.skipped += 1
                return
            senders, kinds = await email_rules.rules(conn)
            if message.sender and message.sender.address.strip().lower() in senders:
                # the user said never from this sender: not even sent to a model, nor read for a
                # follow-up (the rule first, so a blocked sender can never close a task).
                await self._finish(message.id, "done", "rule_sender", None)
                report.processed += 1
                return
            if await _thread_has_open_task(conn, message.thread_id):
                # a follow-up in the thread may close its task ("pacco ritirato"): the triage reads
                # it (no extraction, never a new task); the email's words stay data.
                await self._follow_up(message, now)
                await self._gist_by_reader(message)  # it may reach the model
                await self._finish(message.id, "done", "same_thread", None)
                report.processed += 1
                report.duplicates += 1
                return
            candidates = await _candidates(conn, message, now)
            person = await _person_writes(conn, message)
            history = await _history(conn, message)
        verdict = await self._triage(message, now, candidates, history)
        if isinstance(verdict, IntakeFailed):
            await self._gist_by_reader(message)
            await self._failed(message.id, None, verdict.reason, report)
            return
        # The model may copy the email's ˆ marks into its sentence; stored, it must read plainly.
        verdict = verdict.model_copy(update={"reason": unmark(verdict.reason)})
        verdict = settle(verdict)  # a parcel to collect, a service to renew by hand: never low
        if person and verdict.automated:
            # "automated" is the model's reading of the email's own text; a person
            # writing "verify your account details" is still a person: never a low notice.
            verdict = verdict.model_copy(update={"automated": False})
        notice = verdict.notice if verdict.notice != "none" else None
        level, why = priority_of(verdict)
        keys = await self._describe(message, verdict, level, history)
        if verdict.gist is None:
            await self._gist_by_reader(message)
        async with self._pool.connection() as conn:  # hard keys only
            thing = await things.link_email(
                conn, message_id=message.id, thread_id=message.thread_id, keys=keys, subject=message.subject,
                gist=gist_json(verdict), now=now,
                sender=message.sender.address if message.sender else None,
            )  # fmt: skip
        if await self._confirmed(message, verdict, thing, now):
            # "Delivered": its thing's task (or the wait at home) is closed; no new task from it
            await self._finish(message.id, "done", "confirmed", None, verdict.reason, notice)
            report.processed += 1
            return
        if verdict.same_as and verdict.same_as in {tid for tid, _ in candidates}:
            # the same request again (a reminder, a second notice): no new task.
            await self._finish(message.id, "done", "duplicate", None, verdict.reason, notice)
            report.processed += 1
            report.duplicates += 1
            return
        if verdict.automated and email_rules.email_kind(verdict.category, notice) in kinds:
            # Kind rules are for what systems send: a person's "pay the F24 by the 16th" is never hidden by
            # a "never bills" tap on a utility's email.
            await self._finish(message.id, "done", "rule_kind", None, verdict.reason, notice)
            report.processed += 1
            return
        await self._tell(message, verdict, notice, now)
        if not needs_task(verdict):
            # an account notice (a CI run, a sign-in alert, a code) is never a task; it stays in the low inbox
            # as the email it is. A "something to do" category left out by the rule is recorded as what it is:
            # a notification.
            category = "notification" if verdict.category in EXTRACT_CATEGORIES else verdict.category
            await self._finish(message.id, "done", category, None, verdict.reason, notice)
            report.processed += 1
            return
        outcome = await self._extract(message, now)
        if isinstance(outcome, IntakeFailed):
            await self._failed(message.id, verdict.category, outcome.reason, report)
            return
        if thing is not None and await self._same_thing(thing, message, verdict, outcome, report):
            return
        created = 0
        chosen = sorted(outcome, key=lambda t: t.priority)[:1]  # one task per email: the most important
        async with self._pool.connection() as conn, conn.transaction():
            # the phone shows the task's reason; one not in their language is the code's line
            why = said_reason(
                why, verdict.category, notice, str((await self._owner.settings(conn))["language"]), task=True
            )
            for task in chosen:
                if await _near_duplicate(conn, task.title, now):
                    report.duplicates += 1
                    _log.info("intake_task_duplicate", message_id=message.id)
                    continue
                inserted = await insert_task(
                    conn,
                    title=task.title,
                    now=now,
                    source_kind="email",
                    due_at=task.due_at,
                    due_kind=task.due_kind,
                    duration_min=task.duration_min,
                    place_text=None
                    if online_task(task.title)  # buying tickets online: the venue isn't where to go
                    else physical_place(task.place_text, message.sender.address if message.sender else None),
                    priority=level,  # triage's, with its reason
                    priority_reason=why,
                    source_ref=message.id,
                    source_label=source_label(message),
                    confidence=task.confidence,
                    expected_from=task.expected_from,
                    expected_until=task.expected_until,
                )
                created += inserted is not None
                if inserted is not None and thing is not None:
                    await things.link_task(conn, thing, inserted.id, now)
            await conn.execute(
                "UPDATE email_messages SET intake_status = 'done', intake_category = %s, "
                "intake_error = NULL, intake_at = %s, intake_reason = %s, intake_notice = %s WHERE id = %s",
                (verdict.category, now, verdict.reason[:300], notice, message.id),
            )
        report.processed += 1
        report.tasks_created += created

    async def _same_thing(
        self,
        thing: uuid.UUID,
        message: MailMessage,
        verdict: Triage,
        outcome: list[ValidTask],
        report: IntakeReport,
    ) -> bool:
        """RAIL: one task per thing: a second email about the same order updates its open task (its
        new due date, when it gives one), never adds one. True when it did."""
        async with self._pool.connection() as conn, conn.transaction():
            task_id = await things.open_task(conn, thing)
            if task_id is None:
                return False
            # The email's task that is this one: the same title_key, else the only one with a window, else the
            # only one; its due date and its window both (a "reso" next to the delivery
            # moved the delivery's day; a courier's "Ricevere il pacco" left the window stale).
            row = await (
                await conn.execute("SELECT title_key FROM tasks WHERE id = %s", (uuid.UUID(task_id),))
            ).fetchone()
            same = [t for t in outcome if row is not None and title_key(t.title) == row[0]]
            windowed = [t for t in outcome if t.expected_from and t.expected_until]
            match = same[0] if same else (windowed[0] if len(windowed) == 1 else None)
            if match is None and len(outcome) == 1:
                match = outcome[0]
            dated = match if match is not None and match.due_at else None
            if dated is not None:
                await conn.execute(
                    "UPDATE tasks SET due_at = %s, due_kind = %s, updated_at = %s WHERE id = %s",
                    (dated.due_at, dated.due_kind, self._clock(), uuid.UUID(task_id)),
                )
            # the courier's next email moves the delivery's window ("arriva tra le 10 e le 14").
            if match is not None and match.expected_from and match.expected_until:
                await conn.execute(
                    "UPDATE tasks SET expected_from = %s, expected_until = %s, updated_at = %s WHERE id = %s",
                    (match.expected_from, match.expected_until, self._clock(), uuid.UUID(task_id)),
                )
            await conn.execute(
                "UPDATE email_messages SET intake_status = 'done', intake_category = %s, "
                "intake_error = NULL, intake_at = %s, intake_reason = %s WHERE id = %s",
                (verdict.category, self._clock(), verdict.reason[:300], message.id),
            )
        report.processed += 1
        report.duplicates += 1
        _log.info("intake_same_thing", message_id=message.id, updated=dated is not None)
        return True

    @ledger.on_eval_budget  # a check, on the evals' own budget, never the user's
    async def follow_up_check(self, days: int = FOLLOW_UP_CHECK_DAYS, *, call: bool = True) -> dict[str, Any]:
        """Question 2, a dry run: the real triage over the last `days` of same-thread replies
        (the follow-ups reads), counting how many would close their task, by category. Changes
        nothing; numbers only (never a subject, a sender or a word). `call` False: the count, no call."""
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, thread_id, from_name, from_address, subject, sent_at, labels, snippet, body "
                    "FROM email_messages WHERE intake_category = 'same_thread' AND sent_at >= %s "
                    "AND NOT (labels && %s) ORDER BY sent_at LIMIT %s",
                    (now - timedelta(days=days), list(NEVER_TO_MODEL_LABELS), FOLLOW_UP_CHECK_MAX),
                )
            ).fetchall()
        out: dict[str, Any] = {"days": days, "replies": len(rows)}
        if not call:
            return out
        by: dict[str, dict[str, int]] = {}
        failed = 0
        for row in rows:
            verdict = await self._triage(_message(row), now)
            if isinstance(verdict, IntakeFailed):
                failed += 1
                continue
            verdict = settle(verdict)
            seen = by.setdefault(verdict.category, {"replies": 0, "would_close": 0, "asks_to_act": 0})
            seen["replies"] += 1
            seen["asks_to_act"] += int(verdict.asks_owner_to_act)
            seen["would_close"] += int(verdict.already_done and not verdict.asks_owner_to_act)
        would = sum(c["would_close"] for c in by.values())
        _log.info("follow_up_check", replies=len(rows), would_close=would, failed=failed)
        return out | {"would_close": would, "failed": failed, "by_category": by}

    async def _follow_up(self, message: MailMessage, now: datetime) -> int:
        """RAIL: when the triage reads the thread's follow-up as everything being over (a
        parcel collected, an order delivered, a booking confirmed), Pisa closes its own open tasks from that
        thread and nothing else, with the same Undo as being there (24 h), and says so once. Returns the
        tasks closed."""
        verdict = await self._triage(message, now)
        if isinstance(verdict, IntakeFailed):
            return 0
        verdict = settle(verdict)  # a parcel waiting to be collected is not over (the rail)
        if not verdict.already_done or verdict.asks_owner_to_act:
            return 0
        async with self._pool.connection() as conn, conn.transaction():
            lang = (await self._owner.settings(conn))["language"]
            rows = await (
                await conn.execute(
                    "UPDATE tasks t SET status = 'done', updated_at = %s FROM email_messages m "
                    "WHERE m.id = t.source_ref AND t.source_kind = 'email' AND t.status = 'open' "
                    "AND m.thread_id = %s RETURNING t.id, t.title",
                    (now, message.thread_id),
                )
            ).fetchall()
            await self._closed(conn, lang, now, [(str(t), title) for t, title in rows], [])
        _log.info("intake_thread_closed", message_id=message.id, closed=len(rows))
        return len(rows)

    async def _confirmed(
        self, message: MailMessage, verdict: Triage, thing: uuid.UUID | None, now: datetime
    ) -> int:
        """An email that says its thing is over (Amazon's
        "Delivered", "consegna effettuata") closes what of that thing (its order or tracking number, whatever
        the thread) is truly over, and a delivery's wait at home in progress ("Consegna spesa (resta a casa)":
        it ends now). The email confirms; it never commands: nothing else changes, the triage's reading
        decides, Undo for 24 h, and Pisa says it once. Returns what it closed.

        a cancellation is never done (nothing closes); the thing's tasks close only on a
        delivery (a receipt never closes "Ritira il pacco"); its events only once ended, or a wait at home in
        progress (never tonight's dinner); a wait not of the thing only when the sender's brand is in its
        title (Amazon's "Delivered" never ends the Esselunga groceries' wait)."""
        if not verdict.already_done or verdict.asks_owner_to_act:
            return 0
        if cancels(message.subject, message.snippet):
            return 0
        delivery = verdict.category == "delivery"
        async with self._pool.connection() as conn, conn.transaction():
            lang = (await self._owner.settings(conn))["language"]
            home = await home_store.read(conn)

            def waiting(title: str, place_id: str | None, lat: float | None, lon: float | None) -> bool:
                coord = (lat, lon) if lat is not None and lon is not None else None
                here = event_kinds.at_home(place_id, coord, home)
                return event_kinds.waits(title, at_home=here)

            tasks: list[tuple[str, str]] = []
            events: list[tuple[str, str, str]] = []
            if thing is not None:
                if delivery:
                    rows = await (
                        await conn.execute(
                            "UPDATE tasks t SET status = 'done', updated_at = %s FROM thing_items ti "
                            "WHERE ti.thing_id = %s AND ti.item_kind = 'task' AND ti.item_ref = t.id::text "
                            "AND t.status = 'open' RETURNING t.id::text, t.title",
                            (now, thing),
                        )
                    ).fetchall()
                    tasks = [(t, title) for t, title in rows]
                linked = await (
                    await conn.execute(
                        "SELECT e.id, e.calendar_id, e.title, e.starts_at, e.ends_at, e.place_id::text, "
                        "p.lat, p.lon FROM thing_items ti JOIN calendar_events e ON e.id = ti.item_ref "
                        "LEFT JOIN places p ON p.id = e.place_id WHERE ti.thing_id = %s "
                        "AND ti.item_kind = 'event' AND e.attended_at IS NULL AND e.status <> 'cancelled'",
                        (thing,),
                    )
                ).fetchall()
                events = [
                    (eid, cal, title) for eid, cal, title, starts, ends, place_id, lat, lon in linked
                    if ends <= now or (
                        starts <= now + WAIT_EARLY and ends >= now - WAIT_LATE
                        and waiting(title, place_id, lat, lon)
                    )
                ]  # fmt: skip
            if delivery:
                # the wait at home in progress (or about to start) of this sender: the delivery came
                brand = {plain(b) for b in brands(message.sender)}
                rows = await (
                    await conn.execute(
                        "SELECT e.id, e.calendar_id, e.title, e.place_id::text, p.lat, p.lon "
                        "FROM calendar_events e LEFT JOIN places p ON p.id = e.place_id WHERE NOT e.all_day "
                        "AND e.status <> 'cancelled' AND e.attended_at IS NULL AND e.starts_at <= %s "
                        "AND e.ends_at >= %s",
                        (now + WAIT_EARLY, now - WAIT_LATE),
                    )
                ).fetchall()
                events += [
                    (eid, cal, title) for eid, cal, title, place_id, lat, lon in rows
                    if brand & set(plain(title).split()) and waiting(title, place_id, lat, lon)
                    and all(eid != e[0] for e in events)
                ]  # fmt: skip
            for eid, _, _ in events:
                await conn.execute("UPDATE calendar_events SET attended_at = %s WHERE id = %s", (now, eid))
            await self._closed(conn, lang, now, tasks, events)
        if tasks or events:
            _log.info("intake_confirmed_closed", message_id=message.id, tasks=len(tasks), events=len(events))
        return len(tasks) + len(events)

    async def _closed(
        self,
        conn: AsyncConnection,
        lang: str,
        now: datetime,
        tasks: list[tuple[str, str]],
        events: list[tuple[str, str, str]],
    ) -> None:
        """What an email closed: the same Undo as being there (24 h), said once (a `done` candidate whose
        words rest on outside text: the titles may be the email's)."""
        undo_until = now + done_rules.UNDO_FOR
        day = now.astimezone(self._owner.zone).date()
        until = undo_until.astimezone(self._owner.zone).isoformat()
        for task_id, title in tasks:
            await conn.execute(
                "INSERT INTO done_by_presence (id, task_id, day, learn, title, source, done_at, undo_until) "
                "VALUES (%s, %s, %s, false, %s, 'email', %s, %s)",
                (uuid.uuid4(), task_id, day, title, now, undo_until),
            )
            payload = {"task_id": task_id, "title": title[:200], "place": None, "undo_until": until}
            await self._say_done(conn, lang, now, title, f"thread_done:{task_id}", payload)
        for event_id, calendar_id, title in events:
            await conn.execute(
                "INSERT INTO done_by_presence (id, calendar_id, event_id, day, learn, title, source, "
                "done_at, undo_until) VALUES (%s, %s, %s, %s, false, %s, 'email', %s, %s)",
                (uuid.uuid4(), calendar_id, event_id, day, title, now, undo_until),
            )
            payload = {"event_id": event_id, "title": title[:200], "place": None, "undo_until": until}
            await self._say_done(conn, lang, now, title, f"email_done:{event_id}", payload)

    async def _say_done(
        self, conn: AsyncConnection, lang: str, now: datetime, title: str, key: str, payload: dict[str, Any]
    ) -> None:
        if self._attend is None:
            return
        text = done_rules.done_text(title, lang)
        # the title may be the email's: its words rest on outside text
        candidate = Candidate(str(uuid.uuid4()), "stay", "done", text, lang, now, outside=True, alone=True)
        await self._attend.propose(conn, candidate, key, {"done": payload})

    async def _tell(self, message: MailMessage, verdict: Triage, notice: str | None, now: datetime) -> None:
        """An email the user would want to hear about now (the triage's `tell_owner`, no extra call) becomes
        a candidate, in Pisa's words from the gist's fixed fields. RAIL: never an automated notice; high
        priority is never dropped silently (shown at least)."""
        if self._attend is None or not verdict.tell_owner or verdict.gist is None:
            return
        if verdict.automated and notice is not None:
            return
        gist = gist_json(verdict) or {}
        async with self._pool.connection() as conn:
            lang = (await self._owner.settings(conn))["language"]
            # the gist's free fields are the triage's, never asked for their language: what
            # it is gets the category's line when it is not theirs; what is asked, only when it is theirs
            what = gist_said(gist.get("what") or "", verdict.category, notice, lang) or ""
            asked = None if foreign(gist.get("asked"), lang) else gist.get("asked")
            text = intake_text(gist.get("who"), asked, what, lang)
            candidate = Candidate(
                str(uuid.uuid4()), "intake", "info", text[:300], lang, now, outside=True,
                must_show=verdict.priority == "high",
                speak="reply_waiting" if gist.get("asked") else None,  # someone waits for a reply
            )  # fmt: skip
            person = await guardian.person_of(conn, [message.sender.address] if message.sender else [])
            delivery = {"person_id": str(person)} if person is not None else None
            await self._attend.propose(conn, candidate, f"intake:{message.id}", delivery)

    async def _describe(
        self, message: MailMessage, verdict: Triage, level: int, history: Sequence[str] = ()
    ) -> list[dict[str, str]]:
        """The email's priority, its gist (fixed fields) and its keys (verbatim, plus every key
        the rules find), kept with the email; and its situation reading, each field resting on words
        of the email or of the history given, else unknown."""
        text = f"{message.subject}\n{message.body if message.body is not None else message.snippet}"
        keys = merge_keys([k.model_dump() for k in verdict.keys], text)
        gist = gist_json(verdict)
        raw = verdict.reading.model_dump() if verdict.reading else None
        if raw is not None:
            raw = {name: {k: unmark(v) if isinstance(v, str) else v for k, v in field.items()}
                   for name, field in raw.items()}  # fmt: skip
        # Evidence: the email, and the history's subjects only (never a gist Pisa wrote).
        subjects = [m.group(1) for line in history if (m := re.search(r"«(.*)»", line))]
        read = reading.checked(raw, text, subjects)
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE email_messages SET intake_priority = %s, intake_gist = %s, intake_keys = %s, "
                "intake_reading = %s WHERE id = %s",
                (
                    level,
                    Jsonb(gist) if gist else None,
                    Jsonb(keys),
                    Jsonb(read) if read else None,
                    message.id,
                ),
            )
        return keys

    async def _gist_by_reader(self, message: MailMessage) -> None:
        """Every mail that can reach the model has a summary written at intake by
        the tool-less reader when the triage gave none, so a voice turn never waits on a reader."""
        text = f"{message.subject}\n{message.body if message.body is not None else message.snippet}"
        async with self._pool.connection() as conn:
            lang = str((await self._owner.settings(conn))["language"])
        # the reader's own slot only (never the conversation's budget); a pause (budget, provider
        # down) pauses the intake, as the triage's does
        said = await ReaderService(self._gateway).summary(
            f"email:{message.id}", "email", text, lang, slots=(READER_SLOT,), reraise=_PAUSING
        )
        if not said:
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE email_messages SET intake_gist = %s WHERE id = %s AND intake_gist IS NULL",
                (Jsonb({"what": said}), message.id),
            )

    async def _failed(self, message_id: str, category: str | None, reason: str, report: IntakeReport) -> None:
        await self._finish(message_id, "failed", category, reason)
        report.failed += 1
        report.failures.append(message_id)
        _log.warning("intake_message_failed", message_id=message_id, step="extract" if category else "triage")

    # --- the user's rules and the quiet list ---------------------------------------------------------------

    async def dismiss_task(
        self, tasks: TasksService, task_id: str, never: email_rules.Never | None, lang: str
    ) -> dict[str, Any]:
        """POST /v1/tasks/{id}/dismiss: dismiss it (reversible); with `never`, also "never from this sender" /
        "never this kind": the rule is kept as the user's `source` claim and dismisses the open email tasks
        it covers."""
        task = await tasks.get(task_id)
        rule: str | None = None
        rule_id: str | None = None
        also: list[str] = []
        if never is not None:
            if task.source_kind != "email" or task.source_ref is None:
                raise PisaError(
                    "validation_failed", "Only a task from an email can say never from its sender or kind.",
                    "Dismiss it without never",
                )  # fmt: skip
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT from_address, intake_category, intake_notice FROM email_messages "
                        "WHERE id = %s",
                        (task.source_ref,),
                    )
                ).fetchone()
            value = None
            if row is not None:
                value = row[0] if never == "sender" else email_rules.email_kind(row[1], row[2])
            if not value:
                raise PisaError(
                    "validation_failed",
                    f"Pisa doesn't know this email's {never}.",
                    "Dismiss it without never",
                )
            now = self._clock()
            async with self._pool.connection() as conn, conn.transaction():
                rule, rule_id = await email_rules.add(conn, never, value, lang, now)
                also = [t for t in await email_rules.dismiss_covered(conn, never, value, now) if t != task.id]
                await email_rules.log_made(conn, rule_id, also, now)
        if task.status == "open":
            task = await tasks.update(task.id, {"status": "dismissed"})
        _log.info("task_dismissed", never=never or "none", also=len(also))
        return {"task": task, "rule": rule, "rule_id": rule_id, "also_dismissed": also}

    async def inbox(self, lang: str, limit: int = INBOX_MAX) -> list[dict[str, Any]]:
        """GET /v1/inbox, the quiet "From your email" list: recent emails that became no task, newest
        first, each with why. Never on Today; spam and promotions aren't listed. It is also the "Low
        priority" list's email part."""
        async with self._pool.connection() as conn:
            return await quiet_inbox(conn, lang, limit)

    async def retriage(
        self, since: datetime, *, apply: bool, until: datetime | None = None, to_low: bool = False
    ) -> list[Retriaged]:
        """The first sync's tasks under the rules: the open email tasks created from `since` until the
        rules went live (`CORE6_LIVE`). A task goes (dismissed, never deleted) when another message of its
        thread already has a task kept (two tasks of the same email both stay), when an open task kept before
        it has a near-identical title, or when the triage rule says its email is no task (a notification that
        only informs). The cheap rules run first; the triage calls the model (metered, under the caps).

        The dry run (`apply` false) changes no task: it stores what it would dismiss. `apply` then dismisses
        exactly that list (those still open), with no model call: what was reviewed is what changes.


        the sweep: `until` (default: when the rules went live) set to now checks every open email task under
        today's rules too: an account notice is never a task, and the user's "never from this sender / this
        kind" rules hold.

        `to_low` applies the list by moving the tasks to low priority (with the reason) instead of
        dismissing them: nothing is lost. The user's "never" rules still dismiss."""
        until = until or CORE6_LIVE
        if apply:
            return await self._apply_retriage(since, until, to_low=to_low)
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT t.id::text, t.title, t.title_key, t.created_at, m.id, m.thread_id, m.from_name, "
                    "m.from_address, m.subject, m.sent_at, m.labels, m.snippet, m.body FROM tasks t "
                    "LEFT JOIN email_messages m ON m.id = t.source_ref WHERE t.status = 'open' "
                    "AND t.source_kind = 'email' AND t.created_at >= %s AND t.created_at < %s "
                    "ORDER BY t.created_at, t.id",
                    (since, until),
                )
            ).fetchall()
            others = await (
                await conn.execute(
                    "SELECT t.title_key, m.thread_id, m.id FROM tasks t LEFT JOIN email_messages m "
                    "ON m.id = t.source_ref AND t.source_kind = 'email' WHERE t.status = 'open' "
                    "AND NOT (t.source_kind = 'email' AND t.created_at >= %s AND t.created_at < %s)",
                    (since, until),
                )
            ).fetchall()
            senders, kinds = await email_rules.rules(conn)
        kept_titles: list[tuple[str, str]] = []  # (key, title) of the tasks that stay, in order
        threads: dict[str, set[str | None]] = {}  # thread → the messages with a task kept
        for _, thread, mid in others:
            if thread:
                threads.setdefault(thread, set()).add(mid)
        kept_keys = [key for key, _, _ in others if key]
        out: list[Retriaged] = []
        for task_id, title, key, _, mid, thread, *rest in rows:
            reason, category, notice = None, None, None
            if thread and threads.get(thread, set()) - {mid}:
                reason, category = "another task of the same email thread stays", "same_thread"
            elif (same := next((t for k, t in kept_titles if similar_titles(key, k)), None)) is not None:
                reason, category = f"same as «{same}»", "duplicate"
            elif any(similar_titles(key, k) for k in kept_keys):
                reason, category = "same as another open task", "duplicate"
            elif rest[1] and rest[1].strip().lower() in senders:
                reason, category = f"{RULE_REASON} from this sender", "rule_sender"
            elif mid is not None and may_go_to_model(tuple(rest[4] or ())):
                message = _message((mid, thread, *rest))
                verdict = await self._triage(message, now)
                if isinstance(verdict, Triage) and verdict.automated:
                    async with self._pool.connection() as conn:
                        if await _person_writes(conn, message):  # in the sweep too
                            verdict = verdict.model_copy(update={"automated": False})
                if isinstance(verdict, Triage):
                    verdict = settle(verdict)  # what the user must still do stays (not low)
                    notice = verdict.notice if verdict.notice != "none" else None
                    if verdict.automated and email_rules.email_kind(verdict.category, notice) in kinds:
                        reason, category = f"{RULE_REASON} this kind", "rule_kind"
                    elif not needs_task(verdict):
                        reason = f"no task: {notice or verdict.category}" + (
                            " (automated)" if verdict.automated else ""
                        )
                        # as intake records it: a "something to do" category left out is a notification
                        category = (
                            "notification" if verdict.category in EXTRACT_CATEGORIES else verdict.category
                        )
            if reason is None:
                kept_titles.append((key, title))
                if thread:
                    threads.setdefault(thread, set()).add(mid)
            else:
                notice = notice if category in ("notification", "rule_kind") else None
            out.append(Retriaged(task_id, title, reason, category, notice if reason else None))
        dismiss = [
            {"task_id": r.task_id, "reason": r.reason, "category": r.category, "notice": r.notice}
            for r in out
            if r.reason
        ]
        dry_run = {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "at": now.isoformat(),
            "dismiss": dismiss,
        }
        async with self._pool.connection() as conn:
            await set_setting(conn, RETRIAGE_KEY, Jsonb(dry_run))
        _log.info("intake_retriaged", checked=len(out), dismissed=len(dry_run["dismiss"]), applied=False)
        return out

    async def _apply_retriage(self, since: datetime, until: datetime, *, to_low: bool) -> list[Retriaged]:
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            stored = await get_setting(conn, RETRIAGE_KEY)
            if (
                not isinstance(stored, dict)
                or stored.get("since") != since.isoformat()
                or stored.get("until", CORE6_LIVE.isoformat()) != until.isoformat()
            ):
                raise PisaError(
                    "validation_failed",
                    f"No dry run of the re-triage from {since.date()} is stored, so there is nothing "
                    "reviewed to apply.",
                    "Run it without --apply first, read the list, then apply",
                )
            if any("category" not in d for d in stored["dismiss"]):  # a list from before
                raise PisaError(
                    "validation_failed",
                    "The stored dry run of the re-triage has no categories, so the reasons can't be said in "
                    "their language.",
                    "Run it again without --apply, read the list, then apply",
                )
            reasons = {d["task_id"]: d["reason"] for d in stored["dismiss"]}
            kinds = {d["task_id"]: (d["category"], d["notice"]) for d in stored["dismiss"]}
            ruled = [t for t, (category, _) in kinds.items() if category in ("rule_sender", "rule_kind")]
            lang = str((await self._owner.settings(conn))["language"])
            dismissed = [t for t in reasons if not to_low or t in ruled]
            rows = await (
                await conn.execute(
                    "UPDATE tasks SET status = 'dismissed', updated_at = %s WHERE id = ANY(%s::uuid[]) "
                    "AND status = 'open' RETURNING id::text, title",
                    (now, dismissed),
                )
            ).fetchall()
            if to_low:  # low, with the reason, unless the user set its priority
                for task_id in (t for t in reasons if t not in ruled):
                    rows += await (
                        await conn.execute(
                            "UPDATE tasks SET priority = 3, priority_reason = %s, updated_at = %s "
                            "WHERE id = %s AND status = 'open' AND priority_source = 'pisa' "
                            "RETURNING id::text, title",
                            # the reason they see, in their language; the English stays in
                            # the dry run's list and the logs
                            (why_no_task(*kinds[task_id], lang), now, uuid.UUID(task_id)),
                        )
                    ).fetchall()
            await set_setting(conn, RETRIAGE_KEY, Jsonb(None))  # applied once
        _log.info("intake_retriaged", checked=len(reasons), dismissed=len(rows), applied=True)
        return [Retriaged(task_id, title, reasons[task_id], *kinds[task_id]) for task_id, title in rows]

    async def _triage(
        self,
        message: MailMessage,
        now: datetime,
        candidates: Sequence[tuple[str, str]] = (),
        history: Sequence[str] = (),
    ) -> Triage | IntakeFailed:
        async with self._pool.connection() as conn:  # the reasons in their language
            lang = str((await self._owner.settings(conn))["language"])

        async def call(ctx: CallContext) -> Triage | IntakeFailed:
            output, usage = await triage(
                ctx.model, message, now, self._owner.tz, candidates, history, lang=lang
            )
            ctx.usage.update(usage)
            return output

        return await self._gateway.run(
            TRIAGE_SLOT,
            call,
            prompt_chars=len(TRIAGE_PROMPT)
            + marked_chars(message)
            + sum(len(t) + 40 for _, t in candidates)
            + sum(len(h) + 10 for h in history),
            max_output_tokens=TRIAGE_MAX_OUTPUT_TOKENS,
        )

    async def _extract(self, message: MailMessage, now: datetime) -> list[ValidTask] | IntakeFailed:
        async def call(ctx: CallContext) -> list[ValidTask] | IntakeFailed:
            outcome, usage = await extract(ctx.model, message, now, self._owner.tz)
            ctx.usage.update(usage)
            return outcome

        return await self._gateway.run(
            EXTRACT_SLOT,
            call,
            prompt_chars=len(EXTRACT_PROMPT) + marked_chars(message),
            max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
        )

    async def _finish(
        self,
        message_id: str,
        status: str,
        category: str | None,
        error: str | None,
        reason: str | None = None,
        notice: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE email_messages SET intake_status = %s, intake_category = %s, intake_error = %s, "
                "intake_at = %s, intake_reason = %s, intake_notice = %s WHERE id = %s",
                (
                    status,
                    category,
                    error,
                    self._clock(),
                    reason[:300] if reason else None,
                    notice,
                    message_id,
                ),
            )


INBOX_CATEGORIES = ("notification", "info", "duplicate", "rule_sender", "rule_kind", "same_thread")


async def quiet_inbox(conn: AsyncConnection, lang: str, limit: int = INBOX_MAX) -> list[dict[str, Any]]:
    rows = await (
        await conn.execute(
            "SELECT m.id, m.subject, m.from_name, m.from_address, m.sent_at, m.intake_category, "
            "m.intake_notice, m.intake_reason FROM email_messages m WHERE m.intake_status = 'done' "
            "AND m.intake_category = ANY(%s) AND NOT EXISTS (SELECT 1 FROM tasks t "
            "WHERE t.source_kind = 'email' AND t.source_ref = m.id) ORDER BY m.sent_at DESC LIMIT %s",
            (list(INBOX_CATEGORIES), limit),
        )
    ).fetchall()
    return [
        {
            "id": mid, "subject": subject or "", "sender": name or address or "",
            "sender_address": address, "sent_at": sent_at,
            "kind": email_rules.email_kind(category, notice), "why": why_no_task(category, notice, lang),
            "detail": said_reason(reason, category, notice, lang),
        }
        for mid, subject, name, address, sent_at, category, notice, reason in rows
    ]  # fmt: skip


async def email_view(conn: AsyncConnection, message_id: str, lang: str) -> dict[str, Any] | None:
    """One email by id for the phone's `email:<id>` object, as Pisa's views read it:
    who, when, the subject, Pisa's one line on what it is (the gist), why it is no task or the task it became.
    Never its body; never spam or promotions."""
    row = await (
        await conn.execute(
            "SELECT m.id, m.subject, m.from_name, m.from_address, m.sent_at, m.intake_category, "
            "m.intake_notice, m.intake_reason, m.intake_gist->>'what', "
            "(SELECT t.id::text FROM tasks t WHERE t.source_kind = 'email' AND t.source_ref = m.id "
            "AND t.status <> 'deleted' ORDER BY t.created_at LIMIT 1) "
            "FROM email_messages m WHERE m.id = %s AND NOT (m.labels && %s)",
            (message_id, list(NEVER_TO_MODEL_LABELS)),
        )
    ).fetchone()
    if row is None:
        return None
    mid, subject, name, address, sent_at, category, notice, reason, gist, task_id = row
    quiet = task_id is None and category in INBOX_CATEGORIES
    return {
        "id": mid, "subject": subject or "", "sender": name or address or "", "sender_address": address,
        # Pisa's one line on it is the triage's, never asked for their language: checked on read
        "sent_at": sent_at, "gist": gist_said(gist, category, notice, lang), "task_id": task_id,
        "kind": email_rules.email_kind(category, notice) if quiet else None,
        "why": why_no_task(category, notice, lang) if quiet else None,
        "detail": said_reason(reason, category, notice, lang),
    }  # fmt: skip


async def _person_writes(conn: AsyncConnection, message: MailMessage) -> bool:
    """A person wrote it: a known correspondent (the user has written to this address), or a sender that
    reads like a person (pisa.domain.mail.person_sender)."""
    if message.sender is None:
        return False
    address = message.sender.address.strip().lower()
    # One lookup in the correspondents Pisa keeps as mail syncs, not a scan of the sent mail.
    row = await (
        await conn.execute("SELECT 1 FROM email_correspondents WHERE address = %s", (address,))
    ).fetchone()
    return row is not None or person_sender(message.sender.name, address, FREEMAIL_DOMAINS)


HISTORY_THREAD = 3  # the earlier emails of the thread given to the reading
HISTORY_SENDER = 3  # and the last ones with this sender, in other threads
HISTORY_LINE = 200


async def _history(conn: AsyncConnection, message: MailMessage) -> list[str]:
    """What this email follows, from Pisa's own data, oldest first: the earlier emails of its thread and the
    last ones with this sender (date, from, subject, and what the gist said it was). Never their bodies."""
    address = message.sender.address.strip().lower() if message.sender else ""
    rows = await (
        await conn.execute(
            "(SELECT sent_at, from_address, subject, intake_gist->>'what' FROM email_messages "
            "WHERE thread_id = %s AND id <> %s AND sent_at < %s AND NOT (labels && %s) "
            "ORDER BY sent_at DESC LIMIT %s) "
            "UNION (SELECT sent_at, from_address, subject, intake_gist->>'what' FROM email_messages "
            "WHERE lower(from_address) = %s AND id <> %s AND thread_id <> %s AND sent_at < %s "
            "AND NOT (labels && %s) ORDER BY sent_at DESC LIMIT %s) ORDER BY 1",
            (
                message.thread_id,
                message.id,
                message.sent_at,
                list(NEVER_TO_MODEL_LABELS),  # spam, drafts, promotions stay out
                HISTORY_THREAD,
                address,
                message.id,
                message.thread_id,
                message.sent_at,
                list(NEVER_TO_MODEL_LABELS),
                HISTORY_SENDER,
            ),
        )
    ).fetchall()
    lines = []
    for sent_at, sender, subject, what in rows:
        # Outside text: hidden and tag characters out, as in the email itself.
        said = " ".join(strip_hidden(subject or "").split())
        line = f"{sent_at:%Y-%m-%d}, from {sender or 'unknown'}: «{said}»"
        if what:
            line += f" — {' '.join(strip_hidden(what).split())}"
        lines.append(line[:HISTORY_LINE])
    return lines


CANDIDATES_WITHIN = timedelta(days=30)
CANDIDATES_MAX = 5
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.it", "live.com", "live.it",
    "msn.com", "yahoo.com", "yahoo.it", "icloud.com", "me.com", "mac.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tim.it", "fastwebnet.it", "email.it", "inwind.it", "aol.com",
    "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "gmx.de", "web.de", "ziggo.nl", "kpnmail.nl", "zoho.com", "yandex.com", "mail.com",
})  # fmt: skip


async def _candidates(conn: AsyncConnection, message: MailMessage, now: datetime) -> list[tuple[str, str]]:
    """Duplicates by meaning: the user's open tasks this email might repeat, for the triage model to
    compare as data: from the same sender's domain in the last 30 days (three renewal emails with different
    subjects are one task)."""
    if message.sender is None or "@" not in message.sender.address:
        return []
    address = message.sender.address.strip().lower()
    domain = address.rsplit("@", 1)[1]
    # A shared mail provider's domain is many people: there only the same address.
    freemail = domain in FREEMAIL_DOMAINS
    rows = await (
        await conn.execute(
            "SELECT t.id::text, t.title FROM tasks t JOIN email_messages m ON m.id = t.source_ref "
            "WHERE t.source_kind = 'email' AND t.status = 'open' AND t.created_at > %s "
            "AND CASE WHEN %s THEN lower(m.from_address) = %s "
            "ELSE lower(split_part(m.from_address, '@', 2)) = %s END "
            "ORDER BY t.created_at DESC LIMIT %s",
            (now - CANDIDATES_WITHIN, freemail, address, domain, CANDIDATES_MAX),
        )
    ).fetchall()
    return [(tid, title) for tid, title in rows]


async def pisa_sent(conn: AsyncConnection, message_id: str) -> bool:
    """The mail is one Pisa sent (`pisa_sent_mail`, the id Gmail's send returned)."""
    row = await (
        await conn.execute("SELECT 1 FROM pisa_sent_mail WHERE gmail_message_id = %s", (message_id,))
    ).fetchone()
    return row is not None


async def _thread_has_open_task(conn: AsyncConnection, thread_id: str) -> bool:
    row = await (
        await conn.execute(
            "SELECT 1 FROM tasks t JOIN email_messages m ON m.id = t.source_ref "
            "WHERE t.source_kind = 'email' AND t.status = 'open' AND m.thread_id = %s LIMIT 1",
            (thread_id,),
        )
    ).fetchone()
    return row is not None


async def _near_duplicate(conn: AsyncConnection, title: str, now: datetime) -> bool:
    """An open task created in the last 14 days whose normalised title is near-identical: token-set ≥ 0.8,
    plain ratio ≥ 0.8, and no word of either title missing from the other but for a near match (≥ 0.8: a
    typo). Token-set alone matches "Pagare" with "Pagare multa entro 60 giorni"; both ratios still pass
    "Scrivere a Nino" / "Scrivere a Anna" (80) and the bills for "luce" and "gas" (84): the words don't.
    """
    rows = await (
        await conn.execute(
            "SELECT title_key FROM tasks WHERE status = 'open' AND created_at > %s", (now - SIMILAR_WITHIN,)
        )
    ).fetchall()
    key = title_key(title)
    return any(similar_titles(key, other) for (other,) in rows if other)


def similar_titles(a: str, b: str) -> bool:
    """Two normalised titles that are the same task (the rule above)."""
    return (
        fuzz.token_set_ratio(a, b) >= SIMILAR_RATIO
        and fuzz.ratio(a, b) >= SIMILAR_RATIO
        and _same_words(a, b)
    )


def _same_words(a: str, b: str) -> bool:
    """Every word of each title is in the other, or nearly is (a typo, a plural)."""
    left, right = set(a.split()), set(b.split())

    def covered(words: set[str], other: set[str]) -> bool:
        return all(any(fuzz.ratio(w, o) >= SIMILAR_RATIO for o in other) for w in words - other)

    return covered(left, right) and covered(right, left)
