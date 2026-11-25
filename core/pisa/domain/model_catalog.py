"""The model catalog's shipped data (`model_catalog.json` beside this module): what Pisa sets per model
and the default model ids of the voice connectors. Model ids are data, never code.

- `effort`: per provider, the reasoning effort Pisa asks of a model (Anthropic's `effort`, OpenAI's and
  xAI's `reasoning_effort`), keyed by model id; a dated or suffixed id (`<id>-…`) takes its model's. Only the
  models listed get one: others may refuse the parameter.
- `defaults`: per use (`talker`, `transcribe`, `speech`), the model a provider's connector uses unless the
  user chose another.

The operator can change any of it: the `PISA_MODEL_CATALOG` setting names a JSON file of the same shape,
handed over at start (`set_override`) and laid over the shipped data, per provider (an empty string there
removes the shipped value). No environment read here.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

SHIPPED = Path(__file__).with_name("model_catalog.json")
_override: Path | None = None  # the operator's file, from the settings at start
_talkers: dict[str, str] = {}  # the operator's talker models (PISA_TALKER_MODEL_GEMINI, ..._XAI), at start


def _lay(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        out[key] = _lay(out.get(key) or {}, value) if isinstance(value, dict) else value
    return out


def set_override(path: str | Path | None) -> None:
    """The operator's catalog file (`PISA_MODEL_CATALOG`), or None for the shipped data alone. Set once at
    start, before any model is looked up; what was read before is read again."""
    global _override
    _override = Path(path) if path else None
    data.cache_clear()


def set_talker_models(models: dict[str, str]) -> None:
    """The operator's model per talker (`PISA_TALKER_MODEL_GEMINI`, `PISA_TALKER_MODEL_XAI`; empty: the
    catalog's), laid over `defaults.talker`. Set once at start, like `set_override`."""
    global _talkers
    _talkers = {talker: model.strip() for talker, model in models.items() if model and model.strip()}
    data.cache_clear()


@cache
def data() -> dict[str, Any]:
    shipped: dict[str, Any] = json.loads(SHIPPED.read_text())
    laid = _lay(shipped, json.loads(_override.read_text())) if _override else shipped
    return _lay(laid, {"defaults": {"talker": _talkers}}) if _talkers else laid


def effort(provider: str, model_id: str) -> str | None:
    """The reasoning effort Pisa asks of this model, or None (the provider's own default)."""
    known: dict[str, str] = data().get("effort", {}).get(provider, {})
    found = next((e for m, e in known.items() if model_id == m or model_id.startswith(f"{m}-")), None)
    return found or None


def default_model(use: str, provider: str) -> str:
    """The model a connector uses for `use` with this provider unless the user chose another."""
    model = data().get("defaults", {}).get(use, {}).get(provider)
    if not model:
        raise LookupError(f"no default {use} model for {provider} in the model catalog")
    return str(model)
