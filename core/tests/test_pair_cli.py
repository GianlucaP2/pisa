"""The command line of the privacy revision, against a real Pisa server (uvicorn, TLS with a test
certificate):
- `pisa pair` shows a QR (and the typed code) and confirms the request whose 6 digits match, here or later
  with `--confirm`;
- `pisa client pair` pins the server's TLS key to the QR's pin before sending anything, compares the SAS, and
  keeps its software key; `pisa client token` signs a nonce for a session; `pisa client call --sign` signs a
  device op (`model_server`) with the same key, once it was paired as a signer;
- `pisa setup` walks its four steps without a prompt (`--yes`), each saying what it asks, why, what yes and no
  change and where to change it later, in English and Italian;
- `pisa-conformance live` passes against `/v2/live` with a session token."""

import asyncio
import contextlib
import hashlib
import io
import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from pisa import cli
from pisa.cli_pair import answer_waiting, pair_flow
from pisa.client import Client, ClientError
from pisa.domain import guide
from pisa.domain import pairing as rules
from pisa.services import pairing
from tests.conftest import cloned_database, dsn, pair_v2, set_key, uvicorn_logging_kept
from tests.test_live_v2_socket import hello, run
from tests.test_openai_compat import _certificates


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def serving(
    settings: Any, fake_models: Any, clock: Any, port: int, tls: tuple[Path, Path] | None
) -> Iterator[None]:
    """The real app (fake models) under uvicorn in a thread, with TLS when given."""
    import uvicorn

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    async def factory(s: Any) -> Any:
        return await Runtime.start(s, model_factory=fake_models, clock=clock)

    kwargs: dict[str, Any] = {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])} if tls else {}
    # uvicorn's loggers as they were once the server is gone (later tests read them)
    with uvicorn_logging_kept():
        config = uvicorn.Config(create_app(settings, runtime_factory=factory), host="127.0.0.1", port=port,
                                log_config=None, log_level=None, access_log=False, **kwargs)  # fmt: skip
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        try:
            yield
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            # the API's lock (one API per database) is released only when its lifespan ends
            assert not thread.is_alive(), "the test server did not stop"


@pytest.fixture
def test_database_url(worker_id: str) -> Iterator[str]:
    """Each test here gets a migrated database of its own, not the worker's shared one: the real API under
    uvicorn takes the one-API-per-database lock, which must never meet another test's API (the `client`
    fixture's, or an earlier server's) on the same database."""
    with cloned_database("pisa_test_" + re.sub(r"[^a-z0-9]", "", worker_id.lower()) + "_srv") as url:
        yield url


def on_db(db: psycopg.Connection, go: Any) -> Any:
    async def main() -> Any:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            return await go(conn)

    return asyncio.run(main())


@pytest.fixture
def https(
    app_settings: Any, tmp_path: Path, db: Any, fake_models: Any, clock: Any
) -> Iterator[dict[str, Any]]:
    _, cert, key = _certificates(tmp_path)
    port = free_port()
    url = f"https://127.0.0.1:{port}"
    settings = app_settings.model_copy(update={"tls_cert": str(cert), "addresses": url})
    with serving(settings, fake_models, clock, port, (cert, key)):
        yield {"url": url, "settings": settings, "pin": rules.spki_pin(cert.read_text())}


def show_code(db: psycopg.Connection, settings: Any, name: str = "CLI", **kw: Any) -> dict[str, Any]:
    out = io.StringIO()
    code = on_db(db, lambda c: pair_flow(c, settings, name, wait=False, as_json=True, out=out, **kw))
    assert code == 0
    return json.loads(out.getvalue())


def test_pisa_pair_shows_the_qr_and_the_typed_code(db: psycopg.Connection, https: dict[str, Any]) -> None:
    out = io.StringIO()
    assert on_db(db, lambda c: pair_flow(c, https["settings"], "iPhone", wait=False, out=out, lang="en")) == 0
    text = out.getvalue()
    (uri,) = [line for line in text.splitlines() if line.startswith("pisa://pair?")]
    qr = rules.parse_qr(uri)
    assert qr["spki"] == https["pin"] and qr["addrs"] == [https["url"]]
    assert guide.token_shown(qr["token"]) in text and "Scan this code" in text
    assert "█" in text or "▀" in text or "▄" in text  # the QR itself, drawn in the terminal


