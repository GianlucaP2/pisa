"""§27 a1: "E dove vedo le cose da fare di oggi?" asks where something is in the app. Its answer names a tab
from the app's map; with no list read that turn, it is never replaced by the no-read line ("Non ho riletto la
tua lista…"). A sentence that says what the day holds still waits for a read."""

from typing import Any

import pytest

from pisa.domain import claims
from tests.conftest import pair_v2, set_key
from tests.test_live_v2_socket import connect, hello, opened, run, said

WHERE = "E dove vedo le cose da fare di oggi?"
TAB = "Le trovi nella scheda Oggi: in alto vedi le cose da fare di oggi."


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


@pytest.mark.parametrize(
    ("words", "nav"),
    [
        (WHERE, True),
        ("Dove trovo la lista della spesa?", True),
        ("In quale scheda sono i promemoria?", True),
        ("Where can I see my tasks?", True),
        ("Which tab has my list?", True),
        ("Cosa devo fare oggi?", False),
        ("C'è il latte nella lista?", False),
        ("Dov'è il latte?", False),
    ],
)
def test_asks_where_in_app(words: str, nav: bool) -> None:
    assert claims.asks_where_in_app(words) is nav


def test_a_tab_named_is_said_and_the_day_stated_still_waits() -> None:
    assert claims.unread_state(TAB, False, "", app_nav=True) is None
    assert claims.unread_state(TAB, False, "", app_nav=False) is not None  # as before, outside app help
    assert claims.unread_state("Oggi hai 3 cose da fare.", False, "", app_nav=True) is not None


def test_the_exact_question_gets_the_tab_not_the_no_read_line(  # type: ignore[no-untyped-def]
    client, db, secrets_key, device, fake_models
) -> None:
    device_id, token = device
    set_key(db, secrets_key)

    async def model(messages: Any, info: Any) -> Any:
        yield TAB

    fake_models.default = model
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run(WHERE)
        app.send(turn)
        reply = said(app.run_ends(turn["input"]["runId"]))
    assert reply == TAB, reply
    assert claims.unread_truth("it-IT") not in reply
