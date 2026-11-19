"""The voice gate on Postgres: their consent, prints and recordings sealed and deletable (erase family
`voice`), the API, an enrolment end to end with fake models and generated voices, and the talk opening with
the gate on only when it should, its instructions unchanged."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import psycopg
import pytest
from pydantic import SecretStr

from pisa.domain import voice_gate as gate
from pisa.domain.whisper import Kind
from pisa.infra.keys import KeyStore
from pisa.services import erase
from pisa.services import voice_id as vid
from pisa.services.talk import Thought
from tests import voice_synth as vs
from tests.conftest import FAKE_KEY, dsn, pair
from tests.test_live_talk import FakeLink, FakeTalker, _service
from tests.test_voice_id import tone_profile

RATE = 16_000


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


async def _pool(db: psycopg.Connection) -> Any:
    from pisa.db.pool import open_pool

    pool = await open_pool(dsn(db), max_size=4)
    await pool.wait()
    return pool


def _store_tone(
    db: psycopg.Connection, secrets_key: str, route: str = "airpods", consent: bool = True
) -> None:
    profile, cohort, _ = tone_profile()

    async def go() -> None:
        pool = await _pool(db)
        store = vid.VoiceStore(pool, KeyStore(SecretStr(secrets_key)))
        if consent:
            await store.consent("2026-11-08", None)
        await store.save_profile(
            route, "tone", "t", profile.prints, profile.thresholds, {"fa_per_min": 0.0}, 60.0, True
        )
        await vs.save_cohort(pool, "tone", [("it", c) for c in cohort])
        await store.save_recording(
            route, "A", vid.float_to_pcm(vs.say(vs.speaker(1), 2.0, 0)), [(0, 32000)], [False]
        )
        await pool.close()

    asyncio.run(go())


def test_the_script_consent_profile_relearn_and_delete_everything(client, db, auth, secrets_key) -> None:  # type: ignore[no-untyped-def]
    script = client.get("/v1/voice/enrolment/script?lang=en", headers=auth).json()
    assert [b["block"] for b in script["blocks"]] == ["A", "B", "C", "D", "E"] and len(script["test"]) == 6
    empty = client.get("/v1/voice/profile", headers=auth).json()
    assert empty == {"routes": [], "consent_at": None, "gate": "off"}
    assert client.post("/v1/voice/consent", headers=auth, json={"version": "2026-11-08"}).status_code == 201
    _store_tone(db, secrets_key, consent=False)
    state = client.get("/v1/voice/profile", headers=auth).json()
    assert state["consent_at"] and state["gate"] == "on"
    assert [(r["route"], r["ok"], r["samples_s"]) for r in state["routes"]] == [("airpods", True, 60.0)]
    # sealed at rest: neither the print's numbers nor the audio are readable in the row
    prints, pcm = db.execute("SELECT p.prints, r.pcm FROM voice_profiles p, voice_recordings r").fetchone()  # type: ignore[misc]
    assert bytes(prints).startswith(b"gAAAA") and b"vector" not in bytes(prints)
    assert bytes(pcm).startswith(b"gAAAA")
    assert client.delete("/v1/voice/profile/airpods", headers=auth).json() == {"deleted": 2}
    assert client.delete("/v1/voice/profile/airpods", headers=auth).status_code == 404
    _store_tone(db, secrets_key, consent=False)
    assert (
        client.delete("/v1/voice/profile", headers=auth).json()["deleted"] == 3
    )  # print, recording, consent
    assert client.get("/v1/voice/profile", headers=auth).json() == {
        "routes": [],
        "consent_at": None,
        "gate": "off",
    }


def test_no_consent_no_enrolment_and_no_cohort_says_so(client, db, auth, secrets_key) -> None:  # type: ignore[no-untyped-def]
    client.app.state.runtime.voice_id._models_factory = vs.fake_models  # type: ignore[attr-defined]
    body = {"sdp_offer": "v=0", "trigger": "press", "lang": "it-IT", "route": "airpods", "purpose": "enrol"}
    refused = client.post("/v1/talk/sessions", headers=auth, json=body)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "voice_consent_missing"
    client.post("/v1/voice/consent", headers=auth, json={"version": "1"})
    refused = client.post("/v1/talk/sessions", headers=auth, json=body)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "voice_cohort_missing"
    no_route = client.post("/v1/talk/sessions", headers=auth, json={**body, "route": None})
    assert no_route.status_code == 400


def test_the_voice_family_erases_his_voice_as_one(db, app_settings, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Consent, prints and recordings go together, by his first consent's date."""
    _store_tone(db, secrets_key)
    cutoff = date.today() - timedelta(days=2)

    async def go() -> dict[str, int]:
        async with await psycopg.AsyncConnection.connect(app_settings.database_url, autocommit=True) as conn:
            before = (await erase.count(conn, "voice", cutoff, app_settings.timezone))[0]
            await erase.delete(conn, "voice", cutoff, app_settings.timezone)
            return before

    db.execute("UPDATE voice_recordings SET created_at = now() - interval '10 days'")
    assert asyncio.run(go()) == {
        "voice_recordings": 0,
        "voice_profiles": 0,
        "voice_consents": 0,
    }  # consent new
    db.execute("UPDATE voice_consents SET created_at = now() - interval '10 days'")
    assert asyncio.run(go()) == {"voice_recordings": 1, "voice_profiles": 1, "voice_consents": 1}
    for table in ("voice_recordings", "voice_profiles", "voice_consents"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_a_recording_s_route_is_checked(db) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO voice_recordings (id, route, block, pcm, seconds) VALUES (gen_random_uuid(), "
            "'moon', "
            "'A', '\\x00', 1)"
        )


def _send(block: str, state: str) -> dict[str, Any]:
    return {"type": "enrol.block", "block": block, "state": state}


def test_an_enrolment_records_judges_and_builds_the_route_s_prints_and_thresholds(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    owner = vs.speaker(1)
    sent: list[dict[str, Any]] = []

    async def go() -> None:
        pool = await _pool(db)
        service = vid.VoiceIdService(pool, KeyStore(SecretStr(secrets_key)), vs.fake_models)
        await service.store.consent("2026-11-08", None)
        emb = vs.SpectralEmbedder()
        await vs.save_cohort(
            pool, "spectral", [("it", emb.embed(vs.say(vs.speaker(900 + i), 2.0, 0))) for i in range(40)]
        )
        await vs.save_babble(pool, [("it,fr", 4, vs.say(vs.speaker(700 + i), 8.0, 0)) for i in range(3)])
        e = await service.start_enrolment(uuid.uuid4(), "airpods", sent.append)
        assert not e.mini
        for block in ("A", "B", "C", "D"):
            await e.message(_send(block, "start"))
            for k in range(14 if block == "C" else 10):  # C: 30 s of free speech
                x = vs.sentence(owner, ord(block) * 100 + k, seconds=2.5, pad=0.5)
                e.audio(vid.float_to_pcm(x))
            await e.message(_send(block, "end"))
        e.audio(vid.float_to_pcm(np.zeros(RATE, dtype=np.float32)))  # outside a block: never kept
        await e.message({"type": "enrol.finish"})
        assert e.finished.is_set(), sent
        await pool.close()

    asyncio.run(go())
    quality = [m for m in sent if m["type"] == "enrol.quality"]
    assert [q["block"] for q in quality] == ["A", "B", "C", "D"] and all(q["ok"] for q in quality)
    (done,) = [m for m in sent if m["type"] == "enrol.done"]
    assert done["route"] == "airpods" and "owner_false_reject" in done and done["strangers_per_min"] <= 0.1
    # the merge: the user guard decides `ok` end to end (the fake models leave them above 20%)
    assert done["owner_false_reject"] > 0.2 and done["ok"] is False
    assert db.execute("SELECT ok FROM voice_profiles").fetchone() == (False,)
    blocks = db.execute("SELECT block, held_out FROM voice_recordings ORDER BY block").fetchall()
    assert [b for b, _ in blocks] == ["A", "B", "C", "D"] and all(
        len(h) // 5 == sum(h) for _, h in blocks
    )  # 20%
    model, calibration, bench = db.execute("SELECT model, calibration, bench FROM voice_profiles").fetchone()  # type: ignore[misc]
    assert (
        model == "spectral" and set(calibration["theta"]) == {"1.0", "1.5"} and "owner_false_reject" in bench
    )


def test_a_missing_block_is_named_at_finish_and_cancel_stores_nothing(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    sent: list[dict[str, Any]] = []

    async def go() -> None:
        pool = await _pool(db)
        service = vid.VoiceIdService(pool, KeyStore(SecretStr(secrets_key)), vs.fake_models)
        await service.store.consent("1", None)
        await vs.save_cohort(pool, "spectral", [("it", np.ones(80, dtype=np.float32))])
        e = await service.start_enrolment(uuid.uuid4(), "airpods", sent.append)
        await e.message(_send("A", "start"))
        e.audio(vid.float_to_pcm(vs.say(vs.speaker(1), 20.0, 0)))
        await e.message(_send("A", "end"))
        await e.message({"type": "enrol.finish"})
        await e.message({"type": "enrol.cancel"})
        assert e.outcome == "cancelled"
        await pool.close()

    asyncio.run(go())
    assert sent[-1] == {
        "type": "enrol.done",
        "route": "airpods",
        "ok": False,
        "reason": "missing",
        "missing": ["B", "C", "D"],
    }
    assert db.execute("SELECT count(*) FROM voice_recordings").fetchone() == (0,)


def _open(
    db: Any, secrets_key: str, route: str | None, *, switch: bool = True, now: datetime | None = None
) -> tuple[Any, Any]:
    talker = FakeTalker()
    make, service_for = _service(db, talker, now or datetime.now(UTC))
    _, _, models = tone_profile()
    device = uuid.UUID(pair(db)[0])

    async def go() -> tuple[Any, Any]:
        pool = await make()
        keys = KeyStore(SecretStr(secrets_key))
        async with pool.connection() as conn:
            await keys.set(conn, "xai", FAKE_KEY)
        service = service_for(pool, SecretStr(secrets_key))
        service._voice = vid.VoiceIdService(pool, keys, lambda: models, gate_on=switch)

        async def link_for(rate: int) -> Any:
            return FakeLink()

        async def think(words: str, trace: str, yes_by_code: bool) -> Thought:
            return Thought("Ok.", Kind.SAY)

        from pisa.services.talk import Opening

        session_id, _ = await service.open(
            device,
            trigger="press",
            lang="it-IT",
            link_for=link_for,
            think=think,
            opening=Opening(route=route),
        )
        run = service.runs[session_id]
        service.end(session_id, device)
        await asyncio.sleep(0.05)
        await pool.close()
        return talker.configs[-1], run

    return asyncio.run(go())


def test_the_gate_is_on_only_with_consent_an_enrolled_route_and_the_switch_and_the_prompt_never_changes(
    db, secrets_key
) -> None:  # type: ignore[no-untyped-def]
    _store_tone(db, secrets_key)
    # One clock for the sessions compared: the prompt ends on the minute ("Now: …, 15:05"), and two sessions
    # opened on the wall's clock either side of a minute's turn differed by it now and then
    now = datetime.now(UTC)
    on, run_on = _open(db, secrets_key, "airpods", now=now)
    assert on.client_activity and run_on.gate is not None
    other, run_other = _open(db, secrets_key, "speaker", now=now)  # a route with no prints: today's path
    assert not other.client_activity and run_other.gate is None
    off, run_off = _open(
        db, secrets_key, "airpods", switch=False, now=now
    )  # PISA_VOICE_GATE off: today's path
    assert not off.client_activity and run_off.gate is None
    assert len(on.instructions) == len(off.instructions) and on.instructions == off.instructions
    db.execute("DELETE FROM voice_consents")
    no_consent, _ = _open(db, secrets_key, "airpods")
    assert not no_consent.client_activity


def test_a_print_of_another_model_is_never_used(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    _store_tone(db, secrets_key)
    db.execute("UPDATE voice_profiles SET model_version = 'older'")
    cfg, run = _open(db, secrets_key, "airpods")
    assert not cfg.client_activity and run.gate is None
    assert gate.WINDOWS_S == (1.0, 1.5)


def test_a_route_that_failed_its_guard_never_gates(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """§1.4: a route not `ok` keeps its prints and Pisa listens as before."""
    _store_tone(db, secrets_key)
    db.execute("UPDATE voice_profiles SET ok = false")
    cfg, run = _open(db, secrets_key, "airpods")
    assert not cfg.client_activity and run.gate is None


def _enrol(db: Any, secrets_key: str, script: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def go() -> None:
        pool = await _pool(db)
        service = vid.VoiceIdService(pool, KeyStore(SecretStr(secrets_key)), vs.fake_models)
        await service.store.consent("1", None)
        await vs.save_cohort(pool, "spectral", [("it", np.ones(80, dtype=np.float32))])
        e = await service.start_enrolment(uuid.uuid4(), "airpods", sent.append)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def audio() -> Any:
            if False:
                yield b""
            await asyncio.Event().wait()

        async def close() -> None:
            sent.append({"type": "closed"})

        task = asyncio.create_task(vid.run_enrolment(e, audio, queue, close))
        await script(e, queue, service, db)
        await asyncio.wait_for(task, 10)
        await pool.close()

    asyncio.run(go())
    return sent


def test_a_failure_in_a_message_ends_the_enrolment_with_a_reason(db, secrets_key, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A review fix."""

    async def broken(self: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(vid.Enrolment, "_finish", broken)

    async def script(e: Any, queue: Any, service: Any, db: Any) -> None:
        queue.put_nowait({"type": "enrol.finish"})

    sent = _enrol(db, secrets_key, script)
    assert {"type": "enrol.done", "route": "airpods", "ok": False, "reason": "error"} in sent
    assert sent[-1] == {"type": "closed"}


def test_consent_withdrawn_before_finish_stores_nothing(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """Consent is checked again at finish."""

    async def script(e: Any, queue: Any, service: Any, db: Any) -> None:
        for block in ("A", "B", "C", "D"):
            e.blocks[block] = (np.zeros(RATE, dtype=np.float32), [(0, RATE)], [False])
        await service.store.delete_all()
        queue.put_nowait({"type": "enrol.finish"})

    sent = _enrol(db, secrets_key, script)
    assert {"type": "enrol.done", "route": "airpods", "ok": False, "reason": "consent"} in sent
    assert db.execute("SELECT count(*) FROM voice_recordings").fetchone() == (0,)
