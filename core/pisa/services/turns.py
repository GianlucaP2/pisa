"""A conversation turn, one path for every carrier.

- `answer`: what a turn on `/v2/live` (and `POST /v2/runs`, the same device session without a socket) runs
  once the device session has done its own part (a continuation, a supersede): the briefing phrase, else
  `ConversationService.respond` with every rail of the turn (the brain, its tools, quarantine, the yes-card's
  code, Pisa's moves, the conversation's record, the spend caps). One path, never a copy.
- `TurnsService.sheet`: a sheet kept by a REST turn of contract 1 (`turn_sheets`, until their messages'
  retention), still served by `GET /v1/sheets/{sheet_id}`.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from psycopg_pool import AsyncConnectionPool

from pisa.brain.conversation import SentenceSplitter
from pisa.logging import get_logger
from pisa.services.voice import Speaker

if TYPE_CHECKING:
    from pisa.services.runtime import Runtime

_log = get_logger("pisa.turns")


class Emit(Protocol):
    async def __call__(self, delta: str, final: bool, lang: str = ...) -> None: ...


async def answer(
    runtime: "Runtime",
    device_id: uuid.UUID,
    text: str,
    lang: str,
    emit: Emit,
    *,
    trace_id: str,
    speaker: Speaker | None,
    senders: dict[str, Any],
    continued: bool = False,
    hold: Callable[[str, str], Awaitable[None]] | None = None,
    spoken: bool = True,
    reply_to: str | None = None,
    heard: Any = None,
    origin: str = "words",
) -> str:
    """One final turn of the user's words, whatever carried them: the briefing phrase (its text is the reply,
    sentence by sentence, so a cancel works), else the conversation's turn. Returns Pisa's reply as the
    conversation keeps it (never a restate, holding or progress line `emit` also carried)."""
    # a tap's label in a drawing is never the briefing phrase (the page wrote it)
    briefing = None if origin == "drawing" else await runtime.briefings.for_phrase(text)
    if briefing is not None:
        _log.info("briefing_phrase", trace_id=trace_id, briefing_id=str(briefing.id))
        splitter = SentenceSplitter()
        for sentence in splitter.feed(briefing.text):
            await emit(sentence, False, briefing.lang)
        await runtime.conversations.remember(  # emails' words, retold
            device_id, text, lang, briefing.text, briefing.lang, outside=["briefing"]
        )
        # The briefing is newer than any question Pisa asked before it (newest wins).
        runtime.conversations.asked_nothing(device_id)
        await emit(splitter.rest(), True, briefing.lang)
        if speaker is not None:
            await speaker.done()
        return briefing.text
    return await runtime.conversations.respond(
        device_id,
        text,
        lang,
        emit,
        trace_id=trace_id,
        speaker=speaker,
        continued=continued,
        hold=hold,
        spoken=spoken,
        reply_to=reply_to,
        heard=heard,
        origin=origin,
        **senders,
    )


# --- the sheets of contract 1's REST turns -----------------------------------------------------------------


class TurnsService:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def sheet(self, sheet_id: str) -> dict[str, Any] | None:
        """A REST turn's kept sheet as the `present` object the app renders; None when it is no turn's."""
        try:
            key = uuid.UUID(sheet_id.removeprefix("sheet:"))
        except ValueError:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute("SELECT data, created_at FROM turn_sheets WHERE id = %s", (key,))
            ).fetchone()
        if row is None:
            return None
        return {
            "object_id": f"sheet:{key}",
            "kind": "sheet",
            "revision": int(row[1].timestamp() * 1000),
            "data": row[0],
        }
