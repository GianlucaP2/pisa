"""Pisa's ready answers (`pisa.domain.ready`), kept per language in this process. Built from
Pisa's own data with no paid call; refreshed when a talk opens, after Pisa makes a change, and whenever a
question finds Pisa's data changed since (that question goes to the brain: `ready_stale`), so none is older
than 5 minutes when it is said."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from pisa.domain import ready as rules
from pisa.domain import talk as form
from pisa.domain import zone
from pisa.domain.owner import Owner
from pisa.logging import get_logger
from pisa.services.plan_store import stored_current

_log = get_logger("pisa.ready")
TOP = 3


# Pisa's changes and promises a ready answer can be about (today's, their zone)
VERSION_WINDOW = timedelta(days=2)


class ReadyService:
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
        self._answers: dict[str, dict[rules.Topic, rules.Answer]] = {}
        self._built_with: dict[str, tuple[str, ...]] = {}  # the data each language's answers were built from
        self._refreshing: dict[str, asyncio.Task[None]] = {}

    # --- what the user asks ----------------------------------------------------------------------------

    async def match(self, words: str, lang: str) -> rules.Hit | None:
        """The ready answer for these words: a hit (said at once), stale (Pisa's data changed since: the brain
        answers, a refresh starts), or None (no topic, or none ready yet)."""
        topic = rules.topic_of(words)
        if topic is None:
            return None
        built = self._answers.get(lang)
        if built is None:
            self.refresh_soon(lang)
            return None  # none ready yet: the talk's open builds them
        async with self._pool.connection() as conn:
            version = await self._version(conn)
        answer = built.get(topic)
        if answer is None:  # a topic with nothing to stand on (no plan today): the brain answers
            if version != self._built_with.get(lang):
                self.refresh_soon(lang)  # Pisa's data changed since: perhaps there is now
            return None
        hit = rules.judge(answer, self._clock(), version)
        if not hit.hit:
            self.refresh_soon(lang)
        return hit

    # --- keeping them ready -------------------------------------------------------------------------------

    def forget(self) -> None:
        """Every ready answer gone (the replay's next scenario is another world)."""
        self._answers.clear()
        self._built_with.clear()

    def changed(self) -> None:
        """Pisa made a change: every language's answers are rebuilt."""
        for lang in list(self._answers):
            self.refresh_soon(lang)

    def refresh_soon(self, lang: str) -> None:
        running = self._refreshing.get(lang)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(self._refresh_logged(lang))
        self._refreshing[lang] = task

    async def wait(self) -> None:
        """Tests and shutdown: the refreshes under way are done."""
        if running := [t for t in self._refreshing.values() if not t.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    async def _refresh_logged(self, lang: str) -> None:
        try:
            await self.refresh(lang)
        except Exception as err:  # a refresh never breaks a talk: the brain answers instead
            _log.warning("ready_refresh_failed", error=type(err).__name__)

    async def refresh(self, lang: str) -> None:
        now = self._clock()
        async with self._pool.connection() as conn:
            version = await self._version(conn)
            texts: dict[rules.Topic, str] = {
                "changes": rules.line("changes", await self._changes(conn, now, lang), lang),
                "pool": rules.line("pool", await self._pool_top(conn), lang),
            }
            # with no plan for today there is no "next" to say (the brain answers)
            if (next_item := await self._next(conn, now, lang)) is not None:
                texts["next"] = form.next_line(next_item, lang)
            # "what Pisa is looking into" has no source of its own yet: the brain answers;
            # Pisa's reminders are no research
        self._answers[lang] = {t: rules.Answer(t, text, now, version) for t, text in texts.items()}
        self._built_with[lang] = version
        _log.info("ready_refreshed", lang=lang, topics=len(texts))

    async def _version(self, conn: AsyncConnection) -> tuple[str, ...]:
        """What Pisa's data looks like now: any change to the plan, Pisa's changes (an undo too), the tasks,
        Pisa's promises, and where the user is. Pisa's changes and promises are read
        over the last days only (ready answers are about today), each window an index range (0091), never a
        scan of every row kept."""
        since = self._clock() - VERSION_WINDOW
        row = await (
            await conn.execute(
                "SELECT (SELECT max(generated_at) FROM plans)::text, "
                "(SELECT count(*) || '/' || count(undone_at) || '/' || count(executed_at) || '/' || "
                "coalesce(max(greatest(executed_at, created_at, undone_at))::text, '') FROM approvals "
                "WHERE created_at >= %s OR executed_at >= %s OR undone_at >= %s), "
                # the open pool itself (a task done, renamed or reprioritised), whatever its clock said
                "(SELECT md5(coalesce(string_agg("
                "id::text || title || priority || coalesce(due_at::text, ''), "
                "',' ORDER BY id), '')) FROM tasks WHERE status = 'open'), "
                "(SELECT count(*) || '/' || count(fired_at) || '/' || count(cancelled_at) || '/' || "
                "coalesce(max(greatest(created_at, fired_at, cancelled_at))::text, '') FROM pisa_promises "
                "WHERE created_at >= %s OR fired_at >= %s OR cancelled_at >= %s), "
                "(SELECT tz || coalesce(hour_cycle, '') FROM owner_context WHERE id)",
                (since,) * 6,
            )
        ).fetchone()
        return (*(str(v) for v in (row or ())), self._owner.tz, self._owner.hour_cycle())

    async def _next(self, conn: AsyncConnection, now: datetime, lang: str) -> str | None:
        """The plan's next item as they read its time ("Dentista alle 15:30", "Dentist at 3:30 PM"), "" when
        the
        plan has none left, None with no plan for today. An email's task is outside text: never."""
        tz = self._owner.zone
        local = now.astimezone(tz)
        # as their lists are now: a plan made before a task was added, closed or deleted is none to read
        plan = await stored_current(conn, local.date())
        if plan is None:
            return None
        emails = {
            str(r[0])
            for r in await (await conn.execute("SELECT id FROM tasks WHERE source_kind = 'email'")).fetchall()
        }
        for item in plan.get("items", []):
            start = item.get("start")
            if item.get("kind") not in ("event", "task") or item.get("status") in rules.NOT_NEXT:
                continue
            if item.get("kind") == "task" and str(item.get("task_id")) in emails:
                continue
            if start and datetime.fromisoformat(start) > now:
                at = "alle" if lang == "it-IT" else "at"
                when = zone.clock_text(
                    datetime.fromisoformat(start).astimezone(tz), self._owner.hour_cycle(lang)
                )
                return f"{item.get('title', '')} {at} {when}"
        return ""

    async def _changes(self, conn: AsyncConnection, now: datetime, lang: str) -> list[str]:
        """Pisa's own changes today as Pisa said them when done ("Ho spostato «Cena» a venerdì alle 21"),
        never one undone
        since nor one made after outside text (RAIL: never the approval's question)."""
        local = now.astimezone(self._owner.zone)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = await (
            await conn.execute(
                "SELECT payload->>'said', payload->>'title' FROM approvals WHERE status = 'done' "
                "AND undone_at IS NULL AND executed_at >= %s AND executed_at < %s "
                "AND NOT coalesce((payload->>'after_outside')::boolean, false) "
                "ORDER BY executed_at DESC LIMIT %s",
                (start, start + timedelta(days=1), TOP),
            )
        ).fetchall()
        return [_done_line(said, title, lang) for said, title in rows if said or title]

    async def _pool_top(self, conn: AsyncConnection) -> list[str]:
        """The user's own open tasks, highest priority first (an email's task is outside text: never)."""
        rows = await (
            await conn.execute(
                "SELECT title FROM tasks WHERE status = 'open' AND source_kind <> 'email' "
                "ORDER BY priority, due_at NULLS LAST, created_at LIMIT %s",
                (TOP,),
            )
        ).fetchall()
        return [r[0] for r in rows]


def _done_line(said: str | None, title: str | None, lang: str) -> str:
    """Pisa's own line once the change was done ("Ho spostato «Cena» a venerdì alle 21.") as an item of
    today's list ("ho spostato «Cena» a venerdì alle 21"); its title alone when the line is missing."""
    if not said:
        return ("ho cambiato «{t}»" if lang == "it-IT" else "I changed «{t}»").format(t=title or "?")
    text = said.strip().rstrip(".")
    return text[:1].lower() + text[1:] if lang == "it-IT" else text
