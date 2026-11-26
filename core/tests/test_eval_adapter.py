"""The storyline eval's adapter (`scripts/eval/storylines_adapter.py`) feeds a dataset through the real
kernel: the cutter, the events call, the linker, the engine, the tasks, and the owner's chat words through the
chat tool. With `--fake` (a deterministic events model that answers with the labels' events) the auditor's
scorer (`scripts/eval/score.py`) must find no false close, no quote leakage, no unexplained transition and no
invented work. A few scenarios of the dataset (`tests/data/storylines_mini.jsonl`, synthetic): a sale with an
injected instruction and the owner's "I already shipped it", a booking cancelled in chat, a payment said in
chat, an answer by phone, an appointment moved out of order, two requests side by side."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import _admin_url  # pyright: ignore[reportPrivateUsage]

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "scripts" / "eval"
MINI = Path(__file__).parent / "data" / "storylines_mini.jsonl"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EVAL / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_eval_adapter_oracle_plumbing(tmp_path: Path) -> None:
    admin = _admin_url()
    if admin is None:
        pytest.skip("PISA_TEST_ADMIN_URL not set: skipping database tests")
    adapter, score = load("storylines_adapter"), load("score")
    out = tmp_path / "predictions.jsonl"
    args = argparse.Namespace(
        storylines=str(MINI), output=str(out), fake=True, model=None, admin_url=admin, ids=None, limit=None,
        keep_db=False,
    )  # fmt: skip
    assert adapter.asyncio.run(adapter.run(args)) == 0
    scenarios = [json.loads(line) for line in MINI.read_text().splitlines()]
    predictions = [json.loads(line) for line in out.read_text().splitlines()]
    assert [p["scenario_id"] for p in predictions] == [s["id"] for s in scenarios]
    report = score.score(scenarios, predictions)
    # the four targets: nothing closed that should not be, no owner's sentence made a request, every step
    # explained by an event, no task shown that the labels don't hold
    assert report["false_close"]["rate"] == 0.0 and report["quote_leakage"]["rate"] == 0.0
    assert report["unexplained_transitions"]["rate"] == 0.0 and report["invented_work"]["count"] == 0
    assert report["storyline_purity"]["value"] >= 0.95 and report["storyline_completeness"]["value"] >= 0.9
    assert report["next_action_accuracy"]["value"] >= 0.9
    assert report["missing_predictions"] == 0
    # the owner's chat words moved their storylines: each chat item is the cause of a step, or of a held one
    chat_ids = {i["id"] for s in scenarios for i in s["items"] if i["type"] == "chat"}
    stepped = {t["item_id"] for p in predictions for s in p["storylines"] for t in s["transitions"]}
    events = {e["item_id"] for p in predictions for e in p["events"]}
    assert chat_ids and chat_ids <= events and len(chat_ids & stepped) >= len(chat_ids) - 1
    assert all(isinstance(v, float) for p in predictions for v in p["cost_eur"].values())


def test_eval_adapter_stops_at_the_spending_cap(tmp_path: Path) -> None:
    """`--max-usd`: the run stops before the next scenario once the cap is spent, and what ran is written."""
    admin = _admin_url()
    if admin is None:
        pytest.skip("PISA_TEST_ADMIN_URL not set: skipping database tests")
    adapter = load("storylines_adapter")
    out = tmp_path / "predictions.jsonl"
    args = argparse.Namespace(
        storylines=str(MINI), output=str(out), fake=True, model=None, admin_url=admin, ids=None, limit=None,
        keep_db=False, max_usd=0.000001, usd_per_eur=1.1,
    )  # fmt: skip
    assert adapter.asyncio.run(adapter.run(args)) == 0
    written = out.read_text().splitlines()
    assert 1 <= len(written) < len(MINI.read_text().splitlines())  # the first ran, then the cap stopped it


def test_eval_adapter_runs_the_real_chat_path(tmp_path: Path) -> None:
    """§36 B crashed on the chat model's path (`result.usage()`, a property in pydantic-ai 2), which `--fake`
    never ran: `--fake-chat-model` sends the owner's chat items through the real chat path (the agent, the
    cards, the storyline_progress tool, the gateway) with a scripted model, and the four zeros still hold."""
    admin = _admin_url()
    if admin is None:
        pytest.skip("PISA_TEST_ADMIN_URL not set: skipping database tests")
    adapter, score = load("storylines_adapter"), load("score")
    out = tmp_path / "predictions.jsonl"
    args = argparse.Namespace(
        storylines=str(MINI), output=str(out), fake=True, model=None, admin_url=admin, ids=None, limit=None,
        keep_db=False, fake_chat_model=True,
    )  # fmt: skip
    assert adapter.asyncio.run(adapter.run(args)) == 0
    scenarios = [json.loads(line) for line in MINI.read_text().splitlines()]
    predictions = [json.loads(line) for line in out.read_text().splitlines()]
    report = score.score(scenarios, predictions)
    assert report["missing_predictions"] == 0
    for zero in ("false_close", "quote_leakage", "unexplained_transitions", "invented_work"):
        assert report[zero]["count"] == 0, zero
    chat_ids = {i["id"] for s in scenarios for i in s["items"] if i["type"] == "chat"}
    events = {e["item_id"] for p in predictions for e in p["events"]}
    assert chat_ids and chat_ids <= events  # the chat model called the real tool for each chat item
