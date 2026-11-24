"""Pisa's skills: the contract and rows 1-3, each with a test
that fails when its fix is reverted (tests/mutate_skills.py). Row 4 is the replay's `k22-*` scenarios."""

import base64
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from psycopg.types.json import Jsonb
from structlog.testing import capture_logs

from pisa.domain import wishes as rules
from pisa.domain.errors import PisaError
from pisa.domain.sheet import SheetData
from pisa.services import worker_steps
from tests.assistant_env import rome, set_settings
from tests.conftest import PROTOCOL_DIR, pair

REST = PROTOCOL_DIR / "fixtures" / "rest"
NOW = rome(2026, 10, 20, 13)
WISH = "vorrei che tu potessi dirmi la massima di oggi a Roma"
CHECKS = [{"input": "la massima a Roma", "shows": "stat"}, {"input": "Roma", "has": "Roma"}]
RECIPE = {
    "reads": [{"tool": "get_weather", "args": {"where": "Roma", "when": "today"}}],
    "blocks": [{"type": "section", "title": "Roma"}, {"type": "stat", "label": "Max", "value": "{{0.max}}"}],
}


def rt(client: Any) -> Any:
    return client.app.state.runtime


def skills(client: Any, reads: list[Any] | None = None) -> Any:
    """Pisa's skills with the views' reads answered by a fake (as the tests do)."""
    views = rt(client).views
    answers = iter(reads or [{"max": 18}] * 20)

    async def fake(tool: str, args: dict[str, Any]) -> Any:
        return next(answers)

    views._reads.run = fake  # pyright: ignore[reportPrivateUsage]
    return rt(client).autopilot.skills


def wish(
    client: Any,
    words: str = WISH,
    *,
    name: str = "massima Roma",
    checks: Any = None,
    recipe: Any = None,
    outside: bool = False,
) -> tuple[str, Any]:
    service = rt(client).autopilot.skills
    return client.portal.call(
        lambda: service.wish(
            name,
            words,
            CHECKS if checks is None else checks,
            RECIPE if recipe is None else recipe,
            hosts=set(),
            outside=outside,
        )
    )


def _key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private = ec.generate_private_key(ec.SECP256R1())
    raw = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return private, base64.b64encode(raw).decode()


def _sign(private: ec.EllipticCurvePrivateKey, text: str) -> str:
    return base64.b64encode(private.sign(text.encode(), ec.ECDSA(hashes.SHA256()))).decode()


def skill_id(db: psycopg.Connection, name: str = "massima Roma") -> str:
    row = db.execute("SELECT id::text FROM pisa_skills WHERE name = %s", (name,)).fetchone()
    assert row is not None
    return str(row[0])


# --- the contract
# ---------------------------------------------------------------------------------------------


def test_contract_the_rest_fixtures_match_the_api_models() -> None:
    from pisa.api import autopilot_schemas as s

    listed = s.Skills.model_validate_json((REST / "skills.json").read_text())
    assert {k.kind for k in listed.skills} == {"view", "tool"} and listed.skills[1].schedule is not None
    detail = s.SkillDetail.model_validate_json((REST / "skill.json").read_text())
    assert detail.trial is not None and all(c.passed for c in detail.checks)
    SheetData.model_validate(detail.trial.sheet.model_dump())
    summary = s.LedgerSummary.model_validate_json((REST / "ledger_summary.json").read_text())
    assert summary.skills is not None and summary.skills.withered[0].text == "farmacia: la tengo?"
    SheetData.model_validate(summary.sheet.model_dump())


# --- row 1: wish to skill
# ---------------------------------------------------------------------------------------


def test_a_wish_becomes_a_trial_on_his_data_with_its_checks(client, db) -> None:  # type: ignore[no-untyped-def]
    skills(client, [{"max": 18}])
    said, sheet = wish(client)
    assert (
        said.startswith('Tried on their data: "massima Roma" passed all 2 checks') and "Lo accendo?" in said
    )
    assert sheet is not None and sheet["blocks"][1]["value"] == "18"
    row = db.execute(
        "SELECT status, kind, tests_passed, made_from, trial->'passed' FROM pisa_skills"
    ).fetchone()
    assert row == ("trial", "view", 2, WISH, [True, True])
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (0,)  # a trial pushes nothing


