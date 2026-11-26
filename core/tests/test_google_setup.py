"""The Google setup guide: each user makes their own Google project and OAuth client (Personal mode first),
with the redirect URI on their own server, in English or Italian; `pisa google setup` prints it."""

import argparse
import io
from types import SimpleNamespace
from typing import cast

import pytest

from pisa.cli_google import cmd_google_setup
from pisa.connectors.google.oauth import SCOPES
from pisa.domain import google_setup
from pisa.domain.google_setup import guide, redirect_uri
from pisa.settings import Settings

REDIRECT = "https://pisa.example-tailnet.ts.net/oauth/google/callback"
IDS = ["client", "gmail_api", "calendar_api", "scopes", "paste", "connect", "maps_key"]


@pytest.mark.parametrize("lang", ["en", "it", "it-IT", "en-US", "fr"])
def test_the_guide_has_its_steps_in_order(lang: str) -> None:
    doc = guide(REDIRECT, SCOPES, lang)
    assert [s["id"] for s in doc["steps"]] == IDS
    assert doc["redirect_uri"] == REDIRECT and doc["scopes"] == list(SCOPES)
    for step in doc["steps"]:
        assert set(step) == {"id", "title", "body", "url", "copy"}
        assert step["title"] and step["body"]
    assert len(doc["legal"]) == 4
    assert google_setup.USER_DATA_POLICY_URL in doc["legal"][0]


def test_english_and_italian_differ_and_other_tags_are_english() -> None:
    en, it = guide(REDIRECT, SCOPES, "en"), guide(REDIRECT, SCOPES, "it-IT")
    assert en["steps"][0]["title"] == "Create your OAuth client in Personal mode"
    assert it["steps"][0]["title"] == "Crea il tuo client OAuth in modalità personale"
    assert guide(REDIRECT, SCOPES, "it") == it
    assert guide(REDIRECT, SCOPES, "de") == en
    assert "PISA" in en["legal"][3] and "PISA" in it["legal"][3]
    assert "Web application" in en["steps"][0]["body"] and "Applicazione web" in it["steps"][0]["body"]
    assert "7" in en["steps"][-2]["body"] and "7" in it["steps"][-2]["body"]  # connect


def test_the_client_step_opens_personal_mode_and_copies_the_redirect_uri() -> None:
    client = guide(REDIRECT, SCOPES, "en")["steps"][0]
    assert client["url"] == "https://console.cloud.google.com/auth/overview/personal"
    assert client["copy"] == REDIRECT


def test_without_a_redirect_uri_https_comes_first_and_the_client_waits() -> None:
    for lang in ("en", "it"):
        doc = guide(None, SCOPES, lang)
        assert [s["id"] for s in doc["steps"]] == ["https", *IDS]
        assert "PISA_ADDRESSES" in doc["steps"][0]["body"]
        client = doc["steps"][1]
        assert client["copy"] is None
        assert client["body"].startswith(google_setup.locale.pick(google_setup._TEXT, lang)["client.blocked"])
    assert "can't be finished yet" in guide(None, SCOPES, "en")["steps"][1]["body"]
    assert "client.blocked" not in guide(REDIRECT, SCOPES, "en")["steps"][0]["body"]
    assert "can't be finished" not in guide(REDIRECT, SCOPES, "en")["steps"][0]["body"]


def test_the_project_goes_into_the_api_and_scope_links() -> None:
    steps = {s["id"]: s for s in guide(REDIRECT, SCOPES, "en", project="my-pisa-123")["steps"]}
    base = "https://console.cloud.google.com/apis/library/"
    assert steps["gmail_api"]["url"] == base + "gmail.googleapis.com?project=my-pisa-123"
    assert steps["calendar_api"]["url"] == base + "calendar-json.googleapis.com?project=my-pisa-123"
    assert steps["scopes"]["url"] == "https://console.cloud.google.com/auth/scopes?project=my-pisa-123"
    plain = {s["id"]: s for s in guide(REDIRECT, SCOPES, "en")["steps"]}
    assert plain["gmail_api"]["url"] == base + "gmail.googleapis.com"
    assert plain["calendar_api"]["url"] == base + "calendar-json.googleapis.com"
    assert plain["scopes"]["url"] == "https://console.cloud.google.com/auth/scopes"


