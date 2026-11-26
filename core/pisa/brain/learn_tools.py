"""The brain's Pisa Learn tools, on a turn about a course only (`learn.about_course`):
nothing joins any turn's fixed prompt or always-sent tools.

- `learn_ask`: a deep question about their sources, answered by the cited path (retrieval, the Citations API,
  the per-sentence judge, the three tiers); Pisa says the answer as it comes, tiers included;
- `learn_sources`: their sources and their state.
What comes back was written from their papers: outside text, handed over as a data block, so the turn
that read it can't act on it.
"""

from typing import Any

from pydantic_ai import Agent

from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.registry import offer
from pisa.brain.untrusted import block
from pisa.domain import results
from pisa.domain.errors import PisaError
from pisa.ports.learn import LearnData

# this turn's line, after the cached prefix, only on a course turn (never in the fixed part)
TURN_LINE = (
    "Pisa Learn: their questions about their course's papers and sources go to learn_ask (one question each, "
    "their "
    "words); say its answer as it is: its tiers (the sources say / my inference / none of my sources) and "
    "its "
    "citations, never more than it says. learn_sources lists their sources and their state."
)


def add_learn_tools(agent: Agent[None, str], learn: LearnData, turn: ProfileTurn) -> None:
    @agent.instructions
    def learn_line() -> str:
        return TURN_LINE

    @offer(agent)
    async def learn_ask(question: str) -> str:
        """Answer the user's question about their course's sources (papers they imported): a fact, a number, a
        table, why the authors made a design choice. Every sentence comes back checked and cited; say it as it
        is, tiers included ("le fonti dicono…", "(mia deduzione)", "nessuna delle mie fonti…").

        Args:
            question: their question, in their words.
        """
        try:
            found = await learn.ask(question)
        except PisaError as err:
            return results.error(f"learn_ask: {err.message}", err.action)
        return block(
            "learn-answer",
            _spoken(found),
            "Pisa Learn's checked answer, written from their papers' text: say it as it is",
        )

    @offer(agent)
    async def learn_sources() -> str:
        """The user's Pisa Learn sources (papers, pages, files) and the state of each: ready, still being
        read, needing their copy, or several papers matching a title for them to choose."""
        rows = await learn.sources()
        if not rows:
            return "No sources yet: they can add arXiv ids, DOIs, links or a PDF in the app."
        lines = [_source_line(r) for r in rows]
        return block("learn-sources", "\n".join(lines), "their sources' titles and states")


def _spoken(found: dict[str, Any]) -> str:
    meta = " (lexical search only: no embedding model is chosen)" if found.get("lexical_only") else ""
    return f"{found['answer']}{meta}"


def _source_line(r: dict[str, Any]) -> str:
    name = r.get("title") or r.get("ref")
    bits = [f"- {name}: {r.get('state')}"]
    if r.get("state") == "candidates":
        bits.append(f"({len(r.get('candidates') or [])} papers match: they choose in the app)")
    if not r.get("citable"):
        bits.append("(a study plan: a syllabus hint, never cited)")
    return " ".join(bits)
