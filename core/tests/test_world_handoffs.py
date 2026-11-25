# ruff: noqa: E501
"""Pisa prepares, the user taps. One test (or a
few) per row; each fails when its fix is reverted (the handoff's table)."""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from pisa.domain import decisions
from pisa.domain import handoffs as rules
from pisa.domain.sheet import SheetData
from tests.owner import owner_at
from tests.world_fake import FakePlaces, SheetPhone

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 28, 18, 0, tzinfo=ROME)


class FakeHandoffs:
    """HandoffsData in memory, with the real service's phone confirmation."""

    def __init__(self) -> None:
        from pisa.services.handoffs import HandoffService

        self.preps: list[dict[str, Any]] = []
        self.settled: list[tuple[str, str]] = []
        self._phones = HandoffService.__new__(HandoffService)
        self._phones._asked, self._phones._confirmed = {}, {}

    async def prepare(self, **kw: Any) -> str:
        prep = {"id": str(uuid.uuid4()), "opened": False, "asked": False, "created_at": NOW, **kw}
        self.preps.append(prep)
        return prep["id"]

    async def open_preps(self) -> list[dict[str, Any]]:
        return [p for p in self.preps if not any(s[0] == p["id"] for s in self.settled)]

    async def settle(self, prep_id: str, outcome: str) -> dict[str, Any] | None:
        prep = next((p for p in await self.open_preps() if p["id"] == prep_id), None)
        if prep is not None:
            self.settled.append((prep_id, outcome))
        return prep

    def asked_phone(self, device_id: Any, phone: str, turn_id: str) -> None:
        self._phones.asked_phone(device_id, phone, turn_id)

    def phone_confirmed(self, device_id: Any, phone: str, yes: bool, turn_id: str) -> bool:
        return self._phones.phone_confirmed(device_id, phone, yes, turn_id)


class FakeData:
    """AssistantData's reads the tools make: his calendar's day and his open tasks."""

    def __init__(
        self, events: dict[date, list[tuple[str, str]]] | None = None, tasks: list[str] | None = None
    ) -> None:
        self.events = events or {}
        self.titles = tasks or []

    def today(self) -> date:
        return NOW.date()

    async def agenda_day(self, day: date) -> dict[str, Any]:
        return {"date": day.isoformat(), "events": [
            {"title": "x", "all_day": False, "start": f"{day.isoformat()}T{a}:00+01:00", "end": f"{day.isoformat()}T{b}:00+01:00"}
            for a, b in self.events.get(day, [])
        ]}  # fmt: skip

    async def tasks(self, status: str) -> list[Any]:
        from types import SimpleNamespace

        return [SimpleNamespace(title=t) for t in self.titles]


class FakeActions:
    def __init__(self) -> None:
        self.created: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def create_event(self, *args: Any, **kwargs: Any) -> str:
        self.created.append((args, kwargs))
        return "done: «Cena da Glicine» venerdì 30 alle 21"


def _tools(
    words: str,
    *,
    handoffs: FakeHandoffs | None = None,
    phone: SheetPhone | None = None,
    places: Any = None,
    data: Any = None,
    actions: Any = None,
    lang: str = "it-IT",
    turn_id: str | None = None,
) -> tuple[dict[str, Any], FakeHandoffs, SheetPhone]:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.handoff_tools import add_handoff_tools
    from pisa.brain.present_tools import Presenter
    from pisa.brain.profile_tools import ProfileTurn

    handoffs = handoffs or FakeHandoffs()
    phone = phone or SheetPhone()
    presenter = Presenter(device=phone, words=words, lang=lang, today=NOW.date(), objects={})
    turn = ProfileTurn(words=words, device=uuid.UUID(int=7), lang=lang, turn_id=turn_id or uuid.uuid4().hex)
    agent: Agent[None, str] = Agent(TestModel())
    map_: Any = places if places is not None else FakePlaces()
    add_handoff_tools(agent, handoffs, lambda: presenter, turn, places=map_,
                      data=data, actions=actions, owner=owner_at(), clock=lambda: NOW)  # fmt: skip
    return {n: t.function for n, t in agent._function_toolset.tools.items()}, handoffs, phone


def _run(coro: Any) -> Any:
    with decisions.collecting():
        return asyncio.run(coro)


# --- row 1: the contract (the handoff block, handoff_opened) -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
        "http://wa.me/1",
        "https://wa.me/ x",
        "intent://x#Intent;end",
    ],
)
def test_a_handoff_block_opens_only_https_or_pisa_s_schemes(url: str) -> None:
    block = {
        "type": "handoff",
        "handoff_id": str(uuid.uuid4()),
        "kind": "open_url",
        "title": "Apri",
        "url": url,
    }
    with pytest.raises(ValidationError):
        SheetData.model_validate({"title": "x", "blocks": [block]})


