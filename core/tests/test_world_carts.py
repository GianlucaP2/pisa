# ruff: noqa: E501
"""UCP carts handed off as handoff links, Pisa's own Ed25519
identity (Web Bot Auth signed reads, an agent card), the ladder as data. One test (or a few) per row; each fails
when its fix is reverted (the handoff's table)."""

import asyncio
import base64
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pisa.domain import decisions
from pisa.domain import identity as idrules
from pisa.domain import world as wrules
from tests.owner import owner_at
from tests.support.fake_secrets import pem_private_key
from tests.world_fake import CONTINUE_URL, FakePlaces, SheetPhone, UcpMerchant

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 11, 3, 18, 0, tzinfo=ROME)
AGENT = "https://pisa.example.org"
PROTOCOL_DIR = Path(__file__).resolve().parents[2] / "protocol"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _pool(url: str) -> Any:
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(url, kwargs={"autocommit": True}, open=False, min_size=1, max_size=3)
    await pool.open()
    return pool


def _keys() -> Any:
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from pisa.infra.keys import KeyStore

    return KeyStore(SecretStr(Fernet.generate_key().decode()))


def _identity(pool: Any, *, agent_url: str | None = AGENT, clock: Any = None, keys: Any = None) -> Any:
    from pisa.services.identity import IdentityService

    return IdentityService(pool, keys or _keys(), agent_url=agent_url, clock=clock or Clock())


def _members(header: str) -> dict[str, str]:
    """`sig1=…, sig2=…` → {label: value} (values here hold no top-level comma)."""
    return dict(m.split("=", 1) for m in re.split(r",\s*(?=sig\d+=)", header))


def verify(public_x: str, signature_input: str, signature: str, lines: list[tuple[str, str]]) -> bool:
    """An independent RFC 9421 verifier for the tests: the base from the request's own parts."""
    params = signature_input.split("=", 1)[1] if signature_input.startswith("sig") else signature_input
    text = "\n".join([*(f"{k}: {v}" for k, v in lines), f'"@signature-params": {params}'])
    raw = base64.b64decode(signature.split("=:", 1)[1].rstrip(":"))
    key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(public_x + "="))
    try:
        key.verify(raw, text.encode())
        return True
    except Exception:
        return False


# --- Row 1: Pisa's own identity (Ed25519, Web Bot Auth, the card, rotation) -----------------------------------


