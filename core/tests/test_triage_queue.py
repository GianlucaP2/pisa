"""The queued items: the triage rule for deliveries and bookings (QUEUE, from a retriage)
and the first talk's narrower triggers, its actions only inside an active talk, the waiting write per device,
and the server stating the exact question."""

import uuid

import psycopg

from tests.intake_fake import IntakeModel, task
from tests.test_intake import add_email, intake, statuses, tasks
from tests.test_profile import claims, said

# The four the triage model called "automated, nothing asked" in a retriage: each keeps its
# task.
KEPT = {
    "PuntoPoste: il tuo pacco ti aspetta": ("delivery", "Ritirare il pacco al PuntoPoste"),
    "GLS: consegna prevista domani": ("delivery", "Ricevere la consegna GLS"),
    "Hostel Fietsbel Amsterdam: check-in online": ("event_or_booking", "Fare il check-in al Fietsbel"),
    "Richiesta di prenotazione ricevuta": ("event_or_booking", "Rispondere alla richiesta di prenotazione"),
}
DONE = "GLS: il tuo pacco è stato consegnato"


def test_deliveries_and_bookings_keep_their_task_when_automated(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake = IntakeModel()
    for i, (subject, (category, title)) in enumerate(KEPT.items()):
        add_email(db, f"m-{i}", subject, "…")
        fake.triage[subject] = {"category": category, "automated": True, "asks_owner_to_act": False}
        fake.extract[subject] = {"tasks": [task(title)]}
    # Only confirming something already done: no task.
    add_email(db, "m-done", DONE, "Consegnato alle 10:12.")
    fake.triage[DONE] = {
        "category": "delivery",
        "automated": True,
        "asks_owner_to_act": False,
        "already_done": True,
    }
    report = intake(db, secrets_key, fake)
    assert report.tasks_created == 4
    assert sorted(row[0] for row in tasks(db)) == sorted(title for _, title in KEPT.values())
    assert DONE not in fake.calls("extract") and statuses(db)[DONE][0] == "done"
    instructions = next(s["instructions"] for s in fake.seen if s["kind"] == "triage")
    assert "whether everything it is about is already done" in instructions


# --- the first talk -----------------------------------------------------------------------------------------


def talk_state(db: psycopg.Connection) -> str | None:
    row = db.execute("SELECT value->>'state' FROM app_settings WHERE key = 'profile_first_talk'").fetchone()
    return row[0] if row else None


def test_the_first_talk_starts_only_on_a_request_to_get_to_know_each_other(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    for words in ("Voglio conoscere la zona", "I want to get to know the area", "let's meet Marco at eight"):
        t = said(db, secrets_key, words, [("first_talk", {"action": "start"})])
        assert t.words("first_talk").startswith("Not started"), words
    assert talk_state(db) is None
    t = said(db, secrets_key, "Pisa, let's get to know each other", [("first_talk", {"action": "start"})])
    assert "Section 1 of 8" in t.words("first_talk")


def test_a_paused_talk_resumes_only_when_the_owner_means_it(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = said(db, secrets_key, "conosciamoci", [("first_talk", {"action": "start"})])
    said(db, secrets_key, "dopo", [("first_talk", {"action": "later"})], device=t.device)
    for words in ("continue looking for a bar", "continuiamo a cercare"):
        r = said(db, secrets_key, words, [("first_talk", {"action": "resume"})], device=t.device)
        assert r.words("first_talk").startswith("Not started"), words
    r = said(db, secrets_key, "ok, continuiamo", [("first_talk", {"action": "resume"})], device=t.device)
    assert "Section 1 of 8" in r.words("first_talk")


def test_next_skip_later_and_done_only_inside_an_active_talk(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    t = said(db, secrets_key, "che tempo fa?", [("first_talk", {"action": "next"})])
    assert t.words("first_talk").startswith("No first talk is going on")
    t = said(
        db,
        secrets_key,
        "conosciamoci",
        [("first_talk", {"action": "start"}), ("first_talk", {"action": "next"})],
    )
    assert t.words("first_talk", 1).startswith("Not now: the first talk started in this message")
    # Skip and later need the user's own words for them; done only at the readback.
    s = said(db, secrets_key, "il bistrot sotto casa", [("first_talk", {"action": "skip"})], device=t.device)
    assert s.words("first_talk").startswith("Not skipped: the user didn't say to skip")
    s = said(db, secrets_key, "il bistrot sotto casa", [("first_talk", {"action": "later"})], device=t.device)
    assert s.words("first_talk").startswith("Not paused: the user didn't ask to stop")
    s = said(db, secrets_key, "fatto", [("first_talk", {"action": "done"})], device=t.device)
    assert s.words("first_talk").startswith("Not done: the readback comes first")
    assert talk_state(db) == "in_progress"
    # Paused: nothing but resume.
    said(db, secrets_key, "basta per oggi", [("first_talk", {"action": "later"})], device=t.device)
    s = said(db, secrets_key, "salta", [("first_talk", {"action": "skip"})], device=t.device)
    assert s.words("first_talk").startswith("The first talk is paused")


def test_the_waiting_write_belongs_to_the_device_that_was_asked(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    allergy = (
        "remember_about_me",
        {
            "kind": "constraint",
            "statement": "È allergico alle noci",
            "quote": "allergico alle noci",
            "hard": True,
        },
    )
    phone = said(db, secrets_key, "Sono allergico alle noci", [allergy])
    other = said(db, secrets_key, "sì", [("confirm_remember", {})])  # another device's "sì"
    assert other.words("confirm_remember").startswith("Nothing done")
    yes = said(db, secrets_key, "sì", [("confirm_remember", {})], device=phone.device)
    assert yes.words("confirm_remember").startswith('Remembered: "È allergico alle noci"')
    assert [c[1] for c in claims(db)] == ["È allergico alle noci"]


def test_the_server_says_the_exact_question(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    allergy = (
        "remember_about_me",
        {
            "kind": "constraint",
            "statement": "È allergico alle noci",
            "quote": "allergico alle noci",
            "hard": True,
        },
    )
    # The model asks something vaguer: the server's own question, with the exact statement, ends the reply.
    t = said(db, secrets_key, "Sono allergico alle noci", [allergy], "Ok, ne terrò conto?")
    assert t.words("remember_about_me").endswith("Don't ask it yourself.")
    question = "Vuoi che ne tenga sempre conto: «È allergico alle noci»?"
    assert t.reply.endswith(question) and question in t.words("remember_about_me")
    # Said by the model already, word for word: not said twice.
    said(db, secrets_key, "no", [])
    t = said(db, secrets_key, "Sono allergica al lattosio", [(
        "remember_about_me",
        {"kind": "constraint", "statement": "È allergica al lattosio", "quote": "allergica al lattosio"},
    )], "Vuoi che ne tenga sempre conto: «È allergica al lattosio»?")  # fmt: skip
    assert t.reply.count("«È allergica al lattosio»") == 1
    assert uuid.UUID(str(t.device))
