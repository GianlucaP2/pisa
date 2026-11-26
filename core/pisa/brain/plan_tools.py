"""The conversation agent's plan tools (PL-13): get_plan, what_now, replan. Read-only, no approval.

The plan holds titles from the calendar and from email: every result is a nonce data block.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic_ai import Agent

from pisa.brain.purpose import purposeful
from pisa.brain.registry import offer
from pisa.brain.untrusted import data_block
from pisa.domain.errors import PisaError
from pisa.ports.assistant import AssistantData

FREE_FOR_A_TASK_MIN = 15  # what_now suggests a task only for free time at least this long
TODO_SAID = 5  # the day's to-do list's head, in both reads


def todo_line(plan: dict[str, Any]) -> str | None:
    """The day's to-do
    (tasks with no time, Pisa never times them by itself), most urgent first."""
    todo = [str(t.get("title") or "").strip() for t in plan.get("todo") or []]
    todo = [t for t in todo if t]
    if not todo:
        return None
    more = f"; and {len(todo) - TODO_SAID} more" if len(todo) > TODO_SAID else ""
    return "To do today (no time set, most urgent first): " + "; ".join(todo[:TODO_SAID]) + more + "."


def _block(text: str) -> str:
    block = data_block("plan", text, what="the user's plan for today")
    return block.render()


def _t(value: str, tz: ZoneInfo) -> str:
    return f"{datetime.fromisoformat(value).astimezone(tz):%H:%M}"


def _status(item: dict[str, Any]) -> str:
    """What happened to it and whether Pisa moved it, when there is anything to say."""
    words = [item["status"]] if item.get("status") not in (None, "planned") else []
    words += ["moved"] if item.get("adjusted") else []
    return f" [{', '.join(words)}]" if words else ""


def describe_plan(plan: dict[str, Any], tz: ZoneInfo) -> str:
    """The plan as plain lines: one per item (free time left out), then what didn't fit."""
    lines = []
    for item in plan.get("items") or []:
        kind = item["kind"]
        if kind == "free":
            continue
        span = f"{_t(item['start'], tz)}-{_t(item['end'], tz)}"
        if kind == "travel":
            lines.append(f"{_t(item['leave_at'], tz)} leave: {item['title']}")
        elif kind == "task":
            lines.append(f"{span} task: {item['title']} ({item.get('why') or ''}){_status(item)}")
        else:
            moved = f" ({item['why']})" if item.get("moved_from") else ""  # a soft event Pisa moved
            lines.append(f"{span} event: {item['title']}{moved}{_status(item)}")
    for u in plan.get("unplanned") or []:
        lines.append(f"Not today: {u['title']} ({u['reason']})")
    if (todo := todo_line(plan)) is not None:
        lines.append(todo)
    if not lines:
        lines.append("Nothing planned today: no timed events and no open tasks.")
    skipped = (plan.get("sources") or {}).get("skipped") or []
    if skipped:
        lines.append("Note: " + "; ".join(skipped))
    return "\n".join(lines)


def now_and_next(plan: dict[str, Any], now: datetime, tz: ZoneInfo) -> str:
    """The item happening now and the next one; in free time, a task that fits it (PL-13)."""
    items = list(plan.get("items") or [])
    current = next(
        (i for i in items if datetime.fromisoformat(i["start"]) <= now < datetime.fromisoformat(i["end"])),
        None,
    )
    upcoming = [i for i in items if i["kind"] != "free" and datetime.fromisoformat(i["start"]) > now]
    # "che ore sono qui?" then "e adesso?" was read as "what now?", answered from this
    # tool, and the time never said; the "now" tool says the time first, in their zone
    lines = [f"The time: {now.astimezone(tz):%A %H:%M} ({tz.key})."]
    if current is not None and current["kind"] != "free":
        lines.append(f"Now: {current.get('title')} until {_t(current['end'], tz)}.")
    if upcoming:
        nxt = upcoming[0]
        if nxt["kind"] == "travel":
            target = next((i for i in upcoming[1:] if i["kind"] in ("task", "event")), None)
            to = f" for {target['title']}" if target else ""
            lines.append(f"Next: leave at {_t(nxt['leave_at'], tz)}{to} ({nxt['title']}).")
        else:
            lines.append(f"Next: {nxt['title']} at {_t(nxt['start'], tz)}.")
    free_until = datetime.fromisoformat(upcoming[0]["start"]) if upcoming else None
    in_free = current is None or current["kind"] == "free"
    if in_free and free_until is not None:
        minutes = int((free_until - now).total_seconds() // 60)
        if minutes >= FREE_FOR_A_TASK_MIN:
            fit = next(
                (
                    i
                    for i in upcoming
                    if i["kind"] == "task"
                    and not i.get("place_id")
                    and (
                        datetime.fromisoformat(i["end"]) - datetime.fromisoformat(i["start"])
                    ).total_seconds()
                    / 60
                    <= minutes
                ),
                None,
            )
            free = f"Free for {minutes} min."
            lines.append(free + (f" A task that fits now: {fit['title']}." if fit else ""))
    if not any(i["kind"] == "task" for i in upcoming) and (todo := todo_line(plan)) is not None:
        lines.append(todo)
    if not lines:
        lines.append("Nothing else is planned today.")
    return "\n".join(lines)


def add_plan_tools(agent: Agent[None, str], data: AssistantData) -> None:
    tz = ZoneInfo(data.timezone)

    @offer(agent)
    async def get_plan() -> str:
        """Today's plan: events, the tasks Pisa placed in the free time (with why), when to leave, and what
        didn't fit."""
        try:
            return _block(describe_plan(await data.plan(), tz))
        except PisaError as err:
            return _block(f"No plan: {err.message}")

    @offer(agent)
    async def what_now() -> str:
        """What the user should do now: the item in progress, the next one, or a task that fits the free
        time ("Pisa, cosa faccio adesso?")."""
        try:
            return _block(now_and_next(await data.plan(), data.now(), tz))
        except PisaError as err:
            return _block(f"No plan: {err.message}")

    @offer(agent)
    @purposeful()
    async def replan() -> str:
        """Make today's plan again now (after a change the user mentions: running late, a task done)."""
        try:
            return _block(describe_plan(await data.plan(fresh=True), tz))
        except PisaError as err:
            return _block(f"No plan: {err.message}")
