# ruff: noqa: E501
"""The chart that didn't draw, its
caption, a diagram that fits a phone, spoken prices. The shapes are the recorded ones, every word synthetic.

Request 12: "Confrontami i costi di tre abbonamenti di streaming in Italia in una tabella" (a priced table, the
prices traced to that turn's results), then "Fammelo vedere in un grafico": a turn that reads nothing new. Its
chart's prices traced to none of *this* turn's results, so R-V2 dropped every series, the sheet went with its
title and words only (an empty "chart" card, nothing for the phone to drop: no decode error), and the sheet's
basis, computed with no read and no number left, was "knowledge" ("Da quello che so")."""

import asyncio
import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from pisa.brain.present_tools import (
    LOST_DRAWING,
    SheetBlockIn,
    Shown,
    add_present_tools,
    sheet_basis,
    sheet_blocks,
)
from pisa.domain import decisions, visual
from pisa.domain import layout as layouts
from pisa.domain.claims import split_screen_claim
from tests.test_native_drawings import Phone, _presenter, _tools

# --- request 12's shapes (synthetic) -------------------------------------------------------------------------

# turn 12: what the search returned (the prices as a page writes them, Italian decimal commas)
SEARCH_12 = (
    "1. Abbonamenti streaming a novembre: i prezzi\n   https://news.example.it/streaming\n"
    "   Netflix Standard con pubblicità 7,99 € al mese, Premium 21,99 €; Disney+ Standard con pubblicità "
    "6,99 €; Amazon Prime 4,99 € al mese."
)
TABLE_12 = {
    "type": "table", "columns": ["Servizio", "Piano", "Prezzo al mese"],
    "rows": [["Netflix", "Standard con pubblicità", "7,99 €"], ["Netflix", "Premium", "21,99 €"],
             ["Disney+", "Standard con pubblicità", "6,99 €"], ["Amazon Prime", "Prime", "4,99 €"]],
}  # fmt: skip
# turn 12b: the chart the model wrote from the table (numbers the English way), and its line under the title
CHART_12B = {
    "type": "chart", "mark": "bar", "title": "un piano per barra",
    "x": {"label": "Piano", "kind": "category"}, "y": {"label": "Prezzo", "unit": "€"},
    "series": [{"name": "Prezzo al mese", "points": [
        {"x": "Netflix Standard", "y": 7.99}, {"x": "Netflix Premium", "y": 21.99},
        {"x": "Disney+", "y": 6.99}, {"x": "Amazon Prime", "y": 4.99}]}],
}  # fmt: skip
WORDS_12B = "Fammelo vedere in un grafico."


def _blocks(*raw: dict[str, Any]) -> list[SheetBlockIn]:
    return [SheetBlockIn.model_validate(b) for b in raw]


def _request_12(objects: dict[str, Shown]) -> Phone:
    """Turn 12: the table, shown from this turn's search."""
    with decisions.collecting():
        phone = Phone(frozenset({"diagram", "chart", "timeline"}))
        tools = _tools(_presenter(phone, results=[SEARCH_12], objects=objects))
        said = asyncio.run(tools["show_sheet"](title="Streaming in Italia", blocks=_blocks(TABLE_12)))
        assert said.startswith("Shown"), said
        assert "basis" not in phone.sent[-1]["object"]["data"]  # fetched: no "Da quello che so"
        return phone


def _turn_12b(objects: dict[str, Shown], phone: Phone) -> str:
    """Turn 12b: no read; the corpus is his words alone (as `DrawingTurn.corpus` gives it)."""
    with decisions.collecting():
        presenter = _presenter(phone, results=[], objects=objects)
        presenter.corpus = lambda: [WORDS_12B]
        return asyncio.run(_tools(presenter)["show_sheet"](
            title="Streaming in Italia: prezzi al mese", blocks=_blocks(CHART_12B)))  # fmt: skip