def test_a_call_needs_its_number_and_the_rest_a_url() -> None:
    base = {"type": "handoff", "handoff_id": str(uuid.uuid4()), "title": "Chiama"}
    with pytest.raises(ValidationError):
        SheetData.model_validate(
            {"title": "x", "blocks": [base | {"kind": "call", "url": "https://wa.me/1"}]}
        )
    with pytest.raises(ValidationError):
        SheetData.model_validate({"title": "x", "blocks": [base | {"kind": "message"}]})
    ok = SheetData.model_validate(
        {"title": "x", "blocks": [base | {"kind": "call", "phone": "+390600005678"}]}
    )
    assert ok.model_dump()["blocks"][0]["phone"] == "+390600005678"


def test_handoff_opened_is_a_device_message() -> None:
    from pisa.services.session_messages import HandoffOpened
    from tests.live_app import message_of

    raw = json.dumps({"v": 1, "id": str(uuid.uuid4()), "ts": "2026-10-28T18:00:00+01:00", "type": "handoff_opened",
                      "handoff_id": str(uuid.uuid4()), "object_id": f"sheet:{uuid.uuid4()}"})  # fmt: skip
    assert isinstance(message_of(raw), HandoffOpened)


# --- row 3: every URL from Pisa's templates, its parameters encoded (no open redirect) ----------------------------


def test_a_message_s_words_never_change_the_url() -> None:
    evil = "ciao&phone=1#x https://evil.example/?a=b"
    url = rules.whatsapp("+393331234567", evil)
    assert url.startswith("https://wa.me/393331234567?text=") and url.count("&") == 0 and "#" not in url
    assert unquote(url.split("text=", 1)[1]) == evil and rules.safe_url(url)
    sms = rules.sms(None, "a&body=b")
    assert sms == "sms:&body=a%26body%3Db" and rules.safe_url(sms)
    assert "&" not in rules.uber(None, (41.8, 12.2), "A&dropoff[latitude]=0").split("nickname%5D=", 1)[1]


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://wa.me/39333?text=x", True), ("tel:+390600005678", True), ("https://evil.example/x", False),
        ("javascript:alert(1)", False), ("data:x", False), ("https://user:pw@wa.me/1", False),
        ("https://www.google.com.evil.example/maps", False), ("https://wa.me/ x", False),
        # L4: tel: only the button's own number (no forwarding codes, no premium), sms: Pisa's strict form
        ("tel:*21*3331234567%23", False), ("tel:+390600009999", False), ("tel:+39899123456", False),
        ("sms:&body=Arrivo", True), ("sms:+393331234567&body=a%20b", True), ("sms:&body=a b", False),
        ("sms:+393331234567;body=x", False), ("maps:q=x", False), ("uber://?action=setPickup", False),
    ],
)  # fmt: skip
def test_safe_url_keeps_only_template_hosts_and_schemes(url: str, ok: bool) -> None:
    assert rules.safe_url(url, phone="+390600005678" if url != "tel:+39899123456" else "+39899123456") is ok


def test_a_call_s_tel_and_its_number_agree() -> None:
    base = {"type": "handoff", "handoff_id": str(uuid.uuid4()), "title": "Chiama", "kind": "call"}
    with pytest.raises(ValidationError):
        SheetData.model_validate(
            {"title": "x", "blocks": [base | {"url": "tel:+390600001111", "phone": "+390600005678"}]}
        )
    with pytest.raises(ValidationError):
        SheetData.model_validate({"title": "x", "blocks": [base | {"url": "tel:*21*3331234567"}]})


@pytest.mark.parametrize(
    ("site", "kept"),
    [
        ("https://www.glicine.example/prenota", True), ("http://www.glicine.example/", False),
        ("https://www.glicine.example/r?to=https://evil.example", False),
        ("https://www.glicine.example/r?to=https%3A%2F%2Fevil.example", False),
        ("https://user@www.glicine.example/", False), ("https://www.glicine.example:8443/", False),
        ("https://localhost/", False), ("https://www.glicine.example//evil.example", False),
    ],
)  # fmt: skip
def test_a_place_s_own_site_only_when_it_redirects_nowhere(site: str, kept: bool) -> None:
    assert (rules.place_site(site) is not None) is kept
    assert rules.safe_url(site, site=site) is kept


def test_a_redirecting_site_falls_back_to_the_platform_s_page() -> None:
    places = FakePlaces()
    import tests.world_fake as world

    original = world.PLACES["glicine"]["website"]
    world.PLACES["glicine"]["website"] = "https://www.glicine.example/go?u=https://evil.example"
    try:
        tools, _, phone = _tools("prenotami da Glicine", places=places)
        _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    finally:
        world.PLACES["glicine"]["website"] = original
    online = phone.handoffs()[0]
    assert online["url"] == "https://www.thefork.it/search?queryText=Glicine%20Roma"


