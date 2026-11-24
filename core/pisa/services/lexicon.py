"""The user's lexicon (report-3 §4): the names Pisa knows, from Pisa's own tables, for the understanding
step and the iPhone's contextual strings.

- today's plan (events today and their places, tasks due today and theirs) and home: weight 1.0 / 0.9;
- this week's events and their venues, open tasks and their places: 0.7;
- places visited in the last 30 days: 0.5;
- people: email senders who wrote as a person in the last 90 days, most recent first: 0.5;
- places in the profile (likes, routines): 0.3.

Rebuilt when one of those tables changed or the day did (a cheap fingerprint query per utterance), else kept
in memory. Names never go to the logs: counts only.
"""

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.db.settings import get_setting, set_setting
from pisa.domain import understand as rules
from pisa.domain.mail import AUTOMATED, NEVER_TO_MODEL_LABELS  # AUTOMATED: never a name in the lexicon
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services import foreign
from pisa.services import home as home_store
from pisa.services.places import city_of

_log = get_logger("pisa.lexicon")
FIXED_KEY = "understand_fixed"  # {name: when Pisa last fixed it}: the hints' fifth tier (14 days)
FIXED_DAYS = 14
ENTRIES_MAX = 400
PEOPLE_MAX = 80

# What the lexicon is built from, as a cache key: these tables' latest changes, its own fixes' key, and
# (asked of its owner, `home.version`) their home.
_FINGERPRINT = (
    "SELECT (SELECT max(updated_at) FROM places), (SELECT max(updated_at) FROM tasks), "
    "(SELECT max(updated) FROM calendar_events), (SELECT count(*) FROM calendar_events), "
    "(SELECT max(arrived_at) FROM stays), (SELECT max(sent_at) FROM email_messages), "
    "(SELECT updated_at FROM app_settings WHERE key = %s), "
    "(SELECT max(updated_at) FROM profile_claims)"
)


def _person(name: str | None, address: str | None) -> str | None:
    """A sender's name when it reads like a person's ("Nino Ferri"), else None."""
    if not name or not address or AUTOMATED.match(address.split("@")[0]):
        return None
    clean = " ".join(name.replace('"', " ").split())
    if not 2 <= len(clean) <= rules.NAME_MAX or any(c.isdigit() for c in clean) or "@" in clean:
        return None
    if len(clean.split()) > 4:
        return None
    return clean