def test_the_cli_pairs_pinned_confirms_by_sas_and_signs_a_device_op(
    db: psycopg.Connection, https: dict[str, Any], tmp_path: Path
) -> None:
    # the iPhone's Face ID key exists first: a signer's device key joins the store only beside one (security
    # review finding P1), and from then on account-level ops need a signature: the CLI signs with its device
    # key
    phone_id, _ = pair_v2(db, "iPhone")
    phone_key = db.execute("SELECT device_key FROM devices WHERE id = %s", (phone_id,)).fetchone()
    assert phone_key is not None
    db.execute(
        "INSERT INTO phone_keys (key_id, public_key, label, device_id, created_at) VALUES ('face', %s, 'x', "
        "%s, now())",
        (rules.x963(phone_key[0]), phone_id),
    )
    shown = show_code(db, https["settings"], signer=True)
    client = Client.new(https["url"])
    client.data["spki"] = [shown["qr"]["spki"]]

    async def pair_it() -> dict[str, Any]:
        async with client.http() as http:
            return await client.pair(http, shown["uri"], "CLI")

    started = asyncio.run(pair_it())
    (waiting,) = on_db(db, lambda c: pairing.waiting(c))
    assert waiting.sas == started["sas"] and waiting.platform == "cli"  # the same 6 digits on both sides
    assert on_db(db, lambda c: answer_waiting(c, started["sas"], True, lang="it")) == 0

    async def use_it() -> list[Any]:
        async with client.http() as http:
            state = await client.wait_confirmed(http, started["pairingId"], 10)
            listed = await client.call(http, "GET", "/v1/devices")
            body = json.dumps(
                {"type": "openai_compatible", "id": "home-llm", "base_url": "http://10.0.0.5:8000/v1"}
            )
            target = f"home-llm:{hashlib.sha256(body.encode()).hexdigest()}"
            unsigned = await client.call(http, "POST", "/v1/ai/providers", body=body)
            signed = await client.call(
                http, "POST", "/v1/ai/providers", body=body, sign=("model_server", target)
            )
            return [state, listed.status_code, unsigned, signed]

    state, listed, unsigned, signed = asyncio.run(use_it())
    assert state == "confirmed" and listed == 200
    assert unsigned.status_code == 403 and unsigned.json()["error"]["code"] == "biometric_required"
    assert signed.status_code == 201, signed.text
    path = tmp_path / "client.json"
    client.save(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "deviceId" in json.loads(path.read_text()) and "token" not in json.loads(path.read_text())


def test_the_client_refuses_a_server_whose_key_is_not_the_qr_s(
    db: psycopg.Connection, https: dict[str, Any]
) -> None:
    shown = show_code(db, https["settings"])
    client = Client.new(https["url"])
    client.data["spki"] = ["A" * 43 + "="]  # a QR from another server: whoever answers here is in between
    with pytest.raises(ClientError, match="not the one in the pairing code"):
        client.http()
    assert on_db(db, lambda c: pairing.waiting(c)) == []  # nothing was sent
    assert shown["token"]


def test_pisa_client_commands_end_to_end(
    db: psycopg.Connection, https: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setattr(cli, "get_settings", lambda: https["settings"])
    path = str(tmp_path / "c.json")
    assert cli.main(["pair", "CLI", "--no-wait", "--json"]) == 0
    uri = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["uri"]  # after the log lines
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            cli.main(["client", "pair", uri, "--server", https["url"], "--file", path])
        ),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 20
    while not (found := on_db(db, lambda c: pairing.waiting(c))) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert cli.main(["pair", "--confirm", found[0].sas]) == 0
    thread.join(timeout=30)
    assert result == [0]
    capsys.readouterr()
    assert cli.main(["client", "token", "--file", path]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert len(rules.unb64(token, "t")) == 32
    assert cli.main(["client", "call", "GET", "/v1/ai/disclosure", "--file", path]) == 0
    assert (
        json.loads(capsys.readouterr().out.strip().splitlines()[-1])["healthRequiresSeparateConsent"] is True
    )
    assert cli.main(["pair", "--confirm", "000000"]) == 1  # nothing waits with these digits


def test_pisa_pair_refuses_a_request_whose_digits_differ(
    db: psycopg.Connection, https: dict[str, Any]
) -> None:
    out = io.StringIO()

    async def go(conn: Any) -> int:
        task = asyncio.create_task(pair_flow(conn, https["settings"], "iPhone", yes_sas="123 456", out=out,
                                             qr=False, lang="en"))  # fmt: skip
        for _ in range(400):  # the code is shown before it waits
            if "Waiting" in out.getvalue():
                break
            await asyncio.sleep(0.05)
        uri = next(line for line in out.getvalue().splitlines() if line.startswith("pisa://"))
        client = Client.new(https["url"])
        client.data["spki"] = [https["pin"]]
        async with client.http() as http:
            started = await client.pair(http, uri, "Someone")
            code = await task
            state = (await http.get(f"/v1/pair/{started['pairingId']}")).json()["state"]
        return code if started["sas"] != "123456" and state == "refused" else -1

    assert on_db(db, go) == 1
    assert "The digits differ" in out.getvalue()


# --- pisa setup ---------------------------------------------------------------------------------------------
def setup_args(**over: Any) -> Any:
    import argparse

    base = {"yes": True, "lang": None, "skip": None, "language": None, "timezone": None, "country": None,
            "ai": None, "provider": None, "key_stdin": False, "server_url": None, "server_id": None,
            "model": None,
            "push": None, "pair": None, "signer": False, "yes_sas": None}  # fmt: skip
    return argparse.Namespace(**(base | over))


@pytest.mark.parametrize("lang", ["en", "it"])
def test_setup_explains_each_step_and_runs_without_a_prompt(
    app_settings: Any, db: psycopg.Connection, lang: str
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    out = io.StringIO()
    args = setup_args(lang=lang, language="en", timezone="Europe/London", country="GB", push="relay")
    settings = app_settings.model_copy(update={"database_url": dsn(db), "push_relay_url": ""})
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out))) == 0
    text = out.getvalue()
    for step in guide.STEPS:
        for line in guide.explain(step, lang):
            assert line in text
    assert "PISA_TIMEZONE=Europe/London" in text and "PISA_COUNTRY=GB" in text
    assert guide.text("push.relay_missing", lang) in text  # relay chosen, not set on this server
    assert guide.text("done", lang) in text
    # the suite's user speaks Italian: English is now set
    assert db.execute("SELECT value->>'language' FROM app_settings WHERE key = 'settings'").fetchone() == (
        "en-US",
    )


