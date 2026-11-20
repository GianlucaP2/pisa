"""What the handoff tools need from Pisa's store of preparations (pisa/services/handoffs.py)."""

import uuid
from typing import Any, Protocol


class HandoffsData(Protocol):
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
        """Keep one preparation and its buttons ({id, kind, title, url, phone}); returns its id."""
        ...

    async def open_preps(self) -> list[dict[str, Any]]:
        """The preparations of the last 24 hours the user hasn't said the outcome of, newest first:
        {id, purpose, target, details, opened, asked, created_at}."""
        ...

    async def settle(self, prep_id: str, outcome: str) -> dict[str, Any] | None:
        """Keep their word on a preparation (worked | failed), once; the preparation, or None when there is no
        open one by that id."""
        ...

    def asked_phone(self, device_id: uuid.UUID | None, phone: str, turn_id: str) -> None:
        """RAIL: Pisa asked the user (in this turn) to confirm a number that came from outside text."""
        ...

    def phone_confirmed(self, device_id: uuid.UUID | None, phone: str, yes: bool, turn_id: str) -> bool:
        """The number Pisa asked about in an earlier turn is confirmed by their yes this turn (`yes`), or was
        confirmed before."""
        ...
