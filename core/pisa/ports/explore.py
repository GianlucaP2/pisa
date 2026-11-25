"""What the brain's fog-of-war and Map of the Life tools read."""

from typing import Any, Protocol


class ExploreData(Protocol):
    async def fog_sheet(self, lang: str) -> dict[str, Any] | None:
        """Their fog of war as a sheet (None before Pisa knows where they go)."""
        ...

    async def life_sheet(self, lang: str, which: str = "current") -> dict[str, Any] | None:
        """A season's Map of the Life as a sheet (`current` so far, or `last`); None when it's empty."""
        ...