def test_the_last_check_refuses_a_url_off_pisa_s_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever built it (a template changed by mistake), a URL off Pisa's hosts never reaches a button."""
    monkeypatch.setitem(rules.STORES, "glovo", "https://evil.example/glovo")
    tools, handoffs, phone = _tools("ordina su glovo il pane")
    said = _run(tools["prepare_shopping"](store="glovo", items=["pane"], with_tasks=False))
    assert said.startswith("Not prepared") and phone.sent == [] and handoffs.preps == []


# --- row 4: a table, prepared (two buttons: the page and the call with its script); nothing booked ------------


def test_a_table_is_a_sheet_with_the_page_and_the_call() -> None:
    actions = FakeActions()
    tools, handoffs, phone = _tools("prenotami un tavolo da Glicine venerdì alle 21 per 2", actions=actions)
    said = _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2, name="Ferrante"))
    online, call = phone.handoffs()
    assert (online["kind"], online["url"]) == ("open_url", "https://www.glicine.example/prenota")
    assert (call["kind"], call["url"], call["phone"]) == ("call", "tel:+442079460718", "+442079460718")
    assert (
        call["text"]
        == "Buonasera, vorrei prenotare un tavolo per 2 persone venerdì 30 alle 21 a nome Ferrante."
    )
    assert "Nothing is booked" in said and actions.created == []  # no write before their word
    prep = handoffs.preps[0]
    assert prep["purpose"] == "booking" and prep["details"]["at"] == "2026-10-30T21:00:00+01:00"
    assert "_site" not in prep["details"] and prep["object_id"] == phone.sent[0]["object"]["object_id"]


def test_without_a_site_or_a_number_the_platform_s_page_alone() -> None:
    tools, _, phone = _tools("prenota alla Trattoria Senza Sito")
    said = _run(tools["prepare_table"](place="Trattoria Senza Sito", start="2026-10-30T20:00", people=4))
    assert [h["kind"] for h in phone.handoffs()] == ["open_url"] and "thefork.it/search" in phone.handoffs()[
        0
    ]["url"]
    assert "no phone number" in said
    assert _run(tools["prepare_table"](place="Glicine", start="2026-10-27T21:00", people=2)).startswith(
        "Not prepared"
    )


# --- row 5: a ride (Uber/Lyft universal links; a taxi's dispatcher with the script) ---------------------------


def test_uber_and_lyft_open_with_pickup_and_dropoff() -> None:
    tools, _, phone = _tools("chiamami un Uber per l'aeroporto")
    _run(tools["prepare_ride"](destination="aeroporto", app="uber"))
    _run(tools["prepare_ride"](destination="aeroporto", app="lyft"))
    uber, lyft = (h["url"] for h in phone.handoffs())
    assert uber == ("https://m.uber.com/ul/?action=setPickup&pickup%5Blatitude%5D=41.825000&pickup%5Blongitude%5D=12.429000"
                    "&dropoff%5Blatitude%5D=41.800000&dropoff%5Blongitude%5D=12.239000&dropoff%5Bnickname%5D=Aeroporto%20di%20Fiumicino")  # fmt: skip
    assert (
        lyft.startswith("https://lyft.com/ride?id=lyft&pickup%5Blatitude%5D=41.825000")
        and "destination%5Blatitude%5D=41.800000" in lyft
    )


def test_a_taxi_in_rome_is_the_radio_taxi_with_what_to_say_and_no_place_no_pickup() -> None:
    tools, _, phone = _tools("chiamami un taxi per l'aeroporto")
    _run(tools["prepare_ride"](destination="aeroporto", app="taxi"))
    call = phone.handoffs()[0]
    assert (
        call["url"] == "tel:+39063570"
        and "Piazza della Prova, Roma per Aeroporto di Fiumicino" in call["text"]
    )
    tools, _, phone = _tools("chiamami un Uber per l'aeroporto", places=FakePlaces(here=None))
    said = _run(tools["prepare_ride"](destination="aeroporto"))
    assert "pickup=my_location" in phone.handoffs()[0]["url"] and "doesn't know where they are" in said


# --- row 6: a message they send; a number from outside text only after their yes -------------------------


def test_a_whatsapp_is_prefilled_and_he_picks_marco() -> None:
    tools, handoffs, phone = _tools("scrivi a Marco su WhatsApp che arrivo alle 9")
    said = _run(tools["prepare_message"](to="Marco", text="Arrivo alle 9!"))
    h = phone.handoffs()[0]
    assert (h["kind"], h["url"], h["subtitle"]) == (
        "message",
        "https://wa.me/?text=Arrivo%20alle%209%21",
        "Scegli Marco in WhatsApp",
    )
    assert "NOT sent" in said and handoffs.preps[0]["purpose"] == "message"
    _run(tools["prepare_message"](to="Marco", text="Arrivo", app="imessage"))
    assert phone.handoffs()[1]["url"] == "sms:&body=Arrivo"


