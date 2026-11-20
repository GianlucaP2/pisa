"""The first-talk offer rides a check-in (IOS-REVIEW-62, ruling 2b): the "Conosciamoci" notice is a
`pisa.notice` of kind `info` whose one choice is in `checkIn` (never bare `choices`), and its answer, through
the check-in route, starts the first talk as the app's button does. In English and in Italian. Synthetic
words only."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pisa_conformance.validate import EXT_ID, errors
from pydantic import SecretStr

from pisa.db.pool import open_pool
from pisa.domain import notice as notice_rules
from pisa.infra.keys import KeyStore
from pisa.services.follow import OFFER_KEY
from pisa.services.notifications import Dispatcher, _notice_fields
from pisa.services.push import PushService
from tests.assistant_env import set_settings
from tests.conftest import Clock, dsn, pair
from tests.owner import owner_at
from tests.test_memory import NIGHT, ROME, _at_night, _call, _portrait, _talk
from tests.test_push import Live

LANGS = {
    "en-US": ("I don't know what you like yet: tell me two artists.", "Let's get to know each other"),
    "it-IT": ("Dei tuoi gusti musicali non so ancora nulla: dimmi due artisti.", "Conosciamoci"),
}
DAY = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # Thursday 11:00 in Rome: no quiet hours


@pytest.fixture
def auth(db: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


def _offered(client: Any, db: Any, clock: Any, lang: str) -> tuple[uuid.UUID, str]:
    """Pisa said yesterday it doesn't know what they like; the night offers the talk. Returns the notice's id
    and the device."""
    set_settings(db, {"language": lang})
    said, _ = LANGS[lang]
    cid = _talk(db, [(datetime(2026, 11, 11, 19, 0, tzinfo=ROME), "what music do I like?")], replies=False)
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, lang, created_at) "
        "VALUES (%s, 'assistant', %s, %s, %s)",
        (cid, said, lang, datetime(2026, 11, 11, 19, 0, tzinfo=ROME)),
    )
    _at_night(clock)
    assert _call(client, _portrait(client).offer, NIGHT) == 1
    (row,) = db.execute("SELECT id FROM notifications WHERE path = 'profile.talk_offer'").fetchall()
    (device,) = db.execute("SELECT id::text FROM devices").fetchone()
    return row[0], device


def _deliver(db: Any, notification_id: uuid.UUID, device: str) -> dict[str, Any]:
    """The held notice, delivered in the morning over the socket: its `pisa.notice`. (Quiet hours held it;
    the guardian's digest lets a notice with a check-in out alone, never folded.)"""
    db.execute(
        "UPDATE notifications SET delivered_via = NULL, queued_at = %s, guardian = 'released' WHERE id = %s",
        (DAY, notification_id),
    )
    live = Live({device})

    async def go() -> None:
        pool = await open_pool(dsn(db), max_size=3)
        await pool.wait()
        try:
            keys = KeyStore(SecretStr(Fernet.generate_key().decode()))
            push = PushService(pool, keys, httpx.AsyncClient(), team_id="T", topic="t")
            await Dispatcher(pool, live, push, clock=Clock(DAY), owner=owner_at()).deliver(notification_id)
        finally:
            await pool.close()

    asyncio.run(go())
    ((_, sent),) = live.sent
    return notice_rules.value(_notice_fields(sent), default_lang="it-IT")


@pytest.mark.parametrize("lang", ["en-US", "it-IT"])
def test_the_offer_is_a_notice_riding_a_check_in(client, db, clock, auth, lang: str) -> None:  # type: ignore[no-untyped-def]
    notification_id, device = _offered(client, db, clock, lang)
    notice = _deliver(db, notification_id, device)
    assert errors(notice, EXT_ID + "#/$defs/s_pisa_notice") == []
    assert notice["kind"] == "info" and notice["lang"] == lang and "choices" not in notice
    check_in = notice["checkIn"]
    assert check_in["choices"] == [{"id": "conosciamoci", "label": LANGS[lang][1]}]
    assert uuid.UUID(check_in["checkInId"]) and check_in["expectsReply"] is True
    # the day's check-ins stay the plan's: the offer is no row there and blocks none
    assert db.execute("SELECT count(*) FROM check_ins").fetchone()[0] == 0
    assert client.get("/v1/check-ins", headers=auth, params={"status": "all"}).json() == {"check_ins": []}
    # the weekly rule holds: no second offer the next night
    assert _call(client, _portrait(client).offer, NIGHT) == 0


@pytest.mark.parametrize("lang", ["en-US", "it-IT"])
def test_conosciamoci_answered_starts_the_first_talk_once(client, db, clock, auth, lang: str) -> None:  # type: ignore[no-untyped-def]
    notification_id, device = _offered(client, db, clock, lang)
    check_in_id = _deliver(db, notification_id, device)["checkIn"]["checkInId"]
    clock.now = DAY
    url = f"/v1/check-ins/{check_in_id}/answer"
    wrong = client.post(url, headers=auth, json={"choice": "skip"})
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "validation_failed"
    assert (
        db.execute("SELECT value->>'state' FROM app_settings WHERE key = 'profile_first_talk'").fetchone()
        is None
    )
    answered = client.post(url, headers=auth, json={"choice": "conosciamoci"})
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["plan"] is None and body["options"] is None
    assert body["check_in"] | {"created_at": None, "answered_at": None} == {
        "id": check_in_id, "date": "2026-11-12", "kind": "confirm", "title": LANGS[lang][1],
        "text": body["check_in"]["text"], "choices": [{"id": "conosciamoci", "label": LANGS[lang][1]}],
        "created_at": None, "answered_at": None, "answer": "conosciamoci",
    }  # fmt: skip
    # the first talk began as the app's button begins it: its state, and its opening line on this device
    (talk,) = db.execute("SELECT value FROM app_settings WHERE key = 'profile_first_talk'").fetchone()
    assert talk["state"] == "in_progress" and talk["section"] == 0 and talk["lang"] == lang
    opening = db.execute(
        "SELECT device_id::text, lang, expects_reply FROM notifications WHERE path = 'profile.first_talk'"
    ).fetchall()
    assert opening == [(device, lang, True)]
    (offer,) = db.execute("SELECT value FROM app_settings WHERE key = %s", (OFFER_KEY,)).fetchone()
    assert offer["id"] == check_in_id and offer["answer"] == "conosciamoci" and offer["answered_at"]
    again = client.post(url, headers=auth, json={"choice": "conosciamoci"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "check_in_answered"
    assert (
        db.execute("SELECT count(*) FROM notifications WHERE path = 'profile.first_talk'").fetchone()[0] == 1
    )
    # the talk going on: never offered again
    clock.now = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)
    assert _call(client, _portrait(client).offer, NIGHT.replace(day=20)) == 0


def test_an_offer_taken_never_restarts_a_talk_already_done(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    notification_id, device = _offered(client, db, clock, "it-IT")
    check_in_id = _deliver(db, notification_id, device)["checkIn"]["checkInId"]
    db.execute("INSERT INTO app_settings (key, value) VALUES ('profile_first_talk', '{\"state\": \"done\"}')")
    clock.now = DAY
    answered = client.post(
        f"/v1/check-ins/{check_in_id}/answer", headers=auth, json={"choice": "conosciamoci"}
    )
    assert answered.status_code == 200
    assert db.execute(
        "SELECT value->>'state' FROM app_settings WHERE key = 'profile_first_talk'"
    ).fetchone() == ("done",)
    assert (
        db.execute("SELECT count(*) FROM notifications WHERE path = 'profile.first_talk'").fetchone()[0] == 0
    )


def test_an_unknown_check_in_and_a_spoken_answer_find_no_offer(client, db, clock, auth) -> None:  # type: ignore[no-untyped-def]
    notification_id, device = _offered(client, db, clock, "en-US")
    check_in_id = _deliver(db, notification_id, device)["checkIn"]["checkInId"]
    missing = client.post(
        f"/v1/check-ins/{uuid.uuid4()}/answer", headers=auth, json={"choice": "conosciamoci"}
    )
    assert missing.status_code == 404
    from pisa.domain.errors import PisaError

    follow = client.app.state.runtime.follow
    with pytest.raises(PisaError) as voice:  # only the user's own tap takes the offer, never the model
        _call(client, follow.answer, check_in_id, "conosciamoci", "voice")
    assert voice.value.code == "not_found"
