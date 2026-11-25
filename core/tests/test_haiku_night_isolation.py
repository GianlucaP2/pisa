# ruff: noqa: E501
"""Fixes from a Haiku replay (142/171). Each test fails with its fix reverted; the `b8-*` scenarios in
`tests/replay/scenarios.py` play Haiku's own calls and words."""

from typing import Any

from tests.replay.scenarios import SCENARIOS
from tests.test_replay import _play


def _scenario(name: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == name)


# --- Row 1: ideas-01 and curious-01 (3 of 3): the night of 4 Nov had already run ---------------------------------


def test_ideas_01_and_curious_01_after_night2_01_in_one_database(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """The server plays every scenario in order in one database. night2-01, ideas-01, curious-01 and desk-01
    all live 3 Nov 22:30 → the night of 4 Nov. `pisa_nights` was not emptied between scenarios, so ideas-01's
    night found night2-01's row ("night_already_ran"): no step ran, their "Pisa, problema: …" was never read, and
    `night done: 1` passed on night2-01's row. curious-01 the same. The scripted run plays each alone."""
    for name in (
        "night2-01-the-night-runs-in-shadow-between-two-days",
        "ideas-01-his-problem-is-incubated-in-shadow",
        "curious-01-his-question-is-held-open-in-shadow",
        "desk-01-the-night-desk-shows-no-unchecked-date",
    ):
        got = _play(client, db, fake_models, secrets_key, clock, http_handler, _scenario(name))
        assert got.passed, (name, got.problems)


def test_the_replay_empties_the_night_s_tables_between_scenarios() -> None:
    from tests.replay import harness

    for table in (
        "pisa_nights",
        "pisa_night_searches",
        "pisa_idea_problems",
        "pisa_unknowns",
        "pisa_topics",
        "pisa_desk_nights",
    ):
        assert table in harness.WORLD_TABLES


# --- Row 4: hunches (b3/b4/b5-p4-05, 1-2 of 3): p4-05's confirmed claim in every later variant's prompt --------


def test_s_confirmed_hunch_is_not_in_b3_p4_05_s_prompt(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """The server plays every scenario in one database: p4-05 ends with "Ti piace il sushi il venerdì" confirmed
    (a profile claim), and `profile_claims` was not emptied, so b3/b4/b5/b6/b7-p4-05 each began with "[confirmed
    0.80] like: Ti piace il sushi il venerdì" in Haiku's instructions: a habit already known, said as a fact
    ("è ormai la tua tradizione") or not asked at all. B6's and B7's hints could not fix that."""
    from tests.replay import harness

    seen: list[str] = []
    orig = harness.script

    def spy(scenario: dict[str, Any]) -> Any:
        model = orig(scenario)

        async def told(messages: Any, info: Any) -> Any:
            seen.append(f"{scenario['id']}\n{getattr(messages[-1], 'instructions', '') or ''}")
            async for x in model(messages, info):
                yield x

        return told

    harness.script = spy
    try:
        for name in ("p4-05-a-hunch-asked-once", "b3-p4-05-the-hunch-offered-at-once"):
            got = _play(client, db, fake_models, secrets_key, clock, http_handler, _scenario(name))
            assert got.passed, (name, got.problems)
    finally:
        harness.script = orig
    later = [s for s in seen if s.startswith("b3-p4-05")]
    assert later and not any("like: Ti piace il sushi" in s for s in later)


def test_the_replay_empties_his_profile_between_scenarios() -> None:
    from tests.replay import harness

    for table in ("profile_claims", "profile_evidence", "places", "check_ins", "email_correspondents"):
        assert table in harness.WORLD_TABLES