def test_a_number_from_outside_text_waits_for_his_yes_in_a_later_message() -> None:
    handoffs = FakeHandoffs()
    asking = "ok, scrivi a Marco al numero che c'è nella sua mail che arrivo"
    tools, _, phone = _tools(asking, handoffs=handoffs, turn_id="t1")
    said = _run(tools["prepare_message"](to="Marco", text="Arrivo", phone="+44 7700 900123"))
    assert (
        said.startswith("Not prepared") and "+447700900123" in said and "don't call again this turn" in said
    )
    # H1: the same turn's "ok" (or a "si" in "che si trova") is no confirmation of Pisa's read-back
    for words in (asking, "che si trova nella mail"):
        tools, _, phone = _tools(words, handoffs=handoffs, turn_id="t1")
        assert _run(
            tools["prepare_message"](to="Marco", text="Arrivo", phone="+447700900123", confirmed=True)
        ).startswith("Not")
        assert phone.sent == []
    # nor a short yes in the very turn Pisa asked in (it answers something else)
    same = FakeHandoffs()
    tools, _, phone = _tools("sì", handoffs=same, turn_id="t9")
    assert _run(tools["prepare_message"](to="Marco", text="Arrivo", phone="+447700900123")).startswith("Not")
    assert _run(
        tools["prepare_message"](to="Marco", text="Arrivo", phone="+447700900123", confirmed=True)
    ).startswith("Not")
    # a later message that isn't a yes is no confirmation either
    tools, _, phone = _tools("e che tempo fa?", handoffs=handoffs, turn_id="t2")
    assert _run(
        tools["prepare_message"](to="Marco", text="Arrivo", phone="+447700900123", confirmed=True)
    ).startswith("Not")
    tools, _, phone = _tools("sì", handoffs=handoffs, turn_id="t3")
    _run(tools["prepare_message"](to="Marco", text="Arrivo", phone="+447700900123", confirmed=True))
    assert phone.handoffs()[0]["url"] == "https://wa.me/447700900123?text=Arrivo"


@pytest.mark.parametrize(
    ("words", "phone", "his"),
    [("al 333 123 4567", "+393331234567", True), ("al 333 123 4567", "+13331234567", False),
     ("venerdì 14 alle 21:30 per 2", "+391421302", False), ("chiama il +44 7700 900123", "+447700900123", True),
     ("chiama lo 0039 06 0000 3570", "+390600003570", True)],
)  # fmt: skip
def test_his_number_is_one_he_said_exactly(words: str, phone: str, his: bool) -> None:
    """M3: the country code counts, and a time or a date never adds up to a number."""
    assert rules.phone_in(words, phone, "IT") is his  # the suite's user: Italy


def test_a_number_in_his_own_words_is_his() -> None:
    tools, _, phone = _tools("scrivi su WhatsApp al 333 123 4567 che arrivo")
    _run(tools["prepare_message"](to="333 123 4567", text="Arrivo", phone="+39 333 1234567"))
    assert phone.handoffs()[0]["phone"] == "+393331234567"


# --- row 7: a call they place: the number, 3 lines, the free times from their calendar ---------------------------


def test_a_call_with_its_script_and_the_slots_that_work() -> None:
    thursday, friday = date(2026, 10, 29), date(2026, 10, 30)
    data = FakeData(events={thursday: [("09:00", "12:30"), ("13:00", "19:00")], friday: [("09:00", "10:00")]})
    tools, handoffs, phone = _tools("chiama il dentista per spostare l'appuntamento", data=data)
    _run(
        tools["prepare_call"](
            who="Studio Dentistico Bianchi",
            script=["Sono Ferrante.", "Vorrei spostare.", "Grazie.", "In più."],
        )
    )
    h = phone.handoffs()[0]
    assert (h["kind"], h["phone"]) == ("call", "+442079460432") and "sulla mappa" in h["notes"][0]
    lines = h["text"].split("\n")
    assert lines[:3] == ["Sono Ferrante.", "Vorrei spostare.", "Grazie."]
    assert (
        lines[3] == "Mi andrebbero bene: venerdì 30 10:00-19:00, lunedì 2 09:00-19:00, martedì 3 09:00-19:00."
    )  # Thursday is full
    assert handoffs.preps[0]["purpose"] == "call"


def test_no_number_known_asks_him() -> None:
    tools, _, phone = _tools("chiama la Trattoria Senza Sito")
    assert (
        "Ask the user" in _run(tools["prepare_call"](who="Trattoria Senza Sito", script=["Ciao"]))
        and phone.sent == []
    )


# --- row 8: groceries and delivery: the store's page and the list they paste -----------------------------------


