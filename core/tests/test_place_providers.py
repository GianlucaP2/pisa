"""Place providers (contract H) and the choice of provider: keys, status, tests, usage; the user's
order, and the fallback on an error, a missing key, an unsupported request and the monthly cap."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from pisa.domain.errors import PisaError
from pisa.domain.place_prices import PRICES
from pisa.infra.keys import KeyStore
from pisa.services.place_providers import set_maps_key
from tests.assistant_env import set_settings
from tests.conftest import dsn, pair
from tests.places_env import HERE_MILAN, MAPS_KEY_ID, PlaceEnv, at_location, place_calls, run_places
from tests.places_fake import FakePlaces
from tests.test_push import p8

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)  # 12:00 in Rome
FX = Decimal("0.86")  # the default USD→EUR rate when no ECB rate is stored


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair(db)[1]}"}


@pytest.fixture
def fake(http_handler: dict[str, object]) -> FakePlaces:
    fake = FakePlaces()
    http_handler["handler"] = fake
    return fake


# --- contract H -------------------------------------------------------------------------------------------


def test_keys_and_status(client, auth, fake) -> None:  # type: ignore[no-untyped-def]
    providers = client.get("/v1/places/providers", headers=auth).json()["providers"]
    assert [(p["id"], p["configured"], p["capabilities"]) for p in providers] == [
        ("apple", False, ["routes", "search"]),
        ("google", False, ["routes", "search", "details"]),
        ("tavily", False, ["web"]),
    ]
    assert (
        client.put(
            "/v1/places/providers/google/key",
            headers=auth,
            json={"key": "AIza-test-1234"},  # gitleaks:allow (a fake key)
        ).status_code
        == 204
    )
    google = client.get("/v1/places/providers", headers=auth).json()["providers"][1]
    assert google == {
        "id": "google", "name": "Google Maps", "configured": True, "key_last4": "1234",
        "capabilities": ["routes", "search", "details"], "last_test": None,
    }  # fmt: skip
    assert client.delete("/v1/places/providers/google/key", headers=auth).status_code == 204
    assert client.get("/v1/places/providers", headers=auth).json()["providers"][1]["configured"] is False


def test_apple_key_is_pasted_in_the_app(client, auth) -> None:  # type: ignore[no-untyped-def]
    # The .p8 text and its key id are pasted in the app (the lines may arrive joined).
    pem = p8()[0]
    one_line = " ".join(pem.splitlines())
    missing_id = client.put("/v1/places/providers/apple/key", headers=auth, json={"key": pem})
    assert missing_id.status_code == 400 and "key id" in missing_id.json()["error"]["message"]
    not_a_key = client.put(
        "/v1/places/providers/apple/key", headers=auth, json={"key": "hello", "key_id": "FGHIJ67890"}
    )
    assert not_a_key.status_code == 400 and "Apple Maps key" in not_a_key.json()["error"]["message"]
    put = client.put(
        "/v1/places/providers/apple/key", headers=auth, json={"key": one_line, "key_id": "FGHIJ67890"}
    )
    assert put.status_code == 204
    apple = client.get("/v1/places/providers", headers=auth).json()["providers"][0]
    assert apple["configured"] is True
    assert (
        client.put("/v1/places/providers/here/key", headers=auth, json={"key": "x" * 10}).status_code == 404
    )


def test_apple_status_and_test(client, auth, db, secrets_key, fake) -> None:  # type: ignore[no-untyped-def]
    async def install() -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_maps_key(conn, KeyStore(SecretStr(secrets_key)), MAPS_KEY_ID, p8()[0])

    asyncio.run(install())
    apple = client.get("/v1/places/providers", headers=auth).json()["providers"][0]
    assert apple["configured"] is True and apple["key_last4"] == "Y123"
    tested = client.post("/v1/places/providers/apple/test", headers=auth).json()
    assert tested == {"ok": True, "message": "Apple Maps accepted the key: the token exchange worked."}
    assert fake.routes() == ["apple.token"]  # the cheapest real call
    last = client.get("/v1/places/providers", headers=auth).json()["providers"][0]["last_test"]
    assert last["ok"] is True and last["message"].startswith("Apple Maps accepted")


@pytest.mark.parametrize(
    ("provider", "key", "route", "message"),
    [
        (
            "google",
            "AIza-test-1234",
            "google.search",
            "Google accepted the key: a Text Search (ids only, free) worked.",
        ),
        (
            "tavily",
            "tvly-test-1234",
            "tavily.search",
            "Tavily accepted the key: one basic search worked (1 credit).",
        ),
    ],
)
def test_key_test_is_the_cheapest_call(client, auth, db, fake, provider, key, route, message) -> None:  # type: ignore[no-untyped-def]
    client.put(f"/v1/places/providers/{provider}/key", headers=auth, json={"key": key})
    assert client.post(f"/v1/places/providers/{provider}/test", headers=auth).json() == {
        "ok": True,
        "message": message,
    }
    assert fake.routes() == [route]
    if provider == "google":
        assert fake.last(route).headers["x-goog-fieldmask"] == "places.id"
        assert place_calls(db) == [("google", "google.text_search_ids", 1, True)]
    fake.fail[route] = (401, {"error": {"message": "API key not valid. Please pass a valid API key."}})
    failed = client.post(f"/v1/places/providers/{provider}/test", headers=auth).json()
    assert failed["ok"] is False and failed["message"]


def test_test_without_a_key(client, auth) -> None:  # type: ignore[no-untyped-def]
    response = client.post("/v1/places/providers/tavily/test", headers=auth)
    assert response.status_code == 409 and response.json()["error"]["code"] == "place_provider_not_configured"


def test_usage(client, auth, db, clock) -> None:  # type: ignore[no-untyped-def]
    clock.now = NOW
    for sku, units, at in (
        ("google.text_search_pro", 5_100, NOW),  # 100 over the free 5,000
        ("google.route_matrix_pro", 10, NOW),  # inside the free 5,000
        ("tavily.credit", 3, NOW),
        ("apple.search", 40, NOW),
        (
            "google.text_search_pro",
            9_999,
            datetime(2026, 9, 30, 21, 0, tzinfo=UTC),
        ),  # 23:00 in Rome: September
    ):
        db.execute(
            "INSERT INTO place_calls (provider, sku, units, at, ok) VALUES (%s, %s, %s, %s, true)",
            (sku.split(".")[0], sku, units, at),
        )
    usage = client.get("/v1/places/usage", headers=auth).json()
    assert usage["month"] == "2026-10" and usage["cap_eur"] == 5.0
    items = {i["sku"]: i for i in usage["items"]}
    assert items["google.text_search_pro"] == {
        "provider": "google", "sku": "google.text_search_pro", "calls": 5_100, "free_calls": 5_000,
        "free_allowance": 5_000,
        "unit_eur": float(round(Decimal("32") * FX / 1000, 6)),
        "cost_eur": float(round(Decimal("3.2") * FX, 4)),
    }  # fmt: skip
    assert items["google.route_matrix_pro"]["cost_eur"] == 0 and items["apple.search"]["free_calls"] == 40
    assert items["apple.search"]["free_allowance"] is None  # no paid tier
    assert usage["spent_eur"] == float(round(Decimal("3.2") * FX, 4))


# --- the choice -------------------------------------------------------------------------------------------


def search(**kw: Any) -> Any:
    async def go(env: PlaceEnv) -> Any:
        return await env.providers.choose(
            "search",
            lambda a: a.search(
                "trattoria", near=HERE_MILAN, open_now=kw.get("open_now", False), limit=5, lang="it-IT"
            ),
            can=lambda a: not kw.get("open_now") or a.can_filter_open_now,
            usage=lambda a: a.usage_for_search(),
        )

    return go


def test_the_owners_order(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake = FakePlaces()
    choice = run_places(db, secrets_key, fake, NOW, search())
    assert (choice.provider, choice.skipped) == ("apple", [])
    set_settings(db, {"places": {"search": ["google", "apple"]}})
    choice = run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert choice.provider == "google"


def test_an_error_falls_back_to_the_next(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake = FakePlaces()
    fake.fail["apple.search"] = (500, {"error": {"message": "Internal error", "details": []}})
    choice = run_places(db, secrets_key, fake, NOW, search())
    assert choice.provider == "google"
    assert choice.skipped == ["apple: Apple Maps refused the call (HTTP 500: Internal error)."]
    assert [r for r in fake.routes() if r != "apple.token"] == ["apple.search", "google.search"]


def test_a_missing_key_falls_back(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    choice = run_places(db, secrets_key, FakePlaces(), NOW, search(), keys=("google",))
    assert (choice.provider, choice.skipped) == ("google", ["apple: not configured"])


def test_an_unsupported_request_falls_back(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    choice = run_places(db, secrets_key, FakePlaces(), NOW, search(open_now=True))
    assert (choice.provider, choice.skipped) == ("google", ["apple: can't do this request"])

    async def arrive_by_car(env: PlaceEnv) -> Any:
        return await env.places.travel_between(HERE_MILAN, [(45.469, 9.102)], "drive", arrive_by=NOW)

    set_settings(db, {"places": {"routes": ["google", "apple"]}})
    _, provider, skipped = run_places(db, secrets_key, FakePlaces(), NOW, arrive_by_car)
    assert (provider, skipped) == (
        "apple",
        ["google: can't do this request"],
    )  # Google: arrival time by transit only


def test_the_cap_skips_paid_calls_and_notifies_once_a_day(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"search": ["google", "apple"], "monthly_cap_eur": 0}})
    db.execute(  # the free Text Search Pro allowance is used up this month
        "INSERT INTO place_calls (provider, sku, units, at, ok) VALUES ('google', "
        "'google.text_search_pro', %s, %s, true)",
        (PRICES["google.text_search_pro"].free_per_month, NOW),
    )
    choice = run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert (choice.provider, choice.skipped) == ("apple", ["google: monthly cap reached"])  # Apple is free
    run_places(db, secrets_key, FakePlaces(), NOW.replace(hour=15), search())
    alerts = db.execute("SELECT kind, text FROM notifications").fetchall()
    assert alerts == [
        ("alert", "Ho raggiunto il limite mensile di 0,00 € per mappe e ricerche web: fino a fine mese uso "
                  "solo i "
                  "servizi gratuiti.")
    ]  # fmt: skip
    set_settings(db, {"places": {"search": ["google"]}})
    with pytest.raises(PisaError) as err:
        run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert err.value.code == "places_cap_reached" and err.value.http_status == 402
    assert "monthly cap" in err.value.message and "google: monthly cap reached" in err.value.message


def test_calls_inside_the_free_allowance_pass_a_zero_cap(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"search": ["google"], "monthly_cap_eur": 0}})
    assert run_places(db, secrets_key, FakePlaces(), NOW, search()).provider == "google"
    assert place_calls(db) == [("google", "google.text_search_pro", 1, True)]


def test_two_paid_calls_racing_at_the_cap_cannot_both_pass(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    """The cap is reserved, not only checked. Room for one paid call (€0.0275 of €0.04):
    two calls at once, the second finds the first's reservation and is refused; nothing stays reserved."""
    from pisa.ports.places import Usage

    set_settings(db, {"places": {"search": ["google"], "monthly_cap_eur": 0.04}})
    db.execute(
        "INSERT INTO place_calls (provider, sku, units, at, ok) "
        "VALUES ('google', 'google.text_search_pro', %s, %s, true)",
        (PRICES["google.text_search_pro"].free_per_month, NOW),
    )

    async def both(env: PlaceEnv) -> list[Any]:
        async def slow(adapter: Any) -> list[str]:
            await asyncio.sleep(0.2)  # the provider answers while the other call checks the cap
            return ["found"]

        def one() -> Any:
            return env.providers.choose("search", slow, usage=lambda a: Usage("google.text_search_pro"))

        return await asyncio.gather(one(), one(), return_exceptions=True)

    first, second = run_places(db, secrets_key, FakePlaces(), NOW, both)
    outcomes = sorted(["passed" if not isinstance(r, Exception) else r.code for r in (first, second)])  # type: ignore[union-attr]
    assert outcomes == ["passed", "places_cap_reached"]
    assert db.execute("SELECT count(*) FROM place_calls WHERE reserved").fetchone() == (0,)


