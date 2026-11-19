"""Headless pairing and signing (D-138; audit M3 and M5): nothing needs the iPhone app.

M5, a client's own device key (the P-256 key it registered at pairing, key id `device-<its device id>`) signs
every signed op with the same proof as a Face ID key: a single-use challenge, its 5 minutes, the exact text,
and a device that is not revoked:
- a headless client erases every family of their data, on a fresh install (no Face ID key anywhere);
- it approves (`pisa-approval/2`, signed) and rejects an approval over the REST resume;
- it signs an autonomy level, and vouches (`add_key`) for another device's new key, never for its own;
- a revoked device's key, another device's key, a reused nonce, an iPhone's device key beside its Face ID key
  and a watch's key on an approval are all refused.

M3, pairing without the app and without public TLS:
- PISA_TLS_CERT takes the PEM itself (its line breaks real or `\\n`), as well as a file;
- insecure loopback mode (PISA_INSECURE_LOOPBACK_PAIRING) is refused unless every address is http on loopback,
  no certificate is set and the API listens on loopback (or runs in a container), and then answers only this
  machine;
- the whole first hour against a real server in that mode: `pisa client pair` with the typed code, confirmed
  by `pisa pair --confirm`, then `pisa client erase all`, `pisa client approve` and `pisa client reject`."""

import asyncio
import contextlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from pisa import cli
from pisa.api import loopback
from pisa.domain import pairing as rules
from pisa.domain.errors import PisaError
from pisa.services import endpoints, pairing
from pisa.services.approvals import Outcome
from pisa.services.devices import revoke_device
from pisa.settings import Settings
from tests.conftest import pair_v2_key, uvicorn_logging_kept
from tests.phone_signing import challenge, device_headers, new_key, register, shown_digest, sign
from tests.test_pair_cli import free_port, on_db, test_database_url  # noqa: F401 (a database per test)

PAYLOAD = {"calendar": "primary", "title": "Lunch", "start": "2026-10-21T12:30:00+02:00", "lang": "en-US"}
FAMILIES = ("messages", "places", "mail", "money", "records", "memory", "health", "voice", "learn")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def key_of(device_id: str) -> str:
    return f"device-{device_id}"


def refused(response: Any, status: int = 403) -> bool:
    return response.status_code == status and response.json()["error"]["code"] == "biometric_required"


def erase(client: Any, token: str, key: Any, key_id: str, family: str = "messages") -> Any:
    target = f"{family}:2026-10-01"
    headers = device_headers(client, auth(token), key, "erase", target, key_id=key_id)
    return client.post("/v1/data/erase", json={"family": family, "before": "2026-10-01"}, headers=headers)


@pytest.fixture
def ran(client: Any) -> list[dict[str, Any]]:
    done: list[dict[str, Any]] = []

    async def execute(payload: dict[str, Any]) -> Outcome:
        done.append(payload)
        return Outcome("Added Lunch on Wednesday at 12:30.")

    async def verify(payload: dict[str, Any], outcome: Outcome) -> bool:
        return True

    client.app.state.runtime.approvals.register("calendar_create", execute, verify=verify)
    return done


def make_approval(client: Any, device_id: str) -> str:
    approvals = client.app.state.runtime.approvals
    view = client.portal.call(
        lambda: approvals.create(
            "calendar_create", "Add «Lunch» on Wednesday?", ["Wednesday 12:30"], PAYLOAD,
            requires_biometric=True, device_id=uuid.UUID(device_id), lang="en-US",
        )
    )  # fmt: skip
    return str(view["id"])


def signed_resume(client: Any, token: str, key: Any, key_id: str, approval_id: str) -> tuple[Any, dict]:
    made = challenge(
        client, auth(token), action="approval", approval_id=approval_id, interrupt_id=f"int-{approval_id}"
    )
    digest = made["canonical"].split("digest=")[1].split("\n")[0]
    body = {"decision": "approve", "auth": "biometric", "digest": digest, "nonce": made["nonce"],
            "signature": sign(key, made["canonical"]), "keyId": key_id}  # fmt: skip
    return client.post(f"/v1/approvals/{approval_id}/resume", headers=auth(token), json=body), body


