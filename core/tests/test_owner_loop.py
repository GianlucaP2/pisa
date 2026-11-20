"""The user loop (review 2026-11-05 §4.4): the weekly talk report from production, their corrections
and quick undos as anonymised scenario candidates, and the privacy rail. One test per row that fails on revert
(`tests/mutate_owner_loop.py`)."""

import json
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from structlog.testing import capture_logs

from pisa.domain import owner_loop as rules
from pisa.domain.sheet import SheetData
from pisa.services import worker_steps
from tests.assistant_env import rome, set_settings
from tests.conftest import pair

WEEK = "2026-W43"  # Monday 19 October 2026; the clock fixture is Tuesday the 20th
CONTACT = "Ornella Ravaglia"
ADDRESS = "Via degli Esempi 14"
PLACE = "Trattoria Pilastrino"


def rt(client: Any) -> Any:
    return client.app.state.runtime


def hdr(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


# --- row 1: the weekly talk report from production ---------------------------------------------------------


def _week(db: psycopg.Connection) -> None:
    """A week of production rows: 4 talk turns on two routes (one redone, one failed), the user's AI spend on
    two days against a €2 daily cap, the replay's runs (a personal one newer: never the report's)."""
    session = uuid.uuid4()
    db.execute(
        "INSERT INTO talk_sessions (id, talker, voice, trigger, lang, started_at) "
        "VALUES (%s, 'gemini', 'v', 'press', 'it-IT', %s)",
        (session, rome(2026, 10, 19, 9)),
    )
    for n, (route, ms, failed, redone) in enumerate(
        [("speaker", 800, 0, False), ("speaker", 1200, 0, True), ("speaker", 3000, 0, False),
         ("airpods", 2000, 1, False)], 1
    ):  # fmt: skip
        db.execute(
            "INSERT INTO talk_turns (session_id, n, owner_end_at, route, failed, unbacked, redone, "
            "first_content_ms) VALUES (%s, %s, %s, %s, %s, 0, %s, %s)",
            (session, n, rome(2026, 10, 19, 9, n), route, failed, redone, ms),
        )
    db.execute(  # last week's turn: not this report's
        "INSERT INTO talk_turns (session_id, n, owner_end_at, route, failed, unbacked, redone, "
        "first_content_ms) VALUES (%s, 9, %s, 'car', 0, 0, false, 100)",
        (session, rome(2026, 10, 18, 9)),
    )
    for at, eur, budget in (
        (rome(2026, 10, 19, 10), "1.20", "owner"),
        (rome(2026, 10, 20, 8), "0.30", "owner"),
        (rome(2026, 10, 20, 8), "5.00", "eval"),  # the replay's own budget: never their turns' cost
    ):
        db.execute(
            "INSERT INTO llm_calls (id, slot, model_ref, status, reserved_eur, cost_eur, price_known, "
            "trace_id, created_at, budget) "
            "VALUES (gen_random_uuid(), 'brain', 'm', 'settled', 0, %s, true, 't', %s, %s)",
            (Decimal(eur), at, budget),
        )
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('spend_caps', %s) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value",
        (Jsonb({"daily_eur": "2.00", "monthly_eur": "40.00"}),),
    )
    for at, passed, personal in ((rome(2026, 10, 18, 3), 120, False), (rome(2026, 10, 19, 3), 3, True)):
        db.execute(
            "INSERT INTO pisa_replay_runs (id, at, effort, k, scenarios, passed, personal) "
            "VALUES (gen_random_uuid(), %s, 'low', 3, %s, %s, %s)",
            (at, 160 if not personal else 5, passed, personal),
        )


