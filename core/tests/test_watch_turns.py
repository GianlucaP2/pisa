"""A run without the socket (`POST /v2/runs`, from the phone or the watch), `origin` recorded, one
interruption level rule, `notify.wrist`. Every word here is synthetic. Each item's tests fail on revert
(`scripts/review/revert_core_watch1.py`)."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from pydantic_ai.messages import SystemPromptPart

from pisa.brain.briefing import EMPTY_DAY
from pisa.db.pool import open_pool
from pisa.domain import wrist
from pisa.infra import ledger as spend
from pisa.infra.keys import KeyStore
from pisa.ports.push import PushOutcome
from pisa.services.notifications import Dispatcher, Kind, enqueue
from pisa.services.push import PushService
from pisa.services.session_messages import Notify
from tests.assistant_env import set_settings
from tests.conftest import Clock, dsn, pair, set_key
from tests.live_app import live, message_of
from tests.owner import owner_at
from tests.phone_signing import decide
from tests.test_approvals import dinner, google  # noqa: F401
from tests.test_push import Live
from tests.test_yescard import Says

TZ = "Europe/Rome"
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)  # Tuesday 11:00 in Rome: no quiet hours
GUESTS = [{"email": "owner@example.org", "self": True}, {"email": "paola@y.example", "displayName": "Paola"}]
WEATHER = "Domani piove dalle nove. Porta l'ombrello, e prendi la giacca pesante."


def socket_turn(client: Any, device_id: str, token: str, words: str) -> str:
    with live(client, token, device_id) as app:
        app.say(words, lang="it-IT", source="earbuds_mic")  # type: ignore[arg-type]
        frames: list[dict[str, Any]] = []
        while not frames or not (frames[-1]["type"] == "say" and frames[-1]["final"]):
            frames.append(app.next())
            assert frames[-1]["type"] != "error", frames[-1]
    return " ".join("".join(f.get("delta") or "" for f in frames if f["type"] == "say").split())


def systems(fake_models: Any) -> list[str]:
    """Each model request's system prompt, in order."""
    out = []
    for messages in fake_models.seen_messages:
        parts = [
            p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, SystemPromptPart)
        ]
        out.append("\n".join(parts))
    return out


# --- 1. a run without the socket (POST /v2/runs, which replaced POST /v1/turns) -----------------------------


def run_http(client: Any, token: str, words: str, *, watch: bool = False) -> tuple[int, list[dict[str, Any]]]:
    """A typed turn over contract 2's HTTP binding, from the phone or (`X-Pisa-Origin: watch`) the watch."""
    body = {
        "threadId": "thread-watch",
        "runId": f"run-{uuid.uuid4()}",
        "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": words}],
        "forwardedProps": {"pisa": {"turn": {"source": "typed", "lang": "it-IT"}}},
    }
    headers = {"Authorization": f"Bearer {token}"} | ({"X-Pisa-Origin": "watch"} if watch else {})
    out = client.post("/v2/runs", json=body, headers=headers)
    return out.status_code, out.json().get("events", [])


