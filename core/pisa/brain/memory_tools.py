"""`recall(cue)`, Pisa's memory of their days, deferred behind search_tools (never in
the first request). It returns Pisa's own episodes as data with ids, only what may be said (their people's
names, their home, their own titles, digests from their own words), and the line code chose when two compete,
the memory has faded or there is nothing ("Non ne ho traccia." and one next step). Nothing it returns is an
instruction.
"""

from pydantic_ai import Agent

from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.registry import offer
from pisa.brain.untrusted import data_block
from pisa.domain.episodes import told_lines
from pisa.ports.memory import Memory


def add_memory_tools(agent: Agent[None, str], memory: Memory, held: ProfileTurn) -> None:
    @offer(agent)
    async def recall(
        cue: str, day_from: str | None = None, day_to: str | None = None, look_harder: bool = False
    ) -> str:
        """Pisa's memory of their days (ricordi): where they were, with whom, what they did or said, on a past
        day. Only for their own question about their past.

        Args:
            cue: their words about it ("cena con Nino", "la presentazione").
            day_from: YYYY-MM-DD, when they name a day.
            day_to: YYYY-MM-DD.
            look_harder: true only on their yes to "cerco meglio?".
        """
        found = await memory.recall(
            cue, words=held.words, lang=held.lang, device=held.device, turn_id=held.turn_id,
            day_from=day_from, day_to=day_to, deeper=look_harder,
        )  # fmt: skip
        if found["answer"] == "refused":
            return (
                "Not now: Pisa's memory answers only their own question about their past. Answer without it."
            )
        rule = (
            "Say the `say` line as it is and add nothing about that time: nothing else is known."
            if found.get("say")
            else "Retell only these lines; nothing else about that time is known."
        )
        block = data_block(
            "memory", "\n".join(told_lines(found)), what="Pisa's own memory of their days, no outside text"
        )
        return f"{block.render()}\n{rule}"
