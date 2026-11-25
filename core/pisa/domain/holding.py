"""Holding phrases: what Pisa says at once when its answer isn't ready ~700 ms after the user's
turn arrived, so the first audio never waits for the model. Pure rules, no I/O.

The list is `protocol/holding-phrases.v1.json` (mirrored here; a test keeps the two equal). The app has each
phrase synthesised in its cache and plays it from `holding_id`; the answer follows in the same stream.
- A turn that looks something up (a tool is running) takes a `lookup` phrase ("Ci guardo subito."); any other
  an `any` one ("Vediamo…").
- Never the same phrase twice in a row for a device: people notice a repeated filler.
"""

from dataclasses import dataclass
from typing import Literal

Use = Literal["any", "lookup", "slow"]
# faster than any first token; the model's words win when they come first.
HOLD_AFTER_S = 0.7  # no first sentence this long after the utterance arrived: a holding phrase
PROGRESS_AFTER_S = 6.0  # long work: a line naming the step


@dataclass(frozen=True)
class Phrase:
    id: str
    lang: str
    text: str
    use: Use


PHRASES: tuple[Phrase, ...] = (
    Phrase("it.vediamo", "it-IT", "Vediamo…", "any"),
    Phrase("it.allora", "it-IT", "Allora…", "any"),
    Phrase("it.dunque", "it-IT", "Dunque…", "any"),
    Phrase("it.un_attimo", "it-IT", "Un attimo…", "lookup"),
    Phrase("it.ci_guardo", "it-IT", "Ci guardo subito.", "lookup"),
    Phrase("it.controllo", "it-IT", "Controllo un attimo.", "lookup"),
    Phrase("en.lets_see", "en-US", "Let's see…", "any"),
    Phrase("en.okay_so", "en-US", "Okay, so…", "any"),
    Phrase("en.right", "en-US", "Right…", "any"),
    Phrase("en.one_moment", "en-US", "One moment…", "lookup"),
    Phrase("en.let_me_check", "en-US", "Let me check.", "lookup"),
    Phrase("en.checking_now", "en-US", "Checking now.", "lookup"),
    # past the turn budget, still nothing said (`domain.latency.TURN_BUDGET_S`)
    Phrase("it.ancora_un_momento", "it-IT", "Ancora un momento…", "slow"),
    Phrase("en.bear_with_me", "en-US", "Bear with me…", "slow"),
)


def pick(lang: str, use: Use, last: str | None = None) -> Phrase:
    """The next holding phrase for this language and kind of turn, never `last` (the device's last one)."""
    fits = [p for p in PHRASES if p.lang == lang and p.use == use] or [
        p for p in PHRASES if p.lang == "en-US" and p.use == use
    ]
    ids = [p.id for p in fits]
    start = (ids.index(last) + 1) % len(fits) if last in ids else 0
    return fits[start]


# The step a long turn is on, said once after PROGRESS_AFTER_S ("a progress line naming the step").
_STEPS: dict[str, dict[str, str]] = {
    "it-IT": {
        "web_search": "Sto cercando sul web.",
        "read_page": "Sto leggendo una pagina.",
        "map_search": "Sto cercando sulla mappa.",
        "place_details": "Sto leggendo le recensioni.",
        "open_at": "Sto controllando gli orari.",
        "travel_time": "Sto calcolando il tragitto.",
        "walk_times": "Sto calcolando quanto sono lontani.",
        "detour": "Sto calcolando la deviazione.",
        "plan_options": "Sto rifacendo il piano.",
        "replan": "Sto rifacendo il piano.",
        "plan_stop": "Sto rifacendo il piano.",
        "search_my_email": "Sto cercando nella posta.",
        "get_weather": "Sto guardando il meteo.",
        "draft_reply": "Sto scrivendo la risposta.",
        "": "Ci sto ancora lavorando.",
    },
    "en-US": {
        "web_search": "I'm searching the web.",
        "read_page": "I'm reading a page.",
        "map_search": "I'm searching the map.",
        "place_details": "I'm reading the reviews.",
        "open_at": "I'm checking the hours.",
        "travel_time": "I'm working out the route.",
        "walk_times": "I'm working out how far they are.",
        "detour": "I'm working out the detour.",
        "plan_options": "I'm redoing the plan.",
        "replan": "I'm redoing the plan.",
        "plan_stop": "I'm redoing the plan.",
        "search_my_email": "I'm searching your email.",
        "get_weather": "I'm checking the weather.",
        "draft_reply": "I'm writing the reply.",
        "": "I'm still on it.",
    },
}


def progress_line(lang: str, tool: str | None) -> str:
    """ "Sto leggendo le recensioni.": the step a long turn is on (the tool running, or the last one)."""
    steps = _STEPS.get(lang, _STEPS["en-US"])
    return steps.get(tool or "", steps[""])


# past the turn budget (`domain.latency.TURN_BUDGET_S`) with
# nothing said yet, Pisa says it is taking long, once, as a holding phrase (`use` "slow": the app plays it
# from its cache; it is not the answer's first audio, nor in the reply's text). The turn goes on; its answer
# follows.
# Never a claim: nothing in it says a change was made or not made.


def slow_phrase(lang: str) -> Phrase:
    """What Pisa says when the turn passes its budget in silence (the turn is not cut)."""
    return pick(lang, "slow")
