"""The Live Activity "Next" on the lock screen and the Watch: its APNs pushes from the latest
plan.

Each minute, for every device that registered a Live Activity token:
- **start** (push-to-start token), from the planner day's start while there is a plan and no running activity:
  once, and again after the app deleted the `update` token (the activity ended or was dismissed);
- **update** (the running activity's token) when the next item or its leave time changed, at most once a
  minute; priority 10 when the next stop or leave time changed, else 5. While the app has an `update` token
  the server is the only producer of the content (the app no longer updates it locally);
- **end** at the planner day's end, once (the activity's token is then forgotten: the activity is over).
A token APNs says is dead is dropped. Never logs titles or places.
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import guardian as guardian_rules
from pisa.domain import live_activity as rules
from pisa.domain import push_contract
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.ports.push import PushOutcome
from pisa.services import guardian
from pisa.services.follow import FollowService
from pisa.services.planner import PlannerService, window
from pisa.services.push import PushService

THROTTLE = timedelta(minutes=1)
TICK_SLACK = timedelta(seconds=5)  # minute ticks drift: 59.5 s after the last update still counts as a minute
_log = get_logger("pisa.live_activity")


class LiveActivityService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        push: PushService,
        planner: PlannerService,
        follow: FollowService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._push = push
        self._planner = planner
        self._follow = follow
        self._owner = owner
        self._clock = clock

    async def set_token(self, device_id: uuid.UUID, token: str, kind: str) -> None:
        low, high = push_contract.ACTIVITY_TOKEN_HEX
        hex_token = push_contract.hex_token(token, (low, high))
        if hex_token is None:  # the schema takes any string: the rule is enforced here
            raise PisaError(
                "validation_failed",
                f"The token must be the Live Activity push token in hex ({low} to {high} hex digits).",
                "Send pushTokenUpdates (or pushToStartTokenUpdates) as hex",
            )
        token = hex_token
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO device_live_activity (device_id, kind, token, updated_at) VALUES (%s, %s, %s, "
                "%s) "
                "ON CONFLICT (device_id, kind) DO UPDATE SET token = EXCLUDED.token, updated_at = "
                "EXCLUDED.updated_at",
                (device_id, kind, token, self._clock()),
            )
        _log.info("live_activity_token", device_id=str(device_id), kind=kind)

    async def delete_token(self, device_id: uuid.UUID, kind: str, reason: str = "dismissed") -> None:
        """The app deletes the `update` token when the activity ends or is dismissed. Dismissed by the user:
        off for the rest of the day. Ended by iOS (its 8-hour limit): Pisa may start it again the same day,
        without an alert (verification)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM device_live_activity WHERE device_id = %s AND kind = %s", (device_id, kind)
            )
            if kind == "update" and reason == "ended":
                await conn.execute(
                    "UPDATE live_activity_state SET started_on = NULL, content = NULL WHERE device_id = %s",
                    (device_id,),
                )

    # --- each minute --------------------------------------------------------------------------------------

    async def tick(self) -> int:
        """The pushes due now (see the module docstring). Returns how many went out."""
        now = self._clock()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT d.id, d.apns_environment, u.token, s.token, st.content, st.sent_at, "
                    "st.started_on, st.ended_on, r.device_id IS NOT NULL FROM devices d LEFT JOIN "
                    "device_live_activity u ON u.device_id = d.id AND u.kind = 'update' LEFT JOIN "
                    "device_live_activity s ON s.device_id = d.id AND s.kind = 'start' LEFT JOIN "
                    "live_activity_state st ON st.device_id = d.id LEFT JOIN device_push_relay r ON "
                    "r.device_id = d.id WHERE d.revoked_at IS NULL AND (d.apns_environment IS NOT NULL OR "
                    "r.device_id IS NOT NULL) AND (u.token IS NOT NULL OR s.token IS NOT NULL)"
                )
            ).fetchall()
            if not rows:
                return 0
            settings = await self._owner.settings(conn)
            sender = await self._push.sender(conn) if not all(r[8] for r in rows) else None
        if sender is None and not any(r[8] for r in rows):
            return 0  # no APNs key and no relay registration: nothing can go out
        win = window(settings["planner"], now, self._owner.tz)
        plan = await self._planner.current(win.day)  # the lock screen never shows a task gone since
        content = await self._content(plan, now, settings["language"], win.day_end) if plan else None
        sent = 0
        for device_id, environment, update, start, last, sent_at, started_on, ended_on, relay in rows:
            if not relay and sender is None:
                continue
            push: tuple[str, str, dict[str, Any], int] | None = None  # (event, token, aps, priority)
            stamp = int(now.timestamp())
            if update and now >= win.day_end.astimezone(UTC) and ended_on != win.day:
                state = content or rules.content_state([], now, {}, settings["language"], win.day_end)
                aps = {"timestamp": stamp, "event": "end", "content-state": state, "dismissal-date": stamp}
                push = ("end", update, aps, 10)
            elif update and content is not None and content != last:
                if sent_at is None or now - sent_at >= THROTTLE - TICK_SLACK:
                    aps = {"timestamp": stamp, "event": "update", "content-state": content}
                    push = ("update", update, aps, rules.priority(last, content))
            elif (
                not update and start and content is not None and started_on != win.day
                and win.day_start.astimezone(UTC) <= now < win.day_end.astimezone(UTC)
            ):  # fmt: skip
                # input-push-token: the phone reports the new activity's update token (then: updates only).
                aps = {"timestamp": stamp, "event": "start", "content-state": content,
                       "attributes-type": rules.ATTRIBUTES_TYPE, "attributes": {},
                       "input-push-token": 1}  # fmt: skip
                if sent_at is None or sent_at.astimezone(self._owner.zone).date() != win.day:
                    aps["alert"] = {"title": "Pisa", "body": content["title"]}  # the day's first start only
                push = ("start", start, aps, 10)
            if push is None:
                continue
            event, token, aps, prio = push
            # the one gate. The day's first start carries an alert (`live.start`):
            # held, it starts silently; every other push is the lock screen's state (`live.state`), decided,
            # never kept.
            name = "live.start" if "alert" in aps else "live.state"
            async with self._pool.connection() as conn:
                verdict = await guardian.gate(
                    conn, guardian_rules.Item(f"{device_id}:{win.day}", guardian_rules.PATHS[name]),
                    f"live:{device_id}:{win.day}" if name == "live.start" else f"live:{uuid.uuid4()}", now,
                    self._owner, keep=name == "live.start",
                )  # fmt: skip
            if not verdict.released:
                aps.pop("alert", None)
            if event != "end" and (stale := rules.stale_at(content)) is not None:
                aps["stale-date"] = stale  # past the leave time (else the end), the widget says "late"
            if relay:  # nothing of the user's day through the relay: no text, times or progress
                activity = rules.relay_activity(token, event, prio, stamp)
                async with self._pool.connection() as conn:
                    relayed = await self._push.send_relay_activity(conn, device_id, activity)
                outcome = relayed or PushOutcome("failed", "The relay registration is gone.")
                if outcome.status == "token_invalid":  # the relay's handle, not the activity's token
                    outcome = PushOutcome("failed", outcome.message)
                elif outcome.status == "activity_invalid":  # 422: this activity's token alone is dead
                    outcome = PushOutcome("token_invalid", outcome.message)
            else:
                assert sender is not None
                outcome = await sender.send_live_activity(
                    token, environment, aps=aps, priority=prio, push_id=str(uuid.uuid4())
                )
            async with self._pool.connection() as conn:
                await self._record(conn, device_id, event, outcome.status, content, now, win.day, token)
            sent += outcome.status == "sent"
            _log.info("live_activity_push", device_id=str(device_id), apns_event=event, status=outcome.status,
                      priority=prio)  # fmt: skip
        return sent

    async def _record(
        self, conn: AsyncConnection, device_id: uuid.UUID, event: str, status: str, content: Any,
        now: datetime, day: Any, token: str,
    ) -> None:  # fmt: skip
        if status == "token_invalid":
            await conn.execute(
                "DELETE FROM device_live_activity WHERE device_id = %s AND token = %s", (device_id, token)
            )
            return
        if status != "sent":
            return
        await conn.execute(
            "INSERT INTO live_activity_state (device_id) VALUES (%s) ON CONFLICT DO NOTHING", (device_id,)
        )
        if event == "end":
            await conn.execute(
                "UPDATE live_activity_state SET ended_on = %s, content = NULL, sent_at = %s WHERE device_id "
                "= %s",
                (day, now, device_id),
            )
            await conn.execute(  # the activity is over: its token with it
                "DELETE FROM device_live_activity WHERE device_id = %s AND kind = 'update'", (device_id,)
            )
            return
        started = ", started_on = %s" if event == "start" else ""
        params: tuple[Any, ...] = (Jsonb(content), now, *((day,) if event == "start" else ()), device_id)
        await conn.execute(
            f"UPDATE live_activity_state SET content = %s, sent_at = %s{started} WHERE device_id = %s",  # noqa: S608
            params,
        )

    async def _content(
        self, plan: dict[str, Any], now: datetime, lang: str, day_end: datetime
    ) -> dict[str, Any]:
        """The next item of the plan, with today's statuses."""
        started = time.monotonic()
        overlaid = await self._follow.overlay(plan) or plan
        ids = [i["place_id"] for i in overlaid.get("items", []) if i.get("place_id")]
        places: dict[str, str] = {}
        if ids:
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT id::text, name, address FROM places WHERE id = ANY(%s::uuid[])", (ids,)
                    )
                ).fetchall()
            places = {pid: (address or name).split(",")[0].strip() for pid, name, address in rows}
        content = rules.content_state(
            overlaid.get("items", []), now, places, lang, day_end.astimezone(self._owner.zone)
        )
        _log.debug("live_activity_content", ms=int((time.monotonic() - started) * 1000))
        return content
