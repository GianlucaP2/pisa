"""The holds' minute, and the record's readers (`pisa.services.guardian` is the gate).

`tick` (every minute, Pisa's initiative: "ferma tutto" pauses it):
- **outcomes**: what became of each item that went out (a reply to it, a tap, a swipe, nothing): the kinds'
  decay reads them;
- **release**: a notice held for a breakpoint goes when this moment is one (a meeting block ended, a walk
  ended, a talk ended, idle), or `DEADLINE_MARGIN` before its deadline; one that met no breakpoint in
  `HOLD_MAX` goes to the next digest;
- **digest**: at a breakpoint after a digest slot, what was held for it goes as one notice ("Nel frattempo:
  …"); a notice with its own payload (a check-in's choices, a done's Undo, a ritual's sheet, the card) goes as
  itself;
- **neglect**: a held item that never got out (expired, lapsed, still held after `NEGLECT_AFTER`) is marked.

`week` gives the Sunday card's line; `measures` the user report's numbers. Counts and ids only.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import guardian as rules
from pisa.domain.attend import ANSWERED_WITHIN, IGNORED_AFTER
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.logging import get_logger
from pisa.services import guardian
from pisa.services.notifications import AS_THEMSELVES, OUTBOX_CHANNEL, enqueue

_log = get_logger("pisa.guardian")
DIGEST_KEY = "guardian.digest_at"  # when the last digest went
OUTCOMES_LOOK = timedelta(days=2)
APPROVAL_MARGIN = timedelta(minutes=1)  # a held approval this close to its lapse is not pushed again
AT_ONCE = "coalesce(released_at = decided_at, false)"  # passed: out the moment it was decided (constant SQL)


class GuardianService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        repush: Callable[[uuid.UUID], Awaitable[str | None]] | None = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        # the approvals' push, again (ApprovalService.redeliver)
        self.repush = repush

    async def tick(self) -> int:
        now = self._clock()
        await self.outcomes(now)
        released = await self.release(now)
        released += await self.digest(now)
        await self.neglect(now)
        return released

    # --- outcomes (decay) ---------------------------------------------------------------------------------

    async def outcomes(self, now: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    "SELECT g.id, n.id, n.delivered_at, n.device_id, n.check_in_id, n.ask_id, g.path "
                    "FROM guardian_items g JOIN notifications n ON g.ref = 'notification:' || n.id::text "
                    "WHERE g.outcome IS NULL "
                    "AND g.counted AND n.delivered_at IS NOT NULL AND n.delivered_via IN ('ws', 'apns') "
                    "AND g.decided_at > %s FOR UPDATE OF g SKIP LOCKED",
                    (now - OUTCOMES_LOOK,),
                )
            ).fetchall()
            found = 0
            for item_id, notification_id, delivered, device_id, check_in_id, ask_id, path in rows:
                what = await _outcome(conn, notification_id, delivered, device_id, check_in_id, ask_id, now)
                if what is None:
                    continue
                if what == "ignored" and rules.path_of(path)[0].whose not in rules.DECAYS:
                    continue  # their reminder, their briefing need no reply: never "ignored"
                await conn.execute(
                    "UPDATE guardian_items SET outcome = %s, outcome_at = %s WHERE id = %s",
                    (what, now, item_id),
                )
                found += 1
        if found:
            _log.info("guardian_outcomes", count=found)
        return found

    # --- release at a breakpoint ---------------------------------------------------------------------------

    async def release(self, now: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            settings = await self._owner.settings(conn)
            if is_quiet(now, settings, self._owner.tz):
                return 0  # nothing held goes out in quiet hours (floors were never held)
            rows = await (
                await conn.execute(
                    "SELECT n.id, n.event_at, n.ask_id, g.decided_at, g.kind, n.guardian, g.reason "
                    "FROM notifications n "
                    "LEFT JOIN guardian_items g ON g.ref = 'notification:' || n.id::text "
                    "WHERE (n.guardian = 'breakpoint' OR (n.guardian = 'digest' AND n.event_at IS NOT NULL)) "
                    "AND n.delivered_at IS NULL AND n.delivered_via IS NULL FOR UPDATE OF n SKIP LOCKED"
                )
            ).fetchall()
            moment = await guardian.moment_for(conn, "info", now, self._owner, settings=settings)
            at_point = moment.busy is None and moment.breakpoint is not None
            released = 0
            for notification_id, event_at, ask_id, decided_at, kind, held_as, reason in rows:
                deadline = event_at or await guardian.ask_deadline(conn, ask_id)
                how = None
                if held_as == "digest" and (deadline is None or deadline <= now):
                    continue  # over already: the digest drops it as lapsed
                if deadline is not None and deadline - now <= rules.DEADLINE_MARGIN:
                    how = "deadline"
                    if held_as == "digest" and (reason or "").startswith("his_rule_never"):
                        # their "never" kept it for the digest, and the
                        # digest waited (a meeting at the slot): it goes before its deadline, shown, silently
                        await conn.execute(
                            "UPDATE notifications SET silent = TRUE WHERE id = %s", (notification_id,)
                        )
                elif held_as == "digest":
                    continue  # a digest item with a deadline still far: the digest takes it
                elif at_point:
                    how = f"breakpoint:{moment.breakpoint}"
                elif decided_at is not None and now - decided_at >= rules.HOLD_MAX:
                    if deadline is not None and deadline <= rules.next_digest(now, self._owner.tz):
                        how = "hold_max_deadline"  # its deadline comes before the digest: now
                    elif kind == "digest":
                        continue  # a digest is never folded into another digest: it waits for a breakpoint
                    else:
                        await conn.execute(
                            "UPDATE notifications SET guardian = 'digest', not_before = NULL WHERE id = %s",
                            (notification_id,),
                        )
                        _log.info("guardian_to_digest", ref=f"notification:{notification_id}")
                        continue
                if how is None:
                    continue
                await _let_out(conn, notification_id, how, now)
                released += 1
            if at_point and self.repush is not None:
                released += await self._approvals(conn, now)
        if released:
            _log.info("guardian_breakpoint_released", count=released, breakpoint=moment.breakpoint)
        return released

    async def _approvals(self, conn: AsyncConnection, now: datetime) -> int:
        """An approval the gate held is pushed again at the next breakpoint, while it is pending
        and not about to lapse."""
        rows = await (
            await conn.execute(
                "SELECT a.id FROM guardian_items g JOIN approvals a ON g.ref = 'approval:' || a.id::text "
                "WHERE g.released_at IS NULL AND g.lapsed_at IS NULL AND a.status = 'pending' "
                "AND a.expires_at > %s",
                (now + APPROVAL_MARGIN,),
            )
        ).fetchall()
        pushed = 0
        for (approval_id,) in rows:
            assert self.repush is not None
            try:
                via = await self.repush(approval_id)
            except Exception as exc:  # one approval's push never stops the others
                _log.warning("guardian_approval_repush_failed", cause=exc.__class__.__name__)
                continue
            pushed += via not in (None, "held")
        return pushed

    # --- the digest ----------------------------------------------------------------------------------------

    async def digest(self, now: datetime) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (DIGEST_KEY,))
            slot = rules.last_digest_slot(now, self._owner.tz)
            last = await get_setting(conn, DIGEST_KEY)
            if last is not None and datetime.fromisoformat(str(last)) >= slot:
                return 0  # this slot's digest went
            moment = await guardian.moment_for(conn, "digest", now, self._owner)
            if moment.busy is not None or moment.breakpoint is None:
                return 0  # at the next breakpoint
            rows = await (
                await conn.execute(
                    "SELECT id, kind, coalesce(summary || '.', text), check_in_id IS NOT NULL "
                    "OR choices IS NOT NULL OR done IS NOT NULL OR ritual IS NOT NULL "
                    "OR ledger_week IS NOT NULL OR ask_id IS NOT NULL OR expects_reply IS TRUE "
                    "OR weekly IS TRUE, event_at FROM notifications "
                    "WHERE guardian = 'digest' AND delivered_at IS NULL AND delivered_via IS NULL "
                    "ORDER BY created_at FOR UPDATE SKIP LOCKED"
                )
            ).fetchall()
            await set_setting(conn, DIGEST_KEY, Jsonb(now.isoformat()))
            if not rows:
                return 0
            # an item whose event is over is never folded (its words count from then): it lapsed
            for row in (r for r in rows if r[4] is not None and r[4] <= now):
                await self._mark_lapsed(conn, row[0], now)
            rows = [r for r in rows if not (r[4] is not None and r[4] <= now)]
            alone = [r for r in rows if r[3] or r[1] in AS_THEMSELVES]
            folded = [r for r in rows if not (r[3] or r[1] in AS_THEMSELVES)]
            for row in alone:  # its payload needs its own notice
                await _let_out(conn, row[0], "digest_alone", now)
            if folded:
                lang = str((await self._owner.settings(conn))["language"])
                await enqueue(
                    conn, kind="info", text=rules.digest_text([str(r[2]) for r in folded], lang), lang=lang,
                    now=now, timezone=self._owner.tz, path="guardian.digest",
                )  # fmt: skip
                ids = [r[0] for r in folded]
                await conn.execute(
                    "UPDATE notifications SET delivered_via = 'held', delivered_at = %s, "
                    "guardian = 'released' WHERE id = ANY(%s)",
                    (now, ids),
                )
                for notification_id in ids:
                    await guardian.release(conn, f"notification:{notification_id}", "digest", now)
        _log.info("guardian_digest", alone=len(alone), folded=len(folded))
        return len(rows)

    @staticmethod
    async def _mark_lapsed(conn: AsyncConnection, notification_id: uuid.UUID, now: datetime) -> None:
        await conn.execute(
            "UPDATE notifications SET delivered_via = 'none', delivered_at = %s WHERE id = %s",
            (now, notification_id),
        )
        await guardian.delivered(conn, f"notification:{notification_id}", "none", now)
        _log.info("guardian_lapsed", ref=f"notification:{notification_id}")

    # --- neglect -------------------------------------------------------------------------------------------

    async def neglect(self, now: datetime) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE guardian_items g SET lapsed_at = %s WHERE g.decision <> 'now' "
                "AND g.released_at IS NULL AND g.lapsed_at IS NULL AND (g.decided_at < %s "
                "OR EXISTS (SELECT 1 FROM notifications n WHERE g.ref = 'notification:' || n.id::text "
                "AND n.delivered_at IS NOT NULL) "
                "OR EXISTS (SELECT 1 FROM approvals a WHERE g.ref = 'approval:' || a.id::text "
                "AND a.status = 'expired'))",
                (now, now - rules.NEGLECT_AFTER),
            )
        if cur.rowcount:
            _log.info("guardian_neglected", count=cur.rowcount)
        return cur.rowcount


# --- the record's readers ----------------------------------------------------------------------------------


async def week(conn: AsyncConnection, start: datetime, end: datetime) -> tuple[int, int, int]:
    """(held, passed, never out) of the counted items decided in [start, end): held is what waited; passed
    what went at once."""
    row = await (
        await conn.execute(
            f"SELECT count(*) FILTER (WHERE NOT {AT_ONCE}), count(*) FILTER (WHERE {AT_ONCE}), "  # noqa: S608
            f"count(*) FILTER (WHERE NOT {AT_ONCE} AND lapsed_at IS NOT NULL) FROM guardian_items "
            "WHERE counted AND decided_at >= %s AND decided_at < %s",
            (start, end),
        )
    ).fetchone()
    return (int(row[0]), int(row[1]), int(row[2])) if row else (0, 0, 0)


# held now and not yet out, and not an approval they have already answered (constant SQL)
HOLDING = (
    "g.decision <> 'now' AND g.released_at IS NULL AND g.lapsed_at IS NULL AND g.released_how IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM approvals a WHERE g.ref = 'approval:' || a.id::text "
    "AND a.status <> 'pending')"
)


async def holding(conn: AsyncConnection) -> int:
    """What Pisa holds now and has not yet let out ("Pisa ne tiene N per dopo")."""
    row = await (
        await conn.execute(f"SELECT count(*) FROM guardian_items g WHERE {HOLDING}")  # noqa: S608
    ).fetchone()
    return int(row[0]) if row else 0


async def measures(conn: AsyncConnection, start: datetime, end: datetime) -> dict[str, Any]:
    """The user report's: dismissals a day, held to released, their overrides, the neglect by kind."""
    rows = await (
        await conn.execute(
            f"SELECT decision, reason, override, released_at IS NOT NULL, lapsed_at IS NOT NULL, {AT_ONCE}, "  # noqa: S608
            "kind FROM guardian_items WHERE counted AND decided_at >= %s AND decided_at < %s",
            (start, end),
        )
    ).fetchall()
    dismissed = await (
        await conn.execute(
            "SELECT count(*) FROM device_app_events WHERE event = 'notification_dismissed' "
            "AND received_at >= %s AND received_at < %s",
            (start, end),
        )
    ).fetchone()
    days = max(1, round((end - start) / timedelta(days=1)))
    out = rules.measures(
        [(str(d), str(why), o, bool(rel), bool(lap), bool(once)) for d, why, o, rel, lap, once, _ in rows],
        int(dismissed[0]) if dismissed else 0,
        days,
    )
    never: dict[str, int] = {}
    for r in rows:
        if not r[5] and r[4]:
            never[str(r[6])] = never.get(str(r[6]), 0) + 1
    out["never_out_by_kind"] = never
    return out


