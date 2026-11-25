"""`DELETE /v1/conversations/{threadId}` forgets what the server holds in memory for the deleted conversation,
as `POST /v1/conversations/new` does (security review CD-1, ported from E1's PoC `test_e1_v014.py`), and ends
the device's open talk.

Before the fix the delete removed the rows only, not the conversation service's last turn (`continues`), what
Pisa last said, its objects and taps, the reading, the reflex's lines and facts, nor the live socket's words:
the next turn could carry words of the conversation the user deleted. Synthetic data only."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.services import talk
from pisa.services.session_registry import SessionRegistry
from tests.conftest import pair_v2_key, set_key
from tests.owner import owner_at
from tests.test_conversation_delete import signed, thread
from tests.test_live_v2_socket import run
from tests.test_talk_revoked import _open_talk

WORD = "zebrafelino"
DELETE = "/v1/conversations/conversation:{}"


class _Socket:
    """The device's live socket, as `start_fresh` sees it: whether it was told the conversation is new."""

    def __init__(self) -> None:
        self.fresh = 0

    def new_conversation(self) -> None:
        self.fresh += 1


def _short_lived(conversations: Any, device: uuid.UUID) -> dict[str, Any]:
    """What the conversation service holds in memory for the device (CD-1's list)."""
    held = {
        name: getattr(conversations, name).get(device)
        for name in ("_last_turn", "_reply_said", "_previous", "objects", "_voice_read", "_read_last")
    }
    held["taps"] = [k for k in conversations._taps if k[0] == device]
    return {name: value for name, value in held.items() if value}


def test_the_delete_forgets_the_conversation_held_in_memory_and_the_next_turn_carries_none_of_it(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models, monkeypatch: pytest.MonkeyPatch
) -> None:
    device, token, key = pair_v2_key(db, "Phone A")
    set_key(db, secrets_key)
    auth = {"Authorization": f"Bearer {token}"}
    runtime = client.app.state.runtime
    conversations, did = runtime.conversations, uuid.UUID(device)
    socket = _Socket()
    registry = runtime.live
    assert isinstance(registry, SessionRegistry)
    monkeypatch.setattr(registry, "session", lambda d: socket if d == did else None)
    ended: list[uuid.UUID] = []

    async def conversation_deleted(d: uuid.UUID) -> int:
        ended.append(d)
        return 1

    monkeypatch.setattr(runtime.talk, "conversation_deleted", conversation_deleted)

    said: list[str] = []  # the ids of the user's messages

    def ask(words: str, *, continues: bool = False) -> str:
        body = run(words)["input"]
        if continues:  # the app sends it as going on with the turn before (`continues`)
            body["forwardedProps"]["pisa"]["turn"]["continues"] = said[-1]
        said.append(body["messages"][0]["id"])
        answered = client.post("/v2/runs", headers=auth, json=body)
        assert answered.status_code == 200, answered.text
        return str(fake_models.seen_messages[-1])

    ask(f"ricordati che il mio gatto si chiama {WORD}")
    assert WORD in ask("come si chiama il mio gatto adesso?")  # the same conversation: it's there
    # as in E1's PoC: what Pisa last said and the device's last turn, held in memory
    conversations._reply_said[did] = [f"Il tuo gatto si chiama {WORD}."]
    assert _short_lived(conversations, did)
    gone = client.delete(DELETE.format(device), headers=signed(client, auth, device, key, thread(device)))
    assert gone.status_code == 204, gone.text
    assert _short_lived(conversations, did) == {}  # forgotten, as POST /new forgets it
    assert socket.fresh == 1  # the live socket's words said before, forgotten
    assert ended == [did]  # the open talk ended
    # even sent as going on with the deleted conversation's last turn, nothing of it reaches the model
    after = ask("e che tempo fa domani?", continues=True)
    assert WORD not in after and "come si chiama il mio gatto" not in after


def test_a_new_conversation_and_a_delete_forget_the_same_in_memory_state(client, db) -> None:  # type: ignore[no-untyped-def]
    """The two endpoints share one reset (`start_fresh`): what one forgets, the other forgets."""
    device, token, key = pair_v2_key(db, "Phone A")
    auth = {"Authorization": f"Bearer {token}"}
    conversations, did = client.app.state.runtime.conversations, uuid.UUID(device)

    def hold() -> None:
        conversations._reply_said[did] = [f"Il tuo medico è il dottor {WORD}"]
        conversations._previous[did] = object()
        conversations.objects[did] = {"x": object()}
        conversations._taps[(did, WORD)] = object()

    hold()
    assert client.post("/v1/conversations/new", headers=auth).status_code == 200
    assert _short_lived(conversations, did) == {}
    hold()
    gone = client.delete(DELETE.format(device), headers=signed(client, auth, device, key, thread(device)))
    assert gone.status_code == 204, gone.text
    assert _short_lived(conversations, did) == {}


def test_deleting_the_conversation_ends_the_device_s_open_talk_and_no_other() -> None:
    device, other = uuid.uuid4(), uuid.uuid4()

    async def go() -> tuple[Any, ...]:
        service = talk.TalkService(None, None, {}, owner=owner_at(), clock=lambda: datetime.now(UTC))  # type: ignore[arg-type]
        mine, session_, link, seen = await _open_talk(service, device)
        kept, _, kept_link, _ = await _open_talk(service, other)
        assert await service.conversation_deleted(device) == 1
        assert (mine.reason, mine.closed_by, mine.close_detail) == ("owner", "server", "conversation_deleted")
        assert link.closed == 1 and session_.closed == 1  # closed before it returns: stored, then deleted
        assert kept.reason is None and kept_link.closed == 0
        assert device not in service._revoked_devices
        kept.end("owner", "phone")
        await asyncio.gather(*service._run_tasks.values())
        return mine, seen, service

    mine, seen, service = asyncio.run(go())
    assert dict(seen)["after"] == []  # the words being said as it ended are not answered after it
    assert mine.session_id in service._owner_ends
