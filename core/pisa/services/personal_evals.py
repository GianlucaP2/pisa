"""Every complaint becomes a test: the user's corrections of Pisa kept as private replay
candidates (`personal_evals`), on their server only.

- `heard`: at the start of a turn, when their words correct Pisa ("no, ti ho detto…", "hai sbagliato", "non è
  quello che ho chiesto"), the exchange they correct is kept: their words, Pisa's reply and moves, the
  correction, and the day's world (event and task titles), minimised (`domain.wishes.minimise`: a name, an
  address, a link or a number stays only if they said it; no email ever).
- `scan_undos`: a calendar change they undid within 2 minutes of Pisa making it.
- `export`: the candidates in the replay's scenario format, for the maintenance job `personal-evals` to write
  to a root-only file on the host (never the repo, never CI); `scenarios` gives them to `brain-replay
  --personal`.
Logs carry counts and kinds only.
"""

import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import understand
from pisa.domain import wishes as rules
from pisa.domain.owner import Owner
from pisa.logging import get_logger

_log = get_logger("pisa.personal_evals")
UNDO_WITHIN = timedelta(minutes=2)
# a turn that read outside text: a data block of an outside kind in its tool calls
_OUTSIDE = re.compile(r"<(?:email|web|map|reviews|briefing|attend|history|page|places)[a-z-]*-[0-9a-f]{16}>")
SCAN_BACK = timedelta(days=2)
WORLD_MAX = 10


