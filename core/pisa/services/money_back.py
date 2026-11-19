"""Money that comes back, promises, things that run out.

- `scan` (after each Gmail sync, with the trips): each new email once (`money_reads`): an airline's notice of
  a disrupted flight of their trips → a flight claim; a refund promised or credited; an Italian utility's
  conguaglio over 2 years old; an order or delivery (what they bought: warranties, supplies); and in their OWN
  sent emails, a promise ("ti mando il contratto domani") → a task.
- `tick` (the minute's job, hourly inside): a claim weighed again (a delay announced becomes owed once the
  flight landed), a refund overdue, a supply about to run out → one task each (once).
- Listings and the user's words for the routes and the brain's tools.

RAILS: everything from email is data; amounts as stated (or EU261's fixed sums), never invented; Pisa
never contacts a company: it drafts and the user sends; their own email-task rules and a stop
hold Pisa's tasks; the user's statuses are never changed by Pisa. Logs carry ids and counts, never a sender, a
subject, an item or an amount.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg.errors import ForeignKeyViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from pisa.domain import flight_claims as fc
from pisa.domain import handoffs as handoff_rules
from pisa.domain import intentions as intention_rules
from pisa.domain import ledger as ledger_rules
from pisa.domain import money as money_rules
from pisa.domain import money_back as rules
from pisa.domain import money_links as links
from pisa.domain import trips as trip_rules
from pisa.domain.decisions import Decision
from pisa.domain.errors import PisaError
from pisa.domain.mail import NEVER_TO_MODEL_LABELS
from pisa.domain.owner import Owner
from pisa.domain.profile import no_email_tasks
from pisa.domain.tasks import end_of_day
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services import decisions as decision_log
from pisa.services import email_rules
from pisa.services.tasks import insert_task

if TYPE_CHECKING:
    from pisa.services.handoffs import HandoffService
    from pisa.services.intentions import IntentionsService

_log = get_logger("pisa.money_back")
CLAIMS_SCAN_MAX = 400  # the emails one money-back scan reads at most
TICK_EVERY = timedelta(hours=1)
LISTED = 50
SUPPLY_LEAD = timedelta(days=1)  # the task the day before the expected run-out
PROMISE_FRESH = timedelta(days=14)  # an older sent email's promise is no task (they likely kept it)
# a claim is kept until they delete it; the hourly re-weighing reads the flights of the last 3 years
# (the term the record had): an older flight's verdict no longer moves (past CHECK_FIRST: "check first")
REASSESS_FOR = timedelta(days=3 * 365)
_SEVERITY = {"delay": 0, "cancellation": 1, "denied_boarding": 1}


def _not_found() -> PisaError:
    return PisaError("not_found", "Nothing by that id.", "Refresh and try again")


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Fix the request and try again")


def _money(value: Decimal | None) -> str | None:
    return f"{value:.2f}" if value is not None else None


class MoneyBackService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        handoffs: "HandoffService | None" = None,
        intentions: "IntentionsService | None" = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._handoffs = handoffs
        self._ticked: datetime | None = None
        self.intentions = intentions

    def _local(self, moment: datetime) -> date:
        return moment.astimezone(self._owner.zone).date()

    # --- rails ----------------------------------------------------------------------------------------

    async def _ruled_out(self, conn: AsyncConnection, sender: str | None, *, bill: bool = False) -> bool:
        """RAIL: their rules: never from this sender, never bills (for a bill), no tasks from email."""
        senders, kinds = await email_rules.rules(conn)
        if sender and sender.lower() in {s.lower() for s in senders}:
            return True
        if bill and money_rules.BILL_KIND in kinds:
            return True
        claims = await (
            await conn.execute(
                "SELECT statement FROM profile_claims WHERE state <> 'retired' AND kind <> 'source'"
            )
        ).fetchall()
        return any(no_email_tasks(str(s)) for (s,) in claims)

    async def _task(
        self,
        conn: AsyncConnection,
        *,
        title: str,
        why: str,
        source_kind: str = "email",
        source_ref: str | None,
        source_label: str | None,
        notes: str | None = None,
        due_on: date | None = None,
        priority: int = 2,
    ) -> uuid.UUID | None:
        task = await insert_task(
            conn,
            title=title[:120],
            now=self._clock(),
            source_kind=source_kind,  # type: ignore[arg-type]
            notes=notes,
            due_at=end_of_day(due_on, self._owner.tz) if due_on else None,
            due_kind="date" if due_on else "none",
            priority=priority,
            source_ref=source_ref,
            source_label=(source_label or "")[:60] or None,
            deadline_source=("email" if source_kind == "email" else "owner") if due_on else None,
        )
        if task is None:
            return None
        await decision_log.store(
            conn,
            [Decision("act", "money_task", "pisa_initiative", ref=task.id, purpose=why, outcome="done")],
            at=self._clock(),
            trace_id=None,
            device_id=None,
            outside_kinds=["email"],  # a task made from an email
        )
        return uuid.UUID(str(task.id))

    async def _owner_address(self, conn: AsyncConnection) -> str:
        row = await (
            await conn.execute("SELECT account FROM connector_accounts WHERE connector = 'google' LIMIT 1")
        ).fetchone()
        return str(row[0] or "").strip().lower() if row else ""

    # --- the scan --------------------------------------------------------------------------------------

    async def scan(self) -> int:
        """The emails since the last scan, each once. Returns how many gave something."""
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await (conn.cursor(row_factory=dict_row)).execute(
                    "SELECT e.id, coalesce(e.from_name, '') AS from_name, coalesce(e.from_address, '') AS "
                    "from_address, e.to_addresses, e.subject, e.snippet, coalesce(e.body, e.snippet) AS "
                    "body, "
                    "e.labels, e.sent_at, e.intake_status, e.intake_category, EXISTS (SELECT 1 FROM "
                    "pisa_sent_mail j WHERE j.gmail_message_id = e.id) AS pisa_sent, (SELECT r.waiting FROM "
                    "money_reads r WHERE r.email_id = e.id) AS waiting FROM email_messages e "
                    "WHERE e.sent_at >= %s AND NOT EXISTS (SELECT 1 FROM money_reads r WHERE r.email_id = "
                    "e.id AND r.waiting IS NULL) "
                    "ORDER BY e.sent_at, e.id LIMIT %s",
                    (now - timedelta(days=money_rules.SCAN_DAYS), CLAIMS_SCAN_MAX),
                )
            ).fetchall()
            settings = await self._owner.settings(conn)
            owner = await self._owner_address(conn)
            blocked, _ = await email_rules.rules(conn)
        lang = str(settings["language"])
        found = 0
        later: list[tuple[dict[str, Any], set[str]]] = []
        for row in rows:
            if "SENT" not in (row["labels"] or ()) and row["intake_status"] == "pending":
                continue  # an email is read once the intake judged it (read next time)
            wait: set[str] = set()
            found += await self._read(row, lang, owner, blocked, wait)
            if wait:
                later.append((row, wait))
            else:
                await self._mark_read(row["id"], None, now)
        # one booking's or one shop's emails often share a second; what waited for another
        # email of this scan (a credit for its promise, a notice for its leg) is read once more after the rest
        for row, wait in later:
            again: set[str] = set()
            found += await self._read(row | {"waiting": "|".join(sorted(wait))}, lang, owner, blocked, again)
            await self._mark_read(row["id"], "|".join(sorted(again)) or None, now)
        if found:
            _log.info("money_back_scanned", emails=len(rows), found=found)
        await self.tick(force=True)
        return found

    async def _read(
        self, row: dict[str, Any], lang: str, owner: str | None, blocked: Any, wait: set[str]
    ) -> int:
        """One email through money's readers. An email that waited (`row["waiting"]`) goes through that reader
        only; a reader that can't place it yet (`wait`) has it read again later."""
        labels = set(row["labels"] or ())
        sender = str(row["from_address"]).lower()
        sent = "SENT" in labels
        travel = trip_rules.travel_sender(sender)
        waiting = set(str(row.get("waiting") or "").split("|")) - {""}
        found = 0
        try:
            if waiting:
                if "flight" in waiting:
                    found += await self._flight_notice(row, lang, wait)
                if "refund" in waiting:
                    found += await self._refund(row, lang, wait)
            elif sent:
                # RAIL: their OWN sent words only; never a mail Pisa sent (Pisa's words)
                if owner and sender == owner and not row["pisa_sent"]:
                    found += await self._promises(row, lang)
            elif (
                not labels & NEVER_TO_MODEL_LABELS
                and sender not in {b.lower() for b in blocked}
                # every reader, only what the intake judged and found no promo or phishing
                and row["intake_status"] == "done"
                and row["intake_category"] != "promo_or_spam"
            ):
                if travel:
                    found += await self._flight_notice(row, lang, wait)
                found += await self._refund(row, lang, wait)
                if self._owner.in_country("IT"):  # the Italian bill watchdog: Italy's utilities and law only
                    found += await self._conguaglio(row, lang)
                found += await self._order(row, self._owner.tz)
                found += await self._owed(row)  # what someone owes them (waiting-for)
        except Exception:
            _log.exception("money_back_email_failed", email_id=row["id"])
        return found

    async def _mark_read(self, email_id: str, waiting: str | None, now: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO money_reads (email_id, read_at, waiting) VALUES (%s, %s, %s) "
                "ON CONFLICT (email_id) DO UPDATE SET read_at = EXCLUDED.read_at, waiting = EXCLUDED.waiting",
                (email_id, now, waiting),
            )

    # flights ------------------------------------------------------------------------------------------

    async def _flight_legs(self, conn: AsyncConnection) -> list[tuple[uuid.UUID, trip_rules.Leg]]:
        rows = await (
            await conn.execute(
                "SELECT id, legs FROM trips WHERE doubt IS NULL AND depart_at > %s",
                (self._clock() - fc.CHECK_FIRST,),
            )
        ).fetchall()
        return [
            (trip_id, leg)
            for trip_id, legs in rows
            for leg in (trip_rules.from_stored(x) for x in legs or [])
            if leg.kind == "flight" and leg.ref
        ]

    async def _flight_notice(self, row: dict[str, Any], lang: str, wait: set[str]) -> int:
        notice = fc.read_notice(row["subject"] or "", row["body"] or "")
        if notice is None:
            return 0
        sent_at: datetime = row["sent_at"]
        try:
            return await self._flight_claims(row, notice, sent_at, wait)
        except ForeignKeyViolation:  # its trip went meanwhile: the email waits, never lost
            _log.info("money_flight_notice_waits", email_id=row["id"])
            wait.add("flight")
            return 0

    async def _flight_claims(
        self, row: dict[str, Any], notice: fc.Notice, sent_at: datetime, wait: set[str]
    ) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            # never while the trip reader merges trips (it repoints `flight_claims.trip_id`)
            await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (locks.TRIPS,))
            legs = await self._flight_legs(conn)
            made = 0
            for flight in notice.flights:
                near = [
                    (trip_id, leg)
                    for trip_id, leg in legs
                    if (leg.ref or "").replace(" ", "").upper() == flight
                    and sent_at - timedelta(days=60) <= leg.at <= sent_at + timedelta(days=330)
                ]
                if not near:  # its booking's leg isn't read yet (or doubted): it waits
                    wait.add("flight")
                    continue
                trip_id, leg = min(near, key=lambda p: abs((p[1].at - sent_at).total_seconds()))
                delay = notice.delay_min
                if delay is None and notice.new_arrival and leg.until is not None:
                    delay = fc.delay_from_arrival(leg.until, notice.new_arrival)
                notice_days = max(0, (leg.at - sent_at).days) if notice.disruption == "cancellation" else None
                await self._upsert_claim(
                    conn,
                    leg=leg,
                    trip_id=trip_id,
                    disruption=notice.disruption,
                    delay_min=delay,
                    delay_source="email" if delay is not None else None,
                    notice_days=notice_days,
                    source="email",
                    email_id=row["id"],
                    sent_at=sent_at,
                )
                made += 1
        return made

    async def _upsert_claim(
        self,
        conn: AsyncConnection,
        *,
        leg: trip_rules.Leg,
        trip_id: uuid.UUID | None,
        disruption: fc.Disruption,
        delay_min: int | None,
        delay_source: str | None,
        notice_days: int | None,
        source: str,
        email_id: str | None,
        sent_at: datetime | None = None,
    ) -> uuid.UUID:
        """One claim per flight and day: a later notice adds to it (a cancellation outranks a delay, the
        latest stated delay replaces the one before, their own word on the delay wins over an email's); their
        status is never changed. "latest" is the notice's sent time (then its id), never the
        order Pisa read them in; a cancellation counts from its FIRST notice (the most days' notice)."""
        delay_at = sent_at if delay_min is not None and delay_source == "email" else None
        now = self._clock()
        flight = (leg.ref or "").replace(" ", "").upper()
        codes = re.findall(r"\b([A-Z]{3})\b", leg.place or "")
        origin, destination = (codes[0], codes[1]) if len(codes) >= 2 else (None, None)
        day = leg.at.date()
        old = await (
            await (conn.cursor(row_factory=dict_row)).execute(
                "SELECT * FROM flight_claims WHERE flight = %s AND day = %s FOR UPDATE", (flight, day)
            )
        ).fetchone()
        if old is not None:
            if _SEVERITY[old["disruption"]] > _SEVERITY[disruption]:
                disruption, notice_days = old["disruption"], old["notice_days"]
            elif disruption == old["disruption"] and None not in (old["notice_days"], notice_days):
                notice_days = max(notice_days, old["notice_days"])  # the first notice they had
            # their word wins; otherwise the latest notice stating a delay (never the largest)
            older = (
                delay_source == "email"
                and old["delay_source"] == "email"
                and old["delay_at"] is not None
                and delay_at is not None
                and (delay_at, email_id or "") < (old["delay_at"], old["delay_email_id"] or "")
            )
            if (old["delay_source"] == "said" and delay_source != "said") or delay_min is None or older:
                delay_min, delay_source = old["delay_min"], old["delay_source"]
                delay_at, delay_email = old["delay_at"], old["delay_email_id"]
            else:
                delay_email = email_id if delay_at is not None else None
            if notice_days is None:
                notice_days = old["notice_days"]
        else:
            delay_email = email_id if delay_at is not None else None
        verdict = fc.assess(
            flight=flight,
            origin=origin,
            destination=destination,
            disruption=disruption,
            delay_min=delay_min,
            notice_days=notice_days,
            scheduled_arr=leg.until,
            scheduled_at=leg.at,
            now=now,
        )
        emails = [email_id] if email_id else []
        if old is None:
            claim_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO flight_claims (id, flight, day, origin, destination, scheduled_at, "
                "scheduled_arr, "
                "tz_from, tz_to, booking_last4, disruption, delay_min, delay_source, notice_days, rule, "
                "status, "
                "reason, distance_km, amount, currency, trip_id, source, source_email_ids, created_at, "
                "updated_at, delay_at, delay_email_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    claim_id, flight, day, origin, destination, leg.at, leg.until, leg.tz,
                    leg.destination.tz if leg.destination else None, leg.confirmation_last4, disruption,
                    delay_min, delay_source, notice_days, verdict.rule, verdict.status, verdict.reason,
                    verdict.distance_km, verdict.amount, "EUR" if verdict.amount is not None else None,
                    trip_id, source, emails, now, now, delay_at, delay_email,
                ),
            )  # fmt: skip
            _log.info("flight_claim_found", claim_id=str(claim_id), status=verdict.status)
            return claim_id
        claim_id = old["id"]
        mine = old["status"] in fc.OWNER_STATUSES
        await conn.execute(
            "UPDATE flight_claims SET disruption = %s, delay_min = %s, delay_source = %s, notice_days = %s, "
            "delay_at = %s, delay_email_id = %s, "
            "rule = %s, status = %s, reason = %s, distance_km = %s, amount = %s, currency = %s, "
            "source_email_ids = (SELECT array_agg(DISTINCT x) FROM unnest(source_email_ids || %s::text[]) "
            "x), "
            "updated_at = %s WHERE id = %s",
            (
                disruption, delay_min, delay_source, notice_days, delay_at, delay_email, verdict.rule,
                old["status"] if mine else verdict.status, old["reason"] if mine else verdict.reason,
                verdict.distance_km, verdict.amount, "EUR" if verdict.amount is not None else None, emails,
                now, claim_id,
            ),
        )  # fmt: skip
        return claim_id

    async def _claim_tasks(self, conn: AsyncConnection, lang: str) -> int:
        """An eligible claim without a task gets one (once: `task_id` stays even when they close it)."""
        rows = await (
            await (conn.cursor(row_factory=dict_row)).execute(
                "SELECT c.*, (SELECT from_address FROM email_messages e WHERE e.id = ANY(c.source_email_ids) "
                "LIMIT 1) AS sender FROM flight_claims c WHERE c.status = 'eligible' AND c.tasked_at IS NULL "
                "FOR UPDATE OF c"
            )
        ).fetchall()
        made = 0
        for c in rows:
            await conn.execute(
                "UPDATE flight_claims SET tasked_at = %s WHERE id = %s", (self._clock(), c["id"])
            )
            if c["source"] == "email" and await self._ruled_out(conn, c["sender"]):
                continue
            title = fc.task_title(c["flight"], c["rule"], c["amount"], lang)
            why = (
                f"Un volo disturbato ti può rendere dei soldi: {fc.why(c['reason'], lang)}"
                if lang.startswith("it")
                else f"A disrupted flight may pay you back: {fc.why(c['reason'], lang)}"
            )
            task_id = await self._task(
                conn,
                title=title,
                why=why,
                source_kind="email" if c["source"] == "email" else "voice",
                source_ref=(c["source_email_ids"] or [None])[0],
                source_label=links.AIRLINES[fc.carrier(c["flight"])].name
                if fc.carrier(c["flight"]) in links.AIRLINES
                else c["flight"],
                due_on=fc.deadline(c["scheduled_at"]) if c["rule"] == "eu261" else None,
            )
            if task_id is not None:
                await conn.execute("UPDATE flight_claims SET task_id = %s WHERE id = %s", (task_id, c["id"]))
                made += 1
        return made

    async def _reassess(self, conn: AsyncConnection) -> None:
        """A claim Pisa holds (not their status) weighed again now: a delay announced is owed once it
        landed."""
        rows = await (
            await (conn.cursor(row_factory=dict_row)).execute(
                "SELECT * FROM flight_claims WHERE status IN ('possible', 'eligible', 'not_eligible') "
                "AND day >= %s",
                (self._owner.day(self._clock()) - REASSESS_FOR,),
            )
        ).fetchall()
        for c in rows:
            v = fc.assess(
                flight=c["flight"],
                origin=c["origin"],
                destination=c["destination"],
                disruption=c["disruption"],
                delay_min=c["delay_min"],
                notice_days=c["notice_days"],
                scheduled_arr=c["scheduled_arr"],
                scheduled_at=c["scheduled_at"],
                now=self._clock(),
            )
            if (v.status, v.reason) != (c["status"], c["reason"]):
                await conn.execute(
                    "UPDATE flight_claims SET status = %s, reason = %s, amount = %s, currency = %s, "
                    "updated_at = %s WHERE id = %s AND status NOT IN ('sent', 'paid', 'dismissed')",
                    (
                        v.status, v.reason, v.amount, "EUR" if v.amount is not None else None, self._clock(),
                        c["id"],
                    ),
                )  # fmt: skip

    # refunds ------------------------------------------------------------------------------------------

    async def _refund(self, row: dict[str, Any], lang: str, wait: set[str]) -> int:
        sent_on = self._local(row["sent_at"])
        notice = rules.read_refund(row["subject"] or "", row["body"] or "", sent_on)
        if notice is None:
            return 0
        sender = money_rules.sender_key(row["from_address"], row["from_name"])
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            if notice.kind == "credited":
                waiting = await (
                    await conn.execute(
                        "SELECT id, amount, task_id FROM refunds_promised WHERE sender_key = %s AND status "
                        "IN "
                        "('waiting', 'overdue') AND promised_on <= %s ORDER BY promised_on, source_email_id",
                        (sender, sent_on),
                    )
                ).fetchall()
                for refund_id, amount, task_id in waiting:
                    if rules.credit_matches(amount, notice.amount):
                        await conn.execute(
                            "UPDATE refunds_promised SET status = 'received', received_email_id = %s, "
                            "updated_at = %s, received_at = coalesce((SELECT sent_at FROM email_messages "
                            "WHERE id = %s), %s) WHERE id = %s",
                            (row["id"], now, row["id"], now, refund_id),
                        )
                        if task_id is not None:  # the reminder isn't needed any more
                            await conn.execute(
                                "UPDATE tasks SET status = 'dismissed', updated_at = %s WHERE id = %s "
                                "AND status = 'open'",
                                (now, task_id),
                            )
                        _log.info("refund_received", refund_id=str(refund_id))
                        return 1
                # its promise isn't read yet (the same second, or fetched late): it waits
                wait.add("refund")
                return 0
            made = await conn.execute(
                "INSERT INTO refunds_promised (id, sender_key, merchant, amount, currency, promised_on, "
                "expected_by, status, source_email_id, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, "
                "%s, "
                "%s, 'waiting', %s, %s, %s) ON CONFLICT (source_email_id) DO NOTHING",
                (
                    uuid.uuid4(), sender, (row["from_name"] or row["from_address"] or "?")[:120],
                    notice.amount, notice.currency, sent_on,
                    rules.refund_expected(sent_on, notice.expected_by), row["id"], now, now,
                ),
            )  # fmt: skip
        return made.rowcount

    async def _refund_tasks(self, conn: AsyncConnection, lang: str) -> int:
        today = self._owner.day(self._clock())
        rows = await (
            await conn.execute(
                "SELECT r.id, r.merchant, r.amount, r.currency, r.promised_on, r.expected_by, "
                "r.source_email_id, "
                "r.sender_key FROM refunds_promised r WHERE r.status = 'waiting' AND r.expected_by < %s",
                (today - timedelta(days=rules.REFUND_GRACE_DAYS),),
            )
        ).fetchall()
        made = 0
        for refund_id, merchant, amount, currency, promised_on, expected_by, email_id, sender in rows:
            await conn.execute(
                "UPDATE refunds_promised SET status = 'overdue', updated_at = %s WHERE id = %s",
                (self._clock(), refund_id),
            )
            if await self._ruled_out(conn, sender):
                continue
            money = ledger_rules.money_text(amount, currency, lang)
            task_id = await self._task(
                conn,
                title=rules.refund_task_title(merchant, money, lang),
                why=(
                    f"Un rimborso promesso non è arrivato entro il {expected_by:%d/%m}."
                    if lang.startswith("it")
                    else f"A refund promised by {expected_by:%b %d} hasn't arrived."
                ),
                source_ref=email_id,
                source_label=merchant,
                notes=rules.refund_reminder(merchant, money, promised_on, expected_by, lang),
                priority=2,
            )
            if task_id is not None:
                await conn.execute(
                    "UPDATE refunds_promised SET task_id = %s WHERE id = %s", (task_id, refund_id)
                )
                made += 1
        return made

    # the Italian bill watchdog ---------------------------------------------------------------------------

    async def _conguaglio(self, row: dict[str, Any], lang: str) -> int:
        issued = self._local(row["sent_at"])
        flag = rules.read_conguaglio(row["from_address"], row["subject"] or "", row["body"] or "", issued)
        if flag is None:
            return 0
        supplier = (row["from_name"] or links.domain_of(row["from_address"]) or "?")[:120]
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            check_id = uuid.uuid4()
            made = await conn.execute(
                "INSERT INTO bill_checks (id, source_email_id, supplier, utility, issued_on, period_from, "
                "period_to, amount, old_amount, currency, reason, status, created_at, updated_at) VALUES "
                "(%s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'flagged', %s, %s) ON CONFLICT (source_email_id) DO "
                "NOTHING",
                (
                    check_id, row["id"], supplier, flag.utility, issued, flag.period_from, flag.period_to,
                    flag.amount, flag.old_amount, "EUR" if flag.amount or flag.old_amount else None,
                    flag.reason,
                    now, now,
                ),
            )  # fmt: skip
            if not made.rowcount or await self._ruled_out(conn, row["from_address"], bill=True):
                return made.rowcount
            subject, letter = rules.contest_letter(
                supplier, issued, flag.period_from, flag.period_to, flag.amount, flag.old_amount
            )
            task_id = await self._task(
                conn,
                title=rules.bill_task_title(supplier, lang),
                why=(
                    "Una bolletta fattura consumi di più di 2 anni fa: si possono contestare (L. 205/2017)."
                    if lang.startswith("it")
                    else "A bill charges consumption over 2 years old: it can be contested (L. 205/2017)."
                ),
                source_ref=row["id"],
                source_label=supplier,
                notes=f"{subject}\n\n{letter}"[:4000],
                priority=1,
            )
            if task_id is not None:
                await conn.execute("UPDATE bill_checks SET task_id = %s WHERE id = %s", (task_id, check_id))
        _log.info("bill_check_flagged", check_id=str(check_id))
        return 1

    # orders: warranties and supplies ---------------------------------------------------------------------

    async def _order(self, row: dict[str, Any], home_tz: str) -> int:
        order = rules.read_order(row["subject"] or "", row["body"] or "")
        if order is None:
            return 0
        day = self._local(row["sent_at"])
        seller = (row["from_name"] or links.domain_of(row["from_address"]))[:120] or None
        seller_key = links.domain_of(row["from_address"]) or None
        region = rules.region_of(order.currency, row["from_address"], home_tz)
        now = self._clock()
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            for item in order.items:
                key = rules.item_key(item)
                delivered: date | None = day if order.delivered else None
                took: tuple[Any, ...] | None = None
                if order.delivered:  # a delivery of an order already kept: its delivery day
                    moved = await conn.execute(
                        "UPDATE purchases SET delivered_on = %s, updated_at = %s WHERE id = (SELECT id FROM "
                        "purchases WHERE item_key = %s AND seller_key IS NOT DISTINCT FROM %s AND "
                        "delivered_on "
                        "IS NULL AND ordered_on BETWEEN %s AND %s ORDER BY ordered_on DESC LIMIT 1)",
                        (day, now, key, seller_key, day - timedelta(days=45), day),
                    )
                    if moved.rowcount:
                        made += 1
                        continue
                else:  # an order whose delivery was read first (same minute, a scan's boundary): one row
                    moved = await conn.execute(
                        "UPDATE purchases SET ordered_on = %s, price = coalesce(price, %s), "
                        "currency = coalesce(currency, %s), updated_at = %s WHERE id = (SELECT id "
                        "FROM purchases WHERE item_key = %s AND seller_key IS NOT DISTINCT FROM %s AND "
                        "source = 'email' AND ordered_on = delivered_on AND delivered_on BETWEEN %s AND %s "
                        "ORDER BY delivered_on LIMIT 1)",
                        (day, order.price, order.currency, now, key, seller_key, day - timedelta(days=1),
                         day + timedelta(days=45)),
                    )  # fmt: skip
                    if moved.rowcount:
                        made += 1
                        continue
                    # a delivery read before this order went to an older order of the same
                    # item (the newest it found): read in order, it is this one's. It comes back here.
                    took = await (
                        await conn.execute(
                            "WITH o AS (SELECT id, delivered_on FROM purchases WHERE item_key = %s AND "
                            "seller_key IS NOT DISTINCT FROM %s AND source = 'email' AND ordered_on < %s AND "
                            "delivered_on BETWEEN %s AND %s ORDER BY ordered_on DESC, id LIMIT 1 FOR UPDATE) "
                            "UPDATE purchases p SET delivered_on = NULL, updated_at = %s FROM o "
                            "WHERE p.id = o.id RETURNING o.id, o.delivered_on",
                            (key, seller_key, day, day, day + timedelta(days=45), now),
                        )
                    ).fetchone()
                    if took is not None:
                        delivered = took[1]
                done = await conn.execute(
                    "INSERT INTO purchases (id, item, item_key, seller, seller_key, ordered_on, "
                    "delivered_on, "
                    "price, currency, region, source, source_email_id, created_at, updated_at) VALUES (%s, "
                    "%s, "
                    "%s, %s, %s, %s, %s, %s, %s, %s, 'email', %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        uuid.uuid4(), item[:160], key, seller, seller_key, day, delivered,
                        order.price, order.currency, region, row["id"], now, now,
                    ),
                )  # fmt: skip
                if not done.rowcount and took is not None:  # a move, never a loss
                    await conn.execute(
                        "UPDATE purchases SET delivered_on = %s WHERE id = %s", (took[1], took[0])
                    )
                made += done.rowcount
        return 1 if made else 0

    async def _supply_tasks(self, conn: AsyncConnection, lang: str) -> int:
        today = self._owner.day(self._clock())
        made = 0
        for s, seller, seller_key, email_id in await self._supplies(conn):
            if not (today <= s.next_on <= today + SUPPLY_LEAD):
                continue
            taken = await conn.execute(
                "INSERT INTO supply_tasks (item_key, run_out_on, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (s.item_key, s.next_on, self._clock()),
            )
            if not taken.rowcount or await self._ruled_out(conn, None):
                continue
            store = links.store_again(f"x@{seller_key}") if seller_key else None
            task_id = await self._task(
                conn,
                title=rules.supply_task_title(s.item, lang),
                why=(
                    f"Lo compri ogni {s.every_days} giorni circa: sta per finire."
                    if lang.startswith("it")
                    else f"You buy it about every {s.every_days} days: it's about to run out."
                ),
                source_ref=email_id,
                source_label=seller,
                due_on=s.next_on,
                priority=2,
            )
            prep_id = None
            if task_id is not None and store is not None and self._handoffs is not None:
                prep_id = await self._handoffs.prepare(
                    device_id=None,
                    purpose="money",
                    target=s.item[:200],
                    lang=lang,
                    details={"kind": "supply", "item_key": s.item_key},
                    buttons=[self._button(f"supply:{s.item_key}:{s.next_on}", store[1], store[0], lang)],
                    object_id=f"task:{task_id}",
                )
            await conn.execute(
                "UPDATE supply_tasks SET task_id = %s, prep_id = %s WHERE item_key = %s AND run_out_on = %s",
                (task_id, uuid.UUID(prep_id) if prep_id else None, s.item_key, s.next_on),
            )
            made += task_id is not None
        return made

    async def _supplies(
        self, conn: AsyncConnection
    ) -> list[tuple[rules.Supply, str | None, str | None, str | None]]:
        rows = await (
            await conn.execute(
                "SELECT item_key, item, seller, seller_key, ordered_on, source_email_id FROM purchases "
                "WHERE ordered_on > %s ORDER BY ordered_on, source_email_id, item",
                (self._owner.day(self._clock()) - timedelta(days=money_rules.RECEIPTS_DAYS),),
            )
        ).fetchall()
        by_key: dict[str, list[tuple[str, str | None, str | None, date, str | None]]] = defaultdict(list)
        for key, item, seller, seller_key, day, email_id in rows:
            by_key[key].append((item, seller, seller_key, day, email_id))
        out = []
        for key, buys in by_key.items():
            last = buys[-1]
            if (s := rules.supply(key, last[0], [b[3] for b in buys])) is not None:
                out.append((s, last[1], last[2], last[4]))
        return out

    # promises ------------------------------------------------------------------------------------------

    async def _promises(self, row: dict[str, Any], lang: str) -> int:
        if self._clock() - row["sent_at"] > PROMISE_FRESH:
            return 0
        address = rules.recipient_address(list(row["to_addresses"] or []))
        if not address:
            return 0
        async with self._pool.connection() as conn:
            person = await (
                await conn.execute(
                    "SELECT name FROM people WHERE %s = ANY(emails) ORDER BY name LIMIT 1", (address,)
                )
            ).fetchone()
        # one of their people; else the display name when it is a plain name
        # ("Marco Rossi": letters, at most 4 words), never one a reply copies with a sentence in it; else the
        # address
        shown = rules.recipient_name(list(row["to_addresses"] or [])) or ""
        plain_name = intention_rules.speakable(shown)
        named = plain_name if plain_name and plain_name == " ".join(shown.split()) else None
        to = str(person[0]) if person else named or intention_rules.speakable(address.split("@", 1)[0])
        if not to:
            return 0
        said = self._local(row["sent_at"])
        found = rules.promises(row["body"] or "", said)
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            ruled_out = await self._ruled_out(conn, None)
            fresh: list[tuple[uuid.UUID, rules.Promise]] = []
            for p in found:
                promise_id = uuid.uuid4()
                kept = await conn.execute(
                    "INSERT INTO owner_promises (id, email_id, what, to_name, said_on, due_on, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (promise_id, row["id"], p.what[:120], to, said, p.due_on, self._clock()),
                )
                if kept.rowcount:
                    fresh.append((promise_id, p))
            # their promise is proposed for their yes (an intention), never a task on its own
            if fresh and not ruled_out and self.intentions is not None:
                made = await self.intentions.from_sent_mail(
                    conn, email_id=row["id"], found=fresh, to=to, lang=lang
                )
        if made:
            _log.info("owner_promises_kept", count=made)
        return made

    async def _owed(self, row: dict[str, Any]) -> int:
        """Another person's promise to them in a mail they received ("ti mando il contratto") is a
        waiting-for item for them: never an action, never a task. The sender's own words only."""
        if self.intentions is None or self._clock() - row["sent_at"] > PROMISE_FRESH:
            return 0
        found = rules.promises(row["body"] or "", self._local(row["sent_at"]))
        if not found:
            return 0
        async with self._pool.connection() as conn, conn.transaction():
            return await self.intentions.from_received_mail(
                conn, email_id=row["id"], address=str(row["from_address"]), found=found
            )

    # --- the minute's job ------------------------------------------------------------------------------

    async def tick(self, *, force: bool = False) -> int:
        """Claims weighed again and their tasks; refunds overdue; supplies about to run out (hourly)."""
        now = self._clock()
        if not force and self._ticked is not None and now - self._ticked < TICK_EVERY:
            return 0
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            await self._reassess(conn)
            made = await self._claim_tasks(conn, lang)
            made += await self._refund_tasks(conn, lang)
            made += await self._supply_tasks(conn, lang)
        self._ticked = now  # an hour from a run that finished, not one that failed
        if made:
            _log.info("money_back_tasks", count=made)
        return made

    # --- listings and the user's word ---------------------------------------------------------------

    def _claim_json(self, c: dict[str, Any], lang: str) -> dict[str, Any]:
        tz_from = ZoneInfo(c["tz_from"]) if c["tz_from"] else self._owner.zone
        return {
            "id": str(c["id"]),
            "flight": c["flight"],
            "place": f"{c['origin']} → {c['destination']}" if c["origin"] and c["destination"] else None,
            "scheduled_at": c["scheduled_at"].astimezone(tz_from).isoformat(),
            "disruption": c["disruption"],
            "delay_min": c["delay_min"],
            "notice_days": c["notice_days"],
            "rule": c["rule"],
            "status": c["status"],
            "reason": c["reason"],
            "why": fc.why(c["reason"], lang, reducible=fc.reducible(c["distance_km"], c["delay_min"])),
            "distance_km": c["distance_km"],
            "amount": _money(c["amount"]),
            "currency": c["currency"],
            "deadline_on": fc.deadline(c["scheduled_at"]).isoformat() if c["rule"] == "eu261" else None,
            "source": c["source"],
            "task_id": str(c["task_id"]) if c["task_id"] else None,
            "created_at": c["created_at"].isoformat(),
            "updated_at": c["updated_at"].isoformat(),
        }

    async def claims(self) -> list[dict[str, Any]]:
        lang = await self._owner.lang(self._pool)
        async with self._pool.connection() as conn:
            rows = await (
                await (conn.cursor(row_factory=dict_row)).execute(
                    "SELECT * FROM flight_claims ORDER BY scheduled_at DESC LIMIT %s", (LISTED,)
                )
            ).fetchall()
        return [self._claim_json(c, lang) for c in rows]

    async def _claim_row(self, conn: AsyncConnection, claim_id: str) -> dict[str, Any]:
        try:
            key = uuid.UUID(claim_id)
        except ValueError:
            raise _not_found() from None
        row = await (
            await (conn.cursor(row_factory=dict_row)).execute(
                "SELECT * FROM flight_claims WHERE id = %s", (key,)
            )
        ).fetchone()
        if row is None:
            raise _not_found()
        return row

    async def patch_claim(
        self, claim_id: str, *, status: str | None, delay_min: int | None
    ) -> dict[str, Any]:
        """Their word on a claim: sent, paid, dismissed (dismissing Pisa's task with it); a delay they
        give."""
        lang = await self._owner.lang(self._pool)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            c = await self._claim_row(conn, claim_id)
            if delay_min is not None:
                await conn.execute(
                    "UPDATE flight_claims SET delay_min = %s, delay_source = 'said', updated_at = %s "
                    "WHERE id = %s",
                    (delay_min, now, c["id"]),
                )
                c["delay_min"] = delay_min
                if c["status"] not in fc.OWNER_STATUSES:
                    v = fc.assess(
                        flight=c["flight"], origin=c["origin"], destination=c["destination"],
                        disruption=c["disruption"], delay_min=delay_min, notice_days=c["notice_days"],
                        scheduled_arr=c["scheduled_arr"], scheduled_at=c["scheduled_at"], now=now,
                    )  # fmt: skip
                    await conn.execute(
                        "UPDATE flight_claims SET status = %s, reason = %s, amount = %s, currency = %s "
                        "WHERE id = %s",
                        (v.status, v.reason, v.amount, "EUR" if v.amount is not None else None, c["id"]),
                    )
            if status is not None:
                await conn.execute(
                    "UPDATE flight_claims SET status = %s, updated_at = %s, paid_at = CASE WHEN %s = 'paid' "
                    "THEN coalesce(paid_at, %s) END WHERE id = %s",
                    (status, now, status, now, c["id"]),
                )
                if status in ("dismissed", "sent", "paid") and c["task_id"] is not None:
                    await conn.execute(
                        "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status = 'open'",
                        ("dismissed" if status == "dismissed" else "done", now, c["task_id"]),
                    )
            if not await _stopped(conn):  # a stop holds Pisa's tasks, their requests too
                await self._claim_tasks(conn, lang)
            c = await self._claim_row(conn, claim_id)
        return self._claim_json(c, lang)

    def _button(self, key: str, url: str, title: str, lang: str, note: str | None = None) -> dict[str, Any]:
        if not handoff_rules.safe_url(url, money=True):  # the last check, at runtime
            raise ValueError("a money button Pisa may not open")
        host = url.split("/")[2]
        button: dict[str, Any] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"pisa-money:{key}:{url}")),
            "kind": "open_url",
            "title": title[:60],
            "url": url,
            "subtitle": host,
        }
        if note:
            button["notes"] = [note[:200]]
        return button

    async def claim_draft(self, claim_id: str, lang: str) -> dict[str, Any]:
        """The letter and its buttons: the airline's site, the public form."""
        async with self._pool.connection() as conn:
            c = await self._claim_row(conn, claim_id)
        tz = self._owner.tz
        subject, text = fc.draft(
            lang=lang, flight=c["flight"], origin=c["origin"], destination=c["destination"],
            scheduled_at=c["scheduled_at"].astimezone(ZoneInfo(c["tz_from"] or tz)),
            scheduled_arr=c["scheduled_arr"].astimezone(ZoneInfo(c["tz_to"] or c["tz_from"] or tz))
            if c["scheduled_arr"] else None,
            disruption=c["disruption"], delay_min=c["delay_min"], rule=c["rule"], amount=c["amount"],
            distance_km=c["distance_km"], booking_last4=c["booking_last4"], notice_days=c["notice_days"],
        )  # fmt: skip
        buttons = await self._claim_buttons(c, lang, subject, text)
        return {"lang": lang, "subject": subject, "text": text, "buttons": [_block(b) for b in buttons]}

    async def _claim_buttons(
        self, c: dict[str, Any], lang: str, subject: str, text: str
    ) -> list[dict[str, Any]]:
        buttons = [
            self._button(f"claim:{c['id']}", url, title, lang, note)
            for title, url, note in fc.buttons(c["flight"], c["rule"], lang)
        ]
        if buttons:
            buttons[0]["text"] = subject + "\n\n" + text  # the whole letter, to copy
        await self._keep_buttons(buttons, f"claim:{c['id']}", c["flight"], lang, {"kind": "claim"})
        return buttons

    async def _keep_buttons(
        self, buttons: list[dict[str, Any]], object_id: str, target: str, lang: str, details: dict[str, Any]
    ) -> None:
        """The buttons as a preparation (purpose `money`), once per object."""
        if self._handoffs is None or not buttons:
            return
        async with self._pool.connection() as conn:
            known = await (
                await conn.execute("SELECT 1 FROM handoffs WHERE id = %s", (uuid.UUID(buttons[0]["id"]),))
            ).fetchone()
        if known is None:
            await self._handoffs.prepare(
                device_id=None,
                purpose="money",
                target=target,
                lang=lang,
                details=details,
                buttons=buttons,
                object_id=object_id,
            )

    async def refunds(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, merchant, amount, currency, promised_on, expected_by, status, "
                    "source_email_id, "
                    "task_id FROM refunds_promised ORDER BY promised_on DESC LIMIT %s",
                    (LISTED,),
                )
            ).fetchall()
        return [_refund_json(r) for r in rows]

    async def patch_refund(self, refund_id: str, status: str) -> dict[str, Any]:
        now = self._clock()
        try:
            key = uuid.UUID(refund_id)
        except ValueError:
            raise _not_found() from None
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE refunds_promised SET status = %s, updated_at = %s, received_at = CASE WHEN "
                    "%s = 'received' THEN coalesce(received_at, %s) END WHERE id = %s RETURNING id, "
                    "merchant, "
                    "amount, currency, promised_on, expected_by, status, source_email_id, task_id",
                    (status, now, status, now, key),
                )
            ).fetchone()
            if row is None:
                raise _not_found()
            if row[8] is not None:
                await conn.execute(
                    "UPDATE tasks SET status = 'dismissed', updated_at = %s WHERE id = %s AND status = "
                    "'open'",
                    (now, row[8]),
                )
        return _refund_json(row)

    # subscriptions ------------------------------------------------------------------------------------

    async def subscriptions(self, lang: str | None = None) -> list[dict[str, Any]]:
        """The subscriptions seen, with the service, its flags and the page to cancel it (read-only)."""
        lang = lang or await self._owner.lang(self._pool)
        today = self._owner.day(self._clock())
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT sender_key, from_name, subject, amount, currency, sent_at FROM money_seen "
                    "WHERE kind = 'receipt' AND amount IS NOT NULL AND currency IS NOT NULL AND sent_at >= "
                    "%s "
                    "ORDER BY sent_at",
                    (self._clock() - timedelta(days=money_rules.RECEIPTS_DAYS),),
                )
            ).fetchall()
            marks = {
                i: (u, k)
                for i, u, k, _ in await (
                    await conn.execute("SELECT id, used_on, kept_on, cancelled_on FROM subscription_marks")
                ).fetchall()
            }
            cancelled = {
                i: c
                for i, c in await (
                    await conn.execute(
                        "SELECT id, cancelled_on FROM subscription_marks WHERE cancelled_on IS NOT NULL"
                    )
                ).fetchall()
            }
            subjects: dict[str, str] = {}
            charges: list[tuple[str, str, Decimal, str, date]] = []
            for key, name, subject, amount, currency, sent_at in rows:
                charges.append((key, name, amount, currency, sent_at.astimezone(tz).date()))
                subjects[key] = subject
            subs = money_rules.recurring(charges)
            out: list[dict[str, Any]] = []
            for s in subs:
                mine = sorted(
                    (day, amount)
                    for key, _, amount, currency, day in charges
                    if key == s.sender_key
                    and currency == s.currency
                    and abs(amount - s.amount) <= s.amount * money_rules.SIMILAR
                )
                sid = rules.sub_id(s.sender_key, s.currency)
                service = links.service_of(s.sender_key, f"{s.name} {subjects.get(s.sender_key, '')}")
                used = await self._last_use(conn, s.sender_key)
                said, kept = marks.get(sid, (None, None))
                last_use = max((d for d in (used, said) if d is not None), default=None)
                out.append({
                    "id": sid, "name": s.name, "service": service.key if service else None,
                    "amount": f"{s.amount:.2f}", "currency": s.currency, "period": s.period,
                    "first_on": mine[0][0].isoformat(), "last_on": s.last_on.isoformat(), "count": s.count,
                    "last_use_on": last_use.isoformat() if last_use else None,
                    "cancelled_on": cancelled[sid].isoformat() if sid in cancelled else None,
                    "_amounts": [a for _, a in mine],
                    "_kept": kept, "_service": service,
                })  # fmt: skip
        by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sub in out:
            if sub["service"]:
                by_service[sub["service"]].append(sub)
        for sub in out:
            twins = [x for x in by_service.get(sub["service"] or "", []) if x is not sub]
            sub["duplicate_of"] = twins[0]["id"] if twins else None
            sub["flags"] = rules.flags(
                first_on=date.fromisoformat(sub["first_on"]),
                last_use_on=date.fromisoformat(sub["last_use_on"]) if sub["last_use_on"] else None,
                kept_on=sub.pop("_kept"),
                amounts=sub.pop("_amounts"),
                duplicate=bool(twins),
                today=today,
            )
            service: links.Service | None = sub.pop("_service")
            sub["cancel"] = _cancel(service, lang)
            money = ledger_rules.money_text(Decimal(sub["amount"]), sub["currency"], lang)
            sub["text"] = _sub_text(sub["name"], money, sub["period"], sub["flags"], lang)
        return out

    async def _last_use(self, conn: AsyncConnection, sender_key: str) -> date | None:
        """The last email from the service's domain that says they used it (a login, new episodes)."""
        domain = links.domain_of(sender_key)
        if not domain:
            return None
        parent = ".".join(domain.split(".")[-2:])
        rows = await (
            await conn.execute(
                "SELECT subject, snippet, sent_at FROM email_messages WHERE lower(from_address) ~ %s "
                "AND NOT ('SENT' = ANY(labels)) ORDER BY sent_at DESC LIMIT 60",
                (f"[@.]{re.escape(parent)}$",),
            )
        ).fetchall()
        for subject, snippet, sent_at in rows:
            if rules.use_signal(subject or "", snippet or ""):
                return self._local(sent_at)
        return None

    async def patch_subscription(
        self, sub_id: str, *, used: bool, keep: bool, cancelled: bool = False
    ) -> dict[str, Any]:
        """Their word on a subscription. `cancelled` ("l'ho disdetto"): they cancelled it
        themselves (Pisa never cancels); its last charge as Pisa saw it is kept for the month's "found you"
        line."""
        if not re.fullmatch(r"[0-9a-f]{16}", sub_id):
            raise _not_found()
        subs = {s["id"]: s for s in await self.subscriptions()}
        if sub_id not in subs:
            raise _not_found()
        sub = subs[sub_id]
        today = self._owner.day(self._clock())
        gone = (
            (today, str(sub["name"])[:120], Decimal(sub["amount"]), sub["currency"], sub["period"])
            if cancelled
            else (None, None, None, None, None)
        )
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO subscription_marks (id, used_on, kept_on, updated_at, cancelled_on, "
                "cancelled_name, "
                "cancelled_amount, cancelled_currency, cancelled_period) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET used_on = coalesce(EXCLUDED.used_on, "
                "subscription_marks.used_on), "
                "kept_on = coalesce(EXCLUDED.kept_on, subscription_marks.kept_on), updated_at = "
                "EXCLUDED.updated_at, "
                "cancelled_on = coalesce(subscription_marks.cancelled_on, EXCLUDED.cancelled_on), "
                "cancelled_name = coalesce(subscription_marks.cancelled_name, EXCLUDED.cancelled_name), "
                "cancelled_amount = coalesce(subscription_marks.cancelled_amount, "
                "EXCLUDED.cancelled_amount), "
                "cancelled_currency = coalesce(subscription_marks.cancelled_currency, "
                "EXCLUDED.cancelled_currency), "
                "cancelled_period = coalesce(subscription_marks.cancelled_period, EXCLUDED.cancelled_period)",
                (sub_id, today if used else None, today if keep else None, self._clock(), *gone),
            )
        return {s["id"]: s for s in await self.subscriptions()}[sub_id]

    async def cancel_buttons(self, sub_id: str, lang: str) -> list[dict[str, Any]]:
        """The button to the service's own page (kept as a preparation); none when it has no page."""
        subs = {s["id"]: s for s in await self.subscriptions(lang)}
        sub = subs.get(sub_id)
        if sub is None or not sub["cancel"] or not sub["cancel"].get("url"):
            return []
        title = ("Apri " if lang.startswith("it") else "Open ") + str(sub["name"])[:50]
        button = self._button(f"cancel:{sub_id}", sub["cancel"]["url"], title, lang, sub["cancel"]["steps"])
        await self._keep_buttons([button], f"sub:{sub_id}", sub["name"], lang, {"kind": "cancel"})
        return [_block(button)]

    # bill checks -----------------------------------------------------------------------------------------

    async def bill_checks(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await (conn.cursor(row_factory=dict_row)).execute(
                    "SELECT * FROM bill_checks ORDER BY issued_on DESC LIMIT %s", (LISTED,)
                )
            ).fetchall()
        return [_check_json(r) for r in rows]

    async def _check_row(self, conn: AsyncConnection, check_id: str) -> dict[str, Any]:
        try:
            key = uuid.UUID(check_id)
        except ValueError:
            raise _not_found() from None
        row = await (
            await (conn.cursor(row_factory=dict_row)).execute(
                "SELECT * FROM bill_checks WHERE id = %s", (key,)
            )
        ).fetchone()
        if row is None:
            raise _not_found()
        return row

    async def bill_draft(self, check_id: str, lang: str) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            c = await self._check_row(conn, check_id)
        subject, text = rules.contest_letter(
            c["supplier"], c["issued_on"], c["period_from"], c["period_to"], c["amount"], c["old_amount"]
        )
        return {"lang": "it-IT", "subject": subject, "text": text, "buttons": []}

    async def patch_bill_check(self, check_id: str, status: str) -> dict[str, Any]:
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            c = await self._check_row(conn, check_id)
            await conn.execute(
                "UPDATE bill_checks SET status = %s, updated_at = %s WHERE id = %s", (status, now, c["id"])
            )
            if c["task_id"] is not None:
                await conn.execute(
                    "UPDATE tasks SET status = %s, updated_at = %s WHERE id = %s AND status = 'open'",
                    ("done" if status == "contested" else "dismissed", now, c["task_id"]),
                )
            c = await self._check_row(conn, check_id)
        return _check_json(c)

    # warranties --------------------------------------------------------------------------------------

    async def warranties(self, q: str | None = None) -> list[dict[str, Any]]:
        today = self._owner.day(self._clock())
        async with self._pool.connection() as conn:
            rows = await (
                await (conn.cursor(row_factory=dict_row)).execute(
                    "SELECT * FROM purchases ORDER BY coalesce(delivered_on, ordered_on) DESC LIMIT 500"
                )
            ).fetchall()
        out = []
        for p in rows:
            if q and not rules.matches(p["item"], q):
                continue
            w = rules.warranty(p["ordered_on"], p["delivered_on"], p["region"], today)
            out.append({
                "id": str(p["id"]), "item": p["item"], "seller": p["seller"],
                "purchased_on": p["ordered_on"].isoformat(),
                "delivered_on": p["delivered_on"].isoformat() if p["delivered_on"] else None,
                "price": _money(p["price"]), "currency": p["currency"], "region": p["region"],
                "presumption_until": w.presumption_until.isoformat() if w.presumption_until else None,
                "ends_on": w.ends_on.isoformat() if w.ends_on else None,
                "action_until": w.action_until.isoformat() if w.action_until else None,
                "in_warranty": w.in_warranty, "source": p["source"],
            })  # fmt: skip
        out.sort(key=lambda w: (not w["in_warranty"],))
        return out[:LISTED]

    async def add_purchase(
        self,
        *,
        item: str,
        purchased_on: str,
        seller: str | None,
        delivered_on: str | None = None,
        region: str | None = None,
        source: str,
    ) -> dict[str, Any]:
        """A purchase of their (their words or the app): the warranty starts from it."""
        try:
            bought = date.fromisoformat(
                purchased_on[:10] if len(purchased_on) >= 10 else purchased_on + "-01"
            )
            delivered = date.fromisoformat(delivered_on) if delivered_on else None
        except ValueError:
            raise _invalid("purchased_on and delivered_on are days (YYYY-MM-DD).") from None
        if bought > self._owner.day(self._clock()) or (delivered is not None and delivered < bought):
            raise _invalid("A purchase is in the past, and delivered after it was bought.")
        clean = " ".join(item.split())[:160]
        if len(rules.item_key(clean)) < 2:
            raise _invalid("Name the item.")
        where = region or ("US" if self._owner.tz.startswith("America/") else "EU")
        purchase_id = uuid.uuid4()
        now = self._clock()
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO purchases (id, item, item_key, seller, seller_key, ordered_on, delivered_on, "
                "region, "
                "source, created_at, updated_at) VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s)",
                (
                    purchase_id, clean, rules.item_key(clean), (seller or None) and seller[:120], bought,
                    delivered, where, source, now, now,
                ),
            )  # fmt: skip
        return next(w for w in await self.warranties() if w["id"] == str(purchase_id))

    async def delete_purchase(self, purchase_id: str) -> None:
        try:
            key = uuid.UUID(purchase_id)
        except ValueError:
            raise _not_found() from None
        async with self._pool.connection() as conn:
            cur = await conn.execute("DELETE FROM purchases WHERE id = %s", (key,))
        if not cur.rowcount:
            raise _not_found()

    # promises and supplies --------------------------------------------------------------------------------

    async def promises(self) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, what, to_name, said_on, due_on, task_id FROM owner_promises "
                    "ORDER BY said_on DESC LIMIT %s",
                    (LISTED,),
                )
            ).fetchall()
        return [
            {"id": str(i), "what": w, "to": t, "said_on": s.isoformat(),
             "due_on": d.isoformat() if d else None,
             "task_id": str(k) if k else None}
            for i, w, t, s, d, k in rows
        ]  # fmt: skip

    async def supplies(self, lang: str | None = None) -> list[dict[str, Any]]:
        lang = lang or await self._owner.lang(self._pool)
        async with self._pool.connection() as conn:
            found = await self._supplies(conn)
            tasks = {
                (k, d): (t, p)
                for k, d, t, p in await (
                    await conn.execute("SELECT item_key, run_out_on, task_id, prep_id FROM supply_tasks")
                ).fetchall()
            }
        out = []
        for s, seller, seller_key, _ in sorted(found, key=lambda x: x[0].next_on):
            task_id, prep_id = tasks.get((s.item_key, s.next_on), (None, None))
            store = links.store_again(f"x@{seller_key}") if seller_key else None
            handoff = None
            if store is not None and task_id is not None and prep_id is not None:
                handoff = _block(self._button(f"supply:{s.item_key}:{s.next_on}", store[1], store[0], lang))
            out.append({
                "id": rules.sub_id(s.item_key, "supply"), "item": s.item, "seller": seller, "count": s.count,
                "every_days": s.every_days, "last_on": s.last_on.isoformat(),
                "next_on": s.next_on.isoformat(),
                "task_id": str(task_id) if task_id else None, "handoff": handoff,
            })  # fmt: skip
        return out

    # --- the user's words (the brain's tools) -------------------------------------------------------------

    async def flight_by_words(
        self, flight: str | None, *, before: datetime | None = None
    ) -> tuple[uuid.UUID, trip_rules.Leg] | None:
        """The flight leg of their trips they name (else their latest flight that already left)."""
        now = before or self._clock()
        async with self._pool.connection() as conn:
            legs = await self._flight_legs(conn)
        wanted = (flight or "").replace(" ", "").upper()
        if wanted:
            mine = [p for p in legs if (p[1].ref or "").replace(" ", "").upper() == wanted]
            past = [p for p in mine if p[1].at <= now + timedelta(days=1)]
            pool = past or mine
            return max(pool, key=lambda p: p[1].at) if pool else None
        past = [p for p in legs if p[1].at <= now]
        return max(past, key=lambda p: p[1].at) if past else None

    async def said_claim(
        self,
        leg: trip_rules.Leg,
        trip_id: uuid.UUID | None,
        disruption: fc.Disruption | None,
        delay_min: int | None,
    ) -> dict[str, Any]:
        """A claim from their own words (the brain checked them); its task when it is owed. Item 6:
        written inside the trip reader's shared lock, its trip looked up inside it (a merge may have moved
        the leg since `flight_by_words`, whose `trip_id` is only a hint now). A trip gone anyway is no 500:
        their words are kept, the claim without its trip (as `ON DELETE SET NULL` leaves one)."""
        lang = await self._owner.lang(self._pool)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (locks.TRIPS,))
            trip_id = next((t for t, x in await self._flight_legs(conn) if x.same(leg)), None)
            if disruption is None:
                row = await (
                    await conn.execute(
                        "SELECT id FROM flight_claims WHERE flight = %s AND day = %s",
                        ((leg.ref or "").replace(" ", "").upper(), leg.at.date()),
                    )
                ).fetchone()
                if row is None:
                    raise _not_found()
                claim_id = row[0]
            else:
                said = {"leg": leg, "disruption": disruption, "delay_min": delay_min, "notice_days": None,
                        "delay_source": "said" if delay_min is not None else None, "source": "said",
                        "email_id": None}  # fmt: skip
                try:
                    async with conn.transaction():  # a savepoint: their words are kept whatever the trip
                        claim_id = await self._upsert_claim(conn, trip_id=trip_id, **said)
                except ForeignKeyViolation:
                    _log.info("money_said_claim_without_trip")
                    claim_id = await self._upsert_claim(conn, trip_id=None, **said)
            if not await _stopped(conn):  # a stop holds Pisa's tasks, their requests too
                await self._claim_tasks(conn, lang)
            c = await self._claim_row(conn, str(claim_id))
        return self._claim_json(c, lang)

    async def mark_used(self, name: str, *, keep: bool) -> dict[str, Any] | None:
        """Their word on a subscription they name: they use it (and keeps it)."""
        wanted = rules.item_key(name)
        for sub in await self.subscriptions():
            names = rules.item_key(f"{sub['name']} {sub['service'] or ''}")
            if wanted and set(wanted.split()) & set(names.split()):
                return await self.patch_subscription(sub["id"], used=True, keep=keep)
        return None


