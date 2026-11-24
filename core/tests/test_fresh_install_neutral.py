"""A fresh install is a neutral assistant (audit M1): with PISA_COUNTRY unset, US or DE, the worker's
initiative steps (the vault's starters and reminders, the connectors with the admin radar) put nothing Italian
or US-specific in the pool or the notifications, never ask about AIRE, and the residency counter counts days
for any country without assuming anyone's tax rules. With IT, Italy's features are there as before.
Synthetic data only."""

import json
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from pisa.api.app import create_app
from pisa.domain import skill_starters
from pisa.services import worker_steps
from pisa.services.runtime import Runtime

# What only an Italian or a US user's life would put there: taxes, registries, offices, Italian documents
OWNER_WORDS = ("aire", "f24", "redditi", "irpef", "imu", "isee", "fbar", "irs", "1040", "agenzia", "entrate",
               "commercialista", "comune", "consolato", "consulate", "cie", "carta d'identit", "dmv", "uscis",
               "green card", "roma", "rome", "new york")  # fmt: skip
# the skill starters built on a feed only Italy has
ITALY_ONLY_STARTERS = ("ztl_status", "nearest_pharmacy")
STEPS = ("vault_starters", "vault_reminders", "connectors")  # the vault's and the radar's initiative steps


def _offline(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("network disabled in tests", request=request)


def _app(settings: Any, fake_models: Any, clock: Any) -> Any:
    async def factory(s: Any) -> Runtime:
        return await Runtime.start(
            s, model_factory=fake_models, http_transport=httpx.MockTransport(_offline), clock=clock
        )

    return create_app(settings, runtime_factory=factory)


def _boot_and_run(client: TestClient, clock: Any) -> Any:
    """A passport that expires soon is kept (the app's own form), then the initiative steps run once."""
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    doc = client.portal.call(  # type: ignore[union-attr]
        lambda: runtime.autopilot.vault.add(
            {"kind": "passport", "expires_on": "2027-03-01"}, provenance="app", lang="en-US"
        )
    )
    jobs = {name: worker_steps.job_named(runtime, name) for name in STEPS}
    for name in STEPS:
        client.portal.call(jobs[name])  # type: ignore[union-attr]
    return runtime, doc


def _texts(db: psycopg.Connection) -> list[str]:
    rows = db.execute(
        "SELECT title || ' ' || coalesce(notes, '') || ' ' || coalesce(country, '') FROM tasks "
        "UNION ALL SELECT text FROM notifications UNION ALL SELECT what FROM obligations"
    ).fetchall()
    return [str(r[0]) for r in rows]


@pytest.mark.parametrize("country", [None, "US", "DE"])
def test_a_fresh_install_anywhere_is_neutral(  # type: ignore[no-untyped-def]
    app_settings, db: psycopg.Connection, fake_models: Any, clock: Any, country: str | None
) -> None:
    clock.now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    settings = app_settings.model_copy(update={"country": country, "language": "en", "timezone": "UTC"})
    with TestClient(_app(settings, fake_models, clock)) as client:
        runtime, doc = _boot_and_run(client, clock)
        assert doc["country"] == country  # the user's own country, or none: never Italy by default
        texts = _texts(db)
        assert any("passport" in t.lower() for t in texts)  # the reminder itself works
        leaked = [
            t
            for t in texts
            for w in OWNER_WORDS
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t.lower())
        ]
        assert not leaked, leaked
        # the radar keeps the passport's date (admin_documents), and no tax calendar
        assert db.execute("SELECT source FROM obligations").fetchall() == [("vault",)]
        assert db.execute(
            "SELECT count(*) FROM notifications WHERE path = 'vault.aire'"
        ).fetchone() == (0,)  # fmt: skip
        assert db.execute("SELECT value FROM app_settings WHERE key = 'vault.aire'").fetchone() is None
        # the residency counter counts days for any country, and checks no one's tax rules
        report = client.portal.call(runtime.residency.report, 2026)  # type: ignore[union-attr]
        assert report["by_rule"] == [] and set(report["days"]) == {"unknown"}
        client.portal.call(runtime.residency.correct, date(2026, 11, 2), ["DE"])  # type: ignore[union-attr]
        days = client.portal.call(runtime.residency.report, 2026)["days"]  # type: ignore[union-attr]
        assert days["DE"] == 1 and "IT" not in days and "US" not in days
        said = client.portal.call(runtime.residency.residency_text, 2026, "en-US")  # type: ignore[union-attr]
        assert said.startswith("In 2026: 1 days in Germany") and "Italy" not in said
        assert client.portal.call(runtime.residency.monthly_line, "en-US") is None  # type: ignore[union-attr]
        # no skill built on an Italy-only feed is offered (Rome's ZTL, Lazio's pharmacies)
        offers = runtime.conversations.deps.tools.public[1]
        for tool in ITALY_ONLY_STARTERS:
            assert client.portal.call(offers.offer, tool) is None, tool  # type: ignore[union-attr]
        assert not any(s.tool in ITALY_ONLY_STARTERS for s in skill_starters.offered(country=country))
        assert (
            skill_starters.named("ztl adesso", country=country) is None
            and skill_starters.named("farmacia vicina", country=country) is None
        )


