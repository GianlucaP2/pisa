"""Experimental features: everything built is supported and on; only stubs and unbuilt or unused integrations
(garmin, polar, withings, bank, ade, radar, talker_xai) are hidden by default, by a registry
(`pisa.domain.experimental`) and one env switch (PISA_EXPERIMENTAL), never by a branch.

An off feature doesn't exist: its connectors are absent from `/v1/connectors` (their routes 404), its tools
are not offered to the model, "what can you do" neither counts it nor names it as "not connected yet", its
worker steps don't run, `pisa setup` doesn't offer it; discovery lists an enabled one as
`experimental.<id>`. The suite runs with every feature on (`tests/conftest.py`); these tests set their own. A
test that needs a supported feature off (reminders' rails, the gated routers) forces it experimental again
with `forced_off`, a test-only registry override."""

import dataclasses
import uuid
from typing import Any

import pytest
from pisa_conformance.cli import discovery_problems
from pisa_conformance.validate import EXT_ID, errors

from pisa.api import server as server_api
from pisa.brain.conversation import ToolFamilies, families
from pisa.domain import abilities, experimental
from pisa.domain import connectors as connector_rules
from pisa.domain import tools as tool_registry
from pisa.services import conversation as conversation_service
from pisa.services import worker_steps
from pisa.workers import main as worker
from tests.conftest import pair_v2, set_key
from tests.test_live_v2_socket import connect, hello, opened, run, said
from tests.test_openapi_v2_delta import delta_body_errors

NONE = frozenset[str]()


@pytest.fixture
def experimental_spec() -> str:
    """PISA_EXPERIMENTAL for this test's server: none (the default) unless a test parametrizes it."""
    return ""


@pytest.fixture
def forced_off() -> tuple[str, ...]:
    """Supported features this test forces off: experimental again (a test-only override of the registry),
    and not in its PISA_EXPERIMENTAL. None unless a test parametrizes it."""
    return ()


def force_off(monkeypatch: pytest.MonkeyPatch, *ids: str) -> None:
    for feature_id in ids:
        demoted = dataclasses.replace(experimental.BY_ID[feature_id], experimental=True)
        monkeypatch.setitem(experimental.BY_ID, feature_id, demoted)


