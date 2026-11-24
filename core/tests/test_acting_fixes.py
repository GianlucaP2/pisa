"""The must-fix rows, the reviewer's probes as tests, and the review's
decisions. Each fails without its fix."""

import uuid
from datetime import datetime
from typing import Any, cast

from pisa.domain import decisions
from tests.actions_fake import OWNER
from tests.conftest import pair
from tests.owner import owner_at
from tests.test_acting import PAOLA, Model, patches, row, said
from tests.test_acting import google as google  # the fixture
from tests.test_approvals import dinner
from tests.test_talking_like_a_person import _deliver, _queue

MOVE = ("move_event", {"event": "la cena", "start": "oggi alle 21"})


def _talk(client: Any, db: Any, secrets_key: str, app: Any, words: str) -> list[dict[str, Any]]:

    app.say(words, lang="it-IT", source="earbuds_mic")
    frames: list[dict[str, Any]] = []
    while not frames or not (frames[-1]["type"] == "say" and frames[-1]["final"]):
        frames.append(app.next())
        assert frames[-1]["type"] != "error", frames[-1]
    return frames


# --- HIGH: a "sì" answers only the newest thing Pisa asked, proactive questions included
# ----------------------


def test_a_yes_to_a_check_in_never_approves_the_question_before_it(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key
) -> None:
    from tests.conftest import set_key
    from tests.live_app import live

    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    fake_models.default = Model("sposta la cena alle 21", [MOVE])
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        asked = _talk(client, db, secrets_key, app, "sposta la cena alle 21")
        assert "interrupt" in [f["type"] for f in asked]
        check_in = _queue(client, "check_in", "Sei passato in farmacia?", device=device_id)
        _deliver(client, check_in)
        assert app.next()["type"] == "notify"
        _talk(client, db, secrets_key, app, "sì")
    assert row(db)[1] == "pending" and not patches(google)  # the dinner stays; Paola hears nothing


def test_without_anything_said_since_the_yes_still_answers_by_code(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key
) -> None:
    from tests.conftest import set_key
    from tests.live_app import live

    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    other = pair(db)[0]
    fake_models.default = Model("sposta la cena alle 21", [MOVE])
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        _talk(client, db, secrets_key, app, "sposta la cena alle 21")
        _deliver(client, _queue(client, "info", "Per l'altro telefono.", device=other))  # not to this one
        yes = _talk(client, db, secrets_key, app, "sì")
    assert said(yes).startswith("Ho spostato «Cena»") and row(db)[1] == "done"


def test_the_briefing_phrase_is_newer_than_the_question(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key, monkeypatch
) -> None:
    from types import SimpleNamespace

    from tests.conftest import set_key
    from tests.live_app import live

    briefings = client.app.state.runtime.briefings
    real = briefings.for_phrase

    async def for_phrase(text: str) -> Any:
        if text == "buongiorno":
            briefing = "Oggi hai la cena alle 19:30. Vuoi che te la ricordi?"
            return SimpleNamespace(id=uuid.uuid4(), text=briefing, lang="it-IT")
        return await real(text)

    monkeypatch.setattr(briefings, "for_phrase", for_phrase)
    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    fake_models.default = Model("sposta la cena alle 21", [MOVE])
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        _talk(client, db, secrets_key, app, "sposta la cena alle 21")
        _talk(client, db, secrets_key, app, "buongiorno")
        _talk(client, db, secrets_key, app, "sì")
    assert row(db)[1] == "pending" and not patches(google)


# --- MED: grounding is whole words, tied to the item --------------------------


