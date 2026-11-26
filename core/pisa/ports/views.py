"""What the brain's view tools need: keep a view, find one, and run it into a sheet."""

from typing import Any, Protocol


class ViewsData(Protocol):
    async def names(self) -> list[str]:
        """The saved views' names, newest first."""
        ...

    async def save(
        self, name: str, description: str, recipe: dict[str, Any], *, hosts: set[str], said: str,
        turn_id: str | None, device_id: str | None,
    ) -> str:  # fmt: skip
        """Keep (or replace by name) a view; the sentence for the model."""
        ...

    async def run(self, name: str) -> tuple[str, dict[str, Any] | None, list[str]]:
        """(the view's name or why not, the sheet's data or None, what was left out)."""
        ...