@pytest.fixture
def app_settings(
    app_settings: Any, experimental_spec: str, forced_off: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> Any:
    force_off(monkeypatch, *forced_off)
    return app_settings.model_copy(update={"experimental": experimental_spec})


# the seven that stay experimental: stubs and unbuilt or unused integrations
STILL_OFF = frozenset({"garmin", "polar", "withings", "bank", "ade", "radar", "talker_xai"})
# what a reminders-off test forces off
REMINDERS_OFF = pytest.mark.parametrize("forced_off", [("reminders",)])


@pytest.fixture
def device(db: Any) -> tuple[str, str]:
    return pair_v2(db)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the registry and the switch ------------------------------------------------------------------------


def test_the_switch_names_features_by_id_all_turns_every_one_on_and_an_unknown_id_is_ignored() -> None:
    assert experimental.enabled("") == NONE
    assert experimental.enabled("google,garmin") == {"google", "garmin"}
    assert experimental.enabled(" Google , GARMIN ,") == {"google", "garmin"}  # case and spaces
    assert experimental.enabled("all") == experimental.ALL == frozenset(experimental.BY_ID)
    assert experimental.enabled("garmin,nope") == {"garmin"}  # an unknown id is ignored…
    assert experimental.unknown("garmin,nope,all") == ("nope",)  # …and named, for the caller's warning
    assert experimental.unknown("google") == ()


def test_a_supported_feature_is_always_on_and_an_experimental_one_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert experimental.on("garmin", NONE) is False
    assert experimental.on("garmin", {"garmin"}) is True
    assert experimental.on("garmin", experimental.enabled("all")) is True
    # everything built is supported: on whatever the switch says
    for built in ("google", "apple_health", "reminders", "places", "push_relay", "tts_server", "step_learn"):
        assert experimental.on(built, NONE) is True, built
    # promoting a feature is its one line, experimental=False: then it is on whatever the switch says
    promoted = dataclasses.replace(experimental.BY_ID["bank"], experimental=False)
    monkeypatch.setitem(experimental.BY_ID, "bank", promoted)
    assert experimental.on("bank", NONE) is True
    with pytest.raises(KeyError):  # an id the registry doesn't have is a programming error
        experimental.on("nope", NONE)


def test_only_stubs_and_unbuilt_or_unused_integrations_are_experimental() -> None:
    assert experimental.EXPERIMENTAL == STILL_OFF
    assert all(not f.experimental for f in experimental.REGISTRY if f.kind == "step")  # every step is built


def test_review_mode_turns_every_experimental_feature_off_and_keeps_its_outward_ones_off() -> None:
    """Review mode as it was: every experimental feature off, whatever the list says, and the supported
    features that reach outside the server or are connectors (`REVIEW_OFF`) off too."""
    review = experimental.enabled("all", review_mode=True)
    assert experimental.listed(review) == [] and experimental.unknown(experimental.REVIEW) == ()
    assert not any(experimental.on(f, review) for f in STILL_OFF | experimental.REVIEW_OFF)
    assert experimental.on("reminders", review) and not experimental.on("places", review)
    assert experimental.on("step_plan", review) and not experimental.on("step_public_data", review)
    # RV-5: Learn's fetches, the night's learning and the handoffs' merchants (UCP) reach outside too
    assert not any(experimental.on(f, review) for f in ("learn", "handoffs", "step_learn"))
    assert {"learn", "world", "handoffs"} <= experimental.families_off(review)
    assert not any(experimental.connector_on(c, review) for c in experimental.CONNECTORS)  # no connector
    assert experimental.enabled(experimental.review_spec("garmin", review_mode=True)) == review
    assert experimental.enabled(experimental.review_spec("garmin", review_mode=False)) == {"garmin"}


def test_the_registry_names_only_what_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    kinds = {"connector", "tools", "step", "voice"}
    assert all(f.kind in kinds and f.gates for f in experimental.REGISTRY)
    assert all(re.match(r"^[a-z][a-z0-9_.]{1,60}$", f"experimental.{f.id}") for f in experimental.REGISTRY)
    # every connector belongs to a feature, each feature to real connectors
    assert set(experimental.CONNECTORS) == {s.id for s in connector_rules.REGISTRY}
    assert set(experimental.CONNECTORS.values()) <= set(experimental.BY_ID)
    # the hidden families are ToolFamilies' fields; the hidden single tools are declared tools
    assert set(experimental.FAMILIES) <= {f.name for f in dataclasses.fields(ToolFamilies)}
    every_tool = set(tool_registry.REGISTRY)
    assert all(names <= every_tool for names in experimental.TOOLS.values())
    assert set(experimental.FAMILIES.values()) | set(experimental.TOOLS) <= set(experimental.BY_ID)
    # every gated step is a step of the worker, and its feature is in the registry
    for step in worker_steps.STEPS:
        assert step.feature is None or step.feature in experimental.BY_ID
    steps = {f.id for f in experimental.REGISTRY if f.kind == "step"}
    assert steps == {f"step_{s.name}" for s in worker_steps.STEPS if s.feature == f"step_{s.name}"}
    # the supported path keeps running: core, tasks, today's plan, stop and resume, weather, intentions
    assert experimental.tools_off(NONE) == NONE  # every tool family is built: a default install hides none
    force_off(monkeypatch, "reminders", "places", "phone_actions", "team")
    off = experimental.tools_off(NONE)
    kept = {"finish", "add_task", "complete_task", "delete_task", "get_tasks", "get_plan", "what_now",
            "stop_everything", "resume_everything", "get_weather", "keep_intention"}  # fmt: skip
    assert kept.isdisjoint(off)
    assert {"remember_to", "map_search", "compose_message", "call_place", "tell_the_team"} <= off


# --- connectors: the list, the routes ------------------------------------------------------------------


SUPPORTED_CONNECTORS = ["google", "apple_health"]  # in the registry's order


def test_an_experimental_connector_is_absent_by_default(client, device) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    listed = client.get("/v1/connectors", headers=auth(token))
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()["connectors"]] == SUPPORTED_CONNECTORS  # the built ones only
    for path in ("/v1/connectors/garmin", "/v1/connectors/bank", "/v1/connectors/bank_2",
                 "/v1/connectors/admin_pec", "/v1/connectors/ade_cassetto", "/v1/connectors/polar",
                 "/v1/connectors/withings", "/v1/obligations",
                 "/v1/connectors/bank/return?code=c&state=s"):  # fmt: skip
        got = client.get(path, headers=auth(token), follow_redirects=False)
        assert got.status_code == 404 and got.json()["error"]["code"] == "not_found", path
    patched = client.patch("/v1/connectors/garmin", json={"enabled": True}, headers=auth(token))
    assert patched.status_code == 404
    # Google and Apple Health are supported: their routes exist
    assert client.get("/v1/connectors/google", headers=auth(token)).status_code == 200
    assert client.get("/v1/connectors/google/setup", headers=auth(token)).status_code == 200
    summary = client.post("/v1/health/summary", json={"day": "2026-10-20"}, headers=auth(token))
    assert summary.status_code != 404


@pytest.mark.parametrize("forced_off", [("google", "apple_health")])
def test_a_connector_forced_off_has_no_route(client, device) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    assert client.get("/v1/connectors", headers=auth(token)).json() == {"connectors": []}
    for path in ("/v1/connectors/google", "/v1/connectors/google/setup"):
        got = client.get(path, headers=auth(token), follow_redirects=False)
        assert got.status_code == 404 and got.json()["error"]["code"] == "not_found", path
    summary = client.post("/v1/health/summary", json={"day": "2026-10-20"}, headers=auth(token))
    assert summary.status_code == 404
    assert client.get("/oauth/google/callback?state=x&code=y").status_code == 404


@pytest.mark.parametrize("experimental_spec", ["garmin"])
def test_an_enabled_connector_appears_and_only_it(client, device) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    listed = client.get("/v1/connectors", headers=auth(token)).json()["connectors"]
    assert [c["id"] for c in listed] == ["google", "apple_health", "garmin"]
    one = client.get("/v1/connectors/garmin", headers=auth(token))
    assert one.status_code == 200 and one.json()["id"] == "garmin"
    assert client.get("/v1/connectors/bank_2", headers=auth(token)).status_code == 404
    assert client.get("/v1/connectors/admin_pec", headers=auth(token)).status_code == 404


@pytest.mark.parametrize("experimental_spec", ["all"])
def test_all_turns_every_connector_on(client, device) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    listed = client.get("/v1/connectors", headers=auth(token)).json()["connectors"]
    assert [c["id"] for c in listed] == [s.id for s in connector_rules.REGISTRY]
    assert client.get("/v1/connectors/google/setup", headers=auth(token)).status_code == 200


