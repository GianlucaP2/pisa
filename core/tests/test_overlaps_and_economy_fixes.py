"""The reviewer's findings on the overlap and economy fixes,
each through Pisa's real rules or services: an email confirms only what is truly over, a wait is a wait
at home only, each event keeps its own place, a note for the operator is a note only when said as one."""

import asyncio
import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from pisa.domain import event_kinds
from pisa.domain.home import Home
from tests.assistant_env import add_event, add_task, rome
from tests.conftest import PROTOCOL_DIR
from tests.owner import owner_at
from tests.places_env import at_location
from tests.test_planner import FAR, OFFICE, items_of, place, run, with_place

DAY = (2026, 10, 31)
AMAZON = ("Amazon.it", "ship-confirm@amazon.it")
ESSELUNGA = ("Esselunga a casa", "noreply@esselunga.it")


def _thing(db: Any, *items: tuple[str, str]) -> uuid.UUID:
    """A thing holding these (kind, ref) items."""
    thing = uuid.uuid4()
    db.execute(
        "INSERT INTO things (id, title, created_at, updated_at) VALUES (%s, 'x', now(), now())", (thing,)
    )
    for kind, ref in items:
        db.execute(
            "INSERT INTO thing_items (thing_id, item_kind, item_ref, linked_by, created_at) "
            "VALUES (%s, %s, %s, 'key', now())",
            (thing, kind, ref),
        )
    return thing


def _email(
    db: Any,
    now: datetime,
    category: str,
    *,
    thing: uuid.UUID | None = None,
    sender: tuple[str, str] = AMAZON,
    subject: str = "Consegnato",
) -> int:
    """The triage read the email as all over (`already_done`): what `_confirmed` closes."""
    from pisa.brain.intake import Triage
    from pisa.domain.mail import Address, MailMessage
    from pisa.services.intake import IntakeService
    from tests.test_proactivity import _attend

    verdict = Triage(category=category, automated=True, asks_owner_to_act=False, already_done=True,  # type: ignore[arg-type]
                     reason="x", notice="none")  # fmt: skip
    message = MailMessage("m1", "t1", Address(*sender), (), subject, now, ("INBOX",), "", None)

    async def go() -> int:
        async with _attend(db, now) as env:
            intake = IntakeService(env.pool, None, owner=owner_at(), clock=env.clock, attend=env.attend)  # type: ignore[arg-type]
            return await intake._confirmed(message, verdict, thing, now)

    return asyncio.run(go())


def _attended(db: Any) -> dict[str, Any]:
    return dict(db.execute("SELECT id, attended_at FROM calendar_events").fetchall())


def _status(db: Any, task: str) -> str:
    return db.execute("SELECT status FROM tasks WHERE id = %s", (task,)).fetchone()[0]


def _home(db: Any, place_id: str | None, coord: tuple[float, float]) -> None:
    value = Home(coord[0], coord[1], "Casa", "owner", True, place_id).stored()
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('profile_home', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb(value),),
    )


# --- 1: an email confirms only what is over: never a future event, a receipt never a parcel ----------


def test_1_a_booking_done_never_attends_tonight_s_dinner(db) -> None:  # type: ignore[no-untyped-def]
    now = rome(*DAY, 12, 30)
    dinner = add_event(db, "Cena da Mario", rome(*DAY, 20), minutes=150)
    lunch = add_event(db, "Pranzo da Mario", rome(*DAY, 10), minutes=90)  # over at 11:30
    thing = _thing(db, ("event", dinner), ("event", lunch))
    assert _email(db, now, "event_or_booking", thing=thing, sender=("Da Mario", "info@damario.example")) == 1
    attended = _attended(db)
    assert attended[dinner] is None and attended[lunch] == now  # never "Fatto: Cena da Mario ✓" at noon
    notes = db.execute("SELECT text FROM attend_candidates").fetchall()
    assert [t for (t,) in notes] == ["Fatto: Pranzo da Mario ✓"]


