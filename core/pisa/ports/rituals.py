"""What the brain's ritual tools need: switch a ritual, close the day, place notes,
hunches, and the user's answer to Pisa's newest question."""

import uuid
from typing import Any, Protocol


class RitualsData(Protocol):
    async def switch(self, ritual: str, on: bool, words: str) -> str:
        """Turn a ritual on or off on the user's own words; the sentence for the model."""
        ...

    async def closing_now(self) -> tuple[str, dict[str, Any]] | None:
        """ "Chiudiamo": (sheet id, the sheet's data) or None on an empty day."""
        ...

    async def note_hunch(
        self, guess: str, evidence: list[str], device_id: uuid.UUID | None, kind: str = "like"
    ) -> str:
        """Store a hunch and ask it once; the sentence for the model."""
        ...

    async def hunches(self) -> list[str]:
        """The hunches the user confirmed."""
        ...

    async def answer_in_turn(self, device_id: uuid.UUID, yes: bool) -> str | None:
        """The same, from a tool inside the turn: checked against the device's previous turn."""
        ...

    async def answer_newest(self, device_id: uuid.UUID, yes: bool, latest: Any = None) -> str | None:
        """The user's yes or no to Pisa's newest ritual question; None when none is open. `latest`: the
        device's latest turn (a question asked in it, or after which a notice came)."""
        ...

    def newest_ask(self, device_id: uuid.UUID, latest: Any = None) -> uuid.UUID | None:
        """Pisa's newest question to this device when it is one of the rituals' (the knock's)."""
        ...

    async def answer(
        self, ask_id: uuid.UUID, yes: bool, auth: str, *, hold_for: uuid.UUID | None = None
    ) -> str | None:
        """The user's yes or no to that question, by code; None when it isn't open."""
        ...

    def bind(self, device_id: uuid.UUID, ask_id: uuid.UUID, turn: Any, question: str) -> None:
        """A question code said in its own reply (la prova generale's): their next yes answers it."""
        ...

    def code_says(self, device_id: uuid.UUID, turn: Any) -> str | None:
        """The knock's content opened in this turn, for code to say after the reply."""
        ...

    def question_in_turn(self, device_id: uuid.UUID, turn: Any) -> str | None:
        """The question a ritual (a hunch) asked in this turn, for code to say in its words."""
        ...

    def said(self, question: str, reply: str) -> bool:
        """The reply says the question (its words, in order)."""
        ...


class PlaceNotesData(Protocol):
    async def create(
        self,
        text: str,
        lat: float,
        lon: float,
        *,
        radius_m: int | None = None,
        place_name: str | None = None,
        place_id: str | None = None,
        turn_id: str | None = None,
        device_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        ...

    async def listed(self, *, tasks: bool = True) -> list[dict[str, Any]]: ...

    async def known_place(self, name: str) -> tuple[tuple[float, float], str, str] | None:
        """A place Pisa already knows by this name: (position, name, place id)."""
        ...

    async def forget(self, what: str) -> str: ...