# --- M5: erase -------------------------------------------------------------------------------------------
def test_a_headless_client_erases_every_family_with_its_own_key_on_a_fresh_install(
    client: Any, db: psycopg.Connection
) -> None:
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    assert db.execute("SELECT count(*) FROM phone_keys").fetchone() == (0,)  # no Face ID key anywhere
    unsigned = client.post(
        "/v1/data/erase", json={"family": "messages", "before": "2026-10-01"}, headers=auth(token)
    )
    assert refused(unsigned)
    for family in FAMILIES:
        made = erase(client, token, key, key_of(cli_id), family)
        assert made.status_code == 202, made.text
    assert db.execute("SELECT count(DISTINCT family) FROM erase_jobs").fetchone() == (len(FAMILIES),)


def test_a_device_key_signs_only_for_its_own_device_and_only_once(
    client: Any, db: psycopg.Connection
) -> None:
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    other_id, other_token, other_key = pair_v2_key(db, "Laptop", "cli")
    # the other device's key and name, sent on this device's session: refused
    assert refused(erase(client, token, other_key, key_of(other_id)))
    # this device's name with another key: the signature fails
    assert refused(erase(client, token, other_key, key_of(cli_id)))
    # a reused nonce: refused, though the signature is good
    target = "messages:2026-10-01"
    headers = device_headers(client, auth(token), key, "erase", target, key_id=key_of(cli_id))
    body = {"family": "messages", "before": "2026-10-01"}
    assert client.post("/v1/data/erase", json=body, headers=headers).status_code == 202
    assert refused(client.post("/v1/data/erase", json=body, headers=headers))
    # an expired challenge: refused
    headers = device_headers(client, auth(token), key, "erase", "mail:2026-10-01", key_id=key_of(cli_id))
    db.execute("UPDATE phone_challenges SET expires_at = '2026-10-20 10:59+00'")  # the clock is 11:00
    assert refused(client.post("/v1/data/erase", json=body | {"family": "mail"}, headers=headers))
    assert other_token


def test_a_revoked_device_s_key_signs_nothing(client: Any, db: psycopg.Connection) -> None:
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    nonce = challenge(client, auth(token), action="device", op="erase", target="messages:2026-10-01")
    on_db(db, lambda c: revoke_device(c, uuid.UUID(cli_id)))
    keys = client.app.state.runtime.autopilot.keys
    with pytest.raises(PisaError, match="revoked device"):
        client.portal.call(
            lambda: keys.guard(
                "erase", "messages:2026-10-01", nonce["nonce"], sign(key, nonce["canonical"]), key_of(cli_id),
                always=True, device_id=uuid.UUID(cli_id),
            )
        )  # fmt: skip
    # and over the API its session is gone with it
    gone = client.post(
        "/v1/data/erase", json={"family": "messages", "before": "2026-10-01"}, headers=auth(token)
    )
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "device_revoked"


def test_once_a_face_id_key_exists_a_cli_still_signs_and_the_iphone_s_device_key_does_not(
    client: Any, db: psycopg.Connection
) -> None:
    iphone_id, iphone_token, iphone_key = pair_v2_key(db, "iPhone", "ios")
    face = register(client, auth(iphone_token), "face")
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    # owner ops now need a signature from everyone; the CLI's own key gives it
    assert refused(client.delete(f"/v1/devices/{iphone_id}", headers=auth(token)))
    assert erase(client, token, key, key_of(cli_id)).status_code == 202
    # the iPhone signs with Face ID, never with its device key (the app's path is unchanged)
    assert refused(erase(client, iphone_token, iphone_key, key_of(iphone_id), "mail"))
    assert erase(client, iphone_token, face, "face", "mail").status_code == 202