async def _stopped(conn: AsyncConnection) -> bool:
    """Pisa's own tasks wait while they said "ferma tutto"."""
    return (
        await (await conn.execute("SELECT 1 FROM pisa_stops WHERE resumed_at IS NULL")).fetchone() is not None
    )


def _block(button: dict[str, Any]) -> dict[str, Any]:
    """A button as a `handoff` block."""
    block = {"type": "handoff", "handoff_id": button["id"], "kind": button["kind"], "title": button["title"]}
    block |= {k: button[k] for k in ("subtitle", "url", "phone", "text", "notes") if button.get(k)}
    return block


def _refund_json(r: tuple[Any, ...]) -> dict[str, Any]:
    i, merchant, amount, currency, promised_on, expected_by, status, email_id, task_id = r
    return {
        "id": str(i),
        "merchant": merchant,
        "amount": _money(amount),
        "currency": currency,
        "promised_on": promised_on.isoformat(),
        "expected_by": expected_by.isoformat(),
        "status": status,
        "source_email_id": email_id,
        "task_id": str(task_id) if task_id else None,
    }


def _check_json(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(c["id"]),
        "supplier": c["supplier"],
        "utility": c["utility"],
        "issued_on": c["issued_on"].isoformat(),
        "period_from": c["period_from"].isoformat() if c["period_from"] else None,
        "period_to": c["period_to"].isoformat() if c["period_to"] else None,
        "amount": _money(c["amount"]),
        "old_amount": _money(c["old_amount"]),
        "currency": c["currency"],
        "reason": c["reason"],
        "status": c["status"],
        "source_email_id": c["source_email_id"],
        "task_id": str(c["task_id"]) if c["task_id"] else None,
    }


