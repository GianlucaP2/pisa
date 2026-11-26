"""The review rows, smoke and the evals, lighter turns, the voice's
first audio, the changes feed. Each row with its test."""

import uuid
from datetime import timedelta
from typing import Any

from pisa.infra import hard_limit
from tests.actions_fake import OWNER
from tests.conftest import pair
from tests.test_acting import PAOLA, Model, patches, row, said
from tests.test_acting import google as google  # the fixture
from tests.test_acting_fixes import MOVE, _talk
from tests.test_approvals import dinner
from tests.test_talking_like_a_person import _deliver


def aging_asked(db: Any, task: str, at: Any, *, delivered: bool = True, dropped: bool = True) -> str:
    """Pisa asked "è ancora da fare?" about `task` at `at`: the aging check-in and its notice (reached the
    user at once, or never: attend dropped it)."""
    from psycopg.types.json import Jsonb

    from pisa.domain import follow as rules

    check_in, notice = uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO notifications (id, kind, text, lang, priority, created_at, queued_at, delivered_via, "
        "delivered_at) VALUES (%s, 'check_in', 'è ancora da fare?', 'it-IT', 'normal', %s, %s, %s, %s)",
        (notice, at, at, "ws" if delivered else "none", at),
    )
    db.execute(
        "INSERT INTO check_ins (id, day, kind, task_id, title, text, choices, created_at, purpose, "
        "notification_id, dropped_at) VALUES (%s, %s, 'confirm', %s, 'x', 'è ancora da fare?', %s, %s, "
        "'aging', %s, %s)",
        (check_in, at.date(), task, Jsonb(rules.choices("confirm", "it-IT")), at, notice,
         at + timedelta(hours=1) if dropped else None),
    )  # fmt: skip
    db.execute("UPDATE tasks SET aging_asked_at = %s WHERE id = %s", (at, task))
    return str(check_in)


# --- MED (smoke): `check-in-yes`: a "sì" answers the check-in Pisa asked last, by code
# ------------------------


def _check_in(db: Any, client: Any, text: str, kind: str = "confirm") -> str:
    from psycopg.types.json import Jsonb

    from pisa.domain import follow as rules

    day = client.portal.call(client.app.state.runtime.planner.today)
    check_in, task = uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO tasks (id, title, title_key, source_kind, created_at, updated_at) "
        "VALUES (%s, 'Passare in farmacia', 'passare farmacia', 'voice', now(), now())",
        (task,),
    )
    db.execute(
        "INSERT INTO check_ins (id, day, kind, task_id, title, text, choices, created_at) "
        "VALUES (%s, %s, %s, %s, 'Passare in farmacia', %s, %s, now())",
        (check_in, day, kind, task, text, Jsonb(rules.choices(kind, "it-IT"))),  # type: ignore[arg-type]
    )
    return str(check_in)


def _say_check_in(client: Any, app: Any, device_id: Any, check_in: str, text: str) -> None:
    """The check-in's own notice, delivered to this device (as follow's check-ins are)."""
    from pisa.services.notifications import enqueue

    runtime = client.app.state.runtime

    async def go() -> str:
        async with runtime.pool.connection() as conn:
            nid, _ = await enqueue(
                conn, kind="check_in", text=text, lang="it-IT", now=runtime.clock(), timezone="Europe/Rome",
                device_id=device_id, check_in_id=uuid.UUID(check_in),
            )  # fmt: skip
        return str(nid)

    _deliver(client, client.portal.call(go))
    assert app.next()["type"] == "notify"


