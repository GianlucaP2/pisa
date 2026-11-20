"""Pisa's memory of their days. The
only module that names `episodes`, `episode_links`, `pisa_explicit` and `episode_tombstones`.

**The night's first steps** (`night.STEPS`, inside the one night: its 03:30 in their zone, its "ferma tutto",
its ledger budget `night` and its €0.25; no second night):
- **Segment** (`e1_segment`): the last 3 local days closed into episodes by code: a stay ended (at a place
  Pisa knows, or at home), a talk closed (30 minutes without a message), an event ended, a trip leg ended;
  each cut at midnight in their zone; a day's frame with its emails (id and sender class only). Never at a
  sensitive place, never sensitive words, never an off-limits topic; never a key they had forgotten
  (`pisa_explicit = -1`). Neighbours link only through a shared person, place or thread, within 6 h.
- **Score** (`e2_score`): salience = novelty + their explicit + |outcome_error| (the talk's moves).
- **Digest** (`e3_digest`): ≤ 2 lines each; by code from pointers, a talk's from their own words by the
  model (≤ €0.02 a night, inside the night's €0.25), kept only when every name and number in it is theirs.
- **Downscale** (`e4_downscale`): automatic recall holds a fixed budget; what they retrieved or confirmed
  since the last night gains stability (its rivals don't); the rest fade (found when they ask to look harder).

**recall(cue)** (`recall`, the deferred tool): only on their own question about their past; keys only from
their words; one of the four answers. **Their words about what it answered** ("ricordatelo", "non
ricordarmelo", "no, era con Nino"): by code, only right after recall answered one episode (`heard`).

Nothing here enters a prompt but the tool's own result; nothing reaches them unasked. Logs carry ids and
counts.
"""

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.brain.episodes import Digest
from pisa.domain import episodes as rules
from pisa.domain import geo_rules as geo
from pisa.domain.errors import PisaError
from pisa.domain.health import health_kinds, visit_kinds
from pisa.domain.learning import sensitive_place
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m, text_key
from pisa.domain.profile import plain
from pisa.infra import locks
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services.foreign import EVENTS, FOREIGN_EVENT
from pisa.services.night_step import NightStep
from pisa.services.stop import stopped_now

_log = get_logger("pisa.episodes")
TRACE = "episodes"  # the digest calls' trace prefix: `episodes-<night>-<n>`


@dataclass
class Recalled:
    """What recall last answered on a device's turn: the phrases' path."""

    turn_id: str
    answer: str
    ids: tuple[str, ...]
    at: datetime  # the path closes 10 minutes after recall answered


@dataclass
class _Draft:
    key: str
    kind: str
    day: date
    started_at: datetime
    ended_at: datetime
    place_id: str | None = None
    home: bool = False
    links: list[tuple[str, str, str, str | None]] | None = (
        None  # (target_kind, target_id, role, sender_class)
    )
    blocks: tuple[str, ...] = ()  # more keys that, forgotten, keep it unwritten (its base, its messages)

    def keys(self) -> list[str]:
        base = rules.base_key(self.key)
        return [self.key, *([base] if base else []), *self.blocks]


