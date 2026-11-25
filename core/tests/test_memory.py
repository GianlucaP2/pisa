# ruff: noqa: E501
"""Pisa learns them from what they say, and uses it when it
matters. One test (or a few) per item of the brief; the HIGH and MED rows fail when their part is reverted (the
review request's table). Synthetic conversations only: every word, name and place here is invented."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from pisa.brain.conversation import profile_instruction
from pisa.db.pool import open_pool
from pisa.domain import memory as rules
from pisa.domain.profile import CORE_MAX, Claim
from pisa.services.profile import ProfileService
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.test_learning import at, card, claims, learning, place

ROME = ZoneInfo("Europe/Rome")
NIGHT = date(2026, 11, 12)  # the night that closes Wednesday the 11th


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ROME)


@pytest.fixture
def auth(db: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


def P(kind: str, subject: str, value: str, quote: str) -> rules.Proposal:
    return rules.Proposal(kind, subject, value, quote).clean()


# --- 1. what the night may keep (code's check, never the model's word) -------------------------------------

WORDS = (
    "stasera voglio sentire house, mi piace tanto la tech house\n"
    "a colazione mi piace stare seduto, con calma\n"
    "sono vegetariano da anni\n"
    "a mia moglie piace il jazz\n"
    "trovami un bar qui vicino"
)


def test_1_a_claim_needs_a_subject_a_value_and_his_own_words() -> None:
    good = P("like", "Musica", "house, in particolare tech house", "mi piace tanto la tech house")
    assert rules.why_not(good, WORDS) is None
    assert good.statement == "musica: house, in particolare tech house"
    assert (
        rules.why_not(
            P("like", "colazione", "seduto, con calma", "a colazione mi piace stare seduto, con calma"), WORDS
        )
        is None
    )
    assert (
        rules.why_not(P("like", "", "tech house", "mi piace tanto la tech house"), WORDS)
        == "no subject or value"
    )
    assert (
        rules.why_not(P("like", "musica", "", "mi piace tanto la tech house"), WORDS) == "no subject or value"
    )
    assert rules.why_not(P("like", "musica", "techno", "adoro la techno"), WORDS) == "quote not their words"
    assert rules.why_not(P("like", "musica", "tech house", "house"), WORDS) == "quote too short"
    assert (
        rules.why_not(P("like", "serate", "jazz", "mi piace tanto la tech house"), WORDS)
        == "value not in the quote"
    )
    assert rules.why_not(P("goal", "musica", "tech house", "mi piace tanto la tech house"), WORDS) == "kind"


def test_1_never_sensitive_never_other_people() -> None:
    assert rules.why_not(P("like", "cibo", "vegetariano", "sono vegetariano da anni"), WORDS) == "sensitive"
    assert rules.why_not(P("like", "musica", "jazz", "a mia moglie piace il jazz"), WORDS) == "other people"
    boss = "il mio capo ascolta solo jazz"
    assert (
        rules.why_not(P("like", "musica", "jazz", boss), boss) == "other people"
    )  # a relation as a whole word
    words = "con Teodora ascolto sempre il jazz"
    quote = P("like", "musica", "jazz", "con Teodora ascolto sempre il jazz")
    assert rules.why_not(quote, words) is None
    assert (
        rules.why_not(quote, words, ["Teodora Lume"]) == "other people"
    )  # one of their people, by first name


def test_1_the_same_claim_again_is_the_same_claim() -> None:
    p = P("like", "musica", "tech house", "mi piace tanto la tech house")
    key = rules.subject_key(p)
    assert rules.same(p, "like", "musica: tech house e house", key)
    assert not rules.same(
        p, "like", "colazione: seduto, con calma", rules.subject_key(P("like", "colazione", "seduto", "x y"))
    )
    assert not rules.same(p, "dislike", "musica: tech house", key)  # a no is never a yes
    assert rules.same(p, "like", "Gli piace la tech house", None)  # a sentence they said, no subject


# --- 2/3. the profile in a turn, by relevance; past picks ---------------------------------------------------

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def claim(n: int, kind: str, statement: str, *, state: str = "active", key: str | None = None, hard: bool = False,
          conf: float = 0.8) -> Claim:  # fmt: skip
    return Claim(str(n), kind, statement, {}, "confirmed", conf, False, hard, NOW, key, state)


PROFILE = [
    claim(1, "constraint", "Mai catene", hard=True),
    claim(2, "style", "Risposte brevi"),
    claim(3, "like", "musica: house, in particolare tech house", key="words:like:musica:1"),
    claim(4, "routine", "colazione: seduto, con calma", key="words:routine:colazione:2"),
    claim(5, "mobility", "A piedi fa di solito fino a circa 15 minuti", key="mobility"),
    claim(6, "like", "viaggi: treno più che aereo", key="words:like:viaggi:3"),
    claim(7, "like", "musica: vinili anni 90", state="candidate", key="words:like:musica:4", conf=0.33),
    claim(8, "like", "pizza: napoletana, bordo alto", key="words:like:pizza:5"),
    *[claim(10 + n, "like", f"lavoro: progetto numero {n}", key=f"words:like:lavoro:{n}") for n in range(30)],
]


def lines(words: str, when: str) -> list[str]:
    return [rules.line(c) for c in rules.relevant(PROFILE, words, local(when))]


def test_3_music_for_his_music_question_breakfast_for_breakfast() -> None:
    music = "\n".join(lines("che musica mi piace e chi vado a sentire?", "2026-11-10T19:00"))
    assert "Mai catene" in music and "Risposte brevi" in music  # hard limits and style: always
    assert "tech house" in music  # review M4: their unconfirmed hunches never reach a turn
    assert "vinili anni 90" not in music and "unconfirmed" not in music
    assert "colazione" not in music and "treno" not in music and "lavoro" not in music
    breakfast = "\n".join(lines("il solito posto per colazione?", "2026-11-10T08:30"))
    assert "colazione: seduto" in breakfast and "A piedi" in breakfast  # a pick takes their walking limit
    assert "musica" not in breakfast and "pizza" not in breakfast


def test_3_a_turn_on_nothing_he_told_carries_only_rules_and_never_more_than_today() -> None:
    plain = lines("che ore sono?", "2026-11-10T11:00")
    assert len(plain) == 2 and "Mai catene" in plain[0]
    work = lines("com'è andato il lavoro sul progetto?", "2026-11-10T11:00")
    assert len(work) == CORE_MAX  # at most today's 25
    assert len(rules.relevant(PROFILE, "lavoro progetto", local("2026-11-10T11:00"), room=20)) == 20


def test_3_a_place_asked_with_no_kind_is_the_hour_s_kind() -> None:
    assert "cafe" in rules.pick_kind("il solito posto?", local("2026-11-10T08:30"))
    evening = rules.pick_kind("come l'altra volta", local("2026-11-10T20:30"))
    assert "pizza" in evening and "cafe" not in evening
    assert rules.pick_kind("che ore sono?", local("2026-11-10T20:30")) == ()


TODAY_FIXED = 681  # profile_instruction's text around its lines (682 for one line "x"; 679 before "they")


def test_3_the_turn_s_block_is_no_longer_than_today_s_at_its_largest() -> None:
    biggest = ["x" * 200] * CORE_MAX
    assert len(profile_instruction(biggest)) <= TODAY_FIXED + len("\n".join(biggest))


# --- the DB: the night's pass, the learner, the migration, the turn ---------------------------------------


def _svc(client: Any) -> Any:
    return client.app.state.runtime.memory


def _call(client: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.portal.call(partial(fn, *args, **kwargs))


def _talk(db: Any, said: list[tuple[datetime, str]], replies: bool = True) -> uuid.UUID:
    row = db.execute("SELECT id FROM devices").fetchone()
    device = row[0] if row else uuid.UUID(pair(db, "iPhone")[0])
    cid = uuid.uuid4()
    db.execute("INSERT INTO conversations (id, device_id, started_at, last_message_at) VALUES (%s, %s, %s, %s)",
               (cid, device, said[0][0], said[-1][0]))  # fmt: skip
    for when, words in said:
        db.execute("INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'user', %s, 'it-IT', %s)",
                   (cid, words, when))  # fmt: skip
        if replies:
            db.execute("INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'assistant', %s, 'it-IT', %s)",
                       (cid, "Ti piace anche la techno dura, vero?", when))  # fmt: skip
    return cid


class _Extract:
    """The model, faked: what it proposes, what it read, and its ledger row (the cost a real call would have)."""

    def __init__(self, db: Any, proposals: list[rules.Proposal], cost: str = "0.0004") -> None:
        self.db, self.proposals, self.cost = db, proposals, cost
        self.read: list[str] = []

    async def __call__(self, words: str, lang: str, *, trace: str) -> list[rules.Proposal]:
        self.read.append(words)
        self.db.execute("INSERT INTO llm_calls (id, slot, model_ref, status, trace_id, reserved_eur, cost_eur, price_known, budget) VALUES "
                        "(gen_random_uuid(), 'chat.conversation', 'test:x', 'settled', %s, %s, %s, true, 'night')",
                        (trace, self.cost, self.cost))  # fmt: skip
        return list(self.proposals)


def _at_night(clock: Any) -> None:
    clock.now = datetime(NIGHT.year, NIGHT.month, NIGHT.day, 3, 30, tzinfo=ROME).astimezone(UTC)


def _claims(db: Any) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT kind, statement, state, provenance, support, subject_key FROM profile_claims ORDER BY created_at"
    ).fetchall()


MUSIC = rules.Proposal("like", "musica", "house, in particolare tech house", "mi piace tanto la tech house")


def test_1_the_night_reads_his_words_once_and_keeps_candidates_from_them(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _talk(db, [(local("2026-11-11T19:00"), "stasera voglio sentire house, mi piace tanto la tech house"),
               (local("2026-11-11T19:02"), "a mia moglie piace il jazz")])  # fmt: skip
    fake = _Extract(db, [MUSIC, rules.Proposal("like", "musica", "jazz", "a mia moglie piace il jazz"),
                         rules.Proposal("like", "musica", "techno dura", "Ti piace anche la techno dura")])  # fmt: skip
    _svc(client)._extract = fake
    _at_night(clock)
    assert _call(client, _svc(client).extract, NIGHT) == 1
    assert "Ti piace" not in fake.read[0]  # never Pisa's replies
    rows = _claims(db)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("like", "musica: house, in particolare tech house", "candidate", "inferred")
    ]
    evidence = db.execute("SELECT signal, summary FROM profile_evidence").fetchall()
    assert evidence == [("extracted", "«mi piace tanto la tech house»")]
    assert _call(client, _svc(client).extract, NIGHT) == 0 and len(fake.read) == 1  # read once
    count = _call(client, _svc(client).night, NIGHT)
    assert count["conversations"] == 1 and count["stored"] == 1 and not count["capped"]


def test_1_a_claim_he_already_has_is_reinforced_not_duplicated(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _talk(db, [(local("2026-11-10T19:00"), "mi piace tanto la tech house")])
    _talk(db, [(local("2026-11-11T21:00"), "ancora tech house stasera: mi piace tanto la tech house")])
    _svc(client)._extract = _Extract(db, [MUSIC])
    _at_night(clock)
    _call(client, _svc(client).extract, NIGHT)
    rows = _claims(db)
    assert len(rows) == 1 and rows[0][4] == 2.0  # one claim, two of their quotes
    assert _call(client, _svc(client).night, NIGHT)["reinforced"] == 1


def test_1_a_claim_he_said_wrong_never_comes_back_from_the_night(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _talk(db, [(local("2026-11-11T19:00"), "mi piace tanto la tech house")])
    _svc(client)._extract = _Extract(db, [MUSIC])
    db.execute("INSERT INTO profile_claims (id, kind, statement, subject, provenance, state, retired_by, created_at, updated_at, subject_key) "
               "VALUES (gen_random_uuid(), 'like', 'musica: tech house', '{}', 'inferred', 'retired', 'owner', now(), now(), %s)",
               (rules.subject_key(MUSIC.clean()),))  # fmt: skip
    _at_night(clock)
    assert _call(client, _svc(client).extract, NIGHT) == 0
    assert [(r[2], r[4]) for r in _claims(db)] == [("retired", 0.0)]  # not even reinforced
    assert db.execute("SELECT count(*) FROM profile_evidence").fetchone() == (0,)


def test_1_the_night_stops_at_its_five_cents_and_says_so(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    for hour in (9, 12, 18):
        _talk(db, [(local(f"2026-11-11T{hour:02d}:00"), "mi piace tanto la tech house")])
    fake = _Extract(db, [MUSIC])
    _svc(client)._extract = fake
    db.execute("INSERT INTO llm_calls (id, slot, model_ref, status, trace_id, reserved_eur, cost_eur, price_known, budget) VALUES "
               "(gen_random_uuid(), 'chat.conversation', 'test:x', 'settled', %s, 0.049, 0.049, true, 'night')",
               (f"memory-{NIGHT.isoformat()}-0",))  # fmt: skip
    _at_night(clock)
    _call(client, _svc(client).extract, NIGHT)
    assert fake.read == []  # €0.049 + one call's €0.002 is past €0.05: no call at all
    count = _call(client, _svc(client).night, NIGHT)
    assert count["capped"] and count["waiting"] == 3 and Decimal(count["spent_eur"]) == Decimal("0.049")


def test_1_a_call_dearer_than_half_a_cent_stops_the_night(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    for hour in (9, 12):
        _talk(db, [(local(f"2026-11-11T{hour:02d}:00"), "mi piace tanto la tech house")])
    fake = _Extract(db, [MUSIC], cost="0.006")
    _svc(client)._extract = fake
    _at_night(clock)
    _call(client, _svc(client).extract, NIGHT)
    assert len(fake.read) == 1
    count = _call(client, _svc(client).night, NIGHT)
    assert count["capped"] and count["waiting"] == 1


def test_1_a_synthetic_day_costs_well_under_the_cap(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """The handoff's number: a synthetic day of 8 conversations, each call priced at what the conversation
    slot's tier charges for its words (≈ 1,500 tokens in, ≤ 600 out at Haiku 5.5's $0.10 / $0.50 per M,
    ≈ $0.00045, booked €0.0004)."""
    for n in range(8):
        _talk(
            db,
            [
                (
                    local(f"2026-11-11T{9 + n:02d}:00"),
                    "mi piace tanto la tech house " + "e poi parliamo d'altro " * 60,
                )
            ],
        )
    _svc(client)._extract = _Extract(db, [MUSIC])
    _at_night(clock)
    _call(client, _svc(client).extract, NIGHT)
    count = _call(client, _svc(client).night, NIGHT)
    assert count["conversations"] == 8 and not count["capped"]
    assert Decimal(count["spent_eur"]) == Decimal("0.0032")  # 8 x €0.0004 ≤ €0.05


def test_1_nothing_is_read_while_learning_is_paused(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _talk(db, [(local("2026-11-11T19:00"), "mi piace tanto la tech house")])
    fake = _Extract(db, [MUSIC])
    _svc(client)._extract = fake
    db.execute("INSERT INTO app_settings (key, value) VALUES ('profile_paused_until', %s)",
               (Jsonb((datetime(2026, 12, 1, tzinfo=UTC)).isoformat()),))  # fmt: skip
    _at_night(clock)
    assert _call(client, _svc(client).extract, NIGHT) == 0 and fake.read == []


# --- 2. the learner: a card shown or tapped is never a like ---------------------------------------------------


def test_2_a_tap_is_interest_a_go_or_a_visit_is_a_like(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    tapped, gone = place(db, "Osteria Inventata", (45.47, 9.19)), place(db, "Forno Finto", (45.46, 9.18))
    day = date(2026, 10, 19)
    for n in range(6):
        card(db, at(day + timedelta(days=n), 12), [tapped, gone], "plan", tapped, device)
        card(db, at(day + timedelta(days=n), 13), [gone, tapped], "go", gone, device)
    learning(db, at(date(2026, 10, 26), 3, 30), lambda s: s.nightly())
    learned = claims(db)
    assert f"place:{tapped}" not in learned  # six taps: interest only
    assert learned[f"place:{gone}"]["state"] in ("active", "candidate")
    signals = dict(db.execute("SELECT subject_key, signal FROM profile_evidence").fetchall())
    assert signals[f"place:{tapped}"] == "interest" and signals[f"place:{gone}"] == "S3"


# --- 3. the turn: by relevance, past picks for the kind of place asked ----------------------------------------


def _observed(db: Any, statement: str, key: str, value: dict[str, Any] | None = None) -> uuid.UUID:
    cid = uuid.uuid4()
    db.execute("INSERT INTO profile_claims (id, kind, statement, subject, provenance, support, confidence, state, created_at, updated_at, subject_key, value) "
               "VALUES (%s, 'like', %s, '{}', 'observed', 3, 0.6, 'active', now(), now(), %s, %s)",
               (cid, statement, key, Jsonb(value) if value else None))  # fmt: skip
    return cid


def _evidence(db: Any, key: str, signal: str, ref: str) -> None:
    db.execute("INSERT INTO profile_evidence (id, signal, ref, weight, direction, observed_at, summary, kind, subject_key) "
               "VALUES (gen_random_uuid(), %s, %s, 1, 1, now(), 'x', 'like', %s)", (signal, ref, key))  # fmt: skip


def _core(db: Any, now: datetime, words: str | None) -> list[str]:
    async def go() -> list[str]:
        pool = await open_pool(dsn(db), max_size=2)
        await pool.wait()
        try:
            svc = ProfileService(pool, owner=owner_at(), clock=Clock(now))
            if words is not None:
                await svc.turn_started(words)
            return await svc.core_profile()
        finally:
            await pool.close()

    return asyncio.run(go())


def test_3_a_breakfast_turn_gets_its_past_picks_and_not_the_pizzeria(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    cafe = place(db, "Caffè Inventato", (45.46, 9.18))
    db.execute("UPDATE places SET category = 'cafe' WHERE id = %s", (cafe,))
    pizza = place(db, "Pizzeria Finta", (45.46, 9.18))
    card(db, local("2026-11-07T08:30"), [cafe], "went", cafe, device)
    card(db, local("2026-11-06T20:30"), [pizza], "went", pizza, device)
    _observed(db, "Gli piace «Pizzeria Finta»", f"place:{pizza}")
    _observed(db, "musica: tech house", "words:like:musica:1")
    now = local("2026-11-10T08:30")
    with_words = "\n".join(_core(db, now, "il solito posto per colazione?"))
    assert "[past pick] «Caffè Inventato» (cafe), Pisa's card of Sat 07 Nov: they went" in with_words
    assert "Pizzeria Finta" not in with_words and "musica" not in with_words
    without = "\n".join(_core(db, now, None))  # a job's prompt: every line that fits, as before
    assert "Pizzeria Finta" in without and "musica" in without and "[past pick]" not in without


# --- 5. the memory scenarios on the depth world (the stub) ---------------------------------------------------


def _play(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> tuple[Any, list[str]]:  # fmt: skip
    from pisa.infra.keys import KeyStore
    from tests.actions_fake import ActionsGoogle
    from tests.conftest import set_key
    from tests.replay import harness

    set_key(db, secrets_key)
    google = ActionsGoogle()
    http_handler["handler"] = harness.route(google, harness.local(scenario["now"]).date())
    base = harness.script(scenario)
    told: list[str] = []

    async def model(messages: Any, info: Any) -> Any:
        instructions = str(getattr(messages[-1], "instructions", None) or "")
        if "profile for this turn" in instructions or "knows nothing about the owner" in instructions:
            told.append(instructions)
        async for part in base(messages, info):
            yield part

    fake_models.default = model
    runtime = client.app.state.runtime
    keys = KeyStore(SecretStr(secrets_key))
    result = client.portal.call(
        harness.run, runtime.conversations, runtime.pool, google, scenario, keys, clock, runtime
    )
    return result, told


@pytest.mark.parametrize("scenario", __import__("tests.replay.memory", fromlist=["SCENARIOS"]).SCENARIOS,
                         ids=lambda s: s["id"])  # fmt: skip
def test_5_the_memory_scenarios_on_the_stub(
    client, db, fake_models, secrets_key, clock, http_handler, scenario
) -> None:  # type: ignore[no-untyped-def]
    from tests.replay import memory

    result, told = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    replies = [t.reply for t in result.turns]
    assert memory.problems(scenario, told, replies) == [], (told, replies)


# --- 6. the portrait in "Su di te" --------------------------------


def _claim_row(db: Any, kind: str, statement: str, *, state: str = "active", sensitive: bool = False) -> str:
    cid = uuid.uuid4()
    db.execute("INSERT INTO profile_claims (id, kind, statement, subject, provenance, support, confidence, state, sensitive, created_at, updated_at) "
               "VALUES (%s, %s, %s, '{}', 'confirmed', 2, 0.8, %s, %s, now(), now())", (cid, kind, statement, state, sensitive))  # fmt: skip
    return str(cid)


class _Write:
    """The model, faked: it writes from the numbered items, and once a line with no claim behind it."""

    def __init__(self, db: Any, raw: dict[str, Any] | None = None) -> None:
        self.db, self.raw, self.items, self.calls = db, raw, [], 0

    async def __call__(self, items: list[str], lang: str, *, trace: str) -> dict[str, Any]:
        self.calls += 1
        self.items = items
        self.db.execute("INSERT INTO llm_calls (id, slot, model_ref, status, trace_id, reserved_eur, cost_eur, price_known, budget) VALUES "
                        "(gen_random_uuid(), 'chat.conversation', 'test:x', 'settled', %s, 0.0003, 0.0003, true, 'night')", (trace,))  # fmt: skip
        if self.raw is not None:
            return self.raw
        n = {item.split(": ", 1)[1]: i + 1 for i, item in enumerate(items)}
        return {"text": "Ti piace la tech house e fai colazione con calma.", "themes": [
            {"id": "music", "title": "Musica", "lines": [
                {"text": "La tech house, mai l'EDM commerciale.", "claims": [n["musica: tech house"], n["musica: niente EDM commerciale"]]},
                {"text": "Ami il jazz di New Orleans.", "claims": [n["musica: tech house"]]},  # says nothing its claim says
                {"text": "Ti piace il rock.", "claims": [99]},  # no such item
            ]},
            {"id": "mornings", "title": "Mattine", "lines": [{"text": "Colazione seduto, con calma.", "claims": [n["colazione: seduto, con calma"]]}]},
        ]}  # fmt: skip


def _portrait(client: Any) -> Any:
    return client.app.state.runtime.portrait


def test_6_the_portrait_is_written_from_active_claims_only_and_checked_by_code(
    client, db, clock, auth
) -> None:  # type: ignore[no-untyped-def]
    music = _claim_row(db, "like", "musica: tech house")
    no_edm = _claim_row(db, "dislike", "musica: niente EDM commerciale")
    breakfast = _claim_row(db, "routine", "colazione: seduto, con calma")
    _claim_row(db, "like", "musica: vinili anni 90", state="candidate")
    _claim_row(db, "like", "cibo: dieta senza glutine", sensitive=True)
    fake = _Write(db)
    _portrait(client)._write = fake
    _at_night(clock)
    assert _call(client, _portrait(client).build, NIGHT) == 1
    assert all(
        "vinili" not in i and "glutine" not in i for i in fake.items
    )  # never a candidate, never sensitive
    got = client.get("/v1/profile/portrait", headers=auth).json()
    assert got["text"].startswith("Ti piace la tech house") and got["claims_used"] == 3
    assert [t["id"] for t in got["themes"]] == ["music", "other"]  # an unknown theme id is `other`
    assert got["themes"][0]["lines"] == [
        {"text": "La tech house, mai l'EDM commerciale.", "claim_ids": [music, no_edm]}
    ]
    assert got["themes"][1]["lines"][0]["claim_ids"] == [breakfast]
    assert _call(client, _portrait(client).build, NIGHT) == 0 and fake.calls == 1  # nothing changed: no call


def test_6_too_little_is_an_empty_portrait_and_no_call(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    _claim_row(db, "like", "musica: tech house")
    fake = _Write(db)
    _portrait(client)._write = fake
    _at_night(clock)
    assert _call(client, _portrait(client).build, NIGHT) == 0 and fake.calls == 0
    got = client.get("/v1/profile/portrait", headers=auth).json()
    assert got["text"] == "" and got["themes"] == [] and got["built_at"] is not None


def test_6_his_corrections_retire_or_rewrite_the_line_s_claims(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    music = _claim_row(db, "like", "musica: tech house")
    no_edm = _claim_row(db, "dislike", "musica: niente EDM commerciale")
    breakfast = _claim_row(db, "routine", "colazione: seduto, con calma")
    _claim_row(db, "like", "viaggi: treno più che aereo")
    _claim_row(db, "like", "serate: piccoli club")  # still enough claims once a line is gone
    _portrait(client)._write = _Write(db)
    _at_night(clock)
    _call(client, _portrait(client).build, NIGHT)
    post = partial(client.post, "/v1/profile/portrait/lines", headers=auth)
    assert post(json={"claim_ids": [music], "answer": "wrong"}).status_code == 404  # not a line's own ids
    assert post(json={"claim_ids": [breakfast], "answer": "correct"}).status_code == 400  # no words
    fixed = post(
        json={"claim_ids": [breakfast], "answer": "correct", "words": "colazione al banco, veloce"}
    ).json()
    assert fixed["themes"][1]["lines"] == [{"text": "colazione al banco, veloce", "claim_ids": [breakfast]}]
    row = db.execute(
        "SELECT statement, provenance, state FROM profile_claims WHERE id = %s", (breakfast,)
    ).fetchone()
    assert row == ("colazione al banco, veloce", "confirmed", "active")
    gone = post(json={"claim_ids": [music, no_edm], "answer": "wrong"}).json()
    assert [t["id"] for t in gone["themes"]] == ["other"]
    states = db.execute(
        "SELECT state, retired_by FROM profile_claims WHERE id = ANY(%s)", ([music, no_edm],)
    ).fetchall()
    assert states == [("retired", "owner"), ("retired", "owner")]


def test_6_conosciamoci_is_offered_once_after_pisa_said_it_did_not_know(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    cid = _talk(db, [(local("2026-11-11T19:00"), "che musica mi piace?")], replies=False)
    db.execute("INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'assistant', %s, 'it-IT', %s)",
               (cid, "Dei tuoi gusti musicali non so ancora nulla: dimmi due artisti.", local("2026-11-11T19:00")))  # fmt: skip
    other = _talk(db, [(local("2026-11-10T19:00"), "che ore sono?")], replies=False)
    db.execute("INSERT INTO messages (conversation_id, role, content, lang, created_at) VALUES (%s, 'assistant', %s, 'it-IT', %s)",
               (other, "Sono le sette. Non so se il bar è aperto.", local("2026-11-10T19:00")))  # fmt: skip
    clock.now = datetime(2026, 11, 11, 3, 30, tzinfo=ROME).astimezone(UTC)
    assert (
        _call(client, _portrait(client).offer, date(2026, 11, 11)) == 0
    )  # Pisa said nothing of the kind that day
    _at_night(clock)
    assert _call(client, _portrait(client).offer, NIGHT) == 1
    row = db.execute("SELECT kind, choices, expects_reply FROM notifications").fetchone()
    assert row == ("info", [{"id": "conosciamoci", "label": "Conosciamoci"}], True)
    assert _call(client, _portrait(client).offer, NIGHT) == 0  # once a week


def test_6_pisa_s_own_words_that_it_does_not_know() -> None:
    from pisa.domain.portrait import said_unknown

    for yes in ("Non so ancora cosa ti piace davvero.", "Dei tuoi gusti musicali non so ancora nulla.",
                "Sui tuoi gusti non ho niente di certo.", "I don't know what you like yet."):  # fmt: skip
        assert said_unknown(yes), yes
    for no in ("Non so se è aperto.", "Ti piace la house, vero?", "Non so cosa sia successo."):
        assert not said_unknown(no), no