async def _let_out(conn: AsyncConnection, notification_id: uuid.UUID, how: str, now: datetime) -> None:
    """A hold let out: delivered now by the API's dispatcher (announced on the outbox), never gated again."""
    await conn.execute(
        "UPDATE notifications SET guardian = 'released', not_before = %s WHERE id = %s",
        (now, notification_id),
    )
    # how it was let out; `released_at` once the dispatcher delivered it (`guardian.delivered`)
    await conn.execute(
        "UPDATE guardian_items SET released_how = %s WHERE ref = %s AND released_at IS NULL",
        (how[:60], f"notification:{notification_id}"),
    )
    await conn.execute("SELECT pg_notify(%s, %s)", (OUTBOX_CHANNEL, str(notification_id)))
    _log.info("guardian_released", ref=f"notification:{notification_id}", reason=how)


async def _outcome(
    conn: AsyncConnection, notification_id: uuid.UUID, delivered: datetime, device_id: uuid.UUID | None,
    check_in_id: uuid.UUID | None, ask_id: uuid.UUID | None, now: datetime,
) -> str | None:  # fmt: skip
    """replied (their words soon after), tapped (a check-in answered, Pisa's question answered, the notice
    opened), dismissed (a swipe), ignored (nothing in `IGNORED_AFTER`), or None (not yet known)."""
    tapped = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM check_ins WHERE id = %s AND answered_at IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM ritual_asks WHERE id = %s AND status IN ('yes', 'no'))",
            (check_in_id, ask_id),
        )
    ).fetchone()
    if tapped and tapped[0]:
        return "tapped"
    opened = await (
        await conn.execute(
            "SELECT 1 FROM device_app_events WHERE event = 'notification_opened' AND notification_id = %s",
            (notification_id,),
        )
    ).fetchone()
    if opened:
        return "tapped"  # a tap on the notice itself, read before a swipe
    swiped = await (
        await conn.execute(
            "SELECT 1 FROM device_app_events WHERE event = 'notification_dismissed' AND notification_id = %s",
            (notification_id,),
        )
    ).fetchone()
    if swiped:
        return "dismissed"
    talked = await (
        await conn.execute(
            "SELECT 1 FROM messages m JOIN conversations c ON c.id = m.conversation_id WHERE m.role = 'user' "
            "AND m.created_at >= %s AND m.created_at <= %s AND (%s::uuid IS NULL OR c.device_id = %s::uuid) "
            "LIMIT 1",
            (delivered, delivered + ANSWERED_WITHIN, device_id, device_id),
        )
    ).fetchone()
    if talked:
        return "replied"
    return "ignored" if now - delivered >= IGNORED_AFTER else None
