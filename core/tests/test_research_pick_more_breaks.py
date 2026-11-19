# ruff: noqa: E501
"""The pick's second review: the break tests that found a hole, each fixed (rows V1-V6 in
`scripts/review/revert_agent_brain4.py`). Synthetic places and words."""

import asyncio
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pisa.brain.research_tools import ShownPlace, _show  # pyright: ignore[reportPrivateUsage]
from pisa.brain.research_turn import ResearchTurn
from pisa.domain import claims as claim_rules
from pisa.domain import requirements as need_rules
from pisa.domain import research as research_rules

ROME = ZoneInfo("Europe/Rome")
MORNING = datetime(2026, 11, 10, 10, 30, tzinfo=ROME)  # a Tuesday


def test_v_lunch_at_one_is_the_afternoon_never_one_at_night() -> None:
    """ "Lunch tomorrow at 1" was read as 01:00: every place closed, all excluded ("chiuso il mercoledì a
    pranzo", false), nothing shown."""
    for words, hm in (("Lunch tomorrow at 1", "13:00"), ("Pranzo domani alle 2", "14:00"),
                      ("Where can we have lunch at 2?", "14:00"), ("Un posto aperto domani alle 3", "15:00"),
                      ("Pranzo domani alle 13", "13:00"), ("Cena stasera alle 9", "21:00"),
                      ("colazione domani alle 8", "08:00"), ("Una farmacia aperta domani alle 3 di notte", "03:00")):  # fmt: skip
        got = need_rules.asked(words, MORNING)
        assert f"{got.at:%H:%M}" == hm and got.named, (words, got)


def test_v_a_hyphenated_unit_is_never_the_asked_hour() -> None:
    got = need_rules.asked("Dinner tonight at 4-star places", MORNING)
    assert f"{got.at:%H:%M}" == "20:00", got


def test_v_a_change_that_failed_on_the_phone_is_his_to_hear() -> None:
    """ "Non sono riuscito a metterti la sveglia sul telefono" is a failed change, never Pisa's plumbing."""
    for said in ("Non sono riuscito a metterti la sveglia sul telefono: l'app non risponde.",
                 "I couldn't put the reminder on your phone: the app didn't answer."):  # fmt: skip
        assert claim_rules.internal(said) is None, said
    for said in ("Non sono riuscito a mostrarti la card di Fittizia sul telefono.",
                 "Non sono riuscito a metterti la scheda sullo schermo.",
                 "I couldn't show it on your phone.", "I couldn't put the table on your screen."):  # fmt: skip
        assert claim_rules.internal(said) is not None, said


def test_v_an_estimate_of_his_own_data_needs_no_web_search() -> None:
    """ "Grosso modo 300 euro" on their own spending is their data's, never a web search (their words would go to a
    search engine); an estimate of the world's quantity they asked still waits for one."""
    assert (
        research_rules.unsearched(
            "Grosso modo 300 euro, tra spesa e benzina.", "Quanto ho speso questo mese?", False
        )
        is None
    )
    assert (
        research_rules.unsearched("Roughly an hour, by eye.", "How much did I spend on taxis?", False) is None
    )
    assert research_rules.unsearched(
        "A occhio una sessantina di chili.", "Quanto pesa un frigorifero?", False
    )
    assert research_rules.unsearched("By eye, about 48 metres.", "How long is the Rialto bridge?", False)


def test_v_a_place_s_kind_before_its_written_name_is_its_name() -> None:
    known = research_rules.plain("Fittizia. Osteria del Tralcio. Bar Lampo")
    assert research_rules.misnamed("Ti consiglio la Pizzeria Fittizia.", known) == []
    assert research_rules.misnamed("Ti consiglio la Pizzeria Nuvola.", known) == ["Pizzeria Nuvola"]


def test_v_the_card_never_says_open_now_for_a_time_he_named() -> None:
    """a05's rule on the card too: "aperto stamattina" is no reason for tomorrow's lunch (the row had it)."""
    turn = ResearchTurn(lang="it-IT", prompt_chars=0)
    turn.places = {"o": {"name": "Osteria del Tralcio", "travel_min": 6, "open_now": True}}
    turn.now = MORNING
    turn.asked = need_rules.asked("Dove pranziamo domani?", MORNING)
    shown: list[dict[str, Any]] = []

    async def show(card: dict[str, Any], focus: str, purpose: str) -> str:
        shown.append(card)
        return "shown"

    turn.show = show
    asyncio.run(
        _show(turn, "Pranzo", "Dove pranziamo domani?", [ShownPlace(place_id="o", why="cucina di casa")])
    )
    why = shown[0]["places"][0]["why"]
    assert "aperto" not in why and "6 minuti" in why, why


class _Result:
    """A replay result that passed every check (the replay could not reproduce the calls)."""

    checks: ClassVar[list[tuple[int, str, bool, str]]] = [(1, "quick", True, "2 research calls, table of 0")]
    hard: ClassVar[list[str]] = []


def test_v_e2e_a_change_he_asked_without_a_move_verb_keeps_its_truth(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """ "Mi ricordi di chiamare mamma alle 6?" and "La palestra di domani va alle 20" ask a change, though no
    move or cancel verb says so. The model claims it with none made and says something else too: on the lane
    the claim went to the rewrite, which said nothing, and they heard "Buon allenamento." alone, never that
    nothing was done. The rail's truth stays there."""
    from tests.replay.depth_compare import REWRITE
    from tests.replay.scenarios import SCENARIOS
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    base = next(s for s in SCENARIOS if s["id"] == "b6-f-a-haiku-reads-the-email-and-does-not-move")
    for words, claimed, truth in (
        ("Mi ricordi di chiamare mamma alle 6?", "Fatto: alle 6 ti ricordo di chiamare la mamma. Oggi la palestra è alle 18.", "Non l'ho ancora fatto"),
        ("La palestra di domani va alle 20", "Ho spostato la palestra di domani alle 20. Buon allenamento.", "Non l'ho spostata"),
    ):  # fmt: skip
        s = {**base, "id": "asked-act", "turns": [{"owner": words}],
             "script": [[{"hint": REWRITE, "then": "", "else": claimed}]]}  # fmt: skip
        result = _play(client, db, fake_models, secrets_key, clock, http_handler, s)
        said = result.turns[-1].reply
        assert truth in said and "Fatto" not in said and "Ho spostato" not in said, said
        assert result.passed, result.problems
