"""Approvals: Pisa's actions outside Pisa, each waiting for the user's yes.

- **Create** (a conversation tool): the action is stored with its exact effect (`payload`, for its executor)
  and what the user reads (summary, details); at most 5 pending; it expires 10 minutes later.
- **Delivery**: `approval_request` on the device's live socket (the app speaks it in companion mode and
  listens 8 s for "sì" / "no"); an APNs push with category `APPROVAL` (actions approve / reject) when the
  device has no live socket, or is in the background outside companion mode with earbuds. The
  app's Approvals list always shows the pending ones.
- **Decide**: under a row lock, so two decisions (a tap and a voice "sì", two taps) run the action once: the
  second gets `approval_decided`. A late one gets `approval_expired`. An approval that `requires_biometric`
  needs the phone's signature (verified by the caller: `signed`). Approve runs the kind's executor:
  `done` with its result, or `failed` with the cause.
- **Undo** (calendar kinds): before `undo_until` (24 h), the executor's undo restores the event as it was.
- **Pisa acts**: a change in the user's own words is done at once (`act`: origin `words`, no question), and
  every change done on the user's word or spoken yes is announced to their device as `action_done` (Undo).
- **NB1-yescard** (`pisa.domain.yescard`): each approval is one `Action` with its hash and tier. The yes is
  bound to the hash stored at creation: a changed recipient, amount, time or text voids it (nothing runs). A
  spoken yes holds 120 s from the first read-out and counts only within 10 s of Pisa's latest one
  (`read_out`); the phone's card holds 10 minutes; tier 2 (money, Pisa's code) needs Face ID. After acting,
  the kind's read-back asks the provider: the result says it is done only when that confirms it (`verified`).
- Every creation, decision, execution and undo is logged by id and kind; never the details (they hold
  titles and addresses) nor an email's text.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.brain.untrusted import carried_lines
from pisa.domain import approval_sign, wrist, yescard
from pisa.domain import approvals as rules
from pisa.domain import guardian as guardian_rules
from pisa.domain import locked as locked_rules
from pisa.domain import notice as notice_rules
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.reasons import purpose_said
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.ports.push import ApprovalStates, LiveDevices, OutgoingApproval, PushSender
from pisa.services import decisions as decision_log
from pisa.services import guardian, presence
from pisa.services.notifications import enqueue
from pisa.services.push import PushService, drop_token
from pisa.services.server_identity import server_id
from pisa.services.unasked import Unasked

_log = get_logger("pisa.approvals")
SAME_ASK_WITHIN = timedelta(minutes=5)  # the same question again, this soon, is the same one
INTERRUPTED = {
    ("it-IT", "calendar"): "Potrebbe essere stato fatto: Pisa si è interrotto mentre lo faceva. "
    "Controlla il calendario prima di chiederlo di nuovo.",
    ("it-IT", "email"): "Potrebbe essere stata inviata: Pisa si è interrotto mentre lo faceva. "
    "Controlla la posta inviata prima di chiederlo di nuovo.",
    ("en-US", "calendar"): "It may have been done: Pisa was interrupted while doing it. "
    "Check the calendar before asking again.",
    ("en-US", "email"): "It may have been sent: Pisa was interrupted while doing it. "
    "Check the sent mail before asking again.",
}  # never "not done" when Pisa can't know
_COLUMNS = (
    "id, kind, summary, details, payload, status, result, requires_biometric, undo_minutes, undo, "
    "undo_until, undone_at, device_id, lang, created_at, expires_at, decided_at, decided_auth, executed_at, "
    "origin, action_hash, tier, read_out_at, voice_until, verified"
)
# the yes no longer matches the act (a recipient, an amount, a time or a text changed)
CHANGED = {
    "it-IT": "È cambiato dopo che l'hai visto: non ho fatto niente. Chiedimelo di nuovo.",
    "en-US": "It changed after you saw it: nothing was done. Ask me again.",
}
# a contract 2 resume refused (CONTRACT-2 §6.4): the cause, and what to do
REFUSED = {
    "approval_decided": (
        "This approval was already answered.",
        "Nothing to do; ask Pisa again for a new one",
    ),
    "approval_expired": ("This approval expired: nothing was done.", "Ask Pisa again"),
    "approval_changed": (
        "It changed after you saw it (a recipient, an amount, a time or the text): nothing was done.",
        "Look at it again, or ask Pisa again",
    ),
    "biometric_required": (
        "This approval needs the phone's signature after Face ID.",
        "Approve it with Face ID",
    ),
}


@dataclass(frozen=True)
class Outcome:
    """What an executor did: the sentence the user hears ("Cena spostata alle 21:00."), for the calendar
    kinds what restores it, and what follows once the result is saved (sync and re-plan)."""

    result: str
    undo: dict[str, Any] | None = None
    then: Callable[[], Awaitable[None]] | None = None
    receipt: dict[str, Any] | None = None  # what the read-back asks the provider about (an id)


Execute = Callable[[dict[str, Any]], Awaitable[Outcome]]
Undo = Callable[[dict[str, Any]], Awaitable[str]]
# the provider's state read back after acting: True only when it confirms the act
Verify = Callable[[dict[str, Any], Outcome], Awaitable[bool]]


class ApprovalsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        push: PushService,
        *,
        owner: Owner,
        live: Callable[[], LiveDevices | None] = lambda: None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        unasked: Unasked | None = None,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._push = push
        self._live = live
        self._unasked = unasked or Unasked()  # each question Pisa asks is the newest one
        self._clock = clock
        self._execute: dict[str, Execute] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._undo: dict[str, Undo] = {}
        self._verify: dict[str, Verify] = {}

    def later(self, job: Awaitable[None]) -> None:
        """Run `job` in the background (kept alive until it ends; a failure is logged)."""

        async def guarded() -> None:
            try:
                await job
            except Exception:
                _log.exception("approval_follow_up_failed")

        task = asyncio.create_task(guarded())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def settled(self) -> None:
        """The follow-ups started so far (a calendar change's sync and re-plan), finished (tests, the replay).
        Only this loop's unfinished ones: a follow-up started on another loop (the test client's socket) can't
        be awaited here, and one that ended there may never run its discard (a spin)."""
        loop = asyncio.get_running_loop()
        while running := [t for t in self._background if not t.done() and t.get_loop() is loop]:
            await asyncio.gather(*running, return_exceptions=True)

    def register(
        self, kind: str, execute: Execute, undo: Undo | None = None, verify: Verify | None = None
    ) -> None:
        self._execute[kind] = execute
        if undo is not None:
            self._undo[kind] = undo
        if verify is not None:
            self._verify[kind] = verify

    # --- create -------------------------------------------------------------------------------------------

    async def create(
        self,
        kind: rules.Kind,
        summary: str,
        details: list[str],
        payload: dict[str, Any],
        *,
        requires_biometric: bool = False,
        device_id: uuid.UUID | None = None,
        lang: str | None = None,
        approval_id: uuid.UUID | None = None,
        origin: rules.Origin = "proposal",
        asked: bool = False,
    ) -> dict[str, Any]:
        """Store the approval and send it to the user. Raises PisaError when 5 are already pending.
        `approval_id`: when the payload already names it (a new event's id derives from it). `origin`:
        Pisa's proposal, or a change held because it came from outside text."""
        lang = lang or self._owner.tag
        now = self._clock()
        approval_id = approval_id or uuid.uuid4()
        # the card names the part only outside text gave ("L'indirizzo viene dalla mail
        # di Marco Rossi"), whichever act asks
        details = [*details, *(line for line in carried_lines(lang) if line not in details)]
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.APPROVALS_CREATE,))
            # the same
            # question to the same device, still waiting from a moment ago, is that one approval, never a
            # second (checked under the same lock as the insert)
            # the same payload too (two drafts to Mario are two questions), and never an
            # email's send: each draft is its own yes
            same = None if kind == "email_send" else await (
                await conn.execute(
                    "SELECT id FROM approvals WHERE status = 'pending' AND kind = %s AND summary = %s AND "
                    "payload = %s AND device_id IS NOT DISTINCT FROM %s AND created_at > %s AND "
                    "expires_at > %s ORDER BY created_at DESC LIMIT 1",
                    (kind, rules.fit(summary, rules.SUMMARY_MAX), Jsonb(payload), device_id,
                     now - SAME_ASK_WITHIN, now),
                )
            ).fetchone()  # fmt: skip
            if same is None:
                await self._insert(conn, approval_id, kind, summary, details, payload, requires_biometric,
                                   device_id, lang, now, origin)  # fmt: skip
        if same is not None:
            _log.info("approval_repeated", approval_id=str(same[0]), kind=kind)
            return rules.view(await self._row(same[0]), self._clock(), self._owner.tz)
        _log.info(
            "approval_created",
            approval_id=str(approval_id),
            kind=kind,
            biometric=requires_biometric,
            origin=origin,
        )
        stored = await self._row(approval_id)
        try:
            await self._deliver(stored, asked=asked)
        except Exception:  # the app's list still shows it: a delivery failure never loses the approval
            _log.exception("approval_delivery_failed", approval_id=str(approval_id))
        return rules.view(stored, self._clock(), self._owner.tz)

    async def _insert(
        self, conn: AsyncConnection, approval_id: uuid.UUID, kind: rules.Kind, summary: str,
        details: list[str], payload: dict[str, Any], requires_biometric: bool, device_id: uuid.UUID | None,
        lang: str, now: datetime, origin: rules.Origin,
    ) -> None:  # fmt: skip
        row = await (
            await conn.execute(
                "SELECT count(*) FROM approvals WHERE status = 'pending' AND expires_at > %s", (now,)
            )
        ).fetchone()
        if row and row[0] >= rules.MAX_PENDING:
            raise PisaError(
                "validation_failed",
                f"{rules.MAX_PENDING} approvals are already waiting for the user's answer.",
                "Approve or reject them in the app (Settings → Approvals) first",
            )
        action = await self._action(conn, kind, payload)
        await conn.execute(
            "INSERT INTO approvals (id, kind, summary, details, payload, requires_biometric, "
            "undo_minutes, device_id, lang, created_at, expires_at, origin, action_hash, tier) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                approval_id, kind, rules.fit(summary, rules.SUMMARY_MAX),
                Jsonb(rules.fit_details(details)), Jsonb(payload),
                yescard.needs_face_id(action, requires_biometric), rules.UNDO_MINUTES[kind], device_id, lang,
                now, now + rules.EXPIRES_AFTER, origin, action.hash, action.tier,
            ),
        )  # fmt: skip

    async def act(
        self,
        kind: rules.Kind,
        summary: str,
        details: list[str],
        payload: dict[str, Any],
        *,
        device_id: uuid.UUID | None,
        lang: str,
        approval_id: uuid.UUID | None = None,
        origin: rules.Origin = "words",
        auth: rules.Auth = "words",
        announce: bool = True,
    ) -> dict[str, Any]:
        """The user's own words are the decision. The change is stored already approved (origin
        `words`, auth `words`), run now, and announced to the device as `action_done` with its Undo: no
        `approval_request`, no question. Returns the approval, `done` or `failed`. An option of Pisa's the
        user chose is its own yes (origin `proposal`, auth how they chose it)."""
        now = self._clock()
        approval_id = approval_id or uuid.uuid4()
        async with self._pool.connection() as conn:
            action = await self._action(conn, kind, payload)
            if action.face_id:  # RAIL (tier 2): money or Pisa's code is never done on words alone
                raise PisaError(
                    "biometric_required", "Money or Pisa's own code needs Face ID on the phone's card.", ""
                )
            await conn.execute(
                "INSERT INTO approvals (id, kind, summary, details, payload, status, undo_minutes, "
                "device_id, lang, created_at, expires_at, decided_at, decided_auth, origin, action_hash, "
                "tier) VALUES (%s, %s, %s, %s, %s, 'approved', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    approval_id, kind, rules.fit(summary, rules.SUMMARY_MAX),
                    Jsonb(rules.fit_details(details)), Jsonb(payload), rules.UNDO_MINUTES[kind], device_id,
                    lang, now, now + rules.EXPIRES_AFTER, now, auth, origin, action.hash, action.tier,
                ),
            )  # fmt: skip
        _log.info("approval_acted", approval_id=str(approval_id), kind=kind, origin=origin)
        row = await self._row(approval_id)
        await self._run(approval_id, row)
        if not announce:  # silent: done, in the ledger, no toast
            return rules.view(await self._row(approval_id), self._clock(), self._owner.tz)
        return await self.done_said(approval_id)

    async def done_said(self, approval_id: uuid.UUID) -> dict[str, Any]:
        """After a change ran on the user's word or spoken yes: `action_done` to their device, and
        the approval as it is now."""
        row = await self._row(approval_id)
        if row["status"] == "done" and row["device_id"] is not None and (live := self._live()) is not None:
            try:
                await live.action_done(
                    row["device_id"], rules.action_done(row, self._clock(), self._owner.tz)
                )
            except Exception:  # the toast is a convenience: the change is done and listed in the app
                _log.exception("action_done_failed", approval_id=str(approval_id))
        return rules.view(row, self._clock(), self._owner.tz)

    async def supersede(self, approval_id: str) -> None:
        """An email's approval replaced by the read-back of a newer version (a later "sì" answers
        only the latest)."""
        key = _parse(approval_id)
        async with self._pool.connection() as conn:
            found = await (
                await conn.execute(
                    "UPDATE approvals SET status = 'superseded' WHERE id = %s AND status = 'pending' "
                    "RETURNING device_id",
                    (key,),
                )
            ).fetchone()
        if found is not None:
            await self._state(key, found[0], "replaced", "server")

    async def pending_version(self, approval_id: str, version: int) -> dict[str, Any] | None:
        """An email's approval still pending for this version of its draft, or None."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_COLUMNS} FROM approvals WHERE id = %s AND status = 'pending' "  # noqa: S608
                "AND expires_at > %s AND (payload->>'version')::int = %s",
                (_parse(approval_id), self._clock(), version),
            )
            return await cur.fetchone()

    async def pending_of(self, device_id: uuid.UUID, kind: str) -> list[dict[str, Any]]:
        """This device's approvals of `kind` still waiting for a yes, oldest first (a correction
        re-makes one)."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_COLUMNS} FROM approvals WHERE device_id = %s AND kind = %s "  # noqa: S608
                "AND status = 'pending' AND expires_at > %s ORDER BY created_at",
                (device_id, kind, self._clock()),
            )
            return await cur.fetchall()

    async def unanswered(self, device_id: uuid.UUID, within: timedelta) -> list[dict[str, Any]]:
        """What Pisa asked this device in the last `within` and nobody answered: still pending, or past
        its 10 minutes with no answer (never done, never lost from Pisa's view). A re-made one is superseded
        and left out. Oldest first; `expired` tells which can't be answered any more."""
        now = self._clock()
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_COLUMNS}, (status = 'expired' OR expires_at <= %s) AS expired FROM approvals "  # noqa: S608
                "WHERE device_id = %s AND created_at > %s AND status IN ('pending', 'expired') "
                "AND decided_at IS NULL ORDER BY created_at",
                (now, device_id, now - within),
            )
            return await cur.fetchall()

    async def pending_for(self, device_id: uuid.UUID, ids: list[str]) -> list[dict[str, Any]]:
        """Of `ids`, the approvals still pending for this device and not expired, oldest first."""
        if not ids:
            return []
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"SELECT {_COLUMNS} FROM approvals WHERE id = ANY(%s) AND device_id = %s "  # noqa: S608
                "AND status = 'pending' AND expires_at > %s ORDER BY created_at",
                ([uuid.UUID(i) for i in ids], device_id, self._clock()),
            )
            return await cur.fetchall()

    # --- read ---------------------------------------------------------------------------------------------

    async def list(self, status: str = "pending") -> list[dict[str, Any]]:
        if status not in ("pending", "all"):
            raise PisaError("validation_failed", "The status must be pending or all.", "Fix the request")
        now = self._clock()
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            if status == "pending":
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM approvals WHERE status = 'pending' AND expires_at > %s "  # noqa: S608
                    "ORDER BY created_at",
                    (now,),
                )
            else:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM approvals WHERE created_at > %s ORDER BY created_at DESC",  # noqa: S608
                    (now - timedelta(days=rules.LIST_ALL_DAYS),),
                )
            rows = await cur.fetchall()
            why = await decision_log.why(conn, [str(r["id"]) for r in rows])
            lang = str((await self._owner.settings(conn))["language"])
        return [
            rules.view(r, now, self._owner.tz) | {"why": _said_why(why.get(str(r["id"])), r["kind"], lang)}
            for r in rows
        ]

    async def row(self, approval_id: str) -> dict[str, Any]:
        """The stored approval (its payload too), for Pisa's own follow-ups."""
        return await self._row(_parse(approval_id))

    async def get(self, approval_id: str) -> dict[str, Any]:
        return rules.view(await self._row(_parse(approval_id)), self._clock(), self._owner.tz)

    async def detail(self, approval_id: str) -> dict[str, Any]:
        """One approval, with `preview`: an email's whole text (`email_send` only), for the app to show before
        the yes. Never in a push or a log."""
        row = await self._row(_parse(approval_id))
        preview = None
        if row["kind"] == "email_send" and row["payload"].get("draft_id"):
            async with self._pool.connection() as conn:
                found = await (
                    await conn.execute(
                        "SELECT body FROM email_drafts WHERE id = %s",
                        (uuid.UUID(row["payload"]["draft_id"]),),
                    )
                ).fetchone()
            preview = found[0] if found else None
        async with self._pool.connection() as conn:
            action = await self._action(conn, row["kind"], row["payload"])
            signed = await self.signed(conn, row)
        card = yescard.phone_card(
            action, row["summary"], list(row["details"] or []), verified=row["verified"], lang=row["lang"]
        ) | {"action_hash": row["action_hash"] or action.hash}  # the hash the yes is bound to
        view = rules.view(row, self._clock(), self._owner.tz) | {"preview": preview, "card": card}
        return view | {"payload": signed["payload"], "recipients": signed["recipients"]}

    async def k30_subject(self, approval_id: str) -> dict[str, str]:
        """What the phone signs for this approval, from the row as it is now (the digest binds its kind,
        payload and recipients). 404 unknown; 409 no longer pending; 400 not a Face ID one."""
        row = await self._row(_parse(approval_id))
        if row["status"] == "pending" and row["expires_at"] <= self._clock():
            row["status"] = "expired"
        if row["status"] == "expired":
            raise PisaError("approval_expired", "This approval expired: nothing was done.", "Ask Pisa again")
        if row["status"] != "pending":
            raise PisaError(
                "approval_decided", f"This approval was already answered: it is {row['status']}.", ""
            )
        if not row["requires_biometric"]:
            raise PisaError(
                "validation_failed", "This approval doesn't need Face ID: approve it with a tap.", ""
            )
        async with self._pool.connection() as conn:
            digest = (await self._action(conn, row["kind"], row["payload"])).hash
        return {
            "approval_id": str(row["id"]),
            "kind": row["kind"],
            "summary": row["summary"],
            "digest": digest,
        }

    async def _action(self, conn: AsyncConnection, kind: str, payload: dict[str, Any]) -> yescard.Action:
        """The act as it is now (its kind, payload and recipients): its hash is the digest."""
        return yescard.of(kind, dict(payload), [*await self._recipients(conn, kind, payload)])

    async def _recipients(self, conn: AsyncConnection, kind: str, payload: dict[str, Any]) -> Sequence[str]:
        """Whom the act writes to or notifies: an email's draft's to and cc, or who a calendar change
        tells."""
        if kind == "email_send" and payload.get("draft_id"):
            found = await (
                await conn.execute(
                    "SELECT to_addresses, cc_addresses FROM email_drafts WHERE id = %s",
                    (uuid.UUID(payload["draft_id"]),),
                )
            ).fetchone()
            return [a for _, a in getaddresses([*found[0], *found[1]])] if found is not None else []
        if payload.get("send_updates") == "all":  # calendar kinds: who Google tells
            return [str(n) for n in payload.get("notified") or []]
        return []

    async def signed(self, conn: AsyncConnection, row: dict[str, Any]) -> dict[str, Any]:
        """What a contract 2 approval binds (CONTRACT-2 §6.3), from the row as it is now: `payload`, the act
        exactly as the server will run it (an email's recipients, subject, text and attachments' signs from
        its draft; a calendar change's stored payload without the server's bookkeeping), `recipients`
        (normalised), and their `digest`. `GET /v1/approvals/{id}` returns the first two, so the phone
        recomputes the third from what it shows."""
        kind, stored = row["kind"], dict(row["payload"])
        payload = {k: v for k, v in stored.items() if k not in rules.DIGEST_EXCLUDED}
        if kind == "email_send" and stored.get("draft_id"):
            draft = await (
                await conn.execute(
                    "SELECT to_addresses, cc_addresses, subject, body, attachments, version "
                    "FROM email_drafts WHERE id = %s",
                    (uuid.UUID(stored["draft_id"]),),
                )
            ).fetchone()
            if draft is not None:
                to, cc, subject, body, attachments, version = draft
                payload = {
                    "draft_id": stored["draft_id"],
                    "version": version,
                    "to": list(to),
                    "cc": list(cc),
                    "subject": subject,
                    "body": body,
                    "attachments": [str(a.get("sign") or "") for a in attachments or []],
                }
        recipients = approval_sign.normalise_recipients([*await self._recipients(conn, kind, stored)])
        return {
            "payload": payload,
            "recipients": recipients,
            "digest": approval_sign.digest(kind, payload, recipients),
        }

    async def challenge_subject(
        self, approval_id: str, interrupt_id: str, device_id: uuid.UUID
    ) -> dict[str, str]:
        """What the phone signs for this approval in contract 2 (`pisa-approval/2`), less the nonce and the
        time: the row as it is now, this install and the device asking. 404 unknown; 409 no longer pending;
        400 not a Face ID one, or not this approval's interrupt."""
        if approval_sign.approval_of_interrupt(interrupt_id) != approval_id.lower():
            raise PisaError(
                "validation_failed", "The interrupt_id is not this approval's interrupt.", "Check the request"
            )
        subject = await self.k30_subject(approval_id)
        row = await self._row(_parse(approval_id))
        async with self._pool.connection() as conn:
            signed = await self.signed(conn, row)
            server = await server_id(conn)
        return {
            "server": server,
            "device": str(device_id),
            "approval_id": subject["approval_id"],
            "interrupt_id": interrupt_id,
            "kind": row["kind"],
            "digest": signed["digest"],
            "recipients_count": str(len(signed["recipients"])),
        }

    async def action(self, approval_id: str) -> yescard.Action:
        """The approval's act as it is now (the spoken card's facts)."""
        row = await self._row(_parse(approval_id))
        async with self._pool.connection() as conn:
            return await self._action(conn, row["kind"], row["payload"])

    async def _row(self, approval_id: uuid.UUID, conn: AsyncConnection | None = None) -> dict[str, Any]:
        async def fetch(c: AsyncConnection) -> dict[str, Any] | None:
            cur = c.cursor(row_factory=dict_row)
            lock = " FOR UPDATE" if conn is not None else ""
            await cur.execute(f"SELECT {_COLUMNS} FROM approvals WHERE id = %s{lock}", (approval_id,))  # noqa: S608
            return await cur.fetchone()

        if conn is not None:
            row = await fetch(conn)
        else:
            async with self._pool.connection() as c:
                row = await fetch(c)
        if row is None:
            raise PisaError("not_found", f"There is no approval {approval_id}.", "Refresh the Approvals list")
        return row

    # --- decide -------------------------------------------------------------------------------------------

    async def decide(
        self,
        approval_id: str,
        decision: str,
        auth: str,
        *,
        in_conversation: bool = False,
        send_updates: str | None = None,
        signed: bool = False,
        talk: bool = False,
        said_at: datetime | None = None,
        spoken: bool = True,
        digest2: str | None = None,
        by: str | None = None,
    ) -> dict[str, Any]:
        """The user's answer. Approve runs the action now: the returned approval is `done` or `failed`.
        `spoken` False: their yes was typed in the conversation: the voice's 10 s and 120 s
        don't apply; the card's 10 minutes do.
        `in_conversation`: a spoken yes the server heard in the user's next turn, `said_at` when
        they said it: it counts within 10 s of Pisa's latest read-out of the card and 120 s of its first
        (the phone's own reply window keeps its 8 s and the server's 120 s). `send_updates`:
        "none" when the user said yes without notifying the others ("sì, senza avvisarli"). `signed`:
        the caller verified the phone's Secure Enclave signature over this approval's `approval` challenge; a
        `requires_biometric` approval is approved only so (the `auth` string is the client's word, never
        proof). `talk`: a tap on a card in a talk; like a spoken yes, it approves nothing while the phone
        is locked. `digest2`: a contract 2 resume's digest (`pisa-approval-digest/2`), checked again under the
        row lock (a change refuses it with `approval_changed`). `by`: who decided, for the contract 2 sockets
        that hold its interrupt (`pisa.approval.state`); by default the watch, else `auth`."""
        key = _parse(approval_id)
        if decision not in ("approve", "reject") or auth not in ("tap", "voice", "biometric"):
            raise PisaError(
                "validation_failed",
                "The decision must be approve or reject; auth tap, voice or biometric.",
                "",
            )
        if decision == "approve" and wrist.current() == "watch":
            # the backstop for every path: from the wrist an approval is seen and rejected,
            # approved on the phone (a relayed REST approve, a wrist "sì")
            raise PisaError(
                "validation_failed",
                "From the watch an approval can be rejected, not approved: approve it on the phone.",
                "Approve it in Pisa on the phone",
            )
        now = self._clock()
        expired = voided = changed2 = False
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(key, conn)
            # the act as it is now; the yes binds the hash the card was made with
            action = await self._action(conn, row["kind"], row["payload"])
            if digest2 is not None and decision == "approve" and row["status"] == "pending":
                changed2 = (await self.signed(conn, row))["digest"] != digest2
            heard = yescard.voice_yes(
                said_at or now, read_out_at=row["read_out_at"], until=row["voice_until"],
                created_at=row["created_at"],
            )  # fmt: skip
            if row["status"] == "pending" and row["expires_at"] <= now:
                await conn.execute("UPDATE approvals SET status = 'expired' WHERE id = %s", (key,))
                expired = True
            elif row["status"] == "expired":
                expired = True
            elif row["status"] != "pending":
                raise PisaError(
                    "approval_decided",
                    f"This approval was already answered: it is {row['status']}.",
                    "Nothing to do; ask Pisa again for a new one",
                )
            elif changed2:  # a contract 2 resume: what the phone saw is not what would run; nothing runs
                _log.info("approval_refused", approval_id=approval_id, why="changed after it was shown")
                raise PisaError("approval_changed", *REFUSED["approval_changed"])
            elif decision == "approve" and row["action_hash"] and action.hash != row["action_hash"]:
                # RAIL: a changed recipient, amount, time or text voids the yes: nothing runs
                await conn.execute(
                    "UPDATE approvals SET status = 'superseded', result = %s WHERE id = %s",
                    (CHANGED["en-US" if row["lang"] == "en-US" else "it-IT"], key),
                )
                voided = True
            elif decision == "approve" and auth == "voice" and spoken and heard == "over":
                raise PisaError(
                    "biometric_required",  # the app keeps it waiting and says why (no new code needed)
                    "A spoken yes counts only in the 2 minutes after Pisa reads the card: approve it on the "
                    "phone.",
                    "Approve it in Pisa",
                )
            elif decision == "approve" and auth == "voice" and spoken and in_conversation and heard == "late":
                raise PisaError(  # Pisa reads the card again (`ActionsService.answer`); nothing runs
                    "validation_failed",
                    "The yes came more than 10 seconds after Pisa read the card: Pisa reads it again.",
                    "Say yes right after Pisa reads it",
                )
            elif (
                decision == "approve"
                and (auth == "voice" or talk)
                and await presence.phone_locked(conn, row["device_id"], now)
            ):  # whoever speaks to a locked phone approves nothing
                _log.info("approval_refused", approval_id=approval_id, why="phone locked")
                raise PisaError(
                    "biometric_required", locked_rules.unlock_line(row["lang"]), "Unlock the phone"
                )
            elif decision == "approve" and (row["requires_biometric"] or action.face_id) and not signed:
                raise PisaError(
                    "biometric_required",
                    "This action needs Face ID: it moves money or changes Pisa's own code."
                    if action.face_id and not row["requires_biometric"]
                    else "This action needs Face ID: it goes to someone who isn't in the email thread.",
                    "Approve it with Face ID",
                )
            else:
                status = "approved" if decision == "approve" else "rejected"
                await conn.execute(
                    "UPDATE approvals SET status = %s, decided_at = %s, decided_auth = %s, "
                    "decided_origin = %s WHERE id = %s",
                    (status, now, auth, wrist.current(), key),  # a Reject on the wrist
                )
                if decision == "approve" and send_updates is not None and "send_updates" in row["payload"]:
                    # their own words narrow it ("sì, senza avvisarli"): the record follows them
                    row["payload"] = dict(row["payload"]) | {"send_updates": send_updates}
                    changed = await self._action(conn, row["kind"], row["payload"])
                    await conn.execute(
                        "UPDATE approvals SET payload = %s, action_hash = %s WHERE id = %s",
                        (Jsonb(row["payload"]), changed.hash, key),
                    )
        if voided:
            _log.info("approval_voided", approval_id=approval_id, kind=row["kind"])
            await self._state(key, row["device_id"], "replaced", "server")
            if digest2 is not None:
                raise PisaError("approval_changed", *REFUSED["approval_changed"])
            raise PisaError(
                "approval_expired",
                "It changed after the card was shown (a recipient, an amount, a time or the text): nothing "
                "was done.",
                "Ask Pisa again",
            )
        if expired:
            _log.info("approval_late", approval_id=approval_id, kind=row["kind"])
            if row["status"] == "pending":  # this answer found it lapsed: the sweep has not told anyone yet
                await self._state(key, row["device_id"], "expired", "server")
            raise PisaError(
                "approval_expired",
                f"This approval expired at {row['expires_at'].astimezone(self._owner.zone):%H:%M} "
                "(10 minutes without an answer): nothing was done.",
                "Ask Pisa again",
            )
        _log.info("approval_decided", approval_id=approval_id, kind=row["kind"], decision=decision, auth=auth)
        by = by or ("watch" if wrist.current() == "watch" else auth)
        await self._state(key, row["device_id"], "approved" if decision == "approve" else "rejected", by)
        if decision == "approve":
            await self._run(key, row)
            if auth == "voice" and in_conversation:
                return await self.done_said(key)
        return await self.get(approval_id)

    async def resume(
        self,
        approval_id: str,
        payload: dict[str, Any],
        *,
        device_id: uuid.UUID,
        interrupt_id: str,
        verify: Callable[[dict[str, str], str, str, str], Awaitable[None]],
        by: str | None = None,
    ) -> dict[str, Any]:
        """A contract 2 resume (CONTRACT-2 §6.4): the decision `payload` (`$defs/ApprovalResume`) checked in
        order against the row as it is now (decided, expired, a reject, the digest, Face ID), then the
        challenge and the signature (`verify(subject, nonce, signature, key id)`, which spends the nonce and
        raises `biometric_required`), then decided as every answer is (`decide`). Returns the approval, `done`
        or `failed` when it ran. Raises `approval_decided`, `approval_expired`, `approval_changed` or
        `biometric_required` when nothing ran."""
        key = _parse(approval_id)
        if approval_sign.approval_of_interrupt(interrupt_id) != str(key):
            raise PisaError(
                "not_found", f"There is no interrupt {interrupt_id[:80]!r}.", "Refresh the approvals"
            )
        row = await self._row(key)
        now = self._clock()
        async with self._pool.connection() as conn:
            signed = await self.signed(conn, row)
        status = row["status"]
        if (
            status == "superseded"
        ):  # replaced by a newer version, or voided by a change: what was seen changed
            raise PisaError("approval_changed", *REFUSED["approval_changed"])
        verdict = approval_sign.precheck(
            decided=status not in ("pending", "expired"),
            expired=status == "expired" or row["expires_at"] <= now,
            current_digest=signed["digest"],
            requires_biometric=bool(row["requires_biometric"]),
            payload=payload,
        )
        if verdict not in ("ok", "signature"):
            if verdict == "approval_expired" and status == "pending":
                await self._expire_one(key)
            _log.info("approval_resume_refused", approval_id=str(key), code=verdict)
            raise PisaError(verdict, *REFUSED[verdict])
        if verdict == "signature":
            subject = {
                "approval_id": str(key),
                "interrupt_id": interrupt_id,
                "kind": row["kind"],
                "digest": signed["digest"],
                "recipients_count": str(len(signed["recipients"])),
                "device": str(device_id),
            }
            await verify(
                subject,
                str(payload.get("nonce") or ""),
                str(payload["signature"]),
                str(payload.get("keyId") or ""),
            )
        decision = str(payload["decision"])
        auth = "biometric" if verdict == "signature" else str(payload.get("auth") or "tap")
        return await self.decide(
            str(key),
            decision,
            auth,
            signed=verdict == "signature",
            digest2=signed["digest"] if decision == "approve" else None,
            by=by,
        )

    async def _expire_one(self, key: uuid.UUID) -> None:
        async with self._pool.connection() as conn:
            found = await (
                await conn.execute(
                    "UPDATE approvals SET status = 'expired' WHERE id = %s AND status = 'pending' "
                    "RETURNING device_id",
                    (key,),
                )
            ).fetchone()
        if found is not None:
            await self._state(key, found[0], "expired", "server")

    async def _state(self, key: uuid.UUID, device_id: uuid.UUID | None, state: str, by: str) -> None:
        """`pisa.approval.state` to the contract 2 sockets that may hold the approval's interrupt: it was
        decided or closed (voice, the watch, a notification, another device, expiry), so they drop it."""
        live = self._live()
        if not isinstance(live, ApprovalStates):  # no live sockets in this process
            return
        try:
            await live.approval_state(device_id, {"approval_id": str(key), "state": state, "by": by})
        except Exception:  # the app's list and its own expiry still close it
            _log.exception("approval_state_failed", approval_id=str(key))

    async def _run(self, key: uuid.UUID, row: dict[str, Any]) -> None:
        execute = self._execute.get(row["kind"])
        started = time.monotonic()
        undo: dict[str, Any] | None = None
        then: Callable[[], Awaitable[None]] | None = None
        outcome: Outcome | None = None
        try:
            if execute is None:
                raise PisaError(
                    "internal", f"Pisa can't run {row['kind']} actions in this process.", "Try again"
                )
            outcome = await execute(dict(row["payload"]))
            status, result, undo, then = "done", outcome.result, outcome.undo, outcome.then
        except PisaError as err:
            status, result = "failed", err.message
        except Exception:
            trace = uuid.uuid4().hex
            _log.exception("approval_execution_crashed", approval_id=str(key), trace_id=trace)
            # it may have happened before the crash (a sent email, then a DB error): never "not done"
            lang = "en-US" if row["lang"] == "en-US" else "it-IT"
            family = "email" if row["kind"] == "email_send" else "calendar"
            status, result = "failed", f"{INTERRUPTED[lang, family]} (trace {trace})"
        now = self._clock()
        until = rules.undo_until(row["kind"], now) if status == "done" and undo is not None else None
        # saved first: the read-back can't lose what ran
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE approvals SET status = %s, result = %s, undo = %s, undo_until = %s, "
                "executed_at = %s WHERE id = %s",
                (status, result, Jsonb(undo) if undo else None, until, now, key),
            )
        verified: str | None = None
        if status == "done" and outcome is not None:
            verified = await self._read_back(key, row, outcome)
            unconfirmed = yescard.unverified(row["kind"], row["payload"], row["lang"])
            async with self._pool.connection() as conn:  # RAIL: never "fatto" without it
                await conn.execute(
                    "UPDATE approvals SET verified = %s, result = CASE WHEN %s THEN %s ELSE result END "
                    "WHERE id = %s",
                    (verified, verified == "not_verified", unconfirmed, key),
                )
        _log.info("approval_executed", approval_id=str(key), kind=row["kind"], status=status,
                  verified=verified, ms=int((time.monotonic() - started) * 1000))  # fmt: skip
        if status == "done" and then is not None:
            self.later(then())  # saved first: a failure here can't make it look "not done"

    async def _read_back(self, key: uuid.UUID, row: dict[str, Any], outcome: Outcome) -> yescard.Verified:
        """After acting, the provider's own state: `verified` only when it confirms the act (a
        kind with no read-back, or a read-back that fails, is `not_verified`)."""
        verify = self._verify.get(row["kind"])
        try:
            confirmed = verify is not None and await verify(dict(row["payload"]), outcome)
        except Exception as exc:  # the act ran; only its confirmation is missing
            _log.warning("approval_read_back_failed", approval_id=str(key), cause=exc.__class__.__name__)
            confirmed = False
        return "verified" if confirmed else "not_verified"

    # --- the card read out, the spoken yes ------------------------------------------------------------------

    async def read_out(
        self, ids: Sequence[str], *, said: str = "", renew: bool = True, at: datetime | None = None
    ) -> None:
        """Pisa read these cards out: the read-out ends when `said` has been said aloud (14 characters a
        second from now, or `at`: the talk's own moment). A spoken yes counts within 10 s of its end. `renew`:
        a fresh question from Pisa gives a spoken yes its 120 s again; Pisa reading a card again
        (`_read_again`) never does."""
        if not ids:
            return
        end = at or self._clock() + yescard.spoken_for(said)
        until = "%s" if renew else "coalesce(voice_until, %s)"
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE approvals SET read_out_at = %s, voice_until = {until} "  # noqa: S608
                "WHERE id = ANY(%s) AND status = 'pending'",
                (end, end + yescard.VOICE_TTL, [_parse(i) for i in ids]),
            )

    def heard(self, row: dict[str, Any], said_at: datetime | None = None) -> yescard.VoiceYes:
        """A spoken yes to this pending row, said now (or at `said_at`): ok, late (Pisa reads it again), or
        over."""
        return yescard.voice_yes(
            said_at or self._clock(), read_out_at=row["read_out_at"], until=row["voice_until"],
            created_at=row["created_at"],
        )  # fmt: skip

    async def tell(self, device_id: uuid.UUID, approval: dict[str, Any]) -> None:
        """The outcome of an answer given on the phone, as an `info` notify (spoken in companion mode): what
        was done, why it failed, or that nothing will be done."""
        row = await self._row(_parse(approval["id"]))
        text = rules.outcome_sentence(approval["status"], approval["result"], row["lang"])
        if text is None:
            return
        async with self._pool.connection() as conn:
            await enqueue(
                conn, kind="info", text=text, lang=row["lang"], now=self._clock(), timezone=self._owner.tz,
                device_id=device_id, respect_quiet=False, path="approval.outcome",
            )  # fmt: skip

    # --- undo ---------------------------------------------------------------------------------------------

    async def undo(self, approval_id: str) -> dict[str, Any]:
        """Restore what a calendar approval changed, within its 24 hours (once)."""
        key = _parse(approval_id)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await self._row(key, conn)
            undo = self._undo.get(row["kind"])
            if undo is None or row["status"] != "done" or row["undo"] is None:
                raise PisaError(
                    "validation_failed",
                    "Only a calendar change Pisa made can be undone; this one can't."
                    if undo is None
                    else f"This approval is {row['status']}: there is nothing to undo.",
                    "Change it by asking Pisa",
                )
            if row["undone_at"] is not None:
                raise PisaError("approval_decided", "This change was already undone.", "Nothing to do")
            if row["undo_until"] is None or row["undo_until"] <= now:
                raise PisaError(
                    "approval_expired",
                    "The 24 hours to undo this change are over.",
                    "Ask Pisa to change it back",
                )
            await conn.execute("UPDATE approvals SET undone_at = %s WHERE id = %s", (now, key))
        try:
            result = await undo(dict(row["undo"]))
        except Exception as exc:
            async with self._pool.connection() as conn:  # not undone: it may be tried again in time
                await conn.execute("UPDATE approvals SET undone_at = NULL WHERE id = %s", (key,))
            _log.warning("approval_undo_failed", approval_id=approval_id, cause=exc.__class__.__name__)
            if isinstance(exc, PisaError):
                raise
            raise PisaError(
                "internal", "Pisa couldn't undo it.", "Try again, or change it by asking Pisa"
            ) from None
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE approvals SET result = %s WHERE id = %s", (result, key))
        _log.info("approval_undone", approval_id=approval_id, kind=row["kind"])
        return await self.get(approval_id)

    # --- every minute -------------------------------------------------------------------------------------

    async def lapsing(self) -> int:
        """A proposal asked of a device and still waiting LAPSE_NOTICE_BEFORE_S before its end is
        said once more (`speak: approval_lapsing`: by Pisa's voice in a talk, or a talk Pisa asks to open;
        else a notice). Once per approval."""
        now = self._clock()
        until = now + timedelta(seconds=rules.LAPSE_NOTICE_BEFORE_S)
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    "UPDATE approvals SET lapse_noticed_at = %s WHERE status = 'pending' "
                    "AND device_id IS NOT NULL AND lapse_noticed_at IS NULL AND expires_at > %s "
                    "AND expires_at <= %s "
                    "RETURNING summary, lang, device_id",
                    (now, now, until),
                )
            ).fetchall()
            for summary, lang, device_id in rows:
                await enqueue(
                    conn, kind="info", text=rules.lapsing(summary, lang), lang=lang, now=now,
                    timezone=self._owner.tz, device_id=device_id, speak="approval_lapsing",
                    path="approval.lapsing",
                )  # fmt: skip
        return len(rows)

    async def expire(self) -> int:
        """Unanswered for 10 minutes: expired. Approved but never finished (the process stopped while it
        ran): failed, saying so: the user checks the calendar or the email."""
        now = self._clock()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND expires_at <= %s "
                "RETURNING id, device_id",
                (now,),
            )
            lapsed = await cur.fetchall()
            stuck = await conn.execute(
                "UPDATE approvals SET status = 'failed', executed_at = %s, "
                "result = CASE WHEN lang = 'en-US' AND kind = 'email_send' THEN %s "
                "WHEN lang = 'en-US' THEN %s WHEN kind = 'email_send' THEN %s ELSE %s END "
                "WHERE status = 'approved' AND decided_at < %s",
                (
                    now,
                    INTERRUPTED["en-US", "email"],
                    INTERRUPTED["en-US", "calendar"],
                    INTERRUPTED["it-IT", "email"],
                    INTERRUPTED["it-IT", "calendar"],
                    now - rules.EXPIRES_AFTER,
                ),
            )
        if lapsed or stuck.rowcount:
            _log.info("approvals_expired", count=len(lapsed), interrupted=stuck.rowcount)
        for key, device_id in lapsed:
            await self._state(key, device_id, "expired", "server")
        return len(lapsed)

    # --- delivery -----------------------------------------------------------------------------------------

    async def redeliver(self, approval_id: uuid.UUID) -> str | None:
        """An approval the gate held, pushed again at the next breakpoint while it
        is still pending (its expiry unchanged); None when it is no longer pending."""
        row = await self._row(approval_id)
        if row["status"] != "pending":
            return None
        return await self._deliver(row)

    async def _deliver(self, row: dict[str, Any], *, asked: bool = False) -> str:
        async with self._pool.connection() as conn:
            signed = await self.signed(conn, row)
            server = await server_id(conn)
        outgoing = OutgoingApproval(
            str(row["id"]), row["kind"], row["summary"], list(row["details"]), row["requires_biometric"],
            row["expires_at"].isoformat(),
            min(row["undo_minutes"], rules.WIRE_UNDO_MINUTES_MAX) if row["undo_minutes"] else None,
            digest=signed["digest"], recipients_count=len(signed["recipients"]), server=server,
            lang=row["lang"] or "en-US",
        )  # fmt: skip
        live = self._live()
        used: list[str] = []
        async with self._pool.connection() as conn:
            # the approval's push goes through the one gate. Their own words' (the yes
            # they are waiting for) go at once; Pisa's own proposal held in a busy block stays in the app's
            # list (its lapse, if it lapses there, is the neglect report's)
            # made in a turn in reply to their request (`asked`) is their words' too
            name = "approval.asked" if asked or row.get("origin") == "words" else "approval.request"
            verdict = await guardian.gate(
                conn, guardian_rules.Item(outgoing.id, guardian_rules.PATHS[name], row["expires_at"]),
                f"approval:{outgoing.id}", self._clock(), self._owner,
            )  # fmt: skip
            silent = verdict.silent
            if not verdict.released and verdict.decision == "digest":
                # the digest carries notices only, so an
                # approval never waits for it (it would lapse unpushed): shown now, silently
                silent = True
            elif not verdict.released:
                _log.info("approval_held", approval_id=outgoing.id, decision=verdict.decision)
                return "held"
            devices = await (
                await conn.execute(
                    "SELECT d.id, d.apns_token, d.apns_environment, p.app = 'background', "
                    "coalesce(p.companion = 'on' AND p.earbuds = 'connected', false), r.device_id IS NOT "
                    "NULL FROM devices d LEFT JOIN device_presence p ON p.device_id = d.id LEFT JOIN "
                    "device_push_relay r ON r.device_id = d.id WHERE d.revoked_at IS NULL AND (%s::uuid IS "
                    "NULL OR d.id = %s::uuid) ORDER BY d.created_at",
                    (row["device_id"], row["device_id"]),
                )
            ).fetchall()
            sender: PushSender | None = None
            for device_id, token, environment, backgrounded, companion, relay in devices:
                if live is not None and await live.approval(device_id, outgoing):
                    used.append("ws")
                    self._unasked.said(device_id)
                    if companion:  # the phone reads it out now: a spoken yes is timed from that
                        end = self._clock() + yescard.spoken_for(outgoing.summary)
                        await conn.execute(
                            "UPDATE approvals SET read_out_at = %s, voice_until = %s WHERE id = %s "
                            "AND status = 'pending'",
                            (end, end + yescard.VOICE_TTL, row["id"]),
                        )
                    if not backgrounded or companion:
                        continue
                category = "APPROVAL_BIOMETRIC" if outgoing.requires_biometric else "APPROVAL"
                interruption = "passive" if silent else wrist.APPROVAL_LEVEL
                body = rules.push_body(outgoing.summary, outgoing.details)
                if relay:  # sealed to the device's key: the relay sees no text (CONTRACT-2 §9)
                    outcome = await self._push.send_relay_notice(
                        conn, device_id, approval_notice(outgoing, body),
                        apns={"category": category, "threadId": "approval", "interruption": interruption},
                        collapse_id=outgoing.id,
                    )  # fmt: skip
                    if outcome is not None and outcome.status == "sent":
                        used.append("apns")
                        self._unasked.said(device_id)
                    elif outcome is not None and outcome.status == "failed":
                        _log.warning("push_failed", device_id=str(device_id), cause=outcome.message)
                    continue
                if not token:
                    continue
                sender = sender or await self._push.sender(conn)
                if sender is None:
                    _log.warning("push_not_configured", approval_id=outgoing.id)
                    continue
                # the lock screen shows what the yes does; Face ID can't be given from a
                # notification action, so a biometric one only opens the app (or rejects).
                outcome = await sender.send(
                    token, environment, title="Pisa",
                    body=body,
                    thread_id="approval",
                    data={"kind": "approval", "approval_id": outgoing.id, "approval_kind": outgoing.kind,
                          # the kind and how many their yes would notify, never who or what
                          "wrist": wrist.approval_wrist(
                              outgoing.kind, wrist.notified(outgoing.kind, dict(row["payload"])), row["lang"]
                          )},
                    push_id=outgoing.id,  # also the apns-collapse-id: a re-send replaces it
                    category=category,
                    # 10 minutes to answer: time-sensitive; their "never" (or a digest's) passive
                    interruption=interruption,
                )  # fmt: skip
                if outcome.status == "sent":
                    used.append("apns")
                    self._unasked.said(device_id)
                elif outcome.status == "token_invalid":
                    await drop_token(conn, device_id)
                else:
                    _log.warning("push_failed", device_id=str(device_id), cause=outcome.message)
        via = "ws" if "ws" in used else "apns" if "apns" in used else "none"
        _log.info("approval_delivered", approval_id=outgoing.id, via=via)
        return via


