"""Revoking a device ends its open voice talk at once (security review TK-1).

- The revoke (`SessionRegistry.revoke`, reached by DELETE /v1/devices/{id}, `pisa pair --review --revoke`
  and the device revoke API, each through the revocation's NOTIFY) closes the device's `/v2/live` sockets and
  runs, and now its open talk over WebRTC too: the peer and the talker session closed, no last final awaited,
  no last-words answer (as for the owner's own end), ids only in the log.
- A talk whose device was revoked while nobody listened ends when the listener catches up (`close_revoked`).
- Pisa's closed peer takes no further frame from the phone's data channel.

Synthetic words only."""

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from pisa.api import talk_channel
from pisa.ports.talker import OwnerWords
from pisa.services import talk
from pisa.services.session_registry import SessionRegistry
from pisa.services.talk_media import MediaPeer
from tests.owner import owner_at
from tests.test_live_talk import _run, _until

SAID = "e ricordami di chiamare Marco domani"


def _service(pool: Any = None) -> talk.TalkService:
    return talk.TalkService(pool, None, {}, owner=owner_at(), clock=lambda: datetime.now(UTC))  # type: ignore[arg-type]


async def _open_talk(service: talk.TalkService, device: uuid.UUID) -> tuple[Any, Any, Any, list[Any]]:
    """A talk of `device`'s open on `service`, the user mid-sentence (their partial words heard)."""
    run, session_, link, seen = _run()
    service.runs[run.session_id] = run
    service._device_of[run.session_id] = device  # pyright: ignore[reportPrivateUsage]
    after: list[str] = []
    run.hooks.after = lambda words, t: after.append(words)

    async def runs() -> None:  # as `TalkService._run` holds it
        await run.run()

    task = asyncio.create_task(runs())
    service._run_tasks[run.session_id] = task  # pyright: ignore[reportPrivateUsage]
    session_.queue.put_nowait(OwnerWords(SAID, final=False))
    await _until(lambda: any(e["kind"] == "owner_words" for e in run.timeline.events))
    seen.append(("after", after))
    return run, session_, link, seen


def test_revoking_a_device_ends_its_open_talk_at_once(capsys: pytest.CaptureFixture[str]) -> None:
    device, other = uuid.uuid4(), uuid.uuid4()

    async def go() -> tuple[Any, ...]:
        service = _service()
        registry = SessionRegistry()
        registry.talk = service
        run, session_, link, seen = await _open_talk(service, device)
        kept, kept_session, kept_link, _ = await _open_talk(service, other)  # another device's talk stays
        await registry.revoke(device)  # the revoke paths' one step
        assert run.reason == "owner" and run.closed_by == "server" and run.close_detail == "device_revoked"
        assert link.closed == 1 and session_.closed == 1  # the peer and the talker, closed before it returns
        assert kept.reason is None and kept_link.closed == 0 and kept_session.closed == 0
        # a frame the phone still sends is taken by nothing: the run's parts are stopped
        link.messages.put_nowait({"type": "talk.text", "text": "un'altra cosa"})
        await asyncio.sleep(0.1)
        kept.end("owner", "phone")
        await asyncio.gather(*service._run_tasks.values())  # pyright: ignore[reportPrivateUsage]
        return run, session_, link, seen, service

    run, session_, link, seen, service = asyncio.run(go())
    after = dict(seen)["after"]
    assert after == []  # no last-words answer
    assert session_.ended_turns == 0  # no last final awaited
    assert session_.texts == [] and not any(name == "think" for name, *_ in seen)
    assert link.sent[-1] == {"type": "talk.ended", "reason": "owner"}
    assert run.session_id in service._owner_ends  # pyright: ignore[reportPrivateUsage]
    logged = capsys.readouterr().out
    assert SAID not in logged and "un'altra cosa" not in logged
    assert "talk_revoked" in logged


def test_the_revoked_talk_answers_no_last_words_after_it(db) -> None:  # type: ignore[no-untyped-def]
    """Through the service's own `_after`: the revoked talk counts as the owner's end."""
    from tests.conftest import pair
    from tests.test_talk_heard_once import _notices, _pool

    device = uuid.UUID(pair(db)[0])

    async def go() -> None:
        pool = await _pool(db)
        service = _service(pool)
        run, _, _, _ = await _open_talk(service, device)
        await service.revoked(device)
        service._after(device, "it-IT", run.hooks.think, SAID, None, run=run)  # pyright: ignore[reportPrivateUsage]
        await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
        await pool.close()

    asyncio.run(go())
    assert _notices(db) == []


