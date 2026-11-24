"""The first real-model evaluation's three bugs, each in
English and Italian, with scripted models only:

- M7: Pisa says what is or isn't on their lists ("already there", "not on your list", "nothing matches", "già
  fatto") only after reading them this turn; from memory, the sentence waits for one read round;
- M8: with Google not connected, its tools answer "Google is not connected", never an empty "nothing found";
- M9: "stop everything" / "ferma tutto" stops Pisa by code, whatever the model would have answered;
- S11: a plain timed reminder kept as an intention asks no "first concrete step"."""

from typing import Any, cast

import pytest

from pisa.brain.receipts import Unconnected
from pisa.domain import claims
from pisa.domain import connectors as connector_rules
from pisa.domain import intentions as intention_rules
from pisa.domain import stop as stop_rules
from pisa.services.conversation import READ_LINE
from tests.replay.scenarios import c

N = "2026-11-18T10:00"
READ_HINT = READ_LINE.split(":")[0].strip()  # the read round's own words in the request's instructions


def _scenario(lang: str, words: str, world: dict[str, Any], script: list[Any], sid: str) -> dict[str, Any]:
    return {
        "id": sid, "now": N, "lang": lang, "max_questions": 0, "world": world,
        "turns": [{"owner": words}], "script": [script], "expect": {},
    }  # fmt: skip


def _turn(client: Any, db: Any, fake_models: Any, secrets_key: str, clock: Any, http_handler: Any,
          scenario: dict[str, Any]) -> Any:  # fmt: skip
    from tests.test_replay import _play  # pyright: ignore[reportPrivateUsage]

    result = _play(client, db, fake_models, secrets_key, clock, http_handler, scenario)
    return result.turns[-1]


# --- M7: read before you state ------------------------------------------------------------------------------

STATES = [
    ("en-US", "Nothing on your list matches milk, bread and eggs, so I haven't ticked anything off."),
    ("en-US", "Milk isn't on your list."),
    ("en-US", "It's already on your list."),
    ("en-US", "That's already done."),
    ("en-US", "There's no task called milk."),
    ("it-IT", "Il passaporto è già in lista, non l'ho aggiunto una seconda volta."),
    ("it-IT", "«Chiamare la banca» è già in lista per domani."),
    ("it-IT", "Ce l'ho già: domani alle 9 ti ricordo di chiamare Marco."),
    ("it-IT", "Niente nella tua lista corrisponde a latte, pane e uova."),
    ("it-IT", "Il latte non c'è nella tua lista."),
    ("it-IT", "È già segnato come fatto."),
]
NOT_STATES = [
    "It's already 9 pm.", "The meeting has already started.", "Is milk on your list?",
    "È già tardi.", "Ho aggiunto il latte.", "Il latte è nella tua lista?",
    "Nothing nearby matches what you asked.", "Nessun risultato corrisponde alla ricerca.",
]  # fmt: skip


@pytest.mark.parametrize(("lang", "sentence"), STATES)
def test_m7_a_list_stated_without_a_read_waits_for_one(lang: str, sentence: str) -> None:
    assert claims.unread_state(sentence, read=False) is not None, sentence
    assert claims.unread_state(sentence, read=True) is None  # read this turn: said as it is


@pytest.mark.parametrize("sentence", NOT_STATES)
def test_m7_other_sentences_need_no_read(sentence: str) -> None:
    assert claims.unread_state(sentence, read=False) is None, sentence


def test_m7_the_reads_are_the_tools_that_read_the_lists() -> None:
    assert {"get_tasks", "complete_task", "my_intentions"} <= claims.STATE_READS
    assert "add_task" not in claims.STATE_READS and "web_search" not in claims.STATE_READS


# (lang, their words, the open task, the model's answer from memory, its answer after reading)
OPEN_ITEM = [
    ("en-US", "I bought milk", "Buy milk",
     "Nothing on your list matches milk, so I haven't ticked anything off.",
     "Buy milk is still open on your list: shall I tick it off?"),
    ("it-IT", "Ho comprato il latte", "Comprare il latte",
     "Niente nella tua lista corrisponde al latte, quindi non ho spuntato niente.",
     "«Comprare il latte» è ancora aperto: lo segno come fatto?"),
]  # fmt: skip


