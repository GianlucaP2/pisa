"""The end-state replay through Pisa's own ConversationService, in CI with the scripted model:
every scenario's rails and end state (`tests/replay`). The real model plays the same scenarios on the server
(maintenance → brain-replay)."""

from typing import Any

import pytest
from pydantic import SecretStr

from pisa.infra.keys import KeyStore
from tests.replay import harness
from tests.replay.scenarios import SCENARIOS


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> harness.Result:  # fmt: skip
    from tests.actions_fake import ActionsGoogle
    from tests.conftest import set_key

    set_key(db, secrets_key)
    google = ActionsGoogle()
    http_handler["handler"] = harness.route(
        google, harness.local(scenario["now"]).date(), public=harness.public_for(scenario)
    )  # The weather; the public feeds
    fake_models.default = harness.script(scenario)
    runtime = client.app.state.runtime
    keys = KeyStore(SecretStr(secrets_key))
    return client.portal.call(
        harness.run, runtime.conversations, runtime.pool, google, scenario, keys, clock, runtime
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_the_scenario_ends_as_it_should_through_pisa(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, scenario
) -> None:
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
    assert result.without_owner_said == 0


def test_the_table_names_every_failing_run() -> None:
    ok = harness.Result("a", [], [harness.TurnLog("x", "Fatto.", False, rounds=1, tokens=(100, 10))], 0, [])
    bad = harness.Result("b", ["event 'x' at 10: not there"], [harness.TurnLog("y", "?", False)], 1, [])
    text = harness.table([[ok, ok], [bad, ok]])
    assert "pass^2: 1/2" in text and "- b run 1: event 'x' at 10: not there" in text


def test_the_summary_and_a_failing_run_s_turns() -> None:
    turns = [
        harness.TurnLog("x", "Fatto.", False, rounds=r, tool_calls=r, tokens=(100, 10), seconds=s)
        for r, s in ((1, 2.0), (3, 9.0))
    ]
    turns.append(harness.TurnLog("sì", "Fatto.", True))
    text = harness.summary([[harness.Result("a", [], turns, 0, [])]])
    assert "model turns 2: rounds mean 2.00, max 3" in text and "p95 9.0" in text
    calls: list[tuple[str, dict[str, Any]]] = [("create_event", {"title": "Cena"})]
    bad = harness.TurnLog("metti la cena", "Fatto.", False, rounds=1, calls=calls)
    lines = harness.detail(harness.Result("b", ["false claim"], [bad], 0, ["Fatto."]))
    assert "create_event" in lines and "(NO owner_said)" in lines and "Pisa (1 rounds" in lines


def test_a_view_the_server_won_t_turn_on_is_scored_never_a_crash(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """The app's tap
    on a view whose trial failed is refused by the server; the run goes on, its end state fails it."""
    from tests.replay.scenarios import WEATHER_RECIPE, c

    base = next(s for s in SCENARIOS if s["id"] == "k22-a-wish-becomes-a-view-and-is-switched-on")
    failing = {
        **base,
        "id": "b10-k22-a-trial-that-fails",
        "script": [
            [[c("wish_skill", name="massima Roma",
                checks=[{"input": "la massima di Roma", "shows": "stat"}, {"input": "Roma", "has": "Milano"}],
                recipe=WEATHER_RECIPE)], "La prova non passa: non la accendo."],
            ["Non la accendo: la prova non passa."],
            [[c("run_skill", name="massima Roma")], "Ecco la massima a Roma."],
        ],
    }  # fmt: skip
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, failing)
    assert not result.passed and len(result.turns) == 3
    assert any("massima Roma" in p and "missing" not in p for p in result.problems), result.problems
    assert "trial" in result.turns[2].app and "(the app, before:" in harness.detail(result)  # its words kept