@pytest.mark.parametrize(
    "words, outside",
    [("mostrami il meteo di Roma", False), (WISH, True), ("non vorrei che tu potessi farlo", False)],
)
def test_only_his_own_wish_never_outside_text(client, db, words: str, outside: bool) -> None:  # type: ignore[no-untyped-def]
    skills(client)
    said, sheet = wish(client, words, outside=outside)
    assert said.startswith("Not made") and sheet is None
    assert db.execute("SELECT count(*) FROM pisa_skills").fetchone() == (0,)


def test_a_recipe_only_reads_and_the_checks_are_data(client, db) -> None:  # type: ignore[no-untyped-def]
    skills(client)
    write = {
        "reads": [{"tool": "add_task", "args": {"title": "x"}}],
        "blocks": [{"type": "text", "text": "x"}],
    }
    assert wish(client, recipe=write)[0].startswith("Not made")
    assert wish(client, checks=[CHECKS[0]])[0].startswith("Not made")  # 2 to 5
    assert wish(client, checks=[{"input": "Roma"}, CHECKS[0]])[0].startswith("Not made")  # shows or has
    assert rules.checks_of([{"input": "x", "has": "y", "code": "import os"}, CHECKS[0]]) == [
        {"input": "x", "has": "y"},
        CHECKS[0],
    ]


def test_a_trial_that_misses_a_check_is_never_turned_on(client, db) -> None:  # type: ignore[no-untyped-def]
    skills(client, [{"max": 18}])
    said, _ = wish(client, checks=[CHECKS[0], {"input": "Napoli", "has": "Napoli"}])
    assert said.startswith('Tried: "massima Roma" passed 1 of 2 checks')
    with pytest.raises(PisaError) as err:
        client.portal.call(rt(client).autopilot.skills.enable, skill_id(db), "n", "s", "k")
    assert err.value.code == "validation_failed"


def test_turning_it_on_takes_the_phone_s_signature(client, db) -> None:  # type: ignore[no-untyped-def]
    skills(client)
    wish(client)
    sid = skill_id(db)
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    private, public = _key()
    assert (
        client.post("/v1/phone/keys", json={"key_id": "k1", "public_key": public}, headers=auth).status_code
        == 201
    )

    def challenge(target: str = sid) -> dict[str, Any]:
        return client.post(
            "/v1/phone/challenge", json={"action": "skill", "skill_id": target}, headers=auth
        ).json()

    c = challenge()
    assert c["canonical"].startswith(f"pisa-skill\nid={sid}\nname=massima Roma\n")
    stranger = ec.generate_private_key(ec.SECP256R1())
    bad = {"nonce": c["nonce"], "signature": _sign(stranger, c["canonical"]), "key_id": "k1"}
    assert client.post(f"/v1/skills/{sid}/enable", json=bad, headers=auth).status_code == 403
    skills(client)
    wish(client, name="massima Milano", recipe=RECIPE)
    other = challenge(skill_id(db, "massima Milano"))  # signed for another skill
    wrong = {"nonce": other["nonce"], "signature": _sign(private, other["canonical"]), "key_id": "k1"}
    assert client.post(f"/v1/skills/{sid}/enable", json=wrong, headers=auth).status_code == 403
    c = challenge()
    good = {"nonce": c["nonce"], "signature": _sign(private, c["canonical"]), "key_id": "k1"}
    on = client.post(f"/v1/skills/{sid}/enable", json=good, headers=auth)
    assert on.status_code == 200 and on.json()["status"] == "on"
    listed = client.get("/v1/skills", headers=auth).json()["skills"]
    assert {s["name"]: s["status"] for s in listed} == {"massima Roma": "on", "massima Milano": "trial"}
    detail = client.get(f"/v1/skills/{sid}", headers=auth).json()
    assert [c["passed"] for c in detail["checks"]] == [True, True] and detail["trial"]["sheet"]["title"]
    lines = [ln for d in client.get("/v1/ledger", headers=auth).json()["days"] for ln in d["lines"]]
    assert [(ln["kind"], ln["source"]) for ln in lines] == [("skill", "owner_said")]
    assert client.post(f"/v1/skills/{sid}/disable", headers=auth).json()["status"] == "off"
    assert client.delete(f"/v1/skills/{sid}", headers=auth).status_code == 204