def test_a_yes_after_a_check_in_answers_the_check_in_by_code_never_the_older_question(  # type: ignore[no-untyped-def]
    client, db, google, fake_models, secrets_key
) -> None:
    from tests.conftest import set_key
    from tests.live_app import live

    dinner(google, db, attendees=[{"email": OWNER, "self": True}, PAOLA])
    model = Model("sposta la cena alle 21", [MOVE])
    fake_models.default = model
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        _talk(client, db, secrets_key, app, "sposta la cena alle 21")  # "Sposto «Cena» … e avviso Paola?"
        check_in = _check_in(db, client, "Sei passato in farmacia?")
        _say_check_in(client, app, device_id, check_in, "Sei passato in farmacia?")
        calls = model.seen
        yes = _talk(client, db, secrets_key, app, "sì")
    assert said(yes) == "Bene, segnato." and model.seen == calls  # no model call: code heard the yes
    answered = db.execute("SELECT answer FROM check_ins WHERE id = %s", (check_in,)).fetchone()
    assert answered == ("done",)
    assert row(db)[1] == "pending" and not patches(google)  # the dinner stays; Paola hears nothing


def test_a_no_keeps_it_and_a_missed_check_in_is_left_to_the_model(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key
) -> None:
    from tests.conftest import set_key
    from tests.live_app import live

    model = Model("x", [])
    fake_models.default = model
    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        confirm = _check_in(db, client, "Sei passato in farmacia?")
        _say_check_in(client, app, device_id, confirm, "Sei passato in farmacia?")
        no = _talk(client, db, secrets_key, app, "no")
        assert said(no) == "Ok, allora resta da fare." and model.seen == 0
        assert db.execute("SELECT answer FROM check_ins WHERE id = %s", (confirm,)).fetchone() == (
            "not_done",
        )
        missed = _check_in(
            db, client, "Sembra che tu non sia andato a Pranzo. Lo sposto o lo salto?", "missed"
        )
        _talk(client, db, secrets_key, app, "sì")  # five answers, not a yes/no: the model asks which
    assert model.seen == 1
    assert db.execute("SELECT answer FROM check_ins WHERE id = %s", (missed,)).fetchone() == (None,)


def test_the_conversation_eval_gives_the_model_pisa_s_latest_question_as_production_does() -> None:
    from pisa_tools.talk_eval import CASES, latest_question

    case = next(c for c in CASES if c.name == "check-in-yes")
    assert latest_question(case) == "Sei passato in farmacia?"
    assert latest_question(next(c for c in CASES if c.name == "weather")) is None


# --- LOW: the thread follow-up after the sender rules, its candidate outside; the venue stop-list ----------


def test_the_venue_link_needs_the_venue_s_own_name_never_a_city() -> None:
    from pisa.domain import things as T

    assert not T.same_venue(
        T.venue_words("Consegna a Milano, Via Immaginaria 3"), "Concerto", "Teatro Ombrellai, Milano"
    )
    assert T.same_venue(T.venue_words("Teatro Ombrellai, Milano"), "Concerto", "Teatro degli Ombrellai")
    assert T.link_words("Stazione Centrale, Amsterdam") == set()


def test_the_evals_month_never_eats_the_owner_s(db) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    from datetime import UTC, datetime
    from decimal import Decimal

    import psycopg

    from pisa.domain.errors import PisaError
    from pisa.infra import ledger
    from tests.conftest import dsn

    now = datetime(2026, 10, 29, 10, tzinfo=UTC)
    db.execute(
        "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, trace_id, "
        "created_at, budget) VALUES (gen_random_uuid(), 'chat.conversation', 'openai:x', 'settled', 0, 9.9, "
        "true, 't', %s, 'eval')",
        (now - timedelta(days=5),),
    )  # €9.90 of evals this month, none today

    async def go() -> tuple[Decimal, str]:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            cap = await ledger.get_eval_month_cap(conn, now, "Europe/Rome", hard_limit.DEFAULT)
            with ledger.eval_budget():
                try:
                    await ledger.reserve(
                        conn, slot="chat.conversation", model_ref="openai:x", estimate_eur=Decimal("0.2"),
                        price_known=True, trace_id="e", now=now, tz="Europe/Rome", hard=hard_limit.DEFAULT,
                    )  # fmt: skip
                    refused = ""
                except PisaError as err:
                    refused = err.message
        return cap, refused

    cap, refused = asyncio.run(go())
    assert cap == Decimal(10)  # the hard €30 month less the user's €20: never 31 days x €0.50 = €15.50
    assert refused.startswith("The evals' monthly cap is reached: €9.90")


