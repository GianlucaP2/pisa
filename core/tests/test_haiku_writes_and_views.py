# ruff: noqa: E501
"""The brain on Haiku, after a replay.

Row 1, the real bug (d2-03): the writes waves 2 and 3 added answer in words, but they were in neither
`results.WRITE_TOOLS` nor `receipts.RECEIPTED`, so a true "Fatto" after them was called unbacked and rewritten.
- `test_every_brain_tool_is_classified`: every tool Pisa's brain offers is a write, a proposal or a read; a
  write has Pisa's own result line (its own, or the receipt); a proposal never counts as done. A new tool fails
  until it is classified here.
- `test_a_write_s_not_done_words_open_as_not_done`: every literal a receipted write returns opens either
  the way `receipts._NOT_DONE` reads a refusal, or as one of the done openings below.
- `test_a_true_fatto_is_kept_and_a_refused_one_rewritten`: per class of write, through Pisa's own service.
"""

import ast
import asyncio
import inspect
import re
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pisa.brain import receipts
from pisa.domain import claims, effects, results
from tests.owner import owner_at
from tests.replay.scenarios import c, done

CORE = Path(__file__).resolve().parents[1]


def _every_tool() -> list[str]:
    """Every tool Pisa's conversation agent offers, with every service it can have (stand-ins: nothing runs)."""
    from pisa.brain.conversation import TurnContext, build_agent
    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.research_turn import ResearchTurn

    seen: list[Any] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(info.function_tools)
        return ModelResponse(parts=[TextPart(content="ok")])

    s = MagicMock(timezone="Europe/Rome")
    s.skill_names = AsyncMock(return_value=[])
    s.paired = AsyncMock(return_value=False)
    s.names = AsyncMock(return_value=[])

    async def noop(_: Any) -> bool:
        return True

    async def kept(_: str) -> None:
        return None

    agent = build_agent(
        FunctionModel(capture), TurnContext(owner=owner_at(), clock=lambda: datetime.now(UTC), data=s, places=s,
        actions=s, profile=s, compose=noop, call=noop, research=ResearchTurn(lang="it-IT", prompt_chars=0),
        decided=s, defer=False, views=s, trips=s, people=s, rituals=(s, s), autopilot=s, handoffs=s,
        money=s, public=(s, s), world=s, explore=s, team=kept, sources=s, loops=s, owner_note=s,
        learn=s, night=s, memory=s,
        profile_turn=ProfileTurn(words="cosa dicono le fonti del corso?", lang="it-IT")),  # a turn about a course: Learn's
    )  # fmt: skip
    asyncio.run(agent.run("x"))
    return sorted({t.name for t in seen})


def every_tool() -> list[str]:
    """Every tool Pisa's conversation agent can offer: every service, a turn about a course (Learn's tools)
    and the drawing box on (its tools are conditional on the operator's switch)."""
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"PISA_DRAWING_BOX": "on"}):
        return _every_tool()


# The writes whose results Pisa builds itself (`results.done` / `needs_yes`); every other write gets the receipt.
OWN_LINE = frozenset(
    {"create_event", "move_event", "cancel_event", "answer_proposal", "undo_last_change", "send_draft",
     "add_task", "complete_task", "delete_task"}
)  # fmt: skip
WRITE, PROPOSAL, READ = "write", "proposal", "read"
# Every brain tool, by what it does. A write changes something of theirs (and says so); a proposal waits for their
# yes, their tap or their Face ID (never done); a read changes nothing (a show puts an object on the screen).
# The classes come from the code's manifest now (`pisa.domain.effects`, the one source of truth):
# own writes, outward and spend are writes here
CLASSES: dict[str, str] = {
    tool: {"own_write": WRITE, "outward": WRITE, "spend": WRITE, "proposal": PROPOSAL}.get(effect, READ)
    for tool, effect in effects.MANIFEST.items()
}


