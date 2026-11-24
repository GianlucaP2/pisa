"""Fixes at the merge: each fails without its fix."""

from typing import Any

import psycopg

from pisa.connectors.google.text import html_to_text
from tests.assistant_env import add_event, add_task, rome, set_settings
from tests.test_done import BOBBYS
from tests.test_planner import place, with_place
from tests.test_rain_links_web_yes import READ_WEB, l3_turn

# --- M-A: one visit is one time, not only one place ---------------------------------------------------------


def test_two_events_at_one_place_hours_apart_are_not_one_visit(
    db: psycopg.Connection, secrets_key: str
) -> None:
    from tests.test_research_eval import research  # its clock: Tuesday 13:00 in Rome

    set_settings(db, {"language": "it-IT", "quiet": {"hours": None}})
    gym = place(db, "Palestra Vigore", BOBBYS)
    for hour in (13, 19):
        event = add_event(db, "Palestra", rome(2026, 10, 20, hour), minutes=60)
        with_place(db, "calendar_events", event, gym)
    said: list[Any] = [("mark_done", {"what": "Palestra"})]
    t = research(db, secrets_key, "sono stato in palestra", said, "Quale?", with_data=True)
    assert "Several match" in t.result("mark_done")
    assert db.execute("SELECT count(*) FROM calendar_events WHERE attended_at IS NOT NULL").fetchone() == (0,)


# --- M-B: an end tag the browser ignores never ends a hidden element ----------------------------------------


def test_ignored_end_tags_never_reveal_hidden_text() -> None:
    assert html_to_text('<body><div style="display:none">X</body>INJECT')[0] == ""
    assert html_to_text('<p>Hi<div style="display:none">X</p>INJECT</div>')[0] == "Hi"
    assert html_to_text("<span><div hidden>X</span>INJECT")[0] == ""
    # What L-2 wanted still holds: a block's end, a new paragraph or a table cell ends an unclosed one.
    assert html_to_text("<div><span hidden>X</div>visible")[0] == "visible"
    assert html_to_text("<p><span hidden>X<p>visible")[0] == "visible"
    assert html_to_text("<table><tr><td><div hidden>X</td><td>visible</td></tr></table>")[0] == "visible"


# --- M-C: complete_task waits for the user's yes after outside text, like mark_done ------------------------


def test_complete_task_after_outside_text_waits_for_yes(db: psycopg.Connection, secrets_key: str) -> None:
    add_task(db, "Pagare la bolletta")
    steps: list[Any] = [READ_WEB, ("complete_task", {"task_ref": "bolletta"})]
    t = l3_turn(db, secrets_key, "cerca il traghetto", steps)
    assert t.result("complete_task").startswith("Not done yet: this turn read text from outside")
    assert db.execute("SELECT status FROM tasks").fetchone() == ("open",)
    yes = l3_turn(db, secrets_key, "sì", [("confirm_remember", {})], device=t.device)
    assert "Marked done: «Pagare la bolletta», done (closed)." in yes.result("confirm_remember")
    assert db.execute("SELECT status FROM tasks").fetchone() == ("done",)


# --- LOW: a held priority change is Pisa's, even on a task whose priority the user had set
# --------------------


def test_a_held_priority_change_is_never_the_owners(db: psycopg.Connection, secrets_key: str) -> None:
    add_task(db, "Pagare la bolletta")
    db.execute("UPDATE tasks SET priority_source = 'owner'")
    lower: list[Any] = [READ_WEB, ("set_task_priority", {"task_ref": "bolletta", "priority": 3})]
    t = l3_turn(db, secrets_key, "cerca il traghetto", lower)
    l3_turn(db, secrets_key, "sì", [("confirm_remember", {})], device=t.device)
    assert db.execute("SELECT priority, priority_source FROM tasks").fetchone() == (3, "pisa")


def test_a_mail_the_reader_fails_on_keeps_what_was_read_and_says_so(monkeypatch: Any) -> None:
    from structlog.testing import capture_logs

    from pisa.domain import visible

    def broken(self: object, tag: str, attrs: object) -> None:
        if tag == "b":
            raise IndexError("bug")

    monkeypatch.setattr(visible._Text, "handle_starttag", broken)
    with capture_logs() as logs:
        text, _, _ = html_to_text("<p>Ciao</p> <b>dopo</b>")
    assert text == "Ciao"
    assert [(e["event"], e["cause"]) for e in logs] == [("mail_html_unparsed", "IndexError")]
