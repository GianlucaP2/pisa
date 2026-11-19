# ruff: noqa: E501
"""The voice scenarios on the server's replay (Haiku): the server plays every scenario in order in
one database per pass, and the harness empties only `harness.WORLD_TABLES` between scenarios. voice2-01 asks one
cue-question (a `pisa_cue_asks` row) that stayed: voice2-02, -03 and -04 each counted it ("cue-questions asked: 1,
expected 0") though none of them asked. No cue line followed the email (voice2-03: `voice2_none outside=True`
every run). The scripted run plays each scenario in a fresh database, so it never saw this."""

from typing import Any

from psycopg.types.json import Jsonb

from tests.replay.scenarios import SCENARIOS
from tests.test_replay import _play


def _scenario(name: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == name)


def test_s_scenarios_in_order_in_one_database(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "voice2-01-perceive-then-ask-his-word-kept",
        "voice2-02-come-stai-and-a-trait",
        "voice2-03-outside-text-never-asks",
        "voice2-04-people-near-him-nothing-asked",
    ):
        got = _play(client, db, fake_models, secrets_key, clock, http_handler, _scenario(name))
        assert got.passed, (name, got.problems)
        if name.startswith("voice2-03"):  # as Haiku: the mail tool found through `search_tools`, then read
            assert [n for n, _ in got.turns[0].calls] == ["search_tools", "search_my_email"]


def test_the_replay_empties_voice2_s_tables_between_scenarios() -> None:
    from tests.replay import harness

    for table in ("pisa_cue_asks", "pisa_cue_quiet", "pisa_his_states", "pisa_character", "pisa_opinions"):
        assert table in harness.WORLD_TABLES


def test_a_scenario_before_left_the_knock_quiet(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """`voice1_quiet` (his "basta" to an offer, in `app_settings`) is the scenario's world, not Pisa's settings: a
    scenario before that hushed the knock never silences voice1-01's."""
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('voice1_quiet', %s)",
        (Jsonb({"knock": {"quiet": True, "since": "2026-11-11T07:00:00+00:00"}}),),
    )
    got = _play(
        client, db, fake_models, secrets_key, clock, http_handler, _scenario("voice1-01-the-knock-and-dimmi")
    )
    assert got.passed, got.problems


def test_a_model_drop_of_the_knock_waits_for_a_breakpoint_and_the_rails_still_drop() -> None:
    """The model picks when the knock goes, never whether; code's rails
    still drop it in quiet hours, past the cap and past the morning."""
    from datetime import UTC, datetime

    from pisa.domain import attend as attend_rules
    from tests.test_knock import _ask  # the knock candidate the lane's tests use

    now = datetime(2026, 11, 11, 8, 30, tzinfo=UTC)
    kept = attend_rules.validate(
        attend_rules.Decision(("k",), "drop", "voice", ""), [_ask()], attend_rules.Context(now, morning=True)
    )
    assert kept.choice == "breakpoint" and "ask_kept_by_code" in kept.rails
    late = attend_rules.validate(
        attend_rules.Decision(("k",), "drop", "voice", ""), [_ask()], attend_rules.Context(now, morning=False)
    )
    assert late.choice == "drop" and "knock_past_morning" in late.rails


def test_the_ceo_scenarios_in_order_in_one_database(
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:  # type: ignore[no-untyped-def]
    """The server plays every scenario of a pass in one database; il consiglio's and la prova
    generale's rows, the swarm's requests and their switches never reach the next scenario."""
    for name in (
        "voice4-01-il-consiglio-through-the-knock",
        "voice4-02-la-prova-generale",
        "voice4-03-a-swarm-near-miss-is-the-model-s",
        "voice4-04-a-quoted-label-is-the-sender-s",
        "voice4-01-il-consiglio-through-the-knock",  # again: last pass's offer and talk are gone
    ):
        got = _play(client, db, fake_models, secrets_key, clock, http_handler, _scenario(name))
        assert got.passed, (name, got.problems)
    from tests.replay import harness

    for table in ("pisa_consigli", "pisa_prove", "pisa_swarm_asks"):
        assert table in harness.WORLD_TABLES
