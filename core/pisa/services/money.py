"""Bills and receipts from the user's email: `scan` reads the new emails' sender,
subject and snippet and, a bill email's PDF (read locally with pypdf, gmail.readonly: no
new scope; the PDF is outside text, its facts copied as found) and keeps what is about money in `money_seen`;
a bill to pay becomes a task in the pool, unless their own email-task rules say no
(a sender or a kind they ruled out, or their general "no tasks from email"). Subscriptions are read from
the receipts (read-only: Pisa never contacts a company).

Logs carry counts, never a sender, a subject or an amount.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.connectors.pdf_text import pdf_text
from pisa.domain import bill_pdf
from pisa.domain import ledger as ledger_rules
from pisa.domain import money as rules
from pisa.domain.decisions import Decision
from pisa.domain.mail import NEVER_TO_MODEL_LABELS
from pisa.domain.owner import Owner
from pisa.domain.profile import no_email_tasks
from pisa.domain.tasks import end_of_day
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services import email_rules
from pisa.services.tasks import insert_task

_log = get_logger("pisa.money")
BILLS_SCAN_MAX = 300  # the emails one bills-and-receipts scan reads at most
BILLS_LISTED = 50
PdfFetch = Callable[[str, str], Awaitable[bytes | None]]  # (email id, part id) → the PDF's bytes


async def tasks_ruled_out(conn: AsyncConnection, sender: str) -> bool:
    """RAIL: their own rules: never from this sender, never this kind, or no tasks from email at all."""
    senders, kinds = await email_rules.rules(conn)
    if sender.lower() in {s.lower() for s in senders} or rules.BILL_KIND in kinds:
        return True
    claims = await (
        await conn.execute(
            "SELECT statement FROM profile_claims WHERE state <> 'retired' AND kind <> 'source'"
        )
    ).fetchall()
    return any(no_email_tasks(str(s)) for (s,) in claims)


async def _known(conn: AsyncConnection, address: str) -> bool:
    """Someone the user writes to (their correspondents)."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM email_correspondents WHERE lower(address) = lower(%s)", (address or "",)
        )
    ).fetchone()
    return row is not None