def approval_notice(outgoing: OutgoingApproval, body: str) -> dict[str, Any]:
    """An approval's push as a `pisa.notice` for the relay's sealed blob: an alert that opens the approval
    (`refs`), with the lock screen's line and Pisa's generic one."""
    lang = outgoing.lang or "en-US"
    return {
        "noticeId": outgoing.id.lower(),
        "kind": "alert",
        "priority": "urgent",
        "lang": lang,
        "text": body[:1000] or "-",
        "refs": [f"approval:{outgoing.id.lower()}"],
        "privacy": "people" if outgoing.kind.startswith("email") else "none",  # who it writes to
        "genericText": notice_rules.generic_text("approval", lang),
    }


def _parse(approval_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(approval_id)
    except ValueError:
        raise PisaError(
            "not_found", f"There is no approval {approval_id[:40]!r}.", "Refresh the Approvals list"
        ) from None


def _said_why(why: dict[str, Any] | None, kind: str, lang: str) -> dict[str, Any] | None:
    """An approval's "perché": the move's purpose in their language, else Pisa's line for its kind."""
    if why is None:
        return None
    line = "calendar" if kind.startswith("calendar_") else "email" if kind.startswith("email_") else "other"
    return why | {"purpose": purpose_said(why["purpose"], line, lang)}
