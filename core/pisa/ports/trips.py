"""What the trips and the residency counter need from outside them, and what the brain's tools
read."""

from typing import Any, Protocol


class TripsData(Protocol):
    async def trip_text(self, lang: str) -> str:
        """The next trips, their legs and Pisa's tasks for them (from email: data)."""
        ...

    async def residency_text(self, year: int | None, lang: str) -> str:
        """The residency count for a year, with "è un conteggio, non una consulenza"."""
        ...

    async def while_away(self, city: str) -> tuple[str, list[str]]:
        """(the absence window, the email and calendar items about the city in it)."""
        ...

    async def checklist_sheet(self, lang: str) -> dict[str, Any] | None:
        """The next trip's departure checklist as a sheet."""
        ...