def test_the_weekly_report_is_made_by_code_from_production_rows(client, db) -> None:  # type: ignore[no-untyped-def]
    _week(db)
    report = client.portal.call(rt(client).autopilot.owner_loop.weekly, WEEK)
    assert report["week"] == WEEK and report["turns"] == 4
    assert report["first_content_p50_ms"] == 2000  # Of the 4 raw values, not a median of medians
    assert report["routes"] == [
        {"route": "airpods", "turns": 1, "p50_ms": 2000, "p95_ms": 2000},
        {"route": "speaker", "turns": 3, "p50_ms": 1200, "p95_ms": 3000},
    ]
    assert report["clean"] == {"clean": 2, "turns": 4, "pct": 50}  # the redone and the failed aren't clean
    assert report["cost"] == {
        "spent_eur": "1.5000",
        "per_turn_eur": "0.3750",
        "cap_daily_eur": "2.00",
        "economy_at_eur": "1.00",  # The 50% economy switch
        "max_day_eur": "1.2000",
        "days_economy": 1,
        "days_capped": 0,
    }
    assert report["replay"] == {"at": "2026-10-18", "effort": "low", "k": 3, "scenarios": 160, "passed": 120}
    ((stored,),) = db.execute("SELECT report FROM pisa_owner_reports WHERE week = %s", (WEEK,)).fetchall()
    assert stored == report


def test_the_report_is_a_k13_view_he_can_open(client, db) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    headers = hdr(db)
    assert client.get("/v1/views/report", headers=headers).status_code == 404  # before the first report
    _week(db)
    client.portal.call(rt(client).autopilot.owner_loop.weekly, WEEK)
    body = client.get("/v1/views/report", headers=headers).json()
    assert body["week"] == WEEK
    SheetData.model_validate(body["sheet"])
    blocks = body["sheet"]["blocks"]
    assert {"type": "stat", "label": "Replay pass^3", "value": "120/160 (75%)"} in blocks
    assert {"type": "stat", "label": "Turni puliti", "value": "50", "unit": "%"} in blocks
    table = next(b for b in blocks if b["type"] == "table")
    assert table["rows"] == [["airpods", "1", "2.0 s", "2.0 s"], ["speaker", "3", "1.2 s", "3.0 s"]]
    assert {"type": "fact", "label": "Risparmio da", "value": "€1.00 (50%)"} in blocks
    # "mostrami il report" in a talk: Pisa's built-in view, the same sheet; never listed, never in the prompt
    title, data, dropped = client.portal.call(rt(client).views.run, "il report")
    assert title == f"Report di Pisa · {WEEK}" and data == body["sheet"] and dropped == []
    assert client.portal.call(rt(client).views.names) == []
    assert client.get("/v1/views", headers=headers).json() == {"views": []}


def test_the_sunday_card_carries_the_report_as_one_line(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    _week(db)
    clock.now = rome(2026, 10, 25, 18)
    summary = client.get(f"/v1/ledger/summary?week={WEEK}", headers=hdr(db)).json()
    lines = [
        b["text"] for b in summary["sheet"]["blocks"] if b["type"] == "text" and "Report di Pisa" in b["text"]
    ]
    assert lines == [
        f"Report di Pisa ({WEEK}): 4 turni, puliti 50%, prima risposta 2.0 s (p50), spesa AI €1.5000 (÷ "
        "turni ≈ €0.3750; risparmio da €1.00 al giorno, 1 giorni), replay pass^3 120/160 (75%). Di' "
        "«mostrami il report»."
    ]
    SheetData.model_validate(summary["sheet"])


def test_the_minute_makes_the_report_hourly_without_a_push_even_when_stopped(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    names = [s.name for s in worker_steps.STEPS]
    assert names.index("owner_loop") < names.index("sunday_card")  # the card reads the report
    assert (
        "owner_loop" not in worker_steps.INITIATIVE_STEPS
    )  # numbers and rows only: nothing said, nothing pushed
    _week(db)
    loop = rt(client).autopilot.owner_loop
    client.portal.call(loop.tick)
    made = db.execute("SELECT made_at FROM pisa_owner_reports").fetchall()
    assert len(made) == 1
    clock.now += timedelta(minutes=30)
    client.portal.call(loop.tick)
    assert db.execute("SELECT made_at FROM pisa_owner_reports").fetchall() == made  # once an hour
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)


def test_the_report_never_holds_a_word_of_his(client, db) -> None:  # type: ignore[no-untyped-def]
    _week(db)
    device = pair(db)[0]
    _exchange(db, device, "sposta la palestra con Ornella al 12 novembre", "Fatto.", rome(2026, 10, 19, 9))
    with capture_logs() as logs:
        report = client.portal.call(rt(client).autopilot.owner_loop.weekly, WEEK)
    text = json.dumps(report) + json.dumps(logs, default=str)
    assert "Ornella" not in text and "palestra" not in text and "novembre" not in text
    # only routes (Pisa's own enum), ISO weeks, efforts and numbers
    assert set(rules.strings(report)) <= {WEEK, "speaker", "airpods", "2026-10-18", "low"} | {
        v for v in rules.strings(report["cost"])
    }


# --- row 2: their corrections and quick undos become anonymised scenario candidates
# --------------------------


def _exchange(db: psycopg.Connection, device: str, words: str, reply: str, at: datetime) -> None:
    conversation = uuid.uuid4()
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, device, at, at),
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'user', %s, "
        "'it-IT', %s), (%s, 'assistant', %s, 'it-IT', %s)",
        (conversation, words, at, conversation, reply, at + timedelta(seconds=4)),
    )