def _cancel(service: links.Service | None, lang: str) -> dict[str, Any] | None:
    if service is None:
        return None
    out: dict[str, Any] = {"steps": service.steps_it if lang.startswith("it") else service.steps_en}
    if service.url:
        out["url"] = service.url
        out["host"] = service.url.split("/")[2]
    return out


def _sub_text(name: str, money: str, period: str, flags: list[str], lang: str) -> str:
    base = money_rules.ask_cancel(name, money, period, lang)
    if lang.startswith("it"):
        if "duplicate" in flags:
            every = "al mese" if period == "monthly" else "all'anno"
            return f"{name}: lo paghi due volte ({money} {every}). Ti mostro la pagina per disdirne uno?"
        if "unused" in flags:
            return (
                f"{name}, {money}: non vedo segni che lo usi da 60 giorni. Ti mostro la pagina per disdirlo?"
            )
        if "price_up" in flags:
            return f"{name} è aumentato a {money}: vuoi la pagina per disdirlo?"
        return base
    if "duplicate" in flags:
        return f"{name}: you pay for it twice ({money}). Shall I show the page to cancel one?"
    if "unused" in flags:
        return f"{name}, {money}: no sign you used it for 60 days. Shall I show the page to cancel it?"
    if "price_up" in flags:
        return f"{name} went up to {money}: want the page to cancel it?"
    return base
