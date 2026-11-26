"""Memoria (`pisa.domain.memoria`): their standing instructions and their free notes, and Settings → Memoria.

**Only their words write an instruction**:
- from a conversation turn (`remember_instruction`), never one that read outside text this turn (an email or a
  page: refused, the rail of `keep_intention`); their words this turn, word for word, are kept at once when
  those words ask for a standing instruction; anything else (Pisa's paraphrase) waits for their one yes to
  the very words, in the profile's one pending write (`confirm_remember`);
- from Settings → Memoria (`POST`/`PATCH`), their own text.
The caps hold on every write: at most 20 active, 280 characters each, 2,000 characters in all.

Notes are their text, kept as they asked. Each is embedded by the `memory.embed` slot's model when one is
picked (an AI processor: a turn passed the AI consent check; the REST writes check it first, as Pisa Learn's
do), so `find_note` finds it by meaning too; without one, by their words.

Logs carry ids, counts and lengths, never their words.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from pisa.brain.models.slots import get_slot
from pisa.domain import memoria as rules
from pisa.domain.errors import PisaError
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services.skills import SLOT, Embed

_log = get_logger("pisa.memoria")
_INSTRUCTIONS = "SELECT id, text, source, created_at, updated_at FROM owner_instructions ORDER BY id"


def _shown(row: dict[str, Any]) -> dict[str, Any]:
    """A row as the API shows it (times as ISO 8601)."""
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row.items()}


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def _not_found(what: str, item_id: int) -> PisaError:
    return PisaError("not_found", f"There is no {what} {item_id}.", "Reload Memoria and pick one")


def _invalid(why: str) -> PisaError:
    return PisaError("validation_failed", f"Not saved: {why}.", "Change it and save again")


class MemoriaService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        clock: Callable[[], datetime],
        profile: Any = None,  # ProfileService: Pisa's words wait for their yes (one question at a time)
        embed: Embed | None = None,
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._profile = profile
        self._embed = embed
        if profile is not None:
            profile.instruction_held = self.held  # their yes (confirm_remember) keeps Pisa's words here

    # --- instructions: reads -----------------------------------------------------------------------------

    async def instructions(self) -> list[dict[str, Any]]:
        """The active instructions, oldest first."""
        async with self._pool.connection() as conn:
            return await self._instructions(conn)

    @staticmethod
    async def _instructions(conn: AsyncConnection) -> list[dict[str, Any]]:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(_INSTRUCTIONS)
        return list(await cur.fetchall())

    async def for_turn(self) -> tuple[list[dict[str, Any]], int]:
        """What a turn reads: the instructions within the caps, and how many notes they keep."""
        async with self._pool.connection() as conn:
            rows = await self._instructions(conn)
            row = await (await conn.execute("SELECT count(*) FROM owner_notes")).fetchone()
        return rules.fit(rows), int(row[0]) if row else 0

    # --- instructions: from a turn -----------------------------------------------------------------------

    async def remember(
        self,
        text: str,
        *,
        owner_words: str,
        outside_now: bool,
        device: uuid.UUID | None = None,
        lang: str,
        asked: Callable[[str], None] | None = None,
        turn_id: str | None = None,
    ) -> str:
        """remember_instruction: their words kept at once; Pisa's words after their one yes. The sentence for
        the model."""
        if outside_now:
            _log.warning("instruction_refused", cause="outside text this turn")
            return (
                "Not kept: this turn read text from outside (an email, a page, a note), and a standing "
                "instruction is kept only from their own words. Nothing changed; if they want it, they say "
                "it again in a new message."
            )
        new = rules.clean(text)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.MEMORIA,))
            rows = await self._instructions(conn)
            if any(rules.plain(r["text"]) == rules.plain(new) for r in rows):
                return "Already kept: they have this standing instruction. Say so in a few words."
            if (why := rules.over_caps(new, [r["text"] for r in rows])) is not None:
                _log.info("instruction_over_cap", active=len(rows), chars=len(new))
                return f"Not kept: {why}. Tell them in one short line."
            if not (rules.their_words(new, owner_words) and rules.asks_to_keep(owner_words)):
                # Pisa's words (or their words that don't read as a standing instruction): their one yes
                if self._profile is None:
                    return "Not kept: Pisa can't ask them here. Use their exact words."
                write = {
                    "op": "instruction",
                    "text": new,
                    "turn_id": turn_id,
                    "device": str(device) if device else None,
                    "what": rules.question(new, lang),
                }
                return await self._profile.hold(write, device=device, lang=lang, asked=asked)
            row_id = await self._add(conn, new, "turn", turn_id, device)
        return self._said(row_id, new, len(rows) + 1)

    async def held(self, pending: dict[str, Any]) -> str:
        """Their yes to Pisa's words (`confirm_remember`): kept now, the caps checked again."""
        device = uuid.UUID(pending["device"]) if pending.get("device") else None
        new = rules.clean(pending["text"])
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.MEMORIA,))
            rows = await self._instructions(conn)
            if any(rules.plain(r["text"]) == rules.plain(new) for r in rows):
                return "Already kept: they have this standing instruction. Say so in a few words."
            if (why := rules.over_caps(new, [r["text"] for r in rows])) is not None:
                return f"Not kept: {why}. Tell them in one short line."
            row_id = await self._add(conn, new, "held", pending.get("turn_id"), device)
        return self._said(row_id, new, len(rows) + 1)

    async def forget(self, which: str, *, owner_words: str, outside_now: bool) -> str:
        """forget_instruction: one instruction, by its id or their words, when their words ask to forget."""
        if outside_now:
            return (
                "Not forgotten: this turn read text from outside; only their own words remove an instruction."
            )
        if not rules.asks_to_forget(owner_words):
            return "Not forgotten: their words this turn don't ask to forget one. Ask them which."
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.MEMORIA,))
            found = rules.matching(which, await self._instructions(conn))
            if not found:
                return (
                    "No standing instruction matches: say which one (each has its number in the turn's "
                    "block)."
                )
            if len(found) > 1:
                ids = ", ".join(str(r["id"]) for r in found[:5])
                return f"Several match (numbers {ids}): ask which one, in a few words."
            await conn.execute("DELETE FROM owner_instructions WHERE id = %s", (found[0]["id"],))
        _log.info("instruction_forgotten", instruction_id=found[0]["id"], source="turn")
        return f'Forgotten: instruction {found[0]["id"]} ("{found[0]["text"]}"). Say it in a few words.'

    # --- instructions: from Settings → Memoria -----------------------------------------------------------

    async def add(self, text: str, device: uuid.UUID | None) -> dict[str, Any]:
        new = rules.clean(text)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.MEMORIA,))
            rows = await self._instructions(conn)
            if (why := rules.over_caps(new, [r["text"] for r in rows])) is not None:
                raise _invalid(why)
            row_id = await self._add(conn, new, "app", None, device)
            return await self._one(conn, row_id)

    async def edit(self, item_id: int, text: str) -> dict[str, Any]:
        new = rules.clean(text)
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.MEMORIA,))
            rows = await self._instructions(conn)
            if not any(r["id"] == item_id for r in rows):
                raise _not_found("instruction", item_id)
            others = [r["text"] for r in rows if r["id"] != item_id]
            if (why := rules.over_caps(new, others)) is not None:
                raise _invalid(why)
            await conn.execute(
                "UPDATE owner_instructions SET text = %s, updated_at = %s WHERE id = %s",
                (new, self._clock(), item_id),
            )
            _log.info("instruction_edited", instruction_id=item_id, chars=len(new))
            return await self._one(conn, item_id)

    async def remove(self, item_id: int) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            gone = await conn.execute("DELETE FROM owner_instructions WHERE id = %s", (item_id,))
            if gone.rowcount == 0:
                raise _not_found("instruction", item_id)
        _log.info("instruction_forgotten", instruction_id=item_id, source="app")

    async def _add(
        self, conn: AsyncConnection, text: str, source: str, turn_id: str | None, device: uuid.UUID | None
    ) -> int:
        now = self._clock()
        row = await (
            await conn.execute(
                "INSERT INTO owner_instructions (text, source, turn_id, device_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (text, source, turn_id, await _known(conn, device), now, now),
            )
        ).fetchone()
        assert row is not None
        _log.info("instruction_kept", instruction_id=row[0], chars=len(text), source=source)
        return int(row[0])

    @staticmethod
    async def _one(conn: AsyncConnection, item_id: int) -> dict[str, Any]:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT id, text, source, created_at, updated_at FROM owner_instructions WHERE id = %s",
            (item_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise _not_found("instruction", item_id)
        return _shown(dict(row))

    @staticmethod
    def _said(row_id: int, text: str, active: int) -> str:
        out = [
            f'Kept: standing instruction {row_id} ("{text}"), {active} of {rules.MAX_ACTIVE}. From the next '
            "turn it is in their preferences."
        ]
        if rules.wishes(text):
            out.append(
                "It asks for more than Pisa's rails allow: it is kept as their wish only and changes no "
                "tool, no consent and no approval (what needs their yes still waits for it). Tell them so "
                "in the same line."
            )
        out.append("Say it back in one short line.")
        return " ".join(out)

    # --- notes -------------------------------------------------------------------------------------------

    async def notes(self) -> list[dict[str, Any]]:
        """Their notes, newest first."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT id, title, text, source, created_at, updated_at FROM owner_notes "
                "ORDER BY updated_at DESC, id DESC"
            )
            return list(await cur.fetchall())

    async def embeds(self) -> bool:
        """A note written now goes to the `memory.embed` slot's model (an AI processor)."""
        if self._embed is None:
            return False
        async with self._pool.connection() as conn:
            slot = await get_slot(conn, SLOT)
        return bool(slot.model)

    async def save(
        self, text: str, title: str | None, *, source: str, device: uuid.UUID | None
    ) -> dict[str, Any]:
        body, head = _note_parts(text, title)
        vector = await self._vectors(head, body)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            count = await (await conn.execute("SELECT count(*) FROM owner_notes")).fetchone()
            if count is not None and int(count[0]) >= rules.MAX_NOTES:
                raise _invalid(f"they keep {rules.MAX_NOTES} notes, the most Pisa keeps: delete some first")
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "INSERT INTO owner_notes (title, text, source, device_id, model_ref, embedding, created_at, "
                "updated_at) VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s) "
                "RETURNING id, title, text, source, created_at, updated_at",
                (head, body, source, await _known(conn, device), vector[0], vector[1], now, now),
            )
            row = await cur.fetchone()
        assert row is not None
        _log.info(
            "note_saved", note_id=row["id"], chars=len(body), source=source, embedded=vector[0] is not None
        )
        return _shown(dict(row))

    async def edit_note(
        self, item_id: int, text: str | None, title: str | None, clear_title: bool
    ) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT id, title, text, source, created_at, updated_at FROM owner_notes WHERE id = %s",
                (item_id,),
            )
            now_row = await cur.fetchone()
        if now_row is None:
            raise _not_found("note", item_id)
        new_title = None if clear_title else (title if title is not None else now_row["title"])
        body, head = _note_parts(text if text is not None else now_row["text"], new_title)
        vector = await self._vectors(head, body)
        async with self._pool.connection() as conn, conn.transaction():
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "UPDATE owner_notes SET title = %s, text = %s, model_ref = %s, embedding = %s::vector, "
                "updated_at = %s WHERE id = %s RETURNING id, title, text, source, created_at, updated_at",
                (head, body, vector[0], vector[1], self._clock(), item_id),
            )
            row = await cur.fetchone()
        if row is None:
            raise _not_found("note", item_id)
        _log.info("note_edited", note_id=item_id, chars=len(body))
        return _shown(dict(row))

    async def remove_note(self, item_id: int) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            gone = await conn.execute("DELETE FROM owner_notes WHERE id = %s", (item_id,))
            if gone.rowcount == 0:
                raise _not_found("note", item_id)
        _log.info("note_deleted", note_id=item_id)

    async def find(self, query: str) -> list[dict[str, Any]]:
        """Their notes for `query`: by their words, then by meaning (when notes were embedded), best first."""
        async with self._pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "SELECT id, title, text, source, created_at, updated_at, model_ref FROM owner_notes "
                "ORDER BY updated_at DESC, id DESC LIMIT %s",
                (rules.MAX_NOTES,),
            )
            rows = list(await cur.fetchall())
        scored = {r["id"]: rules.words_score(query, r["title"], r["text"]) for r in rows}
        refs = {r["model_ref"] for r in rows if r["model_ref"]}
        if refs and self._embed is not None and query.strip():
            try:
                made = await self._embed([query], "query")
            except Exception as err:  # the words still find: a turn never waits on embeddings
                _log.warning("notes_embed_failed", error=type(err).__name__)
                made = None
            if made is not None and made[0] in refs:
                async with self._pool.connection() as conn:
                    near = await (
                        await conn.execute(
                            "SELECT id, 1 - (embedding <=> %s::vector) FROM owner_notes WHERE model_ref = %s",
                            (_vector(made[1][0]), made[0]),
                        )
                    ).fetchall()
                for note_id, similarity in near:
                    if similarity is not None and float(similarity) >= rules.SEMANTIC_MIN:
                        scored[note_id] = max(scored.get(note_id, 0.0), float(similarity))
        best = sorted((r for r in rows if scored.get(r["id"], 0.0) > 0), key=lambda r: -scored[r["id"]])
        found = [{k: v for k, v in r.items() if k != "model_ref"} for r in best[: rules.FOUND_MAX]]
        _log.info("notes_found", found=len(found), kept=len(rows))
        return found

    async def _vectors(self, title: str | None, text: str) -> tuple[str | None, str | None]:
        """(model ref, vector) for a note, or (None, None) without an embedding model or on its failure."""
        if self._embed is None:
            return None, None
        try:
            made = await self._embed([f"{title}\n{text}" if title else text], "document")
        except Exception as err:  # kept all the same: found by its words
            _log.warning("note_embed_failed", error=type(err).__name__)
            return None, None
        if made is None or not made[1]:
            return None, None
        return made[0], _vector(made[1][0])

    # --- Settings → Memoria ------------------------------------------------------------------------------

    async def view(self) -> dict[str, Any]:
        rows = await self.instructions()
        return {
            "instructions": [_shown(r) for r in rows],
            "notes": [_shown(n) for n in await self.notes()],
            "caps": {
                "instructions": rules.MAX_ACTIVE,
                "instruction_chars": rules.MAX_CHARS,
                "total_chars": rules.MAX_TOTAL,
                "note_chars": rules.NOTE_MAX_CHARS,
            },
            "total_chars": sum(len(r["text"]) for r in rows),
        }


def _note_parts(text: str, title: str | None) -> tuple[str, str | None]:
    body = rules.clean_note(text)
    head = rules.clean(title or "") or None
    if not body:
        raise _invalid("the note is empty")
    if len(body) > rules.NOTE_MAX_CHARS:
        raise _invalid(f"a note holds at most {rules.NOTE_MAX_CHARS} characters (this one has {len(body)})")
    if head is not None and len(head) > rules.TITLE_MAX_CHARS:
        raise _invalid(f"a title holds at most {rules.TITLE_MAX_CHARS} characters")
    return body, head


async def _known(conn: AsyncConnection, device: uuid.UUID | None) -> uuid.UUID | None:
    """A device since revoked keeps no link (SET NULL), never a failed write."""
    if device is None:
        return None
    hit = await (await conn.execute("SELECT 1 FROM devices WHERE id = %s", (device,))).fetchone()
    return device if hit else None
