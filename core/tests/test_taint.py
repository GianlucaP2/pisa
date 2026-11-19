"""Their data never leaves
by a door unless it passed the strip. The types (`pisa.domain.taint`), the doors (`pisa.connectors.door.guard`
in the web search, the page reads, the map searches; the night's strict door), and the
test-time checker for f-strings (`tests/taint_check.py`)."""

import asyncio
import functools
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from pisa.connectors.tavily import Tavily
from pisa.domain import taint
from pisa.domain.errors import PisaError
from pisa.ports.places import Usage
from pisa.services.night import night_web
from tests.taint_check import door_findings, fstring_findings
from tests.test_curious import _his, _night, _rows, _world
from tests.test_night_frame import CONTACT

ADDRESS = "Via Inesistente 12, 40125 Bologna"


def _refused(err: PisaError) -> bool:
    return err.code == "validation_failed" and err.detail == taint.REFUSED


# --- the types ----------------------------------------------------------------------------------------------


def test_a_string_built_from_a_personal_stays_personal_except_by_an_f_string_or_join() -> None:
    p = taint.Personal(CONTACT)
    built = [
        "Chi è " + p,
        p + "?",
        "ciao %s" % p,  # noqa: UP031
        p[:7],
        p.lower(),
        p.strip(),
        str(p),
        p.replace("a", "b"),
        p * 2,
    ]
    assert all(isinstance(b, taint.Personal) for b in built)
    assert all(isinstance(w, taint.Personal) for w in p.split())
    # the two ways out of the type: what the checker exists for (item 3)
    assert type(f"Chi è {p}") is str and type(" ".join(["Chi è", p])) is str
    # only the strip makes a `Stripped`, and it takes their names out
    out = taint.strip("dove lavora " + p + " a Bologna", [p, "Bologna"])
    assert isinstance(out, taint.Stripped) and out == "dove lavora a" and out.removed == 3
    assert taint.untouched("fotovoltaico in affitto", [p]) == "fotovoltaico in affitto"
    assert taint.untouched("il tetto di " + p, [p]) is None


def test_a_door_refuses_a_personal_and_a_request_holding_one_and_the_strict_door_anything_unstripped() -> (
    None
):
    p = taint.Personal(ADDRESS)
    assert taint.refused("forni a legna", ["x.example"]) == 0
    assert taint.refused(p) == 1 and taint.refused({"prompt": "un sito per " + p, "title": p}) == 2
    assert taint.refused(taint.strip(p, [p])) == 0
    assert taint.refused("forni a legna", strict=True) == 1  # the night's door: a plain str is refused too
    assert taint.refused(taint.strip("forni a legna", []), strict=True) == 0
    err = taint.refusal("the web search", 2)
    assert _refused(err) and ADDRESS not in err.message and "2 personal values" in err.message


# --- the doors ----------------------------------------------------------------------------------------------