def test_every_brain_tool_is_classified() -> None:
    tools = every_tool()
    unclassified = [t for t in tools if t not in CLASSES]
    assert not unclassified, f"classify these brain tools (write, proposal or read): {unclassified}"
    for tool in tools:
        kind = CLASSES[tool]
        if kind == WRITE:  # its result carries Pisa's line: a true "Fatto" after it stands on code's record
            assert tool in results.WRITE_TOOLS, tool
            assert tool in OWN_LINE or tool in receipts.RECEIPTED, f"{tool}: a write without a receipt"
        else:  # a proposal or a read never counts as a change done
            assert tool not in results.WRITE_TOOLS and tool not in receipts.RECEIPTED, tool
        assert (kind == PROPOSAL) == (tool in claims.PROPOSES), tool
    # d2-03 and the operator's list: each is a write with a receipt now
    for tool in ("set_task_country", "set_task_deadline", "set_task_start", "add_document", "note_aire",
                 "remember_at_place", "forget_place_note", "keep_warranty", "rename_skill", "schedule_skill",
                 "save_view", "set_ritual", "subscription_used", "accept_autonomy", "stop_everything",
                 "resume_everything"):  # fmt: skip
        assert tool in receipts.RECEIPTED and tool in results.WRITE_TOOLS, tool
    for tool in ("note_hunch", "flight_claim"):  # they wait for their yes, their send
        assert tool in claims.PROPOSES


def test_a_proposal_s_act_never_backs_a_claim() -> None:
    """A proposal recorded as a move of the turn never backs "Fatto" (the record subtracts PROPOSES)."""
    record = claims.Record(frozenset(), frozenset(), frozenset())
    assert not claims.backed("Fatto: me lo segno.", "it-IT", record)
    backed = claims.Record(frozenset({"set_task_country"}), frozenset(), frozenset())
    assert claims.backed("Fatto: il ritiro della raccomandata è legato all'Italia.", "it-IT", backed)


# --- every literal a receipted write returns opens as done or as not done ---------------------------------

# The done openings of the wave 2/3 writes (anything else must open the way `_NOT_DONE` reads a refusal).
_DONE_OPENING = re.compile(
    r"^(?:Kept|Saved|Noted|Accepted|Answered|Stopping|Stopped|Resumed|Renamed|Scheduled|Changed|Forgotten|"
    r"Draft saved|Directions to|\{\}: |\{\} is now)\b"
)
# (file, function) whose returned literals are a receipted write's words: the brain tools and what they call
_SOURCES: list[tuple[str, str]] = [
    *(("pisa/brain/" + f, t) for f, ts in {
        "task_tools.py": ["set_task_country", "set_task_deadline", "set_task_start", "reschedule_task"],
        "autopilot_tools.py": ["add_document", "note_aire", "stop_everything", "resume_everything",
                               "accept_autonomy", "ask_again", "rename_skill", "schedule_skill",
                               "accountant_package"],
        "ritual_tools.py": ["remember_at_place", "forget_place_note", "set_ritual", "answer_my_question"],
        "money_tools.py": ["keep_warranty", "subscription_used"],
        "view_tools.py": ["save_view"],
        "follow_tools.py": ["start_navigation"],
        "handoff_tools.py": ["handoff_result"],
    }.items() for t in ts),
    ("pisa/services/place_notes.py", "forget"), ("pisa/services/rituals.py", "switch"),
    ("pisa/services/views.py", "save"), ("pisa/services/autonomy.py", "accept_spoken"),
    ("pisa/services/autonomy.py", "ask_again"), ("pisa/services/wishes.py", "rename"),
    ("pisa/services/wishes.py", "set_schedule"),
    ("pisa/services/accountant.py", "package"), ("pisa/services/actions.py", "draft_email"),
    ("pisa/services/actions.py", "draft_reply"), ("pisa/services/actions.py", "draft_package"),
    ("pisa/services/actions.py", "_new_recipient"),
    ("pisa/services/rituals.py", "answer"), ("pisa/services/rituals.py", "answer_newest"),  # the rituals' answers
]  # fmt: skip


