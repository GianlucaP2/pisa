"""A tool call the model wrote as text never reaches the user (auditor's f6-en-1: `finish(say="I couldn't add
lunch with Sara…` was said as written): the words of a written `finish` are said without the call around
them, and a sentence holding any other call goes."""

import psycopg
import pytest

from pisa.domain import talk
from tests.assistant_env import set_settings
from tests.conftest import pair, set_key
from tests.live_app import live


@pytest.mark.parametrize(
    ("sentence", "said"),
    [
        (
            "finish(say=\"I couldn't add lunch with Sara at 13:00. ",
            "I couldn't add lunch with Sara at 13:00. ",
        ),
        ('The calendar is full then.")', "The calendar is full then."),
        ("`finish(say='Fatto.')`", "Fatto."),
        ('add_task(title="Pranzo con Sara", when="13:00") ', ""),
        ('Ok, add_task(title="x") done. ', ""),
        ("Ho spostato (la palestra) alle 17. ", "Ho spostato (la palestra) alle 17. "),
        ("Fatto (alle 17). ", "Fatto (alle 17). "),
    ],
)
def test_rules_a_written_call_is_never_said_as_code(sentence: str, said: str) -> None:
    assert talk.written_call(sentence) == said
    assert talk.keep(sentence, first=False, greet=False) == said


def test_a_finish_written_as_text_is_said_without_the_call(
    client, db: psycopg.Connection, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"briefing": {"window": None}})
    set_key(db, secrets_key)
    device_id, token = pair(db)
    fake_models.default = 'finish(say="Non ho aggiunto il pranzo con Sara. Alle 13 hai già la riunione.")'
    with live(client, token, device_id) as app:
        app.say("Aggiungi pranzo con Sara alle 13", source="glasses_mic")
        text = "".join(c["delta"] for c in app.reply())
    assert "finish" not in text and "say=" not in text and '")' not in text
    assert "Non ho aggiunto il pranzo con Sara." in text
    assert "Alle 13 hai già la riunione." in text
