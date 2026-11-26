"""Settings (contract D): exact defaults, merge-PATCH, validation naming field and range; phrase matching,
spans across midnight, the briefing day and quiet mode."""

from datetime import UTC, date, datetime, time

import psycopg
import pytest

from pisa.domain import settings as rules
from tests.conftest import pair

ROME = "Europe/Rome"


@pytest.fixture
def auth(db: psycopg.Connection) -> dict[str, str]:
    _, token = pair(db)
    return {"Authorization": f"Bearer {token}"}


def test_defaults_are_exactly_the_contract(client, auth) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/v1/settings", headers=auth).json() == {
        "language": "it-IT",
        "briefing": {
            "triggers": {"glasses_on": True, "app_open": True, "phrase": True, "time": False},
            "phrases": ["Buongiorno Pisa", "Good morning Pisa"],
            "time": "07:30",
            "window": {"start": "04:00", "end": "13:00"},
            "repeat_after_min": None,
            "sections": ["agenda", "tasks", "email", "weather"],
            "max_words": 60,
        },
        "reminders": {"event_lead_min": 15, "task_lead_min": 15},
        "quiet": {"hours": {"start": "22:30", "end": "07:00"}, "until": None},
        "places": {
            "routes": ["apple", "google"],
            "search": ["apple", "google"],
            "details": ["google"],
            "web": ["tavily"],
            "default_mode": "transit",
            "monthly_cap_eur": 5.0,
            "modes": ["transit"],  # From default_mode until the user sets them
            "walk_max_min": 45,
        },
        "planner": {
            "day": {"start": "08:00", "end": "21:00"},
            "buffer_min": 10,
            "default_task_min": 30,
            "leave_now": True,
            "leave_lead_min": 5,
            "check_ins": True,
        },
        # Contract V: null is resolved when read: no OpenAI key here, so the phone's voice.
        "voice": {"provider": "device", "model": None, "voice": "marin", "speed": 1.0},
        "profile": {"something_different": True},  # About 1 places card in 5, labelled
        # Economy until setup or the app picks natural; Gemini Live (never Grok Voice) by default
        "conversation": {"mode": "economy", "talker": "gemini", "voices": {"xai": "eve", "gemini": "Aoede"}},
        "messages_retention": "forever",  # Until they change it
        "google_ai_tier": "unknown",  # Google AI off until they say its project is on a paid tier
        "guardian": {"rules": []},  # Their rules over Pisa's interruptions
    }