def test_the_list_from_his_words_and_his_shopping_tasks() -> None:
    data = FakeData(
        tasks=["Comprare il latte", "Chiamare la banca", "Comprare le uova", "Spesa al supermercato"]
    )
    tools, _, phone = _tools("fammi la spesa su Esselunga: pane e latte", data=data)
    _run(tools["prepare_shopping"](store="esselunga", items=["pane", "Latte"]))
    h = phone.handoffs()[0]
    assert h["url"] == "https://www.esselunga.it/it-it/spesa-online.html"
    assert h["text"].split("\n") == ["- pane", "- Latte", "- uova"]  # their tasks' "latte" is the same thing


def test_only_his_words_and_things_to_buy_in_his_tasks() -> None:
    """L1: never a task that isn't buying something, never an item he didn't say."""
    data = FakeData(tasks=["Get back to Marco", "Prendere i bambini a scuola", "Prendi appuntamento dal dentista",
                           "Fare la spesa", "Comprare le uova"])  # fmt: skip
    tools, _, phone = _tools("ordina su Glovo il pane", data=data)
    _run(tools["prepare_shopping"](store="glovo", items=["pane", "vodka", "uova"]))
    assert phone.handoffs()[0]["text"].split("\n") == ["- pane", "- uova"]


# --- row 9: Pisa asks once how it went, after the tap (the rails), never twice ---------------------------------


def _service(client: Any) -> Any:
    return client.app.state.runtime.handoffs


def _prepare(client: Any, purpose: str, buttons: int = 1) -> tuple[str, list[str]]:
    service = _service(client)
    ids = [str(uuid.uuid4()) for _ in range(buttons)]
    prep = client.portal.call(lambda: service.prepare(
        device_id=None, purpose=purpose, target="Glicine", lang="it-IT", details={"at": "2026-10-30T21:00:00+01:00"},
        buttons=[{"id": i, "kind": "call", "title": f"b{n}", "url": "tel:+390600005678", "phone": "+390600005678"}
                 for n, i in enumerate(ids)], object_id="sheet:x",
    ))  # fmt: skip
    return prep, ids


def test_the_follow_up_is_asked_once_and_only_when_it_is_due(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    service = _service(client)
    prep, (online, call) = _prepare(client, "booking", 2)
    _, (ride,) = _prepare(client, "ride")
    assert client.portal.call(lambda: service.opened(None, online, "sheet:x")) is True
    assert client.portal.call(lambda: service.opened(None, online, "sheet:x")) is False  # the first tap only
    assert client.portal.call(lambda: service.opened(None, call, "sheet:other")) is False  # not its sheet
    assert client.portal.call(lambda: service.opened(None, ride, "sheet:x")) is True
    assert client.portal.call(service.tick) == 0  # not yet: 20 minutes after the tap
    clock.now += timedelta(minutes=21)
    assert client.portal.call(service.tick) == 1  # the booking, not the ride
    assert client.portal.call(service.tick) == 0  # never twice
    assert db.execute(
        "SELECT count(*) FROM attend_candidates WHERE key = %s", (f"handoff:{prep}",)
    ).fetchone() == (1,)
    text = db.execute("SELECT text FROM attend_candidates WHERE key = %s", (f"handoff:{prep}",)).fetchone()
    assert text is not None and text[0].startswith("Com'è andata la prenotazione da Glicine?")


def test_no_follow_up_once_he_said_how_it_went(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    service = _service(client)
    prep, (online,) = _prepare(client, "booking")
    client.portal.call(lambda: service.opened(None, online, "sheet:x"))
    assert client.portal.call(lambda: service.settle(prep, "worked")) is not None
    assert client.portal.call(lambda: service.settle(prep, "failed")) is None  # once
    clock.now += timedelta(minutes=30)
    assert client.portal.call(service.tick) == 0


def test_the_phone_s_tap_reaches_pisa_over_the_socket(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair
    from tests.live_app import live

    _, (online,) = _prepare(client, "call")
    device_id, token = pair(db)
    with live(client, token, device_id) as app:
        app.action("pisa.handoff_opened", "sheet:x", handoffId=str(uuid.uuid4()))  # unknown: dropped
        app.action("pisa.handoff_opened", "sheet:x", handoffId=online)
        assert app.ping() == []  # Pisa answers nothing to a tap
        app.check()
    row = db.execute("SELECT opened_at IS NOT NULL, follow_up_at - opened_at FROM handoff_preps").fetchone()
    assert row == (True, timedelta(minutes=15))


# --- row 10: nothing written before their word; a booking that worked goes in the calendar on it -----------------


def test_his_word_this_turn_or_nothing() -> None:
    actions = FakeActions()
    handoffs = FakeHandoffs()
    tools, _, _ = _tools("prenotami da Glicine", handoffs=handoffs, actions=actions)
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    tools, _, _ = _tools("e domani che tempo fa?", handoffs=handoffs, actions=actions)
    assert _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="sì, fatto")).startswith(
        "Not kept"
    )
    handoffs.preps[0]["opened"] = handoffs.preps[0]["asked"] = True
    tools, _, _ = _tools("ok grazie", handoffs=handoffs, actions=actions)  # a yes, but not the words cited
    assert _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="sì, fatto")).startswith(
        "Not kept"
    )
    tools, _, _ = _tools("no, non ancora", handoffs=handoffs, actions=actions)
    assert _run(
        tools["handoff_result"](prepared="Glicine", worked=True, owner_said="no, non ancora")
    ).startswith("Not kept")
    assert actions.created == [] and handoffs.settled == []


