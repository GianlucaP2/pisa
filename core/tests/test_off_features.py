"""An off feature (`domain.experimental`: on a default install garmin, polar, withings, bank, ade, radar,
talker_xai) has one honest answer. The quality audit asked "collega il mio conto in banca" / "link my bank
account" on a default install: in 5 of 6 reps the model called show_setting, then sent them to Settings
("…sezione 'Collegamenti' nelle impostazioni, dove puoi scegliere la tua banca", "The connectors page in
Settings is where you'd add it yourself", "Tell me which bank you use and I'll walk you through the steps").
Now show_setting shows nothing for it and says there is nothing to connect, the rail holds a sentence sending
them to Settings for it or offering steps, and the reply says "<label> isn't available on PISA." once; the
prompt names what is off; search_tools' off answer uses the same wording."""

from typing import Any

import pytest

from pisa.brain import tool_search
from pisa.brain.conversation import FEATURES_OFF_LINE, features_line
from pisa.domain import abilities, experimental
from pisa.services import conversation as conversation_service

NONE = frozenset[str]()
OFF = frozenset(f.id for f in experimental.REGISTRY if not experimental.on(f.id, NONE))

# the audit's own sentences
COLLEGAMENTI = (
    "Per collegarlo vai alla sezione 'Collegamenti' nelle impostazioni, dove puoi scegliere la tua banca."
)
CONNECTORS_PAGE = "The connectors page in Settings is where you'd add it yourself."
WALK = "Tell me which bank you use and I'll walk you through the steps."


@pytest.fixture
def experimental_spec() -> str:
    """PISA_EXPERIMENTAL for this test's server: none (the default) unless a test parametrizes it."""
    return ""


@pytest.fixture
def app_settings(app_settings: Any, experimental_spec: str) -> Any:
    return app_settings.model_copy(update={"experimental": experimental_spec})


# --- the one wording ---------------------------------------------------------------------------------------


def test_every_off_feature_has_one_line_in_both_languages() -> None:
    assert experimental.unavailable("bank") == "Bank linking isn't available on PISA."
    assert (
        experimental.unavailable("bank", "it-IT")
        == "Il collegamento al conto in banca non è disponibile su PISA."
    )
    assert experimental.unavailable("garmin", "it") == "Garmin non è disponibile su PISA."
    for feature in OFF:
        assert experimental.label(feature) and experimental.label(feature, "it")


def test_search_tools_off_answer_uses_the_same_line() -> None:
    label = tool_search.searched_off(["link bank account"], NONE, found=False)
    assert label == "bank linking"
    assert tool_search.searched_off(["collega banca"], NONE, found=True) == "bank linking"
    assert tool_search.searched_off(["garmin sleep"], NONE, found=False) == "Garmin"
    message = tool_search.off_message(label, again=False)
    assert message.startswith(f"{tool_search.OFF_MARK} Bank linking isn't available on PISA (switched off")


def test_the_prompt_names_what_is_off_and_stays_unchanged_with_every_feature_on() -> None:
    line = features_line(NONE)
    assert line == (
        "\nNot on this server: Garmin; bank linking; PEC and admin mail linking; the cassetto fiscale "
        "(Agenzia delle Entrate); Polar; Withings; the xAI voice. Never offer them, and never send them to "
        "Settings for them: say in one line that they aren't available on PISA."
    )
    assert line == FEATURES_OFF_LINE.format(off="; ".join(experimental.off_labels(NONE)))
    assert features_line(experimental.ALL) == ""  # every feature on: the prompt byte-identical
    assert "bank linking" not in features_line(frozenset({"bank"}))


# --- show_setting's check ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "said", "words", "off"),
    [
        ("connectors", ["", "collegare la banca"], "collega il mio conto in banca", "bank"),
        ("connectors", [None, ""], "link my bank account", "bank"),
        ("connectors", ["bank", ""], "", "bank"),
        ("connectors", ["garmin", ""], "collega il mio Garmin", "garmin"),
        ("connectors", ["ade_cassetto", ""], "", "ade"),
        ("connectors", ["", "la PEC"], "", "radar"),
        ("connectors", ["", ""], "collega il cassetto fiscale", "ade"),
        ("connectors", ["google", "collegare Google"], "collega Google e la banca", None),  # Google is on
        ("connectors", ["", ""], "collega Google", None),
        ("voice", ["speak_briefing_on_phone", ""], "collega il mio conto in banca", None),  # another page
        ("briefing", ["", ""], "", None),
    ],
)
def test_show_setting_is_refused_for_an_off_feature(
    section: str, said: list[str | None], words: str, off: str | None
) -> None:
    got = tool_search.setting_off(section, [x or "" for x in said], words, NONE)
    assert got == off
    if off is not None:  # with it on, shown as ever
        assert tool_search.setting_off(section, [x or "" for x in said], words, frozenset({off})) is None