def test_1_a_receipt_never_closes_the_thing_s_parcel_task(db) -> None:  # type: ignore[no-untyped-def]
    now = rome(*DAY, 12, 30)
    task = add_task(db, "Ritira il pacco", rome(*DAY).date())
    thing = _thing(db, ("task", task))
    assert _email(db, now, "bill_or_deadline", thing=thing, subject="Ricevuta di pagamento") == 0
    assert _status(db, task) == "open"
    assert _email(db, now, "delivery", thing=thing) == 1  # the delivery's own email does
    assert _status(db, task) == "done"


def test_1_a_cancellation_is_never_done(db) -> None:  # type: ignore[no-untyped-def]
    now = rome(*DAY, 13)
    task = add_task(db, "Ricevere il pacco Amazon", rome(*DAY).date())
    wait = add_event(db, "Consegna pacchi Amazon", rome(*DAY, 12), minutes=240)
    thing = _thing(db, ("task", task), ("event", wait))
    assert _email(db, now, "delivery", thing=thing, subject="Il tuo ordine è stato annullato") == 0
    assert _status(db, task) == "open" and _attended(db)[wait] is None
    assert db.execute("SELECT count(*) FROM attend_candidates").fetchone() == (0,)


# --- 2: a wait is a clear at-home cue, with no fixed word and no place but home ---------------------


def test_2_only_a_clear_wait_at_home_is_a_wait() -> None:
    start, end = rome(*DAY, 12), rome(*DAY, 14)
    for title in ("Colloquio tecnico", "Riunione ufficio tecnico", "Consegna progetto al cliente",
                  "Consegna tesi", "Aspettare Marco in stazione", "Dentista (sala d'attesa)",
                  "Cena, poi aspetto il corriere"):  # fmt: skip
        assert event_kinds.infer(title, start, end).kind == "fixed", title
        assert not event_kinds.waits(title, at_home=True), title
    for title in ("Consegna spesa (resta a casa)", "Consegna pacchi Amazon", "Consegna cena",
                  "Aspetto il corriere", "Aspetto il tecnico Fastweb", "Visita del tecnico",
                  "Technician visit"):  # fmt: skip
        assert event_kinds.infer(title, start, end).kind == "window", title
    # with a place that is not home it is no wait: "Consegna spesa (resta a casa)" at the park
    assert event_kinds.infer("Consegna spesa (resta a casa)", start, end, home=False).kind == "fixed"
    assert not event_kinds.waits("Consegna spesa (resta a casa)", at_home=False)


def test_2_at_home_is_no_place_or_the_home_s() -> None:
    home = Home(45.4140, 9.1490, "Casa", "owner", True, "p-home")
    assert event_kinds.at_home(None, None, None) and event_kinds.at_home("p-home", None, home)
    assert event_kinds.at_home("p-other", (45.413, 9.149), home)  # ~78 m: the same block
    assert not event_kinds.at_home("p-park", FAR, home) and not event_kinds.at_home("p-park", FAR, None)


