"""The device sessions of this process: one live session per device.

`SessionRegistry` is the `LiveDevices` port the dispatcher and the approvals use: a new connection closes
the old one (4001), a revoked device's closes at once (4003), and what Pisa says unasked goes to the device's
session (spoken first when it should be, or inside its open talk). A `/v2/runs` run is held too, for its
length, under a key of its own (`register_run`): it never replaces the device's socket nor is replaced by it,
gets nothing unasked, and is ended like the socket when the device is revoked.
`listen_for_revocations` closes the sessions of devices revoked in another process, and tells devices
when the AI disclosure changed anywhere.
"""

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg

from pisa.brain.conversation import SentenceSplitter
from pisa.db.errors import UNREACHABLE
from pisa.domain import talk as form
from pisa.logging import get_logger
from pisa.ports.push import Outbox, OutgoingApproval, OutgoingNotification
from pisa.services import devices, disclosure, notifications
from pisa.services.background import Background
from pisa.services.devices import REVOKED_CHANNEL
from pisa.services.session import CLOSE_REPLACED, DeviceSession, done_fields, say_audio_end
from pisa.services.session_messages import ActionDone, Notify, SayAudio, TalkAskToOpen
from pisa.services.voice import Speaker, VoiceService, b64

if TYPE_CHECKING:
    from pisa.services.talk import TalkRun

_log = get_logger("pisa.sessions")
_background = Background()