def test_the_reviewers_probes_ground_nothing() -> None:
    from datetime import date

    from pisa.domain import actions as rules

    today = date(2026, 10, 20)
    tomorrow_9 = datetime(2026, 10, 21, 9, 0)
    # a time in another clause; a negated cancel; "dropbox"; another item's cancel
    train = "alle 9 ho il treno, sposta la cena di domani"
    assert rules.decide_mode("calendar_move", train, read_outside=False,
                             attendees=False, start=tomorrow_9, default_day=today, today=today,
                             title="Cena") == "held"  # fmt: skip
    for words in ("non cancellare la cena", "apri dropbox e cancella", "cancella il promemoria del latte"):
        assert rules.decide_mode("calendar_cancel", words, read_outside=False, attendees=False,
                                 title="Cena") == "held", words  # fmt: skip
    assert rules.decide_mode("calendar_cancel", "cancella la cena", read_outside=False, attendees=False,
                             title="Cena") == "words"  # fmt: skip
    assert rules.decide_mode("calendar_move", "sposta la cena domani alle 9", read_outside=False,
                             attendees=False, start=tomorrow_9, default_day=today, today=today,
                             title="Cena") == "words"  # fmt: skip
    g = rules.grounded_change
    assert not g("apply_option", {"option_id": "o1"}, "pranzo prima del museo", today)  # "prima" ≠ option 1
    assert not g("apply_option", {"option_id": "o1"}, "la prima del mese", today)  # an ordinal, no option
    dropbox = "metti la cena su dropbox"  # "dropbox" is no drop
    assert not rules.says_cancel(dropbox)
    assert (
        rules.decide_mode("calendar_cancel", dropbox, read_outside=False, attendees=False, title="Cena")
        == "held"
    )
    assert g("apply_option", {"option_id": "o1"}, "la prima", today)
    assert g("apply_option", {"option_id": "o2"}, "opzione 2", today)
    pacco = "Ritirare il pacco"
    assert not g("complete_task", {}, "presto ritiro il pacco", today, title=pacco)  # "presto" ⊅ preso
    assert not g("complete_task", {}, "è stato bello, la farmacia", today, title="Farmacia")
    assert g("complete_task", {}, "sono stato in farmacia", today, title="Farmacia")
    assert g("complete_task", {}, "ho ritirato il pacco", today, title=pacco)
    assert not g(
        "complete_task", {}, "fatto", today, title=pacco
    )  # done, but which? the title is not in the words
    for words, level in (("saltare il pacco", 1), ("immediatamente il pacco", 2), ("follow il pacco", 3)):
        assert not g("set_task_priority", {"priority": level}, words, today, title=pacco), words
    assert g("set_task_priority", {"priority": 1}, "il pacco è urgente", today, title=pacco)
    assert not g("reschedule_task", {"when": "none"}, "no, il pacco", today, title=pacco)  # any "no"
    assert g("reschedule_task", {"when": "none"}, "togli la scadenza al pacco", today, title=pacco)
    assert not g("skip_item", {"item": "pranzo"}, "salta la cena", today)
    assert g("skip_item", {"item": "pranzo"}, "salta il pranzo", today)
    assert not g("delete_task", {}, "non cancellare il pane", today, title="Comprare il pane")
    assert g("delete_task", {}, "cancella il task del pane", today, title="Comprare il pane")


def test_send_it_is_whole_words() -> None:
    from pisa.domain.actions import says_send

    for words in ("mandala", "inviala pure", "send it", "manda pure", "ok mandalo"):
        assert says_send(words), words
    for words in ("poi mandami il riassunto", "le mail inviate", "mandarini", "non mandarla"):
        assert not says_send(words), words


