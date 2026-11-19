"""Ports for Pisa's actions outside Pisa: changing the user's Google calendar and sending Gmail replies.
The adapters live in pisa.connectors.google; every call is made only by an approved executor, except
reading an event or a thread, and saving a draft (nothing leaves)."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MessageHeaders:
    """What a reply needs from the message it answers."""

    message_id: str  # the RFC 5322 Message-ID ("<…@mail.gmail.com>")
    references: str
    subject: str
    from_: str  # "Name <address>"
    to: str
    cc: str
    reply_to: str


class CalendarWriter(Protocol):
    async def event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        """The event as Google has it (organizer, attendees, start, end…); None when gone."""
        ...

    async def insert(self, calendar_id: str, body: dict[str, Any], send_updates: str) -> dict[str, Any]: ...

    async def patch(
        self,
        calendar_id: str,
        event_id: str,
        body: dict[str, Any],
        send_updates: str,
        etag: str | None = None,
    ) -> dict[str, Any]:
        """`etag`: If-Match: 412 (a ConnectorError) when the event changed since Pisa read it."""
        ...

    async def delete(
        self, calendar_id: str, event_id: str, send_updates: str, etag: str | None = None
    ) -> None: ...


class DraftMail(Protocol):
    async def headers(self, message_id: str) -> MessageHeaders: ...

    async def participants(self, thread_id: str) -> set[str]:
        """Every address in the thread's From, To and Cc (lower case)."""
        ...

    async def create(self, thread_id: str | None, raw: str) -> str:
        """A draft in the thread (a new email: none); its id."""
        ...

    async def update(self, draft_id: str, thread_id: str | None, raw: str) -> None: ...

    async def send(self, draft_id: str) -> str:
        """Send the draft; the sent message's id."""
        ...

    async def sent(self, message_id: str) -> bool:
        """The read-back: the provider holds this message as sent."""
        ...

    async def get(self, draft_id: str) -> str:
        """The draft as Gmail holds it now: base64url RFC 5322."""
        ...

    async def delete(self, draft_id: str) -> None:
        """Gone already is fine."""
        ...


class ActionsData(Protocol):
    """The conversation's actions outside Pisa. Each returns a sentence for the model (data): an approval
    asked and waiting, or what to say instead (several matches, a refusal and its reason)."""

    async def create_event(
        self, title: str, start: str, end: str | None, duration_min: int | None, place: str | None,
        calendar: str | None, owner_said: str | None = None, replaces: str | None = None, *,
        grounded_by: str | None = None, repeat: Any = None, until: str | None = None,
    ) -> str: ...  # fmt: skip

    async def move_event(
        self, event: str, start: str, day: str | None, *, owner_said: str | None = None
    ) -> str: ...

    async def cancel_event(self, event: str, day: str | None, owner_said: str | None = None) -> str: ...

    async def answer_proposal(self, approval: str, yes: bool, owner_said: str) -> str:
        """The user's yes or no to one of the proposals Pisa asked in their previous turn, in words
        code doesn't decide alone ("sì, solo il primo"): checked against their words this turn."""
        ...

    async def draft_reply(self, email: str, text: str) -> str: ...

    async def draft_email(self, to: str, subject: str, text: str) -> str: ...

    async def edit_draft(
        self, draft: str | None, text: str | None, add_recipient: str | None, remove_recipient: str | None
    ) -> str: ...

    async def send_draft(self, draft: str | None) -> str: ...

    async def answer(
        self,
        device_id: Any,
        words: str,
        lang: str,
        asked: list[str],
        acted: list[str] | None = None,
        proposed: list[str] | None = None,
        spoken: bool = True,
    ) -> str | None:
        """The user's short yes or no to the approval Pisa asked in their previous turn (`asked`), or
        their marked "no, alle 9" to a time Pisa just echoed (`acted`: what that turn changed):
        what Pisa says, or None when the words aren't an answer."""
        ...

    async def waiting(self, device_id: Any) -> list[dict[str, Any]]:
        """What Pisa asked this device and nobody answered (still pending, or expired unanswered): the
        approval rows with `expired`."""
        ...

    async def changed(self, device_id: Any, conversation_id: Any) -> list[dict[str, Any]]:
        """What Pisa changed for this device in this conversation (the ledger)."""
        ...

    async def read_out_now(self, ids: list[str]) -> None:
        """Pisa's question was said aloud just now (the talk): its cards are read out."""
        ...

    async def bundle(self, device_id: Any, asked: list[str], reply: str, spoken: bool = True) -> list[str]:
        """Of the approvals a turn asked, those its reply's one question names (one yes runs
        them)."""
        ...

    async def undo_last_change(self) -> str:
        """The user said to undo Pisa's last change ("rimettilo com'era"): undone, or why not."""
        ...