def test_a_booking_that_worked_goes_in_the_calendar_with_his_words() -> None:
    actions = FakeActions()
    handoffs = FakeHandoffs()
    tools, _, _ = _tools("prenotami da Glicine", handoffs=handoffs, actions=actions)
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    handoffs.preps[0]["opened"] = True  # they tapped it
    tools, _, _ = _tools("sì, fatto", handoffs=handoffs, actions=actions)
    said = _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="sì, fatto"))
    assert "booked" in said and handoffs.settled[0][1] == "worked"
    ((args, kwargs),) = actions.created
    assert args[:4] == ("Cena da Glicine", "2026-10-30T21:00", None, 120) and kwargs == {
        "grounded_by": "sì, fatto"
    }


def test_a_failed_booking_writes_nothing() -> None:
    actions = FakeActions()
    handoffs = FakeHandoffs()
    tools, _, _ = _tools("prenotami da Glicine", handoffs=handoffs, actions=actions)
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    tools, _, _ = _tools("no, era pieno", handoffs=handoffs, actions=actions)
    said = _run(tools["handoff_result"](prepared="Glicine", worked=False, owner_said="no, era pieno"))
    assert "Nothing written" in said and actions.created == [] and handoffs.settled[0][1] == "failed"


def test_the_model_s_create_event_has_no_way_around_his_yes() -> None:
    import inspect

    from pisa.brain import action_tools

    assert "grounded_by" not in inspect.getsource(action_tools)  # code only: never a tool's argument


@pytest.mark.parametrize(
    ("words", "outcome"),
    [("sì, fatto", "worked"), ("prenotato!", "worked"), ("done, booked it", "worked"), ("no, era pieno", "failed"),
     ("non ancora", "failed"), ("sì ma non ho chiamato", "failed"), ("che tempo fa?", None)],
)  # fmt: skip
def test_his_word_says_worked_or_failed_a_negation_wins(words: str, outcome: str | None) -> None:
    assert rules.outcome(words) == outcome


# --- row 11: a handoff only from Pisa's own tools: show_sheet can't put a button on the phone ---------------------


def test_show_sheet_drops_a_handoff_block() -> None:
    from pisa.brain.present_tools import sheet_blocks

    built, _ = sheet_blocks([{"type": "handoff", "kind": "open_url", "title": "x", "url": "https://wa.me/1"},
                             {"type": "text", "text": "ok"}], [], results=[], lang="it-IT")  # fmt: skip
    assert built == [{"type": "text", "text": "ok"}]


# --- row 12: the open preparations reach the brain (only when there are some); kept 90 days -------------------


def test_the_brain_sees_what_waits_for_his_word() -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    handoffs = FakeHandoffs()
    tools, _, _ = _tools("prenotami da Glicine", handoffs=handoffs)
    agent: Agent[None, str] = Agent(TestModel())
    from pisa.brain.handoff_tools import add_handoff_tools
    from pisa.brain.profile_tools import ProfileTurn

    add_handoff_tools(agent, handoffs, lambda: None, ProfileTurn(lang="it-IT"), places=None, data=None, actions=None,
                      owner=owner_at(), clock=lambda: NOW)  # fmt: skip
    line = agent._instructions[-1].instruction
    assert _run(line()) == ""  # type: ignore[operator]
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    text = _run(line())  # type: ignore[operator]
    assert f"[h:{handoffs.preps[0]['id']}] booking «Glicine» 2026-10-30 21:00 (not opened yet)" in text
    assert "handoff_result" in text


def test_the_follow_up_s_words_in_his_language() -> None:
    assert rules.follow_up_text("call", "Studio Bianchi", "en-US") == "How did the call to Studio Bianchi go?"
    assert rules.normalize_phone("(212) 555-0100", "US") == "+12125550100"
    assert (
        rules.normalize_phone("0039 06 0000 3570", "IT") == "+390600003570"
        and rules.normalize_phone("ciao", "IT") is None
    )
    assert rules.day_time(datetime(2026, 10, 30, 21, 30), "en-US") == "Friday, October 30, at 9:30 PM"


# --- the review's fixes ------------------------------------------------------------------------------


