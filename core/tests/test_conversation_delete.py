"""`DELETE /v1/conversations/{threadId}`: a device deletes its own conversations, signed (op `conversation`);
`POST /v1/conversations/new`: a new one, nothing deleted.

What goes: the device's messages and what came from them, its REST turns, its talks. What stays: tasks,
reminders, Memoria, another device's conversation. Synthetic data only."""

import base64
import uuid
from datetime import UTC, datetime
from typing import Any, LiteralString

import psycopg
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from tests.conftest import pair_v2_key
from tests.phone_signing import challenge, new_key, sign

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def thread(device_id: str) -> str:
    return f"conversation:{device_id}"


def signed(client: Any, auth: dict[str, str], device_id: str, key: Any, target: str) -> dict[str, str]:
    """The headers of the delete, signed by the device's own key over its `device` challenge."""
    c = challenge(client, auth, action="device", op="conversation", target=target)
    signature = base64.b64encode(key.sign(c["canonical"].encode(), ec.ECDSA(hashes.SHA256()))).decode()
    return auth | {
        "X-Pisa-Nonce": c["nonce"],
        "X-Pisa-Signature": signature,
        "X-Pisa-Key-Id": f"device-{device_id}",
    }


def talked(db: psycopg.Connection, device_id: str, words: str) -> dict[str, Any]:
    """A conversation of this device's: a message with a topic it opened, a REST turn, a talk with a turn."""
    conversation = uuid.uuid4()
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, device_id, NOW, NOW),
    )
    (message,) = db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) "
        "VALUES (%s, 'user', %s, 'it-IT', %s) RETURNING id",
        (conversation, words, NOW),
    ).fetchone() or (None,)
    db.execute(
        "INSERT INTO pisa_topics (id, message_id, name, opened_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
        (uuid.uuid4(), message, f"topic {words}", NOW, NOW),
    )
    turn = uuid.uuid4()
    db.execute(
        "INSERT INTO turns (id, device_id, client_turn_id, origin, lang, state, say, created_at) "
        "VALUES (%s, %s, %s, 'phone', 'it-IT', 'done', %s, %s)",
        (turn, device_id, uuid.uuid4(), f"risposta {words}", NOW),
    )
    talk = uuid.uuid4()
    db.execute(
        "INSERT INTO talk_sessions (id, device_id, talker, voice, trigger, lang, started_at) "
        "VALUES (%s, %s, 'xai', 'v', 'press', 'it-IT', %s)",
        (talk, device_id, NOW),
    )
    db.execute("INSERT INTO talk_turns (session_id, n, owner_end_at) VALUES (%s, 1, %s)", (talk, NOW))
    return {"conversation": conversation, "message": message, "turn": turn, "talk": talk}


def count(db: psycopg.Connection, sql: LiteralString, *params: Any) -> int:
    row = db.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def what_is_left(db: psycopg.Connection, device_id: str) -> dict[str, int]:
    return {
        "conversations": count(db, "SELECT count(*) FROM conversations WHERE device_id = %s", device_id),
        "messages": count(
            db,
            "SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.device_id = %s",
            device_id,
        ),
        "turns": count(db, "SELECT count(*) FROM turns WHERE device_id = %s", device_id),
        "talks": count(db, "SELECT count(*) FROM talk_sessions WHERE device_id = %s", device_id),
    }


def kept(db: psycopg.Connection) -> None:
    """What the user asked Pisa to keep: a task, a reminder, a Memoria instruction."""
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, source_kind, title_key, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Comprare il pane', 'open', 'none', 'voice', 'comprare pane', %s, %s)",
        (NOW, NOW),
    )
    db.execute(
        "INSERT INTO pisa_promises (id, what, condition, due_at, lang, provenance, created_at) "
        "VALUES (gen_random_uuid(), 'chiamare il fornaio', 'time', %s, 'it-IT', 'owner_words', %s)",
        (NOW, NOW),
    )
    db.execute("INSERT INTO owner_instructions (text, source) VALUES ('rispondi breve', 'app')")