def said_in(events: list[dict[str, Any]]) -> str:
    return " ".join("".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT").split())


def test_1_the_briefing_phrase_is_the_same_over_http_and_the_socket(client, db, fake_models) -> None:  # type: ignore[no-untyped-def]
    """The shared path's first step: "Buongiorno, Pisa!" is the briefing on both, with no model."""
    device_id, token = pair(db)
    on_socket = socket_turn(client, device_id, token, "Buongiorno, Pisa!")
    status, events = run_http(client, token, "Buongiorno, Pisa!")
    assert status == 200 and said_in(events) == on_socket == EMPTY_DAY["it-IT"]
    assert fake_models.calls == [] and db.execute("SELECT count(*) FROM briefings").fetchone() == (2,)


def test_1_a_run_from_the_watch_is_recorded_from_the_watch_and_the_prompt_is_the_same(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    set_key(db, secrets_key)
    fake_models.default = Says({"che tempo fa domani": ([], WEATHER)})
    _, token = pair(db)
    phone = run_http(client, token, "che tempo fa domani?")
    watch = run_http(client, token, "che tempo fa domani?", watch=True)
    assert phone[0] == watch[0] == 200 and said_in(phone[1]) == said_in(watch[1]) == WEATHER
    first, second = systems(fake_models)
    assert first == second  # nothing added to the prompt for the wrist
    assert "orologio" not in second and "watch" not in second.lower()
    # the conversation keeps the whole answer, and where it was asked
    kept = db.execute("SELECT content, origin FROM messages WHERE role = 'assistant' ORDER BY id").fetchall()
    assert [(" ".join(c.split()), o) for c, o in kept] == [(WEATHER, "phone"), (WEATHER, "watch")]
    assert db.execute("SELECT origin FROM messages WHERE role = 'user' ORDER BY id").fetchall() == [
        ("phone",), ("watch",),
    ]  # fmt: skip


def test_1_a_stop_never_refuses_a_run_and_a_cap_ends_it(client, db, fake_models, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_key(db, secrets_key)
    fake_models.default = Says({"che tempo fa domani": ([], WEATHER)})
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/stop", json={}, headers=auth).status_code == 200  # "Pisa, ferma tutto"
    status, events = run_http(client, token, "che tempo fa domani?")  # Their own requests go on
    assert status == 200 and said_in(events) == WEATHER

    async def caps() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await spend.set_caps(conn, spend.Caps(spend.Decimal("0.0001"), spend.Decimal("20")))

    asyncio.run(caps())
    status, events = run_http(client, token, "e dopodomani?")
    assert status == 200 and events[-1]["type"] == "RUN_ERROR" and events[-1]["code"] == "budget_exceeded"


def test_1_a_si_on_a_run_decides_an_ordinary_approval_never_a_biometric_one(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key,  # noqa: F811
) -> None:  # fmt: skip
    """The phone's run without the socket: a "sì" decides what a yes in words decides, never a Face ID one."""
    dinner(google, db, attendees=GUESTS)
    set_key(db, secrets_key)
    fake_models.default = Says({
        "sposta la cena": ([("move_event", {"event": "la cena", "start": "oggi alle 21"})],
                           "Sposto «Cena» a oggi alle 21 e avviso Paola?"),
    })  # fmt: skip
    _, token = pair(db)
    assert said_in(run_http(client, token, "sposta la cena")[1]).endswith("?")
    db.execute("UPDATE approvals SET requires_biometric = true")  # a Face ID one: never by a yes in words
    run_http(client, token, "sì")
    assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",) and not google.writes
    db.execute("DELETE FROM approvals")
    run_http(client, token, "sposta la cena")
    said = said_in(run_http(client, token, "sì")[1])
    assert db.execute("SELECT status, decided_origin FROM approvals").fetchone() == ("done", "phone")
    assert google.writes and "spostat" in said


def test_from_the_wrist_a_si_never_approves_a_no_rejects(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key,  # noqa: F811
) -> None:  # fmt: skip
    """By code: an approval is seen and rejected on the wrist, approved on the phone."""
    from pisa.domain import yescard

    dinner(google, db, attendees=GUESTS)
    set_key(db, secrets_key)
    fake_models.default = Says({
        "sposta la cena": ([("move_event", {"event": "la cena", "start": "oggi alle 21"})],
                           "Sposto «Cena» a oggi alle 21 e avviso Paola?"),
    })  # fmt: skip
    _, token = pair(db)
    run_http(client, token, "sposta la cena", watch=True)
    said = said_in(run_http(client, token, "sì", watch=True)[1])
    assert said == yescard.on_the_phone("it-IT")
    assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",) and not google.writes
    (card,) = db.execute("SELECT id::text FROM approvals").fetchone()
    watch = {"Authorization": f"Bearer {token}", "X-Pisa-Origin": "watch"}
    refused = decide(client, watch, card)
    assert refused.status_code == 400 and "on the phone" in refused.json()["error"]["message"]
    assert db.execute("SELECT status FROM approvals").fetchone() == ("pending",) and not google.writes
    run_http(client, token, "sposta la cena", watch=True)  # Pisa asks again (a new card)
    run_http(client, token, "no", watch=True)  # a no from the wrist still rejects
    again = db.execute(
        "SELECT status, decided_origin FROM approvals WHERE id::text <> %s", (card,)
    ).fetchone()
    assert again == ("rejected", "watch")


# --- 2. origin recorded -------------------------------------------------------------------------------------


def test_2_x_pisa_origin_reaches_the_ledger_and_each_decision(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    _, token = pair(db)
    watch = {"Authorization": f"Bearer {token}", "X-Pisa-Origin": "watch"}
    assert client.post("/v1/stop", json={}, headers=watch).status_code == 200
    assert db.execute("SELECT stopped_origin FROM pisa_stops").fetchone() == ("watch",)
    assert db.execute("SELECT origin FROM pisa_decisions WHERE tool = 'stop_everything'").fetchone() == (
        "watch",
    )
    lines = [ln for d in client.get("/v1/ledger", headers=watch).json()["days"] for ln in d["lines"]]
    (stop,) = [ln for ln in lines if ln["kind"] == "stop"] or lines[:1]
    assert stop["origin"] == "watch" and stop["why"].endswith("dall'orologio")
    phone = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/stop/resume", headers=phone).status_code == 200
    assert db.execute("SELECT resumed_origin FROM pisa_stops").fetchone() == ("phone",)
    bad = client.post("/v1/stop", json={}, headers={**phone, "X-Pisa-Origin": "fridge"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_failed"


def test_2_a_reject_on_the_wrist_is_recorded_from_the_watch(client, db, google) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.test_yescard import run_on

    dinner(google, db)
    device, token = pair(db)
    run_on(client, device, client.app.state.runtime.actions.move_event, "la cena", "oggi alle 21", None)
    ((card,),) = db.execute("SELECT id::text FROM approvals").fetchall()
    headers = {"Authorization": f"Bearer {token}", "X-Pisa-Origin": "watch"}
    out = decide(client, headers, card, "reject")
    assert out.status_code == 200, out.text
    assert db.execute("SELECT status, decided_origin FROM approvals").fetchone() == ("rejected", "watch")


# --- 3. one interruption level rule -------------------------------------------------------------------------


class Sender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, token: str, environment: str, **kw: Any) -> PushOutcome:
        self.sent.append(kw)
        return PushOutcome("sent")


class Push:
    def __init__(self) -> None:
        self.apns = Sender()

    async def sender(self, conn: Any) -> Sender:
        return self.apns


def pushed(
    db: psycopg.Connection, device: str, path: str | None, *, kind: str = "info",
    text: str = "Una nota di prova: Fattura Acme 1.250 € da Marta.", **extra: Any,
) -> dict[str, Any]:  # fmt: skip
    """One notice stored on `path` and delivered to `device` with no socket: the push it made."""
    db.execute("UPDATE devices SET apns_token = %s, apns_environment = 'development'", ("ab" * 32,))
    push = Push()

    async def go() -> None:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        try:
            async with pool.connection() as conn:
                nid, _ = await enqueue(
                    conn, kind=cast(Kind, kind), text=text, lang="it-IT", now=NOW, timezone=TZ, path=path,
                    **extra,
                )  # fmt: skip
            await Dispatcher(
                pool, Live(set()), cast(PushService, push), clock=Clock(NOW), owner=owner_at(TZ)
            ).deliver(nid)
        finally:
            await pool.close()

    asyncio.run(go())
    (sent,) = push.apns.sent
    return sent


@pytest.mark.parametrize(
    ("path", "kind", "extra", "level"),
    [
        ("talk.last_words", "info", {}, "time-sensitive"),  # the guardian's now (their own words)
        ("turn.answer", "info", {"urgency": "breakpoint"}, "active"),  # A late answer
        ("attend.leave", "reminder", {"urgency": "now"}, "time-sensitive"),  # a floor (leave now): now
        ("attend.reminder", "reminder", {}, "active"),  # released at a breakpoint (they are idle)
        ("quiet.summary", "info", {}, "passive"),  # the quiet hours' digest
        ("guardian.digest", "info", {}, "passive"),  # the guardian's digest
    ],
)
def test_3_each_push_path_s_level(db, path, kind, extra, level) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    sent = pushed(db, device, path, kind=kind, **extra)
    assert sent["interruption"] == level


def test_3_his_never_shows_it_silently(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    set_settings(db, {"guardian": {"rules": [{"mode": "never", "kind": "leave"}]}})
    sent = pushed(
        db, device, "attend.leave", kind="reminder", urgency="now"
    )  # a floor: shown, never digested
    assert sent["interruption"] == "passive"


def test_3_an_approval_s_push_is_time_sensitive_with_its_wrist(client, db, google) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.test_yescard import run_on

    attendees = [{"email": "owner@example.org", "self": True}] + [
        {"email": f"p{i}@y.example", "displayName": f"Persona {i}"} for i in range(3)
    ]
    dinner(google, db, attendees=attendees)
    device, _ = pair(db)
    db.execute("UPDATE devices SET apns_token = %s, apns_environment = 'development'", ("ab" * 32,))
    push = Push()
    approvals = client.app.state.runtime.approvals
    approvals._push = push  # pyright: ignore[reportPrivateUsage]
    approvals._live = lambda: Live(set())  # pyright: ignore[reportPrivateUsage]
    run_on(client, device, client.app.state.runtime.actions.move_event, "la cena", "oggi alle 21", None)
    (sent,) = push.apns.sent
    assert sent["interruption"] == "time-sensitive" and sent["category"] == "APPROVAL"
    assert sent["data"]["wrist"] == "Spostare un evento · avvisa 3 persone"
    assert "Persona" not in json.dumps(sent["data"])


# --- 4. notify.wrist ----------------------------------------------------------------------------------------


def test_4_wrist_has_no_outside_text_even_about_an_email(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    set_settings(
        db, {"guardian": {"rules": [{"mode": "always", "kind": "info"}]}}
    )  # their "always": it goes now
    sent = pushed(db, device, "attend.intake", kind="info")
    line = sent["data"]["wrist"]
    assert line == "Una nota di Pisa"
    for word in ("Fattura", "Acme", "1.250", "€", "Marta", "nota di prova"):
        assert word not in line


def test_4_wrist_is_empty_at_a_sensitive_place(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    place = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, category, lat, lon, created_at, updated_at) "
        "VALUES (%s, 'Ospedale San Paolo', 'hospital', 45.397, 9.005, now(), now())",
        (place,),
    )
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, lat, lon, accuracy_m, last_seen_at, place_id) "
        "VALUES (%s, %s, %s, 45.397, 9.005, 20, %s, %s)",
        (uuid.uuid4(), device, NOW - timedelta(minutes=40), NOW - timedelta(minutes=2), place),
    )
    assert pushed(db, device, "attend.reminder", kind="reminder")["data"]["wrist"] == ""


def test_4_wrist_on_the_socket_is_the_same_line(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    live = Live({device})

    async def go() -> None:
        pool = await open_pool(dsn(db), max_size=4)
        await pool.wait()
        try:
            async with pool.connection() as conn:
                nid, _ = await enqueue(
                    conn, kind="reminder", text="Palestra alle 15:30 in via Immaginaria.", lang="it-IT",
                    now=NOW, timezone=TZ, path="attend.reminder",
                    event_at=NOW + timedelta(hours=4, minutes=30),
                )  # fmt: skip
            keys = KeyStore(SecretStr(Fernet.generate_key().decode()))
            push = PushService(pool, keys, httpx.AsyncClient(), team_id="T", topic="t")
            await Dispatcher(pool, live, push, clock=Clock(NOW), owner=owner_at(TZ)).deliver(nid)
        finally:
            await pool.close()

    asyncio.run(go())
    ((_, sent),) = live.sent
    assert sent.wrist == "Promemoria · alle 15:30"
    assert Notify(notification_id=sent.id, text=sent.text, lang="it-IT", kind="reminder", priority="normal",
                  wrist=sent.wrist).wrist == sent.wrist  # fmt: skip


def test_4_the_socket_s_notify_carries_wrist(client, db) -> None:  # type: ignore[no-untyped-def]
    device_id, token = pair(db)
    runtime = client.app.state.runtime

    async def notice() -> None:
        async with runtime.pool.connection() as conn:
            await enqueue(
                conn, kind="reminder", text="Dentista in via Immaginaria.", lang="it-IT", now=runtime.clock(),
                timezone=TZ, path="talk.last_words",
            )  # fmt: skip

    with live(client, token, device_id) as app:
        client.portal.call(notice)
        frame = app.next()
        while frame["type"] != "notify":
            frame = app.next()
    assert frame["wrist"] == "Promemoria" and "Dentista" not in frame["wrist"]
    assert isinstance(message_of(json.dumps(frame)), Notify)


@pytest.mark.parametrize("lang", ["it-IT", "en-US"])
def test_4_wrist_stays_within_60_characters_in_both_languages(lang: str) -> None:
    kinds = ["reminder", "briefing", "replan", "alert", "info", "check_in", "done", "coder_needs_you",
             "coder_done", "unknown"]  # fmt: skip
    lines = [wrist.wrist(k, lang) for k in kinds]
    lines += [wrist.wrist("info", lang, turn=True), wrist.wrist("info", lang, ledger_week="2026-W46"),
              wrist.wrist("info", lang, ritual="closing"), wrist.wrist("info", lang, weekly=True),
              wrist.wrist("alert", lang, speak="leave_now"), wrist.wrist("info", lang, path="quiet.summary"),
              wrist.wrist("reminder", lang, event_at=NOW, timezone="Europe/Rome")]  # fmt: skip
    lines += [
        wrist.approval_wrist(k, n, lang) for k in ("calendar_move", "email_send", "x") for n in (0, 1, 120)
    ]
    lines.append(wrist.approval_wrist("calendar_cancel", 10**60, lang))  # cut, never past the limit
    assert all(0 < len(line) <= wrist.WRIST_MAX for line in lines), [x for x in lines if len(x) > 60]
    assert wrist.wrist("info", lang, withhold=True) == ""


def test_4_wrist_withholds_health_and_sensitive_places() -> None:
    assert wrist.withheld(here=("Chiesa di San Marco", "church"), place=None, path=None)
    assert wrist.withheld(here=None, place="Studio dentistico", path=None)
    assert wrist.withheld(here=None, place=None, path="attend.reminder", about="Dentista alle 9:00")
    assert not wrist.withheld(here=None, place=None, path="attend.weather", about="Dentista alle 9:00")
    assert not wrist.withheld(here=("Bar Centrale", "cafe"), place="Palestra", path="attend.reminder")


def test_4_approval_wrist_counts_people_never_names_them() -> None:
    assert wrist.notified("calendar_move", {"notified": ["Paola", "Nino"], "send_updates": "all"}) == 2
    assert wrist.notified("calendar_move", {"notified": ["Paola"], "send_updates": "none"}) == 0
    assert wrist.notified("email_send", {"names": ["Paola Rossi", "Nino"]}) == 2
    assert wrist.approval_wrist("email_send", 1, "en-US") == "Send an email · notifies 1 person"
    assert wrist.approval_wrist("calendar_move", 3, "it-IT") == "Spostare un evento · avvisa 3 persone"


def test_4_level_rule() -> None:
    assert wrist.level("now") == "time-sensitive"
    assert wrist.level("breakpoint") == "active"
    assert wrist.level("digest") == wrist.level("breakpoint", digest=True) == "passive"
    assert wrist.level("now", silent=True) == "passive"


# --- review additions: the race, one turn at a time, the answer and never its interim lines ---------------


# --- round 2 (the ruling) ----------------------------------


def test_turns_follow_his_messages_retention_and_erase(db) -> None:  # type: ignore[no-untyped-def]
    from psycopg import AsyncConnection
    from psycopg.types.json import Jsonb

    from pisa.domain import erase as erase_rules
    from pisa.services import retention

    device_id = uuid.UUID(pair(db)[0])
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    for days in (200, 10):
        turn = uuid.uuid4()
        db.execute(
            "INSERT INTO turns (id, device_id, client_turn_id, origin, lang, state, say, created_at) "
            "VALUES (%s, %s, %s, 'watch', 'it-IT', 'done', 'Il migliore è il Forno Antico.', %s)",
            (turn, device_id, uuid.uuid4(), now - timedelta(days=days)),
        )
        db.execute(
            "INSERT INTO turn_sheets (id, turn_id, data, created_at) VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), turn, Jsonb({"title": "Colazione"}), now - timedelta(days=days)),
        )

    async def prune() -> dict[str, int]:
        async with await AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await retention.prune_messages(conn, now, "90d", TZ)

    assert asyncio.run(prune())["turns"] == 1
    assert db.execute("SELECT count(*) FROM turns").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM turn_sheets").fetchone() == (1,)  # its sheet went with it
    assert any(p.table == "turns" for p in erase_rules.FAMILIES["messages"])


def test_plan_items_carry_private_and_sensitive(
    client, db, clock, http_handler, owner_plans_every_task
) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_event, add_task, rome
    from tests.places_fake import FakePlaces

    http_handler["handler"] = FakePlaces()
    clock.now = rome(2026, 10, 20, 8, 0)
    _, token = pair(db)
    auth = {"Authorization": f"Bearer {token}"}
    secret = add_event(db, "Colloquio riservato", rome(2026, 10, 20, 11, 0))
    db.execute("UPDATE calendar_events SET visibility = 'private' WHERE id = %s", (secret,))
    add_event(db, "Visita", rome(2026, 10, 20, 15, 0), location="Ospedale San Paolo, Milano")
    add_event(db, "Pranzo con Nino", rome(2026, 10, 20, 13, 0))
    task = add_task(db, "Ritirare le analisi", rome(2026, 10, 20).date())
    patched = client.patch(f"/v1/tasks/{task}", json={"private": True}, headers=auth)
    assert patched.status_code == 200 and patched.json()["private"] is True
    items = client.get("/v1/plan", headers=auth).json()["items"]
    assert all("private" in i and "sensitive" in i for i in items)
    by_title = {i.get("title"): i for i in items if i.get("title")}
    assert (by_title["Colloquio riservato"]["private"], by_title["Colloquio riservato"]["sensitive"]) == (
        True,
        False,
    )
    assert by_title["Visita"]["sensitive"] is True and by_title["Visita"]["private"] is False
    assert (by_title["Pranzo con Nino"]["private"], by_title["Pranzo con Nino"]["sensitive"]) == (
        False,
        False,
    )
    assert by_title["Ritirare le analisi"]["private"] is True


def test_notify_carries_sensitive(db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    assert pushed(db, device, "attend.reminder", kind="reminder")["data"]["sensitive"] is False
    place = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, category, lat, lon, created_at, updated_at) "
        "VALUES (%s, 'Chiesa di San Marco', 'church', 45.397, 9.005, now(), now())",
        (place,),
    )
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, lat, lon, accuracy_m, last_seen_at, place_id) "
        "VALUES (%s, %s, %s, 45.397, 9.005, 20, %s, %s)",
        (uuid.uuid4(), device, NOW - timedelta(minutes=40), NOW - timedelta(minutes=2), place),
    )
    db.execute("DELETE FROM notifications")
    sent = pushed(db, device, "attend.reminder", kind="reminder")
    assert sent["data"]["sensitive"] is True and sent["data"]["wrist"] == ""


def test_a_reminder_about_his_health_fires_the_rail(db) -> None:  # type: ignore[no-untyped-def]
    """L1: the health rail fires on a real guardian path (his reminder, titled by his own item)."""
    device, _ = pair(db)
    sent = pushed(db, device, "attend.reminder", kind="reminder", summary="Dentista alle 9:00")
    assert sent["data"]["wrist"] == "" and sent["data"]["sensitive"] is True
    db.execute("DELETE FROM notifications")
    other = pushed(db, device, "attend.reminder", kind="reminder", summary="Palestra alle 9:00")
    assert other["data"]["wrist"] == "Promemoria" and other["data"]["sensitive"] is False


def test_google_s_visibility_is_read() -> None:
    from zoneinfo import ZoneInfo

    from pisa.connectors.google.calendar import parse_event

    item = {
        "id": "e1",
        "summary": "Colloquio",
        "status": "confirmed",
        "visibility": "confidential",
        "start": {"dateTime": "2026-10-20T11:00:00+02:00"},
        "end": {"dateTime": "2026-10-20T12:00:00+02:00"},
    }
    assert parse_event("primary", item, ZoneInfo(TZ)).visibility == "confidential"


def test_the_plan_flags_fixture_matches_the_model() -> None:
    from pisa.api import schemas
    from tests.conftest import PROTOCOL_DIR

    body = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "plan_watch_flags.json").read_text())
    items = [schemas.PlanItem.model_validate(i) for i in body["items"]]
    assert [(i.private, i.sensitive) for i in items] == [(True, False), (False, True), (False, False)]


