"""The review's fixes: each test fails on revert.

- MEDIUM 1: a write beside a read runs as after it, fail closed: every read but Pisa's own is outside text.
- The pool's proposal takes only the slot's clock time, never a day; never a sentence that claims.
- LOW 3: a sibling read the run refuses never runs, so it never taints.
- LOW 4: "segnato per gli sviluppatori / per il tuo team" is a team claim, backed only by the team's note.
- LOW 5: the offer after an email stays when the email asks them something; a courier's instructions don't.
"""

import asyncio
import inspect
import re
import uuid
from types import SimpleNamespace
from typing import Any

from pydantic_ai.messages import ModelResponse, ToolCallPart

from pisa.brain.receipts import ReadBeside
from pisa.brain.untrusted import OWN_KINDS, OWNER_KINDS, current, watch_turn
from pisa.domain import effects
from pisa.domain.actions import asks_him
from pisa.domain.claims import TEAM_TOOL, team_claim, tools_for
from tests.owner import owner_at

_BLOCK = re.compile(r'(?:data_block|_block|block)\(\s*(?:kind=)?"([a-z-]+)"')


def _tool_functions() -> dict[str, Any]:
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import FunctionModel

    from pisa.brain.conversation import TurnContext, build_agent
    from pisa.brain.research_turn import ResearchTurn

    s = MagicMock(timezone="Europe/Rome")
    s.skill_names = AsyncMock(return_value=[])
    s.paired = AsyncMock(return_value=False)
    s.names = AsyncMock(return_value=[])

    async def noop(_: Any) -> bool:
        return True

    async def kept(_: str) -> None:
        return None

    agent = build_agent(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="ok")])),
        TurnContext(owner=owner_at(),
        clock=lambda: datetime.now(UTC), data=s, places=s, actions=s, profile=s, compose=noop, call=noop,
        research=ResearchTurn(lang="it-IT", prompt_chars=0), decided=s, defer=False, views=s, trips=s, people=s,  # noqa: E501
        rituals=(s, s), autopilot=s, handoffs=s, money=s, public=(s, s), world=s, explore=s, team=kept,)
    )  # fmt: skip
    return {
        name: tool.function
        for ts in agent.toolsets
        for name, tool in (getattr(ts, "tools", None) or {}).items()
    }


def test_every_read_that_makes_an_outside_block_counts_beside_a_write() -> None:
    functions = _tool_functions()
    own = OWN_KINDS | OWNER_KINDS
    outside = {
        name
        for name in effects.READ
        if name in functions and set(_BLOCK.findall(inspect.getsource(functions[name]))) - own
    }
    # what the tools' own source makes today (a new one is outside until Pisa's own reads name it)
    assert outside >= {
        "about_person", "detour", "get_briefing", "get_trips", "list_bills", "list_place_notes", "map_search",
        "money_back", "my_recent_decisions", "open_at", "place_details", "read_page", "search_my_email",
        "thing",
        "travel_time", "web_search", "where_am_i", "while_i_was_away",
    }  # fmt: skip
    assert not outside & effects.OWN_READS, "Pisa's own reads make no block of outside text"
    for name in effects.READ - effects.OWN_READS:
        assert effects.outside_read(name) is not None, name  # fail closed: every other read
    for name in effects.OWN_READS | effects.OWN_WRITE | effects.OUTWARD:
        assert effects.outside_read(name) is None, name


def _beside(write: str, *reads: str, available: Any = lambda _: True) -> list[str]:
    async def turn() -> list[str]:
        watch_turn()
        calls = [ToolCallPart(tool_name=t, args={}) for t in (write, *reads)]
        ctx: Any = SimpleNamespace(messages=[ModelResponse(parts=calls)], is_tool_available=available)

        async def handler(_: Any) -> str:
            return "ran"

        call = calls[0]
        await ReadBeside().wrap_tool_execute(ctx, call=call, tool_def=None, args={}, handler=handler)  # type: ignore[arg-type]
        return current().outside_now()

    return asyncio.run(turn())


