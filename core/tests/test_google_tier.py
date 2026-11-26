"""Google AI only on a paid tier (E1, v0.1.6; GDPR Art. 28 and 44, the Gemini API's terms).

A Google key can carry personal data: Gemini Live hears the whole talk, and any model slot can point at
gemini or gemma. The API's unpaid services may not receive personal data, and Pisa can't read a key's tier,
so the owner says it (`google_ai_tier`, unknown until they do): until it is paid, the gateway builds no
Google model (a slot's fallback included) and no Gemini Live talk opens. No migration: an existing Google
slot refuses until it is set.

Synthetic keys and data only.
"""

from typing import Any

import psycopg
import pytest
from pydantic_ai import Agent
from structlog.testing import capture_logs

from pisa.domain import google_tier
from pisa.domain.errors import PisaError
from tests.assistant_env import set_settings
from tests.conftest import FAKE_KEY, FakeModels, dsn, pair_v2, seed_catalog, set_key
from tests.gateway_env import gateway, run
from tests.owner import owner_at
from tests.test_privacy_v2 import auth
from tests.test_talker_open import NATURAL, Refusing

CODE = "google_ai_tier_not_paid"
GOOGLE = "google:gemini-test"


def _ask(db: psycopg.Connection, secrets_key: str, clock: Any, primary: str, fallback: str | None = None,
         *, cheap: bool = False) -> tuple[Any, FakeModels]:  # fmt: skip
    """One call of the conversation slot (`cheap`: the budget guard, which takes the slot's fallback
    first): what came back, and which clients were built."""
    fake = FakeModels()
    refs = [primary, *([fallback] if fallback else [])]
    seed_catalog(db, [{"ref": r, "input": 1.0, "output": 1.0} for r in refs])
    db.execute(
        "INSERT INTO model_slots (id, model_ref, fallback_ref) VALUES ('chat.conversation', %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET model_ref = EXCLUDED.model_ref, fallback_ref = EXCLUDED.fallback_ref",
        (primary, fallback),
    )

    async def go() -> Any:
        async with gateway(dsn(db), secrets_key, fake, clock) as (gw, pool, keys):
            async with pool.connection() as conn:
                await keys.set(conn, "google", FAKE_KEY)
                await keys.set(conn, "openai", FAKE_KEY)

            async def call(ctx: Any) -> str:
                return str((await Agent(ctx.model).run("hi")).output)

            try:
                return await gw.run(
                    "chat.conversation",
                    call,
                    prompt_chars=10,
                    max_output_tokens=20,
                    trace_id="t",
                    cheap=cheap,
                )
            except PisaError as err:
                return err

    return run(go), fake


@pytest.mark.parametrize("tier", ["unknown", "free", "free_eea_own_use"])
def test_a_google_model_is_refused_until_the_tier_allows_it(db, secrets_key, clock, tier) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"google_ai_tier": tier})
    with capture_logs() as logs:
        got, fake = _ask(db, secrets_key, clock, GOOGLE)
    assert isinstance(got, PisaError) and got.code == CODE, got
    # a model needs paid, whatever else is declared (D-142: the own-use declaration is for the talker only)
    assert (got.message, got.action) == google_tier.refusal(owner_at().tag, talker=False)
    assert fake.calls == []  # no Google client was ever built: nothing was sent
    assert db.execute("SELECT count(*) FROM llm_calls").fetchone() == (0,)
    refused = [log for log in logs if log["event"] == "google_ai_tier_refused"]
    assert refused == [
        {"event": "google_ai_tier_refused", "log_level": "info", "code": CODE, "slot": "chat.conversation"}
    ]
    assert FAKE_KEY not in str(logs)