def test_patch(client, auth) -> None:  # type: ignore[no-untyped-def]
    body = client.patch(
        "/v1/settings",
        headers=auth,
        json={
            "language": "en-US",
            "briefing": {
                "triggers": {"time": True},  # objects merge: the other three triggers stay
                "phrases": ["Ciao Pisa"],  # arrays replace
                "window": {"end": "11:00"},  # objects merge, start stays
                "repeat_after_min": 30,
            },
            "quiet": {"hours": None, "until": "2026-10-20T15:00:00+02:00"},  # null replaces
        },
    )
    assert body.status_code == 200, body.json()
    settings = body.json()
    assert settings["language"] == "en-US"
    assert settings["briefing"]["triggers"] == {
        "glasses_on": True,
        "app_open": True,
        "phrase": True,
        "time": True,
    }
    assert settings["briefing"]["phrases"] == ["Ciao Pisa"]
    assert settings["briefing"]["window"] == {"start": "04:00", "end": "11:00"}
    assert settings["briefing"]["repeat_after_min"] == 30 and settings["briefing"]["max_words"] == 60
    assert settings["quiet"] == {"hours": None, "until": "2026-10-20T15:00:00+02:00"}
    assert client.get("/v1/settings", headers=auth).json() == settings
    window = client.patch("/v1/settings", headers=auth, json={"briefing": {"window": None}}).json()
    assert window["briefing"]["window"] is None
    back = client.patch(
        "/v1/settings", headers=auth, json={"briefing": {"window": {"start": "20:00", "end": "03:00"}}}
    )
    assert back.json()["briefing"]["window"] == {"start": "20:00", "end": "03:00"}  # may cross midnight


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"language": "fr-FR"}, "language must be it-IT or en-US."),
        (
            {"briefing": {"triggers": {"glasses_on": "yes"}}},
            "briefing.triggers.glasses_on must be true or false.",
        ),
        ({"briefing": {"phrases": []}}, "briefing.phrases must be a list of 1 to 10 phrases."),
        (
            {"briefing": {"phrases": [f"P {i}" for i in range(11)]}},
            "briefing.phrases must be a list of 1 to 10 phrases.",
        ),
        ({"briefing": {"phrases": ["P"]}}, "briefing.phrases[0] must be 2 to 60 characters long."),
        ({"briefing": {"phrases": ["x" * 61]}}, "briefing.phrases[0] must be 2 to 60 characters long."),
        (
            {"briefing": {"phrases": ["Buongiorno Pisa", "buongiorno, pisa!"]}},
            "briefing.phrases[1] repeats another phrase (case, accents and punctuation don't count).",
        ),
        ({"briefing": {"time": "7:30"}}, "briefing.time must be a time as HH:MM, from 00:00 to 23:59."),
        ({"briefing": {"time": "24:00"}}, "briefing.time must be a time as HH:MM, from 00:00 to 23:59."),
        ({"briefing": {"time": "07:30\n"}}, "briefing.time must be a time as HH:MM, from 00:00 to 23:59."),
        (
            {"briefing": {"window": {"start": "08:00", "end": "08:00"}}},
            "briefing.window.start and briefing.window.end must differ.",
        ),
        (
            {"briefing": {"repeat_after_min": 4}},
            "briefing.repeat_after_min must be a whole number from 5 to 720.",
        ),
        (
            {"briefing": {"repeat_after_min": 721}},
            "briefing.repeat_after_min must be a whole number from 5 to 720.",
        ),
        (
            {"briefing": {"sections": []}},
            "briefing.sections must be a non-empty list of agenda, tasks, email and weather, each at most "
            "once.",
        ),
        (
            {"briefing": {"sections": ["agenda", "traffic"]}},
            "briefing.sections must be a non-empty list of agenda, tasks, email and weather, each at most "
            "once.",
        ),
        (
            {"briefing": {"sections": ["agenda", "agenda"]}},
            "briefing.sections must be a non-empty list of agenda, tasks, email and weather, each at most "
            "once.",
        ),
        ({"briefing": {"max_words": 19}}, "briefing.max_words must be a whole number from 20 to 200."),
        ({"briefing": {"max_words": 201}}, "briefing.max_words must be a whole number from 20 to 200."),
        (
            {"reminders": {"event_lead_min": -1}},
            "reminders.event_lead_min must be a whole number from 0 to 120.",
        ),
        (
            {"reminders": {"task_lead_min": 121}},
            "reminders.task_lead_min must be a whole number from 0 to 120.",
        ),
        (
            {"reminders": {"task_lead_min": True}},
            "reminders.task_lead_min must be a whole number from 0 to 120.",
        ),
        ({"quiet": {"hours": "22:00-07:00"}}, "quiet.hours must be a JSON object."),
        ({"quiet": {"hours": {"begin": "22:00"}}}, "There is no setting 'quiet.hours.begin'."),
        ({"quiet": {"until": "15:00"}}, "quiet.until must be null or an RFC 3339 date-time with its offset."),
        (
            {"quiet": {"until": "2026-10-20T15:00:00"}},
            "quiet.until must be null or an RFC 3339 date-time with its offset.",
        ),
        ({"briefing": {"colour": "red"}}, "There is no setting 'briefing.colour'."),
        ({"planner": {"day": None}}, "planner.day must be a JSON object."),
        (
            {"planner": {"day": {"start": "8:00"}}},
            "planner.day.start must be a time as HH:MM, from 00:00 to 23:59.",
        ),
        (
            {"planner": {"day": {"start": "21:00", "end": "21:00"}}},
            "planner.day.start and planner.day.end must differ.",
        ),
        ({"planner": {"buffer_min": 61}}, "planner.buffer_min must be a whole number from 0 to 60."),
        (
            {"planner": {"default_task_min": 4}},
            "planner.default_task_min must be a whole number from 5 to 240.",
        ),
        (
            {"planner": {"default_task_min": 241}},
            "planner.default_task_min must be a whole number from 5 to 240.",
        ),
        ({"planner": {"leave_now": "yes"}}, "planner.leave_now must be true or false."),
        ({"planner": {"check_ins": 1}}, "planner.check_ins must be true or false."),
        ({"planner": {"leave_lead_min": 31}}, "planner.leave_lead_min must be a whole number from 0 to 30."),
        (
            {"places": {"routes": []}},
            "places.routes must be a non-empty list of apple and google, each at most once.",
        ),
        (
            {"places": {"search": ["google", "google"]}},
            "places.search must be a non-empty list of apple and google, each at most once.",
        ),
        (
            {"places": {"routes": ["tomtom"]}},
            "places.routes must be a non-empty list of apple and google, each at most once.",
        ),
        ({"places": {"details": ["apple"]}}, "places.details must be a list of google, each at most once."),
        ({"places": {"web": ["google"]}}, "places.web must be a list of tavily, each at most once."),
        (
            {"places": {"default_mode": "scooter"}},
            "places.default_mode must be drive, transit, walk or bike.",
        ),
        ({"places": {"monthly_cap_eur": -1}}, "places.monthly_cap_eur must be a number from 0 to 100."),
        ({"places": {"monthly_cap_eur": 100.5}}, "places.monthly_cap_eur must be a number from 0 to 100."),
        ({"places": {"monthly_cap_eur": "5"}}, "places.monthly_cap_eur must be a number from 0 to 100."),
        ({"briefing": None}, "briefing must be a JSON object."),
    ],
)
def test_validation_names_the_field_and_range(client, auth, patch, message) -> None:  # type: ignore[no-untyped-def]
    response = client.patch("/v1/settings", headers=auth, json=patch)
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "validation_failed",
        "message": message,
        "action": "Fix the setting and try again",
    }
    assert client.get("/v1/settings", headers=auth).json()["language"] == "it-IT"  # nothing was stored


