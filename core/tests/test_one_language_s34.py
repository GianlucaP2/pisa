"""Release audit §34 (v0.1.5): earlier rounds and server lines in another language joined an answer.

- l1v-it (an it-IT device, "Hi, how are you?" spoken): "Doing well, thanks, and your afternoon is wide
  open. Bene, grazie per averlo chiesto…": the round before get_tasks streamed in English.
- l1-it rep 1 ("Scusa, rispondimi in italiano: cosa ho in lista?"): the finish READ_LINE held went on
  saying its other sentences ("Poi c'è prenotare i voli per Lisbona…", "Ti va di partire…?"), and the
  read's answer was dropped as their repeat: the user heard only the third task.
- l1-en rep 1: "Want me to fit the bill and the call into your free time?" was taken for the calls
  ability, and "Not connected yet: messages and calls…" was added to their list.

Scripted model only, synthetic titles.
"""

from typing import Any

from pisa.domain import tongue
from tests.replay.scenarios import c, done
from tests.test_rounds_said_once import IT_TASKS, _play  # pyright: ignore[reportPrivateUsage]

HOW_EN = "Hi, how are you?"
HOW_IT = "Ciao, come va?"


def _sentences(reply: str) -> list[str]:
    import re

    return [x for x in re.split(r"(?<=[.!?])\s+", reply.strip()) if x]


def test_l1v_it_a_spoken_turn_s_earlier_round_in_another_language_is_never_said(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    say = "Bene, grazie per averlo chiesto. Oggi hai due cose aperte: la bolletta della luce e l'idraulico."
    script = [
        ["Doing well, thanks, and your afternoon is wide open. ", c("get_tasks", group="today")],
        [done(say)],
    ]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": HOW_EN}], [script], IT_TASKS)  # fmt: skip
    reply = str(result.turns[0].reply)
    assert not any(tongue.foreign(x, "it-IT") for x in _sentences(reply)), reply
    assert "bolletta della luce" in reply, reply


def test_l1v_en_a_later_round_in_another_language_is_never_joined(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [
        ["Hi, good. Your afternoon is clear. ", c("get_tasks")],
        [
            done(
                "Ciao, tutto bene. Hai tre cose aperte: pagare la bolletta della luce e chiamare l'idraulico."
            )
        ],
    ]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "en-US",
                   [{"owner": HOW_IT}], [script], IT_TASKS)  # fmt: skip
    reply = str(result.turns[0].reply)
    assert not any(tongue.foreign(x, "en-US") for x in _sentences(reply)), reply


def test_l1_it_a_held_sentence_holds_the_rest_of_its_say(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    first = (
        "Oggi hai due cose: pagare la bolletta della luce e chiamare l'idraulico, entrambe entro oggi. "
        "Poi c'è prenotare i voli per Lisbona, entro lunedì 30 novembre. Ti va di partire da una delle due?"
    )
    read = (
        "Hai tre cose aperte: pagare la bolletta della luce e chiamare l'idraulico, entrambe entro oggi, e "
        "prenotare i voli per Lisbona entro lunedì 30 novembre."
    )
    script: list[Any] = [[done(first)], [c("get_tasks")], [done(read)]]
    tasks = [*IT_TASKS, "Prenotare i voli per Lisbona"]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": "Scusa, rispondimi in italiano: cosa ho in lista?", "typed": True}], [script],
                   tasks)  # fmt: skip
    reply = str(result.turns[0].reply)
    assert "Poi c'è" not in reply and "Ti va di partire" not in reply, reply
    assert "Hai tre cose aperte" in reply, reply


def test_l1_en_a_call_on_their_list_is_no_calls_ability() -> None:
    from pisa.domain.abilities import _CALLS  # pyright: ignore[reportPrivateUsage]

    assert not _CALLS.search("Want me to fit the bill and the call into your free time this afternoon?")
    assert not _CALLS.search("Vuoi che metta la chiamata all'idraulico nel pomeriggio?")
    assert _CALLS.search("I can call him for you.")
    assert _CALLS.search("Posso chiamarlo io.")