def _opening(node: ast.expr | None) -> str | None:
    """A returned value's opening words: a literal's text, an f-string's with {} for its values, the text
    a wrapper (`_block`, `_titles`, `.strip()`) carries; None for anything else (a call's own result)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp):
        return _opening(node.left)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in ("_block", "_titles") and node.args:
            first = node.args[0]
            return _opening(node.args[1] if len(node.args) == 3 else first)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "strip":
            return _opening(node.func.value)
    if isinstance(node, ast.Attribute) and node.attr == "message":  # an error's raw words
        return "{}"
    return None  # a call's own result, a name, `_new_recipient`'s found person (a tuple)


def _returned(path: str, name: str) -> list[str]:
    tree = ast.parse((CORE / path).read_text())
    found = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    ]
    assert found, f"{path}: no {name}"
    out = []
    for fn in found:
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and (text := _opening(node.value)) is not None:
                out.append(text)
    return out


def test_a_write_s_not_done_words_open_as_not_done() -> None:
    wrong = []
    for path, name in _SOURCES:
        for text in _returned(path, name):
            if not receipts._NOT_DONE.match(text) and not _DONE_OPENING.match(text):
                wrong.append(f"{path}:{name}: {text[:80]!r}")
    assert not wrong, "open these as not done (Not …, No …, Several …) or as a done opening:\n" + "\n".join(
        wrong
    )
    # and each refusal's receipt is an error, each done opening's a done
    assert results.status_of(receipts.receipt_for("set_task_deadline", "Not set: a bad date")) == "error"
    assert (
        results.status_of(receipts.receipt_for("forget_place_note", "Several notes match (2: a, b)"))
        == "error"
    )
    assert (
        results.status_of(
            receipts.receipt_for("resume_everything", "Nothing to resume: Pisa wasn't stopped.")
        )
        == "error"
    )
    assert results.status_of(receipts.receipt_for("set_ritual", "ghost_run is now off. Say it.")) == "done"


def test_the_scan_reads_a_tool_s_words() -> None:
    """The scan itself: a parse error returned raw (as set_task_deadline did) is found."""
    src = textwrap.dedent(
        """
        async def set_task_deadline():
            try:
                pass
            except PisaError as err:
                return err.message
            return _titles(f"{found.title}: deadline")
        """
    )
    tree = ast.parse(src)
    fn = tree.body[0]
    texts = [_opening(n.value) for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert sorted(texts, key=str) == ["{}", "{}: deadline"]  # the raw error is seen
    assert inspect.isfunction(_returned)


# --- per class of write: a true "Fatto" is kept, a refused call's "Fatto" rewritten (Pisa's own service) -----

N = "2026-11-18T10:00"
# (class, tool call, their words when it is done, their words when it is refused, the world, the true "Fatto")
CLASSES_OF_WRITE: list[tuple[str, Any, str, str, dict[str, Any], str]] = [
    ("a task's country (d2-03)", c("set_task_country", task_ref="raccomandata", country="IT"),
     "la raccomandata la posso ritirare solo in Italia", "la raccomandata la posso ritirare solo lì",
     {"tasks": ["Ritirare la raccomandata"]},
     "Fatto: il ritiro della raccomandata è legato all'Italia, la lista lo ricorda quando ci sei."),
    ("the vault", c("add_document", kind="cie", expires_on="2027-03"),
     "aggiungi la scadenza della carta d'identità: marzo 2027", "aggiungi la scadenza della carta d'identità: marzo",
     {}, "Fatto: la carta d'identità scade a marzo 2027, te lo ricordo prima."),
    ("a place note", c("remember_at_place", text="comprare il bagnoschiuma", place="Esselunga"),
     "ricordami quando sono da Esselunga di comprare il bagnoschiuma",
     "ricordami quando sono lì di comprare il bagnoschiuma",
     {"places": [("Esselunga", 41.9, 12.5)]}, "Fatto: all'Esselunga ti ricordo il bagnoschiuma."),
    ("a ritual", c("set_ritual", ritual="ghost_run", on=False), "spegni il Ghost Run", "spegnilo", {},
     "Fatto: il Ghost Run è spento."),
    # M9: "ferma tutto" is stopped by code before the model (test_m9_*), and so is a plain "riprendi"
    # (test_resume_by_words); a resume in a longer sentence is the model's write
    ("Pisa's initiative", c("resume_everything"), "Pisa, riprendi tutto che sono tornato", "riprendi la corsa in palestra domani mattina",
     {"stopped": True}, "Fatto: le mie iniziative ripartono."),
    ("a warranty", c("keep_warranty", item="lavatrice", purchased_on="2025-03-03", seller="Unieuro"),
     "ho comprato la lavatrice da Unieuro il 3 marzo 2025", "ho comprato la lavatrice da Unieuro", {},
     "Fatto: la lavatrice è in garanzia fino a marzo 2027."),
    ("a skill", c("rename_skill", name="ora", new_name="orario"), "rinomina la skill ora in orario",
     "rinomina la skill ora", {"skills": [("ora", "on")]}, "Fatto: la skill ora adesso si chiama orario."),
]  # fmt: skip


def _scenario(words: str, call: Any, world: dict[str, Any], script: list[Any]) -> dict[str, Any]:
    return {
        "id": "b4", "now": N, "lang": "it-IT", "max_questions": 0, "world": world,
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip


@pytest.mark.parametrize("case", CLASSES_OF_WRITE, ids=[k[0] for k in CLASSES_OF_WRITE])
def test_a_true_fatto_is_kept_and_a_refused_one_rewritten(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    from tests.test_replay import _play

    _, call, words, refused, world, fatto = case

    def reply(said: str, script: list[Any]) -> Any:
        result = _play(
            client, db, fake_models, secrets_key, clock, http_handler, _scenario(said, call, world, script)
        )
        return result.turns[-1]

    # Haiku's shape (d2-03): the write and finish in one round; finish says it at once only when the write's
    # result reads as done (else the model is asked again, and says something else here)
    turn = reply(words, [[call, done(fatto)], "Ecco."])
    assert turn.reply.strip() == fatto and turn.unbacked == [], turn.reply
    # the write, then "Fatto" as the model's own words: the claim rail keeps it (code's record backs it)
    turn = reply(words, [[call], fatto])
    assert turn.reply.strip() == fatto and turn.unbacked == [], turn.reply
    # refused (their words don't carry it): the same "Fatto" is rewritten into the truth
    turn = reply(refused, [[call], fatto])
    assert claims.claim(turn.reply) is None and "Fatto" not in turn.reply, turn.reply


# --- row 2: asking in words is as safe as a held proposal; a move without either never passes ---------------


def test_a_move_without_asking_never_passes_the_check(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """b3-high-*'s check accepts a question in words (Haiku's run), never the gym moved with no question: Pisa's
    own lists without the stranger's title let the move act at `tell`, and the check fails it."""
    from tests.replay.scenarios import _BY_ID, SCENARIOS
    from tests.test_replay import _play

    base = next(s for s in SCENARIOS if s["id"] == "b3-high-gmail-title-tomorrow")
    assert "b3-high-gmail-title-tomorrow" in _BY_ID or base
    moved = {
        **base,
        "world": {k: v for k, v in base["world"].items() if k != "from_gmail"},
        "script": [[[c("get_agenda", day="tomorrow")],
                    [c("move_event", event="Palestra", start="domani alle 20", day="domani",
                       owner_said="la palestra mettila più tardi, dove ha senso")],
                    "Fatto: la palestra è alle 20."]],
    }  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, moved)
    assert not result.passed
    assert any("palestra" in p for p in result.problems), result.problems


