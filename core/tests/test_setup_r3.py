"""The owner's live test of `pisa setup` and the CLI: after the provider's key, every text slot gets a model
of that provider (its main one for the conversation, its small one where the slot asks for fast), memory
search an embeddings model of any provider with a key, and what stays off is named in one sentence; the voice
step picks natural talks with Gemini Live only when a Google key is set (never Grok Voice by default), through
the settings service, so the apps are told; `pisa consent` gives the AI consent on the server; and a CLI
command leaves no catalog refresh running on its closed HTTP client. Fake catalog rows only."""

import asyncio
import io
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from structlog.testing import capture_logs

from pisa.domain import guide, setup_models
from pisa.domain import settings as settings_rules
from tests.conftest import dsn, pair_v2, seed_catalog, set_key
from tests.test_pair_cli import setup_args

TOOLS = ["tools", "structured_output"]
CATALOG: list[dict[str, Any]] = [
    {"ref": "openai:gpt-main", "capabilities": TOOLS, "input": 2.0, "output": 10.0},
    {"ref": "openai:gpt-mini", "capabilities": TOOLS, "input": 0.1, "output": 0.4},
    {"ref": "openai:gpt-research-pro", "capabilities": TOOLS, "input": 20.0, "output": 80.0},
    {"ref": "openai:gpt-main-preview", "capabilities": TOOLS, "input": 3.0, "output": 12.0},
    {"ref": "openai:gpt-realtime", "capabilities": TOOLS, "input": 4.0, "output": 16.0},
    {"ref": "openai:text-embedding-3-small", "capabilities": ["embeddings"], "input": 0.02, "output": 0.0},
    {"ref": "google:gemini-embedding-001", "capabilities": ["embeddings"], "input": 0.15, "output": 0.0},
    {"ref": "mistral:mistral-chat", "capabilities": ["tools"], "input": 0.4, "output": 2.0},
]


def rows(catalog: list[dict[str, Any]], keyed: set[str]) -> list[dict[str, Any]]:
    """`AiService.models(None)`'s rows for a catalog (prices in €, as if the rate were 1)."""
    out = []
    for r in catalog:
        provider, _, model_id = r["ref"].partition(":")
        out.append({
            "ref": r["ref"], "provider": provider, "model_id": model_id, "capabilities": r["capabilities"],
            "key_set": provider in keyed,
            "price_eur_per_mtok": {"input": r["input"], "output": r["output"]} if "output" in r else None,
        })  # fmt: skip
    return out


def test_the_plan_gives_every_text_slot_the_provider_s_main_or_small_model() -> None:
    chosen, missing = setup_models.plan(rows(CATALOG, {"openai", "google"}), "openai")
    assert missing == []
    fast = {s: "openai:gpt-mini" for s in setup_models.FAST_SLOTS}
    main = {s: "openai:gpt-main" for s in setup_models.TEXT_SLOTS if s not in setup_models.FAST_SLOTS}
    assert chosen == main | fast | {"memory.embed": "openai:text-embedding-3-small"}  # its own first


def test_the_plan_takes_another_provider_s_embeddings_and_names_what_stays_off() -> None:
    chosen, missing = setup_models.plan(rows(CATALOG, {"mistral", "google"}), "mistral")
    assert chosen["memory.embed"] == "google:gemini-embedding-001"
    # mistral-chat calls tools but has no structured output: the slots that need it stay off
    needs_structured = [
        s
        for s in setup_models.TEXT_SLOTS
        if s not in ("chat.conversation", "briefing.compose", "understand.confirm", "learn.answer")
    ]
    assert missing == needs_structured
    assert {chosen[s] for s in ("chat.conversation", "briefing.compose", "learn.answer")} == {
        "mistral:mistral-chat"
    }
    _, none = setup_models.plan(rows(CATALOG, {"mistral"}), "mistral")
    assert none[-1] == "memory.embed"


def offline() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))


