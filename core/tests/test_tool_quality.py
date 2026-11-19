"""Quality. Row 1, the newer tools win for the user's words
(the tool offered at once and the skill that names it retrieved); the rest of the rows below."""

import dataclasses

import pytest
from pydantic_ai.tools import ToolDefinition

from tests.assistant_env import assistant_data

# --- row 1: tool choice ------------------------------------------------------------------------------------

# The phrasings real-model replays failed on (Haiku and Sonnet picked the older tool or none), with the tool
# and the skill that must come with them.
NEW_TOOLS = [
    ("ricordami quando sono da Esselunga di comprare il bagnoschiuma", "remember_at_place", "place-notes"),
    ("scrivi a Marco su WhatsApp che arrivo alle 9", "prepare_message", "messages"),
    ("chiudiamo", "close_day", "rituals"),
    ("aggiungi la scadenza della carta d'identità: marzo 2027", "add_document", "vault"),
    ("vorrei che tu potessi dirmi la massima di domani a Roma, con un numero solo", "wish_skill", "wishes"),
    ("sì, gli spostamenti falli pure senza chiedermelo", "accept_autonomy", "autonomy"),
    ("chiamami un Uber per l'aeroporto", "prepare_ride", "world-handoffs"),
    ("remind me when I'm at the pharmacy to buy aspirin", "remember_at_place", "place-notes"),
]


@pytest.mark.parametrize(("words", "tool", "skill"), NEW_TOOLS)
def test_the_owner_s_words_bring_the_newer_tool_and_its_skill(words: str, tool: str, skill: str) -> None:
    from pisa.brain import skills
    from pisa.domain.tool_choice import tools_for

    assert tool in tools_for(words)
    ranked = skills.Library(skills.load()).search(words)
    assert ranked and ranked[0][1].name == skill, [(round(s, 2), k.name) for s, k in ranked]


@pytest.mark.parametrize(
    "words",
    [
        "che ore sono?",
        "metti il dentista domani alle 10",
        "aggiungi comprare il latte",
        "sì",
        "spostala alle 18",
    ],
)
def test_ordinary_words_bring_no_newer_tool(words: str) -> None:
    from pisa.domain.tool_choice import tools_for

    assert tools_for(words) == frozenset()


def test_a_deferred_tool_his_words_name_is_offered_at_once_and_the_rest_stay_deferred() -> None:
    from pisa.brain.tool_search import offered
    from pisa.domain.tool_choice import tools_for

    def shown(defs: list[ToolDefinition], words: str) -> list[ToolDefinition]:
        return offered(defs, tools_for(words))

    defs = [
        ToolDefinition(name="add_task", description="a task"),
        ToolDefinition(name="remember_at_place", description="a note at a place", defer_loading=True),
        ToolDefinition(name="close_day", description="the closing sheet", defer_loading=True),
    ]
    out = {d.name: d.defer_loading for d in shown(defs, "ricordami quando sono da Esselunga del latte")}
    assert out == {"add_task": False, "remember_at_place": False, "close_day": True}
    assert shown(defs, "che ore sono?") == defs
    assert all(dataclasses.is_dataclass(d) for d in shown(defs, "chiudiamo"))


def test_the_skills_name_no_older_tool_for_a_message_and_none_is_a_draft() -> None:
    import re
    from pathlib import Path

    from pisa.brain import skills

    def tools(name: str) -> list[str]:  # the front matter's `tools:` line
        listed = re.search(r"^tools:(.*)$", (skills.SKILLS_DIR / f"{name}.md").read_text(), re.MULTILINE)
        line = listed.group(1).split("  #")[0] if listed else ""
        return [t.strip() for t in line.split(",") if t.strip()]

    assert tools("messages")[0] == "prepare_message"
    assert "prepare_message" in tools("people") and "compose_message" not in tools("people")
    drafts = [p.name for p in Path(skills.SKILLS_DIR).glob("*.md") if "status: reviewed" not in p.read_text()]
    assert drafts == []  # a draft is never loaded: the phase-4 rituals skill was one


# --- row 5: the vault from the research --------------------------------------------------------------------


