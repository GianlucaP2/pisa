"""A day's bugs (O1-O9) and the
contract changes for overlapping events and for why the talk is in economy, each through Pisa's real
services."""

import json
from datetime import datetime
from typing import Any, ClassVar

from pisa.api import schemas
from tests.assistant_env import add_event, rome
from tests.owner import owner_at
from tests.places_env import at_location
from tests.test_planner import FAR, OFFICE, items_of, place, run, with_place
from tests.virtual_time import run_talk

DAY = (2026, 10, 31)
GROCERIES = "Consegna spesa (resta a casa)"


def _afternoon(db: Any) -> tuple[str, str, str]:
    """The grocery wait 12-16 with the run 14-15 and the gym 15-16 inside it."""
    wait = add_event(db, GROCERIES, rome(*DAY, 12), minutes=240)
    run_ = add_event(db, "Corsa", rome(*DAY, 14), minutes=60)
    gym = add_event(db, "Palestra", rome(*DAY, 15), minutes=60)
    return wait, run_, gym


def _at(item: dict[str, Any]) -> tuple[str, str]:
    return (f"{datetime.fromisoformat(item['start']):%H:%M}", f"{datetime.fromisoformat(item['end']):%H:%M}")


# --- overlapping events, each its own item; the grocery wait a window ----------------