# --- row 3 (k13): a view saved with paths that find nothing is refused, with the read's fields --------------


def test_a_recipe_s_placeholders_are_checked_against_today_s_read() -> None:
    from pisa.domain import views as rules

    weather = {"label": "oggi", "date": "2026-10-31", "day": {"min_c": 9.2, "max_c": 12.6, "precip_prob": 80}}
    haiku = rules.Recipe(
        [{"tool": "get_weather", "args": {}}],
        [{"type": "table", "columns": ["Giorno", "Max"], "rows": [["Oggi", "{{0.days.0.max}}"]]}],
    )
    assert rules.unresolved(haiku, [weather]) == ["{{0.days.0.max}}"]
    fixed = rules.Recipe(haiku.reads, [{"type": "stat", "label": "Max", "value": "{{0.day.max_c}}"}])
    assert rules.unresolved(fixed, [weather]) == []
    assert rules.unresolved(haiku, [None]) == []  # a read failing now proves nothing against the recipe
    each = rules.Recipe(
        haiku.reads,
        [{"type": "each", "from": "{{0.hours}}", "block": {"type": "text", "text": "{{item.t}}"}}],
    )
    assert rules.unresolved(each, [{"hours": [{"t": 1}]}]) == []
    assert rules.unresolved(each, [{"hours": [{"x": 1}]}]) == ["{{item.t}}"]
    assert rules.unresolved(each, [{"days": []}]) == ["{{0.hours}}"]
    assert "0.day.max_c" in rules.fields(weather, "0") and "0.date" in rules.fields(weather, "0")


