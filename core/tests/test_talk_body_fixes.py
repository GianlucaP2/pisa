"""One test per finding, each failing with its fix undone."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pisa.domain.whisper import Fresh, Kind
from pisa.ports.talker import OwnerSpeaking, OwnerWords, PisaWords, Speaking
from pisa.services.talk import TAP_GONE, Opening, TalkScreen, Thought
from tests.test_live_talk import FakeLink, FakeTalker, _run, _service, _until
from tests.virtual_time import run_talk

READ_BACK = (
    "Ecco la risposta a Marco: «Ciao Marco, domani alle 10 va benissimo. Porto io i documenti del contratto, "
    "a presto.» Lo mando?"
)


def _said(session: Any, text: str) -> None:
    """Pisa's voice says `text`, then the user stops and says "sì", and the talker answers it (said to
    Pisa)."""
    session.queue.put_nowait(Speaking(True))
    session.queue.put_nowait(PisaWords(text, final=True))
    session.queue.put_nowait(Speaking(False))


def _yes(session: Any) -> None:
    session.queue.put_nowait(OwnerSpeaking(False))
    session.queue.put_nowait(Speaking(True))
    session.queue.put_nowait(OwnerWords("sì", final=True))


# --- 1. HIGH: Pisa's question is never shortened, and a yes answers only what Pisa said whole
# ---------------------


def test_1_on_airpods_a_read_back_is_said_whole_and_the_owner_s_yes_answers_it() -> None:
    from pisa.services.talk_turns import thought_of

    run, session, link, _ = _run()
    run.route = "airpods"
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    run.screen.in_turn = 1  # the turn showed the draft too
    asked: list[tuple[str, bool]] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        asked.append((words, yes_by_code))
        if len(asked) == 1:
            return thought_of(READ_BACK, Kind.ASK, run, "it-IT", asks=True, claimed=False)
        return Thought("Mandata a Marco.", Kind.DONE)

    run.hooks.think = think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("rispondi a Marco che domani alle 10 va bene", final=True))
        await _until(lambda: session.whispers)
        _said(session, session.whispers[-1].text)  # Pisa's voice says what it was given
        _yes(session)
        await _until(lambda: len(asked) == 2)
        run.end("owner")
        await task

    run_talk(go())
    given = next(w for w in session.whispers if w.kind is Kind.ASK)
    assert given.text == READ_BACK  # the whole read-back and its question, never one sentence of it
    assert asked[1] == ("sì", True)  # Pisa said the question aloud: the yes is decided by code
    # a reply that only asks the user (a read-back of a send asked before) is said whole too
    run.screen.in_turn = 0
    assert thought_of(READ_BACK, Kind.SAY, run, "it-IT", asks=True, claimed=False).text == READ_BACK


def test_1_a_shortened_question_never_lets_a_spoken_yes_decide() -> None:
    run, session, _, _ = _run()
    thoughts = iter(
        [
            Thought("Mando la mail a Marco?", Kind.ASK),  # said whole and aloud
            Thought("Ecco la risposta a Nino: «Ciao Nino.»", Kind.ASK, shortened=True),  # its question cut
            Thought("Dimmi tu.", Kind.SAY),
        ]
    )
    asked: list[tuple[str, bool]] = []

    async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
        asked.append((words, yes_by_code))
        return next(thoughts)

    run.hooks.think = think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("scrivi a Marco che arrivo", final=True))
        await _until(lambda: len(session.whispers) == 1)
        _said(session, "Mando la mail a Marco?")
        session.queue.put_nowait(OwnerWords("rispondi anche a Nino", final=True))
        await _until(lambda: len(asked) == 2 and len(session.whispers) == 2)
        _said(session, "Ecco la risposta a Nino: «Ciao Nino.»")
        _yes(session)
        await _until(lambda: len(asked) == 3)
        run.end("owner")
        await task

    run_talk(go())
    # the newest question was never heard whole: the yes goes to the model, which asks again
    assert asked[2] == ("sì", False)


def test_1_a_turn_that_asked_a_yes_or_read_a_draft_back_asks_the_owner(client) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.conversation import TurnState

    conversations = client.app.state.runtime.conversations
    device = uuid.uuid4()
    assert not conversations.asks_owner(device)
    turn = TurnState(uuid.uuid4(), "rispondi a Marco")
    conversations._last_turn[device] = turn  # pyright: ignore[reportPrivateUsage]
    assert not conversations.asks_owner(device)
    turn.tools.append("draft_reply")
    assert conversations.asks_owner(device)
    turn.tools.clear()
    turn.approvals.append(str(uuid.uuid4()))
    assert conversations.asks_owner(device)


# --- 2. MED: "what's next" is the whole short question only -------------------------------------------------


def test_2_what_s_next_is_the_whole_question_never_another_request() -> None:
    from pisa.domain.talk import asks_next

    for words in (
        "cosa c'è dopo la palestra?",
        "what's next week?",
        "what's next for the project?",
        "sposta il prossimo appuntamento alle 5",
        "cancella il prossimo impegno",
        "e adesso cosa facciamo con Marco?",
        "qual è il prossimo treno per Milano?",
        "what's next on my list tomorrow?",
    ):
        assert not asks_next(words), words
    for words in (
        "Pisa, cosa c'è dopo?",
        "e dopo?",
        "What's next?",
        "cosa ho dopo?",
        "Ok, qual è il prossimo impegno?",
        "allora, e adesso?",
        "what's next on my plan today?",
        "Pisa, what" + "\u2019" + "s my next?",  # the transcript's curly apostrophe
    ):
        assert asks_next(words), words


# --- 3. MED: the next item is never a skipped one, and with no plan the brain answers -----------------------


def test_3_a_skipped_item_is_no_next_and_no_plan_is_no_answer(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from zoneinfo import ZoneInfo

    from pydantic import SecretStr

    from tests.conftest import pair

    device = uuid.UUID(pair(db)[0])
    now = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    make, service_for = _service(db, FakeTalker(), now)

    async def read() -> Fresh:
        pool = await make()
        try:
            service = service_for(pool, SecretStr(secrets_key))
            async with pool.connection() as conn:
                return await service.fresh(conn, device, "it-IT")
        finally:
            await pool.close()

    none = asyncio.run(read())
    assert not none.plan and none.next_item == ""
    day = now.astimezone(ZoneInfo("Europe/Rome")).date()
    items = [
        {
            "kind": "event",
            "title": "Riunione",
            "start": (now + timedelta(hours=1)).isoformat(),
            "status": "skipped",
        },
        {"kind": "task", "title": "Spesa", "start": (now + timedelta(hours=2)).isoformat(), "status": "done"},
        {"kind": "event", "title": "Dentista", "start": (now + timedelta(hours=3)).isoformat()},
    ]
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) "
        "VALUES (gen_random_uuid(), %s, %s, 'test', 'f', %s)",
        (day, now, json.dumps({"items": items})),
    )
    fresh = asyncio.run(read())
    assert fresh.plan and fresh.next_item == "Dentista alle 13:00"


def test_3_with_no_plan_what_s_next_has_no_fast_line() -> None:
    run, session, _, seen = _run(Thought("Domani alle 9 hai il dentista.", Kind.SAY))

    async def fresh() -> Fresh:
        return Fresh("adesso")  # no plan row: "nothing else today" may be false

    run.hooks.fresh = fresh

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("Pisa, cosa ho dopo?", final=True))
        await _until(lambda: any(s[0] == "think" for s in seen) and session.whispers)
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert not [w for w in session.whispers if w.id.startswith("next")]
    assert [w.text for w in session.whispers if w.id.startswith("w")] == ["Domani alle 9 hai il dentista."]


# --- 4. MED: a talk opens with Pisa's asked line only within 10 minutes
# ----------------------------------------


def test_4_an_old_ask_opens_an_ordinary_talk(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    runtime = client.app.state.runtime
    device_id = pair(db)[0]
    now = runtime.clock()

    def ask(age: timedelta) -> uuid.UUID:
        nid, ask_id = uuid.uuid4(), uuid.uuid4()
        db.execute(
            "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, queued_at) "
            "VALUES (%s, 'reminder', 'Esci adesso per il dentista.', 'it-IT', 'urgent', %s, %s, %s)",
            (nid, device_id, now - age, now - age),
        )
        db.execute(
            "INSERT INTO talk_asks (id, device_id, notification_id, why, asked_at) "
            "VALUES (%s, %s, %s, 'leave_now', %s)",
            (ask_id, device_id, nid, now - age),
        )
        return ask_id

    old, recent = ask(timedelta(minutes=11)), ask(timedelta(minutes=2))
    device = uuid.UUID(device_id)
    assert client.portal.call(runtime.talk.opened_for, device, old) == ""
    assert db.execute("SELECT opened_at FROM talk_asks WHERE id = %s", (old,)).fetchone() == (None,)
    assert client.portal.call(runtime.talk.opened_for, device, recent) == "Esci adesso per il dentista."


# --- 5. MED: a route change mid-talk is stored on the session -----------------------------------------------


def test_5_airpods_taken_out_mid_talk_no_longer_count_as_worn(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore
    from tests.conftest import FAKE_KEY, pair, talk_naturally

    device = uuid.UUID(pair(db)[0])
    talk_naturally(db)  # with Grok Voice, their consent covering it
    now = datetime.now(UTC)
    talker = FakeTalker()
    make, service_for = _service(db, talker, now)

    async def go() -> Any:
        pool = await make()
        try:
            async with pool.connection() as conn:
                await KeyStore(SecretStr(secrets_key)).set(conn, "xai", FAKE_KEY)
            service = service_for(pool, SecretStr(secrets_key))
            link = FakeLink()

            async def link_for(rate: int) -> Any:
                return link

            async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
                return Thought("Ok.", Kind.SAY)

            session_id, _ = await service.open(
                device, trigger="press", lang="it-IT", link_for=link_for, think=think,
                opening=Opening(route="airpods"),
            )  # fmt: skip
            link.messages.put_nowait({"type": "talk.route", "route": "speaker"})  # the AirPods came out
            for _ in range(200):
                async with pool.connection() as conn:
                    row = await (
                        await conn.execute("SELECT route FROM talk_sessions WHERE id = %s", (session_id,))
                    ).fetchone()
                if row == ("speaker",):
                    break
                await asyncio.sleep(0.01)
            asked = await service.may_ask(device, uuid.uuid4(), "leave_now", "urgent")
            assert service.end(session_id, device)
            await asyncio.gather(*service._tasks)  # pyright: ignore[reportPrivateUsage]
            return row, asked
        finally:
            await pool.close()

    row, asked = asyncio.run(go())
    assert row == ("speaker",)
    assert asked is None  # nothing worn and the talk is on the speaker now: Pisa doesn't ask to open


# --- 6. MED: a newer phone's POST never falls to economy ----------------------------------------------------


def test_6_a_newer_phone_s_field_or_route_is_ignored_and_the_trigger_stays_strict() -> None:
    from pydantic import ValidationError
    from structlog.testing import capture_logs

    from pisa.api.schemas import TalkSessionCreate

    with capture_logs() as logs:
        body = TalkSessionCreate.model_validate(
            {"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT", "route": "hologram", "mood": "calm"}
        )
    assert body.route is None
    ignored = [e for e in logs if e.get("event") == "talk_session_fields_ignored"]
    assert ignored and ignored[0]["fields"] == ["mood"] and ignored[0]["log_level"] == "info"
    assert "calm" not in str(ignored)  # names only
    assert (
        TalkSessionCreate.model_validate(
            {"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT", "route": "car"}
        ).route
        == "car"
    )
    with pytest.raises(ValidationError):
        TalkSessionCreate.model_validate({"sdp_offer": "v=0", "trigger": "telepathy", "lang": "it-IT"})


# --- 7. talk.ask_to_open names the item's own notify --------------------------------------------------------


def test_7_ask_to_open_carries_its_notification_id_and_is_valid_contract_2(  # type: ignore[no-untyped-def]
    client, db, secrets_key
) -> None:
    from pisa.services.notifications import Dispatcher
    from tests.conftest import pair, set_key, talk_naturally
    from tests.live_app import live
    from tests.test_talk_body import _notice, _wearing

    set_key(db, secrets_key, "xai")
    device_id, token = pair(db)
    talk_naturally(db)  # with Grok Voice, their consent covering it: Pisa may ask to open a talk
    runtime = client.app.state.runtime
    with live(client, token, device_id) as app:
        _wearing(db, device_id)
        from tests.conftest import pause_outbox

        pause_outbox(client)  # this test's dispatcher is the only one delivering
        dispatcher = Dispatcher(
            runtime.pool, runtime.live, runtime.push, clock=runtime.clock, owner=runtime.owner
        )
        nid = _notice(db, device_id)
        client.portal.call(dispatcher.deliver, uuid.UUID(nid))
        ask = app.next()
        notify = app.next()
        app.ping()
        app.check()  # contract 2's schemas, as the kit checks them
    assert ask["type"] == "talk.ask_to_open" and ask["notification_id"] == nid == notify["notification_id"]
    asks = [f["event"]["value"] for f in app.frames if f.get("event", {}).get("name") == "pisa.ask_to_open"]
    (wire,) = asks
    assert wire["noticeId"] == nid


# --- 8. LOW: taps -------------------------------------------------------------------------------------------


def _choice(show_id: str, revision: int = 0) -> dict[str, Any]:
    return {
        "present_id": show_id,
        "purpose": "Scegli",
        "focus": "take",
        "object": {
            "object_id": "choice:c1",
            "kind": "choice",
            "revision": revision,
            "data": {
                "question": "Dove?",
                "options": [{"id": "o1", "label": "Bar"}, {"id": "o2", "label": "Porto"}],
            },
        },
    }


def _tapping() -> tuple[Any, Any, list[tuple[str, str]], list[Any]]:
    run, session, link, seen = _run()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    taps: list[tuple[str, str]] = []

    async def chosen(object_id: str, option: str) -> str | None:
        """As conversations.object_event: words only for a choice Pisa showed this device and still open."""
        taps.append((object_id, option))
        return f"[tap {object_id}] {option}" if object_id in ("choice:c1", "choice:before") else None

    run.hooks.chosen = chosen
    return run, session, taps, seen


def _thoughts(seen: list[Any]) -> list[str]:
    return [s[1] for s in seen if s[0] == "think"]


def test_8a_a_tap_on_a_choice_shown_before_the_talk_is_the_owner_s_words() -> None:
    run, session, taps, seen = _tapping()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        run.link.messages.put_nowait({"type": "talk.choose", "id": "choice:before", "choice": "o2"})
        await _until(lambda: _thoughts(seen))
        run.end("owner")
        await task

    run_talk(go())
    assert taps == [("choice:before", "o2")]
    assert _thoughts(seen) == ["[tap choice:before] o2"]
    assert not [w for w in session.whispers if w.id == "tap"]


def test_8b_a_dismissed_show_or_an_older_revision_answers_nothing() -> None:
    run, session, _, seen = _tapping()
    screen = run.screen

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await screen.present(_choice("s1"))
        await screen.present(_choice("s2", revision=1))  # a newer revision replaces s1
        run.link.messages.put_nowait({"type": "talk.choose", "id": "s1", "choice": "o1"})
        await _until(lambda: sum(1 for w in session.whispers if w.id == "tap") == 1)
        assert await screen.close("choice:c1")  # dismissed
        run.link.messages.put_nowait({"type": "talk.choose", "id": "s2", "choice": "o1"})
        await _until(lambda: sum(1 for w in session.whispers if w.id == "tap") == 2)
        run.end("owner")
        await task

    run_talk(go())
    assert not _thoughts(seen)
    assert {w.text for w in session.whispers if w.id == "tap"} == {TAP_GONE["it-IT"]}


def test_8c_a_show_s_choice_is_answered_once() -> None:
    run, session, _, seen = _tapping()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.screen.present(_choice("s1"))
        run.link.messages.put_nowait({"type": "talk.choose", "id": "s1", "choice": "o1"})
        await _until(lambda: _thoughts(seen))
        run.link.messages.put_nowait({"type": "talk.choose", "id": "s1", "choice": "o2"})
        await _until(lambda: any(w.id == "tap" for w in session.whispers))
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    assert _thoughts(seen) == ["[tap choice:c1] o1"]  # no second "[tap] o2" words
    assert [w.text for w in session.whispers if w.id == "tap"] == [TAP_GONE["it-IT"]]


# --- 9. LOW: Pisa's words asked back are the user's
# -----------------------------------------------------------


def test_9_a_question_is_never_an_echo() -> None:
    run, session, link, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(PisaWords("Il dentista è alle due e trentadue.", final=True))
        await asyncio.sleep(0.05)
        session.queue.put_nowait(OwnerWords("alle 2:32?", final=True))  # the user asks it back
        await _until(lambda: _thoughts(seen))
        run.end("owner")
        await task

    run_talk(go())
    assert _thoughts(seen) == ["alle 2:32?"]
    assert not [m for m in link.sent if m.get("ignored")]


# --- 10. LOW: placeholders ----------------------------------------------------------------------------------


def test_10_brackets_only_as_a_short_token_optional_args_absent_and_more_content_checked() -> None:
    from pisa.domain.placeholders import absent, junk, placeholder

    for value in ("<title>", "{}", "[x]", "<event_title>"):
        assert placeholder(value), value
    for value in ("[Lavoro] Riunione [urgente]", "<p>Ciao Marco,</p><p>a domani.</p>", "{Marco} e {Nino}"):
        assert not placeholder(value), value
    # an optional argument left empty or "none" is the model omitting it: absent, never refused
    assert junk({"title": "Cena", "start": "2026-11-12T20:00", "owner_said": "none"}, "create_event") is None
    assert absent({"title": "Cena", "owner_said": "none"}, "create_event") == {
        "title": "Cena",
        "owner_said": None,
    }
    assert junk({"when": "fits", "what": "", "place_id": "p1"}, "plan_stop") is None
    assert absent({"when": "fits", "what": ""}, "plan_stop")["what"] is None
    assert junk({"owner_said": "none", "approval": "a1"}, "answer_proposal") == "owner_said"  # required there
    assert (
        junk({"kind": "like", "statement": "dummy", "quote": "mi piace"}, "remember_about_me") == "statement"
    )
    assert junk({"kind": "like", "statement": "Ama il mare", "quote": "N/A"}, "remember_about_me") == "quote"
    assert junk({"task_ref": "spesa", "notes": "PLACEHOLDER"}, "set_task_notes") == "notes"
    assert junk({"topic": "<topic>"}, "never_learn_about") == "topic"


def test_10_the_rail_runs_a_write_with_an_omitted_optional_argument_as_absent() -> None:
    from pydantic_ai.messages import ToolCallPart

    from pisa.brain.receipts import NoPlaceholders

    ran: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> str:
        ran.append(args)
        return "ok"

    class Ctx:
        messages: tuple[Any, ...] = ()
        run_step = 1

    call = ToolCallPart(tool_name="create_event", args={})
    args = {"title": "Cena", "start": "2026-11-12T20:00", "owner_said": "none"}
    out = run_talk(
        NoPlaceholders().wrap_tool_execute(Ctx(), call=call, tool_def=None, args=args, handler=handler)  # type: ignore[arg-type]
    )
    assert out == "ok" and ran == [{"title": "Cena", "start": "2026-11-12T20:00", "owner_said": None}]


# --- 11. test gap: send words with two questions open decide nothing ----------------------------------------


def test_11_mandala_with_an_email_and_a_calendar_approval_pending_decides_nothing(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    runtime = client.app.state.runtime
    device = uuid.UUID(pair(db)[0])

    async def make(kind: str, summary: str, payload: dict[str, Any]) -> str:
        view = await runtime.approvals.create(kind, summary, ["x"], payload, device_id=device)
        return str(view["id"])

    email = client.portal.call(make, "email_send", "Mando la mail a Marco?", {"draft_id": str(uuid.uuid4())})
    calendar = client.portal.call(make, "calendar_cancel", "Cancello la riunione?", {"title": "Riunione"})
    # oldest first: the email's question, then the calendar's
    db.execute("UPDATE approvals SET created_at = created_at - interval '1 minute' WHERE id = %s", (email,))
    said = client.portal.call(runtime.actions.answer, device, "mandala", "it-IT", [email, calendar])
    assert said is None  # the model reads it
    rows = db.execute("SELECT status FROM approvals WHERE id = ANY(%s)", ([email, calendar],)).fetchall()
    assert rows == [("pending",), ("pending",)]
