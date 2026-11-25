"""A held change made on the user's yes: one dispatch by the tool's registry entry.

Each tool a gate may hold (`held` in `pisa.domain.tools`) has its own write attached here, the very function
its body calls when nothing holds it (`task_tools.REPLAYS`, `follow_tools.REPLAYS`), so the yes makes the
change exactly as the tool would have, with the held change's outside-text marks
(`tests/test_tool_registry.py` checks every held tool has one).
"""

from pisa.brain import follow_tools, task_tools
from pisa.brain.follow_tools import Change
from pisa.ports.assistant import AssistantData

REPLAYS: dict[str, task_tools.Replay] = {**task_tools.REPLAYS, **follow_tools.REPLAYS}


async def run_change(data: AssistantData, change: Change, *, held: bool = True) -> str:
    """A held change the user said yes to (`held`: the idea came from outside text, so it is no label of the
    user's): the tool's result. A tool with no replay raises (never a silent no-op)."""
    replay = REPLAYS.get(change.tool)
    if replay is None:
        raise ValueError(f"{change.tool}: no replay for a held change")
    return await replay(data, change, held)
