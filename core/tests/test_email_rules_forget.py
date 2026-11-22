"""Fixes at the merge: each fails without its fix."""

import asyncio
from datetime import timedelta

from pisa.db.pool import open_pool
from pisa.services.profile import ProfileService
from tests.conftest import dsn
from tests.owner import owner_at
from tests.test_email_precision import (
    NOW,
    IntakeModel,
    add_email,
    open_titles,
    service_call,
    task,
)

# --- H2: an email rule can be forgotten ---------------------------------------------------------------------


def test_an_email_rule_can_be_forgotten_and_mail_comes_back(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    model = IntakeModel()
    add_email(db, "a", "Rinnovo VPS", "billing@ovh.example", NOW - timedelta(days=1))
    model.triage["Rinnovo VPS"] = "bill_or_deadline"
    model.extract["Rinnovo VPS"] = {"tasks": [task("Rinnovare il VPS")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    (tid,) = db.execute("SELECT id::text FROM tasks").fetchone() or ("",)
    service_call(db, secrets_key, model, lambda i, t: i.dismiss_task(t, tid, "sender", "it-IT"))
    (claim,) = db.execute("SELECT id::text FROM profile_claims").fetchone() or ("",)

    async def forget() -> None:
        pool = await open_pool(dsn(db), max_size=2)
        try:
            await ProfileService(pool, owner=owner_at()).forget(claim)
        finally:
            await pool.close()

    asyncio.run(forget())
    assert db.execute("SELECT count(*) FROM profile_claims").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM profile_off_limits").fetchone() == (0,)
    add_email(db, "b", "Il tuo VPS scade", "billing@ovh.example")
    model.triage["Il tuo VPS scade"] = "bill_or_deadline"
    model.extract["Il tuo VPS scade"] = {"tasks": [task("Rinnovare il VPS adesso")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    assert open_titles(db) == ["Rinnovare il VPS adesso"]


# --- M1: "never this kind" never hides a person's email -----------------------------------------------------


def test_never_bills_doesnt_hide_a_persons_bill_or_sweep_other_tasks(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    model = IntakeModel()
    add_email(db, "acc", "F24 da pagare", "studio.rossi@example.com", NOW - timedelta(days=2))
    model.triage["F24 da pagare"] = "bill_or_deadline"  # a person wrote it: not automated
    model.extract["F24 da pagare"] = {"tasks": [task("Pagare l'F24")]}
    add_email(db, "enel", "Paga la fattura", "fatture@enel.it", NOW - timedelta(days=1))
    model.triage["Paga la fattura"] = {"category": "bill_or_deadline", "automated": True,
                                       "asks_owner_to_act": True}  # fmt: skip
    model.extract["Paga la fattura"] = {"tasks": [task("Pagare la fattura Enel")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    (enel,) = db.execute("SELECT id::text FROM tasks WHERE title LIKE '%Enel'").fetchone() or ("",)
    out = service_call(db, secrets_key, model, lambda i, t: i.dismiss_task(t, enel, "kind", "it-IT"))
    assert out["also_dismissed"] == [] and open_titles(db) == ["Pagare l'F24"]
    add_email(db, "acc2", "Altro F24", "studio.rossi@example.com")
    model.triage["Altro F24"] = "bill_or_deadline"
    model.extract["Altro F24"] = {"tasks": [task("Pagare il secondo F24")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    assert "Pagare il secondo F24" in open_titles(db)


# --- M2: two people on Gmail are not one sender -------------------------------------------------------------


def test_another_gmail_users_task_is_no_duplicate_candidate(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    model = IntakeModel()
    add_email(db, "m", "La fattura", "marco@gmail.com", NOW - timedelta(days=1))
    model.triage["La fattura"] = "action_required"
    model.extract["La fattura"] = {"tasks": [task("Mandare la fattura a Marco")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    add_email(db, "a", "Mi mandi la fattura?", "anna@gmail.com")
    model.triage["Mi mandi la fattura?"] = "action_required"
    model.extract["Mi mandi la fattura?"] = {"tasks": [task("Mandare la fattura ad Anna")]}
    service_call(db, secrets_key, model, lambda i, t: i.run())
    sent = next(s for s in model.seen if s["subject"] == "Mi mandi la fattura?")
    assert "Mandare la fattura a Marco" not in sent["prompt"]