class EpisodeService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        digest: Digest | None = None,
    ) -> None:
        self._pool = pool
        self.step = NightStep(pool, name="episodes", trace=TRACE)  # the frame's stop and spend
        self._owner = owner
        self._clock = clock
        self._digest = digest
        self._recalled: dict[uuid.UUID, Recalled] = {}
        self._hashes: dict[str, str] = {}  # an episode their words just kept or forgot → its key's hash
        self._talk_of: dict[str, tuple[str, int, int]] = {}  # a talk being forgotten → its message range

    def steps(self) -> dict[str, Callable[[date], Awaitable[int]]]:
        return {
            "e1_segment": self.segment,
            "e2_score": self.score,
            "e3_digest": self.digest,
            "e4_downscale": self.downscale,
        }

    # --- segmentation by code
    # -----------------------------------------------------------------------------

    async def segment(self, night: date) -> int:
        """The last 3 local days, closed into episodes (idempotent by key). Returns the episodes written."""
        tz, now = self._owner.zone, self._clock()
        first = night - timedelta(days=rules.LOOK_BACK_DAYS)
        start, _ = rules.day_bounds(first, tz)
        _, end = rules.day_bounds(night - timedelta(days=1), tz)
        written = 0
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.EPISODES,))
            if await stopped_now(conn):  # RAIL: a stop said mid-night: nothing written
                return 0
            blocked = {
                h
                for (h,) in await (
                    await conn.execute("SELECT key_hash FROM pisa_explicit WHERE explicit = -1")
                ).fetchall()
            }
            drafts = await self._drafts(conn, start, end, min(end, now), tz)
            made: list[str] = []
            for d in drafts:
                if d.day < first or d.day >= night or any(rules.key_hash(k) in blocked for k in d.keys()):
                    continue  # its base key too (another zone's midnight): its messages
                row = await (
                    await conn.execute(
                        "INSERT INTO episodes (id, key, kind, day, started_at, ended_at, tz, place_id, home, "
                        "created_night, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (key) DO NOTHING RETURNING id",
                        (
                            uuid.uuid4(),
                            d.key,
                            d.kind,
                            d.day,
                            d.started_at,
                            d.ended_at,
                            str(tz),
                            d.place_id,
                            d.home,
                            night,
                            now,
                        ),
                    )
                ).fetchone()
                if row is None:  # written by an earlier night: never touched again (their corrections hold)
                    continue
                written += 1
                made.append(str(row[0]))
                for kind, target, role, sender in d.links or []:
                    await conn.execute(
                        "INSERT INTO episode_links (episode_id, target_kind, target_id, role, source, "
                        "sender_class) VALUES (%s, %s, %s, %s, 'code', %s) ON CONFLICT DO NOTHING",
                        (row[0], kind, target, role, sender),
                    )
            for (day,) in await (  # a day's frame only for a day that has an episode now
                await conn.execute(
                    "SELECT DISTINCT day FROM episodes WHERE kind <> 'day' AND day >= %s AND day < %s "
                    "ORDER BY day",
                    (first, night),
                )
            ).fetchall():
                written += await self._day(conn, day, tz, night, now, blocked)
            linked = await self._neighbours(conn, first, night)
        _log.info("episodes_segmented", written=written, near=linked)
        return written

    async def _drafts(
        self, conn: AsyncConnection, start: datetime, end: datetime, until: datetime, tz: ZoneInfo
    ) -> list[_Draft]:
        places = {
            str(pid): (name, category)
            for pid, name, category in await (
                await conn.execute("SELECT id, name, category FROM places")
            ).fetchall()
        }
        home = await home_store.read(conn)
        off = [
            plain(t)
            for (t,) in await (
                await conn.execute("SELECT topic FROM profile_off_limits WHERE topic IS NOT NULL")
            ).fetchall()
            if t
        ]

        def sensitive_at(place_id: str | None) -> bool:
            return place_id is not None and sensitive_place(*places.get(place_id, (None, None)))

        def unsaid(words: str) -> bool:  # RAIL: sensitive stems, an off-limits topic of theirs
            folded = f" {plain(words)} "
            return rules.sensitive_words(words) or any(f" {t} " in folded for t in off)

        drafts: list[_Draft] = []
        # stays: at a place Pisa knows or at their home; never at a sensitive place
        stays = await (
            await conn.execute(
                "SELECT device_id, arrived_at, departed_at, lat, lon, place_id FROM stays WHERE departed_at "
                "IS NOT NULL AND departed_at > %s AND arrived_at < %s AND departed_at <= %s ORDER BY "
                "arrived_at",
                (start, end, until),
            )
        ).fetchall()
        spans: list[
            tuple[datetime, datetime, str | None, bool, bool]
        ] = []  # (from, to, place, home, sensitive)
        for device, arrived, departed, lat, lon, place_id in stays:
            pid = str(place_id) if place_id else None
            at_home = home is not None and distance_m((lat, lon), (home.lat, home.lon)) <= geo.HOME_M
            bad = sensitive_at(pid)
            spans.append((arrived, departed, pid, at_home, bad))
            if bad or (pid is None and not at_home):
                continue
            for day, a, b in rules.split(arrived, departed, tz):
                drafts.append(
                    _Draft(
                        f"stay:{device}:{int(arrived.timestamp())}:{day}",
                        "stay",
                        day,
                        a,
                        b,
                        pid,
                        at_home,
                        [("place", pid, "at", None)] if pid else [],
                    )
                )

        def where(a: datetime, b: datetime) -> tuple[str | None, bool, bool]:
            for s, e, pid, at_home, bad in spans:
                if s < b and a < e:
                    return pid, at_home, bad
            return None, False, False

        # talks: their words, closed 30 minutes after the last message, cut at midnight
        rows = await (
            await conn.execute(
                "SELECT id, conversation_id, role, created_at, CASE WHEN role = 'user' THEN content ELSE '' "
                "END "
                "FROM messages WHERE created_at >= %s AND created_at < %s ORDER BY created_at, id",
                (start - rules.TALK_GAP, end),
            )
        ).fetchall()
        messages = [rules.Message(int(i), str(c), str(r), at, str(w or "")) for i, c, r, at, w in rows]
        for talk in rules.talks(messages, tz, self._clock()):  # a day's piece ends at its midnight anyway
            pid, at_home, bad = where(talk.started_at, talk.ended_at)
            if bad or not talk.his or unsaid(" ".join(talk.his)):
                continue
            links: list[tuple[str, str, str, str | None]] = [
                ("message_range", f"{talk.conversation_id}:{talk.first_id}-{talk.last_id}", "said", None)
            ]
            if pid:
                links.append(("place", pid, "at", None))
            for (decision,) in await (
                await conn.execute(
                    "SELECT id FROM pisa_decisions WHERE at >= %s AND at <= %s AND provenance = "
                    "'owner_words' ORDER BY n LIMIT 20",
                    (talk.started_at, talk.ended_at + timedelta(minutes=5)),
                )
            ).fetchall():
                links.append(("decision", str(decision), "made", None))
            drafts.append(
                _Draft(
                    talk.key,
                    "talk",
                    talk.day,
                    talk.started_at,
                    talk.ended_at,
                    pid,
                    at_home,
                    links,
                    tuple(rules.message_key(talk.conversation_id, i) for i in talk.ids),
                )
            )
        # events: ended, not all day, not cancelled; an invite's title is outside text (never read here)
        people = await self._people(conn)
        events = await (
            await conn.execute(
                f"SELECT e.calendar_id, e.id, e.title, e.starts_at, e.ends_at, e.location, e.guests, "  # noqa: S608
                f"{FOREIGN_EVENT} FROM {EVENTS} WHERE NOT e.all_day AND e.status <> 'cancelled' "
                "AND e.ends_at > %s AND e.starts_at < %s AND e.ends_at <= %s ORDER BY e.starts_at",
                (start, end, until),
            )
        ).fetchall()
        for cal, eid, title, starts, ends, location, guests, foreign in events:
            if unsaid(str(title or "")) or (location and sensitive_place(str(location), None)):
                continue
            pid = None
            if location:
                found = await (
                    await conn.execute(
                        "SELECT place_id FROM place_resolutions WHERE text_key = %s",
                        (text_key(str(location)),),
                    )
                ).fetchone()
                pid = str(found[0]) if found and found[0] else None
            if sensitive_at(pid):
                continue
            links = [("event", f"{cal}|{eid}", "about", None)]
            if pid:
                links.append(("place", pid, "at", None))
            emails = {str(g.get("email") or "").lower() for g in (guests or []) if isinstance(g, dict)}
            for person, (name, mails, _) in people.items():
                if emails & mails or (not foreign and rules.person_said(name, str(title or ""))):
                    links.append(("person", person, "with", None))
            for day, a, b in rules.split(starts, ends, tz):
                drafts.append(
                    _Draft(
                        f"event:{cal}:{eid}:{int(starts.timestamp())}:{day}",
                        "event",
                        day,
                        a,
                        b,
                        pid,
                        False,
                        links,
                    )
                )
        # trip legs: their confirmed trips only (a doubted one is not theirs yet)
        for trip_id, legs in await (
            await conn.execute("SELECT id, legs FROM trips WHERE status = 'planned' AND doubt IS NULL")
        ).fetchall():
            for leg in legs or []:
                try:
                    a = datetime.fromisoformat(str(leg["at"]))
                    b = datetime.fromisoformat(str(leg.get("until") or leg["at"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if not (start < b <= until) or b < a:
                    continue
                # keyed by the leg (its key, else its email and time), never by the trip: a
                # leg whose trip merged into another since is the same memory. The key it had by its trip
                # blocks it too, so a leg they forgot under that key stays forgotten.
                leg_key = leg.get("key") or f"{leg.get('email_id') or ''}@{int(a.timestamp())}"
                by_trip = f"trip:{trip_id}:{leg.get('key') or int(a.timestamp())}"
                for day, x, y in rules.split(a, b, tz):
                    drafts.append(
                        _Draft(
                            f"trip:{leg_key}:{day}",
                            "trip_leg",
                            day,
                            x,
                            y,
                            None,
                            False,
                            [("trip", str(trip_id), "about", None)],
                            blocks=(f"{by_trip}:{day}", by_trip),
                        )
                    )
        return drafts

    async def _people(self, conn: AsyncConnection) -> dict[str, tuple[str, set[str], bool]]:
        """Their people (not hidden): id → (name, e-mails, the name is theirs: they wrote it, or set it)."""
        return {
            str(pid): (
                str(name),
                {str(e).lower() for e in emails or ()},
                source == "owner" or "name" in (fields or ()),
            )
            for pid, name, emails, source, fields in await (
                await conn.execute(
                    "SELECT id, name, emails, source, owner_fields FROM people WHERE NOT hidden"
                )
            ).fetchall()
        }

    async def _day(
        self, conn: AsyncConnection, day: date, tz: ZoneInfo, night: date, now: datetime, blocked: set[str]
    ) -> int:
        """The day's frame (midnight to midnight in their zone): its emails as pointers with the sender's
        class, never a word of them; Pisa's own sent mail is Pisa's and is no pointer of theirs."""
        key = f"day:{day.isoformat()}"
        if rules.key_hash(key) in blocked:
            return 0
        start, end = rules.day_bounds(day, tz)
        row = await (
            await conn.execute(
                "INSERT INTO episodes (id, key, kind, day, started_at, ended_at, tz, created_night, "
                "created_at) "
                "VALUES (%s, %s, 'day', %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING RETURNING id",
                (uuid.uuid4(), key, day, start, end, str(tz), night, now),
            )
        ).fetchone()
        if row is None:
            return 0
        own = {
            str(a).lower()
            for (a,) in await (
                await conn.execute("SELECT account FROM connector_accounts WHERE account IS NOT NULL")
            ).fetchall()
        }
        contacts = {m for _, mails, _ in (await self._people(conn)).values() for m in mails}
        for mid, sender in await (
            await conn.execute(
                "SELECT id, lower(coalesce(from_address, '')) FROM email_messages WHERE sent_at >= %s "
                "AND sent_at < %s AND id NOT IN (SELECT gmail_message_id FROM pisa_sent_mail) ORDER BY "
                "sent_at LIMIT %s",
                (start, end, rules.EMAILS_A_DAY),
            )
        ).fetchall():
            klass = "own" if sender in own else "contact" if sender in contacts else "other"
            await conn.execute(
                "INSERT INTO episode_links (episode_id, target_kind, target_id, role, source, sender_class) "
                "VALUES (%s, 'email', %s, 'about', 'code', %s) ON CONFLICT DO NOTHING",
                (row[0], str(mid), klass),
            )
        return 1

    async def _entities(self, conn: AsyncConnection, ids: Sequence[str]) -> dict[str, set[str]]:
        """Each episode's people, places and thread (its conversation), as `kind:id`."""
        found: dict[str, set[str]] = {i: set() for i in ids}
        for eid, kind, target, place_id in await (
            await conn.execute(
                "SELECT e.id, l.target_kind, l.target_id, e.place_id FROM episodes e LEFT JOIN "
                "episode_links l "
                "ON l.episode_id = e.id AND l.target_kind IN ('person', 'place', 'message_range') "
                "WHERE e.id = ANY(%s)",
                (list(ids),),
            )
        ).fetchall():
            bag = found.setdefault(str(eid), set())
            if place_id:
                bag.add(f"place:{place_id}")
            if kind == "message_range":
                bag.add(f"thread:{str(target).split(':')[0]}")
            elif kind:
                bag.add(f"{kind}:{target}")
        return found

    async def _neighbours(self, conn: AsyncConnection, first: date, night: date) -> int:
        """RAIL: two episodes link only through a shared person, place or thread, within 6 hours."""
        rows = await (
            await conn.execute(
                "SELECT id, started_at, ended_at FROM episodes WHERE kind <> 'day' AND day >= %s AND day < "
                "%s",
                (first - timedelta(days=1), night),
            )
        ).fetchall()
        ents = await self._entities(conn, [str(r[0]) for r in rows])
        pairs = rules.neighbours([(str(i), a, b, frozenset(ents.get(str(i), set()))) for i, a, b in rows])
        made = 0
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                cur = await conn.execute(
                    "INSERT INTO episode_links (episode_id, target_kind, target_id, role, source) "
                    "VALUES (%s, 'episode', %s, 'near', 'code') ON CONFLICT DO NOTHING",
                    (x, y),
                )
                made += cur.rowcount
        return made

    # --- salience
    # -----------------------------------------------------------------------------------------

    async def score(self, night: date) -> int:
        """Salience for the last days' episodes (and any they confirmed): novelty + explicit + |error|."""
        since = night - timedelta(days=rules.LOOK_BACK_DAYS + 1)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, kind, day, started_at, ended_at FROM episodes WHERE kind <> 'day' AND "
                    "(day >= %s OR confirmed_at IS NOT NULL)",
                    (since,),
                )
            ).fetchall()
            ents = await self._entities(conn, [str(r[0]) for r in rows])
            explicit = {
                str(e): int(x)
                for e, x in await (
                    await conn.execute(
                        "SELECT episode_id, explicit FROM pisa_explicit WHERE episode_id IS NOT NULL"
                    )
                ).fetchall()
            }
            for eid, kind, day, started, ended in rows:
                mine = {e for e in ents.get(str(eid), set()) if not e.startswith("thread:")}
                before = await (
                    await conn.execute(
                        "SELECT DISTINCT l.target_kind || ':' || l.target_id FROM episode_links l JOIN "
                        "episodes e "
                        "ON e.id = l.episode_id WHERE l.target_kind IN ('person', 'place') AND e.day < %s "
                        "AND e.day >= %s UNION SELECT DISTINCT 'place:' || place_id::text FROM episodes "
                        "WHERE place_id IS NOT NULL AND day < %s AND day >= %s",
                        (
                            day,
                            day - timedelta(days=rules.NOVELTY_DAYS),
                            day,
                            day - timedelta(days=rules.NOVELTY_DAYS),
                        ),
                    )
                ).fetchall()
                seen = {str(r[0]) for r in before}
                error = 0.0
                if kind == "talk":  # through the talk that made the moves (a shared entity, never the clock)
                    got = await (
                        await conn.execute(
                            "SELECT max(abs(value)) FROM pisa_signals WHERE signal = 'outcome_error' AND at "
                            ">= %s "
                            "AND at <= %s",
                            (started, ended + timedelta(minutes=5)),
                        )
                    ).fetchone()
                    error = float(got[0]) if got and got[0] is not None else 0.0
                value = rules.salience(len(mine - seen), len(mine), explicit.get(str(eid), 0), error)
                await conn.execute("UPDATE episodes SET salience = %s WHERE id = %s", (value, eid))
        return len(rows)

    # --- the digest
    # ---------------------------------------------------------------------------------------

    async def digest(self, night: date) -> int:
        """Every undigested episode (at most 40 a night): by code from pointers; a talk's from their words by
        the model while its €0.02 lasts (a talk past it waits for the next night). Returns the digests
        written."""
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            rows = await (
                await conn.execute(
                    "SELECT id, kind, started_at, ended_at, home FROM episodes WHERE digest IS NULL "
                    "AND digested_night IS NULL ORDER BY started_at LIMIT %s",
                    (rules.DIGEST_NIGHT_MAX,),
                )
            ).fetchall()
        written = calls = 0
        capped = False
        for eid, kind, started, ended, at_home in rows:
            a, b = rules.hm(started, tz), rules.hm(ended, tz)
            text, by = await self._code_digest(str(eid), str(kind), a, b, bool(at_home), lang)
            if kind == "talk":
                words = await self._his_words(str(eid))
                if not words:  # their messages are gone (their retention): no "0 messaggi tuoi" digest
                    async with self._pool.connection() as conn:
                        await conn.execute(
                            "UPDATE episodes SET digested_night = %s WHERE id = %s", (night, eid)
                        )
                    continue
                if self._digest is not None and words and not capped:
                    if await self.step.stopped():
                        break
                    spent, dearest = await self.step.spent(self.step.tonight(night))
                    if spent + max(Decimal(rules.EST_CALL_EUR), dearest) > Decimal(rules.DIGEST_CAP_EUR):
                        capped = True
                        continue  # past the digest's €0.02: this talk waits for the next night
                    calls += 1
                    try:
                        said = await self._digest(
                            words[: rules.DIGEST_WORDS_MAX],
                            lang,
                            trace=f"{TRACE}-{night.isoformat()}-{calls}",
                        )
                    except PisaError as err:
                        if err.code == "budget_exceeded":
                            raise  # the night's €0.25: the frame ends the night as `capped`
                        said = ""
                    except Exception as exc:  # a model's bad answer ends this call only
                        _log.info("episodes_digest_failed", error=exc.__class__.__name__)
                        said = ""
                    if rules.digest_ok(said, words):
                        text, by = " ".join(said.split())[: rules.DIGEST_MAX], "model"
                elif self._digest is not None and words and capped:
                    continue
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE episodes SET digest = %s, digest_by = %s, digested_night = %s WHERE id = %s "
                    "AND digest IS NULL",
                    (text, by, night, eid),
                )
                written += cur.rowcount
        _log.info("episodes_digested", written=written, calls=calls, capped=capped)
        return written

    async def _his_words(self, episode_id: str) -> str:
        async with self._pool.connection() as conn:
            link = await (
                await conn.execute(
                    "SELECT target_id FROM episode_links WHERE episode_id = %s AND target_kind = "
                    "'message_range'",
                    (episode_id,),
                )
            ).fetchone()
            if link is None:
                return ""
            conv, span = str(link[0]).split(":", 1)
            lo, hi = (int(x) for x in span.split("-"))
            rows = await (
                await conn.execute(
                    "SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' AND id "
                    "BETWEEN %s "
                    "AND %s ORDER BY id",
                    (conv, lo, hi),
                )
            ).fetchall()
        return "\n".join(str(r[0]) for r in rows)

    async def _code_digest(
        self, eid: str, kind: str, a: str, b: str, at_home: bool, lang: str
    ) -> tuple[str, str]:
        async with self._pool.connection() as conn:
            if kind == "stay":
                return rules.code_digest("stay_home" if at_home else "stay", lang, a=a, b=b), "code"
            if kind == "talk":
                n = await (
                    await conn.execute(
                        "SELECT target_id FROM episode_links WHERE episode_id = %s AND target_kind = "
                        "'message_range'",
                        (eid,),
                    )
                ).fetchone()
                count = 0
                if n:
                    conv, span = str(n[0]).split(":", 1)
                    lo, hi = (int(x) for x in span.split("-"))
                    got = await (
                        await conn.execute(
                            "SELECT count(*) FROM messages WHERE conversation_id = %s AND role = 'user' AND "
                            "id "
                            "BETWEEN %s AND %s",
                            (conv, lo, hi),
                        )
                    ).fetchone()
                    count = int(got[0]) if got else 0
                return rules.code_digest("talk", lang, n=count, a=a, b=b), "code"
            if kind == "event":
                title = await self._own_title(conn, eid)
                if title is None:
                    return rules.code_digest("invite", lang, a=a, b=b), "code"
                return rules.code_digest("event", lang, title=title, a=a, b=b), "code"
            if kind == "trip_leg":
                return rules.code_digest("trip_leg", lang, a=a, b=b), "code"
            counts = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE kind = 'stay'), count(*) FILTER (WHERE kind = 'event'), "
                    "count(*) FILTER (WHERE kind = 'talk') FROM episodes WHERE day = (SELECT day FROM "
                    "episodes "
                    "WHERE id = %s)",
                    (eid,),
                )
            ).fetchone()
            stays, events, talks = counts if counts else (0, 0, 0)
            return rules.code_digest("day", lang, stays=stays, events=events, talks=talks), "code"

    async def _own_title(self, conn: AsyncConnection, eid: str) -> str | None:
        """An event's title when it is their own (never an invite's, a shared calendar's, Gmail's)."""
        link = await (
            await conn.execute(
                "SELECT target_id FROM episode_links WHERE episode_id = %s AND target_kind = 'event'", (eid,)
            )
        ).fetchone()
        if link is None:
            return None
        cal, _, event_id = str(link[0]).partition("|")
        row = await (
            await conn.execute(
                f"SELECT e.title, {FOREIGN_EVENT} FROM {EVENTS} WHERE e.calendar_id = %s AND e.id = %s",  # noqa: S608
                (cal, event_id),
            )
        ).fetchone()
        if row is None or row[1] or rules.sensitive_words(str(row[0] or "")):
            return None
        return " ".join(str(row[0]).split())[:80] or None

    async def _any_title(self, conn: AsyncConnection, eid: str) -> str | None:
        """An event's title, an invite's too, for matching their words only (never returned, never said)."""
        row = await (
            await conn.execute(
                "SELECT e.title FROM episode_links l JOIN calendar_events e "
                "ON l.target_id = e.calendar_id || '|' || e.id "
                "WHERE l.episode_id = %s AND l.target_kind = 'event'",
                (eid,),
            )
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    # --- the recall budget
    # --------------------------------------------------------------------------------

    async def downscale(self, night: date) -> int:
        """What they retrieved or confirmed since the last night gains stability; automatic recall holds the
        fixed budget (protected first); the rest fade. Returns the episodes faded."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE episodes SET stability = stability + %s, gained_at = %s WHERE greatest(retrieved_at, "
                "confirmed_at) > coalesce(gained_at, '-infinity'::timestamptz)",
                (rules.GAIN, now),
            )
            future = {
                f"{c}|{i}"
                for c, i in await (
                    await conn.execute(
                        "SELECT calendar_id, id FROM calendar_events WHERE starts_at > %s", (now,)
                    )
                ).fetchall()
            }
            open_tasks = {
                str(t)
                for (t,) in await (
                    await conn.execute("SELECT id FROM tasks WHERE status = 'open'")
                ).fetchall()
            }
            linked: dict[str, bool] = {}
            for eid, kind, target in await (
                await conn.execute(
                    "SELECT episode_id, target_kind, target_id FROM episode_links WHERE target_kind IN "
                    "('event', 'task')"
                )
            ).fetchall():
                if (kind == "event" and target in future) or (kind == "task" and target in open_tasks):
                    linked[str(eid)] = True
            rows = await (
                await conn.execute(  # a day's frame is no memory of its own: never in the budget
                    "SELECT id, coalesce(salience, 0) + stability, kind FROM episodes WHERE kind <> 'day'"
                )
            ).fetchall()
            keep = rules.kept(
                ((str(i), float(v), linked.get(str(i), False)) for i, v, _ in rows), rules.RECALL_BUDGET
            )
            gone = await self._gone(conn)
            faded = 0
            for eid, _, _kind in rows:
                state = "faded" if str(eid) not in keep else "gist" if str(eid) in gone else "kept"
                cur = await conn.execute(
                    "UPDATE episodes SET state = %s WHERE id = %s AND state <> %s", (state, eid, state)
                )
                faded += cur.rowcount if state == "faded" else 0
        _log.info("episodes_downscaled", kept=len(keep), faded=faded)
        return faded

    async def _gone(self, conn: AsyncConnection) -> set[str]:
        """The talks whose messages their retention setting removed: only the digest and the links remain."""
        found: set[str] = set()
        for eid, target in await (
            await conn.execute(
                "SELECT l.episode_id, l.target_id FROM episode_links l WHERE l.target_kind = 'message_range'"
            )
        ).fetchall():
            conv, span = str(target).split(":", 1)
            first = int(span.split("-")[0])
            row = await (
                await conn.execute(
                    "SELECT 1 FROM messages WHERE id = %s AND conversation_id = %s", (first, conv)
                )
            ).fetchone()
            if row is None:
                found.add(str(eid))
        return found

    async def _health_answer(
        self, asked: set[str], lo: date | None, hi: date | None, part: str | None, today: date, lang: str
    ) -> dict[str, Any]:
        """The stays at a place of the kind they named, on the days they named (else the last week), the
        place and the time only. Episodes never hold these (a sensitive stay makes none)."""
        tz = self._owner.zone
        lo = lo or today - rules.HEALTH_LOOK_BACK
        hi = hi or today
        start, end = rules.day_bounds(lo, tz)[0], rules.day_bounds(hi, tz)[1]
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT p.name, p.category, s.arrived_at, coalesce(s.departed_at, s.last_seen_at) "
                    "FROM stays s JOIN places p ON p.id = s.place_id WHERE s.arrived_at < %s "
                    "AND coalesce(s.departed_at, s.last_seen_at) > %s ORDER BY s.arrived_at",
                    (end, start),
                )
            ).fetchall()
        hits = [
            (str(name), a, b)
            for name, category, a, b in rows
            if health_kinds(f"{name} {category or ''}") & asked and rules.in_part(a, b, part, tz)
        ]
        _log.info("recall_answer", answer="health", found=len(hits))  # counts only, never the place
        answer = "none" if not hits else "one" if len(hits) == 1 else "list"
        return {
            "answer": answer,
            "say": rules.say_health(hits, asked, lo, hi, today, tz, lang),
            "episodes": [],
        }

    # --- recall(cue): the four answers
    # ------------------------------------------------------------------------

    async def recall(
        self,
        cue: str,
        *,
        words: str,
        lang: str,
        device: uuid.UUID | None = None,
        turn_id: str = "",
        day_from: str | None = None,
        day_to: str | None = None,
        deeper: bool = False,
    ) -> dict[str, Any]:
        """RAIL: only on their own question about their past; keys (a person, a place, words) only
        from their words; a sensitive cue abstains with no next step. {answer, say, episodes}."""
        if not rules.asks_past(words):
            _log.info("recall_refused", reason="unprompted")
            return {"answer": "refused", "say": None, "episodes": []}
        tz, now = self._owner.zone, self._clock()
        today = now.astimezone(tz).date()
        asked = visit_kinds(words)
        if asked:  # their own question about their visit to a health place, answered from Pisa's stays
            lo, hi, part = rules.when(words, today)
            if lo is None:
                lo, hi = _iso(day_from), _iso(day_to) or _iso(day_from)
            return await self._health_answer(asked, lo, hi, part, today, lang)
        his_cue = " ".join(w for w in cue.split() if plain(w) and plain(w) in plain(words).split())
        if rules.sensitive_words(words) or rules.sensitive_words(his_cue):  # the cue's words they said
            _log.info("recall_answer", answer="none", found=0, sensitive=True)
            return {"answer": "none", "say": rules.say_none(lang, None), "episodes": []}
        lo, hi, part = rules.when(words, today)
        if lo is None:
            lo, hi = _iso(day_from), _iso(day_to) or _iso(day_from)
            if lo is not None and hi is not None and hi < lo:
                lo, hi = hi, lo
        async with self._pool.connection() as conn:
            people = await self._people(conn)
            persons = {pid for pid, (name, _, _) in people.items() if rules.person_said(name, words)}
            places = {
                str(pid): str(name)
                for pid, name, category in await (
                    await conn.execute("SELECT id, name, category FROM places")
                ).fetchall()
                if name and rules.in_words(str(name), words) and not sensitive_place(name, category)
            }
            home_asked = bool(re.search(r"\b(?:casa|home)\b", plain(words)))
            key_words = (
                {w for pid in persons for w in plain(people[pid][0]).split()}
                | {w for name in places.values() for w in plain(name).split()}
                | ({"casa", "home"} if home_asked else set())
            )
            said = set(plain(words).split())
            content = [w for w in rules.content_words(f"{cue} {words}") if w in said and w not in key_words]
            content = list(dict.fromkeys(content))
            keyed = bool(persons or places or home_asked or content)
            if not keyed and lo is None:
                _log.info("recall_answer", answer="none", found=0)
                return {"answer": "none", "say": rules.say_none(lang, None), "episodes": []}
            rows = await (
                await conn.execute(
                    "SELECT id, kind, day, started_at, ended_at, place_id, home, digest, state FROM episodes "
                    "WHERE kind <> 'day' AND (%s::date IS NULL OR day >= %s) AND (%s::date IS NULL OR day "
                    "<= %s) "
                    "ORDER BY started_at LIMIT 2000",
                    (lo, lo, hi, hi),
                )
            ).fetchall()
            links = await self._links(conn, [str(r[0]) for r in rows])
            found: list[rules.Candidate] = []
            info: dict[str, dict[str, Any]] = {}
            for eid, kind, day, started, ended, place_id, at_home, digest, state in rows:
                key = str(eid)
                if not rules.in_part(started, ended, part, tz):
                    continue
                mine = links.get(key, [])
                with_ = {t for k, t, r in mine if k == "person"}
                at = {t for k, t, r in mine if k == "place"} | ({str(place_id)} if place_id else set())
                score = 0.0
                if persons:
                    if not with_ & persons:
                        continue
                    score += 2 * len(with_ & persons)
                if places:
                    if not at & set(places):
                        continue
                    score += 2
                if home_asked:
                    if not at_home:
                        continue
                    score += 2
                title = await self._own_title(conn, key) if kind == "event" else None
                if content:  # an invite's title may pick which of Pisa's records answers; it is never said
                    seen = await self._any_title(conn, key) if kind == "event" else None
                    text = " ".join(
                        [str(digest or ""), seen or "", *(people[p][0] for p in with_ if p in people)]
                    )
                    hit = {w for w in content if rules.matches(w, plain(text).split())}
                    if not hit and not (persons or places or home_asked):
                        continue
                    score += len(hit) / len(content)
                if not keyed:
                    score = 1.0
                score += 0.01 * min(len(mine), 10)  # the pointers count (a tie-break only)
                found.append(rules.Candidate(key, day, started, score, state == "faded" and not deeper))
                info[key] = {
                    "kind": kind,
                    "day": day,
                    "started": started,
                    "ended": ended,
                    "home": at_home,
                    "with": with_,
                    "at": at,
                    "digest": digest,
                    "title": title,
                }
            kind_, picks = rules.answer(found, keyed)
            out: dict[str, Any] = {"answer": kind_, "say": None, "episodes": []}
            if kind_ == "two":
                out["say"] = rules.say_two(picks[0], picks[1], today, tz, lang)
            elif kind_ == "faded":
                out["say"] = rules.say_faded(lang)
            elif kind_ == "none":
                single = lo if lo is not None and lo == hi else None
                out["say"] = rules.say_none(
                    lang, await self._step(conn, single, lo, hi, part, today, lang, tz)
                )
            if kind_ in ("one", "list", "two"):
                out["episodes"] = [
                    self._told(p.id, info[p.id], people, places, words, today, lang, tz) for p in picks
                ]
            if kind_ in ("one", "list"):  # the testing effect: what they retrieved gains at the next night
                await conn.execute(
                    "UPDATE episodes SET retrieved_at = %s WHERE id = ANY(%s)", (now, [p.id for p in picks])
                )
        if device is not None and turn_id:
            self._recalled[device] = Recalled(turn_id, kind_, tuple(p.id for p in picks), now)
        _log.info("recall_answer", answer=kind_, found=len(found), told=len(out["episodes"]))
        return out

    async def _links(
        self, conn: AsyncConnection, ids: Sequence[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        found: dict[str, list[tuple[str, str, str]]] = {}
        for eid, kind, target, role in await (
            await conn.execute(
                "SELECT episode_id, target_kind, target_id, role FROM episode_links WHERE episode_id = "
                "ANY(%s)",
                (list(ids),),
            )
        ).fetchall():
            found.setdefault(str(eid), []).append((str(kind), str(target), str(role)))
        return found

    def _told(
        self,
        eid: str,
        e: dict[str, Any],
        people: dict[str, tuple[str, set[str], bool]],
        places: dict[str, str],
        words: str,
        today: date,
        lang: str,
        tz: ZoneInfo,
    ) -> dict[str, Any]:
        """An episode as recall returns it: only what may be said. A person's name when it is their (they
        wrote or set it, or says it now), else only that someone was there; a place's name only their home or
        one their words name now (a provider's name is outside text); an event's title only when it is their
        own."""
        names = [
            people[p][0]
            for p in sorted(e["with"])
            if p in people and (people[p][2] or rules.in_words(people[p][0], words))
        ]
        others = len([p for p in e["with"] if p in people]) - len(names)
        place = (
            ("casa" if not lang.startswith("en") else "home")
            if e["home"]
            else next((places[p] for p in sorted(e["at"]) if p in places), None)
        )
        return {
            "id": eid,
            "kind": e["kind"],
            "day": rules.day_name(e["day"], today, lang),
            "date": e["day"].isoformat(),
            "from": rules.hm(e["started"], tz),
            "to": rules.hm(e["ended"], tz),
            "place": place,
            "with": names,
            "others": others,
            "title": e["title"],
            "digest": e["digest"],
        }

    async def _step(
        self,
        conn: AsyncConnection,
        day: date | None,
        lo: date | None,
        hi: date | None,
        part: str | None,
        today: date,
        lang: str,
        tz: ZoneInfo,
    ) -> str | None:
        """ONE next step for "non ne ho traccia", from pointers: where they were that day (home, or out), then
        the mail that day holds; else their agenda."""
        if lo is None:
            return None
        rows = await (
            await conn.execute(
                "SELECT kind, started_at, ended_at, home FROM episodes WHERE day >= %s AND day <= %s "
                "ORDER BY started_at",
                (lo, hi),
            )
        ).fetchall()
        home_from = next(
            (
                a
                for k, a, b, h in rows
                if k == "stay" and h and day is not None and rules.in_part(a, b, part, tz)
            ),
            None,
        )
        out_from = next(
            (
                a
                for k, a, b, h in rows
                if k == "stay" and not h and day is not None and rules.in_part(a, b, part, tz)
            ),
            None,
        )
        events = sum(1 for k, a, b, _ in rows if k == "event" and rules.in_part(a, b, part, tz))
        mail = await (
            await conn.execute(
                "SELECT count(*) FROM episode_links l JOIN episodes e ON e.id = l.episode_id WHERE e.kind = "
                "'day' "
                "AND e.day >= %s AND e.day <= %s AND l.target_kind = 'email'",
                (lo, hi),
            )
        ).fetchone()
        return rules.next_step(
            lang=lang,
            day=day,
            today=today,
            home_from=home_from,
            place_from=out_from,
            emails=int(mail[0]) if mail else 0,
            events=events,
            tz=tz,
        )

    # --- their words about what recall answered
    # -----------------------------------------------------------------

    def recalled(self, device: uuid.UUID) -> Recalled | None:
        """What recall told on this device's turn, for 10 minutes."""
        last = self._recalled.get(device)
        if last is not None and self._clock() - last.at > rules.PATH_OPEN:
            self._recalled.pop(device, None)
            return None
        return last

    def done_with(self, device: uuid.UUID) -> None:
        self._recalled.pop(device, None)

    async def read_outside(self, conversation_id: uuid.UUID) -> bool:
        """Pisa's last reply in this talk read outside text (an email, a page): their "it" may be that
        text."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT cardinality(outside_kinds) > 0 OR quoted FROM messages WHERE conversation_id = "
                    "%s "
                    "AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                    (conversation_id,),
                )
            ).fetchone()
        return bool(row and row[0])

    async def heard(
        self, kind: str, name: str | None, episode_id: str, lang: str, conversation_id: uuid.UUID | None
    ) -> str | None:
        """Their closed phrase, done by code: keep (+1), forget (gone, a tombstone, -1), or correct who they
        were with (the name must be one of their people, exactly one; else the model's)."""
        if kind == "keep":
            done = await self.keep(episode_id, conversation_id)
            return rules.KEPT_SAID.get(lang, rules.KEPT_SAID["en-US"]) if done else None
        if kind == "forget":
            done = await self.forget(episode_id, conversation_id)
            return rules.FORGOT_SAID.get(lang, rules.FORGOT_SAID["en-US"]) if done else None
        if kind == "correct" and name:
            async with self._pool.connection() as conn:
                people = await self._people(conn)
            match = [pid for pid, (full, _, _) in people.items() if rules.person_said(full, name)]
            if len(match) != 1:
                return None  # nobody, or two Nino: the model asks which
            done = await self.correct(episode_id, match)
            return rules.corrected_said(name, lang) if done else None
        return None

    async def link_message(self, kind: str, episode_id: str, conversation_id: uuid.UUID) -> None:
        """Their +1 or -1 points at the message they said it in (provenance owner_words), once it is saved."""
        digest = self._hashes.pop(episode_id, None)
        if digest is None or kind not in ("keep", "forget"):
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE pisa_explicit SET message_id = %s WHERE key_hash = %s",
                (await self._message(conn, conversation_id), digest),
            )

    async def _message(self, conn: AsyncConnection, conversation_id: uuid.UUID | None) -> int | None:
        if conversation_id is None:
            return None
        row = await (
            await conn.execute(
                "SELECT max(id) FROM messages WHERE conversation_id = %s AND role = 'user'",
                (conversation_id,),
            )
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def keep(self, episode_id: str, conversation_id: uuid.UUID | None = None) -> bool:
        """Their "ricordatelo": explicit +1 (owner_words), confirmed now (it gains at the next night)."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE episodes SET confirmed_at = %s WHERE id = %s RETURNING key", (now, episode_id)
                )
            ).fetchone()
            if row is None:
                return False
            await conn.execute(
                "INSERT INTO pisa_explicit (key_hash, episode_id, explicit, message_id, at) VALUES (%s, %s, "
                "1, %s, "
                "%s) ON CONFLICT (key_hash) DO UPDATE SET explicit = 1, episode_id = EXCLUDED.episode_id, "
                "message_id = EXCLUDED.message_id, at = EXCLUDED.at",
                (rules.key_hash(str(row[0])), episode_id, await self._message(conn, conversation_id), now),
            )
        self._hashes[episode_id] = rules.key_hash(str(row[0]))
        _log.info("episode_kept", episode_id=episode_id)
        return True

    async def _block(
        self, conn: AsyncConnection, keys: Sequence[str], message_id: int | None, now: datetime
    ) -> None:
        """-1 rows (hashes only) that keep these keys unwritten by every later night."""
        for key in dict.fromkeys(keys):
            await conn.execute(
                "INSERT INTO pisa_explicit (key_hash, episode_id, explicit, message_id, at) "
                "VALUES (%s, NULL, -1, %s, %s) ON CONFLICT (key_hash) DO UPDATE SET explicit = -1, "
                "episode_id = NULL, message_id = EXCLUDED.message_id, at = EXCLUDED.at",
                (rules.key_hash(key), message_id, now),
            )

    async def _drop(self, conn: AsyncConnection, episode_id: str) -> list[str] | None:
        """One episode gone with its links and every `near` link to it; the keys that must stay unwritten: its
        own, its source's (any midnight), and a talk's messages. None: no such episode."""
        row = await (
            await conn.execute("DELETE FROM episodes WHERE id = %s RETURNING key, day", (episode_id,))
        ).fetchone()
        if row is None:
            return None
        await conn.execute(
            "DELETE FROM episode_links WHERE target_kind = 'episode' AND target_id = %s", (episode_id,)
        )
        await conn.execute(  # a day whose last memory they forgot keeps no frame
            "DELETE FROM episodes WHERE kind = 'day' AND day = %s AND NOT EXISTS (SELECT 1 FROM episodes "
            "WHERE kind <> 'day' AND day = %s)",
            (row[1], row[1]),
        )
        key = str(row[0])
        keys = [key, *([b] if (b := rules.base_key(key)) else [])]
        talk = self._talk_of.pop(episode_id, None)
        if talk is not None:
            conv, lo, hi = talk
            keys += [
                rules.message_key(conv, int(i))
                for (i,) in await (
                    await conn.execute(
                        "SELECT id FROM messages WHERE conversation_id = %s AND id BETWEEN %s AND %s",
                        (conv, lo, hi),
                    )
                ).fetchall()
            ] + [rules.message_key(conv, i) for i in (lo, hi)]
        return keys

    async def _range(self, conn: AsyncConnection, episode_id: str) -> tuple[str, int, int] | None:
        row = await (
            await conn.execute(
                "SELECT target_id FROM episode_links WHERE episode_id = %s AND target_kind = 'message_range'",
                (episode_id,),
            )
        ).fetchone()
        if row is None:
            return None
        conv, span = str(row[0]).split(":", 1)
        lo, hi = (int(x) for x in span.split("-"))
        return conv, lo, hi

    async def forget(self, episode_id: str, conversation_id: uuid.UUID | None = None) -> bool:
        """Their "non ricordarmelo" (or the screen's "dimentica"): the episode and its links go (and the other
        episodes' `near` links to it); a tombstone keeps that something was forgotten, never what; -1 rows
        (hashes) stop every later night writing it again, cut at any midnight."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.EPISODES,))
            if (talk := await self._range(conn, episode_id)) is not None:
                self._talk_of[episode_id] = talk
            keys = await self._drop(conn, episode_id)
            if keys is None:
                return False
            await self._block(conn, keys, await self._message(conn, conversation_id), now)
            await conn.execute("INSERT INTO episode_tombstones (forgotten_at) VALUES (%s)", (now,))
        self._hashes[episode_id] = rules.key_hash(keys[0])
        _log.info("episode_forgotten", keys=len(keys))
        return True

    async def forget_turn(self, conversation_id: uuid.UUID, asked: str, said: str, device: uuid.UUID) -> int:
        """A voice forget also forgets the turn that recalled it: their question (the latest of
        their messages in that talk with `asked` as its words) and every message after it, and their own "non
        ricordarmelo" turn (`said`, wherever it was kept), are never a talk episode; one already written over
        them goes. Returns the messages blocked."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (locks.EPISODES,))
            starts = await (
                await conn.execute(
                    "SELECT conversation_id, max(id) FROM messages WHERE role = 'user' AND ((conversation_id "
                    "= %s AND content = %s) OR (content = %s AND conversation_id IN (SELECT id FROM "
                    "conversations WHERE device_id = %s))) GROUP BY conversation_id, content = %s",
                    (conversation_id, asked, said, device, asked),
                )
            ).fetchall()
            ids: set[tuple[str, int]] = set()
            for conv, first in starts:
                rows = await (
                    await conn.execute(
                        "SELECT id FROM messages WHERE conversation_id = %s AND id >= %s", (conv, first)
                    )
                ).fetchall()
                ids |= {(str(conv), int(i)) for (i,) in rows}
            keys = [rules.message_key(c, i) for c, i in sorted(ids)]
            for conv in {c for c, _ in ids}:
                linked = await (
                    await conn.execute(
                        "SELECT episode_id FROM episode_links WHERE target_kind = 'message_range' "
                        "AND target_id LIKE %s",
                        (f"{conv}:%",),
                    )
                ).fetchall()
                for (eid,) in linked:
                    talk = await self._range(conn, str(eid))
                    if talk is not None and any(c == conv and talk[1] <= i <= talk[2] for c, i in ids):
                        self._talk_of[str(eid)] = talk
                        keys += await self._drop(conn, str(eid)) or []
            await self._block(conn, keys, None, now)
        _log.info("episode_turn_forgotten", messages=len(ids))
        return len(ids)

    async def correct(self, episode_id: str, person_ids: Sequence[str]) -> bool:
        """Their correction of who they were with: the code's links go, their stay (`source = owner`), the
        episode is `corrected_by_owner`; the digest is never rewritten by a model, and the night never touches
        it."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "UPDATE episodes SET corrected_by_owner = true, confirmed_at = %s WHERE id = %s "
                    "RETURNING id",
                    (now, episode_id),
                )
            ).fetchone()
            if row is None:
                return False
            await conn.execute(
                "DELETE FROM episode_links WHERE episode_id = %s AND target_kind = 'person' AND role = "
                "'with'",
                (episode_id,),
            )
            for pid in person_ids:
                await conn.execute(
                    "INSERT INTO episode_links (episode_id, target_kind, target_id, role, source) VALUES "
                    "(%s, 'person', %s, 'with', 'owner') ON CONFLICT DO NOTHING",
                    (episode_id, str(pid)),
                )
        _log.info("episode_corrected", episode_id=episode_id, people=len(person_ids))
        return True

    # --- the Memory screen (server side)
    # -----------------------------------------------------------------

    async def days(self, since: date | None = None, until: date | None = None) -> dict[str, Any]:
        """Their days as cards (newest first, at most 30): each episode with its place and people pills. Faded
        ones are shown too (fade is never shown as a state)."""
        tz = self._owner.zone
        today = self._clock().astimezone(tz).date()
        hi = until or today
        lo = since or hi - timedelta(days=13)
        if (hi - lo).days > 30:
            lo = hi - timedelta(days=30)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id FROM episodes WHERE kind <> 'day' AND day >= %s AND day <= %s "
                    "ORDER BY day DESC, started_at",
                    (lo, hi),
                )
            ).fetchall()
            cards: dict[date, list[dict[str, Any]]] = {}
            for (eid,) in rows:
                card = await self._card(conn, str(eid))
                if card is not None:
                    cards.setdefault(date.fromisoformat(card["day"]), []).append(card)
        return {"days": [{"day": d.isoformat(), "episodes": cards[d]} for d in sorted(cards, reverse=True)]}

    async def _card(self, conn: AsyncConnection, eid: str) -> dict[str, Any] | None:
        row = await (
            await conn.execute(
                "SELECT e.id, e.kind, e.day, e.started_at, e.ended_at, e.home, e.digest, "
                "e.corrected_by_owner, "
                "p.id, p.name FROM episodes e LEFT JOIN places p ON p.id = e.place_id WHERE e.id = %s",
                (eid,),
            )
        ).fetchone()
        if row is None:
            return None
        _, kind, day, started, ended, at_home, digest, corrected, pid, pname = row
        people = await (
            await conn.execute(
                "SELECT p.id, p.name, l.source FROM episode_links l JOIN people p ON p.id::text = "
                "l.target_id "
                "WHERE l.episode_id = %s AND l.target_kind = 'person' AND NOT p.hidden ORDER BY p.name",
                (eid,),
            )
        ).fetchall()
        return {
            "id": eid,
            "kind": kind,
            "day": day.isoformat(),
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "home": bool(at_home),
            "place": {"id": str(pid), "name": str(pname)} if pid else None,
            "people": [{"id": str(p), "name": str(n), "by_owner": s == "owner"} for p, n, s in people],
            "digest": digest,
            "corrected_by_owner": bool(corrected),
        }

    async def episode(self, eid: str) -> dict[str, Any]:
        """One card with its sources (tap for sources): each pointer by kind and id; an email's sender class,
        never a word of it; an event's title only when it is their own."""
        try:
            uuid.UUID(eid)
        except ValueError:
            raise PisaError("not_found", "No such memory.", "Pull to refresh the Memory screen") from None
        async with self._pool.connection() as conn:
            card = await self._card(conn, eid)
            if card is None:
                raise PisaError("not_found", "No such memory.", "Pull to refresh the Memory screen")
            sources = []
            for kind, target, role, source, sender in await (
                await conn.execute(
                    "SELECT target_kind, target_id, role, source, sender_class FROM episode_links WHERE "
                    "episode_id = %s AND target_kind NOT IN ('person', 'episode') ORDER BY target_kind, "
                    "target_id",
                    (eid,),
                )
            ).fetchall():
                item: dict[str, Any] = {"kind": kind, "id": target, "role": role, "source": source}
                if kind == "email":
                    item["sender_class"] = sender
                if kind == "event":
                    item["title"] = await self._own_title(conn, eid)
                sources.append(item)
        return card | {"sources": sources}

    async def correct_people(self, eid: str, person_ids: Sequence[str]) -> dict[str, Any]:
        """The screen's "correggi": who they were with (their people's ids; an unknown id is a 400)."""
        async with self._pool.connection() as conn:
            known = {
                str(p)
                for (p,) in await (
                    await conn.execute(
                        "SELECT id::text FROM people WHERE id::text = ANY(%s) AND NOT hidden",
                        (list(person_ids),),
                    )
                ).fetchall()
            }
        if set(person_ids) - known:
            raise PisaError("validation_failed", "Unknown person.", "Pick someone from your people")
        await self.episode(eid)
        await self.correct(eid, sorted(known))
        return await self.episode(eid)

    async def forget_card(self, eid: str) -> dict[str, Any]:
        """The screen's "dimentica"."""
        await self.episode(eid)
        await self.forget(eid)
        return {"forgotten": True}

    async def stanotte(self) -> dict[str, Any]:
        """The "Stanotte" strip: what the last night wrote (episodes made, digests written), as cards."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT max(n) FROM (SELECT max(created_night) AS n FROM episodes UNION ALL "
                    "SELECT max(digested_night) FROM episodes) x"
                )
            ).fetchone()
            night = row[0] if row and row[0] else None
            if night is None:
                return {"night": None, "made": 0, "digested": 0, "episodes": []}
            counts = await (
                await conn.execute(
                    "SELECT count(*) FILTER (WHERE created_night = %s), count(*) FILTER (WHERE "
                    "digested_night = %s)"
                    " FROM episodes WHERE kind <> 'day'",
                    (night, night),
                )
            ).fetchone()
            ids = await (
                await conn.execute(
                    "SELECT id FROM episodes WHERE kind <> 'day' AND (created_night = %s OR digested_night "
                    "= %s) "
                    "ORDER BY started_at DESC LIMIT 12",
                    (night, night),
                )
            ).fetchall()
            cards = [c for (i,) in ids if (c := await self._card(conn, str(i))) is not None]
        made, digested = counts if counts else (0, 0)
        return {"night": night.isoformat(), "made": int(made), "digested": int(digested), "episodes": cards}


def _iso(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None