@pytest.mark.parametrize(
    ("said", "matches"),
    [
        ("Buongiorno Pisa", True),
        ("buongiorno pisa", True),
        ("Buongiorno, Pisa!", True),
        ("  BUONGIORNO   Pisa. ", True),
        ("Buongiorno Jay", False),  # the recogniser's other spellings of the name are the app's
        ("Buongiorno pizza", False),
        ("Buongiorno visa", False),
        ("Buongiorno Gei", False),
        ("BUONGIORNO PISA", True),
        ("buongiorno, pisa.", True),
        ("Buongiorno", True),  # the phrase without its "Pisa" word
        ("Good morning, Pisa", True),
        ("good morning", True),
        ("Buòngiórno Pisa", True),  # accents don't count
        ("buongiorno a tutti", False),
        ("Buongiorno Pisa, che tempo fa?", False),
        ("Pisa buongiorno", False),
        ("Buongiorno Marco", False),
        ("morning", False),
        ("", False),
    ],
)
def test_phrase_matching(said: str, matches: bool) -> None:
    assert rules.matches_phrase(said, ["Buongiorno Pisa", "Good morning Pisa"]) is matches


def test_spans_cross_midnight() -> None:
    night = {"start": "22:30", "end": "07:00"}
    assert rules.in_span(time(23, 30), night) and rules.in_span(time(0, 30), night)
    assert rules.in_span(time(6, 59), night) and not rules.in_span(time(7, 0), night)
    assert not rules.in_span(time(12, 0), night) and rules.in_span(time(12, 0), None)
    day = {"start": "04:00", "end": "13:00"}
    assert rules.in_span(time(4, 0), day) and not rules.in_span(time(13, 0), day)