class SessionRegistry:
    """One live session per device, in this process (there is one API process)."""

    def __init__(self, voice: VoiceService | None = None) -> None:
        self._live: dict[uuid.UUID, DeviceSession] = {}
        # the device's runs over HTTP in flight (`/v2/runs`): a key of their own beside its one socket
        self._runs: dict[uuid.UUID, set[DeviceSession]] = {}
        self._voice = voice
        self.outbox: Outbox | None = None  # the stored notifications (set by the app: the dispatcher)

    def session(self, device_id: uuid.UUID) -> "DeviceSession | None":
        """The device's live socket, when it has one (the thinker shows what it finds there)."""
        session = self._live.get(device_id)
        return session if session is not None and not session.closed else None

    def busy(self, device_id: object) -> bool:
        """Breakpoints: the user is in an exchange with Pisa on this device."""
        session = self._live.get(device_id)  # type: ignore[arg-type]
        return session is not None and not session.closed and session.busy()

    def wait(self, device_id: object, notification_id: str) -> None:
        session = self._live.get(device_id)  # type: ignore[arg-type]
        if session is not None:
            session.wait(notification_id)

    async def notify(self, device_id: object, notification: OutgoingNotification) -> bool:
        """The delivery rule's first choice: `notify` on the device's live socket (LiveDevices port), then its
        audio (`say_audio` with the notification_id) in the background. A `now`
        item opens with "Scusa, un attimo:"; a `breakpoint` one the dispatcher holds while the user talks to
        Pisa (stored: Breakpoints)."""
        session = self._live.get(device_id)  # type: ignore[arg-type]
        if session is None or session.closed:
            return False
        if notification.speak and notification.voice is not False:
            notification = await self._speak_first(session, notification)
        elif notification.voice is not False and (run := session.talking()) is not None:
            notification = await self._in_talk(session, run, notification)
        if notification.urgency == "now":
            notification = dataclasses.replace(
                notification, text=form.interrupting(notification.text, notification.lang)
            )
        return await self.send_notify(session, notification)

    async def _speak_first(
        self, session: "DeviceSession", notification: OutgoingNotification
    ) -> OutgoingNotification:
        """Pisa says it by its own voice. In an open talk: said in the talk (leave-now at once, the
        rest at the next pause); with none, `talk.ask_to_open` under the rails (TalkService.may_ask). Either
        way its `notify` comes with `voice: false` (shown; the talk says it). Unchanged when neither
        applies."""
        runtime, speak = session.runtime, notification.speak or ""
        try:
            run = runtime.talk.run_for(session.device.id)
            if run is not None:
                await run.proactive(notification.text, speak)
                return dataclasses.replace(notification, voice=False)
            ask = await runtime.talk.may_ask(
                session.device.id, uuid.UUID(notification.id), speak, notification.priority
            )
            if ask is None:
                return notification
            sent = await session.send(
                TalkAskToOpen(
                    id=str(ask),
                    why=speak,  # type: ignore[arg-type]
                    line=notification.text,
                    lang=notification.lang,  # type: ignore[arg-type]
                    notification_id=notification.id,  # one alert, not two
                )
            )
        except UNREACHABLE:
            _log.error("database_unreachable", device_id=str(session.device.id), during="speak_first")
            return notification
        except Exception as exc:  # speaking first never costs the notice itself
            _log.warning("speak_first_failed", device_id=str(session.device.id), cause=exc.__class__.__name__)
            return notification
        return dataclasses.replace(notification, voice=False) if sent else notification

    async def _in_talk(
        self, session: "DeviceSession", run: "TalkRun", notification: OutgoingNotification
    ) -> OutgoingNotification:
        """While a
        talk is open, a notice that would be said aloud (companion mode with earbuds, the glasses) is said in
        the talk, at the next pause; either way its `notify` comes with `voice: false` (shown, never a second
        voice)."""
        try:
            if self._voice is not None and await self._voice.spoken_aloud(session.device.id):
                await run.proactive(notification.text, "notice")
                await notifications.mark_spoken(
                    session.runtime.pool, notification.id, session.runtime.clock()
                )
        except UNREACHABLE:
            _log.error("database_unreachable", device_id=str(session.device.id), during="notify_in_talk")
        except Exception as exc:  # the notice itself always goes
            _log.warning(
                "notify_in_talk_failed", device_id=str(session.device.id), cause=exc.__class__.__name__
            )
        return dataclasses.replace(notification, voice=False)

    async def send_notify(self, session: "DeviceSession", notification: OutgoingNotification) -> bool:
        session.last_unprompted = (notification, time.monotonic())
        sent = await session.send(
            Notify(
                notification_id=notification.id,
                text=notification.text,
                lang=notification.lang,  # type: ignore[arg-type]
                kind=notification.kind,  # type: ignore[arg-type]
                priority=notification.priority,  # type: ignore[arg-type]
                check_in_id=notification.check_in_id,  # type: ignore[arg-type]
                choices=notification.choices,  # type: ignore[arg-type]
                expects_reply=notification.expects_reply,
                weekly=notification.weekly,
                ledger_week=notification.ledger_week,
                urgency=notification.urgency,  # type: ignore[arg-type]
                voice=notification.voice,
                ritual=notification.ritual,  # type: ignore[arg-type]
                sheet_id=notification.sheet_id,
                wrist=notification.wrist,
                turn_id=notification.turn_id,
                sensitive=notification.sensitive,
                **done_fields(notification.done),
            )
        )
        talking = session.talking() is not None  # a talk's talker is the one voice
        if sent and self._voice is not None and notification.voice is not False and not talking:
            _background.start(self._speak(self._voice, session, notification))
        return sent

    async def _speak(
        self, voice: VoiceService, session: "DeviceSession", notification: OutgoingNotification
    ) -> None:
        """The notify's audio, only when the phone will speak it rather than show a banner; said aloud,
        it counts for the unprompted voice rate."""
        try:
            if not await voice.spoken_aloud(session.device.id):
                return
            await notifications.mark_spoken(session.runtime.pool, notification.id, session.runtime.clock())
        except UNREACHABLE:
            _log.error("database_unreachable", device_id=str(session.device.id), during="notify_audio")
            return
        if not session.audio_formats:
            return  # the phone speaks the text itself

        async def send_audio(
            n: int, data: bytes, final: bool, fmt: str, rate: int, spoken: int | None, end: bool
        ) -> bool:
            audio = {"notification_id": notification.id, "seq": n, "format": fmt, "sample_rate": rate}
            return await session.send(
                SayAudio.model_validate(audio | {"data": b64(data)} | say_audio_end(final, spoken, end))
            )

        speaker = Speaker(voice, send_audio, notification.id, session.audio_formats)
        splitter = SentenceSplitter()
        for sentence in splitter.feed(notification.text):
            speaker.feed(sentence)
        speaker.feed(splitter.rest())
        speaker.finish()
        await speaker.done()

    async def approval(self, device_id: object, approval: OutgoingApproval) -> bool:
        """The approval on the device's live socket (an interrupt on `/v2/live`)."""
        session = self._live.get(device_id)  # type: ignore[arg-type]
        if session is None or session.closed:
            return False
        return await session.approval(approval)

    async def approval_state(self, device_id: object | None, state: dict[str, Any]) -> int:
        """`pisa.approval.state` to the device's live socket, or to every one when the approval names no
        device (each may hold its interrupt)."""
        if device_id is None:
            sessions = list(self._live.values())
        else:
            sessions = [s for s in [self._live.get(device_id)] if s is not None]  # type: ignore[call-overload]
        told = 0
        for session in sessions:
            if not session.closed and await session.approval_state(state):
                told += 1
        return told

    def tell(self, name: str, value: dict[str, Any], only: set[uuid.UUID] | None = None) -> list[uuid.UUID]:
        """A contract 2 Ring 0 event to the live sockets that speak it (of `only`'s devices); the devices
        told."""
        return [
            d for d, s in list(self._live.items())
            if (only is None or d in only) and not s.closed and s.tell(name, value)
        ]  # fmt: skip

    async def action_done(self, device_id: object, done: dict[str, Any]) -> bool:
        """`action_done` on the device's live socket."""
        session = self._live.get(device_id)  # type: ignore[arg-type]
        if session is None or session.closed:
            return False
        until = done.get("undo_until")
        return await session.send(
            ActionDone(
                approval_id=done["approval_id"],
                kind=done["kind"],
                summary=done["summary"],
                result=done["result"][:500],
                undo_until=datetime.fromisoformat(until) if until else None,
            )
        )

    async def register(self, session: DeviceSession) -> None:
        old = self._live.get(session.device.id)
        self._live[session.device.id] = session
        if old is not None:
            _log.info("device_session_replaced", device_id=str(session.device.id))
            await old.close(CLOSE_REPLACED, "replaced by a newer connection from the same device")

    def unregister(self, session: DeviceSession) -> bool:
        """True when this was the device's live session (not one already replaced by a newer one)."""
        if self._live.get(session.device.id) is session:
            del self._live[session.device.id]
            return True
        return False

    def register_run(self, session: DeviceSession) -> None:
        """A run over HTTP, held for its length: beside the device's socket and its other runs, replacing
        none."""
        self._runs.setdefault(session.device.id, set()).add(session)

    def unregister_run(self, session: DeviceSession) -> None:
        runs = self._runs.get(session.device.id)
        if runs is not None:
            runs.discard(session)
            if not runs:
                del self._runs[session.device.id]

    def runs(self, device_id: uuid.UUID) -> "set[DeviceSession]":
        """The device's runs over HTTP in flight."""
        return set(self._runs.get(device_id, ()))

    async def revoke(self, device_id: uuid.UUID) -> None:
        """The device's socket and its runs in flight each get `device_revoked`, then end."""
        session = self._live.get(device_id)
        if session is not None:
            _log.info("device_session_revoked", device_id=str(device_id))
            await session.revoke()
        for run in list(self._runs.get(device_id, ())):
            if not run.closed:
                _log.info("device_run_revoked", device_id=str(device_id))
                await run.revoke()

    async def close_revoked(self, conn: psycopg.AsyncConnection) -> None:
        """Close sessions of devices revoked while nobody was listening (e.g. the listener reconnected)."""
        held = list(self._live.keys() | self._runs.keys())
        if not held:
            return
        for device_id, is_revoked in (await devices.revoked(conn, held)).items():
            if is_revoked:
                await self.revoke(device_id)


