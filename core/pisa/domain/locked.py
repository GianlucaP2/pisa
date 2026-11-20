"""While the phone says it is locked, the conversation is
read-only for anything sensitive. Someone holding the user's locked iPhone ("Ehi Siri, parla con Pisa") gets
Pisa's public help (the time, the weather, places, the web) and the day's agenda and tasks, never a write,
never a spoken yes to an approval, never an email's body, a contact, a document or money read aloud. Pisa says
"Sblocca il telefono per questo". Pure: no I/O.

An allowlist: a tool not named here is refused while locked (a new tool is safe until someone lists it).
"""

from datetime import datetime
from typing import Any

from pisa.domain import locale
from pisa.domain.body import fresh

LOCKED_OK = frozenset(
    {
        "finish", "get_current_time", "get_weather", "where_am_i", "stop_everything",
        "get_agenda", "get_tasks", "get_plan", "what_now", "low_priority_list",
        "map_search", "place_details", "open_at", "travel_time", "detour", "compare_places",
        "walk_times",  # travel_time for many places at once
        "web_search", "read_page",
        "search_tools",  # pydantic-ai's tool search: it only lists tools
    }
)  # fmt: skip
UNLOCK = {"it-IT": "Sblocca il telefono per questo.", "en-US": "Unlock your phone for this."}


def unlock_line(lang: str) -> str:
    return UNLOCK[locale.tag_of(lang)]


UNLOCKED_SEEN = "unlocked_seen"  # set in the stored state once the phone said `locked: false` this session


def is_locked(state: dict[str, Any] | None, now: datetime, *, companion: bool = False) -> bool:
    """The phone's own word, still fresh (the 10 minutes): `locked: true`; no `locked: false` since
    this session began (the user who unlocked it, then pocketed it to drive, keeps Pisa; someone who found it
    locked never unlocked it); companion mode off (the user's choice: the audit's own exception)."""
    now_state = fresh(state, now)
    if now_state.get("companion", companion) is True:
        return False
    return now_state.get("locked") is True and not (state or {}).get(UNLOCKED_SEEN)


def instruction(lang: str) -> str:
    """The turn's dynamic line while locked: what Pisa may do, and what it says for the rest."""
    return (
        "\nThe user's phone is LOCKED (whoever speaks may not be the user): only the time, the weather, "
        "places, the web and the day's agenda and tasks. No change of any kind, no yes to an approval, never "
        "an email, a contact, a document or money aloud. For anything else say exactly: "
        f'"{unlock_line(lang)}"'
    )