def test_each_overlapping_event_is_its_own_item_with_its_times_and_overlaps(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    wait, run_, gym = _afternoon(db)
    plan = run(db, secrets_key, rome(*DAY, 11), lambda p, env: p.plan(), keys=())
    by = {i["title"]: i for i in items_of(plan, "event")}
    assert set(by) == {GROCERIES, "Corsa", "Palestra"}  # never "Consegna spesa (resta a casa) / Corsa / …"
    assert _at(by[GROCERIES]) == ("12:00", "16:00") and by[GROCERIES]["event_ids"] == [wait]
    assert _at(by["Corsa"]) == ("14:00", "15:00") and _at(by["Palestra"]) == ("15:00", "16:00")
    assert by[GROCERIES]["overlaps"] == [
        {"event_id": run_, "title": "Corsa"},
        {"event_id": gym, "title": "Palestra"},
    ]
    assert by["Corsa"]["overlaps"] == [{"event_id": wait, "title": GROCERIES}]  # 15:00 touches, no overlap
    # O3: the wait is a window (the run and the gym inside it on purpose); late only past its end
    assert by[GROCERIES]["event_kind"] == "window" and "why" not in by["Corsa"]
    schemas.Plan.model_validate(plan)


def test_the_trip_in_is_still_one_and_leave_now_works_on_the_block(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    at_location(db, OFFICE, rome(*DAY, 12, 59))
    park = place(db, "Parco Nord", FAR)
    _, run_, gym = _afternoon(db)
    with_place(db, "calendar_events", run_, park)
    with_place(db, "calendar_events", gym, park)
    plan = run(db, secrets_key, rome(*DAY, 13), lambda p, env: p.plan(), keys=())
    trips = [i for i in items_of(plan, "travel") if i["to_place_id"] == park]
    assert len(trips) == 1 and trips[0]["leave_at"]  # one trip to the run, the gym right after: no second
    corsa = next(i for i in items_of(plan, "event") if i["title"] == "Corsa")
    assert corsa["place_id"] == park


def test_the_wait_ends_when_the_delivery_is_confirmed(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    wait, _, _ = _afternoon(db)
    db.execute("UPDATE calendar_events SET attended_at = %s WHERE id = %s", (rome(*DAY, 13, 10), wait))
    plan = run(db, secrets_key, rome(*DAY, 13, 30), lambda p, env: p.plan(), keys=())
    by = {i["title"]: i for i in items_of(plan, "event")}
    assert _at(by[GROCERIES]) == ("12:00", "13:10")  # the groceries came: the wait is over
    assert "overlaps" not in by["Corsa"]


def test_a_delivery_wait_is_a_window_whatever_its_other_words() -> None:
    from pisa.domain import event_kinds

    for title in (GROCERIES, "Consegna cena", "Aspetto il corriere", "Technician visit"):
        assert event_kinds.infer(title, rome(*DAY, 12), rome(*DAY, 14)).kind == "window", title
    assert event_kinds.infer("Cena con Nino", rome(*DAY, 20), rome(*DAY, 23)).kind == "fixed"
    assert event_kinds.infer("Consegna", rome(*DAY, 12), rome(*DAY, 12, 15)).kind == "fixed"  # too short


def test_the_brain_reads_the_overlap_as_the_owner_sees_it(
    client, db, fake_models, secrets_key, clock
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    clock.now = rome(*DAY, 12, 40)

    def at(h: int) -> str:
        return rome(*DAY, h).isoformat()

    plan = {"items": [
        {"kind": "event", "title": GROCERIES, "start": at(12), "end": at(16), "event_kind": "window",
         "overlaps": [{"event_id": "r", "title": "Corsa"}]},
        {"kind": "event", "title": "Corsa", "start": at(14), "end": at(15),
         "overlaps": [{"event_id": "w", "title": GROCERIES}]},
    ]}  # fmt: skip
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
        "'2026-10-31', %s, 'test', 'f', %s)",
        (clock.now, json.dumps(plan)),
    )
    model = Heard()
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, "a che ora è la corsa?")  # type: ignore[arg-type]
    assert f"14:00-15:00 Corsa (during «{GROCERIES}»)" in model.instructions[0]


def test_the_fixture_is_the_contract() -> None:
    from tests.conftest import PROTOCOL_DIR

    plan = schemas.Plan.model_validate_json(
        (PROTOCOL_DIR / "fixtures" / "rest" / "plan_overlap.json").read_text()
    )
    events = [i for i in plan.items if i.kind == "event"]
    assert len(events) == 3 and all(e.event_ids and len(e.event_ids) == 1 for e in events)
    wait = next(e for e in events if e.event_kind == "window")
    assert wait.overlaps and {o.title for o in wait.overlaps} == {"Corsa", "Palestra"}


def test_arrivals_and_check_ins_work_on_each_event_of_the_block(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_done import done_notes, fixes, track
    from tests.test_done import owner as owner_fixture
    from tests.test_done import run as done_run

    device = owner_fixture.__wrapped__(db)  # type: ignore[attr-defined]
    park = place(db, "Parco Nord", FAR)
    wait, run_, gym = _afternoon(db)
    with_place(db, "calendar_events", run_, park)
    track(db, device, fixes(FAR, rome(*DAY, 14, 2), rome(*DAY, 14, 20)))
    assert done_run(db, secrets_key, rome(*DAY, 14, 20), lambda done, f, env: done.tick()) == 1
    ((text, payload),) = done_notes(db)
    assert payload["event_id"] == run_ and text == "Fatto: Corsa ✓"  # the run, not "Consegna spesa / Corsa"
    plan = run(db, secrets_key, rome(*DAY, 14, 21), lambda p, env: p.plan(), keys=())
    found = done_run(db, secrets_key, rome(*DAY, 14, 21), lambda done, f, env: f.statuses(plan))
    assert found[run_] == "done" and found.get(wait) != "done" and found.get(gym) != "done"


# --- O2: the user's afternoon, six changes in one breath, in one round ------------------------------

AFTERNOON = (
    "Amazon mi ha consegnato il pacco, segnalo come fatto. Alle 14 ho la corsa, poi palestra dalle 15 alle "
    "16, dalle 16 alle 16:30 stampo il costume in ufficio e dalle 17 alle 18 lo preparo"
)


def test_independent_writes_go_in_one_round_and_the_request_allows_parallel_calls(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from pisa.brain.conversation import system_prompt
    from tests.actions_fake import ActionsGoogle, connect
    from tests.assistant_env import add_task, set_settings
    from tests.test_brain_loop import Script, end_state, play

    google = ActionsGoogle()
    http_handler["handler"] = google
    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)
    clock.now = rome(*DAY, 12, 37)
    add_task(db, "Pacco Amazon", rome(*DAY).date())
    writes = [
        ("mark_done", {"what": "Pacco Amazon"}),
        ("create_event", {"title": "Corsa", "start": "oggi alle 14", "end": "15:00",
                          "owner_said": "Alle 14 ho la corsa"}),
        ("create_event", {"title": "Palestra", "start": "oggi alle 15", "end": "16:00",
                          "owner_said": "palestra dalle 15 alle 16"}),
        ("add_task", {"title": "Stampare il costume", "start": "2026-10-31T16:00", "duration_min": 30,
                      "place": "ufficio"}),
        ("add_task", {"title": "Preparare il costume", "start": "2026-10-31T17:00", "duration_min": 60}),
        ("finish", {"say": "Fatto: corsa alle 14, palestra 15-16, costume dalle 16 e 17; pacco segnato."}),
    ]  # fmt: skip

    class Seen(Script):
        settings: ClassVar[list[Any]] = []

        async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
            self.settings.append(info.model_settings or {})
            async for part in super().__call__(messages, info):
                yield part

    model = Seen({AFTERNOON: [writes]})
    fake_models.default = model
    _, _, latency = play(client, db, secrets_key, model, AFTERNOON)
    assert latency[0]["rounds"] <= 3 and latency[0]["tool_calls"] == 6  # the target: ≤ 3 rounds
    assert model.calls <= 2  # every write in the first round; finish said at once or one more call
    state = end_state(google)
    assert state["Corsa"].endswith("14:00") and state["Palestra"].endswith("15:00")
    # A thing they do at a time they said is in their day then, an event (or a task they timed)
    titles = {t for (t,) in db.execute("SELECT title FROM tasks WHERE status = 'open'").fetchall()}
    for title, at in (("Stampare il costume", "16:00"), ("Preparare il costume", "17:00")):
        assert title in titles or state.get(title, "").endswith(at), (title, state)
    assert db.execute("SELECT status FROM tasks WHERE title = 'Pacco Amazon'").fetchone() == ("done",)
    assert all(s.get("parallel_tool_calls") is True for s in model.settings)  # sent on the Responses API
    assert "in ONE round" in system_prompt(lang="it-IT")


# --- O4: a delivery confirmed by email closes its task, or the wait at home ---------------------------


def _confirm(
    db: Any, now: datetime, *, done: bool, category: str = "delivery", keys: Any = None, sender: Any = None
) -> int:
    import asyncio
    from types import SimpleNamespace

    from pisa.brain.intake import Triage
    from pisa.domain.mail import Address
    from pisa.services import things
    from pisa.services.intake import IntakeService
    from tests.test_proactivity import _attend

    verdict = Triage(category=category, automated=True, asks_owner_to_act=False, already_done=done,  # type: ignore[arg-type]
                     reason="consegnato", notice="none")  # fmt: skip

    name, address = sender or ("Amazon.it", AMAZON)

    async def go() -> int:
        async with _attend(db, now) as env:
            intake = IntakeService(env.pool, None, owner=owner_at(), clock=env.clock, attend=env.attend)  # type: ignore[arg-type]
            async with env.pool.connection() as conn:
                thing = await things.link_email(
                    conn, message_id="delivered", thread_id="new-thread", keys=keys or [],
                    subject="Consegnato", gist=None, now=now, sender=address,
                )  # fmt: skip
            message = SimpleNamespace(id="delivered", thread_id="new-thread", subject="Consegnato",
                                      snippet="", sender=Address(name, address))  # fmt: skip
            return await intake._confirmed(message, verdict, thing, now)  # type: ignore[arg-type]

    return asyncio.run(go())


ORDER = [{"kind": "order", "value": "402-1234567-7654321"}]
AMAZON = "ship-confirm@amazon.it"
ESSELUNGA_WAIT = "Consegna spesa Esselunga (resta a casa)"


def test_amazon_s_delivered_in_a_new_thread_closes_its_order_s_task(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from pisa.services import things
    from tests.test_proactivity import _attend

    now = rome(*DAY, 12, 30)
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels) "
        "VALUES ('order', 'order-thread', 'Amazon', %s, 'Ordine', now(), ARRAY['INBOX'])",
        (AMAZON,),
    )
    task = db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, source_ref, created_at, updated_at) "
        "VALUES (gen_random_uuid(), 'Ricevere il pacco Amazon', 'ricevere pacco amazon', 'open', 'email', "
        "'order', now(), now()) RETURNING id::text"
    ).fetchone()[0]

    async def link() -> None:
        async with _attend(db, now) as env, env.pool.connection() as conn:
            thing = await things.link_email(
                conn, message_id="order", thread_id="order-thread", keys=ORDER, subject="Ordine", gist=None,
                now=now, sender=AMAZON,
            )  # fmt: skip
            assert thing is not None
            await things.link_task(conn, thing, task, now)

    asyncio.run(link())
    assert _confirm(db, now, done=False, keys=ORDER) == 0  # "in consegna oggi": nothing closes
    assert _confirm(db, now, done=True, keys=ORDER) == 1
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("done",)
    assert db.execute("SELECT source FROM done_by_presence").fetchone() == ("email",)
    # Pisa says it once, as an outside text's words
    assert db.execute("SELECT kind, alone, outside FROM attend_candidates").fetchall() == [
        ("done", True, True)
    ]


