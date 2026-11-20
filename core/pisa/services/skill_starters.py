"""The research's first skills (`domain.skill_starters`), offered after Pisa answered one of their
questions, and kept on the user's yes to that offer. Keeping one is the own path (`WishService.wish`): the
recipe is checked against the read-only allowlist, tried on their data now, and turned on only after "Lo
accendo?", their yes and Face ID. Offered once: never again after Pisa named it in the last week, never when
they keep it already."""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from psycopg_pool import AsyncConnectionPool

from pisa.domain import autonomy as yes_rules
from pisa.domain import skill_starters as rules
from pisa.domain import talk as talk_rules
from pisa.domain import views as view_rules
from pisa.domain.owner import Owner
from pisa.services.wishes import WishService

OFFERED_WITHIN = timedelta(days=7)  # Pisa named it lately: no second offer
ASKED_WITHIN = timedelta(minutes=10)  # their yes answers Pisa's offer of the last ten minutes


class StarterService:
    def __init__(
        self, pool: AsyncConnectionPool, wishes: WishService, *, owner: Owner, clock: Callable[[], datetime]
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._wishes = wishes
        self._clock = clock

    async def offer(self, tool: str) -> str | None:
        starter = rules.for_tool(tool, self._owner.country)
        if starter is None:
            return None
        async with self._pool.connection() as conn:
            kept = await (
                await conn.execute(
                    "SELECT 1 FROM pisa_skills WHERE name_key = %s", (view_rules.name_key(starter.name),)
                )
            ).fetchone()
            said = await (
                await conn.execute(
                    "SELECT 1 FROM messages WHERE role = 'assistant' AND created_at >= %s "
                    "AND content ILIKE %s LIMIT 1",
                    (self._clock() - OFFERED_WITHIN, f"%{starter.name}%"),
                )
            ).fetchone()
        return None if kept is not None or said is not None else starter.name

    async def keep(
        self, name: str, words: str, *, outside: bool, turn_id: str | None, device_id: Any
    ) -> tuple[str, dict[str, Any] | None]:
        from pisa.services.conversation import current_turn  # the turn asking (no import cycle at load)

        live = current_turn.get()
        # their yes is this turn's own words, never joined to the question they asked before Pisa's offer
        own = live.text if live is not None else words
        if outside or not yes_rules.accepts(own):
            return "Not kept: a skill is kept only on the user's own yes this turn to Pisa's offer.", None
        starter = rules.named(name, self._owner.country)
        if starter is None:
            known = ", ".join(s.name for s in rules.offered(self._owner.country))
            return f"Not kept: no offered skill called {name[:40]!r} (Pisa offers: {known}).", None
        async with self._pool.connection() as conn:
            last = await (
                await conn.execute(
                    "SELECT content FROM messages WHERE role = 'assistant' AND created_at >= %s "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (self._clock() - ASKED_WITHIN,),
                )
            ).fetchone()
        if last is None or not talk_rules.names_skill(starter.name, str(last[0]).casefold()):
            return (
                f'Not kept: Pisa\'s last words did not offer "{starter.name}". Offer it first, naming it.',
                None,
            )
        device = device_id if isinstance(device_id, uuid.UUID) else None
        return await self._wishes.wish(
            starter.name, starter.made_from, [dict(c) for c in starter.checks], starter.recipe,
            hosts=set(), outside=False, turn_id=turn_id, device_id=device,
        )  # fmt: skip
