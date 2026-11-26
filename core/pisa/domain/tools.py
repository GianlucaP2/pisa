"""Pisa's tool registry: each tool the conversation model can be offered, declared once. Pure data, no I/O.

An entry says what the tool does and how Pisa's rails treat it; the brain attaches the callable (its
`add_*_tools` group) and, for a change a gate may hold, the replay that makes it on the user's yes
(`pisa.brain.tools`). Every other list of tool names is derived from here: the effect manifest
(`pisa.domain.effects`), the approval kinds' tools and undo windows (`pisa.domain.approvals`), the tools
offered at once (`pisa.brain.tool_search`), the presentation tools (`pisa.brain.spoken`), the drawing's
reads (`pisa.brain.drawing_tools`), the drafts (`pisa.brain.receipts`), the slow research calls
(`pisa.brain.research_turn`) and the grounding rules (`pisa.domain.actions.grounded_change`). A tool that is
not here is refused in any tainted turn and counts as an outside read (fail closed); a test builds the agent
with every service and fails when an offered tool has no entry (`tests/test_core_manifest.py`).

Fields:
- `effect`: `read` reads or shows and changes nothing; `own_write` changes something of theirs (their
  calendar, tasks, profile, drafts, Pisa's own settings) and says so; `proposal` prepares or proposes (their
  yes, their tap or their Face ID does it); `outward` reaches someone else (an email sent, a call dialled);
  `spend` starts or lets go on work that spends (none today).
- `own`: a read of Pisa's OWN records (their lists, Pisa's records, their profile, the screen's objects),
  never outside text. Every other read is outside text (fail closed); `outside` names the kind its text is
  marked with before it runs (else "outside-read").
- `system`: the agent's own tool, not Pisa's (the deferred tools' search).
- `tier`: offered on every turn (`voice`: spoken turns too; `always`: typed turns only) or found through the
  tool search (`deferred`).
- `shows`: a presentation tool (the object is, or will be, in front of the user: nothing left to say).
- `reads_nothing`: its result is no read of anything (its last words, the tool search, the clock): a
  drawing's numbers never trace to it. `not_data`: a turn may call it and still draw from Pisa's own
  knowledge. `tap_ok`: a turn a tap in a drawing started may still call it (answer, find a tool, show).
- `slow`: a research call that may take seconds (Pisa says "let me look" once).
- `card`: asks the user's yes on a card of its own (every proposal does too). `own_line`: builds its own
  result line (`results.done` / `needs_yes`); every other write gets the receipt. `own_rail`: an outward tool
  left wholly to its own rail (the send's heard read-back), not the central gate. `drafted`: a draft saved,
  never sent.
- `approval`: the approval kind it asks with; `undo_minutes`: how long what it did can be undone (None:
  never).
- `grounding`: the rule by which the user's own words ground the change it makes (`grounded_change`), so it
  acts even after outside text. `earlier_grounds`: their words in an earlier turn of the talk ground it too.
  `offered`: a change Pisa offers from its own lists (an option, a plan item): vague words after the offer are
  Pisa's initiative.
- `held`: a change a gate may hold for the user's yes, made again on it (`pisa.brain.tools.replay`).
  `held_outside`: held after outside text, the thing it makes stays outside text (an email task).
"""

from collections.abc import Callable, Iterable
from dataclasses import KW_ONLY, dataclass
from typing import Literal

Effect = Literal["read", "own_write", "proposal", "outward", "spend"]
Tier = Literal["voice", "always", "deferred"]
ApprovalKind = Literal["calendar_create", "calendar_move", "calendar_cancel", "email_send", "setting_change"]
Grounding = Literal[
    "option", "skip", "done", "dismiss", "delete", "reschedule", "start", "priority", "add", "stop"
]  # fmt: skip


@dataclass(frozen=True)
class Tool:
    name: str
    effect: Effect
    _: KW_ONLY
    own: bool = False
    outside: str | None = None
    system: bool = False
    tier: Tier = "deferred"
    shows: bool = False
    reads_nothing: bool = False
    not_data: bool = False
    tap_ok: bool = False
    slow: bool = False
    card: bool = False
    own_line: bool = False
    own_rail: bool = False
    drafted: bool = False
    approval: ApprovalKind | None = None
    undo_minutes: int | None = None
    grounding: Grounding | None = None
    earlier_grounds: bool = False
    offered: bool = False
    held: bool = False
    held_outside: bool = False