def test_the_scopes_are_copied_exactly() -> None:
    scopes = ("https://www.googleapis.com/auth/a", "https://www.googleapis.com/auth/b.readonly")
    doc = guide(REDIRECT, scopes, "it")
    step = next(s for s in doc["steps"] if s["id"] == "scopes")
    assert step["copy"] == "https://www.googleapis.com/auth/a\nhttps://www.googleapis.com/auth/b.readonly"
    assert doc["scopes"] == list(scopes)
    real = next(s for s in guide(REDIRECT, SCOPES, "en")["steps"] if s["id"] == "scopes")
    assert real["copy"].split("\n") == list(SCOPES)


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        ([], None),
        (["http://127.0.0.1:8000"], None),
        (["http://pisa.example.org"], None),
        (["https://localhost:8443", "https://127.0.0.1", "https://[::1]"], None),
        (["https://192.168.1.20:8443"], None),  # Google refuses raw IP addresses as redirect hosts
        (["relay://relay.example.org/abc"], None),
        (["https://pisa.example.org"], "https://pisa.example.org/oauth/google/callback"),
        (["https://pisa.example.org:443/"], "https://pisa.example.org/oauth/google/callback"),
        (
            [
                "http://127.0.0.1:8000",
                "https://10.0.0.2",
                "https://box.tail0000.ts.net:8443/x?y",
                "https://b.example",
            ],
            "https://box.tail0000.ts.net:8443/oauth/google/callback",
        ),
        (["  HTTPS://Pisa.Example.org  "], "https://pisa.example.org/oauth/google/callback"),
        (["https://bad:port", "https://ok.example"], "https://ok.example/oauth/google/callback"),
    ],
)
def test_redirect_uri_is_the_first_https_address_with_a_name(
    addresses: list[str], expected: str | None
) -> None:
    assert redirect_uri(addresses) == expected


def _settings(addresses: list[str], language: str = "en") -> Settings:
    return cast(Settings, SimpleNamespace(address_list=addresses, language=language))


def test_the_cli_prints_the_guide() -> None:
    out = io.StringIO()
    code = cmd_google_setup(
        argparse.Namespace(lang=None),
        _settings(["https://pisa.example.org"]),
        out,
    )
    text = out.getvalue()
    assert code == 0
    assert "1. Create your OAuth client in Personal mode" in text
    assert "6. Connect Google" in text
    assert "Open: https://console.cloud.google.com/auth/overview/personal" in text
    assert "     https://pisa.example.org/oauth/google/callback" in text
    for scope in SCOPES:
        assert f"     {scope}\n" in text
    assert text.index("6. Connect Google") < text.index("Good to know:")
    assert google_setup.USER_DATA_POLICY_URL in text


def test_the_cli_in_italian_without_an_https_address() -> None:
    out = io.StringIO()
    cmd_google_setup(argparse.Namespace(lang="it"), _settings(["http://127.0.0.1:8000"]), out)
    text = out.getvalue()
    assert "1. Dai al tuo server un indirizzo https" in text
    assert "2. Crea il tuo client OAuth" in text
    assert "URI di reindirizzamento: non ancora" in text
    assert "Da sapere:" in text


def test_the_cli_takes_the_servers_language_by_default() -> None:
    out = io.StringIO()
    cmd_google_setup(argparse.Namespace(lang=None), _settings([], "it"), out)
    assert "Collega Google" in out.getvalue()


def test_pisa_google_setup_is_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pisa import cli
    from pisa.settings import get_settings

    monkeypatch.setenv("PISA_ADDRESSES", "https://pisa.example.org")
    get_settings.cache_clear()
    try:
        assert cli.main(["google", "setup", "--lang", "en"]) == 0
    finally:
        get_settings.cache_clear()
    assert "https://pisa.example.org/oauth/google/callback" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("lang", "window", "again", "details"),
    [
        ("en", "within 10 minutes", "tap Connect Google again", '"error details"'),
        ("it", "entro 10 minuti", "tocca di nuovo Collega Google", '"dettagli dell\'errore"'),
    ],
)
def test_the_connect_step_states_the_sign_in_window_and_the_redirect_mismatch(
    lang: str, window: str, again: str, details: str
) -> None:
    """The owner's setup, 24 Nov: a sign-in finished late was refused (`state_expired`, the 10-minute
    STATE_TTL), and `redirect_uri_mismatch` had no help. The guide (the app's and `pisa google setup`'s) says
    both where it explains the flow."""
    from pisa.services.google import STATE_TTL

    assert STATE_TTL.total_seconds() == 600
    connect = next(s for s in guide(REDIRECT, SCOPES, lang)["steps"] if s["id"] == "connect")["body"]
    assert window in connect and again in connect
    assert "redirect_uri_mismatch" in connect and details in connect and "pisa google setup" in connect
    assert "Web application" in connect and "5 " in connect


# --- v0.1.4: the Google Maps API key step, and `--gcloud` ---------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "it"])
def test_the_maps_key_step_links_the_apis_and_the_credentials_page_never_a_scope(lang: str) -> None:
    steps = {s["id"]: s for s in guide(REDIRECT, SCOPES, lang, project="my-pisa-123")["steps"]}
    maps = steps["maps_key"]
    base = "https://console.cloud.google.com/apis/library/"
    assert maps["url"] == "https://console.cloud.google.com/apis/credentials?project=my-pisa-123"
    assert maps["copy"] == "places.googleapis.com\nroutes.googleapis.com"
    assert base + "places.googleapis.com?project=my-pisa-123" in maps["body"]
    assert base + "routes.googleapis.com?project=my-pisa-123" in maps["body"]
    assert "https://console.cloud.google.com/billing" in maps["body"]
    # an API key, never Google Cloud access through the sign-in
    assert not any("cloud-platform" in scope for scope in SCOPES)
    assert "cloud-platform" not in maps["body"]
    assert ("not a permission" if lang == "en" else "non un permesso") in maps["body"]


