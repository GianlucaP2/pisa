"""Never drop an intention (`pisa/domain/intentions.py` has the rules).

What existed and is reused, never a second reminder system:
- a **time** cue is a pool task with its due date-time: the minute's reminders fire it, a repeat is
  the task's own; an event cue or none: an undated pool task (every task in one list);
- a **place** cue is a place note: the phone monitors its region and `place_enter` fires it, once a
  visit;
- an **event** cue (a meeting ending, arriving home, a call they placed ending) is read here, by attend's
  sense, from what CORE already keeps (the calendar copy, the stays and their home, the call handoffs), and
  goes out as an attend candidate: the guardian's gate covers it;
- a **waiting-for** item's "still waiting?" is an attend candidate too, once, at its due date.

Promises: their own sent mail (the money reader's promise keeper) and a message they dictate become *proposed*
intentions they confirm; another person's promise in a mail they received is a waiting-for item, never an
action. Nothing is deleted, only archived (the table's trigger); "fatto" closes it. Logs: ids and counts
only.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from pisa.domain import geo_rules as geo
from pisa.domain import intentions as rules
from pisa.domain.attend import Candidate
from pisa.domain.errors import PisaError
from pisa.domain.money_back import Promise
from pisa.domain.money_back import promises as find_promises
from pisa.domain.owner import Owner
from pisa.domain.places import distance_m
from pisa.domain.reasons import intention_why
from pisa.domain.tasks import check_repeat, match_score, same_item, title_key
from pisa.logging import get_logger
from pisa.services import home as home_store
from pisa.services.attend import propose
from pisa.services.place_notes import PlaceNotesService
from pisa.services.tasks import TasksService, parse_due

_log = get_logger("pisa.intentions")
RECENT_TASK = timedelta(minutes=5)  # add_task's task for the same words, a moment ago
FRESH = timedelta(hours=2)  # an event older than this when first seen is no cue any more (a late worker)
_PERIOD = {"day": 1, "week": 7, "month": 30}
_COLUMNS = (
    "id, kind, state, what, why, next_step, last_step, who, cue, cue_event, cue_ref, repeat, due_on, "
    "task_id, place_note_id, doc_id, source, source_ref, asked, asked_at, fired_at, nudged_at, created_at, "
    "updated_at, closed_at, archived_at"
)


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


class IntentionsService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        tasks: TasksService,
        notes: PlaceNotesService,
        *,
        owner: Owner,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        morning_hold: Callable[[datetime], Awaitable[bool]] | None = None,
    ) -> None:
        self._pool = pool
        self._tasks = tasks
        self._notes = notes
        self._owner = owner
        self._clock = clock
        # true while the morning knock will carry their loops (`KnockService.carries`)
        self.morning_hold = morning_hold

    # --- keeping one ---------------------------------------------------------------------------------------

    async def keep(
        self,
        *,
        kind: rules.Kind = "mine",
        what: str | None,
        next_step: str | None = None,
        why: str | None = None,
        who: str | None = None,
        cue: rules.Cue = "none",
        due_at: str | None = None,
        due_kind: str | None = None,
        place: tuple[float, float, str, str | None] | None = None,
        event: rules.Event | None = None,
        cue_ref: str | None = None,
        repeat: dict[str, Any] | None = None,
        due_on: date | None = None,
        doc_kind: str | None = None,
        lang: str | None = None,
        device_id: uuid.UUID | None = None,
        ask: bool = True,
    ) -> dict[str, Any]:
        """An intention in their words, kept at once with what it has. When the *what* is vague or (their own)
        the *next step* is missing, the answer carries the one question to ask (asked once, never again).
        `ask` False (the turn already asks something): nothing asked, nothing recorded."""
        lang = lang or self._owner.tag
        what = rules.clean(what, rules.WHAT_MAX)
        what = what if what and len(what) >= rules.WHAT_MIN else None
        next_step, why = rules.clean(next_step, rules.STEP_MAX), rules.clean(why, rules.WHY_MAX)
        who = rules.clean(who, rules.WHO_MAX)
        repeat = check_repeat(repeat)
        if kind == "waiting_for":
            cue, event, cue_ref, repeat = "none", None, None, None
        if cue == "event" and event not in rules.EVENTS:
            raise PisaError("validation_failed", "An event cue is meeting_end, arrive_home or call_end.", "")
        if cue == "time" and not due_at:
            raise PisaError("validation_failed", "A time cue needs its day (and time).", "Say when")
        if cue == "place" and place is None:
            raise PisaError("validation_failed", "A place cue needs the place.", "Say where")
        if what and (same := await self._kept_already(kind, what, cue, due_at, due_kind)) is not None:
            # the same intention, open, with the same cue: never kept twice; the store says it is
            raise PisaError(
                "validation_failed", f"Already kept, open (id {same}): «{what}». Say it is already kept.", ""
            )
        piece = rules.missing(what, next_step, kind, timed=cue == "time") if ask else None
        now = self._clock()
        intention_id = uuid.uuid4()
        task_id = note_id = None
        recent = await self._recent_task(_title(what, lang)) if kind == "mine" and cue != "place" else None
        if recent is not None:
            # the same words made a pool task a moment ago (add_task in the same turn): that
            # task carries it, never a second one; a time cue gives it its due and repeat
            changes: dict[str, Any] = {"notes": _notes(why, next_step, lang)}
            if cue == "time":
                changes |= {"due_at": due_at, "due_kind": due_kind, "repeat": repeat}
            await self._tasks.update(recent, changes)
            task_id = uuid.UUID(recent)
        elif kind == "mine" and cue != "place":
            # the pool's task (every task in one list): a time cue's reminders fire it and repeat it;
            # an event's or no cue's waits in the pool, undated
            task = await self._tasks.create(
                title=_title(what, lang), source_kind="voice", notes=_notes(why, next_step, lang),
                due_at=due_at if cue == "time" else None,
                due_kind=cast(Any, due_kind) if cue == "time" else None,
                repeat=repeat if cue == "time" else None,
            )  # fmt: skip
            task_id = uuid.UUID(task.id)
        elif cue == "place":  # the place note: the phone's region monitoring fires it
            lat, lon, name, place_id = place  # type: ignore[misc]
            note = await self._notes.create(
                _title(what, lang), lat, lon, place_name=name, place_id=place_id, device_id=device_id
            )
            note_id = uuid.UUID(note["id"])
        async with self._pool.connection() as conn, conn.transaction():
            doc_id = await _document(conn, doc_kind)
            await conn.execute(
                "INSERT INTO intentions (id, kind, state, what, why, next_step, who, cue, cue_event, "
                "cue_ref, repeat, due_on, task_id, place_note_id, doc_id, source, asked, asked_at, "
                "created_at, updated_at) VALUES (%s, %s, 'open', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, 'voice', %s, %s, %s, %s)",
                (intention_id, kind, what, why, next_step, who, cue, event if cue == "event" else None,
                 rules.clean(cue_ref, 200) if cue == "event" else None,
                 Jsonb(repeat) if repeat and cue in ("time", "event") else None, due_on, task_id, note_id,
                 doc_id, piece, now if piece else None, now, now),
            )  # fmt: skip
        _log.info("intention_kept", intention_id=str(intention_id), kind=kind, cue=cue, asked=piece or "")
        return {
            "id": str(intention_id),
            "question": rules.question(piece, lang) if piece else None,
            "task_id": str(task_id) if task_id else None,
            "place_note_id": str(note_id) if note_id else None,
            "repeat": repeat if cue in ("time", "event") else None,
        }

    async def answer(
        self,
        intention_id: str,
        *,
        what: str | None = None,
        next_step: str | None = None,
        why: str | None = None,
        last_step: str | None = None,
        lang: str | None = None,
    ) -> dict[str, Any]:
        """Their answer to the one question, or a step done ("ho chiamato; ora gli mando i documenti"): the
        pieces they gave, nothing asked again."""
        lang = lang or self._owner.tag
        row = await self._row(intention_id)
        changes: dict[str, Any] = {}
        if (w := rules.clean(what, rules.WHAT_MAX)) and len(w) >= rules.WHAT_MIN:
            changes["what"] = w
        if n := rules.clean(next_step, rules.STEP_MAX):
            changes["next_step"] = n
        if y := rules.clean(why, rules.WHY_MAX):
            changes["why"] = y
        if s := rules.clean(last_step, rules.STEP_MAX):
            changes["last_step"] = s
            if "next_step" not in changes and row["next_step"] == s:
                changes["next_step"] = None  # the step they named is done; the next one is theirs to say
        if not changes:
            return await self.packet(intention_id, lang)
        async with self._pool.connection() as conn, conn.transaction():
            await _update(conn, row["id"], changes, self._clock())
        task = await self._current_task(row["id"])
        if task is not None and ("what" in changes or "next_step" in changes or "why" in changes):
            merged = {**row, **changes}
            update: dict[str, Any] = {"notes": _notes(merged["why"], merged["next_step"], lang)}
            if "what" in changes:
                update["title"] = _title(changes["what"], lang)
            await self._tasks.update(task, update)
        _log.info("intention_answered", intention_id=str(row["id"]), fields=sorted(changes))
        return await self.packet(intention_id, lang)

    # --- promises, proposed ---------------------------------------------------------------------------------

    async def from_sent_mail(
        self,
        conn: AsyncConnection,
        *,
        email_id: str,
        found: list[tuple[uuid.UUID, Promise]],
        to: str,
        lang: str,
    ) -> int:
        """Their own sent mail's promises ("Ti mando il preventivo entro venerdì"): proposed, for their yes;
        each `owner_promises` row (its id first) points at its intention."""
        made = 0
        for promise_id, p in found:
            made_id = await _insert_proposed(
                conn, source="sent_mail", source_ref=email_id, what=rules.promise_what(p.what, to, lang),
                who=to, due_on=p.due_on, now=self._clock(),
            )  # fmt: skip
            if made_id is not None:
                await conn.execute(
                    "UPDATE owner_promises SET intention_id = %s WHERE id = %s", (made_id, promise_id)
                )
                made += 1
        if made:
            _log.info("intentions_proposed", source="sent_mail", count=made)
        return made

    async def from_dictation(self, *, to: str, body: str, lang: str) -> int:
        """A message they dictated that promises something ("gli mando le foto domani"): proposed."""
        found = find_promises(body, self._owner.day(self._clock()))
        if not found:
            return 0
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            for p in found:
                what = rules.promise_what(p.what, to, lang)
                same = await (
                    await conn.execute(
                        "SELECT 1 FROM intentions WHERE source = 'dictation' AND what = %s AND state IN "
                        "('proposed', 'open') AND created_at > %s",
                        (what, self._clock() - timedelta(days=1)),
                    )
                ).fetchone()
                if same is None:
                    made += (
                        await _insert_proposed(
                            conn,
                            source="dictation",
                            source_ref=None,
                            what=what,
                            who=rules.clean(to, rules.WHO_MAX),
                            due_on=p.due_on,
                            now=self._clock(),
                        )
                        is not None
                    )
        if made:
            _log.info("intentions_proposed", source="dictation", count=made)
        return made

    async def from_received_mail(
        self, conn: AsyncConnection, *, email_id: str, address: str, found: list[Promise]
    ) -> int:
        """Another person's promise to them in a mail they received ("ti mando il contratto"): a waiting-for
        item for them, never an action of Pisa's or a task. Its words are the mail's (outside text). Who:
        one of their people with that address, else the address's name part made speakable (never
        the mail's display name, which anyone writes)."""
        person = await (
            await conn.execute(
                "SELECT name FROM people WHERE %s = ANY(emails) ORDER BY name LIMIT 1", (address.lower(),)
            )
        ).fetchone()
        sender = person[0] if person else rules.speakable(address.split("@", 1)[0])
        made = 0
        for p in found:
            thing = rules.outside_thing(p.what)  # never a stranger's clause
            if not thing:
                continue
            row = await (
                await conn.execute(
                    "INSERT INTO intentions (id, kind, state, what, who, due_on, source, source_ref, "
                    "created_at, updated_at) VALUES (%s, 'waiting_for', 'open', %s, %s, %s, 'received_mail', "
                    "%s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING 1",
                    (
                        uuid.uuid4(),
                        rules.clean(thing, rules.WHAT_MAX),
                        rules.clean(sender, rules.WHO_MAX) if sender else None,
                        p.due_on,
                        email_id,
                        self._clock(),
                        self._clock(),
                    ),
                )
            ).fetchone()
            made += row is not None
        if made:
            _log.info("waiting_for_found", count=made)
        return made

    async def confirm(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """Their yes to a proposed promise: open, with a pool task due on its day (the reminders' path)."""
        lang = lang or self._owner.tag
        row = await self._row(intention_id)
        if row["state"] != "proposed":
            raise PisaError(
                "validation_failed", "Only a proposed intention is confirmed.", "Refresh the list"
            )
        today = self._owner.day(self._clock())
        task = await self._tasks.create(
            title=_title(row["what"], lang), source_kind="voice",
            notes=_notes(row["why"], row["next_step"], lang),
            due_at=row["due_on"].isoformat() if row["due_on"] and row["due_on"] >= today else None,
            due_kind="date" if row["due_on"] and row["due_on"] >= today else None,
        )  # fmt: skip
        async with self._pool.connection() as conn, conn.transaction():
            await _update(
                conn, row["id"], {"state": "open", "task_id": uuid.UUID(task.id), "cue": "time" if task.due_at
                                  else "none"}, self._clock()
            )  # fmt: skip
            await conn.execute(  # their promise's task, on the money screen's list too
                "UPDATE owner_promises SET task_id = %s WHERE intention_id = %s",
                (uuid.UUID(task.id), row["id"]),
            )
        _log.info("intention_confirmed", intention_id=str(row["id"]))
        return await self.packet(intention_id, lang)

    # --- closing: done or archived, never deleted -----------------------------------------------------------

    async def done(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """ "Fatto": its task done (a repeating one goes on: the intention stays open with its next one);
        otherwise closed. A waiting-for item: it arrived."""
        lang = lang or self._owner.tag
        row = await self._row(intention_id)
        if row["state"] in ("done", "archived"):
            return await self.packet(intention_id, lang)
        task = await self._current_task(row["id"])
        repeats = (
            bool(row["repeat"]) and row["cue"] == "event"
        )  # a repeating event cue goes on, its task open
        if task is not None and not repeats:
            done = await self._tasks.update(task, {"status": "done"})
            repeats = bool(done.repeat)
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            if repeats:
                today = self._owner.day(self._clock())
                step = f"{'fatto il' if lang.startswith('it') else 'done on'} {today.isoformat()}"
                await _update(conn, row["id"], {"last_step": step}, now)
            else:
                await _update(conn, row["id"], {"state": "done", "closed_at": now}, now)
        _log.info("intention_done", intention_id=str(row["id"]), repeats=repeats)
        return await self.packet(intention_id, lang)

    async def archive(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """Put away, never deleted: its task dismissed (its series stopped), its place note silent."""
        lang = lang or self._owner.tag
        row = await self._row(intention_id)
        if row["state"] == "archived":
            return await self.packet(intention_id, lang)
        task = await self._current_task(row["id"])
        if task is not None:
            await self._tasks.update(task, {"repeat": None, "status": "dismissed"})
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            await _update(conn, row["id"], {"state": "archived", "archived_at": now, "closed_at": None}, now)
        _log.info("intention_archived", intention_id=str(row["id"]))
        return await self.packet(intention_id, lang)

    # --- reading -------------------------------------------------------------------------------------------

    async def listed(
        self, kind: rules.Kind | None = None, states: tuple[str, ...] = ("proposed", "open")
    ) -> list[dict[str, Any]]:
        await self.follow_tasks()
        async with self._pool.connection() as conn:
            rows = await (
                await conn.cursor(row_factory=dict_row).execute(
                    f"SELECT {_COLUMNS} FROM intentions WHERE state = ANY(%s) AND (%s::text IS NULL OR "  # noqa: S608
                    "kind = %s) ORDER BY due_on NULLS LAST, created_at DESC LIMIT %s",
                    (list(states), kind, kind, rules.LISTED_MAX),
                )
            ).fetchall()
        return [_public(r) for r in rows]

    async def find(self, words: str, kind: rules.Kind | None = None) -> list[dict[str, Any]]:
        """The open intentions their words name (best first); the id itself works too."""
        found = await self.listed(kind)
        if (key := _uuid(words.strip())) is not None:
            return [i for i in found if i["id"] == str(key)]
        scored = sorted(
            ((match_score(words, f"{i['what'] or ''} {i['who'] or ''}"), i) for i in found),
            key=lambda s: -s[0],
        )
        return [i for score, i in scored if score >= 0.5]

    async def packet(self, intention_id: str, lang: str | None = None) -> dict[str, Any]:
        """The resumption packet: what, why, last step done, next step, the cue, the related mail, task and
        document, in one look."""
        lang = lang or self._owner.tag
        row = await self._row(intention_id)
        tz = self._owner.zone
        async with self._pool.connection() as conn:
            thread = task = doc = None
            place = None
            if row["source_ref"] and row["source"] in ("sent_mail", "received_mail"):
                got = await (
                    await conn.execute(
                        "SELECT id, subject FROM email_messages WHERE id = %s", (row["source_ref"],)
                    )
                ).fetchone()
                if got is not None:
                    thread = {"id": str(got[0]), "subject": str(got[1])[:200]}
            current = await _current_task(conn, row["id"])
            if current is not None:
                got = await (
                    await conn.execute(
                        "SELECT id::text, title, due_at FROM tasks WHERE id = %s", (uuid.UUID(current),)
                    )
                ).fetchone()
                if got is not None:
                    task = {"id": got[0], "title": got[1],
                            "due_at": got[2].astimezone(tz).isoformat() if got[2] else None}  # fmt: skip
            if row["doc_id"] is not None:
                got = await (
                    await conn.execute(
                        "SELECT id::text, kind, expires_on FROM vault_documents WHERE id = %s",
                        (row["doc_id"],),
                    )
                ).fetchone()
                if got is not None:
                    doc = {"id": got[0], "kind": got[1], "expires_on": got[2].isoformat()}
            if row["place_note_id"] is not None:
                got = await (
                    await conn.execute(
                        "SELECT place_name FROM place_notes WHERE id = %s", (row["place_note_id"],)
                    )
                ).fetchone()
                place = got[0] if got else None
        at = None
        if task and task["due_at"]:
            at = task["due_at"][:16].replace("T", " ")
        packet = rules.Packet(
            id=str(row["id"]), kind=row["kind"], state=row["state"], what=row["what"],
            why=intention_why(row["why"], row["kind"], lang),  # the model's line: checked on read
            last_step=row["last_step"], next_step=row["next_step"], who=row["who"],
            cue=rules.cue_words(row["cue"], row["cue_event"], row["cue_ref"], at, place, lang),
            due_on=row["due_on"], thread=thread, task=task, document=doc, source=row["source"],
            created_at=row["created_at"],
        )  # fmt: skip
        return {**packet.public(), "lines": rules.packet_lines(packet, lang)}

    # --- the minute: attend's sense ------------------------------------------------------------------------

    async def sense(self, now: datetime) -> int:
        """Attend's sense: the event cues that hold now, and the waiting-for items due today, as candidates
        (the guardian decides when Pisa speaks). Each once (a repeating event cue again after its period)."""
        await self.follow_tasks()
        made = 0
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            rows = await (
                await conn.cursor(row_factory=dict_row).execute(
                    f"SELECT {_COLUMNS} FROM intentions WHERE state = 'open' AND kind = 'mine' "  # noqa: S608
                    "AND cue = 'event' FOR UPDATE SKIP LOCKED"
                )
            ).fetchall()
            for row in rows:
                if not _armed(row, now):
                    continue
                since = max(row["created_at"], row["fired_at"] or row["created_at"], now - FRESH)
                if not await _event_held(conn, row["cue_event"], row["cue_ref"], since, now):
                    continue
                text = rules.fire_text(row["cue_event"], row["what"], row["next_step"], lang)
                # their reminder at a breakpoint, never a floor: the guardian weighs it (quiet
                # hours, a meeting, the voice caps)
                candidate = Candidate(
                    str(uuid.uuid4()), "intention", "reminder", text, lang, now, "breakpoint",
                )  # fmt: skip
                made += await propose(
                    conn,
                    candidate,
                    f"intention:{row['id']}:{now.isoformat()}",
                    {"intention_id": str(row["id"])},
                )
                await conn.execute(
                    "UPDATE intentions SET fired_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, row["id"]),
                )
            today = now.astimezone(self._owner.zone).date()
            # in the knock's morning the dossier carries their loops (a
            # waiting-for due, a promise for their yes): they wait for it; it marks what it said as nudged
            if self.morning_hold is not None and await self.morning_hold(now):
                if made:
                    _log.info("intentions_sensed", proposed=made)
                return made
            waits = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT id, what, who, source FROM intentions WHERE kind = 'waiting_for' AND "
                    "state = 'open' AND due_on <= %s AND nudged_at IS NULL FOR UPDATE SKIP LOCKED",
                    (today,),
                )
            ).fetchall()
            for row in waits:
                text = rules.still_waiting(row["who"], row["what"], lang)
                candidate = Candidate(
                    str(uuid.uuid4()), "intention", "info", text, lang, now,
                    outside=row["source"] == "received_mail",
                )  # fmt: skip
                made += await propose(
                    conn, candidate, f"waiting:{row['id']}", {"intention_id": str(row["id"])}
                )
                await conn.execute(
                    "UPDATE intentions SET nudged_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, row["id"]),
                )
            # a promise Pisa proposed from their own mail or dictation is said once, for their yes
            proposed = await (
                await conn.cursor(row_factory=dict_row).execute(
                    "SELECT id, what, due_on FROM intentions WHERE state = 'proposed' AND nudged_at IS NULL "
                    "FOR UPDATE SKIP LOCKED"
                )
            ).fetchall()
            for row in proposed:
                text = rules.proposed_text(row["what"], row["due_on"], lang)
                candidate = Candidate(str(uuid.uuid4()), "intention", "info", text, lang, now)
                made += await propose(
                    conn, candidate, f"proposed:{row['id']}", {"intention_id": str(row["id"])}
                )
                await conn.execute(
                    "UPDATE intentions SET nudged_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, row["id"]),
                )
        if made:
            _log.info("intentions_sensed", proposed=made)
        return made

    async def follow_tasks(self) -> int:
        """A task they completed or removed elsewhere (complete_task, the app) closes or archives its
        intention: done when its series' last task is done, archived when they dismissed or deleted it."""
        now = self._clock()
        async with self._pool.connection() as conn, conn.transaction():
            rows = await (
                await conn.execute(
                    # a series with an open task goes on (its next occurrence shares the done one's moment)
                    "SELECT i.id, last.status FROM intentions i JOIN tasks t ON t.id = i.task_id "
                    "JOIN LATERAL (SELECT s.status FROM tasks s WHERE coalesce(s.series_id, s.id) = "
                    "coalesce(t.series_id, t.id) ORDER BY s.status = 'open' DESC, s.created_at DESC, "
                    "s.status = 'done' DESC LIMIT 1) last ON true "
                    "WHERE i.state = 'open' AND last.status <> 'open' FOR UPDATE OF i SKIP LOCKED"
                )
            ).fetchall()
            for intention_id, status in rows:
                if status == "done":
                    await _update(conn, intention_id, {"state": "done", "closed_at": now}, now)
                else:
                    await _update(conn, intention_id, {"state": "archived", "archived_at": now}, now)
        return len(rows)

    async def _row(self, intention_id: str) -> dict[str, Any]:
        key = _uuid(intention_id)
        async with self._pool.connection() as conn:
            row = (
                await (
                    await conn.cursor(row_factory=dict_row).execute(
                        f"SELECT {_COLUMNS} FROM intentions WHERE id = %s",  # noqa: S608
                        (key,),
                    )
                ).fetchone()
                if key
                else None
            )
        if row is None:
            raise PisaError("not_found", "No such intention.", "Refresh the list")
        return row

    async def _current_task(self, intention_id: uuid.UUID) -> str | None:
        async with self._pool.connection() as conn:
            return await _current_task(conn, intention_id)

    async def _kept_already(
        self, kind: str, what: str, cue: str, due_at: str | None, due_kind: str | None
    ) -> str | None:
        """An open intention of this kind for the same item (`same_item`) with the same cue and, for a time
        cue, the same due moment; conservative: a different time or place is a second intention."""
        if cue not in ("time", "none"):
            return None
        due = None
        if cue == "time" and due_at:
            kind_of = cast(Any, due_kind or ("date" if len(due_at.strip()) == 10 else "datetime"))
            due = parse_due(due_at, kind_of, self._owner.tz, self._clock())
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT i.id::text, i.what, t.due_at FROM intentions i "
                    "LEFT JOIN tasks t ON t.id = i.task_id "
                    "WHERE i.state = 'open' AND i.kind = %s AND i.cue = %s",
                    (kind, cue),
                )
            ).fetchall()
        for intention_id, kept, kept_due in rows:
            if kept and same_item(what, kept) and kept_due == due:
                return str(intention_id)
        return None

    async def _recent_task(self, title: str) -> str | None:
        """An open pool task with these words made in the last minutes and carrying no intention yet."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT t.id::text FROM tasks t WHERE t.status = 'open' AND t.title_key = %s AND "
                    "t.created_at >= %s AND NOT EXISTS (SELECT 1 FROM intentions i WHERE i.task_id = t.id) "
                    "ORDER BY t.created_at DESC LIMIT 1",
                    (title_key(title), self._clock() - RECENT_TASK),
                )
            ).fetchone()
        return row[0] if row else None


# --- helpers ----------------------------------------------------------------------------------------------


def _title(what: str | None, lang: str) -> str:
    title = (what or ("Promemoria" if lang.startswith("it") else "Reminder"))[:120]
    return title if len(title) >= 3 else f"{title} ({'promemoria' if lang.startswith('it') else 'reminder'})"


def _notes(why: str | None, next_step: str | None, lang: str) -> str | None:
    """The pool task's notes (only a "mine" intention has one); its `why` is the model's: in their
    language."""
    it = lang.startswith("it")
    why = intention_why(why, "mine", lang)
    parts = [f"{'Perché' if it else 'Why'}: {why}" if why else "",
             f"{'Prossimo passo' if it else 'Next step'}: {next_step}" if next_step else ""]  # fmt: skip
    return " · ".join(p for p in parts if p) or None


def _armed(row: dict[str, Any], now: datetime) -> bool:
    """An event cue fires once; a repeating one again after its period."""
    if row["fired_at"] is None:
        return True
    repeat = row["repeat"]
    if not repeat:
        return False
    return now - row["fired_at"] >= timedelta(days=_PERIOD[repeat["every"]] * int(repeat["interval"]))


async def _event_held(
    conn: AsyncConnection, event: str, ref: str | None, since: datetime, now: datetime
) -> bool:
    if event == "meeting_end":
        rows = await (
            await conn.execute(
                "SELECT title FROM calendar_events WHERE NOT all_day AND status <> 'cancelled' "
                "AND ends_at > %s AND ends_at <= %s",
                (since, now),
            )
        ).fetchall()
        return any(rules.names_it(ref, str(title)) for (title,) in rows)
    if event == "arrive_home":
        home = await home_store.read(conn)
        if home is None:
            return False
        rows = await (
            await conn.execute(
                "SELECT lat, lon FROM stays WHERE arrived_at > %s AND arrived_at <= %s", (since, now)
            )
        ).fetchall()
        return any(distance_m((lat, lon), (home.lat, home.lon)) <= geo.HOME_M for lat, lon in rows)
    # a call they placed through Pisa's button: over once they told Pisa, or its follow-up time
    if event == "call_end":
        rows = await (
            await conn.execute(
                "SELECT target FROM handoff_preps WHERE purpose = 'call' AND opened_at > %s AND "
                "coalesce(outcome_at, follow_up_at) <= %s",
                (since, now),
            )
        ).fetchall()
        return any(rules.names_it(ref, str(target)) for (target,) in rows)
    return False


async def _current_task(conn: AsyncConnection, intention_id: uuid.UUID) -> str | None:
    """The open task of the intention's series (a repeating task moves on to its next occurrence)."""
    row = await (
        await conn.execute(
            "SELECT s.id::text FROM intentions i JOIN tasks t ON t.id = i.task_id JOIN tasks s ON "
            "coalesce(s.series_id, s.id) = coalesce(t.series_id, t.id) WHERE i.id = %s AND s.status = 'open' "
            "ORDER BY s.created_at DESC LIMIT 1",
            (intention_id,),
        )
    ).fetchone()
    return row[0] if row else None