def test_a_delivery_closes_today_s_wait_at_home_and_ends_it_in_the_plan(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    wait, _, _ = _afternoon(db)
    other = add_event(db, "Riunione", rome(*DAY, 12), minutes=120)
    # The wait closes for its own shop's email (its brand in the title)
    db.execute("UPDATE calendar_events SET title = %s WHERE id = %s", (ESSELUNGA_WAIT, wait))
    esselunga = ("Esselunga a casa", "noreply@esselunga.it")
    assert _confirm(db, rome(*DAY, 13, 10), done=True, category="bill_or_deadline", sender=esselunga) == 0
    assert _confirm(db, rome(*DAY, 13, 10), done=True, sender=esselunga) == 1
    attended = dict(db.execute("SELECT id, attended_at FROM calendar_events").fetchall())
    assert attended[wait] == rome(*DAY, 13, 10) and attended[other] is None  # only the wait at home
    payload = db.execute("SELECT delivery FROM attend_candidates").fetchone()[0]
    assert payload["done"]["event_id"] == wait
    plan = run(db, secrets_key, rome(*DAY, 13, 30), lambda p, env: p.plan(), keys=())
    spesa = next(i for i in items_of(plan, "event") if i["title"] == ESSELUNGA_WAIT)
    assert _at(spesa) == ("12:00", "13:10")


# --- O5: the user's words earlier in the talk still stand; a done item is never proposed again -------


def test_the_owner_s_earlier_words_stand_after_an_email_and_a_done_item_is_never_proposed_again(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock
) -> None:
    from tests.assistant_env import add_task, set_settings
    from tests.test_brain_loop import Script, play

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    clock.now = rome(*DAY, 12, 38)
    task = add_task(db, "Pacco Amazon", rome(*DAY).date())
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet) VALUES ('amz', 'amz', 'Amazon', %s, 'Consegnato: il tuo pacco', %s, ARRAY['INBOX'], "
        "'Il tuo pacco è stato consegnato')",
        (AMAZON, rome(*DAY, 11)),
    )
    first = "il pacco di Amazon è arrivato, lo puoi segnare come fatto"
    second = "controlla la mail di Amazon"
    third = "e il pacco?"
    model = Script({
        first: ["Lo controllo tra un attimo."],  # answered without the write (the talk went on)
        second: [[("search_my_email", {"query": "Amazon"})], [("mark_done", {"what": "Pacco Amazon"})]],
        third: [[("mark_done", {"what": "Pacco Amazon"})]],
    })  # fmt: skip
    fake_models.default = model
    play(client, db, secrets_key, model, first, second, third)
    assert db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone() == ("done",)
    assert db.execute("SELECT count(*) FROM approvals").fetchone() == (0,)  # never "Posso segnare…?"
    last = [str(p.content) for p in model.messages[-1][-1].parts if hasattr(p, "tool_name")]
    assert any("Already done today: Pacco Amazon" in r for r in last)


