# SPDX-License-Identifier: AGPL-3.0-only
"""`pisa init`: deploy/.env at mode 0600 with generated secrets, never a secret on stdout or stderr, a re-run
that changes nothing unless --force (and keeps the secrets even then), and each reach mode's keys."""

import argparse
import io
import os
import socket
import stat
from pathlib import Path

import pytest

from pisa.cli import main
from pisa.cli_init import MODE_KEYS, SECRETS, add_init_parser, cmd_init, read_env
from tests.support.fake_secrets import tailscale_key

SYNTHETIC_TS_KEY = tailscale_key("auth", "synthetic0000", "-", "0" * 16)
EMAIL = "ops@example.com"


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_init_parser(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["init", *argv])


def run(env: Path, *argv: str, environ: dict[str, str] | None = None) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cmd_init(parse("--env-file", str(env), *argv), environ=environ or {}, out=out, err=err, tty=False)
    return code, out.getvalue() + err.getvalue()


def assert_no_secret(values: dict[str, str], printed: str) -> None:
    for key in (*SECRETS, "TS_AUTHKEY"):
        if values.get(key):
            assert values[key] not in printed, key


@pytest.mark.parametrize("mode", ["tailscale", "caddy", "local"])
def test_each_mode_writes_its_keys_privately(tmp_path: Path, mode: str) -> None:
    env = tmp_path / "deploy" / ".env"
    code, printed = run(
        env, "--yes", "--mode", mode, "--timezone", "Europe/London", "--domain", "pisa.example.com",
        "--email", "ops@example.com", environ={"TS_AUTHKEY": SYNTHETIC_TS_KEY},
    )  # fmt: skip
    assert code == 0, printed
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    values = read_env(env)
    assert values["COMPOSE_PROFILES"] == mode
    assert values["PISA_TIMEZONE"] == "Europe/London"
    for key in (*SECRETS, *MODE_KEYS[mode]):
        assert values[key], key
    assert len(set(values[k] for k in SECRETS)) == len(SECRETS)  # each one its own
    from cryptography.fernet import Fernet

    Fernet(values["PISA_SECRETS_KEY"].encode())
    assert_no_secret(values, printed)
    assert "up -d" in printed and "exec worker pisa setup" in printed and "exec worker pisa pair" in printed
    assert "TODO" not in printed


