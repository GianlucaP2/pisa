"""Part 1: Pisa's loop and the truth, through Pisa's real ConversationService with a fake Google
and a scripted model. The replay's scenarios (research/brain/replay: the night, the range, "late, fix my
afternoon") scored on the end state: what exists and at what time, no false claim, the questions asked. Then
the rows, each with a test that fails without its fix (revert-to-fail: the handoff's table)."""

import json
from datetime import date, datetime
from typing import Any

import pytest
from structlog.testing import capture_logs

from pisa.domain.actions import Change, answers_proposal, grounding, owner_said
from pisa.domain.claims import claim
from tests.assistant_env import rome
from tests.test_acting import spoken

Round = list[tuple[str, dict[str, Any]]] | str


class Script:
    """A conversation model scripted per user's words: each round its tool calls (or its words); past the
    script it says what its results say (`spoken`). It counts its calls and keeps each request's
    instructions and an estimate of its input tokens (about 4 characters each)."""

    def __init__(self, turns: dict[str, list[Round]]) -> None:
        self.turns, self.calls, self.instructions, self.sent = turns, 0, [], []
        self.messages: list[Any] = []

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        from pydantic_ai.messages import ToolReturnPart, UserPromptPart
        from pydantic_ai.models.function import DeltaToolCall

        self.calls += 1
        self.messages.append(messages)
        self.instructions.append(info.instructions or "")
        tools = json.dumps([t.parameters_json_schema | {"d": t.description} for t in info.function_tools])
        self.sent.append((len(info.instructions or "") + len(tools) + len(str(messages))) // 4)
        at = max(i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts))
        words = [str(p.content) for p in messages[at].parts if isinstance(p, UserPromptPart)][-1]
        rounds = sum(1 for m in messages[at + 1 :] if any(isinstance(p, ToolReturnPart) for p in m.parts))
        script = self.turns.get(words, [])
        step = script[rounds] if rounds < len(script) else None
        if isinstance(step, list):
            yield {
                n: DeltaToolCall(name=name, json_args=json.dumps(args)) for n, (name, args) in enumerate(step)
            }
            return
        if isinstance(step, str):
            yield step
            return
        returns = [str(p.content) for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
        yield spoken(returns) if returns else "Va bene."


@pytest.fixture
def calendar(http_handler, db, secrets_key) -> Any:  # type: ignore[no-untyped-def]
    from tests.actions_fake import ActionsGoogle, connect
    from tests.assistant_env import set_settings

    fake = ActionsGoogle()
    http_handler["handler"] = fake
    set_settings(db, {"language": "it-IT"})
    connect(db, secrets_key)
    return fake


def said(frames: list[dict[str, Any]]) -> str:
    return "".join(f.get("delta") or "" for f in frames if f["type"] == "say")


def end_state(google: Any) -> dict[str, str]:
    """What exists in the calendar at the end, and at what time (the replay's scorer)."""
    live = [e for e in google.events.values() if e.get("status") != "cancelled" and e.get("summary")]
    return {e["summary"]: e["start"]["dateTime"][:16] for e in live}


def play(client, db, secrets_key, model, *words, device=None, clock=None, gaps=None):  # type: ignore[no-untyped-def]
    """The user's turns over one socket; the frames of each, the logs, and the turns' measures."""
    from tests.test_acting import socket_turns

    with capture_logs() as logs:
        frames = socket_turns(client, db, secrets_key, model, *words, device=device)
    latency = [e for e in logs if e["event"] == "turn_latency"]
    return frames, logs, latency


def cached(db, google, event_id: str) -> None:  # type: ignore[no-untyped-def]
    from tests.actions_fake import cached_event

    cached_event(db, google.events[event_id])


# --- 9. The replay's scenarios, scored on the end state -----------------------------------------------------

NIGHT = rome(2026, 10, 30, 2, 38)  # venerdì 30 ottobre, 02:38
NIGHT_COMMAND = (
    "metti tutto sul calendario: domani palestra alle 10, pranzo con Nino alle 13 e dentista alle 17"
)


def test_the_night_three_events_on_the_30th_then_saturday_then_the_dentist_at_18(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = NIGHT
    model = Script(
        {
            NIGHT_COMMAND: [
                [
                    (
                        "create_event",
                        {
                            "title": "Palestra",
                            "start": "domani alle 10",
                            "owner_said": "domani palestra alle 10",
                        },
                    ),
                    (
                        "create_event",
                        {
                            "title": "Pranzo con Nino",
                            "start": "domani alle 13",
                            "owner_said": "pranzo con Nino alle 13",
                        },
                    ),
                    (
                        "create_event",
                        {"title": "Dentista", "start": "domani alle 17", "owner_said": "dentista alle 17"},
                    ),
                ],
            ],
            "no aspetta, intendevo sabato": [
                [
                    (
                        "move_event",
                        {"event": "palestra", "start": "sabato alle 10", "owner_said": "intendevo sabato"},
                    ),
                    (
                        "move_event",
                        {
                            "event": "pranzo con Nino",
                            "start": "sabato alle 13",
                            "owner_said": "intendevo sabato",
                        },
                    ),
                    (
                        "move_event",
                        {"event": "dentista", "start": "sabato alle 17", "owner_said": "intendevo sabato"},
                    ),
                ],
            ],
            "il dentista spostalo alle 18": [
                [
                    (
                        "move_event",
                        {
                            "event": "dentista",
                            "start": "sabato alle 18",
                            "owner_said": "il dentista spostalo alle 18",
                        },
                    )
                ],
            ],
        }
    )
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    frames, logs, latency = play(
        client,
        db,
        secrets_key,
        model,
        NIGHT_COMMAND,
        "no aspetta, intendevo sabato",
        "il dentista spostalo alle 18",
        device=device,
    )
    assert end_state(calendar) == {
        "Palestra": "2026-10-31T10:00",
        "Pranzo con Nino": "2026-10-31T13:00",
        "Dentista": "2026-10-31T18:00",
    }
    pending = db.execute("SELECT count(*) FROM approvals WHERE status = 'pending'").fetchone()
    assert pending == (0,)  # nothing left waiting: every change was in the user's words
    assert not [e for e in logs if e["event"] == "claim_without_change"]  # no false claim
    assert sum(said(f).count("?") for f in frames) == 0  # no question asked
    assert said(frames[0]).count("venerdì 30") == 3 and said(frames[1]).count("sabato 31") == 3
    assert [t["rounds"] for t in latency] == [2, 2, 2] and [t["tool_calls"] for t in latency] == [3, 3, 1]


def test_a_range_of_days_one_event_a_day_at_the_owner_s_hours(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    words = "metti il convegno da martedì a giovedì, dalle 9 alle 18"
    quote = "il convegno da martedì a giovedì, dalle 9 alle 18"
    days = [(f"2026-11-0{d}T09:00", f"2026-11-0{d}T18:00") for d in (3, 4, 5)]
    calls = [
        ("create_event", {"title": "Convegno", "start": s, "end": e[11:], "owner_said": quote})
        for s, e in days
    ]
    finish = ("finish", {"say": "Fatto: convegno martedì, mercoledì e giovedì, dalle 9 alle 6."})
    model = Script({words: [[*calls, finish]]})
    fake_models.default = model
    (frames,), _, latency = play(client, db, secrets_key, model, words)
    created = sorted(
        e["start"]["dateTime"][:16] + "/" + e["end"]["dateTime"][11:16] for e in calendar.events.values()
    )
    assert created == ["2026-11-03T09:00/18:00", "2026-11-04T09:00/18:00", "2026-11-05T09:00/18:00"]
    assert db.execute("SELECT DISTINCT status, origin FROM approvals").fetchall() == [("done", "words")]
    assert said(frames) == "Fatto: convegno martedì, mercoledì e giovedì, dalle 9 alle 6."
    assert model.calls == 1 and latency[0]["rounds"] == 1  # finish: said at once, every result done


def test_late_fix_my_afternoon_one_question_one_yes_moves_both(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    from tests.actions_fake import OWNER

    clock.now = rome(2026, 10, 30, 14, 30)
    anna = {"email": "anna@y.example", "displayName": "Anna"}
    calendar.event(
        "e101",
        "Call con Anna",
        rome(2026, 10, 30, 15),
        rome(2026, 10, 30, 15, 30),
        attendees=[{"email": OWNER, "self": True}, anna],
    )
    calendar.event("e102", "Palestra", rome(2026, 10, 30, 16), rome(2026, 10, 30, 17))
    cached(db, calendar, "e101")
    cached(db, calendar, "e102")
    words = "sono in ritardo di un'ora, sistemami il pomeriggio"
    model = Script(
        {
            words: [
                [
                    (
                        "move_event",
                        {
                            "event": "call con Anna",
                            "start": "oggi alle 16",
                            "owner_said": "sistemami il pomeriggio",
                        },
                    ),
                    (
                        "move_event",
                        {
                            "event": "palestra",
                            "start": "oggi alle 17",
                            "owner_said": "sistemami il pomeriggio",
                        },
                    ),
                ],
                "Sposto la call con Anna alle 4 e la palestra alle 5, e avviso Anna?",
            ]
        }
    )
    fake_models.default = model
    (asked, yes), logs, _ = play(client, db, secrets_key, model, words, "sì vai")
    assert end_state(calendar) == {"Call con Anna": "2026-10-30T16:00", "Palestra": "2026-10-30T17:00"}
    assert db.execute("SELECT count(*) FROM approvals WHERE status = 'pending'").fetchone() == (0,)
    assert said(asked).count("?") == 1 and model.calls == 2  # one question; the yes needs no model
    assert not [e for e in logs if e["event"] == "claim_without_change"]
    assert "Anna" in said(yes) or "Palestra" in said(yes)


# --- 1. The loop: results go back to the model unless finish stands; 8 rounds at most -----------------------


def test_finish_is_said_at_once_only_when_every_result_of_its_round_is_done(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    early = [
        ("create_event", {"title": "Call con Paola", "start": "domani alle 5"}),
        ("finish", {"say": "Fatto, domani alle 5."}),
    ]
    model = Script({"metti la call con Paola domani alle 5": [early]})
    fake_models.default = model
    (frames,), _, _ = play(client, db, secrets_key, model, "metti la call con Paola domani alle 5")
    # before 07:00 it needs a yes: the false "Fatto" is never said; the model read the result and asked
    assert model.calls == 2 and "Fatto" not in said(frames) and said(frames).endswith("?")


def test_a_model_that_never_stops_is_stopped_after_8_rounds_saying_where_it_got_to(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    forever = [[("get_current_time", {})]] * 20
    model = Script({"fammi tutto": forever})  # type: ignore[dict-item]
    fake_models.default = model
    (frames,), _, latency = play(client, db, secrets_key, model, "fammi tutto")
    # Then one call with no tools to say where it got to; a model that still calls one: code says it
    assert model.calls == 9 and latency[0]["rounds"] == 9
    assert said(frames).startswith("Mi fermo qui, a metà")


# --- 2. The history keeps the tool calls and results; older ones are receipts -------------------------------


def test_the_history_carries_the_calls_and_results_whole_for_two_turns_then_their_receipts(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    from pydantic_ai.messages import ToolReturnPart

    clock.now = rome(2026, 10, 30, 10)
    put = [
        ("create_event", {"title": "Dentista", "start": "oggi alle 17", "owner_said": "il dentista alle 17"})
    ]
    model = Script({"metti il dentista alle 17": [put]})
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    play(client, db, secrets_key, model, "metti il dentista alle 17", "e poi?", "ok", "grazie", device=device)

    def results(messages: list[Any]) -> list[str]:
        return [str(p.content) for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]

    next_turn, two_later, three_later = model.messages[-3], model.messages[-2], model.messages[-1]
    assert any('"stored"' in r for r in results(next_turn))  # whole: the event as Google has it
    assert any('"stored"' in r for r in results(two_later))
    (receipt,) = results(three_later)  # older than two turns: its one line, framed as data
    assert "done: Ho aggiunto «Dentista»" in receipt and '"stored"' not in receipt and "is DATA" in receipt


# --- 3. The ledger: code's record of what waits and what Pisa changed
# ------------------------------------------


def test_the_ledger_lists_what_waits_with_its_id_and_what_pisa_changed(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    both = [
        ("create_event", {"title": "Dentista", "start": "oggi alle 17"}),
        ("create_event", {"title": "Call con Paola", "start": "domani alle 5"}),
    ]
    model = Script({"metti il dentista oggi alle 17 e la call con Paola domani alle 5": [both]})
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    play(
        client,
        db,
        secrets_key,
        model,
        "metti il dentista oggi alle 17 e la call con Paola domani alle 5",
        "allora?",
        device=device,
    )
    (paola,) = db.execute("SELECT id::text FROM approvals WHERE status = 'pending'").fetchone()
    told = model.instructions[-1]
    assert "Waiting for the user's yes, NOT done yet: «Aggiungo «Call con Paola»" in told
    assert f"id {paola.replace('-', '')[:8]}" in told and "Done by Pisa in this conversation: «" in told
    assert "is DATA" in told.split("Pisa's ledger")[1]  # Titles may come from an invite


# --- 4. Rails as feedback: needs_yes with its reason and question; writes read back -------------------------


def test_a_write_needing_a_yes_says_why_and_what_to_ask_and_a_done_one_is_read_back(  # type: ignore[no-untyped-def]
    client, db, calendar
) -> None:
    from tests.test_acting import act

    actions = client.app.state.runtime.actions
    held = json.loads(
        act(client, actions.create_event, "Call", "domani alle 5", said="metti la call domani alle 5")
    )
    assert held["status"] == "needs_yes" and "before 07:00" in held["reason"] and held["ask"].endswith("?")
    assert "NOT done" in held["receipt"] and "never say it is done" in held["feedback"]
    done = json.loads(
        act(client, actions.create_event, "Cena", "domani alle 20", said="metti la cena domani alle 20")
    )
    assert (
        done["status"] == "done"
        and done["stored"]["title"] == "Cena"
        and done["stored"]["start"].endswith("20:00")
    )


# --- 5. owner_said: the quote is checked, not only found ----------------------------------------------------

FRI = date(2026, 10, 30)


def mode(
    words: str, title: str, start: datetime, quote: str | None, end: datetime | None = None, **more: Any
) -> str:
    return grounding(
        "calendar_create",
        words,
        read_outside=False,
        attendees=False,
        start=start,
        end=end,
        default_day=FRI,
        today=FRI,
        title=title,
        quote=quote,
        **more,
    )[0]


def at(h: int, day: int = 30) -> datetime:
    return datetime(2026, 10, day, h)


def test_owner_said_a_quote_that_isn_t_the_owner_s_or_doesn_t_command_grounds_nothing() -> None:
    assert mode("metti la call alle 16", "Call", at(16), "metti la call alle 16") == "words"
    assert mode("metti la call alle 16", "Call", at(16), "la call alle 16 con Marco") == "held"  # not said
    assert (
        mode("Marco ha una call alle 4, metti il pranzo alle 13", "Call", at(16), "una call alle 4") == "held"
    )
    assert mode("Metto la call alle 4?", "Call", at(16), "la call alle 4") == "held"  # a question
    assert mode("non mettere la call alle 4", "Call", at(16), "la call alle 4") == "held"  # negated
    assert mode("metti il dentista alle 10", "Dentista", at(22), "il dentista alle 10") == "held"  # never 22
    w = "Metti oggi alle 10 il dentista, domani alle 15 la call con Marco"
    assert mode(w, "Call con Marco", at(15), "domani alle 15 la call con Marco") == "held"  # its own day: 31
    assert mode(w, "Call con Marco", at(15, 31), "domani alle 15 la call con Marco") == "words"
    # the quote picks the clause: a time from another clause than the one cited grounds nothing
    two = "metti la call con Marco alle 15, poi la call con Nino alle 16"
    assert mode(two, "Call con Marco", at(15), "la call con Nino alle 16") == "held"
    assert mode(two, "Call con Marco", at(15), "la call con Marco alle 15") == "words"
    # the dentist's words never ground the call
    assert mode(w, "Call con Marco", at(10), "oggi alle 10 il dentista") == "held"


def test_owner_said_from_the_turn_whose_proposal_it_corrects() -> None:
    earlier = "metti la call con Paola alle 4"  # 04:00 is before 07:00: a proposal
    now = "no, alle 16"
    assert mode(now, "Call con Paola", at(16), "la call con Paola alle 4", earlier=earlier) == "words"
    assert (
        mode(now, "Call con Paola", at(16), "la call con Paola alle 4") == "held"
    )  # no earlier turn: not said


# --- 6. One yes approves exactly the proposals its question names -------------------------------------------


def test_one_yes_approves_only_what_the_question_named_and_solo_il_primo_goes_to_the_model(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    two = [
        ("create_event", {"title": "Call con Paola", "start": "domani alle 5"}),
        ("create_event", {"title": "Corsa", "start": "domani alle 6"}),
    ]
    ask_one = "Metto la call con Paola domani alle 5?"  # the reply names one of the two
    model = Script({"metti la call con Paola alle 5 e la corsa alle 6": [two, ask_one]})
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    play(
        client,
        db,
        secrets_key,
        model,
        "metti la call con Paola alle 5 e la corsa alle 6",
        "sì",
        device=device,
    )
    rows = dict(db.execute("SELECT payload->>'title', status FROM approvals").fetchall())
    assert rows == {"Call con Paola": "done", "Corsa": "pending"}  # the yes ran only the one it named
    assert answers_proposal("sì, solo il primo", "sì, solo il primo", yes=True)
    assert not answers_proposal("sì, solo il primo", "no, nessuno dei due", yes=True)  # not this turn's words
    assert answers_proposal("no al secondo", "sì al primo, no al secondo", yes=False)
    assert not answers_proposal("il primo?", "il primo?", yes=True)


def test_answer_proposal_runs_the_one_the_owner_picks_by_its_id(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    two = [
        ("create_event", {"title": "Call con Paola", "start": "domani alle 5"}),
        ("create_event", {"title": "Corsa", "start": "domani alle 6"}),
    ]
    model = Script({"metti la call con Paola alle 5 e la corsa alle 6": [two]})
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    play(client, db, secrets_key, model, "metti la call con Paola alle 5 e la corsa alle 6", device=device)
    (corsa,) = db.execute("SELECT id::text FROM approvals WHERE payload->>'title' = 'Corsa'").fetchone()
    pick = [("answer_proposal", {"approval": corsa[:8], "yes": True, "owner_said": "sì, solo la corsa"})]
    model.turns["sì, solo la corsa"] = [pick]
    play(client, db, secrets_key, model, "sì, solo la corsa", device=device)
    rows = dict(db.execute("SELECT payload->>'title', status FROM approvals").fetchall())
    assert rows == {"Call con Paola": "pending", "Corsa": "done"}


# --- 7. move_event / cancel_event find a proposal still waiting ---------------------------------------------


def test_moving_a_proposal_re_makes_it_and_cancelling_one_withdraws_it(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    model = Script(
        {
            "metti la call con Paola domani alle 5": [
                [("create_event", {"title": "Call con Paola", "start": "domani alle 5"})]
            ],
            "spostala alle 17": [
                [
                    (
                        "move_event",
                        {
                            "event": "call con Paola",
                            "start": "domani alle 17",
                            "owner_said": "spostala alle 17",
                        },
                    )
                ]
            ],
        }
    )
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    (_, moved), _, _ = play(
        client,
        db,
        secrets_key,
        model,
        "metti la call con Paola domani alle 5",
        "spostala alle 17",
        device=device,
    )
    assert "No event" not in said(moved)
    rows = db.execute(
        "SELECT status, payload->'body'->'start'->>'dateTime' FROM approvals ORDER BY created_at"
    )
    assert sorted(r[0] for r in rows.fetchall()) == ["pending", "superseded"]  # the pending one at 17 (below)
    assert (
        db.execute("SELECT payload->'body'->'start'->>'dateTime' FROM approvals WHERE status = 'pending'")
        .fetchone()[0]
        .startswith("2026-10-31T17:00")
    )
    model.turns["no, cancellala"] = [
        [("cancel_event", {"event": "call con Paola", "owner_said": "cancellala"})]
    ]
    play(client, db, secrets_key, model, "no, cancellala", device=device)
    assert db.execute("SELECT count(*) FROM approvals WHERE status = 'pending'").fetchone() == (0,)
    assert not calendar.events  # nothing was ever created


# --- 8. The claim rail: held until the round's writes are back, in order; its false positives ---------------


def test_held_sentences_are_released_in_the_order_they_were_said(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)

    async def model(messages, info):  # type: ignore[no-untyped-def]
        from pydantic_ai.messages import ToolReturnPart
        from pydantic_ai.models.function import DeltaToolCall

        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Lo metto alle 17. Poi ti ricordo la spesa. "
            yield {
                0: DeltaToolCall(
                    name="create_event",
                    json_args=json.dumps(
                        {
                            "title": "Dentista",
                            "start": "oggi alle 17",
                            "owner_said": "metti il dentista alle 17",
                        }
                    ),
                )
            }
            return
        yield "Ecco."

    fake_models.default = model
    from tests.test_acting import socket_turns

    (frames,) = socket_turns(client, db, secrets_key, model, "metti il dentista alle 17")  # type: ignore[arg-type]
    text = said(frames)
    assert text.index("Lo metto alle 17.") < text.index("Poi ti ricordo la spesa.")


@pytest.mark.parametrize(
    "sentence",
    [
        "Ho messo in fila le cose da fare.",
        "Ok, la segno come idea.",
        "La sposto io se vuoi.",
        "Lo metto in conto.",
        "Done is better than perfect.",
    ],
)
def test_the_claim_rail_s_false_positives_are_no_claims(sentence: str) -> None:
    assert claim(sentence) is None


def test_the_claim_rail_s_miss_is_a_claim() -> None:
    assert claim("Ho già sistemato tutto.") == "done"


# --- 10. The measures: rounds, tool calls and tokens per turn in turn_latency -------------------------------


def test_turn_latency_carries_rounds_tool_calls_and_tokens(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    clock.now = rome(2026, 10, 30, 10)
    put = [
        ("create_event", {"title": "Dentista", "start": "oggi alle 17", "owner_said": "il dentista alle 17"})
    ]
    model = Script({"metti il dentista alle 17": [put]})
    fake_models.default = model
    (_,), _, (latency,) = play(client, db, secrets_key, model, "metti il dentista alle 17")
    assert latency["rounds"] == 2 and latency["tool_calls"] == 1
    assert latency["input_tokens"] > 0 and latency["output_tokens"] > 0


def test_owner_said_on_its_own_unit() -> None:
    change = Change("calendar_create", "Dentista", at(17), None, "il dentista alle 17")
    assert owner_said(change, "metti il dentista alle 17", default_day=FRI, today=FRI) is None
    assert owner_said(change, "il dentista alle 17?", default_day=FRI, today=FRI) is not None