# --- M5: approvals -----------------------------------------------------------------------------------------
def test_a_headless_client_approves_with_its_device_key_and_rejects(
    client: Any, db: psycopg.Connection, ran: list[dict[str, Any]]
) -> None:
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    approval_id = make_approval(client, cli_id)
    digest = shown_digest(client, auth(token), approval_id)
    tap = client.post(
        f"/v1/approvals/{approval_id}/resume",
        headers=auth(token),
        json={"decision": "approve", "auth": "tap", "digest": digest},
    )
    assert refused(tap, 401)  # a tap never approves a signed approval
    answer, body = signed_resume(client, token, key, key_of(cli_id), approval_id)
    assert answer.status_code == 200, answer.text
    assert [e["name"] for e in answer.json()["events"] if e["type"] == "CUSTOM"] == ["pisa.action_done"]
    assert len(ran) == 1
    again = client.post(f"/v1/approvals/{approval_id}/resume", headers=auth(token), json=body)
    assert again.status_code == 409 and again.json()["error"]["code"] == "approval_decided"  # replay

    second = make_approval(client, cli_id)
    no = client.post(
        f"/v1/approvals/{second}/resume", headers=auth(token),
        json={"decision": "reject", "auth": "tap", "digest": shown_digest(client, auth(token), second)},
    )  # fmt: skip
    assert no.status_code == 200, no.text
    assert client.get(f"/v1/approvals/{second}", headers=auth(token)).json()["status"] == "rejected"
    assert len(ran) == 1


def test_an_approval_key_must_be_the_device_s_own_and_not_a_watch_s(
    client: Any, db: psycopg.Connection, ran: list[dict[str, Any]]
) -> None:
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    other_id, _, other_key = pair_v2_key(db, "Laptop", "cli")
    approval_id = make_approval(client, cli_id)
    # a challenge issued to this device, signed by another device's key under its own name
    answer, _ = signed_resume(client, token, other_key, key_of(other_id), approval_id)
    assert refused(answer, 401)
    watch_id, watch_token, watch_key = pair_v2_key(db, "Watch", "watchos")
    answer, _ = signed_resume(client, watch_token, watch_key, key_of(watch_id), approval_id)
    assert refused(answer, 401)  # WATCH-1: a watch only rejects
    assert ran == []
    answer, _ = signed_resume(client, token, key, key_of(cli_id), approval_id)
    assert answer.status_code == 200 and len(ran) == 1


# --- M5: autonomy, add_key -------------------------------------------------------------------------------
def test_a_device_key_signs_an_autonomy_level(client: Any, db: psycopg.Connection) -> None:
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    made = challenge(client, auth(token), action="autonomy", type="calendar_create", level="tell")
    body = {"level": "tell", "nonce": made["nonce"], "signature": sign(key, made["canonical"]),
            "key_id": key_of(cli_id)}  # fmt: skip
    answer = client.post("/v1/autonomy/calendar_create", headers=auth(token), json=body)
    assert answer.status_code == 200, answer.text


def test_a_device_key_vouches_for_another_device_s_key_never_its_own(
    client: Any, db: psycopg.Connection
) -> None:
    old_id, old_token, _ = pair_v2_key(db, "Old iPhone", "ios")
    register(client, auth(old_token), "face-old")  # a Face ID key exists: later keys need a signature
    cli_id, token, key = pair_v2_key(db, "CLI", "cli")
    _, new_token, _ = pair_v2_key(db, "New iPhone", "ios")
    _, public = new_key()

    def add(by_token: str, signer: Any, signer_id: str, to_token: str, key_id: str) -> Any:
        made = challenge(client, auth(by_token), action="add_key", key_id=key_id, public_key=public)
        body = {"key_id": key_id, "public_key": public, "nonce": made["nonce"],
                "signature": sign(signer, made["canonical"]), "signer_key_id": key_of(signer_id)}  # fmt: skip
        return client.post("/v1/phone/keys", headers=auth(to_token), json=body)

    assert refused(add(token, key, cli_id, token, "k-self"))  # never for its own device
    assert add(token, key, cli_id, new_token, "k-new").status_code == 201  # for the new iPhone
    on_db(db, lambda c: revoke_device(c, uuid.UUID(cli_id)))
    _, other_token, _ = pair_v2_key(db, "Tablet", "ios")
    made = challenge(client, auth(new_token), action="add_key", key_id="k-late", public_key=public)
    late = client.post("/v1/phone/keys", headers=auth(other_token), json={
        "key_id": "k-late", "public_key": public, "nonce": made["nonce"],
        "signature": sign(key, made["canonical"]), "signer_key_id": key_of(cli_id)})  # fmt: skip
    assert refused(late)  # a revoked device vouches for nothing
    named = client.post(
        "/v1/phone/keys", headers=auth(new_token), json={"key_id": key_of(old_id), "public_key": public}
    )
    assert named.status_code == 400  # a stored key never takes a device key's name
    assert old_id


