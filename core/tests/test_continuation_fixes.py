"""Fixes at the merge (and the contract row): each fails
without its fix."""

import asyncio
from typing import Any

from pisa.services.session_messages import Utterance
from tests.conftest import pair

# --- M2: a continuation names only the latest utterance -----------------------------------------------------


def test_a_continuation_of_an_older_utterance_never_deletes_a_later_turn(
    client, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import set_key
    from tests.live_app import live

    device_id, token = pair(db)
    set_key(db, secrets_key)
    n = 0

    async def reply(messages, info):  # type: ignore[no-untyped-def]
        nonlocal n
        n += 1
        yield f"Risposta {n}."

    fake_models.default = reply
    with live(client, token, device_id) as app:
        a = Utterance(text="Ricordami il latte", lang="it-IT", final=True, source="phone_mic")
        app.utterance(a)
        list(app.reply())
        b = Utterance(text="Che tempo fa domani", lang="it-IT", final=True, source="phone_mic")
        app.utterance(b)
        list(app.reply())
        c = Utterance(text="e il pane", lang="it-IT", final=True, source="phone_mic", continues=a.id)
        app.utterance(c)
        list(app.reply())
    users = [t for (t,) in db.execute("SELECT content FROM messages WHERE role = 'user' ORDER BY id")]
    assert users[:2] == ["Ricordami il latte", "Che tempo fa domani"]  # B's turn is still there
    assert users[2].startswith("e il pane")  # answered as the new words alone


# --- M1: a cancelled reply still being saved never lands after a continuation's delete ----------------------


def test_drop_after_waits_for_a_cancelled_reply_being_saved() -> None:
    from pisa.services.conversation import ConversationService

    order: list[str] = []

    class Conn:
        async def execute(self, sql: str, *a: Any) -> Any:
            if sql.startswith("DELETE FROM messages"):
                order.append("delete")

            class Cur:
                rowcount = 0

                async def fetchall(self) -> list[Any]:
                    return []

            return Cur()

        def transaction(self) -> Any:  # drop_after deletes what came from the turn in the same transaction
            class Tx:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *a: Any) -> None:
                    return None

            return Tx()

    class Pool:
        def connection(self) -> Any:
            class Ctx:
                async def __aenter__(self) -> Conn:
                    return Conn()

                async def __aexit__(self, *a: Any) -> None:
                    return None

            return Ctx()

    service = ConversationService.__new__(ConversationService)
    service._pool = Pool()  # type: ignore[assignment]
    service._saving = set()

    async def go() -> None:
        async def slow_save() -> None:
            await asyncio.sleep(0.2)
            order.append("save")

        save = asyncio.ensure_future(slow_save())
        service._saving.add(save)
        save.add_done_callback(service._saving.discard)
        await service.drop_after(__import__("uuid").uuid4(), 0)

    asyncio.run(go())
    assert order == ["save", "delete"]


# --- M3: a service to renew by hand is a mid task however it was labelled; a pickup is never low ------------


# --- priority_of: only a reason aimed at Pisa is the email rating itself
# ---------------------------------------


# --- the history: spam in the thread stays out; hidden characters go ----------------------------------------
