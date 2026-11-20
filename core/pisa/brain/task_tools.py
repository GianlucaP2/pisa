"""The conversation agent's tools for full control of tasks: rename, reschedule, notes, priority,
the low priority list, place, repeat, dismiss, restore, and delete (done at once on the user's words,
restorable for 24 hours).

Every tool finds the task from the user's words; when several match, it asks which (never guesses). Titles
come from email too: results are data blocks.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic_ai import Agent

from pisa.brain.follow_tools import Change, Gate, change_words
from pisa.brain.purpose import purposeful
from pisa.brain.registry import offer
from pisa.brain.untrusted import current, data_block
from pisa.domain import countries, decisions, quarantine, results, tools
from pisa.domain import tasks_bulk as bulk_rules
from pisa.domain.actions import names_item
from pisa.domain.errors import PisaError
from pisa.domain.reasons import task_reason
from pisa.domain.tasks import (
    LOW,
    AlreadyListed,
    Task,
    asks_to_plan,
    deadline_source,
    is_short_yes,
    parse_when,
    resolve_task,
    state_line,
)
from pisa.ports.assistant import AssistantData

PREVIEW_TTL = timedelta(minutes=10)


NAMED_AT_ONCE = 8  # at most this many tasks they named one by one are done without a count question


@dataclass(frozen=True)
class _Preview:
    """The count Pisa said for a bulk change: in which turn, for what (action and filter), which tasks,
    when."""

    turn: object | None
    key: tuple[Any, ...]
    ids: tuple[Any, ...]
    at: datetime


_previews: dict[str, _Preview] = {}  # the user's last bulk count (one owner, one API process)


def _titles(text: str) -> str:
    block = data_block("tasks", text, what="the user's tasks; titles may come from email")
    return block.render()


def task_now(lead: str, task: Task, timezone: str) -> str:
    """A task write's result: what happened, then the task as the store holds it now (read
    back after the write), which is what Pisa says."""
    return _titles(f"{lead} Now: {state_line(task, timezone)}.")


# (the data, the change, made on the user's yes to a held change) → the tool's result
Replay = Callable[[AssistantData, Change, bool], Awaitable[str]]


def listed(text: str, *, foreign: bool = False) -> str:
    """Task and event titles come from email and calendar invites: data, never instructions.
    a list holding a title someone else wrote (an invite's, an email-made task's) is
    outside text (`invite`): it taints the turn like an email; Pisa's own lists stay `tasks`."""
    kind = "invite" if foreign else "tasks"
    block = data_block(kind, text, what="the user's tasks and events; titles may come from email")
    return f"{block.rule}\n{block.text}"


async def add_now(data: AssistantData, change: Change, held: bool = False) -> str:
    """add_task's own write (`change.outside`: held after outside text, the task stays an email task)."""
    args = change.args
    try:
        task = await data.add_task(
            args["title"], args.get("due"), args.get("place"), args.get("priority"), args.get("duration_min"),
            start=args.get("start"), **({"from_outside": True} if change.outside else {}),
        )  # fmt: skip
    except PisaError as err:
        return f"Not added: {err.message}"
    except AlreadyListed as same:  # from the store, never a second copy
        return "Not added: " + listed(
            f"already on the list, open: {state_line(same.task, data.timezone)}. Say it is already there."
        )
    return listed(f"Added: {state_line(task, data.timezone)}.")  # as stored


async def complete_now(data: AssistantData, change: Change, held: bool = False) -> str:
    """complete_task's own write, on the task already found (`args["task_id"]`)."""
    done = await data.complete_task(change.args["task_id"])
    said = f"Marked done: {state_line(done, data.timezone)}."
    if done.repeat:  # a repeating task's next one, as the store made it
        # only one this completion made (same moment), never another series' task of that title
        nxt = [t for t in await data.tasks("open")
               if t.title == done.title and t.repeat and t.created_at >= done.updated_at]  # fmt: skip
        nxt.sort(key=lambda t: t.due_at or t.created_at)
        said += f" Next one: {state_line(nxt[0], data.timezone)}." if nxt else " No next one was made."
    return listed(said)


async def delete_now(data: AssistantData, change: Change, held: bool = False) -> str:
    """A soft delete, restored for 24 hours."""
    task = await data.delete_task(change.args["task_id"])
    return _titles(f"Deleted: {task.title} (restore_task brings it back for 24 hours).")