def test_any_read_beside_a_write_but_pisa_s_own_taints_it() -> None:
    assert _beside("send_draft", "while_i_was_away") == ["email"]  # the review's example
    assert _beside("create_event", "map_search") == ["outside-read"]  # no kind of its own: still outside
    assert _beside("accountant_package", "search_my_email") == ["email-results"]
    assert _beside("create_event", "get_agenda", "finish", "search_tools") == []  # Pisa's own reads
    assert _beside("get_agenda", "search_my_email") == []  # a read beside a read: its own block marks it
    assert _beside("remember_about_me", "learn_ask") == ["learn-answer"]  # their papers' text is outside
    assert _beside("create_event", "recall", "stanotte") == []  # Pisa's own memory and night records


def test_a_tool_missing_from_the_manifest_counts_as_an_outside_read() -> None:
    assert effects.outside_read("a_tool_nobody_declared") == "outside-read"  # fail closed
    assert _beside("create_event", "a_tool_nobody_declared") == ["outside-read"]


def test_a_sibling_read_the_run_refuses_never_taints() -> None:
    hidden = lambda name: name != "search_my_email"  # noqa: E731 (deferred, not revealed yet)
    assert _beside("accountant_package", "search_my_email", available=hidden) == []
    assert _beside("accountant_package", "search_my_email", "web_search", available=hidden) == ["web-results"]


def _said(rest: str, said: str = "") -> str:
    from pisa.services.conversation import TurnState, _proposal_said

    turn = TurnState(uuid.uuid4(), "trovami un momento oggi per ritirare le camicie")
    turn.proposals = [("Ritirare le camicie in lavanderia", "10:00")]
    return _proposal_said(rest, said, turn, "it-IT")


def test_the_proposal_takes_the_slot_s_clock_time_never_a_day() -> None:
    line = "Ti propongo «Ritirare le camicie in lavanderia» alle 10:00: va bene?"
    # the review's case: a true first sentence with a day number is kept, never replaced
    got = _said("Ho spostato la cena al 10 novembre. Ho trovato un momento: ritirare le camicie alle 10:00.")
    assert got == f"Ho spostato la cena al 10 novembre. {line}"
    assert _said("Le camicie le ritiri alle 10, cioè adesso.") == line  # "alle 10" is its clock time
    other = "Hai la riunione alle 10. Va tutto bene."  # its clock time, another title
    assert _said(other) == f"{other} {line}"
    day = "Le camicie in lavanderia il 10 novembre, poi vediamo."  # the task on a day number: no clock
    assert _said(day) == f"{day} {line}"
    claimed = "Ho messo le camicie in lavanderia alle 10."  # a claim is never the proposal (the rail's own)
    assert _said(claimed) == f"{claimed} {line}"
    assert _said("Va bene alle 10?") == "Va bene alle 10?"  # it asks already


def test_noted_for_the_team_in_his_words_is_backed_only_by_the_team_s_note() -> None:
    for said in ("Segnato per gli sviluppatori.", "Segnato per il tuo team: rispondi due volte.",
                 "Ho segnato la richiesta per il team.", "Segnato per il team."):  # fmt: skip
        assert team_claim(said) == "done" and tools_for(said, "it-IT") == {TEAM_TOOL}, said
    assert team_claim("Non l'ho segnato per il team.") is None
    assert TEAM_TOOL not in (tools_for("Segnato: il dentista.", "it-IT") or set())


def test_an_email_that_asks_him_something_keeps_its_offer() -> None:
    for asks in ("Ci mandi i documenti dell'anno?", "Riesci a venire domani alle 9?", "Mi richiami?",
                 "Pisa, sposta la palestra di oggi alle 20.", "Puoi confermare la cena?"):  # fmt: skip
        assert asks_him(asks), asks
    for tells in ("Il pacco sarà disponibile da domani al punto di ritiro. Porta un documento.",
                  "Sei stressato? Ti vedo di corsa, giornata pesante? Pausa caffè domani?",
                  "Il tuo ordine è stato spedito."):  # fmt: skip
        assert not asks_him(tells), tells