# --- M3: the certificate inline, and insecure loopback mode -----------------------------------------------
def _pem(tmp_path: Path) -> tuple[Path, str]:
    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    path = tmp_path / "cert.pem"
    path.write_text(pem)
    return path, pem


def test_pisa_tls_cert_takes_the_pem_itself(app_settings: Any, tmp_path: Path) -> None:
    path, pem = _pem(tmp_path)
    from_file = endpoints.pins(app_settings.model_copy(update={"tls_cert": str(path)}))
    for inline in (pem, pem.strip().replace("\n", "\\n")):
        assert endpoints.pins(app_settings.model_copy(update={"tls_cert": inline})) == from_file
    with pytest.raises(PisaError, match="inline"):
        endpoints.pins(app_settings.model_copy(update={"tls_cert": "-----BEGIN PUBLIC KEY-----\\nnope"}))


def _settings(**values: Any) -> Settings:
    return Settings(timezone="Europe/Rome", insecure_loopback_pairing=True, **values)  # type: ignore[call-arg]


def test_insecure_loopback_mode_is_refused_unless_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pisa.settings.in_container", lambda: False)
    path, _ = _pem(tmp_path)
    ok = _settings(addresses="http://127.0.0.1:8000,http://[::1]:8000,http://localhost:8000")
    assert endpoints.pins(ok) == [rules.INSECURE_LOOPBACK_PIN]
    for bad in (
        {"addresses": "http://127.0.0.1:8000", "tls_cert": str(path)},
        {"addresses": ""},
        {"addresses": "https://127.0.0.1:8000"},
        {"addresses": "http://192.168.1.5:8000"},
        {"addresses": "http://127.0.0.1:8000,http://pisa.example.com"},
        {"addresses": "http://127.0.0.1:8000", "api_host": "0.0.0.0"},  # noqa: S104
    ):
        with pytest.raises(ValidationError, match="PISA_INSECURE_LOOPBACK_PAIRING is refused"):
            _settings(**bad)
    monkeypatch.setattr("pisa.settings.in_container", lambda: True)  # docker compose: 0.0.0.0 inside
    inside = _settings(addresses="http://127.0.0.1:8000", api_host="0.0.0.0")  # noqa: S104
    assert inside.insecure_loopback_pairing
    # the pin is no key's pin: it never matches a real certificate
    assert rules.INSECURE_LOOPBACK_PIN != rules.spki_pin(path.read_text())


def test_the_route_table_s_default_gateway_is_read(tmp_path: Path) -> None:
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway\tFlags\n"
        "eth0\t0000C0A8\t00000000\t0001\n"
        "eth0\t00000000\t01001DAC\t0003\n"  # 172.29.0.1, little-endian
    )
    assert loopback.default_gateway(table) == "172.29.0.1"
    assert loopback.default_gateway(tmp_path / "none") is None
    assert loopback.local_peers(True, "172.29.0.1", True) == frozenset({"172.29.0.1"})
    assert loopback.local_peers(True, "172.29.0.1") == frozenset()  # only when the owner says so (E1 M3-2)
    assert loopback.local_peers(False, "172.29.0.1", True) == frozenset()


