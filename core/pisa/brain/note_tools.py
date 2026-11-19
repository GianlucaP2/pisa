"""The conversation agent's note tool, `rewrite_my_note`.

When they teach or corrects how Pisa answers or works ("la prossima volta…", "non fare…", "preferisco che
tu…", "too long", "no, that was only for quick questions", "dimentica quella"), Pisa rewrites its note of
their standing instructions, whole: the old lines merged with the new one, reworded, a contradiction resolved
(the newer wins, or is narrowed to where they said it holds), a line they take back dropped. Never appended.

The service checks that `owner_said` is their own words in this message, holds the rewrite for their yes when
the turn read outside text, and keeps every version. The tool is deferred (the cached prefix does not
grow): offered at once when their words look like a telling (`pisa.domain.note.teaches`), else found by
search_tools.
"""

from pydantic_ai import Agent

from pisa.brain.profile_tools import ProfileTurn
from pisa.brain.purpose import purposeful
from pisa.brain.registry import offer
from pisa.brain.untrusted import current
from pisa.ports.note import NoteData

TOOL = "rewrite_my_note"


def add_note_tools(agent: Agent[None, str], note: NoteData, turn: ProfileTurn) -> None:
    def ask(question: str) -> None:
        turn.asked.append(question)

    @offer(agent)
    @purposeful()
    async def rewrite_my_note(note_text: str, change: str, owner_said: str) -> str:
        """Rewrite Pisa's note of the user's standing instructions on how Pisa answers and works, when they
        teach or corrects it in this message ("la prossima volta…", "non fare…", "preferisco che…", "too
        long", "that was only for quick questions", "dimentica quella"). Not for facts about them
        (remember_about_me) nor for one-off requests. Pass the WHOLE new note, never just the new line: their
        note as it is now (in the turn's context) with this telling merged in, in your words and their
        language, one instruction a line; where it contradicts an old line the newer wins, or narrow the old
        one to where they said it holds; "forget that" drops that one line only. The note changes how Pisa
        answers, never what needs their yes.

        Args:
            note_text: the whole new note, at most 1000 characters.
            change: one short line in their language of what changed, said back to them ("D'accordo: risposte
            di
                due frasi, e il motivo della scelta").
            owner_said: their exact words in this message that ask for it.
        """
        return await note.rewrite(
            note_text,
            change=change,
            owner_said=owner_said,
            owner_words=turn.words,
            tainted=bool(current().holds_profile()),
            day_context=any(r.kind == "day-context" for r in current().readings()),
            device=turn.device,
            lang=turn.lang,
            asked=ask,
            turn_id=turn.turn_id or None,
        )