def _updated(lead: Callable[[Task], str]) -> Replay:
    """A task change on the task already found (`args["task_id"]`); `held`: made after a held change's yes,
    the idea came from outside text, so it's no label of the user's. After an email or a page in the same
    turn these wait for the user's yes like the plan tools (a "lower it" read in an email would make a task
    vanish into low priority)."""

    async def run(data: AssistantData, change: Change, held: bool = False) -> str:
        changes: dict[str, Any] = {k: v for k, v in change.args.items() if k not in ("task_id", "when")}
        try:
            task = await data.update_task(change.args["task_id"], changes, label=not held)
        except PisaError as err:
            return f"Not changed: {err.message}"
        return task_now(lead(task), task, data.timezone)

    return run


def _started(task: Task) -> str:
    return (
        "No time to do it now (it stays in the pool)."
        if task.expected_from is None
        else "A block in the plan."
    )


# each held task change's own write, attached to its registry entry (`pisa.brain.replay`)
REPLAYS: dict[str, Replay] = {
    "add_task": add_now,
    "complete_task": complete_now,
    "delete_task": delete_now,
    "dismiss_task": _updated(lambda _: "Dismissed."),
    "set_task_priority": _updated(lambda _: "Priority set."),
    "set_task_start": _updated(_started),
    "reschedule_task": _updated(lambda t: "No due date now." if t.due_at is None else "Deadline set."),
}


def _bulk_said(asked: list[Task], done: dict[str, Any], lead: str, then: str) -> str:
    """A bulk change's result from what the store changed: a partial one says which tasks did
    NOT change (closed or gone meanwhile), and none changed is no "done"."""
    changed = set(done.get("changed") or ())
    left = [t.title for t in asked if t.id not in changed]
    if not changed:
        return _titles(f"Not changed: none of them is open any more ({'; '.join(left[:5])}). Say so.")
    undo = "One Undo (tasks_bulk action undo) brings them all back for 24 hours."
    if not left:
        return f"{lead} {undo} {then}"
    return _titles(f"{lead} NOT changed ({len(left)}, no longer open): {'; '.join(left[:5])}. {undo} {then}")


GIST_LINE_MAX = 160  # a gist's line as the planner sees it


