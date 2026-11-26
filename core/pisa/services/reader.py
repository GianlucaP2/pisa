"""The reader's service. One outside text in, its typed fields out
(`quarantine.Fields`); the planner gets the fields, never the text.

- **The summary**: an email's is the first event's own sentence when the intake read it (the events
  call, a model with no tools), or the gist an older intake wrote; else, and for a page, the tool-less
  reader (`pisa.brain.reader`, slot intake.triage, the conversation's model with no tools when none is
  chosen). Kept in memory by the text's id for the process's life (a bounded map), so an email read twice in
  a talk is read once. A reader that fails leaves the fields alone: never the raw text in its place.
- **Hidden text**: the runs left out are counted per day and source (`pisa_hidden_text`), for the
  user's weekly report. Counts only.
Logs carry ids and counts only.
"""

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from pisa.brain import reader as brain
from pisa.brain.models.gateway import CallContext, ModelGateway
from pisa.domain import quarantine
from pisa.domain.errors import PisaError
from pisa.logging import get_logger

_log = get_logger("pisa.reader")
KEPT = 200  # summaries kept in memory
KEPT_FOR = timedelta(hours=1)  # a page changes; its summary is read again after an hour
PAGE_READ_S = 4.0  # a page's reader waits this long in a turn, then the fields alone
FOCUSED_READ_S = 10.0  # a reader listing up to 15 candidates writes more ("cerco" was said)


async def note_hidden(conn: AsyncConnection, source: str, runs: int, day: Any) -> None:
    """Item 3: hidden runs left out of what Pisa read, added to the day's count (a number, never the text)."""
    if runs <= 0:
        return
    await conn.execute(
        "INSERT INTO pisa_hidden_text (day, source, runs) VALUES (%s, %s, %s) "
        "ON CONFLICT (day, source) DO UPDATE SET runs = pisa_hidden_text.runs + EXCLUDED.runs",
        (day, source, runs),
    )
    _log.info("hidden_text_dropped", source=source, runs=runs)


class ReaderService:
    def __init__(
        self, gateway: ModelGateway | None, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        self._kept: OrderedDict[str, tuple[datetime, str, list[tuple[str, str, str]]]] = OrderedDict()

    async def summary(
        self,
        key: str,
        kind: quarantine.Kind,
        text: str,
        lang: str,
        *,
        slots: tuple[str, ...] = (brain.SLOT, brain.FALLBACK_SLOT),
        reraise: frozenset[str] = frozenset(),
    ) -> str | None:
        """The tool-less reader's summary of one text, or None when no reader could read it. `slots`: the
        models tried, in order; `reraise`: the error codes raised to the caller, never swallowed (the intake
        pauses on them)."""
        read = await self._read(key, kind, text, lang, None, slots=slots, reraise=reraise)
        return read[0] if read is not None else None

    async def _read(
        self,
        key: str,
        kind: quarantine.Kind,
        text: str,
        lang: str,
        question: str | None,
        *,
        slots: tuple[str, ...] = (brain.SLOT, brain.FALLBACK_SLOT),
        reraise: frozenset[str] = frozenset(),
    ) -> tuple[str, list[tuple[str, str, str]]] | None:
        """(summary, candidates) of one text; with `question` the reader also lists the
        candidates that answer it. Kept by the text's key and the question."""
        if question:
            key = f"{key}#q:{hashlib.sha256(question.encode()).hexdigest()[:16]}"
        if (kept := self._kept.get(key)) is not None and self._clock() - kept[0] < KEPT_FOR:
            self._kept.move_to_end(key)
            return kept[1], kept[2]
        if self._gateway is None or not text.strip():
            return None
        instructions, user = brain.prompt(kind, text, lang, question)
        found: list[tuple[str, str, str]] = []

        async def call(ctx: CallContext) -> str:
            said, usage, candidates = await brain.summarise(ctx.model, kind, text, lang, question)
            ctx.usage.update(usage)
            found[:] = candidates
            return said

        said: str | None = None
        for slot in slots:
            try:
                said = await self._gateway.run(
                    slot, call, prompt_chars=len(instructions) + len(user),
                    max_output_tokens=brain.FOCUSED_OUTPUT_TOKENS if question else brain.MAX_OUTPUT_TOKENS,
                )  # fmt: skip
                break
            except PisaError as err:
                if err.code in reraise:
                    raise
                _log.info("reader_failed", slot=slot, code=err.code)
            except Exception as err:
                _log.info("reader_failed", slot=slot, error=type(err).__name__)
        if said is None:
            return None
        self._kept[key] = (self._clock(), said, list(found))
        self._kept.move_to_end(key)
        while len(self._kept) > KEPT:
            self._kept.popitem(last=False)
        return said, list(found)

    async def page(
        self, url: str, text: str, hidden: int, lang: str, question: str | None = None
    ) -> quarantine.Fields:
        """A web page's fields: its address, the values by rule, the reader's summary; with their question
        the candidates that answer it, each kept only when the page's own text names it."""
        host = url.split("://", 1)[-1].split("/", 1)[0]
        wait = FOCUSED_READ_S if question else PAGE_READ_S
        try:
            read = await asyncio.wait_for(self._read(f"page:{url}", "web-page", text, lang, question), wait)
        except TimeoutError:
            read = None
            _log.info("reader_slow", kind="web-page")
        said, found = read if read is not None else (None, [])
        fields = quarantine.read(
            "web-page", text, source=host, title=url, summary=said, hidden=hidden, today=self._clock().date(),
            candidates=found,
        )  # fmt: skip
        if found:
            _log.info("reader_candidates", given=len(found), kept=len(fields.candidates))
        return fields