TOOLS: tuple[Tool, ...] = (
    # reads: their lists, Pisa's records, the screen's objects, the connectors' readers, outside text
    Tool("about_person", "read"),
    Tool("airport_departure", "read"),
    # the app's screen map is untrusted data (contract 2.2): app_help's read counts as outside text
    Tool("app_help", "read", outside="app-map"),
    Tool("open_in_app", "read", own=True),
    Tool("ask_choice", "read", own=True, tier="always", shows=True),
    Tool("close_day", "read", own=True),
    Tool("close_object", "read", own=True, tier="always", shows=True, tap_ok=True),
    Tool("compare_places", "read", tier="always", shows=True),
    Tool("detour", "read"),
    Tool("events_on", "read", outside="event-pages"),
    Tool("find_note", "read", outside="notes"),
    Tool("find_products", "read"),
    Tool("finish", "read", own=True, tier="voice", reads_nothing=True, tap_ok=True),
    Tool("focus_object", "read", own=True, tier="always", shows=True, tap_ok=True),
    Tool("get_agenda", "read", own=True, tier="voice"),
    Tool("get_briefing", "read", tier="always"),
    Tool("get_current_time", "read", own=True, tier="voice", reads_nothing=True),
    Tool("get_plan", "read", own=True, tier="voice"),
    Tool("get_tasks", "read", own=True, tier="voice"),
    Tool("get_trips", "read", outside="email"),
    Tool("get_weather", "read", own=True, tier="voice"),
    Tool("health_summary", "read"),
    Tool("learn_ask", "read", outside="learn-answer"),
    Tool("learn_sources", "read", outside="learn-sources"),
    Tool("list_bills", "read", outside="email"),
    Tool("list_documents", "read"),
    Tool("list_place_notes", "read"),
    Tool("low_priority_list", "read"),
    Tool("map_search", "read", slow=True),
    Tool("money_back", "read"),
    Tool("money_recent", "read"),
    Tool("my_drawing", "read", not_data=True),
    Tool("my_findings", "read"),
    Tool("my_intentions", "read"),
    Tool("my_recent_decisions", "read", tier="always"),
    Tool("nearest_pharmacy", "read"),
    Tool("next_departures", "read"),
    Tool("obligations", "read"),
    Tool("open_at", "read"),
    Tool("place_details", "read", slow=True),
    Tool("plan_options", "read"),
    Tool("read_my_email", "read", outside="email"),
    Tool("read_page", "read", outside="web-page", slow=True),
    Tool("recall", "read", own=True),
    Tool("residency_count", "read"),
    Tool("resume_intention", "read"),
    Tool("run_skill", "read"),
    Tool("search_my_email", "read", outside="email-results"),
    Tool("search_tools", "read", system=True, reads_nothing=True, tap_ok=True, not_data=True),
    Tool("shop_prices", "read", outside="shop-prices"),
    Tool("show_changes", "read", own=True, tier="always", shows=True),
    Tool("show_departure_checklist", "read", own=True),
    Tool("show_drawing", "read", own=True, shows=True),
    Tool("show_email", "read", own=True),
    Tool("show_events", "read"),
    Tool("show_findings", "read", shows=True),
    Tool("show_fog_of_war", "read", own=True),
    Tool("show_item", "read", tier="always", shows=True),
    Tool("show_map_of_life", "read", own=True),
    Tool("show_note", "read", own=True, tier="always", shows=True, tap_ok=True),
    Tool("show_place", "read", tier="always", shows=True),
    Tool("show_places", "read", tier="always", shows=True),
    Tool("show_plan", "read", own=True, tier="always", shows=True),
    Tool("show_products", "read"),
    Tool("show_setting", "read", own=True, tier="always", shows=True),
    Tool("show_sheet", "read", own=True, shows=True, tap_ok=True),
    Tool("show_view", "read"),
    Tool("stanotte", "read", own=True),
    Tool("table_slots", "read", outside="booking-page"),
    Tool("their_time", "read", own=True),
    Tool("thing", "read", outside="email"),
    Tool("transit_alerts", "read"),
    Tool("travel_time", "read"),
    Tool("walk_times", "read", slow=True),
    Tool("warranty_check", "read"),
    Tool("weather_alerts", "read"),
    Tool("web_search", "read", outside="web-results", slow=True),
    Tool("what_did_i_do", "read", own=True),
    Tool("what_now", "read", own=True, tier="voice"),
    Tool("what_you_know_about_me", "read", own=True),
    Tool("where_am_i", "read", tier="voice"),
    Tool("while_i_was_away", "read", outside="email"),
    Tool("ztl_status", "read"),
    # their own things changed, said so
    Tool("accept_autonomy", "own_write"),
    Tool("accountant_package", "own_write"),
    Tool("add_document", "own_write"),
    Tool("add_task", "own_write",
        tier="voice", card=True, own_line=True, grounding="add", held=True, held_outside=True),
    Tool("answer_check_in", "own_write", tier="voice"),
    Tool("answer_intention", "own_write"),
    Tool("answer_my_question", "own_write"),
    Tool("answer_proposal", "own_write", own_line=True),
    Tool("apply_option", "own_write", card=True, grounding="option", offered=True, held=True),
    Tool("ask_again", "own_write"),
    Tool("cancel_event", "own_write",
        tier="voice", card=True, own_line=True, approval="calendar_cancel", undo_minutes=1440),
    Tool("cancel_reminder", "own_write"),
    Tool("close_intention", "own_write"),
    Tool("complete_task", "own_write",
        tier="voice", card=True, own_line=True, grounding="done", earlier_grounds=True, held=True),
    Tool("compose_message", "own_write"),
    Tool("confirm_intention", "own_write"),
    Tool("confirm_remember", "own_write"),
    Tool("create_event", "own_write",
        tier="voice", card=True, own_line=True, approval="calendar_create", undo_minutes=1440),
    Tool("delete_task", "own_write", card=True, own_line=True, grounding="delete", held=True),
    Tool("dismiss_task", "own_write", card=True, grounding="dismiss", held=True),
    Tool("draft_email", "own_write", card=True, drafted=True),
    Tool("draft_reply", "own_write", card=True, drafted=True),
    Tool("edit_draft", "own_write", card=True, drafted=True),
    Tool("first_talk", "own_write"),
    Tool("forget_about_me", "own_write"),
    Tool("forget_instruction", "own_write"),
    Tool("forget_place_note", "own_write"),
    Tool("handoff_result", "own_write"),
    Tool("keep_intention", "own_write"),
    Tool("keep_warranty", "own_write"),
    Tool("mark_done", "own_write",
        tier="voice", card=True, grounding="done", earlier_grounds=True, offered=True, held=True),
    Tool("move_event", "own_write",
        tier="voice", card=True, own_line=True, approval="calendar_move", undo_minutes=1440),
    Tool("never_learn_about", "own_write"),
    Tool("note_aire", "own_write"),
    Tool("plan_stop", "own_write", card=True, grounding="stop", held=True),
    Tool("remember_about_me", "own_write"),
    Tool("remember_about_person", "own_write"),
    Tool("remember_at_place", "own_write"),
    Tool("remember_instruction", "own_write"),
    Tool("remember_date", "own_write"),
    Tool("remember_to", "own_write", tier="voice"),
    Tool("rename_skill", "own_write"),
    Tool("rename_task", "own_write"),
    Tool("replan", "own_write"),
    Tool("reschedule_task", "own_write", card=True, grounding="reschedule", held=True),
    Tool("restore_task", "own_write"),
    Tool("resume_everything", "own_write"),
    Tool("save_drawing", "own_write"),
    Tool("save_note", "own_write"),
    Tool("save_view", "own_write"),
    Tool("schedule_skill", "own_write"),
    Tool("set_home", "own_write"),
    Tool("set_person_place", "own_write"),
    Tool("set_quiet_mode", "own_write"),
    Tool("set_ritual", "own_write"),
    Tool("set_task_country", "own_write"),
    Tool("set_task_deadline", "own_write"),
    Tool("set_task_duration", "own_write"),
    Tool("set_task_notes", "own_write"),
    Tool("set_task_place", "own_write"),
    Tool("set_task_priority", "own_write", card=True, grounding="priority", held=True),
    Tool("set_task_repeat", "own_write"),
    Tool("set_task_start", "own_write", card=True, grounding="start", held=True),
    Tool("skip_item", "own_write", card=True, grounding="skip", offered=True, held=True),
    Tool("start_navigation", "own_write"),
    Tool("stop_everything", "own_write", tier="voice"),
    Tool("stop_orbit", "own_write"),
    Tool("storyline_progress", "own_write"),
    Tool("subscription_used", "own_write"),
    Tool("tasks_bulk", "own_write"),
    Tool("tell_the_team", "own_write"),
    Tool("undo_adjustment", "own_write"),
    Tool("undo_last_change", "own_write", tier="voice", own_line=True),
    # prepared or proposed: their yes, their tap or their Face ID does it
    Tool("contest_bill", "proposal"),
    Tool("flight_claim", "proposal"),
    Tool("keep_skill", "proposal"),
    Tool("note_hunch", "proposal"),
    Tool("plan_task", "proposal"),
    Tool("prepare_call", "proposal"),
    Tool("prepare_cart", "proposal"),
    Tool("prepare_message", "proposal"),
    Tool("prepare_ride", "proposal"),
    Tool("prepare_shopping", "proposal"),
    Tool("prepare_table", "proposal"),
    # one of Pisa's server settings, made on their yes (contract 2.2), with Undo for a day
    Tool("propose_setting_change", "proposal",
        card=True, own_line=True, approval="setting_change", undo_minutes=1440),
    Tool("show_cancel_page", "proposal"),
    Tool("wish_skill", "proposal"),
    # reaching someone else: an email that leaves (after their heard read-back), a call the phone dials
    Tool("call_place", "outward"),
    Tool("send_draft", "outward",
        card=True, own_line=True, own_rail=True, approval="email_send", undo_minutes=None),
)  # fmt: skip


