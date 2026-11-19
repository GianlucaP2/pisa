"""The user's settings: defaults, merge-PATCH, validation, and the time rules built on them
(windows, the briefing day, quiet mode, briefing phrases). Pure rules, no I/O. Times are HH:MM in the user's
time zone (PISA_TIMEZONE, or where the phone is)."""

import copy
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pisa.domain import guardian as guardian_rules
from pisa.domain import locale, places, retention
from pisa.domain import voice as voice_rules
from pisa.domain.errors import PisaError
from pisa.domain.text import fold_words

DEFAULTS: dict[str, Any] = {
    "language": "en-US",  # PISA_LANGUAGE's tag, filled in by defaults()
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
        # the modes the user allows, in order of preference (null: from default_mode, when read), and
        # the longest walk: above it the next allowed mode.
        "modes": None,
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
    # provider null = OpenAI when its key is set, else the phone's own voice (filled in when read).
    "voice": {"provider": None, "model": None, "voice": "marin", "speed": 1.0},
    # about 1 places card in 5 may show one place unlike the user's usual, labelled.
    "profile": {"something_different": True},
    # natural (the talker, the default) or economy (today's cascade); the talker the user picks by
    # ear, and its voice per provider.
    "conversation": {"mode": "natural", "talker": "xai", "voices": {"xai": "eve", "gemini": "Aoede"}},
    # how long the raw conversation messages are kept (what Pisa learned from them stays either way).
    "messages_retention": retention.MESSAGES_DEFAULT,
    # their rules over Pisa's interruptions: "always for X", "never for Y" (who, what kind)
    "guardian": {"rules": []},
}

LANGUAGES = locale.WIRE_TAGS
CONVERSATION_MODES = ("natural", "economy")
TALKERS = ("xai", "gemini")
_VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRIGGERS = ("glasses_on", "app_open", "phrase", "time")
SECTIONS = ("agenda", "tasks", "email", "weather")
# which providers may serve each job, and whether the list may be empty (that job off).
PLACE_JOBS: dict[str, tuple[tuple[str, ...], bool]] = {  # job: (allowed providers, may be empty)
    "routes": (("apple", "google"), False),
    "search": (("apple", "google"), False),
    "details": (("google",), True),
    "web": (("tavily",), True),
}
MODES = places.MODES
# the user's names for travel modes (Settings → Places), and how each is routed. A ferry is transit.
TRAVEL_MODES = ("walk", "transit", "bike", "car", "taxi")
ROUTED: dict[str, str] = {
    "walk": "walk", "transit": "transit", "bike": "bike", "car": "drive", "taxi": "drive",
}  # fmt: skip
NAMED: dict[str, str] = {"walk": "walk", "transit": "transit", "bike": "bike", "drive": "car"}


def modes_from(default_mode: str) -> list[str]:
    """The allowed modes when the user never set them: their default first, then public transport."""
    first = NAMED.get(default_mode, "transit")
    return [first] if first == "transit" else [first, "transit"]


DEFAULT_DAY_START = "04:00"  # the briefing day starts here when briefing.window is null
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# The name as the server hears it; the recogniser's other spellings of it are the app's.
PISA_WORDS = frozenset({"pisa"})


def defaults(language: str) -> dict[str, Any]:
    """The defaults; `language`: Pisa's start language (PISA_LANGUAGE), theirs until they pick one."""
    out = copy.deepcopy(DEFAULTS)
    out["language"] = language
    return out


def _invalid(message: str) -> PisaError:
    return PisaError("validation_failed", message, "Fix the setting and try again")


# --- merge -------------------------------------------------------------------------------------------------


NULLABLE_OBJECTS = frozenset({"window", "hours"})  # objects that may also be null


