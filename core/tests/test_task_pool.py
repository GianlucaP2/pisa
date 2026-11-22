"""The task pool and what Pisa shows. The pool (Pisa
never times a task by itself; deadlines with their source; today's todo), tasks from email by meaning, Pisa's
own rules in the open, research, the sheet, a new email, the push."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pisa.domain.tasks import Task, deadline_source, pool_key, todo
from tests.assistant_env import add_task, rome, set_settings

NOW = rome(2026, 11, 2, 11, 0)


def _task(title: str, due: datetime | None = None, priority: int = 2, at: int = 0) -> Task:
    made = datetime(2026, 11, 1, 9, at, tzinfo=UTC)
    return Task(
        str(uuid.uuid4()), title, None, "open", due, "date" if due else "none", None, None, priority,
        "voice", None, None, None, made, made,
    )  # fmt: skip


# --- The pool ------------------------------------------------------------------------------------------


def test_the_pool_is_by_deadline_then_priority_and_today_s_todo_is_its_head() -> None:
    soap = _task("Comprare il bagnoschiuma", priority=2, at=1)
    ovh = _task("Rinnovare OVHcloud", NOW + timedelta(days=5), priority=2)
    museum = _task("Mostra di Kandinskij", NOW + timedelta(days=2), priority=2)
    urgent = _task("Mandare la mail a Rossi", priority=1, at=2)
    low = _task("Controllare l'account", NOW + timedelta(hours=3), priority=3)
    pool = sorted([soap, ovh, museum, urgent, low], key=pool_key)
    assert [t.title for t in pool] == [
        "Controllare l'account", "Mostra di Kandinskij", "Rinnovare OVHcloud", "Mandare la mail a Rossi",
        "Comprare il bagnoschiuma",
    ]  # fmt: skip
    head = todo([soap, ovh, museum, urgent, low], NOW, {museum.id})
    assert [t.title for t in head] == [
        "Rinnovare OVHcloud",
        "Mandare la mail a Rossi",
        "Comprare il bagnoschiuma",
    ]
    # never a low one, never one already timed in the plan


def test_pisa_never_times_a_task_by_itself_and_the_plan_lists_today_s_todo(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import run

    set_settings(db, {"language": "it-IT"})
    soap = add_task(db, "Comprare il bagnoschiuma")
    ovh = add_task(db, "Rinnovare OVHcloud", rome(2026, 11, 4).date())
    plan = run(db, secrets_key, NOW, lambda p, env: p.plan())
    assert not [i for i in plan["items"] if i.get("kind") == "task"]  # "Bagnoschiuma 13:30-14:04"
    assert [(t["task_id"], t.get("deadline_source")) for t in plan["todo"]] == [(ovh, "owner"), (soap, None)]


def test_a_task_the_owner_asks_pisa_to_plan_gets_a_time_said_by_the_owner(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import run

    soap = add_task(db, "Comprare il bagnoschiuma")
    day = NOW.date()
    db.execute(
        "INSERT INTO plan_adjustments (id, day, action, task_id, title, source, created_at, option_ref) "
        "VALUES (%s, %s, 'plan', %s, 'Comprare il bagnoschiuma', 'voice', now(), %s)",
        (uuid.uuid4(), day, soap, uuid.uuid4()),
    )
    plan = run(db, secrets_key, NOW, lambda p, env: p.plan())
    (item,) = [i for i in plan["items"] if i.get("task_id") == soap]
    assert item["start"] and item["scheduled_by"] == "owner"
    assert soap not in {t["task_id"] for t in plan["todo"]}


def test_get_tasks_returns_the_pool_with_each_deadline_s_source(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    add_task(db, "Comprare il bagnoschiuma")
    add_task(db, "Rinnovare OVHcloud", rome(2026, 11, 20).date())
    add_task(db, "Mostra di Kandinskij", rome(2026, 11, 9).date())
    add_task(db, "Mandare la mail a Rossi", priority=1)  # high, but no deadline: after the dated ones
    tasks = client.get("/v1/tasks", headers=headers).json()["tasks"]
    assert [t["title"] for t in tasks] == [
        "Mostra di Kandinskij",
        "Rinnovare OVHcloud",
        "Mandare la mail a Rossi",
        "Comprare il bagnoschiuma",
    ]
    assert [t["deadline_source"] for t in tasks] == ["owner", "owner", None, None]


def test_a_deadline_s_source_is_code_s() -> None:
    assert deadline_source([], "la mostra chiude il 9 novembre", "2026-11-09") == "owner"
    assert deadline_source(["page"], "trovami quando chiude la mostra", "2026-11-09") == "web"
    assert deadline_source(["email"], "e il rinnovo di OVH?", "2026-11-17") == "email"
    assert deadline_source([], "mettici una scadenza", "2026-11-17") == "pisa"


def test_the_brain_hears_today_s_pool_and_may_suggest_one_when_the_owner_is_free() -> None:
    from zoneinfo import ZoneInfo

    from pisa.services.assistant import _pool_line  # pyright: ignore[reportPrivateUsage]

    tz = ZoneInfo("Europe/Rome")
    plan: dict[str, Any] = {
        "items": [{"kind": "event", "title": "Cena", "start": (NOW + timedelta(hours=1)).isoformat(),
                   "end": (NOW + timedelta(hours=3)).isoformat()}],
        "todo": [{"task_id": "t", "title": "Comprare il bagnoschiuma", "priority": 2}],
    }  # fmt: skip
    line = _pool_line(plan, NOW, tz)
    assert "«Comprare il bagnoschiuma»" in line and "free until 12:00 (60 min)" in line
    assert "never put one in the plan" in line
    plan["items"][0]["start"] = (NOW + timedelta(minutes=10)).isoformat()
    assert "free" not in _pool_line(plan, NOW, tz)  # ten minutes: nothing to suggest


def test_plan_task_only_on_the_owner_s_own_request() -> None:
    from pisa.domain.tasks import asks_to_plan

    asked = ["mettilo oggi quando posso", "trovami un momento oggi per le camicie", "puoi pianificarlo?",
             "find me time for it"]  # fmt: skip
    not_asked = ["aggiungi ritirare le camicie", "hai un'ora libera?", "quando posso ritirare le camicie?",
                 "puoi dirmi quando posso farlo?", "add buy milk"]  # fmt: skip
    shirts = "Ritirare le camicie"  # Tied to this task
    assert all(asks_to_plan(w, shirts) for w in asked) and not any(asks_to_plan(w, shirts) for w in not_asked)


# --- tasks from email by meaning; Pisa's own rules -----------------------------------------------------


def _verdict(**fields: Any) -> Any:
    from pisa.brain.intake import Triage

    base = {"category": "info", "automated": True, "asks_owner_to_act": False, "already_done": False,
            "notice": "none", "priority": "low", "reason": "x"}  # fmt: skip
    return Triage.model_validate(base | fields)


def test_a_targeted_breach_alert_is_a_task_the_daily_review_and_receipts_are_not() -> None:
    from pisa.brain.intake import needs_task

    # "qualcuno è entrato nel tuo account": theirs to act on, whatever the category
    assert needs_task(_verdict(notice="account_breach", asks_owner_to_act=True))
    # Google's daily "check your security settings": a notice, never a task
    assert not needs_task(_verdict(category="action_required", notice="security", asks_owner_to_act=True))
    assert not needs_task(_verdict(category="promo_or_spam"))  # a newsletter
    assert not needs_task(_verdict(category="bill_or_deadline", already_done=True))  # a receipt
    assert needs_task(_verdict(category="action_required", automated=False))  # a person asks them


def test_a_rule_against_tasks_from_emails_is_never_stored() -> None:
    from pisa.domain.profile import no_email_tasks

    assert no_email_tasks("Non creare task dalle email")
    assert no_email_tasks("Never make tasks from my mail")
    assert not no_email_tasks("Mi piace leggere le email la mattina")
    assert not no_email_tasks("Crea i compiti dalle email importanti")


def test_the_same_claim_three_times_in_one_turn_is_one_and_the_rules_are_listed(
    client, db, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    profile = client.app.state.runtime.profile

    async def three() -> list[str]:
        said = "non mi piace correre la mattina presto"
        return [
            await profile.remember(kind="dislike", statement=s, quote=said, owner_words=said, hard=True)
            for s in ("Non gli piace correre la mattina presto", "Non ama correre la mattina presto",
                      "Non gli piace correre la mattina presto")
        ]  # fmt: skip

    import asyncio

    out = asyncio.run(three())
    assert out[0].startswith("Remembered") and all(o.startswith("Already remembered") for o in out[1:])
    assert db.execute("SELECT count(*) FROM profile_claims").fetchone() == (1,)
    # Not in their words (the model's own, or outside text): refused
    refused = asyncio.run(
        profile.remember(kind="dislike", statement="Non creare task dalle email", quote="ok va bene",
                         owner_words="ok va bene", hard=True)
    )  # fmt: skip
    assert refused.startswith("Refused") and "by meaning" in refused
    said_rule = "non creare task dalle email"  # their own words: their rule, saved in their words
    kept = asyncio.run(
        profile.remember(
            kind="dislike",
            statement="Mai task dalle email",
            quote=said_rule,
            owner_words=said_rule,
            hard=True,
        )
    )
    assert not kept.startswith("Refused")
    rules = client.get("/v1/profile/rules", headers=headers).json()["rules"]
    assert [(r["statement"], r["provenance"]) for r in rules] == [
        ("Non gli piace correre la mattina presto", "said"),
        ("non creare task dalle email", "said"),
    ]
    assert rules[0]["created_at"]


# --- Pisa says it is on the phone only when the phone has it ----------------------------------------------


def test_on_your_phone_is_said_only_when_the_phone_has_it() -> None:
    from pisa.services.conversation import ON_SCREEN, _with_screen_claims  # pyright: ignore[reportPrivateUsage]

    claim = "Ti ho messo il link sul telefono."
    assert ON_SCREEN.search(claim) and not ON_SCREEN.search("La mostra chiude il 9.")
    # The claim goes and the answer stands (never a plumbing confession); a
    # reply that was only the claim still says what happened
    assert _with_screen_claims("Chiude il 9.", [claim], False, False, "it-IT") == "Chiude il 9."
    not_shown = "Non sono riuscito a mostrartelo sul telefono."
    assert _with_screen_claims("", [claim], False, False, "it-IT") == not_shown
    assert _with_screen_claims("Chiude il 9.", [claim], True, False, "it-IT") == f"Chiude il 9. {claim}"
    said = _with_screen_claims("Chiude il 9.", [], True, True, "it-IT")
    assert said == "Chiude il 9. Ti ho messo i link sul telefono."  # Pisa's own line, once delivered


def test_research_leads_with_what_is_uncommon_in_the_request() -> None:
    from pisa.brain import skills

    guide = skills.named("research")
    assert guide is not None and "What is uncommon in the request leads" in guide.body
    assert "guides and rankings first" in guide.body and "never replaces the" in guide.body


# --- a new email ("prepara un'email per…" got "non posso") --------------------

from tests.test_approvals import auth, google  # noqa: E402,F401  (fixtures)


def test_a_new_email_is_a_draft_read_back_once_and_one_yes_sends_it(client, db, google, auth) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from tests.test_approvals import decide, pending, run, thread

    thread(google, db)  # Claudio wrote to the user before
    _sent_to(db, "Claudio Rossi <claudio@x.example>")  # And the user wrote to them: known
    actions = client.app.state.runtime.actions
    text = "Ciao Claudio, sabato ci sei a cena?"
    said = run(
        client, actions.draft_email, "Claudio", "Cena di sabato", text, said="prepara un'email per Claudio"
    )
    assert "NOT sent, waiting for their yes" in said and "a new email" in said
    (draft_id,) = google.drafts
    mime = google.raw(draft_id)
    assert (mime["To"], mime["Subject"], mime["In-Reply-To"]) == (
        "Claudio Rossi <claudio@x.example>",
        "Cena di sabato",
        None,
    )
    assert google.drafts[draft_id]["threadId"] is None  # a new email, no thread
    (approval,) = pending(client, auth)  # the read-back is the send's one question
    assert approval["kind"] == "email_send" and approval["requires_biometric"] is False
    assert decide(client, auth, approval["id"], how="voice").json()["status"] == "done"
    assert google.sent == [draft_id]
    unknown = run(client, actions.draft_email, "Mario Bianchi", "Ciao", "Ciao Mario.", said="scrivi a Mario")
    assert "Ask the user for it" in unknown and len(google.drafts) == 1  # never an invented address
    # An address the user didn't say and never wrote to is never used
    never = run(client, actions.draft_email, "nino@nuovo.example", "Ciao", "Ciao Nino.", said="scrivi a Nino")
    assert "Not drafted" in never and len(google.drafts) == 1 and pending(client, auth) == []
    said_it = "scrivi a anna@nuovo.example"  # the user said the address themselves: drafted
    run(client, actions.draft_email, "anna@nuovo.example", "Ciao", "Ciao Anna.", said=said_it)
    # But they never wrote to it: a new recipient, sent with Face ID
    assert [a["requires_biometric"] for a in pending(client, auth)] == [True]


def _sent_to(db: Any, *people: str) -> None:
    """A message the user sent (Gmail's SENT label) to `people`."""
    db.execute(
        "INSERT INTO email_messages (id, thread_id, from_name, from_address, to_addresses, subject, "
        "sent_at, labels, snippet, body) VALUES (%s, %s, 'Owner', 'owner@example.org', %s, 'Ciao', now(), "
        "'{SENT}', '', '')",
        (f"s{uuid.uuid4().hex[:8]}", f"ts{uuid.uuid4().hex[:6]}", list(people)),
    )


# --- The sheet; Pisa speaks first with Pisa closed ------------------------------------------------------


def test_a_sheet_shows_only_what_this_turn_read_and_at_most_six_images() -> None:
    from pisa.brain.present_tools import SheetBlockIn, sheet_blocks
    from pisa.domain.sheet import SheetData

    read = [{"title": "Le migliori pizze al taglio", "url": "https://www.gamberorosso.it/pizza", "host": "x"}]
    blocks = [SheetBlockIn(type="text", text="Il Tondo e Gabrielli, i più citati dalle guide.")]
    blocks += [
        SheetBlockIn(type="image", url=f"https://gamberorosso.it/img{n}.jpg", alt="pizza") for n in range(7)
    ]
    blocks += [
        SheetBlockIn(type="image", url="https://evil.example.com/x.jpg", alt="x"),  # no page of this turn
        SheetBlockIn(type="image", url="http://gamberorosso.it/plain.jpg", alt="x"),  # not https
        SheetBlockIn(type="link", url="https://www.gamberorosso.it/pizza", title="Gambero Rosso"),
        SheetBlockIn(type="fact", label="Distanza", value="3 km"),
    ]
    page = " ".join(
        f"![pizza](https://gamberorosso.it/img{n}.jpg)" for n in range(7)
    )  # the page's own images
    built, dropped = sheet_blocks(blocks, read, [page], results=[], lang="it-IT")
    assert [b["type"] for b in built].count("image") == 6 and len(dropped) == 3
    assert sum("not from a page read this turn" in d for d in dropped) == 2  # the other host, and http
    assert built[-2] == {"type": "link", "url": "https://www.gamberorosso.it/pizza", "title": "Gambero Rosso",
                         "host": "gamberorosso.it"}  # fmt: skip
    SheetData.model_validate({"title": "Pizza al taglio", "blocks": built})


def test_the_sheet_fixture_is_the_contract() -> None:
    import json

    from pydantic import TypeAdapter

    from pisa.services.session_messages import PresentObject
    from tests.conftest import PROTOCOL_DIR

    fixture = json.loads((PROTOCOL_DIR / "fixtures" / "valid" / "present_sheet.json").read_text())
    shown = TypeAdapter(PresentObject).validate_json(json.dumps(fixture["object"]))
    assert shown.kind == "sheet"


def test_with_no_live_socket_the_push_asks_to_open_a_talk(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    import json as js

    from tests.test_push import deliver, with_token

    device_id, _ = pair_device(db)
    with_token(db, device_id)
    ask = uuid.uuid4()
    asked: list[tuple[Any, ...]] = []

    async def may_ask(device: uuid.UUID, nid: uuid.UUID, speak: str, priority: str) -> uuid.UUID | None:
        asked.append((device, speak, priority))
        return ask

    from pisa.services import notifications

    real_init = notifications.Dispatcher.__init__

    def with_ask(self: Any, *a: Any, **k: Any) -> None:
        real_init(self, *a, **k)
        self.ask = may_ask

    db.execute("UPDATE notifications SET speak = NULL")
    notifications.Dispatcher.__init__ = with_ask  # type: ignore[method-assign]
    try:
        orig = notifications.enqueue

        async def speaking(conn: Any, **kw: Any) -> Any:
            kw["speak"] = "leave_now"
            return await orig(conn, **kw)

        notifications_enqueue = speaking
        import tests.test_push as tp

        tp.enqueue = notifications_enqueue  # type: ignore[attr-defined]
        via, _, apple = deliver(db, secrets_key, online=set(), device_id=device_id)
    finally:
        notifications.Dispatcher.__init__ = real_init  # type: ignore[method-assign]
        tp.enqueue = orig  # type: ignore[attr-defined]
    assert via == "apns" and asked and asked[0][1] == "leave_now"
    body = js.loads(apple.requests[0].content)
    assert body["aps"]["category"] == "TALK_ASK" and body["aps"]["interruption-level"] == "time-sensitive"
    assert body["ask_id"] == str(ask) and body["why"] == "leave_now" and body["notification_id"]


def pair_device(db: Any) -> tuple[str, str]:
    from tests.conftest import pair

    return pair(db)


def test_the_pool_fixtures_are_the_contract() -> None:
    import json

    from pisa.api import schemas
    from tests.conftest import PROTOCOL_DIR

    rest = PROTOCOL_DIR / "fixtures" / "rest"
    plan = schemas.Plan.model_validate(json.loads((rest / "plan_todo.json").read_text()))
    assert [t.deadline_source for t in plan.todo] == ["web", "email", None]
    assert [i.scheduled_by for i in plan.items if i.kind == "task"] == ["owner"]
    pool = schemas.TasksResponse.model_validate(json.loads((rest / "tasks_pool.json").read_text()))
    assert [t.deadline_source for t in pool.tasks] == ["web", "email", None]