@pytest.mark.parametrize("experimental_spec", ["garmin,nope"])
def test_an_unknown_id_is_ignored(client, device) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    assert client.app.state.runtime.enabled == {"garmin"}
    listed = client.get("/v1/connectors", headers=auth(token)).json()["connectors"]
    assert [c["id"] for c in listed] == ["google", "apple_health", "garmin"]


# --- the tools offered to the model ---------------------------------------------------------------------


def _offered(client: Any, db: Any, secrets_key: str, device: tuple[str, str], monkeypatch: Any) -> set[str]:
    """Every tool the turn's agent registered (the deferred ones too), from a real turn over /v2/live."""
    from pydantic_ai.toolsets import FunctionToolset

    device_id, token = device
    set_key(db, secrets_key)
    built: list[Any] = []
    original = conversation_service.build_agent

    def spy(model: Any, context: Any) -> Any:
        agent = original(model, context)
        built.append(agent)
        return agent

    monkeypatch.setattr(conversation_service, "build_agent", spy)
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run("ciao")
        app.send(turn)
        app.run_ends(turn["input"]["runId"])
    assert built, "the turn built no agent"
    return {n for t in built[-1].toolsets if isinstance(t, FunctionToolset) for n in t.tools}


@pytest.mark.parametrize(
    "forced_off",
    [("reminders", "places", "memory", "note", "profile", "google", "phone_actions", "apple_health")],
)
def test_an_off_feature_s_tools_are_not_offered(client, db, secrets_key, device, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db.execute("UPDATE connectors SET enabled = true, state = 'needs_owner' WHERE id = 'garmin'")
    offered = _offered(client, db, secrets_key, device, monkeypatch)
    assert (
        "health_summary" not in offered
    )  # garmin's tool: garmin doesn't exist (nor Apple Health, forced off)
    assert {"remember_to", "map_search", "recall", "rewrite_my_note", "remember_about_me"}.isdisjoint(offered)
    assert {"search_my_email", "create_event", "compose_message", "call_place"}.isdisjoint(offered)
    assert {"add_task", "complete_task", "delete_task", "get_tasks", "get_plan", "stop_everything",
            "resume_everything", "get_weather", "finish"} <= offered  # fmt: skip


@pytest.mark.parametrize("forced_off", [("reminders", "google")])
@pytest.mark.parametrize("experimental_spec", ["garmin"])
def test_an_enabled_connector_s_tool_is_offered(client, db, secrets_key, device, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        "UPDATE connectors SET enabled = true, state = 'needs_owner' WHERE id = 'garmin'"
    )  # their switch on
    offered = _offered(client, db, secrets_key, device, monkeypatch)
    assert "health_summary" in offered
    assert "remember_to" not in offered and "search_my_email" not in offered  # only garmin was turned on


def test_families_hide_an_off_family_and_keep_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    services = ToolFamilies(data=object(), places=object(), profile=object(), memory=object())  # type: ignore[arg-type]
    assert families(services, NONE)["memory"] is services.memory  # supported: a default install keeps it
    force_off(monkeypatch, "profile", "memory")
    kept = families(services, NONE)
    assert kept["data"] is services.data and kept["places"] is services.places  # tasks; weather stays
    assert kept["profile"] is None and kept["memory"] is None
    assert families(services, experimental.ALL)["profile"] is services.profile
    assert families(services)["memory"] is services.memory  # built alone: every family as it is


# --- the capability answer ------------------------------------------------------------------------------


def _ask(client: Any, token: str, device_id: str, words: str) -> str:
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run(words)
        app.send(turn)
        frames = app.run_ends(turn["input"]["runId"])
    return said(frames)


@pytest.mark.parametrize("forced_off", [("google", "apple_health", "places", "phone_actions", "reminders")])
def test_what_can_you_do_never_names_an_off_feature(client, db, secrets_key, device) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    answer = _ask(client, token, device_id, "Cosa puoi fare?")
    assert answer.startswith("Qui mi occupo di: le tue attività e il piano della giornata")  # no reminders
    assert "Google" not in answer and "mappe" not in answer and "messaggi" not in answer
    assert "notifiche" in answer  # push itself is there (direct)
    facts = client.portal.call(
        client.app.state.runtime.conversations._abilities, uuid.UUID(device_id), None, None, None
    )
    assert {"google", "health", "bank", "places", "phone", "reminders"} <= facts.off
    assert facts.health is False and facts.google is False


def test_what_can_you_do_names_google_as_not_connected_yet_by_default(
    client, db, secrets_key, device
) -> None:  # type: ignore[no-untyped-def]
    device_id, token = device
    set_key(db, secrets_key)
    answer = _ask(client, token, device_id, "Cosa puoi fare?")
    assert "Google, per posta e calendario" in answer  # Google is supported: not connected yet
    facts = client.portal.call(
        client.app.state.runtime.conversations._abilities, uuid.UUID(device_id), None, None, None
    )
    assert "health" not in facts.off and "google" not in facts.off and "bank" in facts.off


def test_a_hidden_item_is_neither_said_as_done_nor_as_not_connected() -> None:
    on = abilities.Facts(health=True, maps=True, google=False)
    assert any("salute" in x for x in abilities.can(on, "it"))
    hidden = abilities.without(dataclasses.replace(on, off=frozenset({"health", "maps", "places", "google"})))
    assert not any("salute" in x or "luoghi" in x for x in abilities.can(hidden, "it"))
    assert not any("Google" in x or "mappe" in x for x in abilities.missing(hidden, "it"))
    assert abilities.not_connected(hidden, "it", claimed=["mail"]) == ""


# --- the worker ----------------------------------------------------------------------------------------


def test_every_built_step_runs_by_default_and_an_off_step_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    for group in worker_steps.MINUTE_GROUPS:  # every step is supported: a default install runs them all
        assert worker_steps.due(group, False, NONE) == list(worker_steps.MINUTE_GROUPS[group]), group
    assert {s.name for s in worker.schedules(NONE)} == {s.name for s in worker.SCHEDULES}
    force_off(monkeypatch, "reminders", "step_briefing_time", "step_learn", "google")
    off = worker_steps.due(worker_steps.ON_TIME, False, NONE)
    assert "reminders" not in off and "briefing_time" not in off
    assert {"approvals_lapsing", "approvals_expire", "quiet_release"} <= set(off)  # plumbing
    assert "learn" not in worker_steps.due(worker_steps.INITIATIVE, False, NONE)
    assert "reminders" in worker_steps.due(worker_steps.ON_TIME, False, frozenset({"reminders"}))
    names = {s.name for s in worker.schedules(NONE)}
    assert "gmail-sync" not in names and "calendar-sync" not in names and "minute-on-time" in names
    assert {s.name for s in worker.schedules(frozenset({"google"}))} == {s.name for s in worker.SCHEDULES}
    # review mode: the public data's downloads and the night's learning (RV-5) don't run, the rest does
    review = experimental.enabled("", review_mode=True)
    monkeypatch.undo()
    assert "public_data" not in worker_steps.due(worker_steps.INITIATIVE, False, review)
    assert "learn" not in worker_steps.due(worker_steps.INITIATIVE, False, review)
    assert "handoffs" in worker_steps.due(worker_steps.INITIATIVE, False, review)


# --- discovery -----------------------------------------------------------------------------------------


def _discovery(client: Any) -> dict[str, Any]:
    doc = client.get("/v1/server").json()
    assert errors(doc, EXT_ID + "#/$defs/ServerDiscovery") == [] and discovery_problems(doc) == []
    assert delta_body_errors("get", "/v1/server", "200", doc) == []
    return doc


def test_discovery_lists_no_experimental_feature_by_default(client) -> None:  # type: ignore[no-untyped-def]
    ids = [f["id"] for f in _discovery(client)["features"]]
    assert not [i for i in ids if i.startswith("experimental.")]
    assert ids == [f["id"] for f in server_api.FEATURES]


@pytest.mark.parametrize("experimental_spec", ["google,bank,garmin"])
def test_discovery_lists_each_enabled_experimental_feature(client) -> None:  # type: ignore[no-untyped-def]
    features = _discovery(client)["features"]
    extra = [f for f in features if f["id"].startswith("experimental.")]
    # google is supported: never listed as experimental; the experimental ones in the registry's order
    assert extra == [{"id": "experimental.garmin", "version": 1}, {"id": "experimental.bank", "version": 1}]


@pytest.mark.parametrize("experimental_spec", ["all"])
def test_discovery_with_every_feature_on_stays_valid(client) -> None:  # type: ignore[no-untyped-def]
    extra = [f["id"] for f in _discovery(client)["features"] if f["id"].startswith("experimental.")]
    assert extra == [f"experimental.{f}" for f in experimental.listed(experimental.ALL)]
    assert len(extra) == len(experimental.EXPERIMENTAL)


@pytest.mark.parametrize("relay", [True, False])
def test_the_relay_is_offered_by_default_and_never_when_off(  # type: ignore[no-untyped-def]
    app_settings, db, fake_models, clock, relay, monkeypatch
) -> None:
    """push_relay is supported (inert without PISA_PUSH_RELAY_URL); forced off, the relay isn't offered."""
    from fastapi.testclient import TestClient

    from pisa.api.app import create_app
    from pisa.services.runtime import Runtime

    if not relay:
        force_off(monkeypatch, "push_relay")
    settings = app_settings.model_copy(
        update={"experimental": "", "push_relay_url": "https://relay.example.net"}
    )

    async def factory(s: Any) -> Any:
        return await Runtime.start(s, model_factory=fake_models, clock=clock)

    with TestClient(create_app(settings, runtime_factory=factory)) as client:
        doc = _discovery(client)
        ready = client.get("/readyz").json()
    assert doc["push"]["relayConfigured"] is relay and ("relay" in doc["push"]["modes"]) is relay
    assert ready["relay"] == ("on" if relay else "off")


# --- voices --------------------------------------------------------------------------------------------


def test_the_xai_talker_is_off_by_default(client) -> None:  # type: ignore[no-untyped-def]
    assert set(client.app.state.runtime.talk._talkers) == {"gemini"}  # the xAI talker is experimental


@pytest.mark.parametrize("forced_off", [("tts_server",)])
def test_the_server_s_voices_forced_off_leave_the_phone_speaking(client, device) -> None:  # type: ignore[no-untyped-def]
    _, token = device
    got = client.get("/v1/voice/voices?provider=openai", headers=auth(token))
    assert got.status_code == 200 and got.json() == {"voices": []}  # the phone speaks
    assert client.portal.call(client.app.state.runtime.voice.config) is None
    assert set(client.app.state.runtime.talk._talkers) == {"gemini"}  # the xAI talker is experimental


# --- pisa setup ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("relay", [True, False])
def test_setup_offers_the_relay_unless_it_is_off(app_settings, db, relay, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio
    import io

    from pisa.cli_setup import Prompt
    from pisa.cli_setup import run as run_setup
    from tests.conftest import dsn
    from tests.test_pair_cli import setup_args

    if not relay:
        force_off(monkeypatch, "push_relay")
    out = io.StringIO()
    settings = app_settings.model_copy(
        update={"database_url": dsn(db), "experimental": "", "push_relay_url": "https://relay.example.net"}
    )
    args = setup_args(lang="en", skip=["place,ai,talk,pair"], push="relay")
    assert asyncio.run(run_setup(args, settings, Prompt(False, out=out))) == 0
    text = out.getvalue()
    assert ("On this server: relay ready, direct" in text) is relay
    assert ("On this server: direct not set." in text) is not relay
    assert ("The relay is off on this server" in text) is not relay
    assert ("the Pisa relay" in text) is relay  # the step's own explanation names it only when offered


@REMINDERS_OFF
def test_with_reminders_off_a_reminder_is_never_said_as_set(
    client, device, db, secrets_key, fake_models
) -> None:  # type: ignore[no-untyped-def]
    """Reminders forced off: nothing would fire, so "Ti ricorderò domani alle 9" is held and
    the reply says plainly that reminders aren't available yet (the audit's honesty row in the default
    configuration)."""
    from tests.conftest import set_key
    from tests.test_abilities import _ask, _model

    device_id, token = device
    set_key(db, secrets_key)
    fake_models.default = _model([], "Va bene. Ti ricorderò domani alle 9 di chiamare Marco.")
    answer = _ask(client, token, device_id, "Ricordami domani alle 9 di chiamare Marco")
    assert "Ti ricorderò" not in answer
    assert "I promemoria non sono ancora disponibili su questo server." in answer


UNSENT = "Reminders aren't available on this server"


@pytest.mark.parametrize(
    ("sentence", "held"),
    [
        ("Ti ricorderò domani alle 9 di chiamare Marco.", True),
        ("Fatto: domani alle 9 ti ricordo di chiamare Marco.", True),
        ("Ti avviso alle 9.", True),
        ("Ho impostato un promemoria per domani.", True),
        ("Ti ricordo io.", True),
        ("Riceverai un promemoria alle 9.", True),
        ("I'll remind you tomorrow at 9.", True),
        ("I've set a reminder for 9.", True),
        ("I'll ping you at 9.", True),
        ("You'll get a reminder at 9.", True),
        ("I'll let you know at 9.", True),
        ("Ti ricordo che domani hai il dentista.", False),  # a reminder now, of what is already there
        ("L'ho messo nella lista per domani alle 9.", False),
        ("It's on your list for tomorrow at 9.", False),
    ],
)
def test_a_reminder_promise_is_held_only_while_reminders_are_off(sentence: str, held: bool) -> None:
    off = abilities.Facts(off=frozenset({"reminders"}))
    assert (abilities.overclaim(sentence, off) == "reminders") is held
    assert abilities.overclaim(sentence, abilities.Facts()) is None  # reminders on: as it was


def _keep(client: Any, words: str, *, reminders: bool, attend: bool = True, **kwargs: Any) -> str:
    """keep_intention's result, its tools built as a turn with reminders (and attend's step) on or off."""
    from pydantic_ai import Agent

    from pisa.brain.loop_tools import add_loop_tools
    from pisa.brain.profile_tools import ProfileTurn
    from pisa.brain.untrusted import watch_turn

    runtime = client.app.state.runtime
    agent: Agent[None, str] = Agent(None)
    add_loop_tools(
        agent, runtime.loops, ProfileTurn(words=words, lang="it-IT"), places=None, notes=None,
        today=lambda: runtime.owner.today(), reminders=reminders, attend=attend,
    )  # fmt: skip
    keep = next(t.function for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items()
                if n == "keep_intention")  # fmt: skip

    async def go() -> str:
        watch_turn()
        return str(await keep(**kwargs))

    return client.portal.call(go)


@REMINDERS_OFF
def test_with_reminders_off_a_time_cue_is_kept_on_the_list_and_its_result_says_no_reminder_comes(
    client, db
) -> None:  # type: ignore[no-untyped-def]
    """A time cue only fires through the reminders step: with it off the dated pool task is still kept (the
    plan, the list), and the result tells the model plainly that no reminder will come."""
    from tests.test_rituals import _settings

    _settings(db)
    words = "ricordami domani alle 9 di chiamare Marco"
    out = _keep(client, words, reminders=False, what="chiamare Marco", cue="time", when="domani alle 9")
    assert out.startswith("Kept (id ")
    (due,) = db.execute("SELECT t.due_at FROM intentions i JOIN tasks t ON t.id = i.task_id").fetchone()
    at = due.astimezone(client.app.state.runtime.owner.zone)
    assert f"{UNSENT}: it is on their list for {at:%Y-%m-%d %H:%M}, but Pisa won't send a reminder. " in out
    assert "Say so; never promise one." in out and at.hour == 9
    # with reminders on, the result is as it was
    on = _keep(client, "ricordami domani alle 10 di chiamare Anna", reminders=True, what="chiamare Anna",
               cue="time", when="domani alle 10")  # fmt: skip
    assert on.startswith("Kept (id ") and "this server" not in on
    assert on.endswith(" Say it back in a few words, from what is stored.")


@REMINDERS_OFF
def test_an_event_cue_attend_will_not_say_is_kept_and_said_as_no_reminder(client, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_rituals import _settings

    _settings(db)
    words = "quando arrivo a casa ricordami di stendere il bucato"
    out = _keep(client, words, reminders=False, attend=False, what="stendere il bucato", cue="event",
                event="arrive_home")  # fmt: skip
    assert out.startswith("Kept") and "nothing will remind them when it happens" in out
    on = _keep(client, "quando arrivo a casa ricordami di innaffiare le piante", reminders=False, attend=True,
               what="innaffiare le piante", cue="event", event="arrive_home")  # fmt: skip
    assert on.startswith("Kept") and "this server" not in on  # attend's step says it: as it was


def test_keep_intention_s_definition_promises_no_reminder_the_result_denies() -> None:
    """The definition the model sees is the same with reminders on and off (a default install never rewrites
    a tool), so it promises the cue's reminder only as far as the result says."""
    from pydantic_ai import Agent

    from pisa.brain.loop_tools import add_loop_tools
    from pisa.brain.profile_tools import ProfileTurn

    docs = []
    for on in (True, False):
        agent: Agent[None, str] = Agent(None)
        add_loop_tools(agent, None, ProfileTurn(words="", lang="it-IT"), places=None, notes=None,  # type: ignore[arg-type]
                       today=lambda: None, reminders=on, attend=on)  # type: ignore[arg-type,return-value]  # fmt: skip
        docs += [t.function.__doc__ for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items()
                 if n == "keep_intention"]  # fmt: skip
    assert docs[0] == docs[1] and "Pisa reminds them at the cue, unless the result says it won't." in docs[0]


@pytest.mark.parametrize(
    ("lang", "words", "what", "when", "reply", "line"),
    [
        (
            "it-IT",
            "Ricordami domani alle 9 di chiamare Marco",
            "chiamare Marco",
            "domani alle 9",
            "Va bene. Ti ricorderò domani alle 9 di chiamare Marco.",
            "ma non riceverai un promemoria: i promemoria non sono ancora disponibili su questo server.",
        ),
        (
            "it-IT",
            "Ricordami domani alle 9 di chiamare Marco",
            "chiamare Marco",
            "domani alle 9",
            "Fatto: domani alle 9 ti ricordo di chiamare Marco.",
            "ma non riceverai un promemoria: i promemoria non sono ancora disponibili su questo server.",
        ),
        (
            "en-US",
            "Remind me to call Marco tomorrow at 9",
            "call Marco",
            "tomorrow at 9",
            "Done. I'll remind you tomorrow at 9 to call Marco.",
            "but you won't get a reminder: reminders aren't available on this server yet.",
        ),
    ],
    ids=["it-ricordero", "it-ti-ricordo", "en"],
)
@pytest.mark.parametrize("by_finish", [False, True], ids=["text", "finish"])
@REMINDERS_OFF
def test_with_reminders_off_a_reminder_said_after_keep_intention_wrote_is_still_held(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words, what, when, reply, line, by_finish
) -> None:
    """keep_intention wrote this turn (the dated task is kept), but no step will send the reminder: the
    write backs no "Ti ricorderò domani alle 9", which is held, and the reply says reminders aren't
    available. The model read the result's own line first (the script follows it); said as text or with
    `finish`."""
    from tests.replay.scenarios import c, done
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    script = [
        [c("keep_intention", what=what, cue="time", when=when)],
        {
            "seen": UNSENT,
            "then": [done(reply)] if by_finish else reply,
            "else": "NO-LINE",
        },  # the result told it
    ]
    scenario = {
        "id": f"reminders-off-{lang}", "now": "2026-11-18T10:00", "lang": lang, "max_questions": 0,
        "world": {}, "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip
    turn = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]
    assert [name for name, _ in turn.calls] == ["keep_intention", *(["finish"] if by_finish else [])]
    assert db.execute("SELECT count(*) FROM intentions").fetchone() == (1,)  # kept, on their list
    assert "NO-LINE" not in turn.reply, turn.reply
    for promise in ("ricorder", "ti ricordo", "remind you"):
        assert promise not in turn.reply, turn.reply
    assert line in turn.reply, turn.reply


def test_with_reminders_on_a_reminder_after_keep_intention_is_said_as_written(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from tests.replay.scenarios import c
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    reply = "Va bene. Ti ricorderò domani alle 9 di chiamare Marco."
    script = [
        [c("keep_intention", what="chiamare Marco", cue="time", when="domani alle 9")],
        {"seen": UNSENT, "then": "LINE", "else": reply},  # no line: the reminder will fire
    ]
    words = "Ricordami domani alle 9 di chiamare Marco"
    scenario = {
        "id": "reminders-on", "now": "2026-11-18T10:00", "lang": "it-IT", "max_questions": 0, "world": {},
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip
    turn = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]
    assert turn.reply.strip() == reply, turn.reply
    assert "non sono ancora disponibili" not in turn.reply


@pytest.mark.parametrize(
    "sentence",
    [
        "Done. Or I can set a reminder before you leave.",
        "Or I can set a reminder to do it before you leave.",
        "I can remind you before Tuesday if you want.",
        "I can add a reminder for it.",
        "I could create a reminder too.",
        "Want me to remind you?",
        "Shall I remind you tomorrow?",
        "Would you like me to set a reminder?",
        "Posso ricordartelo domani.",
        "Posso ricordarti di comprare il latte.",
        "Vuoi che te lo ricordi?",
        "Se vuoi ti ricordo domani.",
        "Posso impostarti un promemoria.",
        "Posso mandarti un promemoria alle 9.",
        "Ti metto un promemoria?",
    ],
)
def test_a_reminder_offer_is_held_only_while_reminders_are_off(sentence: str) -> None:
    off = abilities.Facts(off=frozenset({"reminders"}))
    assert abilities.overclaim(sentence, off) in ("reminders", "reminder_offer")
    assert abilities.overclaim(sentence, abilities.Facts()) is None  # reminders on: as it was


@pytest.mark.parametrize(
    "sentence",
    [
        "Ti ricordo che domani hai il dentista.",  # a reminder now
        "Remind me what you need for Tuesday.",
        "It's on your list for tomorrow at 9.",
        "Ti ho messo in lista la chiamata a Marco.",
    ],
)
def test_words_that_offer_no_reminder_pass(sentence: str) -> None:
    assert abilities.overclaim(sentence, abilities.Facts(off=frozenset({"reminders"}))) is None


def test_the_kept_line_says_what_was_added_and_that_no_reminder_comes() -> None:
    assert abilities.kept_no_alert("chiamare Marco", "domani alle 9", "it-IT") == (
        "Ho aggiunto «chiamare Marco» per domani alle 9, ma non riceverai un promemoria: i promemoria non "
        "sono ancora disponibili su questo server."
    )
    assert abilities.kept_no_alert("call Marco", "tomorrow at 9 am", "en-US").startswith(
        "I've added «call Marco» for tomorrow at 9 am, but you won't get a reminder"
    )


class _Traces:
    """The conversation's logger, its `turn_trace` fields kept (the rest passed on)."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.traces = inner, []

    def debug(self, event: str, **kw: Any) -> None:
        if event == "turn_trace":
            self.traces.append(kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _play_off(client, db, fake_models, secrets_key, clock, http_handler, lang, words, script):  # type: ignore[no-untyped-def]
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    scenario = {
        "id": f"reminders-off-{lang}", "now": "2026-11-18T10:00", "lang": lang, "max_questions": 0,
        "world": {}, "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip
    return _play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]


@REMINDERS_OFF
def test_with_reminders_off_a_promise_after_a_dated_keep_says_what_was_added_and_no_lone_filler(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, monkeypatch
) -> None:
    """(a) the model's whole reply was the promise: the rail's line names what was kept and when, as
    people say it, and that no reminder comes; no promise, no lone "Dunque…". (c) turn_trace counts it."""
    from tests.replay.scenarios import c

    logs = _Traces(conversation_service._log)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(conversation_service, "_log", logs)
    script = [
        [c("keep_intention", what="chiamare Marco", cue="time", when="domani alle 9")],
        "Dunque… Ti ricorderò domani alle 9 di chiamare Marco.",
    ]
    turn = _play_off(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                     "Ricordami domani alle 9 di chiamare Marco", script)  # fmt: skip
    assert "Ti ricorderò" not in turn.reply, turn.reply
    assert "Ho aggiunto «chiamare Marco» per domani" in turn.reply and "alle 9" in turn.reply, turn.reply
    assert "non riceverai un promemoria" in turn.reply, turn.reply
    assert "Dunque" not in turn.reply, turn.reply
    assert logs.traces and logs.traces[-1]["held"] == {"reminders": 1}


REPLY_OFFER = (
    "Pack a rain jacket. I can remind you before Tuesday if you want. The forecast says rain on Monday."
)


@REMINDERS_OFF
def test_with_reminders_off_a_reminder_offer_is_dropped_and_nothing_said_in_its_place(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, monkeypatch
) -> None:
    """(b) an offer mid-reply ("I can remind you before Tuesday if you want.") goes; the rest stays; nothing
    is said in its place. (A closing "Or I can set a reminder…" already goes with the closing-offer rail.)"""
    logs = _Traces(conversation_service._log)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(conversation_service, "_log", logs)
    turn = _play_off(client, db, fake_models, secrets_key, clock, http_handler, "en-US",
                     "What should I pack for the trip?",
                     [REPLY_OFFER])  # fmt: skip
    assert "remind" not in turn.reply.lower(), turn.reply
    assert "rain jacket" in turn.reply and "rain on Monday" in turn.reply, turn.reply
    assert UNSENT not in turn.reply
    assert logs.traces and logs.traces[-1]["held"] == {"reminder_offer": 1}


def test_with_reminders_on_an_offer_is_said_as_written(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    turn = _play_off(client, db, fake_models, secrets_key, clock, http_handler, "en-US",
                     "What should I pack for the trip?",
                     [REPLY_OFFER])  # fmt: skip
    assert "I can remind you before Tuesday" in turn.reply, turn.reply


def test_the_prompt_says_never_offer_a_missing_feature_only_when_one_is_off() -> None:
    from pisa.brain.conversation import FEATURES_OFF_LINE, features_line

    assert features_line(frozenset()) == FEATURES_OFF_LINE.format(
        off="; ".join(experimental.off_labels(NONE))
    )
    assert "Garmin" in features_line(frozenset()) and "bank linking" in features_line(frozenset())
    assert features_line(experimental.ALL - {"reminders"}) == ""  # reminders are supported: on whatever
    assert features_line(experimental.ALL) == ""  # every feature on: the prompt byte-identical


# --- skills: guidance only for tools the turn has -----------------------------------------------------


def _skills_told(client: Any, db: Any, secrets_key: str, device: tuple[str, str], fake_models: Any,
                 words: str) -> str:  # fmt: skip
    """The instructions the model got on a turn with these words (the skills' block is in them)."""
    device_id, token = device
    set_key(db, secrets_key)
    told: list[str] = []

    async def model(messages: Any, info: Any) -> Any:
        told.append(str(getattr(messages[-1], "instructions", None) or ""))
        yield "Va bene."

    fake_models.default = model
    with connect(client, token) as ws:
        app = opened(ws, hello(device_id))
        turn = run(words)
        app.send(turn)
        app.run_ends(turn["input"]["runId"])
    assert told, "no model request"
    return told[0]


REMINDERS_SKILL = "### Reminders and Pisa's promises"
PLACE_NOTES_SKILL = "### A note for when he is at a place"


@pytest.mark.parametrize("forced_off", [("reminders", "rituals")])
def test_with_reminders_off_the_reminders_skill_is_not_injected(  # type: ignore[no-untyped-def]
    client, db, secrets_key, device, fake_models
) -> None:
    """Its body says "Te lo ricordo alle 6" with `remember_to`, never offered with reminders off: no
    guidance for a tool the turn hasn't (and a skill whose feature is off: rituals' place notes)."""
    told = _skills_told(
        client, db, secrets_key, device, fake_models, "Ricordami domani alle 9 di chiamare Marco"
    )
    assert "How Pisa usually does this" in told  # other skills still guide the turn
    assert REMINDERS_SKILL not in told and "Te lo ricordo alle 6" not in told
    told = _skills_told(client, db, secrets_key, device, fake_models,
                        "ricordami quando sono da Esselunga di comprare il latte")  # fmt: skip
    assert PLACE_NOTES_SKILL not in told


def test_by_default_the_reminders_skill_is_injected(  # type: ignore[no-untyped-def]
    client, db, secrets_key, device, fake_models
) -> None:
    told = _skills_told(
        client, db, secrets_key, device, fake_models, "Ricordami domani alle 9 di chiamare Marco"
    )
    assert REMINDERS_SKILL in told
    told = _skills_told(client, db, secrets_key, device, fake_models,
                        "ricordami quando sono da Esselunga di comprare il latte")  # fmt: skip
    assert PLACE_NOTES_SKILL in told


def test_a_skill_goes_only_when_every_tool_it_names_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """By the tools a default install registers: every built feature is on, so their skills stay; a feature
    forced off takes its skills with it (reminders, place notes, people, the vault…); with every feature on,
    every skill stays, as before."""
    from pisa.brain import skills
    from tests.test_tool_definitions_snapshot import definitions

    def registered(default: bool) -> set[str]:
        kind = "text" if default else "text_box_on"
        return {n.removeprefix("registered:") for n, _ in definitions(kind, monkeypatch, default=default)
                if n.startswith("registered:")}  # fmt: skip

    every = [(1.0, s) for s in skills.load()]
    assert skills.offered(every, registered(False)) == every  # all on: nothing goes
    by_default = {s.name for _, s in skills.offered(every, registered(True))}
    assert {"reminders-promises", "place-notes", "people", "vault", "tasks", "the-day"} <= by_default
    force_off(monkeypatch, "reminders", "rituals", "people", "handoffs", "autopilot")
    kept = {s.name for _, s in skills.offered(every, registered(True))}
    gone = {s.name for _, s in every} - kept
    assert {"reminders-promises", "place-notes", "people", "vault"} <= gone
    assert {"tasks", "the-day", "calendar-move"} <= kept  # the supported path keeps its guidance
    no_tools = skills.Skill("x", "X", "w", (), "reviewed", "body")
    one = dataclasses.replace(no_tools, name="y", tools=("remember_to", "add_task"))
    assert skills.offered([(1.0, no_tools), (1.0, one)], {"add_task"}) == [(1.0, no_tools), (1.0, one)]
    assert skills.offered([(1.0, one)], {"get_tasks"}) == []


GATED = (
    "learn",
    "memory",
    "money",
    "note",
    "people",
    "places",
    "profile",
    "public",
    "rituals",
    "trips",
    "views",
)


def test_an_experimental_feature_s_routes_do_not_exist_by_default(client, device) -> None:  # type: ignore[no-untyped-def]
    """E1 X-1 with the features that stay experimental: the radar's, the banks' and Garmin's routes answer
    404 on a default install, while the built features' routers answer."""
    _, token = device
    for path in ("/v1/obligations", "/v1/connectors/admin_pec", "/v1/connectors/bank",
                 "/v1/connectors/bank_3", "/v1/connectors/garmin"):  # fmt: skip
        got = client.get(path, headers=auth(token), follow_redirects=False)
        assert got.status_code == 404 and got.json()["error"]["code"] == "not_found", path
    assert client.get("/v1/profile/note", headers=auth(token)).status_code == 200  # a built feature's router


@pytest.mark.parametrize("forced_off", [GATED])
def test_an_off_feature_s_rest_routes_do_not_exist(client, device) -> None:  # type: ignore[no-untyped-def]
    """E1 X-1: every route of an off feature's router answers 404, as its tools and connectors are absent
    (learn's fetches and paid answers, rituals, money, trips, people, memory, profile, note, views, places,
    public: each built, so on by default, and forced off here). Walks the app's own routes, so a new route
    under one of these routers is covered too."""
    from fastapi.routing import APIRoute

    from pisa.api import (
        learn,
        memory,
        money,
        note,
        people,
        places,
        profile,
        public,
        rituals,
        trips,
        views,
    )

    _, token = device
    modules = (learn, memory, money, note, people, places, profile, public, rituals, trips, views)
    gated = [r for m in modules for r in m.router.routes if isinstance(r, APIRoute)]
    assert len(gated) > 20, len(gated)  # every route of the gated routers
    for route in gated:
        path = route.path.replace("{", "x").replace("}", "")  # any value: the gate answers before the route
        method = sorted((route.methods or {"GET"}) - {"HEAD"})[0]
        answer = client.request(method, path, headers={"Authorization": f"Bearer {token}"}, json={})
        assert answer.status_code == 404, (method, route.path, answer.status_code, answer.text[:120])
        assert answer.json()["error"]["code"] == "not_found", (method, route.path)