def test_the_aging_week_counts_from_when_the_question_reached_the_owner(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import add_task, rome
    from tests.test_follow import run

    now = rome(2026, 10, 29, 12)
    unseen = add_task(db, "Rinnovare il dominio", rome(2026, 10, 10, 9))
    late = add_task(db, "Pagare il bollo", rome(2026, 10, 10, 9))
    aging_asked(db, unseen, now - timedelta(days=8), delivered=False)  # attend dropped it: never heard
    aging_asked(db, late, now - timedelta(days=8))
    db.execute(
        "UPDATE notifications SET delivered_at = %s FROM check_ins c "
        "WHERE c.notification_id = notifications.id AND c.task_id = %s",
        (now - timedelta(days=2), late),
    )  # queued 8 days ago, heard 2 days ago (held through a meeting)

    async def tick(follow: Any, env: Any) -> int:
        return await follow.aging()

    run(db, secrets_key, now, tick)
    assert db.execute("SELECT count(*) FROM tasks WHERE status = 'open'").fetchone() == (2,)
    asked = db.execute("SELECT aging_asked_at IS NULL FROM tasks WHERE id = %s", (unseen,)).fetchone()
    assert asked == (False,)  # asked again: the reset question went out once more this minute
    assert db.execute("SELECT count(*) FROM check_ins WHERE task_id = %s", (unseen,)).fetchone() == (2,)


def test_the_attend_gate_is_judged_on_the_mean_of_two_runs() -> None:
    from pisa_tools.attend_eval import RUNS, combined

    runs = [{"agreement": 0.717, "missed_floors": 0}, {"agreement": 0.913, "missed_floors": 0}]
    got = combined(runs)  # two measured runs
    assert (
        RUNS == 2
        and got["agreement_mean"] == 0.815
        and got["gate"] == {"missed_floors": True, "agreement": True}
    )
    one_miss = combined([{"agreement": 0.9, "missed_floors": 0}, {"agreement": 0.9, "missed_floors": 1}])
    assert one_miss["gate"]["missed_floors"] is False  # a floor missed in any run fails the gate


# --- 2. Lighter turns (batch E): about 20 tools at once, the rest found by search_tools --------------------


def test_a_turn_carries_the_common_tools_and_finds_the_rest() -> None:
    from pisa.brain import tool_search
    from pisa_tools import agency_eval as ev

    offered = ev.tool_definitions()
    names = {t.name for t in offered}
    # stop_everything is always offered, but needs Pisa's store (the eval has none, as for the views)
    always = set(tool_search.ALWAYS) - {"stop_everything"}
    assert names == always | {"search_tools"} and len(names) <= 31  # + finish
    deferred = ev.deferred_definitions(offered)
    assert {"set_task_priority", "map_search", "draft_reply", "remember_about_me"} <= set(deferred)
    found = tool_search.search(None, ["set_task_priority"], list(deferred.values()))
    assert found[0] == "set_task_priority"  # a name the instructions give is always found first
    assert "map_search" in tool_search.search(None, ["map search places"], list(deferred.values()))
    assert tool_search.search(None, ["xyzzy"], list(deferred.values())) == []


def test_common_turns_send_half_the_tokens_and_the_cached_prefix_never_changes() -> None:
    import asyncio

    from tests.turn_size import numbers

    got = asyncio.run(numbers())
    before, after = got["before"], got["after"]
    assert before["prefix_same_every_turn"] and after["prefix_same_every_turn"]
    common = [w for w, r in after["turns"].items() if r["requests"] == 1]
    assert len(common) == 4
    for words in common:  # no search round: about half the input, the first sentence ~0.9 s sooner
        assert after["turns"][words]["input_tokens"] < 0.6 * before["turns"][words]["input_tokens"]
        assert after["turns"][words]["first_sentence_s"] < before["turns"][words]["first_sentence_s"] - 0.8
    found = [r for r in after["turns"].values() if r["requests"] == 2]  # a deferred tool: one more round
    assert len(found) == 2 and all(r["prefix_tokens"] == after["turns"]["che ore sono?"]["prefix_tokens"]
                                   for r in found)  # fmt: skip


def test_the_agency_eval_finds_deferred_tools_and_labelled_stays_perfect() -> None:
    import asyncio

    from tests.agency_eval import labelled, numbers

    got = asyncio.run(numbers(labelled))
    assert got["right"] == 1.0 and got["breaches_attempted"] == 0 and got["searches_mean"] > 0
    # + finish, + answer_proposal (deferred); + plan_task, set_task_deadline, draft_email,
    # show_sheet (deferred); + set_task_start (deferred; the views tools need a store, the eval has
    # none); + set_task_country (deferred); + read_my_email, app_help (deferred)
    # The three source tools; six; show_email (deferred);
    # walk_times, show_findings, my_findings (deferred); show_drawing,
    # my_drawing (deferred; save_drawing needs the views store); shop_prices, show_products,
    # events_on, show_events, table_slots (deferred)
    assert got["tools"] == 31 and got["deferred"] == 73


# --- 3. The voice's first audio: each step logged, the slow ones moved before the first sentence ----------


def _first_audio(db: Any, secrets_key: str, *, warm: bool) -> tuple[Any, int, list[str]]:
    """The first-audio log line, how often the provider was warmed before the first sentence, and the order
    in which the voice's config was read and the first sentence fed."""
    import asyncio

    from structlog.testing import capture_logs

    from tests.test_voice import Speaker, run_voice

    order: list[str] = []

    async def go(voice, pool, provider):  # type: ignore[no-untyped-def]
        async def send(*args: Any) -> bool:
            return True

        read_config, open_provider = voice.config, provider.warm
        warmed_up = asyncio.Event()

        async def config():  # type: ignore[no-untyped-def]
            order.append("config")
            return await read_config()

        async def warmed() -> None:
            await open_provider()
            warmed_up.set()

        voice.config, provider.warm = config, warmed
        speaker = Speaker(voice, send, "t", ("mp3",))
        if warm:
            speaker.warm()  # the turn starts: the model thinks meanwhile
            # the model "thinks" until the warm-up is done: an event, not a clock (the bound only stops a
            # broken warm-up from hanging the suite; a passing run never comes near it)
            await asyncio.wait_for(warmed_up.wait(), 60)
        warmed_first = provider.warmed  # warmed before the first sentence
        order.append("fed")
        speaker.feed("Ciao. ")
        speaker.finish()
        await speaker.done()
        return warmed_first

    with capture_logs() as logs:
        warmed = run_voice(db, secrets_key, go)
    (line,) = [e for e in logs if e["event"] == "voice_first_audio"]
    return line, warmed, order


def test_each_reply_logs_its_first_audio_steps_in_times_only(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    line, warmed, _ = _first_audio(db, secrets_key, warm=False)
    assert set(line) >= {"config", "reserved", "requested", "received", "sent", "trace_id", "warmed"}
    assert line["config"] <= line["reserved"] <= line["requested"] <= line["received"] <= line["sent"]
    assert line["warmed"] is False and warmed == 0
    assert all(isinstance(line[k], int) for k in ("config", "reserved", "requested", "received", "sent"))
    known = {
        "config",
        "reserved",
        "requested",
        "received",
        "sent",
        "trace_id",
        "warmed",
        "event",
        "log_level",
    }
    assert set(line) <= known  # ids and times only


def test_warmed_at_the_turn_s_start_the_first_sentence_goes_straight_to_the_provider(  # type: ignore[no-untyped-def]
    db, secrets_key
) -> None:
    cold, cold_warmed, cold_order = _first_audio(db, secrets_key, warm=False)
    warm, warmed, warm_order = _first_audio(db, secrets_key, warm=True)
    # no timing at all (CI under load); the conditions instead: the provider's connection opened before the
    # first sentence, and its config was read by then, once
    assert warmed == 1 and warm["warmed"] is True and cold_warmed == 0 and cold["warmed"] is False
    assert warm_order == ["config", "fed"]  # the voice, its prices and its key were read before the sentence
    assert cold_order == ["fed", "config"]  # cold: read only once the first sentence came


# --- 4. GET /v1/changes: Pisa's own changes with their reason and their Undo
# ----------------------------------


def test_changes_lists_what_pisa_did_with_why_and_the_undo_while_it_lasts(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from psycopg.types.json import Jsonb

    from tests.conftest import pair

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    now = clock.now
    aged, closed, restored = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for task, title, status in ((aged, "Rinnovare il dominio", "dismissed"),
                                (closed, "Ritirare il pacco", "done"),
                                (restored, "Pagare il bollo", "open")):  # fmt: skip
        db.execute(
            "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, 'email', %s, %s)",
            (task, title, title.lower(), status, now, now),
        )
    why = "Scaduto e senza risposta da 7 giorni: lo tolgo. Puoi rimetterlo."
    for task, at in ((aged, now - timedelta(hours=1)), (restored, now - timedelta(hours=2))):
        db.execute(
            "INSERT INTO pisa_decisions (at, move, tool, ref, purpose, provenance) "
            "VALUES (%s, 'act', 'aging', %s, %s, 'pisa_initiative')",
            (at, str(task), why),
        )
    db.execute(
        "INSERT INTO done_by_presence (id, task_id, day, learn, title, source, done_at, undo_until) "
        "VALUES (gen_random_uuid(), %s, %s, false, 'Ritirare il pacco', 'email', %s, %s)",
        (closed, now.date(), now - timedelta(minutes=30), now + timedelta(hours=23)),
    )
    bulk = uuid.uuid4()
    db.execute(
        "INSERT INTO task_bulk (id, action, items, origin, created_at) "
        "VALUES (%s, 'dismiss', %s, 'voice', %s)",
        (bulk, Jsonb([{"id": str(aged), "was": "open"}] * 3), now - timedelta(days=2)),
    )  # two days ago: its Undo (24 h) is over
    got = client.get("/v1/changes", headers=auth)
    assert got.status_code == 200
    rows = got.json()["changes"]
    assert [r["kind"] for r in rows] == ["task_done", "task_dismissed", "task_dismissed", "tasks_bulk"]
    done, dismissed, back, many = rows
    assert (
        done["why"] == "Chiuso: una risposta nella conversazione dice che è fatto." and done["by"] == "pisa"
    )
    assert done["undo"]["method"] == "POST" and done["undo"]["path"] == f"/v1/tasks/{closed}/undo-done"
    assert dismissed["why"] == why and dismissed["title"] == "Rinnovare il dominio"
    assert dismissed["undo"] == {"method": "PATCH", "path": f"/v1/tasks/{aged}", "body": {"status": "open"},
                                 "until": None}  # fmt: skip
    assert back["task_id"] == str(restored) and back["undo"] is None  # restored already: nothing to undo
    assert many["why"] == "3 attività tolte insieme." and many["undo"] is None and many["title"] is None
    since = client.get(
        "/v1/changes", headers=auth, params={"since": (now - timedelta(minutes=45)).isoformat()}
    )
    assert [r["kind"] for r in since.json()["changes"]] == ["task_done"]


def test_the_plan_agenda_waits_for_its_switch(client, db, secrets_key, fake_models, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain import present_tools
    from tests.test_interface_tools import _calls, _phone

    assert present_tools.SEND_PLAN_AGENDA is False  # Off until IOS 20b decodes it
    monkeypatch.setattr(present_tools, "SEND_PLAN_AGENDA", True)  # the rule at the merge
    fake_models.default = _calls([("show_plan", {"day": "2026-11-03", "purpose": "il martedì"})], "Eccolo.")
    _, (frames,) = _phone(client, db, secrets_key, ["cosa ho martedì?"])
    (shown,) = [f for f in frames if f["type"] == "present"]
    assert shown["object"]["ref"] == {"date": "2026-11-03", "options_id": None, "agenda": True}


# --- 6. The dry check of thread follow-ups: counts only, nothing changed, on the evals' budget -------------