def test_a_reminder_at_a_health_place_is_sensitive(db) -> None:  # type: ignore[no-untyped-def]
    """Review round 2: his reminder's own text names the place ("Visita (Ospedale San Paolo)"): the same
    rail as the plan item's `sensitive` (its event's location)."""
    device, _ = pair(db)
    sent = pushed(
        db, device, "attend.reminder", kind="reminder", summary="Visita alle 15:00",
        text="Tra 15 minuti: Visita (Ospedale San Paolo, Milano).",
    )  # fmt: skip
    assert sent["data"]["wrist"] == "" and sent["data"]["sensitive"] is True


def test_a_plan_item_titled_health_is_sensitive(  # type: ignore[no-untyped-def]
    client, db, clock, http_handler, owner_plans_every_task
) -> None:
    """`sensitive` is 'a sensitive place, an off-limits topic, or health': health by the item's own title
    too (as the notices' L1 rail), not only by its place or location."""
    from tests.assistant_env import add_event, add_task, rome
    from tests.places_fake import FakePlaces

    http_handler["handler"] = FakePlaces()
    clock.now = rome(2026, 10, 20, 8, 0)
    _, token = pair(db)
    add_event(db, "Dentista", rome(2026, 10, 20, 11, 0))
    add_event(db, "Visita cardiologica", rome(2026, 10, 20, 15, 0), location="Via Immaginaria 5, Milano")
    add_event(db, "Pranzo con Nino", rome(2026, 10, 20, 13, 0))
    add_task(db, "Ritirare le analisi", rome(2026, 10, 20).date())
    items = client.get("/v1/plan", headers={"Authorization": f"Bearer {token}"}).json()["items"]
    flags = {i["title"]: (i["private"], i["sensitive"]) for i in items if i.get("title")}
    assert flags["Dentista"] == flags["Visita cardiologica"] == flags["Ritirare le analisi"] == (False, True)
    assert flags["Pranzo con Nino"] == (False, False)
