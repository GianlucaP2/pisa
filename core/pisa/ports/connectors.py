"""What the brain's and the night's source tools read (`pisa/services/connectors.py`), and the provider
interfaces behind the connectors: adding a bank, a sport platform or a mailbox is a provider, not a new
path."""

from datetime import date, datetime
from typing import Any, Protocol


class SourcesData(Protocol):
    async def health_summary(self, days: int, lang: str) -> dict[str, Any]:
        """Their health and sport days (derived numbers only), described against their own earlier days."""
        ...

    async def money_recent(self, days: int, lang: str) -> dict[str, Any]:
        """Their bank movements (read-only), the counterparties as the bank wrote them (outside text)."""
        ...

    async def obligations(self, window_days: int, lang: str) -> dict[str, Any]:
        """The admin radar's obligations due in the window; flagged messages counted, never tracked."""
        ...


class SportProvider(Protocol):
    """A sport or health platform the server pulls from (Garmin today; Polar, Withings: the next ones)."""

    async def token(self, secret: dict[str, str]) -> tuple[str, datetime]:
        """An access token from their credential, and when it expires."""
        ...

    async def days(self, token: str, start: date, end: date) -> list[dict[str, Any]]:
        """Their daily summaries (sleep, steps, heart, workouts) between the dates, as `DaySummary` dicts."""
        ...


class BankProvider(Protocol):
    """A licensed PSD2 provider (Enable Banking today): read-only, never a payment."""

    async def start(
        self, secret: dict[str, str], settings: dict[str, Any], valid_until: datetime, state: str
    ) -> dict[str, Any]:
        """The bank's consent page: {"url", "authorization_id"}."""
        ...

    async def session(self, secret: dict[str, str], code: str) -> dict[str, Any]:
        """The consent's session from the bank's code: {"session_id", "accounts", "valid_until"}."""
        ...

    async def transactions(
        self, secret: dict[str, str], account_uid: str, since: date
    ) -> list[dict[str, Any]]: ...


class Mailbox(Protocol):
    """A read-only mailbox (the PEC over IMAP): messages since a day, never marked read, never changed."""

    async def since(
        self, secret: dict[str, str], settings: dict[str, Any], day: date
    ) -> list[dict[str, Any]]:
        """[{ref, from_address, subject, text, sent_at}]"""
        ...


class TaxDocuments(Protocol):
    """The AdE's cassetto fiscale API (Provv. 200918, 6 Jul 2026): what it allows, read-only."""

    async def documents(self, secret: dict[str, str], years: int) -> list[dict[str, Any]]: ...