@pytest.mark.parametrize("case", OPEN_ITEM, ids=[k[0] for k in OPEN_ITEM])
def test_m7_an_open_item_is_never_said_missing(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    """The transcript (f2-en-2, f2-it-1): with "Buy milk" open, the model says "Nothing on your list matches
    milk" with no tool call. The sentence is never said: the model is sent back once to read the list, and its
    answer from the list is said."""
    lang, words, task, memory, after = case
    script = [{"hint": READ_HINT, "then": [c("get_tasks")], "else": memory}, after]
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": [task]}, script, f"m7-open-{lang}"))  # fmt: skip
    assert turn.reply.strip() == after, turn.reply
    assert memory not in turn.reply
    assert [name for name, _ in turn.calls] == ["get_tasks"], turn.calls
    assert db.execute("SELECT status FROM tasks WHERE title = %s", (task,)).fetchone() == ("open",)


@pytest.mark.parametrize(("lang", "memory"), [
    ("en-US", "Nothing on your list matches milk, so I haven't ticked anything off."),
    ("it-IT", "Il passaporto è già in lista, non l'ho aggiunto una seconda volta."),
])  # fmt: skip
def test_m7_a_model_that_never_reads_is_caught(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, memory
) -> None:
    """A scripted model that says "not on your list" (or "già in lista") with no read, and says it again in
    the read round without reading: the claim is never said; Pisa says it hasn't checked."""
    words = "I bought milk" if lang == "en-US" else "Aggiungi il passaporto"
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": ["Buy milk"]}, [memory], f"m7-never-{lang}"))  # fmt: skip
    assert memory not in turn.reply, turn.reply
    assert turn.reply.strip() == claims.unread_truth(lang), turn.reply
    assert turn.calls == []


@pytest.mark.parametrize(("lang", "words", "said"), [
    ("en-US", "Did I put oat milk on my list?", "Oat milk isn't on your list."),
    ("it-IT", "Ho messo il latte di avena in lista?", "Il latte di avena non c'è nella tua lista."),
])  # fmt: skip
def test_m7_after_a_read_the_same_sentence_is_said(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words, said
) -> None:
    script = [[c("get_tasks")], said]
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {"tasks": ["Buy bread"]}, script, f"m7-read-{lang}"))  # fmt: skip
    assert turn.reply.strip() == said, turn.reply
    assert [name for name, _ in turn.calls] == ["get_tasks"]


# --- M8: Google not connected is never "nothing found" ---------------------------------------------------


def test_m8_every_google_tool_says_not_connected_while_google_waits() -> None:
    waiting = connector_rules.tools_unconnected({"google": "needs_owner"})
    assert set(waiting) == connector_rules.GOOGLE_TOOLS | {"show_email"}
    assert set(waiting.values()) == {"Google"}
    assert connector_rules.tools_unconnected({"google": "on"}) == {}
    assert connector_rules.tools_unconnected({"google": "error"}) == {}  # connected, a sync failed: it reads
    assert connector_rules.tools_unconnected({"google": "off"}) == {}  # off: never offered (tools_off)


@pytest.mark.parametrize("tool", sorted(connector_rules.GOOGLE_TOOLS | {"show_email"}))
def test_m8_the_tool_layer_answers_not_connected_and_runs_nothing(tool: str) -> None:
    import asyncio

    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.tools import ToolDefinition

    ran: list[Any] = []

    async def handler(args: Any) -> str:
        ran.append(args)
        return "No email in the last 30 days matches 'Tom'."

    async def call(unconnected: dict[str, str]) -> Any:
        cap = Unconnected(lambda: unconnected)
        return await cap.wrap_tool_execute(
            cast(Any, None),
            call=ToolCallPart(tool, {}),
            tool_def=ToolDefinition(name=tool),
            args={},
            handler=handler,
        )

    said = asyncio.run(call(connector_rules.tools_unconnected({"google": "needs_owner"})))
    assert "Google is not connected" in said and "never that nothing was found" in said and ran == []
    assert asyncio.run(call({})) == "No email in the last 30 days matches 'Tom'." and ran == [{}]


# (lang, their words, the model's answer when the result says not connected, else)
TOM = [
    ("en-US", "What's Tom's email address?", "Google isn't connected, so I can't look in your mail.",
     "I can't find an email from Tom in the last 30 days, so I don't have his address."),
    ("it-IT", "Qual è l'indirizzo email di Tom?",
     "Google non è collegato, quindi non posso guardare la posta.",
     "Non trovo email di Tom negli ultimi 30 giorni, quindi non ho il suo indirizzo."),
]  # fmt: skip


@pytest.mark.parametrize("case", TOM, ids=[k[0] for k in TOM])
def test_m8_e2e_mail_with_google_not_connected_says_so(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    """The transcript (f6-en-2, f6-it-2): Google never connected, search_my_email answered "no email matches"
    and the model said "I can't find an email from Tom". Through Pisa's own turn, the tool's result now says
    Google is not connected (the scripted model follows what it sees)."""
    lang, words, connected_line, nothing_found = case
    step = {"seen": "Google is not connected", "then": connected_line, "else": nothing_found}
    script = [[c("search_my_email", query="Tom")], step]
    world = {"google_not_connected": True, "emails": []}
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, world, script, f"m8-{lang}"))  # fmt: skip
    assert turn.reply.strip() == connected_line, turn.reply


def test_m8_e2e_with_google_connected_the_search_runs(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler
) -> None:
    lang, words, connected_line, nothing_found = TOM[0]
    step = {"seen": "Google is not connected", "then": connected_line, "else": nothing_found}
    script = [[c("search_my_email", query="Tom")], step]
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {}, script, "m8-connected"))  # fmt: skip
    assert turn.reply.strip() == nothing_found, turn.reply


