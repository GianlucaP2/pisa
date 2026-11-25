"""No user data in any log line, at any level (debug included).

Real turns run through the app with logging at DEBUG captured: a typed turn on `/v2/runs`, spoken turns on
`/v2/live` (the earbuds' microphone) that add a task, save a note and keep a reminder, each with an unusual
marker word. None of the markers (their words, the task's title, the note's text, the reminder, Pisa's reply)
may appear anywhere in what was logged: neither in the events as the call sites wrote them (before the
deny-list's backstop) nor in the JSON lines written out. Then the backstop alone: a denied field is redacted
and a traceback never carries an exception's message.
"""

import io
import json
import logging
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import pytest
import structlog

from pisa.logging import DENY, REDACTED, configure_logging, drop_user_text, get_logger, safe_exc_info
from tests.conftest import FakeModels, _drop_cached_loggers, pair, set_key  # pyright: ignore[reportPrivateUsage]

# unusual words, so a match anywhere is unambiguous
TASK_WORD = "zanzibarquokka"
NOTE_WORD = "quokkagatecode"
REMIND_WORD = "pellucidmarmot"
TYPED_WORD = "ocelotvermouth"
REPLY_WORD = "wombatpalindromo"
MARKERS = (TASK_WORD, NOTE_WORD, REMIND_WORD, TYPED_WORD, REPLY_WORD)

SAID_TASK = f"segnati di comprare lo {TASK_WORD}"
SAID_NOTE = f"annota: il codice del cancello è {NOTE_WORD}"
SAID_REMIND = f"ricordami domani alle 10 di chiamare il {REMIND_WORD}"
TYPED = f"che ne pensi dello {TYPED_WORD}?"

CALLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    SAID_TASK: [("add_task", {"title": f"Comprare lo {TASK_WORD}"})],
    SAID_NOTE: [("save_note", {"text": f"Codice del cancello: {NOTE_WORD}"})],
    SAID_REMIND: [
        ("keep_intention", {"what": f"chiamare il {REMIND_WORD}", "cue": "time", "when": "domani alle 10"})
    ],
}


def scripted() -> Any:
    """The conversation model: the tools for each of the user's words, then a reply holding a marker."""

    async def model(messages: Any, info: Any) -> Any:
        from pydantic_ai.messages import ToolReturnPart, UserPromptPart
        from pydantic_ai.models.function import DeltaToolCall

        at = max(i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts))
        words = [str(p.content) for p in messages[at].parts if isinstance(p, UserPromptPart)][-1]
        returned = any(isinstance(p, ToolReturnPart) for m in messages[at + 1 :] for p in m.parts)
        calls = next((c for said, c in CALLS.items() if said in words), None)
        if calls and not returned:
            yield {
                n: DeltaToolCall(name=name, json_args=json.dumps(args))
                for n, (name, args) in enumerate(calls)
            }
            return
        yield f"Fatto, {REPLY_WORD}."

    return model


