"""What Pisa says it can do comes from what this install has connected (the owner's "Cosa puoi fare?" was
answered with mail, WhatsApp, calls and bookings, none of them connected, and the list twice).

- "Cosa puoi fare?" is answered by code from the install's state: once, what isn't connected yet named with
  where to connect it; no model round.
- RAIL: any reply's sentence that says Pisa can use what isn't connected is held; the turn ends with the
  honest line."""

import re
from typing import Any

import pytest

from pisa.domain import abilities
from tests.conftest import FakeModels, pair_v2, set_key
from tests.test_connectors import google_connected
from tests.test_live_v2_socket import connect, hello, opened, run, said

OVERCLAIM = (
    "Posso leggere e rispondere alle tue email, scrivere su WhatsApp, chiamare un locale e prenotare. "
    "In pratica gestisco posta, messaggi e prenotazioni."
)
NEVER_AS_ABLE = re.compile(r"whatsapp|prenot|chiam|e-?mail|posta|calendar", re.IGNORECASE)


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


def _model(calls: list[int], text: str) -> Any:
    async def reply(messages, info):  # type: ignore[no-untyped-def]
        calls.append(1)
        yield text

    return reply


def _ask(client: Any, token: str, device_id: str, words: str, lang: str = "it-IT") -> str:
    with connect(client, token) as ws:
        app = opened(
            ws, hello(device_id)
        )  # its app opens no message and no call (no pisa.compose, pisa.call)
        turn = run(words, lang=lang)
        app.send(turn)
        frames = app.run_ends(turn["input"]["runId"])
        app.check()
    return said(frames)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def test_a_bare_install_names_no_unconnected_ability_and_says_how_to_connect(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    calls: list[int] = []
    fake_models.default = _model(calls, OVERCLAIM)  # what the model said; it is never asked
    answer = _ask(client, token, device_id, "Cosa puoi fare?")
    assert calls == []  # by code, from the install's state
    can, gone, never = _sentences(answer)
    assert can.startswith("Qui mi occupo di:") and "attività" in can
    assert NEVER_AS_ABLE.search(can) is None  # nothing that isn't connected, as something Pisa does
    assert gone.startswith("Non ancora collegati:") and "Google, per posta e calendario" in gone
    assert "Impostazioni, Connettori" in gone  # where to connect it
    assert "messaggi e chiamate dal telefono" in gone
    assert never == "Non prenoto e non pago nulla al posto tuo."
    assert answer.count("Google,") == 1 and answer.count("Qui mi occupo") == 1  # said once
    bare = abilities.Facts()
    assert [abilities.overclaim(s, bare, asked=True) for s in _sentences(answer)] == [None] * 3
    stored = db.execute("SELECT role, trim(content) FROM messages ORDER BY id").fetchall()
    assert stored == [("user", "Cosa puoi fare?"), ("assistant", answer)]


def test_with_mail_connected_mail_is_named(  # type: ignore[no-untyped-def]
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:
    device_id, token = device
    set_key(db, secrets_key)
    google_connected(db)
    fake_models.default = _model([], OVERCLAIM)
    answer = _ask(client, token, device_id, "what can you do?", "en-US")
    can = _sentences(answer)[0]
    assert "Gmail" in can and "calendar" in can
    assert "Google," not in answer.split("Not connected yet:", 1)[-1]  # connected: never "not connected"
    assert "messages and calls from the phone" in answer  # this phone's app opens neither


def test_a_reply_that_claims_what_is_not_connected_is_held_and_says_what_is_not(
    client, device, db, secrets_key, fake_models: FakeModels
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = _model([], "Ti aiuto volentieri. " + OVERCLAIM)
    answer = _ask(client, token, device_id, "mi dai una mano con la giornata?")
    assert "rispondere alle tue email" not in answer and "WhatsApp" not in answer
    assert "gestisco posta" not in answer
    assert answer.count("Non ancora collegati:") == 1 and "Impostazioni, Connettori" in answer


def test_the_rules() -> None:
    for words in ("Cosa puoi fare?", "che cosa sai fare", "In cosa mi puoi aiutare?", "What can you do?",
                  "how can you help me?"):  # fmt: skip
        assert abilities.asks(words), words
    for words in ("Cosa devo fare oggi?", "cosa posso fare stasera a Roma?", "che tempo fa domani?"):
        assert not abilities.asks(words), words
    bare, mail = abilities.Facts(), abilities.Facts(google=True)
    assert abilities.overclaim("Posso rispondere alle tue email.", bare) == "mail"
    assert abilities.overclaim("Posso rispondere alle tue email.", mail) is None
    assert abilities.overclaim("Leggere la posta, scrivere su WhatsApp.", mail, asked=True) == "messages"
    assert abilities.overclaim("Posso prenotarti un tavolo.", mail) == "booking"  # never
    assert abilities.overclaim("Google non è collegato: lo colleghi nelle Impostazioni.", bare) is None
    assert abilities.overclaim("Posso ricordarti di scrivere a Marco.", bare) is None
    assert abilities.overclaim("La mail di Tom dice che arriva alle 9.", bare) is None  # no "posso": no claim
    assert abilities.overclaim("Gestisco la tua posta.", bare) == "mail"
    assert abilities.not_connected(abilities.Facts(google=True, maps=True, calls=True, push=True), "it") == ""