class PersonalEvals:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Callable[[], datetime]) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock

    def _when(self, moment: datetime) -> str:
        return f"{moment.astimezone(self._owner.zone):%Y-%m-%dT%H:%M}"

    async def _world(self, conn: AsyncConnection, at: datetime, said: str) -> dict[str, Any]:
        tz = self._owner.zone
        day = at.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        events = await (
            await conn.execute(
                "SELECT title, starts_at, ends_at FROM calendar_events WHERE status <> 'cancelled' "
                "AND starts_at < %s AND ends_at > %s ORDER BY starts_at LIMIT %s",
                (day + timedelta(days=2), day, WORLD_MAX),
            )
        ).fetchall()
        tasks = await (
            await conn.execute(
                "SELECT title FROM tasks WHERE status = 'open' ORDER BY created_at DESC LIMIT %s",
                (WORLD_MAX,),
            )
        ).fetchall()
        return {
            "events": [
                [f"p{n}", rules.minimise(t, said), self._when(s), self._when(e), []]
                for n, (t, s, e) in enumerate(events)
            ],
            "tasks": [rules.minimise(t, said) for (t,) in tasks],
        }

    async def heard(self, device_id: uuid.UUID, words: str, lang: str) -> bool:
        """Their words this turn: when they correct Pisa, the exchange they correct becomes a candidate."""
        if not rules.is_complaint(words):
            return False
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    "SELECT m.id, m.role, m.content, m.created_at, coalesce(m.model_messages::text, ''), "
                    "m.corrections "
                    "FROM messages m JOIN conversations c "
                    "ON c.id = m.conversation_id WHERE c.device_id = %s AND m.created_at >= %s "
                    "ORDER BY m.created_at DESC, m.id DESC LIMIT 4",
                    (device_id, now - timedelta(hours=1)),
                )
            ).fetchall()
            reply = next((r for r in rows if r[1] == "assistant"), None)
            asked = next((r for r in rows if r[1] == "user" and reply is not None and r[3] <= reply[3]), None)
            if reply is None or asked is None:
                return False
            # a skill Pisa ran in the exchange they correct is a corrected use (the ledger's,
            # shadow)
            await conn.execute(
                "UPDATE pisa_skill_uses SET outcome = 'corrected' WHERE outcome = 'kept' AND how <> "
                "'schedule' AND used_at BETWEEN %s AND %s",
                (asked[3] - timedelta(seconds=1), reply[3] + timedelta(seconds=1)),
            )
            # their turn is their recognised words (`content`), never Pisa's `meant`; the
            # world keeps what their words name, with the understanding step's corrections
            said = f"{understand.his_words(asked[2], asked[5])} {words}"
            moves = await (
                await conn.execute(
                    "SELECT move, tool, outcome, provenance FROM pisa_decisions WHERE device_id = %s "
                    "AND at >= %s AND at <= %s ORDER BY n",
                    (device_id, asked[3] - timedelta(seconds=1), reply[3] + timedelta(seconds=1)),
                )
            ).fetchall()
            outside = any(p == "outside_text" for _, _, _, p in moves) or bool(_OUTSIDE.search(reply[4]))
            scenario = {
                "id": f"personal-{uuid.uuid4().hex[:8]}",
                "now": self._when(asked[3]),
                "lang": lang,
                "max_questions": 1,
                "world": await self._world(conn, asked[3], said),
                "turns": [
                    {"owner": asked[2]},
                    {"owner": words, "advance_s": max(1, int((now - reply[3]).total_seconds()))},
                ],
                "script": [None, None],
                "expect": {},
                "personal": {
                    "kind": "correction",
                    # Pisa's reply after reading outside text may paraphrase it: only its moves
                    # stay
                    "pisa_said": None if outside else rules.minimise(reply[2], said),
                    "pisa_moves": [{"move": m, "tool": t, "outcome": o} for m, t, o, _ in moves],
                },
            }
            made = await conn.execute(
                "INSERT INTO personal_evals (id, kind, source_ref, lang, scenario, made_at) "
                "VALUES (%s, 'correction', %s, %s, %s, %s) ON CONFLICT (source_ref) DO NOTHING",
                (uuid.uuid4(), f"turn:{reply[0]}", lang, Jsonb(scenario), now),
            )
        if made.rowcount:
            _log.info("personal_eval_kept", kind="correction")
        return bool(made.rowcount)

    async def scan_undos(self) -> int:
        """A calendar change undone within 2 minutes of Pisa making it: a candidate (once per approval)."""
        now = self._clock()
        kept = 0
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT a.id::text, a.kind, a.summary, a.lang, a.executed_at, "
                    "coalesce(a.payload->>'owner_words', '') "
                    "FROM approvals a WHERE a.undone_at IS NOT NULL AND a.executed_at IS NOT NULL "
                    "AND a.undone_at - a.executed_at <= %s AND a.undone_at >= %s AND NOT EXISTS "
                    "(SELECT 1 FROM personal_evals p WHERE p.source_ref = 'approval:' || a.id::text)",
                    (UNDO_WITHIN, now - SCAN_BACK),
                )
            ).fetchall()
            for approval_id, kind, summary, lang, at, words in rows:
                scenario = {
                    "id": f"personal-{uuid.uuid4().hex[:8]}",
                    "now": self._when(at),
                    "lang": lang,
                    "max_questions": 1,
                    "world": await self._world(conn, at, words),
                    "turns": [{"owner": words}] if words else [],
                    "script": [None] if words else [],
                    "expect": {},
                    "personal": {
                        "kind": "undo",
                        "pisa_moves": [
                            {"move": "act", "tool": kind, "outcome": rules.minimise(summary, words)}
                        ],
                    },
                }
                await conn.execute(
                    "INSERT INTO personal_evals (id, kind, source_ref, lang, scenario, made_at) "
                    "VALUES (%s, 'undo', %s, %s, %s, %s) ON CONFLICT (source_ref) DO NOTHING",
                    (uuid.uuid4(), f"approval:{approval_id}", lang, Jsonb(scenario), now),
                )
                kept += 1
        if kept:
            _log.info("personal_eval_kept", kind="undo", count=kept)
        return kept

    async def counts(self) -> dict[str, int]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT kind, count(*) FROM personal_evals GROUP BY kind")
            ).fetchall()
        return {k: n for k, n in rows}

    async def export(self, *, mark: bool) -> list[dict[str, Any]]:
        """Every candidate as a replay scenario (with turns), oldest first; `mark` records the export."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT id, scenario FROM personal_evals ORDER BY made_at")
            ).fetchall()
            if mark and rows:
                await conn.execute(
                    "UPDATE personal_evals SET exported_at = %s WHERE id = ANY(%s)",
                    (self._clock(), [r[0] for r in rows]),
                )
        return [s for _, s in rows if s.get("turns")]


async def scenarios(conn: AsyncConnection) -> list[dict[str, Any]]:
    """For `brain-replay --personal` (never CI): the candidates with turns, marked personal."""
    rows = await (await conn.execute("SELECT scenario FROM personal_evals ORDER BY made_at")).fetchall()
    return [s | {"personal_run": True} for (s,) in rows if s.get("turns")]