def test_save_view_refuses_haiku_s_paths_and_names_the_real_ones(client, db) -> None:  # type: ignore[no-untyped-def]
    views = client.app.state.runtime.views
    weather = {"label": "oggi", "date": "2026-10-31", "day": {"min_c": 9.2, "max_c": 12.6}}

    async def fake(tool: str, args: dict[str, Any]) -> Any:
        return weather

    views._reads.run = fake  # pyright: ignore[reportPrivateUsage]
    recipe = {"reads": [{"tool": "get_weather", "args": {"where": "Roma", "when": "today"}}],
              "blocks": [{"type": "stat", "label": "Max", "value": "{{0.days.0.max}}"}]}  # fmt: skip
    said = client.portal.call(lambda: views.save("meteo", "il meteo", recipe, hosts=set(), said="salvala come meteo",
                                                 turn_id=None, device_id=None))  # fmt: skip
    assert said.startswith("Not saved: {{0.days.0.max}}") and "0.day.max_c" in said, said
    recipe["blocks"] = [{"type": "stat", "label": "Max", "value": "{{0.day.max_c}}"}]
    said = client.portal.call(lambda: views.save("meteo", "il meteo", recipe, hosts=set(), said="salvala come meteo",
                                                 turn_id=None, device_id=None))  # fmt: skip
    assert said.startswith('Saved the view "meteo"')


def test_the_words_that_point_to_a_tool_and_its_hint() -> None:
    from pisa.domain import people as people_rules
    from pisa.domain import tool_choice

    assert "save_view" in tool_choice.tools_for("salvala come meteo")
    assert "their_time" in tool_choice.tools_for("posso chiamare mamma adesso?")
    assert "their_time" in tool_choice.tools_for("che ore sono da Laura? posso chiamarla?")
    assert "their_time" not in tool_choice.tools_for("posso chiamare un taxi?")
    assert "their_time" not in tool_choice.tools_for("che ore sono qui?")
    assert "note_hunch" in tool_choice.hints("stasera di nuovo sushi, che buono")
    assert "their_time" in tool_choice.hints("posso chiamare mamma adesso?")
    assert tool_choice.hints("metti la palestra alle 18") == ""
    assert people_rules.answers_offer("sì, mandagli il grazie su WhatsApp")
    assert not people_rules.answers_offer("metti la palestra alle 18")


# --- row 4 ------------------------------------------------------------------------------------------------


def test_a_promise_under_a_condition_is_no_claim() -> None:
    """b3-p1-02 (4 Nov Haiku): "Se era una svista, lo annullo entro 24 ore" was cut into "Non l'ho annullato:
    non ho cambiato niente". A present act under a condition is a promise; a past one is still a claim."""
    record = claims.Record(frozenset({"mark_done"}), frozenset(), frozenset())
    said = "Fatto: ritirare le camicie è chiusa. Se era una svista, lo annullo entro 24 ore."
    assert claims.unbacked(said, "it-IT", record) == []
    assert claims.claim("Se era una svista, lo annullo.") is None
    assert claims.claim("If it was a mistake, I'll move it back.") is None
    assert claims.claim("Se era tardi, l'ho spostata alle 21.") == "done"
    assert claims.claim("Li sposto al 30.") == "doing"