class _Places:
    """The night's provider behind `night_web`: records what reached it."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.providers = self

    async def night_search(self, query: str, **_: Any) -> list[Any]:
        self.sent.append(query)
        return [{"url": "https://fonte.example/a", "title": "Fonte", "content": "x"}]


def test_privacy_a_contact_s_name_in_an_ignorance_row_never_reaches_an_outgoing_query_at_the_door(
    client, db, clock
) -> None:  # type: ignore[no-untyped-def]
    world = _world(client, db, clock)
    db.execute(
        "INSERT INTO people (id, name, name_key, source, emails, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, 'ornella ravaglia', 'email', '{ornella.f@example.invalid}', now(), "
        "now())",
        (CONTACT,),
    )
    _his(db, "Pisa, tienimelo aperto: conviene a Ornella Ravaglia il fotovoltaico in affitto?")
    world.echo = True  # the model copies her name into every query it writes
    _night(client)
    assert world.sent
    for _, q in world.sent:  # every query that reached the door is the strip's, and her name is not in it
        assert isinstance(q, taint.Stripped), type(q)
        assert "ornella" not in q.casefold() and "ravaglia" not in q.casefold(), q
    # the row itself, read as theirs (`Personal`), handed to the night's door unstripped: refused, nothing
    # sent
    (question,) = _rows(db, "SELECT question FROM pisa_unknowns")[0]
    places = _Places()
    door = night_web(places)
    for raw in (taint.Personal(question), str(question)):
        with pytest.raises(PisaError) as err:
            asyncio.run(door(raw))  # pyright: ignore[reportArgumentType]
        assert _refused(err.value)
    assert places.sent == []


def test_a_stripped_query_goes_out_and_a_personal_one_does_not() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"results": [{"url": "https://a.example", "title": "A"}], "usage": {}}
        )

    async def meter(_: Usage, __: bool) -> None:
        return None

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            tavily = Tavily(http, "sk-test-xxxx", meter)
            p = taint.Personal(CONTACT)
            clean = taint.strip("commercialista di " + p + " partita iva forfettaria", [p])
            assert len(await tavily.search(clean, max_results=5)) == 1
            assert (
                len(await tavily.search("forni a legna Bologna", max_results=5)) == 1
            )  # the model's own words
            with pytest.raises(PisaError) as err:
                await tavily.search("commercialista di " + p, max_results=5)
            assert _refused(err.value)
            with pytest.raises(PisaError):
                await tavily.search(
                    "forni", include_domains=[taint.Personal("ornella.example")], max_results=5
                )

    asyncio.run(go())
    assert [b["query"] for b in sent] == [
        "commercialista di partita iva forfettaria",
        "forni a legna Bologna",
    ]
    places = _Places()
    asyncio.run(night_web(places)(taint.strip("fotovoltaico in affitto sui tetti", [])))  # pyright: ignore[reportArgumentType]
    assert places.sent == ["fotovoltaico in affitto sui tetti"]  # the night's door lets the strip's text out


def test_the_night_s_search_count_on_the_sunday_card_is_unchanged_for_clean_queries(
    client, db, clock
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_stanotte import _run, _shadow_nights, _svc

    night = client.app.state.runtime.night
    last = _shadow_nights(client, db, clock, 7)  # the gate open; tonight (the 7th) still running
    clock.now = datetime(last.year, last.month, last.day, 1, 40, tzinfo=UTC)
    places = _Places()
    night._web = night_web(places)  # pyright: ignore[reportPrivateUsage]  # the real door, a fake provider
    queries = ("fotovoltaico in affitto sui tetti", "forni a legna consumi", "biciclette cargo usate prezzi")
    for q in queries:
        assert _run(client, functools.partial(night.search, q, kind="c3_hunter")) is not None
    assert places.sent == list(queries)  # every clean query went out, unchanged
    clock.now = datetime(last.year, last.month, last.day, 18, 0, tzinfo=UTC)
    part = _run(client, _svc(client).sunday, "it-IT")
    assert {"type": "stat", "label": "Ricerche", "value": str(len(queries))} in part


# --- Item 1: every door, each with its own test ----------------------------------------------------
# Each test hands one door a `Personal` value and sees the refusal with nothing sent; a clean value goes out
# through the same door, so the guard refuses only their values. `tests/mutate_taint.py` removes each guard
# alone.


def _sent_nothing(fake: Any, *routes: str) -> None:
    assert not [r for r in fake.routes() if r in routes], fake.routes()


def test_door_apple_map_search_refuses_a_personal_query() -> None:
    from tests.places_fake import FakePlaces
    from tests.test_place_adapters import apple

    fake = FakePlaces()
    maps, _, _ = apple(fake)

    async def go() -> None:
        await maps.search("forni a legna", near=None, open_now=False, limit=3, lang="it")
        with pytest.raises(PisaError) as err:
            await maps.search(taint.Personal(ADDRESS), near=None, open_now=False, limit=3, lang="it")
        assert _refused(err.value)

    asyncio.run(go())
    assert fake.routes("apple.search") == ["apple.search"]  # only the clean one went out


def test_door_apple_geocode_refuses_a_personal_text_and_keeps_its_docstring() -> None:
    from pisa.connectors.apple_maps import AppleMaps
    from tests.places_fake import FakePlaces
    from tests.test_place_adapters import apple

    fake = FakePlaces()
    maps, _, _ = apple(fake)
    with pytest.raises(PisaError) as err:
        asyncio.run(maps.geocode("un sito per " + taint.Personal(ADDRESS), near=None, lang="it"))
    assert _refused(err.value)
    _sent_nothing(fake, "apple.token", "apple.geocode")
    assert (AppleMaps.geocode.__doc__ or "").startswith("Street addresses for a text")  # Item 4


def test_door_google_map_search_refuses_a_personal_query() -> None:
    from tests.places_fake import FakePlaces
    from tests.test_place_adapters import google

    fake = FakePlaces()
    maps, _ = google(fake)

    async def go() -> None:
        await maps.search("forni a legna", near=None, open_now=False, limit=3, lang="it")
        with pytest.raises(PisaError) as err:
            await maps.search(
                "vicino a " + taint.Personal(ADDRESS), near=None, open_now=False, limit=3, lang="it"
            )
        assert _refused(err.value)

    asyncio.run(go())
    assert fake.routes("google.search") == ["google.search"]  # only the clean one went out


def test_door_tavily_extract_refuses_a_link_holding_a_personal_value() -> None:
    from tests.places_fake import FakePlaces
    from tests.test_place_adapters import Meter

    fake = FakePlaces()

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            tavily = Tavily(http, "sk-test-xxxx", Meter())
            await tavily.extract(["https://fonte.example/pagina"])
            with pytest.raises(PisaError) as err:
                await tavily.extract(["https://fonte.example/?q=" + taint.Personal(CONTACT)])
            assert _refused(err.value)

    asyncio.run(go())
    assert fake.routes("tavily.extract") == ["tavily.extract"]  # only the clean link went out


def test_door_pisa_s_signed_page_read_refuses_a_link_holding_a_personal_value() -> None:
    from tests.test_world_carts import _reader

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="pagina", headers={"content-type": "text/plain"})

    reader, signer = _reader(handler)

    async def go() -> None:
        assert await reader.read("https://fonte.example/pagina") is not None
        with pytest.raises(PisaError) as err:
            await reader.read("https://fonte.example/?chi=" + taint.Personal(CONTACT))
        assert _refused(err.value)

    asyncio.run(go())
    assert seen == ["https://fonte.example/pagina"] and len(signer.asked) == 1  # not even signed


def test_door_the_curiosity_reads_its_ignorance_rows_as_his_and_a_door_refuses_them(
    client, db, clock
) -> None:  # type: ignore[no-untyped-def]
    """The marking in `c3_read`: a row's question reaches the night's reads as `Personal`, so a non-strict
    door (here Tavily's search) refuses it if a future change hands it there unstripped."""
    world = _world(client, db, clock)
    _his(db, "Pisa, tienimelo aperto: conviene a Ornella Ravaglia il fotovoltaico in affitto?")
    curious = client.app.state.runtime.curious
    seen: list[object] = []
    one = curious._one  # pyright: ignore[reportPrivateUsage]

    async def spy(night: Any, uid: Any, question: Any, *rest: Any) -> Any:
        seen.append(question)
        return await one(night, uid, question, *rest)

    curious._one = spy  # pyright: ignore[reportPrivateUsage]
    _night(client)
    assert world.sent and seen
    assert all(isinstance(q, taint.Personal) for q in seen), [type(q) for q in seen]
    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json={"results": [], "usage": {}})

    async def meter(_: Usage, __: bool) -> None:
        return None

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(PisaError) as err:
                await Tavily(http, "sk-test-xxxx", meter).search(seen[0], max_results=5)  # pyright: ignore[reportArgumentType]
            assert _refused(err.value)

    asyncio.run(go())
    assert sent == []