def _his_world(db: psycopg.Connection) -> None:
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, city, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, 'ornella ravaglia', 'email', '{ornella.f@example.invalid}', "
        "'Viterbo', now(), now())",
        (CONTACT,),
    )
    db.execute(
        "INSERT INTO places (id, name, address, created_at, updated_at) VALUES (gen_random_uuid(), %s, %s, "
        "now(), now())",
        (PLACE, f"{ADDRESS}, 00186 Roma"),
    )
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('c', 'Personale', 'owner') "
        "ON CONFLICT DO NOTHING"
    )


SEEDED = [
    "Ornella", "Ravaglia", "ornella.f", "Pilastrino", "Trattoria", "Viterbo", "00186",
    "1.250", "1250", "980", "12 novembre", "novembre", "13/11", "2026", "10-20", "10-19", "ottobre",
]  # fmt: skip


def _correction(client: Any, db: psycopg.Connection, clock: Any) -> None:
    device = pair(db)[0]
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES ('c', 'e1', 'Cena con Ornella Ravaglia', %s, %s, false, 'confirmed')",
        (clock.now + timedelta(hours=6), clock.now + timedelta(hours=8)),
    )
    _exchange(
        db,
        device,
        f"sposta la cena con Ornella Ravaglia da {PLACE} in {ADDRESS} al 12 novembre e mandale 1.250 €",
        "Fatto: cena con Ornella Ravaglia il 12 novembre alla Trattoria Pilastrino.",
        clock.now - timedelta(minutes=1),
    )
    heard = client.portal.call(
        rt(client).autopilot.evals.heard,
        uuid.UUID(device),
        "no, ti ho detto il 13/11 e 980 euro a Ornella",
        "it-IT",
    )
    assert heard is True


def _undo(db: psycopg.Connection, clock: Any) -> None:
    done = clock.now - timedelta(minutes=10)
    db.execute(
        "INSERT INTO approvals (id, kind, summary, details, payload, status, lang, created_at, expires_at, "
        "executed_at, undone_at, origin) VALUES (gen_random_uuid(), 'calendar_move', %s, '[]', %s, 'done', "
        "'it-IT', %s, %s, %s, %s, 'words')",
        (
            "«Cena con Ornella» il 12 novembre",
            Jsonb({"owner_words": f"sposta la cena con Ornella in {ADDRESS} al 12 novembre"}),
            done, done, done, done + timedelta(minutes=1),
        ),
    )  # fmt: skip


