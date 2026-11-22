# ruff: noqa: E501
"""Pisa draws natively: diagram, chart and timeline blocks, laid out by
CORE's code, every number traced to this turn's results, a tap on an element a read-only question. Every word
here is synthetic."""

import asyncio
import json
import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from pisa.brain.present_tools import Presenter, SheetBlockIn, Shown, for_phone, sheet_basis, sheet_blocks
from pisa.domain import decisions, visual
from pisa.domain import layout as layouts
from pisa.domain.sheet import SheetData
from tests.virtual_time import run_talk

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def _diagram(**extra: Any) -> dict[str, Any]:
    """How a model talks to a database, as the model writes it (its own ids)."""
    block: dict[str, Any] = {
        "type": "diagram", "style": "flow", "direction": "down",
        "nodes": [
            {"id": "app", "label": "App"}, {"id": "llm", "label": "Modello", "group": "ai"},
            {"id": "tool", "label": "Chiamata di tool", "group": "ai"},
            {"id": "pool", "label": "Connection pool", "group": "db", "shape": "box"},
            {"id": "pg", "label": "Postgres", "group": "db", "shape": "store"},
            {"id": "user", "label": "Utente", "shape": "actor"},
        ],
        "edges": [
            {"from": "user", "to": "app"}, {"from": "app", "to": "llm", "label": "domanda"},
            {"from": "llm", "to": "tool"}, {"from": "tool", "to": "pool", "label": "SQL"},
            {"from": "pool", "to": "pg"}, {"from": "pg", "to": "llm", "style": "dashed", "label": "righe"},
        ],
        "groups": [{"id": "ai", "label": "Modello"}, {"id": "db", "label": "Database"}],
    }  # fmt: skip
    return block | extra


def _laid(block: dict[str, Any]) -> dict[str, Any]:
    (built,), _ = sheet_blocks([block], [], results=[], lang="it-IT")
    return built


