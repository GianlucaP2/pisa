"""Answers with sources: the findings rows, the map, the product cards and
`excluded`; prices from shops, events by date and a booking page's times, read only; each phone gets what it
draws, an older one today's tables. No database, no network: fakes for the web."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from pisa.brain import answer_tools, findings
from pisa.brain.research_turn import ResearchTurn
from pisa.domain import answers, decisions, effects, tool_choice, visual
from pisa.domain.sheet import SheetData

FIXTURES = Path(__file__).resolve().parents[2] / "protocol" / "fixtures" / "proposed-answers"
NOW = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)  # a Saturday, 18:00 in Rome
ALL = frozenset({"findings", "map", "product"})


# --- the shapes -------------------------------------------------------------------------------------------


def row(n: int, **more: Any) -> dict[str, Any]:
    return {"id": f"r{n}", "kind": "place", "name": f"Posto {n}", **more}


def sheet(*blocks: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {"title": "Cena", "blocks": list(blocks), **more}


def test_a_findings_sheet_with_map_and_excluded_validates() -> None:
    rows = [
        row(1, pick=True, lat=41.9, lon=12.47, price="€€",
            open_at_asked={"ok": True, "hours": "19:30-23:00", "source": "map"},
            recognition=[{"guide": "Michelin", "award": "★", "year": 2026, "url": "https://guide.michelin.com/x"}],
            slots={"day": "2026-11-14", "times": ["20:00", "20:30"], "url": "https://www.thefork.it/x",
                   "source": "https://www.thefork.it/x"}),
        row(2, lat=41.8, lon=12.4, open_at_asked={"ok": None, "source": "https://example.org/hours"}),
    ]  # fmt: skip
    pins = [{"id": "r1", "name": "Posto 1", "lat": 41.9, "lon": 12.47, "badge": "Michelin ★", "pick": True}]
    data = sheet({"type": "map", "pins": pins}, {"type": "findings", "rows": rows},
                 excluded=[{"name": "Trattoria X", "why": "chiuso il sabato", "source": "map"}])  # fmt: skip
    assert SheetData.model_validate(data).model_dump(mode="json") == data


@pytest.mark.parametrize(
    ("data", "said"),
    [
        (sheet({"type": "findings", "rows": [row(n) for n in range(1, 12)]}), "at most 10"),
        (sheet({"type": "findings", "rows": [row(1), row(2, pick=True)]}), "first row"),
        (sheet({"type": "findings", "rows": [row(1, lat=41.0)]}), "lat and lon"),
        (sheet({"type": "findings", "rows": [row(1, at="2026-11-14T21:00")]}), "event's"),
        (
            sheet(
                {"type": "map", "pins": [{"id": "r2", "name": "x", "lat": 1, "lon": 1}]},
                {"type": "findings", "rows": [row(1)]},
            ),
            "names a row",
        ),
        (
            sheet(
                {
                    "type": "map",
                    "pins": [{"id": f"r{n}", "name": "x", "lat": 1, "lon": 1} for n in range(1, 11)]
                    + [{"id": f"p{n}", "name": "x", "lat": 1, "lon": 1} for n in range(1, 4)],
                }
            ),
            "at most 12",
        ),
        (
            sheet({"type": "text", "text": "x"}, excluded=[{"name": "X", "why": "chiuso", "source": "map"}]),
            "excluded goes with",
        ),
        (
            sheet(
                *[
                    {
                        "type": "product",
                        "id": f"p{n % 6 + 1}",
                        "title": "t",
                        "price": 1.0,
                        "currency": "EUR",
                        "shop": "s",
                        "url": "https://a.it/x",
                        "source": "https://a.it/x",
                    }
                    for n in range(7)
                ]
            ),
            "at most 6",
        ),
        (
            sheet(
                {
                    "type": "findings",
                    "rows": [
                        row(
                            1,
                            slots={
                                "day": "2026-11-14",
                                "times": ["21:00", "20:00"],
                                "url": "https://a.it",
                                "source": "https://a.it",
                            },
                        )
                    ],
                }
            ),
            "sorted",
        ),
        (
            sheet(
                {
                    "type": "findings",
                    "rows": [row(1, recognition=[{"guide": "G", "award": "A", "url": "http://a"}])],
                }
            ),
            "https",
        ),
    ],
)
def test_the_limits_are_the_contract_s(data: dict[str, Any], said: str) -> None:
    with pytest.raises(ValueError, match=said):
        SheetData.model_validate(data)


def test_ring_1_draws_the_answer_blocks() -> None:
    from pisa.domain.sheet import PHONE_BLOCKS

    assert {"chart", "findings", "map", "product"} <= set(PHONE_BLOCKS)  # what a Ring 1 phone is sent


# --- each phone gets what it draws ------------------------------------------------------------------------


def _research(**places: dict[str, Any]) -> ResearchTurn:
    r = ResearchTurn(lang="it-IT", prompt_chars=0)
    r.places = dict(places)
    return r


def _composed() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    class Chosen:
        def __init__(self, pid: str) -> None:
            self.place_id, self.why, self.pros, self.cons = pid, "Tranquillo.", [], []

    research = _research(
        a={"name": "Fittizia", "lat": 41.9, "lon": 12.47, "open_now": True, "rating": 4.6,
           "rating_count": 900, "price_level": 2, "travel_min": 6,
           "next_change": "2026-11-14T23:00:00+01:00"},
        b={"name": "Nonna Finta", "open_now": False, "price_level": 3},
        c={"name": "Per Finta", "lat": 41.89, "lon": 12.46},
    )  # fmt: skip
    research.slots["a"] = {"day": "2026-11-14", "times": ["20:00", "20:30"], "url": "https://www.thefork.it/v",
                           "source": "https://www.thefork.it/v"}  # fmt: skip
    data, rows, _ = findings.compose(
        research, "Cena", ["tranquillo"], "a", [Chosen("a"), Chosen("b")], "it-IT"
    )
    assert data is not None
    return data, rows


def test_the_research_answer_carries_the_rows_and_the_map() -> None:
    data, _ = _composed()
    SheetData.model_validate(data)
    kinds = [b["type"] for b in data["blocks"]]
    assert kinds.index("map") < kinds.index("findings") < kinds.index("table")
    [found] = [b for b in data["blocks"] if b["type"] == "findings"]
    first = found["rows"][0]
    assert (
        first["pick"] is True
        and first["name"] == "Fittizia"
        and (first["lat"], first["lon"]) == (41.9, 12.47)
    )
    assert first["open_at_asked"] == {"ok": True, "hours": "aperto, fino alle 23:00", "source": "map"}
    assert first["slots"]["times"] == ["20:00", "20:30"] and first["price"] == "€€"
    assert "lat" not in found["rows"][1]  # the map never found it: no coordinates, no pin
    [pins] = [b["pins"] for b in data["blocks"] if b["type"] == "map"]
    assert [p["id"] for p in pins] == ["r1", "r3"] and pins[0]["pick"] is True  # r3: the filled third row
    assert "pick" not in pins[1]


def test_an_older_phone_gets_today_s_tables_once_and_a_newer_one_never_the_same_table_twice() -> None:
    data, _ = _composed()
    old = visual.answers_for_phone(data, None, "it-IT")
    SheetData.model_validate(old)
    kinds = [b["type"] for b in old["blocks"]]
    assert "findings" not in kinds and "map" not in kinds
    assert [b["columns"][0] for b in old["blocks"] if b["type"] == "table"] == ["Posto", "Posto"]  # once each
    assert any(b["type"] == "link" and b["url"] == "https://www.thefork.it/v" for b in old["blocks"])
    new = visual.answers_for_phone(data, ALL, "it-IT")
    SheetData.model_validate(new)
    assert not any(b["type"] == "table" for b in new["blocks"])  # the findings block shows them
    assert [b["type"] for b in new["blocks"]].count("findings") == 1


def test_the_fallback_columns_are_the_research_table_s() -> None:
    for lang in ("it-IT", "en-US"):
        assert visual.FINDINGS_COLUMNS[lang] == findings._COLUMNS[lang]
        assert visual.POINTS_COLUMNS[lang] == findings._POINTS[lang]


def test_excluded_goes_under_the_table_on_an_older_phone() -> None:
    data, _ = _composed()
    data["excluded"] = [{"name": "Trattoria X", "why": "chiuso il sabato", "source": "map"}]
    old = visual.answers_for_phone(data, None, "it-IT")
    assert "excluded" not in old
    at = next(i for i, b in enumerate(old["blocks"]) if b.get("title") == "Esclusi")
    assert old["blocks"][at - 1]["type"] == "table"
    assert old["blocks"][at + 1] == {"type": "list", "items": ["Trattoria X: chiuso il sabato"]}
    assert visual.answers_for_phone(data, ALL, "it-IT")["excluded"] == data["excluded"]


def test_products_go_as_a_table_and_links_on_an_older_phone() -> None:
    product = answers.product_block(1, {"shop": "Amazon.it", "price": 199.0, "shipping": 0.0, "low_6m": 179.9,
                                        "source": "https://www.trovaprezzi.it/x"},
                                     "Sony WH-1000XM5", None)  # fmt: skip
    data = sheet(product)
    SheetData.model_validate(data)
    old = visual.answers_for_phone(data, frozenset({"chart"}), "it-IT")
    [table, link] = old["blocks"]
    assert table["rows"] == [["Sony WH-1000XM5", "Amazon.it", "199,00 €", "gratis", "179,90 €"]]
    assert link["host"] == "trovaprezzi.it"
    assert visual.answers_for_phone(data, ALL, "en-US") == data


# --- prices from shops ------------------------------------------------------------------------------------

COMPARATOR = (
    "Sony WH-1000XM5 Cuffie wireless. Prezzo minimo degli ultimi 6 mesi: 164,97 €. Offerte: "
    "Amazon 199,00 € Spedizione gratuita · Bpm Power 185,85 € + 4,00 € spedizione · "
    "Venduto da: Sennheiser Store 249,90 € Sped. 0,00 € · MediaWorld 219,99 €"
)


def test_offers_are_read_off_the_page() -> None:
    found = answers.offers(COMPARATOR, "https://www.trovaprezzi.it/cuffie/sony")
    assert [(o["shop"], o["price"], o.get("shipping")) for o in found] == [
        ("Bpm Power", 185.85, 4.0), ("Amazon", 199.0, 0.0), ("MediaWorld", 219.99, None),
        ("Sennheiser Store", 249.9, 0.0),
    ]  # fmt: skip
    assert answers.low_6m(COMPARATOR) == 164.97
    shop = answers.offers("Sony WH-1000XM5 a 189,00 €", "https://www.unieuro.it/p/xm5")
    assert shop == [
        {"shop": "unieuro.it", "price": 189.0, "currency": "EUR", "source": "https://www.unieuro.it/p/xm5"}
    ]


def test_amounts_read_the_italian_and_english_way() -> None:
    assert [answers.amount(x) for x in ("1.299", "1.234,56", "189.85", "35", "1 299,00")] == [
        1299.0, 1234.56, 189.85, 35.0, 1299.0,
    ]  # fmt: skip


# --- the tools, end to end on fakes -----------------------------------------------------------------------


class Agent:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool_plain(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        self.tools[fn.__name__] = fn
        return fn


PROGRAMME = (
    "Auditorium Parco della Musica, programma. Sabato 14 novembre 2026: ore 18:00 Coro dei bambini, Sala "
    "Petrassi, ingresso 8 €, dai 3 anni. 21:00 Concerto di Franco Inventato, Sala Santa Cecilia, "
    "da 35 €. "
    "Domenica 15 novembre: 11:00 Lezione di jazz."
)


class Web:
    def __init__(self, pages: dict[str, str], results: list[dict[str, str]]) -> None:
        self.pages, self.results, self.searched = pages, results, []

    async def web_search(self, query: str, **kw: Any) -> list[dict[str, str]]:
        self.searched.append((query, kw))
        return self.results

    async def read_page(self, url: str, question: str | None = None) -> dict[str, Any] | None:
        return {"url": url, "text": self.pages[url]} if url in self.pages else None


def _tools(web: Web) -> tuple[dict[str, Callable[..., Any]], ResearchTurn, list[dict[str, Any]]]:
    shown: list[dict[str, Any]] = []

    async def show(data: dict[str, Any], focus: str, purpose: str) -> str:
        SheetData.model_validate(data)
        shown.append(data)
        return "shown"

    research = ResearchTurn(lang="it-IT", prompt_chars=0, show_sheet=show)
    agent = Agent()
    answer_tools.add_answer_tools(agent, web, "Europe/Rome", research, lambda: NOW)  # type: ignore[arg-type]
    return agent.tools, research, shown


def test_prices_from_a_comparator_become_cards_whose_numbers_trace() -> None:
    url = "https://www.trovaprezzi.it/cuffie/sony"
    web = Web({url: COMPARATOR}, [{"title": "Sony WH-1000XM5 prezzi", "url": url, "snippet": "Confronta"}])
    tools, _, shown = _tools(web)
    said = asyncio.run(tools["shop_prices"]("Sony WH-1000XM5"))
    assert "o1. Bpm Power: 185,85 €, shipping 4,00 €" in said and "164,97 €" in said
    assert web.searched[0][1]["include_domains"] == list(answers.COMPARATORS)
    out = asyncio.run(tools["show_products"]("Cuffie", [
        answer_tools.ProductPick(offer="o2", title="Sony WH-1000XM5"),
        answer_tools.ProductPick(offer="o1", title="Sony WH-1000XM5"),
        answer_tools.ProductPick(offer="o9", title="Inventata"),
    ], pick="o2"))  # fmt: skip
    assert "On the user's phone: 2 cards" in out and "'o9'" in out
    products = shown[0]["blocks"]
    assert [(p["shop"], p["price"], p["low_6m"]) for p in products] == [
        ("Amazon", 199.0, 164.97),
        ("Bpm Power", 185.85, 164.97),
    ]


def test_events_stand_on_a_programme_read_with_their_date_and_time() -> None:
    url = "https://www.auditorium.com/it/eventi"
    web = Web({url: PROGRAMME}, [{"title": "Programma", "url": url, "snippet": "eventi"}])
    tools, research, shown = _tools(web)
    research.places["v"] = {"name": "Sala Santa Cecilia", "lat": 41.93, "lon": 12.47}
    read = asyncio.run(tools["events_on"]("2026-11-14", "Roma", "concerti"))
    assert "page https://www.auditorium.com/it/eventi" in read and "Inventato" in read
    E = answer_tools.EventIn
    out = asyncio.run(tools["show_events"]("Sabato a Roma", "2026-11-14", [
        E(title="Coro dei bambini", time="18:00", url="https://www.ticketone.it/coro"),  # its page never read
        E(title="Concerto di Franco Inventato", time="21:00", url=url, venue="Sala Santa Cecilia",
          price="35 €"),
        E(title="Coro dei bambini", time="18:00", url=url, venue="Sala Petrassi", price="8 €",
          age="dai 3 anni"),
        E(title="Lezione di jazz", time="11:00", url=url),  # Sunday's, not Saturday's
        E(title="Coro dei bambini", time="19:30", url=url),  # the wrong time
        E(title="Brignano", time="21:00", url="https://www.ticketone.it/x"),  # a page never read
    ]))  # fmt: skip
    [data] = shown
    [found] = [b for b in data["blocks"] if b["type"] == "findings"]
    assert [(r["name"], r["at"]) for r in found["rows"]] == [
        ("Coro dei bambini", "2026-11-14T18:00"), ("Concerto di Franco Inventato", "2026-11-14T21:00"),
    ]  # fmt: skip
    assert found["rows"][0]["age"] == "dai 3 anni" and "lat" not in found["rows"][0]
    assert {r["url"] for r in found["rows"]} == {url}  # never the page no tool read
    [pins] = [b["pins"] for b in data["blocks"] if b["type"] == "map"]
    assert pins == [{"id": "r2", "name": "Concerto di Franco Inventato", "lat": 41.93, "lon": 12.47,
                     "badge": "21:00"}]  # fmt: skip
    assert "'Lezione di jazz'" in out and "'Brignano'" in out and "Only 2 with a checked time" in out
    old = visual.answers_for_phone(data, None, "it-IT")
    [table] = [b for b in old["blocks"] if b["type"] == "table"]
    assert table["columns"] == ["Quando", "Evento", "Dove", "Prezzo", "Età"]


def test_a_booking_page_s_times_are_kept_on_the_row_as_text() -> None:
    url = "https://www.thefork.it/ristorante/vico"
    page = (
        "Fittizia. Prenota un tavolo. Sabato 14 novembre: disponibilità alle 19:30, 20:00, 21:15. "
        "Domenica 15 novembre: 13:00"
    )
    web = Web({url: page}, [])
    tools, research, _ = _tools(web)
    research.places["a"] = {"name": "Fittizia", "website": url}
    assert "between 2026-11-14 and 2026-11-15" in asyncio.run(tools["table_slots"]("a", url, "2026-11-20"))
    said = asyncio.run(tools["table_slots"]("a", url, "2026-11-14"))
    assert "19:30, 20:00, 21:15" in said
    assert research.slots["a"] == {"day": "2026-11-14", "times": ["19:30", "20:00", "21:15"], "url": url,
                                   "source": url}  # fmt: skip
    assert "only pages web_search found" in asyncio.run(
        tools["table_slots"]("a", "https://evil.example/x", "2026-11-14")
    )


def test_the_new_tools_only_read_and_are_deferred() -> None:
    from pisa.brain.tool_search import ALWAYS, VOICE_ALWAYS

    names = {"shop_prices", "show_products", "events_on", "show_events", "table_slots"}
    assert all(effects.MANIFEST[n] == "read" for n in names)
    assert {effects.outside_read(n) for n in ("shop_prices", "events_on", "table_slots")} == {
        "shop-prices", "event-pages", "booking-page",
    }  # fmt: skip
    assert not names & (ALWAYS | VOICE_ALWAYS)


@pytest.mark.parametrize(
    ("words", "tool"),
    [
        ("dove le compro al prezzo migliore?", "shop_prices"),
        ("cuffie ANC sotto 200 euro, dove al prezzo migliore", "shop_prices"),
        ("cosa c'è stasera a Roma?", "events_on"),
        ("concerti sabato?", "events_on"),
        ("c'è posto stasera da Fittizia?", "table_slots"),
    ],
)
def test_his_words_offer_the_tool(words: str, tool: str) -> None:
    assert tool in tool_choice.tools_for(words)


def test_everyday_words_offer_none() -> None:
    for words in ("che ore sono?", "sposta la palestra a domani", "quanto costa il biglietto del bus"):
        assert not {"shop_prices", "events_on", "table_slots"} & tool_choice.tools_for(words), words


def test_event_checks_and_slots_read_the_day() -> None:
    day = date(2026, 11, 14)
    ok = answers.event_checked(PROGRAMME, day, title="Coro dei bambini", time="18:00", venue="Sala Petrassi",
                               price="8 €", age="dai 3 anni")  # fmt: skip
    assert ok is None
    assert answers.event_checked(PROGRAMME, day, title="Coro dei bambini", time="18:00", venue=None,
                                 price="12 €", age=None) == "its price isn't on that page"  # fmt: skip
    assert "doesn't name" in str(
        answers.event_checked(
            PROGRAMME,
            date(2026, 11, 21),
            title="Coro dei bambini",
            time="18:00",
            venue=None,
            price=None,
            age=None,
        )
    )
    assert answers.slot_times("Nessuna disponibilità", day) == []


# --- the fixtures the phone builds against ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["present_sheet_findings", "present_sheet_events", "present_sheet_products"])
def test_the_fixtures_are_valid_sheets(name: str) -> None:
    message = json.loads((FIXTURES / f"{name}.json").read_text())
    data = message["object"]["data"]
    assert SheetData.model_validate(data).model_dump(mode="json") == data


def test_the_fallback_fixture_is_what_an_older_phone_gets() -> None:
    rich = json.loads((FIXTURES / "present_sheet_findings.json").read_text())["object"]["data"]
    plain = json.loads((FIXTURES / "present_sheet_fallback.json").read_text())["object"]["data"]
    assert visual.answers_for_phone(rich, None, "it-IT") == plain


# --- the presenter sends each phone what it draws, and keeps the whole sheet --------------------------------


def test_the_presenter_sends_an_older_phone_the_tables_and_keeps_the_rows() -> None:
    with decisions.collecting():
        from tests.test_native_drawings import Phone, _presenter

        data, _ = _composed()
        for draws, kinds in (
            (None, {"table"}),
            (frozenset({"chart"}), {"table"}),
            (ALL, {"findings", "map"}),
        ):
            phone = Phone(draws)
            presenter = _presenter(phone)
            ack = asyncio.run(presenter.show("show_findings", "sheet", "sheet:x", "", "take", data=data))
            sent = phone.sent[0]["object"]["data"]
            types = {b["type"] for b in sent["blocks"]}
            assert kinds <= types and not (({"findings", "map", "table"} - kinds) & types), (draws, types)
            SheetData.model_validate(sent)
            assert ack.status == "shown"
            kept = presenter.objects["sheet:x"].data
            assert kept is not None and any(
                b["type"] == "findings" for b in kept["blocks"]
            )  # a tap grounds on it


# --- review fixes -------------------------------------------------------------------------------------


def test_a_model_number_s_last_digit_is_never_part_of_the_price() -> None:
    """H1: "Sony WH-1000XM5 199,00 €" (the docstring's own example) was read as 5199 €."""
    text = "Amazon Sony WH-1000XM5 199,00 € spedizione gratuita. MediaWorld Sony WH-1000XM5 219,99 €"
    found = answers.offers(text, "https://www.trovaprezzi.it/x")
    assert [(o["shop"], o["price"]) for o in found] == [("Amazon", 199.0), ("MediaWorld", 219.99)]
    assert [answers.amount(x) for x in ("1 299,00",)] == [1299.0]  # a thousands space still reads


def test_l_the_shop_after_a_price_is_never_the_next_offer_s() -> None:
    found = answers.offers("Offerte: 199,00 € Amazon 210,00 € MediaWorld", "https://www.idealo.it/x")
    assert sorted((o["shop"], o["price"]) for o in found) == [("Amazon", 199.0), ("MediaWorld", 210.0)]


def test_programme_excerpts_carry_no_link_and_their_values_taint() -> None:
    """H2 (the quarantine's first rules): events_on gave the planner a page's raw text, links and addresses
    included, and none of its values tainted an act."""
    from pisa.brain import untrusted

    url = "https://www.auditorium.com/it/eventi"
    page = PROGRAMME + " Biglietti: scrivi a promo@evil.example o vai su https://evil.example/paga subito."
    page = page.replace("Domenica", "Sabato 14 novembre 2026 info promo@evil.example. Domenica")
    web = Web({url: page}, [{"title": "Programma", "url": url, "snippet": "eventi"}])
    tools, _, _ = _tools(web)
    taint = untrusted.watch_turn()
    read = asyncio.run(tools["events_on"]("2026-11-14", "Roma", "concerti"))
    values = taint.values()
    assert "Inventato" in read and "promo@evil.example" not in read and "evil.example/paga" not in read
    assert any(what == "address" and "promo@evil.example" in value for what, value in values)


def test_opening_hours_are_never_tonight_s_tables() -> None:
    hours = "Trattoria Da Mario. Orari: 12:30 - 15:00, 19:30 - 23:30. Chiuso il lunedì. Prenota un tavolo."
    assert answers.slot_times(hours, date(2026, 11, 14), relative=("oggi", "stasera")) == []
    dated = "Sabato 14 novembre orari: aperto 19:30 - 23:30. Prenota un tavolo."
    assert answers.slot_times(dated, date(2026, 11, 14)) == []
    widget = "Prenota un tavolo. Stasera: disponibilità alle 19:30, 20:00 e 21:15."
    assert answers.slot_times(widget, date(2026, 11, 14), relative=("oggi", "stasera")) == [
        "19:30", "20:00", "21:15",
    ]  # fmt: skip


def test_an_event_s_price_stands_beside_it_never_on_its_date() -> None:
    page = "Sabato 15 novembre ore 21:00 Concerto Blu al Teatro Verdi, biglietti 12 €."
    day = date(2026, 11, 15)
    assert answers.event_checked(page, day, title="Concerto Blu", time="21:00", venue=None, price="12 €",
                                 age=None) is None  # fmt: skip
    assert answers.event_checked(page, day, title="Concerto Blu", time="21:00", venue=None, price="15 €",
                                 age=None) == "its price isn't on that page"  # fmt: skip


def test_dotted_times_and_dates_with_their_year_read() -> None:
    day = date(2026, 11, 15)
    page = "Programma. Sabato 15 novembre ore 21.00 Concerto Blu, 12 €. Domenica 16 novembre ore 18.00 Altro"
    assert answers.event_checked(page, day, title="Concerto Blu", time="21:00", venue=None, price="12 €",
                                 age=None) is None  # fmt: skip
    assert answers.day_at("Sabato 15/11/2026 ore 21:00", day) and answers.day_at("Sabato 15.11.26", day)
    assert answers.event_checked(page, day, title="Altro", time="18:00", venue=None, price=None,
                                 age=None) is not None  # Sunday's still never Saturday's  # fmt: skip


def test_a_number_the_page_doesn_t_show_never_reaches_a_card() -> None:
    url = "https://www.trovaprezzi.it/cuffie/sony"
    web = Web({url: COMPARATOR}, [{"title": "Sony WH-1000XM5 prezzi", "url": url, "snippet": "Confronta"}])
    tools, _, shown = _tools(web)
    asyncio.run(tools["shop_prices"]("Sony WH-1000XM5"))
    out = asyncio.run(tools["show_products"]("Cuffie", [
        answer_tools.ProductPick(offer="o1", title="Sony WH-1000XM5 a soli 99 €"),
        answer_tools.ProductPick(offer="o2", title="Sony WH-1000XM5"),
    ]))  # fmt: skip
    assert "title has a number" in out
    assert [p["title"] for p in shown[0]["blocks"]] == ["Sony WH-1000XM5"]
    url2 = "https://www.auditorium.com/it/eventi"
    tools, _, shown = _tools(Web({url2: PROGRAMME}, [{"title": "P", "url": url2, "snippet": "e"}]))
    asyncio.run(tools["events_on"]("2026-11-14", "Roma"))
    sneaked = answer_tools.EventIn(
        title="Concerto di Franco Inventato", time="21:00", url=url2, why="Ultimi posti a 12 €"
    )
    asyncio.run(tools["show_events"]("Sabato", "2026-11-14", [sneaked]))
    [found] = [b for b in shown[0]["blocks"] if b["type"] == "findings"]
    assert "why" not in found["rows"][0]


def test_a_phone_that_draws_the_map_only_gets_pins_that_name_what_it_has() -> None:
    data, _ = _composed()
    only_map = visual.answers_for_phone(data, frozenset({"map"}), "it-IT")
    SheetData.model_validate(only_map)  # was: "every pin names a row or a product of this sheet"
    assert not any(b["type"] in ("map", "findings") for b in only_map["blocks"])


def test_a_row_s_open_at_asked_or_recognition_off_the_contract_is_left_out() -> None:
    row_in = {"name": "Fittizia", "place_id": "a", "open_at_asked": {"ok": None},
              "recognition": [{"guide": "Michelin", "award": "★", "url": "http://x"}]}  # fmt: skip
    out = answers.place_row(1, row_in, {"open_now": True}, pick=True)
    assert out["open_at_asked"] == {"ok": True, "source": "map"} and "recognition" not in out
    SheetData.model_validate({"title": "x", "blocks": [{"type": "findings", "rows": [out]}]})


def test_l_a_booking_request_doesn_t_offer_the_availability_tool() -> None:
    assert "table_slots" not in tool_choice.tools_for("prenotami un tavolo da Gabri venerdì alle 21 per 2")
    assert "table_slots" in tool_choice.tools_for("c'è posto stasera da Fittizia?")
