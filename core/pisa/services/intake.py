"""Intake: new email -> one events call -> storylines -> tasks (LIFE-GRAPH section 4, EMAIL-UNDERSTANDING).

- Promotions, social and spam (by Gmail label), mail Pisa sent itself, and the senders the user said never
  to make tasks from are never sent to a model: skipped.
- Every other new message gets exactly ONE model call (`brain.mail_events`, slot intake.extract). It reads
  the message's new text only (its quoted history is cut away), and answers with events and effects. Code
  checks them (`domain.mail_events`) and `services.storylines` links them, replays the storyline through its
  playbook, and projects the owner's pending actions as tasks. There is no triage, no extraction and no
  summary call.
- Sent mail counts: the owner's own messages are read too (his reply moves a storyline: the shop owes the
  next move), and his own words never come back as a request to him.
- Every model call goes through the gateway, so intake counts against the same caps. budget_exceeded (or a
  provider that is down, a missing key or model) **pauses** intake: the messages stay pending and the next
  sync tries again, so nothing is silently skipped. The pause is logged and kept in
  app_settings['intake_paused'].
- An answer still invalid after one retry marks that message `failed` (counted, logged) and intake moves on.
- Re-processing a message never duplicates its events (`mail_event` is written once per message) or its
  tasks (one per entity and intent).
- The one-time re-triage (`retriage`) reads the open tasks the old intake made through the same path and
  writes PROPOSALS only: nothing closes or changes until the owner says yes (`storylines.accept_proposal`).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain.mail_events import MAX_OUTPUT_TOKENS, PROMPT, SLOT, EventsFailed, marked_chars, read_events
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.db.settings import delete_setting, get_setting, set_setting
from pisa.domain import mail_events
from pisa.domain.errors import PisaError
from pisa.domain.mail import FREEMAIL_DOMAINS, NEVER_TO_MODEL_LABELS, may_go_to_model, person_sender
from pisa.domain.owner import Owner
from pisa.domain.playbooks import mail_category
from pisa.domain.quote_cut import cut_text
from pisa.domain.reasons import gist_said, said_reason, why_no_task
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services import email_rules, storylines, things
from pisa.services.storylines import Msg, OwnerContext
from pisa.services.tasks import TasksService

__all__ = ["FREEMAIL_DOMAINS", "IntakeReport", "IntakeService", "email_view", "pisa_sent", "quiet_inbox"]

_LOCK_INTAKE = locks.INTAKE
_PAUSING = frozenset(
    {"budget_exceeded", "model_not_configured", "provider_key_missing", "provider_error", "rate_limited"}
)
INBOX_MAX = 50  # the quiet "From your email" list
THREAD_MAX = 20  # a thread's messages the re-triage reads at most
OWNER_SAID_MAX = 5  # the owner's earlier messages of a thread whose sentences guard against quote leakage
_log = get_logger("pisa.intake")
# what "a key" is called in `things` (the older link of an email to the tasks and events it is about)
_THING_KIND = {
    "order_id": "order",
    "tracking": "tracking",
    "booking_ref": "booking",
    "invoice_no": "reference",
    "other": "reference",
}


@dataclass
class IntakeReport:
    processed: int = 0
    skipped: int = 0
    tasks_created: int = 0
    tasks_changed: int = 0
    proposals: int = 0
    failed: int = 0
    paused: str | None = None
    failures: list[str] = field(default_factory=list)


@dataclass
class RetriageReport:
    """What the one-time re-triage did: counts only (never a title, a sender or a word)."""

    tasks: int = 0
    threads: int = 0
    messages_read: int = 0
    proposals: int = 0
    failed: int = 0
    paused: str | None = None


_ROW = (
    "id, thread_id, from_name, from_address, subject, sent_at, labels, snippet, body, new_text, quoted, "
    "owner_authored"
)


def _msg(row: tuple[Any, ...]) -> tuple[Msg, str]:
    """(the message as the linker reads it, the text the model reads: its new text, never its quote)."""
    mid, thread, name, address, subject, sent_at, _labels, snippet, body, new_text, _quoted, authored = row
    text = new_text if new_text is not None else (cut_text(body).new_text if body else snippet or "")
    return Msg(mid, thread, name, address, subject or "", sent_at, bool(authored)), text.strip()


class IntakeService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        gateway: ModelGateway,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._gateway = gateway
        self._owner = owner
        self._clock = clock

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
                proposals=report.proposals,
                failed=report.failed,
                paused=report.paused is not None,
            )
        return report

    async def _run(self, report: IntakeReport, limit: int) -> None:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"SELECT {_ROW} FROM email_messages WHERE intake_status = 'pending' "  # noqa: S608
                    "ORDER BY sent_at, id LIMIT %s",
                    (limit,),
                )
            ).fetchall()
            was_paused = await get_setting(conn, "intake_paused")
        for row in rows:
            try:
                await self._one(row, report)
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

    async def _one(self, row: tuple[Any, ...], report: IntakeReport) -> None:
        msg, text = _msg(row)
        labels = tuple(row[6])
        if not may_go_to_model(labels):
            await self._finish(msg.id, "skipped", "promo_or_spam")
            report.skipped += 1
            return
        async with self._pool.connection() as conn:
            if await pisa_sent(conn, msg.id):
                # RAIL: a mail Pisa sent (on their yes) is Pisa's own words: no events, never a task or a
                # closing
                await self._finish(msg.id, "skipped", "pisa_sent")
                report.skipped += 1
                return
            senders, kinds = await email_rules.rules(conn)
        if msg.from_address and msg.from_address.strip().lower() in senders:
            # RAIL: the user said never from this sender: not sent to a model, and (the rule first) a blocked
            # sender can never move a storyline
            await self._finish(msg.id, "done", "rule_sender")
            report.processed += 1
            return
        if not text:
            await self._finish(msg.id, "done", "info")
            report.processed += 1
            return
        found = await self._read(msg, text)
        if isinstance(found, EventsFailed):
            await self._finish(msg.id, "failed", None, found.reason)
            report.failed += 1
            report.failures.append(msg.id)
            _log.warning("intake_message_failed", message_id=msg.id)
            return
        category = mail_category([e.kind for e in found.events])
        automated = not msg.owner_authored and not person_sender(
            msg.from_name, msg.from_address, FREEMAIL_DOMAINS
        )
        if automated and category in kinds:
            # Kind rules are for what systems send: a person's "pay the F24 by the 16th" is never hidden by a
            # "never bills" tap on a utility's email.
            await self._finish(msg.id, "done", "rule_kind")
            report.processed += 1
            return
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            owner = await self._owner_context(conn)
            out = await storylines.ingest(conn, msg, found, now, owner)
            if keys := [
                {"kind": _THING_KIND.get(k.kind, "reference"), "value": k.verbatim} for k in found.keys
            ]:
                thing = await things.link_email(
                    conn, message_id=msg.id, thread_id=msg.thread_id, keys=keys,
                    subject=msg.subject, gist=None,
                    now=now, sender=msg.from_address,
                )  # fmt: skip
                if thing is not None:
                    for task_id in out.task_ids:
                        await things.link_task(conn, thing, task_id, now)
            await conn.execute(
                "UPDATE email_messages SET intake_status = 'done', intake_category = %s, "
                "intake_error = NULL, intake_at = %s WHERE id = %s",
                (category, now, msg.id),
            )
        report.processed += 1
        report.tasks_created += out.tasks_created
        report.tasks_changed += out.tasks_changed
        report.proposals += out.proposals

    async def _read(self, msg: Msg, text: str) -> mail_events.Understood | EventsFailed:
        """The events call: ONE per message. The model reads the new text; code checks what comes back."""
        now = self._clock()
        async with self._pool.connection() as conn:
            said = await _owner_said(conn, msg)
            replying = msg.owner_authored and await _others_wrote(conn, msg)

        async def call(ctx: CallContext) -> Any:
            output, usage = await read_events(
                ctx.model,
                subject=msg.subject,
                sender=f"{msg.from_name} <{msg.from_address}>" if msg.from_address else None,
                sent_at=msg.sent_at,
                text=text,
                owner_wrote=msg.owner_authored,
                now=now,
                tz=self._owner.tz,
            )
            ctx.usage.update(usage)
            return output

        raw = await self._gateway.run(
            SLOT,
            call,
            prompt_chars=len(PROMPT) + marked_chars(msg.subject, text),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        if isinstance(raw, EventsFailed):
            return raw
        return mail_events.validate(
            raw, text=text, subject=msg.subject, owner_authored=msg.owner_authored, owner_said=said,
            replying=replying,
        )  # fmt: skip

    async def _owner_context(self, conn: AsyncConnection) -> OwnerContext:
        return await storylines.owner_context(conn, self._owner)

    async def _finish(
        self, message_id: str, status: str, category: str | None, error: str | None = None
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE email_messages SET intake_status = %s, intake_category = %s, intake_error = %s, "
                "intake_at = %s WHERE id = %s",
                (status, category, error, self._clock(), message_id),
            )

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

    # --- the one-time re-triage ----------------------------------------------------------------------------

    async def retriage(self, since: datetime, until: datetime | None = None) -> RetriageReport:
        """The open tasks the old intake made (no state yet, from an email, created in [since, until)) are
        read again through the events path, thread by thread, and what the storylines say becomes a
        PROPOSAL on each task: it is over, it is void, it waits for someone else, or it is no task under the
        new rules.
        NOTHING closes or changes: a task changes only when the owner says yes (`storylines.accept_proposal`).
        A message already read by the events path is not read again (one call per message, ever)."""
        report = RetriageReport()
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT t.id::text, m.thread_id FROM tasks t JOIN email_messages m "
                    "ON m.id = t.source_ref "
                    "WHERE t.status = 'open' AND t.state IS NULL AND t.source_kind = 'email' "
                    "AND t.created_at >= %s AND t.created_at < %s ORDER BY t.created_at, t.id",
                    (since, until or now + timedelta(days=1)),
                )
            ).fetchall()
        report.tasks = len(rows)
        threads = list(dict.fromkeys(thread for _, thread in rows))
        report.threads = len(threads)
        try:
            for thread in threads:
                report.messages_read += await self._read_thread(thread, now)
        except PisaError as err:
            if err.code not in _PAUSING:
                raise
            report.paused = err.message
        async with self._pool.connection() as conn, conn.transaction():
            owner = await self._owner_context(conn)
            for task_id, _ in rows:
                report.proposals += await self._propose_for(conn, task_id, now, owner)
        _log.info(
            "intake_retriaged", tasks=report.tasks, read=report.messages_read, proposals=report.proposals
        )
        return report

    async def _read_thread(self, thread_id: str, now: datetime) -> int:
        """The thread's messages the events path has not read yet, oldest first. Their events and storylines
        are written; no task is made, closed or changed."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"SELECT {_ROW} FROM email_messages m WHERE thread_id = %s AND NOT (labels && %s) "  # noqa: S608
                    "AND NOT EXISTS (SELECT 1 FROM assertion a WHERE a.subject_kind = 'message' "
                    "AND a.subject_id = m.id AND a.predicate = 'read') ORDER BY sent_at, id LIMIT %s",
                    (thread_id, list(NEVER_TO_MODEL_LABELS), THREAD_MAX),
                )
            ).fetchall()
        read = 0
        for row in rows:
            msg, text = _msg(row)
            if not text or await self._skip(msg):
                continue
            found = await self._read(msg, text)
            if isinstance(found, EventsFailed):
                continue
            async with self._pool.connection() as conn, conn.transaction():
                owner = await self._owner_context(conn)
                await storylines.ingest(conn, msg, found, now, owner, tasks=False)
            read += 1
        return read

    async def _skip(self, msg: Msg) -> bool:
        async with self._pool.connection() as conn:
            if await pisa_sent(conn, msg.id):
                return True
            senders, _ = await email_rules.rules(conn)
        return bool(msg.from_address and msg.from_address.strip().lower() in senders)

    async def _propose_for(
        self, conn: AsyncConnection, task_id: str, now: datetime, owner: OwnerContext
    ) -> int:
        """What the storyline of this task's email says about it, as a proposal (none when it still
        stands)."""
        row = await (
            await conn.execute(
                "SELECT t.source_ref, s.id::text FROM tasks t LEFT JOIN storyline_event se ON "
                "se.message_id = t.source_ref AND se.undone_at IS NULL LEFT JOIN storyline s ON "
                "s.id = se.storyline_id WHERE t.id = %s ORDER BY se.created_at LIMIT 1",
                (uuid.UUID(task_id),),
            )
        ).fetchone()
        if row is None:
            return 0
        source, storyline_id = row
        if storyline_id is None:
            reason = "not_a_task"  # the events path found nothing in it that is the owner's to do
            ref, quote = source, None
        else:
            got = await storylines.recompute(conn, storyline_id, now, owner, tasks=False)
            if got is None:
                return 0
            rep, plan, _ = got
            if any(a.status == "task" for a in plan.actions):
                return 0  # still the owner's move: nothing to propose
            if rep.state in ("cancelled", "refused"):
                reason = "void"
            elif plan.waiting_on != "none":
                reason = "waits"
            else:
                reason = "fulfilled"
            ref, quote = source, None
        exists = await (
            await conn.execute(
                "SELECT 1 FROM assertion WHERE subject_kind = 'task' AND subject_id = %s AND predicate = "
                "'proposed_change' AND valid_to IS NULL AND value->>'reason' = %s",
                (task_id, reason),
            )
        ).fetchone()
        if exists is not None:
            return 0
        await conn.execute(
            "INSERT INTO assertion (subject_kind, subject_id, predicate, value, valid_from, provenance, "
            "confidence, source, source_ref, quote) VALUES ('task', %s, 'proposed_change', %s, %s, "
            "'inferred', 0.6, 'mail', %s, %s)",
            (task_id, Jsonb({"reason": reason, "storyline": storyline_id, "message": ref}), now, ref, quote),
        )
        return 1


