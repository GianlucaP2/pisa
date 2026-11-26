"""What the profile tools need; pisa.services.profile implements it."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol


class ProfileData(Protocol):
    async def remember(
        self,
        *,
        kind: str,
        statement: str,
        quote: str,
        owner_words: str,
        scope: dict[str, Any] | None = None,
        hard: bool = False,
        sensitive: bool = False,
        tainted: bool = False,
        ref: str | None = None,
        device: uuid.UUID | None = None,
        lang: str | None = None,
        asked: Callable[[str], None] | None = None,
        turn: str | None = None,
    ) -> str: ...

    async def confirm_pending(
        self,
        owner_words: str,
        device: uuid.UUID | None = None,
        *,
        run_change: Callable[[str, dict[str, Any], bool], Awaitable[str]] | None = None,
    ) -> str: ...

    async def hold_change(
        self,
        tool: str,
        args: dict[str, Any],
        what: str,
        *,
        device: uuid.UUID | None = None,
        lang: str | None = None,
        asked: Callable[[str], None] | None = None,
        outside: bool = False,
    ) -> str | None: ...

    async def hold_value(
        self,
        tool: str,
        args: dict[str, Any],
        what: str,
        *,
        device: uuid.UUID | None = None,
        lang: str | None = None,
        asked: Callable[[str], None] | None = None,
    ) -> str | None:
        """A write carrying an outside value waits for their yes to its own question;
        None when this very call is the one their yes answered."""
        ...

    async def repeated_change(self, tool: str, args: dict[str, Any], device: uuid.UUID | None) -> bool:
        """The user asked again for the change Pisa waits on their yes for: it is their yes."""
        ...

    async def forget_said(
        self,
        what: str,
        owner_words: str,
        *,
        tainted: bool,
        device: uuid.UUID | None = None,
        lang: str | None = None,
        asked: Callable[[str], None] | None = None,
    ) -> str: ...

    async def never_learn_said(
        self,
        topic: str,
        owner_words: str,
        *,
        tainted: bool,
        device: uuid.UUID | None = None,
        lang: str | None = None,
        asked: Callable[[str], None] | None = None,
    ) -> str: ...

    async def home_said(
        self,
        place: str,
        owner_words: str,
        *,
        tainted: bool,
        device: uuid.UUID | None = None,
        lang: str | None = None,
        asked: Callable[[str], None] | None = None,
    ) -> str: ...

    async def known(self, limit: int = 5) -> tuple[list[str], int]: ...

    async def talk(self) -> dict[str, Any]: ...

    async def set_talk(self, talk: dict[str, Any]) -> None: ...

    async def talk_claims(self, since: datetime) -> tuple[list[str], int]: ...

    async def lang(self) -> str: ...

    def now(self) -> datetime: ...

    def elapsed(self, started: str) -> timedelta: ...
