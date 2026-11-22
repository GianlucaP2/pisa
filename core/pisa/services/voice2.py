# ruff: noqa: E501
"""`pisa.domain.voice2`: perceive then ask, Pisa's own states, the character file, Pisa's opinions.

Two ways into a turn, both by code (no tool, no model round, nothing in the conversation's prompt):
- `after_turn`: at the end of a model turn, at most **one** line of Pisa's own, appended to the reply: a cue-question
  when the guard (`rules.decide`) finds Pisa's own reason, else Pisa's view once on a decision or a habit of theirs. Never
  after outside text in the turn, never while others may be present, never inside a floor message, never on a
  reply that already asks;
- `heard`: a reflex before the yes chain: their answer to Pisa's cue-question (wave it away, name it, keep their word on
  "sì, ricordalo"), "e tu, come stai?" (Pisa's state, from its records only), their yes or no to a trait Pisa proposed,
  "com'è il tuo carattere?".

RAIL: the cues come only from their voice (the phone's observables), their own words this turn and their
own calendar (never a stranger's event). Mail, invites, pages, search results and tool output never reach
`rules.decide`. Logs carry ids and counts only.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import voice2 as rules
from pisa.domain.actions import is_short_no
from pisa.domain.owner import Owner
from pisa.domain.tasks import is_short_yes
from pisa.logging import get_logger
from pisa.services.foreign import EVENTS, FOREIGN_EVENT
from pisa.services.notifications import in_meeting

_log = get_logger("pisa.voice2")


async def _lock(conn: AsyncConnection) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('pisa.voice2'))")


WEEK = timedelta(days=7)


@dataclass
class Pending:
    """Pisa's open question to one device, bound to the turn that asked it (answered only in the next turn)."""

    kind: str  # cue | remember | trait
    turn: Any
    at: datetime
    ask_id: uuid.UUID | None = None
    tempo: rules.Tempo = "up"
    word: str = ""
    trait: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Voice2Service:
    def __init__(self, pool: AsyncConnectionPool, *, owner: Owner, clock: Any) -> None:
        self._pool = pool
        self._owner = owner
        self._clock = clock
        self._pending: dict[uuid.UUID, Pending] = {}
        # their approved traits, held in memory (read at start, again on their yes or
        # no): the turn's prompt never waits on the DB for them
        self._traits: list[str] | None = None

    # --- the day's records ------------------------------------------------------------------------------

    def _day(self, now: datetime) -> tuple[datetime, datetime]:
        local = now.astimezone(self._owner.zone)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    async def _own_events_today(self, conn: AsyncConnection, now: datetime) -> int:
        """Their own timed events today (an invite, a Gmail-made or a shared calendar's event is a
        stranger's, never a cue)."""
        start, end = self._day(now)
        row = await (
            await conn.execute(
                f"SELECT count(*) FROM {EVENTS} WHERE e.status <> 'cancelled' AND NOT e.all_day "  # noqa: S608
                f"AND e.starts_at >= %s AND e.starts_at < %s AND NOT coalesce({FOREIGN_EVENT}, false)",
                (start, end),
            )
        ).fetchone()
        return int(row[0]) if row else 0

    async def _asked_today(self, conn: AsyncConnection, now: datetime) -> tuple[str, ...]:
        start, _ = self._day(now)
        rows = await (
            await conn.execute(
                "SELECT tempo FROM pisa_cue_asks WHERE asked_at >= %s ORDER BY asked_at", (start,)
            )
        ).fetchall()
        return tuple(r[0] for r in rows)

    async def _quiet(self, conn: AsyncConnection) -> frozenset[str]:
        rows = await (await conn.execute("SELECT tempo FROM pisa_cue_quiet")).fetchall()
        return frozenset(r[0] for r in rows)

    async def _his(self, conn: AsyncConnection, tempo: str) -> str | None:
        row = await (
            await conn.execute(
                "SELECT word FROM pisa_his_states WHERE tempo = %s ORDER BY kept_at DESC LIMIT 1", (tempo,)
            )
        ).fetchone()
        return row[0] if row else None

    async def _go_quiet(self, conn: AsyncConnection, ask_id: uuid.UUID, tempo: str, why: str) -> None:
        now = self._clock()
        cur = await conn.execute(
            "UPDATE pisa_cue_asks SET status = %s, answered_at = %s WHERE id = %s AND status = 'open'",
            ("waved" if why == "waved" else "unanswered", now, ask_id),
        )
        if not cur.rowcount:
            return  # already answered (the sweep and an answer never both decide one question)
        await conn.execute(
            "INSERT INTO pisa_cue_quiet (tempo, since, why) VALUES (%s, %s, %s) "
            "ON CONFLICT (tempo) DO UPDATE SET since = excluded.since, why = excluded.why",
            (tempo, now, why),
        )

    async def _sweep(self, conn: AsyncConnection) -> None:
        """A cue-question still open that no pending answer holds (a later turn, a restart) was unanswered."""
        held = {p.ask_id for p in self._pending.values() if p.ask_id is not None}
        rows = await (
            await conn.execute("SELECT id, tempo FROM pisa_cue_asks WHERE status = 'open'")
        ).fetchall()
        for ask_id, tempo in rows:
            if ask_id not in held:
                await self._go_quiet(conn, ask_id, tempo, "unanswered")

    async def _raised(self, conn: AsyncConnection, text: str) -> set[rules.Tempo]:
        """Their own words name a kind: it is their again (no longer quiet)."""
        kinds = rules.raised(text)
        if kinds:
            await conn.execute("DELETE FROM pisa_cue_quiet WHERE tempo = ANY(%s)", (list(kinds),))
        return kinds

    # --- after a model turn -----------------------------------------------------------------------------

    async def after_turn(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        reply: str,
        *,
        turn: Any,
        heard: Any = None,
        outside: bool = False,
        open_question: bool = False,
        floor: bool = False,
        moves: list[Any] | None = None,
    ) -> str | None:
        """At most one line of Pisa's own for the end of this reply, or None."""
        now = self._clock()
        voice = rules.Heard.of(heard)
        self._pending.pop(device_id, None)  # their turn went on: whatever Pisa asked before is past
        async with self._pool.connection() as conn, conn.transaction():
            await _lock(conn)  # one decision at a time (the daily ceiling, the sweep, an answer)
            await self._sweep(conn)  # the question they just went past (popped above) was unanswered
            raised = await self._raised(conn, text)
            others = rules.others_maybe(voice) or await in_meeting(conn, now)
            asks = open_question or "?" in reply
            if outside or others or floor or asks:
                _log.info("voice2_none", outside=outside, others=others, floor=floor, asks=asks)
                return None
            if not raised:
                line = await self._cue(conn, device_id, text, lang, now, voice, turn)
                if line is not None:
                    return line
            return await self._opinion(conn, lang, now, moves or [])

    async def _cue(
        self,
        conn: AsyncConnection,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        now: datetime,
        voice: rules.Heard | None,
        turn: Any,
    ) -> str | None:
        events = await self._own_events_today(conn, now)
        cues: dict[rules.Channel, rules.Tempo] = {}
        if (v := rules.voice_tempo(voice)) is not None:
            cues["voice"] = v
        if (w := rules.words_tempo(text)) is not None:
            cues["words"] = w
        if (d := rules.day_tempo(events)) is not None:
            cues["day"] = d
        ask = rules.decide(
            rules.Context(
                cues=cues,
                asked_for=rules.asks_for_fit(text),
                quiet=await self._quiet(conn),
                asked_today=await self._asked_today(conn, now),
            )
        )
        if ask is None:
            return None
        his = await self._his(conn, ask.tempo)
        line = rules.question(ask, lang, his=his, events=events)
        if not rules.holds(line, his):  # RAIL: never a label, an observable only with a question
            _log.warning("voice2_question_refused", tempo=ask.tempo)
            return None
        ask_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO pisa_cue_asks (id, device_id, tempo, channels, reason, asked_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (ask_id, device_id, ask.tempo, list(ask.channels), ask.reason, now),
        )
        self._pending[device_id] = Pending("cue", turn, now, ask_id=ask_id, tempo=ask.tempo)
        _log.info(
            "voice2_asked", ask_id=str(ask_id), tempo=ask.tempo, reason=ask.reason, channels=len(ask.channels)
        )
        return line

    async def _said_once(self, conn: AsyncConnection, key: str, kind: str, now: datetime) -> bool:
        """Each opinion once, then let go: True when this key was never said (and is now)."""
        cur = await conn.execute(
            "INSERT INTO pisa_opinions (key, kind, said_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (key, kind, now),
        )
        return cur.rowcount == 1

    async def _people(self, conn: AsyncConnection) -> list[str]:
        rows = await (await conn.execute("SELECT name FROM people")).fetchall()
        return [r[0] for r in rows]

    async def _opinion(self, conn: AsyncConnection, lang: str, now: datetime, moves: list[Any]) -> str | None:
        people = await self._people(conn)
        for move in moves:  # disagree once: the same item of theirs moved again and again
            if getattr(move, "move", None) != "act" or getattr(move, "tool", None) != "move_event":
                continue
            if getattr(move, "outcome", None) not in (None, "done") or not move.ref:
                continue
            if getattr(move, "provenance", None) == "outside_text":
                continue  # a move a mail asked for is never Pisa's to argue
            row = await (
                await conn.execute("SELECT payload FROM approvals WHERE id::text = %s", (str(move.ref),))
            ).fetchone()
            if row is None:
                continue
            payload = row[0] or {}
            if payload.get("notified") or payload.get("after_outside"):
                continue  # an event with other people is about them, never Pisa's opinion
            event_id, title = payload.get("event_id"), str(payload.get("title") or "")
            if rules.private_title(title, people):
                continue  # a medical word or a person in the title: never Pisa's opinion
            foreign = await (
                await conn.execute(
                    f"SELECT 1 FROM {EVENTS} WHERE e.id = %s AND {FOREIGN_EVENT}",  # noqa: S608
                    (event_id,),
                )
            ).fetchone()
            if foreign is not None:
                continue
            count = await (
                await conn.execute(
                    "SELECT count(*) FROM approvals WHERE kind = 'calendar_move' AND status = 'done' "
                    "AND payload->>'event_id' = %s AND created_at >= %s",
                    (event_id, now - WEEK),
                )
            ).fetchone()
            n = int(count[0]) if count else 0
            if n < rules.MOVES_FOR_DISAGREE:
                continue
            line = rules.opinion("disagree", lang, n=n)  # the title is never quoted
            if rules.opinion_rails(line, people):
                continue
            if await self._said_once(conn, f"disagree:event:{event_id}", "disagree", now):
                _log.info("voice2_opinion", kind="disagree", n=n)
                return line
        local = now.astimezone(self._owner.zone)
        if rules.LATE_FROM_HOUR <= local.hour < rules.LATE_UNTIL_HOUR:  # a habit, said while it happens
            nights = await self._late_nights(conn, now)
            if nights >= rules.LATE_NIGHTS:
                line = rules.opinion("habit", lang, n=nights)
                if not rules.opinion_rails(line, people) and await self._said_once(
                    conn, "habit:late", "habit", now
                ):
                    _log.info("voice2_opinion", kind="habit", n=nights)
                    return line
        return None

    async def _late_nights(self, conn: AsyncConnection, now: datetime) -> int:
        """The nights of the last 7 (their zone) with a turn of their between 01:00 and 05:00."""
        rows = await (
            await conn.execute(
                "SELECT created_at FROM messages WHERE role = 'user' AND created_at >= %s AND created_at <= %s",
                (now - WEEK, now),
            )
        ).fetchall()
        nights = set()
        for (at,) in rows:
            local = at.astimezone(self._owner.zone)
            if rules.LATE_FROM_HOUR <= local.hour < rules.LATE_UNTIL_HOUR:
                nights.add(local.date())
        local = now.astimezone(self._owner.zone)
        nights.add(local.date())  # tonight's turn is one
        return len(nights)

    # --- their words: the reflex --------------------------------------------------------------------------

    def bind(self, device_id: uuid.UUID, turn: Any) -> None:
        """The reflex's own turn asked Pisa's question: its answer comes in the next turn."""
        if (p := self._pending.get(device_id)) is not None and p.turn is None:
            p.turn = turn

    async def heard(
        self,
        device_id: uuid.UUID,
        text: str,
        lang: str,
        before: Any,
        *,
        newer: bool = False,
        asked_else: bool = False,
        yes_by_code: bool = True,
    ) -> str | None:
        """Their words to Pisa's open question, "come stai?", or "com'è il tuo carattere?". None: not these.

        `newer`: Pisa said something unasked since `before` (a check-in, a reminder, an approval): newest wins, so
        their "sì" is not this question's. `asked_else`: Pisa's last reply asked them something else ("Cosa gli
        scrivo?"): "e tu, come stai?" is then their answer to it. `yes_by_code`: in a talk, a bare
        yes or no decides only right after Pisa said its question aloud."""
        now = self._clock()
        pending = self._pending.get(device_id)
        if pending is not None and (
            before is None
            or before is not pending.turn
            or newer
            or (now - pending.at).total_seconds() > rules.REMEMBER_WITHIN_S  # it expires
        ):
            if pending.kind != "cue":
                self._pending.pop(device_id, None)  # a word never kept late; the sweep closes a cue
            pending = None
        if pending is not None and pending.kind == "cue":
            said = await self._cue_answer(device_id, pending, text, lang, now, yes_by_code)
            if said is not None:
                return said
        elif pending is not None and pending.kind == "remember" and yes_by_code:
            kept = rules.remember_answer(text)
            if kept is not None:
                self._pending.pop(device_id, None)
                if kept and await self._keep(pending.tempo, pending.word, now):
                    return rules.said("kept", lang, pending.word)
                return rules.said("not_kept", lang)
        elif pending is not None and pending.kind == "trait" and yes_by_code:
            yes, no = is_short_yes(text), is_short_no(text)
            if yes or no:
                self._pending.pop(device_id, None)
                async with self._pool.connection() as conn:
                    await conn.execute(
                        "UPDATE pisa_character SET status = %s, decided_at = %s WHERE trait = %s AND status = 'proposed'",
                        ("approved" if yes else "declined", now, pending.trait),
                    )
                await self.load_traits()  # the turn's cache follows their yes or no
                _log.info("voice2_trait", approved=yes)
                return rules.TRAIT_DECIDED[yes][rules.lang_key(lang)]
        if asked_else:
            return None
        if rules.is_how_are_you(text):
            return await self.state(device_id, lang)
        if rules.is_character(text):
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT trait FROM pisa_character WHERE status = 'approved' ORDER BY decided_at"
                    )
                ).fetchall()
            return rules.traits_said([r[0] for r in rows], lang)
        return None

    async def approved(self) -> list[str]:
        """The traits they said yes to (never a proposed or a declined one), for the turn's prompt.
        from memory (`load_traits`, at start and on their yes or no); the DB only before the
        first read."""
        if self._traits is None:
            await self.load_traits()
        return list(self._traits or [])

    async def load_traits(self) -> list[str]:
        """The approved traits, read again (at start, and when they approve or declines one)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT trait FROM pisa_character WHERE status = 'approved' ORDER BY trait"
                )
            ).fetchall()
        self._traits = [str(r[0]) for r in rows]
        return list(self._traits)

    async def _cue_answer(
        self, device_id: uuid.UUID, pending: Pending, text: str, lang: str, now: datetime, yes_by_code: bool
    ) -> str | None:
        kind = rules.answer(text, pending.tempo)
        if kind is None or (kind != "named" and not yes_by_code):
            return None  # unanswered: the next turn (or the sweep) makes the kind quiet
        assert pending.ask_id is not None
        word = rules.his_word(text) if kind == "named" else None
        if word is not None and not await self._keepable(word):
            word, kind = None, "yes"  # a name of their people is never kept (nor a health word: STATES)
        async with self._pool.connection() as conn, conn.transaction():
            await _lock(conn)
            if kind == "wave":
                await self._go_quiet(conn, pending.ask_id, pending.tempo, "waved")
                status = "waved"
            else:
                status = "named" if word else "answered"
                cur = await conn.execute(
                    "UPDATE pisa_cue_asks SET status = %s, answered_at = %s WHERE id = %s AND status = 'open'",
                    (status, now, pending.ask_id),
                )
                if not cur.rowcount:
                    return None  # the sweep closed it first
        self._pending.pop(device_id, None)
        _log.info("voice2_answer", ask_id=str(pending.ask_id), answer=status)
        if kind == "wave":
            return rules.said("wave", lang)
        if word is None:
            return rules.said("yes", lang)
        if rules.says_remember(text) and await self._keep(pending.tempo, word, now):  # "sì, cotto, ricordalo"
            return rules.said("kept", lang, word)
        # Their word waits in memory only (never stored before their "sì, ricordalo"), for REMEMBER_WITHIN_S
        self._pending[device_id] = Pending("remember", None, now, tempo=pending.tempo, word=word)
        return rules.said("named", lang, word)

    async def _keepable(self, word: str) -> bool:
        """HIGH 1-2: never a health word, never a name of their people (the other-people rail)."""
        async with self._pool.connection() as conn:
            return rules.keepable(word, await self._people(conn))

    async def _keep(self, tempo: str, word: str, now: datetime) -> bool:
        if not await self._keepable(word):
            return False
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pisa_his_states (id, tempo, word, kept_at) VALUES (%s, %s, %s, %s)",
                (uuid.uuid4(), tempo, word[: rules.WORD_MAX], now),
            )
        _log.info("voice2_word_kept", tempo=tempo)
        return True

    # --- Pisa's own states ---------------------------------------------------------------------------------

    async def records(self, conn: AsyncConnection, now: datetime) -> rules.StateRecords:
        """Pisa's own records, the only source of "come stai?"."""
        fixed = await (
            await conn.execute(
                "SELECT heard, meant FROM understand_corrections WHERE created_at >= %s "
                "ORDER BY created_at DESC LIMIT 1",
                (now - WEEK,),
            )
        ).fetchone()
        learned = await (
            await conn.execute(
                "SELECT count(*) FROM profile_claims WHERE state = 'active' AND provenance IN ('said', 'confirmed') "
                "AND created_at >= %s",
                (now - WEEK,),
            )
        ).fetchone()
        wondering = await (
            await conn.execute("SELECT count(*) FROM profile_claims WHERE state IN ('candidate', 'asking')")
        ).fetchone()
        return rules.StateRecords(
            fixed=(fixed[0], fixed[1]) if fixed else None,
            learned=int(learned[0]) if learned else 0,
            wondering=int(wondering[0]) if wondering else 0,
        )

    async def state(self, device_id: uuid.UUID, lang: str) -> str:
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            said = rules.state_answer(await self.records(conn, now), lang)
            rows = await (await conn.execute("SELECT trait FROM pisa_character")).fetchall()
            trait = rules.next_trait({r[0] for r in rows})
            if trait is not None:  # Pisa proposes one trait of its own, once; they approve it
                await conn.execute(
                    "INSERT INTO pisa_character (trait, proposed_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (trait, now),
                )
                said = f"{said} {rules.trait_offer(trait, lang)}"
                self._pending[device_id] = Pending("trait", None, now, trait=trait)
        if rules.needy(said):  # RAIL: never a feeling beyond its records (never reached: templates)
            said = rules.state_answer(rules.StateRecords(), lang)
        _log.info("voice2_state", trait=trait is not None)
        return said

    # --- the Sunday card --------------------------------------------------------------------------------

    async def week(self, start: datetime, end: datetime) -> dict[str, int]:
        """The week's cue-questions, counted ("the Sunday card counts them")."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT status, count(*) FROM pisa_cue_asks WHERE asked_at >= %s AND asked_at < %s GROUP BY status",
                    (start, end),
                )
            ).fetchall()
        counts = {r[0]: int(r[1]) for r in rows}
        return {
            "asked": sum(counts.values()),
            "answered": counts.get("answered", 0) + counts.get("named", 0),
            "waved": counts.get("waved", 0),
            "unanswered": counts.get("unanswered", 0) + counts.get("open", 0),
        }