# --- O6, O8: noise gets silence; "talker deaf" only when the user's turn ended with no words ---


def test_the_talker_s_rules_answer_noise_with_silence() -> None:
    from pisa.domain.whisper import Fresh, instructions

    rules = instructions(Fresh("sabato 31 ottobre, 12:40"), "", "it-IT", pisa_lang="it-IT")
    assert 'Noise, a cough, a lone sound or word you can\'t make sense of ("A", "AJ", "Mmm")' in rules
    assert 'Never "Dimmi pure, sono qui"' in rules


def test_talker_deaf_is_noted_once_after_the_owner_stops_with_no_words(monkeypatch: Any) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from pisa.ports.talker import OwnerWords
    from pisa.services import talk
    from tests.test_live_talk import FakeLink, _run
    from tests.test_talk_timeline import LOUD, QUIET, kinds

    class Monologue(FakeLink):
        async def audio_in(self, rate: int) -> AsyncIterator[bytes]:
            started = asyncio.get_running_loop().time()
            while True:
                talking = asyncio.get_running_loop().time() - started < 0.3
                yield LOUD if talking else QUIET
                await asyncio.sleep(0.005)

    monkeypatch.setattr(talk, "TICK_S", 0.01)
    monkeypatch.setattr(talk, "DEAF_AFTER_S", 0.1)

    def session(words_at_end: bool) -> list[str]:
        run, talker, _, _ = _run()
        run.link = Monologue()

        async def go() -> None:
            task = asyncio.create_task(run.run())
            await asyncio.sleep(0.32)
            if words_at_end:  # Gemini transcribes the monologue when it ends
                talker.queue.put_nowait(OwnerWords("ecco il mio pomeriggio", final=True))
            await asyncio.sleep(0.3)
            run.end("owner")
            await task

        run_talk(go())
        return kinds(run)

    deaf = session(words_at_end=False)
    assert deaf.count("talker_deaf") == 1  # once, after the voice stopped: not every 9 s of the monologue
    assert "talker_deaf" not in session(words_at_end=True)