def test_with_italy_the_italian_features_are_there(  # type: ignore[no-untyped-def]
    app_settings, db: psycopg.Connection, fake_models: Any, clock: Any
) -> None:
    clock.now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    with TestClient(_app(app_settings, fake_models, clock)) as client:  # PISA_COUNTRY=IT, Italian
        runtime, doc = _boot_and_run(client, clock)
        assert doc["country"] == "IT"
        aire = db.execute("SELECT text FROM notifications WHERE path = 'vault.aire'").fetchall()
        assert len(aire) == 1 and "AIRE" in aire[0][0]  # asked once, at the Italian passport's reminder
        # Italy's 183 days by default, said word for word; the US tests only when their setting names them
        report = client.portal.call(runtime.residency.report, 2026)  # type: ignore[union-attr]
        assert [r["rule"] for r in report["by_rule"]] == ["it_presence_183"]
        assert report["by_rule"][0]["note"] == "Italia: 0 su 183. È un conteggio, non una consulenza."
        assert {"IT", "unknown"} == set(report["days"])
        said = client.portal.call(runtime.residency.residency_text, 2026, "it-IT")  # type: ignore[union-attr]
        assert said.startswith("Nel 2026: 0 giorni in Italia, ") and "Italia: 0 su 183." in said
        assert "USA" not in said
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('residency_rules', %s)",
            (
                json.dumps(
                    [
                        {"rule": "it_presence_183"},
                        {"rule": "us_substantial_presence"},
                        {"rule": "us_closer_connection"},
                    ]
                ),
            ),
        )
        named = client.portal.call(runtime.residency.report, 2026)  # type: ignore[union-attr]
        assert [r["rule"] for r in named["by_rule"]] == [
            "it_presence_183", "us_substantial_presence", "us_closer_connection",
        ]  # fmt: skip
        said = client.portal.call(runtime.residency.residency_text, 2026, "it-IT")  # type: ignore[union-attr]
        assert said.startswith("Nel 2026: 0 giorni in Italia, 0 negli USA")
        # Rome's ZTL and Lazio's pharmacies are offered as skills in Italy
        offers = runtime.conversations.deps.tools.public[1]
        for tool in ITALY_ONLY_STARTERS:
            assert client.portal.call(offers.offer, tool) is not None, tool  # type: ignore[union-attr]


def test_aire_is_asked_only_about_an_italian_document(  # type: ignore[no-untyped-def]
    app_settings, db: psycopg.Connection, fake_models: Any, clock: Any
) -> None:
    clock.now = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    with TestClient(_app(app_settings, fake_models, clock)) as client:  # PISA_COUNTRY=IT
        runtime = client.app.state.runtime  # type: ignore[attr-defined]
        vault = runtime.autopilot.vault
        client.portal.call(  # type: ignore[union-attr]
            lambda: vault.add(
                {"kind": "passport", "country": "DE", "expires_on": "2027-03-01"},
                provenance="app",
                lang="it-IT",
            )
        )
        client.portal.call(vault.tick)  # type: ignore[union-attr]
        assert db.execute("SELECT count(*) FROM tasks").fetchone() == (1,)  # the reminder fired
        assert db.execute(
            "SELECT count(*) FROM notifications WHERE path = 'vault.aire'"
        ).fetchone() == (0,)  # fmt: skip
        assert client.portal.call(runtime.autopilot.aire_question, "passport", "DE", "it-IT") is None  # type: ignore[union-attr]
        assert client.portal.call(runtime.autopilot.aire_question, "passport", "IT", "it-IT") is not None  # type: ignore[union-attr]