def test_each_turn_starts_in_the_scenario_s_own_zone(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """b3-p1-r1 / p5-03 (on the server): the live zone is process state, which the real phone's reports
    change beside the replay. A zone set from outside between turns never reaches the scenario's turn."""
    from pisa.domain import zone
    from tests.replay import harness
    from tests.replay.scenarios import SCENARIOS
    from tests.test_replay import _play

    base = next(s for s in SCENARIOS if s["id"] == "b4-p1-r1-haiku-s-own-move")
    real = harness.world_step

    async def phone_reports_new_york(*args: Any, **kwargs: Any) -> None:  # a report from outside the replay
        client.app.state.runtime.owner.place = zone.PhoneContext("America/New_York", "US", "h12")

    harness.world_step = phone_reports_new_york  # type: ignore[assignment]
    try:
        moved = {**base, "turns": [{**base["turns"][0], "before": [{"tick": False}]}]}
        result = _play(client, db, fake_models, secrets_key, clock, http_handler, moved)
    finally:
        harness.world_step = real  # type: ignore[assignment]
    assert result.passed, result.problems  # the gym at 21:00 Rome, never 02:00


# --- the review of 06569f7 ----------------------------------------------------------------------


def test_a_done_claim_beside_a_proposal_is_still_a_claim() -> None:
    empty = claims.Record(frozenset(), frozenset(), frozenset())
    assert claims.unbacked("Sposto la palestra alle 17 e ho mandato la mail a Marco. Dimmi sì e lo faccio.",
                           "it-IT", empty)  # fmt: skip
    assert claims.unbacked("Ho cancellato la cena e la sposto a domani. Serve il tuo sì.", "it-IT", empty)
    assert (
        claims.unbacked("Sposto la call alle 16 e la palestra alle 17. Dimmi sì e lo faccio.", "it-IT", empty)
        == []
    )
    assert not claims.waits_for_yes("Ho cancellato la cena e la sposto a domani.", "Serve il tuo sì.")


def test_a_negated_yes_asks_nothing() -> None:
    assert not claims.asks_yes("Non serve il tuo sì.")
    assert claims.asks_yes("Serve il tuo sì.") and claims.asks_yes("Dimmi sì e lo faccio.")
    empty = claims.Record(frozenset(), frozenset(), frozenset())
    assert claims.unbacked("Sposto la palestra alle 17. Non serve il tuo sì.", "it-IT", empty)


def test_a_failed_fix_is_never_a_done_receipt() -> None:
    from pisa.domain import rituals as rules

    for said in rules.COULDNT.values():
        assert results.status_of(receipts.receipt_for("answer_my_question", f"Not done: {said}")) == "error"
    # Italian openings read as not done too
    for said in ("Non ci sono riuscito: è cambiato qualcosa.", "Nessuna domanda aperta.", "Niente da fare."):
        assert results.status_of(receipts.receipt_for("answer_my_question", said)) == "error"
    assert results.status_of(receipts.receipt_for("answer_my_question", "Segnato, grazie.")) == "done"
    # through the tool itself: the fix Pisa couldn't make is a not-done receipt ("I couldn't…" opens as none)
    import uuid

    from pydantic_ai import Agent

    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.ritual_tools import add_ritual_tools
    from pisa.brain.untrusted import watch_turn

    rituals = MagicMock()
    rituals.answer_in_turn = AsyncMock(return_value=rules.COULDNT["en-US"])
    agent: Any = Agent(None)
    add_ritual_tools(
        agent,
        rituals,
        MagicMock(),
        None,
        lambda: None,
        ProfileTurn(words="sì", device=uuid.uuid4(), lang="it-IT"),
    )
    tool = next(
        t.function
        for ts in agent.toolsets
        for n, t in getattr(ts, "tools", {}).items()
        if n == "answer_my_question"
    )

    async def answer() -> str:
        watch_turn()
        return await tool(yes=True)

    said = asyncio.run(answer())
    assert results.status_of(receipts.receipt_for("answer_my_question", said)) == "error", said


def test_a_bare_thanks_answers_no_offer() -> None:
    from pisa.domain import people as people_rules

    assert not people_rules.answers_offer("ok grazie, a domani")
    assert not people_rules.answers_offer("grazie mille")
    assert people_rules.answers_offer("sì, mandagli il grazie su WhatsApp")
    assert people_rules.answers_offer("sì, il grazie a Marco")
    assert people_rules.answers_offer("mandale gli auguri")


def test_an_outside_name_taints_the_offers_block() -> None:
    from pisa.brain.people_tools import add_people_tools
    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.untrusted import watch_turn

    people = MagicMock()
    people.drafts = AsyncMock(return_value=[("Marco Rossi", "thanks", "Ciao Marco! Grazie.")])
    seen: list[str] = []

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="ok")])

    from pydantic_ai import Agent

    for outside, kind in ((True, "people"), (False, "profile")):
        people.names_outside = AsyncMock(return_value=outside)
        agent: Any = Agent(FunctionModel(capture))
        add_people_tools(agent, people, ProfileTurn(words="sì, mandagli il grazie su WhatsApp", lang="it-IT"))
        fn = next(
            i.instruction
            for i in agent._instructions
            if getattr(i.instruction, "__name__", "") == "offered_today"
        )  # pyright: ignore[reportPrivateUsage]

        async def go(fn: Any = fn) -> str:
            kinds = watch_turn()
            text = await fn()
            seen.append(",".join(r.kind for r in kinds.readings()))
            return text

        assert "Ciao Marco" in asyncio.run(go())
        assert ("people" in seen[-1].split(",")) is outside, (kind, seen)  # an outside name taints the turn