def test_request_12_the_cause_this_turn_s_results_never_carried_the_prices() -> None:
    """The cause, on CORE's own functions: the chart's series is dropped by R-V2 (no number of it in this
    turn's results), and what is left of the sheet is captioned "knowledge". A "7,99 €" against 7.99 is no
    cause: it traces either way."""
    built, dropped = sheet_blocks(
        [CHART_12B, {"type": "text", "text": "un piano per barra"}], [], results=[WORDS_12B], lang="it-IT"
    )
    assert [b["type"] for b in built] == ["text"] and "not in this turn's results" in dropped[0]
    assert sheet_basis(built, []) == "knowledge"  # what the phone captioned "Da quello che so"
    for written in ("7,99 €", "7.99", "€7.99", "7,99"):
        assert visual.traces(7.99, visual.Corpus([written])), written
    (chart,), none = sheet_blocks([CHART_12B], [], results=[SEARCH_12], lang="it-IT")
    assert chart["type"] == "chart" and not none and len(chart["series"][0]["points"]) == 4


def test_request_12_the_chart_draws_the_prices_of_the_table_pisa_showed() -> None:
    """The fix: a sheet Pisa showed this conversation carries numbers that traced when it was shown; the chart
    asked for next draws them, with bars, and is never captioned "Da quello che so"."""
    objects: dict[str, Shown] = {}
    phone = _request_12(objects)
    said = _turn_12b(objects, phone)
    assert said.startswith("Shown"), said
    data = phone.sent[-1]["object"]["data"]
    (chart,) = data["blocks"]
    assert chart["type"] == "chart" and [p["y"] for p in chart["series"][0]["points"]] == [
        7.99,
        21.99,
        6.99,
        4.99,
    ]
    assert data.get("basis") not in ("knowledge",)  # tools: the field left out


def test_request_12_a_number_no_shown_sheet_carried_still_never_draws() -> None:
    """R-V2 holds: only the shown sheet's numbers trace, never a price the model adds (9,99 € here)."""
    objects: dict[str, Shown] = {}
    phone = _request_12(objects)
    extra = json.loads(json.dumps(CHART_12B))
    extra["series"][0]["points"].append({"x": "Altro", "y": 9.99})
    presenter = _presenter(phone, results=[], objects=objects)
    presenter.corpus = lambda: [WORDS_12B]
    said = asyncio.run(_tools(presenter)["show_sheet"](title="Prezzi", blocks=_blocks(extra)))
    assert said.startswith("Not shown") and len(phone.sent) == 1


def test_shown_numbers_take_tables_and_charts_never_a_knowledge_sheet_or_a_stat() -> None:
    data = {"blocks": [TABLE_12, {"type": "stat", "label": "Media", "value": "10,49"},
                       {"type": "table", "columns": ["a"], "rows": [["circa 500 € l'anno"], ["Piano 2"]]}]}  # fmt: skip
    found = visual.shown_numbers(data)
    assert (
        "21,99 €" in found and "10,49" not in found and "500 €" in found and not any("2" == f for f in found)
    )
    assert visual.shown_numbers({**data, "basis": "knowledge"}) == [] and visual.shown_numbers(None) == []


def test_a_chart_that_can_t_be_drawn_is_never_an_empty_card() -> None:
    """Every drawing left out and nothing else to see: nothing is sent, and the model is told to say it or
    show the table."""
    phone = Phone(frozenset({"diagram", "chart", "timeline"}))
    presenter = _presenter(phone, results=[])
    presenter.corpus = lambda: [WORDS_12B]
    said = asyncio.run(_tools(presenter)["show_sheet"](
        title="Prezzi", blocks=_blocks(CHART_12B, {"type": "text", "text": "un piano per barra"},
                                       {"type": "section", "title": "Fonti"})))  # fmt: skip
    assert (
        said.startswith("Not shown: the chart can't be drawn") and LOST_DRAWING in said and phone.sent == []
    )


