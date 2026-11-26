"""Life on autopilot: one place the runtime, the API and the brain reach the
ledger, stop everything, the vault and the bills and earned autonomy."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from pisa.domain import vault as vault_rules
from pisa.domain import wishes as wish_rules
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.autonomy import AutonomyService
from pisa.services.ledger import LedgerService, Undoers
from pisa.services.money import MoneyService, PdfFetch
from pisa.services.owner_loop import OwnerLoop
from pisa.services.personal_evals import PersonalEvals
from pisa.services.phone_keys import PhoneKeys
from pisa.services.stop import StopService
from pisa.services.vault import VaultService
from pisa.services.wishes import Sheets, WishService

_log = get_logger("pisa.autopilot")
HEARD_WITHIN = 3.0  # seconds: keeping a correction never outlives this


class AutopilotService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        undoers: Undoers,
        sheets: Sheets,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        keys: PhoneKeys | None = None,
        autonomy: AutonomyService | None = None,
        vault: VaultService | None = None,
        owner_loop: OwnerLoop | None = None,
        pdfs: PdfFetch | None = None,
        accountant: Any = None,
        profile: Any = None,
        voice2: Any = None,
        voice6: Any = None,
    ) -> None:
        """`keys`, `autonomy`, `vault` and `owner_loop` are built by the runtime when others need them before
        this service exists (the approvals read the autonomy, the trips the vault, the views the report);
        built here otherwise."""
        self._pool = pool
        self._owner = owner
        self.stops = StopService(pool, owner=owner, clock=clock)
        self.keys = keys or PhoneKeys(pool, owner=owner, clock=clock)
        self.autonomy = autonomy or AutonomyService(pool, self.keys, owner=owner, clock=clock)
        self.vault = vault or VaultService(pool, owner=owner, clock=clock)
        self.money = MoneyService(pool, owner=owner, clock=clock, pdfs=pdfs)
        # Pisa's skills and the user's corrections kept as private tests
        self.skills = WishService(pool, sheets, self.keys, owner=owner, clock=clock)
        self.evals = PersonalEvals(pool, owner=owner, clock=clock)
        # the user loop: the weekly talk report, their corrections as candidates
        self.owner_loop = owner_loop or OwnerLoop(pool, owner=owner, clock=clock)
        self.ledger = LedgerService(
            pool, undoers, self.autonomy, self.money, owner=owner, clock=clock, skills=self.skills,
            owner_loop=self.owner_loop, voice2=voice2, voice6=voice6, profile=profile,
        )  # fmt: skip
        self.accountant = accountant  # the accountant package (it needs the actions)
        self._background: set[asyncio.Task[None]] = set()

    # --- AutopilotData (the brain's tools) ------------------------------------------------------------------

    async def stopped(self) -> bool:
        return await self.stops.stopped()

    async def stop(self, lang: str) -> dict[str, Any]:
        return await self.stops.stop("voice", lang)

    async def resume(self, lang: str) -> dict[str, Any]:
        return await self.stops.resume("voice", lang)

    async def add_document(self, fields: dict[str, Any], lang: str) -> dict[str, Any]:
        return await self.vault.add(fields, provenance="said", lang=lang)

    async def documents(self) -> list[dict[str, Any]]:
        return await self.vault.documents()

    async def aire_question(self, kind: str, country: str | None, lang: str) -> str | None:
        if not vault_rules.aire_matters(kind, country, self._owner.country):
            return None
        async with self._pool.connection() as conn, conn.transaction():
            if not await self.vault.ask_aire(conn):
                return None
        return vault_rules.AIRE_QUESTION["it-IT" if lang == "it-IT" else "en-US"]

    async def aire_asked(self) -> bool:
        return await self.vault.aire_asked_recently()

    async def note_aire(self, registered: bool) -> None:
        await self.vault.set_aire(registered, "voice")

    async def bills(self, *, unpaid: bool = False) -> list[dict[str, Any]]:
        return await self.money.bills(unpaid=unpaid)

    async def accountant_package(self, to: str, year: int | None, lang: str) -> str:
        if self.accountant is None:
            return "Not drafted: Pisa's email isn't connected. Say so in one line."
        return await self.accountant.package(to, year, lang)

    async def accept_autonomy(self, type_: str, level: str, words: str, *, outside: bool, lang: str) -> str:
        return await self.autonomy.accept_spoken(type_, level, words, outside=outside, lang=lang)

    async def ask_again(self, type_: str, words: str, *, outside: bool, lang: str) -> str:
        return await self.autonomy.ask_again(type_, words, outside=outside, lang=lang)

    # --- skills
    # --------------------------------------------------------------------------------------------------

    async def wish(self, name: str, words: str, checks: Any, recipe: Any, **given: Any) -> Any:
        return await self.skills.wish(name, words, checks, recipe, **given)

    async def run_skill(self, name: str) -> tuple[str, dict[str, Any] | None, list[str]]:
        return await self.skills.run(name, "voice")

    async def rename_skill(self, old: str, new: str, words: str, *, outside: bool) -> str:
        return await self.skills.rename(old, new, words, outside=outside)

    async def schedule_skill(self, name: str, at: str, days: str, words: str, *, outside: bool) -> str:
        return await self.skills.set_schedule(name, at, days, words, outside=outside)

    async def skill_names(self) -> list[str]:
        return await self.skills.names()

    async def skill_kind(self, name: str) -> str:
        return await self.skills.kind_of(name)

    async def heard(self, device_id: Any, words: str, lang: str) -> None:
        """A correction of Pisa becomes a private test; never raises, never longer than HEARD_WITHIN."""
        try:
            await asyncio.wait_for(self.evals.heard(device_id, words, lang), HEARD_WITHIN)
        except Exception:
            _log.warning("personal_eval_failed")

    def hear(self, device_id: Any, words: str, lang: str) -> None:
        """In the background: the turn never waits for it (no I/O at all unless their words are a
        correction)."""
        if not wish_rules.is_complaint(words):
            return
        task = asyncio.create_task(self.heard(device_id, words, lang))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def settled(self) -> None:
        """The background work started so far, finished (tests; a clean shutdown)."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)


class ServiceUndoers:
    """The ledger's Undo runs the paths Pisa already has: an approval's undo, the 24 h of a done or
    attended item, a bulk's undo, a task's status."""

    def __init__(self, approvals: Any, done: Any, tasks_bulk: Any, tasks: Any) -> None:
        self._approvals, self._done, self._bulk, self._tasks = approvals, done, tasks_bulk, tasks

    async def approval(self, approval_id: str) -> object:
        return await self._approvals.undo(approval_id)

    async def done_task(self, task_id: str) -> object:
        return await self._done.undo_task(task_id)

    async def done_event(self, event_id: str) -> object:
        return await self._done.undo_event(event_id)

    async def bulk(self, bulk_id: str) -> object:
        return await self._bulk.undo(bulk_id)

    async def task_status(self, task_id: str, status: str) -> object:
        return await self._tasks.update(task_id, {"status": status})
