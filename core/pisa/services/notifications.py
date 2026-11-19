"""Proactive messages: stored first, then delivered by the API process, which owns
the live sockets.

- `enqueue` (any process) stores the message. Outside quiet mode, when urgent, or for a message that ignores
  quiet (a briefing because the user just showed up), it is announced on NOTIFY pisa_outbox. Otherwise it is
  **held**.
- The API's `Dispatcher` delivers each one: protocol `notify` to every target device with a live WebSocket,
  else an APNs push if the device has a token; a device with neither gets nothing. A device whose last
  presence is `app_background` gets both: a suspended app may never read its socket, unless companion mode is
  on with earbuds connected: then the app is awake and speaks it, so no push. Pushes carry
  `apns-collapse-id` = the notification id, so a repeat replaces the first on the phone. `delivered_via`
  records the best channel used (ws, apns, none).
- **At-least-once**: the message is marked delivered after the sends, in the same
  transaction that locks it. A crash between a send and the commit sends it again on restart; the repeat
  carries the same notification id (the app drops a `notify` it already has; APNs collapses the push). The
  other order (mark first) would lose a reminder on a crash, which is worse than a repeat.
- `release_held` (every minute, and when quiet is switched off) delivers held briefings as themselves and
  every other held message as **one** `info` summary: "While you were in quiet mode: …". A message stored
  with a `summary` (a reminder: "Dentista alle 9:00") appears in that form, not as the text it had then.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import psycopg
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import notice as notice_rules
from pisa.domain import talk as form
from pisa.domain import wrist
from pisa.domain.approvals import LAPSE_NOTICE_BEFORE_S
from pisa.domain.guardian import MEETING_MAX
from pisa.domain.guardian import TALK_HOLD as BUSY_HOLD
from pisa.domain.owner import Owner
from pisa.domain.settings import is_quiet
from pisa.domain.talk import Urgency
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.ports.push import Breakpoints, LiveDevices, OutgoingNotification, PushSender
from pisa.services import guardian
from pisa.services.push import PushService, drop_token
from pisa.services.settings import load
from pisa.services.stop import stopped_now
from pisa.services.unasked import Unasked

OUTBOX_CHANNEL = "pisa_outbox"
# A message the API couldn't deliver this long after it was queued (the API was down) is stale: not sent.
PENDING_MAX_AGE = timedelta(minutes=15)
# Held while the user talks to Pisa: delivered at the end of the exchange, or by the sweep after this long (a
# socket that closed meanwhile: the sweep pushes it).
HERE_WITHIN = timedelta(minutes=30)  # an open stay seen this recently is where they are (as `talk.fresh`)
MEETING_HOLD_MAX = timedelta(hours=2)  # and a meeting holds a notification at most this long
NOTIFY_MAX_CHARS = 1000  # protocol: notify.text maxLength
AS_THEMSELVES = frozenset({"briefing"})  # never folded into the summary
SUMMARY_PREFIX = {"it-IT": "Mentre eri in modalità silenziosa: ", "en-US": "While you were in quiet mode: "}
Kind = Literal["reminder", "briefing", "replan", "alert", "info", "check_in", "done"]
# what still goes out while Pisa is stopped: the reminders they asked for and the alerts (leave now, safety)
UNSTOPPED = frozenset({"reminder", "alert"})
Priority = Literal["low", "normal", "urgent"]
# the gate's reasons a meeting can't hold: a floor, their "always", their "never" shown silently
PIERCE = frozenset({"floor", "his_rule_always", "his_rule_never_floor", "his_rule_never_silent"})
LATE_ANSWERS = frozenset({"turn.answer"})  # a REST turn's answer past its wait (urgency breakpoint): active
DIGEST_PATHS = frozenset({"quiet.summary", "guardian.digest"})  # the holds' own digests: passive on the phone
_log = get_logger("pisa.notifications")


def fit(text: str, limit: int = NOTIFY_MAX_CHARS) -> str:
    """At most `limit` characters, cut after a sentence when possible."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…"