def _through(guard: loopback.LoopbackOnly, kind: str, peer: str, headers: list[Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"} if kind == "websocket" else {"type": "http.request", "body": b""}

    async def send(message: Any) -> None:
        sent.append(dict(message))

    scope = {"type": kind, "client": (peer, 5000), "headers": headers, "path": "/v1/pair/begin"}
    asyncio.run(guard(scope, receive, send))
    return sent


def test_loopback_mode_answers_this_machine_only() -> None:
    passed: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        passed.append(scope["client"][0])

    guard = loopback.LoopbackOnly(app, frozenset({"172.29.0.1"}))
    for peer in ("127.0.0.1", "::1", "172.29.0.1"):
        assert _through(guard, "http", peer, []) == []
    assert passed == ["127.0.0.1", "::1", "172.29.0.1"]
    for peer, headers in (
        ("172.29.0.7", []),  # another container
        ("192.168.1.20", []),  # another machine, through a published port
        ("testclient", []),
        ("127.0.0.1", [(b"x-forwarded-for", b"203.0.113.9")]),  # through a proxy
        ("172.29.0.1", [(b"Forwarded", b"for=203.0.113.9")]),
    ):
        sent = _through(guard, "http", peer, headers)
        assert sent[0]["status"] == 403 and b"auth_failed" in sent[1]["body"], (peer, headers)
    closed = _through(guard, "websocket", "192.168.1.20", [])
    assert closed == [{"type": "websocket.close", "code": 1008, "reason": "loopback only"}]
    assert len(passed) == 3


# --- the first hour, headless, against a real server in insecure loopback mode -----------------------------
@contextlib.contextmanager
def serving_loopback(settings: Any, fake_models: Any, clock: Any, port: int) -> Iterator[dict[str, Any]]:
    """The real app under uvicorn on 127.0.0.1, plain http; its runtime and loop, to raise approvals."""
    import uvicorn

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    held: dict[str, Any] = {}

    async def factory(s: Any) -> Any:
        held["loop"] = asyncio.get_running_loop()
        held["runtime"] = await Runtime.start(s, model_factory=fake_models, clock=clock)
        return held["runtime"]

    with uvicorn_logging_kept():
        config = uvicorn.Config(create_app(settings, runtime_factory=factory), host="127.0.0.1", port=port,
                                log_config=None, log_level=None, access_log=False)  # fmt: skip
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        try:
            yield held
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            assert not thread.is_alive(), "the test server did not stop"


def test_the_first_hour_headless_in_insecure_loopback_mode(
    app_settings: Any, db: psycopg.Connection, fake_models: Any, clock: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    settings = app_settings.model_copy(update={"insecure_loopback_pairing": True, "addresses": url})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    path = str(tmp_path / "client.json")
    with serving_loopback(settings, fake_models, clock, port) as held:
        # the server shows the code (and says loudly what mode it is in); the client types it
        assert cli.main(["pair", "CLI", "--no-wait", "--json"]) == 0
        shown = capsys.readouterr()
        assert "PISA_INSECURE_LOOPBACK_PAIRING" in shown.err
        typed = json.loads(shown.out.strip().splitlines()[-1])["token"]
        result: list[int] = []
        thread = threading.Thread(
            target=lambda: result.append(
                cli.main(["client", "pair", typed, "--server", url, "--file", path])
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
        device_id = json.loads(Path(path).read_text())["deviceId"]
        capsys.readouterr()

        # erase everything, signed with the client's own key (no iPhone, no Face ID key, no --signer)
        assert cli.main(["client", "erase", "all", "--before", "2026-10-20", "--file", path]) == 0
        assert db.execute("SELECT count(DISTINCT family) FROM erase_jobs").fetchone() == (len(FAMILIES),)

        # an approval that needs a signature: approved with the device key; another rejected
        done: list[dict[str, Any]] = []

        async def execute(payload: dict[str, Any]) -> Outcome:
            done.append(payload)
            return Outcome("Added Lunch.")

        async def verified(payload: dict[str, Any], outcome: Outcome) -> bool:
            return True

        runtime = held["runtime"]
        runtime.approvals.register("calendar_create", execute, verify=verified)

        def raise_one(title: str) -> str:
            made = runtime.approvals.create(
                "calendar_create", "Add «Lunch»?", ["Wednesday 12:30"], PAYLOAD | {"title": title},
                requires_biometric=True, device_id=uuid.UUID(device_id), lang="en-US",
            )  # fmt: skip
            return str(asyncio.run_coroutine_threadsafe(made, held["loop"]).result(30)["id"])

        first, second = raise_one("Lunch"), raise_one("Dinner")  # two of a kind: different acts
        capsys.readouterr()
        assert cli.main(["client", "approve", first, "--file", path]) == 0, capsys.readouterr()
        assert "pisa.action_done" in capsys.readouterr().out and len(done) == 1
        assert cli.main(["client", "reject", second, "--file", path]) == 0, capsys.readouterr()
        statuses = dict(db.execute("SELECT id::text, status FROM approvals").fetchall())
        assert statuses[first] == "done" and statuses[second] == "rejected" and len(done) == 1
        assert date(2026, 10, 20)  # the clock's day: `--before` is never after their today
