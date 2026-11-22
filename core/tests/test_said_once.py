"""F6 (Google off): the model searched for Gmail three to five times, said the refusal, then a working filler,
then the refusal again. RAIL: a sentence already said in this turn (case, punctuation and spaces set aside) is
never said again, whatever path emits it, and a continuation never says the cut reply's sentences again."""

import asyncio
import json

import psycopg
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall

from pisa.domain import talk
from pisa.services.conversation import _keeping
from tests.assistant_env import set_settings
from tests.conftest import pair, set_key
from tests.live_app import live

REFUSAL = "Non posso leggere la tua email: Google non è attivo su questo server."


def test_rules_a_sentence_key_sets_case_punctuation_and_spaces_aside() -> None:
    assert talk.sentence_key("Google non è attivo!") == talk.sentence_key("  google   NON è attivo. ")
    assert talk.sentence_key("...") == ""


def _said(deltas: list[str], earlier: tuple[str, ...] = ()) -> str:
    out: list[str] = []
    kept: list[str] = []

    async def emit(delta: str, final: bool) -> None:
        out.append(delta)

    async def run() -> None:
        keeping = _keeping(emit, kept, earlier)
        for d in deltas:
            await keeping(d, False)
        await keeping("", True)

    asyncio.run(run())
    return "".join(out)


def test_a_sentence_said_in_the_turn_is_never_said_again() -> None:
    said = _said(
        [f"{REFUSAL} ", "Ci sto ancora lavorando. ", REFUSAL.upper().replace(":", ";") + " ", "Altro? "]
    )
    assert said.casefold().count("google non è attivo") == 1
    assert "Ci sto ancora lavorando." in said and "Altro?" in said


def test_a_continuation_never_says_the_cut_reply_again() -> None:
    said = _said([f"{REFUSAL} Vuoi che lo segni?"], earlier=(f"{REFUSAL} ",))
    assert "Google" not in said and "Vuoi che lo segni?" in said


def test_searches_for_an_off_feature_then_the_refusal_twice_says_it_once(
    client, db: psycopg.Connection, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"window": None}})
    set_key(db, secrets_key)
    device_id, token = pair(db)

    async def model(messages, info):  # type: ignore[no-untyped-def]
        searched = sum(
            1 for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart) and p.tool_name == "search_tools"
        )  # fmt: skip
        if searched == 0:
            yield "Google non è attivo qui. "
        if searched < 3:
            args = {"queries": ["gmail read email"]}
            yield {0: DeltaToolCall(name="search_tools", json_args=json.dumps(args))}
            return
        yield "Google non è attivo qui! Ti leggo invece l'agenda di oggi?"

    fake_models.default = model
    with live(client, token, device_id) as app:
        app.say("leggi la mia ultima email", source="glasses_mic")
        text = "".join(c["delta"] for c in app.reply())
    assert text.casefold().count("google non è attivo") == 1, text