class LexiconService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._lock = asyncio.Lock()
        self._key: tuple[Any, ...] | None = None
        self._built: tuple[rules.Lexicon, list[rules.Entry], datetime] | None = None

    async def current(self) -> tuple[rules.Lexicon, list[rules.Entry], datetime]:
        """The lexicon (indexed), its entries in the hints' order, and when it was built."""
        async with self._pool.connection() as conn:
            row = await (await conn.execute(_FINGERPRINT, (FIXED_KEY,))).fetchone()
            key = (self._owner.day(self._clock()), await home_store.version(conn), *(row or ()))
            if self._built is not None and key == self._key:
                return self._built
            async with self._lock:
                if self._built is None or key != self._key:
                    entries = await self._entries(conn)
                    self._built = (
                        rules.lexicon(entries, self._owner.day(self._clock())),
                        entries,
                        self._clock(),
                    )
                    self._key = key
                    _log.info("lexicon_built", entries=len(entries))
            return self._built

    def latest(self) -> tuple[rules.Lexicon, list[rules.Entry], datetime] | None:
        """The last lexicon built, without asking the database (for a turn that can't wait for a rebuild)."""
        return self._built

    async def fixed(self, conn: AsyncConnection) -> frozenset[str]:
        value = await get_setting(conn, FIXED_KEY) or {}
        since = self._clock() - timedelta(days=FIXED_DAYS)
        return frozenset(n for n, at in value.items() if datetime.fromisoformat(at) >= since)

    async def note_fixed(self, names: list[str]) -> None:
        """Names Pisa fixed in what the user said: they rank higher in the hints for 14 days."""
        if not names:
            return
        async with self._pool.connection() as conn, conn.transaction():
            value = await get_setting(conn, FIXED_KEY) or {}
            now = self._clock().isoformat()
            value.update({n[: rules.NAME_MAX]: now for n in names})
            newest = sorted(value.items(), key=lambda kv: kv[1], reverse=True)[: rules.HINTS_MAX]
            await set_setting(conn, FIXED_KEY, Jsonb(dict(newest)))

    async def hints(self, zipf: rules.Zipf) -> dict[str, Any]:
        """GET /v1/speech/hints: the 100 best names for the iPhone's recogniser."""
        _, entries, built = await self.current()
        async with self._pool.connection() as conn:
            fixed = await self.fixed(conn)
        return {
            "hints": rules.hints(entries, zipf, fixed),
            "updated_at": built.astimezone(self._owner.zone).isoformat(),
        }

    # --- from Pisa's tables
    # -----------------------------------------------------------------------------------

    async def _entries(self, conn: AsyncConnection) -> list[rules.Entry]:
        now = self._clock()
        today = self._owner.day(self._clock())
        start = datetime.combine(today, datetime.min.time(), tzinfo=self._owner.zone)
        out: dict[str, rules.Entry] = {}

        def add(
            name: str | None, kind: rules.Kind, why: rules.Why, city: str | None = None, outside: bool = False
        ) -> None:
            name = " ".join((name or "").split())[: rules.NAME_MAX]
            if len(rules.orth(name)) < 3:
                return
            known = out.get(name)
            if known is not None and rules.TIERS[known.why] <= rules.TIERS[why]:
                if outside and not known.outside:  # a stranger wrote it too: outside
                    out[name] = dataclasses.replace(known, outside=True)
                return
            outside = outside or bool(known is not None and known.outside)
            out[name] = rules.Entry(name, kind, rules.forms_of(name, kind, city), city or None, why, outside)

        home = await home_store.read(conn)
        events = await (
            await conn.execute(
                # a title a stranger controls (foreign.FOREIGN_EVENT) is an outside entry:
                # never fixed into their words by the rules alone (the email senders' rule)
                "SELECT e.title, e.starts_at < %s, p.name, p.address, "  # noqa: S608 (constant fragments)
                f"coalesce({foreign.FOREIGN_EVENT}, true) FROM {foreign.EVENTS} "
                # an event Pisa made teaches no word or place (Pisa's title is Pisa's words)
                "LEFT JOIN places p ON p.id = e.place_id WHERE e.status <> 'cancelled' AND NOT "
                "e.made_by_pisa AND e.ends_at > %s AND e.starts_at < %s ORDER BY e.starts_at",
                (start + timedelta(days=1), start - timedelta(days=3), start + timedelta(days=7)),
            )
        ).fetchall()
        tasks = await (
            await conn.execute(
                "SELECT t.title, t.due_at IS NOT NULL AND t.due_at < %s, p.name, p.address, "  # noqa: S608
                f"{foreign.FOREIGN_TASK} FROM tasks t "
                "LEFT JOIN places p ON p.id = t.place_id WHERE t.status = 'open' ORDER BY t.updated_at DESC "
                "LIMIT 100",
                (start + timedelta(days=1),),
            )
        ).fetchall()
        visited = await (
            await conn.execute(
                "SELECT p.name, p.address FROM stays s JOIN places p ON p.id = s.place_id "
                "WHERE s.arrived_at > %s GROUP BY p.id, p.name, p.address ORDER BY max(s.arrived_at) DESC "
                "LIMIT 100",
                (now - timedelta(days=30),),
            )
        ).fetchall()
        people = await (
            await conn.execute(
                # Never spam, promotions or social mail.
                "SELECT from_name, from_address FROM email_messages WHERE sent_at > %s "
                "AND from_name IS NOT NULL AND NOT (coalesce(labels, '{}') && %s::text[]) "
                "GROUP BY from_name, from_address ORDER BY max(sent_at) DESC LIMIT %s",
                (now - timedelta(days=90), sorted(NEVER_TO_MODEL_LABELS), PEOPLE_MAX * 2),
            )
        ).fetchall()
        liked = await (
            await conn.execute(
                "SELECT p.name, p.address FROM profile_claims c JOIN places p "
                "ON c.subject_key = 'place:' || p.id::text WHERE c.state <> 'retired' LIMIT 100"
            )
        ).fetchall()
        if home is not None and home.name:
            add(home.name, "place", "home")
        for title, is_today, place, address, strangers in events:
            why: rules.Why = "today" if is_today else "week"
            add(title, "event", why, outside=bool(strangers))
            add(place, "place", why, city_of(address))  # a resolved place's name
        for title, due_today, place, address, from_mail in tasks:
            add(title, "task", "today" if due_today else "week", outside=bool(from_mail))
            add(place, "place", "today" if due_today else "week", city_of(address))
        for place, address in visited:
            add(place, "place", "visited", city_of(address))
        persons = [p for name, address in people if (p := _person(name, address))]
        for person in list(dict.fromkeys(persons))[:PEOPLE_MAX]:
            add(person, "person", "contact", outside=True)
        for place, address in liked:
            add(place, "place", "known", city_of(address))
        return list(out.values())[:ENTRIES_MAX]
