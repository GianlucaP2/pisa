"""What the brain's one night tool, `stanotte(query)`, reads (`services/voice3.NightVoice`)."""

from typing import Any, Protocol


class NightRecords(Protocol):
    async def find(self, query: str, lang: str) -> dict[str, Any] | None:
        """Pisa's own night records matching `query`: {night: the story, records: [{id, kind, text}]}, with no
        third-party text; None while the night's surface is off (the stanotte gate)."""
        ...