@pytest.mark.parametrize("lang", ["it", "en"])
def test_setup_sets_every_slot_from_the_catalog(
    app_settings: Any, db: psycopg.Connection, secrets_key: str, lang: str
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    set_key(db, secrets_key, "openai")
    set_key(db, secrets_key, "google")
    seed_catalog(db, CATALOG)
    out = io.StringIO()
    args = setup_args(lang=lang, skip=["place,talk,push,pair"], ai="cloud", provider="openai")
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out), http=offline())) == 0
    slots = dict(db.execute("SELECT id, model_ref FROM model_slots").fetchall())
    assert (
        slots["chat.conversation"] == "openai:gpt-main" and slots["conversation.reflex"] == "openai:gpt-mini"
    )
    assert slots["memory.embed"] == "openai:text-embedding-3-small"
    assert {s: slots[s] for s in setup_models.TEXT_SLOTS} == {
        s: "openai:gpt-mini" if s in setup_models.FAST_SLOTS else "openai:gpt-main"
        for s in setup_models.TEXT_SLOTS
    }
    text = out.getvalue()
    assert guide.text("ai.slots_set", lang) in text and "  attend.decide: openai:gpt-mini" in text
    assert guide.text("ai.model_set", lang, model="openai:gpt-main") in text
    assert guide.text("ai.slots_missing", lang, slots="X").split("X")[0] not in text  # nothing left unset


def test_setup_names_the_slots_it_could_not_fill_in_one_sentence(
    app_settings: Any, db: psycopg.Connection, secrets_key: str
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    set_key(db, secrets_key, "mistral")
    seed_catalog(db, CATALOG)
    out = io.StringIO()
    args = setup_args(lang="it", skip=["place,talk,push,pair"], ai="cloud", provider="mistral")
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out), http=offline())) == 0
    text = out.getvalue()
    off = "conversation.reflex, intake.triage, intake.extract, places.research, attend.decide"
    sentence = guide.text("ai.slots_missing", "it", slots=f"{off}, {guide.text('ai.embed_missing', 'it')}")
    # every slot left unset, memory search with them, in one sentence on one line
    assert sentence in text.splitlines() and sentence.count(". ") == 0 and "memory.embed" in sentence
    assert sum("memory.embed" in line for line in text.splitlines()) == 1
    slots = dict(db.execute("SELECT id, model_ref FROM model_slots").fetchall())
    assert slots["chat.conversation"] == "mistral:mistral-chat" and slots["memory.embed"] != "mistral"


@pytest.mark.parametrize(
    ("provider", "page"),
    [
        ("anthropic", "https://platform.claude.com/settings/keys"),
        ("openai", "https://platform.openai.com/api-keys"),
        ("google", "https://aistudio.google.com/apikey"),
    ],
)
def test_setup_s_ai_step_points_at_the_chosen_provider_s_key_page(
    app_settings: Any, db: psycopg.Connection, provider: str, page: str
) -> None:
    """The cloud choice prints the provider's own key page (the docs' ai-providers page links the same)."""
    from pisa.cli_setup import KEY_PAGE, Prompt
    from pisa.cli_setup import run as run_setup

    seed_catalog(db, CATALOG)
    for lang in ("en", "it"):
        out = io.StringIO()
        args = setup_args(lang=lang, skip=["place,talk,push,pair"], ai="cloud", provider=provider)
        settings = app_settings.model_copy(update={"database_url": dsn(db)})
        assert asyncio.run(run_setup(args, settings, Prompt(False, out=out), http=offline())) == 0
        assert KEY_PAGE[lang].format(url=page) in out.getvalue().splitlines()


