"""What the brain's note tool and the conversation's per-turn block need
(`services/note.NoteService`)."""

import uuid
from collections.abc import Callable
from typing import Any, Protocol


class NoteData(Protocol):
    async def current(self) -> dict[str, Any] | None:
        """The note now: {version, text, change, source, at}, or None before their first telling."""
        ...

    async def rewrite(
        self,
        text: str,
        *,
        change: str,
        owner_said: str,
        owner_words: str,
        tainted: bool,
        device: uuid.UUID | None = None,
        day_context: bool = False,
        lang: str,
        asked: Callable[[str], None] | None = None,
        turn_id: str | None = None,
    ) -> str:
        """The whole new note from Pisa, grounded in their words this turn; the sentence for the model."""
        ...