# --- item 3: the checker ------------------------------------------------------------------------------------


def test_the_checker_flags_an_f_string_over_a_personal_in_a_door_module() -> None:
    bad = (
        "from pisa.domain import taint\n"
        "def job(address: taint.Personal) -> str:\n"
        "    return f'un sito per {address}'\n"
        "def other() -> str:\n"
        "    name = taint.Personal('x')\n"
        "    return f'{name.upper()}!'\n"
        "def fine(address: str) -> str:\n"
        "    return f'un sito per {address}'\n"
    )
    assert sorted({f.split(":", 1)[1] for f in fstring_findings(bad)}) == ["3: address", "6: name.upper()"]
    awaited = (
        "from pisa.domain import taint\n"
        "class Desk:\n"
        "    async def _his_names(self) -> list[taint.Personal]:\n"
        "        return []\n"
        "    async def look(self) -> list[str]:\n"
        "        names = await self._his_names()\n"
        "        out = []\n"
        "        for n in names:\n"
        "            q = f'{n} prezzi'\n"
        "            out.append(q)\n"
        "        one = await self._his_names()\n"
        "        return out + [f'{m} orari' for m in one]\n"
        "    async def fine(self, words: list[str]) -> list[str]:\n"
        "        return [f'{w} prezzi' for w in words]\n"
    )
    assert sorted({f.split(":", 1)[1] for f in fstring_findings(awaited)}) == [
        "12: m",
        "9: n",
    ]
    assert door_findings() == []  # the door modules today: none


def test_at_run_time_an_f_string_over_a_personal_raises_in_a_door_module_only() -> None:
    p = taint.Personal(CONTACT)
    door_scope = {"__name__": "pisa.services.night", "p": p}
    with pytest.raises(taint.PersonalInFString):
        exec("f'query {p}'", door_scope)  # noqa: S102
    exec("x = f'query {p}'", elsewhere := {"__name__": "pisa.brain.prefix", "p": p})  # noqa: S102
    assert elsewhere["x"] == f"query {CONTACT}"
