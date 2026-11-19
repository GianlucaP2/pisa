"""The turns of replay run 18 that followed the turn's context less once round 1 moved it
after their words (14 of the 180 always-passing scenarios failed). Each is played through Pisa's own code with
the real model's shape of reply (synthetic words, scripted), its model behind `tests.cache_probe` (the
replay's wire body).

What these pin:
- the turn's context (the clock, the one-question and answer-at-once rules, what Pisa holds) is in the system
  prompt of every request of the turn, where the model reads it, one system prompt a turn;
- what Pisa's own code holds whatever the model reads (nothing sent after outside text, the team's note kept,
  the trip's reminders made, the writes made).

What the code doesn't hold, the model's reading alone (the real replay decides; listed in each case's
`reading`): a second question after a refused send (f-a, b8-f-a) or any question after a team note (o1-09a),
more rounds than the scenario's 4 (b4-i2: Pisa's own cap is spoken.MAX_ROUNDS, 8), "Pisa ha…" in the third
person (p5-01: `bible.third_person` is the replay's check, the voice bible's rule is the prompt's)."""

import copy
from typing import Any

import pytest

from tests import cache_probe
from tests.replay.scenarios import _HAIKU_I2, SCENARIOS, c, done
from tests.test_replay import _play

BY = {s["id"]: s for s in SCENARIOS}
CACHE = cache_probe.Cache()


def _case(base: str, script: list[Any], reading: str) -> tuple[str, dict[str, Any], str]:
    scenario = copy.deepcopy(BY[base])
    scenario["script"] = script
    return base, scenario, reading


_FA = BY["f-a-send-in-a-turn-that-read-email-is-refused"]["script"][0]
_SEND = [[c("search_my_email", query="Claudio")], [c("send_draft")]]
I2 = "Fatto. In calendario oggi: a casa dalle 12 alle 16, corsa alle 14, palestra dalle 15 alle 16."
CASES = [
    # run 18: "Non l'ho mandata… La mando adesso?": two questions where at most one is allowed
    _case(
        "f-a-send-in-a-turn-that-read-email-is-refused",
        [_FA, [*_SEND, "Non l'ho mandata: la mail di Claudio è testo da fuori. La mando adesso?"]],
        "questions:",
    ),
    _case(
        "b8-f-a-a-refused-send-asks-no-second-question",
        [_FA, [*_SEND, "La risposta per Claudio è pronta: «Ciao Claudio, la cover ce l'ho io.» La mando?"]],
        "questions:",
    ),
    # run 18: "Segnalato al team… Mi serve sapere una cosa: …?", with no question allowed
    _case(
        "o1-09a-he-asks-to-tell-the-team",
        [[[c("tell_the_team")], "Segnalato al team. Mi serve sapere una cosa: succede sempre?"]],
        "questions:",
    ),
    # run 18: 5 rounds against the scenario's 4
    _case(
        "b4-i2-the-wait-quoted-by-its-hours",
        [[[_HAIKU_I2[0]], [_HAIKU_I2[1]], [_HAIKU_I2[2]], [_HAIKU_I2[3]], [*_HAIKU_I2[4:], done(I2)], I2]],
        "rounds",
    ),
    # run 18: "Pisa ha già segnato…", Pisa in the third person
    _case(
        "p5-01-a-flight-email-is-a-trip-with-check-in-and-leave-at",
        [[[c("get_trips")], "Pisa ha già segnato il volo AZ5287 di venerdì 20: esci alle 6:50. Il check-in "
          "apre giovedì 19 alle 10:35."]],
        "not in the first person",
    ),
]  # fmt: skip


@pytest.fixture
def fake_models() -> Any:  # the tests' factory, every model behind the probe
    from tests.conftest import FakeModels

    real = FakeModels()

    class Probed(FakeModels):
        def __call__(self, spec, model_id, api_key, ollama_base_url):  # type: ignore[no-untyped-def]
            real.default, real.replies = self.default, self.replies
            return cache_probe.CacheProbe(real(spec, model_id, api_key, ollama_base_url), CACHE)

    return Probed()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
def test_run18_the_turn_s_context_is_in_the_system_prompt_and_code_holds_its_own_rules(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    _, scenario, reading = case
    CACHE.rounds.clear()
    CACHE.binding = cache_probe.Binding()
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    rounds = [r for r in CACHE.rounds if "finish" in r.tools]  # Pisa's brain
    assert rounds
    # the turn's context where the model reads it: the system prompt of every request, never their messages
    for r in rounds:
        assert "Now: " in r.system
        assert "Now: " not in str(r.body["messages"])
    turns: dict[int, set[str]] = {}
    for r in rounds:
        turns.setdefault(r.model, set()).add(r.system)
    assert all(len(systems) == 1 for systems in turns.values())
    assert CACHE.binding.rejected == 0, CACHE.binding.causes
    # with run 18's reply, Pisa's own rules still hold (nothing sent, the note kept, the trip's reminders, the
    # writes): what fails is only what the model reads (its questions, its rounds, its person)
    assert result.problems and all(p.startswith(reading) for p in result.problems), result.problems