def test_briefing_day_starts_at_the_window_start() -> None:
    settings = rules.defaults(language="it-IT")
    at = datetime(2026, 10, 21, 1, 30, tzinfo=UTC)  # 03:30 in Rome: still the 20th's briefing day
    assert rules.briefing_day(at, settings, ROME) == date(2026, 10, 20)
    assert rules.briefing_day(datetime(2026, 10, 21, 2, 0, tzinfo=UTC), settings, ROME) == date(2026, 10, 21)
    settings["briefing"]["window"] = {"start": "20:00", "end": "03:00"}  # a night owl
    assert rules.briefing_day(datetime(2026, 10, 21, 0, 30, tzinfo=UTC), settings, ROME) == date(2026, 10, 20)
    assert rules.briefing_day(datetime(2026, 10, 21, 18, 30, tzinfo=UTC), settings, ROME) == date(
        2026, 10, 21
    )
    settings["briefing"]["window"] = None  # no window: the day starts at 04:00
    assert rules.briefing_day(datetime(2026, 10, 21, 1, 59, tzinfo=UTC), settings, ROME) == date(2026, 10, 20)
    settings["briefing"]["time"] = "02:00"  # earlier than the day start: it belongs to the end of the day
    assert (
        rules.briefing_time_on(date(2026, 10, 20), settings, ROME).isoformat() == "2026-10-21T02:00:00+02:00"
    )


def test_quiet_hours_and_until() -> None:
    settings = rules.defaults(language="it-IT")
    assert rules.is_quiet(datetime(2026, 10, 20, 21, 0, tzinfo=UTC), settings, ROME)  # 23:00 Rome
    assert rules.is_quiet(datetime(2026, 10, 20, 4, 30, tzinfo=UTC), settings, ROME)  # 06:30 Rome
    assert not rules.is_quiet(datetime(2026, 10, 20, 10, 0, tzinfo=UTC), settings, ROME)
    settings["quiet"]["until"] = "2026-10-20T15:00:00+02:00"
    assert rules.is_quiet(datetime(2026, 10, 20, 12, 59, tzinfo=UTC), settings, ROME)
    assert not rules.is_quiet(datetime(2026, 10, 20, 13, 0, tzinfo=UTC), settings, ROME)
    settings["quiet"]["hours"] = None
    settings["quiet"]["until"] = None
    assert not rules.is_quiet(datetime(2026, 10, 20, 21, 0, tzinfo=UTC), settings, ROME)


def test_places_merge_patch(client, auth) -> None:  # type: ignore[no-untyped-def]
    body = client.patch(
        "/v1/settings",
        headers=auth,
        json={
            "places": {
                "routes": ["google"],
                "details": [],
                "web": [],
                "default_mode": "walk",
                "monthly_cap_eur": 0,
            }
        },
    )
    assert body.status_code == 200, body.json()
    assert body.json()["places"] == {
        "routes": ["google"],
        "search": ["apple", "google"],  # objects merge: untouched lists stay
        "details": [],  # off
        "web": [],
        "default_mode": "walk",
        "monthly_cap_eur": 0.0,
        "modes": ["walk", "transit"],  # Default_mode moves walk first
        "walk_max_min": 45,
    }


def test_planner_merge_patch(client, auth) -> None:  # type: ignore[no-untyped-def]
    body = client.patch(
        "/v1/settings",
        headers=auth,
        json={"planner": {"day": {"start": "22:00", "end": "02:00"}, "leave_now": False}},
    )
    assert body.status_code == 200, body.json()
    assert body.json()["planner"] == {
        "day": {"start": "22:00", "end": "02:00"},  # may cross midnight
        "buffer_min": 10,
        "default_task_min": 30,
        "leave_now": False,
        "leave_lead_min": 5,
        "check_ins": True,
    }
