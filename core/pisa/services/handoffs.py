"""Pisa's preparations (handoffs) and the one follow-up after a tap.

- `prepare` keeps what Pisa prepared (one preparation, one row per button); nothing is sent, booked or called.
- `opened` (the phone's `handoff_opened`): the first tap counts; for a booking or a call it sets when Pisa
  asks how it went (FOLLOW_UP_AFTER later).
- `tick` (every minute): asks that once (RAIL: `followed_up_at` is set by one UPDATE, so never twice for the
  same preparation), through attend (the rails: quiet hours, the budget), as a check-in notice.
- `settle`: their word (worked | failed), once; what Pisa writes after it (the booking in the calendar) is the
  tool's, with their words.
- Phone numbers from outside text: asked about in one turn, confirmed only by their yes in a LATER turn
  (10 minutes), per device, in memory.
- "Asked" (what the brain sees) means the follow-up reached them (its notification went out), not only queued.

Never logged: their words, a number, a place's name, a URL.
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import handoffs as rules
from pisa.domain.attend import Candidate
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import guardian
from pisa.services.attend import deliver_now

if TYPE_CHECKING:
    from pisa.services.attend import AttendService
    from pisa.services.intentions import IntentionsService

PHONE_ASKED_FOR_S = 600.0  # a number Pisa asked about can be confirmed this long after
PHONE_CONFIRMED_FOR_S = 600.0  # … and stays confirmed this long, never for good
_log = get_logger("pisa.handoffs")


async def insert_prep(
    conn: AsyncConnection,
    *,
    device_id: uuid.UUID | None,
    purpose: str,
    target: str,
    lang: str,
    details: dict[str, Any],
    buttons: list[dict[str, Any]],
    object_id: str | None,
    now: datetime,
) -> str:
    """Keep one preparation and its buttons (in the caller's transaction): nothing is sent, booked or called.
    a people offer keeps its button here when it goes out, so its tap is recognised."""
    prep_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO handoff_preps (id, device_id, purpose, target, object_id, lang, details, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (prep_id, device_id, purpose, target[:200], object_id, lang, Jsonb(details), now),
    )
    for b in buttons:
        await conn.execute(
            "INSERT INTO handoffs (id, prep_id, kind, title, url, phone) VALUES (%s, %s, %s, %s, %s, %s)",
            (uuid.UUID(b["id"]), prep_id, b["kind"], b["title"], b.get("url"), b.get("phone")),
        )
    _log.info("handoff_prepared", prep_id=str(prep_id), purpose=purpose, buttons=len(buttons))
    return str(prep_id)


class HandoffService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attend: "AttendService | None" = None,
        intentions: "IntentionsService | None" = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._attend = attend
        self._asked: dict[uuid.UUID | None, tuple[str, str, float]] = {}  # (number, the asking turn, when)
        self.intentions = intentions
        self._confirmed: dict[uuid.UUID | None, dict[str, float]] = {}

    async def prepare(
        self,
        *,
        device_id: uuid.UUID | None,
        purpose: str,
        target: str,
        lang: str,
        details: dict[str, Any],
        buttons: list[dict[str, Any]],
        object_id: str | None,
    ) -> str:
        async with self._pool.connection() as conn, conn.transaction():
            prep_id = await insert_prep(
                conn, device_id=device_id, purpose=purpose, target=target, lang=lang, details=details,
                buttons=buttons, object_id=object_id, now=self._clock(),
            )  # fmt: skip
        if purpose == "message" and self.intentions is not None:
            # a promise in the message they dictated ("ti mando le foto domani") is proposed to them
            body = next((str(b.get("text") or "") for b in buttons if b.get("kind") == "message"), "")
            try:
                await self.intentions.from_dictation(to=target, body=body, lang=lang)
            except Exception as exc:  # never fails the message they are sending
                _log.warning("dictation_promise_failed", cause=exc.__class__.__name__)
        return prep_id

    async def opened(self, device_id: uuid.UUID | None, handoff_id: str, object_id: str) -> bool:
        """The phone's `handoff_opened`: True when it was this button's first tap. An unknown id (or one from
        another sheet) is dropped."""
        now = self._clock()
        try:
            key = uuid.UUID(handoff_id)
        except ValueError:
            return False
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE handoffs h SET opened_at = %s FROM handoff_preps p "
                    "WHERE h.id = %s AND p.id = h.prep_id AND h.opened_at IS NULL AND p.object_id = %s "
                    "AND (p.device_id IS NULL OR p.device_id = %s) RETURNING p.id, p.purpose",
                    (now, key, object_id, device_id),
                )
            ).fetchone()
            if row is None:
                _log.info("handoff_opened_unknown", device_id=str(device_id))
                return False
            prep_id, purpose = row
            after = rules.FOLLOW_UP_AFTER.get(purpose)
            await conn.execute(
                "UPDATE handoff_preps SET opened_at = coalesce(opened_at, %s), "
                "follow_up_at = coalesce(follow_up_at, %s) WHERE id = %s",
                (now, now + after if after is not None else None, prep_id),
            )
        _log.info("handoff_opened", prep_id=str(prep_id), purpose=purpose)
        return True

    async def tick(self) -> int:
        """Ask how it went, once per preparation, when its time came (and the user hasn't said yet). RAIL:
        one UPDATE takes the due ones (followed_up_at set), so a second tick, or two at once, never asks
        again; the question goes out in the same transaction, or neither does."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            due = await (
                await conn.execute(
                    "UPDATE handoff_preps SET followed_up_at = %s "
                    "WHERE follow_up_at <= %s AND followed_up_at IS NULL AND outcome IS NULL "
                    "AND created_at > %s "
                    "RETURNING id, purpose, target, lang, created_at",
                    (now, now, now - rules.OPEN_FOR),
                )
            ).fetchall()
            for prep_id, purpose, target, lang, created in due:
                text = rules.follow_up_text(purpose, target, lang)
                candidate = Candidate(
                    str(uuid.uuid4()),
                    "check_in",
                    "info",
                    text,
                    lang,
                    now,
                    alone=True,
                    speak="check_in",
                    deadline=created + rules.OPEN_FOR,  # attend may hold it, never past the day
                )
                # the follow-up about one of their people carries
                # that person ("How did the call to Marta go?"), so their "always for Marta" reaches it
                delivery: dict[str, Any] = {}
                if person := await guardian.person_named(conn, target):
                    delivery["person_id"] = str(person)
                if self._attend is not None:
                    await self._attend.propose(conn, candidate, f"handoff:{prep_id}", delivery)
                else:
                    notification_id = await deliver_now(conn, candidate, {}, timezone=self._owner.tz)
                    await conn.execute(
                        "UPDATE handoff_preps SET notification_id = %s WHERE id = %s",
                        (notification_id, prep_id),
                    )
                _log.info("handoff_followed_up", prep_id=str(prep_id), purpose=purpose)
        return len(due)

    async def open_preps(self) -> list[dict[str, Any]]:
        """The open preparations, newest first. `asked`: Pisa's follow-up reached them (its notification went
        out, by attend or at once), never only queued."""
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT p.id, p.purpose, p.target, p.details, p.opened_at, p.created_at, "
                    "EXISTS (SELECT 1 FROM notifications n WHERE n.delivered_at IS NOT NULL "
                    "AND n.delivered_via <> 'none' AND (n.id = p.notification_id OR n.id = (SELECT "
                    "ac.notification_id FROM attend_candidates ac WHERE ac.key = 'handoff:' || p.id::text))) "
                    "FROM handoff_preps p WHERE p.outcome IS NULL AND p.created_at > %s "
                    "AND p.purpose <> 'money' "
                    "ORDER BY p.created_at DESC LIMIT 5",
                    (now - rules.OPEN_FOR,),
                )
            ).fetchall()
        return [
            {"id": str(i), "purpose": p, "target": t, "details": d, "opened": o is not None, "asked": bool(f),
             "created_at": c}
            for i, p, t, d, o, c, f in rows
        ]  # fmt: skip

    async def settle(self, prep_id: str, outcome: str) -> dict[str, Any] | None:
        now = self._clock()
        try:
            key = uuid.UUID(prep_id.removeprefix("h:"))
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE handoff_preps SET outcome = %s, outcome_at = %s "
                    "WHERE id = %s AND outcome IS NULL AND created_at > %s "
                    "RETURNING id, purpose, target, details, opened_at, followed_up_at",
                    (outcome, now, key, now - rules.OPEN_FOR),
                )
            ).fetchone()
        if row is None:
            return None
        _log.info("handoff_settled", prep_id=str(row[0]), outcome=outcome)
        return {
            "id": str(row[0]),
            "purpose": row[1],
            "target": row[2],
            "details": row[3],
            "opened": row[4] is not None,
            "asked": row[5] is not None,
        }

    def asked_phone(self, device_id: uuid.UUID | None, phone: str, turn_id: str) -> None:
        self._asked[device_id] = (phone, turn_id, time.monotonic())

    def phone_confirmed(self, device_id: uuid.UUID | None, phone: str, yes: bool, turn_id: str) -> bool:
        """RAIL: their yes counts only in a LATER turn than the one Pisa read the number back in."""
        at = self._confirmed.get(device_id, {}).get(phone)
        if at is not None and time.monotonic() - at <= PHONE_CONFIRMED_FOR_S:
            return True
        asked = self._asked.get(device_id)
        if (
            not yes
            or asked is None
            or asked[0] != phone
            or not turn_id
            or not asked[1]
            or asked[1] == turn_id
        ):
            return False
        if time.monotonic() - asked[2] > PHONE_ASKED_FOR_S:
            return False
        del self._asked[device_id]
        self._confirmed.setdefault(device_id, {})[phone] = time.monotonic()
        return True