@pytest.mark.parametrize(
    ("google", "mode", "talker"), [(True, "natural", "gemini"), (False, "economy", None)]
)
def test_setup_s_voice_step_is_natural_with_gemini_only_with_a_google_key(
    app_settings: Any, db: psycopg.Connection, secrets_key: str, google: bool, mode: str, talker: str | None
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    set_key(db, secrets_key, "xai")  # an xAI key alone is never natural mode by default
    if google:
        set_key(db, secrets_key, "google")
    listener = psycopg.connect(dsn(db), autocommit=True)
    listener.execute("LISTEN pisa_ai_disclosure")
    out = io.StringIO()
    args = setup_args(lang="en", skip=["place,ai,push,pair"])
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out))) == 0
    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    assert row is not None
    conversation = row[0]["conversation"]
    assert conversation["mode"] == mode
    if talker is not None:
        assert conversation["talker"] == talker
        assert guide.text("talk.natural", "en") in out.getvalue()
        # through the settings service: the apps are told (the talker is in the disclosure now)
        assert [n.channel for n in listener.notifies(timeout=2, stop_after=1)] == ["pisa_ai_disclosure"]
    else:
        assert guide.text("talk.economy", "en") in out.getvalue()
    listener.close()


def test_a_fresh_install_talks_in_economy_and_never_points_at_xai() -> None:
    conversation = settings_rules.defaults("it-IT")["conversation"]
    assert (conversation["mode"], conversation["talker"]) == ("economy", "gemini")


# --- pisa consent -------------------------------------------------------------------------------------------


def consent(db: psycopg.Connection, app_settings: Any, **kw: Any) -> tuple[int, str]:
    from pisa.cli_consent import consent_flow

    out = io.StringIO()
    settings = app_settings.model_copy(update={"database_url": dsn(db)})

    async def go() -> int:
        async with await psycopg.AsyncConnection.connect(dsn(db), autocommit=True) as conn:
            base = {"device": None, "health": False, "yes": False, "lang": "it", "ask": None}
            return await consent_flow(conn, settings, out=out, **(base | kw))

    return asyncio.run(go()), out.getvalue()


