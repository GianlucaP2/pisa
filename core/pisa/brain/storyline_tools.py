"""The conversation agent's tool for the user's own word on a storyline: "ho già spedito", "l'ho pagato",
"risolto" (LIFE-GRAPH section 7: chat episodes).

It moves the storyline the way a mail would (`services/storyline_chat`), inside the normal turn: no extra
model call. Code decides what it accepts:
- the quote must be the user's own words of THIS turn and must say the event (`domain/chat_events`), so a
  sentence in an email never moves a storyline;
- after outside text (this turn's reads, or the history's), no word of the storyline's key or the quote may be
  one only the outside text holds (`owner_scope.outside_only`): the user's words must carry it;
- a closing the user's words don't name stays a proposal, and more than one candidate asks which.
"""

from typing import Any, Literal

from pydantic_ai import Agent

from pisa.brain import owner_scope
from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.registry import offer
from pisa.brain.untrusted import current
from pisa.domain import decisions

Kind = Literal["shipped", "paid", "acknowledged", "cancelled", "attended", "owner_replied", "undo"]
NOT_THERE = "Not recorded: storylines are not available now."


def add_storyline_tools(agent: Agent[None, str], data: Any, held: ProfileTurn) -> None:
    @offer(agent)
    async def storyline_progress(storyline: str, kind: Kind, quote: str) -> str:
        """The user says something happened in one of the storylines in the context ("ho già spedito", "l'ho
        pagato", "risolto", "I already paid"). It moves that storyline like a mail would, and they can take it
        back. Only their own words this turn count, never an email's.

        Args:
            storyline: the ref in its card (s3f9a1c), or its exact key.
            kind: shipped, paid, acknowledged (done, settled), cancelled, attended, owner_replied, or undo
                (they take it back).
            quote: their words that say it, copied exactly.
        """
        record = getattr(data, "storyline_progress", None)
        if record is None:
            return NOT_THERE
        words = decisions.owner_words()[0] or held.words
        if not words.strip():
            return "Not recorded: only the user's own words move a storyline. Ask them."
        if current().outside():  # their words must carry it: no key or quote word only outside has
            ask = owner_scope.Ask(words, held.recent, held.offered)
            found = owner_scope.outside_only([storyline, quote], ask, await owner_scope.owner_items(data))
            if found:
                return owner_scope.refusal("Not recorded", found)
        said = await record(storyline, kind, quote, words, held.turn_id or "turn")
        if not said.startswith("Not "):
            decisions.note_if_collecting("act", "storyline_progress", "owner_words", ref=storyline)
        return said
