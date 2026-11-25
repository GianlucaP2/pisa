# ruff: noqa: E501
""" "Stanotte": the morning sheet and "Dammi tutto", the gate,
Pisa's diary and the meter. Each test fails on revert (`tests/mutate_stanotte.py`). Invented names and places."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from pisa.domain import ideas as idea_rules
from pisa.domain import stanotte as rules
from pisa.domain.lexicon.advice import NOT_ADVICE_IT
from pisa.domain.sheet import RitualSheetData
from tests.conftest import PROTOCOL_DIR, pair

NIGHT = date(2026, 10, 21)
NIGHT_AT = datetime(2026, 10, 21, 1, 30, tzinfo=UTC)  # 03:30 in Rome: the night of 21 October
MORNING = datetime(2026, 10, 21, 6, 30, tzinfo=UTC)  # 08:30 in Rome
COMPETITOR = "https://competitor.example/app"
ISTAT = "https://istat.example/freelance"
BANDO = "https://www.regione.example/bando-digitale"
GAP = "https://www.sba.example/rules"


def _svc(client: Any) -> Any:
    return client.app.state.runtime.stanotte


def _run(client: Any, fn: Any, *args: Any) -> Any:
    return client.portal.call(fn, *args)


def _count(db: psycopg.Connection, table: str, where: str = "true") -> int:
    return int(db.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0])  # type: ignore[index]


def _night(db: psycopg.Connection, night: date = NIGHT, status: str = "done") -> None:
    at = datetime(night.year, night.month, night.day, 1, 30, tzinfo=UTC)
    db.execute(
        "INSERT INTO pisa_nights (night, started_at, finished_at, status) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (night, at, at, status),
    )


def _ev(db: psycopg.Connection, iid: uuid.UUID, rung: str, claim: str, kind: str, *, passed: bool | None = True,
        url: str | None = None, final: str | None = None, number: float | None = None, run: Any = None,
        simulated: bool = False) -> uuid.UUID:  # fmt: skip
    eid = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_idea_evidence (id, idea_id, rung, claim, url, final_url, number, run, check_kind, passed, "
        "simulated, checked_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
        (
            eid,
            iid,
            rung,
            claim,
            url,
            final,
            number,
            Jsonb(run) if run is not None else None,
            kind,
            passed,
            simulated,
        ),
    )
    return eid


def _econ() -> dict[str, Any]:
    inputs = idea_rules.Inputs(38000, 12.0, 0.01, 2.0, 40.0)
    return {"inputs": inputs.__dict__, "linked": {}, "assumed": [], "output": idea_rules.economics(inputs)}


def _idea(db: psycopg.Connection, night: date = NIGHT, *, state: str = "ready", statement: str = "Un bot che legge le ricevute e prepara la fattura",
          link_ok: bool = True, number_ok: bool = True, final: str = COMPETITOR, run: Any = None, p: float = 0.6) -> uuid.UUID:  # fmt: skip
    """A ready idea with its ladder: V0 (a competitor's page, fetched), V1 (a number on its page), V2 (code's
    run), V4 (simulated), V5 (conscience)."""
    iid = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_ideas (id, night, statement, mechanism, market, kind, cycle, state, rung, kill_criteria, "
        "kills_at, p_keep, created_at, updated_at) VALUES (%s, %s, %s, 'foto della ricevuta, estrazione dei campi, "
        "bozza', 'it', 'software', 1, %s, 'V5', %s, now(), %s, now(), now())",
        (iid, night, statement, state, Jsonb([{"metric": "competitors", "op": ">", "value": 2}]), p),
    )
    _ev(db, iid, "V0", "esiste già: Concorrente", "link", passed=link_ok, url=COMPETITOR, final=final)
    _ev(
        db,
        iid,
        "V1",
        "freelance con partita IVA",
        "number",
        passed=number_ok,
        url=ISTAT,
        final=ISTAT,
        number=38000,
    )
    _ev(db, iid, "V2", "conti in codice", "run", run=run if run is not None else _econ())
    _ev(db, iid, "V4", "pannello simulato (non è una prova)", "simulated", passed=None, simulated=True,
        run={"objections": ["costa"], "would_try_of_10": 4})  # fmt: skip
    _ev(db, iid, "V5", "chi ci perde", "conscience",
        run={"who_loses": "i commercialisti che vivono di data entry", "dark_patterns": [], "opposite": "farlo a mano",
             "refuse_to_polish": False})  # fmt: skip
    return iid


def _killed(db: psycopg.Connection, night: date = NIGHT, *, passed: bool = True) -> uuid.UUID:
    iid = uuid.uuid4()
    db.execute(
        "INSERT INTO pisa_ideas (id, night, statement, mechanism, market, kind, cycle, state, kill_criteria, created_at, "
        "updated_at) VALUES (%s, %s, 'Calendario fiscale vocale sul telefono', 'promemoria parlati per scadenze', "
        "'it', 'software', 1, 'killed', %s, now(), now())",
        (iid, night, Jsonb([{"metric": "competitors", "op": ">", "value": 2}])),
    )
    eid = _ev(db, iid, "V0", "esiste già: Altro", "link", passed=passed, url=COMPETITOR, final=COMPETITOR)
    at = datetime(night.year, night.month, night.day, 1, 40, tzinfo=UTC)
    db.execute(
        "INSERT INTO pisa_idea_cemetery (idea_id, killed_at, criterion, evidence_id, revival) VALUES (%s, %s, 0, %s, "
        "'new_evidence:competitors')",
        (iid, at, eid),
    )
    return iid


DESK_DDL = """
CREATE TABLE IF NOT EXISTS pisa_desk_windows (id uuid PRIMARY KEY, country text NOT NULL, kind text NOT NULL,
    title text NOT NULL, url text NOT NULL, day date NOT NULL, quote text NOT NULL, forms text[] NOT NULL DEFAULT '{}',
    passed boolean, first_seen date NOT NULL, checked_on date NOT NULL);