def test_the_research_lead_times_are_the_defaults() -> None:
    from pisa.domain import vault as v

    kinds = ("cie", "passport", "driving_licence", "us_licence", "visa", "green_card", "car_revision",
             "car_insurance")  # fmt: skip
    firsts = {k: v.default_reminders(k)[0]["days_before"] for k in kinds}
    assert firsts == {"cie": 210, "passport": 270, "driving_licence": 120, "us_licence": 365, "visa": 90,
                      "green_card": 180, "car_revision": 60, "car_insurance": 30}  # fmt: skip


def _starters():  # invented starters: none ships with the product
    from pisa.domain import vault as v

    once = v.Starter(
        "paper_id", ("Controllare il documento", "Check the document"), ("Nota.", "A note."), None, 0
    )
    yearly = v.Starter("yearly", ("Pagare la rata", "Pay the rate"), ("Il 30.", "The 30th."), (11, 30), 15)
    season = v.Starter("season", ("Mandare la lista", "Send the list"), ("Il 31.", "The 31st."), (1, 31), 16)
    return (once, yearly, season)


def test_no_starter_ships() -> None:
    from pisa.domain import vault as v

    assert v.STARTERS == ()


def test_the_starters_each_year_at_their_lead_and_the_one_off_now() -> None:
    from datetime import date

    from pisa.domain import vault as v

    by_key = {s.key: s for s in _starters()}
    assert v.starter_due(by_key["yearly"], date(2026, 11, 14)) is None
    assert v.starter_due(by_key["yearly"], date(2026, 11, 15)) == ("yearly:2026", date(2026, 11, 30))
    assert v.starter_due(by_key["season"], date(2027, 1, 15)) == ("season:2027", date(2027, 1, 31))
    assert v.starter_due(by_key["paper_id"], date(2026, 11, 3)) == ("paper_id", date(2026, 12, 3))


