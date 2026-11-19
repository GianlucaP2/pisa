"""Every reason Pisa shows them is in their Settings language:
checked on read as on write, the stored ones re-said once by a maintenance job, and retriage writes their
language."""

import asyncio
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from pisa.domain.reasons import (
    PURPOSE_SAID,
    TASK_SAID_BREACH,
    WHY_NO_TASK,
    purpose_said,
    retriage_kind,
    said_reason,
    why_no_task,
)
from pisa.domain.tongue import speaks
from pisa.services.approvals import _said_why
from pisa.services.tasks import resay_reasons
from tests.assistant_env import add_task, rome, set_settings
from tests.conftest import dsn, pair
from tests.intake_fake import IntakeModel
from tests.owner import owner_at
from tests.test_email_precision import NOW, service_call

# Synthetic: an English triage reason stored before the check on write existed (the case on the server).
BREACH_EN = "Someone is trying to get into the account now, and the email asks to deny the request."


def _email(db: psycopg.Connection, mid: str, category: str, notice: str | None) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, intake_status, intake_category, intake_notice) "
        "VALUES (%s, %s, 'Example', 'noreply@example.com', 'Synthetic subject', %s, '{INBOX}', 'x', 'done', "
        "%s, %s)",
        (mid, f"t-{mid}", NOW - timedelta(hours=1), category, notice),
    )


def _email_task(
    db: psycopg.Connection, title: str, mid: str, reason: str, *, priority: int = 3, **more: Any
) -> str:
    tid = add_task(db, title, priority=priority, **more)
    db.execute(
        "UPDATE tasks SET source_kind = 'email', source_ref = %s, priority_reason = %s WHERE id = %s",
        (mid, reason, tid),
    )
    return tid


# --- 1. checked on read ----------------------------------------------------------------------------------


def test_a_stored_english_breach_reason_reads_in_his_language(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, "b1", "info", "account_breach")
    tid = _email_task(db, "Negare la richiesta di accesso", "b1", BREACH_EN)
    it = TASK_SAID_BREACH[0]

    low = client.get("/v1/tasks", headers=auth, params={"priority": "low"}).json()
    assert [t["priority_reason"] for t in low["tasks"]] == [it]  # under it-IT: Pisa's line, never the English
    patched = client.patch(f"/v1/tasks/{tid}", headers=auth, json={"notes": "Nota"}).json()
    assert patched["priority_reason"] == it  # every task_json, not only the list

    set_settings(db, {"language": "en-US"})
    low = client.get("/v1/tasks", headers=auth, params={"priority": "low"}).json()
    # under en-US the English line is their language: as written, the same check as on write
    assert [t["priority_reason"] for t in low["tasks"]] == [BREACH_EN]
    # the stored line is untouched: only what leaves CORE is checked
    assert db.execute("SELECT priority_reason FROM tasks").fetchone()[0] == BREACH_EN