CREATE TABLE IF NOT EXISTS pisa_desk_dossiers (id uuid PRIMARY KEY, idea_id uuid NOT NULL UNIQUE, night date NOT NULL,
    market text NOT NULL, neighbours uuid[] NOT NULL DEFAULT '{}', window_id uuid, created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS pisa_desk_arbitrage (id uuid PRIMARY KEY, night date NOT NULL, dossier_id uuid,
    way text NOT NULL, has text NOT NULL, name text NOT NULL, claim text NOT NULL, url text NOT NULL, other_url text,
    passed boolean, checked_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS pisa_desk_money (dossier_id uuid PRIMARY KEY, stack jsonb NOT NULL,
    state text NOT NULL DEFAULT 'draft', updated_at timestamptz NOT NULL);
"""


def _desk(
    db: psycopg.Connection, *, window_passed: bool = True, closes: date = NIGHT + timedelta(days=19),
    gap_night: date = NIGHT,
) -> uuid.UUID:  # fmt: skip
    """The desk's tables (0080) with its real columns, until its migration is in."""
    db.execute(DESK_DDL)
    wid = uuid.uuid4()
    db.execute("INSERT INTO pisa_desk_windows (id, country, kind, title, url, day, quote, passed, first_seen, checked_on) "
               "VALUES (%s, 'it', 'grant', 'il bando regionale per il digitale', %s, %s, 'entro il 9 novembre', %s, "
               "%s, %s)", (wid, BANDO, closes, window_passed, NIGHT, NIGHT))  # fmt: skip
    db.execute("INSERT INTO pisa_desk_arbitrage (id, night, way, has, name, claim, url, passed, checked_at) VALUES "
               "(%s, %s, 'regulation', 'it', 'la fattura elettronica obbligatoria', 'negli USA non è obbligatoria', %s, true, "
               "now())", (uuid.uuid4(), gap_night, GAP))  # fmt: skip
    return wid


def _money(db: psycopg.Connection, iid: uuid.UUID, wid: uuid.UUID, *, note: bool = True) -> None:
    did = uuid.uuid4()
    db.execute("INSERT INTO pisa_desk_dossiers (id, idea_id, night, market, window_id, created_at, updated_at) VALUES "
               "(%s, %s, %s, 'it', %s, now(), now())", (did, iid, NIGHT, wid))  # fmt: skip
    stack = [{"step": "non_dilutive", "window": str(wid), "eligible": "unknown"},
             {"step": "customers", "words": "clienti: 54.720 € l'anno nei conti in codice", "buyers": "38000"},
             {"step": "equity", "words": "capitale di terzi: solo dopo i primi due"}]  # fmt: skip
    if note:
        stack.append({"step": "note", "words": NOT_ADVICE_IT})
    db.execute(
        "INSERT INTO pisa_desk_money (dossier_id, stack, updated_at) VALUES (%s, %s, now())",
        (did, Jsonb(stack)),
    )


def _curious(db: psycopg.Connection, question: str = "Il volo di giovedì atterra alle 23: chi apre lo studio venerdì?",
             *, task_open: bool = True) -> None:  # fmt: skip
    """The curiosity's horizon (0079), minimal, until its migration is in: a question on a task of his."""
    db.execute("CREATE TABLE IF NOT EXISTS pisa_unknowns (id uuid PRIMARY KEY, state text, liked boolean, "
               "updated_at timestamptz)")  # fmt: skip
    db.execute("CREATE TABLE IF NOT EXISTS pisa_unknown_reads (id uuid PRIMARY KEY, unknown_id uuid, url text, "
               "supported boolean, at timestamptz)")  # fmt: skip
    db.execute("CREATE TABLE IF NOT EXISTS pisa_unknown_changes (id bigserial PRIMARY KEY, unknown_id uuid, night date, "
               "change text)")  # fmt: skip
    db.execute("CREATE TABLE IF NOT EXISTS pisa_horizon (id uuid PRIMARY KEY, night date, item text, question text, "
               "unknown_id uuid, created_at timestamptz)")  # fmt: skip
    tid = uuid.uuid4()
    db.execute(
        "INSERT INTO tasks (id, title, title_key, status, source_kind, created_at, updated_at) VALUES (%s, "
        "'aprire lo studio', 'aprire lo studio', %s, 'manual', now(), now())",
        (tid, "open" if task_open else "done"),
    )
    db.execute(
        "INSERT INTO pisa_horizon VALUES (%s, %s, %s, %s, NULL, now())",
        (uuid.uuid4(), NIGHT, f"task:{tid}", question),
    )


def _sheet(db: psycopg.Connection, night: date = NIGHT) -> dict[str, Any]:
    row = db.execute("SELECT data, everything, lines, shown, passed, idea_id, story FROM pisa_sheets WHERE night = %s",
                     (night,)).fetchone()  # fmt: skip
    assert row is not None
    keys = ("data", "everything", "lines", "shown", "passed", "idea_id", "story")
    return dict(zip(keys, row, strict=True))


def _texts(sheet: dict[str, Any]) -> str:
    return json.dumps(sheet, ensure_ascii=False)


# --- the rules (pure) -----------------------------------------------------------------------------------------


def test_a_line_passes_only_on_its_own_code_check() -> None:
    ok = rules.Evidence("1", "V0", "esiste già: X", "link", True, False, COMPETITOR, COMPETITOR)
    assert rules.line_ok(ok)
    assert not rules.line_ok(rules.Evidence("1", "V0", "x", "link", False, False, COMPETITOR, COMPETITOR))
    # a link that landed on another site is no evidence, even when the row says passed
    assert not rules.line_ok(
        rules.Evidence("1", "V0", "x", "link", True, False, COMPETITOR, "https://parked.example/")
    )
    assert not rules.line_ok(
        rules.Evidence("1", "V4", "x", "simulated", True, True)
    )  # simulated never counts
    run = _econ()
    assert rules.line_ok(rules.Evidence("1", "V2", "conti", "run", True, False, run=run))
    tampered = {**run, "output": {**run["output"], "revenue_year_eur": 999999.0}}
    assert not rules.line_ok(
        rules.Evidence("1", "V2", "conti", "run", True, False, run=tampered)
    )  # recomputed
    assert not rules.line_ok(rules.Evidence("1", "V1", "n", "number", True, False, ISTAT, None, 38000))


def test_no_line_labels_his_feelings() -> None:
    assert not rules.clean("Sei stressato: ecco un'idea.")
    assert rules.clean("Un bot che legge le ricevute")
    assert not rules.clean("Dormi poco: chi apre lo studio venerdì?", about_him=True)
    assert rules.question_shown(rules.Question("1", "Chi apre lo studio venerdì?", True))
    assert not rules.question_shown(rules.Question("1", "Chi apre lo studio venerdì?", False))  # item gone
    assert not rules.question_shown(rules.Question("1", "Apri lo studio venerdì.", True))  # never a claim


def test_the_gate_needs_seven_nights_an_idea_and_every_line_passing() -> None:
    assert rules.SHADOW_NIGHTS == 7
    assert rules.gate_opens(7, 1, 10, 10)
    assert not rules.gate_opens(6, 6, 10, 10)
    assert not rules.gate_opens(7, 0, 10, 10)
    assert not rules.gate_opens(7, 1, 10, 9)
    assert not rules.gate_opens(7, 1, 0, 0)


# --- the sheet, built at night from rows that passed ----------------------------------------------------------


def test_the_sheet_holds_the_idea_made_the_killed_the_window_the_gap_and_a_question(
    client, db, clock
) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    iid = _idea(db)
    _killed(db)
    _desk(db)
    _curious(db)
    calls, searches = _count(db, "llm_calls"), _count(db, "pisa_night_searches")
    shown = _run(client, _svc(client).build, NIGHT)
    s = _sheet(db)
    RitualSheetData.model_validate(s["data"])
    RitualSheetData.model_validate(s["everything"])
    blocks = s["data"]["blocks"]
    types = [b["type"] for b in blocks]
    text = _texts(s["data"])
    # one idea made: its statement, the code's revenue as a stat, the sensitivities as a chart table, its links
    assert s["idea_id"] == iid and "Un bot che legge le ricevute" in text
    assert {"type": "stat", "label": "Ricavi l'anno", "value": "€54.720", "unit": "conti in codice"} in blocks
    table = next(b for b in blocks if b["type"] == "table")
    assert [r[1] for r in table["rows"]] == [27360.0, 43776.0, 54720.0, 65664.0, 109440.0]
    assert any(b["type"] == "link" and b["url"] == ISTAT for b in blocks)
    assert "would_try" not in text and "costa" not in text  # V4's panel is never on the sheet as a finding
    assert (
        "pannello simulato" not in text or "pannello simulato: non è una prova" in text
    )  # only as its label
    # the killed and why, the nearest window (days by code), the gap, the question
    assert "Calendario fiscale vocale sul telefono — simili già sul mercato > 2" in text
    assert "Tra 19 giorni chiude: il bando regionale per il digitale" in text
    assert (
        "In Italia c'è la fattura elettronica obbligatoria" in text and "chi apre lo studio venerdì?" in text
    )
    assert types.count("section") == 5
    # every line names its row and passed; nothing outward: no model call, no search
    assert shown == s["shown"] == s["passed"] == len(s["lines"]) == 8
    assert {line["part"] for line in s["lines"]} == {"idea", "killed", "window", "gap", "question"}
    assert (_count(db, "llm_calls"), _count(db, "pisa_night_searches")) == (calls, searches)
    # "Dammi tutto" behind it: the night-shift story, the counts, every list
    every = _texts(s["everything"])
    assert (
        s["everything"]["title"] == "Dammi tutto"
        and "Stanotte ho ripreso" in every
        and "Idee pronte" in every
    )
    assert s["story"].startswith("Stanotte ho ripreso")
    # shadow: built and stored, never sent
    assert _count(db, "ritual_sheets") == 0 and _count(db, "notifications") == 0
    assert db.execute("SELECT state FROM pisa_ideas WHERE id = %s", (iid,)).fetchone() == ("ready",)


def test_an_idea_whose_evidence_fails_stays_off_the_sheet(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    run = _econ()
    bad = [
        _idea(db, link_ok=False, statement="Idea con un link che non risolve", p=0.9),
        _idea(db, number_ok=False, statement="Idea con un numero non sulla pagina", p=0.8),
        _idea(db, final="https://parked.example/", statement="Idea il cui link atterra altrove", p=0.7),
        _idea(db, run={**run, "output": {**run["output"], "revenue_year_eur": 1.0}}, statement="Idea con i conti ritoccati", p=0.65),
    ]  # fmt: skip
    _run(client, _svc(client).build, NIGHT)
    s = _sheet(db)
    assert s["idea_id"] is None and s["shown"] == 0
    text = _texts(s)
    for statement in (
        "link che non risolve",
        "numero non sulla pagina",
        "atterra altrove",
        "conti ritoccati",
    ):
        assert statement not in text  # neither on the sheet nor in "Dammi tutto"
    assert all(
        db.execute("SELECT state FROM pisa_ideas WHERE id = %s", (i,)).fetchone() == ("ready",) for i in bad
    )
    good = _idea(db, statement="Idea con ogni riga verificata", p=0.1)
    db.execute("DELETE FROM pisa_sheets")
    _run(client, _svc(client).build, NIGHT)
    assert _sheet(db)["idea_id"] == good  # the only one whose every line passes, though Pisa ranked it last


def test_a_window_not_found_on_its_page_or_past_and_a_question_on_a_gone_item_are_not_shown(
    client, db, clock
) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    yesterday = NIGHT - timedelta(days=1)
    _desk(db, window_passed=False, gap_night=yesterday)  # yesterday's gaps: not tonight's
    _desk(db, closes=yesterday, gap_night=yesterday)
    _curious(db, task_open=False)
    _curious(db, "Sei stanco, chi apre lo studio venerdì?")  # Never about how they feel
    _run(client, _svc(client).build, NIGHT)
    s = _sheet(db)
    assert s["lines"] == [] and "fattura elettronica" not in _texts(s)
    assert "bando regionale" not in _texts(s) and "studio" not in _texts(s["data"])


def test_the_money_line_keeps_its_closing_words_or_is_not_shown(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    iid = _idea(db)
    wid = _desk(db)
    _money(db, iid, wid)
    _run(client, _svc(client).build, NIGHT)
    s = _sheet(db)
    money = next(b for b in s["data"]["blocks"] if b["type"] == "fact" and b["label"] == "I soldi (bozza)")
    assert money["value"].startswith("bandi prima: 1; clienti: 54.720 €")
    assert "pisa_desk_money" in {line["table"] for line in s["lines"]}
    full = rules.Money(
        "d", tuple(db.execute("SELECT stack FROM pisa_desk_money").fetchone()[0]), frozenset({str(wid)})
    )
    assert full.words("it-IT").endswith("È un conteggio, non una consulenza.")
    db.execute("DELETE FROM pisa_sheets")
    db.execute("DELETE FROM pisa_desk_money")
    db.execute("DELETE FROM pisa_desk_dossiers")
    _money(db, iid, wid, note=False)  # a stack without "È un conteggio, non una consulenza": never shown
    _run(client, _svc(client).build, NIGHT)
    assert "I soldi" not in _texts(_sheet(db)["data"])


def test_a_part_with_no_passing_row_is_left_out_never_filled_in(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    _killed(db, passed=False)  # the line that killed it doesn't pass: not shown
    _run(client, _svc(client).build, NIGHT)
    s = _sheet(db)
    assert s["data"]["blocks"] == [{"type": "text", "text": rules._W["it-IT"]["nothing"]}] and s["shown"] == 0


# --- the gate: shut by default; code counts the nights and opens it; they can switch it off -------------------


def _shadow_nights(
    client: Any, db: psycopg.Connection, clock: Any, n: int, *, idea_on: int | None = 0, killed: bool = False
) -> date:
    night = NIGHT
    for k in range(n):
        night = NIGHT + timedelta(days=k)
        clock.now = datetime(night.year, night.month, night.day, 1, 30, tzinfo=UTC)
        _night(db, night, status="running")
        if idea_on is not None and k == idea_on:
            _idea(db, night)
        if killed:
            _killed(db, night)
        _run(client, _svc(client).build, night)
    return night


def _rituals(client: Any, db: psycopg.Connection) -> dict[str, Any]:
    hdr = {"Authorization": f"Bearer {pair(db)[1]}"}
    return client.get("/v1/rituals", headers=hdr).json()


def test_the_gate_stays_shut_for_six_nights_and_code_opens_it_on_the_seventh(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    assert _rituals(client, db)["stanotte"] == {"on": False}  # off by default
    _shadow_nights(client, db, clock, 6)
    assert db.execute("SELECT 1 FROM app_settings WHERE key = 'stanotte_gate'").fetchone() is None
    assert _rituals(client, db)["stanotte"] == {"on": False}
    _shadow_nights(client, db, clock, 7)
    assert _rituals(client, db)["stanotte"] == {"on": True}
    gate = db.execute("SELECT value FROM app_settings WHERE key = 'stanotte_gate'").fetchone()[0]
    assert gate["nights"] == 7 and gate["lines"] > 0


def test_the_gate_stays_shut_without_an_idea_or_with_one_line_failing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _shadow_nights(client, db, clock, 7, idea_on=None, killed=True)
    assert _count(db, "pisa_sheets", "shown > 0") == 7  # lines shown, all passing, but no idea made
    assert _rituals(client, db)["stanotte"] == {"on": False}  # seven nights, no idea made
    db.execute("DELETE FROM pisa_sheets")
    _shadow_nights(client, db, clock, 6)
    # a line shown on night 1 no longer passes when code counts (its page now lands elsewhere)
    db.execute("UPDATE pisa_idea_evidence SET final_url = 'https://parked.example/' WHERE rung = 'V0'")
    _shadow_nights(client, db, clock, 7, idea_on=None)
    assert _rituals(client, db)["stanotte"] == {"on": False}


def test_his_off_is_never_undone_by_code(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _shadow_nights(client, db, clock, 7)
    assert _rituals(client, db)["stanotte"] == {"on": True}
    hdr = {"Authorization": f"Bearer {pair(db)[1]}"}
    assert client.put("/v1/rituals", headers=hdr, json={"stanotte": {"on": False}}).json()["stanotte"] == {
        "on": False
    }
    db.execute(
        "DELETE FROM app_settings WHERE key = 'stanotte_owner'"
    )  # the gate's own once, alone (not the briefing's)
    night = NIGHT + timedelta(days=7)
    clock.now = datetime(night.year, night.month, night.day, 1, 30, tzinfo=UTC)
    _night(db, night, status="running")
    _idea(db, night)
    _run(client, _svc(client).build, night)
    assert _rituals(client, db)["stanotte"] == {"on": False}
    clock.now = datetime(night.year, night.month, night.day, 6, 30, tzinfo=UTC)

    async def morning() -> Any:
        async with client.app.state.runtime.pool.connection() as conn:
            return await _svc(client).morning(conn, "it-IT")

    assert _run(client, morning) is None  # off: nothing sent


# --- the morning: sent only with the gate open, with what they already get ------------------------------------


def _briefing(client: Any, db: psycopg.Connection, clock: Any, at: datetime) -> tuple[str, str | None, Any]:
    from tests.test_surprise import _settings

    if db.execute("SELECT 1 FROM app_settings WHERE key = 'settings'").fetchone() is None:
        _settings(db)
    runtime = client.app.state.runtime
    row = db.execute("SELECT id FROM devices").fetchone()
    phone = row[0] if row else uuid.UUID(pair(db, "iPhone")[0])
    clock.now = at
    got = _run(client, runtime.briefings.on_presence, phone, "app_open")
    assert got is not None
    found = db.execute("SELECT text, ritual, sheet_id FROM notifications WHERE id = (SELECT notification_id FROM "
                       "briefings WHERE id = %s)", (got.id,)).fetchone()  # fmt: skip
    assert found is not None
    return found[0], found[1], found[2]


def test_until_the_gate_opens_the_sheet_is_stored_and_never_sent(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    last = _shadow_nights(client, db, clock, 6)
    text, ritual, sheet = _briefing(
        client, db, clock, datetime(last.year, last.month, last.day, 6, 30, tzinfo=UTC)
    )
    assert ritual is None and sheet is None and "foglio di stanotte" not in text
    assert _count(db, "ritual_sheets") == 0 and _count(db, "pisa_sheets", "sent_at IS NOT NULL") == 0
    assert _count(db, "pisa_sheets") == 6
    # their own "on" before code opened the gate sends nothing either: the gate is the seven nights
    hdr = {"Authorization": f"Bearer {pair(db)[1]}"}
    client.put("/v1/rituals", headers=hdr, json={"stanotte": {"on": True}})
    db.execute(
        "UPDATE pisa_sheets SET built_at = %s", (datetime(last.year, last.month, last.day, 5, 0, tzinfo=UTC),)
    )
    db.execute("UPDATE app_settings SET value = jsonb_set(value, '{briefing,repeat_after_min}', '60') "
               "WHERE key = 'settings'")  # fmt: skip
    _, ritual, _ = _briefing(client, db, clock, datetime(last.year, last.month, last.day, 8, 30, tzinfo=UTC))
    assert ritual is None and _count(db, "ritual_sheets") == 0


def test_once_open_the_morning_briefing_s_notice_carries_the_sheet_once(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    last = _shadow_nights(client, db, clock, 7, idea_on=6)
    iid = db.execute("SELECT idea_id FROM pisa_sheets WHERE night = %s", (last,)).fetchone()[0]
    text, ritual, sheet = _briefing(
        client, db, clock, datetime(last.year, last.month, last.day, 6, 30, tzinfo=UTC)
    )
    assert ritual == "stanotte" and sheet is not None and text.endswith("Il foglio di stanotte è pronto.")
    hdr = {"Authorization": f"Bearer {pair(db)[1]}"}
    got = client.get(f"/v1/sheets/sheet:{sheet}", headers=hdr)
    assert got.status_code == 200 and got.json()["data"]["title"] == "Stanotte"
    tutto = db.execute("SELECT tutto_id FROM pisa_sheets WHERE night = %s", (last,)).fetchone()[0]
    assert client.get(f"/v1/sheets/sheet:{tutto}", headers=hdr).json()["data"]["title"] == "Dammi tutto"
    assert db.execute("SELECT state FROM pisa_ideas WHERE id = %s", (iid,)).fetchone() == ("shown",)
    db.execute(
        "UPDATE app_settings SET value = jsonb_set(value, '{briefing,repeat_after_min}', '60') "
        "WHERE key = 'settings'"
    )  # a second automatic briefing two hours later: not sent again
    _, again, _ = _briefing(client, db, clock, datetime(last.year, last.month, last.day, 8, 30, tzinfo=UTC))
    assert again is None and _count(db, "ritual_sheets", "ritual = 'stanotte'") == 1


def test_a_sheet_whose_line_no_longer_passes_at_send_time_is_withheld(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    last = _shadow_nights(client, db, clock, 7, idea_on=6)
    db.execute("UPDATE pisa_idea_evidence SET passed = false WHERE rung = 'V1'")
    _, ritual, sheet = _briefing(
        client, db, clock, datetime(last.year, last.month, last.day, 6, 30, tzinfo=UTC)
    )
    assert ritual is None and sheet is None and _count(db, "ritual_sheets") == 0


def test_the_sunday_card_gains_a_part_only_with_the_gate_open(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(client)
    last = _shadow_nights(client, db, clock, 6)
    assert _run(client, svc.sunday, "it-IT") == []
    last = _shadow_nights(client, db, clock, 7)
    clock.now = datetime(last.year, last.month, last.day, 18, 0, tzinfo=UTC)
    part = _run(client, svc.sunday, "it-IT")
    assert part[0] == {"type": "section", "title": "Stanotte"}
    assert {"type": "stat", "label": "Notti", "value": "7"} in part


# --- Pisa's diary, the meter, the story -------------------------------------------------------------------------


def test_pisa_s_diary_and_the_meter_are_written_from_records(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    _idea(db)
    _killed(db)
    _run(client, _svc(client).build, NIGHT)
    lines = [r[0] for r in db.execute("SELECT line FROM pisa_diary WHERE night = %s", (NIGHT,)).fetchall()]
    assert lines and all(rules.clean(line) for line in lines)
    assert db.execute("SELECT origin FROM pisa_diary LIMIT 1").fetchone() == ("pisa_self",)
    meter = db.execute(
        "SELECT verified, map_cells, flat FROM pisa_meter WHERE month = '2026-10-01'"
    ).fetchone()
    assert meter == (1, 0, False)
    assert rules.flat(rules.Meter(0, 1, 0, 0), rules.Meter(0, 1, 0, 0))
    assert not rules.flat(rules.Meter(0, 2, 0, 0), rules.Meter(0, 1, 0, 0))
    story = rules.story("it-IT", {"problems": 2, "incubated": 20, "validated": 8, "ready": 1, "killed": 3,
                                  "searches": 31, "spent_eur": 0.14})  # fmt: skip
    assert "31 ricerche su 40" in story and "€0,14" in story and len(story.split()) <= 90  # about 30 seconds


# --- the rails: the stop, shadow, corollary discharge, the prompt -------------------------------------------


def test_with_ferma_tutto_on_the_night_builds_no_sheet(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    night = client.app.state.runtime.night
    assert "s1_sheet" in night.steps() and night.steps()["s1_sheet"] == _svc(client).build
    from pisa.domain import night as night_rules

    assert night_rules.STEPS[-1] == "s1_sheet"  # last: it reads what the steps before it wrote
    clock.now = NIGHT_AT
    db.execute("INSERT INTO pisa_stops (stopped_at, stopped_by) VALUES (%s, 'voice')", (NIGHT_AT,))
    _run(client, night.run)
    assert _count(db, "pisa_sheets") == 0 and _count(db, "pisa_diary") == 0
    db.execute("UPDATE pisa_stops SET resumed_at = %s", (NIGHT_AT,))
    _run(client, night.run)
    assert _count(db, "pisa_sheets") == 1


def test_no_turn_intake_profile_grounding_or_style_path_reads_the_sheet_s_tables() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "pisa"
    readers = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if any(t in p.read_text(encoding="utf-8") for t in ("pisa_sheets", "pisa_diary", "pisa_meter"))
    )  # fmt: skip
    # Their deletion by family (the API only, signed; never a turn path)
    # The knock's lead and "Dammi tutto" read the sheet by voice, said to
    # them by code from the sheet's checked lines; never into a prompt, their profile, intake, grounding or style
    assert readers == [
        "domain/erase.py",
        "domain/voice1.py",
        "services/stanotte.py",
        "services/voice1.py",
    ]


def test_a_turn_s_prompt_is_byte_identical_with_the_sheet_s_tables_empty_and_full(
    client, db, fake_models, secrets_key, clock
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_acting import socket_turns
    from tests.test_measure import _same
    from tests.test_talk_timeline import Heard

    class Tools(Heard):
        """Heard, and the tools each request offered (review LOW: the tool list is compared too)."""

        def __init__(self) -> None:
            super().__init__()
            self.tools: list[list[str]] = []

        async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
            self.tools.append(sorted(t.name for t in info.function_tools))
            async for part in super().__call__(messages, info):
                yield part

    seen: list[tuple[str, str, int, str]] = []
    clock.now = NIGHT_AT
    for filled in (False, True):
        if filled:
            _night(db, status="running")
            _idea(db)
            _killed(db)
            _run(client, _svc(client).build, NIGHT)
            db.execute(
                "INSERT INTO app_settings (key, value) VALUES ('stanotte_gate', '{}') ON CONFLICT DO NOTHING"
            )
        model = Tools()
        fake_models.default = model
        socket_turns(client, db, secrets_key, model, "Pisa, cosa hai fatto stanotte?")  # type: ignore[arg-type]
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
    assert seen[0] == seen[1]  # byte-identical: the sheet adds 0 chars and 0 tools to the first request
    assert json.loads(seen[0][3])  # the tools were seen


def test_the_contract_fixtures() -> None:
    from pisa.api import schemas
    from tests.live_app import message_of

    note = message_of((PROTOCOL_DIR / "fixtures" / "valid" / "notify_stanotte.json").read_text())
    assert note.ritual == "stanotte" and note.kind == "briefing"  # type: ignore[union-attr]
    sheet = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "stanotte_sheet.json").read_text())
    schemas.RitualSheet.model_validate(sheet)
    RitualSheetData.model_validate(sheet["data"])


# --- the review of 7f76d36 -------------------------------------------------------------------------


def test_a_sheet_that_fails_never_fails_the_morning_briefing(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    briefings = client.app.state.runtime.briefings

    async def broken(conn: Any, lang: str) -> Any:
        await conn.execute("SELECT no_such_column FROM pisa_sheets")  # aborts the transaction it runs in
        return None

    briefings.stanotte = broken
    text, ritual, sheet = _briefing(client, db, clock, MORNING)
    assert text and ritual is None and sheet is None  # the briefing went out, without a sheet


def test_one_bad_night_delays_the_gate_never_keeps_it_shut(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _shadow_nights(client, db, clock, 1)  # night 1 with an idea, then its page lands elsewhere
    db.execute("UPDATE pisa_idea_evidence SET final_url = 'https://parked.example/' WHERE rung = 'V0'")
    db.execute("UPDATE pisa_ideas SET state = 'killed'")  # it no longer stands for the sheet
    for k in range(1, 8):
        night = NIGHT + timedelta(days=k)
        clock.now = datetime(night.year, night.month, night.day, 1, 30, tzinfo=UTC)
        _night(db, night, status="running")
        if k == 7:
            _idea(db, night, final=COMPETITOR)
        _run(client, _svc(client).build, night)
        if k < 7:
            assert _rituals(client, db)["stanotte"] == {"on": False}
    assert _rituals(client, db)["stanotte"] == {"on": True}  # 7 clean nights after the bad one


def test_a_question_whose_item_went_and_a_killed_line_that_fails_are_rechecked(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NIGHT_AT
    _night(db, status="running")
    _curious(db)
    _killed(db)
    _run(client, _svc(client).build, NIGHT)
    lines = _sheet(db)["lines"]

    async def recheck() -> Any:
        async with client.app.state.runtime.pool.connection() as conn:
            return await _svc(client).recheck(conn, lines)

    assert _run(client, recheck) == (2, 2)
    db.execute("UPDATE tasks SET status = 'done'")  # the dated item is gone: its question is not sent
    assert _run(client, recheck) == (2, 1)
    db.execute("UPDATE pisa_idea_evidence SET passed = false")  # the line that killed it no longer passes
    assert _run(client, recheck) == (2, 0)


def test_any_database_error_in_another_lane_s_table_leaves_the_part_off(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.stanotte import _read

    db.execute(DESK_DDL)

    async def read() -> Any:
        async with client.app.state.runtime.pool.connection() as conn, conn.transaction():
            got = await _read(
                conn, "pisa_desk_windows", "SELECT 1 / 0 FROM pisa_desk_windows, (SELECT 1) x", ()
            )
            await conn.execute("SELECT 1")  # the transaction is still usable
            return got

    db.execute("INSERT INTO pisa_desk_windows (id, country, kind, title, url, day, quote, first_seen, checked_on) "
               "VALUES (%s, 'it', 'grant', 't', %s, %s, 'q', %s, %s)", (uuid.uuid4(), BANDO, NIGHT, NIGHT, NIGHT))  # fmt: skip
    assert _run(client, read) == []


def test_a_time_briefing_held_by_quiet_hours_never_spends_the_sheet(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.test_surprise import _settings

    last = _shadow_nights(client, db, clock, 7, idea_on=6)
    _settings(db, quiet={"hours": {"start": "22:00", "end": "09:00"}})
    clock.now = datetime(last.year, last.month, last.day, 6, 30, tzinfo=UTC)  # 08:30 in Rome: quiet
    briefings = client.app.state.runtime.briefings

    async def sheet(trigger: str) -> Any:
        from pisa.services.settings import load

        async with client.app.state.runtime.pool.connection() as conn, conn.transaction():
            return await briefings._sheet(conn, trigger, await load(conn, language="it-IT"), clock.now)

    assert _run(client, sheet, "time") is None
    assert _count(db, "pisa_sheets", "sent_at IS NOT NULL") == 0 and _count(db, "ritual_sheets") == 0
    assert _run(client, sheet, "app_open") is not None  # they show up: now it is theirs


def test_his_off_before_the_gate_opens_survives_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _shadow_nights(client, db, clock, 3)
    hdr = {"Authorization": f"Bearer {pair(db)[1]}"}
    client.put("/v1/rituals", headers=hdr, json={"stanotte": {"on": False}})
    _shadow_nights(client, db, clock, 7)
    assert db.execute("SELECT 1 FROM app_settings WHERE key = 'stanotte_gate'").fetchone() is not None
    assert _rituals(client, db)["stanotte"] == {"on": False}  # code opened the gate, their off stands


def test_numbers_as_people_read_them_in_both_languages() -> None:
    assert rules.num(1_000_000, lang="it-IT") == "1.000.000" and rules.num(1_000_000, "en-US") == "1,000,000"
    assert (
        rules.num(0.5, lang="it-IT") == "0,5"
        and rules.num(0.01, "en-US") == "0.01"
        and rules.num(38000.0, lang="it-IT") == "38.000"
    )
    assert rules.cents(0.14, lang="it-IT") == "€0,14" and rules.cents(0.14, "en-US") == "€0.14"
    k = rules.Killed("k", "Un'idea", "buyers", "<", 1_000_000.0, None)
    assert rules.killed_line(k, "it-IT").endswith("clienti possibili < 1.000.000")
    en = rules.grade_words(
        "en-US", ["V0", "V1", "V2", "V5"], {"competitors": 1, "buyers": 38000, "revenue_year_eur": 54720}
    )
    assert en.startswith("1 similar found, linked; market: 38,000") and "€54,720 a year" in en
    assert "conti fatti in codice" in rules.grade_words("it-IT", ["V2"], {"revenue_year_eur": 54720})


def test_links_titles_and_the_block_room(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(rules, "SHEET_ROOM", 12)  # a small room, so that the last parts don't fit
    js = rules.Evidence(
        "1", "V1", "n", "number", True, False, "javascript:alert(1)", "javascript:alert(1)", 3
    )
    assert not rules.line_ok(js)
    run = _econ()
    ev = (
        rules.Evidence("1", "V0", "esiste già: Sei stressato", "link", True, False, COMPETITOR, COMPETITOR),
        rules.Evidence("2", "V1", "freelance", "number", True, False, ISTAT, ISTAT, 38000),
        rules.Evidence("3", "V2", "conti", "run", True, False, run=run),
        rules.Evidence("4", "V5", "chi", "conscience", True, False, run={"who_loses": "x", "opposite": "y"}),
    )
    idea = rules.Idea("i", "Un bot", "foto della ricevuta e bozza", "it", "software", "g", ev)
    assert all("stressato" not in json.dumps(b) for b in rules.idea_blocks(idea, "it-IT"))
    many = [
        rules.Window(str(n), f"bando {n}", "it", NIGHT + timedelta(days=n + 1), BANDO, True) for n in range(3)
    ]
    gap = rules.Gap("g", "cost", "it", "x", "y", GAP, True)
    q = rules.Question("q", "Chi apre lo studio?", True)
    built = rules.build("it-IT", NIGHT, ideas=[idea], killed=[], windows=many, gaps=[gap] * 3, questions=[q] * 3,
                        story="s", diary=[], counts={})  # fmt: skip
    assert len(built.data["blocks"]) <= rules.SHEET_ROOM
    parts = {line.part for line in built.lines}
    shown = {b["title"] for b in built.data["blocks"] if b["type"] == "section"}
    assert ("question" in parts) == (
        "Una cosa a cui forse non hai pensato" in shown
    )  # a cut part counts nothing