def test_setup_asks_on_a_terminal_and_skips_what_it_is_told(
    app_settings: Any, db: psycopg.Connection
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    answers = iter(["", "", "none"])  # language, time zone, push: the country is init's, not asked
    out = io.StringIO()
    prompt = Prompt(True, ask=lambda q: next(answers), out=out)
    args = setup_args(yes=False, lang="en", skip=["ai,pair"])
    settings = app_settings.model_copy(update={"database_url": dsn(db), "country": None})  # none set yet
    assert asyncio.run(run_setup(args, settings, prompt)) == 0
    text = out.getvalue()
    assert text.count(guide.text("skipped", "en")) == 2 and guide.text("push.none", "en") in text
    assert "PISA_TIMEZONE=" not in text  # Enter kept every value
    assert guide.text("place.country_unset", "en") in text and "--force --country IT" in text
    assert "PISA_COUNTRY=" not in text


def test_setup_confirms_the_country_init_set(app_settings: Any, db: psycopg.Connection) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    answers = iter(["", "", "none"])
    out = io.StringIO()
    args = setup_args(yes=False, lang="it", skip=["ai,pair"])
    settings = app_settings.model_copy(update={"database_url": dsn(db), "country": "IT"})
    assert asyncio.run(run_setup(args, settings, Prompt(True, ask=lambda q: next(answers), out=out))) == 0
    text = out.getvalue()
    assert guide.text("place.country_set", "it", country="IT") in text
    assert guide.text("place.country_unset", "it") not in text and "PISA_COUNTRY=" not in text


@pytest.mark.parametrize(
    ("argv", "log_level", "level", "to_stderr"),
    [(["setup", "--yes"], "info", "warning", True), (["setup", "--yes"], "debug", "debug", True),
     (["doctor"], "info", "warning", True), (["doctor"], "error", "warning", True),
     (["pair", "phone"], "info", "warning", True), (["secrets", "set", "openai"], "info", "warning", True),
     (["models", "set", "chat.conversation", "openai:x"], "info", "warning", True),
     (["catalog", "refresh"], "info", "warning", True), (["consent", "--yes"], "info", "warning", True),
     (["serve-api"], "info", "info", False), (["worker"], "debug", "debug", False)],
)  # fmt: skip
def test_interactive_commands_log_warnings_and_up_to_stderr(
    monkeypatch: pytest.MonkeyPatch, app_settings: Any, argv: list[str], log_level: str, level: str,
    to_stderr: bool,
) -> None:  # fmt: skip
    """setup, pair, secrets and doctor print for a person: their logs go to stderr, warnings and up (all of
    them at PISA_LOG_LEVEL=debug). The server processes keep stdout and PISA_LOG_LEVEL."""
    import sys

    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr("pisa.cli.configure_logging", lambda lvl, **kw: calls.append((lvl, kw.get("stream"))))
    configured = app_settings.model_copy(update={"log_level": log_level})
    monkeypatch.setattr("pisa.cli.get_settings", lambda: configured)
    for name in (
        "_cmd_doctor",
        "_cmd_serve_api",
        "_cmd_worker",
        "_cmd_secrets_set",
        "_cmd_models_set",
        "_cmd_catalog_refresh",
    ):
        monkeypatch.setattr(f"pisa.cli.{name}", lambda _: 0)
    monkeypatch.setattr("pisa.cli_setup.cmd_setup", lambda _, __: 0)
    monkeypatch.setattr("pisa.cli_pair.cmd_pair", lambda _, __: 0)
    monkeypatch.setattr("pisa.cli_consent.cmd_consent", lambda _, __: 0)
    assert cli.main(argv) == 0
    assert calls == [(level, sys.stderr if to_stderr else None)]


def test_setup_prints_no_log_line_and_speaks_the_guide_s_language(
    app_settings: Any,
    db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The owner's Mac: `pisa setup` in Italian printed provider_key_set and the other INFO JSON lines among
    its own, and "Prova: 14 models are available." Logged as `pisa setup` logs, stdout holds the guide alone,
    in Italian."""
    import sys

    import httpx
    import structlog

    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup
    from pisa.logging import configure_logging

    def models(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "gpt-4o", "object": "model"}, {"id": "gpt-4o-mini", "object": "model"}]})  # fmt: skip
        return httpx.Response(404)

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-test-not-a-real-key\n"))
    args = setup_args(lang="it", skip=["place,push,pair"], ai="cloud", provider="openai", key_stdin=True)
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(models))
    try:
        configure_logging("warning", stream=sys.stderr)  # as `pisa.cli.main` does for `setup`
        assert asyncio.run(run_setup(args, settings, Prompt(False), http=http)) == 0
    finally:
        structlog.reset_defaults()
    out = capsys.readouterr().out
    assert guide.text("ai.key_set", "it", result="sono disponibili 2 modelli.") in out
    assert "models are available" not in out
    for line in out.splitlines():
        assert not line.lstrip().startswith("{"), line  # no JSON log line


@pytest.mark.parametrize(
    ("ok", "found", "message", "it"),
    [(True, 14, "14 models are available.", "sono disponibili 14 modelli."),
     (True, 0, "The key works.", "la chiave funziona."),
     (None, 0, "X offers no free way to check a key", "questo fornitore non offre"),
     (False, 0, "The OpenAI model list failed (ConnectError).", "non è riuscita (The OpenAI model list")],
)  # fmt: skip
def test_the_key_test_line_is_in_the_guide_s_language(
    ok: bool | None, found: int, message: str, it: str
) -> None:
    from pisa.cli_setup import test_line
    from pisa.services.ai import TestResult

    result = TestResult(ok, message, found)
    assert test_line(result, "en") == message  # English: as before
    assert test_line(result, "it").startswith(it)


def test_pair_without_a_name_says_so_in_pisa_s_language() -> None:
    assert guide.pair_text("need_name", "it-IT").startswith("errore:")
    assert guide.pair_text("need_name", "en") == "error: give the device's name: pisa pair <name>"


@pytest.mark.parametrize(("preset", "set_it"), [("vllm", True), ("vllm-no-tools", False)])
def test_setup_adds_the_user_s_model_server_probes_it_and_says_what_needs_tools(
    app_settings: Any, db: psycopg.Connection, preset: str, set_it: bool
) -> None:
    import httpx

    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup
    from tests.openai_compat_fake import FakeServer

    out = io.StringIO()
    args = setup_args(lang="en", skip=["place,push,pair"], ai="own", server_url="http://home-llm.lan:8000/v1",
                      server_id="home-llm", model="home-llm:test-model")  # fmt: skip
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    http = httpx.AsyncClient(transport=FakeServer.preset(preset).transport())
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out), http=http)) == 0
    text = out.getvalue()
    assert guide.text("ai.server_added", "en", id="home-llm") in text and "It can: " in text
    slot = db.execute("SELECT model_ref FROM model_slots WHERE id = 'chat.conversation'").fetchone()
    if set_it:
        assert guide.text("ai.model_set", "en", model="home-llm:test-model") in text
        assert slot == ("home-llm:test-model",)
    else:  # no tool calling: the guide names what would not work, and the conversation keeps its model
        assert "Without tool calling these features do not work" in text and "conversation" in text
        assert slot != ("home-llm:test-model",)


# --- the conformance kit, live ------------------------------------------------------------------------------
def test_pisa_conformance_live_passes_against_v2_live(
    app_settings: Any, db: psycopg.Connection, fake_models: Any, clock: Any, secrets_key: str, tmp_path: Path,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from pisa_conformance.cli import main as kit

    set_key(db, secrets_key)
    device_id, token = pair_v2(db)
    hello_file, send_file = tmp_path / "hello.json", tmp_path / "send.jsonl"
    hello_file.write_text(json.dumps(hello(device_id)))
    send_file.write_text(json.dumps(run("ciao")) + "\n" + json.dumps({"op": "ping"}) + "\n")
    monkeypatch.setenv("PISA_TEST_SESSION", token)
    port = free_port()
    with serving(app_settings, fake_models, clock, port, None):
        code = kit(["live", f"ws://127.0.0.1:{port}/v2/live", "--token-env", "PISA_TEST_SESSION", "--hello",
                    str(hello_file), "--send", str(send_file), "--seconds", "6"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out


def test_local_mode_pairs_over_the_pair_pisa_init_made(
    db: psycopg.Connection, app_settings: Any, tmp_path: Path, fake_models: Any, clock: Any
) -> None:
    """docker compose's local mode end to end, minus docker: `pisa init --mode local` makes the key and
    certificate, the API serves TLS with them (PISA_API_TLS_*), the QR carries that certificate's pin, and a
    client that trusts only that pin starts pairing."""
    from pisa.cli_init import local_tls

    port = free_port()
    values = {"PISA_ADDRESSES": f"https://127.0.0.1:{port}"}
    local_tls(tmp_path, values, say=lambda _line: None)
    cert, key = tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"
    settings = app_settings.model_copy(
        update={"tls_cert": str(cert), "addresses": values["PISA_ADDRESSES"], "api_tls_cert": str(cert),
                "api_tls_key": str(key)}
    )  # fmt: skip
    options = cli.api_tls(settings)
    assert options is not None
    with serving(settings, fake_models, clock, port, (Path(options[0]), Path(options[1]))):
        shown = show_code(db, settings)
        assert shown["qr"]["spki"] == rules.spki_pin(cert.read_text())
        client = Client.new(values["PISA_ADDRESSES"])
        client.data["spki"] = [shown["qr"]["spki"]]

        async def pair_it() -> dict[str, Any]:
            async with client.http() as http:
                return await client.pair(http, shown["uri"], "CLI")

        started = asyncio.run(pair_it())
        (waiting,) = on_db(db, lambda c: pairing.waiting(c))
        assert waiting.sas == started["sas"]


# --- the tailscale and caddy modes: a WebPKI proxy in front, its leaf pinned for pairing -------------------


@pytest.fixture
def webpki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A test CA standing in for the WebPKI roots, and a leaf it signed for 127.0.0.1 (what Caddy or
    `tailscale serve` would present)."""
    import ssl

    from pisa.services import endpoints

    ca, cert, key = _certificates(tmp_path)
    endpoints._observed.clear()

    def trusting_the_test_ca() -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(ca))

    yield {
        "ca": ca,
        "cert": cert,
        "key": key,
        "trust": trusting_the_test_ca,
        "pin": rules.spki_pin(cert.read_text()),
    }
    endpoints._observed.clear()


def pairs_over(db: psycopg.Connection, settings: Any, url: str) -> str:
    """Show a code, then pair a client that trusts only the QR's pin: the pin shown."""
    shown = show_code(db, settings)
    client = Client.new(url)
    client.data["spki"] = [shown["qr"]["spki"]]

    async def pair_it() -> dict[str, Any]:
        async with client.http() as http:
            return await client.pair(http, shown["uri"], "CLI")

    started = asyncio.run(pair_it())
    (waiting,) = on_db(db, lambda c: pairing.waiting(c))
    assert waiting.sas == started["sas"]
    return shown["qr"]["spki"]


def test_tailscale_mode_pins_the_certificate_the_sidecar_exported(
    db: psycopg.Connection, app_settings: Any, tmp_path: Path, fake_models: Any, clock: Any,
    webpki: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from pisa.domain.errors import PisaError
    from pisa.services import endpoints

    exported = tmp_path / "ts-cert"
    exported.mkdir()
    monkeypatch.setattr(endpoints, "TAILSCALE_CERT_DIR", f"{exported}/")
    port = free_port()
    url = f"https://127.0.0.1:{port}"
    settings = app_settings.model_copy(update={"tls_cert": f"{exported}/cert.pem", "addresses": url})
    with pytest.raises(PisaError, match="Tailscale hasn't fetched its certificate yet"):
        endpoints.pins(settings)  # before the sidecar's first export
    assert not cli._pairing_possible(settings)
    (exported / "cert.pem").write_bytes(webpki["cert"].read_bytes())  # the sidecar's export: no key
    with serving(settings, fake_models, clock, port, (webpki["cert"], webpki["key"])):
        assert pairs_over(db, settings, url) == webpki["pin"]


def test_caddy_mode_pins_the_leaf_caddy_presents_once_it_passes_webpki(
    db: psycopg.Connection, app_settings: Any, fake_models: Any, clock: Any, webpki: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from pisa.services import endpoints

    monkeypatch.setattr(endpoints, "webpki_context", webpki["trust"])
    port = free_port()
    url = f"https://127.0.0.1:{port}"
    settings = app_settings.model_copy(
        update={"tls_cert": None, "tls_pin_from": f"127.0.0.1:{port}", "addresses": url}
    )
    with serving(settings, fake_models, clock, port, (webpki["cert"], webpki["key"])):
        assert endpoints.pins(settings) == [webpki["pin"]]
        assert pairs_over(db, settings, url) == webpki["pin"]


@pytest.mark.parametrize("why", ["untrusted", "wrong name"])
def test_caddy_mode_never_pins_a_certificate_that_fails_webpki(
    db: psycopg.Connection, app_settings: Any, fake_models: Any, clock: Any, webpki: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, why: str,
) -> None:  # fmt: skip
    from pisa.domain.errors import PisaError
    from pisa.services import endpoints

    port = free_port()
    host = "127.0.0.1"
    if why == "wrong name":  # a CA the test trusts, but the leaf names 127.0.0.1, not the address's host
        monkeypatch.setattr(endpoints, "webpki_context", webpki["trust"])
        host = "localhost"
    settings = app_settings.model_copy(
        update={"tls_cert": None, "tls_pin_from": f"127.0.0.1:{port}", "addresses": f"https://{host}:{port}"}
    )
    with serving(settings, fake_models, clock, port, (webpki["cert"], webpki["key"])):
        with pytest.raises(PisaError, match="fails WebPKI verification"):
            endpoints.pins(settings)
        out = io.StringIO()
        with pytest.raises(PisaError, match="not pinned"):
            on_db(db, lambda c: pair_flow(c, settings, "CLI", wait=False, out=out))
        assert "pisa://pair" not in out.getvalue()
    assert on_db(db, lambda c: pairing.waiting(c)) == []