def test_a_stored_italian_breach_reason_reads_as_the_english_template_under_en_us(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    set_settings(db, {"language": "en-US"})
    _email(db, "b2", "info", "account_breach")
    _email_task(
        db, "Negare la richiesta di accesso", "b2", "Qualcuno sta provando ad accedere al tuo conto adesso"
    )
    low = client.get("/v1/tasks", headers=auth, params={"priority": "low"}).json()
    assert [t["priority_reason"] for t in low["tasks"]] == [TASK_SAID_BREACH[1]]


def test_a_reason_in_his_language_passes_as_written(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, "c1", "bill_or_deadline", None)
    _email_task(db, "Pagare la bolletta", "c1", "La bolletta scade entro venerdì")
    low = client.get("/v1/tasks", headers=auth, params={"priority": "low"}).json()
    assert [t["priority_reason"] for t in low["tasks"]] == ["La bolletta scade entro venerdì"]


def test_the_plans_todo_reads_in_his_language(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import run as plan_run

    _email(db, "p1", "info", "account_breach")
    tid = _email_task(db, "Negare la richiesta di accesso", "p1", BREACH_EN, priority=1)

    def why() -> Any:
        plan = plan_run(db, secrets_key, rome(2026, 10, 20, 9, 0), lambda p, env: p.plan())
        return next(t.get("why") for t in plan["todo"] if t["task_id"] == tid)

    assert why() == TASK_SAID_BREACH[0]
    set_settings(db, {"language": "en-US"})
    assert why() == BREACH_EN  # their language: as written


def test_a_category_line_and_an_old_retriage_diagnostic_on_read(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, "d1", "delivery", None)
    _email_task(db, "Ritirare il pacco", "d1", "The parcel arrives tomorrow at the locker.")
    _email(db, "d2", "action_required", None)
    _email_task(db, "Liberare spazio", "d2", "no task: storage (automated)")
    low = client.get("/v1/tasks", headers=auth, params={"priority": "low"}).json()
    reasons = {t["title"]: t["priority_reason"] for t in low["tasks"]}
    assert reasons == {
        "Ritirare il pacco": "Una consegna",
        "Liberare spazio": "Notifica automatica: niente da fare (avvisi sullo spazio)",
    }


def test_the_pure_rules() -> None:
    assert retriage_kind("no task: storage (automated)") == ("notification", "storage")
    assert retriage_kind("no task: info") == ("info", None)
    assert retriage_kind("same as «Pagare»") == ("duplicate", None)
    assert retriage_kind("another task of the same email thread stays") == ("same_thread", None)
    assert retriage_kind("Someone tried to sign in") is None
    assert said_reason(BREACH_EN, "info", "account_breach", "en-US", task=True) == BREACH_EN  # their already
    # a move's purpose (Changes, the ledger, an approval's "perché")
    assert purpose_said("To keep the afternoon free for the gym.", "plan", "it-IT") == PURPOSE_SAID["plan"][0]
    assert (
        purpose_said("Per tenere libero il pomeriggio", "plan", "it-IT") == "Per tenere libero il pomeriggio"
    )
    assert purpose_said("To keep the afternoon free for the gym.", "plan", "en-US") == (
        "To keep the afternoon free for the gym."
    )
    # the Italian markers cover a plain Italian purpose; an English line with none of their words still fails
    assert speaks("così arrivi in tempo", "it-IT") and speaks("Più tardi, quando esci", "it-IT")
    assert not speaks("Log in now", "it-IT")
    why = _said_why({"purpose": "So that the meeting does not clash.", "provenance": "owner_words"},
                    "calendar_move", "it-IT")  # fmt: skip
    assert why == {"purpose": PURPOSE_SAID["calendar"][0], "provenance": "owner_words"}


def test_the_changes_feed_says_a_purpose_in_his_language(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    tid = add_task(db, "Chiamare il fornitore", status="dismissed")
    db.execute(
        "INSERT INTO pisa_decisions (at, move, tool, ref, purpose, provenance) "
        "VALUES (%s, 'act', 'aging', %s, 'Overdue for a week and no answer to the question.', "
        "'pisa_initiative')",
        (rome(2026, 10, 20, 12), tid),  # the client's clock's day
    )
    found = client.get("/v1/changes", headers=auth).json()["changes"]
    assert [c["why"] for c in found if c.get("task_id") == tid] == [PURPOSE_SAID["task_dismissed"][0]]
    lines = [ln for d in client.get("/v1/ledger", headers=auth).json()["days"] for ln in d["lines"]]
    assert [ln["why"] for ln in lines] == [PURPOSE_SAID["task_dismissed"][0]]  # the week's ledger too


# --- 2. the stored ones, once -------------------------------------------------------------------------------


def _resay(db: psycopg.Connection, *, apply: bool) -> dict[str, int]:
    async def go() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await resay_reasons(conn, apply=apply, language="it-IT")

    return asyncio.run(go())


def test_the_jobs_dry_run_counts_and_changes_nothing_then_apply_rewrites(db) -> None:  # type: ignore[no-untyped-def]
    _email(db, "j1", "info", "account_breach")
    breach = _email_task(db, "Negare la richiesta di accesso", "j1", BREACH_EN)
    _email(db, "j2", "action_required", None)
    old = _email_task(db, "Liberare spazio", "j2", "no task: storage (automated)")
    _email(db, "j3", "bill_or_deadline", None)
    _email_task(db, "Pagare la bolletta", "j3", "La bolletta scade entro venerdì")  # already theirs
    closed = _email_task(db, "Una cosa chiusa", "j1", BREACH_EN, status="done")  # not open: left alone
    before = db.execute("SELECT id::text, priority_reason, updated_at FROM tasks ORDER BY id").fetchall()

    assert _resay(db, apply=False) == {"account_breach": 1, "retriage": 1}
    assert (
        db.execute("SELECT id::text, priority_reason, updated_at FROM tasks ORDER BY id").fetchall() == before
    )

    assert _resay(db, apply=True) == {"account_breach": 1, "retriage": 1}
    stored = dict(db.execute("SELECT id::text, priority_reason FROM tasks").fetchall())
    assert stored[breach] == TASK_SAID_BREACH[0]
    assert stored[old] == why_no_task("notification", "storage", "it-IT")
    assert stored[closed] == BREACH_EN
    assert _resay(db, apply=False) == {}  # once: nothing left


# --- 3. retriage writes their language
# ------------------------------------------------------------------------


def _retriage_to_low(db: psycopg.Connection, secrets_key: str) -> dict[str, tuple[str, int, str | None]]:
    since = NOW - timedelta(days=5)
    listed = []
    for mid, title, reason, category, notice in [
        ("s1", "Libera spazio su Google One", "no task: storage (automated)", "notification", "storage"),
        ("s2", "Rinnova il VPS", "your rule: never from this sender", "rule_sender", None),
        ("s3", "Leggi le novità", "no task: info", "info", None),
    ]:
        tid = add_task(db, title, created=NOW - timedelta(days=1))
        db.execute("UPDATE tasks SET source_kind = 'email', source_ref = %s WHERE id = %s", (mid, tid))
        listed.append({"task_id": tid, "reason": reason, "category": category, "notice": notice})
    dry_run = {"since": since.isoformat(), "until": NOW.isoformat(), "at": NOW.isoformat(), "dismiss": listed}
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('intake_retriage_dry_run', %s)", (Jsonb(dry_run),)
    )
    done = service_call(
        db, secrets_key, IntakeModel(), lambda i, t: i.retriage(since, apply=True, until=NOW, to_low=True)
    )
    assert {r.reason for r in done} >= {"no task: storage (automated)"}  # the English stays for the logs
    return {
        t: (st, p, r)
        for t, st, p, r in db.execute("SELECT title, status, priority, priority_reason FROM tasks")
    }


def test_retriage_writes_the_reason_in_his_language(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    rows = _retriage_to_low(db, secrets_key)
    assert rows["Libera spazio su Google One"] == (
        "open", 3, "Notifica automatica: niente da fare (avvisi sullo spazio)"
    )  # fmt: skip
    assert rows["Leggi le novità"] == ("open", 3, WHY_NO_TASK["info"][0])
    assert rows["Rinnova il VPS"][0] == "dismissed"  # the user's rule: no task at all


def test_retriage_writes_english_under_en_us(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "en-US"})
    rows = _retriage_to_low(db, secrets_key)
    assert rows["Libera spazio su Google One"][2] == "An automatic notice: nothing to do (storage notices)"


def test_js_own_read_of_the_low_list_is_in_his_language(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_research_eval import research

    _email(db, "v1", "info", "account_breach")
    _email_task(db, "Negare la richiesta di accesso", "v1", BREACH_EN)
    t = research(db, secrets_key, "cosa c'è in bassa priorità?", [("low_priority_list", {})], "Ok.",
                 with_data=True)  # fmt: skip
    said = t.result("low_priority_list")
    assert TASK_SAID_BREACH[0] in said and BREACH_EN not in said


# --- the review's fixes --------------------------------------------------------------------------


def _low_reasons(client, db) -> dict[str, str | None]:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    low = client.get("/v1/tasks", headers=auth, params={"priority": "low"}).json()
    return {t["title"]: t["priority_reason"] for t in low["tasks"]}


def test_an_old_diagnostic_with_an_italian_title_is_never_shown_raw(client, db) -> None:  # type: ignore[no-untyped-def]
    _email(db, "h1", "bill_or_deadline", None)
    _email_task(db, "Pagare il gas", "h1", "same as «Pagare la bolletta del gas»")
    _email(db, "h2", "info", None)
    _email_task(
        db, "Una novità", "h2", "same as you said, the bill is paid"
    )  # a model line, not a diagnostic
    assert speaks("same as «Pagare la bolletta del gas»", "it-IT")  # why the order matters
    reasons = _low_reasons(client, db)
    assert reasons["Pagare il gas"] == WHY_NO_TASK["duplicate"][0]
    assert reasons["Una novità"] == "Da fare"  # the model's English line: the task's line
    assert _resay(db, apply=False) == {"retriage": 1, "info": 1}


def test_an_english_diagnostic_under_en_us_reads_as_the_template(client, db) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "en-US"})
    _email(db, "h3", "action_required", None)
    _email_task(db, "Liberare spazio", "h3", "no task: storage (automated)")
    assert _low_reasons(client, db) == {
        "Liberare spazio": "An automatic notice: nothing to do (storage notices)"
    }
    assert _resay(db, apply=False) == {"retriage": 1}


def test_retriages_own_lines_read_as_written_and_the_job_is_once(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    _retriage_to_low(db, secrets_key)
    reasons = _low_reasons(client, db)
    assert reasons["Leggi le novità"] == WHY_NO_TASK["info"][0]  # "Solo informazioni": Pisa's own line
    assert reasons["Libera spazio su Google One"] == why_no_task("notification", "storage", "it-IT")
    assert _resay(db, apply=False) == {}


def test_an_approvals_purpose_reads_in_his_language(client, db) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    device = pair(db)
    now = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)  # the client's clock
    approval = db.execute(
        "INSERT INTO approvals (id, kind, summary, payload, status, device_id, created_at, expires_at) "
        "VALUES (gen_random_uuid(), 'email_send', 'Mando la mail a Marco?', '{}', 'pending', %s, %s, %s) "
        "RETURNING id::text",
        (device[0], now, now + timedelta(minutes=5)),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO pisa_decisions (at, move, tool, ref, purpose, provenance) "
        "VALUES (%s, 'propose', 'send_draft', %s, 'So that the reply goes out before the meeting.', "
        "'owner_words')",
        (now, approval),
    )
    found = client.get("/v1/approvals", headers={"Authorization": f"Bearer {device[1]}"}).json()["approvals"]
    assert [a["why"]["purpose"] for a in found] == [PURPOSE_SAID["email"][0]]


def test_the_agendas_due_tasks_read_in_his_language(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, "a1", "info", "account_breach")
    _email_task(db, "Negare la richiesta di accesso", "a1", BREACH_EN, priority=1, due=rome(2026, 10, 20, 18))
    agenda = client.get("/v1/agenda", headers=auth, params={"from": "2026-10-20", "days": 1}).json()
    assert [t["priority_reason"] for d in agenda["days"] for t in d["tasks_due"]] == [TASK_SAID_BREACH[0]]


def test_an_emails_gist_reads_in_his_language(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    _email(db, "g1", "delivery", None)
    _email(db, "g2", "delivery", None)
    db.execute("UPDATE email_messages SET intake_gist = %s WHERE id = 'g1'",
               (Jsonb({"what": "An order confirmation with the delivery date"}),))  # fmt: skip
    db.execute("UPDATE email_messages SET intake_gist = %s WHERE id = 'g2'",
               (Jsonb({"what": "La conferma dell'ordine con la data di consegna"}),))  # fmt: skip
    assert client.get("/v1/emails/g1", headers=auth).json()["gist"] == "Una consegna"
    assert client.get("/v1/emails/g2", headers=auth).json()["gist"] == (
        "La conferma dell'ordine con la data di consegna"
    )


def test_an_email_pisa_tells_him_about_now_is_in_his_language(db) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from pisa.brain.intake import Gist, Triage
    from pisa.services.intake import IntakeService
    from tests.test_proactivity import T0, TZ, _attend

    verdict = Triage(
        category="action_required", automated=False, asks_owner_to_act=True, already_done=False,
        reason="Marco asks", priority="high", tell_owner=True,
        gist=Gist(what="The cover of the record", who="Marco", asked="send the cover by tonight"),
    )  # fmt: skip

    async def go() -> None:
        async with _attend(db, T0) as env:
            intake = IntakeService(env.pool, None, owner=owner_at(TZ), clock=env.clock, attend=env.attend)  # type: ignore[arg-type]
            await intake._tell(SimpleNamespace(id="m1", sender=None), verdict, None, T0)  # type: ignore[arg-type]

    asyncio.run(go())
    assert db.execute("SELECT text FROM attend_candidates").fetchall() == [
        ("Email da Marco: Ti chiede di fare qualcosa.",)
    ]


def _intention(db: psycopg.Connection, kind: str, why: str) -> str:
    row = db.execute(
        "INSERT INTO intentions (id, kind, state, what, why, source, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, 'open', 'Rinnovare il passaporto', %s, 'voice', now(), now()) "
        "RETURNING id::text",
        (kind, why),
    ).fetchone()
    assert row is not None
    return row[0]


def test_an_intentions_why_reads_in_his_language(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain.reasons import INTENTION_WHY

    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    english = _intention(db, "mine", "Because the trip abroad needs a valid document.")
    italian = _intention(db, "waiting_for", "Perché il viaggio è a dicembre")
    listed = {i["id"]: i["why"] for i in client.get("/v1/intentions", headers=auth).json()["intentions"]}
    assert listed == {english: INTENTION_WHY["mine"][0], italian: "Perché il viaggio è a dicembre"}
    assert client.get(f"/v1/intentions/{english}", headers=auth).json()["why"] == INTENTION_WHY["mine"][0]


def test_the_pool_tasks_notes_carry_the_why_in_his_language() -> None:
    from pisa.domain.reasons import INTENTION_WHY
    from pisa.services.intentions import _notes

    notes = _notes("Because the trip abroad needs a valid document.", "Prendere l'appuntamento", "it-IT")
    assert notes == f"Perché: {INTENTION_WHY['mine'][0]} · Prossimo passo: Prendere l'appuntamento"


def test_a_places_cards_why_and_the_plans_own_lines() -> None:
    from datetime import UTC, datetime

    from pisa.domain.reasons import PLACE_WHY, place_why
    from pisa.planner.explain import TaskFacts, unplanned_reason, why

    assert place_why("Quiet and close to the station, with good reviews.", "it-IT") == PLACE_WHY[0]
    assert place_why("Tranquillo e vicino alla stazione", "it-IT") == "Tranquillo e vicino alla stazione"
    facts = TaskFacts("Spedire il pacco", 1, "today", None, "date", None)
    assert why(facts, "Europe/Rome", "it-IT") == "Scade oggi; priorità alta."
    assert why(facts, "Europe/Rome", "en-US") == "Due today; high priority."
    start = datetime(2026, 10, 20, 7, tzinfo=UTC)
    common: dict[str, Any] = {"duration": 60, "windows": [], "longest_free": 120, "deadline": None,
                              "plan_start": start, "timezone": "Europe/Rome"}  # fmt: skip
    assert unplanned_reason(**common, lang="it-IT") == "Chiuso per il resto della giornata."
    assert unplanned_reason(**common, lang="en-US") == "Closed for the rest of the day."


def test_a_places_card_keeps_his_short_line_and_drops_an_english_one() -> None:
    from pisa.brain.research_tools import ShownPlace, _show
    from pisa.brain.research_turn import ResearchTurn
    from pisa.domain.reasons import PLACE_WHY

    cards: list[dict[str, Any]] = []

    async def show(card: dict[str, Any], focus: str, purpose: str) -> str:
        cards.append(card)
        return "shown"

    research = ResearchTurn(lang="it-IT", prompt_chars=0, show=show)
    research.places = {"p1": {"name": "Bar Uno"}, "p2": {"name": "Bar Due"}}
    english = "Quiet and close to the station, with good reviews."
    chosen = [ShownPlace(place_id="p1", why="Terrazza tranquilla sul Naviglio"),
              ShownPlace(place_id="p2", why=english)]  # fmt: skip
    asyncio.run(_show(research, "Bar", "un bar", chosen))
    assert [p["why"] for p in cards[0]["places"]] == ["Terrazza tranquilla sul Naviglio", PLACE_WHY[0]]
