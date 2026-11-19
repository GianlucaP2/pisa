# ruff: noqa: E501
"""The memory review's fixes. Synthetic words, names and places
only. Each fails when its fix is reverted (scripts/review/revert_core_memory1.py, the review's rows)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from pisa.domain import memory as rules
from pisa.domain import portrait as portrait_rules
from pisa.domain.errors import PisaError
from tests.conftest import pair
from tests.test_learning import at, card, claims, learning, place
from tests.test_memory import (
    MUSIC,
    NIGHT,
    _at_night,
    _call,
    _claim_row,
    _evidence,
    _Extract,
    _observed,
    _portrait,
    _svc,
    _talk,
    _Write,
    local,
)


def _retire_card_only(db: Any, key: str) -> None:
    """Pisa took the claim back itself as card-only: its "go" evidence was a tap, kept as interest."""
    db.execute(
        "UPDATE profile_evidence SET signal = 'interest', value = '{\"was\": \"S3\"}' WHERE subject_key = %s",
        (key,),
    )
    db.execute("INSERT INTO profile_retirements (id, at, count) VALUES (gen_random_uuid(), now(), 1)")
    db.execute("UPDATE profile_claims SET retired_from = state, state = 'retired', retired_by = 'pisa', retired_reason = 'card_only', "
               "retired_batch = (SELECT id FROM profile_retirements), updated_at = now() WHERE subject_key = %s", (key,))  # fmt: skip


@pytest.fixture
def auth(db: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


def P(kind: str, subject: str, value: str, quote: str) -> rules.Proposal:
    return rules.Proposal(kind, subject, value, quote).clean()


# --- H1: whose taste it is, decided on the sentence around the quote ------------------------------------------


def test_someone_else_s_taste_or_words_never_become_his() -> None:
    cases = [
        ("Guarda cosa mi ha scritto: «Adoro il jazz e odio la techno, vediamoci al Blue Note»",
         P("like", "musica", "jazz", "Adoro il jazz"), "quoted text"),
        ("Ho letto su questo sito: i romani preferiscono la carbonara al ristorante",
         P("like", "cibo", "carbonara", "la carbonara al ristorante"), "quoted text"),
        ("A Ottavio piace tantissimo il sushi, prenoto lì?",
         P("like", "cibo", "sushi", "piace tantissimo il sushi"), "other people"),
        ("Ernestina ama la musica techno, trovami un club",
         P("like", "musica", "techno", "la musica techno"), "other people"),
        ("Sono vegano da anni, quindi niente carne per me",
         P("dislike", "cibo", "carne", "niente carne per me"), "sensitive"),
        ("Ho il diabete, quindi niente dolci a colazione",
         P("dislike", "colazione", "dolci", "niente dolci a colazione"), "sensitive"),
    ]  # fmt: skip
    for words, proposal, why in cases:
        assert rules.why_not(proposal, words) == why, words
    # their own, plainly: still kept (and "gli" as an article is no other person, "letto" a bed, "ti ho detto" theirs)
    for words, proposal in (
        ("Di solito vado a letto presto la sera", P("routine", "sera", "a letto presto", "vado a letto presto")),
        ("Come ti ho detto, adoro la house", P("like", "musica", "house", "adoro la house")),
        ("Mi piacciono gli spritz al tramonto", P("like", "aperitivo", "spritz", "Mi piacciono gli spritz")),
        ("a colazione mi piace stare seduto, con calma", P("routine", "colazione", "seduto, con calma", "a colazione mi piace stare seduto, con calma")),
        ("Ho detto a dicembre. La musica che ascolto è house, soprattutto tech house",
         P("like", "musica", "house, in particolare tech house", "house, soprattutto tech house")),
    ):  # fmt: skip
        assert rules.why_not(proposal, words) is None, words


# --- H2: what Pisa took back itself comes back with a real visit or a "go" ---------------------------------------


def test_a_place_retired_as_card_only_is_a_like_again_after_he_goes(db) -> None:  # type: ignore[no-untyped-def]
    device = pair(db)[0]
    tapped = place(db, "Osteria Inventata", (45.47, 9.19))
    old_card = uuid.uuid4()
    db.execute("INSERT INTO recommendation_outcomes (id, card_id, place_id, action, created_at) VALUES (gen_random_uuid(), %s, %s, 'plan', %s)",
               (old_card, tapped, at(date(2026, 9, 1), 12)))  # fmt: skip
    _observed(db, "Gli piace «Osteria Inventata»", f"place:{tapped}")
    _evidence(db, f"place:{tapped}", "S3", f"card:{old_card}")
    db.execute("UPDATE profile_evidence SET observed_at = %s", (at(date(2026, 9, 1), 12),))
    db.execute("UPDATE profile_claims SET last_evidence_at = %s", (at(date(2026, 9, 1), 12),))
    _retire_card_only(db, f"place:{tapped}")
    assert claims(db)[f"place:{tapped}"]["state"] == "retired"
    day = date(2026, 10, 19)
    for n in range(6):  # now they really go: a "go" on Pisa's card, six times
        card(db, at(day + timedelta(days=n), 13), [tapped], "go", tapped, device)
    learning(db, at(date(2026, 10, 26), 3, 30), lambda s: s.nightly())
    assert claims(db)[f"place:{tapped}"]["state"] in ("active", "candidate")
    row = db.execute("SELECT retired_by, retired_reason, retired_from FROM profile_claims").fetchone()
    assert row == (None, None, None)


# --- M1: a model or provider that fails, or a dear model, never burns or overspends the night ----------------


class _Down(_Extract):
    async def __call__(self, words: str, lang: str, *, trace: str) -> list[rules.Proposal]:
        self.read.append(words)
        raise PisaError("provider_error", "down", "Try again")


def test_a_provider_down_leaves_the_conversations_waiting(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    for hour in (9, 12):
        _talk(db, [(local(f"2026-11-11T{hour:02d}:00"), "mi piace tanto la tech house")])
    down = _Down(db, [MUSIC])
    _svc(client)._extract = down
    _at_night(clock)
    _call(client, _svc(client).extract, NIGHT)
    assert len(down.read) == 1  # the first failure stops the pass
    assert db.execute("SELECT count(*) FROM profile_extractions").fetchone() == (0,)  # nothing burned
    assert _call(client, _svc(client).night, NIGHT)["waiting"] == 2
    up = _Extract(db, [MUSIC])
    _svc(client)._extract = up
    _call(client, _svc(client).extract, NIGHT)
    assert len(up.read) == 2  # read the next night, as if nothing happened


def test_a_model_dearer_than_the_caps_is_never_called(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    _talk(db, [(local("2026-11-11T19:00"), "mi piace tanto la tech house")])
    fake = _Extract(db, [MUSIC])
    svc = _svc(client)
    svc._extract = fake

    async def dear(words: str, lang: str) -> Decimal:
        return Decimal("0.004")  # the slot set to a dearer model: within €0.05, but with a retry past €0.005

    old, svc._estimate = svc._estimate, dear
    try:
        _at_night(clock)
        _call(client, svc.extract, NIGHT)
    finally:
        svc._estimate = old
    assert fake.read == []
    assert _call(client, svc.night, NIGHT)["capped"]
    portrait = _portrait(client)
    for n in range(3):
        _claim_row(db, "like", f"musica: genere {n}")
    write = _Write(db, {"text": "", "themes": []})
    portrait._write, old_est = write, portrait._estimate

    async def dear_items(items: list[str], lang: str) -> Decimal:
        return Decimal("0.07")

    portrait._estimate = dear_items
    try:
        assert _call(client, portrait.build, NIGHT) == 0 and write.calls == 0
    finally:
        portrait._estimate = old_est


# --- M2: a retirement's Undo gives the taps back ---------------------------------------------------------------


def test_the_undo_of_a_retirement_restores_the_taps(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    pair(db)
    tapped = place(db, "Pizzeria Finta", (45.4, 9.1))
    gone = _observed(db, "Gli piace «Pizzeria Finta»", f"place:{tapped}")
    tap = uuid.uuid4()
    db.execute("INSERT INTO recommendation_outcomes (id, card_id, place_id, action, created_at) VALUES (gen_random_uuid(), %s, %s, 'plan', now())",
               (tap, tapped))  # fmt: skip
    _evidence(db, f"place:{tapped}", "S3", f"card:{tap}")
    _retire_card_only(db, f"place:{tapped}")
    states = dict(db.execute("SELECT id, state FROM profile_claims").fetchall())
    assert states[gone] == "retired"
    (batch,) = db.execute("SELECT id FROM profile_retirements").fetchone()
    clock.now = datetime.now(UTC)
    _call(client, client.app.state.runtime.autopilot.ledger.undo, f"profile:{batch}")
    assert db.execute(
        "SELECT signal, value FROM profile_evidence WHERE subject_key = %s", (f"place:{tapped}",)
    ).fetchone() == ("S3", None)


# --- M3: the paragraph is held to the lines' rules, and leaves with the claims it was written from ----------


def test_the_paragraph_leaves_with_its_claims_and_never_says_a_sensitive_word(
    client, db, clock, auth
) -> None:  # type: ignore[no-untyped-def]
    music = _claim_row(db, "like", "musica: tech house")
    _claim_row(db, "dislike", "musica: niente EDM commerciale")
    breakfast = _claim_row(db, "routine", "colazione: seduto, con calma")
    _claim_row(db, "like", "viaggi: treno più che aereo")
    _claim_row(db, "like", "serate: piccoli club")
    _portrait(client)._write = _Write(db)
    _at_night(clock)
    _call(client, _portrait(client).build, NIGHT)
    assert (
        client.get("/v1/profile/portrait", headers=auth).json()["text"].startswith("Ti piace la tech house")
    )
    assert client.delete(f"/v1/profile/claims/{music}", headers=auth).status_code in (
        200,
        204,
    )  # they forget it
    got = client.get("/v1/profile/portrait", headers=auth).json()
    assert got["text"] == ""  # the paragraph named it: gone until the night writes it again
    assert got["themes"] and all(music not in ln["claim_ids"] for t in got["themes"] for ln in t["lines"])
    # the model's own words: a sensitive word, or a title of its own, never reach the page
    src = [portrait_rules.Source(breakfast, "routine", "colazione: seduto, con calma")]
    raw = {"text": "Fai colazione con calma, da vegano.", "themes": [{"id": "food", "title": "La tua dieta", "lines": [
        {"text": "Colazione seduto, con calma.", "claims": [1]},
        {"text": "Colazione con calma, senza glutine.", "claims": [1]}]}]}  # fmt: skip
    out = portrait_rules.checked(raw, src, "it-IT")
    assert out["text"] == "" and out["themes"] == [
        {
            "id": "food",
            "title": "Cibo",
            "lines": [{"text": "Colazione seduto, con calma.", "claim_ids": [breakfast]}],
        }
    ]


# --- L: a past pick at a place of a kind they put off limits is not built on ---------------------------------


def test_l_no_past_pick_at_an_off_limits_kind(db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_memory import _core

    device = pair(db)[0]
    club = place(db, "Club Inventato", (45.46, 9.18))
    db.execute("UPDATE places SET category = 'night_club' WHERE id = %s", (club,))
    card(db, local("2026-11-07T23:30"), [club], "went", club, device)
    now = local("2026-11-10T22:30")
    assert any("Club Inventato" in ln for ln in _core(db, now, "dove andiamo a ballare stasera?"))
    db.execute(
        "INSERT INTO profile_off_limits (id, topic, created_at) VALUES (gen_random_uuid(), 'discoteche', now())"
    )
    assert not any("Club Inventato" in ln for ln in _core(db, now, "dove andiamo a ballare stasera?"))


def test_l_a_past_pick_he_likes_is_never_said_as_not_gone() -> None:
    from datetime import UTC, datetime

    pick = rules.Pick(
        "Caffè Inventato", "cafe", datetime(2026, 11, 7, 8, tzinfo=UTC), False, False, True, False
    )
    line = rules.pick_line(pick, datetime(2026, 11, 7, 9))
    assert "like the place" in line and "didn't go" not in line