def add_task_control_tools(
    agent: Agent[None, str],
    data: AssistantData,
    gate: Gate | None,
    lang: Callable[[], str],
    words: Callable[[], str] = lambda: "",
) -> None:
    async def gated(task: Task, changes: dict[str, Any], words: dict[str, str]) -> str:
        """The running tool's change to one task: held by the gate, or made."""
        change = Change.here({"task_id": task.id, **changes}, title=task.title)
        key = tools.REGISTRY[change.tool].grounding or change.tool  # its words are its grounding rule's

        async def describe() -> str:
            return change_words(lang(), key, title=f"«{task.title}»", **words)

        # the task's title grounds the change in their words (`grounded_change`)
        if gate is not None and (held := await gate(change, describe)):
            return held
        return await REPLAYS[change.tool](data, change, False)

    async def one(task_ref: str, status: str = "open") -> Task | str:
        """The task the user means, or what to say instead."""
        match = resolve_task(task_ref, await data.tasks(status))
        if match.task is not None:
            return match.task
        if match.candidates:
            names = "; ".join(t.title for t in match.candidates)
            return _titles(f"Several tasks match: {names}. Ask the user which one; don't guess.")
        which = "open " if status == "open" else ""
        return _titles(f"No {which}task matches {task_ref!r}. Ask the user which task they mean.")

    async def change(task_ref: str, changes: dict[str, Any], lead: str, status: str = "open") -> str:
        found = await one(task_ref, status)
        if isinstance(found, str):
            return found
        try:
            task = await data.update_task(found.id, changes)
        except PisaError as err:
            return f"Not changed: {err.message}"
        return task_now(lead, task, data.timezone)

    @offer(agent)
    @purposeful()
    async def tasks_bulk(
        action: Literal["done", "dismiss", "delete", "undo"],
        filter: Literal[
            "all", "overdue", "low_priority", "from_sender", "older_than", "source", "ids", "named"
        ] = "all",
        days: int | None = None,
        source: Literal["voice", "email", "manual", "calendar"] | None = None,
        sender: str | None = None,
        task_ids: list[str] | None = None,
        confirm_count: int | None = None,
        titles: list[str] | None = None,
    ) -> str:
        """Change many open tasks at once ("togli tutti i task scaduti", "segna fatti quelli di Amazon",
        "cancella tutto"), or undo the last such change (action undo). Items they named one by one ("latte,
        pane e uova presi"): action done, filter named, their titles: done at once. A group: first call
        without confirm_count: Pisa
        gets the count; say it and ask once ("Sono 23: li tolgo tutti?"). On the user's yes, call again with
        the same filter and confirm_count = that count. One Undo restores the whole set for 24 hours.

        Args:
            action: done, dismiss, delete, or undo (the last bulk change back).
            filter: all, overdue, low_priority, from_sender (sender), older_than (days), source (voice, email,
                manual or calendar: the reminders of their people's dates), ids (task_ids), named (titles).
            days: for older_than: created more than this many days ago.
            source: for source.
            sender: for from_sender: the sender's name or address, as the user said it.
            task_ids: for ids.
            confirm_count: the count you said, after the user's yes.
            titles: for named: each task as they named it ("latte", "pane", "uova"); done at once.
        """
        if action == "undo":
            try:
                undone = await data.tasks_bulk_undo()
            except PisaError as err:
                return f"Not changed: {err.message}"
            return f"Restored {undone['count']} tasks. Say it in one sentence."
        if filter == "named":  # the items they named, each found by their words
            open_tasks = await data.tasks("open")
            matches = [resolve_task(t, open_tasks) for t in titles or []]
            if not matches or any(m.task is None for m in matches):
                missing = [t for t, m in zip(titles or [], matches, strict=True) if m.task is None]
                return _titles(f"Not changed: no single open task matches {missing or titles}. Ask which.")
            found = list({m.task.id: m.task for m in matches if m.task is not None}.values())
            filter, task_ids = "ids", [t.id for t in found]
        else:
            found = await data.tasks_bulk_match(filter, days=days, source=source, ids=task_ids, sender=sender)
        if not found:
            return "No open task matches that filter: nothing to change."
        key = (action, filter, days, source, sender, tuple(task_ids or ()))
        ids = tuple(t.id for t in found)
        # the items they named one by one this turn ("latte, pane e uova presi") are
        # done at once, as mark_done would do each: every title in their own words, no outside text, a few;
        # one Undo brings them back. A group ("tutti", "quelli di Amazon") still says its count and asks once.
        words = decisions.owner_words()[0]
        if (
            confirm_count is None and action == "done" and filter == "ids" and len(found) <= NAMED_AT_ONCE
            and not current().outside() and all(names_item(t.title, words) for t in found)
        ):  # fmt: skip
            done = await data.tasks_bulk_apply(list(ids), action)
            return _bulk_said(
                found, done, f"Done: {done['count']} tasks they named (done).",
                "Say it in one sentence, with what is left if anything.",
            )  # fmt: skip
        if confirm_count is None:
            _previews["bulk"] = _Preview(decisions.turn(), key, ids, data.now())
            sample = "; ".join(t.title for t in found[:3])
            ask = bulk_rules.count_text(len(found), action, lang())
            return _titles(
                f"{len(found)} open tasks match (e.g. {sample}). Ask once, with the count: «{ask}». On the "
                f"user's yes, call tasks_bulk again with the same filter and confirm_count={len(found)}."
            )
        # RAIL: one yes from the user, in a later message than the count
        # Pisa said, in a turn that read no outside text, for that action and filter, only on the tasks
        # counted.
        if not is_short_yes(decisions.owner_words()[0]):
            return "Not changed: the user hasn't said yes to the count. Ask once, with the count."
        if current().outside():
            return "Not changed: this turn read an email or a page. Say the count again and ask once."
        preview = _previews.get("bulk")
        if preview is None or preview.key != key or data.now() - preview.at > PREVIEW_TTL:
            return (
                "Not changed: Pisa hasn't said this count. Call tasks_bulk without "
                "confirm_count and ask once."
            )
        if preview.turn is decisions.turn():
            return "Not changed: the user's yes comes in the next message. Ask once, with the count."
        if set(ids) - set(preview.ids):
            _previews["bulk"] = _Preview(decisions.turn(), key, ids, data.now())
            now_n, said_n = len(found), len(preview.ids)
            return f"Not changed: now {now_n} match, not {said_n}. Say the new count and ask again."
        acting = [i for i in preview.ids if i in set(ids)]  # what Pisa counted and is still open
        if confirm_count < len(acting):
            return (
                f"Not changed: {len(acting)} match, more than {confirm_count}. Say the count and ask again."
            )
        del _previews["bulk"]
        done = await data.tasks_bulk_apply(acting, action)
        counted = [t for t in found if t.id in set(acting)]
        lead = f"Done: {done['count']} tasks ({action})."
        if gone := len(preview.ids) - len(acting):  # counted, then closed before their yes
            lead += f" NOT changed ({gone} of the {len(preview.ids)} counted, no longer open)."
        return _bulk_said(counted, done, lead, "Say it in one sentence.")

    @offer(agent)
    async def thing(ref: str) -> str:
        """An order, a booking or a trip as one thing ("a che punto è l'ordine Zalando?", "il volo di
        venerdì"): its emails (newest first, as their gist), its task and its events, linked by the same
        thread, an order or booking number, or a date and a place. Say where it stands in a sentence ("spedito
        ieri, arriva venerdì").

        Args:
            ref: an order, booking or tracking number, or the user's words for it ("Zalando", "il volo").
        """
        found = await data.thing(ref)
        if found is None:
            return "No thing matches that: try the task or the email."
        lines = [f"Thing: {found['title']}"]
        for e in found["emails"]:
            gist = e.get("gist") or {}
            # each gist's values registered, its mail named as their source
            said = quarantine.gist_summary(gist) or ""
            source = quarantine.plain_source(e.get("from_name"), e.get("from_address"))
            current().note_fields(
                quarantine.read("email", "", source=source, summary=said, today=data.today()), lang()
            )
            what = f" — {quarantine.cut(gist['what'], GIST_LINE_MAX)}" if gist.get("what") else ""
            when = f" (when: {quarantine.cut(gist['when'], GIST_LINE_MAX)})" if gist.get("when") else ""
            lines.append(f"Email {e['when']}: {e['subject']}{what}{when}")
        lines += [
            f"Task: {t['title']} ({t['status']}{', due ' + t['due'] if t['due'] else ''})"
            for t in found["tasks"]
        ]
        lines += [
            f"Event: {e['title']} {e['start'] or ''} {e['location'] or ''}".rstrip() for e in found["events"]
        ]
        # RAIL: the emails' words are data.
        block = data_block("email", "\n".join(lines), what="a thing: its emails' gists, tasks and events")
        return block.render()

    @offer(agent)
    @purposeful()
    async def rename_task(task_ref: str, title: str) -> str:
        """Give an open task a new title ("chiamalo 'Ritirare il pacco'").

        Args:
            task_ref: the user's words for the task.
            title: the new title, short, in the user's language.
        """
        return await change(task_ref, {"title": title}, "Renamed.")

    @offer(agent)
    @purposeful()
    async def plan_task(task_ref: str) -> str:
        """Fit an open task into today's plan at a time Pisa picks, ONLY when the user asks ("mettilo oggi
        quando posso", "trovami un momento per…"). Pisa never times a task by itself: tasks stay in the
        pool; when the user is free you may only suggest one.

        Args:
            task_ref: the user's words for the task.
        """
        found = await one(task_ref)
        if isinstance(found, str):
            return found
        if not asks_to_plan(decisions.owner_words()[0], found.title):
            return "Not planned: only the user's own request puts a task at a time. Suggest it instead."
        plan = await data.plan_task(found.id, found.title)
        item = next((i for i in plan.get("items", []) if i.get("task_id") == found.id), None)
        if item is None or not item.get("start"):
            return _titles(f"{found.title}: it doesn't fit today (no free time that works). Say so.")
        start = datetime.fromisoformat(str(item["start"])).astimezone(ZoneInfo(data.timezone))
        return _titles(f"Planned: {found.title} at {start:%H:%M} today.")

    @offer(agent)
    @purposeful()
    async def set_task_deadline(task_ref: str, deadline: str, why: str) -> str:
        """Set the deadline Pisa found for an open task: an exhibition's last day from the page you read,
        a renewal date from the emails (set it a few days before), the user's own words. Code records where
        it came from. A deadline, never a time to do it: the task stays in the pool.

        Args:
            task_ref: the user's words for the task, or its title.
            deadline: YYYY-MM-DD.
            why: one line: what the deadline is ("la mostra chiude il 9 novembre").
        """
        try:
            due, kind = parse_when(deadline, data.today())
        except PisaError as err:  # its words open as not done (receipts read the first word)
            return f"Not set: {err.message}"
        if due is None:
            return "Not set: give the deadline as YYYY-MM-DD."
        found = await one(task_ref)
        if isinstance(found, str):
            return found
        source = deadline_source(
            [r.kind for r in current().readings()], decisions.owner_words()[0], due, data.today()
        )
        # the user's deadline changes only on their own words this turn, and their time is
        # never turned into a date
        his = found.due_at is not None and found.deadline_source in (None, "owner")
        if his and source != "owner":
            return _titles(
                f"Not set: {found.title}'s deadline is the user's own. Only their words change it: tell them "
                "what you found and ask."
            )
        if his and found.due_kind == "datetime" and kind == "date":
            return _titles(f"Not set: the user gave {found.title} a time; a date would lose it. Ask them.")
        changes = {"due_at": due, "due_kind": kind, "deadline_source": source}
        try:
            task = await data.update_task(found.id, changes, label=False)
        except PisaError as err:
            return f"Not set: {err.message}"
        return task_now(f"Deadline set (from {source}); the task stays in the pool.", task, data.timezone)

    @offer(agent)
    @purposeful()
    async def set_task_start(task_ref: str, when: str) -> str:
        """When to DO an open task ("spostalo alle 15", "il notaio lo chiamo domani alle 10"): a block at that
        time in the plan. Never its deadline ("entro le 19" is reschedule_task). "none" clears it.

        Args:
            task_ref: the user's words for the task.
            when: today, tomorrow or a weekday with HH:MM, YYYY-MM-DD HH:MM, or none.
        """
        found = await one(task_ref)
        if isinstance(found, str):
            return found
        if when.strip().casefold() in ("none", "null", ""):
            return await gated(found, {"start_at": None}, {"when": "none"})
        try:
            start, kind = parse_when(when, data.today())
        except PisaError as err:
            return f"Not set: {err.message}"
        if start is None or kind != "datetime":
            return "Not set: a start needs a time (HH:MM) as well as the day."
        return await gated(found, {"start_at": start}, {"when": when})

    @offer(agent)
    @purposeful()
    async def reschedule_task(task_ref: str, when: str) -> str:
        """Move an open task's deadline ("spostalo a domani", "entro venerdì alle 10"), or clear it ("none").
        A time here is a deadline the task must be done by, never a time in the plan.

        Args:
            task_ref: the user's words for the task.
            when: today, tomorrow, a weekday, YYYY-MM-DD, any of them with HH:MM, or none.
        """
        try:
            due, kind = parse_when(when, data.today())
        except PisaError as err:
            return f"Not changed: {err.message}"
        found = await one(task_ref)
        if isinstance(found, str):
            return found
        changes = {"due_at": due, "due_kind": kind}
        return await gated(found, changes, {"when": when})

    @offer(agent)
    @purposeful()
    async def set_task_notes(task_ref: str, notes: str | None) -> str:
        """Write (or clear) an open task's notes ("aggiungi una nota: il codice è nella mail").

        Args:
            task_ref: the user's words for the task.
            notes: the whole note text; null to clear it.
        """
        return await change(task_ref, {"notes": notes}, "Notes saved." if notes else "Notes cleared.")

    @offer(agent)
    @purposeful()
    async def set_task_priority(task_ref: str, priority: Literal[1, 2, 3]) -> str:
        """Set an open task's priority ("è urgente" = 1).

        Args:
            task_ref: the user's words for the task.
            priority: 1 high, 2 normal, 3 low.
        """
        found = await one(task_ref)
        if isinstance(found, str):
            return found
        return await gated(found, {"priority": priority}, {"priority": str(priority)})

    @offer(agent)
    @purposeful()
    async def set_task_place(task_ref: str, place: str | None) -> str:
        """Set where an open task happens ("il pacco è alla posta di via Roma"), or clear it.

        Args:
            task_ref: the user's words for the task.
            place: a place name or address, as the user said it; null to clear it.
        """
        lead = "Place saved; Pisa finds it on the map." if place else "Place cleared."
        return await change(task_ref, {"place_text": place}, lead)

    @offer(agent)
    @purposeful()
    async def set_task_country(task_ref: str, country: str | None) -> str:
        """Bind an open task to the one country where it can be done ("questo solo in Italia", "da fare
        quando torno a New York"), or free it (null: "si può fare ovunque"). On landing Pisa lists them;
        before leaving they go in the checklist.

        Args:
            task_ref: the user's words for the task.
            country: the country, two letters (IT, US, DE...); null when it can be done anywhere.
        """
        country = country.strip().upper() if country else None
        if country is not None and not countries.is_code(country):
            return "Not changed: the country is its two-letter code (IT, US, DE...)."
        # RAIL: their own words name it, never outside text
        if not countries.named_in_words(words(), country):
            return (
                "Not changed: only their own words this turn naming the country bind a task to it. Ask them."
            )
        lead = "Country set." if country else "Freed: anywhere."
        return await change(task_ref, {"country": country}, lead)

    @offer(agent)
    @purposeful()
    async def set_task_repeat(
        task_ref: str, every: Literal["day", "week", "month", "none"], interval: int = 1
    ) -> str:
        """Make an open task repeat ("ogni settimana", "ogni due mesi"), or stop it repeating ("none").
        Completing it then makes the next one.

        Args:
            task_ref: the user's words for the task.
            every: day, week, month, or none to stop.
            interval: every how many days, weeks or months (1-12).
        """
        repeat = None if every == "none" else {"every": every, "interval": interval}
        return await change(task_ref, {"repeat": repeat}, "Repeat set." if repeat else "No longer repeats.")

    @offer(agent)
    async def low_priority_list() -> str:
        """What's in low priority ("cosa c'è in bassa priorità?"): the low tasks, never planned or
        reminded, and the emails that became no task. Read them briefly; the user may raise one
        (set_task_priority 2) or dismiss one (dismiss_task)."""
        low = [t for t in await data.tasks("open") if t.priority == LOW]
        emails = await data.low_emails()
        if not low and not emails:
            return "Nothing in low priority."
        lines = [f"Low tasks ({len(low)}):"] + [
            f"- {t.title} ({task_reason(t, lang()) or 'low'})" for t in low[:10]
        ]
        if len(low) > 10:
            lines.append(f"…and {len(low) - 10} more in the app.")
        if emails:
            lines.append(f"Emails that are no task ({len(emails)} newest):")
            lines += [f"- {e['subject']} · {e['sender']} ({e['why']})" for e in emails]
        return _titles("\n".join(lines))

    @offer(agent)
    @purposeful()
    async def dismiss_task(task_ref: str) -> str:
        """Drop an open task without doing it ("lascia perdere"). restore_task brings it back. A repeating
        task's next one isn't made.

        Args:
            task_ref: the user's words for the task.
        """
        found = await one(task_ref)
        if isinstance(found, str):
            return found
        return await gated(found, {"status": "dismissed"}, {})

    @offer(agent)
    @purposeful()
    async def restore_task(task_ref: str | None = None) -> str:
        """Bring back a task that was done or dismissed: the last one ("rimettilo"), or by title.

        Args:
            task_ref: the user's words for the task; omit for the last one done or dismissed.
        """
        deleted = await data.deleted_tasks()  # deleted by voice, within 24 h
        gone = deleted + [t for t in await data.tasks("all") if t.status in ("done", "dismissed")]
        if not gone:
            return "No done, dismissed or deleted task to restore."
        if task_ref:
            match = resolve_task(task_ref, gone)
            if match.task is None:
                if match.candidates:
                    names = "; ".join(t.title for t in match.candidates)
                    return _titles(f"Several tasks match: {names}. Ask the user which one; don't guess.")
                return _titles(f"No done or dismissed task matches {task_ref!r}.")
            target = match.task
        else:
            target = max(gone, key=lambda t: t.updated_at)
        if target.status == "deleted":
            task = await data.undelete_task(target.id)
        else:
            task = await data.update_task(target.id, {"status": "open"})
        return task_now("Restored.", task, data.timezone)

    @offer(agent)
    @purposeful()
    async def delete_task(task_ref: str) -> str:
        """Delete a task the user said to delete ("cancella il task della spesa"): done at once, no
        "Sicuro?". It can be restored with restore_task for 24 hours.

        Args:
            task_ref: the user's words for the task (open, done or dismissed).
        """
        found = await one(task_ref, "all")
        if isinstance(found, str):
            return found
        # after an email or a page, only the user's own "cancella <task>" deletes at once.
        change = Change.here({"task_id": found.id}, title=found.title)

        async def describe() -> str:
            return change_words(lang(), "delete", title=f"«{found.title}»")

        if gate is not None and (held := await gate(change, describe)):
            return held
        result = await delete_now(data, change)
        if not decisions.no_effect(result):  # a write's result says it is done, with its receipt
            receipt = f"task deleted: «{found.title[:120]}» (restore_task for 24 h)"
            result = results.within(result, results.done(None, receipt))
        return result