REGISTRY: dict[str, Tool] = {tool.name: tool for tool in TOOLS}
if len(REGISTRY) != len(TOOLS):  # a check that holds under `python -O` too
    _twice = sorted({t.name for t in TOOLS if sum(u.name == t.name for u in TOOLS) > 1})
    raise RuntimeError(f"a tool declared twice: {', '.join(_twice)}")


# the tools whose result is the user's phone acting (a sheet of buttons they tap, the compose screen, a dial,
# the maps app), each with the `pisa.*` extension a client's hello must have granted for it to be offered: a
# message is `pisa.compose`, a call `pisa.call`, a trip `pisa.directions`, the other handoff sheets (a table,
# a cart, a shopping list) `pisa.surface` (the sheet the app acknowledges). A hello that granted only
# `pisa.call` gets only the call tools; a run over plain HTTP granted none, so it gets none of them (a "Tap
# Send" with no button)
PHONE_ACT_EXTENSION: dict[str, str] = {
    "compose_message": "pisa.compose", "prepare_message": "pisa.compose",
    "call_place": "pisa.call", "prepare_call": "pisa.call",
    "start_navigation": "pisa.directions", "prepare_ride": "pisa.directions",
    "prepare_table": "pisa.surface", "prepare_cart": "pisa.surface", "prepare_shopping": "pisa.surface",
}  # fmt: skip
PHONE_ACTS = frozenset(PHONE_ACT_EXTENSION)
if not PHONE_ACTS <= set(REGISTRY):
    raise RuntimeError(f"a phone act not declared: {', '.join(sorted(PHONE_ACTS - set(REGISTRY)))}")
# every extension a phone act needs: a caller that says nothing of the client (a test, the eval) has them all
PHONE_EXTENSIONS = frozenset(PHONE_ACT_EXTENSION.values())


def phone_acts_withheld(granted: Iterable[str] | None) -> frozenset[str]:
    """The phone acts a client that granted `granted` is not offered (None: a run over HTTP, which granted
    none: every one)."""
    if granted is None:
        return PHONE_ACTS
    have = frozenset(granted)
    return frozenset(name for name, extension in PHONE_ACT_EXTENSION.items() if extension not in have)


def named(predicate: Callable[[Tool], bool], tools: Iterable[Tool] = TOOLS) -> frozenset[str]:
    """The names of the declared tools the predicate holds for."""
    return frozenset(tool.name for tool in tools if predicate(tool))


def entry(name: str) -> Tool | None:
    """A tool's declaration; None for a tool missing from the registry."""
    return REGISTRY.get(name)
