"""The REST fixtures (`protocol/fixtures/rest`) match the API models they document."""

import json

from tests.conftest import PROTOCOL_DIR


def test_the_rest_fixtures_match_the_api_models() -> None:
    """The detail sheet's fixture is what GET /v1/places/{id}/details answers (its REST model)."""
    from pisa.api import schemas

    body = json.loads((PROTOCOL_DIR / "fixtures" / "rest" / "place_details.json").read_text())
    parsed = schemas.PlaceDetails.model_validate(body)
    assert parsed.review_summary is not None and parsed.review_summary.source == "google"
    assert len(parsed.reviews) <= 5 and all(len(r.text) <= 300 for r in parsed.reviews)
    assert parsed.menu is not None and len(parsed.menu.highlights) <= 6
    # The profile with scopes, support, evidence and state; the weekly check.
    profile = schemas.Profile.model_validate_json(
        (PROTOCOL_DIR / "fixtures" / "rest" / "profile.json").read_text()
    )
    assert all(set(c.scope) == {"day", "company", "where", "time"} for c in profile.claims)
    # The user's home, and the recogniser's hints.
    assert profile.home.source == "owner" and profile.home.confirmed and profile.home.place is not None
    hints = schemas.SpeechHints.model_validate_json(
        (PROTOCOL_DIR / "fixtures" / "rest" / "speech_hints.json").read_text()
    )
    assert 0 < len(hints.hints) <= 100
    assert {c.state for c in profile.claims} <= {"candidate", "active", "asking", "retired"}
    weekly = schemas.Weekly.model_validate_json(
        (PROTOCOL_DIR / "fixtures" / "rest" / "profile_weekly.json").read_text()
    )
    assert len(weekly.items) <= 3 and {i.kind for i in weekly.items} <= {
        "confirm",
        "changed",
        "new",
        "expiring",
        "buffer",
    }


def test_the_core12_rest_fixtures_match_the_api_models() -> None:
    """Planning with intent (events with kinds, a plan with a window event moved, insert's options) and email
    precision (the quiet list, a dismiss with never)."""
    from pisa.api import schemas

    def load(name: str) -> str:
        return (PROTOCOL_DIR / "fixtures" / "rest" / name).read_text()

    events = schemas.EventsResponse.model_validate_json(load("events.json"))
    assert {e.kind for e in events.events} == {"window", "fixed"}
    assert all((e.min_stay_min is not None) == (e.kind == "window") for e in events.events)
    plan = schemas.Plan.model_validate_json(load("plan_window.json"))
    window = next(i for i in plan.items if i.kind == "event")
    assert window.event_kind == "window" and window.arrive_at is not None and window.min_stay_min
    options = schemas.PlanOptions.model_validate_json(load("plan_insert_options.json"))
    assert 1 <= len(options.options) <= 3
    inbox = schemas.InboxResponse.model_validate_json(load("inbox.json"))
    assert len(inbox.emails) <= 50
    dismissed = schemas.TaskDismissed.model_validate_json(load("task_dismissed.json"))
    assert dismissed.task.status == "dismissed" and dismissed.rule


def test_the_core13_rest_fixtures_match_the_api_models() -> None:
    """Low is quiet (the Low list: low tasks with why, and the emails that became no task) and done by being
    there (an attended event)."""
    from pisa.api import schemas

    def load(name: str) -> str:
        return (PROTOCOL_DIR / "fixtures" / "rest" / name).read_text()

    low = schemas.TasksResponse.model_validate_json(load("tasks_low.json"))
    assert low.tasks and all(t.priority == 3 for t in low.tasks) and low.emails
    assert all((t.priority_reason is None) == (t.priority_source == "owner") for t in low.tasks)
    events = schemas.EventsResponse.model_validate_json(load("events.json"))
    assert [e.attended_at is not None for e in events.events] == [True, False]
