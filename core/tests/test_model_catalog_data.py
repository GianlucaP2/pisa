"""The model catalog's shipped data: per-model effort and the connectors' default models are data the operator
can change, never ids in code. Synthetic ids only."""

import json
from pathlib import Path

import pytest

from pisa.domain import model_catalog
from pisa.settings import Settings


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    shipped = {
        "effort": {"acme": {"acme-large": "low"}},
        "defaults": {"talker": {"acme": "acme-voice"}, "speech": {"acme": "acme-tts"}},
    }
    path = tmp_path / "shipped.json"
    path.write_text(json.dumps(shipped))
    monkeypatch.setattr(model_catalog, "SHIPPED", path)
    model_catalog.set_override(None)
    yield tmp_path
    model_catalog.set_override(None)


def test_one_lookup_for_every_provider_s_effort(catalog: Path) -> None:
    assert model_catalog.effort("acme", "acme-large") == "low"
    assert model_catalog.effort("acme", "acme-large-2026-10-01") == "low"  # a dated id takes its model's
    assert model_catalog.effort("acme", "acme-largest") is None  # another model, not a prefix of a word
    assert model_catalog.effort("other", "acme-large") is None  # per provider
    assert model_catalog.default_model("talker", "acme") == "acme-voice"
    with pytest.raises(LookupError):
        model_catalog.default_model("transcribe", "acme")


def test_the_operator_lays_their_own_file_over_the_shipped_data(catalog: Path) -> None:
    mine = catalog / "mine.json"
    mine.write_text(json.dumps({"effort": {"acme": {"acme-large": "", "acme-small": "high"}},
                                "defaults": {"speech": {"acme": "acme-tts-2"}}}))  # fmt: skip
    model_catalog.set_override(str(mine))  # the PISA_MODEL_CATALOG setting, handed over at start
    assert model_catalog.effort("acme", "acme-large") is None  # removed
    assert model_catalog.effort("acme", "acme-small") == "high"
    assert model_catalog.default_model("speech", "acme") == "acme-tts-2"
    assert model_catalog.default_model("talker", "acme") == "acme-voice"  # the rest stays shipped


def test_the_shipped_file_names_every_connector_s_default() -> None:
    model_catalog.set_override(None)
    for use, provider in (("talker", "xai"), ("talker", "gemini"), ("transcribe", "xai"),
                          ("speech", "openai"), ("speech", "elevenlabs")):  # fmt: skip
        assert model_catalog.default_model(use, provider)


def test_the_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PISA_TIMEZONE", "Europe/London")
    monkeypatch.setenv("PISA_MODEL_CATALOG", "/etc/pisa/models.json")
    assert Settings(_env_file=None).model_catalog == "/etc/pisa/models.json"  # type: ignore[call-arg]