def _overlap(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def _on_border(point: list[float], box: dict[str, float]) -> bool:
    x, y = point
    inside = (
        box["x"] - 0.11 <= x <= box["x"] + box["w"] + 0.11
        and box["y"] - 0.11 <= y <= box["y"] + box["h"] + 0.11
    )
    edge = min(
        abs(x - box["x"]), abs(x - box["x"] - box["w"]), abs(y - box["y"]), abs(y - box["y"] - box["h"])
    )
    return inside and edge <= 0.11


# --- 1. the domain: the three blocks, strict -------------------------------------------------------------


def test_1_the_blocks_are_strict_ids_unique_and_resolved() -> None:
    good = _laid(_diagram())
    SheetData.model_validate({"title": "x", "blocks": [good]})
    for broken in (
        good | {"extra": 1},  # unknown field
        good | {"nodes": [*good["nodes"], dict(good["nodes"][0])]},  # a duplicate id
        good | {"edges": [*good["edges"], {"from": "n1", "to": "n24", "style": "solid", "arrow": "end"}]},
        good | {"nodes": [good["nodes"][0] | {"group": "g6"}, *good["nodes"][1:]]},  # an unknown group
        good | {"layout": good["layout"] | {"edges": []}},  # the layout misses an edge
    ):
        with pytest.raises(ValidationError):
            SheetData.model_validate({"title": "x", "blocks": [broken]})
    with pytest.raises(ValidationError, match="at most 2 drawing"):
        SheetData.model_validate({"title": "x", "blocks": [good, good, good]})
    chart = {
        "type": "chart",
        "mark": "pie",
        "x": {"label": "a", "kind": "category"},
        "y": {"label": "b"},
        "series": [
            {"id": "s1", "name": "x", "points": [{"id": f"p{n}", "x": f"c{n}", "y": n} for n in range(1, 10)]}
        ],
    }
    with pytest.raises(ValidationError, match="a pie"):
        SheetData.model_validate({"title": "x", "blocks": [chart]})
    timeline = {"type": "timeline", "items": [{"id": "t1", "at": "2026-11-12", "label": "b"},
                                              {"id": "t2", "at": "2026-11-10", "label": "a"}]}  # fmt: skip
    with pytest.raises(ValidationError, match="sorted"):
        SheetData.model_validate({"title": "x", "blocks": [timeline]})
    chart["mark"], chart["series"][0]["points"] = "bar", chart["series"][0]["points"][:3]
    with pytest.raises(ValidationError, match="never Pisa's knowledge"):
        SheetData.model_validate({"title": "x", "blocks": [chart], "basis": "knowledge"})


# --- 2. the layout ------------------------------------------------------------------------------------------


def _random_graph(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)  # noqa: S311  (a fixed graph, not a secret)
    nodes = [{"id": f"n{i}", "label": f"Nodo {i}", "shape": "box"} | ({"group": f"g{i % 3 + 1}"} if i % 4 == 0 else {})
             for i in range(1, 25)]  # fmt: skip
    edges = []
    while len(edges) < 40:
        a, b = rng.sample(range(1, 25), 2)
        edges.append({"from": f"n{a}", "to": f"n{b}", "style": "solid", "arrow": "end"})
    groups = [{"id": f"g{i}", "label": f"Gruppo {i}"} for i in range(1, 4)]
    return {"style": "flow", "direction": "down", "nodes": nodes, "edges": edges, "groups": groups}


@pytest.mark.parametrize("style", ["flow", "graph", "layers"])
@pytest.mark.parametrize("direction", ["down", "right"])
def test_2_the_layout_is_deterministic_never_overlaps_and_edges_touch_their_boxes(
    style: str, direction: str
) -> None:
    diagram: Any
    for diagram in (
        _laid(_diagram(style=style, direction=direction)),
        _random_graph(7) | {"style": style, "direction": direction},
    ):
        laid = layouts.layout(diagram)
        assert json.dumps(layouts.layout(json.loads(json.dumps(diagram)))) == json.dumps(
            laid
        )  # byte-identical
        boxes = list(laid["nodes"].values())
        assert not any(_overlap(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1 :])
        for gid, box in laid.get("groups", {}).items():  # a group's box holds no other node
            assert not any(
                _overlap(box, laid["nodes"][n["id"]]) for n in diagram["nodes"] if n.get("group") != gid
            )
        for edge, path in zip(diagram["edges"], laid["edges"], strict=True):
            assert _on_border(path["points"][0], laid["nodes"][edge["from"]]), (edge, path)
            assert _on_border(path["points"][-1], laid["nodes"][edge["to"]]), (edge, path)


def test_2_a_24_node_40_edge_graph_lays_out_in_under_50_ms() -> None:
    graph = _random_graph(3)
    layouts.layout(graph)  # warm
    # The layout's own CPU time: a busy machine stretches the wall clock, not the work (the budget is the work's)
    started = time.thread_time()
    layouts.layout(graph)
    assert (time.thread_time() - started) * 1000 < 50


def test_2_node_sizes_come_from_the_label_at_the_phone_s_metrics() -> None:
    short, long_ = (
        layouts.node_size("DB", "box"),
        layouts.node_size("Un'etichetta di quaranta caratteri tonda", "box"),
    )
    assert short is not None and long_ is not None
    assert short == (layouts.NODE_MIN_W, 39.0) and long_[1] > short[1]  # wraps to more lines
    assert layouts.LABEL_PT == 15 and layouts.SECONDARY_PT == 13


# --- 3. the render check -----------------------------------------------------------------------------------


def test_3_too_wide_scrolls_and_a_label_that_can_t_show_whole_drops_the_diagram() -> None:
    wide = _diagram(direction="right", nodes=[{"id": f"x{n}", "label": f"Passo {n}"} for n in range(8)],
                    edges=[{"from": f"x{n}", "to": f"x{n + 1}"} for n in range(7)], groups=[])  # fmt: skip
    # 8 steps asked left to right go top to bottom on a phone, whole
    assert _laid(wide)["direction"] == "down" and "scroll" not in _laid(wide)
    grid = _diagram(nodes=[{"id": f"x{c}{n}", "label": f"Passo lungo numero {c}{n}"} for c in range(4) for n in range(5)],
                    edges=[{"from": f"x{c}{n}", "to": f"x{c}{n + 1}"} for c in range(4) for n in range(4)],
                    groups=[])  # fmt: skip
    assert _laid(grid)["scroll"] is True  # wider than a phone at 0.6 either way: the phone pans it
    assert "scroll" not in _laid(_diagram())
    cut = _diagram(nodes=[{"id": "a", "label": "Supercalifragilistichespiralidoso"}, {"id": "b", "label": "B"}],
                   edges=[{"from": "a", "to": "b"}], groups=[])  # fmt: skip
    built, dropped = sheet_blocks([cut, {"type": "text", "text": "ok"}], [], results=[], lang="it-IT")
    assert [b["type"] for b in built] == ["text"] and any("doesn't fit" in d for d in dropped)


def test_3_the_model_never_writes_the_layout_and_ids_are_numbered_by_code() -> None:
    made_up = {"width": 1, "height": 1, "nodes": {}, "edges": []}
    block = SheetBlockIn.model_validate(_diagram() | {"layout": made_up})  # a model-written layout: ignored
    (built,), _ = sheet_blocks([block], [], results=[], lang="it-IT")
    assert built["layout"] != made_up and set(built["layout"]["nodes"]) == {f"n{i}" for i in range(1, 7)}
    assert built["edges"][0] == {"from": "n6", "to": "n1", "style": "solid", "arrow": "end"}
    assert {g["id"] for g in built["groups"]} == {"g1", "g2"}


# --- 4. the trace check -----------------------------------------------------------------------------

WEATHER = (  # get_weather's recorded result for three days (describe_weather's lines)
    "Oggi, 2026-11-10: pioggia debole, 9.2 to 12.6 °C, rain chance 80% (4.2 mm), wind up to 25 km/h.\n"
    "Domani, 2026-11-11: poco nuvoloso, 11 to 16 °C, rain chance 10% (0 mm), wind up to 12 km/h.\n"
    "Giovedì, 2026-11-12: poco nuvoloso, 10.4 to 15.1 °C, rain chance 10% (0 mm), wind up to 12 km/h."
)


def _chart(*ys: float, derive: str | None = None) -> dict[str, Any]:
    days = ["2026-11-10", "2026-11-11", "2026-11-12"]
    series: dict[str, Any] = {
        "name": "Massime",
        "points": [{"x": d, "y": y} for d, y in zip(days, ys, strict=True)],
    }
    if derive:
        series["derive"] = derive
    return {"type": "chart", "mark": "line", "x": {"label": "Giorno", "kind": "time"},
            "y": {"label": "Massima", "unit": "°C"}, "series": [series]}  # fmt: skip


def test_4_a_chart_over_get_weather_s_result_passes_and_one_invented_point_drops_its_series() -> None:
    (chart,), dropped = sheet_blocks([_chart(12.6, 16, 15.1)], [], results=[WEATHER], lang="it-IT")
    assert [p["y"] for p in chart["series"][0]["points"]] == [12.6, 16, 15.1] and not dropped
    assert [p["id"] for p in chart["series"][0]["points"]] == ["p1", "p2", "p3"] and chart["series"][0][
        "id"
    ] == "s1"
    built, dropped = sheet_blocks(
        [_chart(12.6, 21, 15.1)], [], results=[WEATHER], lang="it-IT"
    )  # 21 °C: no result says it
    assert built == [] and any("not in this turn's results" in d for d in dropped)


def test_4_a_derive_mean_passes_and_the_model_never_writes_the_result() -> None:
    stat = {"type": "stat", "label": "Media", "derive": "mean", "of": ["s1/p1", "s1/p2", "s1/p3"], "unit": "°C",
            "value": "99"}  # fmt: skip
    built, dropped = sheet_blocks([_chart(12.6, 16, 15.1), stat], [], results=[WEATHER], lang="it-IT")
    assert built[1] == {"type": "stat", "label": "Media", "value": "14,57", "unit": "°C"} and not dropped
    untraced = stat | {"of": ["s1/p1", "s1/p9"]}
    _, dropped = sheet_blocks([_chart(12.6, 16, 15.1), untraced], [], results=[WEATHER], lang="it-IT")
    assert any("derive needs traced" in d for d in dropped)
    (series,), _ = sheet_blocks([_chart(12.6, 16, 15.1, derive="delta")], [], results=[WEATHER], lang="it-IT")
    assert [p["y"] for p in series["series"][0]["points"]] == [3.4, -0.9]  # computed by CORE


def test_4_numbers_are_read_the_italian_and_the_english_way() -> None:
    corpus = visual.Corpus(["Totale 1.234,5 € e 1,234.5 $; 45% di 1.234; 12,5 mm"])
    for shown in (1234.5, "1.234,5 €", 0.45, 45, 1234, 12.5, "12,5 mm", 13, 1.2):  # 13: 12,5 shown whole
        assert visual.traces(shown, corpus), shown
    for shown in (1234.6, 46, "14 mm", 12):
        assert not visual.traces(shown, corpus), shown
    moments = visual.Corpus(["Oggi, 2026-11-10: 09:00 sereno, 12:30 nuvoloso, 2026"])
    assert not any(visual.traces(n, moments) for n in (9, 12, 30, 11, 10)) and visual.traces(2026, moments)


def test_4_an_untraced_stat_or_table_cell_drops_its_block() -> None:
    blocks = [
        {"type": "stat", "label": "Massima", "value": "12,6 °C"},
        {"type": "stat", "label": "Minima", "value": "3 °C"},  # no result says 3
        {"type": "table", "columns": ["Giorno", "Max"], "rows": [["oggi", 12.6], ["domani", 16]]},
        {"type": "table", "columns": ["Giorno", "Max"], "rows": [["oggi", 12.6], ["domani", "19 °C"]]},
    ]
    built, dropped = sheet_blocks(blocks, [], results=[WEATHER], lang="it-IT")
    # An untraced cell is emptied, the rest of the table stays
    assert [b.get("value") or b.get("rows") for b in built] == [
        "12,6 °C",
        [["oggi", 12.6], ["domani", 16]],
        [["oggi", 12.6], ["domani", ""]],
    ]
    assert len(dropped) == 2


def test_4_basis_by_code() -> None:
    drawn = [_laid(_diagram())]
    assert sheet_basis(drawn, []) == "knowledge"  # nothing read, no number: Pisa's own knowledge
    (chart,), _ = sheet_blocks([_chart(12.6, 16, 15.1)], [], results=[WEATHER], lang="it-IT")
    assert sheet_basis([chart], [WEATHER]) is None  # every value traced: tools (left out)
    assert sheet_basis([*drawn, chart], [WEATHER]) == "mixed"  # a knowledge diagram beside traced data
    from_tools = _laid(_diagram(nodes=[{"id": "a", "label": "Oggi"}, {"id": "b", "label": "Domani"}],
                                edges=[{"from": "a", "to": "b"}], groups=[]))  # fmt: skip
    assert sheet_basis([from_tools], [WEATHER]) is None  # its labels are the results'


# --- 5. show_sheet: replaces, the fallback ------------------------------------------------------------------


class Phone:
    kinds = frozenset({"sheet"})

    def __init__(self, blocks: frozenset[str] | None) -> None:
        self.blocks = blocks
        self.sent: list[dict[str, Any]] = []

    async def present(self, message: dict[str, Any]) -> tuple[str, str] | None:
        self.sent.append(message)
        return ("shown", "phone")

    async def focus(self, present_id: str, object_id: str, highlight: str | None) -> tuple[str, str] | None:
        return ("shown", "phone")

    async def close(self, object_id: str) -> bool:
        return True

    async def card(self, kind: str, title: str, lines: list[str]) -> bool:
        return True


def _tools(presenter: Presenter) -> dict[str, Any]:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.present_tools import add_present_tools

    agent: Agent[None, str] = Agent(TestModel())
    add_present_tools(agent, lambda: presenter)
    return {n: t.function for n, t in agent._function_toolset.tools.items()}


def _presenter(
    phone: Phone, results: list[str] | None = None, objects: dict[str, Shown] | None = None
) -> Presenter:
    clock = iter(NOW + timedelta(seconds=n) for n in range(100))
    return Presenter(device=phone, words="come fa un modello a parlare con un database?", lang="it-IT",
                     today=NOW.date(), objects=objects if objects is not None else {},
                     results=lambda: list(results or []), corpus=lambda: list(results or []),
                     clock=lambda: next(clock))  # fmt: skip


def test_5_replaces_redraws_the_same_object_and_a_wrong_id_is_not_shown() -> None:
    with decisions.collecting():
        phone = Phone(frozenset({"diagram", "chart", "timeline"}))
        phone.width = 1300.0  # type: ignore[attr-defined]  # a wide surface: the direction asked stays
        objects: dict[str, Shown] = {}
        tools = _tools(_presenter(phone, objects=objects))
        said = asyncio.run(
            tools["show_sheet"](title="Modello e database", blocks=[SheetBlockIn.model_validate(_diagram())])
        )
        assert said.startswith("Shown") and "Say one sentence" in said
        first = phone.sent[-1]["object"]
        assert first["data"]["basis"] == "knowledge" and first["data"]["lang"] == "it-IT"
        side = _diagram(direction="right")
        again = asyncio.run(tools["show_sheet"](title="Dal lato del database", blocks=[SheetBlockIn.model_validate(side)],
                                                replaces=first["object_id"]))  # fmt: skip
        second = phone.sent[-1]["object"]
        assert again.startswith("Shown") and second["object_id"] == first["object_id"]
        assert second["revision"] > first["revision"] and second["data"]["blocks"][0]["direction"] == "right"
        wrong = asyncio.run(tools["show_sheet"](title="x", blocks=[SheetBlockIn(type="text", text="a")],
                                                replaces=f"sheet:{uuid.uuid4()}"))  # fmt: skip
        assert wrong.startswith("Not shown") and len(phone.sent) == 2


def test_7_a_phone_that_doesn_t_list_the_block_gets_a_table_or_a_list() -> None:
    with decisions.collecting():
        timeline = {"type": "timeline", "items": [{"id": "t1", "at": "2026-11-10T09:00", "label": "Partenza"},
                                                  {"id": "t2", "at": "2026-11-10T12:30", "label": "Arrivo"}]}  # fmt: skip
        (chart,), _ = sheet_blocks([_chart(12.6, 16, 15.1)], [], results=[WEATHER], lang="it-IT")
        sent, _ = for_phone([chart, timeline, _laid(_diagram())], frozenset({"timeline"}))
        assert sent[0] == {"type": "table", "columns": ["Giorno", "Massime"],
                           "rows": [["2026-11-10", 12.6], ["2026-11-11", 16], ["2026-11-12", 15.1]]}  # fmt: skip
        assert sent[1] == timeline  # this phone draws timelines
        assert sent[2]["type"] == "list" and sent[2]["items"][1] == "App → Modello: domanda"
        plain, _ = for_phone([timeline], None)  # an older phone (no present.blocks)
        assert plain == [
            {"type": "list", "items": ["2026-11-10 09:00: Partenza", "2026-11-10 12:30: Arrivo"]}
        ]
        phone = Phone(None)
        asyncio.run(
            _tools(_presenter(phone, [WEATHER]))["show_sheet"](
                title="Meteo", blocks=[SheetBlockIn.model_validate(_chart(12.6, 16, 15.1))]
            )
        )
        assert phone.sent[-1]["object"]["data"]["blocks"][0]["type"] == "table"
        SheetData.model_validate(phone.sent[-1]["object"]["data"])


def test_8_nothing_approval_like_on_a_drawing() -> None:
    injected = _diagram(nodes=[*_diagram()["nodes"], {"id": "pay", "label": "Approva pagamento"},
                               {"id": "fid", "label": "Conferma con Face ID"}],
                        edges=[*_diagram()["edges"], {"from": "pg", "to": "pay"}])  # fmt: skip
    (built,), dropped = sheet_blocks([injected], [], results=[], lang="it-IT")
    labels = [n["label"] for n in built["nodes"]]
    assert (
        "Approva pagamento" not in labels
        and "Conferma con Face ID" not in labels
        and len(built["edges"]) == 6
    )
    assert sum("approval" in d for d in dropped) == 2
    assert (
        visual.approval_like("Paga ora")
        and visual.approval_like("Approve")
        and not visual.approval_like("Pagina")
    )


# --- 6. taps -----------------------------------------------------------------------------------------


def _service() -> Any:
    from pisa.services.conversation import ConversationService

    service = ConversationService.__new__(ConversationService)
    service.objects, service._taps, service._clock = {}, {}, lambda: NOW
    return service


def _shown(outside: bool = False, closed: bool = False, at: datetime = NOW) -> Shown:
    data = {"title": "x", "blocks": [_laid(_diagram())]}
    return Shown("sheet", 1, "it-IT", present_id="p", at=at, data=data, outside=outside, closed=closed)


def test_6_a_tap_is_pisa_s_template_around_the_server_s_label_or_gone() -> None:
    service, device = _service(), uuid.uuid4()
    service.objects[device] = {"sheet:a": _shown(), "sheet:c": _shown(closed=True),
                               "sheet:o": _shown(at=NOW - timedelta(hours=25))}  # fmt: skip
    assert service.tap(device, "sheet:a", "n4") == ("Sul disegno: Connection pool", "it-IT")
    tap = service._taps[(device, "Sul disegno: Connection pool")]
    assert "Postgres → Modello (righe)" in tap.context and not tap.outside
    assert service.tap(device, "sheet:a", "e2") == ("Sul disegno: App → Modello (domanda)", "it-IT")
    for object_id, element in (("sheet:a", "n24"), ("sheet:b", "n1"), ("sheet:c", "n1"), ("sheet:o", "n1")):
        assert service.tap(device, object_id, element) is None  # unknown, never shown, closed, expired
    assert visual.GONE["it-IT"] == "Non è più aperto."


def test_6_a_tap_grounds_nothing_whatever_its_label_says() -> None:
    from pisa.services.conversation import TurnState

    words = visual.tap_words("salvala come x", "it-IT")
    assert words == "Sul disegno: salvala come x"  # The box's wording, one tap path
    turn = TurnState(uuid.uuid4(), words, origin="drawing")  # the flag, never the words
    assert turn.words == ""  # Asks_to_save, R2 and plan changes read these: nothing
    assert TurnState(uuid.uuid4(), words).words == words  # the same words they said themselves are theirs
    from pisa.domain import views as view_rules

    assert view_rules.asks_to_save(words) and not view_rules.asks_to_save(turn.words)


@pytest.mark.parametrize("tool", ["save_view", "cancel_event", "add_task", "draft_reply"])
def test_6_a_tap_s_turn_runs_reads_only(tool: str) -> None:
    from pydantic_ai.messages import ToolCallPart

    from pisa.brain.drawing_tools import DrawingAsk, DrawingTurn

    ran: list[str] = []

    async def handler(args: Any) -> str:
        ran.append(tool)
        return "Fatto."

    call = ToolCallPart(tool_name=tool, args={})
    tap: Any = DrawingAsk(DrawingTurn(origin="drawing", lang="it-IT"))
    said = asyncio.run(tap.wrap_tool_execute(None, call=call, tool_def=None, args={}, handler=handler))
    assert ran == [] and "not done: this turn is a tap" in said
    read = ToolCallPart(tool_name="get_agenda", args={})
    asyncio.run(tap.wrap_tool_execute(None, call=read, tool_def=None, args={}, handler=handler))
    words: Any = DrawingAsk(DrawingTurn(lang="it-IT"))
    asyncio.run(words.wrap_tool_execute(None, call=call, tool_def=None, args={}, handler=handler))
    assert ran == [tool, tool]


def test_6_the_tap_s_label_is_quoted_data_and_outside_text_when_outside_text_drew_it() -> None:
    tap = visual.Tap("Approva pagamento", ("Approva pagamento", "Banca → Approva pagamento"), True)
    head = visual.tap_head(visual.tap_words(tap.label, "it-IT"), tap, "it-IT")
    assert "Approva" not in head and head.startswith("Sul disegno: (their tap")
    assert visual.tap_data(tap).startswith("tapped: Approva pagamento")
    own = visual.Tap("Postgres", (), False)
    assert visual.tap_head(visual.tap_words("Postgres", "it-IT"), own, "it-IT").startswith(
        "Sul disegno: Postgres"
    )


def test_6_object_event_asked_carries_an_element_id_only() -> None:
    from pydantic import ValidationError

    from tests.live_app import message_of

    base = {"v": 1, "type": "object_event", "id": str(uuid.uuid4()), "ts": "2026-11-10T09:00:00Z",
            "object_id": f"sheet:{uuid.uuid4()}"}  # fmt: skip
    asked: Any = message_of(json.dumps(base | {"event": "asked", "element_id": "s1/p3"}))
    assert asked.element_id == "s1/p3"
    for wrong in ({"event": "asked"}, {"event": "seen", "element_id": "n1"},
                  {"event": "asked", "element_id": "Approva pagamento"}):  # fmt: skip
        with pytest.raises(ValidationError):  # the app's frame is refused: invalid_frame
            message_of(json.dumps(base | wrong))


def test_his_words_ask_to_see_a_drawing_and_the_sheet_is_offered() -> None:
    from pisa.domain.tool_choice import tools_for

    for words in ("come fa un modello a parlare con un database?", "fammi un grafico delle massime",
                  "fammelo vedere dal lato del database", "draw how DNS works"):  # fmt: skip
        assert visual.asks(words) and "show_sheet" in tools_for(words), words
    for words in ("che ore sono?", "piove domani?", "come funziona? solo testo"):
        assert not visual.asks(words), words


# --- 9. views: a saved diagram re-runs and re-lays out identically -------------------------------------------


def test_9_a_saved_diagram_view_reruns_and_relays_identically() -> None:
    from pisa.domain import views as view_rules
    from pisa.services.views import ViewService as ViewsService

    recipe = view_rules.validate(
        {"reads": [], "blocks": [_diagram(), {"type": "text", "text": "Da quello che so."}]}, set()
    )
    assert isinstance(recipe, view_rules.Recipe)
    first, _ = ViewsService._sheet("Modello e database", recipe, [], "it-IT")
    again, _ = ViewsService._sheet("Modello e database", recipe, [], "it-IT")
    assert first is not None and json.dumps(first) == json.dumps(again)
    assert first["blocks"][0]["layout"] == _laid(_diagram())["layout"] and first["basis"] == "knowledge"
    assert view_rules.validate({"reads": [], "blocks": [_diagram() | {"layout": {}}]}, set()) == (
        "diagram takes only ['direction', 'edges', 'groups', 'nodes', 'style']"
    )


# --- 6. taps through the socket and the talk ---------------------------------------------------------------


def test_6_a_tap_on_a_gone_sheet_answers_non_e_piu_aperto_with_no_model_call(  # type: ignore[no-untyped-def]
    client, db, secrets_key, fake_models
) -> None:
    from tests.conftest import pair, set_key
    from tests.live_app import live

    set_key(db, secrets_key)
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        index = len(app.frames)
        app.object_event({"object_id": f"sheet:{uuid.uuid4()}", "event": "asked", "element_id": "n3"})
        frame = app.next()
        assert app.run_ended_since(index)["event"]["type"] == "RUN_FINISHED"  # the tap's run says it, whole
        app.check()
    assert frame["type"] == "say" and frame["final"]
    assert frame["delta"] == "Non è più aperto." and not fake_models.calls


def test_6_a_tap_while_pisa_answers_waits_one_at_most_the_newer_wins() -> None:
    from types import SimpleNamespace

    from pisa.services.session import DeviceSession
    from pisa.services.session_messages import ObjectEvent

    session = DeviceSession.__new__(DeviceSession)
    session.device = SimpleNamespace(id=uuid.uuid4())  # type: ignore[assignment]
    session._replies, session._queued_tap = {"busy": object()}, None  # type: ignore[dict-item]
    started: list[tuple[str, str]] = []
    session._answer = lambda u, origin="words": started.append((u.text, origin))  # type: ignore[method-assign]
    first, second = (ObjectEvent.model_validate({"object_id": "sheet:x", "event": "asked", "element_id": e})
                     for e in ("n1", "n2"))  # fmt: skip
    asyncio.run(session._asked(first, (visual.tap_words("Postgres", "it-IT"), "it-IT")))
    asyncio.run(session._asked(second, (visual.tap_words("Cache", "it-IT"), "it-IT")))
    assert started == [] and session._queued_tap is not None and "Cache" in session._queued_tap.text
    session._replies = {}
    asyncio.run(session._asked(first, (visual.tap_words("Postgres", "it-IT"), "it-IT")))
    assert started == [("Sul disegno: Postgres", "drawing")]  # the flag, the box's wording


def _sheet_show(show_id: str, revision: int) -> dict[str, Any]:
    return {"present_id": show_id, "purpose": "Lo schema", "focus": "take", "lang": "it-IT",
            "object": {"object_id": "sheet:s", "kind": "sheet", "revision": revision,
                       "data": {"title": "x", "blocks": [_laid(_diagram())]}}}  # fmt: skip


def test_6_talk_ask_is_his_tap_and_a_superseded_revision_answers_nothing() -> None:
    from pisa.services.talk import Opening, TalkScreen
    from tests.test_live_talk import _run, _until

    run, session, link, seen = _run()
    run.screen = TalkScreen(link, Opening().kinds | {"sheet"}, "it-IT", frozenset({"diagram"}))
    asked: list[tuple[str, str]] = []

    async def hook(object_id: str, element_id: str) -> str | None:
        asked.append((object_id, element_id))
        return visual.tap_words("Connection pool", "it-IT")

    run.hooks.asked = hook

    async def go() -> None:
        task = asyncio.create_task(run.run())
        await run.screen.present(_sheet_show("v1", 1))  # type: ignore[union-attr]
        await run.screen.present(_sheet_show("v2", 2))  # type: ignore[union-attr]  # redrawn in place
        link.messages.put_nowait({"type": "talk.ask", "id": "v1", "element_id": "n4"})  # the old revision
        await _until(lambda: any(w.id == "tap" for w in session.whispers))
        link.messages.put_nowait({"type": "talk.ask", "id": "v2", "element_id": "n4"})
        await _until(lambda: any(s[0] == "think" for s in seen))
        run.end("owner")
        await task

    run_talk(go())
    assert [w.text for w in session.whispers if w.id == "tap"] == ["Non è più aperto."]
    assert asked == [("sheet:s", "n4")]
    assert ("think", "Sul disegno: Connection pool") in seen and ("origin", "drawing") in seen


def test_the_proposed_fixtures_round_trip_through_core_s_models() -> None:
    """Staged in protocol/fixtures/proposed-native-blocks until the schema takes them; the server's models
    already take them."""
    from tests.conftest import PROTOCOL_DIR
    from tests.live_app import message_of

    staged = sorted((PROTOCOL_DIR / "fixtures" / "proposed-native-blocks").glob("*.json"))
    assert len(staged) == 6
    for path in staged:
        body = json.loads(path.read_text())
        if body["type"] == "talk.ask":  # the talk's data channel, not the WebSocket
            assert set(body) == {"type", "id", "element_id"}
            continue
        if body["type"] == "hello":  # v1's hello: contract 2 negotiates the blocks with Ring 1
            continue
        dumped = message_of(path.read_text()).model_dump(mode="json", by_alias=True)
        assert dumped == {k: v for k, v in body.items() if k not in ("v", "ts")}, path.name
