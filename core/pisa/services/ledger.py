"""The ledger: a view over what Pisa already records, one week at a time, every line with
its why, its source and its Undo where one exists (the paths, run by `undo`):

- `pisa_decisions` acts (and proposals their yes made done, and promises): with their approval (a calendar
  change, a sent email), their task (aged out, a bill, a vault reminder) or nothing else (task edits);
- `done_by_presence`: what closed because they were there or said so;
- `task_bulk`: tasks changed at once.

The Sunday card (`sunday_tick`, Sunday 18:00 in their zone, once a week: the worker's period
`schedule.SUNDAY_CARD`) queues a notify with `ledger_week`; quiet hours hold it. Its sheet is `summary`'s.
The card's € per accepted outcome, the user budget's week of AI spend (every call, the failed ones' cost
kept) over their yeses to Pisa's proposals, the tasks Pisa made that got done and Pisa's drafts they sent
(`_accepted`; counts only).
"""

import uuid
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol

from psycopg import AsyncConnection

from pisa.domain import cost as cost_per
from pisa.domain import cost_found as cost_rules
from pisa.domain import guardian as guardian_rules
from pisa.domain import ledger as rules
from pisa.domain import schedule, wrist
from pisa.domain import tasks_bulk as bulk_rules
from pisa.domain import unlocks as unlock_rules
from pisa.domain import voice6 as voice6_rules
from pisa.domain.decisions import Decision
from pisa.domain.errors import PisaError
from pisa.domain.owner import Owner
from pisa.domain.reasons import purpose_said
from pisa.domain.sheet import SheetData
from pisa.infra import spend
from pisa.logging import get_logger
from pisa.services import cost_found, erase
from pisa.services import decisions as decision_log
from pisa.services.autonomy import AutonomyService
from pisa.services.guardian_tick import holding as guardian_holding
from pisa.services.guardian_tick import week as guardian_week
from pisa.services.money import MoneyService
from pisa.services.notifications import enqueue

_log = get_logger("pisa.ledger")
# a task Pisa put in the pool (a bill, a renewal) can be taken back for a week
PISA_TASK_UNDO = timedelta(days=7)
_DONE_WHY = {
    "it-IT": {"stay": "Chiuso: eri lì.", "voice": "Chiuso: l'hai detto tu."},
    "en-US": {"stay": "Closed: you were there.", "voice": "Closed: you said so."},
}
_BULK_WHY = {
    "it-IT": {
        "done": "{n} attività segnate fatte insieme.",
        "dismiss": "{n} attività tolte insieme.",
        "delete": "{n} attività eliminate insieme.",
    },
    "en-US": {
        "done": "{n} tasks marked done together.",
        "dismiss": "{n} tasks dismissed together.",
        "delete": "{n} tasks deleted together.",
    },
}
_PROFILE_WHY = {  # the Sunday ledger's own words
    "it-IT": "Ho tolto {n} cose che avevo capito male di te.",
    "en-US": "I removed {n} things I had misunderstood about you.",
}
_TYPE_BY_TOOL = {
    "complete_task": "task_done",
    "aging": "task_close",
    "draft_email": "email_draft",
    "draft_reply": "email_draft",
}


class Undoers(Protocol):
    """The existing Undo paths the ledger runs."""

    async def approval(self, approval_id: str) -> object: ...
    async def done_task(self, task_id: str) -> object: ...
    async def done_event(self, event_id: str) -> object: ...
    async def bulk(self, bulk_id: str) -> object: ...
    async def task_status(self, task_id: str, status: str) -> object: ...


def _expired() -> PisaError:
    return PisaError("undo_expired", "This can't be undone any more.", "Change it by hand")


def _not_found() -> PisaError:
    return PisaError("not_found", "There is no such line in the ledger.", "Check the id")


