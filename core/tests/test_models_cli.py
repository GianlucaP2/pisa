"""No model slot has a default: a turn on an unset slot fails with `model_not_configured` naming it, and
`pisa models` lists, sets and finds the models."""

from collections.abc import Iterator

import psycopg
import pytest

from pisa.domain.ai import SLOTS
from tests.conftest import seed_catalog, set_key
from tests.live_app import live


def _clear_slots(db: psycopg.Connection) -> None:
    db.execute("UPDATE model_slots SET model_ref = NULL, fallback_ref = NULL")


def test_a_fresh_pisa_has_no_model_in_any_slot(test_database_url: str) -> None:
    from pisa.db.migrate import load_migrations

    baseline = load_migrations()[0].sql
    seeded = baseline.split("INSERT INTO model_slots", 1)[1].split(";", 1)[0]
    assert "NULL" in seeded and ":" not in seeded.split("VALUES", 1)[1]  # no provider:model anywhere
    assert not hasattr(SLOTS[0], "default_model")


def test_a_turn_on_an_unset_slot_says_which_model_to_choose(client, db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    set_key(db, secrets_key)
    _clear_slots(db)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        app.say("ciao", lang="it-IT", source="typed")
        err = app.next()
    assert err["type"] == "error" and err["code"] == "model_not_configured"
    assert "chat.conversation" in err["message"]
    # a typed turn: the slot is named as the conversation, never as a voice one
    assert err["message"] == "No model is chosen for the conversation (chat.conversation)."
    assert "Choose a model for chat.conversation" in err["action"]
    assert "pisa models set chat.conversation <provider:model>" in err["action"]


@pytest.fixture
def cli(test_database_url: str, secrets_key: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pisa.settings import get_settings

    monkeypatch.setenv("PISA_DATABASE_URL", test_database_url)
    monkeypatch.setenv("PISA_SECRETS_KEY", secrets_key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_models_list_shows_every_slot_and_what_is_unset(
    cli: None, db: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    from pisa import cli as pisa_cli

    _clear_slots(db)
    db.execute("UPDATE model_slots SET model_ref = 'openai:gpt-test' WHERE id = 'intake.triage'")
    assert pisa_cli.main(["models"]) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line and not line.startswith(" ")}
    assert {s.id for s in SLOTS} <= set(lines)
    assert "(not set)" in lines["chat.conversation"] and "openai:gpt-test" in lines["intake.triage"]
    assert "pisa models set <slot> <provider:model>" in out
    assert pisa_cli.main(["models", "list"]) == 0


def test_models_set_checks_the_model_against_the_slot(
    cli: None, db: psycopg.Connection, secrets_key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from pisa import cli as pisa_cli

    _clear_slots(db)
    set_key(db, secrets_key)  # a slot points only at a provider whose key was set
    seed_catalog(
        db,
        [
            {"ref": "openai:gpt-test", "capabilities": ["tools", "structured_output"]},
            {"ref": "openai:plain-test", "capabilities": []},
        ],
    )
    assert pisa_cli.main(["models", "set", "chat.conversation", "openai:gpt-test"]) == 0
    assert "chat.conversation now uses openai:gpt-test." in capsys.readouterr().out
    row = db.execute("SELECT model_ref FROM model_slots WHERE id = 'chat.conversation'").fetchone()
    assert row == ("openai:gpt-test",)

    assert pisa_cli.main(["models", "set", "chat.conversation", "openai:plain-test"]) == 1
    assert "lacks tools" in capsys.readouterr().err
    assert pisa_cli.main(["models", "set", "no.such.slot", "openai:gpt-test"]) == 1
    assert "There is no slot 'no.such.slot'" in capsys.readouterr().err


def test_models_available_lists_the_models_of_providers_with_a_key(
    cli: None, db: psycopg.Connection, secrets_key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from pisa import cli as pisa_cli

    seed_catalog(
        db,
        [
            {"ref": "openai:gpt-test", "capabilities": ["tools", "structured_output"]},
            {"ref": "openai:plain-test", "capabilities": []},
            {"ref": "anthropic:claude-test", "capabilities": ["tools"]},
        ],
    )
    assert pisa_cli.main(["models", "available"]) == 0
    assert "No model is available yet" in capsys.readouterr().out  # no key yet
    set_key(db, secrets_key)
    assert pisa_cli.main(["models", "available"]) == 0
    out = capsys.readouterr().out
    assert "openai:" in out and "openai:gpt-test" in out and "openai:plain-test" in out
    assert "anthropic" not in out  # no key for it
    assert pisa_cli.main(["models", "available", "--slot", "chat.conversation"]) == 0
    out = capsys.readouterr().out
    assert "openai:gpt-test" in out and "openai:plain-test" not in out  # it lacks tools
