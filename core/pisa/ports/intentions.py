"""What the brain's intention tools read and write (`pisa/services/intentions.py`)."""

import uuid
from datetime import date
from typing import Any, Protocol


class IntentionsData(Protocol):
    async def keep(
        self,
        *,
        kind: Any = "mine",
        what: str | None,
        next_step: str | None = None,
        why: str | None = None,
        who: str | None = None,
        cue: Any = "none",
        due_at: str | None = None,
        due_kind: str | None = None,
        place: tuple[float, float, str, str | None] | None = None,
        event: Any = None,
        cue_ref: str | None = None,
        repeat: dict[str, Any] | None = None,
        due_on: date | None = None,
        doc_kind: str | None = None,
        lang: str | None = None,
        device_id: uuid.UUID | None = None,
        ask: bool = True,
    ) -> dict[str, Any]:
        """An intention kept at once with what it has; `question` the one piece to ask, or None."""
        ...

    async def answer(
        self,
        intention_id: str,
        *,
        what: str | None = None,
        next_step: str | None = None,
        why: str | None = None,
        last_step: str | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Their answer to the one question, or a step done; the packet after it."""
        ...

    async def confirm(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """Their yes to a proposed promise."""
        ...

    async def done(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """ "Fatto": closed (a repeating one goes on)."""
        ...

    async def archive(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """Put away, never deleted."""
        ...

    async def listed(
        self, kind: Any = None, states: tuple[str, ...] = ("proposed", "open")
    ) -> list[dict[str, Any]]:
        """The open and proposed intentions (or one kind)."""
        ...

    async def find(self, words: str, kind: Any = None) -> list[dict[str, Any]]:
        """The open intentions their words name, best first."""
        ...

    async def packet(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """The resumption packet."""
        ...