def test_a_move_in_another_clause_is_held(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import act

    dinner(google, db)
    actions = client.app.state.runtime.actions
    act(client, actions.move_event, "la cena", "domani alle 9", None,
        said="alle 9 ho il treno, sposta la cena di domani")  # fmt: skip
    assert row(db)[1:3] == ("pending", "held") and not patches(google)


def test_cancelling_another_thing_never_cancels_the_dinner(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import act

    dinner(google, db)
    actions = client.app.state.runtime.actions
    for words in ("non cancellare la cena", "cancella il promemoria del latte", "apri dropbox"):
        act(client, actions.cancel_event, "la cena", None, said=words)
        assert google.events["ev-dinner"]["status"] == "confirmed", words
    assert {r[0] for r in db.execute("SELECT origin FROM approvals").fetchall()} == {"held"}


def test_the_draft_is_sent_only_on_its_words_and_never_after_outside_text(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import act
    from tests.test_approvals import thread

    thread(google, db)
    actions = client.app.state.runtime.actions
    device = uuid.UUID(str(pair(db)[0]))
    act(client, actions.draft_reply, "Claudio cover", "La porto io sabato.", said="rispondi a Claudio",
        device=device)  # fmt: skip
    for words, outside in (("poi mandami il riassunto", None), ("mandarini", None), ("mandala", "email")):
        act(client, actions.send_draft, None, said=words, outside=outside, device=device)
        assert not google.sent, words
    (asked,) = db.execute("SELECT id::text FROM approvals WHERE status = 'pending'").fetchone()
    out = act(client, actions.send_draft, None, said="mandala", device=device)
    assert not google.sent  # H5: never a version Pisa didn't ask about in an earlier turn
    out = act(client, actions.send_draft, None, said="mandala", device=device, asked=[asked])
    assert not google.sent and "hasn't heard the whole text" in out  # C1: no read-back of it yet
    client.portal.call(actions.bundle, device, [asked], "Il testo: «La porto io  sabato.» Lo mando?")
    out = act(client, actions.send_draft, None, said="mandala", device=device, asked=[asked])
    assert len(google.sent) == 1 and "Not done" not in out


def test_delete_task_after_a_web_read_waits_unless_the_owner_named_it(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import l3

    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Comprare il pane', 'comprare pane', 'open', 'manual', now(), now())"
    )
    web = ("web_search", {"query": "traghetto Veenmond biglietti"})
    held = l3(db, secrets_key, "cerca il traghetto", [web, ("delete_task", {"task_ref": "pane"})])
    assert held.result("delete_task").startswith("Not done yet")
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)
    said = l3(db, secrets_key, "cerca il traghetto e cancella il task del pane",
              [web, ("delete_task", {"task_ref": "pane"})])  # fmt: skip
    assert '"status": "done"' in said.result("delete_task")  # The result says done
    assert db.execute("SELECT status FROM tasks").fetchone() == ("deleted",)


# --- MED: a correction is only the correction, right after; a new event is moved, never made twice ----------


def _answer(client: Any, device: Any, words: str, acted: list[str]) -> Any:
    from pisa.services.conversation import TurnState, current_turn

    actions = client.app.state.runtime.actions

    async def go() -> Any:
        with decisions.collecting():
            current_turn.set(TurnState(uuid.uuid4(), words))
            return await actions.answer(device, words, "it-IT", [], acted)

    return client.portal.call(go)


def test_no_alle_23_ce_la_partita_is_no_correction_and_a_late_one_neither(client, db, google, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.test_acting import act

    dinner(google, db)
    device = pair(db)[0]
    actions = client.app.state.runtime.actions
    act(client, actions.move_event, "la cena", "oggi alle 21", None, said="sposta la cena alle 21",
        device=device)  # fmt: skip
    moved = row(db)[0]
    assert _answer(client, device, "No, alle 23 c'è la partita", [moved]) is None
    clock.now += timedelta(minutes=11)
    assert _answer(client, device, "no, alle 22", [moved]) is None
    clock.now -= timedelta(minutes=11)
    assert len(patches(google)) == 1
    assert _answer(client, device, "no, alle 22", [moved]) is not None
    assert google.events["ev-dinner"]["start"]["dateTime"].startswith("2026-10-20T22:00")


def test_a_correction_of_a_new_event_moves_it(client, db, google) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import act

    device = pair(db)[0]
    actions = client.app.state.runtime.actions
    act(client, actions.create_event, "Dentista", "oggi alle 18", None, None, None, None,
        said="aggiungi dentista oggi alle 18", device=device)  # fmt: skip
    made = row(db)[0]
    assert row(db)[1:3] == ("done", "words")
    said_now = _answer(client, device, "no, alle 19", [made])
    created = [e for e in google.events.values() if e["summary"] == "Dentista"]
    assert len(created) == 1 and created[0]["start"]["dateTime"].startswith("2026-10-20T19:00"), said_now


# --- MED: B4 write-through follows choice 2; the option names who is invited (choice 1) ---------------------


def _option(client: Any, db: Any, invited: int | None = None) -> str:
    from psycopg.types.json import Jsonb

    from tests.assistant_env import rome
    from tests.test_approvals import DINNER

    runtime = client.app.state.runtime
    day = DINNER[0].date()
    fingerprint = client.portal.call(runtime.planner.fingerprint_for, day)
    move = {"action": "move", "event_id": "ev-dinner", "start": rome(2026, 10, 20, 21).isoformat(),
            "end": rome(2026, 10, 20, 22, 30).isoformat()}  # fmt: skip
    move |= {"invited": invited} if invited else {}
    body = {"target_title": "Cena", "options": [{"id": "o2", "changes": [move], "plan": {"items": []}}]}
    options_id = uuid.uuid4()
    db.execute(
        "INSERT INTO plan_options (id, day, fingerprint, body, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, now(), now() + interval '15 minutes')",
        (options_id, day, fingerprint, Jsonb(body)),
    )
    return str(options_id)


def _choose(
    client: Any, db: Any, said_words: str, outside: str | None = None, invited: int | None = None
) -> Any:
    from tests.test_acting import act

    options = client.app.state.runtime.plan_options
    return act(client, options.apply, _option(client, db, invited), "o2", "voice", said=said_words,
               outside=outside)  # fmt: skip


def test_an_option_after_outside_text_waits_for_one_yes(client, db, google) -> None:  # type: ignore[no-untyped-def]
    dinner(google, db)
    plan = _choose(client, db, "la seconda", outside="email")
    assert not patches(google) and row(db)[1:3] == ("pending", "held")
    assert "waits for one yes" in plan["calendar"][0]
    db.execute("DELETE FROM plan_adjustments")
    _choose(client, db, "la seconda")
    assert len(patches(google)) == 1 and row(db)[1:3] == ("done", "proposal")
    # "va bene" names no option, but the option is Pisa's own proposal on the
    # user's own event with nobody invited: Pisa applies it with Undo (tests in test_body_line.py).


def test_an_option_that_named_the_invited_is_the_one_yes_and_asks_if_they_changed(client, db, google) -> None:  # type: ignore[no-untyped-def]
    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    _choose(client, db, "la seconda")  # the option didn't say anyone would hear of it
    assert not patches(google) and row(db)[1:3] == ("pending", "proposal")
    db.execute("DELETE FROM plan_adjustments")
    _choose(client, db, "la seconda", invited=1)  # "… e avviso 1 invitato": the choice is the yes
    ((_, _, params, _),) = patches(google)
    assert params["sendUpdates"] == ["all"] and row(db)[1:3] == ("done", "proposal")


def test_a_failed_calendar_write_is_said(client, db, google, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    dinner(google, db)
    real = google._calendar

    def down(method: str, path: str, body: Any, if_match: Any = None) -> httpx.Response:
        if method == "PATCH":
            return httpx.Response(503, json={"error": {"code": 503, "message": "Backend Error"}})
        return real(method, path, body, if_match)

    monkeypatch.setattr(google, "_calendar", down)
    plan = _choose(client, db, "la seconda")
    assert "was not moved" in plan["calendar"][0] and "only Pisa's plan changed" in plan["calendar"][0]


def test_the_option_sentence_names_how_many_are_invited(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.planner.options import Change
    from pisa.services.plan_options import _with_invited

    db.execute("INSERT INTO calendars (id, name, access_role) VALUES ('cal', 'Lavoro', 'owner')")
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, "
        "attendees) VALUES ('cal', 'm1', 'Riunione', now(), now() + interval '1 hour', false, 'confirmed', 2)"
    )
    options = client.app.state.runtime.plan_options
    move = Change("move", "m1", None, None, None, None, None)
    assert client.portal.call(options._invited, [move]) == {"m1": 2}
    assert (
        _with_invited("Sposta la riunione alle 15.", 2, "it-IT")
        == "Sposta la riunione alle 15 e avviso 2 invitati"
    )
    assert _with_invited("Sposta la riunione alle 15", 1, "it-IT").endswith("e avviso 1 invitato")
    assert _with_invited("Sposta la riunione alle 15", 0, "it-IT") == "Sposta la riunione alle 15"


# --- LOW: what counts as a meeting, and how long it holds ---------------------------------------------------


def _note(client: Any, kind: str = "info", event_at: Any = None) -> str:
    from pisa.services.notifications import enqueue

    runtime = client.app.state.runtime

    async def go() -> str:
        with decisions.collecting():
            async with runtime.pool.connection() as conn:
                nid, _ = await enqueue(
                    conn, kind=kind, text="Il pacco è arrivato.", lang="it-IT", now=runtime.clock(),  # type: ignore[arg-type]
                    timezone="Europe/Rome", event_at=event_at,
                )  # fmt: skip
            return str(nid)

    return client.portal.call(go)


def _meeting(
    db: Any, clock: Any, title: str, hours: float = 1, calendar: str = "cal", attendees: Any = None,
    accepted: Any = None, made_by_pisa: bool = False,
) -> None:  # fmt: skip
    from datetime import timedelta

    db.execute("DELETE FROM calendar_events")
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES (%s, 'Lavoro', 'owner') ON CONFLICT DO NOTHING",
        (calendar,),
    )
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status, "
        "attendees, accepted, made_by_pisa) VALUES (%s, 'm1', %s, %s, %s, false, 'confirmed', %s, %s, %s)",
        (calendar, title, clock.now - timedelta(minutes=10), clock.now + timedelta(hours=hours), attendees,
         accepted, made_by_pisa),
    )  # fmt: skip


def _held_until(client: Any, db: Any, nid: str) -> Any:
    from tests.test_talking_like_a_person import _deliver, _until

    db.execute("UPDATE notifications SET not_before = NULL WHERE id = %s", (nid,))
    return _until(
        db,
        f"SELECT coalesce(not_before::text, delivered_via) FROM notifications WHERE id = '{uuid.UUID(nid)}'",
        lambda: _deliver(client, nid),
    )[0]


def test_a_meeting_is_people_or_a_whole_meeting_word_not_declined_not_js_not_days_long(
    client, db, clock
) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from pisa.domain import talk

    assert (
        not talk.is_meeting("Pranzo classico")
        and talk.is_meeting("Class di yoga")
        and talk.is_meeting("Riunione")
    )
    end = (clock.now + timedelta(hours=1)).isoformat()
    for title, columns, holds in [
        ("Pranzo classico", {}, False),  # "classico" is no class
        ("Pranzo con Paola", {"attendees": 1}, True),  # people: a meeting whatever the title
        ("Riunione budget", {"accepted": False}, False),  # declined
        # Pisa's own event, made on their yes: their meeting, it holds (the ruling: the
        # meeting hold is courtesy, not learning; the dead plan_calendar_id setting is retired)
        ("Riunione budget", {"made_by_pisa": True}, True),
    ]:
        db.execute("DELETE FROM notifications")
        _meeting(db, clock, title, **columns)
        held = _held_until(client, db, _note(client))
        assert (held != "none") == holds, (title, columns, held)
        if holds:
            assert held.startswith(end[:16].replace("T", " ")), held
    db.execute("DELETE FROM notifications")
    _meeting(db, clock, "Conference", hours=72)  # three days: no meeting
    assert _held_until(client, db, _note(client)) == "none"


def test_a_meeting_holds_two_hours_at_most_and_never_past_a_reminders_event(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    _meeting(db, clock, "Riunione", hours=3)
    held = _held_until(client, db, _note(client))
    assert held.startswith((clock.now + timedelta(hours=2)).isoformat()[:16].replace("T", " ")), held
    db.execute("DELETE FROM notifications")
    soon = clock.now + timedelta(minutes=40)  # its event starts during the meeting: no hold
    assert _held_until(client, db, _note(client, "reminder", event_at=soon)) == "none"


# --- LOW: a reply said from the tools' results goes back to the model as data ------------------------------


def test_a_reply_said_from_a_tool_is_quoted_data_in_the_history(
    client, db, google, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import ModelResponse, TextPart

    from tests.test_acting import socket_turns

    dinner(google, db)
    first = Model("sposta la cena alle 21", [MOVE])
    seen: list[Any] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        from pydantic_ai.messages import UserPromptPart

        prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        if prompts[-1] == "e domani?":
            seen.append(messages)
            yield "Ok."
            return
        async for out in first(messages, info):  # It reads the move's result and says it
            yield out

    fake_models.default = model
    socket_turns(client, db, secrets_key, first, "sposta la cena alle 21", "e domani?")
    assert db.execute("SELECT quoted FROM messages WHERE role = 'assistant' ORDER BY id").fetchall()[0] == (
        True,
    )
    said = [
        p.content for m in seen[0] if isinstance(m, ModelResponse) for p in m.parts if isinstance(p, TextPart)
    ]
    assert said and "is DATA" in said[0] and "«Cena»" in said[0]


# --- the review's decision: provider prompt caching with the stable prefix ---------------------------------


def test_the_stable_prefix_is_cached_and_the_turns_own_lines_come_after_it(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from pydantic_ai.models.test import TestModel

    from pisa.brain.conversation import TurnContext, build_agent, system_prompt
    from tests.test_acting import socket_turns

    seen: list[Any] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        seen.append(info)
        yield "Sono le tredici."

    fake_models.default = model
    socket_turns(client, db, secrets_key, Model("x", []), "che ore sono?")
    (info,) = seen
    agent = build_agent(TestModel(), TurnContext(owner=owner_at(), clock=lambda: datetime(2026, 10, 20)))
    settings = cast(dict[str, Any], agent.model_settings)
    # tools + system prompt, and the conversation: a turn's later rounds read its earlier ones
    assert settings["cache"] == {"messages": True}
    parts = info.model_request_parameters.instruction_parts
    assert (
        parts[0].content == system_prompt(lang="it-IT").strip() and not parts[0].dynamic
    )  # the cached prefix
    assert all(p.dynamic for p in parts[1:]) and "The user spoke Italian" in parts[1].content


# --- the review's decision: the real-model eval as a maintenance job through Pisa's gateway
# --------------------


def _price(eur: str | None) -> Any:
    from decimal import Decimal

    async def price(tokens_in: int, tokens_out: int) -> Any:
        return None if eur is None else Decimal(eur)

    return price


def test_the_eval_is_estimated_first_and_refused_above_its_cap(client, fake_models) -> None:  # type: ignore[no-untyped-def]
    from pisa_tools import talk_eval

    calls: list[str] = []

    async def model(messages, info):  # type: ignore[no-untyped-def]
        calls.append("x")
        yield "Ok."

    fake_models.default = model
    runtime = client.app.state.runtime
    over = client.portal.call(lambda: talk_eval.run(runtime, _price("0.05")))  # 13 cases at €0.05: €0.65
    assert over["refused"].startswith("estimated €0.650 is above €0.50") and not calls
    unknown = client.portal.call(lambda: talk_eval.run(runtime, _price(None)))
    assert "unknown" in unknown["refused"] and not calls
    estimate = client.portal.call(lambda: talk_eval.run(runtime, _price("0.001"), call=False))
    assert estimate == {"cases": 13, "estimate_eur": 0.013} and not calls


def test_the_eval_reports_rule_names_only_and_holds_the_two_new_cases(
    client, db, fake_models, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai.messages import UserPromptPart

    from pisa_tools import talk_eval
    from tests.conftest import set_key

    set_key(db, secrets_key)

    def careless(messages, info):  # type: ignore[no-untyped-def]
        words = str([p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)][-1])
        if words == "sì":
            return "Fatto, ho spostato la cena e avvisato Paola."  # the older question, not the check-in
        if words.startswith("alle 9 ho il treno"):
            return "Ho spostato la cena di domani alle 9."  # the train's time
        return "Ok."

    fake_models.default = careless
    out = client.portal.call(lambda: talk_eval.run(client.app.state.runtime, _price("0.001")))
    assert out["results"]["check-in-yes"]["broke"] == ["answered the older question"], out
    assert out["results"]["train-then-move"]["broke"] == ["moved to the train's time"]
    assert out["passed"] == 11 and "Paola" not in str(out) and "treno" not in str(out)  # numbers only
    calls = db.execute("SELECT budget, count(*) FROM llm_calls WHERE slot = 'chat.conversation' GROUP BY 1")
    assert calls.fetchall() == [("eval", 13)]  # through Pisa's gateway, on the evals' own budget


def test_a_yes_never_sends_a_body_the_reply_did_not_say_whole(client, db, google) -> None:  # type: ignore[no-untyped-def]
    """The model read back only the first line of a body carrying the user's data;
    the code-answered "sì" must not send it (no bundling), and a "mandala" waits for the whole text."""
    from tests.test_acting import act
    from tests.test_approvals import thread

    thread(google, db)
    actions = client.app.state.runtime.actions
    device = uuid.UUID(str(pair(db)[0]))
    body = "Ciao Marco, eccoti i dati.\nIBAN IT60X0542811101000000123456, passaporto YA1234567."
    act(client, actions.draft_reply, "Claudio cover", body, said="rispondi a Marco", device=device)
    (asked,) = db.execute("SELECT id::text FROM approvals WHERE status = 'pending'").fetchone()
    partial = "Ciao Marco, eccoti i dati. Lo mando?"
    assert client.portal.call(actions.bundle, device, [asked], partial) == []
    out = act(client, actions.send_draft, None, said="mandala", device=device, asked=[asked])
    assert not google.sent and "hasn't heard the whole text" in out
