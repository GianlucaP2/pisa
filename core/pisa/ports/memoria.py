"""What the brain's Memoria tools and the turn's preferences block read
(`services/memoria.MemoriaService`)."""

import uuid
from collections.abc import Callable
from typing import Any, Literal, Protocol


class MemoriaData(Protocol):
    async def for_turn(self) -> tuple[list[dict[str, Any]], int]:
        """The active instructions within the caps ({id, text, …}, oldest first) and how many notes
        they keep."""
        ...

    async def remember(
        self,
        text: str,
        *,
        owner_words: str,
        outside_now: bool,
        device: uuid.UUID | None = None,
        lang: str,
        asked: Callable[[str], None] | None = None,
        turn_id: str | None = None,
    ) -> str:
        """One standing instruction: their words kept at once, Pisa's after their yes; the model's
        sentence."""
        ...

    async def forget(self, which: str, *, owner_words: str, outside_now: bool) -> str:
        """One standing instruction removed, by its id or their words; the model's sentence."""
        ...

    async def save(
        self, text: str, title: str | None, *, source: Literal["turn", "app"], device: uuid.UUID | None
    ) -> dict[str, Any]:
        """A note kept: {id, title, text, …}. Raises PisaError when it can't be."""
        ...

    async def find(self, query: str) -> list[dict[str, Any]]:
        """Their notes for the query, best first."""
        ...