def test_the_refusal_says_there_is_nothing_to_connect() -> None:
    assert tool_search.setting_refused("bank") == (
        "Not shown: Bank linking isn't available on PISA: there is nothing to connect. Say so in one line; "
        "don't send them to Settings and don't offer steps."
    )


# --- RAIL: sending them to Settings for an off feature ----------------------------------------------------


def _facts(*asked: str, unavailable: frozenset[str] = OFF) -> abilities.Facts:
    return abilities.Facts(unavailable=unavailable, asked_off=asked)


@pytest.mark.parametrize(
    ("sentence", "asked", "held"),
    [
        (COLLEGAMENTI, ("bank",), "bank"),
        (COLLEGAMENTI, (), "bank"),  # it names the bank itself
        (CONNECTORS_PAGE, ("bank",), "bank"),
        (WALK, ("bank",), "bank"),
        (WALK, (), "bank"),
        ("Se mi dici qual è la tua banca, ti guido passo passo.", ("bank",), "bank"),
        ("Your bank isn't connected yet: connect it in Settings, Connectors.", (), "bank"),
        ("Posso leggere i movimenti della tua banca.", (), "bank"),
        ("Lo trovi in Impostazioni, Collegamenti: tocca Garmin e accedi.", ("garmin",), "garmin"),
        ("Open Settings, Connectors and pick Garmin.", (), "garmin"),
        ("I'll walk you through linking your Garmin.", (), "garmin"),
        ("Puoi collegare il cassetto fiscale dalle impostazioni.", (), "ade"),
    ],
)
def test_a_sentence_sending_them_to_settings_for_an_off_feature_is_held(
    sentence: str, asked: tuple[str, ...], held: str
) -> None:
    assert abilities.overclaim(sentence, _facts(*asked)) == f"off:{held}"
    # with that feature on, the sentence is said as written
    assert abilities.overclaim(sentence, _facts(*asked, unavailable=OFF - {held})) is None


@pytest.mark.parametrize(
    ("sentence", "asked"),
    [
        ("Bank linking isn't available on PISA.", ("bank",)),
        ("Il collegamento al conto in banca non è disponibile su PISA.", ("bank",)),
        ("Bank links are switched off on this install.", ("bank",)),
        ("Garmin non è disponibile su PISA, quindi non c'è nulla da collegare.", ("garmin",)),
        ("Ti ricordo di passare in banca domani alle 9.", ()),  # names the bank, sends nowhere
        ("Ho segnato la spesa di 30 euro.", ("bank",)),
        ("Connect Google in Settings, Connectors.", ()),  # a supported connector
        ("Google si collega in Impostazioni, Connettori.", ("bank",)),  # names only what is on
        ("Vai in Impostazioni per cambiare la voce.", ()),  # nothing off named, nothing off asked
        ("I can't read your bank account.", ()),
    ],
)
def test_honest_or_unrelated_sentences_pass(sentence: str, asked: tuple[str, ...]) -> None:
    assert abilities.overclaim(sentence, _facts(*asked)) is None


def test_the_line_is_said_once_per_feature() -> None:
    claimed = ["off:bank", "off:bank", "mail", "off:garmin"]
    assert abilities.unavailable_lines(claimed, "it-IT") == (
        "Il collegamento al conto in banca non è disponibile su PISA. Garmin non è disponibile su PISA."
    )
    said = "Bank linking isn't available on PISA."
    assert abilities.unavailable_lines(["off:bank"], "en-US", said=said) == ""


def test_what_can_you_do_about_an_off_feature_says_it_isnt_on_pisa() -> None:
    facts = abilities.Facts(off=abilities.hidden(lambda f: experimental.on(f, NONE)), asked_off=("bank",))
    said = abilities.answer(facts, "it-IT")
    assert "Il collegamento al conto in banca non è disponibile su PISA." in said
    assert "banca" not in abilities.not_connected(facts, "it-IT")  # never "not connected yet"


# --- through Pisa: a scripted model ---------------------------------------------------------------------