# --- O7: a note for the operator is the user's feedback, never a message --------------------------


def test_a_note_for_the_architect_is_stored_and_said_back_never_a_message(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock
) -> None:
    from pisa.domain.talk import for_architect
    from tests.test_brain_loop import Script, play, said

    clock.now = rome(*DAY, 12, 50)
    note = "Questa è per l'architetto: dall'app risulta ancora tutto un evento"
    model = Script({note: [[("compose_message", {"to": "architetto", "text": "tutto un evento"})]]})
    fake_models.default = model
    frames, _, _ = play(client, db, secrets_key, model, note)
    assert said(frames[0]) == "Segnato per l'operatore." and model.calls == 0  # no model, no message
    (words, at), *_ = db.execute("SELECT words, said_at FROM owner_feedback").fetchall()
    assert words == note and at == clock.now
    assert db.execute("SELECT count(*) FROM approvals").fetchone() == (0,)
    for words in ("nota per l'architetto: il pranzo era sbagliato", "for the architect: the gym moved",
                  "per i log: il piano unisce gli eventi", "nota per l'operatore: il pranzo era sbagliato",
                  "for the operator: the gym moved"):  # fmt: skip
        assert for_architect(words), words
    assert not for_architect("chiama l'architetto Rossi") and not for_architect("manda un messaggio a Nino")
    assert not for_architect("chiama l'operatore Rossi") and not for_architect("tell the operator Rossi hi")


# --- row 9: one holding phrase per wait, across utterances -----------------------------------------------


def test_one_holding_phrase_per_wait_across_utterances(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    import uuid as uuid_

    from tests.conftest import pair, set_key
    from tests.test_talking_like_a_person import slow

    set_key(db, secrets_key)
    device = uuid_.UUID(pair(db)[0])
    conversations = client.app.state.runtime.conversations
    conversations.hold_after_s = 0.05
    fake_models.default = slow("Sono le tredici.", 0.4)
    held: list[str] = []

    async def hold(phrase_id: str, text: str) -> None:
        held.append(text)

    async def emit(delta: str, final: bool) -> None:
        return None

    async def say(words: str, cut: bool) -> None:
        turn = asyncio.ensure_future(conversations.respond(device, words, "it-IT", emit, hold=hold,
                                                           trace_id=uuid_.uuid4().hex))  # fmt: skip
        if cut:  # the user speaks again while Pisa works: this reply is dropped for the new words
            await asyncio.sleep(0.2)
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        else:
            await turn

    async def wait() -> None:
        await say("che ore sono", cut=True)
        await say("che ore sono adesso", cut=True)
        await say("allora, che ore sono?", cut=False)

    client.portal.call(wait)
    assert len(held) == 1  # one "Vediamo…" for the whole wait, not one per utterance
    held.clear()
    client.portal.call(say, "e domani?", False)
    assert len(held) == 1  # a new wait after Pisa's reply: its own holding phrase


# --- Why the talk is in economy -----------------------------------------------------------------------


def test_the_refusal_says_why_the_talk_is_in_economy(client, db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.assistant_env import set_settings
    from tests.conftest import PROTOCOL_DIR, pair, set_key

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    body = {"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT"}

    def refused(mode: str = "natural") -> dict[str, Any]:
        set_settings(db, {"conversation": {"mode": mode, "talker": "gemini",
                                           "voices": {"xai": "eve", "gemini": "Aoede"}}})  # fmt: skip
        answer = client.post("/v1/talk/sessions", headers=auth, json=body)
        assert answer.status_code == 409
        return dict(answer.json()["error"])

    assert refused("economy")["economy_reason"] == "setting"
    missing = refused()
    assert missing["code"] == "provider_key_missing" and missing["provider"] == "google"
    set_key(db, secrets_key, provider="google")
    now = clock()  # the app's clock, not the wall clock: in November the "1st" was the app's future

    def spent(eur: float, at: datetime) -> None:
        db.execute(
            "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, "
            "trace_id, created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'x:y', 'settled', 0, %s, "
            "true, 't', %s)",
            (eur, at),
        )

    spent(500.0, now.replace(day=1, hour=0, minute=1) if now.day > 1 else now - timedelta(minutes=1))
    month = refused()
    assert month["code"] == "talk_economy"
    assert month["economy_reason"] == ("month_guard" if now.day > 1 else "day_guard")
    spent(500.0, now - timedelta(minutes=1))
    assert refused()["economy_reason"] == "day_guard"
    fixture = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "talk_economy.json").read_text())
    reasons = [schemas.ErrorResponse.model_validate(r).error for r in fixture["refusals"]]
    assert {e.economy_reason for e in reasons} == {"setting", "day_guard", "month_guard", None}
    assert reasons[-1].provider == "google"