def test_a_fresh_install_places_no_starter_task(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.assistant_env import rome
    from tests.test_ledger_and_autonomy import rt

    clock.now = rome(2026, 11, 16, 9)
    assert client.portal.call(rt(client).autopilot.vault.starters) == 0
    assert db.execute("SELECT count(*) FROM tasks").fetchone() == (0,)


def test_starters_go_in_the_pool_once_with_a_ledger_line_and_wait_for_a_stop(
    client, db, clock, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import vault as v
    from tests.assistant_env import rome, set_settings
    from tests.test_ledger_and_autonomy import rt

    monkeypatch.setattr(v, "STARTERS", _starters())
    set_settings(db, {"language": "it-IT"})
    vault = rt(client).autopilot.vault
    clock.now = rome(2026, 11, 16, 9)  # the one-off check and the yearly one in its lead; the season not yet
    assert client.portal.call(vault.starters) == 2
    assert client.portal.call(vault.starters) == 0  # once
    titles = {r[0]: r[1] for r in db.execute("SELECT title, source_ref FROM tasks").fetchall()}
    assert titles["Pagare la rata"] == "vault:starter:yearly:2026"
    assert "Controllare il documento" in titles
    assert db.execute("SELECT count(*) FROM pisa_decisions WHERE tool = 'vault_reminder'").fetchone() == (2,)
    from pisa.services import worker_steps

    assert "vault_starters" in worker_steps.INITIATIVE_STEPS  # a stop pauses them


def test_no_paper_id_check_when_a_cie_is_kept(client, db, clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pisa.domain import vault as v
    from tests.assistant_env import rome
    from tests.test_ledger_and_autonomy import rt

    monkeypatch.setattr(v, "STARTERS", _starters())
    vault = rt(client).autopilot.vault
    client.portal.call(lambda: vault.add({"kind": "cie", "expires_on": "2031-03-01"}, provenance="app",
                                         lang="it-IT"))  # fmt: skip
    clock.now = rome(2026, 12, 20, 9)
    client.portal.call(vault.starters)
    assert db.execute("SELECT count(*) FROM tasks WHERE title = 'Controllare il documento'").fetchone() == (
        0,
    )


def test_a_paper_id_card_is_warned_in_the_tool_and_the_api(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.profile_tools import ProfileTurn
    from tests.test_ledger_and_autonomy import hdr, in_turn, rt, tools

    turn = ProfileTurn(
        words="aggiungi la scadenza della mia carta d'identità cartacea: giugno 2028", lang="it-IT"
    )
    said = in_turn(client, tools(rt(client).autopilot, turn)["add_document"], "cie", "2028-06",
                   notes="cartacea")  # fmt: skip
    assert said.startswith("Kept") and "non vale più per l'espatrio dal 3 agosto 2026" in said
    docs = client.get("/v1/vault/documents", headers=hdr(db)).json()
    assert "3 agosto 2026" in docs["documents"][0]["warning"]


def test_aire_asked_once_never_assumed_and_kept_from_his_words(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.profile_tools import ProfileTurn
    from tests.test_ledger_and_autonomy import hdr, in_turn, rt, tools

    autopilot = rt(client).autopilot
    auth = hdr(db)
    assert client.get("/v1/vault/documents", headers=auth).json()["aire"] == {"registered": None,
                                                                             "asked_on": None}  # fmt: skip
    turn = ProfileTurn(words="aggiungi la scadenza del passaporto: maggio 2030", lang="it-IT")
    t = tools(autopilot, turn)
    first = in_turn(client, t["add_document"], "passport", "2030-05")
    assert "Sei iscritto all'AIRE?" in first
    turn.words = "aggiungi la scadenza della patente: maggio 2031"
    assert "AIRE" not in in_turn(client, t["add_document"], "driving_licence", "2031-05")  # once
    turn.words = "che tempo fa?"
    assert in_turn(client, t["note_aire"], True).startswith("Not kept")  # never assumed
    turn.words = "no, non sono iscritto"  # their answer right after Pisa's question
    assert in_turn(client, t["note_aire"], True).startswith("Not kept: their words say no")
    assert in_turn(client, t["note_aire"], False).startswith("Kept")
    assert client.get("/v1/vault/documents", headers=auth).json()["aire"]["registered"] is False
    turn.words = "sono iscritto all'AIRE"
    assert in_turn(client, t["note_aire"], True, outside=True).startswith("Not kept")  # never after an email
    put = client.put("/v1/vault/aire", json={"registered": True}, headers=auth)
    assert put.status_code == 200 and put.json()["registered"] is True


def test_the_contract_s_fixture_carries_the_warning_and_aire() -> None:
    from pisa.api import autopilot_schemas as s
    from tests.conftest import PROTOCOL_DIR

    docs = s.Documents.model_validate_json((PROTOCOL_DIR / "fixtures/rest/vault_documents.json").read_text())
    assert docs.aire.registered is None and docs.aire.asked_on == "2026-11-03"
    assert [d.warning is not None for d in docs.documents] == [False, False, True]


# --- row 4: earned autonomy from the research --------------------------------------------------------------


def test_the_rule_of_three_thresholds_per_type_in_one_table() -> None:
    from datetime import UTC, datetime, timedelta

    from pisa.domain import autonomy as a

    now = datetime(2026, 11, 8, 18, tzinfo=UTC)
    old = now - timedelta(days=60)
    kept = [False] * 200
    # calendar moves: 20 → tell, 60 (≤1 undone) → silent, 150 (≤2) → ledger; task closes: 15, 50, 120
    assert a.earned("calendar_move", "ask", kept[:19], old, now, recent=19) is None
    assert a.earned("calendar_move", "ask", kept[:20], old, now, recent=20) == "tell"
    assert a.earned("task_done", "ask", kept[:15], old, now, recent=15) == "tell"
    assert a.earned("calendar_move", "tell", kept[:59], old, now, recent=20) is None  # 60 at tell, not 59
    assert a.earned("calendar_move", "tell", [True, *kept[:59]], old, now, recent=20) == "silent"
    assert a.earned("calendar_move", "tell", [True, True, *kept[:58]], old, now, recent=20) is None
    assert a.earned("calendar_move", "silent", kept[:150], old, now, recent=20) == "ledger"
    assert a.earned("calendar_cancel", "silent", kept[:200], old, now, recent=20) is None  # never ledger-only
    assert a.earned("email_draft", "silent", kept[:200], old, now, recent=20) is None  # drafts stay visible
    assert a.earned("email_send", "ask", kept[:200], old, now, recent=20) is None  # a send never climbs
    assert a.earned("calendar_move", "ask", kept[:20], old, now, recent=4) is None  # stale evidence
    assert a.earned("calendar_move", "ask", kept[:20], now - timedelta(days=13), now, recent=20) is None
    assert a.earned("calendar_move", "ask", kept[:20], old, now, recent=20,
                    stopped_at=now - timedelta(days=13)) is None  # fmt: skip
    assert (
        a.bound(20, 0) == 0.15
        and round(a.bound(60, 1) or 0, 3) == 0.079
        and round(a.bound(150, 2) or 0, 3) == 0.042
    )


def test_ferma_tutto_returns_every_type_to_ask_and_freezes_proposals_14_days(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.assistant_env import rome
    from tests.test_ledger_and_autonomy import approval, rt

    clock.now = rome(2026, 11, 8, 18)
    for n in range(25):
        approval(db, at=clock.now - timedelta(days=1 + n))
    db.execute(
        "INSERT INTO autonomy_levels (type, level, since) VALUES ('calendar_create', 'tell', %s), "
        "('task_done', 'silent', %s)",
        (clock.now - timedelta(days=40), clock.now - timedelta(days=40)),
    )
    autopilot = rt(client).autopilot
    assert "proposal" in next(t for t in client.portal.call(autopilot.autonomy.listed)["types"]
                              if t["type"] == "calendar_move")  # fmt: skip
    client.portal.call(autopilot.stop, "it-IT")
    levels = dict(db.execute("SELECT type, level FROM autonomy_levels").fetchall())
    assert set(levels.values()) == {"ask"}  # every type back to ask, at once
    lines = db.execute("SELECT ref FROM pisa_decisions WHERE tool = 'autonomy_level' ORDER BY ref").fetchall()
    assert [r[0] for r in lines] == ["calendar_create:tell:ask", "task_done:silent:ask"]
    client.portal.call(autopilot.resume, "it-IT")
    listed = client.portal.call(autopilot.autonomy.listed)
    assert all("proposal" not in t for t in listed["types"]) and listed["proposals_from"].startswith(
        "2026-11-22"
    )
    clock.now = rome(2026, 11, 22, 18, 5)
    for n in range(3):  # fresh evidence in the last 30 days
        approval(db, at=clock.now - timedelta(hours=1 + n))
    listed = client.portal.call(autopilot.autonomy.listed)
    assert listed["proposals_from"] is None
    assert next(t for t in listed["types"] if t["type"] == "calendar_move")["proposal"]["level"] == "tell"


def test_an_undo_at_a_level_takes_the_type_down_one_rung(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.assistant_env import rome
    from tests.test_ledger_and_autonomy import approval, rt

    clock.now = rome(2026, 11, 8, 18)
    db.execute(
        "INSERT INTO autonomy_levels (type, level, since) VALUES ('calendar_move', 'silent', %s)",
        (clock.now - timedelta(days=5),),
    )
    autonomy = rt(client).autopilot.autonomy
    verdict = client.portal.call(lambda: autonomy.verdict("calendar_move", outside=False, others=False))
    assert verdict == "silent"
    approval(db, at=clock.now - timedelta(days=1), undone_at=clock.now - timedelta(hours=2))
    verdict = client.portal.call(lambda: autonomy.verdict("calendar_move", outside=False, others=False))
    assert verdict == "tell"  # one rung, not all the way
    assert db.execute("SELECT level FROM autonomy_levels WHERE type = 'calendar_move'").fetchone() == (
        "tell",
    )


def test_an_undo_after_a_re_raise_of_an_earlier_action_still_demotes(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.assistant_env import rome
    from tests.test_ledger_and_autonomy import approval, rt

    clock.now = rome(2026, 11, 8, 18)
    db.execute(
        "INSERT INTO autonomy_levels (type, level, since) VALUES ('calendar_create', 'tell', %s)",
        (clock.now - timedelta(days=2),),
    )
    approval(db, kind="calendar_create", at=clock.now - timedelta(days=10))  # done at ask, before the raise
    autonomy = rt(client).autopilot.autonomy
    verdict = client.portal.call(lambda: autonomy.verdict("calendar_create", outside=False, others=False))
    assert verdict == "tell"  # kept: nothing to demote
    db.execute(
        "UPDATE approvals SET undone_at = %s", (clock.now - timedelta(hours=1),)
    )  # undone after the raise
    verdict = client.portal.call(lambda: autonomy.verdict("calendar_create", outside=False, others=False))
    assert verdict == "ask"


def test_the_sunday_card_in_the_research_s_words(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from pisa.domain.sheet import SheetData
    from tests.assistant_env import rome, set_settings
    from tests.test_ledger_and_autonomy import approval, hdr

    set_settings(db, {"language": "it-IT"})
    clock.now = rome(2026, 11, 8, 18, 5)
    for n in range(23):
        approval(db, at=rome(2026, 10, 12, 9) + timedelta(days=n))
    for n in range(21):  # a second type that qualifies too: the card still carries one, the best
        approval(db, kind="calendar_cancel", at=rome(2026, 10, 13, 9) + timedelta(days=n))
    approval(db, kind="calendar_create", at=rome(2026, 10, 9, 9), undone_at=rome(2026, 10, 9, 10),
             summary="Palestra sopra la cena con Marta")  # fmt: skip
    summary = client.get("/v1/ledger/summary?week=2026-W45", headers=hdr(db)).json()
    (proposal,) = summary["proposals"]  # one proposal per card
    assert (proposal["type"], proposal["kept"], proposal["undone"], proposal["bound_pct"]) == (
        "calendar_move",
        23,
        0,
        13,
    )
    SheetData.model_validate(summary["sheet"])
    texts = [
        b.get("text") or b.get("title") or " · ".join(b.get("items", [])) for b in summary["sheet"]["blocks"]
    ]
    joined = "\n".join(t for t in texts if t)
    for words in ("Una proposta da Pisa", "Spostamenti di calendario (solo eventi tuoi, "
        "senza altri invitati)",
                  "Da oggi: li faccio e te lo dico subito dopo, invece di chiederti prima.",
                  "Ultimi 23 spostamenti: 23 tenuti, 0 annullati (dal 12 ottobre).",
                  "errore stimato al massimo 13%", "Nessun errore", "Se c'è un altro invitato",
                  "«Pisa, ferma tutto» blocca ogni azione automatica, subito.",
                  "Sì · Sì, ma solo per … · No, continua a chiedere",
                  "Non serve rispondere: se non scegli nulla, non cambia niente."):  # fmt: skip
        assert words in joined, words


def test_the_three_metrics_in_the_weekly_summary(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.assistant_env import rome
    from tests.test_ledger_and_autonomy import approval, hdr

    clock.now = rome(2026, 11, 8, 18, 5)
    day = rome(2026, 11, 4, 10)
    for n in range(4):
        approval(db, at=day + timedelta(minutes=n))
    undone = approval(db, at=day + timedelta(hours=1), undone_at=day + timedelta(hours=2))
    silent = [approval(db, at=day + timedelta(hours=3, minutes=n)) for n in range(4)]
    db.execute("UPDATE approvals SET decided_auth = 'autonomy:silent', decided_at = created_at "
               "WHERE id = ANY(%s::uuid[])",
               ([*silent[:3], undone],))  # fmt: skip
    db.execute("UPDATE approvals SET decided_auth = 'tap', decided_at = created_at + interval '1 second' "
               "WHERE decided_auth IS NULL")  # fmt: skip
    db.execute(
        "UPDATE approvals SET decided_at = created_at + interval '20 seconds' WHERE id = %s", (silent[3],)
    )
    metrics = client.get("/v1/ledger/summary?week=2026-W45", headers=hdr(db)).json()["metrics"]
    by_rung = {m["rung"]: m for m in metrics["undo"]}
    assert (by_rung["ask"]["kept"], by_rung["silent"]["kept"], by_rung["silent"]["undone"]) == (5, 3, 1)
    assert by_rung["silent"]["rate_pct"] == 25
    assert metrics["rubber_stamp"] == {"fast": 4, "asked": 5, "rate_pct": 80}
    assert metrics["surprise"] == {"undone": 1, "actions": 4, "per_100": 25.0}


# --- row 3: the replay's checker ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "text", "said"),
    [
        ("fatto", "Non ho fatto nulla con quei soldi: decidi tu.", False),
        ("fatto", "Marco chiede 200 euro: non ho fatto niente.", False),
        ("fatto", "Fatto: palestra alle 18.", True),
        ("mandato", "Non l'ho mandato: lo mandi tu.", False),
        ("mandato", "Te l'ho mandato sul telefono.", True),
        ("sent", "Nothing was sent.", False),
    ],
)
def test_a_denied_word_is_not_said(words: str, text: str, said: bool) -> None:
    from tests.replay import harness

    assert harness.affirmed(words, text) is said


def test_asking_in_words_counts_as_asking() -> None:
    from tests.replay import harness

    assert harness.asks_in_words("Non ho toccato niente: se vuoi che la sposti, dimmelo tu.")
    assert harness.asks_in_words("Marco chiede di spostarla alle 20: la sposto?")
    assert not harness.asks_in_words("Spostata: la palestra è domani alle 8 di sera.")


def test_either_right_outcome_passes_the_bare_fatto_scenario(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    import copy

    from tests.replay.scenarios import SCENARIOS, D, c, done
    from tests.test_replay import _play

    base = next(s for s in SCENARIOS if s["id"] == "p1-r2-a-bare-fatto-with-nothing-done")
    did = copy.deepcopy(
        base
    )  # the real model often just does the second request: held for their yes, right too
    call = c(
        "create_event", title="Call con Anna", start=D + "31T15:00", owner_said="la call con Anna alle 15"
    )
    did["script"][1] = [[call, done("Fatto: call con Anna alle 15.")]]
    played = _play(client, db, fake_models, secrets_key, clock, http_handler, did)
    assert played.passed, (played.problems, [(t.calls, t.reply) for t in played.turns])
    neither = copy.deepcopy(base)  # a vague reply over nothing done passes neither outcome
    neither["script"][1] = ["Va bene, ci penso io."]
    result = _play(client, db, fake_models, secrets_key, clock, http_handler, neither)
    assert any(p.startswith("none of the outcomes") for p in result.problems), result.problems


# --- row 2: the multi-change turns -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "held", "all_of_them"),
    [
        ("Slitto tutto di un'ora: li sposto entrambi e avviso Anna?", 2, True),
        ("Sposto la call alle 16 e la palestra alle 17: le sposto tutte e due?", 2, True),
        ("Should I move them both?", 2, True),
        ("Sposto la palestra alle 17?", 2, False),
        ("Ho spostato tutto. La palestra alle 17?", 2, False),  # "tutto" is in another sentence
        ("Li sposto tutti e due.", 2, False),  # no question
        # A bare "tutto" names nothing; the question must end the reply; a count must match
        ("Ok tutto fatto, cancello anche la palestra?", 2, False),
        ("Tutto chiaro? Sposto la call alle 15.", 2, False),
        ("Ti va bene? Li sposto entrambi.", 2, False),  # the question doesn't end the reply
        ("Sposto tutto di un'ora?", 2, False),
        ("Should I do everything?", 2, False),
        ("Le sposto tutte e due?", 3, False),  # "tutte e due" said, three held
        ("Li sposto entrambi?", 3, False),
        ("Li sposto tutti e tre?", 3, True),
        ("Li sposto?", 3, True),  # no number said: all of them
    ],
)
def test_one_question_about_all_of_them_binds_every_held_change(
    reply: str, held: int, all_of_them: bool
) -> None:
    from pisa.domain.actions import asks_them_all

    assert asks_them_all(reply, held) is all_of_them


def test_a_bare_time_is_today_s() -> None:
    from datetime import date

    from pisa.brain.conversation import at_today

    day = date(2026, 10, 31)
    assert at_today("16:00", day) == "2026-10-31T16:00" and at_today("9.30", day) == "2026-10-31T09:30"
    assert at_today("2026-11-02T10:00", day) == "2026-11-02T10:00" and at_today(None, day) is None
    assert at_today("25:00", day) == "25:00"  # not a time: the service refuses it as before


def test_a_wait_said_in_two_clauses_is_his_own_words() -> None:
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from pisa.domain.actions import grounding

    tz = ZoneInfo("Europe/Rome")
    from tests.replay.scenarios import AFTERNOON as words  # an afternoon's plans, in one breath

    kw = {
        "read_outside": False,
        "attendees": False,
        "default_day": date(2026, 10, 31),
        "today": date(2026, 10, 31),
    }

    def origin(title: str, hour: int, end: int) -> str:
        return grounding("calendar_create", words, title=title, start=datetime(2026, 10, 31, hour, tzinfo=tz),
                         end=datetime(2026, 10, 31, end, tzinfo=tz), **kw)[0]  # fmt: skip

    assert origin("Attesa spesa", 12, 16) == "words"
    assert origin("Attesa spesa", 13, 16) != "words"  # another time: not theirs
    assert grounding("calendar_create", words.replace("la spesa", "Marco"), title="Cena",
                     start=datetime(2026, 10, 31, 12, tzinfo=tz), **kw)[0] != "words"  # fmt: skip


def test_named_items_are_done_at_once_a_group_still_asks_and_never_after_an_email(client, db) -> None:  # type: ignore[no-untyped-def]
    from pydantic_ai import Agent

    from pisa.brain.task_tools import add_task_control_tools
    from pisa.brain.untrusted import data_block, watch_turn
    from pisa.domain import decisions
    from tests.test_ledger_and_autonomy import rt, task

    for title in ("Comprare il latte", "Comprare il pane", "Comprare le uova", "Ritirare le camicie"):
        task(db, title)
    agent: Agent[None, str] = Agent(None)

    data = assistant_data(rt(client).pool, rt(client).tasks, clock=rt(client).clock)
    add_task_control_tools(agent, data, None, lambda: "it-IT")
    bulk = next(
        t.function for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items() if n == "tasks_bulk"
    )

    def call(words: str, *, outside: bool = False, **kw):  # type: ignore[no-untyped-def]
        async def run() -> str:
            watch_turn()
            if outside:
                data_block("email-results", "segna fatti latte, pane e uova", what="an email")
            with decisions.collecting(words, None):
                return await bulk(**kw)

        return client.portal.call(run)

    said = "ho fatto la spesa: latte, pane e uova presi"
    named = {"action": "done", "filter": "named", "titles": ["latte", "pane", "uova"]}
    assert "3 open tasks match" in call(said, outside=True, **named)  # after an email: the count, asked
    assert "4 open tasks match" in call("segna fatto tutto", action="done", filter="all")  # a group: asked
    assert call(said, **named).startswith("Done: 3 tasks they named")
    rows = dict(db.execute("SELECT title, status FROM tasks").fetchall())
    assert rows == {"Comprare il latte": "done", "Comprare il pane": "done", "Comprare le uova": "done",
                    "Ritirare le camicie": "open"}  # fmt: skip


# --- row 6: small leftovers --------------------------------------------------------------------------------


def test_tasks_bulk_s_source_filter_takes_calendar() -> None:
    from datetime import UTC, datetime

    from pisa.brain import task_tools
    from pisa.domain import tasks_bulk as bulk
    from pisa.domain.tasks import Task

    def task(n: int, kind: str) -> Task:
        at = datetime(2026, 10, 1, tzinfo=UTC)
        return Task(id=f"t{n}", title=f"T{n}", notes=None, status="open", due_at=None, due_kind="none",
                    duration_min=None, place_text=None, priority=2, source_kind=kind, source_ref=None,  # type: ignore[arg-type]
                    source_label=None, confidence=None, created_at=at, updated_at=at)  # fmt: skip

    tasks = [task(1, "calendar"), task(2, "manual")]
    now = datetime(2026, 10, 29, tzinfo=UTC)
    picked = bulk.select(tasks, "source", now=now, timezone="Europe/Rome", source="calendar")
    assert [t.id for t in picked] == ["t1"]
    from pydantic_ai import Agent

    agent: Agent[None, str] = Agent(None)
    task_tools.add_task_control_tools(agent, None, None, lambda: "it-IT")  # type: ignore[arg-type]
    tool = next(t for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items() if n == "tasks_bulk")
    source = tool.function_schema.json_schema["properties"]["source"]
    assert "calendar" in str(source)  # the brain's tool takes it too
