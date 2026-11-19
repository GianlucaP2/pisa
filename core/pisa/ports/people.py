"""What the brain's people tools need. Every write stands on the user's own words this turn
(`owner_said`, checked against `words`); the reads give Pisa's own lines apart from an email's words."""

from typing import Protocol


class PeopleData(Protocol):
    async def remember_date(
        self, person: str, owner_said: str, words: str, lang: str, remind: list[int] | None
    ) -> str: ...

    async def remember_note(
        self, person: str, text: str, kind: str, owner_said: str, words: str, lang: str
    ) -> str: ...

    async def set_where(
        self, person: str, city: str | None, tz: str | None, owner_said: str, words: str
    ) -> str: ...

    async def stop_orbit(self, person: str | None, owner_said: str, words: str) -> str: ...

    async def about(self, person: str, lang: str) -> tuple[str, str, bool]:
        """(Pisa's own lines and their words about the person, the person's own words from their emails,
        whether the person's name came from an email or an invite they never set)."""
        ...

    async def hour(self, person: str, lang: str) -> str: ...

    async def person_outside(self, person: str, lang: str) -> bool:
        """The person their words name holds a name from an email or an invite they never
        set."""
        ...

    async def drafts(self) -> list[tuple[str, str, str]]:
        """Today's drafts Pisa offered in the morning: (person, kind, text)."""
        ...

    async def names_outside(self, names: list[str]) -> bool:
        """One of these names came from an email or an invite they never set."""
        ...
