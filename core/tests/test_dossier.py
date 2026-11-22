# ruff: noqa: E501
"""One finite morning. Each test guards a row of
`tests/mutate_nb2_dossier.py` and fails with its fix undone. Every word here is invented."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb
from structlog.testing import capture_logs

from pisa.domain import actuals as A
from pisa.domain import dossier as D
from pisa.domain import night as night_rules
from pisa.domain import rituals as R
from pisa.domain import voice1 as knock_rules
from tests.conftest import pause_outbox
from tests.owner import owner_at
from tests.test_knock import (
    DAY,
    _decide_as,
    _delivered,
    _event,
    _phone,
    _say,
    _settings,
    _task,
    _thought,
    _tick,
    local,
)

TODAY = date.fromisoformat(DAY)  # Wednesday 11 November 2026
NOW = local(f"{DAY}T08:30")


def rt(client: Any) -> Any:
    return client.app.state.runtime


# --- the world: one of each source ---------------------------------------------------------------------------


def _obligation(db: Any, what: str, due: date, state: str = "upcoming", ref: str | None = None) -> uuid.UUID:
    oid = uuid.uuid4()
    db.execute(
        "INSERT INTO obligations (id, kind, what, due_on, source, source_ref, state, created_at, updated_at) "
        "VALUES (%s, 'documento', %s, %s, 'vault', %s, %s, now(), now())",
        (oid, what, due, ref or f"doc:{oid}", state),
    )
    return oid


def _intention(db: Any, what: str, *, kind: str = "mine", state: str = "open", due: date | None = TODAY,
               source: str = "voice", who: str | None = None) -> uuid.UUID:  # fmt: skip
    iid = uuid.uuid4()
    closed = datetime.now(UTC) if state == "done" else None
    db.execute(
        "INSERT INTO intentions (id, kind, state, what, who, due_on, source, created_at, updated_at, closed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s)",
        (iid, kind, state, what, who, due, source, closed),
    )
    return iid


def _draft(db: Any, at: datetime, sent: bool = False) -> uuid.UUID:
    did = uuid.uuid4()
    db.execute(
        "INSERT INTO email_drafts (id, gmail_draft_id, thread_id, reply_to_id, in_reply_to, to_addresses, "
        "subject, body, created_at, updated_at, sent_at) VALUES (%s, 'g', 't', 'm', '<x>', "
        "ARRAY['zeno@example.invalid'], 'Re: Il preventivo', 'Ciao Zeno, va bene.', %s, %s, %s)",
        (did, at, at, at if sent else None),
    )
    return did


def _world(db: Any) -> dict[str, Any]:
    """Five things this morning: the day's next event (floor), an obligation due tomorrow (radar), an
    intention due today (loop), a draft from the night (draft), Pisa's morning thought (thought)."""
    _settings(db)
    event = _event(db, "Dentista", local(f"{DAY}T11:00"))
    task = _task(db, "Sistemare la cantina", local("2026-10-20T09:00"))
    _thought(db, task)
    return {
        "event": event,
        "obligation": _obligation(db, "Rinnovo della tessera della biblioteca", TODAY + timedelta(days=1)),
        "intention": _intention(db, "chiamare l'idraulico"),
        "draft": _draft(db, local(f"{DAY}T03:40")),
    }


def _morning(client: Any, db: Any, clock: Any, device: uuid.UUID) -> str:
    _decide_as(client)
    _tick(client, clock, f"{DAY}T08:30")
    assert _delivered(client, db, device) is not None
    said, by_code = _say(client, device, "dimmi")
    assert by_code  # no model round
    return said


def _items(db: Any) -> list[tuple[str, bool, str]]:
    return [
        (str(k), bool(s), str(t))
        for k, s, t in db.execute(
            "SELECT kind, spoken, text FROM dossier_items WHERE current ORDER BY NOT spoken, rank"
        ).fetchall()
    ]


# --- 1. one knock, finite ----------------------------------------------------------------------------------


def _item(kind: D.Kind, text: str, ref: str, **kw: Any) -> D.Item:
    return D.Item(kind, text, "t", ref, (D.Source("t", ref),), D.RANK[kind], **kw)


def test_1_a_fourth_item_is_never_spoken_rules() -> None:
    items = [
        _item(k, f"Cosa numero {n}.", str(n))
        for n, k in enumerate(("floor", "loop", "draft", "thought", "sheet"))
    ]
    out = D.assemble(items)
    assert [i.spoken for i in out] == [True, True, True, False, False]
    said = D.spoken(out, "it-IT")
    assert "Cosa numero 3" not in said and "Cosa numero 4" not in said
    # already said three this morning: a second reading never adds a fourth
    again = D.assemble(
        [*items, _item("provato", "Urgente.", "9")], said=frozenset({("t", "0"), ("t", "1"), ("t", "2")})
    )
    assert sum(i.spoken for i in again) == 3 and not next(i for i in again if i.ref == "9").spoken


