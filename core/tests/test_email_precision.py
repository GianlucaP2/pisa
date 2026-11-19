"""Built from a list of open tasks: a security notice (change the password if it wasn't you), a
storage-full notice (twice), a hosting renewal (three near-duplicates), a dependency alert, and a check of a
domain's contact data. Precision first: account notices
are never tasks on Today (they are low tasks); the user teaches Pisa "never from this sender /
this kind" (kept as a `source` claim, applied to the open tasks too); the same request again is no new task
(the triage model compares the candidate tasks, as data); the quiet "From your email" list says why; a sweep
(dry run, then apply) for the tasks already there."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from pisa.brain.intake import TRIAGE_PROMPT
from pisa.services.intake import IntakeService
from pisa.services.tasks import TasksService
from tests.conftest import FAKE_KEY, Clock, dsn, pair
from tests.gateway_env import gateway, run
from tests.intake_fake import IntakeModel, task
from tests.owner import owner_at

NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
NOTICES = {
    "Avviso di sicurezza critico": ("no-reply@accounts.cloudbox.example", "action_required", "security"),
    "Il tuo spazio di archiviazione è quasi pieno": (
        "storage@cloudbox.example",
        "bill_or_deadline",
        "storage",
    ),
    "Rinnovo del tuo VPS": ("billing@hostingco.example", "bill_or_deadline", "auto_renewal"),
    "[GitHub] Dependabot alert": ("noreply@github.example", "action_required", "developer"),
    "Verifica i dati di contatto di example.org": (
        "noreply@hostingco.example",
        "action_required",
        "contact_check",
    ),
}
PERSON = "Ci vediamo giovedì?"


def add_email(db: psycopg.Connection, mid: str, subject: str, sender: str, sent: datetime = NOW) -> None:
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, subject, sent_at, labels, "
        "snippet, body) VALUES (%s, %s, %s, %s, %s, %s, '{INBOX}', %s, %s)",
        (mid, f"t-{mid}", sender.split("@")[0], sender, subject, sent - timedelta(hours=1), subject, subject),
    )


def service_call(db: psycopg.Connection, secrets_key: str, model: IntakeModel, action: Any) -> Any:
    async def go() -> Any:
        async with gateway(dsn(db), secrets_key, model, Clock(NOW)) as (gw, pool, keys):
            async with pool.connection() as conn:
                if not await keys.statuses(conn):
                    await keys.set(conn, "openai", FAKE_KEY)
            intake = IntakeService(pool, gw, owner=owner_at(), clock=lambda: NOW)
            tasks = TasksService(pool, owner=owner_at(), clock=lambda: NOW)
            return await action(intake, tasks)

    return run(go)


def notice_model() -> IntakeModel:
    model = IntakeModel()
    for subject, (_, category, notice) in NOTICES.items():
        model.triage[subject] = {"category": category, "automated": True, "asks_owner_to_act": True,
                                 "notice": notice}  # fmt: skip
        model.extract[subject] = {"tasks": [task(f"Fai qualcosa: {subject}")]}
    model.triage[PERSON] = "action_required"
    model.extract[PERSON] = {"tasks": [task("Rispondere a Marco per giovedì")]}
    return model


def open_titles(db: psycopg.Connection) -> list[str]:
    return [t for (t,) in db.execute("SELECT title FROM tasks WHERE status = 'open' ORDER BY title")]


def test_account_notices_are_no_task_and_stay_in_the_low_inbox(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """A CI run, a sign-in alert, a code is never a task, so
    the task list never fills with them; each stays in the low inbox as the email it is. The person's email
    is the only task."""
    for i, (subject, (sender, _, _)) in enumerate(NOTICES.items()):
        add_email(db, f"n{i}", subject, sender, NOW - timedelta(minutes=i))
    add_email(db, "p", PERSON, "marco@example.com")
    model = notice_model()
    service_call(db, secrets_key, model, lambda i, t: i.run())
    assert open_titles(db) == ["Rispondere a Marco per giovedì"]
    assert set(model.calls("extract")) == {PERSON}  # no extraction for a notice
    inbox = service_call(db, secrets_key, model, lambda i, t: i.inbox("it-IT"))
    assert len(inbox) == len(NOTICES)  # nothing is lost: each notice is in the low inbox


def test_the_prompt_says_precision_first() -> None:
    prompt = " ".join(TRIAGE_PROMPT.split())
    assert "Precision first" in prompt and "is never a task, whatever it asks" in prompt
    assert "When in doubt, it is not a task." in prompt


def test_never_from_this_sender_dismisses_the_others_and_holds_for_new_mail(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    model = IntakeModel()
    for n, subject in enumerate(["Rinnovo dominio example.org", "Rinnovo VPS", "Auto backup in scadenza"]):
        add_email(db, f"hosting{n}", subject, "billing@hostingco.example", NOW - timedelta(days=n + 1))
        model.triage[subject] = "bill_or_deadline"
        model.extract[subject] = {"tasks": [task(f"Rinnova {subject.lower()}")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    first = db.execute("SELECT id::text FROM tasks ORDER BY title LIMIT 1").fetchone()
    assert first is not None and len(open_titles(db)) == 3
    out = service_call(db, secrets_key, model, lambda i, t: i.dismiss_task(t, first[0], "sender", "it-IT"))
    assert out["rule"] == "Nessun task dalle email di «billing@hostingco.example»"
    assert len(out["also_dismissed"]) == 2 and open_titles(db) == []
    claim = db.execute("SELECT kind, provenance, hard, subject_key FROM profile_claims").fetchone()
    assert claim == (
        "source",
        "said",
        True,
        "email_sender:billing@hostingco.example",
    )  # the user's rule, in About you
    add_email(db, "hosting-new", "Il tuo ordine è stato rinnovato", "Billing@HostingCo.example")
    seen = len(model.seen)
    service_call(db, secrets_key, model, lambda i, t: i.run())
    assert len(model.seen) == seen  # never even sent to a model
    assert open_titles(db) == []
    row = db.execute("SELECT intake_category FROM email_messages WHERE id = 'hosting-new'").fetchone()
    assert row == ("rule_sender",)


def test_never_this_kind_holds_for_another_sender(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    model = IntakeModel()
    add_email(db, "a", "Paga la fattura di ottobre", "fatture@enel.it")
    model.triage["Paga la fattura di ottobre"] = "bill_or_deadline"
    model.extract["Paga la fattura di ottobre"] = {"tasks": [task("Pagare la fattura Enel")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    (tid,) = db.execute("SELECT id::text FROM tasks").fetchone() or ("",)
    out = service_call(db, secrets_key, model, lambda i, t: i.dismiss_task(t, tid, "kind", "it-IT"))
    assert out["rule"] == "Nessun task dalle email: pagamenti e scadenze"
    add_email(db, "b", "Bolletta del gas", "noreply@eni.example")
    model.triage["Bolletta del gas"] = {"category": "bill_or_deadline", "automated": True,
                                        "asks_owner_to_act": True}  # fmt: skip
    service_call(db, secrets_key, model, lambda i, t: i.run())
    assert open_titles(db) == [] and "Bolletta del gas" not in model.calls("extract")


def test_the_same_request_again_is_no_new_task(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Three hosting renewal emails with different subjects: the triage model sees the open tasks from the
    same domain (as data) and says which one this is again."""
    model = IntakeModel()
    add_email(db, "r1", "Rinnovo del tuo dominio", "billing@hostingco.example", NOW - timedelta(days=2))
    model.triage["Rinnovo del tuo dominio"] = "bill_or_deadline"
    model.extract["Rinnovo del tuo dominio"] = {"tasks": [task("Rinnovare il dominio example.org")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    (existing,) = db.execute("SELECT id::text FROM tasks").fetchone() or ("",)
    add_email(db, "r2", "Promemoria: scadenza example.org", "support@hostingco.example")
    model.triage["Promemoria: scadenza example.org"] = {
        "category": "bill_or_deadline",
        "same_as": existing,
    }
    add_email(db, "r3", "Ultimo avviso", "support@hostingco.example", NOW + timedelta(minutes=5))
    model.triage["Ultimo avviso"] = {
        "category": "bill_or_deadline",
        "same_as": str(uuid.uuid4()),
    }  # not given
    model.extract["Ultimo avviso"] = {"tasks": [task("Rinnovare subito il dominio")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    sent = next(s for s in model.seen if s["subject"] == "Promemoria: scadenza example.org")
    assert existing in sent["prompt"] and "Rinnovare il dominio example.org" in sent["prompt"]
    assert "open-tasks" in sent["prompt"]  # as a data block, never instructions
    assert open_titles(db) == ["Rinnovare il dominio example.org", "Rinnovare subito il dominio"]
    row = db.execute("SELECT intake_category FROM email_messages WHERE id = 'r2'").fetchone()
    assert row == ("duplicate",)  # an id the model wasn't given is ignored: r3 is a task


def test_the_sweep_dry_run_then_apply(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The tasks already there, under today's rules: the dry run stores the list; --apply dismisses it."""
    model = notice_model()
    ids = {}
    for i, (subject, (sender, _, _)) in enumerate(NOTICES.items()):
        add_email(db, f"n{i}", subject, sender, NOW - timedelta(days=3))
        ids[subject] = str(uuid.uuid4())
        db.execute(
            "INSERT INTO tasks (id, title, status, due_kind, priority, source_kind, source_ref, title_key, "
            "created_at, updated_at) VALUES (%s, %s, 'open', 'none', 2, 'email', %s, %s, %s, %s)",
            (
                ids[subject],
                f"Task {i}",
                f"n{i}",
                f"task {i}",
                NOW - timedelta(days=3),
                NOW - timedelta(days=3),
            ),
        )
    add_email(db, "p", PERSON, "marco@example.com", NOW - timedelta(days=3))
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, priority, source_kind, source_ref, title_key, "
        "created_at, updated_at) VALUES (%s, 'Rispondere a Marco', 'open', 'none', 2, 'email', 'p', "
        "'rispondere a marco', %s, %s)",
        (uuid.uuid4(), NOW - timedelta(days=3), NOW - timedelta(days=3)),
    )
    since, until = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 25, tzinfo=UTC)
    dry = service_call(db, secrets_key, model, lambda i, t: i.retriage(since, apply=False, until=until))
    assert {r.task_id for r in dry if r.reason} == set(ids.values())
    assert len(open_titles(db)) == 6  # the dry run changes nothing
    with pytest.raises(Exception, match="No dry run"):  # other dates: not what was reviewed
        service_call(db, secrets_key, model, lambda i, t: i.retriage(since, apply=True))
    done = service_call(db, secrets_key, model, lambda i, t: i.retriage(since, apply=True, until=until))
    assert {r.task_id for r in done} == set(ids.values()) and open_titles(db) == ["Rispondere a Marco"]


def test_dismiss_and_inbox_over_rest(client, db) -> None:  # type: ignore[no-untyped-def]
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    add_email(db, "m1", "Rinnovo VPS", "billing@hostingco.example")
    tid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO tasks (id, title, status, due_kind, priority, source_kind, source_ref, title_key, "
        "created_at, updated_at) VALUES (%s, 'Rinnova il VPS', 'open', 'none', 2, 'email', 'm1', "
        "'rinnova il vps', now(), now())",
        (tid,),
    )
    db.execute("UPDATE email_messages SET intake_status = 'done', intake_category = 'bill_or_deadline'")
    got = client.post(f"/v1/tasks/{tid}/dismiss", headers=auth, json={"never": "sender"})
    assert got.status_code == 200, got.json()
    body = got.json()
    assert body["task"]["status"] == "dismissed" and body["rule"] and body["also_dismissed"] == []
    manual = client.post("/v1/tasks", headers=auth, json={"title": "Comprare il pane"}).json()
    refused = client.post(f"/v1/tasks/{manual['id']}/dismiss", headers=auth, json={"never": "kind"})
    assert refused.status_code == 400
    assert client.post(f"/v1/tasks/{manual['id']}/dismiss", headers=auth, json={}).json()["rule"] is None
    add_email(db, "m2", "Avviso di sicurezza", "no-reply@accounts.cloudbox.example")
    db.execute(
        "UPDATE email_messages SET intake_status = 'done', intake_category = 'notification', "
        "intake_notice = 'security' WHERE id = 'm2'"
    )
    inbox = client.get("/v1/inbox", headers=auth).json()["emails"]
    assert [e["subject"] for e in inbox] == ["Avviso di sicurezza"]
