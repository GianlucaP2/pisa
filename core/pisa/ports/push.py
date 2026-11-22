"""Port for sending a push notification to one device (implemented by the APNs connector)."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class PushOutcome:
    # token_invalid: drop the device's token (or relay registration); activity_invalid: the relay says a Live
    # Activity's token is dead (422): drop that activity's token only
    status: Literal["sent", "token_invalid", "activity_invalid", "failed"]
    message: str | None = None  # the real cause when not sent


class PushSender(Protocol):
    async def send(
        self,
        token: str,
        environment: str,
        *,
        title: str,
        body: str,
        thread_id: str,
        data: dict[str, object],
        push_id: str,
        category: str | None = None,
        interruption: str | None = None,
    ) -> PushOutcome: ...

    async def send_live_activity(
        self, token: str, environment: str, *, aps: dict[str, object], priority: int, push_id: str
    ) -> PushOutcome: ...


@dataclass(frozen=True)
class OutgoingNotification:
    id: str
    kind: str  # reminder | briefing | replan | alert | info | check_in | done
    text: str
    lang: str
    priority: str  # low | normal | urgent
    check_in_id: str | None = None  # kind check_in
    choices: list[dict[str, str]] | None = None
    expects_reply: bool | None = None
    weekly: bool | None = None  # the weekly check's offer: the app opens the weekly card
    done: dict[str, object] | None = None  # kind done: task_id | event_id, title, place, undo_until
    urgency: str = "breakpoint"  # now (said after the current sentence) | breakpoint
    voice: bool | None = None  # False = show it, don't speak it (past the unprompted voice rate)
    # why Pisa may say it first (leave_now, reply_waiting, approval_lapsing, check_in)
    speak: str | None = None
    ritual: str | None = None  # ghost_run | closing | week_poster | something_different
    sheet_id: str | None = None  # the ritual's sheet (`sheet:<uuid>`, GET /v1/sheets/{id})
    ledger_week: str | None = None  # the Sunday card of this ISO week is ready (the app opens its sheet)
    wrist: str = ""  # the wrist's line (`wrist.wrist`): Pisa's template, '' when withheld
    turn_id: str | None = None  # a REST turn's answer past its wait
    sensitive: bool = False  # a sensitive place or health: the kind only, never spoken


@dataclass(frozen=True)
class OutgoingApproval:
    """An approval to answer: what its interrupt (contract 2) or its push carries."""

    id: str
    kind: str
    summary: str
    details: list[str]
    requires_biometric: bool
    expires_at: str  # RFC 3339
    undo_minutes: int | None
    # the approval interrupt (CONTRACT-2 §6): the digest of `pisa-approval-digest/2`, how many it writes to or
    # notifies, this install's server id, and the language of its templated line
    digest: str | None = None
    recipients_count: int = 0
    server: str | None = None
    lang: str = "en-US"


class LiveDevices(Protocol):
    """The devices with a live WebSocket in the API process."""

    async def notify(self, device_id: object, notification: OutgoingNotification) -> bool:
        """Send `notify` on the device's live socket. False when it has none (or the send failed)."""
        ...

    async def approval(self, device_id: object, approval: OutgoingApproval) -> bool:
        """Send `approval_request` on the device's live socket. False when it has none (or the send
        failed)."""
        ...

    async def action_done(self, device_id: object, done: dict[str, Any]) -> bool:
        """Send `action_done` (approval_id, kind, summary, result, undo_until) on the device's live
        socket. False when it has none."""
        ...


@runtime_checkable
class ApprovalStates(Protocol):
    """The API process's live sockets, told when an approval is decided or closed (contract 2)."""

    async def approval_state(self, device_id: object | None, state: dict[str, Any]) -> int:
        """An approval decided or closed (`approval_id`, `state`, `by`): told to the contract 2 sockets that
        may hold its interrupt (the device's, or every one when the approval names none). How many were
        told."""
        ...


class Breakpoints(Protocol):
    """In the API process: whether the user is in an exchange with Pisa on a device, and the
    held notifications to deliver when it ends."""

    def busy(self, device_id: object) -> bool: ...

    def wait(self, device_id: object, notification_id: str) -> None:
        """Deliver this one at the device's next breakpoint (it is stored with `not_before` meanwhile)."""
        ...


class Outbox(Protocol):
    """The stored notifications the sockets release (at a breakpoint) or put off ("dopo")."""

    async def release(self, notification_ids: list[str]) -> None: ...

    async def put_off(self, notification_id: str, seconds: float) -> bool:
        """Undelivered again until `seconds` from now (or the next breakpoint); False when already put off."""
        ...


class LiveSlot:
    """Where the API process puts its live sockets (`Runtime.live`), handed to the services that deliver
    first on a live socket (the approvals) when they are built. The sockets exist only once the API has
    started, after every service: the services read the slot at delivery time, never a runtime they were
    built before. Empty in the worker."""

    def __init__(self) -> None:
        self.devices: LiveDevices | None = None

    def get(self) -> LiveDevices | None:
        return self.devices
