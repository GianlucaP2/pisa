"""FL-1: the REST routes that reach a model or an embedding pass the same AI consent check as a run, the live
session and a talk (`services.disclosure.require`), before any AI call or queued ingestion: a device with no
recorded consent gets 409 `ai_consent_required`; with it, the route answers as before.

The routes: `POST /v1/learn/ask` (embeds the question, the answer's calls), `POST /v1/learn/sources` (the
background ingestion embeds the text), `POST /v1/briefings` (a model writes it) and
`POST /v1/places/{id}/research` (a model answers). Synthetic data only."""

import base64
import uuid
from typing import Any

import psycopg
import pytest

from pisa.domain.errors import PisaError
from tests.conftest import pair, pair_v2

CONSENT = "ai_consent_required"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def refused(response: Any) -> bool:
    return response.status_code == 409 and response.json()["error"]["code"] == CONSENT


class _Spy:
    """Stands in for a service's AI-reaching call: records that it ran."""

    def __init__(self, answer: Any = None, error: PisaError | None = None) -> None:
        self.calls = 0
        self._answer, self._error = answer, error

    async def __call__(self, *_: Any, **__: Any) -> Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._answer


def _spies(client: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, _Spy]:
    runtime = client.app.state.runtime
    spies = {
        "ask": _Spy(
            {
                "answer_id": uuid.uuid4(),
                "answer": "Le fonti non lo dicono.",
                "lang": "it",
                "rationale": False,
                "abstained": True,
                "lexical_only": True,
                "latency_ms": 1,
                "sentences": [],
                "passages": [],
            }
        ),
        "research": _Spy(error=PisaError("not_found", "No such place.", "Search again")),
    }
    monkeypatch.setattr(runtime.learn_ask, "ask", spies["ask"])
    monkeypatch.setattr(runtime.places, "research", spies["research"])
    return spies


PLAN = base64.b64encode(b"# Plan\nread the probes chapter").decode()


def test_no_consent_refuses_every_ai_route_before_any_call(
    client: Any, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _spies(client, monkeypatch)
    _, token = pair_v2(db, consent=False)
    asked = client.post("/v1/learn/ask", json={"question": "cosa mostra la Tabella 3?"}, headers=auth(token))
    assert refused(asked) and spies["ask"].calls == 0
    assert asked.json()["error"]["action"]  # what to do: allow it in the app, or `pisa consent`
    added = client.post("/v1/learn/sources", json={"items": ["2601.00001"]}, headers=auth(token))
    upload = client.post(
        "/v1/learn/sources",
        json={"file": {"name": "plan.md", "media_type": "text/markdown", "data": PLAN}},
        headers=auth(token),
    )
    assert refused(added) and refused(upload)
    assert db.execute("SELECT count(*) FROM learn_sources").fetchone() == (0,)  # nothing queued
    briefing = client.post("/v1/briefings", headers=auth(token))
    assert refused(briefing)
    assert db.execute("SELECT count(*) FROM briefings").fetchone() == (0,)  # nothing composed
    research = client.post(f"/v1/places/{uuid.uuid4()}/research", json={}, headers=auth(token))
    assert refused(research) and spies["research"].calls == 0


def test_with_consent_the_ai_routes_answer_as_before(
    client: Any, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _spies(client, monkeypatch)
    _, token = pair(db)
    asked = client.post("/v1/learn/ask", json={"question": "cosa mostra la Tabella 3?"}, headers=auth(token))
    assert asked.status_code == 200 and spies["ask"].calls == 1
    upload = client.post(
        "/v1/learn/sources",
        json={"file": {"name": "plan.md", "media_type": "text/markdown", "data": PLAN}},
        headers=auth(token),
    )
    assert upload.status_code == 202 and upload.json()[0]["citable"] is False
    client.portal.call(client.app.state.runtime.learn.settled)
    assert client.post("/v1/briefings", headers=auth(token)).status_code == 201
    research = client.post(f"/v1/places/{uuid.uuid4()}/research", json={}, headers=auth(token))
    assert research.status_code == 404 and spies["research"].calls == 1  # past the gate, to the service


def test_a_health_question_needs_the_health_consent_too(
    client: Any, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _spies(client, monkeypatch)
    _, token = pair_v2(db)  # their AI consent, without health
    health = client.post(
        "/v1/learn/ask", json={"question": "che dose di farmaco per la mia pressione?"}, headers=auth(token)
    )
    assert refused(health) and spies["ask"].calls == 0
    plain = client.post("/v1/learn/ask", json={"question": "cosa mostra la Tabella 3?"}, headers=auth(token))
    assert plain.status_code == 200 and spies["ask"].calls == 1


def test_a_disclosure_changed_since_the_consent_is_refused(
    client: Any, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _spies(client, monkeypatch)
    _, token = pair(db)
    db.execute("UPDATE devices SET ai_consent_revision = %s", ("0" * 32,))  # allowed another disclosure
    asked = client.post("/v1/learn/ask", json={"question": "cosa mostra la Tabella 3?"}, headers=auth(token))
    assert refused(asked) and spies["ask"].calls == 0
