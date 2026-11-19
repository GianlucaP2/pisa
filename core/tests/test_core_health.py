# ruff: noqa: E501, RUF001
"""A health-sensitive place (pharmacy, clinic, hospital, doctor, dentist, lab) is
answered on their own direct question from Pisa's stays, and never surfaced by Pisa on its own: no episode or digest,
no line in a briefing or the night story, no hunch or claim, no walk recap, no "where you are" in the talk, no
idea or map. Synthetic places and times, never theirs."""

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from pisa.domain import episodes as episode_rules
from pisa.domain.health import health_kinds, health_place
from pisa.domain.learning import sensitive_place, taste_place
from tests.conftest import pair

ROME = ZoneInfo("Europe/Rome")
PISA_ROOT = Path(__file__).resolve().parents[1] / "pisa"
PHARMACY = "Farmacia Aurora"


def _place(db: Any, name: str, category: str | None) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, category, lat, lon, created_at, updated_at) "
        "VALUES (%s, %s, %s, 45.1, 9.1, now(), now())",
        (pid, name, category),
    )
    return pid


def _stay(db: Any, device: Any, place: uuid.UUID, a: datetime, b: datetime | None) -> None:
    db.execute(
        "INSERT INTO stays (id, device_id, arrived_at, departed_at, lat, lon, accuracy_m, last_seen_at, place_id) "
        "VALUES (%s, %s, %s, %s, 45.1, 9.1, 10, %s, %s)",
        (uuid.uuid4(), device, a, b, b or a, place),
    )


# --- the helper ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Farmacia Aurora", None), ("Bottega", "pharmacy"), ("Ospedale Maggiore", None), (None, "hospital"),
        ("Studio Dentistico Sole", None), (None, "dentist"), (None, "doctor"), (None, "medical_lab"),
        ("Poliambulatorio Nord", None), ("Clinica Verde", None), ("Laboratorio Analisi Est", None),
    ],
)  # fmt: skip
def test_a_health_place_by_its_type_or_its_name(name: str | None, category: str | None) -> None:
    assert health_place(name, category) and sensitive_place(name, category)
    assert not taste_place(name, category)  # never evidence for a claim


@pytest.mark.parametrize(
    ("name", "category"),
    [("Bar Aurora", "cafe"), ("Laboratorio di pasticceria", "bakery"), ("Dott. Neri, commercialista", None)],
)
def test_not_a_health_place(name: str, category: str | None) -> None:
    assert not health_place(name, category)


def test_his_words_name_the_kind() -> None:
    assert health_kinds("ieri sono stato in farmacia?", asked=True) == {"pharmacy"}
    assert health_kinds("sono andato dal dottore martedì?", asked=True) == {"doctor"}
    assert health_kinds("did I go to the dentist yesterday?", asked=True) == {"dentist"}
    assert health_kinds("com'è andata la cena?", asked=True) == set()


# --- their direct question ------------------------------------------------------------------------------------


