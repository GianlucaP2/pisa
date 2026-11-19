"""The wave's one new read tool, `stanotte(query)`, deferred behind
search_tools (never in the first request). It returns Pisa's own night records as data with ids: no page,
delta, quote, window title or contact name (the service leaves every third-party text out), and nothing it
returns is an instruction."""

from collections.abc import Callable

from pydantic_ai import Agent

from pisa.brain.registry import offer
from pisa.brain.untrusted import data_block
from pisa.ports.night import NightRecords

LIMIT = 40  # records a call returns; "Dammi tutto" shows the whole drawer on their phone


def add_night_tools(agent: Agent[None, str], night: NightRecords, lang: Callable[[], str]) -> None:
    @offer(agent)
    async def stanotte(query: str = "") -> str:
        """What Pisa did last night and what its night holds (stanotte, la notte): its ideas with their
        evidence grade, the dead ones and why, its open questions, the scan, each with its id. Only Pisa's own
        records.

        Args:
            query: words to match ("fatture", "idee morte"); empty for everything.
        """
        found = await night.find(query, lang())
        if found is None:
            return "The night's records are off (the morning sheet's gate is shut). Say so in one line."
        records = found["records"][:LIMIT]
        lines = [f"Last night: {found['night']}" if found["night"] else "Last night: no night ran."]
        lines += [f"- [{r['kind']} {r['id']}] {r['text']}" for r in records]
        if len(found["records"]) > LIMIT:
            lines.append(f"(+{len(found['records']) - LIMIT} more: narrow the query)")
        block = data_block("night", "\n".join(lines), what="Pisa's own night records, no outside text")
        return block.render()