def test_2_the_plan_s_wait_at_the_park_is_no_window(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    park = place(db, "Parco Nord", FAR)
    wait = add_event(db, "Consegna pacchi Amazon", rome(*DAY, 12), minutes=240)
    with_place(db, "calendar_events", wait, park)
    plan = run(db, secrets_key, rome(*DAY, 11), lambda p, env: p.plan(), keys=())
    (item,) = items_of(plan, "event")
    assert item["event_kind"] == "fixed"


# --- 3: a delivery closes the wait of its own shop (or its thing), never every wait ------------------


def test_3_a_delivery_closes_only_its_shop_s_wait(db) -> None:  # type: ignore[no-untyped-def]
    now = rome(*DAY, 13, 10)
    groceries = add_event(db, "Consegna spesa Esselunga (resta a casa)", rome(*DAY, 12), minutes=240)
    fastweb = add_event(db, "Aspetto il tecnico Fastweb", rome(*DAY, 12), minutes=240)
    assert _email(db, now, "delivery", sender=AMAZON) == 0  # Amazon's "Delivered": not these
    assert _attended(db) == {groceries: None, fastweb: None}
    assert _email(db, now, "delivery", sender=ESSELUNGA) == 1
    assert _attended(db) == {groceries: now, fastweb: None}


def test_3_a_delivery_closes_its_thing_s_wait_whatever_the_title(db) -> None:  # type: ignore[no-untyped-def]
    now = rome(*DAY, 13, 10)
    wait = add_event(db, "Consegna spesa (resta a casa)", rome(*DAY, 12), minutes=240)
    assert _email(db, now, "delivery", sender=AMAZON) == 0
    assert _email(db, now, "delivery", sender=AMAZON, thing=_thing(db, ("event", wait))) == 1
    assert _attended(db)[wait] == now


# --- 4: each event keeps its own place: the wait at home in the park's block has no trip ------------


def test_4_a_placeless_wait_keeps_no_place_and_the_run_keeps_its_trip(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    at_location(db, OFFICE, rome(*DAY, 12, 59))
    park = place(db, "Parco Nord", FAR)
    add_event(db, "Consegna spesa (resta a casa)", rome(*DAY, 12), minutes=240)
    corsa = add_event(db, "Corsa", rome(*DAY, 14), minutes=60)
    with_place(db, "calendar_events", corsa, park)
    plan = run(db, secrets_key, rome(*DAY, 13), lambda p, env: p.plan(), keys=())
    by = {i["title"]: i for i in items_of(plan, "event")}
    wait = by["Consegna spesa (resta a casa)"]
    assert wait["place_id"] is None and "arrive_at" not in wait and "why" not in wait
    assert datetime.fromisoformat(wait["start"]) == rome(*DAY, 12)
    assert by["Corsa"]["place_id"] == park
    assert [i["to_place_id"] for i in items_of(plan, "travel")].count(park) == 1


# --- 5: a note for the operator only when said as one ------------------------------------------------


def test_5_a_note_for_the_architect_only_when_said_as_one() -> None:
    from pisa.domain.talk import for_architect

    for words in ("dillo all'architetto Marco", "scrivi all'architetto Rossi che arrivo tardi",
                  "compra un regalo per l'architetto", "porta i documenti all'architetto",
                  "sì, mandalo all'architetto", "cerca nei log del server",
                  "Per l'architetto Rossi: arrivo"):  # fmt: skip
        assert not for_architect(words), words
    for words in ("Questa è per l'architetto. Vedi che comunque sente ancora l'eco.",
                  "Nota per l'architetto: il piano unisce gli eventi",
                  "Pisa, per l'architetto: l'eco c'è ancora", "ok, nota per i log: l'eco",
                  "Note for the architect: the echo is back"):  # fmt: skip
        assert for_architect(words), words


# --- 6: "during" only when inside; the wait that holds the run "overlaps" it -------------------------


def test_6_the_wait_overlaps_the_run_and_the_run_is_during_the_wait(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock
) -> None:
    import json

    from tests.test_acting import socket_turns
    from tests.test_talk_timeline import Heard

    clock.now = rome(*DAY, 12, 40)
    wait = "Consegna spesa (resta a casa)"

    def at(h: int) -> str:
        return rome(*DAY, h).isoformat()

    plan = {"items": [
        {"kind": "event", "event_id": "w", "title": wait, "start": at(12), "end": at(16),
         "event_kind": "window",
         "overlaps": [{"event_id": "r", "title": "Corsa"}, {"event_id": "g", "title": "Palestra"}]},
        {"kind": "event", "event_id": "r", "title": "Corsa", "start": at(14), "end": at(15),
         "overlaps": [{"event_id": "w", "title": wait}]},
        {"kind": "event", "event_id": "g", "title": "Palestra", "start": at(15), "end": at(16),
         "overlaps": [{"event_id": "w", "title": wait}]},
    ]}  # fmt: skip
    db.execute(
        "INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (gen_random_uuid(), "
        "'2026-10-31', %s, 'test', 'f', %s)",
        (clock.now, json.dumps(plan)),
    )
    model = Heard()
    fake_models.default = model
    socket_turns(client, db, secrets_key, model, "a che ora è la corsa?")  # type: ignore[arg-type]
    said = model.instructions[0]
    assert f"12:00-16:00 {wait} (overlaps «Corsa», «Palestra»)" in said
    assert f"14:00-15:00 Corsa (during «{wait}»)" in said
    assert f"15:00-16:00 Palestra (during «{wait}»)" in said


# --- 7: being at home never ends a wait there; its confirmation does ---------------------------------


def test_7_two_hours_at_home_never_attend_the_wait(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_done import owner as owner_fixture
    from tests.test_done import run as done_run
    from tests.test_follow import fixes, track

    device = owner_fixture.__wrapped__(db)  # type: ignore[attr-defined]
    home = place(db, "Casa", OFFICE)
    _home(db, home, OFFICE)
    wait = add_event(db, "Consegna spesa (resta a casa)", rome(*DAY, 12), minutes=240)
    with_place(db, "calendar_events", wait, home)
    track(db, device, fixes(OFFICE, rome(*DAY, 12), rome(*DAY, 14, 20)))
    assert done_run(db, secrets_key, rome(*DAY, 14, 20), lambda done, f, env: done.tick()) == 0
    assert _attended(db)[wait] is None


# --- 8: the month's guard says it is the month's ------------------------------------------------------


def test_8_the_month_s_guard_says_the_month(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    import json
    from datetime import UTC

    from pydantic import SecretStr

    from pisa.domain.errors import PisaError
    from tests.conftest import pair, talk_naturally
    from tests.test_live_talk import FakeLink, FakeTalker, _service

    device = uuid.UUID(pair(db)[0])
    talk_naturally(db)  # natural with Grok Voice: the month's guard is what refuses
    now = datetime(2026, 11, 4, 10, tzinfo=UTC)
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at) VALUES (gen_random_uuid(), 'chat.conversation', 'x:y', 'settled', 0, 15, true, 't', %s)",
        (datetime(2026, 11, 2, 10, tzinfo=UTC),),
    )  # €15 of the default €20 month, nothing today
    make, service_for = _service(db, FakeTalker(), now)

    async def attempt() -> PisaError:
        pool = await make()
        try:

            async def link_for(rate: int) -> Any:
                return FakeLink()

            try:
                await service_for(pool, SecretStr(secrets_key)).open(
                    device,
                    trigger="press",
                    lang="it-IT",
                    link_for=link_for,
                    think=None,  # type: ignore[arg-type]
                )
            except PisaError as err:
                return err
            raise AssertionError("opened")
        finally:
            await pool.close()

    err = asyncio.run(attempt())
    assert err.extra["economy_reason"] == "month_guard"
    assert err.message == (
        "This month's AI spend passed 50% of a cap: Pisa talks in economy until the "
        "month's spend is under it."
    )
    fixture = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "talk_economy.json").read_text())
    (month,) = [r["error"] for r in fixture["refusals"] if r["error"].get("economy_reason") == "month_guard"]
    assert month["message"] == err.message


# --- 9: no turn end is not "SPOKE" ------------------------------------------------------------------


# --- 10: the contract says what `overlaps` is ---------------------------------------------------------------


def test_10_the_contract_says_what_overlaps_is() -> None:
    text = " ".join((PROTOCOL_DIR / "README.md").read_text().split())
    for rule in (
        '`overlaps` is always a list of objects `{"event_id", "title"}`: never a bare id, never a list of',
        "It is absent when the event overlaps nothing: never `null`, never `[]`.",
        "It is symmetric: the wait lists the run, and the run lists the wait.",
        "It names only events of the same route stop",
        "Two events that only touch (14:00-15:00, then 15:00-16:00) don't overlap.",
    ):
        assert rule in text, rule
