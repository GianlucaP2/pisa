"""The accountant package. On their words ("prepara i documenti per il commercialista"), Pisa
lists the year's documents it holds (bills, receipts, the residency count, the vault) and drafts **one**
email to the accountant they name, with the attachments: the bills' and receipts' PDFs (Gmail still holds
them; at most `MAX_FILES`, `MAX_BYTES` together) and two summaries Pisa writes. It is sent only through the
approval path: the card, with Face ID (`ActionsService.draft_package`). The draft stays a draft.

Logs carry counts only.
"""

from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from pisa.domain import accountant as rules
from pisa.domain.owner import Owner
from pisa.logging import get_logger

_log = get_logger("pisa.accountant")
PdfFetch = Callable[[str, str], Awaitable[bytes | None]]


class Drafts(Protocol):
    async def draft_package(
        self, to: str, subject: str, text: str, files: list[tuple[str, str, bytes]]
    ) -> str: ...


class Residency(Protocol):
    async def residency_text(self, year: int | None, lang: str) -> str: ...


class Vault(Protocol):
    async def documents(self) -> list[dict[str, Any]]: ...


class AccountantService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        drafts: Drafts,
        residency: Residency | None,
        vault: Vault,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        pdfs: PdfFetch | None = None,
    ) -> None:
        self._pool = pool
        self._drafts = drafts
        self._residency = residency
        self._vault = vault
        self._owner = owner
        self._clock = clock
        self.pdfs = pdfs

    async def items(self, year: int) -> tuple[list[rules.Item], list[rules.Item]]:
        """The year's bills and receipts Pisa holds (by the day each email came), oldest first."""
        tz = self._owner.zone
        start = datetime.combine(date(year, 1, 1), time(0), tz)
        end = datetime.combine(date(year + 1, 1, 1), time(0), tz)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT m.kind, coalesce(m.payee, m.from_name), m.amount, m.currency, m.sent_at, "
                    "m.due_on, "
                    "m.paid OR coalesce(t.status = 'done', false), m.email_id, m.pdf_part_id "
                    "FROM money_seen m "
                    "LEFT JOIN tasks t ON t.id = m.task_id WHERE m.sent_at >= %s AND m.sent_at < %s "
                    "ORDER BY m.sent_at",
                    (start, end),
                )
            ).fetchall()
        found = [
            rules.Item(kind, who, amount, currency, sent.astimezone(tz).date(), due, bool(paid), email, part)
            for kind, who, amount, currency, sent, due, paid, email, part in rows
        ]
        return [i for i in found if i.kind == "bill"], [i for i in found if i.kind == "receipt"]

    async def _files(self, items: list[rules.Item]) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        files: list[tuple[str, str, bytes]] = []
        left_out: list[str] = []
        total = 0
        for n, item in enumerate((i for i in items if i.part_id), start=1):
            name = rules.pdf_name(item, n)
            if self.pdfs is None or len(files) >= rules.MAX_FILES:
                left_out.append(name)
                continue
            try:
                data = await self.pdfs(item.email_id, str(item.part_id))
            except Exception:  # Gmail failed or the email is gone: named, not attached
                data = None
            if data is None or total + len(data) > rules.MAX_BYTES:
                left_out.append(name)
                continue
            total += len(data)
            files.append((name, "application/pdf", data))
        return files, left_out

    async def package(self, to: str, year: int | None, lang: str) -> str:
        """The draft and its read-back (the sentence for the model), or why nothing was drafted."""
        today = self._owner.day(self._clock())
        year = rules.tax_year(today, year, self._owner.country)
        if not 2000 <= year <= today.year:
            return f"Not drafted: {year} is not a year Pisa holds documents for. Ask which year."
        bills, receipts = await self.items(year)
        documents = await self._vault.documents()
        residency = None
        if self._residency is not None:
            try:
                residency = await self._residency.residency_text(year, lang)
            except Exception:
                _log.warning("accountant_residency_unavailable")
        if not (bills or receipts or documents or residency):
            return (
                f"Nothing to send: Pisa holds no bills, receipts or documents for {year}. Say so in one line."
            )
        pdfs, left_out = await self._files(bills + receipts)
        files = list(pdfs)
        if residency:
            files.append(rules.residency_file(year, residency, lang))
        if documents:
            files.append(rules.vault_file(year, documents, lang))
        text = rules.body(year, bills, receipts, residency, documents, len(files), left_out, lang)
        said = await self._drafts.draft_package(to, rules.subject(year, lang), text, files)
        if not said.startswith("Not drafted"):  # the year always said in the read-back
            said += f" Say the year these documents are for: {year}."
        _log.info(
            "accountant_package",
            bills=len(bills),
            receipts=len(receipts),
            files=len(files),
            left=len(left_out),
        )
        return said