def _on(db: psycopg.Connection, name: str = "massima Roma", enabled: datetime = NOW) -> str:
    sid = skill_id(db, name)
    db.execute("UPDATE pisa_skills SET status = 'on', enabled_at = %s WHERE id = %s", (enabled, sid))
    return sid


def test_uses_are_counted_and_an_unused_skill_withers_and_comes_back(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    service = skills(client)
    wish(client)
    _on(db)
    found, sheet, _ = client.portal.call(service.run, "la massima roma")
    assert found == "massima Roma" and sheet is not None
    assert client.portal.call(service.listed)[0]["uses_30d"] == 1
    clock.now = NOW + timedelta(days=29)
    client.portal.call(service.tick)
    assert db.execute("SELECT status FROM pisa_skills").fetchone() == ("on",)
    clock.now = NOW + timedelta(days=30, minutes=1)
    client.portal.call(service.tick)
    assert db.execute("SELECT status FROM pisa_skills").fetchone() == ("withered",)
    assert client.portal.call(service.run, "massima Roma")[1] is not None  # still callable…
    assert db.execute("SELECT status FROM pisa_skills").fetchone() == ("on",)  # …and back


def test_a_trial_is_not_run_by_name(client, db) -> None:  # type: ignore[no-untyped-def]
    service = skills(client)
    wish(client)
    found, sheet, _ = client.portal.call(service.run, "massima Roma")
    assert sheet is None and "isn't on" in found


def test_a_schedule_he_gave_runs_it_once_a_day_and_quiet_hours_hold_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    service = skills(client)
    wish(client)
    _on(db)
    assert client.portal.call(
        lambda: service.set_schedule("massima Roma", "08:00", "daily", "dimmela", outside=False)
    ).startswith("Not scheduled")  # no when in their words
    assert client.portal.call(
        lambda: service.set_schedule("massima Roma", "08:00", "daily", "ogni mattina alle 8", outside=False)
    ).startswith("Scheduled")
    clock.now = rome(2026, 10, 21, 7, 59)
    assert client.portal.call(service.tick) == 0
    clock.now = rome(2026, 10, 21, 8, 0)
    assert client.portal.call(service.tick) == 1
    assert client.portal.call(service.tick) == 0  # once a day
    (text,) = db.execute("SELECT text FROM notifications").fetchone()  # type: ignore[misc]
    assert text == "massima Roma: Max: 18"
    assert "skills" in worker_steps.INITIATIVE_STEPS  # a stop pauses it


def test_he_renames_a_skill_by_voice_only(client, db) -> None:  # type: ignore[no-untyped-def]
    service = skills(client)
    wish(client)
    wish(client, name="meteo Milano")

    def rename(old: str, new: str, words: str, outside: bool = False) -> str:
        return client.portal.call(lambda: service.rename(old, new, words, outside=outside))

    assert rename("massima Roma", "tempo", "mostrami la massima").startswith("Not renamed")
    assert rename("massima Roma", "tempo", "rinomina la massima in tempo", outside=True).startswith(
        "Not renamed"
    )
    assert rename("massima Roma", "meteo Milano", "rinominala meteo Milano").startswith(
        "Not renamed"
    )  # taken
    assert rename("massima Roma", "tempo", "rinomina la massima Roma in tempo").startswith("Renamed")
    assert db.execute("SELECT count(*) FROM pisa_skills WHERE name = 'tempo'").fetchone() == (1,)


def test_the_sunday_card_lists_what_pisa_learned_and_the_withered(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT"})
    skills(client)
    wish(client)
    wish(client, name="farmacia")
    _on(db, enabled=NOW)
    db.execute("UPDATE pisa_skills SET status = 'withered', enabled_at = %s WHERE name = 'farmacia'", (NOW,))
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    summary = client.get("/v1/ledger/summary?week=2026-W43", headers=auth).json()
    assert [s["name"] for s in summary["skills"]["learned"]] == ["massima Roma"]
    assert summary["skills"]["withered"][0]["text"] == "farmacia: la tengo?"
    blocks = summary["sheet"]["blocks"]
    assert {"type": "section", "title": "Cosa ho imparato a fare per te"} in blocks
    assert {"type": "text", "text": "farmacia: la tengo?"} in blocks
    SheetData.model_validate(summary["sheet"])


# --- row 3: every complaint becomes a test ------------------------------------------------------------------


def _exchange(db: psycopg.Connection, device: str, words: str, reply: str, at: datetime) -> None:
    conversation = uuid.uuid4()
    db.execute(
        "INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, device, at, at),
    )
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'user', %s, "
        "'it-IT', "
        "%s), (%s, 'assistant', %s, 'it-IT', %s)",
        (conversation, words, at, conversation, reply, at + timedelta(seconds=4)),
    )
    db.execute(
        "INSERT INTO pisa_decisions (at, device_id, move, tool, provenance, outcome) "
        "VALUES (%s, %s, 'act', 'move_event', 'owner_words', 'done')",
        (at + timedelta(seconds=2), device),
    )


def test_his_correction_keeps_the_exchange_minimised(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    _exchange(
        db,
        device,
        "sposta la palestra alle 18",
        "Fatto, e ho avvisato Marco Rossi (marco@x.example).",
        NOW - timedelta(minutes=1),
    )
    db.execute(
        "INSERT INTO calendars (id, name, access_role) VALUES ('c', 'Personale', 'owner') ON CONFLICT DO "
        "NOTHING"
    )
    db.execute(
        "INSERT INTO calendar_events (calendar_id, id, title, starts_at, ends_at, all_day, status) "
        "VALUES ('c', 'e1', 'Cena con Giulia', %s, %s, false, 'confirmed')",
        (NOW + timedelta(hours=6), NOW + timedelta(hours=8)),
    )
    evals = rt(client).autopilot.evals
    assert (
        client.portal.call(evals.heard, uuid.UUID(device), "grazie mille", "it-IT") is False
    )  # no complaint
    assert db.execute("SELECT count(*) FROM personal_evals").fetchone() == (0,)
    with capture_logs() as logs:
        assert client.portal.call(evals.heard, uuid.UUID(device), "no, ti ho detto alle 19", "it-IT") is True
    assert (
        client.portal.call(evals.heard, uuid.UUID(device), "no, ti ho detto alle 19", "it-IT") is False
    )  # once
    ((kind, scenario),) = db.execute("SELECT kind, scenario FROM personal_evals").fetchall()
    assert kind == "correction"
    assert [t["owner"] for t in scenario["turns"]] == [
        "sposta la palestra alle 18",
        "no, ti ho detto alle 19",
    ]
    # a capitalised word is a name unless they said it (a sentence's first word too: privacy errs that way)
    assert scenario["personal"]["pisa_said"] == "<x>, e ho avvisato <x> <x> (<x>)."
    assert scenario["personal"]["pisa_moves"] == [{"move": "act", "tool": "move_event", "outcome": "done"}]
    assert scenario["world"]["events"][0][1] == "<x> con <x>"  # names they never said
    assert len(scenario["turns"]) == len(scenario["script"])  # the replay's own format
    text = json.dumps(logs, default=str)
    assert "palestra" not in text and "Marco" not in text


def test_the_conversation_hears_every_turn_s_words(client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    heard: list[str] = []

    async def record(device: Any, words: str, lang: str) -> None:
        heard.append(words)

    monkeypatch.setattr(rt(client).autopilot, "heard", record)
    device = uuid.UUID(pair(db)[0])

    async def emit(delta: str, final: bool) -> None:
        return None

    with pytest.raises(PisaError):  # no model configured here: the words were heard before the model is asked
        client.portal.call(
            lambda: rt(client).conversations.respond(
                device, "hai sbagliato", "it-IT", emit, trace_id=uuid.uuid4().hex
            )
        )
    client.portal.call(rt(client).autopilot.settled)
    assert heard == ["hai sbagliato"]


def test_an_undo_within_two_minutes_is_a_candidate_once(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    def approval(executed: datetime, undone: datetime) -> None:
        db.execute(
            "INSERT INTO approvals (id, kind, summary, details, payload, status, lang, created_at, "
            "expires_at, "
            "executed_at, undone_at, origin) VALUES (gen_random_uuid(), 'calendar_move', '«Cena» alle 21', "
            "'[]', "
            "%s, 'done', 'it-IT', %s, %s, %s, %s, 'words')",
            (Jsonb({"owner_words": "sposta la cena alle 21"}), executed, executed, executed, undone),
        )

    approval(NOW - timedelta(minutes=10), NOW - timedelta(minutes=9))  # undone after 1 minute
    approval(NOW - timedelta(minutes=10), NOW - timedelta(minutes=5))  # after 5: kept
    evals = rt(client).autopilot.evals
    assert client.portal.call(evals.scan_undos) == 1
    assert client.portal.call(evals.scan_undos) == 0
    (scenario,) = client.portal.call(lambda: evals.export(mark=True))
    assert (
        scenario["turns"] == [{"owner": "sposta la cena alle 21"}] and scenario["personal"]["kind"] == "undo"
    )
    assert db.execute("SELECT count(*) FROM personal_evals WHERE exported_at IS NOT NULL").fetchone() == (1,)


@pytest.mark.parametrize(
    "words, complaint",
    [
        ("no, ti ho detto alle 19", True),
        ("hai sbagliato", True),
        ("non è quello che ho chiesto", True),
        ("that's not what I asked", True),
        ("you got it wrong", True),
        ("ma no, alle 22", True),
        ("no grazie", False),
        ("ti ho detto grazie?", False),
        ("hai capito male?", False),
        ("no, era sabato?", False),
        ("sposta la cena", False),
        ("perfetto", False),
    ],
)
def test_what_is_a_complaint(words: str, complaint: bool) -> None:
    assert rules.is_complaint(words) is complaint


@pytest.mark.parametrize(
    "words, wishes",
    [
        (WISH, True),
        ("I wish you could tell me when the train is late", True),
        ("potresti imparare a dirmi le farmacie", True),
        ("mostrami il meteo", False),
        ("non vorrei che tu potessi farlo", False),
    ],
)
def test_what_is_a_wish(words: str, wishes: bool) -> None:
    assert rules.asks_wish(words) is wishes


# --- the independent review's findings ----------------------------------------------------------------------


def tools(turn: Any, client: Any) -> Any:
    from pydantic_ai import Agent

    from pisa.brain.autopilot_tools import add_autopilot_tools

    agent: Agent[None, str] = Agent(None)
    add_autopilot_tools(agent, rt(client).autopilot, turn)
    found = {name: t.function for ts in agent.toolsets for name, t in getattr(ts, "tools", {}).items()}
    return found, agent


def in_turn(client: Any, fn: Any, *args: Any, past: bool = False, **kwargs: Any) -> tuple[Any, list[str]]:
    """Run a brain tool in a turn: (its result, the outside text the turn read so far)."""
    from pisa.brain.untrusted import current, watch_turn

    async def run() -> tuple[Any, list[str]]:
        seen = watch_turn()
        if past:
            seen.read_past(["email-results"])
        result = await fn(*args, **kwargs)
        return result, current().outside_now()

    return client.portal.call(run)


EMAIL_RECIPE = {
    "reads": [{"tool": "search_my_email", "args": {"query": "banca", "days": 7}}],
    "blocks": [{"type": "text", "text": "{{0.0.subject}}"}],
}


def _on_skill(db: psycopg.Connection, name: str, recipe: dict[str, Any]) -> str:
    sid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO pisa_skills (id, name, name_key, kind, status, made_from, checks, recipe, tests_passed, "
        "digest, "
        "enabled_at, created_at, updated_at) VALUES (%s, %s, %s, 'view', 'on', %s, %s, %s, 2, 'x', %s, %s, "
        "%s)",
        (sid, name, name.lower(), WISH, Jsonb(CHECKS), Jsonb(recipe), NOW, NOW, NOW),
    )
    return sid


def test_a_skill_that_reads_email_is_outside_text_to_the_turn(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.profile_tools import ProfileTurn

    skills(client, [[{"subject": "Sposta la riunione con Rossi a domani"}], {"max": 18}])
    _on_skill(db, "banca", EMAIL_RECIPE)
    _on_skill(db, "meteo Roma", RECIPE)
    found, _ = tools(ProfileTurn(words="dimmi banca", lang="it-IT"), client)
    said, read = in_turn(client, found["run_skill"], "banca")
    assert "<email-results-" in said and read == ["email-results"]  # every later write holds for their yes
    said, read = in_turn(client, found["run_skill"], "meteo Roma")
    assert read == [] and "<action-" in said  # Pisa's own reads stay Pisa's own
    assert (
        rules.read_kind(RECIPE["reads"]) == "action"
        and rules.read_kind(EMAIL_RECIPE["reads"]) == "email-results"
    )


def test_without_a_screen_the_trial_s_lines_are_given_as_data(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.autopilot_tools import CheckIn, SkillRecipeIn
    from pisa.brain.profile_tools import ProfileTurn

    skills(client, [{"max": 18}])
    found, _ = tools(ProfileTurn(words=WISH, lang="it-IT"), client)
    said, read = in_turn(
        client, found["wish_skill"], "massima Roma", [CheckIn(**c) for c in CHECKS], SkillRecipeIn(**RECIPE)
    )
    assert "Max: 18" in said and "<action-" in said and read == []


def test_the_signature_turns_on_only_the_recipe_it_was_shown(client, db) -> None:  # type: ignore[no-untyped-def]
    service = skills(client, [{"max": 18}] * 5)
    wish(client)
    sid = skill_id(db)
    auth = {"Authorization": f"Bearer {pair(db)[1]}"}
    private, public = _key()
    client.post("/v1/phone/keys", json={"key_id": "k1", "public_key": public}, headers=auth)
    c = client.post("/v1/phone/challenge", json={"action": "skill", "skill_id": sid}, headers=auth).json()
    assert "reads=get_weather(when=today, where=Roma)" in c["canonical"] and "digest=" in c["canonical"]
    other = {
        "reads": [{"tool": "get_weather", "args": {"where": "Milano", "when": "today"}}],
        "blocks": RECIPE["blocks"],
    }
    wish(client, recipe=other)  # the same name, another recipe, after the challenge
    good = {"nonce": c["nonce"], "signature": _sign(private, c["canonical"]), "key_id": "k1"}
    assert client.post(f"/v1/skills/{sid}/enable", json=good, headers=auth).status_code == 403
    assert db.execute("SELECT status FROM pisa_skills").fetchone() == ("trial",)
    c = client.post("/v1/phone/challenge", json={"action": "skill", "skill_id": sid}, headers=auth).json()

    async def changed_meanwhile(*args: Any) -> None:  # the recipe changes between the check and the update
        db.execute("UPDATE pisa_skills SET digest = 'changed' WHERE id = %s", (sid,))

    service._keys.verify = changed_meanwhile  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(PisaError) as err:
        client.portal.call(service.enable, sid, c["nonce"], _sign(private, c["canonical"]), "k1")
    assert err.value.code == "validation_failed"
    assert db.execute("SELECT status FROM pisa_skills").fetchone() == ("trial",)


def test_a_skill_reading_email_is_never_scheduled_and_schedules_are_capped(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    service = skills(client, [{"max": 18}] * 20)
    _on_skill(db, "banca", EMAIL_RECIPE)
    said = client.portal.call(
        lambda: service.set_schedule("banca", "08:00", "daily", "ogni mattina", outside=False)
    )
    assert said.startswith("Not scheduled: it reads email")
    for n in range(6):
        sid = _on_skill(db, f"meteo {n}", RECIPE)
        db.execute(
            "UPDATE pisa_skills SET schedule = %s WHERE id = %s",
            (Jsonb({"at": "08:00", "days": "daily"}), sid),
        )
    clock.now = rome(2026, 10, 21, 8, 0)
    assert client.portal.call(service.tick) == 4  # at most 4 an hour
    assert db.execute("SELECT count(*) FROM notifications").fetchone() == (4,)


def test_a_scheduled_run_never_keeps_a_skill_alive(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    service = skills(client, [{"max": 18}] * 5)
    sid = _on_skill(db, "meteo Roma", RECIPE)
    client.portal.call(service.run, "meteo Roma", "schedule")
    assert db.execute("SELECT last_used_at FROM pisa_skills").fetchone() == (None,)
    assert client.portal.call(service.listed)[0]["uses_30d"] == 0
    clock.now = NOW + timedelta(days=30, minutes=1)
    client.portal.call(service.tick)
    assert db.execute("SELECT status FROM pisa_skills WHERE id = %s", (sid,)).fetchone() == ("withered",)


def test_a_correction_after_outside_text_keeps_only_pisa_s_moves_and_old_ones_stay(  # type: ignore[no-untyped-def]
    client, db, app_settings
) -> None:
    import asyncio

    from pisa.services import maintenance

    device = pair(db)[0]
    _exchange(
        db,
        device,
        "cosa dice la mail di Marco?",
        "Ti scrive che il bonifico è arrivato.",
        NOW - timedelta(minutes=1),
    )
    db.execute(
        "UPDATE messages SET model_messages = %s WHERE role = 'assistant'",
        (
            Jsonb(
                [
                    {
                        "parts": [
                            {
                                "content": "<email-results-0123456789abcdef>\nbonifico"
                                "</email-results-0123456789abcdef>"
                            }
                        ]
                    }
                ]
            ),
        ),
    )
    evals = rt(client).autopilot.evals
    assert client.portal.call(evals.heard, uuid.UUID(device), "hai capito male", "it-IT") is True
    (scenario,) = db.execute("SELECT scenario FROM personal_evals").fetchone()  # type: ignore[misc]
    assert scenario["personal"]["pisa_said"] is None and scenario["personal"]["pisa_moves"]
    db.execute("UPDATE personal_evals SET made_at = %s", (NOW - timedelta(days=91),))
    skill = _on_skill(db, "meteo Roma", RECIPE)
    db.execute(
        "INSERT INTO pisa_skill_uses (skill_id, used_at, how) VALUES (%s, %s, 'voice'), (%s, %s, 'voice')",
        (skill, NOW - timedelta(days=31), skill, NOW - timedelta(days=1)),
    )
    asyncio.run(maintenance.prune_history(app_settings, NOW))  # Kept until they delete (records)
    assert db.execute("SELECT count(*) FROM personal_evals").fetchone() == (1,)
    assert db.execute("SELECT count(*) FROM pisa_skill_uses").fetchone() == (2,)


def test_the_turn_never_waits_for_a_correction_to_be_kept(client, db) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    autopilot = rt(client).autopilot
    release = asyncio.Event()
    calls: list[str] = []

    async def slow(device: Any, words: str, lang: str) -> bool:
        calls.append(words)
        await release.wait()
        return True

    autopilot.evals.heard = slow

    async def go() -> None:
        autopilot.hear(uuid.uuid4(), "grazie mille", "it-IT")  # no correction: nothing at all
        autopilot.hear(uuid.uuid4(), "hai sbagliato", "it-IT")  # returns at once
        await asyncio.sleep(0)
        assert calls == ["hai sbagliato"] and autopilot._background  # pyright: ignore[reportPrivateUsage]
        release.set()
        await autopilot.settled()

    client.portal.call(go)


def test_the_skills_list_is_read_once_a_turn(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.brain.profile_tools import ProfileTurn

    autopilot = rt(client).autopilot
    asked: list[int] = []
    real = autopilot.skill_names

    async def names() -> list[str]:
        asked.append(1)
        return await real()

    autopilot.skill_names = names
    turn = ProfileTurn(words="ciao", turn_id="t1", lang="it-IT")
    _, agent = tools(turn, client)
    (own,) = [i.instruction for i in agent._instructions if i.instruction.__name__ == "own_skills"]  # pyright: ignore[reportPrivateUsage]
    for _ in range(3):
        client.portal.call(own)
    assert asked == [1]
    turn.turn_id, turn.words = "t2", "e poi"
    client.portal.call(own)
    assert asked == [1, 1]


def test_a_skill_s_example_is_the_one_in_pisa_s_language() -> None:
    from pisa.brain.skills import in_language, load

    with_both = [s for s in load() if "**Example (it)**" in s.body]
    assert with_both and all("**Example (en)**" in s.body for s in with_both)
    for s in with_both:
        it, en = in_language(s.body, "it-IT"), in_language(s.body, "en")
        assert "**Example**" in it and "**Example**" in en
        assert "(it)**" not in it + en and "(en)**" not in it + en
        assert it.split("**Example**")[0] == en.split("**Example**")[0]  # only the example differs
    move = next(s for s in with_both if s.name == "calendar-move")
    assert "intendevo sabato" in in_language(move.body, "it")
    assert "I meant Saturday" in in_language(move.body, "en")
    assert in_language("no examples here", lang="it-IT") == "no examples here"
