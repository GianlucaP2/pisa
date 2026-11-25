"""Lighter turns. Every request carries
the tools most turns need, about 20 (the cached prefix stays the same from turn to turn); the rest are
deferred and found on demand through Pydantic AI's tool search (`ToolSearch`: the model calls `search_tools`
with what it needs, the matches join the turn's tools). The first real Grok turns read ~36k input tokens
each, most of it the 71 tool schemas, even "che ore sono?".

The search is plain keyword overlap over each deferred tool's name and description (Pydantic AI's own idea, on
public API only): the system prompt names every tool, so a name is always a match. No embeddings, no router.
"""

import dataclasses
import re
from collections.abc import Sequence
from typing import Any

from pydantic_ai.tools import ToolDefinition

from pisa.domain import connectors, experimental
from pisa.domain.tools import named

# FACT: what most turns need (the day, the basic tasks, the calendar actions, the answers to Pisa's own
# questions, every presentation tool but the sheet, the findings and the drawing, the weather; small tools
# asked often, where a search round would cost more than their schemas; "Pisa, ferma tutto"). The rest (task
# fine-grained edits, places and the web, email drafts, the profile, the plan's options) is found by
# search_tools. Each tool's `tier` in the registry (`pisa.domain.tools`).
ALWAYS = named(lambda t: t.tier in ("voice", "always"))
# the voice path's turn (an ask_brain from the talker) offers fewer still: what a spoken
# question or command needs; the cards and the rest are found by search_tools (the whole
# tool list is most of a spoken turn's input, the cards the largest).
VOICE_ALWAYS = named(lambda t: t.tier == "voice")
MAX_RESULTS = 5
DESCRIPTION = (
    "Load more of Pisa's tools (only the common ones are offered). Every other tool the instructions name "
    "(task edits, places and travel, the web, email, messages and calls, plan options, quiet mode, the "
    "profile, documents and bills, Pisa's autonomy) is "
    "found here: search its name or what it does, in English (['set_task_priority'], ['map search']), then "
    "call it. Nothing found: do without it."
)
_TOKEN = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower().replace("_", " ")))


def search(_ctx: Any, queries: Sequence[str], tools: Sequence[ToolDefinition]) -> list[str]:
    """The deferred tools that match: an exact name first, then by how many query words appear in a tool's
    name and description; at most MAX_RESULTS."""
    wanted = [q.strip().lower() for q in queries if q.strip()]
    exact = [t.name for t in tools if t.name.lower() in wanted]
    words = set().union(*(_terms(q) for q in wanted)) if wanted else set()
    scored = sorted(
        (
            (len(words & _terms(f"{t.name} {t.description or ''}")), t.name)
            for t in tools
            if t.name not in exact
        ),
        key=lambda pair: -pair[0],
    )
    return (exact + [name for score, name in scored if score > 0])[:MAX_RESULTS]


def deferred(names: Sequence[str], always: frozenset[str] = ALWAYS) -> list[str]:
    """The tools of `names` that are found by search, not offered at once."""
    return [n for n in names if n not in always]


def names_one(words: str, names: Sequence[str]) -> bool:
    """Their words name one of Pisa's own views, as whole words ("meteo", "mostrami meteo")."""
    said = f" {' '.join(_TOKEN.findall(words.lower()))} "
    return any(f" {' '.join(_TOKEN.findall(n.lower()))} " in said for n in names if _TOKEN.findall(n.lower()))


def offered(defs: list[ToolDefinition], wanted: frozenset[str]) -> list[ToolDefinition]:
    """These deferred tools offered at once for this request: their `defer_loading` off, and
    after every other tool, in name order, so the tools before them (the provider's cached prefix) stay the
    same bytes in the same order as on a turn that offers nothing."""
    if not wanted:
        return defs
    shown_now = [d.name for d in defs if d.name in wanted and d.defer_loading]
    if not shown_now:
        return defs
    rest = [d for d in defs if d.name not in shown_now]
    tail = sorted((d for d in defs if d.name in shown_now), key=lambda d: d.name)
    return rest + [dataclasses.replace(d, defer_loading=False) for d in tail]


