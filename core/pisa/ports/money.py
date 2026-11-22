"""What the brain's money tools need from pisa/services/money_back.py."""

import uuid
from typing import Any, Protocol

from pisa.domain.flight_claims import Disruption
from pisa.domain.trips import Leg


class MoneyData(Protocol):
    async def claims(self) -> list[dict[str, Any]]: ...

    async def refunds(self) -> list[dict[str, Any]]: ...

    async def subscriptions(self, lang: str | None = None) -> list[dict[str, Any]]: ...

    async def bill_checks(self) -> list[dict[str, Any]]: ...

    async def promises(self) -> list[dict[str, Any]]: ...

    async def supplies(self, lang: str | None = None) -> list[dict[str, Any]]: ...

    async def warranties(self, q: str | None = None) -> list[dict[str, Any]]: ...

    async def flight_by_words(self, flight: str | None) -> tuple[uuid.UUID, Leg] | None:
        """The flight leg of their trips they name (else their latest flight that left)."""
        ...

    async def said_claim(
        self, leg: Leg, trip_id: uuid.UUID | None, disruption: Disruption | None, delay_min: int | None
    ) -> dict[str, Any]:
        """The claim their own words make (or the one Pisa holds, `disruption` None); raises PisaError
        not_found."""
        ...

    async def claim_draft(self, claim_id: str, lang: str) -> dict[str, Any]: ...

    async def bill_draft(self, check_id: str, lang: str) -> dict[str, Any]: ...

    async def add_purchase(
        self,
        *,
        item: str,
        purchased_on: str,
        seller: str | None,
        delivered_on: str | None = None,
        region: str | None = None,
        source: str,
    ) -> dict[str, Any]: ...

    async def mark_used(self, name: str, *, keep: bool) -> dict[str, Any] | None: ...

    async def cancel_buttons(self, sub_id: str, lang: str) -> list[dict[str, Any]]: ...