class MoneyService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        pdfs: PdfFetch | None = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self.pdfs = pdfs  # Gmail's attachment, when Google is connected

    async def _pdf(
        self, email_id: str, address: str, subject: str, snippet: str, sent: date
    ) -> tuple[bill_pdf.PdfBill | None, str | None]:
        """The bill in this email's PDF and the part it was read from (read once: its outcome is
        kept), or (None, None). RAIL: a candidate only (a biller's domain or a bill's words, never marketing);
        the size and page caps before a parse. Whatever the PDF does, its outcome is written (in
        `finally`), so no email is parsed twice; only Gmail failing to give it leaves it for the next scan."""
        if self.pdfs is None or not bill_pdf.candidate(address, subject, snippet):
            return None, None
        async with self._pool.connection() as conn:
            done = await (
                await conn.execute("SELECT 1 FROM bill_pdf_reads WHERE email_id = %s", (email_id,))
            ).fetchone()
            parts = await (
                await conn.execute(
                    "SELECT part_id, size FROM email_attachments WHERE email_id = %s ORDER BY part_id "
                    "LIMIT %s",
                    (email_id, bill_pdf.MAX_PDFS),
                )
            ).fetchall()
        if done is not None or not parts:
            return None, None
        outcome, found, read_part, keep = "unreadable", None, None, True
        try:
            outcome = "no_bill"
            for part_id, size in parts:
                if size > bill_pdf.MAX_BYTES:
                    outcome = "too_big"
                    continue
                try:
                    data = await self.pdfs(email_id, part_id)
                except Exception:  # the connector's failure: tried again on the next scan
                    _log.warning("bill_pdf_fetch_failed")
                    keep = False
                    return None, None
                if data is None:
                    outcome = "too_big"
                    continue
                outcome = "unreadable"
                read, text = await pdf_text(data)
                if read != "read":
                    outcome = read
                    continue
                found = bill_pdf.read(text, sent)
                outcome = "no_bill"
                if found is not None:
                    outcome, read_part = "read", str(part_id)
                    break
        except Exception:
            _log.warning("bill_pdf_unreadable")
            outcome, found, read_part = "unreadable", None, None
        finally:
            if keep:
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO bill_pdf_reads (email_id, outcome, read_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT (email_id) DO NOTHING",
                        (email_id, outcome, self._clock()),
                    )
                _log.info("bill_pdf_read", outcome=outcome)
        return found, read_part

    async def scan(self) -> int:
        """The new emails since the last scan: bills and receipts kept, a task for a bill to pay."""
        now = self._clock()
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT e.id, coalesce(e.from_name, ''), coalesce(e.from_address, ''), e.subject, "
                    "e.snippet, "
                    "e.sent_at FROM email_messages e WHERE e.sent_at >= %s AND NOT (e.labels && %s) "
                    # a mail Pisa sent (the accountant package) is never a bill or receipt
                    "AND NOT EXISTS (SELECT 1 FROM pisa_sent_mail j WHERE j.gmail_message_id = e.id) "
                    "AND NOT EXISTS (SELECT 1 FROM money_seen m WHERE m.email_id = e.id) "
                    "ORDER BY e.sent_at LIMIT %s",
                    (now - timedelta(days=rules.SCAN_DAYS), list(NEVER_TO_MODEL_LABELS), BILLS_SCAN_MAX),
                )
            ).fetchall()
            lang = await self._owner.lang(conn)
        kept = tasks = 0
        for email_id, name, address, subject, snippet, sent_at in rows:
            sent = sent_at.astimezone(tz).date()
            seen = rules.read(name, address, subject, snippet, sent)
            pdf, part = None, None
            if seen is None or seen.kind == "bill":  # a bill's PDF (a receipt's is never opened)
                pdf, part = await self._pdf(email_id, address, subject, snippet, sent)
                seen = bill_pdf.merge(seen, pdf, biller=rules.biller(address))
            if seen is None:
                continue
            kept += 1
            tasks += await self._keep(email_id, name, address, subject, sent_at, seen, lang, pdf, part)
        if kept:
            _log.info("money_seen", kept=kept, tasks=tasks)
        return kept

    async def _keep(
        self,
        email_id: str,
        name: str,
        address: str,
        subject: str,
        sent_at: datetime,
        seen: rules.Seen,
        lang: str,
        pdf: bill_pdf.PdfBill | None = None,
        pdf_part: str | None = None,
    ) -> int:
        now = self._clock()
        sender = rules.sender_key(address, name)
        who = (name or address)[:120] or "?"
        row_id = uuid.uuid4()
        payee = bill_pdf.clean_name(pdf.payee) if pdf is not None else None
        async with self._pool.connection() as conn, conn.transaction():
            # the accountant forwards only a PDF that read as this bill; a receipt's, only from a
            # biller or someone they write to (never a stranger's file under a name Pisa gives it)
            part = (pdf_part,) if pdf is not None and pdf_part else None
            if seen.kind == "receipt" and (rules.biller(address) or await _known(conn, address)):
                part = await (
                    await conn.execute(
                        "SELECT part_id FROM email_attachments WHERE email_id = %s ORDER BY part_id LIMIT 1",
                        (email_id,),
                    )
                ).fetchone()
            made = await conn.execute(
                "INSERT INTO money_seen (id, email_id, kind, sender_key, from_name, subject, amount, "
                "currency, "
                "due_on, paid, sent_at, seen_at, payee, iban_last4, source, pdf_part_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (email_id) DO NOTHING",
                (
                    row_id,
                    email_id,
                    seen.kind,
                    sender,
                    who,
                    subject[:200],
                    seen.amount,
                    seen.currency,
                    seen.due_on,
                    seen.paid,
                    sent_at,
                    now,
                    payee,
                    pdf.iban_last4 if pdf is not None else None,
                    "pdf" if pdf is not None else "email",
                    part[0] if part else None,
                ),
            )
            if not made.rowcount or seen.kind != "bill" or seen.paid:
                return 0
            if seen.due_on is not None and seen.due_on < self._owner.day(self._clock()):
                return 0  # already past: no task out of an old bill
            # the task intake already made from this email or its thread, or an open bill task
            # of the same sender with the same amount or due date (a reminder of the same bill): linked, never
            # two
            existing = await (
                await conn.execute(
                    "SELECT t.id FROM tasks t WHERE t.status = 'open' AND t.deleted_at IS NULL AND ("
                    "(t.source_kind = 'email' AND t.source_ref IN (SELECT e.id FROM email_messages e WHERE "
                    "e.thread_id = (SELECT thread_id FROM email_messages WHERE id = %s))) OR (t.source_kind "
                    "= 'email' "
                    "AND t.source_ref = %s) OR t.id IN (SELECT m.task_id FROM money_seen m WHERE "
                    "m.sender_key = %s "
                    "AND m.id <> %s AND (m.amount = %s OR m.due_on = %s))) LIMIT 1",
                    (email_id, email_id, sender, row_id, seen.amount, seen.due_on),
                )
            ).fetchone()
            if existing is not None:
                await conn.execute("UPDATE money_seen SET task_id = %s WHERE id = %s", (existing[0], row_id))
                return 0
            if await tasks_ruled_out(conn, address or name):
                return 0
            today = now.astimezone(self._owner.zone).date()
            money = ledger_rules.money_text(seen.amount, seen.currency, lang)
            named = bill_pdf.short_payee(payee) if payee else who
            task = await insert_task(
                conn,
                title=rules.bill_task_title(named, seen.due_on, lang, money=money, today=today),
                now=now,
                source_kind="email",
                due_at=end_of_day(seen.due_on, self._owner.tz) if seen.due_on else None,
                due_kind="date" if seen.due_on else "none",
                priority=2,
                source_ref=email_id,
                source_label=who[:60],
                deadline_source="email" if seen.due_on else None,
            )
            if task is None:
                return 0
            await conn.execute("UPDATE money_seen SET task_id = %s WHERE id = %s", (task.id, row_id))
            why = _bill_why(seen.amount, seen.currency, lang)
            await decision_log.store(
                conn,
                [Decision("act", "bill_task", "pisa_initiative", ref=task.id, purpose=why, outcome="done")],
                at=now,
                trace_id=None,
                device_id=None,
                outside_kinds=["email"],  # a task made from an email
            )
        return 1

    async def bills(self, *, unpaid: bool = False) -> list[dict[str, Any]]:
        today = self._owner.day(self._clock())
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT m.id, m.from_name, m.subject, m.amount, m.currency, m.due_on, m.paid OR "
                    "coalesce(t.status = 'done', false), m.email_id, m.task_id, m.payee, m.iban_last4, "
                    "m.source "
                    "FROM money_seen m "
                    "LEFT JOIN tasks t ON t.id = m.task_id WHERE m.kind = 'bill' ORDER BY m.sent_at DESC "
                    "LIMIT %s",
                    (BILLS_LISTED,),
                )
            ).fetchall()
        out = [
            {
                "id": str(i),
                "from": who,
                "subject": subject,
                "amount": f"{amount:.2f}" if amount is not None else None,
                "currency": currency,
                "due_on": due.isoformat() if due else None,
                "paid": bool(paid),
                "source_email_id": email_id,
                "task_id": str(task) if task else None,
                "payee": payee,
                "iban_last4": iban,
                "source": source,
            }
            for i, who, subject, amount, currency, due, paid, email_id, task, payee, iban, source in rows
        ]
        if unpaid:
            out = [
                b
                for b in out
                if not b["paid"]
                and (b["due_on"] is None or b["due_on"] >= (today - timedelta(days=30)).isoformat())
            ]
        return out

    async def subscriptions(self, lang: str) -> list[dict[str, Any]]:
        since = self._clock() - timedelta(days=rules.RECEIPTS_DAYS)
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT sender_key, from_name, amount, currency, sent_at FROM money_seen "
                    "WHERE kind = 'receipt' AND amount IS NOT NULL AND currency IS NOT NULL AND sent_at >= "
                    "%s",
                    (since,),
                )
            ).fetchall()
        charges: list[tuple[str, str, Decimal, str, date]] = [
            (k, n, a, c, s.astimezone(tz).date()) for k, n, a, c, s in rows
        ]
        return [
            {
                "name": s.name,
                "amount": f"{s.amount:.2f}",
                "currency": s.currency,
                "period": s.period,
                "last_on": s.last_on.isoformat(),
                "count": s.count,
                "text": rules.ask_cancel(
                    s.name, ledger_rules.money_text(s.amount, s.currency, lang), s.period, lang
                ),
            }
            for s in rules.recurring(charges)
        ]


def _bill_why(amount: Decimal | None, currency: str | None, lang: str) -> str:
    money = ledger_rules.money_text(amount, currency, lang)
    if lang == "it-IT":
        return f"Una bolletta nella tua email: {money}." if money else "Una bolletta nella tua email."
    return f"A bill in your email: {money}." if money else "A bill in your email."