async def enqueue(
    conn: AsyncConnection,
    *,
    kind: Kind,
    text: str,
    lang: str,
    now: datetime,
    timezone: str,
    priority: Priority = "normal",
    device_id: uuid.UUID | None = None,
    respect_quiet: bool = True,
    summary: str | None = None,
    check_in_id: uuid.UUID | None = None,
    choices: list[dict[str, str]] | None = None,
    expects_reply: bool = False,
    weekly: bool = False,
    done: dict[str, object] | None = None,
    urgency: Urgency | None = None,
    event_at: datetime | None = None,
    not_before: datetime | None = None,
    silent: bool = False,
    speak: str | None = None,
    ledger_week: str | None = None,
    path: str | None = None,
    person_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    sheet_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, bool]:
    """Store a proactive message; announce it unless quiet mode holds it. Returns (id, held). A check-in
    carries its id and choices, and expects a reply; a `done` carries what was closed and its Undo.
    `not_before`: delivered from then (attend's `at`); `silent`: shown on the phone, never spoken (attend's
    phone channels). `speak`: why Pisa may say it first by its own voice.
    `ledger_week`: the Sunday card of that week is ready. `path`: the map's name for the
    way it started (`pisa.domain.guardian.PATHS`), read by the gate at delivery; `person_id`: one of their
    people it is about (their "always for Marta"). `turn_id`, `sheet_id`: a REST turn's answer, past its
    wait, and the sheet it kept for the phone."""
    held = respect_quiet and priority != "urgent" and is_quiet(now, await load(conn, lang), timezone)
    notification_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, queued_at, "
        "delivered_via, summary, check_in_id, choices, expects_reply, weekly, done, urgency, event_at, "
        "not_before, silent, speak, ledger_week, path, person_id, turn_id, sheet_id) VALUES (%s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            notification_id, kind, text, lang, priority, device_id, now, now, "held" if held else None,
            summary, check_in_id, Jsonb(choices) if choices else None,
            True if check_in_id or expects_reply else None,
            True if weekly else None, Jsonb(done) if done else None, urgency or form.urgency(kind, priority),
            event_at, not_before, True if silent else None, speak if speak in form.SPEAKS else None,
            ledger_week, path, person_id, turn_id, sheet_id,
        ),
    )  # fmt: skip
    if not held:
        await conn.execute("SELECT pg_notify(%s, %s)", (OUTBOX_CHANNEL, str(notification_id)))
    _log.info("notification_queued", notification_id=str(notification_id), kind=kind, held=held)
    return notification_id, held


async def release_held(pool: AsyncConnectionPool, *, now: datetime, owner: Owner) -> int:
    """When quiet mode is over: held briefings go out as themselves, everything else as one summary."""
    async with locks.held(pool, locks.QUIET_RELEASE) as got:
        if not got:
            return 0
        async with pool.connection() as conn, conn.transaction():
            settings, timezone = await owner.settings(conn), owner.tz
            if is_quiet(now, settings, timezone) or await stopped_now(conn):  # held while stopped
                return 0
            rows = await (
                await conn.execute(
                    "SELECT n.id, CASE WHEN n.ledger_week IS NOT NULL THEN 'card' ELSE n.kind END, "
                    "coalesce(n.summary || '.', n.text), "
                    # an ask that is over by now (an approval's "scade tra due minuti", a
                    # check-in answered or dropped) is never delivered after the quiet hours
                    "(n.speak = 'approval_lapsing' AND n.created_at + make_interval(secs => %s) <= %s) "
                    "OR (c.id IS NOT NULL AND (c.answered_at IS NOT NULL OR c.dropped_at IS NOT NULL)) "
                    "FROM notifications n LEFT JOIN check_ins c ON c.id = n.check_in_id "
                    "WHERE n.delivered_via = 'held' "
                    "AND n.delivered_at IS NULL ORDER BY n.created_at FOR UPDATE OF n",
                    (LAPSE_NOTICE_BEFORE_S, now),
                )
            ).fetchall()
            if not rows:
                return 0
            over = [r[0] for r in rows if r[3]]
            if over:
                await conn.execute(
                    "UPDATE notifications SET delivered_at = %s WHERE id = ANY(%s)", (now, over)
                )
                _log.info("quiet_expired_dropped", count=len(over))
            held = [(i, kind, text) for i, kind, text, stale in rows if not stale]
            if not held:
                return len(over)
            for notification_id, kind, _ in held:
                # delivered as themselves: the briefing (contract: time trigger), the Sunday card
                if kind in AS_THEMSELVES or kind == "card":
                    await conn.execute(
                        "UPDATE notifications SET delivered_via = NULL, queued_at = %s WHERE id = %s",
                        (now, notification_id),
                    )
                    await conn.execute("SELECT pg_notify(%s, %s)", (OUTBOX_CHANNEL, str(notification_id)))
            others = [(i, text) for i, kind, text in held if kind not in AS_THEMSELVES and kind != "card"]
            if others:
                lang = settings["language"]
                summary = SUMMARY_PREFIX[lang] + " ".join(text for _, text in others)
                await enqueue(
                    conn,
                    kind="info",
                    text=fit(summary),
                    lang=lang,
                    now=now,
                    timezone=timezone,
                    respect_quiet=False,
                    path="quiet.summary",
                )
                await conn.execute(
                    "UPDATE notifications SET delivered_at = %s WHERE id = ANY(%s)",
                    (now, [i for i, _ in others]),
                )
        _log.info("quiet_released", held=len(held), summarized=len(others))
        return len(held)