# --- M9: "stop everything" always stops ------------------------------------------------------------------

# (lang, their words, what the small model said in the transcript: no stop_everything call)
STOPS = [
    ("en-US", "Stop everything", [[c("search_tools", queries=["stop"])],
                                  "I can't reach the stop-everything switch from here."]),
    ("en-US", "Pisa, stop everything you're doing", ["Sure."]),
    ("it-IT", "Pisa, ferma tutto", [[c("search_tools", queries=["ferma"])],
                                    "Non riesco a fermare tutto da qui."]),
    ("it-IT", "ferma tutto", ["Va bene."]),
]  # fmt: skip


@pytest.mark.parametrize("case", STOPS, ids=[f"{k[0]}-{k[1]}" for k in STOPS])
def test_m9_the_phrase_stops_pisa_whatever_the_model_would_say(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, case
) -> None:
    lang, words, script = case
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {}, script, f"m9-{lang}"))  # fmt: skip
    assert turn.reply.strip() == stop_rules.stopped_line(lang), turn.reply
    assert turn.by_code and turn.calls == []  # no model round at all
    assert db.execute("SELECT count(*) FROM pisa_stops WHERE resumed_at IS NULL").fetchone() == (1,)
    said = db.execute("SELECT role, content FROM messages ORDER BY created_at, role DESC").fetchall()
    assert said[-2:] == [("user", words), ("assistant", stop_rules.stopped_line(lang))]  # the next turn knows


@pytest.mark.parametrize(
    ("lang", "words"),
    [("en-US", "Don't stop everything"), ("it-IT", "non fermare tutto"),
     ("en-US", "How do I stop everything?"), ("it-IT", "Come faccio a fermare tutto?")],
)  # fmt: skip
def test_m9_a_negated_stop_or_a_question_goes_to_the_model(  # type: ignore[no-untyped-def]
    client, db, fake_models, secrets_key, clock, http_handler, lang, words
) -> None:
    turn = _turn(client, db, fake_models, secrets_key, clock, http_handler,
                 _scenario(lang, words, {}, ["Ok."], f"m9-not-{lang}"))  # fmt: skip
    assert turn.reply.strip() == "Ok." and not turn.by_code
    assert db.execute("SELECT count(*) FROM pisa_stops").fetchone() == (0,)


def test_m9_stop_everything_is_a_core_tool_with_a_clear_description() -> None:
    import inspect

    from pisa.brain import autopilot_tools, tool_search

    assert "stop_everything" in tool_search.ALWAYS and "stop_everything" in tool_search.VOICE_ALWAYS
    source = inspect.getsource(autopilot_tools.add_autopilot_tools)
    assert "never\n        say the stop isn't available" in source


# --- S11: a plain timed reminder asks no first step ------------------------------------------------------


def test_s11_a_timed_reminder_asks_no_first_step() -> None:
    assert intention_rules.missing("chiamare Anna", None, timed=True) is None
    assert intention_rules.missing("call Anna", None, timed=True) is None
    assert intention_rules.missing("rinnovare il passaporto", None) == "next_step"  # no time: still asked
    assert intention_rules.missing("quella cosa", None, timed=True) == "what"  # a vague what is still asked


@pytest.mark.parametrize(
    ("lang", "words", "what", "when"),
    [
        ("it-IT", "ricordami domani alle 19 di chiamare Anna", "chiamare Anna", "domani alle 19"),
        ("en-US", "remind me to call Anna tomorrow at 7 pm", "call Anna", "tomorrow at 19:00"),
    ],
)
def test_s11_keep_intention_with_a_time_asks_nothing(client, db, lang, words, what, when) -> None:  # type: ignore[no-untyped-def]
    """The transcript (f5-en-1, f5-it-1): "I'll remind you tomorrow at 7 pm to call Anna" came with "What's
    the first concrete step?". A timed reminder is kept with no question."""
    from pydantic_ai import Agent

    from pisa.brain.loop_tools import add_loop_tools
    from pisa.brain.profile_tools import ProfileTurn
    from tests.test_intentions import call, rt
    from tests.test_rituals import _settings  # pyright: ignore[reportPrivateUsage]

    _settings(db)
    turn = ProfileTurn(words=words, lang=lang)
    agent: Agent[None, str] = Agent(None)
    loops = rt(client).loops
    add_loop_tools(agent, loops, turn, places=None, notes=None, today=lambda: rt(client).owner.today())
    found = {n: t.function for ts in agent.toolsets for n, t in getattr(ts, "tools", {}).items()}
    out = call(client, found["keep_intention"], what=what, cue="time", when=when)
    assert out.startswith("Kept") and turn.asked == [], out
    assert intention_rules.question("next_step", lang) not in out
    assert db.execute("SELECT asked FROM intentions").fetchone() == (None,)