def _key(only: str | None, prefix: str) -> tuple[str | None, str | None]:
    """The id one source's query is narrowed to (both placeholders), or (None, None) for every line; a line
    of another source matches nothing."""
    if only is None:
        return None, None
    kind, _, key = only.partition(":")
    value = key if kind == prefix else "00000000-0000-0000-0000-000000000000"
    return value, value


class LedgerService:
    def __init__(
        self,
        pool: Any,
        undoers: Undoers,
        autonomy: AutonomyService,
        money: MoneyService,
        *,
        owner: Owner,
        clock: Callable[[], datetime],
        skills: Any = None,
        owner_loop: Any = None,
        voice2: Any = None,
        voice6: Any = None,
        profile: Any = None,
    ) -> None:
        self._pool = pool
        self._undo = undoers
        self._autonomy = autonomy
        self._money = money
        self._owner = owner
        self._clock = clock
        self.skills = skills  # Pisa's skills (what it learned this week, the withered)
        self.owner_loop = owner_loop  # the user loop: the talk report's one line
        self.voice2 = voice2  # the week's cue-questions, counted
        self.voice6 = voice6  # the week's prurito rows, counted (changed something, nothing)
        self.profile = profile  # Pisa's own corrections of their profile, restored by their Undo

    def _bounds(self, monday: date) -> tuple[datetime, datetime]:
        tz = self._owner.zone
        start = datetime.combine(monday, time(0), tz)
        return start, datetime.combine(monday + timedelta(days=7), time(0), tz)

    async def lines(
        self, conn: AsyncConnection, start: datetime, end: datetime, lang: str, only: str | None = None
    ) -> list[dict[str, Any]]:
        """The lines in [start, end), newest first (`at` still a datetime, `undone` set)."""
        now = self._clock()
        words = "it-IT" if lang == "it-IT" else "en-US"
        out: list[dict[str, Any]] = []
        rows = await (
            await conn.execute(
                "SELECT d.id, d.at, d.move, d.tool, d.ref, d.purpose, d.provenance, d.rail, d.outcome, "
                "a.id::text, a.kind, coalesce(a.result, a.summary), a.status, a.undo_until, a.undone_at, "
                "t.title, t.status, p.what, al.level, coalesce(a.decided_origin, d.origin) "
                "FROM pisa_decisions d LEFT JOIN approvals a ON a.id::text = d.ref "
                "LEFT JOIN tasks t ON t.id::text = d.ref LEFT JOIN pisa_promises p ON p.id::text = d.ref "
                "LEFT JOIN autonomy_levels al ON d.tool = 'autonomy_level' AND al.type = split_part(d.ref, "
                "':', 1) "
                "WHERE d.at >= %s AND d.at < %s AND d.move IN ('act', 'propose', 'promise') "
                "AND (%s::uuid IS NULL OR d.id = %s::uuid) ORDER BY d.n DESC",
                (start, end, *_key(only, "decision")),
            )
        ).fetchall()
        for (
            decision,
            at,
            move,
            tool,
            ref,
            why,
            provenance,
            rail,
            outcome,
            approval,
            akind,
            summary,
            astatus,
            until,
            undone_at,
            title,
            tstatus,
            what,
            level_now,
            origin,
        ) in rows:
            if tool in rules.DUPLICATED:
                continue
            if approval is not None and astatus != "done":
                continue  # a proposal still waiting, refused or failed: nothing was done
            if approval is None and (move == "propose" or outcome not in (None, "done")):
                continue
            source: rules.Source = (
                "owner_yes"
                if move == "propose" and provenance != "outside_text"
                else rules.source_of(tool, provenance, rail)
            )
            line: dict[str, Any] = {
                "id": f"decision:{decision}",
                "at": at,
                "kind": rules.kind_of(tool),
                "title": summary or title or what,
                "why": purpose_said(why, rules.kind_of(tool), lang) or "",
                "source": source,
                "origin": origin,
                "type": akind
                if akind in ("calendar_create", "calendar_move", "calendar_cancel", "email_send")
                else _TYPE_BY_TOOL.get(tool),
                "undo": {"available": False, "until": None},
                "undone": False,
            }
            if approval is not None:
                line["undone"] = undone_at is not None
                line["undo"] = {
                    "available": undone_at is None and until is not None and until > now,
                    "until": until,
                }
            elif tool == "aging" and tstatus is not None:
                line["undone"] = tstatus != "dismissed"
                line["undo"] = {"available": tstatus == "dismissed", "until": None}
            elif tool in ("bill_task", "vault_reminder", "money_task") and tstatus is not None:
                line["undone"] = tstatus == "dismissed"
                until = at + PISA_TASK_UNDO
                line["undo"] = {"available": tstatus == "open" and until > now, "until": until}
            elif tool == "autonomy_level" and ref:
                level = ref.split(":")[-1]
                line["undone"] = level_now != level
                line["undo"] = {"available": level != "ask" and level_now == level, "until": None}
            out.append(line)
        for row_id, task_id, _event, title, source_kind, at, until, undone, origin in await (
            await conn.execute(
                "SELECT id, task_id::text, event_id, title, source, done_at, undo_until, undone_at, origin "
                "FROM done_by_presence WHERE done_at >= %s AND done_at < %s AND (%s::uuid IS NULL OR id = "
                "%s::uuid)",
                (start, end, *_key(only, "done")),
            )
        ).fetchall():
            out.append(
                {
                    "id": f"done:{row_id}",
                    "at": at,
                    "kind": "task_done" if task_id else "event_done",
                    "title": title,
                    "why": _DONE_WHY[words].get(source_kind, ""),
                    "source": "owner_said" if source_kind == "voice" else "rule",
                    "origin": origin,
                    "type": "task_done" if task_id else None,
                    "undone": undone is not None,
                    "undo": {"available": undone is None and until > now, "until": until},
                }
            )
        for bulk_id, action, count, at, undone in await (
            await conn.execute(
                "SELECT id, action, jsonb_array_length(items), created_at, undone_at FROM task_bulk "
                "WHERE created_at >= %s AND created_at < %s AND (%s::uuid IS NULL OR id = %s::uuid)",
                (start, end, *_key(only, "bulk")),
            )
        ).fetchall():
            until = at + bulk_rules.UNDO_KEPT
            out.append(
                {
                    "id": f"bulk:{bulk_id}",
                    "at": at,
                    "kind": "tasks_bulk",
                    "title": None,
                    "why": _BULK_WHY[words].get(action, "").format(n=count),
                    "source": "owner_said",
                    "type": None,
                    "undone": undone is not None,
                    "undo": {"available": undone is None and until > now, "until": until},
                }
            )
        for batch, at, count, restored in await (
            await conn.execute(
                "SELECT id, at, count, restored_at FROM profile_retirements WHERE at >= %s AND at < %s "
                "AND count > 0 AND (%s::uuid IS NULL OR id = %s::uuid)",
                (start, end, *_key(only, "profile")),
            )
        ).fetchall():  # what Pisa took back of what it understood of them, restorable
            out.append(
                {
                    "id": f"profile:{batch}",
                    "at": at,
                    "kind": "other",
                    "title": None,
                    "why": _PROFILE_WHY[words].format(n=count),
                    "source": "job",
                    "type": None,
                    "undone": restored is not None,
                    "undo": {"available": restored is None and self.profile is not None, "until": None},
                }
            )
        for line in out:  # what they did on the watch says so
            line.setdefault("origin", "phone")
            if (where := wrist.from_line(line["origin"], words)) and where not in line["why"]:
                line["why"] = f"{line['why']} · {where}" if line["why"] else where
        out.sort(key=lambda ln: ln["at"], reverse=True)
        return out

    def _wire(self, line: dict[str, Any]) -> dict[str, Any]:
        tz = self._owner.zone
        until = line["undo"]["until"]
        out = {k: v for k, v in line.items() if k != "undone"}
        out["at"] = line["at"].astimezone(tz).isoformat()
        out["undo"] = {
            "available": line["undo"]["available"],
            "until": until.astimezone(tz).isoformat() if until is not None else None,
        }
        if out.get("type") is None:
            out.pop("type", None)
        return out

    async def week(self, week: str | None, page: int = 0) -> dict[str, Any]:
        try:
            monday = rules.parse_week(week, self._owner.day(self._clock()))
        except ValueError:
            raise PisaError(
                "validation_failed", "The week must be YYYY-Www (an ISO week).", "Check the week"
            ) from None
        start, end = self._bounds(monday)
        async with self._pool.connection() as conn:
            lines = await self.lines(conn, start, end, await self._owner.lang(conn))
        tz = self._owner.zone
        days: dict[date, list[dict[str, Any]]] = {}
        # the week is paged, never cut (each line's Undo stays reachable)
        shown = lines[page * rules.LINES_MAX : (page + 1) * rules.LINES_MAX]
        for ln in shown:
            days.setdefault(ln["at"].astimezone(tz).date(), []).append(self._wire(ln))
        return {
            "week": rules.week_of(monday),
            "from": monday.isoformat(),
            "to": (monday + timedelta(days=6)).isoformat(),
            "days": [{"date": d.isoformat(), "lines": ls} for d, ls in sorted(days.items(), reverse=True)],
            "next_page": page + 1 if len(lines) > (page + 1) * rules.LINES_MAX else None,
            "totals": {
                "lines": len(lines),
                "undone": sum(1 for ln in lines if ln["undone"]),
                "by_kind": dict(Counter(ln["kind"] for ln in lines)),
                "by_source": dict(Counter(ln["source"] for ln in lines)),
            },
        }

    async def find(self, line_id: str) -> dict[str, Any]:
        """A line by its id, from any week Pisa still keeps (90 days back)."""
        now = self._clock()
        async with self._pool.connection() as conn:  # by its id, never a capped list
            lines = await self.lines(
                conn,
                now - timedelta(days=90),
                now + timedelta(minutes=1),
                await self._owner.lang(conn),
                only=line_id,
            )
        if not lines:
            raise _not_found()
        return lines[0]

    async def undo(self, line_id: str) -> dict[str, Any]:
        """RAIL: only the item's own Undo, only while it lasts (409 `undo_expired` otherwise)."""
        prefix, _, key = line_id.partition(":")
        try:
            uuid.UUID(key)
        except ValueError:
            raise _not_found() from None
        line = await self.find(line_id)
        if not line["undo"]["available"]:
            raise _expired()
        async with self._pool.connection() as conn:
            if prefix == "bulk":
                await self._undo.bulk(key)
            elif prefix == "profile":
                await self.profile.restore(key)
            elif prefix == "done":
                row = await (
                    await conn.execute(
                        "SELECT task_id::text, event_id FROM done_by_presence WHERE id = %s", (key,)
                    )
                ).fetchone()
                if row is None:
                    raise _not_found()
                await (self._undo.done_task(row[0]) if row[0] else self._undo.done_event(row[1]))
            else:
                row = await (
                    await conn.execute("SELECT tool, ref FROM pisa_decisions WHERE id = %s", (key,))
                ).fetchone()
                if row is None or row[1] is None:
                    raise _expired()
                tool, ref = row
                if tool == "aging":
                    await self._undo.task_status(ref, "open")
                elif tool in ("bill_task", "vault_reminder", "money_task"):
                    await self._undo.task_status(ref, "dismissed")
                elif tool == "autonomy_level":
                    await self._autonomy.lower(ref.split(":")[0], await self._owner.lang(conn))
                else:
                    await self._undo.approval(ref)
        _log.info("ledger_undone", kind=line["kind"])
        return self._wire(await self.find(line_id))

    async def summary(self, week: str | None) -> dict[str, Any]:
        try:
            monday = rules.parse_week(week, self._owner.day(self._clock()))
        except ValueError:
            raise PisaError(
                "validation_failed", "The week must be YYYY-Www (an ISO week).", "Check the week"
            ) from None
        start, end = self._bounds(monday)
        async with self._pool.connection() as conn:
            lang = await self._owner.lang(conn)
            lines = await self.lines(conn, start, end, lang)
            # the month of the card's Sunday, to the week's end (or now), as recorded
            spent_found = await cost_found.month(
                conn, monday + timedelta(days=6), min(end, self._clock()), self._owner.tz
            )
            accepted = await self._accepted(conn, start, end)
            week_spent = await self._week_spent(conn, start, end)
            held, passed, never_out = await guardian_week(conn, start, end)
            holding = await guardian_holding(conn)
        week_key = rules.week_of(monday)
        # announced once; at most MAX_SHOWN a card (the rest wait for the next)
        unlocks = (await self._autonomy.unlocks(week_key, end, lang))[: unlock_rules.MAX_SHOWN]
        done = [ln for ln in lines if ln["kind"] not in ("stop", "card")]
        top = rules.mattered(done)
        undoable = [ln for ln in done if ln["undo"]["available"]][: rules.UNDOABLE_MAX]
        # one proposal per card (the type with the most evidence), at most one rung a type a week
        proposals = sorted(await self._autonomy.proposals(lang), key=lambda p: -p["kept"])[:1]
        metrics = await self._autonomy.metrics(start, end)
        subscriptions = await self._money.subscriptions(lang)
        undone = sum(1 for ln in done if ln["undone"])
        skills = (
            await self.skills.week(start, end, lang)
            if self.skills is not None
            else {"learned": [], "withered": []}
        )
        # the user loop: the week's talk report as one line (numbers only, made by code from the week's rows)
        talk_line = await self.owner_loop.card_line(week_key, lang) if self.owner_loop is not None else None
        cues = await self.voice2.week(start, end) if self.voice2 is not None else None
        curious = await self.voice6.week(start, end) if self.voice6 is not None else None
        async with self._pool.connection() as conn:  # the cost of keeping
            store = {
                "bytes": await erase.store_size(conn),
                "families": await erase.kept(conn),
                "offsite_backup_at": await erase.offsite_backup_at(conn),
            }
        sheet = rules.sheet(
            lang,
            total=len(done),
            undone=undone,
            top=top,
            undoable=undoable,
            proposals=proposals,
            subscriptions=subscriptions,
            skills=skills,
            cost_line=cost_rules.line(spent_found, lang),
            unlocks=unlocks,
            talk_line=talk_line,
            cue_line=rules.cue_line(cues, lang) if cues else None,
            curious_line=voice6_rules.card_line(curious["changed"], curious["quiet"], curious["opened"], lang)
            if curious
            else None,
            store=rules.store_line(store["bytes"], store["families"], lang),
            outcome_line=cost_per.card_line(week_spent, accepted, lang),
            interruptions_line=guardian_rules.card_line(held, passed, never_out, lang),
            backup=rules.backup_ask(store["offsite_backup_at"], self._clock(), lang),
        )
        SheetData.model_validate(sheet)  # RAIL: the app's own templates, the protocol's limits
        return {
            "week": rules.week_of(monday),
            "counts": dict(Counter(ln["kind"] for ln in done)),
            "total": len(done),
            "undone": undone,
            "mattered": [self._wire(ln) for ln in top],
            "undoable": [self._wire(ln) for ln in undoable],
            "proposals": proposals,
            "subscriptions": subscriptions,
            "skills": skills,
            "metrics": metrics,
            "cost_found": cost_rules.wire(spent_found, lang),
            "unlocks": unlocks,
            "cost_per_outcome": cost_per.week(week_spent, accepted),
            "interruptions": {"held": held, "passed": passed, "never_out": never_out, "holding": holding},
            "store": store | {"offsite_backup_at": _iso(store["offsite_backup_at"])},
            "sheet": sheet,
        }

    @staticmethod
    async def _accepted(conn: AsyncConnection, start: datetime, end: datetime) -> cost_per.Accepted:
        """The week's accepted outcomes, from what Pisa records. Their yes to a change Pisa proposed or
        held (never their own words' change, never a send: that is the draft's), done and not undone; a task
        Pisa made (from an email, their calendar, Pisa's own initiative or proposal) done this week; a draft
        of Pisa's they sent, not undone."""
        row = await (
            await conn.execute(
                "SELECT (SELECT count(*) FROM approvals WHERE status = 'done' AND executed_at >= %s "
                "AND executed_at < %s AND origin <> 'words' AND kind <> 'email_send' AND undone_at IS NULL "
                "AND decided_auth IN ('tap', 'voice', 'biometric', 'words')), "
                "(SELECT count(*) FROM tasks t WHERE t.status = 'done' AND t.updated_at >= %s "
                "AND t.updated_at < %s AND (t.source_kind IN ('email', 'calendar') OR EXISTS ("
                "SELECT 1 FROM pisa_decisions d WHERE d.ref = t.id::text AND d.move IN ('act', 'propose') "
                "AND d.provenance IN ('pisa_initiative', 'pisa_proposal')))), "
                "(SELECT count(*) FROM email_drafts e LEFT JOIN approvals a ON a.id = e.approval_id "
                "WHERE e.sent_at >= %s AND e.sent_at < %s AND a.undone_at IS NULL)",
                (start, end, start, end, start, end),
            )
        ).fetchone()
        yes, tasks, drafts = row or (0, 0, 0)
        return cost_per.Accepted(int(yes), int(tasks), int(drafts))

    @staticmethod
    async def _week_spent(conn: AsyncConnection, start: datetime, end: datetime) -> Decimal:
        """The user budget's AI spend in the week as the ledger counts it (a failed call's
        recorded cost and a running call's reservation included: a failure's cost counts)."""
        row = await (
            await conn.execute(
                f"SELECT {spend.SPEND} FROM llm_calls WHERE budget = 'owner' "  # noqa: S608
                "AND created_at >= %s AND created_at < %s",
                (start, end),
            )
        ).fetchone()
        return Decimal(row[0]) if row else Decimal(0)

    async def sunday_tick(self) -> bool:
        """The card, once a week from Sunday 18:00 in their zone (the worker's step, period
        `schedule.SUNDAY_CARD`; an initiative step: it waits while Pisa is stopped): Pisa's proposals marked
        made, one notify with the week that Sunday ends, a ledger line."""
        now = self._clock()
        week = rules.week_of(schedule.SUNDAY_CARD.start(self._owner.local(now)).date())
        summary = await self.summary(week)
        await self._autonomy.mark_proposed(summary["proposals"])
        await self._autonomy.mark_announced(summary["unlocks"], week)  # once, in this card
        async with self._pool.connection() as conn, conn.transaction():
            lang = await self._owner.lang(conn)
            key = "it-IT" if lang == "it-IT" else "en-US"
            extra = rules.CARD_EXTRA[key].format(p=len(summary["proposals"])) if summary["proposals"] else ""
            text = rules.CARD_TEXT[key].format(n=summary["total"], extra=extra)
            await enqueue(
                conn,
                kind="info",
                text=text,
                lang=lang,
                now=now,
                timezone=self._owner.tz,
                ledger_week=week,
                path="sunday.card",
            )
            await decision_log.store(
                conn,
                [Decision("act", "sunday_card", "pisa_initiative", purpose=text[:120], outcome="done")],
                at=now,
                trace_id=None,
                device_id=None,
            )
        _log.info("sunday_card_queued", lines=summary["total"], proposals=len(summary["proposals"]))
        return True


def _iso(at: Any) -> str | None:
    return at.isoformat() if isinstance(at, datetime) else None