def test_a_talk_revoked_while_nobody_listened_ends_when_the_listener_catches_up(db) -> None:  # type: ignore[no-untyped-def]
    import psycopg

    from pisa.services.devices import revoke_device
    from tests.conftest import dsn, pair

    device = uuid.UUID(pair(db)[0])

    async def go() -> Any:
        service = _service()
        registry = SessionRegistry()
        registry.talk = service
        run, _, link, _ = await _open_talk(service, device)
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await revoke_device(conn, device)  # as DELETE /v1/devices/{id} and `pisa pair --review --revoke`
            await registry.close_revoked(conn)
        return run, link

    run, link = asyncio.run(go())
    assert run.closed_by == "server" and link.closed == 1


class _Channel:
    """The phone's data channel as Pisa's peer sees it (aiortc's event API, no network)."""

    label = "pisa"
    protocol = "pisa.v2"
    readyState = "open"

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., Any]] = {}
        self.sent: list[str] = []

    def on(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def add(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.handlers[name] = fn
            return fn

        return add

    def send(self, text: str) -> None:
        self.sent.append(text)


def test_a_closed_peer_takes_no_further_frame_from_the_phone() -> None:
    frame = json.dumps({"op": "custom", "name": "pisa.presence", "value": {"state": {"route": "speaker"}}})

    async def go() -> tuple[list[Any], list[Any]]:
        peer = MediaPeer([], 16_000, talk_channel.talk_dialects(str(uuid.uuid4()), "conversation:x", "it-IT"))
        channel = _Channel()
        peer._pc.emit("datachannel", channel)  # pyright: ignore[reportPrivateUsage]
        channel.handlers["message"](frame)
        before = [peer.messages.get_nowait() for _ in range(peer.messages.qsize())]
        await peer.close()
        channel.handlers["message"](frame)  # a late frame after the revoke closed the peer
        after = [peer.messages.get_nowait() for _ in range(peer.messages.qsize())]
        return before, after

    before, after = asyncio.run(go())
    assert before == [{"type": "talk.route", "route": "speaker"}]
    assert after == []


@pytest.mark.parametrize("committed", [True, False])
def test_a_talk_still_opening_when_its_device_is_revoked_ends_once_it_opens(
    db, secrets_key, committed
) -> None:  # type: ignore[no-untyped-def]
    """E1 (v0.1.2): the revoke came while the talker session was opening, so `TalkService.revoked` found no
    talk of the device's to end, and the talk went on. Once registered, the device is checked again (its row,
    or a revoke `revoked` already ran) and the talk ends at once, as a revoked one."""
    import psycopg
    from pydantic import SecretStr

    from pisa.domain.whisper import Kind
    from pisa.infra.keys import KeyStore
    from pisa.ports.talker import TalkerConfig
    from pisa.services.devices import revoke_device
    from pisa.services.talk import Thought
    from tests.conftest import FAKE_KEY, dsn, pair, talk_naturally
    from tests.test_live_talk import FakeLink, FakeSession, FakeTalker
    from tests.test_live_talk import _service as live_service

    device = uuid.UUID(pair(db)[0])
    talk_naturally(db)
    holder: dict[str, Any] = {}

    class _RevokedWhileOpening(FakeTalker):
        async def open(self, cfg: TalkerConfig) -> FakeSession:
            if committed:  # the revoke committed; its notice not yet handled
                async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
                    await revoke_device(conn, device)
            else:  # the revoke's notice handled, its row not read again (the in-memory path alone)
                assert await holder["service"].revoked(device) == 0  # nothing open yet to end
            return await super().open(cfg)

    talker = _RevokedWhileOpening()
    make, service_for = live_service(db, talker, datetime.now(UTC))

    async def go() -> Any:
        pool = await make()
        async with pool.connection() as conn:
            await KeyStore(SecretStr(secrets_key)).set(conn, "xai", FAKE_KEY)
        service = service_for(pool, SecretStr(secrets_key))
        holder["service"] = service

        async def link_for(rate: int) -> Any:
            return FakeLink()

        async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
            return Thought("Ok.", Kind.SAY)

        session_id, link = await service.open(
            device, trigger="press", lang="it-IT", link_for=link_for, think=think
        )
        run = service.runs.get(session_id)
        await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
        await pool.close()
        return run, link, session_id, service

    run, link, session_id, service = asyncio.run(go())
    assert run.reason == "owner" and run.closed_by == "server" and run.close_detail == "device_revoked"
    assert link.closed == 1 and talker.sessions[-1].closed == 1
    assert session_id in service._owner_ends  # pyright: ignore[reportPrivateUsage]