class _Traces:
    """The conversation's logger, its `turn_trace` fields kept (the rest passed on)."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.traces = inner, []

    def debug(self, event: str, **kw: Any) -> None:
        if event == "turn_trace":
            self.traces.append(kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


REFUSED = "isn't available on PISA: there is nothing to connect"


def _play(client, db, fake_models, secrets_key, clock, http_handler, lang, words, script):  # type: ignore[no-untyped-def]
    from tests.test_replay import _play as play  # pyright: ignore[reportPrivateUsage]

    scenario = {
        "id": f"off-feature-{lang}", "now": "2026-11-18T10:00", "lang": lang, "max_questions": 0,
        "world": {}, "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip
    return play(client, db, fake_models, secrets_key, clock, http_handler, scenario).turns[-1]


def _refused_then(false_reply: str) -> list[Any]:
    """show_setting first; the model then says the audit's false sentence when the tool refused it (the rail
    must hold it), and "SHOWN" when it was shown (the test fails)."""
    from tests.replay.scenarios import c

    return [
        [c("show_setting", section="connectors", purpose="collegare la banca")],
        {"seen": REFUSED, "then": false_reply, "else": "SHOWN"},
    ]


@pytest.mark.parametrize(
    ("lang", "words", "false_reply", "line"),
    [
        ("it-IT", "collega il mio conto in banca", f"Certo! {COLLEGAMENTI}",
         "Il collegamento al conto in banca non è disponibile su PISA."),
        ("en-US", "link my bank account", f"Sure. {CONNECTORS_PAGE} {WALK}",
         "Bank linking isn't available on PISA."),
    ],
)  # fmt: skip
def test_link_my_bank_on_a_default_install_is_answered_honestly(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, monkeypatch, lang, words, false_reply, line
) -> None:
    logs = _Traces(conversation_service._log)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(conversation_service, "_log", logs)
    turn = _play(
        client, db, fake_models, secrets_key, clock, http_handler, lang, words, _refused_then(false_reply)
    )
    assert "SHOWN" not in turn.reply, turn.reply  # show_setting was refused
    for false in ("Collegamenti", "impostazioni", "connectors page", "walk you through", "Tell me which"):
        assert false not in turn.reply, turn.reply
    assert turn.reply.count(line) == 1, turn.reply
    assert logs.traces and set(logs.traces[-1]["held"]) == {"off:bank"}


def test_link_my_garmin_on_a_default_install_is_answered_honestly(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from tests.replay.scenarios import c

    script = [
        [c("show_setting", section="connectors", highlight="garmin")],
        {
            "seen": REFUSED,
            "then": "Lo trovi in Impostazioni, Collegamenti: tocca Garmin e accedi.",
            "else": "SHOWN",
        },
    ]
    turn = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT", "collega il mio Garmin",
                 script)  # fmt: skip
    assert "SHOWN" not in turn.reply and "Collegamenti" not in turn.reply, turn.reply
    assert turn.reply.count("Garmin non è disponibile su PISA.") == 1, turn.reply


@pytest.mark.parametrize("experimental_spec", ["bank"])
def test_with_the_banks_on_show_setting_works_as_before(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    from tests.replay.scenarios import c

    script = [
        [c("show_setting", section="connectors", purpose="collegare la banca")],
        {"seen": REFUSED, "then": "REFUSED", "else": "Ecco: {last}"},
    ]
    turn = _play(client, db, fake_models, secrets_key, clock, http_handler, "it-IT",
                 "collega il mio conto in banca", script)  # fmt: skip
    assert "REFUSED" not in turn.reply and "non è disponibile su PISA" not in turn.reply, turn.reply
    assert turn.reply.startswith("Ecco:"), turn.reply


def test_app_help_on_a_turn_about_an_off_feature_says_it_isnt_on_pisa() -> None:
    import asyncio
    from datetime import UTC, datetime

    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from pisa.brain.conversation import TurnContext, build_agent
    from pisa.brain.profile_tools import ProfileTurn
    from tests.owner import owner_at

    returns: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns[:] = [
            str(p.content) for m in messages for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart) and p.tool_name == "app_help"
        ]  # fmt: skip
        if not returns:
            return ModelResponse(parts=[ToolCallPart("app_help", {})])
        return ModelResponse(parts=[TextPart("Fatto.")])

    words = "come collego la mia banca nell'app?"
    for enabled, said in ((NONE, True), (frozenset({"bank"}), False)):
        context = TurnContext(owner=owner_at(), clock=lambda: datetime.now(UTC), enabled=enabled,
                              profile_turn=ProfileTurn(words=words, lang="it-IT"))  # fmt: skip
        asyncio.run(build_agent(FunctionModel(model), context).run(words))
        assert ("Bank linking isn't available on PISA." in returns[0]) is said, returns
