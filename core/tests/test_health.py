import importlib.metadata
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from pisa.api.app import create_app
from pisa.settings import Settings


def test_health_is_ok_without_database() -> None:
    app = create_app(Settings(timezone="Europe/Rome", database_url="postgresql://nobody@127.0.0.1:1/none"))
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_the_package_version() -> None:
    app = create_app(Settings(timezone="Europe/Rome", database_url="postgresql://nobody@127.0.0.1:1/none"))
    reported = TestClient(app).get("/health").json()["version"]
    in_pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())[
        "project"
    ]["version"]
    assert reported == importlib.metadata.version("pisa-assistant") == in_pyproject
    assert app.version == reported


def test_readyz_names_the_failing_check_when_database_is_down() -> None:
    app = create_app(Settings(timezone="Europe/Rome", database_url="postgresql://nobody@127.0.0.1:1/none"))
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"].startswith("unreachable")


def test_readyz_ok_on_migrated_database(test_database_url: str) -> None:
    app = create_app(Settings(timezone="Europe/Rome", database_url=test_database_url))
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200, response.json()
    assert response.json()["checks"] == {"database": "ok", "schema": "ok"}


def test_without_apns_pisa_is_ready_with_push_off(test_database_url: str, db) -> None:  # type: ignore[no-untyped-def]
    plain = Settings(_env_file=None, timezone="UTC", database_url=test_database_url)  # type: ignore[call-arg]
    assert plain.apns_team_id is None and plain.apns_topic is None
    response = TestClient(create_app(plain)).get("/readyz")
    assert response.status_code == 200 and response.json()["push"] == "off"
    apple = Settings(
        timezone="UTC",
        database_url=test_database_url,
        apns_team_id="EXAMPLETEAM",
        apns_topic="com.example.pisa",
    )
    assert TestClient(create_app(apple)).get("/readyz").json()["push"] == "off"  # no key stored yet
    db.execute("INSERT INTO server_secrets (name, ciphertext, meta) VALUES ('apns', 'x', '{}')")
    assert TestClient(create_app(apple)).get("/readyz").json()["push"] == "on"
    assert TestClient(create_app(plain)).get("/readyz").json()["push"] == "off"  # no team id, no topic
