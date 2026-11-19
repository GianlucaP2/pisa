"""A fresh install outside Italy starts: its Italy-only connectors arrive switched off ("off means off": the
row's switch and its state agree, as the connectors table requires), and the server answers discovery. An
install that moves out of Italy starts too, its Italian connectors switched off."""

from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from pisa.api.app import create_app
from pisa.domain import connectors as connector_rules
from pisa.services.runtime import Runtime

ITALIAN = sorted(s.id for s in connector_rules.REGISTRY if s.country == "IT")
ITALIAN_ON = sorted(s.id for s in connector_rules.REGISTRY if s.country == "IT" and s.default_enabled)


def _offline(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("network disabled in tests", request=request)


def _app(settings: Any, fake_models: Any, clock: Any) -> Any:
    async def factory(s: Any) -> Runtime:
        transport = httpx.MockTransport(_offline)
        return await Runtime.start(s, model_factory=fake_models, http_transport=transport, clock=clock)

    return create_app(settings, runtime_factory=factory)


def _states(db: psycopg.Connection, ids: list[str]) -> dict[str, tuple[bool, str]]:
    rows = db.execute("SELECT id, enabled, state FROM connectors WHERE id = ANY(%s)", (ids,)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@pytest.mark.parametrize("country", [None, "US", "DE"])
def test_a_fresh_install_outside_italy_starts_and_serves_discovery(  # type: ignore[no-untyped-def]
    app_settings, db: psycopg.Connection, fake_models: Any, clock: Any, country: str | None
) -> None:
    settings = app_settings.model_copy(update={"country": country, "language": "en"})
    assert db.execute("SELECT count(*) FROM connectors").fetchone() == (0,)  # a fresh database
    with TestClient(_app(settings, fake_models, clock)) as client:
        server = client.get("/v1/server")
        assert server.status_code == 200 and server.json()["readiness"]["ready"] is True
    assert ITALIAN and _states(db, ITALIAN) == dict.fromkeys(ITALIAN, (False, "off"))


def test_an_install_that_moves_out_of_italy_switches_its_italian_connectors_off(  # type: ignore[no-untyped-def]
    app_settings, db: psycopg.Connection, fake_models: Any, clock: Any
) -> None:
    assert ITALIAN_ON  # the case: an Italian connector on by default
    with TestClient(_app(app_settings, fake_models, clock)):  # installed in Italy
        pass
    assert all(enabled for enabled, _ in _states(db, ITALIAN_ON).values())
    moved = app_settings.model_copy(update={"country": "US", "language": "en"})
    with TestClient(_app(moved, fake_models, clock)) as client:  # PISA_COUNTRY changed
        assert client.get("/v1/server").status_code == 200
    assert _states(db, ITALIAN_ON) == dict.fromkeys(ITALIAN_ON, (False, "off"))