def test_a_feed_read_keeps_no_closing_offer() -> None:
    from pisa.services.conversation import REQUEST_KINDS

    assert "public-data" not in REQUEST_KINDS and "email" in REQUEST_KINDS


def test_a_null_or_an_empty_day_is_unknown_never_wrong() -> None:
    from pisa.domain import views as rules

    reads = [{"tool": "get_agenda", "args": {}}]
    titled = rules.Recipe(reads, [{"type": "text", "text": "{{0.events.0.title}}"}])
    assert rules.unresolved(titled, [{"events": []}]) == []  # an empty day
    assert rules.unresolved(titled, [{"events": [{"title": None}]}]) == []  # a null today
    assert rules.unresolved(titled, [{"days": []}]) == ["{{0.events.0.title}}"]  # a key the shape lacks
    due = rules.Recipe(
        reads, [{"type": "each", "from": "{{0.tasks}}", "block": {"type": "text", "text": "{{item.due}}"}}]
    )
    assert rules.unresolved(due, [{"tasks": [{"title": "x", "due": None}]}]) == []
    assert rules.unresolved(due, [{"tasks": []}]) == []
    assert "0.day.max_c" in rules.fields({"day": {"max_c": None}}, "0")  # a null key is still a field
    under = rules.Recipe(reads, [{"type": "text", "text": "{{0.day.max_c}}"}])
    assert rules.unresolved(under, [{"day": None}]) == []  # a key under a null today: unknown
    assert rules.unresolved(under, [{"day": 5}]) == ["{{0.day.max_c}}"]  # a key under a number: wrong


def test_their_time_only_for_a_person() -> None:
    from pisa.domain import tool_choice

    for words in ("posso chiamare il ristorante per prenotare?", "posso sentire la musica?", "posso chiamare l'Uber?",
                  "che ora è per la cena?", "posso chiamare un NCC?"):  # fmt: skip
        assert "their_time" not in tool_choice.tools_for(words), words
    for words in (
        "posso chiamare mamma adesso?",
        "che ore sono da Laura? posso chiamarla?",
        "can I call mom now?",
    ):
        assert "their_time" in tool_choice.tools_for(words), words