@contextmanager
def debug_logs() -> Iterator[tuple[list[str], io.StringIO]]:
    """Logging as the server runs it, at DEBUG, into a buffer; plus every structlog event as its call site
    wrote it (before any processor), to check the call sites without the backstop."""
    out = io.StringIO()
    raw: list[str] = []
    configure_logging("debug", stream=out)

    def spy(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        raw.append(repr(dict(event_dict)))
        return event_dict

    processors = list(structlog.get_config()["processors"])
    structlog.configure(processors=[processors[0], spy, *processors[1:]])
    _drop_cached_loggers()
    try:
        yield raw, out
    finally:
        _drop_cached_loggers()  # the fixture `cli_keeps_test_logging` puts the test configuration back


def _leaks(text: str) -> list[str]:
    low = text.lower()
    return [m for m in MARKERS if m in low]


def test_turns_with_a_task_a_note_a_reminder_and_a_typed_question_log_none_of_their_words(  # type: ignore[no-untyped-def]
    client, db, fake_models: FakeModels, secrets_key
) -> None:
    from tests.live_app import live
    from tests.test_live_v2_socket import run

    set_key(db, secrets_key)
    fake_models.default = scripted()
    device_id, token = pair(db)
    with debug_logs() as (raw, out):
        with live(client, token, device_id) as app:
            for said in (SAID_TASK, SAID_NOTE, SAID_REMIND):
                frames = app.turn(said, source="earbuds_mic")
                assert all(f["type"] != "error" for f in frames), frames
        typed = client.post(
            "/v2/runs",
            headers={"Authorization": f"Bearer {token}"},
            json=run(TYPED, thread="thread-typed")["input"],
        )
        assert typed.status_code == 200, typed.text

    # the turns ran: the task, the note and the reminder were kept, and Pisa's reply said the marker
    assert (
        db.execute("SELECT count(*) FROM tasks WHERE title ILIKE %s", (f"%{TASK_WORD}%",)).fetchone()[0] >= 1
    )
    assert db.execute("SELECT count(*) FROM owner_notes WHERE text ILIKE %s", (f"%{NOTE_WORD}%",)).fetchone()[
        0
    ]
    assert db.execute(
        "SELECT count(*) FROM intentions WHERE what ILIKE %s", (f"%{REMIND_WORD}%",)
    ).fetchone()[0]
    assert REPLY_WORD in typed.text
    said = db.execute("SELECT string_agg(content, ' ') FROM messages WHERE role = 'user'").fetchone()[0]
    assert all(w in said for w in (TASK_WORD, NOTE_WORD, REMIND_WORD, TYPED_WORD))

    # the logs saw the turns, at DEBUG too
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
    events = {line["event"] for line in lines}
    assert {"utterance", "reply", "turn_trace", "task_created", "note_saved", "intention_kept"} <= events
    assert any(line["level"] == "debug" for line in lines)
    assert len(raw) == len([line for line in lines if "logger" not in line])  # every structlog event spied

    # none of their words, titles, notes or Pisa's reply: at the call sites, and in what was written
    assert _leaks("\n".join(raw)) == []
    assert _leaks(out.getvalue()) == []
    # what is logged of their words: counts
    utterances = [line for line in lines if line["event"] == "utterance"]
    assert {u["chars"] for u in utterances} >= {len(SAID_TASK), len(SAID_NOTE), len(SAID_REMIND), len(TYPED)}
    assert all("text" not in u for u in utterances)


MAPS_WORD = "quokkapharmacist"  # the phone's maps path (contract 2.3): the query, the place, its street
# synthetic coordinates, built here (at most 3 decimals in a literal): none of their digits may reach a log
MAPS_POINT = (45 + 0.123456, 9 + 0.654321)


def test_a_turn_that_asks_the_phone_s_maps_logs_no_query_place_or_coordinate(  # type: ignore[no-untyped-def]
    client, db, fake_models: FakeModels, secrets_key
) -> None:
    """The phone's own maps answer a place search in the turn's run (`pisa.maps.search`, `pisa.maps.result`):
    its query, the place's name, address and phone and every coordinate stay out of every log line, the
    request's and the answer's, at DEBUG too; the answer reaches the model as a data block."""
    from tests.conftest import pair_v2
    from tests.test_app_context import scripted as calls
    from tests.test_live_v2_socket import connect, hello, opened, run

    device_id, token = pair_v2(db)
    set_key(db, secrets_key)
    db.execute(  # where the phone was: the search is biased to it, and never logs it
        "INSERT INTO device_locations (device_id, lat, lon, accuracy_m, measured_at) "
        "VALUES (%s, %s, %s, 10, now())",
        (device_id, *MAPS_POINT),
    )
    seen: list[str] = []
    fake_models.default = calls(
        "map_search", {"query": f"ferramenta {MAPS_WORD}"}, seen, f"Ecco, {REPLY_WORD}."
    )
    place = {
        "id": "I5E0A1B2C3D4F5061", "name": f"Ferramenta {MAPS_WORD.title()}",
        "address": f"Via {MAPS_WORD} 1, Testville",
        "lat": 45 + 0.120987, "lon": 9 + 0.650123, "phone": "+15550100123", "distanceMeters": 420,
    }  # fmt: skip
    with debug_logs() as (raw, out), connect(client, token) as ws:
        app = opened(ws, hello(device_id, contract={"versions": ["2.2", "2.3"]},
                               extensions={"pisa.notice": 1, "pisa.maps": 1}))  # fmt: skip
        assert app.got[0]["extensions"].get("pisa.maps") == 1  # granted
        frame = run("trovami una ferramenta")
        app.send(frame)
        got = app.until(
            lambda f: (
                f.get("event", {}).get("name") == "pisa.maps.search"
                or f.get("event", {}).get("type") in ("RUN_FINISHED", "RUN_ERROR")
            )
        )
        asked = got[-1]
        assert asked["event"].get("name") == "pisa.maps.search", got
        value = asked["event"]["value"]
        assert asked["runId"] == frame["input"]["runId"] and value["query"] == f"ferramenta {MAPS_WORD}"
        assert value["near"] == {"lat": round(MAPS_POINT[0], 5), "lon": round(MAPS_POINT[1], 5)}
        result = {"requestId": value["requestId"], "ok": True, "places": [place]}
        app.send({"op": "custom", "name": "pisa.maps.result", "value": result})
        app.run_ends(frame["input"]["runId"])
        app.check()
    # the phone's place answered, inside the map results' data block
    assert place["name"] in seen[-1] and "the phone's map" in seen[-1]
    assert db.execute("SELECT coords_source FROM places WHERE name = %s", (place["name"],)).fetchone() == (
        "phone",
    )
    everything = " ".join(raw) + out.getvalue()
    for private in (MAPS_WORD, "45.12", "9.65", "123456", "654321", "120987", "650123", "+1555"):
        assert private not in everything.lower(), private
    events = {json.loads(line)["event"] for line in out.getvalue().splitlines() if line.startswith("{")}
    assert "phone_maps_result" in events  # the answer was logged: by kind and count only


NOTIFY_WORD = "capibaranotturno"  # the phone's own notifications (contract 2.3): a reminder's words
NOTIFY_POINT = (45 + 0.135791, 9 + 0.246802)  # synthetic, built here: a place the phone watches


def test_the_phone_s_schedule_and_its_ack_log_no_title_time_or_coordinate(  # type: ignore[no-untyped-def]
    client, db, monkeypatch, clock
) -> None:
    """The phone's own notifications (`pisa.notify`): on a connection the schedule and the regions go after
    the welcome, the phone acks them; neither the reminder's words (on the lock screen here: details on), nor
    its time, nor the watched place's coordinates reach any log line, at DEBUG too."""
    from datetime import UTC, datetime, timedelta  # the suite's clock: the server's now
    from zoneinfo import ZoneInfo

    from pisa.domain import phone_notify as notify_rules
    from tests.conftest import pair_v2
    from tests.test_live_v2_socket import connect, hello, opened

    device_id, token = pair_v2(db)
    monkeypatch.setattr(client.app.state.runtime.settings, "phone_notify_details", True)
    rome = ZoneInfo("Europe/Rome")
    due = (clock.now.astimezone(rome) + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    reminder, waiting = uuid.uuid4(), uuid.uuid4()
    where = uuid.uuid4()
    db.execute(
        "INSERT INTO places (id, name, lat, lon, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, now(), now())",
        (where, f"Bottega {NOTIFY_WORD}", *NOTIFY_POINT),
    )
    for ident, condition, at, place in ((reminder, "time", due, None), (waiting, "arrive", None, where)):
        db.execute(
            "INSERT INTO pisa_promises (id, what, condition, due_at, place_id, lang, provenance, "
            "created_at) VALUES (%s, %s, %s, %s, %s, 'it-IT', 'owner_words', now())",
            (ident, f"chiamare il {NOTIFY_WORD}", condition, at, place),
        )
    item, region = notify_rules.reminder_id(reminder), notify_rules.region_id(waiting)
    with debug_logs() as (raw, out), connect(client, token) as ws:
        app = opened(ws, hello(device_id, contract={"versions": ["2.2", "2.3"]},
                               extensions={"pisa.notice": 1, "pisa.notify": 1}))  # fmt: skip
        welcome = app.got[0]
        assert welcome["extensions"].get("pisa.notify") == 1 and "notify.local" in {
            f["id"] for f in welcome["features"]
        }
        got = app.until(lambda f: f.get("event", {}).get("name") == "pisa.notify.regions")
        sent = {
            f["event"]["name"]: f["event"]["value"] for f in got if f.get("event", {}).get("type") == "CUSTOM"
        }
        (scheduled,) = sent["pisa.notify.schedule"]["items"]
        assert scheduled["id"] == item and scheduled["title"] == "Promemoria"
        assert NOTIFY_WORD in scheduled["body"]  # details on: their own words, on their own phone
        assert [r["id"] for r in sent["pisa.notify.regions"]["regions"]] == [region]
        for name, ids in (("pisa.notify.schedule", [item]), ("pisa.notify.regions", [region])):
            ack = {"revision": sent[name]["revision"], "scheduled": ids, "failed": []}
            app.send({"op": "custom", "name": "pisa.notify.ack", "value": ack})
        app.send({"op": "ping"})
        app.until(lambda f: f.get("control") == "pong")  # the acks before it were taken
        app.check()
    states = db.execute("SELECT id, state FROM device_notify_items ORDER BY id").fetchall()
    assert sorted(states) == sorted([(item, "scheduled"), (region, "scheduled")])
    everything = " ".join(raw) + out.getvalue()
    fire = datetime.fromisoformat(scheduled["fireAt"]).astimezone(UTC)
    for private in (NOTIFY_WORD, scheduled["fireAt"], fire.isoformat(), "45.135", "9.246", "135791", "246802",
                    f"{NOTIFY_POINT[0]:.4f}", f"{NOTIFY_POINT[1]:.4f}"):  # fmt: skip
        assert private.lower() not in everything.lower(), private
    events = {json.loads(line)["event"] for line in out.getvalue().splitlines() if line.startswith("{")}
    assert {"notify_sent", "notify_acked"} <= events  # logged: by device, revision and counts only


def test_the_backstop_redacts_denied_fields_at_every_level_and_keeps_counts_and_flags() -> None:
    event = {
        "event": "x",
        "text": f"comprare lo {TASK_WORD}",
        "title": ["a", TASK_WORD],
        "said": 12,
        "words": True,
        "lat": 45.464,
        "query": None,
        "trace_id": "t-1",
        "nested": {"body": NOTE_WORD, "chars": 3, "parts": [{"prompt": REPLY_WORD}]},
    }
    got = drop_user_text(None, "debug", dict(event))
    assert got["text"] == got["title"] == got["lat"] == REDACTED
    assert (got["said"], got["words"], got["query"], got["trace_id"]) == (12, True, None, "t-1")
    assert got["nested"] == {"body": REDACTED, "chars": 3, "parts": [{"prompt": REDACTED}]}
    assert {"text", "words", "utterance", "reply", "said", "title", "query", "content", "body", "prompt",
            "transcript", "message_text"} <= DENY  # fmt: skip


def test_a_logged_exception_keeps_its_frames_and_class_never_its_message() -> None:
    def fails() -> None:
        try:
            raise KeyError(f"no task called {TASK_WORD}")
        except KeyError as exc:
            raise ValueError(f"the note {NOTE_WORD} is gone") from exc

    out = io.StringIO()
    configure_logging("debug", stream=out)
    _drop_cached_loggers()
    log = get_logger("pisa.test")
    try:
        fails()
    except ValueError:
        log.exception("failed_here", task_id=str(uuid.uuid4()))
    try:
        fails()
    except ValueError as exc:
        log.debug("failed_trace", exc_info=exc)
        logging.getLogger("uvicorn.error").exception("library failure")  # a library's record, the same way
    logging.getLogger("openai._base_client").debug("Request options: %s", {"content": REPLY_WORD})
    _drop_cached_loggers()
    written = out.getvalue()
    assert _leaks(written) == []
    lines = [json.loads(line) for line in written.splitlines()]
    assert [line["event"] for line in lines] == ["failed_here", "failed_trace", "library failure"]
    for line in lines:
        assert "KeyError" in line["exception"] and "ValueError" in line["exception"]
        assert "in fails" in line["exception"]  # the frames are kept
    assert safe_exc_info(None, "info", {"event": "y"}) == {"event": "y"}


def test_a_provider_error_that_echoes_the_user_s_words_logs_status_kind_and_request_id_only() -> None:
    """A provider's error body can quote what it was sent (a search's query, a place, a name): the failure
    line keeps the provider, the operation, the status, the kind and the request id, never those words."""
    from pisa import failures

    echoed = f'{{"error": "No results for \\"{MARKERS[0]}\\"", "request_id": "req_abcdef123456"}}'
    with debug_logs() as (raw, out):
        failures.external_failure("tavily", "POST /search", 400, echoed, "tvly-key")
        failures.external_failure("google_maps", "POST /v1/places:searchText", 404, ValueError(MARKERS[0]))
    written = out.getvalue()
    assert not _leaks(" ".join(raw)) and not _leaks(written)
    lines = [json.loads(line) for line in written.splitlines() if '"external_failure"' in line]
    assert [(e["provider"], e["status"], e["kind"]) for e in lines] == [
        ("tavily", 400, "response"),
        ("google_maps", 404, "ValueError"),
    ]
    assert lines[0]["request_id"] == "req_abcdef123456" and lines[1]["request_id"] is None
    assert all("reason" not in e for e in lines)


def test_dbos_s_own_handler_is_replaced_and_its_lines_lose_the_words_they_quote() -> None:
    """E1 LOG-2: DBOS gives its logger a StreamHandler of its own (propagate off) and writes an exception into
    its message; adopted, the line goes through Pisa's handler and keeps only what happened."""
    from pisa.logging import adopt

    lib = logging.getLogger("dbos")
    own = io.StringIO()
    with debug_logs() as (_raw, out):
        lib.handlers[:] = [logging.StreamHandler(own)]
        lib.propagate = False
        adopt("dbos")
        try:
            raise RuntimeError(f"No open task matches '{MARKERS[0]}'")
        except RuntimeError as e:
            lib.error(f"Exception encountered in asynchronous workflow: {e}", exc_info=e)
    written = out.getvalue()
    assert own.getvalue() == "" and not _leaks(written)
    (line,) = [json.loads(x) for x in written.splitlines() if "asynchronous workflow" in x]
    assert line["event"] == "Exception encountered in asynchronous workflow" and "RuntimeError" in str(line)


# --- a worker step that fails (E1 LOG-1): its exception's words, an email and a phone never logged ---------

EMAIL = "mario.rossi@example.com"
PHONE = "+39 333 123 4567"
# made-up values, built at run time so the source holds no lookalike of a real coordinate or landline (the
# export's scanners: test_no_precise_coordinates, test_no_personal_data); the redaction still sees them whole
LAT, LON = ".".join(("45", "4642")), ".".join(("9", "1900"))
PAIR = f"{LAT}, {LON}"
LAT5 = ".".join(("45", "46421"))
LANDLINE = "0" + "6 1234 5678"
FAILED_WITH = f"no contact {TASK_WORD} at {EMAIL}, phone {PHONE}, near {PAIR}"


def _private(text: str) -> list[str]:
    """What of the failure's words, the email, the phone or the place a log holds."""
    found = _leaks(text)
    for bit in (EMAIL, PHONE, "333 123 4567", LAT, LON, "rossi"):
        if bit in text:
            found.append(bit)
    return found


def test_a_worker_step_that_fails_logs_its_name_and_frames_never_its_words(client) -> None:  # type: ignore[no-untyped-def]
    """`maintenance.run_job` as the worker runs a minute's step (`job_step` → `WorkerHost.job` →
    `run_step` → `run_job`): the step raises with the user's words, an email and a phone in its message."""
    from pisa.services import maintenance

    runtime = client.app.state.runtime

    async def job() -> None:
        raise RuntimeError(FAILED_WITH)

    with debug_logs() as (raw, out):
        outcome = client.portal.call(lambda: maintenance.run_job(runtime, "test_failing_step", job))
    assert outcome["outcome"] == "failed"
    written = out.getvalue()
    assert _private(" ".join(raw)) == [] and _private(written) == []
    (line,) = [json.loads(x) for x in written.splitlines() if '"minute_step_failed"' in x]
    assert line["step"] == "test_failing_step" and "RuntimeError" in line["exception"]
    assert "in job" in line["exception"]  # the frames are kept


def test_a_worker_host_whose_runtime_fails_logs_none_of_the_failure_s_words() -> None:
    """`WorkerHost.job` (the DBOS step `job_step`'s body): the runtime's start raises with the words."""
    from pisa.workers.host import WorkerHost

    async def start(_settings: Any) -> Any:
        raise ValueError(FAILED_WITH)

    host = WorkerHost(None, start=start, name="test-failing")
    with debug_logs() as (raw, out):
        try:
            result = host.job("reminders")
        finally:
            host.close()
    assert result == {"job": "reminders", "outcome": "failed"}
    written = out.getvalue()
    assert _private(" ".join(raw)) == [] and _private(written) == []
    (line,) = [json.loads(x) for x in written.splitlines() if '"worker_job_not_run"' in x]
    assert "ValueError" in line["exception"]


def test_dbos_s_failure_line_through_pisa_s_handler_holds_no_email_phone_or_words(capfd) -> None:  # type: ignore[no-untyped-def]
    """DBOS's own failure line for a step (an f-string of the exception, with or without `: `), as DBOS
    writes it once Pisa set its logger up: Pisa's handler, its words cut, an email and a phone redacted."""
    from pisa.logging import adopt

    lib = logging.getLogger("dbos")
    with debug_logs() as (_raw, out):
        adopt("dbos")
        try:
            raise RuntimeError(FAILED_WITH)
        except RuntimeError as e:
            lib.error(f"Exception encountered in workflow step job_step: {e}", exc_info=e)
        lib.warning(f"Step job_step failed for {EMAIL} and {PHONE} at {PAIR}")  # no ": " to cut at
    written = out.getvalue()
    assert _private(written) == [] and _private(capfd.readouterr().err) == []
    lines = [json.loads(x) for x in written.splitlines() if '"dbos"' in x]
    assert [line["event"] for line in lines] == [
        "Exception encountered in workflow step job_step",
        "Step job_step failed for {email} and {phone} at {coords}",
    ]


def test_dbos_never_installs_its_own_handler_once_pisa_has_adopted_its_logger(capfd) -> None:  # type: ignore[no-untyped-def]
    """`configure_logging` adopts the dbos logger before DBOS starts: DBOS's `init_logger` installs its own
    StreamHandler (propagate off) only on a logger with no handler, so the `NullHandler` adopt leaves keeps it
    out, and a recovered workflow failing before `workers.main` adopts again still goes through Pisa's."""
    from dbos._logger import dbos_logger, init_logger

    from pisa.logging import adopt

    with debug_logs() as (_raw, out):
        adopt("dbos")  # as configure_logging does
        init_logger()  # DBOS(config)
        assert dbos_logger.propagate is True
        assert all(type(h) is logging.NullHandler for h in dbos_logger.handlers)
        try:
            raise RuntimeError(FAILED_WITH)
        except RuntimeError as e:
            dbos_logger.error(f"Exception encountered in asynchronous workflow: {e}", exc_info=e)
    assert _private(out.getvalue()) == [] and _private(capfd.readouterr().err) == []
    assert "asynchronous workflow" in out.getvalue()


@pytest.mark.parametrize(
    "value",
    [
        "0fa31234-0123-4567-8901-012345678901",
        "01234567-1234-4567-8901-123456789012",
        "req_0331234567",
        "trace 3f2a9c0b1d4e4f6a8b7c6d5e4f3a2b1c",
        "anthropic:claude-opus-5-5",
        "openai/gpt-4o-2024-08-06",
        "/v1/devices/0331234567/keys",
        "/home/user/j/core/pisa/services/talk.py",
        "2026-11-24T11:42:22.123456Z",
        "2026-11-24 11:42:22+01:00",
        "took 1.234s",
        "seconds=0.123456",
        "3 of 4 rows, 1200 ms",
        "1732451234567",
        "version 3.13.0",
    ],
)
def test_ids_refs_paths_times_and_counts_pass_the_value_backstop_unchanged(value: str) -> None:
    from pisa.logging import redact_values

    assert redact_values(value) == value
    got = drop_user_text(None, "info", {"event": value, "request_id": value, "nested": [{"path": value}]})
    assert got == {"event": value, "request_id": value, "nested": [{"path": value}]}


@pytest.mark.parametrize(
    ("value", "written"),
    [
        (f"sent to {EMAIL}", "sent to {email}"),
        ("call +39 333 123 4567 now", "call {phone} now"),
        ("call 333-123-4567", "call {phone}"),
        (f"landline {LANDLINE}", "landline {phone}"),
        ("0039 333 1234567", "{phone}"),
        (f"at {PAIR}", "at {coords}"),
        (f"lat={LAT5} lon=9.19", "lat={coords} lon={coords}"),
        (f"near {LAT5}", "near {coords}"),
    ],
)
def test_the_value_backstop_redacts_emails_phones_and_coordinates_in_any_field(
    value: str, written: str
) -> None:
    got = drop_user_text(None, "info", {"event": value, "reason": value, "nested": {"parts": [value]}})
    assert got == {"event": written, "reason": written, "nested": {"parts": [written]}}
