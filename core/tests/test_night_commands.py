"""Part 1: the brain on a late night's commands, replayed in a synthetic form (no personal
data). At 02:38 a clear multi-event command became three questions for the wrong day; a correction called no
tool; Pisa said "Sono sul 31. Li sposto al 30." with nothing done. Each fix with its test (revert-to-fail in
the handoff)."""

import json
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from pisa.domain.actions import Change, owner_said, spoken_when
from pisa.domain.claims import claim
from pisa.domain.tasks import day_named
from tests.assistant_env import rome
from tests.test_acting import spoken

NIGHT = rome(2026, 10, 30, 2, 38)  # venerdì 30 ottobre, 02:38: the user hasn't slept yet
COMMAND = (
    "Domani tra le 10 e le 14 mi arriva la spesa… alle 14 in ufficio, call con Marco fino alle quattro, "
    "dalle "
    "quattro call con Alessandro… metti tutto sul calendario e ricordamelo."
)


class Night:
    """A conversation model scripted by the user's words: the tool calls of its first round, then what it
    says (None: the tools' own sentences). It counts its calls and keeps the instructions each call was given.
    """

    def __init__(self, script: dict[str, tuple[list[tuple[str, dict[str, Any]]], str | None]]) -> None:
        self.script, self.calls, self.instructions = script, 0, []
        self.sent: list[int] = []

    async def __call__(self, messages, info):  # type: ignore[no-untyped-def]
        from pydantic_ai.messages import ToolReturnPart, UserPromptPart
        from pydantic_ai.models.function import DeltaToolCall

        self.calls += 1
        self.instructions.append(info.instructions or "")
        # what this request carries, in tokens (about 4 characters each): instructions, tools, history
        tools = json.dumps([t.parameters_json_schema | {"d": t.description} for t in info.function_tools])
        self.sent.append((len(info.instructions or "") + len(tools) + len(str(messages))) // 4)
        at = max(i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts))
        words = [str(p.content) for p in messages[at].parts if isinstance(p, UserPromptPart)][-1]
        rounds = sum(1 for m in messages[at + 1 :] if any(isinstance(p, ToolReturnPart) for p in m.parts))
        calls, text = self.script.get(words, ([], "Va bene."))
        if rounds == 0 and calls:
            yield {
                n: DeltaToolCall(name=name, json_args=json.dumps(args))
                for n, (name, args) in enumerate(calls)
            }
            return
        returns = [str(p.content) for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
        yield text if text is not None else (spoken(returns) if returns else "Va bene.")


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


def created(google: Any) -> dict[str, str]:
    return {e["summary"]: e["start"]["dateTime"][:16] for e in google.events.values() if e.get("summary")}


# --- 1 + 4: a direct command acts, several events in one sentence, "domani" after midnight ------------------


def test_the_night_s_command_puts_all_four_events_on_the_30th_at_once_in_one_model_call(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    from tests.test_acting import socket_turns

    clock.now = NIGHT
    four = [
        ("create_event", {"title": "Arrivo spesa", "start": "domani alle 10", "end": "14:00"}),
        ("create_event", {"title": "Ufficio", "start": "domani alle 14", "duration_min": 120}),
        ("create_event", {"title": "Call con Marco", "start": "domani alle 14", "end": "16:00"}),
        ("create_event", {"title": "Call con Alessandro", "start": "domani alle 16", "duration_min": 60}),
    ]
    model = Night(
        {
            COMMAND: (four, None),
            "è passata la mezzanotte, sono per oggi, il 30?": ([], "Sì, sono già sul 30."),
        }
    )
    fake_models.default = model
    check_words = "è passata la mezzanotte, sono per oggi, il 30?"
    put, check = socket_turns(client, db, secrets_key, model, COMMAND, check_words)  # type: ignore[arg-type]
    assert created(calendar) == {
        "Arrivo spesa": "2026-10-30T10:00",  # "domani" at 02:38 is the day the user wakes: the 30th
        "Ufficio": "2026-10-30T14:00",
        "Call con Marco": "2026-10-30T14:00",
        "Call con Alessandro": "2026-10-30T16:00",
    }
    statuses = db.execute("SELECT status, origin FROM approvals ORDER BY created_at").fetchall()
    assert statuses == [("done", "words")] * 4  # done at once: no question, no yes
    assert "interrupt" not in [f["type"] for f in put]
    assert said(put).count("venerdì 30") == 4  # every date confirmed with its weekday (the results say it)
    # The command, then its results read (it may finish in one: the replay below), then the check
    assert model.calls == 3
    assert said(check) == "Sì, sono già sul 30."  # true: Pisa put them there (the claim rail lets it through)


def test_each_event_is_grounded_on_its_own_clause_and_range() -> None:
    # `owner_said` replaced slot_grounded (None: grounded); the same probes, the same answers
    d = lambda h: rome(2026, 10, 30, h).replace(tzinfo=None)  # noqa: E731
    friday = date(2026, 10, 30)

    def grounded(words: str, title: str, start: datetime, end: datetime | None) -> bool:
        change = Change("calendar_create", title, start, end)
        return owner_said(change, words, default_day=friday, today=friday, small_hours=True) is None

    assert grounded(COMMAND, "Arrivo spesa", d(10), d(14))  # "tra le 10 e le 14 … la spesa"
    assert grounded(COMMAND, "Call con Marco", d(14), d(16))  # "fino alle quattro", from "alle 14" before
    assert grounded(COMMAND, "Call con Alessandro", d(16), None)  # "dalle quattro"
    assert not grounded(COMMAND, "Call con Alessandro", d(17), None)  # a time nobody said
    assert not grounded(COMMAND, "Call con Marco", d(14), d(17))  # an end nobody said
    assert not grounded("la call con Marco alle 15", "Call con Marco", d(15), None)  # no put verb
    assert not grounded("non mettere la call con Marco alle 15", "Call con Marco", d(15), None)


def test_domani_after_midnight_is_the_day_the_owner_wakes_and_dates_carry_their_weekday() -> None:
    friday = date(2026, 10, 30)
    assert day_named("domani alle 10", friday, small_hours=True) == friday
    assert day_named("dopodomani", friday, small_hours=True) == friday + timedelta(days=1)
    assert day_named("domani alle 10", friday) == friday + timedelta(days=1)  # at 09:00 it is tomorrow
    at_ten = rome(2026, 10, 30, 10)
    assert spoken_when(at_ten, friday, "it-IT", "Europe/Rome", small_hours=True) == "venerdì 30 alle 10"
    assert spoken_when(at_ten, friday, "it-IT", "Europe/Rome") == "oggi, venerdì 30, alle 10"


# --- 3: no claimed action without an action -----------------------------------------------------------------


def test_li_sposto_al_30_with_no_change_made_is_replaced_by_the_truth(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    from tests.test_acting import socket_turns

    clock.now = NIGHT
    model = Night({"devi modificare il calendario, spostali": ([], "Sono sul 31. Li sposto al 30.")})
    fake_models.default = model
    with capture_logs() as logs:
        (reply,) = socket_turns(client, db, secrets_key, model, "devi modificare il calendario, spostali")  # type: ignore[arg-type]
    # What really happened, naming the change ("Sono sul 31." alone has no verb to name)
    assert said(reply) == "Non li ho spostati: non ho cambiato niente."
    assert [e for e in logs if e["event"] == "claim_without_change"]
    assert not calendar.events and claim("Li metto oggi.") == "doing" and claim("Fatto.") == "done"
    assert claim("Sposto la cena alle 21?") is None and claim("Non l'ho ancora fatto.") is None


# --- 2: Pisa knows what waits for the user's yes; a correction re-makes it
# ------------------------------------


def test_a_pending_proposal_is_listed_as_not_done_and_a_correction_re_makes_it(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock
) -> None:
    from tests.test_acting import socket_turns

    clock.now = NIGHT
    first = [("create_event", {"title": "Call con Paola", "start": "oggi alle 5"})]  # before 07:00: one yes
    again = [("create_event", {"title": "Call con Paola", "start": "domani alle 17"})]
    model = Night({"metti la call con Paola alle 5": (first, None), "mettila domani alle 17": (again, None)})
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    socket_turns(client, db, secrets_key, model, "metti la call con Paola alle 5", device=device)  # type: ignore[arg-type]
    socket_turns(client, db, secrets_key, model, "mettila domani alle 17", device=device)  # type: ignore[arg-type]
    told = model.instructions[-1]
    assert "Waiting for the user's yes, NOT done yet: «" in told and "Call con Paola" in told
    rows = db.execute(
        "SELECT status, payload->'body'->'start'->>'dateTime' FROM approvals ORDER BY created_at"
    )
    # by status, not by row: both were made at one frozen time
    by = {status: start for status, start in rows.fetchall()}
    assert set(by) == {"superseded", "pending"} and by["pending"].startswith("2026-10-30T17:00")
    assert not calendar.events  # nothing done until the user's yes
    # past its 10 minutes and never answered: still listed, as not done
    clock.now = NIGHT + timedelta(minutes=12)
    socket_turns(client, db, secrets_key, model, "e allora?", device=device)  # type: ignore[arg-type]
    assert "never answered (expired), NOT done: «" in model.instructions[-1]


# --- 5: a delivery from email goes on its day ---------------------------------------------------------------


# --- 6: the turn's model calls and tokens, replayed ---------------------------------------------------------


@pytest.mark.parametrize("with_finish", [True, False])
def test_the_night_replayed_model_calls_and_tokens_per_turn(  # type: ignore[no-untyped-def]
    client, db, calendar, fake_models, secrets_key, clock, capsys, with_finish
) -> None:
    """The five turns of the night with the fake model: the calls each makes and its tokens (the fake's
    estimate), printed for the handoff. A command or a check is one call; nothing needs get_current_time (the
    date is in the instructions)."""
    from tests.test_acting import socket_turns

    clock.now = NIGHT
    four = [
        ("create_event", {"title": "Arrivo spesa", "start": "domani alle 10", "end": "14:00"}),
        ("create_event", {"title": "Ufficio", "start": "domani alle 14", "duration_min": 120}),
        ("create_event", {"title": "Call con Marco", "start": "domani alle 14", "end": "16:00"}),
        ("create_event", {"title": "Call con Alessandro", "start": "domani alle 16", "duration_min": 60}),
    ]
    finish = ("finish", {"say": "Fatto: tutti e quattro su venerdì 30."})
    turns = [
        (
            COMMAND,
            ([*four, finish] if with_finish else four, None),
        ),  # Its final words with its writes, said once all are done (else it reads the results)
        ("è passata la mezzanotte, sono per oggi, il 30?", ([], "Sì, sono già sul 30.")),
        ("non li hai spostati, spostali", ([], "Sono già sul 30: non serve spostarli.")),
        ("devi modificare il calendario", ([], "È già fatto: sono su venerdì 30.")),
        ("metti per il 31 la consegna dei pacchi", ([], "A che ora arriva?")),
    ]
    model = Night(dict(turns))
    fake_models.default = model
    device = __import__("tests.conftest", fromlist=["pair"]).pair(db)
    numbers, measured = [], []
    for words, _ in turns:
        before = model.calls
        with capture_logs() as logs:
            socket_turns(client, db, secrets_key, model, words, device=device)  # type: ignore[arg-type]
        numbers.append((model.calls - before, sum(model.sent[before:])))
        (turn,) = [e for e in logs if e["event"] == "turn_latency"]
        measured.append((turn["rounds"], turn["tool_calls"]))
    print(f"\nBRAIN-1 night replay, finish={with_finish} (model calls, input tokens sent):", numbers)
    print("  turn_latency (rounds, tool calls):", measured)
    # Turn 4's "È già fatto" (no change asked, none made) goes back to the model to
    # rewrite, one more call, never the claim rail's canned truth line
    assert [calls for calls, _ in numbers] == ([1, 1, 1, 2, 1] if with_finish else [2, 1, 1, 2, 1])
    assert [rounds for rounds, _ in measured] == [calls for calls, _ in numbers]
    assert max(tokens / calls for calls, tokens in numbers) < 20_000  # each call stays within the budget
    assert "Now: Friday 2026-10-30 02:38" in model.instructions[0]  # the date, no tool round for it
    assert 'their "domani" / "tomorrow" is today, Friday 2026-10-30' in model.instructions[0]