def test_a_rerun_refuses_then_force_keeps_the_secrets(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert run(env, "--yes", "--mode", "local", "--timezone", "UTC")[0] == 0
    first = env.read_text()
    code, printed = run(env, "--yes", "--mode", "caddy", "--domain", "pisa.example.com", "--email", EMAIL)
    assert code == 2 and "--force" in printed
    assert env.read_text() == first  # untouched
    code, printed = run(env, "--yes", "--force")
    assert code == 0 and env.read_text() == first  # same answers, same file
    before = read_env(env)
    assert (
        run(env, "--yes", "--force", "--mode", "caddy", "--domain", "pisa.example.com", "--email", EMAIL)[0]
        == 0
    )
    after = read_env(env)
    assert after["PISA_ADDRESSES"] == "https://pisa.example.com"  # the local mode's address went with it
    assert all(after[k] == before[k] for k in SECRETS)
    assert after["COMPOSE_PROFILES"] == "caddy"
    code, printed = run(env, "--yes", "--force", "--rotate-secrets")
    rotated = read_env(env)
    assert code == 0 and all(rotated[k] != after[k] for k in SECRETS)
    assert_no_secret(rotated, printed)


def test_an_empty_placeholder_env_is_filled_and_other_settings_kept(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("POSTGRES_PASSWORD=\nPISA_COUNTRY=GB\n")
    env.chmod(0o644)
    assert run(env, "--yes", "--timezone", "UTC", "--image", "registry.example/pisa:1.0.0")[0] == 0
    values = read_env(env)
    assert values["POSTGRES_PASSWORD"] and values["PISA_COUNTRY"] == "GB"
    assert values["PISA_IMAGE"] == "registry.example/pisa:1.0.0"
    assert values["COMPOSE_PROFILES"] == "tailscale" and values["TS_AUTHKEY"] == ""
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_interactive_answers_and_a_hidden_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    answers = iter(["tailscale", "Europe/Rome", "it", "", "home-server", "tail1234.ts.net"])
    asked: list[str] = []
    out, err = io.StringIO(), io.StringIO()

    def secret(question: str) -> str:
        asked.append(question)
        return SYNTHETIC_TS_KEY

    code = cmd_init(
        parse("--env-file", str(env)), ask=lambda _q: next(answers), secret=secret, environ={}, out=out,
        err=err, tty=True,
    )  # fmt: skip
    assert code == 0, err.getvalue()
    values = read_env(env)
    assert values["TS_AUTHKEY"] == SYNTHETIC_TS_KEY and values["TS_HOSTNAME"] == "home-server"
    assert values["PISA_LANGUAGE"] == "it" and values["PISA_COUNTRY"] == "IT" and asked  # the zone's
    assert values["PISA_ADDRESSES"] == "https://home-server.tail1234.ts.net"
    assert_no_secret(values, out.getvalue() + err.getvalue())


@pytest.mark.parametrize(
    ("argv", "word"),
    [
        (("--mode", "local"), "--timezone"),
        (("--timezone", "Not/AZone"), "time zone"),
        (("--timezone", "UTC", "--mode", "caddy"), "--domain"),
        (("--timezone", "UTC", "--mode", "caddy", "--domain", "pisa.example.com"), "--email"),
        (
            (
                "--timezone",
                "UTC",
                "--mode",
                "caddy",
                "--domain",
                "pisa example.com",
                "--email",
                "a@example.com",
            ),
            "not a domain",
        ),
        (
            ("--timezone", "UTC", "--mode", "caddy", "--domain", "localhost", "--email", "a@example.com"),
            "not a domain",
        ),
        (
            (
                "--timezone",
                "UTC",
                "--mode",
                "caddy",
                "--domain",
                "pisa.example.com",
                "--email",
                "a b@example.com",
            ),
            "address",
        ),
        (
            ("--timezone", "UTC", "--mode", "caddy", "--domain", "pisa.example.com", "--email", "a@{x}.com"),
            "address",
        ),
    ],
)
def test_refusals_write_nothing(tmp_path: Path, argv: tuple[str, ...], word: str) -> None:
    env = tmp_path / ".env"
    code, printed = run(env, "--yes", *argv)
    assert code == 2 and word in printed and not env.exists()


def test_through_the_cli_with_no_server_settings(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("PISA_TIMEZONE", raising=False)  # init needs no server settings
    monkeypatch.setenv("TS_AUTHKEY", SYNTHETIC_TS_KEY)
    env = tmp_path / ".env"
    assert main(["init", "--env-file", str(env), "--yes", "--timezone", "UTC", "--show-dir", "deploy"]) == 0
    printed = "".join(capsys.readouterr())
    assert "docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d" in printed
    assert_no_secret(read_env(env), printed)


def test_local_mode_writes_where_the_api_is_published(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    code, printed = run(env, "--yes", "--mode", "local", "--timezone", "UTC")
    values = read_env(env)
    assert code == 0 and values["PISA_LAN_BIND"] == "127.0.0.1" and values["PISA_LAN_PORT"] == "8443"
    assert "PISA_LAN_BIND=0.0.0.0" in printed  # how to open it to the network
    every = "0.0.0.0"  # noqa: S104 -- the setting under test, not a bind
    env.write_text(env.read_text().replace("PISA_LAN_BIND=127.0.0.1", f"PISA_LAN_BIND={every}"))
    code, printed = run(env, "--yes", "--force")
    assert code == 0 and read_env(env)["PISA_LAN_BIND"] == every and "PISA_LAN_BIND=" not in printed


def test_tailscale_mode_addresses_from_the_tailnet_name(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    base = ("--yes", "--mode", "tailscale", "--timezone", "UTC")
    code, printed = run(env, *base)
    assert code == 0 and "PISA_ADDRESSES" not in read_env(env) and "PISA_ADDRESSES=https://pisa." in printed
    assert run(env, *base, "--force", "--tailnet", "Tail1234.ts.net", "--hostname", "home")[0] == 0
    assert read_env(env)["PISA_ADDRESSES"] == "https://home.tail1234.ts.net"
    assert run(env, *base, "--force")[0] == 0  # the saved tailnet is kept
    assert read_env(env)["PISA_ADDRESSES"] == "https://home.tail1234.ts.net"
    for bad in (("--tailnet", "example.com"), ("--hostname", "no_way")):
        code, printed = run(env, *base, "--force", *bad)
        assert code == 2, bad


def test_caddy_follows_a_new_domain(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    base = ("--yes", "--mode", "caddy", "--timezone", "UTC", "--email", EMAIL)
    assert run(env, *base, "--domain", "pisa.example.com")[0] == 0
    assert run(env, *base, "--force", "--domain", "Home.Example.org")[0] == 0
    values = read_env(env)
    assert (
        values["PISA_DOMAIN"] == "home.example.org" and values["PISA_ADDRESSES"] == "https://home.example.org"
    )


# --- local mode's TLS -------------------------------------------------------------------------------------


def local(env: Path, *argv: str) -> tuple[int, str]:
    hosts = ("--lan-host", "pisa.local,192.168.1.20")
    return run(env, "--yes", "--mode", "local", "--timezone", "UTC", *hosts, *argv)


def test_local_mode_makes_a_private_key_and_pins_its_certificate(tmp_path: Path) -> None:
    from cryptography import x509

    from pisa.domain import pairing as rules

    env = tmp_path / "deploy" / ".env"
    code, printed = local(env)
    assert code == 0, printed
    tls = env.parent / "tls"
    key, cert = tls / "key.pem", tls / "cert.pem"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert.stat().st_mode) == 0o644 and stat.S_IMODE(tls.stat().st_mode) == 0o755
    values = read_env(env)
    assert values["PISA_TLS_CERT"] == "/etc/pisa/tls/cert.pem"
    assert values["PISA_ADDRESSES"] == "https://pisa.local:8443,https://192.168.1.20:8443"
    pin = rules.spki_pin(cert.read_text())
    assert pin in printed and "PRIVATE KEY" not in printed and key.read_text() not in printed
    # who api-lan runs as: the key's owner (run as root, init gives the pair to the image's user)
    if os.geteuid() == 0:
        assert key.stat().st_uid == 10001 and "PISA_LAN_UID" not in values
    else:
        assert values["PISA_LAN_UID"] == str(os.getuid()) and values["PISA_LAN_GID"] == str(os.getgid())
    san = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    assert "pisa.local" in san.value.get_values_for_type(x509.DNSName)
    # the endpoints the QR is made from accept these addresses
    assert len(rules.endpoints(values["PISA_ADDRESSES"].split(","), [pin])["endpoints"]) == 2


def test_local_tls_is_never_remade_unless_rotated(tmp_path: Path) -> None:
    from pisa.domain import pairing as rules

    env = tmp_path / ".env"
    assert local(env)[0] == 0
    tls = env.parent / "tls"
    key, cert = (tls / "key.pem").read_bytes(), (tls / "cert.pem").read_bytes()
    code, printed = local(env, "--force", "--lan-host", "other.local")
    assert code == 0 and (tls / "key.pem").read_bytes() == key and (tls / "cert.pem").read_bytes() == cert
    assert "does not name other.local" in printed and "Kept the TLS key" in printed
    (tls / "cert.pem").unlink()  # a lost certificate is made again for the same key: the pin holds
    assert local(env, "--force")[0] == 0 and (tls / "key.pem").read_bytes() == key
    assert rules.spki_pin((tls / "cert.pem").read_text()) == rules.spki_pin(cert.decode())
    code, printed = local(env, "--rotate-tls")  # no --force needed
    assert code == 0 and (tls / "key.pem").read_bytes() != key and "pair again" in printed
    assert stat.S_IMODE((tls / "key.pem").stat().st_mode) == 0o600


def test_leaving_local_mode_drops_its_certificate(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert local(env)[0] == 0
    caddy = ("--mode", "caddy", "--domain", "pisa.example.com", "--email", EMAIL)
    assert run(env, "--yes", "--force", *caddy)[0] == 0
    values = read_env(env)
    assert "PISA_TLS_CERT" not in values and values["PISA_ADDRESSES"] == "https://pisa.example.com"


@pytest.mark.parametrize("host", ["not a host", "pisa.tail1234.ts.net", "a,b,c,d,e"])
def test_bad_lan_hosts_are_refused(tmp_path: Path, host: str) -> None:
    env = tmp_path / ".env"
    code, _ = run(env, "--yes", "--mode", "local", "--timezone", "UTC", "--lan-host", host)
    assert code == 2 and not env.exists() and not (tmp_path / "tls").exists()


def test_the_default_lan_host_is_the_machines_name() -> None:
    from pisa.cli_init import default_lan_host

    assert default_lan_host({"PISA_HOST_NAME": "Kitchen-Pi"}) == "kitchen-pi.local"
    assert default_lan_host({"PISA_HOST_NAME": "studio.local"}) == "studio.local"
    assert default_lan_host({"PISA_HOST_NAME": "bad name"}) == "pisa.local"


def test_serve_api_serves_tls_with_the_pair_init_made(tmp_path: Path) -> None:
    """A real TLS handshake: uvicorn with the API's options from PISA_API_TLS_*, presenting the certificate
    whose pin the QR carries, and a client that trusts exactly that certificate gets in."""
    import ssl
    import threading
    import time
    import urllib.request

    import uvicorn

    from pisa.cli import api_tls
    from pisa.domain import pairing as rules
    from pisa.domain.errors import PisaError
    from pisa.settings import Settings

    env = tmp_path / ".env"
    assert local(env)[0] == 0
    cert, key = tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"
    base = Settings(timezone="UTC")  # type: ignore[call-arg]
    options = api_tls(base.model_copy(update={"api_tls_cert": str(cert), "api_tls_key": str(key)}))
    assert options == (str(cert), str(key))
    assert api_tls(base) is None
    for bad in (
        {"api_tls_cert": str(cert)},
        {"api_tls_cert": str(cert), "api_tls_key": str(tmp_path / "no")},
    ):
        with pytest.raises(PisaError):
            api_tls(base.model_copy(update=bad))

    async def app(scope: dict, receive: object, send: object) -> None:  # type: ignore[type-arg]
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
            await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, lifespan="off", ssl_certfile=cert, ssl_keyfile=key
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        presented = ssl.get_server_certificate(("127.0.0.1", port), timeout=5)
        assert rules.spki_pin_der(ssl.PEM_cert_to_DER_cert(presented)) == rules.spki_pin(cert.read_text())
        context = ssl.create_default_context(cadata=presented)
        context.check_hostname = False
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/", context=context, timeout=5) as r:
            assert r.read() == b"ok"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_api_lan_runs_as_the_keys_owner(tmp_path: Path) -> None:
    """The install script runs init as the host user: the key is theirs (0600), so api-lan runs as them."""
    from pisa.cli_init import _lan_user

    key = tmp_path / "key.pem"
    key.write_text("synthetic")
    (tmp_path / "cert.pem").write_text("synthetic")
    values: dict[str, str] = {"PISA_LAN_UID": "1", "PISA_LAN_GID": "1"}
    st = key.stat()
    if st.st_uid not in (0, 10001):  # a non-root run: the file is this user's
        _lan_user(key, values)
        assert values["PISA_LAN_UID"] == str(st.st_uid) and values["PISA_LAN_GID"] == str(st.st_gid)
        return
    os.chown(key, 1234, 2345)  # as root: a host user's key
    _lan_user(key, values)
    assert values["PISA_LAN_UID"] == "1234" and values["PISA_LAN_GID"] == "2345"
    os.chown(key, 10001, 10001)  # the image's user: the compose default
    _lan_user(key, values)
    assert "PISA_LAN_UID" not in values and "PISA_LAN_GID" not in values


def test_each_mode_sets_where_its_pin_comes_from(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    base = ("--yes", "--force", "--timezone", "UTC")
    assert run(env, *base, "--mode", "tailscale", "--tailnet", "tail1234.ts.net")[0] == 0
    values = read_env(env)
    assert values["PISA_TLS_CERT"] == "/etc/pisa/ts-cert/cert.pem" and "PISA_TLS_PIN_FROM" not in values
    assert run(env, *base, "--mode", "caddy", "--domain", "pisa.example.com", "--email", EMAIL)[0] == 0
    values = read_env(env)
    assert values["PISA_TLS_PIN_FROM"] == "caddy:443" and "PISA_TLS_CERT" not in values
    assert run(env, *base, "--mode", "local", "--lan-host", "pisa.local")[0] == 0
    values = read_env(env)
    assert values["PISA_TLS_CERT"] == "/etc/pisa/tls/cert.pem" and "PISA_TLS_PIN_FROM" not in values
    # a certificate of your own is kept in every mode, and wins over the observed pin
    env.write_text(env.read_text().replace("/etc/pisa/tls/cert.pem", "/etc/pisa/tls/mine.pem"))
    assert run(env, *base, "--mode", "caddy", "--domain", "pisa.example.com", "--email", EMAIL)[0] == 0
    values = read_env(env)
    assert values["PISA_TLS_CERT"] == "/etc/pisa/tls/mine.pem" and "PISA_TLS_PIN_FROM" not in values


@pytest.mark.parametrize(
    "weak",
    ["changeme", "init", "password", "postgres", "<a password>", "CHANGEME", "short-but-mine", "a" * 20,
     "passwordpassword"],
)  # fmt: skip
def test_force_refuses_a_weak_password_kept_from_the_file(tmp_path: Path, weak: str) -> None:
    """A hand-filled weak password is not kept on --force: init stops, says to rotate it, and
    never prints it."""

    def refused(env: Path, value: str) -> str:
        assert run(env, "--yes", "--mode", "caddy", "--timezone", "UTC", "--domain", "pisa.example.com",
                   "--email", EMAIL)[0] == 0  # fmt: skip
        env.write_text(env.read_text().replace(read_env(env)["PISA_APP_PASSWORD"], value))
        before = env.read_text()
        code, printed = run(env, "--yes", "--force")
        assert code == 2, printed
        assert env.read_text() == before  # nothing changed
        return printed

    env = tmp_path / ".env"
    printed = refused(env, weak)
    assert "PISA_APP_PASSWORD" in printed and "too weak" in printed and "--rotate-secrets" in printed
    assert "POSTGRES_PASSWORD" not in printed  # only the weak one is named
    other = tmp_path / "other" / ".env"
    assert printed.replace(str(env), "") == refused(other, "x").replace(str(other), "")  # no value
    code, printed = run(env, "--yes", "--force", "--rotate-secrets")  # a database not created yet: rotated
    assert code == 0, printed
    assert read_env(env)["PISA_APP_PASSWORD"] != weak


def test_a_strong_hand_typed_password_is_kept(tmp_path: Path) -> None:
    from pisa.cli_init import MIN_PASSWORD, weak_password

    env = tmp_path / ".env"
    strong = "correct-horse-battery-staple-42"
    env.write_text(f"POSTGRES_PASSWORD={strong}\n")
    code, printed = run(env, "--yes", "--force", "--timezone", "UTC")
    assert code == 0, printed
    assert read_env(env)["POSTGRES_PASSWORD"] == strong
    assert MIN_PASSWORD == 16 and weak_password("x" * 15) and not weak_password("0123456789abcdef")


@pytest.mark.parametrize(
    ("euid", "owners", "chowned", "lan_uid"),
    [
        (10001, {"key.pem": 10001, "cert.pem": 10001}, [], None),  # install.sh as root: init runs as 10001
        (0, {"key.pem": 0, "cert.pem": 0}, ["key.pem", "cert.pem"], None),  # root inside the container
        (0, {"key.pem": 0, "cert.pem": 10001}, ["key.pem"], None),  # already the image's user's: skipped
        (1000, {"key.pem": 1000, "cert.pem": 1000}, [], "1000"),  # a host user
    ],
)  # fmt: skip
def test_who_api_lan_runs_as_with_the_os_calls_stubbed(
    tmp_path: Path, monkeypatch, euid: int, owners: dict[str, int], chowned: list[str], lan_uid: str | None
) -> None:  # type: ignore[no-untyped-def]
    from pisa import cli_init

    calls: list[str] = []
    monkeypatch.setattr(cli_init.os, "geteuid", lambda: euid, raising=False)
    monkeypatch.setattr(cli_init.os, "chown", lambda path, uid, gid: calls.append(Path(path).name))
    monkeypatch.setattr(cli_init, "_owner", lambda path: (owners[Path(path).name], owners[Path(path).name]))
    values: dict[str, str] = {}
    cli_init._lan_user(tmp_path / "key.pem", values)
    assert calls == chowned
    assert values.get("PISA_LAN_UID") == lan_uid


def test_a_chown_the_container_may_not_do_says_how_to_run_init(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The bug the root path fixes: as root under cap_drop ALL, os.chown raises PermissionError. Init now
    stops with what to do (give the folder to uid 10001 and run as it) instead of a traceback."""
    from pisa import cli_init

    def refuse(path: object, uid: int, gid: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(cli_init.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(cli_init.os, "chown", refuse)
    monkeypatch.setattr(cli_init, "_owner", lambda path: (0, 0))
    with pytest.raises(cli_init.InitError, match=r"--user 10001:10001"):
        cli_init._lan_user(tmp_path / "key.pem", {})


# --- every question asks again on a bad answer ------------------------------------------------------------


def interactive(
    env: Path,
    answers: list[str],
    *argv: str,
    environ: dict[str, str] | None = None,
    keys: tuple[str, ...] = (),
    country: bool = False,
) -> tuple[int, str, list[str]]:
    """init on a terminal: `answers` typed in turn, `keys` pasted at the hidden question. The country is
    answered with --country unless `country` (its own tests ask it)."""
    if "--country" not in argv and not country:
        argv = (*argv, "--country", "-")
    typed, pasted, asked = iter(answers), iter(keys), []
    out, err = io.StringIO(), io.StringIO()

    def ask(question: str) -> str:
        asked.append(question)
        return next(typed)

    def secret(question: str) -> str:
        asked.append(question)
        return next(pasted, "")

    code = cmd_init(
        parse("--env-file", str(env), *argv), ask=ask, secret=secret, environ=environ or {}, out=out, err=err,
        tty=True,
    )  # fmt: skip
    return code, out.getvalue() + err.getvalue(), asked


TS = ("--timezone", "UTC", "--language", "en")
# (the flags that answer every other question, the bad answer, the good one, the key it sets, its value, the
# flag that passes it)
QUESTIONS = [
    ((*TS, "--lan-host", "pisa.local"), "tailscle", "Local", "COMPOSE_PROFILES", "local", "--mode"),
    (("--mode", "local", "--language", "en", "--lan-host", "pisa.local"), "Not/AZone", "europe/rome",
     "PISA_TIMEZONE", "Europe/Rome", "--timezone"),
    (("--mode", "local", "--timezone", "UTC", "--lan-host", "pisa.local"), "fr", "IT", "PISA_LANGUAGE", "it",
     "--language"),
    (("--mode", "tailscale", *TS, "--tailnet", "tail1234.ts.net"), "no_way", "home", "TS_HOSTNAME", "home",
     "--hostname"),
    (("--mode", "tailscale", *TS, "--hostname", "pisa"), "example.com", "tail1234.ts.net", "PISA_ADDRESSES",
     "https://pisa.tail1234.ts.net", "--tailnet"),
    (("--mode", "caddy", *TS, "--email", EMAIL), "pisa example.com", "pisa.example.com", "PISA_DOMAIN",
     "pisa.example.com", "--domain"),
    (("--mode", "caddy", *TS, "--domain", "pisa.example.com"), "a b@example.com", EMAIL, "PISA_ACME_EMAIL",
     EMAIL, "--email"),
    (("--mode", "local", *TS), "not a host", "pisa.local", "PISA_ADDRESSES", "https://pisa.local:8443",
     "--lan-host"),
]  # fmt: skip
ARGS = ("argv", "bad", "good", "key", "value", "flag")


@pytest.mark.parametrize(ARGS, QUESTIONS)
def test_each_question_asks_again_on_a_bad_answer(
    tmp_path: Path, argv: tuple[str, ...], bad: str, good: str, key: str, value: str, flag: str
) -> None:
    env = tmp_path / ".env"
    code, printed, asked = interactive(env, [bad, good], *argv)
    assert code == 0, printed
    typed = [q for q in asked if "hidden" not in q]  # tailscale also asks for the key, hidden
    assert len(typed) == 2 and typed[0] == typed[1]  # the same question, again
    assert repr(bad) in printed and "Please try again (2 tries left)" in printed
    assert read_env(env)[key] == value
    assert "Traceback" not in printed


@pytest.mark.parametrize(ARGS, QUESTIONS)
def test_three_bad_answers_stop_with_how_to_pass_it(
    tmp_path: Path, argv: tuple[str, ...], bad: str, good: str, key: str, value: str, flag: str
) -> None:
    env = tmp_path / ".env"
    code, printed, _ = interactive(env, [bad, bad, bad, good], *argv)
    assert code == 2 and not env.exists() and not (tmp_path / "tls").exists()
    last = printed.strip().splitlines()[-1]
    assert last.startswith("error: ") and "No valid answer after 3 tries, so nothing was written" in last
    assert f"with {flag}, e.g. {flag} " in last
    assert printed.count("Please try again") == 2 and "Traceback" not in printed


def test_the_hidden_key_is_asked_again_and_never_shown(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    argv = ("--mode", "tailscale", *TS, "--hostname", "pisa", "--tailnet", "tail1234.ts.net")
    wrong = "ghp_" + "NotATailscaleKey000000000"
    code, printed, asked = interactive(env, [], *argv, keys=(wrong, SYNTHETIC_TS_KEY))
    assert code == 0, printed
    assert len(asked) == 2 and "starts with tskey-" in printed and wrong not in printed
    assert read_env(env)["TS_AUTHKEY"] == SYNTHETIC_TS_KEY
    assert_no_secret(read_env(env), printed)
    code, printed, _ = interactive(tmp_path / "b" / ".env", [], *argv, keys=(wrong,) * 3)
    assert code == 2 and "No valid key after 3 tries" in printed and "TS_AUTHKEY" in printed
    assert wrong not in printed and "Traceback" not in printed
    # with no terminal, a bad key from the environment stops at once, unnamed
    code, printed = run(tmp_path / "c" / ".env", "--yes", *argv, environ={"TS_AUTHKEY": wrong})
    assert code == 2 and "tskey-" in printed and wrong not in printed


def test_a_bad_flag_on_a_terminal_is_said_then_asked(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    code, printed, asked = interactive(
        env, ["Europe/Rome"], "--mode", "local", "--timezone", "Atlantis", "--language", "en", "--lan-host",
        "pisa.local",
    )  # fmt: skip
    assert code == 0 and "--timezone: 'Atlantis' is not a time zone" in printed and len(asked) == 1
    assert read_env(env)["PISA_TIMEZONE"] == "Europe/Rome"


@pytest.mark.parametrize(
    ("argv", "flag"),
    [(("--timezone", "Atlantis"), "--timezone"),
     (("--timezone", "UTC", "--hostname", "no_way"), "--hostname"),
     (("--timezone", "UTC", "--tailnet", "example.com"), "--tailnet"),
     (("--timezone", "UTC", "--image", "not an image"), "--image")],
)  # fmt: skip
def test_with_no_terminal_a_bad_flag_stops_at_once(tmp_path: Path, argv: tuple[str, ...], flag: str) -> None:
    env = tmp_path / ".env"
    code, printed = run(env, "--yes", *argv)
    assert code == 2 and flag in printed and not env.exists()
    assert printed.strip().splitlines()[-1].startswith("error: ") and "Traceback" not in printed


# --- the time zone: a full name in any case, or a city; the host's as the default ------------------------


@pytest.mark.parametrize(
    ("typed", "zone"),
    [("Rome", "Europe/Rome"), ("rome", "Europe/Rome"), ("New York", "America/New_York"),
     ("new_york", "America/New_York"), ("europe/rome", "Europe/Rome"),
     ("AMERICA/new_york", "America/New_York"),
     ("utc", "UTC")],
)  # fmt: skip
def test_a_city_or_a_name_in_any_case_is_the_zone(tmp_path: Path, typed: str, zone: str) -> None:
    env = tmp_path / ".env"
    code, printed, asked = interactive(
        env, [typed], "--mode", "local", "--language", "en", "--lan-host", "pisa.local"
    )
    assert code == 0, printed
    assert read_env(env)["PISA_TIMEZONE"] == zone and len(asked) == 1
    assert (f"Using {zone}." in printed) is ("/" not in typed and zone != "UTC")


def test_a_city_in_several_zones_lists_them_and_asks_again(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    code, printed, asked = interactive(
        env,
        ["Istanbul", "Europe/Istanbul"],
        "--mode",
        "local",
        "--language",
        "en",
        "--lan-host",
        "pisa.local",
    )
    assert code == 0 and len(asked) == 2
    assert "'Istanbul' is in more than one time zone: Asia/Istanbul, Europe/Istanbul" in printed
    assert read_env(env)["PISA_TIMEZONE"] == "Europe/Istanbul"


def test_an_unknown_city_says_so_and_shows_the_format() -> None:
    from pisa.cli_init import Invalid, resolve_zone

    with pytest.raises(Invalid, match=r"'Atlantis' is not a time zone or a city in one: .*Europe/Rome"):
        resolve_zone("Atlantis", print)
    said: list[str] = []
    assert resolve_zone("Rome", said.append) == "Europe/Rome" and said == ["Using Europe/Rome."]


def test_the_hosts_zone_is_the_default(tmp_path: Path) -> None:
    host = {"PISA_HOST_TZ": "Europe/Rome"}
    argv = ("--mode", "local", "--language", "en", "--lan-host", "pisa.local")
    code, printed, asked = interactive(tmp_path / "a" / ".env", [""], *argv, environ=host)
    assert code == 0 and asked[0].endswith("[Europe/Rome]: ")  # Return takes it
    assert read_env(tmp_path / "a" / ".env")["PISA_TIMEZONE"] == "Europe/Rome"
    assert run(tmp_path / "b" / ".env", "--yes", *argv, environ=host)[0] == 0  # --yes takes it too
    assert read_env(tmp_path / "b" / ".env")["PISA_TIMEZONE"] == "Europe/Rome"
    # not a valid IANA name (a city, or garbage): no default
    for i, bad in enumerate(("Rome", "Not/AZone", "Europe/Rome; x")):
        env = tmp_path / f"c{i}" / ".env"
        code, printed, asked = interactive(env, ["UTC"], *argv, environ={"PISA_HOST_TZ": bad})
        assert code == 0 and asked[0].endswith("Europe/Rome): "), asked
        code, printed = run(tmp_path / f"d{i}" / ".env", "--yes", *argv, environ={"PISA_HOST_TZ": bad})
        assert code == 2 and "--timezone" in printed
    # a zone saved in the .env wins over the host's
    env = tmp_path / "e" / ".env"
    assert run(env, "--yes", *argv, "--timezone", "Asia/Tokyo")[0] == 0
    assert run(env, "--yes", "--force", environ=host)[0] == 0
    assert read_env(env)["PISA_TIMEZONE"] == "Asia/Tokyo"


@pytest.mark.parametrize(("stop", "code"), [(EOFError, 2), (KeyboardInterrupt, 130)])
def test_end_of_input_or_ctrl_c_stops_with_no_traceback(
    tmp_path: Path, stop: type[BaseException], code: int
) -> None:
    out, err = io.StringIO(), io.StringIO()

    def ask(_question: str) -> str:
        raise stop

    env = tmp_path / ".env"
    assert cmd_init(parse("--env-file", str(env)), ask=ask, environ={}, out=out, err=err, tty=True) == code
    assert "error: " in err.getvalue() and "Traceback" not in err.getvalue() and not env.exists()


# --- the country ------------------------------------------------------------------------------------------

LOCAL = ("--mode", "local", "--language", "en", "--lan-host", "pisa.local")


@pytest.mark.parametrize(
    ("typed", "code"),
    [("IT", "IT"), ("it", "IT"), ("Italy", "IT"), ("Italia", "IT"), ("germania", "DE"), ("Germany", "DE"),
     ("United States", "US"), ("the US", "US"), ("fr", "FR"), ("-", None)],
)  # fmt: skip
def test_the_country_is_a_code_or_a_name_in_english_or_italian(
    tmp_path: Path, typed: str, code: str | None
) -> None:
    env = tmp_path / ".env"
    result, printed, asked = interactive(env, [typed], *LOCAL, "--timezone", "UTC", country=True)
    assert result == 0, printed
    assert asked[0].endswith("[-]: ")  # UTC is in no country: no default
    assert read_env(env).get("PISA_COUNTRY") == code


def test_the_country_defaults_from_an_unambiguous_zone(tmp_path: Path) -> None:
    from pisa.cli_init import zone_country

    assert zone_country("Europe/Rome") == "IT" and zone_country("America/New_York") == "US"
    assert zone_country("UTC") == "" and zone_country("Etc/GMT+1") == ""
    env = tmp_path / ".env"
    code, printed, asked = interactive(env, [""], *LOCAL, "--timezone", "Rome", country=True)
    assert code == 0 and asked[0].endswith("[IT]: ") and read_env(env)["PISA_COUNTRY"] == "IT"
    assert "Country: IT (Italy)." in printed
    assert run(tmp_path / "b" / ".env", "--yes", "--timezone", "America/Chicago")[0] == 0  # --yes takes it
    assert read_env(tmp_path / "b" / ".env")["PISA_COUNTRY"] == "US"
    # a country saved in the .env wins over the zone's, and `-` in it means none
    other = tmp_path / "c" / ".env"
    assert run(other, "--yes", "--timezone", "Europe/Rome", "--country", "Svizzera")[0] == 0
    assert run(other, "--yes", "--force")[0] == 0 and read_env(other)["PISA_COUNTRY"] == "CH"


def test_a_bad_country_is_asked_again_then_stops_with_the_flag(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    code, printed, asked = interactive(env, ["Narnia", "Italia"], *LOCAL, "--timezone", "UTC", country=True)
    assert code == 0 and len(asked) == 2 and "'Narnia' is not a country" in printed
    assert read_env(env)["PISA_COUNTRY"] == "IT"
    bad = ["XX", "Narnia", "zz"]
    code, printed, _ = interactive(tmp_path / "b" / ".env", bad, *LOCAL, "--timezone", "UTC", country=True)
    assert code == 2 and "with --country, e.g. --country IT" in printed and "Traceback" not in printed
    code, printed = run(tmp_path / "c" / ".env", "--yes", "--timezone", "UTC", "--country", "Narnia")
    assert code == 2 and "--country" in printed and not (tmp_path / "c" / ".env").exists()


# --- what init prints names the host's folder, never the install container's ------------------------------


def test_no_output_names_the_containers_folder(tmp_path: Path) -> None:
    env = tmp_path / "setup" / ".env"  # as in the install container: /setup/.env
    host = "/Users/someone/pisa/deploy"
    show = ("--show-dir", host)
    for argv in (
        ("--yes", "--mode", "local", "--timezone", "UTC", "--lan-host", "pisa.local", *show),
        ("--yes", "--mode", "local", *show),  # refused: already configured
        ("--yes", "--force", "--rotate-tls", *show),
        ("--yes", "--force", "--mode", "tailscale", "--started", *show),
        ("--yes", "--force", "--mode", "caddy", "--domain", "pisa.example.com", "--email", EMAIL, *show),
    ):
        code, printed = run(env, *argv)
        assert "/setup/" not in printed and str(tmp_path) not in printed, printed
        assert host in printed
    code, printed = run(env, "--yes", "--force", *show)
    assert code == 0
    assert f"Wrote {host}/.env (readable by you only)." in printed


def test_started_leaves_up_and_the_later_steps_to_the_installer(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    code, printed = run(env, "--yes", "--mode", "tailscale", "--timezone", "UTC", "--started")
    assert code == 0 and "Next:" not in printed and "up -d\n" not in printed
    assert "exec worker pisa setup" not in printed and "exec worker pisa pair" not in printed
    assert "TS_AUTHKEY=<your auth key>" in printed  # what still needs the user's hand
    code, printed = run(tmp_path / "b" / ".env", "--yes", "--mode", "tailscale", "--timezone", "UTC")
    assert "Next:" in printed and "up -d" in printed and "exec worker pisa setup" in printed


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (
            'connection failed: FATAL:  password authentication failed for user "pisa_migrate"',
            "refused the login",
        ),
        (
            'connection failed: connection to server at "postgres" (172.29.0.3), port 5432 failed: '
            "Connection refused",
            "no answer from it on the network",
        ),
        (
            'connection failed: could not translate host name "postgres" to address',
            "no answer from it on the network",
        ),
        ('connection failed: FATAL:  database "pisa" does not exist', "does not exist"),
        ("something else", "unreachable (OperationalError)"),
    ],
)
def test_a_failed_database_connection_is_said_by_its_kind(text: str, said: str) -> None:
    import psycopg

    from pisa.cli import _unreachable

    assert said in _unreachable(psycopg.OperationalError(text)).message