FAKE_PROJECT = "pisa-example-123"


def _fake_gcloud(tmp_path, *, logged_in: bool = True, project: str = FAKE_PROJECT, fail: bool = False):  # type: ignore[no-untyped-def]
    """A fake `gcloud` on PATH: it records its arguments, never touches the network; its own output names a
    synthetic account, which the CLI must never print."""
    log = tmp_path / "calls.txt"
    script = tmp_path / "bin" / "gcloud"
    script.parent.mkdir()
    account = "someone@example.invalid" if logged_in else ""
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1 $2" in\n'
        f'  "auth list") echo "{account}" ;;\n'
        f'  "config get-value") echo "{project}" ;;\n'
        f'  "services enable") echo "Done for someone@example.invalid" >&2; exit {int(fail)} ;;\n'
        "esac\n"
    )
    script.chmod(0o755)
    return script.parent, log


def _gcloud_run(tmp_path, monkeypatch, *, answer: str = "", interactive: bool = True, **argv):  # type: ignore[no-untyped-def]
    import shutil

    fake = argv.pop("fake")
    monkeypatch.setenv("PATH", str(fake))  # the fake alone: never a real gcloud
    args = argparse.Namespace(
        **({"lang": "en", "gcloud": True, "maps": False, "project": None, "yes": False} | argv)
    )
    out = io.StringIO()
    asked: list[str] = []

    def ask(question: str) -> str:
        asked.append(question)
        return answer

    code = cmd_google_setup(
        args,
        _settings(["https://pisa.example.org"]),
        out,
        which=shutil.which,
        ask=ask,
        interactive=interactive,
    )
    return code, out.getvalue(), asked


def test_gcloud_shows_the_command_asks_and_enables_gmail_and_calendar(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake, log = _fake_gcloud(tmp_path)
    code, text, asked = _gcloud_run(tmp_path, monkeypatch, fake=fake, answer="y")
    assert code == 0 and asked == ["Run it? [y/N]: "]
    command = (
        f"gcloud services enable gmail.googleapis.com calendar-json.googleapis.com --project={FAKE_PROJECT}"
    )
    assert f"  {command}\n" in text
    assert text.index(command) < text.index("Enabled in")
    assert log.read_text().splitlines()[-1] == command.removeprefix("gcloud ")
    assert "example.invalid" not in text  # nothing gcloud says is printed (its account)
    assert f"apis/library/gmail.googleapis.com?project={FAKE_PROJECT}" in text  # the guide's links name it


def test_gcloud_with_maps_enables_places_and_routes_and_with_yes_asks_nothing(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake, log = _fake_gcloud(tmp_path)
    code, _, asked = _gcloud_run(
        tmp_path, monkeypatch, fake=fake, interactive=False, maps=True, yes=True, project="my-own-proj"
    )
    assert code == 0 and asked == []
    assert log.read_text().splitlines()[-1] == (
        "services enable gmail.googleapis.com calendar-json.googleapis.com places.googleapis.com "
        "routes.googleapis.com --project=my-own-proj"
    )
    assert "config get-value" not in log.read_text()  # --project given


def test_gcloud_never_runs_on_a_no_or_without_a_terminal(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake, log = _fake_gcloud(tmp_path)
    _, said_no, _ = _gcloud_run(tmp_path, monkeypatch, fake=fake, answer="n")
    _, no_tty, asked = _gcloud_run(tmp_path, monkeypatch, fake=fake, interactive=False)
    assert "services enable" not in log.read_text()
    assert "Not run." in said_no and "Not run (no terminal to ask)" in no_tty and asked == []
    assert "gcloud services enable" in no_tty  # shown all the same


def test_gcloud_missing_logged_out_or_failing_falls_back_to_the_steps(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    empty = tmp_path / "empty"
    empty.mkdir()
    _, missing, _ = _gcloud_run(tmp_path, monkeypatch, fake=empty)
    assert missing.startswith("gcloud is not installed here") and "2. Enable the Gmail API" in missing
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    fake, log = _fake_gcloud(out_dir, logged_in=False)
    _, logged_out, _ = _gcloud_run(tmp_path, monkeypatch, fake=fake, answer="y")
    assert logged_out.startswith("gcloud is not logged in") and "services enable" not in log.read_text()
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    fake, _ = _fake_gcloud(bad_dir, fail=True)
    _, failed, _ = _gcloud_run(tmp_path, monkeypatch, fake=fake, answer="y")
    assert "gcloud failed (exit 1)" in failed and "example.invalid" not in failed
    odd_dir = tmp_path / "odd"
    odd_dir.mkdir()
    fake, log = _fake_gcloud(odd_dir, project="Not A Project;rm")
    _, odd, _ = _gcloud_run(tmp_path, monkeypatch, fake=fake, answer="y")
    assert "not a Google Cloud project id" in odd and "services enable" not in log.read_text()