def test_the_signature_base_is_rfc_9421_s_own_ed25519_example() -> None:
    """RFC 9421 B.2.6: Pisa's base reproduces the RFC's signature exactly (Ed25519 is deterministic)."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    # RFC 9421 B.1.4 test-key-ed25519 (published), wrapped at run time
    pem = pem_private_key("MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF")
    key = load_pem_private_key(pem, None)
    lines = [('"date"', "Tue, 20 Apr 2021 02:07:55 GMT"), ('"@method"', "POST"), ('"@path"', "/foo"),
             ('"@authority"', "example.com"), ('"content-type"', "application/json"), ('"content-length"', "18")]  # fmt: skip
    sp = '("date" "@method" "@path" "@authority" "content-type" "content-length");created=1618884473;keyid="test-key-ed25519"'
    sig = base64.b64encode(key.sign(idrules.base(lines, sp).encode())).decode()  # type: ignore[union-attr,call-arg]
    assert sig == "wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw=="


def test_the_key_id_is_the_rfc_7638_thumbprint() -> None:
    """RFC 8037 A.3's example key: its JWK thumbprint."""
    assert (
        idrules.thumbprint("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo")
        == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    )
    assert idrules.jwk(base64.urlsafe_b64decode("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo="))["kid"] == (
        "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    )


def test_pisa_s_reads_are_signed_and_a_site_can_verify_them(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    async def go() -> None:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                identity = _identity(pool)
                kid = await identity.rotate()
                h = await identity.headers("GET", "https://www.glicine.example/menu?lang=it", "read")
                directory, _ = await identity.directory("pisa.example.org")
            finally:
                await pool.close()
            assert (
                h["User-Agent"]
                == "Pisa/1.0 (personal assistant acting for its owner; +https://pisa.example.org/.well-known/agent-card.json)"
            )
            assert h["Signature-Agent"] == 'sig1="https://pisa.example.org"'
            si = h["Signature-Input"]
            assert si.startswith(
                'sig1=("@authority" "@method" "@path" "signature-agent";key="sig1");created='
            )
            created = int(re.search(r";created=(\d+)", si).group(1))  # type: ignore[union-attr]
            expires = int(re.search(r";expires=(\d+)", si).group(1))  # type: ignore[union-attr]
            assert created == int(NOW.timestamp()) and expires - created == 60
            assert f'keyid="{kid}"' in si and 'tag="web-bot-auth"' in si and 'alg="ed25519"' in si
            assert re.search(r';nonce="[A-Za-z0-9+/=]{44}"', si)
            [jwk] = directory["keys"]
            assert jwk["kid"] == kid and jwk["kty"] == "OKP" and jwk["crv"] == "Ed25519"
            lines = [('"@authority"', "www.glicine.example"), ('"@method"', "GET"), ('"@path"', "/menu"),
                     ('"signature-agent";key="sig1"', '"https://pisa.example.org"')]  # fmt: skip
            assert verify(jwk["x"], si, h["Signature"], lines)
            assert not verify(
                jwk["x"],
                si,
                h["Signature"],
                [(k, "evil.example" if k == '"@authority"' else v) for k, v in lines],
            )

    asyncio.run(go())


def test_pisa_never_signs_a_purchase(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    """No action is signed for a purchase: a completion, a payment, an order, a PUT; http; a cart with a query."""
    from pisa.services.identity import NotSignable

    never = [
        ("POST", "https://api.target.com/ucp/v1/checkout-sessions/cs_1/complete", "ucp_cart"),
        ("POST", "https://api.target.com/ucp/v1/checkout-sessions/cs_1", "ucp_cart"),
        ("POST", "https://api.target.com/ucp/v1/checkout-sessions?complete=1", "ucp_cart"),
        ("PUT", "https://api.target.com/ucp/v1/checkout-sessions", "ucp_cart"),
        ("POST", "https://api.target.com/ucp/v1/orders", "ucp_catalog"),
        ("POST", "https://api.target.com/ucp/v1/payment", "ucp_catalog"),
        ("POST", "https://www.glicine.example/book", "read"),
        ("DELETE", "https://www.glicine.example/", "read"),
        ("GET", "http://www.glicine.example/", "read"),
        ("GET", "https://user:pw@www.glicine.example/", "read"),
        ("GET", "https://www.glicine.example/", "checkout"),
    ]
    for method, url, purpose in never:
        assert not idrules.may_sign(method, url, purpose), (method, url, purpose)  # type: ignore[arg-type]
    assert idrules.may_sign("POST", "https://api.target.com/ucp/v1/checkout-sessions", "ucp_cart")
    assert idrules.may_sign("POST", "https://api.target.com/ucp/v1/catalog/search", "ucp_catalog")
    assert idrules.may_sign("HEAD", "https://www.glicine.example/", "read")

    async def go() -> None:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                identity = _identity(pool)
                await identity.rotate()
                with pytest.raises(NotSignable):
                    await identity.headers(
                        "POST", "https://api.target.com/ucp/v1/checkout-sessions/cs_1/complete", "ucp_cart"
                    )
            finally:
                await pool.close()

    asyncio.run(go())
    source = (Path(__file__).resolve().parents[1] / "pisa" / "connectors" / "ucp.py").read_text()
    assert "/complete" not in source and "def complete" not in source and "def pay" not in source


def test_the_key_is_pisa_s_own_sealed_in_the_keystore(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    """The private key is sealed (Fernet), never in the clear, never among the user's phone keys."""
    keys = _keys()

    async def go() -> str:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                return await _identity(pool, keys=keys).rotate()
            finally:
                await pool.close()

    kid = asyncio.run(go())
    sealed, x = db.execute(
        "SELECT sealed, public_x FROM pisa_identity_keys WHERE kid = %s", (kid,)
    ).fetchone()
    raw = base64.b64decode(keys.unseal(bytes(sealed)))
    assert len(raw) == 32 and raw not in bytes(sealed) and base64.b64encode(raw) not in bytes(sealed)
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone()[0] == 0
    assert idrules.thumbprint(x) == kid


def test_rotation_keeps_the_old_key_published_then_drops_it_and_revoke_stops_signing(
    db, test_database_url
) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()

    async def go() -> None:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                identity = _identity(pool, clock=clock)
                first = await identity.rotate()
                second = await identity.rotate()
                listed = [k["kid"] for k in (await identity.directory("pisa.example.org"))[0]["keys"]]
                assert listed == [second, first]
                h = await identity.headers("GET", "https://example.com/", "read")
                assert f'keyid="{second}"' in h["Signature-Input"]
                clock.now = NOW + timedelta(seconds=idrules.RETIRE_AFTER_S + 1)
                listed = [k["kid"] for k in (await identity.directory("pisa.example.org"))[0]["keys"]]
                assert listed == [second]
                assert (await identity.status())["kid"] == second
                assert await identity.revoke() == 2  # both rows erased (the retired one too)
                body, headers = await identity.directory("pisa.example.org")
                assert body == {"keys": []} and "Signature" not in headers
                after = await identity.headers("GET", "https://example.com/", "read")
                assert set(after) == {"User-Agent"}
                assert (await identity.status())["signing"] is False
            finally:
                await pool.close()

    asyncio.run(go())


def test_without_a_public_url_pisa_signs_nothing_but_says_what_it_is(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    async def go() -> dict[str, str]:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                identity = _identity(pool, agent_url=None)
                await identity.rotate()
                return await identity.headers("GET", "https://example.com/", "read")
            finally:
                await pool.close()

    assert asyncio.run(go()) == {"User-Agent": "Pisa/1.0 (personal assistant acting for its owner)"}
    assert idrules.agent_url_ok("https://pisa.example.org/") == "https://pisa.example.org"
    for bad in (
        "http://pisa.example.org",
        "https://pisa.example.org/card",
        "https://pisa.example.org?x=1",
        "https://localhost",
    ):
        assert idrules.agent_url_ok(bad) is None


@pytest.fixture
def published_client(test_database_url, secrets_key, db, fake_models, clock, http_handler):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime
    from pisa.settings import Settings
    from tests.conftest import _dispatch

    settings = Settings(
        env="test",
        timezone="Europe/Rome",
        database_url=test_database_url,
        secrets_key=SecretStr(secrets_key),
        agent_url=AGENT,
    )

    async def factory(s):  # type: ignore[no-untyped-def]
        transport = httpx.MockTransport(lambda request: _dispatch(http_handler, request))
        return await Runtime.start(s, model_factory=fake_models, http_transport=transport, clock=clock)

    with TestClient(create_app(settings, runtime_factory=factory)) as test_client:
        yield test_client


def test_the_card_and_the_key_directory_are_public_and_hold_no_owner_data(published_client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import pair

    runtime = published_client.app.state.runtime
    kid = published_client.portal.call(runtime.identity.rotate)
    db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('profile_name', '\"Livia Ferrante\"') ON CONFLICT DO NOTHING"
    )
    r = published_client.get("/.well-known/http-message-signatures-directory")
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/http-message-signatures-directory+json"
    )
    assert r.headers["cache-control"] == "public, max-age=3600"
    [jwk] = r.json()["keys"]
    assert jwk["kid"] == kid
    si, sig = r.headers["signature-input"], r.headers["signature"]
    assert 'tag="http-message-signatures-directory"' in si
    assert verify(
        jwk["x"],
        _members(si)["sig1"],
        "sig1=" + _members(sig)["sig1"],
        [('"@authority";req', "pisa.example.org")],
    )
    card = published_client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Pisa" and card["description"] == "Pisa, a personal assistant acting for its owner"
    assert card["keys"] == {
        "directory": AGENT + "/.well-known/http-message-signatures-directory",
        "kids": [kid],
    }
    assert set(card) == {"name", "description", "url", "version", "category", "purpose", "conduct", "keys",
                         "ucp_profile", "capabilities", "skills", "defaultInputModes", "defaultOutputModes"}  # fmt: skip
    assert "Ferrante" not in json.dumps(card) and "Livia" not in json.dumps(card)
    profile = published_client.get("/.well-known/pisa-ucp-profile.json").json()
    assert profile["ucp"]["version"] == "2026-08-25" and profile["keys"][0]["kid"] == kid
    assert published_client.get("/v1/world/identity").status_code == 401
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    status = published_client.get("/v1/world/identity", headers=headers).json()
    assert status["kid"] == kid and status["signing"] is True and status["agent_url"] == AGENT
    assert "sealed" not in json.dumps(status) and "private" not in json.dumps(status)


def test_without_pisa_agent_url_the_card_is_404(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/.well-known/agent-card.json").status_code == 404
    assert client.get("/.well-known/http-message-signatures-directory").json() == {"keys": []}


def test_the_host_rotates_and_revokes_from_the_command_line(
    db, test_database_url, secrets_key, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    from pisa import cli
    from pisa.settings import get_settings

    monkeypatch.setenv("PISA_DATABASE_URL", test_database_url)
    monkeypatch.setenv("PISA_SECRETS_KEY", secrets_key)
    monkeypatch.setenv("PISA_AGENT_URL", AGENT)
    get_settings.cache_clear()
    try:
        assert cli.main(["identity", "rotate"]) == 0
        out = capsys.readouterr().out
        kid = db.execute("SELECT kid FROM pisa_identity_keys WHERE retired_at IS NULL").fetchone()[0]
        assert kid in out and "signing: yes" in out
        assert cli.main(["identity", "revoke"]) == 0
        assert "signing: no" in capsys.readouterr().out
        assert (
            db.execute("SELECT count(*) FROM pisa_identity_keys WHERE retired_at IS NULL").fetchone()[0] == 0
        )
    finally:
        get_settings.cache_clear()


# --- Row 2: Pisa's own reads, signed (read_page, places research) ----------------------------------------------


class _Signer:
    agent_url: str | None = AGENT

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    async def headers(self, method: str, url: str, purpose: Any = "read") -> dict[str, str]:
        self.asked.append((method, url, purpose))
        return {"User-Agent": "Pisa/1.0 (test)", "Signature": "sig1=:x:"}


def _reader(handler: Any, addresses: dict[str, list[str]] | None = None) -> tuple[Any, _Signer]:
    from pisa.connectors.web_reader import SignedReader

    async def resolve(host: str) -> list[str]:
        return (addresses or {}).get(host, ["93.184.215.14"])

    signer = _Signer()
    return SignedReader(signer, resolver=resolve, transport=lambda _ip: httpx.MockTransport(handler)), signer


def test_a_page_pisa_reads_itself_carries_its_signature_and_gives_its_words() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        html = "<html><head><title>x</title><script>steal()</script></head><body><h1>Menù</h1><p>Carbonara 16 €</p><style>p{}</style></body></html>"
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    reader, signer = _reader(handler)
    got = asyncio.run(reader.read("https://www.glicine.example/menu"))
    assert got == ("https://www.glicine.example/menu", "Menù Carbonara 16 €")
    assert seen[0].headers["signature"] == "sig1=:x:" and seen[0].headers["user-agent"] == "Pisa/1.0 (test)"
    assert signer.asked == [("GET", "https://www.glicine.example/menu", "read")]


def test_no_read_into_a_private_address_even_by_a_redirect() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "jump.example":
            return httpx.Response(302, headers={"location": "https://inside.example/admin"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    private = {"tailnet.example": ["100.64.0.7"], "local.example": ["127.0.0.1"], "lan.example": ["192.168.1.2"],
               "mapped.example": ["::ffff:10.0.0.1"], "mixed.example": ["93.184.215.14", "10.0.0.2"],
               "inside.example": ["169.254.169.254"]}  # fmt: skip
    reader, _ = _reader(handler, private)
    for host in ("tailnet.example", "local.example", "lan.example", "mapped.example", "mixed.example"):
        assert asyncio.run(reader.read(f"https://{host}/")) is None
    assert seen == []
    assert asyncio.run(reader.read("https://jump.example/")) is None
    assert seen == ["https://jump.example/"]  # the redirect's target was never asked
    for bad in (
        "http://www.glicine.example/",
        "https://www.glicine.example:8443/",
        "https://u:p@www.glicine.example/",
    ):
        assert asyncio.run(reader.read(bad)) is None


def test_a_redirect_is_followed_and_checked_and_a_refusal_gives_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        if request.url.path == "/blocked":
            return httpx.Response(403, text="bots not allowed", headers={"content-type": "text/html"})
        if request.url.path == "/pdf":
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        return httpx.Response(200, text="<p>nuovo</p>", headers={"content-type": "text/html"})

    reader, signer = _reader(handler)
    assert asyncio.run(reader.read("https://www.glicine.example/old")) == (
        "https://www.glicine.example/new",
        "nuovo",
    )
    assert [u for _, u, _ in signer.asked] == [
        "https://www.glicine.example/old",
        "https://www.glicine.example/new",
    ]
    assert asyncio.run(reader.read("https://www.glicine.example/blocked")) is None
    assert asyncio.run(reader.read("https://www.glicine.example/pdf")) is None


def test_read_page_and_research_read_through_pisa_first_then_the_provider() -> None:
    from types import SimpleNamespace

    from pisa.services.place_assistant import PlaceAssistantData
    from pisa.services.places import PlacesService

    extracted: list[list[str]] = []

    class Providers:
        async def choose(self, job: str, call: Any, usage: Any) -> Any:
            adapter = SimpleNamespace(extract=lambda urls: _extract(urls))
            return SimpleNamespace(value=await call(adapter))

    async def _extract(urls: list[str]) -> list[Any]:
        extracted.append(urls)
        return [SimpleNamespace(url=u, text=f"provider {u}") for u in urls]

    async def reader(url: str) -> tuple[str, str] | None:
        return None if "blocked" in url else (url, f"Pisa read {url}")

    places = PlacesService.__new__(PlacesService)
    places.providers = Providers()  # type: ignore[assignment]
    places.reader = reader

    async def counted(url: str) -> tuple[str, str, int] | None:  # The same read, counted
        found = await reader(url)
        return (*found, 0) if found is not None else None

    places.counted_reader = counted
    got = asyncio.run(places.pages(["https://a.example/", "https://blocked.example/"]))
    assert got == {
        "https://a.example/": "Pisa read https://a.example/",
        "https://blocked.example/": "provider https://blocked.example/",
    }
    assert extracted == [["https://blocked.example/"]]
    data = PlaceAssistantData.__new__(PlaceAssistantData)
    data._places = places  # type: ignore[attr-defined]
    data.reader = None
    page = asyncio.run(data.read_page("https://a.example/"))
    assert page is not None and (page["url"], page["text"]) == (
        "https://a.example/",
        "Pisa read https://a.example/",
    )
    assert extracted == [["https://blocked.example/"]]
    places.reader = places.counted_reader = None
    asyncio.run(places.pages(["https://a.example/"]))
    assert extracted[-1] == ["https://a.example/"]


def test_the_runtime_reads_pages_itself_once_pisa_is_published(published_client) -> None:  # type: ignore[no-untyped-def]
    assert published_client.app.state.runtime.places.reader is not None


def test_unpublished_pages_go_through_the_provider_as_before(client) -> None:  # type: ignore[no-untyped-def]
    assert client.app.state.runtime.places.reader is None
    assert (
        client.app.state.runtime.conversations.deps.tools.world is client.app.state.runtime.world is not None
    )


# --- Row 3: US retail through UCP: a cart they pay themselves -------------------------------------------------


def _world(pool: Any, merchant: UcpMerchant, *, signer: Any = None, clock: Any = None) -> Any:
    from pisa.connectors.ucp import UcpClient
    from pisa.services.world import WorldService

    http = httpx.AsyncClient(transport=httpx.MockTransport(merchant))
    return WorldService(pool, UcpClient(http, signer or _Signer()), clock=clock or Clock())


def _tools(words: str, world: Any, *, lang: str = "it-IT") -> tuple[dict[str, Any], Any, SheetPhone]:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pisa.brain.handoff_tools import add_handoff_tools
    from pisa.brain.present_tools import Presenter
    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.world_tools import add_world_tools
    from tests.test_world_handoffs import FakeHandoffs

    handoffs, phone = FakeHandoffs(), SheetPhone()
    presenter = Presenter(device=phone, words=words, lang=lang, today=NOW.date(), objects={})
    turn = ProfileTurn(words=words, device=uuid.UUID(int=7), lang=lang, turn_id=uuid.uuid4().hex)
    agent: Agent[None, str] = Agent(TestModel())
    show = add_handoff_tools(agent, handoffs, lambda: presenter, turn, places=cast(Any, FakePlaces()), data=None, actions=None,
                             owner=owner_at(), clock=lambda: NOW, world=world)  # fmt: skip
    add_world_tools(agent, world, show, turn)
    return {n: t.function for n, t in agent._function_toolset.tools.items()}, handoffs, phone


def _with_world(test_database_url: str, merchant: UcpMerchant, fn: Any, **kw: Any) -> Any:
    async def go() -> Any:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                return await fn(_world(pool, merchant, **kw), pool)
            finally:
                await pool.close()

    return asyncio.run(go())


def test_comprami_delle_lampadine_su_target_is_a_cart_link_nothing_bought(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    merchant = UcpMerchant()

    async def go(world: Any, pool: Any) -> tuple[str, str, Any, SheetPhone]:
        with decisions.collecting():
            tools, handoffs, phone = _tools("comprami delle lampadine su Target", world)
            found = await tools["find_products"]("Target", "light bulbs")
            prepared = await tools["prepare_cart"]("Target", ["tcin-1001"], [2])
            return found, prepared, handoffs, phone

    found, prepared, handoffs, phone = _with_world(test_database_url, merchant, go)
    assert "[tcin-1001] Philips LED A19 60W Soft White, 4-pack — 12,99 $" in found
    assert "[tcin-1002] GE Basic LED 8-pack — prezzo non indicato" in found
    assert "<merchant-" in found  # the merchant's words are outside text
    [h] = phone.handoffs()
    assert h["kind"] == "open_url" and h["url"] == CONTINUE_URL
    assert (
        h["subtitle"] == "target.com · il carrello di Target, paghi tu lì"
        and h["title"] == "Rivedi e paga su Target"
    )
    facts = {
        b["label"]: b["value"]
        for m in phone.sent
        for b in m["object"]["data"]["blocks"]
        if b["type"] == "fact"
    }
    assert facts == {"Philips LED A19 60W Soft White x2": "25,98 $", "Totale": "28,06 $ (Target)"}
    assert "Nothing is booked, sent, called or ordered" in prepared and "28,06 $" in prepared
    [prep] = handoffs.preps
    assert prep["purpose"] == "shopping" and prep["details"]["route"] == "protocol"
    methods = [(r.method, str(r.url)) for r in merchant.requests]
    assert methods == [("GET", "https://target.com/.well-known/ucp"), ("POST", "https://api.target.com/ucp/v1/catalog/search"),
                       ("POST", "https://api.target.com/ucp/v1/checkout-sessions")]  # fmt: skip
    assert all("complete" not in u for _, u in methods)
    assert all(
        r.headers["ucp-agent"] == f'profile="{AGENT}/.well-known/pisa-ucp-profile.json"'
        for r in merchant.requests
    )
    assert json.loads(merchant.requests[2].content) == {
        "line_items": [{"item": {"id": "tcin-1001"}, "quantity": 2}],
        "currency": "USD",
    }
    assert db.execute("SELECT has_ucp, endpoint FROM ucp_merchants").fetchall() == [
        (True, "https://api.target.com/ucp/v1")
    ]


def test_only_merchants_on_the_allowlist_are_ever_contacted(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    merchant = UcpMerchant()

    async def go(world: Any, pool: Any) -> str:
        with decisions.collecting():
            tools, _, phone = _tools("comprami delle lampadine su Amazon", world)
            said = await tools["find_products"]("Amazon", "light bulbs")
            assert phone.sent == []
            return said

    assert "isn't one" in _with_world(test_database_url, merchant, go) and merchant.requests == []
    db.execute("INSERT INTO app_settings (key, value) VALUES ('world', %s)", (json.dumps({"merchants": [
        {"key": "amazon", "name": "Amazon", "host": "amazon.com", "country": "US", "search": "https://www.amazon.com/s?k={q}"},
        {"key": "evil", "name": "Evil", "host": "evil.com", "country": "US", "search": "https://bit.ly/s?k={q}"}]}),))  # fmt: skip

    async def go2(world: Any, pool: Any) -> tuple[Any, Any, Any]:
        return await world.merchant("Amazon"), await world.merchant("Target"), await world.merchant("Evil")

    amazon, target, evil = _with_world(test_database_url, merchant, go2)
    assert amazon["host"] == "amazon.com" and target is None and evil is None


def test_a_cart_holds_only_products_pisa_s_search_found(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    merchant = UcpMerchant()

    async def go(world: Any, pool: Any) -> tuple[str, str, str]:
        with decisions.collecting():
            tools, _, _ = _tools("comprami delle lampadine su Target", world)
            before = await tools["prepare_cart"]("Target", ["tcin-1001"])
            await tools["find_products"]("Target", "light bulbs")
            invented = await tools["prepare_cart"]("Target", ["tcin-9999"])
            too_many = await tools["prepare_cart"]("Target", ["tcin-1001"], [50])
            return before, invented, too_many

    before, invented, too_many = _with_world(test_database_url, merchant, go)
    assert (
        "isn't a product Pisa's search found" in before and "isn't a product Pisa's search found" in invented
    )
    assert "between 1 and 20" in too_many
    assert not any("checkout-sessions" in str(r.url) for r in merchant.requests)


def test_prices_only_as_the_merchant_states_them() -> None:
    assert wrules.price(1299, "USD") == wrules.Price(1299, "USD")
    for bad in (12.99, "12.99", -1, True, None):
        assert wrules.price(bad, "USD") is None
    assert wrules.price(1299, "dollars") is None and wrules.price({"amount": 500}, None) is None
    assert wrules.money(wrules.Price(129900, "USD"), "en-US") == "$1,299.00"
    assert wrules.money(wrules.Price(129900, "USD"), "it-IT") == "1\u2009299,00 $"
    assert wrules.money(wrules.Price(500, "JPY"), "en-US") == "500 JPY"
    assert wrules.money(None, "it-IT") == "prezzo non indicato"
    # the total is the merchant's own total line, never Pisa's sum of the lines
    body = {"id": "cs", "currency": "USD", "continue_url": CONTINUE_URL,
            "line_items": [{"item": {"id": "a", "title": "A", "price": 100}, "quantity": 1}],
            "totals": [{"type": "subtotal", "amount": 100}, {"type": "total", "amount": 777}]}  # fmt: skip
    cart = wrules.cart(body, "target.com")
    assert cart is not None and cart.total == wrules.Price(777, "USD")
    body["totals"] = [{"type": "subtotal", "amount": 100}]
    assert wrules.cart(body, "target.com").total is None  # type: ignore[union-attr]


def test_the_checkout_link_is_the_merchant_s_own_or_there_is_no_cart_sheet(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    for link in ("https://evil.example/checkout", "http://www.target.com/checkout/1", "https://bit.ly/abc",
                 "https://www.target.com/redirect?to=https%3A%2F%2Fevil.example", "https://target.com.evil.example/c",
                 "https://www.tarğet.com/c", None):  # fmt: skip
        merchant = UcpMerchant(continue_url=link)

        async def go(world: Any, pool: Any) -> tuple[str, SheetPhone]:
            with decisions.collecting():
                tools, _, phone = _tools("comprami delle lampadine su Target", world)
                await tools["find_products"]("Target", "light bulbs")
                return await tools["prepare_cart"]("Target", ["tcin-1001"]), phone

        said, phone = _with_world(test_database_url, merchant, go)
        assert "gave no checkout link" in said and phone.sent == [], link
        db.execute("DELETE FROM ucp_merchants")
    assert wrules.on_domain("https://secure.target.com/co/1", "target.com")


def test_a_merchant_without_ucp_gets_its_own_search_page(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    merchant = UcpMerchant(ucp=False)

    async def go(world: Any, pool: Any) -> tuple[str, SheetPhone, Any]:
        with decisions.collecting():
            tools, handoffs, phone = _tools("comprami delle lampadine su Target", world)
            return await tools["find_products"]("Target", "light bulbs & co/../x"), phone, handoffs

    said, phone, handoffs = _with_world(test_database_url, merchant, go)
    [h] = phone.handoffs()
    assert h["url"] == "https://www.target.com/s?searchTerm=light%20bulbs%20%26%20co%2F..%2Fx"
    assert h["subtitle"] == "target.com · la ricerca di Target" and "no cart Pisa can build" in said
    assert handoffs.preps[0]["details"]["route"] == "deep_link"
    assert db.execute("SELECT has_ucp FROM ucp_merchants").fetchall() == [(False,)]


def test_a_profile_off_the_merchant_s_domain_is_no_ucp_and_discovery_is_cached(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    profile = {
        "ucp": {
            "version": "2026-08-25",
            "services": {"dev.ucp.shopping": {"rest": {"endpoint": "https://ucp.evil.example/v1"}}},
        }
    }
    assert wrules.profile(profile, "target.com") is None
    assert (
        wrules.profile(
            {"ucp": {"services": {"dev.ucp.shopping": {"rest": {"endpoint": "http://api.target.com/v1"}}}}},
            "target.com",
        )
        is None
    )
    merchant = UcpMerchant()
    clock = Clock()

    async def go(world: Any, pool: Any) -> None:
        with decisions.collecting():
            m = wrules.merchant(wrules.MERCHANTS, "target")
            assert m is not None
            await world.discovery(m)
            await world.discovery(m)
            clock.now = NOW + timedelta(days=8)
            await world.discovery(m)

    _with_world(test_database_url, merchant, go, clock=clock)
    assert [str(r.url) for r in merchant.requests] == ["https://target.com/.well-known/ucp"] * 2


def test_after_outside_text_only_his_words_asking_to_shop_prepare_a_cart() -> None:
    from pisa.domain.handoffs import asks_for

    assert asks_for("shopping", "comprami delle lampadine su Target")
    assert asks_for("shopping", "buy me light bulbs at Target")
    assert asks_for("shopping", "mettile nel carrello")
    assert not asks_for("shopping", "che tempo fa domani?")


# --- Row 4: the ladder as data ------------------------------------------------------------------------------


def test_the_ladder_s_seed_is_the_research_s_table() -> None:
    table = wrules.ladder(None)
    assert wrules.first_rung(table, "retail", "US", {"protocol", "deep_link"}) == "protocol"
    assert wrules.first_rung(table, "retail", "US", {"deep_link"}) == "deep_link"
    assert (
        wrules.first_rung(table, "retail", "IT", {"protocol", "deep_link"}) == "deep_link"
    )  # UCP only watched
    assert wrules.first_rung(table, "restaurants", "IT", {"call", "deep_link"}) == "call"
    assert wrules.first_rung(table, "utilities", "IT", {"call", "deep_link"}) == "deep_link"
    assert wrules.first_rung(table, "public_it", "IT", {"call", "deep_link", "protocol"}) == "deep_link"
    assert (
        wrules.first_rung(table, "travel", "US", {"business_agent", "signed_browser", "deep_link"})
        == "deep_link"
    )
    assert [r.rung for r in wrules.routes(table, "travel", "US")] == list(wrules.RUNGS)
    assert all(
        r.source.startswith("a survey of public ordering protocols")
        for c in table.values()
        for v in c.values()
        for r in v
    )
    assert wrules.order(table, "restaurants", "IT", ["deep_link", "call"]) == ["call", "deep_link"]


def test_his_settings_replace_a_category_s_routes(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    db.execute("INSERT INTO app_settings (key, value) VALUES ('world', %s)", (json.dumps({"ladder": {
        "retail": {"US": [{"rung": "deep_link", "state": "live", "via": "the store's page"},
                          {"rung": "teleport", "state": "live", "via": "x"}]}}}),))  # fmt: skip
    merchant = UcpMerchant()

    async def go(world: Any, pool: Any) -> tuple[Any, SheetPhone]:
        with decisions.collecting():
            tools, _, phone = _tools("comprami delle lampadine su Target", world)
            await tools["find_products"]("Target", "light bulbs")
            return await world.ladder(), phone

    ladder, phone = _with_world(test_database_url, merchant, go)
    retail = next(c for c in ladder["categories"] if c["category"] == "retail")
    assert retail["countries"]["US"] == [
        {"rung": "deep_link", "state": "live", "via": "the store's page", "note": "", "source": ""}
    ]
    assert phone.handoffs()[0]["url"].startswith(
        "https://www.target.com/s?searchTerm="
    )  # the protocol rung is off
    assert not any("catalog" in str(r.url) for r in merchant.requests)


def test_a_table_s_buttons_follow_the_ladder(db, test_database_url) -> None:  # type: ignore[no-untyped-def]

    async def go(world: Any, pool: Any) -> list[str]:
        with decisions.collecting():
            tools, _, phone = _tools("prenotami un tavolo da Glicine venerdì alle 21 per 2", world)
            await tools["prepare_table"]("Glicine", "2026-11-06T21:00", 2)
            return [h["kind"] for h in phone.handoffs()]

    assert _with_world(test_database_url, UcpMerchant(), go) == ["call", "open_url"]


def test_the_ladder_and_the_identity_on_the_api_match_the_fixture(client, db) -> None:  # type: ignore[no-untyped-def]
    from pisa.api import world_schemas
    from tests.conftest import pair

    fixture = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "world.json").read_text())
    world_schemas.Ladder.model_validate(fixture["ladder"])
    world_schemas.Identity.model_validate(fixture["identity"])
    headers = {"Authorization": f"Bearer {pair(db)[1]}"}
    assert client.get("/v1/world/ladder").status_code == 401
    body = client.get("/v1/world/ladder", headers=headers).json()
    assert world_schemas.Ladder.model_validate(body).carried == ["call", "deep_link", "protocol"]
    assert [c["category"] for c in body["categories"]] == [
        "restaurants",
        "travel",
        "retail",
        "utilities",
        "public_it",
    ]
    status = client.get("/v1/world/identity", headers=headers).json()
    assert status == {"agent_url": None, "signing": False, "kid": None, "created_at": None, "published": [],
                      "card_url": None, "directory_url": None}  # fmt: skip


# --- The review: each fix's test ------------------------------------------------------


class _Refusing(_Signer):
    async def headers(self, method: str, url: str, purpose: Any = "read") -> dict[str, str]:
        from pisa.domain.identity import NotSignable

        raise NotSignable("never")


def test_a_request_pisa_never_signs_falls_back_to_the_store_s_search_never_a_crash(
    db, test_database_url
) -> None:  # type: ignore[no-untyped-def]
    from pisa.connectors.ucp import UcpClient, UcpError

    merchant = UcpMerchant()
    client = UcpClient(httpx.AsyncClient(transport=httpx.MockTransport(merchant)), _Refusing())
    with pytest.raises(UcpError):
        asyncio.run(client.search("https://api.target.com/ucp/v1", "bulbs", 5))
    assert merchant.requests == []
    orders = {
        "ucp": {
            "services": {"dev.ucp.shopping": {"rest": {"endpoint": "https://api.target.com/api/orders/ucp"}}}
        }
    }
    assert wrules.profile(orders, "target.com") is None

    async def go(world: Any, pool: Any) -> tuple[str, SheetPhone]:
        with decisions.collecting():
            tools, _, phone = _tools("comprami delle lampadine su Target", world)
            return await tools["find_products"]("Target", "light bulbs"), phone

    said, phone = _with_world(test_database_url, merchant, go, signer=_Refusing())
    assert "no cart Pisa can build" in said and phone.handoffs()[0]["url"].startswith(
        "https://www.target.com/s?"
    )


def test_a_catalog_signature_is_only_exactly_a_catalog_search() -> None:
    for path in ("/ucp/v1/checkout-sessions/abc", "/ucp/v1/checkout-sessions/abc/update", "/ucp/v1/carts",
                 "/ucp/v1/catalog/search/x", "/ucp/v1/catalog/search?pay=1", "/ucp/v1/anything"):  # fmt: skip
        assert not idrules.may_sign("POST", "https://api.target.com" + path, "ucp_catalog"), path
    assert idrules.may_sign("POST", "https://api.target.com/ucp/v1/catalog/search", "ucp_catalog")
    assert idrules.may_sign("GET", "https://api.target.com/ucp/v1/catalog/items/1", "ucp_catalog")


def test_pages_are_read_together_and_a_slow_one_is_cut_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import time
    from types import SimpleNamespace

    from pisa.services import places as places_mod
    from pisa.services.places import PlacesService

    monkeypatch.setattr(places_mod, "PAGE_READ_S", 0.5)

    class Providers:
        async def choose(self, job: str, call: Any, usage: Any) -> Any:
            async def extract(urls: list[str]) -> list[Any]:
                return [SimpleNamespace(url=u, text="provider") for u in urls]

            return SimpleNamespace(value=await call(SimpleNamespace(extract=extract)))

    async def reader(url: str) -> tuple[str, str] | None:
        await asyncio.sleep(5 if "drip" in url else 0.3)
        return (url, "Pisa")

    places = PlacesService.__new__(PlacesService)
    places.providers = Providers()  # type: ignore[assignment]
    places.reader = reader
    started = time.monotonic()
    got = asyncio.run(
        places.pages(
            ["https://a.example/", "https://b.example/", "https://c.example/", "https://drip.example/"]
        )
    )
    assert time.monotonic() - started < 1.0  # together: the slowest cut-off (0.5 s), not the sum (1.4 s)
    assert got == {
        "https://a.example/": "Pisa",
        "https://b.example/": "Pisa",
        "https://c.example/": "Pisa",
        "https://drip.example/": "provider",
    }


def test_l_a_revoke_from_the_host_stops_the_next_signature_and_erases_the_key(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    keys = _keys()

    async def go() -> dict[str, str]:
        with decisions.collecting():
            pool = await _pool(test_database_url)
            try:
                api, host = _identity(pool, keys=keys), _identity(pool, keys=keys)
                await api.rotate()
                assert "Signature" in await api.headers("GET", "https://example.com/", "read")
                await host.revoke()  # another process (`pisa identity revoke`)
                return await api.headers("GET", "https://example.com/", "read")
            finally:
                await pool.close()

    assert set(asyncio.run(go())) == {"User-Agent"}
    assert db.execute("SELECT count(*) FROM pisa_identity_keys").fetchone()[0] == 0


def test_l_a_failed_discovery_is_tried_again_within_the_hour(db, test_database_url) -> None:  # type: ignore[no-untyped-def]
    class Down(UcpMerchant):
        def __call__(self, request: Any) -> Any:
            self.requests.append(request)
            return httpx.Response(503)

    merchant, clock = Down(), Clock()

    async def go(world: Any, pool: Any) -> None:
        with decisions.collecting():
            m = wrules.merchant(wrules.MERCHANTS, "target")
            assert m is not None
            assert await world.discovery(m) is None
            await world.discovery(m)
            clock.now = NOW + timedelta(hours=1, minutes=1)
            await world.discovery(m)

    _with_world(test_database_url, merchant, go, clock=clock)
    assert len(merchant.requests) == 2


def test_l_a_store_s_reply_is_capped_while_it_streams() -> None:
    from pisa.connectors.ucp import BODY_MAX, UcpClient, UcpError

    sent: list[int] = []

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def,override]
            for _ in range(200):
                sent.append(1)
                yield b"x" * 65536

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Endless(), headers={"content-type": "application/json"})

    client = UcpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), _Signer())
    with pytest.raises(UcpError, match="too large"):
        asyncio.run(client.search("https://api.target.com/ucp/v1", "bulbs", 5))
    assert len(sent) * 65536 <= BODY_MAX + 65536 * 2