def recorded(db: psycopg.Connection, device_id: str) -> tuple[Any, Any]:
    row = db.execute(
        "SELECT ai_consent_revision, ai_consent_health FROM devices WHERE id = %s", (device_id,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_pisa_consent_shows_the_disclosure_and_records_it_for_the_chosen_device(
    app_settings: Any, db: psycopg.Connection, secrets_key: str, client: Any
) -> None:
    set_key(db, secrets_key, "openai")
    seed_catalog(db, CATALOG)
    db.execute("UPDATE model_slots SET model_ref = 'openai:gpt-main' WHERE id = 'chat.conversation'")
    first, _ = pair_v2(db, "iPad", consent=False)
    second, token = pair_v2(db, "iPhone", consent=False)
    answers = iter(["2", "s"])
    code, text = consent(db, app_settings, ask=lambda q: next(answers))
    assert code == 0, text
    assert guide.consent_text("which", "it") in text and "iPad" in text and "iPhone" in text
    assert "OpenAI" in text and "gpt-main" in text and guide.consent_category("mail", "it") in text
    assert guide.consent_text("given", "it", name="iPhone") in text
    revision = client.get("/v1/ai/disclosure", headers={"Authorization": f"Bearer {token}"}).json()[
        "revision"
    ]
    assert recorded(db, second) == (revision, False)
    assert recorded(db, first) == (None, False)
    code, _ = consent(db, app_settings, device=first, yes=True, health=True)
    assert code == 0 and recorded(db, first) == (revision, True)


def test_pisa_consent_asks_and_takes_no_for_an_answer(app_settings: Any, db: psycopg.Connection) -> None:
    device, _ = pair_v2(db, consent=False)
    code, text = consent(db, app_settings, ask=lambda q: "n", lang="en")
    assert code == 1 and guide.consent_text("not_given", "en") in text
    assert recorded(db, device) == (None, False)
    code, text = consent(db, app_settings, lang="en")  # no terminal, no --yes
    assert code == 2 and guide.consent_text("give_yes", "en") in text
    pair_v2(db, "iPad", consent=False)
    code, text = consent(db, app_settings, lang="en", yes=True)  # several devices, no terminal, no --device
    assert code == 2 and guide.consent_text("give_device", "en") in text


def test_pisa_consent_logs_to_stderr_only(
    app_settings: Any,
    db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The owner's Mac: ai_consent_recorded printed as JSON among the command's own lines."""
    import structlog

    from pisa import cli

    pair_v2(db, consent=False)
    monkeypatch.setattr(
        "pisa.cli.get_settings", lambda: app_settings.model_copy(update={"database_url": dsn(db)})
    )
    from pisa.logging import configure_logging

    monkeypatch.setattr("pisa.cli.configure_logging", configure_logging)  # the real one, as `pisa` runs
    try:
        assert cli.main(["consent", "--yes"]) == 0
    finally:
        structlog.reset_defaults()
    captured = capsys.readouterr()
    assert all(not line.lstrip().startswith("{") for line in captured.out.splitlines()), captured.out
    assert "ai_consent_recorded" not in captured.out


# --- no refresh left on a closed client ---------------------------------------------------------------------


def test_drain_leaves_no_refresh_running(app_settings: Any, db: psycopg.Connection, secrets_key: str,
                                         monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from pisa.db.pool import open_pool
    from pisa.infra.keys import KeyStore
    from pisa.services import ai as ai_module
    from pisa.services.ai import AiService
    from pisa.services.owner import provide

    async def slow(*_: Any, **__: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(ai_module, "refresh_catalog", slow)

    async def go() -> int:
        pool = await open_pool(dsn(db), max_size=2)
        try:
            async with offline() as http:
                ai = AiService(
                    pool,
                    KeyStore(app_settings.secrets_key),
                    http,
                    speech={},
                    owner=provide(app_settings, lambda: datetime.now(UTC)),
                    ollama_base_url=None,
                )
                ai.refresh_in_background()
                (task,) = ai._background
                await ai.drain(within_s=0.2)
                return len(ai._background) + (0 if task.done() else 1)
        finally:
            await pool.close()

    with capture_logs() as logs:
        assert asyncio.run(go()) == 0
    assert not [log for log in logs if log["event"] == "catalog_refresh_failed"]


def test_a_cli_command_s_refresh_never_runs_on_its_closed_client(
    app_settings: Any, db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pisa setup` with a new key: the key test starts a catalog refresh; the command's end waits for it (or
    cancels it) before its HTTP client closes, so nothing logs 'client has been closed'."""
    import sys

    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    def models(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": "gpt-main", "object": "model"}]}
            )
        return httpx.Response(404)

    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-test-not-a-real-key\n"))
    args = setup_args(lang="en", skip=["place,talk,push,pair"], ai="cloud", provider="openai", key_stdin=True)
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    with capture_logs() as logs:
        code = asyncio.run(
            run_setup(args, settings, Prompt(False, out=io.StringIO()),
                      http=httpx.AsyncClient(transport=httpx.MockTransport(models)))
        )  # fmt: skip
    assert code == 0
    assert not [log for log in logs if "closed" in str(log.get("cause", ""))], logs


@pytest.mark.parametrize(
    ("answer", "mode"), [("n", "economy"), ("no", "economy"), ("sì", "natural"), ("", "natural")]
)
def test_setup_asks_before_natural_talks_and_says_they_need_the_health_consent(
    app_settings: Any, db: psycopg.Connection, secrets_key: str, answer: str, mode: str
) -> None:
    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup

    set_key(db, secrets_key, "google")
    out = io.StringIO()
    args = setup_args(lang="it", skip=["place,ai,push,pair"])
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    prompt = Prompt(True, ask=lambda _q: answer, out=out)
    assert asyncio.run(run_setup(args, settings, prompt)) == 0
    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    assert row is not None and row[0]["conversation"]["mode"] == mode
    said = out.getvalue()
    assert guide.text("talk.health", "it") in said
    if mode == "economy":
        assert guide.text("talk.kept_economy", "it") in said
