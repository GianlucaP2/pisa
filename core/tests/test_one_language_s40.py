"""Release audit §40 (v0.1.6): the one-language rail (§34) emptied whole turns.

f11-it-1 rep 3: on an Italian conversation the user typed "And what about tomorrow?"; the turn is theirs in
English, the model answered from the history in Italian ("Domani, venerdì 27, non hai né eventi né
scadenze…"), and the rail left every sentence out: they heard "Let's see…" and nothing was kept. The rail
drops another language's sentences only beside an answer in the turn's own; a whole answer in another
language is asked again in the turn's, once, and said as written if that round is no better. Never an empty
turn.

Scripted model only, synthetic titles.
"""

from tests.replay.scenarios import c, done
from tests.test_one_language_s34 import _sentences  # pyright: ignore[reportPrivateUsage]
from tests.test_rounds_said_once import IT_TASKS, _play  # pyright: ignore[reportPrivateUsage]

ASKED = "And what about tomorrow?"
ITALIAN = "Domani, venerdì 27, non hai né eventi né scadenze. Hai la giornata libera."
ENGLISH = "Tomorrow, Friday the 27th, you have no events and nothing due."


def test_a_whole_answer_in_another_language_is_asked_again_in_the_turn_s(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [[c("get_agenda")], [done(ITALIAN)], [done(ENGLISH)]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": ASKED, "typed": True}], [script], IT_TASKS)  # fmt: skip
    reply = str(result.turns[0].reply)
    assert ENGLISH in reply and "Domani" not in reply, reply


def test_never_an_empty_turn_when_asked_again_is_no_better(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    script = [[c("get_agenda")], [done(ITALIAN)], [done("Domani è libero: niente eventi né scadenze.")]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": ASKED, "typed": True}], [script], IT_TASKS)  # fmt: skip
    reply = str(result.turns[0].reply)
    assert [x for x in _sentences(reply) if x not in ("Let's see…",)], reply
    assert "Domani" in reply, reply
    stored = db.execute("SELECT content FROM messages WHERE role = 'assistant'").fetchall()
    assert stored and any("Domani" in str(r[0]) for r in stored), stored


def test_a_stray_sentence_beside_the_turn_s_own_answer_still_goes(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    """§34 holds: a mix keeps the turn's language only, and asks nothing again."""
    script = [[c("get_agenda")], [done(f"{ENGLISH} Hai la giornata libera.")]]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                   [{"owner": ASKED, "typed": True}], [script], IT_TASKS)  # fmt: skip
    reply = str(result.turns[0].reply)
    assert ENGLISH in reply and "giornata libera" not in reply, reply