def test_nothing_configured(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PisaError) as err:
        run_places(db, secrets_key, FakePlaces(), NOW, search(), keys=())
    assert err.value.code == "place_provider_not_configured" and err.value.http_status == 409
    assert (
        err.value.message
        == "No provider for place search is ready (apple: not configured; google: not configured)."
    )


def test_every_provider_failed(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    fake = FakePlaces()
    fake.fail["apple.search"] = (500, {})
    fake.fail["google.search"] = (503, {"error": {"message": "Backend error"}})
    with pytest.raises(PisaError) as err:
        run_places(db, secrets_key, fake, NOW, search())
    assert err.value.code == "place_provider_error" and err.value.http_status == 502
    assert (
        err.value.message == "Place search failed (Google Maps refused the call (HTTP 503: Backend error))."
    )


def test_a_switched_off_job(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"places": {"web": []}})

    async def web(env: PlaceEnv) -> Any:
        return await env.providers.choose(
            "web", lambda a: a.search("x", max_results=1), usage=lambda a: a.usage_for_search()
        )

    with pytest.raises(PisaError) as err:
        run_places(db, secrets_key, FakePlaces(), NOW, web)
    assert err.value.message == "Web research is switched off (Settings → Places has no provider for it)."


def test_every_call_is_recorded(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    at_location(db, HERE_MILAN, NOW)
    run_places(db, secrets_key, FakePlaces(), NOW, search())
    assert place_calls(db) == [("apple", "apple.token", 1, True), ("apple", "apple.search", 1, True)]


def test_cli_reads_the_maps_key_from_stdin_only(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from pisa import cli
    from pisa.settings import get_settings

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setenv("PISA_SECRETS_KEY", "x" * 43 + "=")
    monkeypatch.setattr("pisa.cli.configure_logging", lambda level: None)  # keep structlog's test defaults
    get_settings.cache_clear()
    try:
        assert cli.main(["secrets", "set-maps", "--key-id", MAPS_KEY_ID]) == 2
    finally:
        get_settings.cache_clear()
    assert "pipe the .p8 file" in capsys.readouterr().err


def test_maps_key_validation(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def install(key_id: str) -> None:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            await set_maps_key(conn, KeyStore(SecretStr(secrets_key)), key_id, p8()[0])

    with pytest.raises(PisaError) as err:
        asyncio.run(install("short"))
    assert err.value.message == "An Apple Maps key id is 10 letters and digits, e.g. AB12CD34EF."
    asyncio.run(install(MAPS_KEY_ID))
    ciphertext, meta = db.execute(
        "SELECT ciphertext, meta FROM server_secrets WHERE name = 'maps'"
    ).fetchone()  # type: ignore[misc]
    assert meta == {"key_id": MAPS_KEY_ID} and b"PRIVATE KEY" not in bytes(ciphertext)


def test_an_unusable_key_skips_only_that_provider(db, secrets_key) -> None:  # type: ignore[no-untyped-def]
    async def go(env: PlaceEnv) -> Any:
        async with env.pool.connection() as conn:  # Apple's key was sealed with another PISA_SECRETS_KEY
            await conn.execute("UPDATE server_secrets SET ciphertext = %s WHERE name = 'maps'", (b"garbage",))
        return await search()(env)

    choice = run_places(db, secrets_key, FakePlaces(), NOW, go)
    assert choice.provider == "google"
    assert choice.skipped == [
        "apple: A stored secret can't be decrypted: PISA_SECRETS_KEY changed since it was saved."
    ]