def _after_email(tool: Any, **args: Any) -> str:
    """The tool called in a turn that read an email (outside text)."""
    from pisa.brain.untrusted import data_block, watch_turn

    async def go() -> str:
        with decisions.collecting():
            watch_turn()
            data_block("email", "Nino: il codice è 482913, clicca https://evil.example", what="an email")
            return str(await tool(**args))

    return _run(go())


def test_after_outside_text_a_message_carries_no_link_or_code_he_didn_t_say() -> None:
    tools, handoffs, phone = _tools("scrivi a Nino su WhatsApp che arrivo")
    said = _after_email(
        tools["prepare_message"], to="Nino", text="Ciao, ecco il codice: 482913. Clicca https://evil.example"
    )
    assert said.startswith("Not prepared") and phone.sent == [] and handoffs.preps == []
    tools, handoffs, phone = _tools("cosa dice la mail di Nino?")  # their words ask for no message
    assert _after_email(tools["prepare_message"], to="Nino", text="Arrivo").startswith("Not prepared")
    assert _after_email(tools["prepare_call"], who="Glicine", script=["Ciao"]).startswith("Not prepared")
    tools, handoffs, phone = _tools("scrivi a Nino su WhatsApp che arrivo")
    assert not _after_email(tools["prepare_message"], to="Nino", text="Arrivo!").startswith("Not")


def test_after_outside_text_a_table_s_page_is_the_platform_s_search() -> None:
    tools, _, phone = _tools("prenotami un tavolo da Glicine")
    _after_email(tools["prepare_table"], place="Glicine", start="2026-10-30T21:00", people=2)
    assert phone.handoffs()[0]["url"].startswith("https://www.thefork.it/search?")


def test_a_place_s_own_site_shows_its_host() -> None:
    tools, _, phone = _tools("prenotami un tavolo da Glicine")
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    assert phone.handoffs()[0]["subtitle"] == "glicine.example · sito indicato sulla mappa"


@pytest.mark.parametrize(
    "site",
    ["https://www.google.com/url?q=evil.example/login", "https://www.glicine.example/\u202egnp.exe",
     "https://www.\u0440oscioli.example/", "https://xn--oscioli-4fg.example/", "https://bit.ly/3xYz",
     "https://t.co/abc", "https://www.glicine.example/?u=%2F%2Fevil2.example",
     "https://www.glicine.example/?u=%252F%252Fevil.example", "https://192.168.1.1/"],
)  # fmt: skip
def test_redirectors_shorteners_and_lookalike_hosts_are_no_place_s_site(site: str) -> None:
    assert rules.place_site(site) is None


def test_a_negation_anywhere_in_his_words_wins() -> None:
    actions, handoffs = FakeActions(), FakeHandoffs()
    tools, _, _ = _tools("prenotami da Glicine", handoffs=handoffs, actions=actions)
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    handoffs.preps[0]["opened"] = handoffs.preps[0]["asked"] = True
    tools, _, _ = _tools("sì ma non l'ho ancora fatto, lo faccio dopo", handoffs=handoffs, actions=actions)
    assert _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="fatto")).startswith(
        "Not kept"
    )
    assert actions.created == [] and handoffs.settled == []


def test_a_booking_never_opened_or_a_bare_yes_to_something_else_writes_nothing() -> None:
    actions, handoffs = FakeActions(), FakeHandoffs()
    tools, _, _ = _tools("prenotami da Glicine", handoffs=handoffs, actions=actions)
    _run(tools["prepare_table"](place="Glicine", start="2026-10-30T21:00", people=2))
    tools, _, _ = _tools("sì, fatto", handoffs=handoffs, actions=actions)  # never opened, never asked
    assert _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="sì, fatto")).startswith(
        "Not kept"
    )
    handoffs.preps[0]["opened"] = (
        True  # opened, but Pisa's follow-up hasn't reached them: "sì" answers something else
    )
    tools, _, _ = _tools("sì", handoffs=handoffs, actions=actions)
    assert _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="sì")).startswith(
        "Not kept"
    )
    assert actions.created == [] and handoffs.settled == []
    handoffs.preps[0]["asked"] = True  # Pisa asked: their bare yes answers it
    _run(tools["handoff_result"](prepared="Glicine", worked=True, owner_said="sì"))
    assert len(actions.created) == 1


