"""Fixes at the merge (F1 to F3), each failing without its fix."""

from typing import Any

from tests.actions_fake import OWNER
from tests.test_acting import PAOLA, act, patches, row
from tests.test_acting import auth as auth  # the fixture
from tests.test_acting import google as google  # the fixture
from tests.test_acting_fixes import _option
from tests.test_approvals import dinner


def _apply(client: Any, db: Any, said: str, *, offered: bool, invited: int | None = None) -> None:
    from pisa.services.conversation import current_turn

    option, options = _option(client, db, invited), client.app.state.runtime.plan_options

    async def apply() -> Any:
        turn = current_turn.get()
        assert turn is not None
        if offered:
            turn.named_options = frozenset({option})  # Pisa offered them in the previous turn
        return await options.apply(option, "o2", "voice")

    act(client, apply, said=said)


def test_vague_words_never_tell_the_invited(client, db, google) -> None:  # type: ignore[no-untyped-def]
    """Pisa offered the option ("… e avviso 1 invitato"), the user says only "va bene": Paola hears of it only
    after a yes in the user's words or a tap."""
    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    _apply(client, db, "va bene", offered=True, invited=1)
    assert not patches(google) and row(db)[1] == "pending"


def test_a_yes_to_one_event_does_not_tell_another_event_s_people(client, db, google) -> None:  # type: ignore[no-untyped-def]
    """An approved option's other events: their people take their own yes."""
    from tests.assistant_env import rome

    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    actions = client.app.state.runtime.actions

    async def through() -> Any:
        return await actions.write_through("ev-dinner", rome(2026, 10, 20, 21), rome(2026, 10, 20, 22, 30),
                                           "approved_option", invited=1)  # fmt: skip

    approval_id, _ = act(client, through, said="sì")
    assert approval_id is not None and not patches(google) and row(db)[1] == "pending"


def test_the_option_passes_approved_option_for_its_other_events() -> None:
    import inspect

    from pisa.services.plan_options import PlanOptionsService

    assert '"approved_option" if approved is not None' in inspect.getsource(PlanOptionsService._through)


def test_options_pisa_did_not_offer_take_one_yes(client, db, google) -> None:  # type: ignore[no-untyped-def]
    """Options seen only in the day's context (next to invite titles), never offered by Pisa's own tools: a
    vague "va bene" is not the user choosing them."""
    dinner(google, db)
    _apply(client, db, "va bene", offered=False)
    assert not patches(google) and row(db)[1] == "pending"


def test_options_pisa_offered_are_applied_with_undo(client, db, google) -> None:  # type: ignore[no-untyped-def]
    dinner(google, db)
    _apply(client, db, "va bene", offered=True)
    assert len(patches(google)) == 1 and row(db)[1] == "done"


def test_the_offer_is_recorded_by_pisa_s_tools_and_carried_to_the_next_turn() -> None:
    import uuid

    from pisa.services.assistant import _offered
    from pisa.services.conversation import TurnState, current_turn

    turn = TurnState(uuid.uuid4(), "fammi delle opzioni")
    token = current_turn.set(turn)
    try:
        _offered({"options_id": "abc"})
    finally:
        current_turn.reset(token)
    assert turn.offered == ["abc"]


def test_grok_4_7_thinks_at_low_in_conversation_and_other_grok_models_get_no_effort() -> None:
    """Grok 4.7 defaults to "high": 2 to 47 s before a word on the conversation eval (2026-10-28)."""
    import asyncio
    import json
    from typing import cast

    import httpx
    from openai import AsyncOpenAI
    from pydantic_ai import Agent

    from pisa.brain.models.providers import PROVIDERS_BY_ID, xai_model

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 1, "model": "grok",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Ciao."}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })  # fmt: skip

    async def ask(model_id: str) -> None:
        base_url = PROVIDERS_BY_ID["xai"].base_url
        fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AsyncOpenAI(base_url=base_url, api_key="xai-test-key", http_client=cast(Any, fake))
        await Agent(xai_model(model_id, "xai-test-key", base_url, client)).run("ciao")

    for model_id in ("grok-4.7", "grok-4-fast"):
        asyncio.run(ask(model_id))
    assert sent[0].get("reasoning_effort") == "low"
    assert "reasoning_effort" not in sent[1]


def test_anthropic_models_get_the_effort_the_replays_measured() -> None:
    """Pisa on Anthropic; left unset, Sonnet 5.5 thinks at "high" (slow): Haiku at medium, Sonnet low."""
    from pisa.brain.models.providers import PROVIDERS_BY_ID, build_model

    spec = PROVIDERS_BY_ID["anthropic"]
    haiku = build_model(spec, "claude-haiku-5-5", "k", None)
    sonnet = build_model(spec, "claude-sonnet-5-5", "k", None)
    other = build_model(spec, "claude-opus-5-5", "k", None)
    assert (haiku.settings or {}).get("anthropic_effort") == "medium"
    assert (sonnet.settings or {}).get("anthropic_effort") == "low"
    assert not (other.settings or {}).get("anthropic_effort")
