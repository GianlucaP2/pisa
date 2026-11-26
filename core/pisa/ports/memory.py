"""What the brain's memory tool, `recall(cue)`, reads (`services/episodes.EpisodeService`)."""

import uuid
from typing import Any, Protocol


class Memory(Protocol):
    async def recall(
        self,
        cue: str,
        *,
        words: str,
        lang: str,
        device: uuid.UUID | None = None,
        turn_id: str = "",
        day_from: str | None = None,
        day_to: str | None = None,
        deeper: bool = False,
    ) -> dict[str, Any]:
        """Pisa's own episodes for their question about their past: {answer: one | list | two | faded | none |
        refused, say: the line to say (two, faded, none), episodes: [what may be said of each]}."""
        ...
