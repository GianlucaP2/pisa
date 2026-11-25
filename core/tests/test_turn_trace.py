"""The turn's trace for an audit run (`turn_trace`, DEBUG only): what the model was given by part name and
length, where an added task's title was seen, each traced result's kind and the understanding step's flags;
never a title, their words or a part's text. Synthetic words only."""


def test_the_turn_trace_names_where_a_title_was_seen_never_the_title() -> None:
    """`turn_trace` (DEBUG, `PISA_LOG_LEVEL=debug`): part names, lengths and flags; no title, no words."""
    import json
    import uuid

    from pisa.services.conversation import TurnState, trace_turn

    turn = TurnState(uuid.uuid4(), "Add 'call the bank' for tomorrow")
    turn.calls.append(("add_task", {"title": "Chiamare la banca", "due": "2026-11-20", "place": None}))
    turn.tools.append("add_task")
    parts = [("language", "The user spoke English (en-US)."),
             ("context", "Today's tasks with no time: «Chiamare la banca»."), ("history", "")]  # fmt: skip
    trace = trace_turn("en-US", turn, parts)
    assert trace["added"] == [
        {"args": ["due", "title"], "title_chars": 17, "title_other_language": True, "title_in": ["context"]}
    ]
    assert trace["parts"] == {"language": 31, "context": 48}
    assert "banca" not in json.dumps(trace) and "bank" not in json.dumps(trace)


def test_a_traced_result_is_kept_by_its_kind_never_its_titles() -> None:
    from pisa.services.conversation import outcome_of

    assert outcome_of("<plan-1>\nDone: Pay the electricity bill. The user can undo it.\n</plan-1>") == "Done"
    assert outcome_of("<plan-1>\nNothing open matches that, today or in the tasks.\n</plan-1>") == (
        "Nothing open matches"
    )
    assert outcome_of("<plan-1>\nAlready done: nothing left open matches that.\n</plan-1>") == "Already done"
    assert outcome_of("Renew passport") == "other:unknown"  # a hint from code's words only, never the text
    assert outcome_of("Waiting for the user's yes, NOT done yet: one change.") == "other:held"
    assert outcome_of("Not added: this title isn't in English, the user's language.") == "Not added"
    assert outcome_of("Call add_task again with the title in English.") == "other:retry"


def test_the_understanding_step_is_traced_by_its_flags() -> None:
    import uuid

    from pisa.domain import understand as rules
    from pisa.services.conversation import TurnState, trace_turn

    fix = rules.Correction("paid the electricity bill", "Pay the electricity bill", 0.9, "model")
    step = rules.Result(meant="I Pay the electricity bill", changed=True, corrections=[fix])
    trace = trace_turn("en-US", TurnState(uuid.uuid4(), "x"), [], [("mark_done", "Already done")], step)
    assert trace["understood"] == {"changed": True, "fixes": 1, "by_model": 1}
    assert trace["outcomes"] == ["mark_done:Already done"]