def test_the_owner_device_deletes_its_conversation_and_what_came_from_it(client, db) -> None:  # type: ignore[no-untyped-def]
    device, token, key = pair_v2_key(db, "Phone A")
    other, _, _ = pair_v2_key(db, "Phone B")
    auth = {"Authorization": f"Bearer {token}"}
    mine, theirs = talked(db, device, "zebrapane"), talked(db, other, "gnuformaggio")
    kept(db)
    url = f"/v1/conversations/{thread(device)}"
    gone = client.delete(url, headers=signed(client, auth, device, key, thread(device)))
    assert gone.status_code == 204, gone.text
    assert what_is_left(db, device) == {"conversations": 0, "messages": 0, "turns": 0, "talks": 0}
    assert count(db, "SELECT count(*) FROM talk_turns WHERE session_id = %s", mine["talk"]) == 0
    assert count(db, "SELECT count(*) FROM pisa_topics WHERE name = 'topic zebrapane'") == 0  # derived: gone
    # another device's conversation stays whole, and what they asked to keep stays
    assert what_is_left(db, other) == {"conversations": 1, "messages": 1, "turns": 1, "talks": 1}
    assert count(db, "SELECT count(*) FROM pisa_topics WHERE message_id = %s", theirs["message"]) == 1
    assert count(db, "SELECT count(*) FROM tasks") == 1
    assert count(db, "SELECT count(*) FROM pisa_promises") == 1
    assert count(db, "SELECT count(*) FROM owner_instructions") == 1
    # idempotent: deleting it again is a 204 too, nothing more goes
    again = client.delete(url, headers=signed(client, auth, device, key, thread(device)))
    assert again.status_code == 204 and what_is_left(db, other)["messages"] == 1


def test_another_device_s_conversation_is_403_and_an_unknown_one_404(client, db) -> None:  # type: ignore[no-untyped-def]
    device, token, key = pair_v2_key(db, "Phone A")
    other, _, _ = pair_v2_key(db, "Phone B")
    auth = {"Authorization": f"Bearer {token}"}
    talked(db, other, "gnuformaggio")
    theirs = thread(other)
    refused = client.delete(f"/v1/conversations/{theirs}", headers=signed(client, auth, device, key, theirs))
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "other_device"
    assert what_is_left(db, other)["messages"] == 1
    for unknown in (thread(str(uuid.uuid4())), "chat-1", "conversation:not-a-uuid", "pisa-notices:x"):
        got = client.delete(f"/v1/conversations/{unknown}", headers=auth)
        assert got.status_code == 404 and got.json()["error"]["code"] == "not_found", unknown


def test_unsigned_badly_signed_or_replayed_is_refused_and_nothing_goes(client, db) -> None:  # type: ignore[no-untyped-def]
    device, token, key = pair_v2_key(db, "Phone A")
    auth = {"Authorization": f"Bearer {token}"}
    talked(db, device, "zebrapane")
    url = f"/v1/conversations/{thread(device)}"
    assert client.delete(url, headers=auth).status_code == 403  # always signed, a fresh install too
    c = challenge(client, auth, action="device", op="conversation", target=thread(device))
    forged = auth | {"X-Pisa-Nonce": c["nonce"], "X-Pisa-Signature": sign(new_key()[0], c["canonical"]),
                     "X-Pisa-Key-Id": f"device-{device}"}  # fmt: skip
    assert client.delete(url, headers=forged).status_code == 403  # another key's signature
    other_target = signed(client, auth, device, key, thread(str(uuid.uuid4())))
    assert client.delete(url, headers=other_target).status_code == 403  # signed for another thread
    assert what_is_left(db, device)["messages"] == 1
    good = signed(client, auth, device, key, thread(device))
    assert client.delete(url, headers=good).status_code == 204
    talked(db, device, "nuovo")
    assert client.delete(url, headers=good).status_code == 403  # replayed: the nonce is spent
    assert what_is_left(db, device)["messages"] == 1


def test_a_new_conversation_starts_fresh_for_the_model_and_deletes_nothing(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from tests.conftest import pair_v2, set_key
    from tests.test_live_v2_socket import run

    device, token = pair_v2(db)
    set_key(db, secrets_key)
    auth = {"Authorization": f"Bearer {token}"}

    def ask(words: str) -> str:
        answered = client.post("/v2/runs", headers=auth, json=run(words)["input"])
        assert answered.status_code == 200, answered.text
        return str(fake_models.seen_messages[-1])

    ask("ricordati che il mio gatto si chiama zebrafelino")
    assert "zebrafelino" in ask("come si chiama il mio gatto adesso?")  # the same conversation: it's there
    kept(db)
    made = client.post("/v1/conversations/new", headers=auth)
    assert made.status_code == 200 and made.json()["thread_id"] == thread(device)
    again = client.post("/v1/conversations/new", headers=auth)
    assert again.json()["conversation_id"] == made.json()["conversation_id"]  # twice: the same fresh one
    after = ask("parliamo d'altro: che tempo fa domani?")
    assert "zebrafelino" not in after and "come si chiama il mio gatto" not in after
    # nothing deleted: the earlier conversation's messages, the task, the reminder, Memoria
    assert what_is_left(db, device)["messages"] == 6
    assert count(db, "SELECT count(*) FROM conversations WHERE device_id = %s", device) == 2
    assert count(db, "SELECT count(*) FROM tasks") == 1
    assert count(db, "SELECT count(*) FROM pisa_promises") == 1
    assert count(db, "SELECT count(*) FROM owner_instructions") == 1