def test_his_question_is_answered_from_stays_the_place_and_the_time_only(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    pharmacy = _place(db, PHARMACY, "pharmacy")
    clock.now = datetime(2026, 11, 12, 18, 0, tzinfo=ROME).astimezone(UTC)
    _stay(
        db,
        device,
        pharmacy,
        datetime(2026, 11, 11, 18, 2, tzinfo=ROME),
        datetime(2026, 11, 11, 18, 20, tzinfo=ROME),
    )
    svc = client.app.state.runtime.episodes

    def recall(words: str, lang: str = "it-IT") -> dict[str, Any]:
        return client.portal.call(lambda: svc.recall("farmacia", words=words, lang=lang))

    got = recall("ieri sono stato in farmacia?")
    assert got["answer"] == "one" and got["say"] == f"Sì: ieri {PHARMACY}, 18:02–18:20."
    assert recall("sono passato in farmacia?")["say"] == f"Sì: ieri {PHARMACY}, 18:02–18:20."  # last week
    assert recall("lunedì sono stato in farmacia?")["answer"] == "none"
    assert (
        recall("did I go to a pharmacy yesterday?", "en-US")["say"]
        == f"Yes: yesterday {PHARMACY}, 18:02–18:20."
    )
    assert recall("ho comprato la farmacia")["answer"] == "refused"  # not a question about their past


def test_the_answer_never_says_why(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    """Never a diagnosis: the line is code's, the place and the time; the tool tells the model to add nothing."""
    say = episode_rules.say_health(
        [(PHARMACY, datetime(2026, 11, 11, 17, tzinfo=UTC), datetime(2026, 11, 11, 17, 20, tzinfo=UTC))],
        {"pharmacy"}, date(2026, 11, 11), date(2026, 11, 11), date(2026, 11, 12), ROME, "it-IT",
    )  # fmt: skip
    assert say == f"Sì: ieri {PHARMACY}, 18:00–18:20."
    from pisa.brain import memory_tools

    assert "add nothing" in Path(memory_tools.__file__).read_text()


# --- never on Pisa's own ---------------------------------------------------------------------------------------


def test_no_episode_or_digest_holds_it(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    pharmacy = _place(db, PHARMACY, "pharmacy")
    _stay(
        db,
        device,
        pharmacy,
        datetime(2026, 11, 11, 17, tzinfo=ROME),
        datetime(2026, 11, 11, 17, 20, tzinfo=ROME),
    )
    night = date(2026, 11, 12)
    clock.now = datetime(2026, 11, 12, 3, 30, tzinfo=ROME).astimezone(UTC)
    svc = client.app.state.runtime.episodes
    client.portal.call(svc.segment, night)
    client.portal.call(svc.digest, night)
    rows = db.execute("SELECT place_id, coalesce(digest, '') FROM episodes").fetchall()
    assert all(place != pharmacy and "Farmacia" not in digest for place, digest in rows)


def test_the_briefing_and_the_night_story_never_read_stays() -> None:
    """The briefing and the night's story (stanotte, the night) are built from Pisa's other records: neither
    reads stays, episodes or the GPS. A test fails the day one does without the gate."""
    for name in ("briefing.py", "stanotte.py", "night.py"):
        text = (PISA_ROOT / "services" / name).read_text()
        for table in ("stays", "device_locations", "device_visits", "episodes"):
            assert f"FROM {table}" not in text and f"JOIN {table}" not in text, (name, table)


def test_the_briefing_leaves_it_out(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device, token = pair(db)
    pharmacy = _place(db, PHARMACY, "pharmacy")
    now = clock.now
    _stay(db, device, pharmacy, now - timedelta(hours=20), now - timedelta(hours=19, minutes=40))
    briefing = client.post("/v1/briefings", headers={"Authorization": f"Bearer {token}"})
    assert briefing.status_code == 201 and "Farmacia" not in briefing.json()["text"]


def test_a_hunch_from_it_is_refused(client, db) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    runtime = client.app.state.runtime
    said = client.portal.call(
        lambda: runtime.rituals.note_hunch(
            "Il martedì pomeriggio passi sempre in centro?", [f"martedì 17:00, {PHARMACY}"], uuid.UUID(device)
        )
    )
    assert said.startswith("Not stored") and db.execute("SELECT count(*) FROM hunches").fetchone() == (0,)


def test_the_talk_never_says_he_is_there(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    device, _ = pair(db)
    pharmacy, bar = _place(db, PHARMACY, "pharmacy"), _place(db, "Bar Aurora", "cafe")
    talk = client.app.state.runtime.talk

    async def fresh() -> Any:
        async with client.app.state.runtime.pool.connection() as conn:
            return await talk.fresh(conn, uuid.UUID(device), "it-IT")

    _stay(db, device, pharmacy, clock.now - timedelta(minutes=10), None)
    assert client.portal.call(fresh).place == ""
    db.execute("DELETE FROM stays")
    _stay(db, device, bar, clock.now - timedelta(minutes=10), None)
    assert client.portal.call(fresh).place == "Bar Aurora"


def test_a_walk_that_ends_there_gets_no_recap(client, db) -> None:  # type: ignore[no-untyped-def]
    walks = client.app.state.runtime.rituals.walks
    pharmacy, bar = _place(db, PHARMACY, "pharmacy"), _place(db, "Bar Aurora", "cafe")

    async def known(place: uuid.UUID) -> bool:
        async with client.app.state.runtime.pool.connection() as conn:
            return await walks._known(conn, place, 45.1, 9.1)  # pyright: ignore[reportPrivateUsage]

    assert client.portal.call(known, bar) and not client.portal.call(known, pharmacy)


def test_every_unprompted_path_names_the_one_helper() -> None:
    """Round 3, item 8 (kept beside the behavioural test below): the gate is `learning.sensitive_place`
    (health via `health.health_place`) wherever Pisa speaks of theirs stays by itself."""
    for name in ("episodes.py", "learning.py", "talk.py", "walks.py", "rituals.py", "explore.py", "done.py"):
        assert "sensitive_place(" in (PISA_ROOT / "services" / name).read_text(), name
    assert "health_place(" in (PISA_ROOT / "domain" / "learning.py").read_text()


def test_every_unprompted_path_checks_the_one_helper(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The conversation's own day ("cosa ho fatto oggi?", or the model's own call of
    what_did_i_do) never names a pharmacy stay; the bar beside it is named. Behavioural: the tool's own result."""
    from tests.test_follow_tools import NOW, ask
    from tests.test_place_tools import data_of

    device, _ = pair(db)
    pharmacy, bar = _place(db, PHARMACY, "pharmacy"), _place(db, "Bar Aurora", "cafe")
    _stay(db, device, pharmacy, NOW - timedelta(hours=3), NOW - timedelta(hours=2, minutes=40))
    _stay(db, device, bar, NOW - timedelta(hours=2), NOW - timedelta(hours=1, minutes=30))
    said = data_of(ask(db, secrets_key, "what_did_i_do", {"day": NOW.date().isoformat()}))
    assert "Bar Aurora" in said and "Farmacia" not in said