def test_a_lost_chart_beside_its_table_sends_the_table() -> None:
    with decisions.collecting():
        phone = Phone(frozenset({"diagram", "chart", "timeline"}))
        presenter = _presenter(phone, results=[SEARCH_12])
        stray = json.loads(json.dumps(CHART_12B))
        stray["series"][0]["points"][0]["y"] = 8.49  # traces nowhere
        said = asyncio.run(_tools(presenter)["show_sheet"](title="Prezzi", blocks=_blocks(stray, TABLE_12)))
        assert said.startswith("Shown") and [
            b["type"] for b in phone.sent[-1]["object"]["data"]["blocks"]
        ] == ["table"]


def test_the_basis_is_never_knowledge_with_a_traced_number() -> None:
    """R-V3: a sheet with a traced number is the tools' (or mixed beside a diagram of what Pisa knows), even
    when the number traced to an earlier sheet and this turn read nothing."""
    knowing = {"type": "diagram", "nodes": [{"label": "Abbonato"}, {"label": "Catalogo"}]}
    assert sheet_basis([CHART_12B], []) is None
    assert sheet_basis([CHART_12B, knowing], []) == "mixed"
    assert sheet_basis([knowing], []) == "knowledge" and sheet_basis([knowing], [SEARCH_12]) == "knowledge"


# --- request 11: the diagram cut at a phone's right edge -------------------------------------------------------

DIAGRAM_11 = {
    "type": "diagram", "style": "flow", "direction": "right",
    "nodes": [{"id": "tu", "label": "Tu", "shape": "actor"}, {"id": "ai", "label": "Modello AI"},
              {"id": "q", "label": "Strumento di query"}, {"id": "db", "label": "Database", "shape": "store"},
              {"id": "r", "label": "Risposta", "shape": "note"}],
    "edges": [{"from": "tu", "to": "ai", "label": "domanda"}, {"from": "ai", "to": "q", "label": "richiesta"},
              {"from": "q", "to": "db", "label": "SQL"}, {"from": "db", "to": "r", "label": "righe"}],
}  # fmt: skip


def test_request_11_a_chain_on_a_portrait_phone_goes_top_to_bottom() -> None:
    (built,), _ = sheet_blocks([DIAGRAM_11], [], results=[], phone=393.0, lang="it-IT")
    assert built["direction"] == "down" and "scroll" not in built
    assert built["layout"]["width"] <= 393.0 - 2 * layouts.SHEET_GUTTER  # whole, at full size


def test_request_11_the_direction_asked_stays_where_it_fits() -> None:
    """A landscape phone (its surface's width) keeps the row it was asked for; so does a diagram that fits."""
    (wide,), _ = sheet_blocks([DIAGRAM_11], [], results=[], phone=1300.0, lang="it-IT")
    assert wide["direction"] == "right"
    two = {**DIAGRAM_11, "nodes": DIAGRAM_11["nodes"][:2], "edges": DIAGRAM_11["edges"][:1]}
    (small,), _ = sheet_blocks([two], [], results=[], phone=393.0, lang="it-IT")
    assert small["direction"] == "right"


def test_request_11_the_screen_s_surface_width_reaches_the_layout() -> None:
    with decisions.collecting():
        phone = Phone(frozenset({"diagram"}))
        phone.width = 1300.0  # type: ignore[attr-defined]  # what SessionScreen carries, when known
        asyncio.run(
            _tools(_presenter(phone))["show_sheet"](title="Modello e database", blocks=_blocks(DIAGRAM_11))
        )
        assert phone.sent[-1]["object"]["data"]["blocks"][0]["direction"] == "right"
        phone.width = 390.0  # type: ignore[attr-defined]
        asyncio.run(
            _tools(_presenter(phone))["show_sheet"](title="Modello e database", blocks=_blocks(DIAGRAM_11))
        )
        assert phone.sent[-1]["object"]["data"]["blocks"][0]["direction"] == "down"


def test_the_gateway_s_screen_carries_the_surface_width() -> None:
    from pisa.services.session import SessionScreen

    screen = SessionScreen(None, frozenset({"sheet"}), frozenset({"diagram"}), 402.0)  # type: ignore[arg-type]
    assert screen.width == 402.0 and SessionScreen(None, frozenset()).width is None  # type: ignore[arg-type]


# --- spoken numbers ----------------------------------------------------------------------------------------


