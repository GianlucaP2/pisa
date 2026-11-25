"""Quality audit §22 m1: after a conversation reset, "rispondimi sempre in modo breve" was kept, yet the
replies ran to 38-46 words, said the answer twice, and one opened on an orphan question: "Vuoi che ne metta
una nel piano? Oggi hai tre cose…".

The cause, replayed with the scripted model: the model's first round said the day from memory and closed
with an offer ("Non hai niente da fare oggi. Vuoi che ne metta una nel piano?"). The unread rail held the day
for a read round; the closing-offer rail held the offer "until something follows it", and what followed was
the read round's answer. So the earlier round's offer opened that answer, or sat glued between two answers,
with a double space. A closing offer is now closing for its own round only, and the read round is told the
user heard the rest.

The standing instruction: its brevity is a reply control in code (`memoria.style`), with the cap enforced on
the reply, and the preferences block is the last of the turn's instructions.
"""

from typing import Any

import pytest

from pisa.domain import memoria
from pisa.domain.research import spoken_words
from pisa.services.conversation import READ_LINE
from tests.replay import harness
from tests.replay.scenarios import c

N = "2026-11-18T10:00"
READ_HINT = READ_LINE.split(":")[0].strip()
HEARD_HINT = "The user heard the rest of your reply"
TODAY = ["Pagare l'affitto", "Chiamare il dentista", "Comprare il latte"]
WORDS = "Che programmi ho oggi?"
BRIEF = "D'ora in poi rispondimi sempre in modo breve"
OFFER = "Vuoi che ne metta una nel piano?"
ANSWER = "Oggi non hai appuntamenti: hai tre cose da fare, l'affitto, il dentista e il latte."
# what the model wrote in the audit's long replies: over 40 words, the answer twice in other words
LONG = (
    "Oggi non hai appuntamenti in agenda e il piano non ha orari fissati. Hai tre cose da fare: pagare "
    "l'affitto, chiamare il dentista e comprare il latte. La giornata è abbastanza libera, puoi farle quando "
    "vuoi, anche nel pomeriggio, con calma e senza fretta."
)


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          script: list[Any], *, taught: bool = False) -> Any:  # fmt: skip
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario: dict[str, Any] = {
        "id": "m1-one-answer", "now": N, "lang": "it-IT", "max_questions": 0,
        "world": {"tasks_today": TODAY}, "turns": [{"owner": WORDS}], "script": [script], "expect": {},
    }  # fmt: skip
    if taught:  # the evening before, its own conversation: the scenario's turn comes after a reset
        scenario |= {
            "taught": [BRIEF],
            "taught_script": [
                [[c("remember_instruction", text=BRIEF)], "Va bene, risposte brevi da ora in poi."]
            ],
        }
    return play(client, db, fake_models, secrets_key, clock, http_handler, scenario)


def _reply(result: Any) -> str:
    return str(result.turns[-1].reply).strip()


def test_an_earlier_round_s_offer_never_opens_the_answer(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """Two rounds of text and an offer, after a reset: the first round's day (from memory) waits for the
    read, and its offer never opens the read round's answer."""
    script = [
        {"hint": READ_HINT, "then": [c("get_tasks")], "else": f"Non hai niente da fare oggi. {OFFER}"},
        ANSWER,
    ]
    reply = _reply(_play(client, db, fake_models, secrets_key, clock, http_handler, script, taught=True))
    assert reply == ANSWER, reply
    assert "  " not in reply and "?" not in reply


def test_an_earlier_round_s_offer_is_never_glued_between_two_answers(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """The audit's other shape: "La giornata è libera… Vuoi che ne metta una nel piano?  Oggi non hai
    appuntamenti…". The read round, told the user heard the rest, adds what the read says, once."""
    said = "La giornata è libera: nessun evento in agenda."
    added = "Hai tre cose da fare: l'affitto, il dentista e il latte."
    script = [
        {"hint": READ_HINT, "then": [c("get_tasks")], "else": f"{said} Non hai niente da fare oggi. {OFFER}"},
        {"hint": HEARD_HINT, "then": added, "else": ANSWER},
    ]
    reply = _reply(_play(client, db, fake_models, secrets_key, clock, http_handler, script))
    assert reply == f"{said} {added}", reply


def test_a_round_s_own_closing_offer_is_still_the_rails_to_decide(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """A round's own closing offer after its answer: the closing-offer rail drops it, as before."""
    script = [[c("get_tasks")], f"{ANSWER} {OFFER}"]
    reply = _reply(_play(client, db, fake_models, secrets_key, clock, http_handler, script))
    assert reply == ANSWER, reply


def test_a_standing_brief_caps_the_reply_after_a_reset(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, monkeypatch
) -> None:
    """m1: "rispondimi sempre in modo breve" kept the evening before; today's first turn, a new
    conversation, with a model that writes over 40 words: the reply is within the cap, in whole sentences,
    and still answers. Their preferences are the last of the turn's instructions."""
    told: list[str] = []
    scripted = harness.script

    def recording(scenario: dict[str, Any]) -> Any:
        model = scripted(scenario)

        async def recorded(messages: Any, info: Any) -> Any:
            told.append(str(getattr(messages[-1], "instructions", None) or ""))
            async for part in model(messages, info):
                yield part

        return recorded

    monkeypatch.setattr(harness, "script", recording)
    assert spoken_words(LONG) > 40
    result = _play(
        client, db, fake_models, secrets_key, clock, http_handler, [[c("get_tasks")], LONG], taught=True
    )
    reply = _reply(result)
    assert [r[0] for r in db.execute("SELECT text FROM owner_instructions").fetchall()] == [BRIEF]
    assert spoken_words(reply) <= memoria.BRIEF_WORDS <= 30, reply
    # the cut keeps the answer (quality audit §23 m1): the sentence that carries the tasks, not the framing
    assert reply.startswith("Hai tre cose da fare") and reply.endswith("."), reply
    last = told[-1].rstrip()
    assert BRIEF in last and last.endswith(">") and last.rsplit("\n", 1)[-1].startswith("</user-preferences-")


def test_without_the_instruction_the_same_reply_is_said_whole(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    reply = _reply(_play(client, db, fake_models, secrets_key, clock, http_handler, [[c("get_tasks")], LONG]))
    assert spoken_words(reply) > 40, reply


@pytest.mark.parametrize(
    ("text", "words", "lang"),
    [
        (BRIEF, memoria.BRIEF_WORDS, None),
        ("Risposte brevi, per favore.", memoria.BRIEF_WORDS, None),
        ("From now on, keep your answers short", memoria.BRIEF_WORDS, None),
        ("Rispondimi al massimo 15 parole", 15, None),
        ("Non rispondermi in modo breve", None, None),
        ("Mai tabelle.", None, None),
        ("Dammi del lei", None, None),  # the register: Pisa says "tu" (rule 24), the line stays text
        ("Rispondimi sempre in inglese", None, "en-US"),
        ("Answer me in Italian", None, "it-IT"),
    ],
)
def test_the_reply_controls_code_recognises(text: str, words: int | None, lang: str | None) -> None:
    style = memoria.style([{"id": 1, "text": text}])
    assert (style.words, style.lang) == (words, lang)


def test_a_newer_instruction_wins() -> None:
    rows = [{"id": 1, "text": "Rispondi breve"}, {"id": 2, "text": "Voglio risposte più dettagliate"}]
    assert memoria.style(rows).words is None
    assert memoria.style(list(reversed(rows))).words == memoria.BRIEF_WORDS