def test_a_correction_and_a_quick_undo_become_candidates_once(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _his_world(db)
    _correction(client, db, clock)
    _undo(db, clock)
    assert client.portal.call(rt(client).autopilot.evals.scan_undos) == 1
    loop = rt(client).autopilot.owner_loop
    with capture_logs() as logs:
        assert client.portal.call(loop.scan_candidates) == 2
    assert client.portal.call(loop.scan_candidates) == 0  # once per personal eval
    assert json.dumps(logs, default=str).count("scenario_candidates_made") == 1
    rows = dict(db.execute("SELECT kind, scenario FROM pisa_scenario_candidates").fetchall())
    correction, undo = rows["correction"], rows["undo"]
    assert [t["owner"] for t in correction["turns"]] == [
        "sposta la cena con [NAME] da [PLACE] in [ADDRESS] al [DATE] e mandale [AMOUNT]",
        "no, ti ho detto il [DATE] e [AMOUNT] a [NAME]",
    ]
    assert len(correction["turns"]) == len(correction["script"])  # the replay's own format
    assert correction["now"] == "2030-01-08T12:59"  # a Tuesday in the synthetic week, their clock time kept
    assert correction["world"]["events"][0][1] == "Cena con [NAME]"
    assert correction["world"]["events"][0][2].startswith("2030-01-08T")
    assert undo["turns"] == [{"owner": "sposta la cena con [NAME] in [ADDRESS] al [DATE]"}]
    assert undo["personal"]["pisa_moves"][0]["outcome"] == "«Cena con [NAME]» il [DATE]"
    # never added to the suite by code: the replay's scenarios and `--personal` read none of it
    from tests.replay.scenarios import SCENARIOS

    assert not any(str(s["id"]).startswith("candidate-") for s in SCENARIOS)


def test_the_maintenance_export_is_ids_and_anonymised_text(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _his_world(db)
    _correction(client, db, clock)
    loop = rt(client).autopilot.owner_loop
    client.portal.call(loop.scan_candidates)
    assert client.portal.call(loop.counts) == {"correction": 1}  # APPLY=0: the dry count
    (exported,) = client.portal.call(lambda: loop.export(mark=True))
    assert set(exported) == {"id", "eval_id", "kind", "replaced", "scenario"}
    assert exported["kind"] == "correction" and exported["replaced"]["[NAME]"] >= 2
    assert client.portal.call(loop.counts) == {"correction (exported)": 1}
    assert client.portal.call(lambda: loop.export(mark=True)) == []  # Each goes out once
    # the candidate goes with its personal eval (90 days)
    db.execute("DELETE FROM personal_evals")
    assert db.execute("SELECT count(*) FROM pisa_scenario_candidates").fetchone() == (0,)


# --- row 3: privacy: nothing of theirs in a candidate
# ----------------------------------------------------------


def test_a_contact_an_address_an_amount_and_a_date_never_appear_in_a_candidate(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _his_world(db)
    _correction(client, db, clock)
    _undo(db, clock)
    client.portal.call(rt(client).autopilot.evals.scan_undos)
    raw = json.dumps(
        [s for (s,) in db.execute("SELECT scenario FROM personal_evals").fetchall()], ensure_ascii=False
    )
    assert all(
        word in raw for word in ("Ornella", "Pilastrino", "1.250", "12 novembre", "13/11", "980")
    )  # their words
    loop = rt(client).autopilot.owner_loop
    with capture_logs() as logs:
        client.portal.call(loop.scan_candidates)
        exported = client.portal.call(lambda: loop.export(mark=False))
    assert len(exported) == 2
    out = json.dumps(exported, ensure_ascii=False) + json.dumps(logs, default=str, ensure_ascii=False)
    # CI: a random id ("21e72026-…", "…e810-19ab-…", "candidate-1250c0de") held "2026", "10-19" or "1250" now
    # and then; ids are not their words, so they are blanked before the check (every seeded word still
    # checked): the rows' uuids, and each candidate's own id, exactly
    out = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", out, flags=re.I)
    for candidate in exported:
        assert re.fullmatch(r"candidate-[0-9a-f]{8}", candidate["scenario"]["id"])
        out = out.replace(json.dumps(candidate["scenario"]["id"]), '"<id>"')
    leaked = [word for word in SEEDED if word.casefold() in out.casefold()]
    assert leaked == []


@pytest.mark.parametrize(
    "said, kept",
    [
        ("manda 84,20 € a Ornella", "manda [AMOUNT] a [NAME]"),
        ("pagati duemila euro il 3 marzo 2027", "pagati [AMOUNT] il [DATE]"),
        ("scrivi a ornella.f@example.invalid e apri https://x.example/a", "scrivi a [EMAIL] e apri [URL]"),
        ("vediamoci in Via degli Esempi 14 alle 18:30", "vediamoci in [ADDRESS] alle [TIME]"),
        ("la riunione è il 2026-11-04 con Giorgio Bassi", "la riunione è il [DATE] con [NAME]"),
        ("chiama il 3471234567 dopo le 19", "chiama il [N] dopo le [N]"),
        ("Sposta la palestra a domani", "Sposta la palestra a domani"),  # every word allowed: untouched
        ("chiama ornella domani", "chiama [NAME] domani"),  # a contact said in lower case
        ("cena da Trattoria Pilastrino con l'Ornella", "cena da [PLACE] con l'[NAME]"),
        ("prenota da 寿司 stasera", "prenota da [WORD] stasera"),
        # the review: a deny-list let each of these through
        ("no, la cena è con ornella da pippo stasera", "no, la cena è con [NAME] da [WORD] stasera"),
        ("Ornella ha detto di spostare la riunione", "[NAME] ha detto di spostare la riunione"),
        ("Marta ha detto di spostare la riunione", "[NAME] ha detto di spostare la riunione"),
        ("«Marta» ha chiamato", "«[NAME]» ha chiamato"),
        ("porta il pacco in via dei mille 12", "porta il pacco in [ADDRESS]"),
        ("sono a bologna", "sono a [WORD]"),
        ("lavoro da accenture", "lavoro da [WORD]"),
        ("vai su ornellafoto.it/prenota", "vai su [URL]"),
        ("gli devo cinquanta", "gli devo [N]"),
        ("non lo fare mai, ci vediamo a novembre", "non lo fare mai, ci vediamo a [DATE]"),
        (
            "ho la chemio giovedì, e mia moglie è incinta",
            "ho la [PRIVATE] [DAY], e mia [PRIVATE] è [PRIVATE]",
        ),
        ("dalla psicologa per il divorzio", "dalla [PRIVATE] per il [PRIVATE]"),
        ("sposta la cena con giulia e andrea a sabato", "sposta la cena con [WORD] e [WORD] a [DAY]"),
        ("chiama nino, francesca e paola", "chiama [WORD], [WORD] e [WORD]"),  # lower-case names, no contact
    ],
)
def test_the_anonymiser_replaces_by_type(said: str, kept: str) -> None:
    assert rules.anonymise(said, [CONTACT], [PLACE, ADDRESS])[0] == kept


def test_the_allow_list_holds_nothing_private() -> None:
    """No allowed word is a family, health or faith word, a weekday, a month, a number word, or a word
    the replay suite writes capitalised (its names and places)."""
    import re

    from pisa.domain import owner_vocab
    from pisa.domain.lexicon.scrub import QUERY_MONTHS
    from pisa.domain.text import fold
    from tests.replay.scenarios import SCENARIOS

    allowed = owner_vocab.ALLOWED
    assert not allowed & owner_vocab.PRIVATE and not allowed & owner_vocab.WEEKDAYS
    # words before months ("mai" never, "may", "giu" down): a month alone elsewhere is [DATE]
    months = {fold(w) for w in allowed} & QUERY_MONTHS
    assert months <= {"mai", "may", "giu"}
    capitalised = {
        m.group(0).casefold()
        for s in SCENARIOS
        for t in s["turns"]
        if isinstance(t, dict)
        for m in re.finditer(r"\b[A-ZÀ-Ý][^\W\d_]+", str(t.get("owner") or ""))
    }
    names = capitalised - {
        "a",
        "alle",
        "da",
        "pisa",
        "aspetto",
        "ehm",
        "web",
        "promo",
        "macro",
        "studio",
        "target",
        "stati",
    }
    assert not allowed & names, sorted(allowed & names)
    for name in ("giulia", "marco", "nino", "francesca", "paola", "pippo", "ornella", "bologna", "accenture"):
        assert name not in allowed