def test_a_fallback_on_google_is_refused_too(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    """E1: the slot's fallback model passes the same check (the budget guard takes it first)."""
    set_settings(db, {"google_ai_tier": "unknown"})
    got, fake = _ask(db, secrets_key, clock, "openai:main", GOOGLE, cheap=True)
    assert isinstance(got, PisaError) and got.code == CODE, got
    assert all(c[0] != "google" for c in fake.calls), fake.calls  # no Google client was built


def test_a_google_model_runs_on_a_paid_tier(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    set_settings(db, {"google_ai_tier": "paid"})
    got, fake = _ask(db, secrets_key, clock, GOOGLE)
    assert not isinstance(got, PisaError), got
    assert [c[:2] for c in fake.calls] == [("google", "gemini-test")]


@pytest.mark.parametrize(
    ("tier", "lang"),
    [("unknown", "en-US"), ("free", "it-IT"), ("paid", "en-US"), ("free_eea_own_use", "it-IT")],
)
def test_gemini_live_opens_only_on_a_paid_tier(
    client: Any, db: psycopg.Connection, secrets_key: str, tier: str, lang: str
) -> None:
    """Unknown and free: 409 with the tier's line in the talk's language, before any talker is asked (no
    socket). Paid: the talker is asked (here one that refuses on its own, so the guard is past)."""
    set_key(db, secrets_key, provider="google")
    set_settings(db, {**NATURAL, "google_ai_tier": tier})
    _, token = pair_v2(db, health=True)
    asked: list[bool] = []

    class Counting(Refusing):
        async def open(self, cfg: Any) -> Any:
            asked.append(True)
            return await super().open(cfg)

    client.app.state.runtime.talk._talkers = {"gemini": Counting()}
    body = {"sdp_offer": "v=0", "trigger": "press", "lang": lang}
    with capture_logs() as logs:
        answer = client.post("/v1/talk/sessions", headers=auth(token), json=body)
    error = answer.json()["error"]
    if tier in ("paid", "free_eea_own_use"):
        assert answer.status_code == 502 and error["code"] == "talker_unavailable" and asked == [True]
        return
    assert answer.status_code == 409 and error["code"] == CODE, answer.text
    assert asked == [] and (error["message"], error["action"]) == google_tier.refusal(lang, talker=True)
    refused = {"event": "google_ai_tier_refused", "log_level": "info", "code": CODE, "talker": "gemini"}
    assert refused in logs and FAKE_KEY not in str(logs)


def test_the_tier_is_a_setting_with_four_values(client: Any, db: psycopg.Connection) -> None:
    _, token = pair_v2(db)
    assert client.get("/v1/settings", headers=auth(token)).json()["google_ai_tier"] == "unknown"
    wrong = client.patch("/v1/settings", headers=auth(token), json={"google_ai_tier": "maybe"})
    assert wrong.status_code == 400 and "google_ai_tier" in wrong.text
    for tier in ("free", "free_eea_own_use", "paid", "unknown"):
        set_to = client.patch("/v1/settings", headers=auth(token), json={"google_ai_tier": tier})
        assert set_to.status_code == 200 and set_to.json()["google_ai_tier"] == tier
    # E1's scope (D-142): the own-use declaration lets Gemini Live hear them, never a Google model
    assert google_tier.TALKER_TIERS == {"paid", "free_eea_own_use"}
    assert google_tier.MODEL_TIERS == {"paid"}


def test_each_route_s_refusal_is_one_text_in_both_languages() -> None:
    message, action = google_tier.refusal("en-US", talker=False)
    assert message.startswith("Google AI needs a paid tier to receive your data (Gemini API terms)")
    assert action.endswith("Settings → Connections (or run pisa setup).")
    message, action = google_tier.refusal("it-IT", talker=False)
    assert message.startswith("Google AI richiede un piano a pagamento per ricevere i tuoi dati")
    message, action = google_tier.refusal("en-US", talker=True)
    assert "EEA, Switzerland or the UK" in message and "free for your own use" in action
    message, action = google_tier.refusal("it-IT", talker=True)
    assert "SEE, in Svizzera o nel Regno Unito" in message and "gratuito per uso personale" in action


@pytest.mark.parametrize(
    ("answers", "tier"),
    [
        (["sì"], "paid"),
        (["no", "sì", "sì"], "free_eea_own_use"),
        (["no", "sì", "no"], "unknown"),
        (["no", "no"], "unknown"),  # not in the EEA, CH or UK: the second question isn't asked
        ([""], "unknown"),  # no answer is no
    ],
)
def test_setup_asks_billing_then_both_questions_of_the_declaration(
    app_settings: Any, db: psycopg.Connection, secrets_key: str, answers: list[str], tier: str
) -> None:
    import asyncio
    import io

    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup
    from pisa.domain import guide
    from tests.test_pair_cli import setup_args

    set_key(db, secrets_key, "google")
    asked: list[str] = []
    queue = list(answers)
    keys = ("google.ask_paid", "google.ask_eea", "google.ask_only_user")
    google_questions = [guide.text(k, "it") for k in keys]

    def ask(question: str) -> str:
        mine = next((q for q in google_questions if question.startswith(q)), None)
        if mine is not None:
            asked.append(mine)
            return queue.pop(0) if queue else ""
        return "sì"  # natural talks, when offered

    out = io.StringIO()
    args = setup_args(lang="it", skip=["place,ai,push,pair"])
    settings = app_settings.model_copy(update={"database_url": dsn(db)})
    assert asyncio.run(run_setup(args, settings, Prompt(True, ask=ask, out=out))) == 0
    row = db.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
    stored = (row[0] if row else {}).get("google_ai_tier", "unknown")
    assert stored == tier
    # billing first; the declaration's second question only after a yes to the first
    shown = 1 if tier == "paid" else (3 if answers[1:2] == ["sì"] else 2)
    assert asked == google_questions[:shown]
    natural = (row[0] if row else {}).get("conversation", {}).get("mode") == "natural"
    assert natural is (tier in ("paid", "free_eea_own_use"))


def test_a_server_the_user_added_on_a_google_host_is_google_s(db, secrets_key, clock) -> None:  # type: ignore[no-untyped-def]
    """E1 GT-1: Gemini's OpenAI-compatible endpoint, added as the user's own server, is refused the same way,
    before any probe reaches it."""
    for url, governed in (
        ("https://generativelanguage.googleapis.com/v1beta/openai", True),
        ("https://googleapis.com/v1", True),
        ("https://models.home.test/v1", False),
        ("https://notgoogleapis.com/v1", False),
        ("https://googleapis.com.example.test/v1", False),
    ):
        assert google_tier.governs("my-server", url) is governed, url
    assert google_tier.governs("google", "")
    set_settings(db, {"google_ai_tier": "free"})
    db.execute(
        "INSERT INTO model_servers (id, name, base_url) VALUES "
        "('gem', 'Gemini compatible', 'https://generativelanguage.googleapis.com/v1beta/openai')"
    )
    got, fake = _ask(db, secrets_key, clock, "gem:gemini-test")
    assert isinstance(got, PisaError) and got.code == CODE, got
    assert fake.calls == []
