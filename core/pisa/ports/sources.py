"""Ports for the information sources Pisa reads. Connectors implement them."""

from datetime import datetime
from typing import Protocol

from pisa.domain.calendar import CalendarInfo, EventChanges
from pisa.domain.mail import MailChanges, MailMessage


class HistoryExpired(Exception):
    """The mail source no longer has changes since this point: resynchronise from scratch."""


class SyncTokenExpired(Exception):
    """The calendar sync token is no longer valid: resynchronise from scratch."""


class MailSource(Protocol):
    async def history_point(self) -> str:
        """The current point in the mailbox's change history (taken before a full sync)."""
        ...

    async def recent(
        self, *, days: int, limit: int, skip: frozenset[str] = frozenset(), fetch: int | None = None
    ) -> list[MailMessage]:
        """Messages from the last `days` days, newest first, among the newest `limit`: those not in `skip`,
        at most `fetch` of them (all when None)."""
        ...

    async def changes_since(self, history_point: str) -> MailChanges:
        """Changes since `history_point`. Raises HistoryExpired when it is too old."""
        ...


class CalendarSource(Protocol):
    async def calendars(self) -> list[CalendarInfo]:
        """The calendars the user has selected (shown) in their calendar app."""
        ...

    async def events(
        self, calendar_id: str, *, sync_token: str | None, time_min: datetime, time_max: datetime
    ) -> EventChanges:
        """A full sync in [time_min, time_max) when `sync_token` is None, else the changes since the token.
        Raises SyncTokenExpired when the token is no longer valid."""
        ...
