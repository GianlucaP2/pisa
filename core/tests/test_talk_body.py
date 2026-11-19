"""The body (words set aside, showing while talking, the route, Pisa speaking first) and the replay's two bugs
the user would hit first. One test per fix or rail, each failing with it undone."""

from typing import Any

from pisa.domain import placeholders
from tests.replay.scenarios import SCENARIOS, c, done
from tests.test_replay import _play
from tests.virtual_time import run_talk

D = "2026-10-"


def _scenario(id_: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == id_)


# --- 0. the replay's bugs -----------------------------------------------------------------------------------


def test_a_placeholder_write_never_runs_and_the_real_ones_beside_it_do(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # A replay's scenario 01: Luna's first (parallel) round padded the real writes with add_task
    # "PLACEHOLDER_NOT_USED" / "dummy"; one placeholder task was stored.
    scenario = {
        "id": "x-junk",
        "now": D + "30T10:00",
        "lang": "it-IT",
        "world": {},
        "max_questions": 0,
        "turns": [{"owner": "metti la palestra alle 18"}],
        "script": [
            [
                [
                    c(
                        "create_event",
                        title="Palestra",
                        start=D + "30T18:00",
                        owner_said="la palestra alle 18",
                    ),
                    c("add_task", title="PLACEHOLDER_NOT_USED"),
                    c("add_task", title="dummy"),
                    c("add_task", title="  "),
                    done("Fatto: palestra alle 6."),
                ],
                "Fatto: palestra alle 6.",
            ]
        ],
        "expect": {"events": [("palestra", D + "30T18:00")], "events_total": 1},
    }
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
    assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert result.junk == 3


def test_placeholders_are_whole_values_never_real_titles() -> None:
    for value in ("PLACEHOLDER_NOT_USED", "dummy", "", " ", "<title>", "N/A", "null", "...", "{}"):
        assert placeholders.placeholder(value), value
    for value in ("Test medico", "Todo il giorno", "Bar Roma", "Comprare il latte", "Palestra", "TBD Srl"):
        assert not placeholders.placeholder(value), value
    # a placeholder in an optional detail is the model omitting it: the write stands
    assert placeholders.junk({"title": "Latte", "place": "none"}) is None
    assert placeholders.junk({"title": "Latte", "purpose": ""}) == "purpose"


def test_send_words_after_the_read_back_send_by_code_one_yes(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # The replay's scenario 13 (3/3): after "Lo mando?" the user's "sì mandala" went to the model, and
    # send_draft asked a second time. Now code sends it: the scripted second turn has no model step at all.
    scenario = _scenario("13-email-readback-yes")
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
    assert result.turns[1].by_code


def test_send_words_never_answer_anything_but_the_one_send_asked(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    # Pisa asked about a calendar change: "mandala" is no yes to it (the model reads the words)
    scenario = {
        "id": "x-send-words-not-a-yes",
        "now": D + "30T11:00",
        "lang": "it-IT",
        "max_questions": 2,
        "world": {
            "events": [
                (
                    "e101",
                    "Riunione con Marco",
                    "2026-11-02T10:00",
                    "2026-11-02T11:00",
                    [{"email": "marco@example.invalid"}],
                )
            ]
        },
        "turns": [
            {"owner": "cancella la riunione con Marco di lunedì"},
            {"owner": "mandala", "advance_s": 5},
        ],
        "script": [
            [
                [
                    c(
                        "cancel_event",
                        event="riunione con Marco",
                        day="lunedì",
                        owner_said="cancella la riunione con Marco di lunedì",
                    )
                ],
                "La cancello e Marco riceve la disdetta?",
            ],
            ["Cosa devo mandare?"],
        ],
        "expect": {"events": [("riunione", "2026-11-02T10:00")], "pending": 1},
    }
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    assert result.passed, result.problems
    assert not result.turns[1].by_code


# --- Words Pisa set aside ----------------------------------------------------------------------------------


def test_noise_and_echo_are_sent_as_ignored_and_start_no_turn() -> None:
    import asyncio

    from pisa.ports.talker import OwnerWords, PisaWords
    from tests.test_live_talk import _run, _until

    run, session, link, seen = _run()

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("A", final=True))
        session.queue.put_nowait(PisaWords("Il dentista è alle due e trentadue.", final=True))
        await asyncio.sleep(0.05)
        # Pisa's own line heard back through the speaker, its numbers as digits ("…e trentadue")
        session.queue.put_nowait(OwnerWords("il dentista è alle 2:32", final=True))
        await _until(lambda: sum(1 for m in link.sent if m.get("ignored")) == 2)
        run.end("owner")
        await task

    run_talk(go())
    ignored = [(m["text"], m["ignored"]) for m in link.sent if m.get("ignored")]
    assert ignored == [("A", "noise"), ("il dentista è alle 2:32", "echo")]
    assert not [s for s in seen if s[0] == "think"]


def test_numbers_compare_as_digits() -> None:
    from pisa.domain.talk import echo_words

    assert echo_words("alle due e trentadue") == ["alle", "2", "e", "32"]
    assert echo_words("2:32") == ["2", "32"] and echo_words("2:00") == ["2"]
    assert echo_words("thirty two") == ["32"] and echo_words("quarantotto") == ["48"]


# --- Showing while talking ------------------------------------------------------------------------------


def _choice(show_id: str) -> dict[str, Any]:
    return {
        "present_id": show_id,
        "reply_to": None,
        "purpose": "Scegli",
        "focus": "take",
        "lang": "it-IT",
        "object": {
            "object_id": "choice:c1",
            "kind": "choice",
            "revision": 0,
            "data": {
                "question": "Dove?",
                "options": [{"id": "o1", "label": "Bar Centrale"}, {"id": "o2", "label": "Caffè del Porto"}],
            },
        },
    }


def test_a_show_goes_on_the_talk_channel_and_a_tap_on_its_option_is_the_owner_s_words() -> None:
    import asyncio

    from pisa.services.talk import Opening, TalkScreen
    from tests.test_live_talk import _run, _until

    run, _, link, seen = _run()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    taps: list[tuple[str, str]] = []

    async def chosen(object_id: str, option: str) -> str | None:
        taps.append((object_id, option))
        return "[tap] o2: Caffè del Porto"

    run.hooks.chosen = chosen

    async def go() -> None:
        task = asyncio.create_task(run.run())
        assert await run.screen.present(_choice("s1")) == ("shown", "phone")  # type: ignore[union-attr]
        link.messages.put_nowait({"type": "talk.choose", "id": "s1", "choice": "o2"})
        await _until(lambda: any(s == ("think", "[tap] o2: Caffè del Porto") for s in seen))
        assert await run.screen.close("choice:c1")  # type: ignore[union-attr]
        run.end("owner")
        await task

    run_talk(go())
    shown = [m for m in link.sent if m["type"] == "talk.present"]
    assert shown[0]["id"] == "s1" and shown[0]["object"]["kind"] == "choice" and shown[0]["focus"] == "take"
    assert taps == [("choice:c1", "o2")]
    assert {"type": "talk.dismiss", "id": "s1"} in link.sent
    # the tap comes back as the user's final words (the phone's exchange), never set aside as noise
    assert {"type": "talk.transcript", "text": "[tap] o2: Caffè del Porto", "final": True} in link.sent


def test_a_tap_on_a_shown_approval_is_decided_by_code_and_said() -> None:
    import asyncio

    from pisa.domain.whisper import Kind
    from pisa.services.talk import Opening, TalkScreen
    from tests.test_live_talk import _run, _until

    run, session, link, seen = _run()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    decided: list[tuple[str, bool]] = []

    async def decide(approval_id: str, yes: bool) -> tuple[str, bool]:
        decided.append((approval_id, yes))
        return "Mandata a Claudio.", True

    run.hooks.decide = decide
    item = {
        "present_id": "s2",
        "purpose": "Il testo",
        "focus": "take",
        "object": {"object_id": "approval:a1", "kind": "item", "revision": 0, "ref": {"ref": "approval:a1"}},
    }

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.screen.present(item)  # type: ignore[union-attr]
        link.messages.put_nowait({"type": "talk.choose", "id": "s2", "choice": "yes"})
        await _until(lambda: any(w.id == "tap" for w in session.whispers))
        run.end("owner")
        await task

    run_talk(go())
    assert decided == [("a1", True)]
    tap = next(w for w in session.whispers if w.id == "tap")
    assert tap.kind is Kind.DONE and tap.text == "Mandata a Claudio."
    assert not [s for s in seen if s[0] == "think"]  # code decided it: no model


def test_a_tap_on_anything_not_shown_in_this_talk_answers_nothing() -> None:
    import asyncio

    from pisa.services.talk import TAP_GONE, Opening, TalkScreen
    from tests.test_live_talk import _run, _until

    run, session, link, _ = _run()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    called: list[str] = []

    async def chosen(object_id: str, option: str) -> str | None:
        # as conversations.object_event: words only for a choice Pisa showed this device and still open
        called.append(option)
        return "[tap] o1: x" if object_id == "choice:c1" else None

    async def decide(approval_id: str, yes: bool) -> tuple[str, bool]:
        called.append(approval_id)
        return "Fatto.", True

    run.hooks.chosen, run.hooks.decide = chosen, decide

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.screen.present(_choice("s1"))  # type: ignore[union-attr]
        link.messages.put_nowait({"type": "talk.choose", "id": "never-shown", "choice": "yes"})
        link.messages.put_nowait(
            {"type": "talk.choose", "id": "never-shown", "choice": "o1"}
        )  # not shown here
        link.messages.put_nowait({"type": "talk.choose", "id": "s1", "choice": "o5"})  # no such option
        link.messages.put_nowait(
            {"type": "talk.choose", "id": "s1", "choice": "yes"}
        )  # a choice is no approval
        await _until(lambda: sum(1 for w in session.whispers if w.id == "tap") == 4)
        run.end("owner")
        await task

    run_talk(go())
    assert called == ["o1"]  # An unknown id is asked as an object's own: none is open
    assert {w.text for w in session.whispers if w.id == "tap"} == {TAP_GONE["it-IT"]}


def test_pisa_says_one_line_for_what_it_shows() -> None:
    from pisa.domain.talk import shown_line

    reply = "Domani hai tre cose: il dentista alle 9, la palestra alle 18 e la cena alle 21. Poi riposo."
    assert shown_line(reply, "it-IT") == (
        "Domani hai tre cose: il dentista alle 9, la palestra alle 18 e la cena alle 21. Te lo mostro."
    )
    assert shown_line("Ecco la tua giornata di domani.", "it-IT") == "Ecco la tua giornata di domani."


# --- The route ------------------------------------------------------------------------------------------


def test_a_route_change_keeps_the_talk_and_is_in_the_timeline_and_the_turn() -> None:
    import asyncio

    from pisa.ports.talker import OwnerSpeaking, OwnerWords
    from tests.test_live_talk import _run, _until

    run, session, link, seen = _run()
    run.route = "speaker"

    async def go() -> None:
        task = asyncio.create_task(run.run())
        link.messages.put_nowait({"type": "talk.route", "route": "airpods"})
        link.messages.put_nowait({"type": "talk.route", "route": "jetpack"})  # unknown: ignored
        await _until(lambda: run.route == "airpods")
        session.queue.put_nowait(OwnerSpeaking(False))
        session.queue.put_nowait(OwnerWords("che ore sono?", final=True))
        await _until(lambda: any(s[0] == "think" for s in seen))
        await asyncio.sleep(0.05)
        assert not task.done()  # a route change never ends the session
        run.end("owner")
        await task

    run_talk(go())
    routes = [e for e in run.timeline.events if e.get("kind") == "route"]
    assert [(e["route"], e["was"]) for e in routes] == [("airpods", "speaker")]
    exchanges = [s[1] for s in seen if s[0] == "exchange"]
    assert exchanges and exchanges[0].route == "airpods"


def test_on_the_speaker_gemini_hears_speech_start_less_eagerly() -> None:
    from google.genai import types

    from pisa.connectors.talker_gemini import PREFIX_PADDING_MS, SPEAKER_PREFIX_PADDING_MS, connect_config
    from pisa.ports.talker import TalkerConfig

    def detection(route: str | None) -> Any:
        cfg = TalkerConfig(key="k", voice="Aoede", lang="it-IT", instructions="x", route=route)
        realtime = connect_config(cfg).realtime_input_config
        assert realtime is not None
        return realtime.automatic_activity_detection

    speaker, airpods = detection("speaker"), detection("airpods")
    assert speaker.start_of_speech_sensitivity is types.StartSensitivity.START_SENSITIVITY_LOW
    assert speaker.prefix_padding_ms == SPEAKER_PREFIX_PADDING_MS
    assert airpods.start_of_speech_sensitivity is types.StartSensitivity.START_SENSITIVITY_HIGH
    assert airpods.prefix_padding_ms == PREFIX_PADDING_MS


def test_on_airpods_glasses_or_the_car_pisa_says_one_sentence() -> None:
    from pisa.domain.talk import short_line

    assert short_line("Fatto. Ho spostato la cena alle 21 e avvisato Marco. Poi altro.") == (
        "Fatto. Ho spostato la cena alle 21 e avvisato Marco."
    )


def test_a_tap_decides_only_this_device_s_waiting_approval_and_never_one_that_needs_face_id(  # type: ignore[no-untyped-def]
    client, db
) -> None:
    import uuid

    from pisa.domain.talk import TAP_FACE_ID
    from pisa.services.talk_turns import tap_decision
    from tests.conftest import pair

    mine, other = (uuid.UUID(pair(db, name)[0]) for name in ("iPhone", "iPad"))
    runtime = client.app.state.runtime

    async def make(device: uuid.UUID, biometric: bool = False) -> str:
        view = await runtime.approvals.create(
            "calendar_cancel",
            "Cancello la riunione",
            ["lunedì 10:00"],
            {"title": "Riunione"},
            requires_biometric=biometric,
            device_id=device,
        )
        return str(view["id"])

    theirs, face, kept = (client.portal.call(make, *a) for a in ((other,), (mine, True), (mine,)))
    # another device's approval, and one that isn't an approval id at all: no answer
    assert client.portal.call(tap_decision, runtime, mine, theirs, True, "it-IT") == ("", False)
    assert client.portal.call(tap_decision, runtime, mine, "not-an-id", True, "it-IT") == ("", False)
    # Face ID: never by a tap; the card stays on the phone
    assert client.portal.call(tap_decision, runtime, mine, face, True, "it-IT") == (
        TAP_FACE_ID["it-IT"],
        False,
    )
    # a no: decided by code, auth tap, and said
    line, done = client.portal.call(tap_decision, runtime, mine, kept, False, "it-IT")
    assert line and not done
    row = db.execute("SELECT status, decided_auth FROM approvals WHERE id = %s", (kept,)).fetchone()
    assert row == ("rejected", "tap")
    assert db.execute("SELECT status FROM approvals WHERE id = %s", (theirs,)).fetchone()[0] == "pending"
    # answered once: a second tap answers nothing
    assert client.portal.call(tap_decision, runtime, mine, kept, True, "it-IT") == ("", False)


def test_the_voice_line_in_a_talk() -> None:
    from pisa.services.talk import Opening, TalkScreen
    from pisa.services.talk_turns import spoken
    from tests.test_live_talk import _run

    run, _, link, _ = _run()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    reply = "Domani hai tre cose: spesa, palestra e cena. La palestra è alle 18 in via Immaginaria."
    assert spoken(reply, run, "it-IT") == reply  # the phone's speaker, nothing shown: the whole reply
    run.screen.in_turn = 1
    assert spoken(reply, run, "it-IT").endswith("Te lo mostro.")  # shown: one line
    run.screen.in_turn, run.route = 0, "glasses"
    assert spoken(reply, run, "it-IT") == "Domani hai tre cose: spesa, palestra e cena."
    note = [m for m in link.sent if m["type"] == "talk.present"][-1]["object"]
    assert note["kind"] == "note" and note["data"]["lines"] == [reply]  # the rest is on the screen


def test_the_wrist_rail_a_health_matter_or_a_sensitive_place_is_never_said_on_a_short_route() -> None:
    """RAIL (`wrist.touches_sensitive`): on AirPods, the glasses, in the car and on the watch, a turn that
    names their health or a sensitive place is shown, never said, a question included; the phone's speaker
    says it."""
    from pisa.domain.whisper import Kind
    from pisa.services.talk import Opening, TalkScreen
    from pisa.services.talk_turns import thought_of
    from tests.test_live_talk import _run

    run, _, link, _ = _run()
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    health = "La visita dal cardiologo è domani alle 9. Vuoi un promemoria?"
    assert thought_of(health, Kind.SAY, run, "it-IT", asks=False, claimed=False).text == health  # speaker
    for route in ("airpods", "glasses", "car"):
        run.route = route
        thought = thought_of(health, Kind.ASK, run, "it-IT", asks=True, claimed=False)
        assert (thought.text, thought.shortened) == ("Te lo mostro.", True)  # a yes decides nothing
        note = [m for m in link.sent if m["type"] == "talk.present"][-1]["object"]
        assert note["data"]["lines"] == [health]  # the answer is on the screen
    # the user's own words name it: Pisa's plain answer is not said either
    words = "quando ho il dentista?"
    said = thought_of("Alle 9.", Kind.SAY, run, "it-IT", asks=False, claimed=False, words=words)
    assert said.text == "Te lo mostro."
    # the watch (a talk opened from it), whatever the audio route, nothing shown
    run.route, run.screen = "speaker", None
    plain = "Alle 9 hai la riunione."
    assert thought_of(plain, Kind.SAY, run, "en-US", asks=False, claimed=False, watch=True).text == plain
    watch = thought_of(health, Kind.SAY, run, "en-US", asks=False, claimed=False, watch=True)
    assert watch.text == "It's on your screen."


# --- Pisa speaks first -------------------------------------------------------------------------------------


def _notice(db: Any, device_id: str, speak: str = "leave_now", priority: str = "urgent") -> str:
    import uuid

    nid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO notifications (id, kind, text, lang, priority, device_id, created_at, queued_at, "
        "urgency, speak) VALUES (%s, 'reminder', 'Esci adesso per il dentista.', 'it-IT', %s, %s, now(), "
        "now(), 'now', %s)",
        (nid, priority, device_id, speak),
    )
    return nid


def _wearing(db: Any, device_id: str, earbuds: str = "connected") -> None:
    db.execute(
        "INSERT INTO device_presence (device_id, app, earbuds, updated_at) "
        "VALUES (%s, 'foreground', %s, now()) "
        "ON CONFLICT (device_id) DO UPDATE SET earbuds = EXCLUDED.earbuds",
        (device_id, earbuds),
    )


def test_with_airpods_on_and_no_talk_pisa_asks_to_open_and_the_notice_is_not_spoken_twice(  # type: ignore[no-untyped-def]
    client, db, secrets_key
) -> None:
    import uuid

    from pisa.services.notifications import Dispatcher
    from tests.conftest import pair, set_key
    from tests.live_app import live, message_of

    set_key(db, secrets_key, "xai")  # natural mode can open (the talker's key)
    device_id, token = pair(db)
    runtime = client.app.state.runtime
    with live(client, token, device_id) as app:
        _wearing(db, device_id)  # after the hello (which records the app in front)
        from tests.conftest import pause_outbox

        pause_outbox(client)  # this test's dispatcher is the only one delivering
        dispatcher = Dispatcher(
            runtime.pool, runtime.live, runtime.push, clock=runtime.clock, owner=runtime.owner
        )
        nid = _notice(db, device_id)
        assert client.portal.call(dispatcher.deliver, uuid.UUID(nid)) == "ws"
        ask = app.next()
        assert ask["type"] == "talk.ask_to_open"  # Pisa asks to open first; its notice follows
        notify = app.next()
    import json

    message_of(json.dumps(ask))
    assert ask["type"] == "talk.ask_to_open" and ask["why"] == "leave_now"
    assert ask["line"] == "Esci adesso per il dentista."
    assert notify["type"] == "notify" and notify["voice"] is False  # shown; the talk says it
    row = db.execute("SELECT id, why FROM talk_asks WHERE notification_id = %s", (nid,)).fetchone()
    assert row == (uuid.UUID(ask["id"]), "leave_now")
    # never twice for the same item (the earbuds still on: the hello and the close moved presence)
    _wearing(db, device_id)
    again = client.portal.call(
        runtime.talk.may_ask, uuid.UUID(device_id), uuid.UUID(nid), "leave_now", "urgent"
    )
    assert again is None
    # the talk opened for it starts with Pisa's line, once; the ask is marked opened
    opened = client.portal.call(runtime.talk.opened_for, uuid.UUID(device_id), uuid.UUID(ask["id"]))
    assert opened == "Esci adesso per il dentista."
    assert client.portal.call(runtime.talk.opened_for, uuid.UUID(device_id), uuid.UUID(ask["id"])) == ""


def test_the_rails_no_route_worn_a_low_item_quiet_economy_and_four_an_hour(  # type: ignore[no-untyped-def]
    client, db, secrets_key
) -> None:
    import uuid

    from tests.conftest import pair, set_key

    runtime = client.app.state.runtime
    device_id, _ = pair(db)
    device = uuid.UUID(device_id)

    def ask(speak: str = "check_in", priority: str = "normal") -> Any:
        return client.portal.call(
            runtime.talk.may_ask, device, uuid.UUID(_notice(db, device_id)), speak, priority
        )

    set_key(db, secrets_key, "xai")
    _wearing(db, device_id, "disconnected")
    assert ask() is None  # nothing worn, no recent talk on AirPods
    _wearing(db, device_id)
    assert ask(priority="low") is None  # A low item never asks
    assert ask(speak="weather") is None  # not a reason Pisa speaks first for
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('settings', %s) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value",
        (
            '{"conversation": {"mode": "economy", "talker": "xai", '
            '"voices": {"xai": "eve", "gemini": "Aoede"}}}',
        ),
    )
    assert ask() is None  # economy: no talk to open
    db.execute("DELETE FROM app_settings WHERE key = 'settings'")
    assert all(ask() is not None for _ in range(4))
    assert ask() is None  # at most 4 an hour


def test_in_an_open_talk_pisa_says_it_in_the_talk_leave_now_at_once_the_rest_at_the_pause(  # type: ignore[no-untyped-def]
    client, db
) -> None:
    import uuid

    from pisa.domain.whisper import Priority
    from pisa.services.notifications import Dispatcher
    from tests.conftest import pair
    from tests.live_app import live
    from tests.test_live_talk import _run

    device_id, token = pair(db)
    runtime = client.app.state.runtime
    run, session, _, _ = _run()
    runtime.talk.runs[run.session_id] = run
    runtime.talk._by_device[uuid.UUID(device_id)] = run.session_id  # pyright: ignore[reportPrivateUsage]
    with live(client, token, device_id) as app:
        from tests.conftest import pause_outbox

        pause_outbox(client)  # this test's dispatcher is the only one delivering
        dispatcher = Dispatcher(
            runtime.pool, runtime.live, runtime.push, clock=runtime.clock, owner=runtime.owner
        )
        client.portal.call(dispatcher.deliver, uuid.UUID(_notice(db, device_id)))
        notify = app.next()
        client.portal.call(dispatcher.deliver, uuid.UUID(_notice(db, device_id, "check_in", "normal")))
        app.next()
    assert notify["type"] == "notify" and notify["voice"] is False
    said = [(w.priority, w.text) for w in session.whispers if w.id.startswith("notice-")]
    assert said == [
        (Priority.NOW, "Esci adesso per il dentista."),
        (Priority.PAUSE, "Esci adesso per il dentista."),
    ]
    assert not db.execute("SELECT count(*) FROM talk_asks").fetchone()[0]  # no ask: the talk is open


def test_a_proposal_about_to_lapse_is_said_once_more(client, db) -> None:  # type: ignore[no-untyped-def]
    import uuid
    from datetime import timedelta

    from tests.conftest import pair

    runtime = client.app.state.runtime
    device = uuid.UUID(pair(db)[0])

    async def make() -> str:
        view = await runtime.approvals.create(
            "calendar_cancel",
            "Cancello la riunione con Marco?",
            ["x"],
            {"title": "Riunione"},
            device_id=device,
        )
        return str(view["id"])

    approval = client.portal.call(make)
    assert client.portal.call(runtime.approvals.lapsing) == 0  # 10 minutes left: not yet
    db.execute(
        "UPDATE approvals SET expires_at = %s WHERE id = %s",
        (runtime.clock() + timedelta(seconds=90), approval),
    )
    assert client.portal.call(runtime.approvals.lapsing) == 1
    assert client.portal.call(runtime.approvals.lapsing) == 0  # once
    row = db.execute("SELECT text, speak, device_id FROM notifications WHERE speak IS NOT NULL").fetchone()
    assert row == (
        "«Cancello la riunione con Marco» aspetta ancora il tuo sì: scade tra due minuti.",
        "approval_lapsing",
        device,
    )


def test_leave_now_carries_its_reason_through_attend_to_the_notice(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    from datetime import UTC, datetime

    import psycopg

    from pisa.domain.attend import Candidate
    from pisa.services.attend import deliver_now
    from tests.conftest import dsn

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            now = datetime(2026, 11, 12, 8, 31, tzinfo=UTC)
            leave = Candidate(
                "c1",
                "leave",
                "reminder",
                "Esci adesso per il dentista.",
                "it-IT",
                now,
                "now",
                floor=True,
                speak="leave_now",
            )
            await deliver_now(conn, leave, {}, timezone="Europe/Rome")

    asyncio.run(go())
    assert db.execute("SELECT speak FROM notifications").fetchone() == ("leave_now",)


def test_what_s_next_is_said_from_pisa_s_plan_at_once_with_no_model() -> None:
    import asyncio

    from pisa.domain.whisper import Fresh
    from pisa.ports.talker import OwnerWords
    from tests.test_live_talk import _run, _until

    run, session, _, seen = _run()
    thought = asyncio.Event()

    async def fresh() -> Fresh:
        return Fresh("adesso", next_item="Dentista alle 15:30")

    async def think(words: str, trace: str, yes_by_code: bool) -> Any:
        await thought.wait()  # the brain is slow: the plan's line goes first
        from pisa.domain.whisper import Kind
        from pisa.services.talk import Thought

        return Thought("Il prossimo è il dentista alle 15:30.", Kind.SAY)

    run.hooks.fresh, run.hooks.think = fresh, think

    async def go() -> None:
        task = asyncio.create_task(run.run())
        session.queue.put_nowait(OwnerWords("Pisa, cosa ho dopo?", final=True))
        await _until(lambda: any(w.id.startswith("next") for w in session.whispers))
        thought.set()
        await asyncio.sleep(0.05)
        run.end("owner")
        await task

    run_talk(go())
    first = next(w for w in session.whispers if w.id.startswith("next"))
    assert first.text == "Il prossimo è Dentista alle 15:30."
    content = [e for e in run.timeline.events if e.get("kind") == "first_content"]
    assert content and content[0]["by"] == "next"
    del seen


def test_a_talk_opened_for_pisa_s_line_starts_with_it_and_marks_it_said() -> None:
    import asyncio

    from pisa.domain.whisper import Priority
    from pisa.ports.talker import PisaWords
    from tests.test_live_talk import _run, _until

    run, session, _, seen = _run()
    run.first_line = "Esci adesso per il dentista: in metro ci vogliono 25 minuti."
    marked: list[bool] = []

    async def first_said() -> None:
        marked.append(True)

    run.hooks.first_said = first_said

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await _until(lambda: any(w.id == "first" for w in session.whispers))
        session.queue.put_nowait(
            PisaWords("Esci adesso per il dentista: in metro ci vogliono 25 minuti.", final=True)
        )
        await _until(lambda: marked)
        run.end("owner")
        await task

    run_talk(go())
    first = session.whispers[0]
    assert first.id == "first" and first.priority is Priority.NOW  # Pisa's line first, not a question
    assert marked == [True] and not [s for s in seen if s[0] == "think"]


# --- the contract -------------------------------------------------------------------------------------------


def test_the_conv3_fixtures_are_the_contract() -> None:
    import json

    from pydantic import TypeAdapter

    from pisa.api import schemas
    from pisa.domain.talk import ROUTES
    from pisa.services.session_messages import PresentObject, TalkAskToOpen
    from tests.conftest import PROTOCOL_DIR
    from tests.live_app import message_of

    rest = PROTOCOL_DIR / "fixtures" / "rest"
    objects = TypeAdapter(PresentObject)
    for name in ("talk_present_plan", "talk_present_choice", "talk_present_approval"):
        message = json.loads((rest / f"{name}.json").read_text())
        assert message["type"] == "talk.present" and message["id"]
        objects.validate_json(json.dumps(message["object"]))  # exactly the WebSocket present's object
    ask_raw = (PROTOCOL_DIR / "fixtures" / "valid" / "talk_ask_to_open.json").read_text()  # in the schema now
    ask = message_of(ask_raw)
    raw = {k: v for k, v in json.loads(ask_raw).items() if k not in ("v", "ts")}
    assert isinstance(ask, TalkAskToOpen) and ask.model_dump(mode="json") == raw
    created = schemas.TalkSessionCreate.model_validate_json(
        (rest / "talk_session_create_proactive.json").read_text()
    )
    assert created.trigger == "proactive" and created.route == "airpods" and str(created.ask_id) == ask.id
    channel = json.loads((rest / "talk_channel_conv3.json").read_text())
    assert {m["ignored"] for m in channel["server_to_phone"] if "ignored" in m} == {"noise", "echo"}
    routes = [m["route"] for m in channel["phone_to_server"] if m["type"] == "talk.route"]
    assert routes and set(routes) <= ROUTES


def test_in_a_talk_pisa_shows_on_the_talk_channel_never_on_the_websocket_too() -> None:
    from pisa.services.talk import Opening, TalkScreen
    from pisa.services.talk_turns import turn_senders
    from tests.test_live_talk import _run

    class Live:
        def turn_senders(self, reply_to: Any) -> dict[str, Any]:
            return {"device": "the WebSocket's screen", "card": "the WebSocket's card"}

    run, _, link, _ = _run()
    assert turn_senders(Live(), None)["device"] == "the WebSocket's screen"  # no talk: as today
    run.screen = TalkScreen(link, Opening().kinds, "it-IT")
    run.screen.in_turn = 3
    senders = turn_senders(Live(), run)
    assert senders["device"] is run.screen and run.screen.in_turn == 0  # this turn's shows counted afresh
    assert turn_senders(None, run)["device"] is run.screen  # a talk with no live socket still shows