def test_the_brain_sees_the_place_as_he_said_it_and_the_map_s_facts_as_outside_text() -> None:
    from pisa.brain.untrusted import current, watch_turn

    handoffs = FakeHandoffs()
    tools, _, _ = _tools("prenota da glicine ai gelsomini", handoffs=handoffs)
    said = _run(tools["prepare_table"](place="glicine ai gelsomini", start="2026-10-30T21:00", people=2))
    assert (
        handoffs.preps[0]["target"] == "glicine ai gelsomini" and "+442079460718" not in said
    )  # on their screen

    async def off_screen() -> tuple[str, list[str]]:
        watch_turn()
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        from pisa.brain.handoff_tools import add_handoff_tools as add
        from pisa.brain.present_tools import Presenter
        from pisa.brain.profile_tools import ProfileTurn

        agent: Agent[None, str] = Agent(TestModel())
        presenter = Presenter(
            device=None, words="prenota da glicine", lang="it-IT", today=NOW.date(), objects={}
        )
        map_: Any = FakePlaces()
        add(agent, handoffs, lambda: presenter, ProfileTurn(words="prenota da glicine", turn_id="x", lang="it-IT"),
            places=map_, data=None, actions=None, owner=owner_at(), clock=lambda: NOW)  # fmt: skip
        tool: Any = agent._function_toolset.tools["prepare_table"].function
        out = await tool(place="glicine", start="2026-10-30T21:00", people=2)
        return out, current().outside()

    said, outside = _run(off_screen())
    assert "+442079460718" in said and outside == ["place"]  # said in words, as the map's (outside) facts


def test_control_and_bidi_characters_never_reach_a_button() -> None:
    tools, _, phone = _tools("scrivi a Marco che arrivo alle 9")  # Their words are the text
    _run(tools["prepare_message"](to="Marco\u202e", text="Arrivo\u200b alle 9\u2066!"))
    h = phone.handoffs()[0]
    assert h["url"] == "https://wa.me/?text=Arrivo%20alle%209%21" and h["title"] == "WhatsApp a Marco"
    assert rules.whatsapp(None, "a\u202eb\u200c") == "https://wa.me/?text=ab"  # every template's parameter


def test_a_call_s_number_from_the_map_is_one_around_him_and_never_premium() -> None:
    places = FakePlaces()
    tools, _, phone = _tools("chiama lo studio dentistico bianchi", places=places)
    _run(tools["prepare_call"](who="Studio Dentistico Bianchi", script=["Ciao"], slots=False))
    assert places.searched == [("Studio Dentistico Bianchi", "here")]
    tools, _, phone = _tools("chiama il 899 123 456")
    assert "premium" in _run(tools["prepare_call"](who="x", script=["Ciao"], phone="899 123 456"))
    assert phone.sent == []


def test_asked_means_it_reached_him_and_it_waits_a_day_at_most(client, db, clock) -> None:  # type: ignore[no-untyped-def]
    from pisa.services.handoffs import HandoffService

    runtime = client.app.state.runtime
    service = HandoffService(runtime.pool, owner=owner_at(), clock=clock)  # no attend: at once
    _, (online,) = _prepare(client, "booking")
    client.portal.call(lambda: service.opened(None, online, "sheet:x"))
    clock.now += timedelta(minutes=21)
    assert client.portal.call(service.tick) == 1
    assert not client.portal.call(service.open_preps)[0]["asked"]  # queued, not delivered yet
    db.execute("UPDATE notifications SET delivered_via = 'ws', delivered_at = now()")
    assert client.portal.call(service.open_preps)[0]["asked"]
    _, (other,) = _prepare(client, "call")
    client.portal.call(lambda: runtime.handoffs.opened(None, other, "sheet:x"))
    clock.now += timedelta(minutes=16)
    client.portal.call(runtime.handoffs.tick)
    row = db.execute(
        "SELECT deadline - created_at FROM attend_candidates WHERE key LIKE 'handoff:%'"
    ).fetchone()
    assert row is not None and row[0] > timedelta(hours=23)


def test_a_tap_from_another_device_is_dropped(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    service = _service(client)
    mine, _ = pair(db)
    theirs, _ = pair(db)
    ids = [str(uuid.uuid4())]
    client.portal.call(lambda: service.prepare(
        device_id=uuid.UUID(mine), purpose="call", target="x", lang="it-IT", details={},
        buttons=[{"id": ids[0], "kind": "call", "title": "Chiama", "url": "tel:+390600005678", "phone": "+390600005678"}],
        object_id="sheet:x",
    ))  # fmt: skip
    assert client.portal.call(lambda: service.opened(uuid.UUID(theirs), ids[0], "sheet:x")) is False
    assert client.portal.call(lambda: service.opened(uuid.UUID(mine), ids[0], "sheet:x")) is True


def test_a_turn_s_reads_set_at_a_test_s_top_level() -> None:
    from pisa.brain.untrusted import current, data_block, watch_turn

    watch_turn()
    data_block("email", "x", what="an email")
    assert current().outside() == ["email"]


def test_the_next_test_starts_without_them() -> None:
    from pisa.brain.untrusted import current

    assert current().outside() == []


def test_the_follow_up_is_pisa_s_initiative_a_stop_pauses_it() -> None:
    """ "ferma tutto" pauses Pisa's own "com'è andata?"; preparing on his words never stops."""
    from pisa.services.worker_steps import INITIATIVE_STEPS

    assert "handoffs" in INITIATIVE_STEPS
