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
IDS = ["client", "gmail_api", "calendar_api", "scopes", "paste", "connect"]


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
    assert "7" in en["steps"][-1]["body"] and "7" in it["steps"][-1]["body"]


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