async def listen_for_revocations(
    database_url: str,
    registry: SessionRegistry,
    *,
    on_disclosure: Callable[[], Awaitable[None]] | None = None,
    retry_s: float = 1.0,
) -> None:
    """LISTEN for `pisa devices revoke` (another process); close the device's sockets at once (4003). With
    `on_disclosure`, LISTEN too for a slot or model server changed in any process (`disclosure.changed`, the
    CLI's `pisa models set` included) and tell the connected devices (`disclosure.tell`)."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
                await conn.execute(f"LISTEN {REVOKED_CHANNEL}")
                if on_disclosure is not None:
                    await conn.execute(f"LISTEN {disclosure.CHANGED_CHANNEL}")
                    _background.start(_told(on_disclosure))  # a change made while nobody was listening
                await registry.close_revoked(conn)
                async for note in conn.notifies():
                    if note.channel == disclosure.CHANGED_CHANNEL and on_disclosure is not None:
                        _background.start(_told(on_disclosure))
                        continue
                    try:
                        device_id = uuid.UUID(note.payload)
                    except ValueError:
                        _log.warning("revocation_payload_invalid")
                        continue
                    # Closing a socket can wait on the network: never hold up the next notification.
                    _background.start(registry.revoke(device_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("revocation_listener_failed", cause=exc.__class__.__name__, retry_s=retry_s)
            await asyncio.sleep(retry_s)


async def _told(on_disclosure: Callable[[], Awaitable[None]]) -> None:
    try:
        await on_disclosure()
    except Exception as exc:  # the next change, or the device's next connection, tells it
        _log.warning("disclosure_tell_failed", cause=exc.__class__.__name__)