async def _document(conn: AsyncConnection, kind: str | None) -> uuid.UUID | None:
    if not kind:
        return None
    row = await (
        await conn.execute(
            "SELECT id FROM vault_documents WHERE kind = %s ORDER BY expires_on LIMIT 1", (kind,)
        )
    ).fetchone()
    return row[0] if row else None


async def _update(conn: AsyncConnection, intention_id: Any, changes: dict[str, Any], now: datetime) -> None:
    from psycopg import sql

    values = {**changes, "updated_at": now}
    query = sql.SQL("UPDATE intentions SET {} WHERE id = %s").format(
        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in values)
    )
    await conn.execute(query, (*values.values(), intention_id))


async def _insert_proposed(
    conn: AsyncConnection,
    *,
    source: str,
    source_ref: str | None,
    what: str,
    who: str | None,
    due_on: date | None,
    now: datetime,
) -> uuid.UUID | None:
    row = await (
        await conn.execute(
            "INSERT INTO intentions (id, kind, state, what, who, due_on, source, source_ref, created_at, "
            "updated_at) VALUES (%s, 'mine', 'proposed', %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING "
            "RETURNING id",
            (uuid.uuid4(), rules.clean(what, rules.WHAT_MAX), who, due_on, source, source_ref, now, now),
        )
    ).fetchone()
    return row[0] if row else None


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "kind": row["kind"], "state": row["state"], "what": row["what"],
        "why": row["why"], "next_step": row["next_step"], "last_step": row["last_step"], "who": row["who"],
        "cue": row["cue"], "cue_event": row["cue_event"], "cue_ref": row["cue_ref"], "repeat": row["repeat"],
        "due_on": row["due_on"].isoformat() if row["due_on"] else None,
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "place_note_id": str(row["place_note_id"]) if row["place_note_id"] else None,
        "source": row["source"], "asked": row["asked"],
        "nudged_at": row["nudged_at"].isoformat() if row["nudged_at"] else None,
        "created_at": row["created_at"].isoformat(),
    }  # fmt: skip