def by_reference(model: Any) -> bool:
    """The model reveals a deferred tool by reference (Anthropic's `tool_addition` block), so
    the turn's offers can stay out of the cached tool list. A wrapper model asks the model it wraps."""
    seen = 0
    while model is not None and seen < 8:
        if (
            getattr(type(model), "supported_tool_addition_modes", frozenset())
            and getattr(model, "tool_addition_mode", None) == "by_reference"
        ):
            return True
        reveals = getattr(model, "reveals_by_reference", None)
        if reveals is not None:
            return bool(reveals)
        model, seen = getattr(model, "wrapped", None), seen + 1
    return False


# --- a search for what is switched off on this install (`domain.experimental`) ----------------------------

# the words that name an off feature outright, whatever else matched: a search with one of them is told the
# feature is off, not left to search again (F6: three to five searches for Gmail with Google off)
_OFF_WORDS: dict[str, frozenset[str]] = {
    "google": frozenset("google gmail email emails mail mails inbox".split()),
    "places": frozenset("places restaurant restaurants map maps web navigation directions".split()),
    "reminders": frozenset("reminder reminders remind".split()),
    "phone_actions": frozenset("sms whatsapp".split()),
    "drawing": frozenset("drawing drawings draw sketch".split()),
    "bank": frozenset("bank banks banking banca banche".split()),
    "radar": frozenset("pec".split()),
    "ade": frozenset("cassetto".split()),
    "talker_xai": frozenset("xai grok".split()),
}
# words too common to say a feature was meant (a tool name's "show" or "list")
_COMMON = frozenset(
    "show my list the to at on of a an add run ask again last change me what read get find set open search "
    "note notes".split()
)
OFF_MARK = "Off on this install:"


def _off_terms(feature: str) -> set[str]:
    """The words a search for `feature` would use: its id, its strong words, its own tools' names."""
    names = set(experimental.TOOLS.get(feature, frozenset()))
    names |= {f for f, feat in experimental.FAMILIES.items() if feat == feature}
    if feature == "google":
        names |= set(connectors.GOOGLE_TOOLS)
    words = set().union(*(_terms(n) for n in names)) if names else set()
    return (words | _terms(feature) | _OFF_WORDS.get(feature, frozenset())) - _COMMON


def searched_off(queries: Sequence[str], enabled: frozenset[str], *, found: bool) -> str | None:
    """The off feature a search_tools call was for, as its label, or None. With matches (`found`) only a
    feature's own strong words count (a search for "calendar" finds Pisa's calendar, on); with none, any of
    its words."""
    words = set().union(*(_terms(q) for q in queries)) if queries else set()
    for f in experimental.REGISTRY:
        if f.kind not in ("tools", "connector") or experimental.on(f.id, enabled):
            continue
        wanted = (_OFF_WORDS.get(f.id, frozenset()) - _COMMON) if found else _off_terms(f.id)
        if words & wanted:
            return experimental.label(f.id)
    return None


def off_message(label: str, *, again: bool) -> str:
    """What search_tools answers for a feature switched off on this install: once plainly, then shorter."""
    if again:
        return f"{OFF_MARK} {label}, as already found. Stop searching and answer now."
    return (
        f"{OFF_MARK} {experimental.not_on_pisa(label)[:-1]} (switched off on this install), so no tool for "
        "it exists here. Tell them so plainly, in one line, and don't search for it again."
    )


# show_setting's page for what they asked to connect: the one that lists the connectors
CONNECTORS_PAGE = "connectors"


def setting_off(section: str, said: Sequence[str], words: str, enabled: frozenset[str]) -> str | None:
    """The off feature a show_setting call would send them to Settings for, by its id, or None: one its own
    words name (`said`: the highlight, the purpose), or, on the connectors page with no feature named, one
    their words (`words`) name ("collega il mio conto in banca")."""
    if (feature := experimental.first_off(said, enabled)) is not None:
        return feature
    if section != CONNECTORS_PAGE or any(experimental.named(x) for x in said):
        return None
    return experimental.first_off([words], enabled)


def setting_refused(feature_id: str) -> str:
    """show_setting's answer for a feature that is off: nothing shown, nothing to connect."""
    return (
        f"Not shown: {experimental.unavailable(feature_id)[:-1]}: there is nothing to connect. Say so in one "
        "line; don't send them to Settings and don't offer steps."
    )