class Dispatcher:
    """Delivers stored messages (API process only: it needs the live sockets)."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        live: LiveDevices,
        push: PushService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        unasked: Unasked | None = None,
        breakpoints: Breakpoints | None = None,
        *,
        owner: Owner,
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._live = live
        self._push = push
        # the rails for Pisa speaking first (TalkService.may_ask), set by the app: with no live
        # socket, the push itself asks to open a talk (category TALK_ASK)
        self.ask: Callable[[uuid.UUID, uuid.UUID, str, str], Awaitable[uuid.UUID | None]] | None = None
        self._clock = clock
        self._unasked = unasked or Unasked()  # what Pisa said unasked, per device (newest wins)
        self._breakpoints = breakpoints  # the API process's sockets: who is talking to Pisa now

    async def deliver(self, notification_id: uuid.UUID) -> str | None:
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT kind, text, lang, priority, device_id, queued_at, check_in_id, choices, "
                    "expects_reply, weekly, done, urgency, not_before, event_at, silent, speak, ledger_week, "
                    "ritual, sheet_id, ask_id, guardian, path, person_id, turn_id, summary "
                    "FROM notifications "
                    "WHERE id = %s AND delivered_via IS NULL FOR UPDATE SKIP LOCKED",
                    (notification_id,),
                )
            ).fetchone()
            if row is None:
                return None  # already delivered, held, or being delivered elsewhere
            if row[0] not in UNSTOPPED and await stopped_now(conn):
                return None  # Pisa's own initiative waits; it goes after the resume
            kind, text, lang, priority, target, queued_at, check_in_id, choices, expects_reply, weekly = row[
                :10
            ]
            done, urgency, not_before, event_at, unspoken, speak = (
                row[10],
                row[11],
                row[12],
                row[13],
                row[14],
                row[15],
            )
            ledger_week = row[16]  # the Sunday card
            ritual, sheet_id, ask_id = row[17], row[18], row[19]  # a ritual's sheet, Pisa's question in it
            asked = str(check_in_id) if check_in_id else (f"ask:{ask_id}" if ask_id else None)
            if not_before is not None and not_before > now:
                return None  # held for a breakpoint
            state, path, person_id, turn_id, summary = row[20], row[21], row[22], row[23], row[24]
            if state == "digest":
                return None  # the next digest takes it (GuardianService.tick)
            # the guardian's decision, for the push's interruption level (a re-delivery keeps
            # the one it had: `now`, or released at a breakpoint)
            decided = "now" if state == "now" else "breakpoint"
            pierce = False  # a floor or their "always": through the meeting's and the exchange's holds below
            hushed = False  # their "never" over a floor: shown, no voice, no sound
            if state in (None, "breakpoint"):
                # one gate for every unprompted word; nothing below goes around it
                talking = (
                    [d for d in await self._live_targets(conn, target) if self._breakpoints.busy(d)]
                    if self._breakpoints is not None
                    else []
                )
                item = guardian.notice_item(
                    notification_id, path, floor=form.is_floor(kind, urgency),
                    deadline=event_at or await guardian.ask_deadline(conn, ask_id), person_id=person_id,
                    kind=kind,
                )  # fmt: skip
                verdict = await guardian.gate(
                    conn,
                    item,
                    f"notification:{notification_id}",
                    now,
                    self._owner,
                    talking=bool(talking) if self._breakpoints is not None else None,
                    recheck=state == "breakpoint",  # a hold's re-check: quiet hours hold it
                )
                if not verdict.released:
                    await conn.execute(
                        "UPDATE notifications SET guardian = %s, not_before = %s WHERE id = %s",
                        (verdict.decision, verdict.until if verdict.decision == "breakpoint" else None,
                         notification_id),
                    )  # fmt: skip
                    if verdict.decision == "breakpoint":
                        for device_id in talking:  # the exchange's end releases it (Breakpoints)
                            self._breakpoints.wait(device_id, str(notification_id))  # type: ignore[union-attr]
                    # the exchange may have ended during the UPDATE's await (its
                    # breakpoint found nothing to release): decided again now, not after the hold
                    if not (talking and not any(self._breakpoints.busy(d) for d in talking)):  # type: ignore[union-attr]
                        return None
                    verdict = await guardian.gate(
                        conn, item, f"notification:{notification_id}", now, self._owner, talking=False
                    )
                    if not verdict.released:
                        return None
                    await conn.execute("UPDATE notifications SET not_before = NULL WHERE id = %s",
                                       (notification_id,))  # fmt: skip
                    not_before = None
                # only a floor and their "always" pierce a meeting; a high score
                # is still held by `_meeting_until` below
                pierce = verdict.reason in PIERCE
                hushed = verdict.silent
                decided = verdict.decision
                await conn.execute(
                    "UPDATE notifications SET guardian = %s WHERE id = %s",
                    ("now" if verdict.decision == "now" else "released", notification_id),
                )
            if (
                urgency == "breakpoint"
                and not pierce
                and (until := await _meeting_until(conn, now, queued_at, event_at))
            ):
                # Never during a meeting: said when it ends (the sweep delivers it then).
                await conn.execute(
                    "UPDATE notifications SET not_before = %s WHERE id = %s", (until, notification_id)
                )
                _log.info("notification_deferred", notification_id=str(notification_id), kind=kind)
                return None
            if urgency == "breakpoint" and not pierce and self._breakpoints is not None:
                talking = [d for d in await self._live_targets(conn, target) if self._breakpoints.busy(d)]
                if talking:  # the user is talking to Pisa: at the end of the exchange (stored, never lost)
                    await conn.execute(
                        "UPDATE notifications SET not_before = %s WHERE id = %s",
                        (now + BUSY_HOLD, notification_id),
                    )
                    for device_id in talking:
                        self._breakpoints.wait(device_id, str(notification_id))
                    # the exchange may have ended during the UPDATE's await (its breakpoint found
                    # nothing to release): then it goes now, not after BUSY_HOLD.
                    if any(self._breakpoints.busy(d) for d in talking):
                        _log.info("notification_held", notification_id=str(notification_id), kind=kind)
                        return None
                    await conn.execute("UPDATE notifications SET not_before = NULL WHERE id = %s",
                                       (notification_id,))  # fmt: skip
                    not_before = None
            if ask_id is not None and await _offer_lapsed(conn, ask_id, now):
                # a knock deferred past noon (a meeting, "ferma tutto") never goes
                await self._mark(conn, notification_id, "none", now)
                _log.info("notification_offer_lapsed", notification_id=str(notification_id))
                return "none"
            if now - (not_before or queued_at) > PENDING_MAX_AGE:
                await self._mark(conn, notification_id, "none", now)
                _log.warning("notification_expired", notification_id=str(notification_id), kind=kind)
                return "none"
            # RAIL: past the unprompted voice rate an item is shown, not spoken (floors always speak).
            # Counted and marked in this transaction, one delivery at a time: items
            # released together (a breakpoint, two outbox wake-ups) can't all count the same zero.
            floor = form.is_floor(kind, urgency)
            if not floor:
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.VOICE_RATE,))
            spoken = await _spoken_since(conn, now, self._owner.tz)
            unspoken = unspoken or hushed
            voice = form.voice_allowed(floor, *spoken) and not unspoken  # attend chose the phone
            aloud = False  # a device that speaks it got it live
            devices = await (
                await conn.execute(
                    "SELECT d.id, d.apns_token, d.apns_environment, p.app = 'background', "
                    "coalesce(p.companion = 'on' AND p.earbuds = 'connected', false), coalesce(p.glasses = "
                    "'on', false), r.device_id IS NOT NULL FROM devices d LEFT JOIN device_presence p ON "
                    "p.device_id = d.id LEFT JOIN device_push_relay r ON r.device_id = d.id WHERE "
                    "d.revoked_at IS NULL AND (%s::uuid IS NULL OR d.id = %s::uuid) ORDER BY d.created_at",
                    (target, target),
                )
            ).fetchall()
            # the wrist's line, Pisa's template for the kind (none at a sensitive place, health)
            withhold = wrist.withheld(
                here=await here(conn, now), place=str((done or {}).get("place") or "") or None, path=path,
                about=" ".join(str(x) for x in (summary, (done or {}).get("title"), text) if x) or None,
            )  # fmt: skip
            wrist_line = wrist.wrist(
                kind, lang, speak=speak, event_at=event_at, timezone=self._owner.tz, turn=turn_id is not None,
                ledger_week=ledger_week, ritual=ritual, weekly=weekly, path=path, withhold=withhold,
            )  # fmt: skip
            outgoing = OutgoingNotification(
                str(notification_id), kind, fit(text), lang, priority,
                str(check_in_id) if check_in_id else None, choices, expects_reply, weekly, done, urgency,
                speak=speak if not unspoken else None,  # never for what attend kept on the phone
                ritual=ritual, sheet_id=f"sheet:{sheet_id}" if sheet_id else None,
                ledger_week=ledger_week, wrist=wrist_line, turn_id=str(turn_id) if turn_id else None,
                sensitive=withhold,
            )  # fmt: skip
            used: list[str] = []
            # Resolved only when a device needs APNs: a broken key must never stop WebSocket delivery.
            sender: PushSender | None = None
            sender_resolved = False
            for device_id, token, environment, backgrounded, companion, glasses, relay in devices:
                silent = not voice and (companion or glasses)  # this device would have spoken it
                if silent and not unspoken:
                    _log.info("voice_rate_reached", notification_id=str(notification_id), kind=kind)
                try:
                    live = await self._live.notify(
                        device_id,
                        dataclasses.replace(outgoing, voice=False) if silent or hushed else outgoing,
                    )
                except Exception as exc:  # one channel failing never stops the others
                    live = False
                    _log.warning(
                        "notify_socket_failed", device_id=str(device_id), cause=exc.__class__.__name__
                    )
                if live:
                    used.append("ws")
                    self._unasked.said(device_id, asked)
                    aloud = aloud or (voice and (companion or glasses))
                    # Companion mode with earbuds: the app speaks it; no push as well (unless it
                    # isn't to be spoken: then the banner shows it).
                    if not backgrounded or (companion and not silent):
                        continue
                if not token and not relay:
                    continue
                if not relay and not sender_resolved:
                    sender_resolved = True
                    try:
                        sender = await self._push.sender(conn)
                    # e.g. the key no longer decrypts after a PISA_SECRETS_KEY change
                    except Exception as exc:
                        _log.error("push_key_unusable", cause=exc.__class__.__name__)
                if not relay and sender is None:
                    _log.warning("push_not_configured", notification_id=str(notification_id))
                    continue
                data = _push_data(kind, notification_id, check_in_id, choices, weekly, done, ledger_week)
                if ritual:  # the push opens the ritual's sheet (GET /v1/sheets/{sheet_id})
                    data |= {"ritual": ritual, **({"sheet_id": f"sheet:{sheet_id}"} if sheet_id else {})}
                if turn_id is not None:  # a REST turn's answer, and its sheet on the phone
                    data |= {
                        "turn_id": str(turn_id),
                        **({"sheet_id": f"sheet:{sheet_id}"} if sheet_id else {}),
                    }
                data["wrist"] = wrist_line
                # the watch shows the kind, never speaks it
                data["sensitive"] = withhold
                # one interruption level for every notice, from the guardian's decision
                category = _category(kind, choices)
                interruption: str = wrist.level(
                    "breakpoint" if path in LATE_ANSWERS else decided,
                    digest=path in DIGEST_PATHS,
                    silent=hushed,
                )  # a late answer's urgency is breakpoint: active, never time-sensitive
                ask = await self._ask(device_id, notification_id, speak, priority, unspoken, live, voice)
                if ask is not None:  # Pisa speaks first with Pisa closed: "Rispondi" opens the talk for it
                    data |= {"ask_id": str(ask), "why": speak}
                    category, interruption = "TALK_ASK", "time-sensitive"
                try:
                    if relay:  # sealed to the device's key: the relay sees no text (CONTRACT-2 §9)
                        apns: dict[str, object] = {"category": category, "threadId": kind,
                                                   "interruption": interruption}  # fmt: skip
                        if ask is not None:
                            apns |= {"askId": str(ask), "why": speak}
                        outcome = await self._push.send_relay_notice(
                            conn, device_id, notice_rules.value(_notice_fields(outgoing), self._owner.tag),
                            apns=apns,
                            collapse_id=str(notification_id),
                        )  # fmt: skip
                    else:
                        assert sender is not None
                        outcome = await sender.send(
                            token,
                            environment,
                            title="Pisa",
                            body=text,
                            thread_id=kind,
                            data=data,
                            push_id=str(notification_id),
                            category=category,
                            interruption=interruption,
                        )
                except Exception as exc:  # one device's failure never blocks the others
                    _log.warning("push_failed", device_id=str(device_id), cause=exc.__class__.__name__)
                    await self._unask(ask)
                    continue
                if outcome is None:  # the relay registration went meanwhile (mode none)
                    await self._unask(ask)
                    continue
                if outcome.status != "sent":
                    await self._unask(ask)  # never sent, never asked
                if outcome.status == "sent":
                    used.append("apns")
                    self._unasked.said(device_id, asked)
                    if ask is not None:  # pushed only once APNs took it
                        await conn.execute("UPDATE talk_asks SET pushed = true WHERE id = %s", (ask,))
                elif outcome.status == "token_invalid":
                    if not relay:  # a relay registration the relay forgot is dropped by the push service
                        await drop_token(conn, device_id)
                    _log.warning("push_token_dropped", device_id=str(device_id), cause=outcome.message)
                else:
                    _log.warning("push_failed", device_id=str(device_id), cause=outcome.message)
            via = "ws" if "ws" in used else "apns" if "apns" in used else "none"
            await self._mark(conn, notification_id, via, now)
            if aloud:  # counted before the lock is released (the session's own mark is then a no-op)
                await conn.execute(
                    "UPDATE notifications SET spoken_at = %s WHERE id = %s AND spoken_at IS NULL",
                    (now, notification_id),
                )
        _log.info(
            "notification_delivered",
            notification_id=str(notification_id),
            kind=kind,
            via=via,
            devices=len(used),
        )
        return via

    @staticmethod
    async def _live_targets(conn: AsyncConnection, target: uuid.UUID | None) -> list[uuid.UUID]:
        rows = await (
            await conn.execute(
                "SELECT id FROM devices WHERE revoked_at IS NULL AND (%s::uuid IS NULL OR id = %s::uuid)",
                (target, target),
            )
        ).fetchall()
        return [r[0] for r in rows]

    async def release(self, notification_ids: list[str]) -> None:
        """A breakpoint: what waited for it goes now (the hold or the "dopo" lifted)."""
        ids = [uuid.UUID(i) for i in notification_ids]
        async with self._pool.connection() as conn:
            await conn.execute(  # aged from now, not from when it was queued: a long hold isn't stale
                "UPDATE notifications SET not_before = %s WHERE id = ANY(%s) AND delivered_via IS NULL",
                (self._clock(), ids),
            )
        for notification_id in ids:
            await self.deliver_logged(notification_id)

    async def put_off(self, notification_id: str, seconds: float) -> bool:
        """ "Dopo": the notification is undelivered again, until the next breakpoint or
        `seconds` from now, once."""
        now = self._clock()
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "UPDATE notifications SET delivered_via = NULL, delivered_at = NULL, not_before = %s, "
                    "put_off_at = %s WHERE id = %s AND put_off_at IS NULL RETURNING id",
                    (now + timedelta(seconds=seconds), now, uuid.UUID(notification_id)),
                )
            ).fetchone()
        return row is not None

    async def _ask(
        self, device_id: uuid.UUID, notification_id: uuid.UUID, speak: str | None, priority: str,
        unspoken: bool, live: bool, voice: bool,
    ) -> uuid.UUID | None:  # fmt: skip
        """The ask this push carries, when Pisa may speak first (the rails of) and no live socket took it
        (a live one got `talk.ask_to_open`). Never for what attend kept on the phone: never
        past the voice rate (`voice` false: the item is shown, not spoken). A failure costs nothing but
        the ask. Marked `pushed` by the caller, once APNs took it."""
        if self.ask is None or not speak or unspoken or live or not voice:
            return None
        try:
            return await self.ask(device_id, notification_id, speak, priority)
        except Exception as exc:
            _log.warning("push_ask_failed", device_id=str(device_id), cause=exc.__class__.__name__)
            return None

    async def _unask(self, ask: uuid.UUID | None) -> None:
        """A TALK_ASK push APNs never took is no ask: it counts toward no hourly rate and the item
        may ask again (the unique ask per item would block the retry)."""
        if ask is None:
            return
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM talk_asks WHERE id = %s AND NOT pushed", (ask,))
        _log.info("push_ask_withdrawn", ask_id=str(ask))

    async def deliver_pending(self) -> None:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id FROM notifications WHERE delivered_via IS NULL ORDER BY queued_at"
                )
            ).fetchall()
        for (notification_id,) in rows:
            await self.deliver_logged(notification_id)

    async def deliver_logged(self, notification_id: uuid.UUID) -> None:
        """deliver() for background use: a failure is logged; the message stays pending for the next sweep."""
        try:
            await self.deliver(notification_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("notification_delivery_failed", notification_id=str(notification_id))

    @staticmethod
    async def _mark(conn: AsyncConnection, notification_id: uuid.UUID, via: str, now: datetime) -> None:
        await conn.execute(
            "UPDATE notifications SET delivered_via = %s, delivered_at = %s WHERE id = %s",
            (via, now, notification_id),
        )
        await guardian.delivered(conn, f"notification:{notification_id}", via, now)  # out, or never out


async def _spoken_since(conn: AsyncConnection, now: datetime, tz: str) -> tuple[int, int]:
    """The unprompted items spoken in the last hour and in the user's local day, floors aside."""
    day = now.astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    row = await (
        await conn.execute(
            "SELECT count(*) FILTER (WHERE spoken_at > %s), count(*) FILTER (WHERE spoken_at >= %s) "
            "FROM notifications WHERE spoken_at >= %s AND NOT (kind = 'reminder' AND urgency = 'now')",
            (
                now - timedelta(hours=1),
                day,
                min(now - timedelta(hours=1), day),
            ),  # never the whole store
        )
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


async def mark_spoken(pool: AsyncConnectionPool, notification_id: str, now: datetime) -> None:
    """The item was said aloud: it counts for the voice rate (what was spoken, not what was queued)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE notifications SET spoken_at = %s WHERE id = %s AND spoken_at IS NULL",
            (now, notification_id),
        )


async def _meeting_until(
    conn: AsyncConnection, now: datetime, queued_at: datetime, event_at: datetime | None
) -> datetime | None:
    """Until when the meeting the user is in now holds this notification, or None: Pisa doesn't speak up
    unasked in one. A meeting: a timed event with other people invited, or a meeting
    word in its title (a whole word), not declined, not Pisa's plan calendar, at most MEETING_MAX long
    ("Conference" over three days is no meeting). The hold lasts at most MEETING_HOLD_MAX from when the
    notification was queued, and never past the start of the event a reminder is about."""
    rows = await (
        await conn.execute(
            # a meeting Pisa created on their yes is their meeting: it holds notifications (courtesy, not
            # learning)
            "SELECT title, ends_at, coalesce(attendees, 0) FROM calendar_events WHERE status <> 'cancelled' "
            "AND NOT all_day AND starts_at <= %s AND ends_at > %s AND coalesce(accepted, true) "
            "AND ends_at - starts_at <= %s",
            (now, now, MEETING_MAX),
        )
    ).fetchall()
    ends = [end for title, end, others in rows if others > 0 or form.is_meeting(title)]
    if not ends:
        return None
    until = min(max(ends), queued_at + MEETING_HOLD_MAX)
    if event_at is not None and event_at <= until:  # a reminder for an event during the meeting: now
        return None
    return until if until > now else None


async def here(conn: AsyncConnection, now: datetime) -> tuple[str | None, str | None] | None:
    """Where they are now (their open stay's place name and category), for the wrist."""
    row = await (
        await conn.execute(
            "SELECT p.name, p.category FROM stays s JOIN places p ON p.id = s.place_id "
            "WHERE s.departed_at IS NULL AND s.last_seen_at > %s ORDER BY s.arrived_at DESC LIMIT 1",
            (now - HERE_WITHIN,),
        )
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _category(kind: str, choices: list[dict[str, str]] | None) -> str:
    """The APNs category, whose actions are the check-in's choices: one per check-in kind; a `done` has
    its Undo; every other notice is `NOTICE` (contract 9: the app's
    category with a dismiss action, so a swipe-away is reported as `dismissed`)."""
    if kind == "done":
        return "DONE"
    if kind != "check_in":
        return "NOTICE"
    ids = {c["id"] for c in choices or []}
    return "CHECK_IN_CONFIRM" if ids == {"done", "not_done"} else "CHECK_IN_MISSED"


def _notice_fields(outgoing: OutgoingNotification) -> dict[str, Any]:
    """The notice's v1 `notify` fields, as the socket sends them (`domain.notice` makes `pisa.notice`)."""
    fields: dict[str, Any] = {
        "notification_id": outgoing.id, "text": outgoing.text, "lang": outgoing.lang, "kind": outgoing.kind,
        "priority": outgoing.priority, "check_in_id": outgoing.check_in_id, "choices": outgoing.choices,
        "expects_reply": outgoing.expects_reply, "weekly": outgoing.weekly,
        "ledger_week": outgoing.ledger_week,
        "urgency": outgoing.urgency, "voice": outgoing.voice, "ritual": outgoing.ritual,
        "sheet_id": outgoing.sheet_id, "wrist": outgoing.wrist, "turn_id": outgoing.turn_id,
        "sensitive": outgoing.sensitive,
    }  # fmt: skip
    return fields | dict(outgoing.done or {})


def _push_data(
    kind: str,
    notification_id: uuid.UUID,
    check_in_id: uuid.UUID | None,
    choices: list[dict[str, str]] | None,
    weekly: bool | None = None,
    done: dict[str, object] | None = None,
    ledger_week: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {"kind": kind, "notification_id": str(notification_id)}
    if ledger_week:  # the Sunday card of that week: a tap opens its sheet
        data["ledger_week"] = ledger_week
    if check_in_id is not None:  # the actions of its CHECK_IN_* category answer it (ids = choice ids)
        data |= {"check_in_id": str(check_in_id), "choices": choices or []}
    if weekly:  # the weekly check is ready: a tap opens its card, as over the WebSocket
        data["weekly"] = True
    if done:  # what was closed and until when its Undo works, as over the WebSocket
        data |= {k: v for k, v in done.items() if v is not None}
    return data


async def listen_outbox(
    database_url: str, dispatcher: Dispatcher, *, retry_s: float = 1.0, sweep_s: float = 60.0
) -> None:
    """Deliver each message announced on pisa_outbox; on (re)connect and every minute, whatever is pending."""
    background: set[asyncio.Task[object]] = set()
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
                await conn.execute(f"LISTEN {OUTBOX_CHANNEL}")
                while True:
                    # On connect and every sweep_s, whatever is still pending is retried or expired.
                    await dispatcher.deliver_pending()
                    async for note in conn.notifies(timeout=sweep_s):
                        try:
                            notification_id = uuid.UUID(note.payload)
                        except ValueError:
                            continue
                        task = asyncio.create_task(dispatcher.deliver_logged(notification_id))
                        background.add(task)
                        task.add_done_callback(background.discard)
        except asyncio.CancelledError:  # stopped: the deliveries already started finish first
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            raise
        except Exception as exc:
            _log.warning("outbox_listener_failed", cause=exc.__class__.__name__, retry_s=retry_s)
            await asyncio.sleep(retry_s)


async def spoken_since(conn: AsyncConnection, now: datetime, tz: str) -> tuple[int, int]:
    """The voice rate's counts, for attend's validator."""
    return await _spoken_since(conn, now, tz)


async def _offer_lapsed(conn: AsyncConnection, ask_id: uuid.UUID, now: datetime) -> bool:
    """The knock's or the tragitto's question is no longer open (lapsed at noon, or answered)."""
    row = await (
        await conn.execute(
            "SELECT status <> 'open' OR expires_at <= %s FROM ritual_asks WHERE id = %s "
            "AND kind IN ('knock', 'tragitto')",
            (now, ask_id),
        )
    ).fetchone()
    return bool(row and row[0])


async def in_meeting(conn: AsyncConnection, now: datetime) -> bool:
    """Whether the user is in a meeting now, for attend's validator."""
    return await _meeting_until(conn, now, now, None) is not None