def test_1_a_fourth_item_is_never_spoken(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _world(db)
    device = _phone(db)
    said = _morning(client, db, clock, device)
    items = _items(db)
    assert len(items) == 5 and sum(s for _, s, _ in items) == 3
    assert [k for k, s, _ in items if s] == ["floor", "radar", "loop"]  # the most pressing first
    assert said == (
        "Oggi alle 11:00 hai Dentista. Rinnovo della tessera della biblioteca: scade domani. "
        "Da fare oggi: chiamare l'idraulico. Le altre 2 sono sul telefono. È tutto."
    )
    for _, spoken, text in items:
        if not spoken:
            assert text.split(":")[0] not in said  # the draft and the thought: only on the phone
    # "dimmi" again: the same three, never a fourth
    assert _say(client, device, "dimmi")[0] == said
    assert db.execute("SELECT count(*) FROM dossier_items WHERE spoken").fetchone()[0] == 3


def test_1_the_table_refuses_a_fourth_spoken_item(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _world(db)
    _morning(client, db, clock, _phone(db))
    with pytest.raises(Exception, match="at most three"):
        db.execute("UPDATE dossier_items SET spoken = true, said_at = now() WHERE NOT spoken")


def test_1_e_tutto_closes_the_spoken_part(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    one = D.assemble([_item("floor", "Oggi alle 9:00 hai Yoga.", "a")])
    assert D.spoken(one, "it-IT") == "Oggi alle 9:00 hai Yoga. È tutto."
    assert D.spoken(one, "en-US").endswith("That's all.")
    four = D.assemble(
        [
            _item("floor", "A.", "a"),
            _item("loop", "B.", "b"),
            _item("draft", "C.", "c"),
            _item("sheet", "D.", "d"),
        ]
    )
    assert D.spoken(four, "it-IT") == "A. B. C. L'altra è sul telefono. È tutto."
    _world(db)
    assert _morning(client, db, clock, _phone(db)).endswith(" È tutto.")


def test_1_nothing_reads_as_done_unless_it_was_done_rules() -> None:
    for text in ("Ho inviato la mail a Zeno.", "Bollo pagato.", "Cena prenotata per le 20.", "Fatto: la spesa.",
                 "The invoice was sent.", "Tutto completato."):  # fmt: skip
        assert D.reads_done(text), text
        assert not D.speakable(_item("loop", text, "x"))  # the record isn't done: never said
        assert D.speakable(_item("loop", text, "x", done=True))  # done (and read back): it may say so
    for text in ("Una bozza di risposta aspetta il tuo sì.", "Da fare oggi: chiamare l'idraulico.",
                 "Rinnovo della tessera: scade domani."):  # fmt: skip
        assert not D.reads_done(text), text
    out = D.assemble(
        [_item("loop", "Bonifico mandato a Zeno.", "a"), _item("draft", D.drafts_line(1, "it-IT"), "b")]
    )
    assert [i.spoken for i in out] == [False, True]  # the false "done" goes to the phone, never aloud


def test_1_nothing_reads_as_done_unless_it_was_done(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _intention(db, "bonifico mandato a Zeno")  # their words read "sent"; the loop is still open
    _intention(db, "comprare il pane", state="done")  # done: never a loop of the morning
    _obligation(db, "Bollo della moto", TODAY, state="done")  # done: the radar never shows it
    _draft(db, local(f"{DAY}T03:00"))  # a draft is never "sent"
    _draft(db, local(f"{DAY}T03:10"), sent=True)  # one that left is no longer a draft
    said = _morning(client, db, clock, _phone(db))
    assert said == "Una bozza di risposta aspetta il tuo sì. L'altra è sul telefono. È tutto."
    items = _items(db)
    assert ("loop", False, "Da fare oggi: bonifico mandato a Zeno.") in items  # shown, never said
    assert not any("pane" in t or "moto" in t for _, _, t in items)
    assert not any(D.reads_done(t) for _, s, t in items if s)


def test_1_outside_text_is_never_spoken(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    waiting = _intention(
        db, "il contratto", kind="waiting_for", source="received_mail", who="Zeno"
    )  # a stranger's
    db.execute(
        "UPDATE intentions SET nudged_at = now() WHERE id = %s", (waiting,)
    )  # the loops' own nudge: done
    knock = rt(client).conversations.deps.knock.knock
    assert (
        client.portal.call(knock, NOW.astimezone(UTC)) == 0
    )  # only a stranger's words: nothing to say, no knock
    _event(db, "Dentista", local(f"{DAY}T11:00"))
    said = _morning(client, db, clock, _phone(db))
    assert said == "Oggi alle 11:00 hai Dentista. L'altra è sul telefono. È tutto."
    assert db.execute("SELECT outside, spoken FROM dossier_items WHERE kind = 'loop'").fetchone() == (
        True,
        False,
    )


def test_1_each_item_names_the_sources_read_and_the_log_carries_ids_only(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    w = _world(db)
    task = db.execute("INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at, due_at, "
                      "due_kind) VALUES (gen_random_uuid(), 'Portare la bici', 'portare la bici', 'open', 'manual', now(), "
                      "now(), %s, 'datetime') "
                      "RETURNING id", (local(f"{DAY}T18:00"),)).fetchone()[0]  # fmt: skip
    linked = _intention(db, "portare la bici", due=None)
    db.execute("UPDATE intentions SET task_id = %s, cue = 'time' WHERE id = %s", (task, linked))
    with capture_logs() as logs:
        _morning(client, db, clock, _phone(db))
    sources = {
        str(k): s for k, s in db.execute("SELECT kind || ':' || ref, sources FROM dossier_items").fetchall()
    }
    assert sources[f"radar:{w['obligation']}"] == [{"table": "obligations", "id": str(w["obligation"])}]
    assert sources[f"loop:{linked}"] == [
        {"table": "intentions", "id": str(linked)},
        {"table": "tasks", "id": str(task)},
    ]
    assert sources[f"draft:{w['draft']}"] == [{"table": "email_drafts", "id": str(w["draft"])}]
    assert sources[f"floor:{w['event']}"] == [{"table": "calendar_events", "id": w["event"]}]
    lines = [e for e in logs if e.get("event") in ("dossier_item", "dossier_built")]
    assert {e["event"] for e in lines} == {"dossier_item", "dossier_built"}
    assert any(f"obligations:{w['obligation']}" in e.get("sources", []) for e in lines)
    text = repr(lines)
    for words in ("Dentista", "biblioteca", "idraulico", "bici", "cantina", "bozza"):
        assert words not in text  # ids and counts only


def test_1_thumbs_on_the_phone(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    _world(db)
    _, token = pair(db, "iPhone")
    auth = {"Authorization": f"Bearer {token}"}
    clock.now = NOW.astimezone(UTC)
    got = client.get("/v1/dossier", headers=auth)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["day"] == DAY and body["closing"] == "È tutto." and body["said_at"] is None
    assert len(body["items"]) == 5 and not any(i["spoken"] for i in body["items"])  # the phone never speaks
    first = body["items"][0]
    assert first["sources"] and first["thumb"] is None
    up = client.post(f"/v1/dossier/items/{first['id']}/thumb", json={"thumb": "down"}, headers=auth)
    assert up.json() == {"id": first["id"], "thumb": "down"}
    again = client.get("/v1/dossier", headers=auth).json()
    assert next(i for i in again["items"] if i["id"] == first["id"])["thumb"] == "down"  # kept by a rebuild
    assert (
        client.post(f"/v1/dossier/items/{first['id']}/thumb", json={"thumb": None}, headers=auth).json()[
            "thumb"
        ]
        is None
    )
    gone = client.post(f"/v1/dossier/items/{uuid.uuid4()}/thumb", json={"thumb": "up"}, headers=auth)
    assert gone.status_code == 404
    assert (
        client.post(f"/v1/dossier/items/{first['id']}/thumb", json={"thumb": "meh"}, headers=auth).status_code
        == 400
    )
    assert client.get("/v1/dossier").status_code == 401


def test_1_the_dossier_s_knock_goes_through_the_guardian_s_gate(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.test_guardian import deliver, item_row, set_settings

    _settings(db)
    _obligation(db, "Rinnovo della tessera della biblioteca", TODAY)  # only the radar has something today
    device = _phone(db)
    set_settings(
        db, {"guardian": {"rules": [{"mode": "never", "kind": "ask"}]}}
    )  # their rule: Pisa's asks never aloud
    asked = _decide_as(client)
    # The app's outbox listener is stopped first, so the delivery below is the notice's only one. Running, it
    # took the tick's notice every time: the test's own delivery found it locked or done (None), its Live got
    # nothing, and the gate's record was read before the listener's transaction had committed now and then.
    pause_outbox(client)
    _tick(client, clock, f"{DAY}T08:30")
    assert asked == ["knock"]  # the radar alone knocks, through attend
    nid, payload = db.execute(
        "SELECT n.id, a.payload FROM notifications n JOIN ritual_asks a ON a.id = n.ask_id"
    ).fetchone()
    assert [p["kind"] for p in payload["parts"]] == ["radar"]  # the item rides the knock by its record
    via, live = deliver(db, nid, str(device), NOW.astimezone(UTC))
    assert via == "ws"  # this delivery made it
    row = item_row(db, nid)
    assert row["path"] == "attend.knock" and row["kind"] == "ask"  # decided by the gate, kept in its record
    # their "never" for Pisa's asks: the knock (it lapses at noon, before any digest) goes silently, never spoken
    assert (row["override"], row["reason"]) == ("never", "his_rule_never_silent")
    assert [(d, n.voice, n.speak) for d, n in live.sent] == [(str(device), False, None)]  # shown, no voice


# --- 2. "domani, provato": only for a real conflict -------------------------------------------------------


def _block(ref: str, title: str, start: str, end: str, place: str | None = None) -> R.Block:
    return R.Block("event", ref, title, local(f"{DAY}T{start}"), local(f"{DAY}T{end}"), place)


def test_2_provato_only_for_an_overlap_or_a_trip_that_doesn_t_fit_rules() -> None:
    a, b = _block("a", "Riunione", "09:00", "10:00", "p1"), _block("b", "Pilates", "10:15", "11:00", "p2")
    assert D.conflict([a, b], {}, "it-IT") is None  # a normal day: no line
    assert D.conflict([a, b], {("a", "b"): 10}, "it-IT") is None  # 10 minutes for 15: it fits
    late = D.conflict([a, b], {("a", "b"): 25}, "it-IT")
    assert (
        late is not None
        and late.kind == "transfer"
        and late.line == "Da riunione a pilates servono 25 minuti, ne hai 15."
    )
    clash = D.conflict([a, _block("c", "Pranzo", "09:30", "10:30")], {}, "it-IT")
    assert clash is not None and clash.kind == "clash" and [x.ref for x in clash.blocks] == ["a", "c"]
    assert (
        D.provato_line(clash.line, "it-IT")
        == "Ho provato la giornata: Pranzo alle 9:30 si sovrappone a riunione."
    )


def test_2_provato_appears_only_when_the_day_has_a_real_conflict(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    dossier = rt(client).dossier
    _event(db, "Riunione", local(f"{DAY}T09:00"), minutes=60)
    _event(db, "Pilates", local(f"{DAY}T10:00"), minutes=60)
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at, due_at, due_kind) "
        "VALUES (gen_random_uuid(), 'Pagare la palestra', 'x', 'open', 'manual', now(), now(), %s, 'date')",
        (local(f"{DAY}T23:59"),),
    )  # fmt: skip  (a deadline with no block: the Ghost Run's, never a conflict)
    assert client.portal.call(dossier.rehearse, TODAY) == 0
    assert db.execute("SELECT count(*) FROM day_rehearsals").fetchone()[0] == 0  # never a routine line
    _event(db, "Pranzo con Zeno", local(f"{DAY}T09:30"), minutes=60)
    assert client.portal.call(dossier.rehearse, TODAY) == 1
    said = _morning(client, db, clock, _phone(db))
    assert said.startswith("Ho provato la giornata: Pranzo con Zeno alle 9:30 si sovrappone a riunione.")
    refs = db.execute("SELECT refs FROM day_rehearsals").fetchone()[0]
    assert {r["table"] for r in refs} == {"calendar_events"} and len(refs) == 2


def test_2_provato_runs_in_the_night_after_the_scores(client) -> None:  # type: ignore[no-untyped-def]
    steps = night_rules.STEPS
    assert steps.index("p1_actuals") == steps.index("n10_sources") + 1
    assert steps.index("p2_provato") == steps.index("p1_actuals") + 1
    assert {"p1_actuals", "p2_provato"} <= set(rt(client).night.steps())


# --- 3. planned against actual ---------------------------------------------------------------------------


def _s(at: str, until: str | None, place: str | None) -> A.Stay:
    return A.Stay(local(f"{DAY}T{at}"), local(f"{DAY}T{until}") if until else None, place)


def test_3_planned_against_actual_rules() -> None:
    start, end = local(f"{DAY}T10:00"), local(f"{DAY}T10:30")
    stays = [_s("07:00", "09:40", "home"), _s("10:05", "10:50", "shop"), _s("11:20", "13:00", "office")]
    s = A.task_actual("t", start, end, "shop", None, stays)
    assert s is not None and (s.planned_min, s.actual_min, s.source) == (30, 45, "stay")
    assert A.task_actual("t", start, end, "elsewhere", None, stays) is None  # no stay there: not scored
    done = A.task_actual("u", start, end, None, local(f"{DAY}T10:40"), stays)
    assert done is not None and (done.actual_min, done.source) == (40, "done")
    assert A.task_actual("u", start, end, None, None, stays) is None  # not done: not scored
    assert (
        A.task_actual("u", start, end, None, local(f"{DAY}T18:00"), stays) is None
    )  # 16 times the plan: no match
    ev = A.event_actual("e", local(f"{DAY}T11:00"), local(f"{DAY}T12:00"), "office", stays)
    assert ev is not None and ev.actual_min == 100  # arrived 11:20, left 13:00
    trip = A.travel_actual("office", local(f"{DAY}T10:50"), 20, "office", stays)
    assert trip is not None and trip.actual_min == 30  # left the shop 10:50, arrived 11:20
    assert A.band("task", [1.0, 1.2, 1.5]) is None  # fewer than five: no band
    b = A.band("task", [1.0, 1.1, 1.2, 1.4, 1.5, 9.0])  # 9.0 is a matching error, never their spread
    assert b is not None and b.n == 5 and (b.lo, b.mid, b.hi) == (1.08, 1.2, 1.42)
    assert A.estimate(30, b) == 36 and A.range_of(36, b) == (32, 43)
    assert A.factor(A.Band("task", 9, 3.0, 3.0, 3.0)) == A.FACTOR_MAX and A.estimate(30, None) == 30


def _plan(db: Any, day: str, at: datetime, items: list[dict[str, Any]]) -> None:
    db.execute("INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, %s, %s, 'test', 'x', %s)",
               (uuid.uuid4(), day, at, Jsonb({"date": day, "items": items})))  # fmt: skip


def _stay(db: Any, device: uuid.UUID, place: uuid.UUID | None, at: datetime, until: datetime) -> None:
    db.execute("INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at, place_id) "
               "VALUES (%s, %s, %s, %s, 45.459, 9.093, 10, %s, %s)", (uuid.uuid4(), device, at, until, until, place))  # fmt: skip


def test_3_each_night_scores_planned_against_actual_and_keeps_his_band(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_knock import _place

    device = _phone(db)
    shop, office = _place(db, "Ferramenta", 45.459, 9.093), _place(db, "Ufficio", 45.469, 9.082)
    actuals = rt(client).night.steps()["p1_actuals"]
    for n in range(5):  # five days, each a 30-minute errand at the shop that took 45, a task done in 40
        day = TODAY - timedelta(days=5 - n)
        at = datetime.combine(day, datetime.min.time()).replace(tzinfo=local(DAY).tzinfo)
        errand, call = uuid.uuid4(), uuid.uuid4()
        db.execute("INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) VALUES "
                   "(%s, 'Chiamata', 'chiamata', 'done', 'manual', %s, %s)", (call, at, at))  # fmt: skip
        db.execute(
            "UPDATE tasks SET done_at = %s WHERE id = %s", (at + timedelta(hours=15, minutes=40), call)
        )
        _plan(db, day.isoformat(), at + timedelta(hours=7), [
            {"kind": "task", "task_id": str(errand), "place_id": str(shop), "start": (at + timedelta(hours=10)).isoformat(),
             "end": (at + timedelta(hours=10, minutes=30)).isoformat(), "status": "planned"},
            {"kind": "task", "task_id": str(call), "start": (at + timedelta(hours=15)).isoformat(),
             "end": (at + timedelta(hours=15, minutes=30)).isoformat(), "status": "planned"},
            {"kind": "travel", "to_place_id": str(office), "minutes": 20, "start": (at + timedelta(hours=10, minutes=50)).isoformat(),
             "end": (at + timedelta(hours=11, minutes=10)).isoformat(), "leave_at": (at + timedelta(hours=10, minutes=50)).isoformat()},
        ])  # fmt: skip
        _plan(db, day.isoformat(), at + timedelta(hours=12), [  # made after the trip began: not the one they followed
            {"kind": "travel", "to_place_id": str(office), "minutes": 5, "start": (at + timedelta(hours=10, minutes=50)).isoformat(),
             "end": (at + timedelta(hours=10, minutes=55)).isoformat(), "leave_at": (at + timedelta(hours=10, minutes=50)).isoformat()},
            {"kind": "task", "task_id": str(call), "start": (at + timedelta(hours=15)).isoformat(),
             "end": (at + timedelta(hours=15, minutes=30)).isoformat(), "status": "planned"},
        ])  # fmt: skip
        _stay(db, device, shop, at + timedelta(hours=10, minutes=5), at + timedelta(hours=10, minutes=50))
        _stay(db, device, office, at + timedelta(hours=11, minutes=20), at + timedelta(hours=13))
        assert client.portal.call(actuals, day + timedelta(days=1)) == 3  # the night of the next day
    rows = db.execute(
        "SELECT kind, planned_min, actual_min, source FROM plan_actuals ORDER BY day, kind, source"
    ).fetchall()
    assert (
        set(rows) == {("task", 30, 45, "stay"), ("task", 30, 40, "done"), ("travel", 20, 30, "stay")}
        and len(rows) == 15
    )
    bands = {
        k: (n, lo, mid, hi)
        for k, n, lo, mid, hi in db.execute("SELECT kind, n, lo, mid, hi FROM plan_bands").fetchall()
    }
    assert bands["travel"] == (5, 1.5, 1.5, 1.5) and bands["task"][0] == 10 and "event" not in bands
    assert client.portal.call(actuals, TODAY) == 0  # a night is scored once


@pytest.mark.usefixtures("owner_plans_every_task")
def test_3_the_planner_uses_his_band(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome
    from tests.test_planner import run

    task = add_task(db, "Riordinare lo studio", rome(2026, 10, 20).date())
    db.execute("UPDATE tasks SET duration_min = 40 WHERE id = %s", (uuid.UUID(task),))

    def planned() -> dict[str, Any]:
        plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
        return next(i for i in plan["items"] if i["kind"] == "task")

    item = planned()
    minutes = (
        datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])
    ).total_seconds() / 60
    assert minutes == 40 and "band" not in item  # no band yet: their own 40 minutes
    db.execute(
        "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('task', 8, 1.1, 1.5, 1.8, now())"
    )
    item = planned()
    minutes = (
        datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])
    ).total_seconds() / 60
    assert minutes == 60 and item["band"] == {"lo_min": 44, "hi_min": 72}  # their median, their spread


@pytest.mark.usefixtures("owner_plans_every_task")
def test_3_the_planner_times_trips_at_his_median(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome
    from tests.places_env import at_location
    from tests.test_planner import OFFICE, place, run, with_place

    at_location(db, OFFICE, rome(2026, 10, 20, 8, 59))
    shop = place(db, "Cartoleria", (45.468, 9.09))
    task = add_task(db, "Comprare le penne", rome(2026, 10, 20).date())
    with_place(db, "tasks", task, shop)

    def trip() -> dict[str, Any]:
        plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan(), keys=())
        return next(i for i in plan["items"] if i["kind"] == "travel")

    before = trip()
    db.execute(
        "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('travel', 6, 1.2, 1.5, 2.0, now())"
    )
    after = trip()
    assert after["minutes"] == A.estimate(before["minutes"], A.Band("travel", 6, 1.2, 1.5, 2.0))
    spread = A.range_of(after["minutes"], A.Band("travel", 6, 1.2, 1.5, 2.0))
    assert spread is not None and after["band"] == {"lo_min": spread[0], "hi_min": spread[1]}
    assert "band" not in before


# --- 4. the prompt stays byte-identical ---------------------------------------------------


def test_4_the_first_request_is_byte_identical_with_the_dossier_s_tables_empty_or_full(
    client, db, fake_models, secrets_key, clock
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import socket_turns
    from tests.test_measure import _same
    from tests.test_talk_timeline import Heard

    class Tools(Heard):
        def __init__(self) -> None:
            super().__init__()
            self.tools: list[list[str]] = []

        async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
            self.tools.append(sorted(t.name for t in info.function_tools if not t.defer_loading))
            async for part in super().__call__(messages, info):
                yield part

    seen: list[tuple[str, str, int, str]] = []
    clock.now = local(f"{DAY}T08:40").astimezone(UTC)
    for filled in (False, True):
        if filled:
            _world(db)
            client.portal.call(rt(client).dossier.today)  # the dossier built and kept
            db.execute("INSERT INTO day_rehearsals (day, kind, line, refs, rehearsed_at) VALUES (%s, 'clash', "
                       "'Pranzo alle 9:30 si sovrappone a riunione.', '[]', now())", (TODAY,))  # fmt: skip
            db.execute("INSERT INTO plan_actuals (id, day, kind, ref, planned_min, actual_min, source, scored_at) "
                       "VALUES (gen_random_uuid(), %s, 'task', 'x', 30, 45, 'done', now())", (TODAY,))  # fmt: skip
            db.execute(
                "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('task', 8, 1.1, 1.5, 1.8, now())"
            )
            db.execute("UPDATE dossier_items SET thumb = 'up', thumbed_at = now()")
            assert db.execute("SELECT count(*) FROM dossier_items").fetchone()[0] >= 4
        model = Tools()
        fake_models.default = model
        socket_turns(client, db, secrets_key, model, "Pisa, che tempo fa domani?")  # type: ignore[arg-type]
        history = json.dumps(
            [[str(getattr(p, "content", "")) for p in m.parts] for m in fake_models.seen_messages[-1]]
        )
        seen.append(
            (
                _same(model.instructions[0]),
                _same(history),
                len(model.instructions[0]),
                json.dumps(model.tools[0]),
            )
        )
        db.execute("TRUNCATE messages, conversations, pisa_decisions, notifications, devices CASCADE")
    assert seen[0] == seen[1]  # byte-identical: the dossier adds 0 chars and 0 tools to the first request


def test_4_no_brain_module_reads_the_dossier() -> None:
    from pathlib import Path

    brain = Path(__file__).resolve().parents[1] / "pisa" / "brain"
    for file in brain.rglob("*.py"):
        text = file.read_text()
        for name in (
            "dossier_items",
            "day_rehearsals",
            "plan_actuals",
            "plan_bands",
            "pisa.services.dossier",
        ):
            assert name not in text, f"{file.name} reads {name}"


def test_4_the_parts_a_knock_carries_include_the_dossier_s_kinds() -> None:
    assert {"provato", "radar", "loop", "draft"} <= set(knock_rules.PartKind.__args__)  # type: ignore[attr-defined]
    assert set(D.KINDS) == set(knock_rules.PartKind.__args__)  # type: ignore[attr-defined]


def test_2_a_stranger_s_title_in_a_conflict_is_never_spoken(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "Riunione", local(f"{DAY}T09:00"), minutes=60)
    _event(db, "Revisione trimestrale", local(f"{DAY}T09:30"), attendees=4)  # an invite: a stranger's title
    assert client.portal.call(rt(client).dossier.rehearse, TODAY) == 1
    assert db.execute("SELECT outside FROM day_rehearsals").fetchone()[0] is True
    _event(db, "Dentista", local(f"{DAY}T11:00"))
    said = _morning(client, db, clock, _phone(db))
    assert "Revisione" not in said and not said.startswith("Ho provato")
    assert db.execute("SELECT spoken FROM dossier_items WHERE kind = 'provato'").fetchone()[0] is False


def test_1_a_part_is_kept_as_said_only_when_the_dossier_spoke_it() -> None:
    """Il prurito's answer is marked told, il quarto d'ora's offer kept, only when it was among the three."""
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from pisa.services.voice1 import KnockService

    calls: list[tuple[str, bool]] = []

    def producer(kind: str) -> Any:
        async def part_of(conn: Any, now: Any, lang: str, *, speaking: bool = False) -> Any:
            calls.append((kind, speaking))
            return knock_rules.Part(kind, f"Una cosa {kind}.", "pisa_unknowns", kind)  # type: ignore[arg-type]

        return part_of

    class Conn:
        @asynccontextmanager
        async def transaction(self) -> AsyncIterator[None]:
            yield None

    service = KnockService(None, owner=owner_at())  # type: ignore[arg-type]
    service.later, service.quarto = [producer("prurito")], producer("quarto")
    asyncio.run(service._keep_said(Conn(), NOW, "it-IT", {"prurito"}))  # type: ignore[arg-type]
    assert ("prurito", True) in calls and ("quarto", True) not in calls


# --- the review's findings ---------------------------------------------------------------------------------


def _errand_day(
    db: Any,
    device: uuid.UUID,
    shop: Any,
    office: Any,
    day: date,
    factor: float | None,
    scaled: bool | None = None,
) -> None:
    """A day: a 30-minute errand at the shop that took 45, a 20-minute trip to the office that took 30. With
    `factor`, the plan was made at their band: the items are already scaled, and the plan says so."""
    at = datetime.combine(day, datetime.min.time()).replace(tzinfo=local(DAY).tzinfo)
    f = factor or 1.0
    errand = uuid.uuid4()
    plan: dict[str, Any] = {"date": day.isoformat(), "items": [
        {"kind": "task", "task_id": str(errand), "place_id": str(shop), "scaled": factor is not None if scaled is None else scaled, "start": (at + timedelta(hours=10)).isoformat(),
         "end": (at + timedelta(hours=10, minutes=30 * f)).isoformat(), "status": "planned"},
        {"kind": "travel", "to_place_id": str(office), "minutes": round(20 * f), "start": (at + timedelta(hours=10, minutes=50)).isoformat(),
         "end": (at + timedelta(hours=10, minutes=50 + 20 * f)).isoformat(), "leave_at": (at + timedelta(hours=10, minutes=50)).isoformat()},
    ]}  # fmt: skip
    if factor is not None:
        plan["factors"] = {"task": factor, "travel": factor}
    db.execute("INSERT INTO plans (id, day, generated_at, reason, fingerprint, plan) VALUES (%s, %s, %s, 'test', 'x', %s)",
               (uuid.uuid4(), day, at + timedelta(hours=7), Jsonb(plan)))  # fmt: skip
    _stay(db, device, shop, at + timedelta(hours=10, minutes=5), at + timedelta(hours=10, minutes=50))
    _stay(db, device, office, at + timedelta(hours=11, minutes=20), at + timedelta(hours=13))


def test_the_band_never_feeds_back_into_itself(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_knock import _place

    device = _phone(db)
    shop, office = _place(db, "Ferramenta", 45.459, 9.093), _place(db, "Ufficio", 45.469, 9.082)
    actuals = rt(client).night.steps()["p1_actuals"]
    first = TODAY - timedelta(days=10)
    for n in range(5):  # night 1..5: plans made without a band; their true ratio is 1.5
        _errand_day(db, device, shop, office, first + timedelta(days=n), None)
        client.portal.call(actuals, first + timedelta(days=n + 1))
    assert db.execute("SELECT mid FROM plan_bands WHERE kind = 'task'").fetchone()[0] == pytest.approx(1.5)
    for n in range(
        5, 10
    ):  # the next nights: plans made at their band (x1.5); they still take 1.5 times their own number
        _errand_day(db, device, shop, office, first + timedelta(days=n), 1.5)
        client.portal.call(actuals, first + timedelta(days=n + 1))
    scaled = db.execute("SELECT kind, planned_min, actual_min FROM plan_actuals WHERE day >= %s ORDER BY kind",
                        (first + timedelta(days=5),)).fetchall()  # fmt: skip
    assert set(scaled) == {("task", 30, 45), ("travel", 20, 30)}  # scored against the unscaled plan
    bands = dict(db.execute("SELECT kind, mid FROM plan_bands").fetchall())
    assert bands["task"] == pytest.approx(1.5) and bands["travel"] == pytest.approx(1.5)  # their real median


def test_r2b_h1_a_task_at_his_own_time_is_scored_as_planned(client, db) -> None:  # type: ignore[no-untyped-def]
    """A task he timed himself ("At the time you said") never took the band, so the night
    never divides it: done on time is a ratio of 1.0, not the factor."""
    from tests.test_knock import _place

    device = _phone(db)
    shop, office = _place(db, "Ferramenta", 45.459, 9.093), _place(db, "Ufficio", 45.469, 9.082)
    night = TODAY - timedelta(days=3)
    _errand_day(db, device, shop, office, night, 1.5, scaled=False)  # planned 45 at their time, took 45
    client.portal.call(rt(client).night.steps()["p1_actuals"], night + timedelta(days=1))
    rows = db.execute("SELECT planned_min, actual_min FROM plan_actuals WHERE kind = 'task'").fetchall()
    assert rows == [(45, 45)]


def test_the_plan_says_the_factors_it_was_made_with(db, secrets_key, owner_plans_every_task) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome
    from tests.test_planner import run

    add_task(db, "Riordinare lo studio", rome(2026, 10, 20).date())
    db.execute(
        "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('task', 8, 1.1, 1.5, 1.8, now())"
    )
    plan = run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
    assert plan["factors"] == {"task": 1.5, "travel": 1.0}
    assert [it.get("scaled") for it in plan["items"] if it["kind"] == "task"] == [
        True
    ]  # the night divides it
    stored = db.execute("SELECT plan->'factors' FROM plans").fetchone()[0]
    assert stored == {"task": 1.5, "travel": 1.0}  # what the night reads


def test_the_band_reaches_the_phone(client, db, clock, http_handler, owner_plans_every_task) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome
    from tests.conftest import pair
    from tests.places_fake import FakePlaces

    http_handler["handler"] = FakePlaces()
    clock.now = rome(2026, 10, 20, 9, 0)
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    add_task(db, "Chiamare Zeno", rome(2026, 10, 20).date())
    db.execute(
        "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('task', 8, 1.1, 1.5, 1.8, now())"
    )
    got = client.get("/v1/plan", headers=auth)
    assert got.status_code == 200, got.text
    task = next(i for i in got.json()["items"] if i["kind"] == "task")
    assert task["band"] == {"lo_min": 33, "hi_min": 54}  # 30 min planned at 45: their 1.1 to 1.8 times
    schema = json.loads((Path(__file__).resolve().parents[2] / "protocol" / "openapi.json").read_text())
    assert "band" in schema["components"]["schemas"]["PlanItem"]["properties"]
    assert set(schema["components"]["schemas"]["PlanBand"]["properties"]) == {"lo_min", "hi_min"}


def test_provato_is_rechecked_when_the_morning_reads_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    dossier = rt(client).dossier
    _event(db, "Riunione", local(f"{DAY}T09:00"), minutes=60)
    lunch = _event(db, "Pranzo con Zeno", local(f"{DAY}T09:30"), minutes=60)
    assert client.portal.call(dossier.rehearse, TODAY) == 1
    db.execute(
        "UPDATE calendar_events SET status = 'cancelled' WHERE id = %s", (lunch,)
    )  # cancelled by morning
    _event(db, "Dentista", local(f"{DAY}T11:00"))
    said = _morning(client, db, clock, _phone(db))
    assert said == "Oggi alle 9:00 hai Riunione. È tutto."  # the cancelled lunch's conflict is never said
    assert db.execute("SELECT count(*) FROM day_rehearsals").fetchone()[0] == 0  # the stale row is gone


def test_a_rehearsal_with_no_conflict_clears_the_day_s_row(client, db) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    db.execute("INSERT INTO day_rehearsals (day, kind, line, refs, rehearsed_at) VALUES (%s, 'clash', "
               "'Pranzo alle 9:30 si sovrappone a riunione.', '[]', now())", (TODAY,))  # fmt: skip
    _event(db, "Riunione", local(f"{DAY}T09:00"), minutes=60)
    assert client.portal.call(rt(client).dossier.rehearse, TODAY) == 0
    assert db.execute("SELECT count(*) FROM day_rehearsals").fetchone()[0] == 0


def test_still_rules() -> None:
    a, b = _block("a", "Riunione", "09:00", "10:00"), _block("b", "Pilates", "10:15", "11:00")
    assert not D.still("clash", ["a", "b"], None, [a, b])  # no longer overlapping
    assert D.still("transfer", ["a", "b"], 25, [a, b]) and not D.still("transfer", ["a", "b"], 10, [a, b])
    assert not D.still("transfer", ["a", "b"], 25, [a])  # one is gone
    assert D.still("clash", ["a", "c"], None, [a, _block("c", "Pranzo", "09:30", "10:30")])


def test_his_deletion_takes_the_band_and_a_thin_kind_has_none(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import erase as erase_rules
    from pisa.services import erase

    assert "plan_bands" in {p.table for p in erase_rules.FAMILIES["records"]}
    db.execute("INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('task', 8, 1.1, 1.5, 1.8, %s)",
               (datetime(2026, 1, 10, tzinfo=UTC),))  # fmt: skip
    db.execute(
        "INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('travel', 8, 1.1, 1.5, 1.8, now())"
    )
    client.portal.call(rt(client).night.steps()["p1_actuals"], TODAY)  # no samples in 28 days: no band
    assert db.execute("SELECT count(*) FROM plan_bands").fetchone()[0] == 0

    db.execute("INSERT INTO plan_bands (kind, n, lo, mid, hi, computed_at) VALUES ('task', 8, 1.1, 1.5, 1.8, %s)",
               (datetime(2026, 1, 10, tzinfo=UTC),))  # fmt: skip

    async def run() -> dict[str, int]:
        async with rt(client).pool.connection() as conn:
            tables, _ = await erase.delete(conn, "records", date(2026, 2, 1), "Europe/Rome")
            return tables

    assert client.portal.call(run)["plan_bands"] == 1


def test_one_morning_a_loop_the_dossier_said_is_never_said_again(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    waiting = _intention(db, "il preventivo", kind="waiting_for", who="Zeno")  # their own words, due today
    loops = rt(client).loops
    morning = local(f"{DAY}T08:15").astimezone(UTC)
    assert client.portal.call(loops.sense, morning) == 0  # the morning's knock will carry it: it waits
    assert db.execute("SELECT nudged_at FROM intentions WHERE id = %s", (waiting,)).fetchone()[0] is None
    said = _morning(client, db, clock, _phone(db))
    assert said == "Aspetti ancora il preventivo da Zeno. È tutto."
    assert db.execute("SELECT nudged_at FROM intentions WHERE id = %s", (waiting,)).fetchone()[0] is not None
    assert client.portal.call(loops.sense, local(f"{DAY}T13:00").astimezone(UTC)) == 0  # never twice
    assert db.execute("SELECT count(*) FROM attend_candidates WHERE source = 'intention'").fetchone()[0] == 0


def test_r2b_m3_a_loop_due_at_midnight_waits_for_the_morning_too(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """The hold runs from midnight, not the knock window's start: a loop due today is not
    nudged at 00:30 and then spoken again by the morning's dossier."""
    _settings(db)
    waiting = _intention(db, "il preventivo", kind="waiting_for", who="Zeno")
    loops = rt(client).loops
    assert client.portal.call(loops.sense, local(f"{DAY}T00:30").astimezone(UTC)) == 0
    assert db.execute("SELECT nudged_at FROM intentions WHERE id = %s", (waiting,)).fetchone()[0] is None
    assert db.execute("SELECT count(*) FROM attend_candidates WHERE source = 'intention'").fetchone()[0] == 0
    assert _morning(client, db, clock, _phone(db)) == "Aspetti ancora il preventivo da Zeno. È tutto."


def test_after_the_morning_the_loop_s_own_nudge_goes(client, db) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _intention(db, "il preventivo", kind="waiting_for", who="Zeno")
    assert client.portal.call(rt(client).loops.sense, local(f"{DAY}T13:00").astimezone(UTC)) == 1


def test_the_lock_comes_before_what_was_said() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "pisa" / "services" / "dossier.py").read_text()
    body = source[source.index("    async def build(") :]
    assert body.index("pg_advisory_xact_lock") < body.index("WHERE day = %s AND spoken")


def test_a_dead_draft_waits_for_nothing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    dead = _draft(db, local(f"{DAY}T03:00"))
    approval = uuid.uuid4()
    db.execute("INSERT INTO approvals (id, kind, summary, payload, status, created_at, expires_at) VALUES "
               "(%s, 'email_send', 'Invio', '{}', 'rejected', now(), now())", (approval,))  # fmt: skip
    db.execute("UPDATE email_drafts SET approval_id = %s WHERE id = %s", (approval, dead))
    _event(db, "Dentista", local(f"{DAY}T11:00"))
    said = _morning(client, db, clock, _phone(db))
    assert (
        "bozza" not in said
        and db.execute("SELECT count(*) FROM dossier_items WHERE kind = 'draft'").fetchone()[0] == 0
    )


def test_the_fall_back_hour_compares_in_utc() -> None:
    from zoneinfo import ZoneInfo

    rome = ZoneInfo("Europe/Rome")
    a = R.Block(
        "event",
        "a",
        "Volo",
        datetime(2026, 10, 25, 2, 10, tzinfo=rome),
        datetime(2026, 10, 25, 2, 40, tzinfo=rome),
    )
    b = R.Block(
        "event",
        "b",
        "Treno",
        datetime(2026, 10, 25, 2, 20, fold=1, tzinfo=rome),
        datetime(2026, 10, 25, 2, 50, fold=1, tzinfo=rome),
    )  # fmt: skip  (an hour later, in CET)
    assert D.conflict([a, b], {}, "it-IT") is None  # by the wall clock they overlap; in time they don't


def test_a_focus_block_is_never_a_conflict(client, db) -> None:  # type: ignore[no-untyped-def]
    _settings(db)
    _event(db, "Riunione", local(f"{DAY}T09:00"), minutes=60)
    for kind in ("focusTime", "workingLocation", "outOfOffice"):
        ref = _event(db, f"Blocco {kind}", local(f"{DAY}T09:30"), minutes=60)
        db.execute("UPDATE calendar_events SET event_type = %s WHERE id = %s", (kind, ref))
    assert client.portal.call(rt(client).dossier.rehearse, TODAY) == 0


def test_two_trips_to_one_place_are_two_samples(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_knock import _place

    device = _phone(db)
    home, gym = _place(db, "Casa", 45.397, 9.005), _place(db, "Palestra", 45.469, 9.082)
    day = TODAY - timedelta(days=1)
    at = datetime.combine(day, datetime.min.time()).replace(tzinfo=local(DAY).tzinfo)
    items = []
    for hour, target in ((8, "e1"), (18, "e2")):
        leave = at + timedelta(hours=hour)
        items += [
            {"kind": "travel", "to_place_id": str(gym), "minutes": 20, "start": leave.isoformat(),
             "end": (leave + timedelta(minutes=20)).isoformat(), "leave_at": leave.isoformat()},
            {"kind": "event", "event_id": target, "start": (leave + timedelta(minutes=20)).isoformat(),
             "end": (leave + timedelta(minutes=80)).isoformat(), "place_id": str(gym)},
        ]  # fmt: skip
        _stay(db, device, home, leave - timedelta(hours=1), leave)
        _stay(db, device, gym, leave + timedelta(minutes=30), leave + timedelta(minutes=90))
    _plan(db, day.isoformat(), at + timedelta(hours=6), items)
    client.portal.call(rt(client).night.steps()["p1_actuals"], TODAY)
    trips = db.execute(
        "SELECT ref, actual_min FROM plan_actuals WHERE kind = 'travel' ORDER BY ref"
    ).fetchall()
    assert [m for _, m in trips] == [30, 30] and len({r for r, _ in trips}) == 2