# --- the leftovers (more in test_talk_timeline and test_one_brain) ----------------------------------------


def test_the_timeline_keeps_room_for_its_close_and_is_stored_however_the_run_ends(
    monkeypatch: Any,
) -> None:
    import asyncio
    import uuid as uuid_
    from types import SimpleNamespace

    from pisa.services import talk

    monkeypatch.setattr(talk, "TIMELINE_MAX", 3)
    timeline = talk.Timeline()
    for n in range(5):
        timeline.add("audio", n=n)
    timeline.add("close", reason="idle")
    assert [e["kind"] for e in timeline.events] == [
        "audio",
        "audio",
        "audio",
        "close",
    ] and timeline.dropped == 2

    stored: list[str] = []

    class Cancelled:
        async def run(self) -> str:
            raise asyncio.CancelledError

    async def fake_stored(opened: Any, run: Any, device: Any, reason: str) -> None:
        stored.append(reason)

    service = talk.TalkService(None, None, {}, owner=owner_at(), clock=datetime.now)  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_stored", fake_stored)

    async def go() -> None:
        opened = SimpleNamespace(session_id=uuid_.uuid4())
        await service._run(opened, Cancelled(), uuid_.uuid4())  # type: ignore[arg-type]

    try:
        run_talk(go())
    except asyncio.CancelledError:
        pass
    assert stored == ["error"]  # a cancelled run (Pisa's shutdown) still stores its timeline


def test_two_requests_that_differ_by_a_real_word_are_never_one_utterance() -> None:
    from pisa.services.talk import one_utterance

    two = "metti nella lista pane, metti nella lista panettone"
    assert one_utterance(two) == two
    grown = "Che ora ho l'appunta Che ora ho l'appuntamento?"
    assert one_utterance(grown) == "Che ora ho l'appuntamento?"  # a word the transcript cut short still folds


def test_the_shared_state_starts_fresh_with_a_new_conversation() -> None:
    import uuid as uuid_
    from datetime import timedelta

    from pisa.domain.state import STATE_FOR
    from pisa.services.conversation import NEW_CONVERSATION_AFTER
    from pisa.services.reflex import PisaReads, ReflexService

    now = [rome(*DAY, 12)]
    reflex = ReflexService(None, PisaReads(None, None, None, owner=owner_at(), clock=lambda: now[0]),
                           owner=owner_at(), clock=lambda: now[0])  # fmt: skip
    device = uuid_.uuid4()
    reflex.state(device).pisa_said("Ti avviso alle 5.", "voice", now[0])
    now[0] += timedelta(minutes=20)
    assert reflex.state(device).said  # the same conversation
    now[0] += timedelta(minutes=31)
    assert not reflex.state(device).said  # hours-old lines never reach the next brain turn
    assert STATE_FOR == NEW_CONVERSATION_AFTER
