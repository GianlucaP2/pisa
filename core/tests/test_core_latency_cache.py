"""The cached prefix is byte-stable. Pisa's agent runs as for
`anthropic:claude-haiku-5-5` (`tests.cache_probe`): its requests rendered as Anthropic's wire bodies against a
simulated prompt cache. A turn's offered tool never enters the cache-keyed tool list; it is revealed by
reference in the turn's own request, after the breakpoint."""

import json
from typing import Any

import pytest

from tests import cache_probe
from tests.replay.scenarios import SCENARIOS
from tests.test_core_latency import OFFERS, PLAIN
from tests.test_replay import _play

# The offer called end to end. the own turns ("mostrami il meteo…", then "salvala come meteo",
# its scripted save_view) after a plain first turn.
VIEW = next(s for s in SCENARIOS if s["id"] == "k13-save-the-view-then-show-it-again")
SCENARIO: dict[str, Any] = {
    "id": "latency-offer-saved",
    "now": VIEW["now"],
    "lang": "it-IT",
    "world": {},
    "turns": [{"owner": PLAIN}, *VIEW["turns"][:2]],
    "script": [[], *VIEW["script"][:2]],
}
assert SCENARIO["turns"][-1]["owner"] == OFFERS

CACHE = cache_probe.Cache()


@pytest.fixture
def fake_models() -> Any:  # the tests' factory, every model behind the probe (the client is built from it)
    from tests.conftest import FakeModels

    real = FakeModels()

    class Probed(FakeModels):
        def __call__(self, spec, model_id, api_key, ollama_base_url):  # type: ignore[no-untyped-def]
            real.default, real.replies = self.default, self.replies
            return cache_probe.CacheProbe(real(spec, model_id, api_key, ollama_base_url), CACHE)

    return Probed()


def _conversation_rounds() -> list[cache_probe.Round]:
    return [r for r in CACHE.rounds if "finish" in r.tools]  # Pisa's brain (other slots carry no finish)


def test_an_offered_tool_stays_out_of_the_cached_prefix_and_is_revealed_after_it(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    CACHE.rounds.clear()
    _play(client, db, fake_models, secrets_key, clock, http_handler, SCENARIO)
    rounds = _conversation_rounds()
    # the user's words are the request's last user message (the turn's context follows them)
    plain = [
        r for r in rounds if PLAIN in json.dumps([m for m in r.body["messages"] if m["role"] == "user"][-1:])
    ]
    offer = [r for r in rounds if OFFERS in json.dumps(r.body["messages"])]
    assert plain and offer
    # the same cache-keyed tools, byte for byte and in the same order, on the turn that offers save_view
    assert all(r.keyed == plain[0].keyed for r in offer)
    assert "save_view" not in offer[0].tools and "save_view" in offer[0].deferred
    # revealed by reference in the turn's own request (Anthropic's tool_addition), after the breakpoint
    revealed = [
        block
        for message in offer[0].body["messages"]
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if block.get("type") == "tool_addition"
    ]
    assert [b["tool"]["name"] for b in revealed] == ["save_view"]
    # the second turn reads the whole prefix the first one wrote: tools and the system prompt
    assert offer[0].read > 0 and offer[0].read >= sum(len(k) // 4 for k in offer[0].keyed)
    # and the model called it, revealed, with no search round: the view is saved
    messages = offer[-1].body["messages"]
    at = max(i for i, m in enumerate(messages) if m["role"] == "user" and OFFERS in json.dumps(m))
    called = [  # this turn's own calls (the earlier turns' are in its history)
        block["name"]
        for message in messages[at:]
        if message["role"] == "assistant" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_use"
    ]
    assert "save_view" in called and "search_tools" not in called, called
    assert db.execute("SELECT name FROM pisa_views").fetchall() == [("meteo",)]
