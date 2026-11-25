"""What the brain's autopilot tools need: stop and resume Pisa, the vault, the
bills, and the user's spoken yes to Pisa's autonomy proposal."""

from typing import Any, Protocol


class AutopilotData(Protocol):
    async def stopped(self) -> bool: ...

    async def stop(self, lang: str) -> dict[str, Any]:
        """Stop Pisa's own initiative (by voice); the state."""
        ...

    async def resume(self, lang: str) -> dict[str, Any]: ...

    async def add_document(self, fields: dict[str, Any], lang: str) -> dict[str, Any]:
        """Keep a document (said by the user); the stored document. Raises PisaError when refused."""
        ...

    async def documents(self) -> list[dict[str, Any]]: ...

    async def aire_question(self, kind: str, country: str | None, lang: str) -> str | None:
        """The AIRE question when this document's renewal depends on it and Pisa never asked it."""
        ...

    async def aire_asked(self) -> bool:
        """Pisa asked about AIRE within the last day."""
        ...

    async def note_aire(self, registered: bool) -> None:
        """Their answer about AIRE, from their own words."""
        ...

    async def bills(self, *, unpaid: bool = False) -> list[dict[str, Any]]: ...

    async def accountant_package(self, to: str, year: int | None, lang: str) -> str:
        """One draft to their accountant with the year's documents (sent only from the
        card); the sentence for the model."""
        ...

    async def accept_autonomy(self, type_: str, level: str, words: str, *, outside: bool, lang: str) -> str:
        """Their spoken yes to Pisa's proposal for this type and level; the sentence for the model."""
        ...

    async def ask_again(self, type_: str, words: str, *, outside: bool, lang: str) -> str:
        """Their words take a kind back to `ask`."""
        ...

    # Pisa's skills
    async def wish(self, name: str, words: str, checks: Any, recipe: Any, **given: Any) -> Any:
        """(the sentence for the model, the trial's sheet or None)."""
        ...

    async def run_skill(self, name: str) -> tuple[str, dict[str, Any] | None, list[str]]: ...

    async def rename_skill(self, old: str, new: str, words: str, *, outside: bool) -> str: ...

    async def schedule_skill(self, name: str, at: str, days: str, words: str, *, outside: bool) -> str: ...

    async def skill_names(self) -> list[str]: ...

    async def skill_kind(self, name: str) -> str:
        """The data block kind of what a skill reads."""
        ...

    def hear(self, device_id: Any, words: str, lang: str) -> None:
        """Every turn's words: a correction of Pisa becomes a private test, in the background; never
        raises."""
        ...
