"""The public data Pisa reads (transit, weather alerts, pharmacies): the fetcher (an adapter in
pisa.connectors) and what the brain's tools and Pisa's views need from the service."""

from typing import Any, Protocol


class PublicDataError(Exception):
    """A public source failed: the message names the cause (never the user's data)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PublicFetch(Protocol):
    async def get(
        self, url: str, *, params: dict[str, str] | None = None, big: bool = False, cap: int = 8 * 1024 * 1024
    ) -> bytes:
        """The body of a GET (read-only; `big`: a static dataset, a longer timeout; `cap`: the most read).
        Raises PublicDataError."""
        ...


class PublicData(Protocol):
    async def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """One of the public reads (`domain.views.PUBLIC_READS`) as JSON-able data: always a `summary` line,
        `ok` False with `why` when the source couldn't answer (never a guess)."""
        ...


class StarterSkills(Protocol):
    """The research's first skills as recipes (`domain.skill_starters`)."""

    async def offer(self, tool: str) -> str | None:
        """The starter's name to offer after this read, or None (none fits, or they keep it already)."""
        ...

    async def keep(
        self, name: str, words: str, *, outside: bool, turn_id: str | None, device_id: Any
    ) -> tuple[str, dict[str, Any] | None]:
        """Their yes to Pisa's offer: the starter made a skill (the trial); (the sentence, the trial's
        sheet)."""
        ...