def test_a_price_is_said_whole_when_the_screen_claim_is_cut() -> None:
    """12b's lead: "21,99 €" came out "21, 99 €" (the clause split cut at the decimal comma)."""
    said, claim = split_screen_claim(
        "Netflix premium è il più caro a 21,99 €, Amazon Prime il più economico a 4,99 €, te l'ho messo sul telefono."
    )
    assert said == "Netflix premium è il più caro a 21,99 €, Amazon Prime il più economico a 4,99 €."
    assert claim == "Te l'ho messo sul telefono."
    assert split_screen_claim("Posti per 3, 4 o 5, te l'ho messo sul telefono.")[0] == "Posti per 3, 4 o 5."


# --- the fixed prompt ----------------------------------------------------------------------------------------


def test_the_fixed_prompt_and_show_sheet_do_not_grow() -> None:
    from pisa.brain.conversation import system_prompt

    agent: Agent[None, str] = Agent(TestModel())
    add_present_tools(agent, lambda: None)
    sheet = agent._function_toolset.tools["show_sheet"].tool_def
    assert (
        len(system_prompt(lang="it-IT")) <= 5278
    )  # its size before charts (+32: the new name; +10: the user as "they")
    assert len(sheet.description or "") <= 637 and len(json.dumps(sheet.parameters_json_schema)) <= 6332


# --- review ------------------------------------------------------------------------------


def test_a_knowledge_sheet_after_a_priced_table_is_still_knowledge() -> None:
    """R-V3: an earlier sheet's numbers are no read of this turn; a list of what Pisa knows, shown after the
    priced table, keeps "Da quello che so" (before the fix it was captioned as the tools')."""
    with decisions.collecting():
        objects: dict[str, Shown] = {}
        phone = _request_12(objects)
        presenter = _presenter(phone, results=[], objects=objects)
        presenter.corpus = lambda: ["Come funziona lo streaming adattivo?"]
        said = asyncio.run(_tools(presenter)["show_sheet"](title="Streaming adattivo", blocks=_blocks(
            {"type": "list", "items": ["Il video è diviso in segmenti", "Il player sceglie la qualità"]})))  # fmt: skip
        assert said.startswith("Shown") and phone.sent[-1]["object"]["data"]["basis"] == "knowledge"
        assert sheet_basis([CHART_12B], [], ["7,99 €"]) is None  # a chart traced to the earlier sheet: tools


def test_a_sheet_from_an_earlier_conversation_traces_nothing() -> None:
    """R-V2: a ranking table shown 20 hours ago (still kept for taps) never lets a chart of made-up 1…5 draw."""
    with decisions.collecting():
        from datetime import timedelta

        from tests.test_native_drawings import NOW

        phone = Phone(frozenset({"diagram", "chart", "timeline"}))
        objects: dict[str, Shown] = {}
        ranking = {"type": "table", "columns": ["Posto", "Città"],
                   "rows": [[1, "Roma"], [2, "Milano"], [3, "Napoli"], [4, "Torino"], [5, "Bologna"]]}  # fmt: skip
        first = _presenter(
            phone, results=["1. Roma 2. Milano 3. Napoli 4. Torino 5. Bologna"], objects=objects
        )
        assert asyncio.run(_tools(first)["show_sheet"](title="Città", blocks=_blocks(ranking))).startswith(
            "Shown"
        )
        for shown in objects.values():
            shown.at = NOW - timedelta(hours=20)
        made_up = {**CHART_12B, "series": [{"name": "Ore", "points": [
            {"x": a, "y": y} for a, y in zip(["Gatto", "Cane", "Cavallo", "Koala", "Giraffa"], [1, 2, 3, 4, 5], strict=True)]}]}  # fmt: skip
        later = _presenter(phone, results=[], objects=objects)
        later.corpus = lambda: ["Fammi un grafico di quanto dormono gli animali."]
        said = asyncio.run(_tools(later)["show_sheet"](title="Ore di sonno", blocks=_blocks(made_up)))
        assert said.startswith("Not shown") and len(phone.sent) == 1
