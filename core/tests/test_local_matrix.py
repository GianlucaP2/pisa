"""The local-model matrix script's parsing (scripts/local-models/matrix.py), on canned wire bytes."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local-models" / "matrix.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("local_models_matrix", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load()


def _sse(*events: object) -> bytes:
    lines = [f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events]
    return "".join(lines).encode()


def _delta(**delta: object) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def test_sse_events_carry_the_time_their_bytes_arrived() -> None:
    body = _sse(_delta(content="Hel"), _delta(content="lo"), "[DONE]")
    first = len(_sse(_delta(content="Hel")))
    events = matrix.parse_sse(body, [(0.5, first), (0.9, len(body))])
    assert [t for t, _ in events] == [0.5, 0.9, 0.9]
    assert events[-1][1] == "[DONE]"


def test_streamed_text_reasoning_and_first_token() -> None:
    body = _sse(
        _delta(role="assistant", content=None),
        _delta(reasoning_content="add the tens"),
        _delta(content="42"),
        {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}},
        "[DONE]",
    )
    events = [(i * 0.1, e) for i, (_, e) in enumerate(matrix.parse_sse(body))]
    parts = matrix.stream_text_parts(events)
    assert parts["content"] == "42"
    assert parts["reasoning_content"] == "add the tens"
    assert parts["first_token_s"] == 0.1  # the reasoning delta counts as the first token
    assert parts["usage"] == {"prompt_tokens": 9, "completion_tokens": 3}
    assert parts["done_marker"]


def test_tool_call_deltas_in_pieces() -> None:
    body = _sse(
        _delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "get_weather", "arguments": ""}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"city": '}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '"Paris"}'}}]),
        _delta(tool_calls=[{"id": "c2", "function": {"name": "get_time", "arguments": '{"city": "Tokyo"}'}}]),
    )
    shape = matrix.stream_tool_deltas(matrix.parse_sse(body))
    # the last chunk has no index: it lands on call 0 and breaks its arguments, as a client assuming 0 would
    assert shape["calls"] == 1
    assert shape["missing_index_chunks"] == 1
    call = shape["per_call"]["0"]
    assert call["arg_chunks"] == 3
    assert call["distinct_ids"] == 2
    assert not call["args_json_valid"]


def test_tool_markup_and_think_tags_are_spotted_in_text() -> None:
    assert matrix.TOOL_TEXT_RE.search('<tool_call>\n{"name": "get_weather", "arguments": {}}\n</tool_call>')
    assert matrix.TOOL_TEXT_RE.search('{"name": "get_time", "arguments": {"city": "Rome"}}')
    assert not matrix.TOOL_TEXT_RE.search("It is sunny in Paris.")
    assert matrix.THINK_RE.search("<think>\nadd\n</think>\n42")
    assert not matrix.THINK_RE.search("I think it is 42.")


def test_shorten_cuts_strings_masks_the_key_and_summarizes_vectors() -> None:
    value = {"k": "sk-local-0000 " + "x" * 500, "v": [0.1] * 384, "n": [1, 2]}
    out = matrix.shorten(value, 100, "sk-local-0000")
    assert out["k"].startswith("<api-key> x") and out["k"].endswith("chars>")
    assert len(out["k"]) <= 100
    assert out["v"] == "<384 numbers>"
    assert out["n"] == [1, 2]


def test_longest_silence_is_the_widest_gap_from_the_send() -> None:
    ex = matrix.Exchange(
        "POST", "http://h/v1/chat/completions", None, chunk_times=[(30.0, 10), (134.8, 20), (135.0, 30)]
    )
    assert round(ex.longest_silence_s(), 1) == 104.8


def test_check_status_from_its_parts() -> None:
    check = matrix.Check("x", "x")
    check.add(matrix.Part("a", "pass", ""))
    check.add(matrix.Part("b", "info", ""))
    check.settle()
    assert check.status == "pass"
    check.add(matrix.Part("c", "fail", ""))
    check.settle()
    assert check.status == "partial"


def test_summary_table_merges_runs(tmp_path: Path) -> None:
    for fw, status in (("ollama", "pass"), ("llama.cpp", "fail")):
        report = {
            "framework": fw,
            "model_label": "m",
            "probe": {"caps": {"tools": status == "pass"}},
            "checks": [
                {
                    "id": "c",
                    "title": "single tool call",
                    "status": status,
                    "parts": [{"name": "raw", "status": status}],
                }
            ],
        }
        (tmp_path / f"{fw}.json").write_text(json.dumps(report))
    table = matrix.summarize(tmp_path)
    assert "| c. single tool call | **FAIL** (raw: FAIL) | **pass** |" in table
    assert "| tools | no | yes |" in table