def merge(current: dict[str, Any], patch: Any, path: str = "") -> dict[str, Any]:
    """Merge-PATCH: objects merge key by key; arrays, scalars and null replace. Unknown fields are refused."""
    if not isinstance(patch, dict):
        raise _invalid(f"{path or 'The settings'} must be a JSON object.")
    merged = copy.deepcopy(current)
    for key, value in patch.items():
        where = f"{path}.{key}" if path else key
        if key not in current:
            raise _invalid(f"There is no setting {where!r}.")
        existing = current[key]
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge(existing, value, where)
        elif isinstance(existing, dict) and not (value is None and key in NULLABLE_OBJECTS):
            raise _invalid(f"{where} must be a JSON object.")
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# --- validation --------------------------------------------------------------------------------------------


def parse_hhmm(value: Any, where: str) -> time:
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        raise _invalid(f"{where} must be a time as HH:MM, from 00:00 to 23:59.")
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _span(value: Any, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise _invalid(f"{where} must be null or an object with start and end (HH:MM).")
    if parse_hhmm(value["start"], f"{where}.start") == parse_hhmm(value["end"], f"{where}.end"):
        raise _invalid(f"{where}.start and {where}.end must differ.")


def _int(value: Any, where: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"{where} must be a whole number from {low} to {high}.")


def _bool(value: Any, where: str) -> None:
    if not isinstance(value, bool):
        raise _invalid(f"{where} must be true or false.")


def normalize_phrase(text: str) -> str:
    """Lowercase, no accents, no punctuation, single spaces; the name is the one word "pisa"."""
    words = fold_words(text).split()
    return " ".join("pisa" if w in PISA_WORDS else w for w in words)


def validate(settings: dict[str, Any]) -> None:
    """Every message names the field and what is allowed."""
    if settings["language"] not in LANGUAGES:
        raise _invalid("language must be it-IT or en-US.")
    briefing = settings["briefing"]
    for name in TRIGGERS:
        _bool(briefing["triggers"][name], f"briefing.triggers.{name}")
    phrases = briefing["phrases"]
    if not isinstance(phrases, list) or not 1 <= len(phrases) <= 10:
        raise _invalid("briefing.phrases must be a list of 1 to 10 phrases.")
    seen: set[str] = set()
    for i, phrase in enumerate(phrases):
        if not isinstance(phrase, str) or not 2 <= len(phrase.strip()) <= 60:
            raise _invalid(f"briefing.phrases[{i}] must be 2 to 60 characters long.")
        key = normalize_phrase(phrase)
        if not key:
            raise _invalid(f"briefing.phrases[{i}] must contain letters or digits.")
        if key in seen:
            raise _invalid(
                f"briefing.phrases[{i}] repeats another phrase (case, accents and punctuation don't count)."
            )
        seen.add(key)
    parse_hhmm(briefing["time"], "briefing.time")
    _span(briefing["window"], "briefing.window")
    if briefing["repeat_after_min"] is not None:
        _int(briefing["repeat_after_min"], "briefing.repeat_after_min", 5, 720)
    sections = briefing["sections"]
    if (
        not isinstance(sections, list)
        or not sections
        or any(s not in SECTIONS for s in sections)
        or len(set(sections)) != len(sections)
    ):
        raise _invalid(
            "briefing.sections must be a non-empty list of agenda, tasks, email and weather, "
            "each at most once."
        )
    _int(briefing["max_words"], "briefing.max_words", 20, 200)
    _int(settings["reminders"]["event_lead_min"], "reminders.event_lead_min", 0, 120)
    _int(settings["reminders"]["task_lead_min"], "reminders.task_lead_min", 0, 120)
    _span(settings["quiet"]["hours"], "quiet.hours")
    _places(settings["places"])
    _planner(settings["planner"])
    voice_rules.validate(settings["voice"])
    _bool(settings["profile"]["something_different"], "profile.something_different")
    _conversation(settings["conversation"])
    _guardian(settings["guardian"])
    if settings["messages_retention"] not in retention.MESSAGES_RETENTION:
        raise _invalid("messages_retention must be forever, 1y or 90d.")
    until = settings["quiet"]["until"]
    if until is not None:
        parse_until(until)


GUARDIAN_RULES_MAX = 50
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _guardian(guardian: dict[str, Any]) -> None:
    rules = guardian["rules"]
    if not isinstance(rules, list) or len(rules) > GUARDIAN_RULES_MAX:
        raise _invalid(f"guardian.rules must be a list of at most {GUARDIAN_RULES_MAX} rules.")
    for i, rule in enumerate(rules):
        where = f"guardian.rules[{i}]"
        if not isinstance(rule, dict) or not set(rule) <= {"mode", "kind", "person_id"}:
            raise _invalid(f"{where} must be an object with mode, and kind or person_id.")
        if rule.get("mode") not in ("always", "never"):
            raise _invalid(f"{where}.mode must be always or never.")
        kind, person = rule.get("kind"), rule.get("person_id")
        if kind is None and person is None:
            raise _invalid(f"{where} must name a kind or a person_id (or both).")
        if kind is not None and kind not in guardian_rules.RULE_KINDS:
            raise _invalid(f"{where}.kind must be one of {', '.join(guardian_rules.RULE_KINDS)}.")
        if person is not None and (not isinstance(person, str) or not _UUID.match(person)):
            raise _invalid(f"{where}.person_id must be the id of one of their people (a UUID, lower case).")


def _conversation(conversation: dict[str, Any]) -> None:
    if conversation["mode"] not in CONVERSATION_MODES:
        raise _invalid("conversation.mode must be natural or economy.")
    if conversation["talker"] not in TALKERS:
        raise _invalid("conversation.talker must be xai or gemini.")
    voices = conversation["voices"]
    if not isinstance(voices, dict) or set(voices) != set(TALKERS):
        raise _invalid("conversation.voices must have a voice for xai and one for gemini.")
    for talker, voice in voices.items():
        if not isinstance(voice, str) or not _VOICE_ID.match(voice):
            raise _invalid(f"conversation.voices.{talker} must be a voice id (letters, digits, - or _).")


def _places(places: dict[str, Any]) -> None:
    for job, (allowed, may_be_empty) in PLACE_JOBS.items():
        value = places[job]
        names = " and ".join(allowed)
        size = "a list" if may_be_empty else "a non-empty list"
        if (
            not isinstance(value, list)
            or (not value and not may_be_empty)
            or any(v not in allowed for v in value)
            or len(set(value)) != len(value)
        ):
            raise _invalid(f"places.{job} must be {size} of {names}, each at most once.")
    if places["default_mode"] not in MODES:
        raise _invalid("places.default_mode must be drive, transit, walk or bike.")
    modes = places["modes"]
    if (
        not isinstance(modes, list)
        or not modes
        or any(m not in TRAVEL_MODES for m in modes)
        or len(set(modes)) != len(modes)
    ):
        raise _invalid(
            "places.modes must be a non-empty list of walk, transit, bike, car and taxi, each at most once."
        )
    _int(places["walk_max_min"], "places.walk_max_min", 10, 180)
    cap = places["monthly_cap_eur"]
    if isinstance(cap, bool) or not isinstance(cap, int | float) or not 0 <= cap <= 100:
        raise _invalid("places.monthly_cap_eur must be a number from 0 to 100.")


def _planner(planner: dict[str, Any]) -> None:
    _span(planner["day"], "planner.day")  # never null: the merge refuses it
    _int(planner["buffer_min"], "planner.buffer_min", 0, 60)
    _int(planner["default_task_min"], "planner.default_task_min", 5, 240)
    _bool(planner["leave_now"], "planner.leave_now")
    _int(planner["leave_lead_min"], "planner.leave_lead_min", 0, 30)
    _bool(planner["check_ins"], "planner.check_ins")


def parse_until(value: Any) -> datetime:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise _invalid("quiet.until must be null or an RFC 3339 date-time with its offset.")
    return moment


def apply_patch(current: dict[str, Any], patch: Any) -> dict[str, Any]:
    merged = merge(current, patch)
    _sync_modes(merged, patch)
    voice_rules.normalize(current, patch, merged)
    validate(merged)
    phrases = merged["briefing"]["phrases"]
    merged["briefing"]["phrases"] = [p.strip() for p in phrases]
    merged["places"]["monthly_cap_eur"] = float(merged["places"]["monthly_cap_eur"])
    merged["voice"]["speed"] = float(merged["voice"]["speed"])
    if merged["quiet"]["until"] is not None:
        merged["quiet"]["until"] = parse_until(merged["quiet"]["until"]).isoformat()
    return merged


def with_defaults(stored: Any, language: str) -> dict[str, Any]:
    """Stored settings over the defaults, so a setting added later gets its default. `language`: Pisa's
    start language (PISA_LANGUAGE), theirs until they pick one."""
    settings = _overlay(defaults(language), stored) if isinstance(stored, dict) else defaults(language)
    places = settings["places"]
    if places.get("modes") is None:
        places["modes"] = modes_from(places["default_mode"])
    return settings


def _sync_modes(merged: dict[str, Any], patch: Any) -> None:
    """places.modes and the older places.default_mode say the same: the first allowed mode is the default.
    Whichever the patch sets wins."""
    places_patch = patch.get("places") if isinstance(patch, dict) else None
    if not isinstance(places_patch, dict):
        return
    places = merged["places"]
    if "modes" in places_patch and isinstance(places["modes"], list) and places["modes"]:
        first = places["modes"][0]
        places["default_mode"] = ROUTED.get(first, places["default_mode"])
    elif "default_mode" in places_patch and places["default_mode"] in NAMED:
        first = NAMED[places["default_mode"]]
        rest = [m for m in places.get("modes") or [] if m != first]
        places["modes"] = [first, *rest] if rest else modes_from(places["default_mode"])


def _overlay(base: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    for key, value in stored.items():
        if key in base:
            base[key] = (
                _overlay(base[key], value)
                if isinstance(base[key], dict) and isinstance(value, dict)
                else value
            )
    return base


# --- time rules --------------------------------------------------------------------------------------------


def in_span(moment: time, span: dict[str, str] | None) -> bool:
    """Inside [start, end); a span may cross midnight ("20:00" to "03:00"). null means always."""
    if span is None:
        return True
    start, end = parse_hhmm(span["start"], "start"), parse_hhmm(span["end"], "end")
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def day_start(settings: dict[str, Any]) -> time:
    window = settings["briefing"]["window"]
    return parse_hhmm(window["start"] if window else DEFAULT_DAY_START, "briefing.window.start")


def briefing_day(now: datetime, settings: dict[str, Any], timezone: str) -> date:
    """The briefing day starts at window.start (04:00 without a window); before it, it is yesterday's."""
    local = now.astimezone(ZoneInfo(timezone))
    if local.time() >= day_start(settings):
        return local.date()
    return local.date() - timedelta(days=1)


def briefing_day_start(day: date, settings: dict[str, Any], timezone: str) -> datetime:
    return datetime.combine(day, day_start(settings), tzinfo=ZoneInfo(timezone))


def briefing_time_on(day: date, settings: dict[str, Any], timezone: str) -> datetime:
    """When the time trigger fires during this briefing day: the first briefing.time at or after its start."""
    at = parse_hhmm(settings["briefing"]["time"], "briefing.time")
    first = datetime.combine(day, at, tzinfo=ZoneInfo(timezone))
    return first if first >= briefing_day_start(day, settings, timezone) else first + timedelta(days=1)


# RAIL: quiet hours are the user's setting; nothing unprompted speaks through them but the floors.
def is_quiet(now: datetime, settings: dict[str, Any], timezone: str) -> bool:
    quiet = settings["quiet"]
    if quiet["until"] is not None and now < parse_until(quiet["until"]):
        return True
    hours = quiet["hours"]
    return hours is not None and in_span(now.astimezone(ZoneInfo(timezone)).time(), hours)


def matches_phrase(utterance: str, phrases: list[str]) -> bool:
    """The utterance equals a phrase, or the phrase without its "Pisa" word. No model involved."""
    said = normalize_phrase(utterance)
    if not said:
        return False
    for phrase in phrases:
        key = normalize_phrase(phrase)
        without_pisa = " ".join(w for w in key.split() if w != "pisa")
        if said in (key, without_pisa):
            return True
    return False