# --- the quiet list and one email's view ------------------------------------------------------------------

# the categories an email can be left in without a task, as `why_no_task` says them (the first six also
# come from before the storylines)
INBOX_CATEGORIES = (
    "notification", "info", "duplicate", "rule_sender", "rule_kind", "same_thread",
    "delivery", "event_or_booking", "action_required",
)  # fmt: skip


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
    who, when, the subject, Pisa's one line on what it is (the first event's own sentence), why it is no task
    or the task it became. Never its body; never spam or promotions."""
    row = await (
        await conn.execute(
            "SELECT m.id, m.subject, m.from_name, m.from_address, m.sent_at, m.intake_category, "
            "m.intake_notice, m.intake_reason, coalesce(m.intake_gist->>'what', (SELECT e.quote FROM "
            "mail_event e WHERE e.message_id = m.id ORDER BY e.created_at LIMIT 1)), "
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
        # Pisa's one line on it is the event's own sentence, never asked for their language: checked on read
        "sent_at": sent_at, "gist": gist_said(gist, category, notice, lang), "task_id": task_id,
        "kind": email_rules.email_kind(category, notice) if quiet else None,
        "why": why_no_task(category, notice, lang) if quiet else None,
        "detail": said_reason(reason, category, notice, lang),
    }  # fmt: skip


async def _owner_said(conn: AsyncConnection, msg: Msg) -> list[str]:
    """The sentences the owner wrote earlier in this message's thread (his new text): whatever of them comes
    back in a later message is never a request to him."""
    rows = await (
        await conn.execute(
            "SELECT new_text FROM email_messages WHERE thread_id = %s AND owner_authored AND id <> %s "
            "AND sent_at <= %s AND new_text IS NOT NULL ORDER BY sent_at DESC LIMIT %s",
            (msg.thread_id, msg.id, msg.sent_at, OWNER_SAID_MAX),
        )
    ).fetchall()
    return [s for (text,) in rows for s in mail_events.sentences(text)]


async def _others_wrote(conn: AsyncConnection, msg: Msg) -> bool:
    """Someone other than the owner wrote earlier in this message's thread: the owner's message is a reply."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM email_messages WHERE thread_id = %s AND NOT owner_authored AND id <> %s "
            "AND sent_at <= %s LIMIT 1",
            (msg.thread_id, msg.id, msg.sent_at),
        )
    ).fetchone()
    return row is not None


async def pisa_sent(conn: AsyncConnection, message_id: str) -> bool:
    """The mail is one Pisa sent (`pisa_sent_mail`, the id Gmail's send returned)."""
    row = await (
        await conn.execute("SELECT 1 FROM pisa_sent_mail WHERE gmail_message_id = %s", (message_id,))
    ).fetchone()
    return row is not None
